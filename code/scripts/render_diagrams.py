#!/usr/bin/env python3
"""Pre-render .state/diagrams.json to static SVG for the dashboard.

The dashboard is a static GitHub Pages site. Shipping mermaid to render at page
load means either a 3.5 MB vendored bundle in the repo or a CDN dependency that
takes the diagrams with it when it is blocked. Rendering once, here, gives small
self-contained SVGs and — more usefully — turns a syntax error into a build
failure instead of a broken panel on the published site.

Usage:
    python3 code/scripts/render_diagrams.py --mermaid /path/to/mermaid.min.js
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<script src="%s"></script></head>
<body style="background:#0d1117">
<div id="host"></div>
<script>
window.__done = false; window.__err = null; window.__svgs = {};
mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'loose',
  themeVariables: { background:'#0d1117', primaryColor:'#161b22',
    primaryTextColor:'#e6edf3', primaryBorderColor:'#30363d', lineColor:'#8b949e',
    secondaryColor:'#1c2430', tertiaryColor:'#21262d',
    fontFamily:'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize:'13px' } });
(async function () {
  const defs = %s;
  for (const d of defs) {
    try {
      const out = await mermaid.render('m_' + d.id.replace(/[^a-z0-9]/gi,'_'), d.mermaid);
      window.__svgs[d.id] = out.svg;
    } catch (e) {
      window.__err = d.id + ': ' + (e && e.message ? e.message : String(e));
      break;
    }
  }
  window.__done = true;
})();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mermaid", required=True, help="path to mermaid.min.js (UMD)")
    ap.add_argument("--diagrams", default=os.path.join(ROOT, ".state", "diagrams.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "dashboard", "diagrams"))
    args = ap.parse_args()

    import ui_snapshot as U
    from cdp import Session

    defs = json.load(open(args.diagrams))["diagrams"]
    work = os.path.join(ROOT, ".ui-snapshots", "mermaid")
    os.makedirs(work, exist_ok=True)
    os.makedirs(args.out, exist_ok=True)

    page = os.path.join(work, "render.html")
    with open(page, "w") as f:
        f.write(PAGE % ("file://" + os.path.abspath(args.mermaid), json.dumps(defs)))

    U.start_browser(os.path.join(ROOT, ".ui-snapshots", "profile"))
    s = Session("file://" + page, port=U.CDP_PORT, settle=3)
    for _ in range(60):
        if s.js("window.__done"):
            break
        time.sleep(1)

    err = s.js("window.__err")
    if err:
        print("mermaid failed to parse -> %s" % err)
        s.close()
        return 1

    svgs = s.js("window.__svgs") or {}
    s.close()

    for d in defs:
        svg = svgs.get(d["id"])
        if not svg:
            print("no SVG produced for %s" % d["id"])
            return 1
        dest = os.path.join(args.out, d["id"] + ".svg")
        with open(dest, "w") as f:
            f.write(svg)
        print("%-22s %6d bytes" % (d["id"], len(svg)))
    print("\n%d diagrams -> %s" % (len(defs), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
