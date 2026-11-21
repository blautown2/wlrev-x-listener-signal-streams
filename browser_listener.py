"""Run an isolated, browser-backed X listener."""
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from listener import assess, initialize_drafts

HOME = "https://x.com/home"
PROFILE_LINK = '[data-testid="AppTabBar_Profile_Link"]'
POSTS = 'article[data-testid="tweet"]'
EXTRACT = r"""articles => articles.filter(article =>
    article.checkVisibility() && !article.querySelector('[data-testid="socialContext"]')
).flatMap(article => {
    const own = element => element.closest('article') === article &&
        !element.closest('[data-testid="quoteTweet"], [role="link"]');
    const clock = [...article.querySelectorAll('time')].find(own);
    const text = [...article.querySelectorAll('[data-testid="tweetText"]')].find(own);
    const link = clock?.closest('a')?.getAttribute('href');
    const match = link?.match(/^\/([a-z0-9_]+)\/status\/(\d+)(?:\?|$)/i);
    if (!match || !text || !clock.dateTime) return [];
    return [{id: match[2], author_id: match[1].toLowerCase(),
             text: text.innerText, created_at: clock.dateTime}];
})"""


class Deployment:
    def __init__(self, config_path):
        self.config = json.loads(config_path.read_text())
        self.stream = self.config["stream_id"]
        self.handle = self.config.get("x_handle", "").removeprefix("@").lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", self.stream):
            raise ValueError("stream_id must contain lowercase letters, numbers, or hyphens")
        if self.handle and not re.fullmatch(r"[a-z0-9_]{1,15}", self.handle):
            raise ValueError("Enter a valid X handle")
        self.directory = config_path.resolve().parent / ".listener-data" / self.stream
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.patterns = [
            (rule, re.compile(rule["match"], re.IGNORECASE)) for rule in self.config["rules"]
        ]

    @contextmanager
    def database(self):
        with closing(sqlite3.connect(self.directory / "listener.sqlite", timeout=30)) as db:
            db.row_factory = sqlite3.Row
            initialize_drafts(db)
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS connection (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    handle TEXT NOT NULL, connected_at TEXT NOT NULL
                )""")
                db.execute("""CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY, author_id TEXT NOT NULL,
                    text TEXT NOT NULL, created_at TEXT NOT NULL
                )""")
            yield db

    def connection(self):
        with self.database() as db:
            row = db.execute("SELECT * FROM connection WHERE singleton = 1").fetchone()
            return dict(row) if row else None

    @contextmanager
    def browser(self, *, headless=False):
        from playwright.sync_api import sync_playwright

        profile = self.directory / "chromium"
        profile.mkdir(exist_ok=True, mode=0o700)
        launch = {"headless": headless, "viewport": {"width": 1280, "height": 900}, "locale": "en-US"}
        if executable := os.environ.get("LISTENER_CHROMIUM_PATH"):
            launch["executable_path"] = executable
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(str(profile), **launch)
            try:
                yield context.pages[0] if context.pages else context.new_page()
            finally:
                context.close()

    def connect(self):
        if not self.handle:
            raise ValueError("Enter the expected X handle first")
        with self.browser() as page:
            page.goto(HOME, wait_until="domcontentloaded")
            # Leave the operator time to enter credentials and finish verification on X.
            page.locator(PROFILE_LINK).wait_for(timeout=300_000)
            self.verify_account(page)
            with self.database() as db, db:
                db.execute("""INSERT INTO connection VALUES (1, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET connected_at = excluded.connected_at""",
                    (self.handle, datetime.now(timezone.utc).isoformat()))

    def verify_account(self, page):
        href = page.locator(PROFILE_LINK).get_attribute("href", timeout=12_000)
        if not href or href.strip("/").lower() != self.handle:
            raise RuntimeError(f"The browser must be signed in to @{self.handle}")

    def require_connection(self):
        state = self.connection()
        if not state or state["handle"] != self.handle:
            raise RuntimeError("Connect the expected X account in the setup panel first")

    def scan(self):
        self.require_connection()
        with self.browser(headless=self.config.get("headless_scan", False)) as page:
            page.goto(HOME, wait_until="domcontentloaded")
            self.verify_account(page)
            posts = capture_feed(page, self.config.get("scroll_steps", 5))
        drafts = self.record(posts)
        return {"stream_id": self.stream, "posts_seen": len(posts), "drafts": drafts}

    def record(self, posts):
        now = datetime.now(timezone.utc)
        earliest = now - timedelta(minutes=max(rule["window_minutes"] for rule, _ in self.patterns))
        drafts = []
        with self.database() as db, db:
            db.execute("BEGIN IMMEDIATE")
            for post in {post["id"]: post for post in posts}.values():
                created = datetime.fromisoformat(post["created_at"]).astimezone(timezone.utc)
                if earliest <= created <= now and post["author_id"] != self.handle:
                    db.execute("INSERT OR IGNORE INTO observations VALUES (?, ?, ?, ?)",
                               (post["id"], post["author_id"], post["text"], created.isoformat()))
            db.execute("DELETE FROM observations WHERE created_at < ?", (earliest.isoformat(),))
            for rule, pattern in self.patterns:
                since = (now - timedelta(minutes=rule["window_minutes"])).isoformat()
                matching = [post for post in db.execute(
                    "SELECT * FROM observations WHERE created_at >= ?", (since,)
                ) if pattern.search(post["text"])]
                if draft := assess(db, rule, matching, now, self.handle):
                    drafts.append({"id": draft[0], "text": draft[1]})
        return drafts

    def drafts(self):
        with self.database() as db:
            return [dict(row) for row in db.execute("SELECT * FROM drafts ORDER BY id DESC LIMIT 25")]

    def compose(self, draft_id):
        self.require_connection()
        with self.database() as db:
            draft = db.execute("SELECT message FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if draft is None:
            raise ValueError("Draft was not found")
        with self.browser() as page:
            page.goto(HOME, wait_until="domcontentloaded")
            self.verify_account(page)
            page.locator('[data-testid="SideNav_NewTweet_Button"]').click(timeout=12_000)
            editor = page.locator('[data-testid="tweetTextarea_0"]')
            editor.fill(draft["message"], timeout=12_000)
            # Review and any publication happen in X. Closing the tab ends this action.
            page.wait_for_event("close", timeout=0)


def capture_feed(page, steps):
    for_you = page.get_by_role("tab", name="For you", exact=True)
    for_you.click(timeout=12_000)
    for_you.and_(page.locator('[aria-selected="true"]')).wait_for(timeout=12_000)
    page.locator(POSTS).first.wait_for(timeout=12_000)
    seen = {}
    for index in range(steps + 1):
        for post in page.locator(POSTS).evaluate_all(EXTRACT):
            seen[post["id"]] = post
        if index < steps:
            page.mouse.wheel(0, 780)
            page.wait_for_timeout(850)
    return list(seen.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("action", choices=["setup", "connect", "scan", "watch"])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    deployment = Deployment(args.config)
    if args.action == "setup":
        from setup_panel import serve
        serve(args.config.resolve(), args.port)
    elif args.action == "connect":
        deployment.connect()
    else:
        interval = deployment.config.get("poll_seconds", 300)
        if interval < 1:
            parser.error("poll_seconds must be positive")
        while True:
            print(json.dumps(deployment.scan()), flush=True)
            if args.action != "watch":
                break
            time.sleep(interval)


if __name__ == "__main__":
    main()
