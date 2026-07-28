# UI snapshot harness

`ui_snapshot.py` renders the console and the `/learn` explainer in a real
Chromium at five viewport sizes, screenshots every view, and reports layout
problems (horizontal overflow, sub-11px text, sub-32px tap targets).

```bash
python3 code/scripts/ui_snapshot.py            # against http://127.0.0.1:5000
python3 code/scripts/ui_snapshot.py --keep     # leave the browser up to poke at
```

Output lands in `.ui-snapshots/` (git-ignored): `shots/` for the PNGs,
`report.json` for the measurements.

## Why it snapshots instead of browsing the server

Chromium 150 on this Pi cannot commit an HTTP navigation. The request is issued
and answered — it shows up in the server's access log with a 200 — but the
renderer stays on `about:blank`, `Page.navigate` never returns, and
`--dump-dom` hangs forever. Verified against the Flask app, a plain
`python3 -m http.server`, and `https://example.com`; and under `--headless=old`,
`--headless=new`, `--single-process`, `--no-sandbox`, `--ozone-platform=headless`,
`--process-per-site` and `--disable-quic`. No error is logged and nothing
crashes. `file://` URLs render normally. The kernel here uses 16 KB pages, which
is the standout suspect.

This is also why the `chromium-arm64` MCP server cannot drive the live UI on
this box: its `navigate` tool returns `CDP command timeout: Page.navigate`. The
server itself is installed and healthy — the browser underneath it is not.

So the harness captures the served HTML, CSS, JS and API responses, rewrites the
URLs to relative, and loads the result over `file://`. Real markup, real
stylesheets, real application code, real payloads, real layout engine. The only
doubles are the transport: Socket.IO is stubbed and `fetch()` is served from the
captured JSON, with `SEED` replaying representative traffic so panels are
reviewed populated rather than empty.

If a future Chromium fixes HTTP navigation, point `cdp.Session` at the live URL
and delete the capture step.

## Disk

Each run creates a ~123 MB Chromium profile under `.ui-snapshots/profile` and
deletes it on exit. `--keep` leaves both the browser and the profile in place;
remove `.ui-snapshots/` when you are done with the shots.
