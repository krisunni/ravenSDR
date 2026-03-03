# Changelog

## [0.5.1] — 2026-03-03

### Fixed — Audio pipeline & transcription quality

- **Preset squelch not applied**: tuning to a preset ignored its squelch value (always defaulted to 0). Now applies preset squelch before starting rtl_fm
- **Transcription required audio playback**: audio queue `put(timeout=0.5)` blocked the read loop when nobody was listening, starving the transcriber. Changed to non-blocking `put_nowait()` so audio chunks are dropped silently when browser audio isn't playing
- **Audio stream breaks on squelch/gain change**: changing squelch or gain restarts rtl_fm, killing the HTTP audio stream. Frontend now auto-reconnects the stream if audio was playing
- **Whisper hallucination spam**: Whisper produced garbage transcripts on noise/static — `(roaring)`, `[Music]`, `[Groans]`, `[Birds]`, etc. Added two-tier hallucination filter: known phrases + structural pattern matching (bracketed sound descriptions, short fragments, repetitive syllables). All filtered transcripts logged at DEBUG level

### Added

- **Per-mode sample rate config**: `MODE_SAMPLE_RATES` dict in Tuner for mode-specific rtl_fm bandwidth (extensible for AM tuning)
- **Default startup preset**: UI now defaults to Weather tab and auto-tunes NOAA Seattle on page load
- **Cleanup script** (`scripts/cleanup.sh`): kills orphaned rtl_fm, dump1090, and ffmpeg processes

## [0.5.0] — 2026-03-02

### Fixed — RTL-SDR Blog V4 driver & Hailo NPU transcription

- **RTL-SDR Blog V4 driver**: stock Debian `librtlsdr` does NOT support the V4's R828D tuner, causing "PLL not locked" errors on every frequency. Setup script now builds the patched driver from `rtlsdrblog/rtl-sdr-blog` and reinstalls it after dump1090 (which pulls in the stock lib as a dependency)
- **Whisper decode prefix**: Hailo NPU decoder was seeded with only `<|startoftranscript|>`, causing immediate EOS (2/32 tokens). Now seeds with full Whisper prefix: `<|startoftranscript|> <|en|> <|transcribe|> <|notimestamps|>` — decoder now produces 28/32 tokens of real transcription
- **Signal meter flickering**: heartbeat loop emitted `rms: 0` every 500ms, overriding real signal values from the transcriber. Now only emits 0 on stop transition
- **ADS-B scan scheduler eventlet crash**: scheduler used `threading.Thread` causing "Cannot switch to a different thread" greenlet errors. Now uses `eventlet.spawn` and `eventlet.sleep`
- **ADS-B scans interrupting non-aviation presets**: scan scheduler now only activates when tuned to an Aviation preset
- **NOAA Weather presets**: changed from `wbfm` (wideband FM) to `fm` (narrowband) — NOAA Weather Radio is narrowband FM
- **Hailo detection in setup.sh**: `hailortcli fw-control identify` returns non-zero even on success; now also checks output text

### Added — Operations guides & UI improvements

- **Antenna guide** (`operations/antenna-guide.md`): element lengths per band, V-dipole orientation diagrams (vertical/horizontal/flat), positioning guidelines, troubleshooting (PLL fix, weak signal, USB power)
- **System diagram** (`operations/system-diagram.md`): physical setup, software architecture, data flow, ADS-B time-sharing, hardware stack, driver requirements
- **Squelch & Gain tooltips**: info icons with hover descriptions explaining what each control does
- **Satellite panel visibility**: now hidden by default, only shown on Weather tab
- **Audio auto-stop**: playback stops automatically when source stops
- **Setup script**: RTL-SDR Blog V4 driver build from source, dump1090 systemd service disabled (ravenSDR manages it), correct install order (dump1090 before Blog driver)

## [0.4.1] — 2026-03-02

### Fixed — Eventlet subprocess isolation & hardware integration bugs

- **Eventlet subprocess isolation**: tuner, stream_source, and input_source now use `eventlet.patcher.original("subprocess")` and `original("threading")` to get real stdlib modules. Eventlet's green subprocess caused fd conflicts ("Second simultaneous read on fileno"), broken `wait()` timeouts, and orphaned processes
- **NPU inference loop indentation**: mel spectrogram → encoder → decoder → emit block was outside the `for chunk in vad_segments:` loop, causing `UnboundLocalError` and immediate CPU fallback
- **Celestrak TLE URL**: changed to `gp.php?GROUP=weather&FORMAT=tle` (old path returned 404)
- **Shutdown crash**: `shutdown()` now spawns `_do_shutdown()` via `socketio.start_background_task()` to avoid `RuntimeError: do not call blocking functions from the mainloop`
- **SDR detection**: uses `lsusb` to check for RTL2838 USB ID (`0bda:2838`) — works even when dump1090 holds exclusive device access
- **Process cleanup**: tuner/stream_source `stop()` uses `os.kill()` + `os.waitpid()` directly, bypassing eventlet's broken `subprocess.wait()`
- **setup.sh rtl_test zombie**: `timeout --signal=KILL` prevents unkillable `rtl_test` from holding the dongle indefinitely; also stops dump1090 before SDR test
- **LiveATC stream headers**: added `User-Agent` and `Referer` headers to ffmpeg commands (LiveATC blocks headless requests)
- **Duplicate preset**: removed duplicate KUOW-FM entry from presets

## [0.3.0] — 2026-02-28

### Added — Phase 10: ADS-B Aviation Correlation & Voice-Activity Segmentation

- **ADS-B Receiver** (`adsb_receiver.py`): dump1090 process manager with JSON flight poller, single-dongle time-sharing and dual-dongle modes
- **Voice-Activity Segmenter**: silence-boundary audio chunking replaces fixed 10s chunks — no more mid-word splits. Configurable threshold, holdoff, min/max segment duration
- **Callsign Correlator** (`adsb_correlator.py`): regex extraction of airline callsigns (Alaska → ASA), ICAO codes (UAL), and N-numbers from Whisper transcripts, matched against live ADS-B flight list
- **Leaflet.js Map Panel**: real-time aircraft markers with directional icons, callsign tooltips, and 8-second highlight on transcript match
- **REST endpoint**: `GET /api/adsb/flights` returns current aircraft list
- **Socket.IO events**: `adsb_update` (flight list push every 2s), `callsign_match` (transcript correlation)
- **ADS-B 1090 MHz preset**: map-only tracking mode in Aviation category
- **Transcript callsign highlighting**: matched callsigns highlighted in red in the transcript feed
- **Setup**: dump1090-mutability install step in `setup.sh`, `requests` added to requirements.txt
- **Tests**: unit tests for callsign extraction, VAD segmenter, and integration tests for ADS-B receiver

### Changed

- ADS-B enabled by default (single-dongle mode). Set `ADSB_ENABLED=false` to disable
- Transcriber now uses `VoiceActivitySegmenter` in both Hailo NPU and CPU fallback paths
- Map panel auto-shows on Aviation presets when ADS-B is enabled, hidden otherwise

### Config (environment variables)

- `ADSB_ENABLED` — enable ADS-B receiver (default: `true`)
- `ADSB_DUAL_DONGLE` — use dedicated dongle on device 1 (default: `false`)
- `ADSB_SCAN_INTERVAL` — seconds between scan windows in single-dongle mode (default: `60`)
- `ADSB_SCAN_DURATION` — seconds per scan window (default: `30`)

## [0.2.0] — 2026-02-27

- Phases 1–9, 11 implemented
- Hailo-8L NPU inference, faster-whisper CPU fallback
- Flask + Socket.IO backend, vanilla JS frontend
- Frequency presets, error handling, inference stats dashboard
