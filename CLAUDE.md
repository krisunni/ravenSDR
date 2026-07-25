# ravenSDR — Project Instructions for Claude

## What is this project?
Passive RF intelligence collection node — auto-scans unencrypted VHF/UHF frequencies, transcribes with on-device Whisper (Hailo-8L NPU), correlates with ADS-B flight data, geo-tags with GPS, logs structured intelligence output. No cloud dependency, fully air-gapped capable.

## Architecture Summary
- **Two input modes:** SDR (rtl_fm) and Web Stream (ffmpeg), unified behind `InputSource` abstraction
- **Transcription:** Whisper on Hailo-8L NPU, with faster-whisper CPU fallback
- **Backend:** Flask + Flask-SocketIO (Python 3.11+)
- **Frontend:** Vanilla JS + Web Audio API + Socket.IO client
- **Target hardware:** Raspberry Pi 5, Hailo AI Hat, RTL-SDR Blog V4

## Directory Structure
```
ravenSDR/
├── .state/                    # Project state JSON files
├── architecture/              # System design docs
│   ├── overview.md
│   └── design.md             # Primary technical reference
├── components/                # Component documentation stubs
├── features/                  # Feature documentation stubs
├── implementation-plans/      # Phase-by-phase implementation plans
├── templates/                 # Document templates
├── operations/                # Deployment runbook
├── dashboard/                 # Status dashboards (HTML)
├── code/                      # Source code
│   ├── ravensdr/              # Python package
│   │   ├── app.py            # Flask app, routes, Socket.IO
│   │   ├── input_source.py   # InputSource abstraction
│   │   ├── tuner.py          # RTL-FM process manager
│   │   ├── stream_source.py  # ffmpeg web stream ingest
│   │   ├── transcriber.py    # Hailo Whisper wrapper
│   │   ├── audio_router.py   # HTTP audio streaming
│   │   └── presets.py        # Frequency presets
│   ├── static/               # JS + CSS
│   ├── templates/            # HTML templates
│   ├── tests/                # Unit + integration tests
│   ├── scripts/              # Utility scripts
│   ├── requirements.txt
│   └── setup.sh              # System dependency installer
└── CLAUDE.md                 # This file
```

## Key Files
- **Technical design:** `architecture/design.md` — full component specs, API routes, Socket.IO events
- **State tracking:** `.state/system.json` — phases, status, stack info
- **Implementation plans:** `implementation-plans/phase-{1-9}-*.md`

## Stack
- Python 3.11+, Flask, Flask-SocketIO, eventlet
- numpy, faster-whisper (CPU fallback)
- Hailo SDK (NPU inference)
- rtl-sdr, ffmpeg (audio sources)
- Vanilla JS, Socket.IO client, Web Audio API

## Common Commands
```bash
# Development (no hardware needed)
python3 code/ravensdr/app.py

# Full setup on Raspberry Pi
bash code/setup.sh
pip install -r code/requirements.txt
python3 code/ravensdr/app.py

# Tests
pytest code/tests/unit/
pytest code/tests/integration/
```

## Running 24x7 (deployed node)
ravenSDR runs as a systemd unit, `ravensdr.service` (enabled at boot,
`Restart=always`). Do not launch a second copy alongside it — both will fight
for the RTL-SDR dongle (`usb_claim_interface error -6`).

```bash
sudo systemctl restart ravensdr   # after code changes
./code/scripts/start.sh           # start (wraps systemctl)
./code/scripts/stop.sh            # stop
./code/scripts/logs.sh            # follow logs (errors | sat | today)
journalctl -u ravensdr -f         # same, raw
```

Logs go to journald, **not** `ravensdr.log`. On startup the app auto-tunes the
last preset tuned from the UI (`code/ravensdr/config.json` → `last_preset`);
pin one with `startup.default_preset`, or set `startup.auto_tune: false` to
start idle.

## Implementation Phases
1. System Dependencies & Environment Setup
2. Input Source Abstraction (SDR + Web Stream)
3. Transcriber (Hailo Whisper)
4. Audio Router (HTTP Streaming)
5. Backend API (Flask App)
6. Frontend (Console UI)
7. Frequency Presets
8. Error Handling & Edge Cases
9. Setup Script & Requirements
