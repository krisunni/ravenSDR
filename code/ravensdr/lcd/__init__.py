# Panel drivers for the Waveshare Zero LCD HAT (A).
#
# LCD_1inch3.py, LCD_0inch96.py and lcdconfig.py are Waveshare's own demo
# sources (MIT), vendored so the register init sequences come from the vendor
# rather than from us guessing at them. They already use relative imports.
#
# ONE deviation from upstream: lcdconfig.RaspberryPi.__init__ defaulted to
# `spi=spidev.SpiDev(0,0)`, which opens the device in a default argument and so
# runs at IMPORT time — importing the module raised FileNotFoundError whenever
# SPI happened to be off. It now defaults to None; callers pass spi explicitly.
#
#   https://www.waveshare.com/wiki/Zero_LCD_HAT_(A)
#   https://files.waveshare.com/wiki/Zero-LCD-HAT-A/Zero_LCD_HAT_A_Demo.zip
#
# The HAT is three independent SPI panels on two buses. The pinout in the wiki
# is given in PHYSICAL header pins; these are the BCM numbers the code wants:
#
#   panel            controller  res      spidev     DC   RST  BL
#   1.3in main       ST7789      240x240  1.0        22   27   19
#   0.96in aux #1    ST7735S     160x80   0.0         4   24   13
#   0.96in aux #2    ST7735S     160x80   0.1         5   23   12
#
#   KEY1 = BCM 25, KEY2 = BCM 26 (active low, internal pull-up)
#
# Requires `dtparam=spi=on` (spidev0.x) AND `dtoverlay=spi1-1cs` (spidev1.0);
# the main panel is on SPI1, so enabling SPI alone leaves it dark.
#
# gpiozero works here on Pi 5 via its lgpio backend. The vendor's RPi.GPIO
# instructions do NOT — RPi.GPIO cannot drive RP1.

PANELS = {
    "main": {"spi": (1, 0), "dc": 22, "rst": 27, "bl": 19, "size": (240, 240)},
    "aux0": {"spi": (0, 0), "dc": 4, "rst": 24, "bl": 13, "size": (160, 80)},
    "aux1": {"spi": (0, 1), "dc": 5, "rst": 23, "bl": 12, "size": (160, 80)},
}

KEY1 = 25
KEY2 = 26
