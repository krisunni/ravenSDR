# Changelog

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
