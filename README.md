# wlrev-plugin: X-listener-signal-streamer

The browser runtime reads the signed-in account’s For You feed, groups observed posts by configured signal, and prepares source-linked drafts. The operator panel prompts for the X connection, opens the dedicated browser for sign-in and feed warm-up, and lets the operator review drafts and open them in the X composer. The browser runtime uses regular X sign-in and requires no X API credentials or Premium subscription.

## Stand up one deployment

Use a desktop host with Python 3.10+ and Chromium. The operator panel is local to that host.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
cp browser.example.json buyer-stream.json
python browser_listener.py buyer-stream.json setup
```

On Linux hosts without the browser system libraries, install them with `python -m playwright install --with-deps chromium`. Set `LISTENER_CHROMIUM_PATH` to use an existing Chromium executable.

1. Set a unique `stream_id`, a buyer-facing `brand_name`, and signal rules in the copied config. Each rule has a case-insensitive Python regex in `match`, a time window, a threshold for distinct authors, a cooldown, and a post template. Templates accept `{count}` and `{source}`.
2. Open the localhost panel at the printed URL. Enter the expected X handle and, optionally, the account’s routed email alias. Click **Connect X** and sign in directly in the browser window. The panel verifies the account handle. X passwords and verification codes are entered in X itself.
3. Use **Open feed to warm account** to curate the account’s feed. Close the window when finished, then use **Scan For You feed** to check the resulting signal mix. Matching posts accumulate across scans within the configured time window.
4. Review a draft and choose **Open in X composer**. Review and publish in X. The operator panel does not record publication and keeps the draft available for review.
5. To scan periodically, run `python browser_listener.py buyer-stream.json watch`. Stop watch before opening the setup panel’s browser actions for that same stream, as Chromium locks its profile while running. The panel can display draft history during watch. Use `--port` to run multiple panels on separate localhost ports.

### Stream identity and branding

Each stream owns a directory at `.listener-data/<stream_id>/` beside its config. It contains a Chromium profile, a verified account record, observations, and draft history. Keep that directory private and persistent across restarts. Give a different X account a different stream ID so its session and feed are isolated. The brand name and editorial templates live in the deployment config.

For a single operator inbox, create a distinct Cloudflare Email Routing address for each stream and forward all those addresses to the same verified destination. Match the alias to the stream ID where possible. Enter the alias in the panel to associate it with that deployment. Cloudflare handles forwarding. The listener has no mailbox access and the operator completes account verification and recovery. [Cloudflare Email Routing](https://developers.cloudflare.com/email-routing/)

Distribute the source and a fresh branded configuration. Browser profiles, credentials, and draft history belong to the operator host and should stay out of distribution archives.

## Optional API worker

The original API worker remains available as a separate runtime with its own configuration and state. It polls X recent search and supports opt-in automatic publishing.

### Demo

Requires Python 3.10 or later. Run these commands from this directory.

```sh
python3 listener.py example.json --fixture fixture.json
python3 listener.py example.json --fixture fixture.json --list
```

The fixture generates one draft from three distinct authors. The copied fourth post is excluded. Demo history lives in its own SQLite file.

### Run an API instance

1. Copy `example.json` to an instance-specific filename and set its numeric X user ID, credential prefix, and signal rules. Each rule has an X search query, time window, minimum distinct-author count, cooldown, and original editorial template. The template can use `{count}` and `{source}`. Include the source in independently written templates that reference observations.
2. Provide OAuth 1.0a credentials for the account through the environment. For the example prefix, use `DEVTOOLS_X_API_KEY`, `DEVTOOLS_X_API_SECRET`, `DEVTOOLS_X_ACCESS_TOKEN`, and `DEVTOOLS_X_ACCESS_SECRET`. The app needs access to X recent search and user-context write permissions for publishing.
3. Run `python3 listener.py your-instance.json --watch` under a process supervisor on an always-on host. Omit `--watch` for a single scan. Inspect drafts and their evidence with `python3 listener.py your-instance.json --list`.
4. Set `publish` to `true` and restart when you want automatic publishing. The publish interval is shared across that account's rules. Drafts older than 24 hours are ineligible for publishing.

Signal matching uses the configured X query. A rule can include alternate phrasings. Counts exclude reposts, the operating account, repeated authors, and matching text after punctuation and links are removed. The count measures distinct accounts in the available search results. After a trigger, qualifying posts for the next trigger must be newer than that draft.

If a publish attempt stops at `sending`, inspect the X timeline. Use `python3 listener.py your-instance.json --resolve DRAFT_ID POST_ID` if X published it, or use `discard` in place of the post ID if it did not. This avoids automatically retrying a request X may already have accepted.

### API handoff

Deliver the source and the instance configuration with its signal queries and editorial templates. The buyer supplies credentials for their own account and runs the listener under their own hosting. Keep credentials and SQLite history outside the distribution. Use one configuration and state database per account.
