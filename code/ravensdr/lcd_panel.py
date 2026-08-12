#!/usr/bin/env python3
"""Screen driver for the Waveshare Zero LCD HAT (A).

Third peer in the split: the radio daemon owns the hardware and serves
`radio.sock`, and both the web app (`ui_app.py`) and this process are clients of
it. Nothing here touches the SDR, the NPU, or eventlet — it asks the radio for a
status snapshot and paints pixels. That is the whole reason it is a separate
process: a wedged panel, a yanked HAT or an SPI stall can never take the radio
down with it.

  ravensdr.service       radio daemon   owns SDR/Hailo, serves radio.sock
  ravensdr-ui.service    web app        RadioLink client
  ravensdr-lcd.service   this           RadioLink client

Three panels:
  1.3in  240x240  paged status, KEY1 cycles the page
  0.96in 160x80   frequency
  0.96in 160x80   node health

KEY2 cycles brightness (100 -> 30 -> off), because the node lives in a room
where a permanently lit screen is not always wanted.

The process must stay resident: gpiozero releases every pin on exit, which drops
the backlight and leaves the panels dark even though the image is still held in
each controller's RAM.
"""

import argparse
import collections
import logging
import os
import socket
import subprocess
import threading
import time

import spidev
from gpiozero import Button
from PIL import Image, ImageDraw, ImageFont

from ravensdr.ipc import resolve_socket_path
from ravensdr.lcd import KEY1, KEY2, PANELS, LCD_0inch96, LCD_1inch3
from ravensdr.radio_link import RadioLink, RadioLinkError

log = logging.getLogger("ravensdr.lcd")

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

BG = "#000000"
GREEN = "#3ddc84"
BLUE = "#4aa3ff"
AMBER = "#ffb347"
RED = "#ff5c5c"
DIM = "#7a7a7a"
FAINT = "#3a3a3a"

BRIGHTNESS = (100, 30, 0)
PAGES = ("RADIO", "WATER", "CLASS", "DECODE", "SYSTEM")

# Rows kept for the waterfall. The main panel is 240 tall; an aux is 80. Keeping
# the larger history costs nothing and lets the same buffer feed either.
WATERFALL_ROWS = 240
# Rows arrive only while IQ collection holds the dongle. Past this the display
# is history, not a live view, and saying so beats a frozen picture.
WATERFALL_STALE_S = 6.0

# Blue-black through cyan to white: dark noise floor, bright signals. Matches
# the browser waterfall closely enough to read the same way.
def _heat(v):
    v = max(0, min(255, int(v)))
    if v < 64:
        return (0, 0, 40 + v)
    if v < 128:
        return (0, (v - 64) * 4, 104 + (v - 64) // 2)
    if v < 192:
        return ((v - 128) * 3, 255, 136 - (v - 128) * 2)
    return (192 + (v - 192), 255, 8 + (v - 192) * 3)

_font_cache = {}


def font(size, bold=False):
    key = (size, bold)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(FONT_BOLD if bold else FONT_PATH, size)
    return _font_cache[key]


# ── Local facts the radio daemon does not know about ──

def cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as fh:
            return int(fh.read()) / 1000.0
    except OSError:
        return None


def load_avg():
    try:
        return os.getloadavg()[0]
    except OSError:
        return None


def uptime():
    try:
        with open("/proc/uptime") as fh:
            secs = float(fh.read().split()[0])
    except OSError:
        return "?"
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d{h:02d}h" if d else f"{h:02d}h{m:02d}m"


def primary_ip():
    """Address a client would actually reach us on, without needing a route up.

    Uses a connectionless UDP socket so nothing is transmitted; it only asks the
    kernel which source address it would pick.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))     # TEST-NET-1, never routed
        return s.getsockname()[0]
    except OSError:
        return "no-net"
    finally:
        s.close()


def service_active(name):
    try:
        out = subprocess.run(["systemctl", "is-active", name],
                             capture_output=True, text=True, timeout=3)
        return out.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


# ── Radio link ──

class _PollEvent:
    """RadioLink polls `is_done()` rather than blocking, so this only has to
    exist. threading.Event has .set(), not the .send() eventlet shape it calls.
    """

    def send(self, _value=None):
        pass

    def wait(self, _timeout=None):
        pass


class Radio:
    """Status snapshot from the radio daemon, plus its pushed event stream.

    Status is polled; the waterfall and classifications are pushed, because they
    arrive at ~3 fps and only exist while IQ collection holds the dongle.
    """

    def __init__(self, socket_path, waterfall_rows=WATERFALL_ROWS):
        self.rows = collections.deque(maxlen=waterfall_rows)
        self.last_class = None
        self.last_row_at = 0.0
        self._lock = threading.Lock()

        self.link = RadioLink(
            socket_path=socket_path,
            spawn_fn=lambda fn, *a: threading.Thread(
                target=fn, args=a, daemon=True).start(),
            event_factory=_PollEvent,
            on_event=self._on_event,
            timeout=4.0,
        )
        self.link.start()

    def _on_event(self, name, data):
        # Runs on the link's reader thread; keep it to a lock and a deque.
        #
        # The radio fans every Socket.IO event straight onto IPC, so the payload
        # is whatever the browser gets: spectrogram_row is a BARE LIST of 256
        # bins, not an object. The dict form is tolerated so a future sender
        # that wraps it does not silently drop the waterfall.
        if name == "spectrogram_row":
            bins = data.get("bins") if isinstance(data, dict) else data
            if isinstance(bins, list) and bins:
                with self._lock:
                    self.rows.append(bins)
                    self.last_row_at = time.time()
        elif name == "signal_classified":
            if isinstance(data, dict):
                with self._lock:
                    self.last_class = data

    def waterfall(self):
        with self._lock:
            return list(self.rows), self.last_row_at

    def classification(self):
        with self._lock:
            return self.last_class

    def status(self):
        if not self.link.is_up:
            return None, f"link {self.link.link}"
        try:
            return self.link.request("status"), None
        except RadioLinkError as exc:
            return None, str(exc)


# ── Drawing ──

def draw_main(size, page, st, err, rows=(), last_at=0.0, clf=None):
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)

    d.rectangle([(0, 0), (size[0], 28)], fill="#0a3d2a")
    d.text((6, 4), "ravenSDR", fill=GREEN, font=font(18, bold=True))
    d.text((size[0] - 58, 8), PAGES[page], fill=GREEN, font=font(12))

    # The waterfall and classifier own the whole area below the header, so they
    # render at their own size and get pasted rather than drawn inline.
    if page == 1 and not err:
        body = (size[0], size[1] - 28)
        img.paste(draw_waterfall(body, rows, last_at, st, err, header=False), (0, 28))
        stale = (time.time() - last_at) > WATERFALL_STALE_S
        if rows:
            d.text((size[0] - 120, 8), "STALE" if stale else "LIVE",
                   fill=AMBER if stale else GREEN, font=font(12))
        return img
    if page == 2 and not err:
        body = (size[0], size[1] - 28)
        img.paste(draw_classify(body, clf, st, err), (0, 28))
        return img

    if err:
        d.text((6, 44), "RADIO LINK", fill=RED, font=font(18, bold=True))
        d.text((6, 70), "DOWN", fill=RED, font=font(22, bold=True))
        for i, line in enumerate(_wrap(err, 22)[:4]):
            d.text((6, 104 + i * 16), line, fill=DIM, font=font(12))
        d.text((6, 214), primary_ip(), fill=FAINT, font=font(12))
        return img

    if page == 0:
        label = str(st.get("label") or "—")
        d.text((6, 38), label[:20], fill="WHITE", font=font(15))
        d.text((6, 62), str(st.get("freq") or "—"), fill=BLUE, font=font(26, bold=True))
        mode = str(st.get("mode") or "?")
        running = st.get("running")
        d.text((6, 100), f"{mode}  {'RUN' if running else 'IDLE'}",
               fill=GREEN if running else DIM, font=font(14))

        # Commanded vs actual: the radio can be mid-switch, and "tuned" in the
        # UI means commanded. Showing both is the difference between "it ignored
        # me" and "it is still killing rtl_fm".
        sdr = st.get("sdr") or {}
        cmd = (sdr.get("commanded") or {}).get("id")
        act = (sdr.get("actual") or {}).get("id")
        locked = cmd == act
        d.text((6, 124), "LOCKED" if locked else "SWITCHING",
               fill=GREEN if locked else AMBER, font=font(14, bold=True))
        if not locked:
            d.text((6, 142), f"-> {cmd or '?'}"[:26], fill=DIM, font=font(11))

        d.text((6, 158), f"SQ {st.get('squelch', '?')}   GAIN {st.get('gain', '?')}",
               fill=DIM, font=font(12))
        auto = st.get("automation") or {}
        d.text((6, 180), "SCHED " + ("on" if auto.get("enabled") else "off"),
               fill=GREEN if auto.get("enabled") else DIM, font=font(12))
        collecting = collecting_now(st)
        d.text((6, 196), "COLLECT " + ("on" if collecting else "off"),
               fill=AMBER if collecting else DIM, font=font(12))

    elif page == 3:
        rows = [
            ("ISM", st.get("ism_running")),
            ("ADS-B", st.get("adsb_scanning") or st.get("adsb_dedicated")),
            ("AIS", st.get("ais_dedicated")),
            ("ACARS", st.get("acars_running")),
            ("APRS", st.get("aprs_running")),
            ("PAGER", st.get("pager_running")),
            ("APT", st.get("apt_mode") or st.get("apt_recording")),
            ("METEOR", st.get("meteor_running")),
        ]
        for i, (name, on) in enumerate(rows):
            y = 38 + i * 22
            d.text((6, y), name, fill="WHITE" if on else DIM, font=font(14))
            d.text((150, y), "ACTIVE" if on else "idle",
                   fill=GREEN if on else FAINT, font=font(12))

    else:
        t = cpu_temp()
        la = load_avg()
        lines = [
            ("IP", primary_ip()),
            ("NPU", str(st.get("classifier_backend") or "?")),
            ("ASR", str(st.get("transcriber_backend") or "?")),
            ("CPU", f"{t:.1f} C" if t is not None else "?"),
            ("LOAD", f"{la:.2f}" if la is not None else "?"),
            ("UP", uptime()),
            ("RADIO", "active" if service_active("ravensdr") else "DOWN"),
        ]
        for i, (k, v) in enumerate(lines):
            y = 40 + i * 26
            d.text((6, y), k, fill=DIM, font=font(13))
            d.text((78, y), str(v), fill="WHITE", font=font(15))

    return img


def collecting_now(st):
    """Effective collection state, not the raw flag.

    config.is_automation_enabled lets the master switch veto every task, so
    iq_collect can read true while nothing is collected. Reporting the flag
    would leave the waterfall permanently empty with no explanation.
    """
    auto = st.get("automation") or {}
    return bool(auto.get("enabled", True)) and bool(auto.get("iq_collect"))


def draw_waterfall(size, rows, last_at, st, err, header=True):
    """Spectrogram history, newest row at the bottom.

    Each row is 256 FFT bins; the panel is narrower, so bins are folded by max
    rather than sampled — a one-bin-wide carrier must not vanish because the
    nearest sample missed it.
    """
    w, h = size
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)
    top = 16 if header else 0

    if not rows:
        d.text((6, 3), "WATERFALL", fill=GREEN, font=font(12, bold=True))
        collecting = collecting_now(st)
        if err:
            d.text((6, 26), "radio link down", fill=RED, font=font(12))
        elif not collecting:
            gated = (st.get("automation") or {}).get("iq_collect") and \
                not (st.get("automation") or {}).get("enabled", True)
            d.text((6, 24), "no IQ: collection", fill=AMBER, font=font(11))
            d.text((6, 38), "is off", fill=AMBER, font=font(11))
            d.text((6, 56), "Sched off blocks it" if gated else "enable Collect",
                   fill=DIM, font=font(9 if gated else 10))
        else:
            d.text((6, 30), "waiting for IQ...", fill=DIM, font=font(11))
        return img

    band = h - top
    shown = rows[-band:]
    for y, row in enumerate(shown):
        n = len(row)
        if n >= w:
            step = n / float(w)
            for x in range(w):
                lo = int(x * step)
                hi = max(lo + 1, int((x + 1) * step))
                d.point((x, top + band - len(shown) + y), fill=_heat(max(row[lo:hi])))
        else:
            for x in range(w):
                d.point((x, top + band - len(shown) + y),
                        fill=_heat(row[int(x * n / float(w))]))

    if header:
        d.rectangle([(0, 0), (w, top - 1)], fill=BG)
        stale = (time.time() - last_at) > WATERFALL_STALE_S
        d.text((3, 2), str(st.get("freq") or "—"), fill=BLUE, font=font(11, bold=True))
        d.text((w - 40, 2), "STALE" if stale else "LIVE",
               fill=AMBER if stale else GREEN, font=font(10))
    return img


def draw_classify(size, clf, st, err):
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)
    big = size[0] > 200
    d.text((6, 3), "CLASSIFIER", fill=GREEN, font=font(14 if big else 12, bold=True))

    if not clf:
        backend = str(st.get("classifier_backend") or "?")
        d.text((6, 30), "no signal yet", fill=DIM, font=font(13 if big else 11))
        d.text((6, 50 if big else 46), f"backend {backend}", fill=FAINT,
               font=font(12 if big else 10))
        if not collecting_now(st):
            d.text((6, 72 if big else 62), "collection off", fill=AMBER,
                   font=font(12 if big else 10))
        return img

    mod = str(clf.get("modulation") or "?")
    conf = float(clf.get("confidence") or 0.0)
    # An unproven class can score high by recognising one band's noise floor
    # rather than the modulation, so the caveat travels with the number.
    caveat = clf.get("validation") in ("unproven", "unprovable")

    # The 160x80 panel has ~74 usable rows once the title is drawn, so the
    # caveat only fits if everything above it is packed tighter than on the
    # 240x240 — and the caveat is the one line that must never be cut off.
    d.text((6, 24 if big else 18), mod[:12],
           fill=AMBER if caveat else "WHITE", font=font(30 if big else 18, bold=True))
    y = 62 if big else 40
    pct = int(conf * 100)
    d.text((6, y), f"{pct}%", fill=GREEN if conf > 0.7 else DIM,
           font=font(20 if big else 13, bold=True))

    bar_y = y + (28 if big else 15)
    bar_h = 10 if big else 5
    bar_w = size[0] - 12
    d.rectangle([(6, bar_y), (6 + bar_w, bar_y + bar_h)], outline=FAINT)
    if pct:
        d.rectangle([(6, bar_y), (6 + int(bar_w * conf), bar_y + bar_h)],
                    fill=GREEN if conf > 0.7 else AMBER)
    if caveat:
        d.text((6, bar_y + (18 if big else 8)), "unproven class",
               fill=AMBER, font=font(12 if big else 9))
    return img


def draw_freq(size, st, err):
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)
    if err:
        d.text((6, 4), "RADIO", fill=RED, font=font(13, bold=True))
        d.text((6, 30), "LINK DOWN", fill=RED, font=font(15, bold=True))
        return img
    d.text((6, 3), "TUNED", fill=GREEN, font=font(13, bold=True))
    d.text((6, 24), str(st.get("freq") or "—"), fill=BLUE, font=font(20, bold=True))
    d.text((6, 52), str(st.get("label") or "")[:20], fill=DIM, font=font(11))
    d.text((6, 66), str(st.get("mode") or ""), fill=FAINT, font=font(10))
    return img


def draw_health(size, st, err):
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)
    t = cpu_temp()
    colour = GREEN if (t or 0) < 65 else AMBER if (t or 0) < 80 else RED
    d.text((6, 3), "NODE", fill=GREEN, font=font(13, bold=True))
    d.text((6, 22), f"{t:.1f} C" if t is not None else "? C",
           fill=colour, font=font(18, bold=True))
    d.text((6, 46), primary_ip(), fill="WHITE", font=font(12))
    d.text((6, 62), ("radio ok" if not err else "radio DOWN"),
           fill=DIM if not err else RED, font=font(11))
    return img


def draw_compact(size, page, st, err, rows=(), last_at=0.0, clf=None):
    """Everything the main panel would show, squeezed onto a 160x80 aux panel.

    Used when the 1.3in panel is not driveable. Without this a node with one
    working panel shows only a temperature, and the frequency — the single most
    useful number on the whole HAT — is nowhere.
    """
    img = Image.new("RGB", size, BG)
    d = ImageDraw.Draw(img)
    if err:
        d.text((6, 3), "RADIO", fill=RED, font=font(13, bold=True))
        d.text((6, 24), "LINK DOWN", fill=RED, font=font(15, bold=True))
        d.text((6, 50), primary_ip(), fill=DIM, font=font(11))
        return img

    # Full-bleed pages return before the page label is drawn over them.
    if page == 1:
        return draw_waterfall(size, rows, last_at, st, err, header=True)
    if page == 2:
        return draw_classify(size, clf, st, err)

    d.text((size[0] - 46, 3), PAGES[page], fill=FAINT, font=font(10))
    if page == 0:
        d.text((6, 2), "TUNED", fill=GREEN, font=font(12, bold=True))
        d.text((6, 20), str(st.get("freq") or "—"), fill=BLUE, font=font(19, bold=True))
        d.text((6, 46), str(st.get("label") or "")[:21], fill="WHITE", font=font(10))
        auto = st.get("automation") or {}
        d.text((6, 62), "sched %s  collect %s"
               % ("on" if auto.get("enabled") else "off",
                  "on" if collecting_now(st) else "off"),
               fill=DIM, font=font(9))
    elif page == 3:
        active = [n for n, on in (
            ("ISM", st.get("ism_running")),
            ("ADSB", st.get("adsb_scanning") or st.get("adsb_dedicated")),
            ("AIS", st.get("ais_dedicated")),
            ("ACARS", st.get("acars_running")),
            ("APRS", st.get("aprs_running")),
            ("PAGER", st.get("pager_running")),
            ("APT", st.get("apt_mode") or st.get("apt_recording")),
        ) if on]
        d.text((6, 2), "DECODERS", fill=GREEN, font=font(12, bold=True))
        if active:
            for i, line in enumerate(_wrap(" ".join(active), 20)[:3]):
                d.text((6, 22 + i * 16), line, fill="WHITE", font=font(12))
        else:
            d.text((6, 26), "none active", fill=DIM, font=font(12))
    else:
        t = cpu_temp()
        la = load_avg()
        d.text((6, 2), "SYSTEM", fill=GREEN, font=font(12, bold=True))
        d.text((6, 20), primary_ip(), fill="WHITE", font=font(12))
        d.text((6, 38), "%s  %s" % (f"{t:.0f}C" if t is not None else "?C",
                                    f"load {la:.2f}" if la is not None else ""),
               fill=DIM, font=font(11))
        d.text((6, 56), "up %s  npu %s" % (uptime(),
                                           st.get("classifier_backend", "?")),
               fill=DIM, font=font(10))
    return img


def _wrap(text, width):
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


# ── Panels ──

CLASSES = {
    "main": LCD_1inch3.LCD_1inch3,
    "aux0": LCD_0inch96.LCD_0inch96,
    "aux1": LCD_0inch96.LCD_0inch96,
}


class Panels:
    """Only the panels asked for, and only the ones that actually come up.

    A panel is driven per-name rather than as a fixed trio: this HAT stacks over
    the M.2 HAT+, and a panel whose DC/CS line does not make contact is lit but
    blank while its driver reports success. Being able to run a subset keeps the
    node's status visible on whatever hardware is genuinely working.
    """

    def __init__(self, names, freq=10_000_000):
        self.panels = {}
        for name in names:
            cfg = PANELS[name]
            try:
                p = CLASSES[name](
                    spi=spidev.SpiDev(*cfg["spi"]), spi_freq=freq,
                    rst=cfg["rst"], dc=cfg["dc"], bl=cfg["bl"])
                p.Init()
                p.clear()
                self.panels[name] = p
                log.info("panel %s up: spidev%d.%d %dx%d", name, cfg["spi"][0],
                         cfg["spi"][1], p.width, p.height)
            except Exception as exc:
                log.error("panel %s failed to initialise: %s", name, exc)
        if not self.panels:
            raise RuntimeError("no panels initialised")
        self.brightness(BRIGHTNESS[0])

    def __contains__(self, name):
        return name in self.panels

    def get(self, name):
        return self.panels.get(name)

    def brightness(self, pct):
        for p in self.panels.values():
            p.bl_DutyCycle(pct)

    def close(self):
        for p in self.panels.values():
            try:
                p.module_exit()
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser(description="ravenSDR LCD panel driver")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="seconds between refreshes (default 2)")
    ap.add_argument("--socket", default=None, help="radio IPC socket path")
    ap.add_argument("--panels", default="main,aux0,aux1",
                    help="comma-separated panels to drive (default all). Use to "
                         "skip a panel that is lit but not receiving signal.")
    ap.add_argument("--freq", type=float, default=10e6, help="SPI Hz")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    names = [n.strip() for n in args.panels.split(",") if n.strip()]
    bad = [n for n in names if n not in PANELS]
    if bad:
        ap.error("unknown panel(s): %s (choose from %s)"
                 % (", ".join(bad), ", ".join(PANELS)))

    radio = Radio(args.socket or resolve_socket_path())
    panels = Panels(names, freq=int(args.freq))

    state = {"page": 0, "bright": 0}
    cache = {"st": None, "err": None, "at": 0.0}

    def next_page():
        state["page"] = (state["page"] + 1) % len(PAGES)
        log.info("KEY1 -> page %s", PAGES[state["page"]])

    def next_brightness():
        state["bright"] = (state["bright"] + 1) % len(BRIGHTNESS)
        pct = BRIGHTNESS[state["bright"]]
        panels.brightness(pct)
        log.info("KEY2 -> brightness %d%%", pct)

    k1 = Button(KEY1, pull_up=True, bounce_time=0.08)
    k2 = Button(KEY2, pull_up=True, bounce_time=0.08)
    k1.when_pressed = next_page
    k2.when_pressed = next_brightness
    log.info("LCD driver up: KEY1 cycles page, KEY2 cycles brightness")

    # With the big panel present each aux gets a fixed job. Without it, the
    # paged view has to live on an aux or the frequency is never shown at all.
    compact = "main" not in panels
    if compact:
        log.info("main panel not driven — paging the compact view onto an aux")

    try:
        while True:
            # The waterfall redraws ~3x/s but status changes at human speed, so
            # it is cached rather than fetched per frame — otherwise switching to
            # that page would triple the IPC round-trips for nothing.
            now = time.time()
            if now - cache["at"] >= args.interval:
                cache["st"], cache["err"] = radio.status()
                cache["at"] = now
            st, err = cache["st"] or {}, cache["err"]
            page = state["page"]
            rows, last_at = radio.waterfall()
            clf = radio.classification()

            if (p := panels.get("main")) is not None:
                p.ShowImage(draw_main(PANELS["main"]["size"], page, st, err,
                                      rows, last_at, clf))
            if (p := panels.get("aux1")) is not None:
                p.ShowImage(draw_compact(PANELS["aux1"]["size"], page, st, err,
                                         rows, last_at, clf)
                            if compact
                            else draw_health(PANELS["aux1"]["size"], st, err))
            if (p := panels.get("aux0")) is not None:
                p.ShowImage(draw_compact(PANELS["aux0"]["size"], page, st, err,
                                         rows, last_at, clf)
                            if compact and "aux1" not in panels
                            else draw_freq(PANELS["aux0"]["size"], st, err))
            # The waterfall is a live view; polling it at the status interval
            # would show ~1 row in 6. Refresh faster when it is on screen.
            time.sleep(0.35 if page == 1 else args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        panels.close()
        radio.link.stop()


if __name__ == "__main__":
    main()
