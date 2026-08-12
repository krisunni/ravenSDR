# ravenSDR — Deployment Runbook

## Target Environment
- Raspberry Pi 5 running Raspberry Pi OS (Bookworm, 64-bit)
- Hailo AI Hat (Hailo-8L, 13 TOPS)
- RTL-SDR Blog V4 (R828D tuner)

## Deployment Steps

1. Clone repository to Pi
2. Run `code/setup.sh` to install system dependencies
3. Create venv: `python3 -m venv venv && source venv/bin/activate`
4. Install Python deps: `pip install -r code/requirements.txt`
5. Verify SDR: `rtl_test -t`
6. Verify Hailo: `hailortcli fw-control identify`
7. Start app: `python3 code/ravensdr/app.py`
8. Open http://localhost:5000

## Development (No Hardware)

1. Install Python deps (skip setup.sh)
2. Run `python3 code/ravensdr/app.py`
3. App auto-detects no SDR, starts in Web Stream mode
4. Select NOAA Monterey preset for testing

## LCD panel (Waveshare Zero LCD HAT (A))

Three SPI panels — one 1.3in ST7789 (240x240) and two 0.96in ST7735S (160x80) —
plus two buttons. The screen driver is a **separate process** and a peer of the
web app: both are clients of the radio daemon over `radio.sock`, and neither
touches the SDR.

```
ravensdr.service       radio daemon   owns SDR/Hailo, serves radio.sock
ravensdr-ui.service    web app        RadioLink client
ravensdr-lcd.service   screen driver  RadioLink client
```

A wedged SPI write or a yanked HAT therefore restarts only the screen.

### Enabling SPI (required, and easy to lose)

```
dtparam=spi=on        # spidev0.0 / 0.1 — the two 0.96in panels
dtoverlay=spi1-1cs    # spidev1.0     — the 1.3in main panel
```

Both live in `/boot/firmware/config.txt`. **The main panel is on SPI1**, so with
`spi=on` alone it stays dark no matter what the driver does. These can be applied
without rebooting (`sudo dtparam spi=on; sudo dtoverlay spi1-1cs`) but a runtime
overlay does **not** survive a power cycle — a panel that "stopped working after
a reboot" is usually this, so check `ls /dev/spidev*` first.

### Pinout (BCM — the wiki gives PHYSICAL pins)

| panel | controller | res | spidev | DC | RST | BL |
|---|---|---|---|---|---|---|
| 1.3in main | ST7789 | 240x240 | 1.0 | 22 | 27 | 19 |
| 0.96in aux0 | ST7735S | 160x80 | 0.0 | 4 | 24 | 13 |
| 0.96in aux1 | ST7735S | 160x80 | 0.1 | 5 | 23 | 12 |

KEY1 = BCM 25, KEY2 = BCM 26 (active low). KEY1 cycles the page, KEY2 cycles
brightness (100 → 30 → off).

`gpiozero` drives this on a Pi 5 through its lgpio backend. The vendor's
`RPi.GPIO` instructions do **not** work — RPi.GPIO cannot drive RP1. Use the apt
packages (`python3-lgpio python3-spidev python3-gpiozero python3-pil`), not the
venv: `lgpio` has no prebuilt wheel for this platform, which is why the unit runs
`/usr/bin/python3`.

### Running

```bash
sudo cp code/scripts/ravensdr-lcd.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ravensdr-lcd

# by hand — must run from code/, or PYTHONPATH must point at it
cd code && python3 -m ravensdr.lcd_panel --interval 2
python3 -m ravensdr.lcd_panel --panels aux1     # skip panels that do not work
```

### Diagnosing a blank panel

```bash
python3 code/scripts/lcd_selftest.py              # main=RED aux0=GREEN aux1=BLUE
python3 code/scripts/lcd_selftest.py --only main --freq 1e6
```

The driver **must stay resident**: gpiozero releases every pin at process exit,
which drops the backlight and leaves the panel dark even though the image is
still in the controller's RAM. A one-shot script that worked still looks like it
did nothing — this is the single most misleading failure here.

Read the symptom this way:

- **No backlight at all** → the pin is not reaching the HAT, or nothing is driving it.
- **Backlight on, nothing drawn** → power reaches the panel but signal does not.
  If the driver also reports no exception, suspect the DC/CS wiring rather than
  the code. Verify the Pi side first: `pinctrl get 7,8,18,20,21` should show
  GPIO20/21 as `SPI1_MOSI`/`SPI1_SCLK`, and the CS pins read low *during* a
  transfer. If all that is right and an identical panel on the same bus works,
  the fault is between the header and the panel — try mounting the HAT directly
  on the 40-pin header instead of stacked over the M.2 HAT+.
- Clock rate is worth one try (`--freq 1e6`), but it only explains marginal
  contact; an open circuit fails identically at every speed.
