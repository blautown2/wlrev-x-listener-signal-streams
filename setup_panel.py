"""Local operator panel for account connection and feed scans."""
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
from threading import Lock, Thread
from urllib.parse import parse_qs

STYLE = """
:root{font:16px system-ui,sans-serif;color:#192721;background:#eef2ef}
main{max-width:760px;margin:64px auto;padding:0 24px}
section{background:white;border:1px solid #ccd8cf;padding:24px;border-radius:14px;margin:18px 0}
h1{font-size:2.4rem;margin:.25em 0}h2{margin-top:0;font-size:1.25rem}
form{display:flex;gap:10px;align-items:end;flex-wrap:wrap}
input,button{font:inherit;padding:.6rem .85rem;border-radius:7px}
input{border:1px solid #9faea3}button{border:0;background:#155539;color:white;cursor:pointer}
button:disabled{opacity:.55}label{display:grid;gap:5px}
small{color:#43574a}a{color:#155539}li{margin:1em 0}p{line-height:1.45}
"""


def serve(config_path, port):
    # Import at runtime so the scanner can also run without the panel.
    from browser_listener import Deployment

    token = secrets.token_urlsafe(32)
    guard = Lock()
    state = {"busy": False, "message": "Connect the X account to start listening."}

    def work(action, deployment, fields):
        try:
            if action == "connect":
                deployment.connect()
                message = f"Connected @{deployment.handle}. Warm the feed before your first scan."
            elif action == "open":
                deployment.require_connection()
                from browser_listener import HOME
                with deployment.browser() as page:
                    page.goto(HOME, wait_until="domcontentloaded")
                    deployment.verify_account(page)
                    page.wait_for_event("close", timeout=0)
                message = "Browser session saved."
            elif action == "scan":
                result = deployment.scan()
                message = f"Scanned {result['posts_seen']} feed posts and created {len(result['drafts'])} drafts."
            else:
                deployment.compose(int(fields["draft_id"][0]))
                message = "Composer closed. Check X for publication status."
        except Exception as exc:
            message = str(exc).splitlines()[0] or type(exc).__name__
        with guard:
            state.update(busy=False, message=message)

    class Handler(BaseHTTPRequestHandler):
        def local(self):
            return self.path == "/" and self.headers.get("Host") in (
                f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"
            )

        def do_GET(self):
            if not self.local():
                return self.send_error(404)
            deployment = Deployment(config_path)
            config = deployment.config
            connection = deployment.connection()
            with guard:
                busy, message = state["busy"], state["message"]
            disabled = "disabled" if busy else ""
            unavailable = "disabled" if busy or not connection else ""
            csrf = f'<input type="hidden" name="token" value="{token}">'
            drafts = []
            for row in deployment.drafts():
                sources = " ".join(
                    f'<a href="{escape(url)}" target="_blank" rel="noopener">Source {i}</a>'
                    for i, url in enumerate(json.loads(row["evidence"]), 1)
                )
                drafts.append(f'''<li><p>{escape(row["message"])}</p><p>{sources}</p>
                <form method="post">{csrf}<input type="hidden" name="draft_id" value="{row["id"]}">
                <button name="action" value="compose" {unavailable}>Open in X composer</button></form></li>''')
            brand = escape(config.get("brand_name") or deployment.stream)
            handle = escape(deployment.handle)
            alias = escape(config.get("account_email") or "")
            identity = "readonly" if connection else ""
            refresh = '<meta http-equiv="refresh" content="3">' if busy else ""
            status = f"Verified connection for @{handle}" if connection else "X account setup"
            body = f'''<!doctype html><html lang="en"><meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">{refresh}
            <title>{brand} · Listener setup</title><style>{STYLE}</style><main>
            <small>Stream {escape(deployment.stream)}</small><h1>{brand}</h1>
            <section><h2>{status}</h2><p>Sign in directly in the X window. Use the account email alias
            for registration and recovery. Route that alias to the operator inbox.</p>
            <form method="post">{csrf}
            <label>X handle<input name="handle" value="{handle}" placeholder="@yourhandle" required {identity}></label>
            <label>Account email alias<input name="alias" type="email" value="{alias}" {identity}></label>
            <button name="action" value="connect" {disabled}>Connect X</button></form>
            <p role="status">{escape(message)}</p>
            <form method="post">{csrf}
            <button name="action" value="open" {unavailable}>Open feed to warm account</button>
            <button name="action" value="scan" {unavailable}>Scan For You feed</button></form>
            </section><section><h2>Drafts</h2>
            {("<ol>" + "".join(drafts) + "</ol>") if drafts else "<p>No drafts yet.</p>"}
            </section></main></html>'''.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not self.local():
                return self.send_error(404)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8192:
                    return self.send_error(400)
                fields = parse_qs(self.rfile.read(size).decode(), keep_blank_values=True)
            except (ValueError, UnicodeError):
                return self.send_error(400)
            if not secrets.compare_digest(fields.get("token", [""])[0].encode(), token.encode()):
                return self.send_error(403)
            action = fields.get("action", [""])[0]
            if action not in {"connect", "open", "scan", "compose"}:
                return self.send_error(400)
            with guard:
                if state["busy"]:
                    return self.send_error(409, "The stream already has a browser action running")
                deployment = Deployment(config_path)
                if action == "connect" and not deployment.connection():
                    import re
                    handle = fields.get("handle", [""])[0].strip().removeprefix("@").lower()
                    alias = fields.get("alias", [""])[0].strip()
                    if not re.fullmatch(r"[a-z0-9_]{1,15}", handle):
                        return self.send_error(400, "Enter a valid X handle")
                    if alias and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", alias):
                        return self.send_error(400, "Enter a valid account email alias")
                    deployment.config.update(x_handle=handle, account_email=alias)
                    temporary = config_path.with_suffix(".tmp")
                    try:
                        temporary.write_text(json.dumps(deployment.config, indent=2) + "\n")
                        os.replace(temporary, config_path)
                    finally:
                        temporary.unlink(missing_ok=True)
                    deployment = Deployment(config_path)
                state.update(busy=True, message=(
                    "Finish signing in and any verification in the X window."
                    if action == "connect" else "Browser action in progress."
                ))
                Thread(target=work, args=(action, deployment, fields), daemon=True).start()
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"Operator panel: http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()
