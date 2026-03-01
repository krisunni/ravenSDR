# Phase 13: NOAA APT Satellite Imaging

## Overview

NOAA-15 and NOAA-19 transmit live Earth imagery via APT (Automatic Picture Transmission) on 137 MHz as they pass overhead. Each pass lasts ~14 minutes and produces a visible + infrared image strip. ravenSDR auto-schedules recording based on pass predictions, captures the pass with rtl_fm, decodes the APT audio to image using noaa-apt CLI, and displays the decoded Pacific Northwest satellite image in the UI.

## Hardware Context

- RTL-SDR Blog V4 R828D tuner performs well at 137 MHz — better than older R820T2 dongles at this frequency
- Dipole antenna from the V4 kit: extend each element to ~53cm for quarter-wave at 137 MHz
- Frequencies: NOAA-15 at 137.6200 MHz, NOAA-19 at 137.9125 MHz
- NOAA-18 decommissioned June 2025 — do not include
- Recording duration: 900 seconds (15 min) to capture full pass including horizon-to-horizon

## Known Limitations

- Single dongle: APT recording pauses all other ravenSDR monitoring for ~15 min per pass. Document this clearly. Dual dongle mode resolves it (second dongle dedicated to 137 MHz).
- NOAA-15 and NOAA-19 are aging satellites with finite lifespan — no replacement APT satellites planned
- Low-elevation passes (<20°) produce heavily distorted images due to atmosphere — skip these
- Image quality depends heavily on antenna. The included dipole is functional but a turnstile or QFH antenna significantly improves results
- noaa-apt must be installed separately — not available in standard apt repos on Pi OS, may require building from source (Rust toolchain needed)

---

## Tasks

### T074: Create APT pass scheduler with TLE fetching and ephem prediction

**File:** `code/ravensdr/apt_scheduler.py`

Create the APT pass scheduler module:

- Fetch TLE data from Celestrak (`https://celestrak.org/NOAA/elements/noaa.txt`)
- Parse TLE lines for NOAA-15 and NOAA-19 (skip NOAA-18)
- Use `ephem` Python library to predict satellite passes for Redmond WA (47.6740° N, 122.1215° W, elevation 46m)
- Filter passes to only those with max elevation > 20°
- Refresh TLEs every 24 hours (satellites drift)
- Provide `get_next_passes(hours=24)` returning list of pass dicts with satellite name, AOS time, LOS time, max elevation, duration
- Background thread that continuously checks for upcoming passes and triggers recording jobs
- Emit `satellite_pass_upcoming` Socket.IO event 10 minutes before a pass

**Acceptance criteria:**
- TLE data fetched and cached locally with 24h refresh
- Pass predictions accurate for NOAA-15 and NOAA-19
- Passes below 20° max elevation filtered out
- Upcoming pass event emitted 10 minutes before AOS

---

### T075: Create APT decoder with rtl_fm recording and noaa-apt integration

**File:** `code/ravensdr/apt_decoder.py`

Create the APT recording and decoding module:

- Record APT pass using rtl_fm:
  ```
  rtl_fm -f 137.620M -M fm -s 60k -r 11025 -g 40 > pass_recording.wav
  ```
- 11025 Hz sample rate is correct for APT decoding
- Gain 40 as default, make configurable
- Save raw WAV to `/tmp/ravensdr/apt/` with timestamp + satellite name in filename (e.g., `NOAA-19_2026-02-28T1430Z.wav`)
- Recording duration: 900 seconds (15 min)
- Decode using noaa-apt CLI:
  ```
  noaa-apt pass_recording.wav -o decoded_image.png --rotate auto
  ```
- `--rotate auto` corrects for ascending vs descending pass orientation
- Output PNG saved to `code/static/images/apt/`
- Emit `apt_image_ready` Socket.IO event with image URL and metadata (satellite, pass time, max elevation, location)
- Clean up raw WAV after successful decode to save disk space

**Acceptance criteria:**
- rtl_fm recording starts/stops for correct duration at correct frequency
- noaa-apt CLI invoked with correct arguments
- Decoded PNG saved to static images directory
- Socket.IO event emitted with image metadata on decode completion

---

### T076: Add SDR input source APT recording mode

**File:** `code/ravensdr/input_source.py` (modify)

Add APT recording mode to the input source abstraction:

- `enter_apt_mode(frequency_mhz)` — pause normal scanning, dedicate SDR to 137 MHz for pass duration
- `exit_apt_mode()` — resume normal scanning after pass completes
- Lock mechanism to prevent frequency changes during APT recording
- Coordinate with tuner.py to cleanly stop/restart rtl_fm with APT-specific parameters (FM mode, 60k sample rate, 11025 Hz output rate, gain 40)
- Emit status events so UI shows SDR is in satellite recording mode

**Acceptance criteria:**
- Normal scanning pauses cleanly when APT mode engaged
- SDR tunes to correct APT frequency with correct demodulation parameters
- Normal scanning resumes after APT mode exits
- No orphaned rtl_fm processes

---

### T077: Add satellite API routes and Socket.IO events to Flask app

**File:** `code/ravensdr/app.py` (modify)

Add satellite endpoints and events:

- `GET /api/satellite/passes` — returns next 24 hours of predicted passes (satellite name, AOS, LOS, max elevation, duration)
- `GET /api/satellite/latest-image` — returns URL and metadata for most recently decoded APT image
- Socket.IO event `satellite_pass_upcoming` — emitted 10 min before pass (satellite name, AOS time, frequency)
- Socket.IO event `apt_image_ready` — emitted when decode completes (image URL, satellite, pass time, max elevation, location)
- Initialize APT scheduler on app startup

**Acceptance criteria:**
- `/api/satellite/passes` returns valid JSON pass list
- `/api/satellite/latest-image` returns latest decoded image info or 404 if none
- Socket.IO events emitted at correct times
- Scheduler starts with app and runs in background

---

### T078: Create satellite panel JavaScript module

**File:** `code/static/satellite.js`

Create the satellite panel frontend module:

- Pass schedule: list next 5 passes with satellite name, time, max elevation, duration
- Countdown timer to next pass, updating every second
- Active pass: progress bar showing recording progress, "Recording in progress" state indicator
- Latest image: decoded APT image displayed inline with timestamp and satellite name
- Image history: last 5 decoded passes as clickable thumbnails that expand to full view
- Listen for `satellite_pass_upcoming` event to show notification and start countdown
- Listen for `apt_image_ready` event to display new decoded image
- Fetch initial pass schedule from `/api/satellite/passes` on load
- Fetch latest image from `/api/satellite/latest-image` on load

**Acceptance criteria:**
- Pass schedule renders with correct data
- Countdown timer counts down accurately to next pass
- Recording progress bar displays during active pass
- Decoded images display inline and in history thumbnails
- Real-time updates via Socket.IO events

---

### T079: Create satellite panel CSS styles

**File:** `code/static/satellite.css`

Style the satellite panel consistent with existing ravenSDR UI:

- Pass schedule list styling with elevation indicators
- Countdown timer prominent display
- Recording progress bar with animation
- Decoded image display (responsive, maintains aspect ratio)
- Thumbnail grid for image history
- Status indicators for upcoming/active/completed passes
- Match existing color scheme and font choices from ravensdr.css

**Acceptance criteria:**
- Styles consistent with existing ravenSDR panels
- Responsive layout for decoded satellite images
- Visual distinction between pass states (upcoming, active, completed)

---

### T080: Integrate satellite panel into main UI

**Files:** `code/templates/index.html` (modify), `code/static/ravensdr.js` (modify)

Add satellite panel to the main ravenSDR interface:

- Add satellite panel section to `index.html` with container divs for pass schedule, countdown, active recording status, decoded image display, and image history
- Include `satellite.js` and `satellite.css` references
- Integrate satellite panel initialization into `ravensdr.js` startup sequence
- Add satellite tab/section toggle if using tabbed layout

**Acceptance criteria:**
- Satellite panel visible in main UI
- Panel initializes correctly on page load
- No conflicts with existing UI panels

---

### T081: Update setup script for APT dependencies

**File:** `code/setup.sh` (modify)

Add APT satellite imaging dependencies to the setup script:

- Install `python3-ephem` via apt or pip
- Install `noaa-apt`:
  - Check if available via apt package manager
  - If not, build from source (requires Rust toolchain — install via rustup if needed)
  - Document that noaa-apt build may take several minutes on Raspberry Pi
- Create `/tmp/ravensdr/apt/` directory for raw recordings
- Create `code/static/images/apt/` directory for decoded images

**Acceptance criteria:**
- ephem Python library installed and importable
- noaa-apt CLI installed and on PATH
- Required directories created
- Setup script remains idempotent

---

### T082: Write unit tests for APT scheduler and decoder

**Files:** `code/tests/unit/test_apt_scheduler.py`, `code/tests/unit/test_apt_decoder.py`

Test the APT modules:

**apt_scheduler tests:**
- TLE parsing extracts correct NOAA-15 and NOAA-19 data
- Pass prediction returns valid passes for known date/location
- Passes below 20° max elevation are filtered out
- TLE refresh logic triggers after 24 hours
- NOAA-18 is excluded from predictions

**apt_decoder tests:**
- rtl_fm command constructed with correct frequency, sample rate, gain parameters
- noaa-apt CLI command constructed correctly
- Output filename follows expected naming convention
- Socket.IO event payload contains required metadata fields

**Test fixture:**
- Include a sample APT WAV file in `code/tests/fixtures/` for noaa-apt CLI integration testing (or mock the CLI call)

**Acceptance criteria:**
- All unit tests pass
- Pass prediction tested against known historical passes
- CLI command construction verified for both rtl_fm and noaa-apt

---

### T083: Integration test for end-to-end APT pipeline

**File:** `code/tests/integration/test_apt_pipeline.py`

Integration test for the full APT pipeline:

- Test scheduler → recorder → decoder → UI event chain with mocked hardware
- Verify SDR enters and exits APT mode correctly
- Verify decoded image file is created in correct location
- Verify Socket.IO events emitted with correct payloads
- Note: full live integration test requires real satellite pass — document manual test procedure

**Acceptance criteria:**
- Mocked pipeline test passes end-to-end
- SDR mode transitions verified
- File output and event emission verified
- Manual test procedure documented for live pass validation
