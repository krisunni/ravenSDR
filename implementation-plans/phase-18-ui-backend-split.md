# Phase 18 — UI / Radio Process Split

## Overview

Split the monolithic Flask app into two independent systemd services: a UI process
that owns no hardware and a radio process that owns all of it. They communicate
over a Unix domain socket using newline-delimited JSON. The console must load and
render whether or not the radio is alive.

## Why This Matters

The console used to die with the hardware. Everything ran in a single eventlet
process, and eventlet's hub is cooperatively scheduled on one OS thread — so any
blocking call stalls every HTTP request and Socket.IO frame in flight. Three
observed production failures, all the same root cause:

1. **Meteor freeze.** The Science tab's detector did a blocking read on `rtl_fm`'s
   pipe from a *green* thread. The entire web UI froze the moment a meteor was
   detected — HTTP response time went from ~2 ms to unbounded.
2. **Orphaned `rtl_fm`.** Rapid preset switching raced two `/api/tune` handlers on
   the dongle. `_kill_pid` yields (green-patched `time.sleep`), so one greenthread
   cleared `self._pid` *after* another had stored a fresh pid — orphaning an
   `rtl_fm` that then held the device for 17 minutes. Every subsequent tune, the
   16:41Z NOAA-15 APT pass, and the pager decoder all failed with
   `usb_claim_interface error -6`. The pager's failure was then misreported as
   "is multimon-ng installed?" because the *downstream* decoder merely saw EOF.
3. **Hailo driver fault.** 45 kernel oops traces in `hailo_vdma_buffer_map` took
   down inference — and with it the process serving the console.

In all three cases the operator lost the interface because the interface shared a
process with the radio. The SDR is separate hardware; it should be commanded,
report its actual state, and be allowed to fail alone.

## Topology

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

The radio process is deliberately eventlet-free. That is the structural fix: a
blocking read there *cannot* freeze anything a browser talks to.

## Components

### Done

- **`ipc.py`** — NDJSON codec, `FrameBuffer` (reassembles frames split across
  `recv()` boundaries, 4 MB frame cap against a desynced stream),
  `CommandRegistry` (a raising handler becomes an error response, never a dropped
  connection).
- **`ipc_server.py`** — radio-side Unix-socket server on real threads. Accepts
  many UIs, dispatches commands, fans out events, drops clients that fail to
  receive. Unlinks a stale socket file before bind.
- **`radio_link.py`** — UI-side client. Auto-reconnect with exponential backoff
  (0.5 s → 10 s), request/response correlation, observable `LINK UP/DOWN` with
  reconnect count and last error. Commands while down fail fast with the reason.
- **`sdr_arbiter.py`** — serialized, coalescing SDR command queue with C2 states
  (`LOCKED` / `SWITCHING` / `FAULT`) and a `snapshot()` for the UI.
- **Tuner pid-set reap** — `tuner.py` tracks every spawned pid and kills the whole
  set on stop, making the orphan leak unreachable regardless of interleaving.
- **Accurate decoder diagnostics** — `subprocess_decoder.py` checks the *source*
  process first and keeps its stderr, so "device busy" is reported instead of
  being blamed on the decoder binary.

- **Radio-side command surface** — `app.py` hosts an `IpcServer` exposing
  `status`, `sdr_state`, `presets`, `tune`, `stop`, `squelch`, `gain`. The radio
  process is the existing app: it already owns every hardware object, so it
  became the radio half rather than being rebuilt.
- **Event relay** — every Socket.IO event fans out to connected UI processes.
  `socketio.emit` is replaced once, which captures all existing call sites; the
  emit bridge is late-bound so real-thread emitters route through it too.
- **`ui_app.py`** — the console process. Serves templates/static, relays radio
  events to browsers, forwards commands over IPC, resyncs on reconnect, and
  renders LINK state. Owns no hardware.
- **`operations/ravensdr-ui.service`** — `Wants=` (never `Requires=`) the radio.
- **Emit bridge / logging locks** — real threads no longer touch eventlet's green
  primitives (see design.md §3.9 and `emit_bridge.py`).

### Remaining

- **Retire the radio's own Flask app.** The radio still serves the legacy HTTP
  API, and `ui_app` proxies not-yet-migrated `/api/*` routes to it. Each route
  migrated to an IPC command removes one proxy hop; the split is complete when
  the radio serves no HTTP at all.
- **Audio transport** — `/audio-stream` is currently relayed over HTTP by the UI.
  Moving it to a dedicated PCM socket (`audio.sock`) is the highest-risk item:
  continuous and latency-sensitive, so it stays on the proven HTTP path until the
  command plane has soaked.
- **Cutover** — run the UI on :5001 alongside the radio's :5000 until it has
  soaked, then move the UI to :5000 and stop the radio's web server.
- **Per-subsystem query commands** — ADS-B / ISM / ACARS / pager / meteor tables
  are still proxied rather than commanded.

## Command Surface (IPC)

| Command | Args | Returns |
|---|---|---|
| `status` | — | full status snapshot incl. arbiter `snapshot()` |
| `tune` | `{preset_id}` | arbiter snapshot after queueing (does not wait for hardware) |
| `stop` | — | arbiter snapshot |
| `squelch` | `{level}` | `{squelch}` |
| `gain` | `{value}` | `{gain}` |

`tune` returns as soon as the command is queued. The UI follows the transition via
`status` events — this is what removes the request-vs-actual-time gap that made
rapid clicking race in the first place.

## Frontend (C2 panel)

The SDR is presented as commanded hardware, not as an app mode:

- **COMMANDED** vs **ACTUAL** preset/frequency shown side by side.
- **STATE** lamp: `LOCKED` (green) / `SWITCHING` (amber, animated) / `FAULT` (red).
- **LINK** lamp: `UP` / `DOWN`, with reconnect count on hover.
- Preset buttons show a pending indicator while commanded ≠ actual, so a click
  visibly registers even though the hardware takes ~1–2 s.

## Risks

| Risk | Mitigation |
|---|---|
| Audio path regression | Migrate audio last, behind a flag; keep the in-process path until the socket path is proven |
| Live-node cutover | Land IPC + arbiter first (no behaviour change), then flip services in one restart with a documented rollback to the monolith |
| Two processes to supervise | UI does not `Require=` radio; each restarts independently; both log to journald |
| Socket permissions | `0660` on the socket, both units run as the same user |
| Extra IPC latency | NDJSON over a Unix socket is microseconds; the SDR switch it replaces takes ~1–2 s |

## Testing

- `test_ipc.py` — codec, frame reassembly, partial/multi-frame chunks, oversize
  guard, dispatch of unknown/raising commands.
- `test_ipc_loopback.py` — real server + real client over a real Unix socket:
  round-trip, event fan-out to multiple UIs, UI-up-before-radio,
  radio-restart-reconnect, stale socket file, fail-fast while down.
- `test_sdr_arbiter.py` — coalescing (5 clicks → 1 switch), no overlap, state
  transitions, `FAULT` retains last confirmed actual, recovery.
- `test_tuner_process_leak.py` — orphan pid reaped; back-to-back tunes leave one
  live `rtl_fm`.

## Acceptance Criteria

1. `systemctl stop ravensdr-radio` → console still loads and renders, shows LINK
   DOWN, and rejects commands with a clear reason.
2. `systemctl restart ravensdr-radio` → UI reconnects unaided and resyncs state.
3. Ten rapid preset clicks → exactly one live `rtl_fm`, hardware ends on the last
   clicked preset, no orphans.
4. A meteor detection no longer affects HTTP response time.
5. Satellite passes continue to record while the UI process is stopped.
