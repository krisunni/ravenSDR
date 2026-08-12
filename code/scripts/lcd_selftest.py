#!/usr/bin/env python3
"""Self-test for the Waveshare Zero LCD HAT (A) — one solid colour per panel.

Run this before blaming the panel driver: it exercises each panel independently
and reports per-panel failures instead of letting the first one hide the rest.

    python3 code/scripts/lcd_selftest.py            # 10 MHz, hold 60s
    python3 code/scripts/lcd_selftest.py --freq 1e6 # marginal-contact check

Expected: main=RED, aux0=GREEN, aux1=BLUE, each labelled.

The process holds the panels for --hold seconds and only then exits, because
gpiozero releases every pin on exit — which drops the backlight and leaves the
panels dark even though the image is still in each controller's RAM. A one-shot
script that "worked" will still look like it did nothing.

A panel that stays lit but blank has power and backlight (BL reaches it) while
its DC/RST/SPI path does not — on a stacked HAT that usually means an uneven
seat rather than a dead board.
"""
import argparse
import os
import sys
import time
import traceback

import spidev
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ravensdr.lcd import LCD_0inch96, LCD_1inch3      # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

# name, class, (spi bus, device), rst, dc, bl, colour
SPEC = [
    ("main 1.3in ", LCD_1inch3.LCD_1inch3, (1, 0), 27, 22, 19, "RED"),
    ("aux0 0.96in", LCD_0inch96.LCD_0inch96, (0, 0), 24, 4, 13, "GREEN"),
    ("aux1 0.96in", LCD_0inch96.LCD_0inch96, (0, 1), 23, 5, 12, "BLUE"),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--freq", type=float, default=10e6, help="SPI Hz (default 10e6)")
    ap.add_argument("--hold", type=int, default=60, help="seconds to hold (default 60)")
    ap.add_argument("--only", choices=["main", "aux0", "aux1"],
                    help="drive a single panel, in isolation")
    args = ap.parse_args()

    if not os.path.exists("/dev/spidev0.0"):
        print("no /dev/spidev0.0 — SPI is off. Need dtparam=spi=on", file=sys.stderr)
    if not os.path.exists("/dev/spidev1.0"):
        print("no /dev/spidev1.0 — the 1.3in panel is on SPI1. "
              "Need dtoverlay=spi1-1cs", file=sys.stderr)

    live, failed = [], []
    for name, cls, (bus, dev), rst, dc, bl, colour in SPEC:
        if args.only and not name.startswith(args.only):
            continue
        try:
            p = cls(spi=spidev.SpiDev(bus, dev), spi_freq=int(args.freq),
                    rst=rst, dc=dc, bl=bl)
            p.Init()
            p.bl_DutyCycle(100)
            img = Image.new("RGB", (p.width, p.height), colour)
            d = ImageDraw.Draw(img)
            big = p.width > 200
            d.text((8, 8), name.strip().split()[0].upper(), fill="WHITE",
                   font=ImageFont.truetype(FONT, 22 if big else 14))
            d.text((8, 40 if big else 30), f"{p.width}x{p.height}", fill="WHITE",
                   font=ImageFont.truetype(FONT, 18 if big else 12))
            p.ShowImage(img)
            print(f"  {name}  spidev{bus}.{dev}  dc={dc:<3} rst={rst:<3} "
                  f"bl={bl:<3} -> {colour}")
            live.append(p)
        except Exception:
            print(f"  {name}  spidev{bus}.{dev}  FAILED")
            traceback.print_exc()
            failed.append(name)

    if not live:
        print("no panels initialised", file=sys.stderr)
        return 1

    print(f"\nholding {args.hold}s at {args.freq/1e6:.1f} MHz — look at the panels")
    print("a panel that is lit but blank is seated for power, not for signal")
    try:
        time.sleep(args.hold)
    except KeyboardInterrupt:
        pass
    finally:
        for p in live:
            try:
                p.module_exit()
            except Exception:
                pass
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
