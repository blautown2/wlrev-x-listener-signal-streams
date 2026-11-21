"""Poll X for recurring topics and turn them into source-linked posts."""
import argparse
import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API = "https://api.x.com/2"


def encode(value):
    return quote(str(value), safe="~")


class X:
    def __init__(self, prefix):
        self.consumer_key, self.consumer_secret, self.token, self.token_secret = (
            os.environ[prefix + "_" + suffix] for suffix in
            ("API_KEY", "API_SECRET", "ACCESS_TOKEN", "ACCESS_SECRET")
        )

    def request(self, path, params=None, body=None):
        method = "POST" if body is not None else "GET"
        url = API + path
        params = params or {}
        oauth = {
            "oauth_consumer_key": self.consumer_key,
            "oauth_token": self.token,
            "oauth_nonce": secrets.token_hex(16),
            "oauth_timestamp": str(int(time.time())),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_version": "1.0",
        }
        pairs = sorted((encode(k), encode(v)) for k, v in (params | oauth).items())
        normalized = "&".join(f"{key}={value}" for key, value in pairs)
        message = "&".join(map(encode, (method, url, normalized)))
        key = f"{encode(self.consumer_secret)}&{encode(self.token_secret)}"
        digest = hmac.digest(key.encode(), message.encode(), hashlib.sha1)
        oauth["oauth_signature"] = base64.b64encode(digest).decode()
        authorization = "OAuth " + ", ".join(f'{k}="{encode(v)}"' for k, v in oauth.items())
        request = Request(
            url + ("?" + urlencode(params) if params else ""),
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": authorization, "Content-Type": "application/json"},
            method=method,
        )
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    def search(self, query, since):
        params = {
            "query": f"({query}) -is:retweet",
            "tweet.fields": "author_id,created_at",
            "max_results": 100,
            "start_time": since.isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        while True:
            page = self.request("/tweets/search/recent", params)
            if page.get("errors"):
                raise RuntimeError("X returned incomplete search results")
            yield from page.get("data", [])
            token = page.get("meta", {}).get("next_token")
            if not token:
                return
            params["next_token"] = token


def assess(db, rule, posts, now, own_id):
    previous = db.execute(
        "SELECT max(created_at) FROM drafts WHERE rule = ?", (rule["id"],)
    ).fetchone()[0]
    if previous and now - datetime.fromisoformat(previous) < timedelta(hours=rule["cooldown_hours"]):
        return None

    authors, copies, evidence = set(), set(), []
    for post in sorted(posts, key=lambda post: post["created_at"]):
        created = datetime.fromisoformat(post["created_at"])
        if post["author_id"] == own_id or (previous and created <= datetime.fromisoformat(previous)):
            continue
        normalized = re.sub(r"\W+", "", re.sub(r"https?://\S+", "", post["text"].casefold()))
        if not normalized or normalized in copies or post["author_id"] in authors:
            continue
        copies.add(normalized)
        authors.add(post["author_id"])
        evidence.append(f'https://x.com/i/web/status/{post["id"]}')

    if len(authors) < rule["min_authors"]:
        return None
    message = rule["template"].format(count=len(authors), source=evidence[0])
    if len(message) > 280:
        raise ValueError(f"Post exceeds 280 characters for rule {rule['id']}")
    cursor = db.execute(
        "INSERT INTO drafts(rule, created_at, message, evidence, status) VALUES (?, ?, ?, ?, 'draft')",
        (rule["id"], now.isoformat(), message, json.dumps(evidence)),
    )
    return cursor.lastrowid, message


def scan(db, config, client, fixture):
    now = datetime.now(timezone.utc)
    own_id = config["account_id"]
    if client and client.request("/users/me")["data"]["id"] != own_id:
        raise RuntimeError("X credentials belong to a different account")
    for rule in config["rules"]:
        since = now - timedelta(minutes=rule["window_minutes"])
        posts = (
            list(client.search(rule["query"], since)) if client else
            [dict(post, created_at=(now - timedelta(minutes=post["minutes_ago"])).isoformat())
             for post in fixture.get(rule["id"], []) if 0 <= post["minutes_ago"] <= rule["window_minutes"]]
        )
        with db:
            db.execute("BEGIN IMMEDIATE")
            draft = assess(db, rule, posts, now, own_id)
        if draft:
            print(json.dumps({"draft_id": draft[0], "text": draft[1]}), flush=True)

    if config.get("publish") and client:
        publish(db, client, config.get("min_hours_between_posts", 6))


def publish(db, client, min_hours):
    with db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM drafts WHERE status = 'sending'").fetchone():
            raise RuntimeError("Check the account timeline and reconcile the sending draft before resuming")
        now = datetime.now(timezone.utc)
        last = db.execute("SELECT max(posted_at) FROM drafts WHERE status = 'posted'").fetchone()[0]
        if last and now - datetime.fromisoformat(last) < timedelta(hours=min_hours):
            return
        row = db.execute(
            "SELECT id, message FROM drafts WHERE status = 'draft' AND created_at >= ? ORDER BY id DESC LIMIT 1",
            ((now - timedelta(hours=24)).isoformat(),),
        ).fetchone()
        if row is None:
            return
        draft_id, message = row
        db.execute("UPDATE drafts SET status = 'sending' WHERE id = ?", (draft_id,))

    posted = client.request("/tweets", body={"text": message})["data"]["id"]
    with db:
        db.execute(
            "UPDATE drafts SET status = 'posted', posted_at = ?, post_id = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), posted, draft_id),
        )
    print(f"Published https://x.com/i/web/status/{posted}", flush=True)



def initialize_drafts(db):
    db.execute("""CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY, rule TEXT, created_at TEXT, message TEXT, evidence TEXT,
            status TEXT, posted_at TEXT, post_id TEXT
        )""")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--resolve", nargs=2, metavar=("DRAFT_ID", "POST_ID_OR_DISCARD"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    fixture = json.loads(args.fixture.read_text()) if args.fixture else None
    state = args.config.parent / (args.config.stem + (".demo.sqlite" if args.fixture else ".sqlite"))
    with closing(sqlite3.connect(state, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        initialize_drafts(db)
        if args.list:
            for row in db.execute("SELECT * FROM drafts ORDER BY id DESC"):
                print(json.dumps(dict(row) | {"evidence": json.loads(row["evidence"])}))
            return
        if args.resolve:
            draft_id, resolution = args.resolve
            if resolution != "discard" and not resolution.isdecimal():
                parser.error("Use a numeric X post ID or discard")
            discarded = resolution == "discard"
            with db:
                result = db.execute(
                    "UPDATE drafts SET status = ?, posted_at = ?, post_id = ? WHERE id = ? AND status = 'sending'",
                    ("discarded" if discarded else "posted",
                     None if discarded else datetime.now(timezone.utc).isoformat(),
                     None if discarded else resolution, int(draft_id)),
                )
                if result.rowcount != 1:
                    parser.error("No sending draft with that ID")
            return
        client = None if fixture is not None else X(config["credential_prefix"])
        interval = config.get("poll_seconds", 300)
        if interval < 1:
            parser.error("poll_seconds must be positive")
        while True:
            try:
                scan(db, config, client, fixture)
            except (OSError, RuntimeError) as exc:
                if not args.watch:
                    raise
                print(f"Scan failed: {exc}", file=sys.stderr, flush=True)
            if not args.watch:
                return
            time.sleep(interval)


if __name__ == "__main__":
    main()
