# ravenSDR — Technical Design Document

## 1. Overview

ravenSDR is a real-time RF signal transcription pipeline that tunes an RTL-SDR dongle to preset emergency/monitoring frequencies, streams demodulated audio to the browser, and transcribes radio chatter to text using the Raspberry Pi AI Hat (Hailo-8L NPU) running OpenAI Whisper.

When no SDR hardware is present, ravenSDR falls back to **Web Stream Mode** — pulling live audio from public internet streams through the same transcription pipeline.

### Target Hardware
- **SBC:** Raspberry Pi 5
- **NPU:** Hailo AI Hat (Hailo-8L, 13 TOPS)
- **SDR:** RTL-SDR Blog V4 (R828D tuner, RTL2832U, 1PPM TCXO, Bias Tee, SMA, USB)

### Target OS
Raspberry Pi OS (Bookworm, 64-bit)

---

## 2. Tech Stack

| Layer | Technology |
|---|---|
| SDR demodulation (Mode A) | `rtl_fm` (part of `rtl-sdr` package) |
| Web stream ingest (Mode B) | `ffmpeg` subprocess — decodes MP3/AAC → raw PCM |
| Input abstraction | `InputSource` class — unified PCM queue for both modes |
| SDR auto-detection | `rtl_test` subprocess on startup; fallback to Mode B |
| NPU inference | Hailo-8L via `hailo-apps` Python SDK |
| Speech-to-text model | Whisper `tiny` or `base` (.hef compiled for Hailo) |
| CPU fallback | `faster-whisper` (CTranslate2-based) |
| Backend | Python 3.11+, Flask, Flask-SocketIO |
| Audio routing | ALSA loopback (`snd-aloop` kernel module) |
| Audio streaming | HTTP chunked response (WAV/PCM over HTTP) |
| Frontend | Single-file HTML + Vanilla JS + Web Audio API |
| Real-time comms | Socket.IO (WebSocket) |
| Process management | Python `subprocess` with threading |

---

## 3. Component Design

### 3.1 Tuner (`tuner.py`)

RTL-FM process manager for SDR mode.

**Properties:**
- `current_freq` — active frequency string (e.g. `"162.550M"`)
- `current_mode` — demodulation mode: `"fm"`, `"am"`, `"wbfm"`, `"usb"`, `"lsb"`
- `squelch` — integer 0–100 (maps to rtl_fm `-l` flag)
- `gain` — integer or `"auto"` (maps to rtl_fm `-g` flag)
- `is_running` — bool

**Methods:**
- `tune(freq, mode)` — kills existing process, starts new rtl_fm
- `stop()` — SIGTERM, then SIGKILL after 1s
- `set_squelch(level)` — updates and retunes
- `set_gain(value)` — updates and retunes
- `_read_loop()` — background thread; reads 4096-byte chunks from stdout, pushes to both queues

**rtl_fm command:**
```bash
rtl_fm -f {freq} -M {mode} -s 200k -r 16k -l {squelch} -g {gain} -
```

### 3.2 StreamSource (`stream_source.py`)

Web stream ingest via ffmpeg for Mode B.

**ffmpeg command:**
```bash
ffmpeg -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 \
  -i {stream_url} -vn -acodec pcm_s16le -ar 16000 -ac 1 -f s16le pipe:1
```

- Output: raw 16kHz mono 16-bit PCM to stdout (identical format to rtl_fm)
- `_read_loop()` reads 4096-byte chunks into shared `pcm_queue`
- Reconnect flags handle stream drops; after 3 retries, emit Socket.IO error event

### 3.3 InputSource (`input_source.py`)

Unified abstraction over Tuner and StreamSource.

```python
class InputSource:
    def __init__(self, mode: str):   # "SDR" or "WEBSTREAM"
        self.mode = mode
        self.pcm_queue = queue.Queue(maxsize=200)
        self._source = Tuner() if mode == "SDR" else StreamSource()

    def tune(self, preset: dict): ...
    def stop(self): ...
    @property
    def is_running(self) -> bool: ...
```

**Auto-detection:**
```python
def detect_sdr() -> bool:
    result = subprocess.run(["rtl_test", "-t"], capture_output=True, timeout=5)
    return result.returncode == 0
```

### 3.4 Transcriber (`transcriber.py`)

Hailo Whisper wrapper with silence detection and CPU fallback.

**Whisper input requirements:**
- Sample rate: 16,000 Hz
- Bit depth: 16-bit signed PCM
- Channels: mono
- Chunk size: ~32,000 samples (2 seconds) minimum

**Silence detection:**
```python
SILENCE_THRESHOLD = 500   # RMS value
CHUNK_SAMPLES = 48000     # 3 seconds at 16kHz

def is_signal_present(pcm_bytes):
    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    rms = np.sqrt(np.mean(samples.astype(np.float32)**2))
    return rms > SILENCE_THRESHOLD
```

**Transcript output format:**
```python
{
    "timestamp": "14:32:01",
    "freq": "162.550 MHz",
    "label": "NOAA Seattle",
    "text": "...wind northwest at 12 knots...",
    "rms": 1842.3
}
```

**Fallback:** If `HAILO_AVAILABLE = False`, use `faster-whisper` CPU inference with the same `tiny` model.

### 3.5 Audio Router (`audio_router.py`)

HTTP audio streaming endpoint.

- Reads raw PCM from `audio_pipe` queue
- Wraps in WAV container headers (streaming WAV with `0xFFFFFFFF` size)
- Serves as chunked HTTP response at `/audio-stream`

```python
@app.route("/audio-stream")
def audio_stream():
    def generate():
        yield make_wav_header()
        while True:
            chunk = audio_pipe.get(timeout=5)
            yield chunk
    return Response(stream_with_context(generate()), mimetype="audio/wav")
```

### 3.6 Flask App (`app.py`)

**REST Routes:**

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | Serve the single-page UI |
| GET | `/api/presets` | Return JSON list of all frequency presets |
| POST | `/api/tune` | Switch to a frequency |
| POST | `/api/stop` | Stop audio source |
| POST | `/api/squelch` | Update squelch level |
| POST | `/api/gain` | Update gain |
| GET | `/api/status` | Return current state JSON |
| GET | `/audio-stream` | Chunked WAV audio stream |

**Socket.IO Events (Server → Client):**

| Event | Payload | Description |
|---|---|---|
| `transcript` | `{timestamp, freq, label, text, rms}` | New transcription segment |
| `status` | `{running, freq, label, mode, squelch, gain}` | State change broadcast |
| `signal_level` | `{rms, freq}` | Emitted every 500ms |
| `mode` | `{mode, sdr_available}` | Input mode on connect/change |
| `error` | `{message}` | Error notifications |

**Thread Model:**
1. Flask/SocketIO main thread (HTTP + WebSocket)
2. `tuner._read_loop()` — reads subprocess stdout
3. `transcriber._inference_loop()` — runs Whisper on PCM chunks
4. `signal_meter_loop()` — samples RMS every 500ms

### 3.7 Frontend (`index.html` + `ravensdr.js` + `ravensdr.css`)

Single-page console. Twelve panels once shared one vertical scroll, which put
the classifier — where the node now spends most of its effort — below the fold
and the decoders somewhere past that. They are grouped into six **views**,
switched by a sticky tab bar. The preset selector sits above the tabs and stays
visible in every view, because tuning is how you drive the radio regardless of
what you are looking at.

| View | Panels |
|---|---|
| Listen | status strip (tuned / signal / audio), transcript, weather, inference stats, controls, advanced |
| Classify | signal classification, emitter tracking (SEI) |
| Decoders | ADS-B, ISM, APRS, ACARS, pager |
| Imagery | NOAA APT satellite, WEFAX |
| Science | meteor scatter |
| Model | active model, trust, collection, training corpus by class |

Behaviour:
- **View memory** — the active view is stored in `localStorage`; a refresh
  returns you to it rather than the top of the page.
- **Follow the radio** — an *explicit* tune switches to the view that preset
  feeds (ADS-B → Decoders, WEFAX → Imagery, meteor → Science). Page load never
  does this: it would override the view the operator had open.
- **Tab badges** — live aircraft count on Decoders, last modulation on Classify,
  `REC` on Model while the IQ collector is capturing.
- **Empty states** — a view whose panels are all hidden explains which preset
  fills it instead of rendering blank.

Components: **PresetSelector** (category tabs, preset grid), **SignalMeter**,
**AudioPlayer**, **TranscriptFeed**, **ControlBar**, **ClassifierPanel**,
**ModelView**.

Typography splits monospace for data (frequencies, counts, callsigns) from a UI
sans for labels and prose. Layout is verified at 1440/1280/834/390/360px by
`code/scripts/ui_snapshot.py` — see `code/scripts/README-ui-snapshot.md` for why
it snapshots the server rather than browsing it.

### 3.8 Presets (`presets.py`)

**Schema:**
```python
{
    "id": str,           # unique slug
    "label": str,        # display name
    "freq": str,         # rtl_fm format
    "mode": str,         # "fm", "am", "wbfm", "usb", "lsb"
    "category": str,     # "weather", "aviation", "marine", "public_safety", "broadcast"
    "squelch": int,      # preset-specific override (optional)
    "stream_url": str,   # web stream URL (optional)
    "note": str,         # display note (optional)
}
```

**Categories:** Weather (3), Aviation (5), Marine (2), Public Safety (2), Broadcast (2)

---

## 3.9 Process Architecture — UI / Radio Split

### Why

The console used to die with the hardware. Everything ran in one eventlet
process, and eventlet's hub is cooperatively scheduled on a single OS thread: any
*blocking* call — a read on an `rtl_fm` pipe, a Hailo inference, a decoder's
stdout — stalls the hub, which means every HTTP request and Socket.IO frame stops
until it returns. Observed failures:

- A meteor detection did a blocking pipe read from a green thread; the entire web
  UI froze the instant a meteor was detected.
- Overlapping `/api/tune` requests raced on the dongle, orphaning an `rtl_fm`
  that then held the device so every later tune, APT capture, and piped decoder
  failed with `usb_claim_interface error -6`.
- A Hailo driver fault took down inference, and with it the process serving the
  console.

The SDR is separate hardware and is treated as such: it is *commanded*, it
reports its *actual* state, and it is allowed to fail without taking the operator
interface with it.

### Topology

```
  ravensdr-ui.service                      ravensdr-radio.service
  ┌───────────────────────────┐            ┌──────────────────────────────┐
  │ Flask + Socket.IO         │   NDJSON   │ SdrArbiter (queue of one)    │
  │ static assets, templates  │◄──────────►│ tuner / rtl_fm, decoders     │
  │ RadioLink (auto-reconnect)│   unix     │ Hailo Whisper, schedulers    │
  │ owns NO hardware          │   socket   │ real OS threads, NO eventlet │
  └───────────────────────────┘            └──────────────────────────────┘
        /run/ravensdr/radio.sock  (commands + events)
        /run/ravensdr/audio.sock  (PCM for /audio-stream)
```

The UI process owns no hardware and never blocks on it. The radio process is
deliberately **eventlet-free**, so a blocking read there cannot freeze anything a
browser talks to.

### IPC protocol (`ipc.py`)

Newline-delimited JSON over a Unix domain socket — trivially framed, greppable,
no schema compiler on a Pi. Three message kinds:

| Kind | Direction | Shape |
|---|---|---|
| `req` | UI → radio | `{"t":"req","id":7,"cmd":"tune","args":{…}}` |
| `res` | radio → UI | `{"t":"res","id":7,"ok":true,"data":{…}}` |
| `ev` | radio → UI | `{"t":"ev","name":"status","data":{…}}` (unsolicited) |

`ev` is what keeps the console live: the radio pushes status / transcript /
detection events and the UI relays them to browsers over Socket.IO.

`FrameBuffer` reassembles frames split across `recv()` boundaries and caps a
single frame at 4 MB so a desynced stream can't consume unbounded memory.
`CommandRegistry.dispatch` converts a raising handler into an error response, so
one bad command never drops the connection.

**Sockets must be `shutdown()` before `close()`.** While another thread is
blocked in `recv()` on the same fd, the kernel keeps the socket alive and sends
no FIN — the peer would go on believing the link is healthy. Without `shutdown()`
a radio process that exits leaves every UI reporting LINK UP forever.

### SDR arbiter (`sdr_arbiter.py`)

Switching the dongle takes ~1–2 s (SIGTERM `rtl_fm`, wait for the kernel to
release the USB interface, respawn). HTTP requests arrive in milliseconds. The
arbiter is a **queue of one**, applied by exactly one worker, never concurrently:

- **Serialized** — overlapping stop/start paths can no longer interleave and
  orphan a process that holds the device.
- **Coalescing** — while a switch is in flight a newer command *replaces* any
  older pending one. Five rapid clicks move the hardware once, to the final
  target, instead of grinding through every intermediate preset.
- **Observable** — it is the single source of truth for SDR state.

States are command-and-control: `LOCKED` (actual == commanded), `SWITCHING`
(actual != commanded, transition in progress), `FAULT` (last command failed;
`actual` is the last confirmed state, not the requested one).

### Link state as telemetry

`RadioLink` reports `UP`/`DOWN` with reconnect count and last error, and
reconnects with exponential backoff (0.5 s → 10 s). The console renders link
state next to SDR state so an operator can distinguish *"the radio says nothing
is tuned"* from *"I cannot reach the radio at all"*. Commands issued while the
link is down fail fast with that reason rather than hanging an HTTP handler.

### Failure modes

| Failure | Behaviour |
|---|---|
| Radio service stopped / restarting | UI loads and renders last-known state; LINK DOWN; commands rejected with reason; auto-reconnects |
| UI service stopped | Radio keeps receiving, decoding, transcribing, and recording satellite passes |
| Radio crashes mid-command | In-flight command fails fast ("link dropped mid-command"); no hang |
| UI started before radio | UI connects on its own once the radio is up |
| Unclean exit leaves socket file | Server unlinks the stale path before bind, so no spurious `EADDRINUSE` |
| Slow/wedged UI client | Radio drops that client on send failure; never blocks the hardware |

---

## 4. Error Handling

| Scenario | Response |
|---|---|
| SDR not connected | Emit error event, UI banner, poll every 10s, auto-recover |
| rtl_fm crash | Monitor with process.poll(), emit error, expose retry button |
| Orphaned rtl_fm holding the dongle | Tuner tracks every spawned pid and kills the whole set on stop; orphan reap is logged as a warning |
| Overlapping tune requests | Serialized and coalesced by `SdrArbiter`; superseded commands never touch hardware |
| Radio process unreachable | UI stays up, reports LINK DOWN, fails commands fast with the reason |
| Audio stream drop | Browser resets src after 2s delay |
| ALSA loopback missing | Warn on startup |
| Hailo NPU absent | Auto-fallback to faster-whisper CPU, show "CPU mode" badge |
| Web stream offline | ffmpeg reconnect flags, error after 3 retries |

---

## 5. Known Limitations

| Limitation | Notes |
|---|---|
| One frequency at a time | RTL-SDR can only tune one freq |
| Encrypted P25 | No workaround for encrypted channels |
| Whisper accuracy on noisy radio | Consider RNNoise denoising pass |
| Audio latency | ~3–5 second delay is normal |
| No scan mode | Not in v1 |
| No recording | Not in v1 |
| No authentication | Add if exposing beyond localhost |

---

## 6. Directory Structure

```
ravensdr/
├── app.py                  # Flask app, routes, Socket.IO events (UI process)
├── ipc.py                  # NDJSON protocol: codec, FrameBuffer, CommandRegistry
├── ipc_server.py           # Radio-side Unix-socket server (real threads)
├── radio_link.py           # UI-side client: auto-reconnect, LINK UP/DOWN
├── sdr_arbiter.py          # Serialized, coalescing SDR command queue
├── input_source.py         # InputSource abstraction
├── tuner.py                # RTL-FM process manager
├── stream_source.py        # Web stream ingest via ffmpeg
├── transcriber.py          # Hailo Whisper wrapper
├── audio_router.py         # HTTP audio streaming
├── presets.py              # Frequency preset definitions
├── requirements.txt
├── setup.sh                # System dependency installer
├── static/
│   ├── ravensdr.js          # Frontend logic
│   └── ravensdr.css         # UI stylesheet
└── templates/
    └── index.html          # Console single-page app
```
