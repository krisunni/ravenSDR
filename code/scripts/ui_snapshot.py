#!/usr/bin/env python3
"""Render the console in a real browser and screenshot every view and viewport.

Why not just point Chromium at the server
-----------------------------------------
Chromium 150 on this Pi (Debian trixie, 16 KB-page kernel) issues the HTTP
request — it reaches the server and returns 200, the access log proves it — but
the navigation never commits in the renderer. Page.navigate never answers,
--dump-dom never returns, and location.href stays about:blank. Every flag
combination fails the same way (single-process, old headless, no-sandbox,
ozone-headless, process-per-site). file:// URLs render normally.

So this snapshots the running server, rewrites it to load from disk, and drives
THAT. What is real: the shipped HTML, CSS and JavaScript, the live API payloads,
and a real Chromium layout engine at real device sizes. What is faked: the
transport — Socket.IO is stubbed and fetch() is served from the captured JSON.

Usage:
    python3 code/scripts/ui_snapshot.py                     # capture + shoot
    python3 code/scripts/ui_snapshot.py --url http://127.0.0.1:5000
    python3 code/scripts/ui_snapshot.py --keep               # keep the browser
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DEFAULT_URL = "http://127.0.0.1:5000"
CDP_PORT = 9444

# Endpoints the console calls on load. Captured live so the rendered page shows
# real numbers rather than placeholder zeros.
API_ENDPOINTS = [
    "/api/presets", "/api/status", "/api/config", "/api/config/secondary",
    "/api/automation", "/api/sdr/state", "/api/radio-activity", "/api/classifier/status",
    "/api/iq-collect", "/api/sei/status", "/api/emitters",
    "/api/adsb/aircraft", "/api/ism/devices", "/api/aprs/stations",
    "/api/acars/messages", "/api/pager/pages",
    "/api/satellite/passes", "/api/satellite/latest-image",
    "/api/wefax/latest", "/api/wefax/schedule",
    "/api/meteor/stats", "/api/meteor/events", "/api/meteor/showers",
    "/api/weather/current", "/api/training/stats",
    "/api/gain", "/api/squelch", "/api/ppm", "/api/sample_rate",
    "/api/deemp", "/api/direct_sampling",
]

# Representative live traffic, replayed through the stubbed socket. Reviewing a
# console only in its empty state hides every layout problem that appears once
# data arrives.
SEED = """
// Everything in the console hangs off the socket's "connect" event — presets,
// panel construction, the lot. Without it the page renders as empty chrome.
window.__fire("connect");
window.__fire("mode", {mode: "sdr"});
window.__fire("status", {running: true, label: "KC ARES Primary",
    freq: "146.960 MHz", squelch: 12, preset: "kc-ares-primary"});
window.__fire("signal_level", {rms: 4600});
[["Seattle approach, november four seven two, requesting descent to six thousand.", 0.94],
 ["Copy that, maintain heading two seven zero, contact tower on one one nine point niner.", 0.91],
 ["All units, respond code three, eastbound I-90 near mile marker eight.", 0.88]
].forEach(function (t, i) {
    window.__fire("transcript", {text: t[0], confidence: t[1],
        timestamp: "13:0" + i + ":00", label: "KC ARES Primary"});
});
window.__fire("inference_stats", {backend: "hailo", latency_ms: 214, rtf: 0.18,
    tokens_per_s: 41.6, decoder: "whisper-base", chunks: 128, silence_pct: 62});
["FM", "MSK", "OOK", "WFM", "FM", "FSK"].forEach(function (m, i) {
    window.__fire("signal_classified", {modulation: m, confidence: 0.72 + i * 0.04,
        frequency_hz: 146960000 + i * 250000, validated: i % 2 === 0});
});
window.__fire("adsb_update", [
  {hex: "a1b2c3", flight: "ASA412", lat: 47.61, lon: -122.33, altitude: 21000,
   speed: 412, track: 145, seen: 2},
  {hex: "d4e5f6", flight: "UAL891", lat: 47.44, lon: -122.30, altitude: 8300,
   speed: 288, track: 20, seen: 5},
  {hex: "778899", flight: "SKW5512", lat: 47.53, lon: -122.19, altitude: 15400,
   speed: 361, track: 310, seen: 1}
]);
"""

SCROLLABLE = """
  (function () {
    var before = window.scrollX;
    window.scrollTo(9999, window.scrollY);
    var reached = window.scrollX;
    window.scrollTo(before, window.scrollY);
    return reached;          // 0 means nothing is cut off, whatever scrollWidth says
  })()
"""


VIEWPORTS = [
    ("desktop", 1440, 900, False),
    ("laptop", 1280, 800, False),
    ("tablet", 834, 1112, True),
    ("phone", 390, 844, True),
    ("phone-small", 360, 740, True),
]

VIEWS = ["listen", "classify", "decoders", "imagery", "science", "model"]


# ── capture ───────────────────────────────────────────────────────────────
def fetch(url, binary=False):
    with urllib.request.urlopen(url, timeout=20) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def capture(base, outdir):
    """Pull the page and every asset it references into outdir."""
    os.makedirs(outdir, exist_ok=True)
    pages = {}
    for name, path in [("console", "/"), ("learn", "/learn")]:
        try:
            pages[name] = fetch(base + path)
        except urllib.error.URLError as e:
            print("  ! %s unreachable: %s" % (path, e))
    assets = set()
    for html in pages.values():
        assets |= set(re.findall(r'(?:src|href)="/static/([^"?]+)', html))

    for a in sorted(assets):
        dest = os.path.join(outdir, "static", a)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            with open(dest, "wb") as f:
                f.write(fetch(base + "/static/" + a, binary=True))
        except urllib.error.URLError:
            print("  ! missing asset", a)

    api = {}
    for ep in API_ENDPOINTS:
        try:
            api[ep] = json.loads(fetch(base + ep))
        except Exception:
            pass                       # not every endpoint exists in every build
    print("  captured %d pages, %d assets, %d API responses"
          % (len(pages), len(assets), len(api)))
    return pages, api


# ── rewrite ───────────────────────────────────────────────────────────────
SHIM = """
<script>
// Transport doubles. Everything below this line is the real application.
window.__errors = [];
window.addEventListener("error", function (e) {
    window.__errors.push(e.message + " @ " +
        String(e.filename || "").split("/").pop() + ":" + e.lineno);
});
window.addEventListener("unhandledrejection", function (e) {
    window.__errors.push("promise: " + e.reason);
});
var CANNED = %s;
window.__sockets = [];
window.io = function () {
    var h = {};
    var sock = {
        on: function (ev, fn) { (h[ev] = h[ev] || []).push(fn); return sock; },
        emit: function () { return sock; },
        fire: function (ev, d) { (h[ev] || []).forEach(function (f) { f(d); }); },
        connected: true, id: "probe"
    };
    window.__sockets.push(sock);
    return sock;
};
window.fetch = function (url, opts) {
    var path = String(url).split("?")[0];
    var body = CANNED[path];
    return Promise.resolve({
        ok: body !== undefined,
        status: body !== undefined ? 200 : 404,
        json: function () { return Promise.resolve(body === undefined ? {} : body); },
        text: function () { return Promise.resolve(JSON.stringify(body || {})); }
    });
};
window.__fire = function (ev, data) {
    window.__sockets.forEach(function (s) { s.fire(ev, data); });
};
</script>
"""


def rewrite(html, api):
    html = re.sub(r'(src|href)="/static/([^"?]+)(\?[^"]*)?"',
                  r'\1="static/\2"', html)
    # Drop the real Socket.IO client. It defines window.io itself and would
    # overwrite the stub, leaving the page waiting forever on a server that is
    # not there — which is exactly how the first pass came out blank.
    html = re.sub(r'\s*<script src="static/vendor/socket\.io[^"]*"></script>',
                  "", html)
    html = html.replace('href="/learn"', 'href="learn.html"')
    html = html.replace('href="/"', 'href="console.html"')
    return html.replace("</head>", (SHIM % json.dumps(api)) + "</head>", 1)


# ── drive ─────────────────────────────────────────────────────────────────
def start_browser(profile):
    if os.path.exists(profile):
        shutil.rmtree(profile, ignore_errors=True)
    subprocess.Popen(
        ["chromium", "--headless", "--no-sandbox", "--disable-gpu",
         "--disable-dev-shm-usage", "--hide-scrollbars", "--force-device-scale-factor=1",
         "--remote-debugging-port=%d" % CDP_PORT,
         "--user-data-dir=" + profile, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    for _ in range(40):
        try:
            urllib.request.urlopen(
                "http://127.0.0.1:%d/json/version" % CDP_PORT, timeout=2)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--out", default=os.path.join(HERE, "..", "..",
                                                  ".ui-snapshots"))
    ap.add_argument("--keep", action="store_true",
                    help="leave the browser running afterwards")
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    work = os.path.join(out, "site")
    shots = os.path.join(out, "shots")
    os.makedirs(shots, exist_ok=True)

    print("Capturing %s ..." % args.url)
    pages, api = capture(args.url, work)
    if "console" not in pages:
        print("Console page unreachable — is ravensdr running?")
        return 1
    for name, html in pages.items():
        with open(os.path.join(work, name + ".html"), "w") as f:
            f.write(rewrite(html, api))

    print("Starting Chromium ...")
    if not start_browser(os.path.join(out, "profile")):
        print("Chromium did not expose a debugging port.")
        return 1

    from cdp import Session                                   # noqa: E402

    report = {"console": {}, "learn": {}, "errors": []}
    console_url = "file://" + os.path.join(work, "console.html")

    for label, w, h, mobile in VIEWPORTS:
        s = Session(console_url, port=CDP_PORT, settle=5)
        s.viewport(w, h, mobile=mobile)
        s.js(SEED)
        time.sleep(2.5)
        for view in VIEWS:
            s.js("document.querySelector('.view-tab[data-view=\"%s\"]').click()"
                 % view)
            time.sleep(0.8)
            s.shot(os.path.join(shots, "console-%s-%s.png" % (label, view)))
            metrics = s.js("""
              (function () {
                var v = document.querySelector('.view.active');
                return { hOverflow: document.documentElement.scrollWidth -
                                    document.documentElement.clientWidth,
                         pageHeight: document.documentElement.scrollHeight,
                         viewport: window.innerHeight,
                         tinyText: [].slice.call(v.querySelectorAll('*'))
                            .filter(function (e) {
                                return e.children.length === 0 &&
                                       e.textContent.trim() &&
                                       parseFloat(getComputedStyle(e).fontSize) < 11;
                            }).length,
                         smallTargets: [].slice.call(
                            document.querySelectorAll('button, a, input, select'))
                            .filter(function (e) {
                                var r = e.getBoundingClientRect();
                                return r.width > 0 && r.height > 0 && r.height < 32;
                            }).length };
              })()
            """)
            metrics["panX"] = s.js(SCROLLABLE)
            report["console"].setdefault(label, {})[view] = metrics
        report["errors"] += s.console() + (s.js("window.__errors") or [])
        s.close()
        print("  console @ %-11s %dx%d" % (label, w, h))

    if "learn" in pages:
        learn_url = "file://" + os.path.join(work, "learn.html")
        for label, w, h, mobile in VIEWPORTS:
            s = Session(learn_url, port=CDP_PORT, settle=6)
            s.viewport(w, h, mobile=mobile)
            s.shot(os.path.join(shots, "learn-%s.png" % label))
            lm = s.js("""
              ({ hOverflow: document.documentElement.scrollWidth -
                            document.documentElement.clientWidth,
                 pageHeight: document.documentElement.scrollHeight,
                 sections: document.querySelectorAll('section').length,
                 svgs: document.querySelectorAll('svg').length,
                 canvases: document.querySelectorAll('canvas').length })
            """)
            lm["panX"] = s.js(SCROLLABLE)
            report["learn"][label] = lm
            report["errors"] += s.console() + (s.js("window.__errors") or [])
            s.close()
            print("  learn   @ %-11s %dx%d" % (label, w, h))

    with open(os.path.join(out, "report.json"), "w") as f:
        json.dump(report, f, indent=1)

    if not args.keep:
        subprocess.run(["pkill", "-f", "remote-debugging-port=%d" % CDP_PORT],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        # A Chromium profile is ~123 MB. Leaving one behind per run fills a Pi
        # quickly, and nothing here needs to persist between runs.
        shutil.rmtree(os.path.join(out, "profile"), ignore_errors=True)

    pans = [("console/%s/%s" % (vp, v)) for vp, vs in report["console"].items()
            for v, m in vs.items() if m.get("panX")]
    pans += ["learn/%s" % vp for vp, m in report["learn"].items() if m.get("panX")]
    print("\nHorizontal pan (real, user-visible): %s" % (", ".join(pans) or "none"))
    print("Shots  -> %s" % shots)
    print("Report -> %s" % os.path.join(out, "report.json"))
    # Leaflet's stylesheet references images/*.png relative to itself; the
    # capture only walks the HTML, so those 404 in the harness and nowhere else.
    errs = sorted({e for e in report["errors"]
                   if "ERR_FILE_NOT_FOUND" not in e})
    print("JS errors: %s" % ("\n  " + "\n  ".join(errs) if errs else "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
