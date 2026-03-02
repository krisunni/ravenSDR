# Antenna Setup Guide — RTL-SDR Blog V4

## Kit Contents

- RTL-SDR Blog V4 dongle (SMA female connector)
- Telescoping dipole antenna (two extendable elements)
- Dipole base (V-mount with SMA male)
- SMA extension cable
- Suction cup window mount
- Flexible tripod

---

## Antenna Length by Frequency Band

The dipole elements must be adjusted to quarter-wavelength for the target frequency.

| Band | Frequency | Each Element Length | Mode |
|------|-----------|-------------------|------|
| FM Broadcast | 88–108 MHz | **75 cm** (fully extended) | wbfm |
| Aviation VHF | 118–136 MHz | **58 cm** | am |
| Marine VHF | 156–162 MHz | **47 cm** | fm |
| NOAA Weather | 162.4–162.55 MHz | **46 cm** | fm |
| Public Safety | 150–170 MHz | **45–50 cm** | fm |
| ADS-B | 1090 MHz | **6.9 cm** (almost fully collapsed) | adsb |
| NOAA APT Satellite | 137 MHz | **55 cm** | fm |

**Formula:** Element length (cm) = 7500 / frequency (MHz)

---

## Dipole Setup

1. Screw the two telescoping elements into the dipole base (V-mount)
2. Adjust each element to the correct length for your target band (see table above)
3. Spread the elements into a **V shape** (~120 degrees apart)
4. Connect the SMA cable from the dipole base to the RTL-SDR dongle
5. Mount using the suction cup (window) or flexible tripod (desk/shelf)

---

## Vertical vs Horizontal Orientation

The dipole orientation must match the signal polarization. Getting this wrong
can reduce signal strength by 20 dB or more (100x weaker).

### Vertical (Aviation, Marine, Weather, Public Safety, ADS-B)

Most two-way radio signals are vertically polarized. Orient the V-dipole
so the elements point **up and out** like a "V" standing upright:

```
        |         |
        |         |        ← elements pointing UP
   58cm |         | 58cm      (adjust length per band)
        |         |
         \       /
          \     /
           \   /
            [V-mount]      ← mount on tripod or suction cup
              |
           [cable]
              |
          [RTL-SDR V4]
              |
          [Raspberry Pi]
```

Place near a window, as high as possible. For aviation, face the window
toward the airport if you can.

### Horizontal (FM Broadcast)

FM broadcast stations transmit with horizontal polarization. Lay the
V-dipole on its side so the elements spread **left and right**:

```
                  ___  [suction cup on window]
                 /
    ← 75cm →  [V-mount]  ← 75cm →
              /           \
   __________/             \__________
   element L                element R

         [cable hangs down]
              |
          [RTL-SDR V4]
              |
          [Raspberry Pi]
```

Alternatively, lay the dipole flat on a windowsill with elements
spreading out to each side. The key is that both elements are
**parallel to the ground**, not pointing up.

### Flat / Ground Plane (General Purpose)

If you're switching between bands frequently, laying the dipole flat
on a desk or the floor with elements in a V works as a compromise:

```
         (top view — looking down)

              \       /
     element   \     /   element
                \   /
              [V-mount]
                 |
              [cable to dongle below]
```

This gives partial reception of both vertical and horizontal signals.
Not optimal for either, but convenient when experimenting.

---

## Positioning Guidelines

### FM Broadcast (88–108 MHz)
- **Orientation:** Horizontal (FM is horizontally polarized)
- **Position:** Window-mounted with suction cup, or flat on windowsill
- **Element length:** 75 cm each (fully extended)
- **Range:** 30–80 km typical, 100+ km with good line of sight
- **Tips:** Extend elements fully. Higher = better. Avoid metal window frames.

### Aviation VHF (118–136 MHz)
- **Orientation:** Vertical (aviation comms are vertically polarized)
- **Position:** Window or outdoor, as high as possible, airport-facing
- **Element length:** 58 cm each
- **Range:** 30–100 km depending on aircraft altitude
- **Tips:** ATIS broadcasts continuously — good for testing. Tower/Approach
  are intermittent (pilots transmit, then silence).

### ADS-B (1090 MHz)
- **Orientation:** Vertical
- **Position:** Outdoors, rooftop or attic is ideal. Window works but cuts range.
- **Element length:** 6.9 cm each (almost fully collapsed)
- **Range:** 50–150 km outdoors, 20–50 km at a window
- **Tips:** The included dipole is NOT ideal for 1090 MHz. A dedicated ADS-B
  antenna will dramatically improve range (see Recommended Upgrades below).

### Marine VHF (156–162 MHz)
- **Orientation:** Vertical
- **Position:** Elevated, waterfront-facing if possible
- **Element length:** 47 cm each
- **Range:** 20–60 km (line of sight over water is excellent)

### NOAA Weather Radio (162 MHz)
- **Orientation:** Vertical
- **Position:** Window-mounted
- **Element length:** 46 cm each
- **Range:** 60–100 km from NOAA transmitter

### NOAA APT Satellite (137 MHz)
- **Orientation:** Horizontal or V-dipole tilted ~30° for sky coverage
- **Position:** Outdoors with clear sky view, away from buildings
- **Element length:** 55 cm each
- **Range:** Satellite passes overhead (horizon to horizon)
- **Tips:** A purpose-built V-dipole or QFH antenna dramatically improves APT reception.

---

## Recommended Antenna Upgrades

| Use Case | Antenna | Approx Cost | Improvement |
|----------|---------|-------------|-------------|
| ADS-B | RTL-SDR Blog ADS-B antenna | $10 | 3–5x range over dipole |
| ADS-B | FlightAware 1090 MHz + filtered LNA | $40 | Best performance |
| NOAA APT | V-dipole (137 MHz, DIY) | $5 | Much better satellite images |
| NOAA APT | QFH antenna (DIY or purchased) | $20–40 | Best satellite reception |
| General VHF/UHF | Discone antenna | $30–50 | Wideband, all frequencies |

---

## Dual-Dongle Setup (Recommended for ADS-B)

For simultaneous ADS-B tracking + voice monitoring, use two RTL-SDR dongles:

```
[Dongle 0] ── VHF dipole antenna ──── Voice (aviation, marine, weather)
[Dongle 1] ── ADS-B 1090 antenna ──── ADS-B aircraft tracking (dump1090)
```

Connect both to the Raspberry Pi via USB. Set environment variables:

```bash
ADSB_ENABLED=true ADSB_DUAL_DONGLE=true python3 code/ravensdr/app.py
```

With a single dongle, ravenSDR time-shares between voice and ADS-B scanning (60s voice, 30s ADS-B by default).

---

## Troubleshooting

### "PLL not locked" errors (no audio, only static)

The **stock Debian rtl-sdr driver does NOT support the RTL-SDR Blog V4** (R828D tuner).
The tuner PLL (Phase-Locked Loop) fails to lock on any frequency, producing only noise.

**Fix:** Install the RTL-SDR Blog patched driver (setup.sh does this automatically):

```bash
sudo apt remove -y librtlsdr-dev librtlsdr0
git clone https://github.com/rtlsdrblog/rtl-sdr-blog.git
cd rtl-sdr-blog && mkdir build && cd build
cmake ../ -DINSTALL_UDEV_RULES=ON
make && sudo make install && sudo ldconfig
sudo cp ../rtl-sdr.rules /etc/udev/rules.d/
```

**Verify fix:** `rtl_test -t` should show "RTL-SDR Blog V4 Detected" with no PLL errors.

### Weak Signal

1. **Check SMA connection** — must be finger-tight, no wobble
2. **Correct element length** — use the table above for your frequency
3. **Try manual gain** — switch from Auto to 40–50 dB in the UI
4. **Move to a window** — walls attenuate VHF/UHF significantly
5. **Avoid USB 3.0 ports** — USB 3.0 generates RF interference near the SDR. Use a USB 2.0 port or a USB extension cable to separate the dongle from the Pi
6. **Use the extension cable** — keep the dongle away from the Pi and other electronics
7. **Check for local interference** — LED lights, switching power supplies, and monitors generate RF noise
8. **USB power** — if other USB devices are connected (ethernet adapters, etc.), they may starve the SDR of power. Try a powered USB hub or disconnect other devices to test.
