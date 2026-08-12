#!/usr/bin/env python3
"""Watch the radio daemon's IPC event stream.

The LCD driver renders whatever crosses radio.sock, so when a panel shows
nothing this answers "is the data even arriving?" without involving SPI at all.

    python3 code/scripts/ipc_watch.py --seconds 30
    python3 code/scripts/ipc_watch.py --seconds 30 --names spectrogram_row
"""
import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ravensdr.ipc import resolve_socket_path            # noqa: E402
from ravensdr.radio_link import RadioLink               # noqa: E402


class _PollEvent:
    def send(self, _value=None):
        pass

    def wait(self, _timeout=None):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--names", default="", help="comma-separated filter")
    ap.add_argument("--socket", default=None)
    args = ap.parse_args()

    wanted = {n.strip() for n in args.names.split(",") if n.strip()}
    counts = {}

    def on_event(name, data):
        if wanted and name not in wanted:
            return
        counts[name] = counts.get(name, 0) + 1
        if counts[name] <= 3 or counts[name] % 20 == 0:
            # Waterfall rows are 256 bins; print shape, not the payload.
            # The radio fans Socket.IO events onto IPC verbatim, so this arrives
            # as a bare list rather than an object.
            if name == "spectrogram_row":
                bins = data.get("bins") if isinstance(data, dict) else data
                bins = bins if isinstance(bins, list) else []
                extra = "%d bins, max=%s" % (len(bins), max(bins) if bins else "-")
            else:
                extra = str(data)[:120]
            print("[%7.1fs] %-18s #%-4d %s"
                  % (time.time() - t0, name, counts[name], extra), flush=True)

    link = RadioLink(
        socket_path=args.socket or resolve_socket_path(),
        spawn_fn=lambda fn, *a: threading.Thread(target=fn, args=a,
                                                 daemon=True).start(),
        event_factory=_PollEvent, on_event=on_event, timeout=4.0)
    link.start()

    t0 = time.time()
    while time.time() - t0 < args.seconds:
        time.sleep(0.2)
    link.stop()

    print("\n--- events in %.0fs ---" % args.seconds)
    if not counts:
        print("  (none)")
    for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print("  %-20s %d" % (name, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
