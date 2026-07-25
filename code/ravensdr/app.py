# Flask app, routes, Socket.IO events
import eventlet
eventlet.monkey_patch()

import atexit
import logging
import signal
import sys
import threading

from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from flask_socketio import SocketIO

from ravensdr.audio_router import audio_stream_generator
from ravensdr.emit_bridge import ThreadSafeEmitter
from ravensdr.sdr_arbiter import SdrArbiter
from ravensdr.ipc import CommandRegistry, resolve_socket_path
from ravensdr.ipc_server import IpcServer
from ravensdr.input_source import InputSource, detect_sdr
from ravensdr.presets import get_presets, get_preset_by_id, CATEGORY_LABELS
from ravensdr.transcriber import Transcriber
from ravensdr.adsb_receiver import (
    AdsbReceiver, AdsbScanScheduler,
    ADSB_ENABLED, ADSB_DUAL_DONGLE,
)
from ravensdr.ais_receiver import AisReceiver
from ravensdr.ism_receiver import IsmReceiver
from ravensdr.acars_receiver import AcarsReceiver, correlate_with_adsb
from ravensdr.pager_receiver import PagerReceiver
from ravensdr.adsb_correlator import extract_callsigns, match_flights
from ravensdr.noaa_parser import WeatherAccumulator, detect_priority_alert
from ravensdr.apt_scheduler import AptScheduler
from ravensdr.apt_decoder import AptDecoder
from ravensdr.wefax_scheduler import WefaxScheduler, WEFAX_ENABLED
from ravensdr.wefax_receiver import WefaxReceiver
from ravensdr.meteor_detector import MeteorDetector, METEOR_ENABLED, METEOR_DUAL_DONGLE, METEOR_FREQUENCY
from ravensdr.meteor_analyzer import MeteorAnalyzer
from ravensdr.signal_classifier import SignalClassifier, iq_to_spectrogram, spectrogram_to_image
from ravensdr.sei_model import SEIModel
from ravensdr.iq_segmenter import IQSegmenter
from ravensdr.config import (
    load_config, save_config, get_secondary_task, set_secondary_task,
    get_startup_preset, set_last_preset, get_settings, update_settings,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)


def _make_logging_thread_safe():
    """Give logging REAL locks instead of eventlet's green ones.

    eventlet.monkey_patch() replaces threading.RLock, so every logging handler
    lock created afterwards is a GREEN semaphore. Those may only be touched from
    the hub's thread. But several subsystems legitimately run on real OS threads
    (meteor detector, subprocess decoders) and they log — and when a real thread
    contends a green lock, the hub tries to switch to a greenlet it does not own:

        greenlet.error: Cannot switch to a different thread

    which is raised inside the hub's fire_timers and destroys whichever
    greenthread was running — observed killing both the meteor detector and the
    in-flight /api/tune request that started it.

    Real locks are correct here: they are safe from any thread, and a log write
    is short enough that a greenthread briefly blocking on one is harmless.

    This is a mitigation, not a cure. The structural fix is phase 18 — move the
    hardware into a process that never imports eventlet at all.
    """
    try:
        from eventlet.patcher import original
        real_threading = original("threading")
    except ImportError:
        return
    logging._lock = real_threading.RLock()
    for handler in logging.root.handlers:
        handler.lock = real_threading.RLock()
    # Handlers created later (per-module) must get real locks too.
    logging.Handler.createLock = lambda self: setattr(
        self, "lock", real_threading.RLock())


_make_logging_thread_safe()
log = logging.getLogger(__name__)

VERSION = "1.2.0"

# ── Flask + Socket.IO ──
app = Flask(
    __name__,
    template_folder="../templates",
    static_folder="../static",
)
app.config["SECRET_KEY"] = "ravensdr-dev"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ── Thread-safe emit bridge ──
# Anything running on a REAL OS thread (meteor detector, rtl_433 / acarsdec /
# multimon-ng decoders) must emit through this instead of calling socketio.emit
# directly — a direct call from a non-hub thread raises
# "greenlet.error: Cannot switch to a different thread" and kills the caller.
# A greenthread (emit_bridge_loop) drains it. See emit_bridge.py.
def _late_emit(event, data=None, **kwargs):
    """Call whatever socketio.emit currently is.

    Late binding matters: the IPC fan-out below replaces socketio.emit, and
    events from real threads must go through the replacement too.
    """
    if data is None and not kwargs:
        return socketio.emit(event)
    return socketio.emit(event, data, **kwargs)


emit_safe = ThreadSafeEmitter(_late_emit)

# ── Detect mode ──
sdr_available = detect_sdr()
mode = "SDR" if sdr_available else "WEBSTREAM"
log.info("Mode: %s (SDR detected: %s)", mode, sdr_available)

# ── Core components ──
input_source = InputSource(mode)
transcriber = Transcriber(input_source.pcm_queue, emit_fn=_late_emit)

# ── Persistent config ──
_config = load_config()
_secondary_task = get_secondary_task(_config)
_secondary_device = _config.get("secondary_dongle", {}).get("device_index", 1)
log.info("Secondary dongle: %s (device %d)", _secondary_task or "disabled", _secondary_device)

# ── ADS-B Receiver ──
adsb_receiver = None
adsb_scheduler = None

if ADSB_ENABLED:
    _adsb_is_secondary = (_secondary_task == "adsb")
    device_idx = _secondary_device if _adsb_is_secondary else 0
    adsb_receiver = AdsbReceiver(device_index=device_idx, dual_dongle=_adsb_is_secondary)

    if _adsb_is_secondary:
        # Secondary dongle: start immediately and run continuously
        adsb_receiver.start()
        log.info("ADS-B receiver started (secondary dongle, device %d)", device_idx)
    else:
        # Single-dongle: ADS-B on-demand via Aviation tab
        adsb_scheduler = AdsbScanScheduler(adsb_receiver, input_source)
        log.info("ADS-B configured (on-demand via Aviation tab)")

# ── AIS Receiver ──
ais_receiver = AisReceiver(device_index=0)

# ── ISM sensor receiver (rtl_433) ──
ism_receiver = IsmReceiver(device_index=0)


def _ism_on_record(record, is_new):
    """Emit each rtl_433 device update to the ISM panel.

    Called from rtl_433's REAL reader thread — emit via the bridge.
    """
    emit_safe("ism_device", record)


ism_receiver.on_record = _ism_on_record

# ── ACARS receiver (acarsdec) ──
acars_receiver = AcarsReceiver(device_index=0)


def _acars_on_record(record, is_new):
    """Emit each ACARS message; correlate with tracked ADS-B flights.

    Called from acarsdec's REAL reader thread — emit via the bridge.
    """
    payload = dict(record)
    if adsb_receiver and adsb_receiver.is_running:
        match = correlate_with_adsb(record, adsb_receiver.get_flights())
        if match:
            payload["adsb_hex"] = match.get("hex")
            payload["adsb_flight"] = match.get("flight")
    emit_safe("acars_message", payload)


acars_receiver.on_record = _acars_on_record

# ── Pager receiver (rtl_fm | multimon-ng) ──
pager_receiver = PagerReceiver(device_index=0)


def _pager_on_record(record, is_new):
    """Emit each decoded pager message to the Pager panel.

    Called from multimon-ng's REAL reader thread — emit via the bridge.
    """
    emit_safe("pager_message", record)


pager_receiver.on_record = _pager_on_record

# ── Weather state ──
# Accumulate transcripts across NOAA's broadcast loop and re-summarize; a single
# garbled Whisper chunk can't carry city/temp/forecast, but voting over a window can.
_weather_accumulator = WeatherAccumulator()
_latest_weather = None


def _on_weather_update(parsed_data):
    """Feed the raw transcript into the accumulator and emit the voted summary."""
    global _latest_weather
    raw = parsed_data.get("raw_transcript", "")
    summary = _weather_accumulator.add(raw)
    _latest_weather = summary
    socketio.emit("weather_update", summary)

    if detect_priority_alert(raw):
        preset = input_source.current_preset or {}
        alert_payload = {
            "alerts": parsed_data.get("alerts", []),
            "raw_snippet": parsed_data.get("raw_transcript", "")[:200],
            "timestamp": parsed_data.get("parsed_at", ""),
            "freq": preset.get("freq", ""),
            "source": mode,
        }
        socketio.emit("priority_alert", alert_payload)
        # Structured intelligence log entry for each alert
        for alert in parsed_data.get("alerts", []):
            log.warning(
                "INTEL WEATHER_ALERT | ts=%s | freq=%s | type=%s | name=%s | area=%s | source=%s | snippet=%.200s",
                parsed_data.get("parsed_at", ""),
                preset.get("freq", ""),
                alert.get("type", ""),
                alert.get("name", ""),
                alert.get("area", ""),
                mode,
                parsed_data.get("raw_transcript", "")[:200],
            )


transcriber.set_weather_callback(_on_weather_update)


def _scan_keywords(text):
    """Scan a transcript for user watchlist terms; emit a keyword_hit per match.

    The watchlist lives in the settings block (Settings tab). Matching is
    case-insensitive substring; each hit reuses the priority_alert UI banner and
    writes a structured INTEL log line, mirroring the weather-alert path.
    """
    if not text:
        return
    settings = get_settings()
    if not settings.get("keywords_enabled", True):
        return
    lowered = text.lower()
    preset = input_source.current_preset or {}
    for entry in settings.get("keywords", []):
        term = entry.get("term", "")
        if not term or not entry.get("enabled", True):
            continue
        if term.lower() in lowered:
            severity = entry.get("severity", "info")
            payload = {
                "term": term,
                "severity": severity,
                "transcript": text[:300],
                "freq": preset.get("freq", ""),
                "label": preset.get("label", ""),
                "source": mode,
            }
            socketio.emit("keyword_hit", payload)
            # Also drive the shared alert banner for warning/critical hits
            if severity in ("warning", "critical"):
                socketio.emit("priority_alert", {
                    "alerts": [{"type": "keyword", "name": term, "area": ""}],
                    "raw_snippet": text[:200],
                    "freq": preset.get("freq", ""),
                    "source": mode,
                })
            log.warning(
                "INTEL KEYWORD_HIT | term=%s | severity=%s | freq=%s | source=%s | snippet=%.200s",
                term, severity, preset.get("freq", ""), mode, text[:200],
            )


def _on_transcript(text):
    """General per-transcript hook: keyword watchlist + ADS-B callsign match."""
    _scan_keywords(text)
    if adsb_receiver:
        callsigns = extract_callsigns(text)
        if callsigns:
            matches = match_flights(callsigns, adsb_receiver.get_flights())
            if matches:
                socketio.emit("callsign_match", {
                    "transcript": text,
                    "matches": matches,
                })


transcriber.set_transcript_callback(_on_transcript)

# ── APT Satellite Imaging ──
apt_decoder = AptDecoder(emit_fn=_late_emit)


def _on_apt_pass_start(pass_info):
    """Called by scheduler when a satellite pass begins — start recording."""
    satellite = pass_info.get("satellite", "")
    frequency = pass_info.get("frequency", "")

    # Stop meteor detector if it's holding the device (single-dongle mode)
    if meteor_detector and meteor_detector.is_running and not METEOR_DUAL_DONGLE:
        meteor_detector.stop()
        log.info("Stopped meteor detector for APT recording")
        socketio.emit("notice", {
            "message": f"Meteor detector paused — SDR dedicated to {satellite} pass",
            "type": "apt_preempt",
        })

    # Stop dedicated decoders that seize the dongle directly (ISM/AIS/ACARS/Pager)
    for _rx, _name in ((ism_receiver, "ISM"), (ais_receiver, "AIS"),
                       (acars_receiver, "ACARS"), (pager_receiver, "Pager")):
        if _rx and _rx.is_running:
            _rx.stop()
            log.info("Stopped %s for APT recording", _name)
            socketio.emit("notice", {
                "message": f"{_name} paused — SDR dedicated to {satellite} pass",
                "type": "apt_preempt",
            })

    # Stop ADS-B if it's holding the device (single-dongle mode)
    resumed_adsb_scheduler = False
    if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
        adsb_receiver.stop()
        if adsb_scheduler:
            adsb_scheduler.stop()
            resumed_adsb_scheduler = True
        log.info("Stopped ADS-B for APT recording")
        socketio.emit("notice", {
            "message": f"ADS-B paused — SDR dedicated to {satellite} pass",
            "type": "apt_preempt",
        })

    if input_source.enter_apt_mode(frequency):
        apt_decoder.record_pass(pass_info)
        socketio.emit("status", _get_status())

        # Release APT mode once the recording actually finishes.
        #
        # Don't just sleep for the pass duration: record_pass() returns True for
        # merely *spawning* the capture task, and the capture can die immediately
        # (e.g. an orphaned rtl_fm still holds the dongle -> "device busy"). A
        # blind sleep then pins the SDR in APT mode for the full ~15 min while
        # nothing is being recorded, and every tune is refused with
        # "Cannot tune — SDR is in APT satellite recording mode". So poll
        # is_recording and drop APT mode as soon as it is no longer active.
        def _exit_apt():
            import eventlet as _ev

            deadline = pass_info.get("duration", 900) + 30
            waited = 0
            # Grace period for the capture task to come up before we judge it.
            startup_grace = 20
            while waited < deadline:
                _ev.sleep(1)
                waited += 1
                if apt_decoder.is_recording:
                    break
                if waited >= startup_grace:
                    log.warning("APT capture for %s never started (device busy?) — "
                                "releasing APT mode after %ds instead of holding "
                                "the SDR for the whole pass", satellite, waited)
                    break
            # Now wait out the actual recording, if one is running.
            while apt_decoder.is_recording and waited < deadline:
                _ev.sleep(1)
                waited += 1

            if input_source.apt_mode:
                input_source.exit_apt_mode()
                socketio.emit("status", _get_status())
            # Resume opportunistic ADS-B scanning we suspended for the pass
            if resumed_adsb_scheduler and adsb_scheduler:
                adsb_scheduler.start()
                log.info("Resumed ADS-B scan scheduler after APT recording")
                socketio.emit("notice", {
                    "message": "ADS-B scanning resumed after satellite pass",
                    "type": "apt_preempt",
                })

        socketio.start_background_task(_exit_apt)
    else:
        log.warning("Could not enter APT mode for %s", satellite)
        socketio.emit("error", {
            "message": f"Could not enter APT mode for {satellite} — pass missed",
            "type": "apt_failed",
        })
        # Nothing to record — put ADS-B scanning back
        if resumed_adsb_scheduler and adsb_scheduler:
            adsb_scheduler.start()


apt_scheduler = AptScheduler(emit_fn=_late_emit, on_pass_start=_on_apt_pass_start)

# ── WEFAX Weather Fax ──
wefax_receiver = WefaxReceiver(emit_fn=_late_emit)


def _on_wefax_broadcast_start(broadcast_info):
    """Called by scheduler when a WEFAX broadcast begins — start recording."""
    frequency_khz = broadcast_info.get("frequency_khz", 0)

    # Stop meteor detector if it's holding the device (single-dongle mode)
    if meteor_detector and meteor_detector.is_running and not METEOR_DUAL_DONGLE:
        meteor_detector.stop()
        log.info("Stopped meteor detector for WEFAX recording")

    # Stop ADS-B if it's holding the device (single-dongle mode)
    if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
        adsb_receiver.stop()
        if adsb_scheduler:
            adsb_scheduler.stop()
        log.info("Stopped ADS-B for WEFAX recording")

    if input_source.enter_wefax_mode(frequency_khz):
        wefax_receiver.record_broadcast(broadcast_info)
        socketio.emit("status", _get_status())

        # Schedule exit from WEFAX mode after recording duration
        def _exit_wefax():
            import eventlet as _ev
            duration_min = broadcast_info.get("duration_minutes", 10)
            _ev.sleep(duration_min * 60 + 30)
            if input_source.wefax_mode:
                input_source.exit_wefax_mode()
                socketio.emit("status", _get_status())

        socketio.start_background_task(_exit_wefax)
        return True

    log.warning("Could not enter WEFAX mode for %s %s",
                broadcast_info.get("station"), broadcast_info.get("chart_type"))
    return False


wefax_scheduler = WefaxScheduler(emit_fn=_late_emit, on_broadcast_start=_on_wefax_broadcast_start)

# ── Meteor Scatter Detection ──
meteor_analyzer = MeteorAnalyzer()

_meteor_is_secondary = (_secondary_task == "meteor")
_meteor_device_idx = _secondary_device if _meteor_is_secondary else 0
meteor_detector = MeteorDetector(
    emit_fn=_late_emit,
    frequency_hz=METEOR_FREQUENCY,
    device_index=_meteor_device_idx,
)
meteor_detector.load_events_from_log()

# ── Signal Classifier ──
import os as _os
_classifier_hef = _os.environ.get("CLASSIFIER_HEF_PATH")
_classifier_classes = _os.environ.get("CLASSIFIER_CLASSES_PATH")
signal_classifier = SignalClassifier(
    emit_fn=_late_emit,
    hef_path=_classifier_hef,
    class_map_path=_classifier_classes,
)
log.info("Signal classifier initialized (backend: %s)", signal_classifier.backend)

# ── Specific Emitter Identification ──
_sei_hef = _os.environ.get("SEI_HEF_PATH")
sei_model = SEIModel(emit_fn=_late_emit, hef_path=_sei_hef)
signal_classifier.set_sei_model(sei_model)
log.info("SEI model initialized (backend: %s, %d emitters loaded)",
         sei_model.backend, sei_model.get_status()["emitter_count"])

# ── IQ Pipeline (segmenter + classifier + spectrogram waterfall) ──
iq_segmenter = IQSegmenter(
    sample_rate=240000,
    on_segment=signal_classifier.classify_segment,
)

_iq_chunk_counter = 0
_pending_spectrogram_row = None  # buffered for eventlet emission
_pending_classification = None   # buffered for eventlet emission


def _on_iq_chunk(iq_samples, frequency_hz):
    """Called by pyrtlsdr IQCapture for each raw IQ chunk.

    Runs in a real OS thread (not eventlet greenlet), so must NOT call
    socketio.emit directly. Buffer data for the eventlet broadcast loop.
    """
    global _iq_chunk_counter, _pending_spectrogram_row, _pending_classification
    _iq_chunk_counter += 1

    # Feed segmenter every chunk (accurate TX boundary detection)
    iq_segmenter.set_frequency(frequency_hz)
    iq_segmenter.feed(iq_samples)

    # Run classification every 5th chunk (~500ms) — buffer result, don't emit
    if _iq_chunk_counter % 5 == 0:
        preset = input_source.current_preset or {}
        try:
            result = signal_classifier.classify_iq(
                iq_samples,
                frequency_hz=frequency_hz,
                expected_modulation=preset.get("expected_modulation"),
            )
            if result:
                _pending_classification = result
        except Exception:
            pass

    # Compute spectrogram row every 3rd chunk (~300ms) — buffer, don't emit
    if _iq_chunk_counter % 3 == 0:
        try:
            spec = iq_to_spectrogram(iq_samples, fft_size=256, hop=128)
            img = spectrogram_to_image(spec, size=256)
            _pending_spectrogram_row = img[-1].tolist()
        except Exception:
            pass


# Prevent classifier from emitting directly (it runs in the IQ thread)
signal_classifier.emit_fn = lambda *a, **kw: None

input_source.set_iq_callback(_on_iq_chunk)
log.info("IQ pipeline wired (segmenter + classifier + spectrogram waterfall)")

# Wire analyzer to tag shower info on each detection.
# NOTE: this runs on the meteor detector's REAL OS thread (it does a blocking read
# on rtl_fm's pipe), so it must emit through the bridge, never socketio.emit
# directly — the direct call raised "greenlet.error: Cannot switch to a different
# thread" and killed the detector the instant a meteor was detected.
def _meteor_emit_wrapper(event, data, **kw):
    if event == "meteor_detection" and isinstance(data, dict):
        meteor_analyzer.tag_event_shower(data)
    emit_safe(event, data, **kw)


meteor_detector.emit_fn = _meteor_emit_wrapper
log.info("Meteor detector configured (device %d, %s)",
         _meteor_device_idx,
         "secondary dongle" if _meteor_is_secondary else "on-demand via Science tab")


def _input_error_callback(event, data):
    """Handle input source error/recovery events."""
    if event == "sdr_disconnected":
        socketio.emit("error", {"message": data["message"], "recoverable": True, "type": "sdr_disconnect"})
        input_source.stop()
        _broadcast_status()
    elif event == "sdr_reconnected":
        socketio.emit("error", {"message": data["message"], "type": "sdr_reconnected"})
        _broadcast_status()


input_source.set_error_callback(_input_error_callback)

# ── Emit bridge drain ──
def emit_bridge_loop():
    """Deliver events queued by real OS threads.

    This is the ONLY place those events reach Socket.IO. It must run in a
    greenthread — the hub owns socketio.emit, and calling it from the hardware
    threads directly is what raised "greenlet.error: Cannot switch to a
    different thread".
    """
    emit_safe.drain_forever(
        sleep_fn=eventlet.sleep,
        should_run=lambda: not _signal_stop.is_set(),
    )


# ── Signal meter thread ──
_signal_stop = threading.Event()


def signal_meter_loop():
    """Reset signal meter to 0 when input source stops."""
    _was_running = False
    while not _signal_stop.is_set():
        eventlet.sleep(0.5)
        running = input_source.is_running
        # Only emit 0 on the transition from running → stopped
        # (real signal levels are emitted by the transcriber inference loop)
        if _was_running and not running:
            preset = input_source.current_preset or {}
            socketio.emit("signal_level", {
                "rms": 0,
                "freq": preset.get("freq", ""),
            })
        _was_running = running


# ── REST Routes ──

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/presets")
def api_presets():
    return jsonify({
        "presets": get_presets(),
        "categories": CATEGORY_LABELS,
    })


@app.route("/api/tune", methods=["POST"])
def api_tune():
    """Command the SDR to a preset. Returns as soon as the command is QUEUED.

    The hardware switch takes ~1-2s (kill rtl_fm, wait for the kernel to release
    the USB interface, respawn). This handler does not wait for it: it validates,
    hands the command to the arbiter, and returns the C2 snapshot. The UI follows
    the transition via `sdr_state` / `status` events. That gap between "request"
    and "actually switched" is what used to let rapid clicks race on the dongle.
    """
    data = request.get_json(force=True)
    preset_id = data.get("preset_id")
    preset = get_preset_by_id(preset_id)
    if not preset:
        return jsonify({"error": "Unknown preset"}), 400

    # Validate before queueing so a bad request fails synchronously.
    if mode == "WEBSTREAM" and not preset.get("stream_url"):
        return jsonify({"error": "No web stream available for this preset (SDR only)"}), 400

    snapshot = sdr_arbiter.request(preset)
    return jsonify({"status": "commanded", "preset": preset, "sdr": snapshot}), 202

def _apply_tune(preset):
    """Perform the actual SDR switch. Runs ONLY in the arbiter worker.

    Returns (ok, error_message). Never touches Flask's request context.
    """
    # Start weather accumulation fresh when the station changes
    if input_source.current_preset is None or \
            input_source.current_preset.get("id") != preset.get("id"):
        _weather_accumulator.reset()

    # Science tab: display-only, start meteor detector if not running
    if preset.get("category") == "science":
        input_source.stop()
        input_source.current_preset = preset
        if ism_receiver.is_running:
            ism_receiver.stop()
        if acars_receiver.is_running:
            acars_receiver.stop()
        if pager_receiver.is_running:
            pager_receiver.stop()
        if ais_receiver.is_running:
            ais_receiver.stop()
        if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
            adsb_receiver.stop()
            if adsb_scheduler:
                adsb_scheduler.start()
        # Start meteor detector on the main dongle if not already running
        if meteor_detector and not meteor_detector.is_running:
            meteor_detector.start()
        _broadcast_status()
        return True, None

    # If switching away from Science, stop meteor detector on main dongle
    if meteor_detector and meteor_detector.is_running and not METEOR_DUAL_DONGLE:
        meteor_detector.stop()

    # WEFAX tab: display-only, scheduler handles recording automatically
    if preset.get("category") == "wefax":
        input_source.stop()
        input_source.current_preset = preset
        # Stop ADS-B dedicated mode if active
        if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
            adsb_receiver.stop()
            if adsb_scheduler:
                adsb_scheduler.start()
        # Stop AIS / ISM / ACARS / Pager if active
        if ais_receiver.is_running:
            ais_receiver.stop()
        if ism_receiver.is_running:
            ism_receiver.stop()
        if acars_receiver.is_running:
            acars_receiver.stop()
        if pager_receiver.is_running:
            pager_receiver.stop()
        _broadcast_status()
        return True, None

    is_adsb = preset.get("mode") == "adsb"
    is_ais = preset.get("mode") == "ais"
    is_ism = preset.get("mode") == "ism"
    is_acars = preset.get("mode") == "acars"
    is_pager = preset.get("mode") == "pager"

    # Pager dedicated mode: stop audio pipeline, run rtl_fm|multimon-ng continuously
    if is_pager:
        input_source.stop()
        input_source.current_preset = preset
        for _rx in (ais_receiver, ism_receiver, acars_receiver):
            if _rx.is_running:
                _rx.stop()
        if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
            adsb_receiver.stop()
            if adsb_scheduler:
                adsb_scheduler.start()
        pager_receiver.frequency = preset.get("freq", pager_receiver.frequency)
        pager_receiver.start()
        if not pager_receiver.is_running:
            reason = pager_receiver.last_error or "unknown error"
            log.error("Failed to start multimon-ng pager decoder: %s", reason)
            return False, f"Failed to start pager decoder — {reason}"
        log.info("Pager dedicated mode — multimon-ng on %s", pager_receiver.frequency)
        _broadcast_status()
        return True, None

    # Switching away from pager: stop multimon-ng
    if pager_receiver.is_running:
        pager_receiver.stop()

    # ACARS dedicated mode: stop audio pipeline, run acarsdec continuously
    if is_acars:
        input_source.stop()
        input_source.current_preset = preset
        if ais_receiver.is_running:
            ais_receiver.stop()
        if ism_receiver.is_running:
            ism_receiver.stop()
        if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
            adsb_receiver.stop()
            if adsb_scheduler:
                adsb_scheduler.start()
        acars_receiver.start()
        if not acars_receiver.is_running:
            reason = acars_receiver.last_error or "unknown error"
            log.error("Failed to start acarsdec: %s", reason)
            return False, f"Failed to start acarsdec — {reason}"
        log.info("ACARS dedicated mode — acarsdec running on %s",
                 ",".join(acars_receiver.channels))
        _broadcast_status()
        return True, None

    # Switching away from ACARS: stop acarsdec
    if acars_receiver.is_running:
        acars_receiver.stop()

    # ISM dedicated mode: stop audio pipeline, run rtl_433 continuously
    if is_ism:
        input_source.stop()
        input_source.current_preset = preset
        if ais_receiver.is_running:
            ais_receiver.stop()
        if acars_receiver.is_running:
            acars_receiver.stop()
        if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
            adsb_receiver.stop()
            if adsb_scheduler:
                adsb_scheduler.start()
        ism_receiver.frequency = preset.get("freq", ism_receiver.frequency)
        ism_receiver.start()
        if not ism_receiver.is_running:
            reason = ism_receiver.last_error or "unknown error"
            log.error("Failed to start rtl_433: %s", reason)
            return False, f"Failed to start rtl_433 — {reason}"
        log.info("ISM dedicated mode — rtl_433 running on %s", ism_receiver.frequency)
        _broadcast_status()
        return True, None

    # Switching away from ISM: stop rtl_433
    if ism_receiver.is_running:
        ism_receiver.stop()

    # AIS dedicated mode: stop audio pipeline, run rtl_ais continuously
    if is_ais:
        input_source.stop()
        input_source.current_preset = preset
        # Stop ADS-B if running in dedicated mode
        if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
            adsb_receiver.stop()
            if adsb_scheduler:
                adsb_scheduler.start()
        ais_receiver.start()
        if not ais_receiver.is_running:
            log.error("Failed to start rtl_ais")
            return False, "Failed to start rtl_ais"
        log.info("AIS dedicated mode — rtl_ais running continuously")
        _broadcast_status()
        return True, None

    # Switching away from AIS: stop rtl_ais
    if ais_receiver.is_running:
        ais_receiver.stop()

    if is_adsb and adsb_receiver:
        # ADS-B dedicated mode: stop audio pipeline, run dump1090 continuously
        input_source.stop()
        input_source.current_preset = preset
        if adsb_scheduler:
            adsb_scheduler.stop()
        adsb_receiver.start()
        if not adsb_receiver.is_running:
            log.error("Failed to start dump1090")
            return False, "Failed to start dump1090"
        log.info("ADS-B dedicated mode — dump1090 running continuously")
        _broadcast_status()
        return True, None

    # Switching away from ADS-B: stop dedicated dump1090, restart scheduler
    if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
        adsb_receiver.stop()
        if adsb_scheduler:
            adsb_scheduler.start()

    success = input_source.tune(preset)
    if not success:
        return False, "Failed to tune — SDR busy or unavailable"

    transcriber.set_preset(preset)
    set_last_preset(preset.get("id"))
    _broadcast_status()

    return True, None


# ── SDR arbiter ──
# Serializes and coalesces every SDR switch. See sdr_arbiter.py for why: the
# hardware takes ~1-2s to switch while HTTP requests arrive in milliseconds, and
# applying them concurrently orphaned rtl_fm processes that then held the dongle.
def _on_sdr_state_change(snapshot):
    """Push the C2 snapshot (commanded vs actual) to every console."""
    emit_safe("sdr_state", snapshot)


def _on_sdr_fault(message, preset):
    label = (preset or {}).get("label") or (preset or {}).get("id") or "preset"
    emit_safe("error", {
        "message": f"SDR switch to {label} failed — {message}",
        "type": "sdr_fault",
    })


sdr_arbiter = SdrArbiter(
    apply_fn=_apply_tune,
    on_change=_on_sdr_state_change,
    on_error=_on_sdr_fault,
    sleep_fn=eventlet.sleep,
)


@app.route("/api/sdr/state")
def api_sdr_state():
    """C2 view of the radio: commanded vs actual, and the transition between."""
    return jsonify(sdr_arbiter.snapshot())


# ── Radio-side IPC (phase 18) ──
# This process owns the hardware, so it serves the radio half of the UI/radio
# boundary. A separate UI process (ui_app.py) drives it over this socket; the
# built-in Flask routes above remain during the transition.
ipc_commands = CommandRegistry()
RADIO_SOCKET_PATH = resolve_socket_path()
ipc_server = IpcServer(RADIO_SOCKET_PATH, registry=ipc_commands)


@ipc_commands.command("status")
def _cmd_status(args):
    return _get_status()


@ipc_commands.command("sdr_state")
def _cmd_sdr_state(args):
    return sdr_arbiter.snapshot()


@ipc_commands.command("presets")
def _cmd_presets(args):
    return {"presets": get_presets(), "categories": CATEGORY_LABELS}


@ipc_commands.command("tune")
def _cmd_tune(args):
    preset = get_preset_by_id(args.get("preset_id"))
    if not preset:
        raise ValueError(f"unknown preset: {args.get('preset_id')!r}")
    if mode == "WEBSTREAM" and not preset.get("stream_url"):
        raise ValueError("no web stream available for this preset (SDR only)")
    return {"preset": preset, "sdr": sdr_arbiter.request(preset)}


@ipc_commands.command("stop")
def _cmd_stop(args):
    input_source.stop()
    sdr_arbiter.adopt(None)
    _broadcast_status()
    return sdr_arbiter.snapshot()


@ipc_commands.command("squelch")
def _cmd_squelch(args):
    input_source.set_squelch(int(args.get("level", 0)))
    _broadcast_status()
    return {"squelch": input_source.squelch}


@ipc_commands.command("gain")
def _cmd_gain(args):
    input_source.set_gain(args.get("value", "auto"))
    _broadcast_status()
    return {"gain": input_source.gain}


# Fan every Socket.IO event out to connected UI processes as well.
# Replacing socketio.emit catches all ~40 existing call sites without touching
# them; _late_emit above ensures real-thread emitters route through here too.
_socketio_emit = socketio.emit


def _emit_with_ipc_fanout(event, data=None, **kwargs):
    try:
        if data is None and not kwargs:
            result = _socketio_emit(event)
        else:
            result = _socketio_emit(event, data, **kwargs)
    finally:
        # A UI process must never be able to break local emission.
        try:
            ipc_server.broadcast(event, data)
        except Exception:
            log.debug("IPC fan-out failed for %r", event, exc_info=True)
    return result


socketio.emit = _emit_with_ipc_fanout


@app.route("/api/stop", methods=["POST"])
def api_stop():
    input_source.stop()
    sdr_arbiter.adopt(None)      # nothing is tuned now
    # Stop dedicated AIS / ISM mode if active
    if ais_receiver.is_running:
        ais_receiver.stop()
    if ism_receiver.is_running:
        ism_receiver.stop()
    if acars_receiver.is_running:
        acars_receiver.stop()
    if pager_receiver.is_running:
        pager_receiver.stop()
    # Stop dedicated ADS-B mode if active
    if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
        adsb_receiver.stop()
        if adsb_scheduler:
            adsb_scheduler.start()
    _broadcast_status()
    return jsonify({"status": "stopped"})


@app.route("/api/squelch", methods=["POST"])
def api_squelch():
    data = request.get_json(force=True)
    level = data.get("level", 0)
    input_source.set_squelch(int(level))
    _broadcast_status()
    return jsonify({"status": "ok", "squelch": input_source.squelch})


@app.route("/api/gain", methods=["POST"])
def api_gain():
    data = request.get_json(force=True)
    value = data.get("value", "auto")
    input_source.set_gain(value)
    _broadcast_status()
    return jsonify({"status": "ok", "gain": input_source.gain})


@app.route("/api/sample_rate", methods=["POST"])
def api_sample_rate():
    data = request.get_json(force=True)
    value = data.get("value")  # None = auto
    input_source.set_sample_rate(value)
    _broadcast_status()
    return jsonify({"status": "ok", "sample_rate": input_source.sample_rate,
                     "effective_sample_rate": input_source.effective_sample_rate})


@app.route("/api/deemp", methods=["POST"])
def api_deemp():
    data = request.get_json(force=True)
    value = data.get("value")  # None = auto, true/false = explicit
    input_source.set_deemp(value)
    _broadcast_status()
    return jsonify({"status": "ok", "deemp": input_source.deemp,
                     "effective_deemp": input_source.effective_deemp})


@app.route("/api/ppm", methods=["POST"])
def api_ppm():
    data = request.get_json(force=True)
    value = data.get("value", 0)
    input_source.set_ppm(int(value))
    _broadcast_status()
    return jsonify({"status": "ok", "ppm": input_source.ppm})


@app.route("/api/direct_sampling", methods=["POST"])
def api_direct_sampling():
    data = request.get_json(force=True)
    value = data.get("value", 0)
    input_source.set_direct_sampling(int(value))
    _broadcast_status()
    return jsonify({"status": "ok", "direct_sampling": input_source.direct_sampling})


@app.route("/api/retry", methods=["POST"])
def api_retry():
    if not input_source.current_preset:
        return jsonify({"error": "No preset to retry"}), 400
    success = input_source.restart()
    if success:
        _broadcast_status()
        return jsonify({"status": "restarted"})
    return jsonify({"error": "Restart failed"}), 500


@app.route("/api/stats")
def api_stats():
    return jsonify(transcriber.stats)


@app.route("/api/adsb/flights")
def api_adsb_flights():
    if adsb_receiver:
        return jsonify(adsb_receiver.get_flights())
    return jsonify([])


@app.route("/api/ais/vessels")
def api_ais_vessels():
    return jsonify(ais_receiver.get_vessels())


@app.route("/api/ism/devices")
def api_ism_devices():
    return jsonify(ism_receiver.get_devices())


@app.route("/api/acars/messages")
def api_acars_messages():
    return jsonify(acars_receiver.get_messages())


@app.route("/api/pager/pages")
def api_pager_pages():
    return jsonify(pager_receiver.get_pages())


@app.route("/api/weather/current")
def api_weather_current():
    if _latest_weather is None:
        return jsonify({"error": "No weather data received yet"}), 404
    return jsonify(_latest_weather)


@app.route("/api/satellite/passes")
def api_satellite_passes():
    passes = apt_scheduler.get_next_passes(hours=24)
    return jsonify(passes)


@app.route("/api/satellite/latest-image")
def api_satellite_latest_image():
    image = apt_decoder.get_latest_image()
    if image is None:
        return jsonify({"error": "No decoded satellite images yet"}), 404
    return jsonify(image)


@app.route("/api/wefax/latest")
def api_wefax_latest():
    chart_type = request.args.get("chart_type")
    image = wefax_receiver.get_latest_image(chart_type=chart_type)
    if image is None:
        return jsonify({"error": "No decoded WEFAX charts yet"}), 404
    return jsonify(image)


@app.route("/api/wefax/schedule")
def api_wefax_schedule():
    broadcasts = wefax_scheduler.get_upcoming_broadcasts(hours=6)
    return jsonify(broadcasts)


@app.route("/api/wefax/history")
def api_wefax_history():
    chart_type = request.args.get("chart_type")
    history = wefax_receiver.get_image_history(count=10, chart_type=chart_type)
    return jsonify(history)


@app.route("/api/wefax/record", methods=["POST"])
def api_wefax_record():
    """Manually trigger an on-demand WEFAX capture (doesn't wait for the schedule).

    Body (JSON, all optional): frequency_khz, station, chart_type, duration_minutes.
    """
    if not WEFAX_ENABLED:
        return jsonify({"error": "WEFAX is disabled (WEFAX_ENABLED=false)"}), 403
    if wefax_receiver.is_recording:
        return jsonify({"error": "WEFAX capture already in progress"}), 409

    data = request.get_json(silent=True) or {}
    try:
        freq_khz = float(data.get("frequency_khz", 8682.0))
        duration = max(1, min(15, int(data.get("duration_minutes", 8))))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid frequency_khz or duration_minutes"}), 400

    broadcast = {
        "station": data.get("station", "MANUAL"),
        "frequency_khz": freq_khz,
        "chart_type": data.get("chart_type", "manual_capture"),
        "duration_minutes": duration,
        "description": "Manual WEFAX capture",
    }
    if _on_wefax_broadcast_start(broadcast):
        log.info("Manual WEFAX capture started: %.1f kHz for %d min", freq_khz, duration)
        return jsonify({"status": "recording", "broadcast": broadcast})
    return jsonify({"error": "Could not start WEFAX capture — SDR busy or not in SDR mode"}), 409


@app.route("/api/meteor/events")
def api_meteor_events():
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    shower = request.args.get("shower")
    trail_type = request.args.get("trail_type")
    events = meteor_detector.get_events(limit=limit, offset=offset,
                                         shower=shower, trail_type=trail_type)
    return jsonify(events)


@app.route("/api/meteor/stats")
def api_meteor_stats():
    events = meteor_detector.get_events(limit=10000)
    stats = meteor_analyzer.get_session_stats(events)
    hourly = meteor_analyzer.get_hourly_stats(events, hours=24)
    current_shower = meteor_analyzer.get_current_shower()
    next_shower = meteor_analyzer.get_next_shower()
    stats["hourly"] = hourly
    stats["shower"] = current_shower["name"] if current_shower else None
    stats["next_shower"] = next_shower
    stats["baseline_dbm"] = round(meteor_detector.baseline_power_db, 1)
    stats["frequency_hz"] = meteor_detector.frequency_hz
    stats["meteor_enabled"] = True
    return jsonify(stats)


@app.route("/api/meteor/showers")
def api_meteor_showers():
    return jsonify(meteor_analyzer.get_showers())


@app.route("/api/classifier/status")
def api_classifier_status():
    return jsonify(signal_classifier.get_status())


@app.route("/api/emitters")
def api_emitters():
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    return jsonify(sei_model.list_emitters(limit=limit, offset=offset))


@app.route("/api/emitters/<emitter_id>")
def api_emitter_detail(emitter_id):
    record = sei_model.get_emitter(emitter_id)
    if record is None:
        return jsonify({"error": "Emitter not found"}), 404
    return jsonify(record)


@app.route("/api/emitters/<emitter_id>/label", methods=["POST"])
def api_emitter_label(emitter_id):
    data = request.get_json(force=True)
    label = data.get("label", "")
    if sei_model.label_emitter(emitter_id, label):
        return jsonify({"status": "ok", "emitter_id": emitter_id, "label": label})
    return jsonify({"error": "Emitter not found"}), 404


@app.route("/api/emitters/<emitter_id>", methods=["DELETE"])
def api_emitter_delete(emitter_id):
    if sei_model.delete_emitter(emitter_id):
        return jsonify({"status": "deleted", "emitter_id": emitter_id})
    return jsonify({"error": "Emitter not found"}), 404


@app.route("/api/sei/status")
def api_sei_status():
    return jsonify(sei_model.get_status())


@app.route("/api/training/stats")
def api_training_stats():
    """On-device SEI training-corpus stats for the Training panel.

    Reports the enrolled fingerprint DB plus any IQ collected under
    data/collected/<icao>/. Actual model retrain + HEF compile is an OFFLINE
    x86 step (Hailo Dataflow Compiler), so `collection_available` is False here —
    the panel shows this as read-only guidance, not a live trainer.
    """
    import os as _os2
    data_dir = _os2.path.join(_os2.path.dirname(__file__), "data")
    collected_dir = _os2.path.join(data_dir, "collected")
    per_emitter = []
    total_files = 0
    total_bytes = 0
    if _os2.path.isdir(collected_dir):
        for name in sorted(_os2.listdir(collected_dir)):
            sub = _os2.path.join(collected_dir, name)
            if not _os2.path.isdir(sub):
                continue
            files = [f for f in _os2.listdir(sub) if f.endswith((".npy", ".iq", ".cf32"))]
            nbytes = 0
            for f in files:
                try:
                    nbytes += _os2.path.getsize(_os2.path.join(sub, f))
                except OSError:
                    pass
            total_files += len(files)
            total_bytes += nbytes
            per_emitter.append({"label": name, "samples": len(files), "bytes": nbytes})

    sei_status = sei_model.get_status()
    return jsonify({
        "enrolled_emitters": sei_status.get("emitter_count", 0),
        "collected_samples": total_files,
        "collected_bytes": total_bytes,
        "per_emitter": per_emitter,
        "collection_available": False,
        "note": ("On-device: curate fingerprints, label emitters, tune thresholds. "
                 "Model retrain + Hailo HEF compile run offline on x86; load the "
                 "new .hef to update the weights."),
    })


@app.route("/api/config/secondary")
def api_config_secondary_get():
    cfg = load_config()
    sec = cfg.get("secondary_dongle", {})
    return jsonify({
        "enabled": sec.get("enabled", False),
        "task": sec.get("task"),
        "device_index": sec.get("device_index", 1),
        "running": _get_secondary_running(),
    })


@app.route("/api/config/secondary", methods=["POST"])
def api_config_secondary_set():
    data = request.get_json(force=True)
    task = data.get("task")  # "adsb", "meteor", "wefax", or null

    if task and task not in ("adsb", "meteor", "wefax"):
        return jsonify({"error": "Invalid task. Use: adsb, meteor, wefax, or null"}), 400

    # Stop current secondary task
    _stop_secondary_task()

    # Save new config
    cfg = set_secondary_task(task)
    log.info("Secondary dongle config changed: %s", task or "disabled")

    # Start new secondary task
    if task:
        _start_secondary_task(task)

    _broadcast_status()
    return jsonify({
        "status": "ok",
        "task": task,
        "running": _get_secondary_running(),
    })


def _get_secondary_running():
    """Check if the secondary dongle task is currently running."""
    task = get_secondary_task()
    if task == "adsb":
        return adsb_receiver.is_running if adsb_receiver else False
    elif task == "meteor":
        return meteor_detector.is_running if meteor_detector else False
    elif task == "wefax":
        return wefax_receiver.is_recording if wefax_receiver else False
    return False


def _stop_secondary_task():
    """Stop whatever secondary task is running."""
    if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
        adsb_receiver.stop()
        log.info("Stopped ADS-B secondary task")
    if meteor_detector and meteor_detector.is_running:
        meteor_detector.stop()
        log.info("Stopped meteor secondary task")


def _start_secondary_task(task):
    """Start the given task on the secondary dongle."""
    cfg = load_config()
    dev = cfg.get("secondary_dongle", {}).get("device_index", 1)

    if task == "adsb" and adsb_receiver:
        adsb_receiver.device_index = dev
        adsb_receiver.start()
        log.info("Started ADS-B on secondary dongle (device %d)", dev)
    elif task == "meteor" and meteor_detector:
        meteor_detector.device_index = dev
        meteor_detector.start()
        log.info("Started meteor detector on secondary dongle (device %d)", dev)
    elif task == "wefax":
        # WEFAX runs via scheduler — just log that it will use secondary dongle
        log.info("WEFAX configured for secondary dongle (device %d)", dev)


@app.route("/api/config")
def api_config_get():
    """Return the full runtime settings block (keywords + thresholds)."""
    return jsonify(get_settings())


@app.route("/api/config", methods=["POST"])
def api_config_set():
    """Merge a partial settings patch, persist it, and apply it live."""
    patch = request.get_json(force=True)
    if not isinstance(patch, dict):
        return jsonify({"error": "Body must be a JSON object"}), 400

    # Validate the keyword list shape if present
    if "keywords" in patch:
        cleaned, err = _sanitize_keywords(patch["keywords"])
        if err:
            return jsonify({"error": err}), 400
        patch["keywords"] = cleaned

    settings = update_settings(patch)
    _apply_settings(settings)
    log.info("Settings updated: %s", ", ".join(sorted(patch.keys())))
    return jsonify(settings)


def _sanitize_keywords(raw):
    """Normalise the keyword list from the UI. Returns (list, error_or_None)."""
    if not isinstance(raw, list):
        return None, "keywords must be a list"
    cleaned = []
    for item in raw:
        if isinstance(item, str):
            item = {"term": item}
        if not isinstance(item, dict):
            return None, "each keyword must be a string or object"
        term = str(item.get("term", "")).strip()
        if not term:
            continue
        severity = item.get("severity", "info")
        if severity not in ("info", "warning", "critical"):
            severity = "info"
        cleaned.append({
            "term": term,
            "severity": severity,
            "enabled": bool(item.get("enabled", True)),
        })
    return cleaned, None


def _apply_settings(settings):
    """Push runtime settings into the modules that own each threshold."""
    try:
        sei_model.apply_settings(settings)
        iq_segmenter.apply_settings(settings)
        transcriber.apply_settings(settings)
        signal_classifier.apply_settings(settings)
    except Exception as e:
        log.warning("Failed to apply some settings: %s", e)


@app.route("/api/status")
def api_status():
    return jsonify(_get_status())


@app.route("/audio-stream")
def audio_stream():
    def generate():
        yield from audio_stream_generator(input_source.audio_queue)

    response = Response(
        stream_with_context(generate()),
        mimetype="audio/wav",
    )
    response.headers["Cache-Control"] = "no-cache, no-store"
    response.headers["X-Accel-Buffering"] = "no"
    return response


# ── Socket.IO Events ──

@socketio.on("connect")
def on_connect():
    log.info("Client connected")
    socketio.emit("mode", {
        "mode": mode,
        "version": VERSION,
        "sdr_available": sdr_available,
        "transcriber_backend": transcriber.backend,
        "adsb_enabled": ADSB_ENABLED,
    })
    socketio.emit("status", _get_status())


# ── Helpers ──

def _get_status():
    preset = input_source.current_preset or {}
    return {
        "running": input_source.is_running,
        "freq": preset.get("freq", ""),
        "label": preset.get("label", ""),
        "mode": mode,
        "squelch": input_source.squelch,
        "gain": input_source.gain,
        "sdr": sdr_arbiter.snapshot(),
        "sample_rate": input_source.sample_rate,
        "effective_sample_rate": input_source.effective_sample_rate,
        "deemp": input_source.deemp,
        "effective_deemp": input_source.effective_deemp,
        "ppm": input_source.ppm,
        "direct_sampling": input_source.direct_sampling,
        "resample_rate": "16k",
        "sdr_available": sdr_available,
        "sdr_connected": input_source.sdr_connected,
        "transcriber_backend": transcriber.backend,
        "adsb_enabled": ADSB_ENABLED,
        "adsb_scanning": adsb_scheduler.is_scanning if adsb_scheduler else False,
        "adsb_dedicated": adsb_receiver.is_running if adsb_receiver else False,
        "ais_dedicated": ais_receiver.is_running,
        "ism_running": ism_receiver.is_running,
        "acars_running": acars_receiver.is_running,
        "pager_running": pager_receiver.is_running,
        "apt_mode": input_source.apt_mode,
        "apt_recording": apt_decoder.is_recording,
        "wefax_enabled": WEFAX_ENABLED,
        "wefax_mode": input_source.wefax_mode,
        "wefax_recording": wefax_receiver.is_recording,
        "meteor_enabled": True,
        "meteor_mode": input_source.meteor_mode,
        "meteor_running": meteor_detector.is_running,
        "classifier_active": signal_classifier.is_active,
        "classifier_backend": signal_classifier.backend,
        "sei_active": sei_model.is_active,
        "sei_backend": sei_model.backend,
        "sei_emitter_count": sei_model.get_status()["emitter_count"],
        "secondary_task": get_secondary_task(),
        "secondary_running": _get_secondary_running(),
    }


def _broadcast_status():
    socketio.emit("status", _get_status())


# ── SDR health check thread ──

def stats_broadcast_loop():
    """Broadcast inference stats every 5s to keep UI updated during silence."""
    while not _signal_stop.is_set():
        eventlet.sleep(5)
        socketio.emit("inference_stats", transcriber.stats)


def adsb_broadcast_loop():
    """Push ADS-B flight updates to clients every 2s."""
    while not _signal_stop.is_set():
        eventlet.sleep(2)
        if adsb_receiver:
            flights = adsb_receiver.get_flights()
            if flights:
                socketio.emit("adsb_update", flights)


def ais_broadcast_loop():
    """Push AIS vessel updates to clients every 2s."""
    while not _signal_stop.is_set():
        eventlet.sleep(2)
        if ais_receiver.is_running:
            vessels = ais_receiver.get_vessels()
            if vessels:
                socketio.emit("ais_update", vessels)


def ism_broadcast_loop():
    """Push the full ISM device table (with TTL expiry) to clients every 3s."""
    while not _signal_stop.is_set():
        eventlet.sleep(3)
        if ism_receiver.is_running:
            socketio.emit("ism_update", ism_receiver.get_devices())


def acars_broadcast_loop():
    """Push the ACARS aircraft table (with TTL expiry) to clients every 3s."""
    while not _signal_stop.is_set():
        eventlet.sleep(3)
        if acars_receiver.is_running:
            socketio.emit("acars_update", acars_receiver.get_messages())


def pager_broadcast_loop():
    """Push the pager address table (with TTL expiry) to clients every 3s."""
    while not _signal_stop.is_set():
        eventlet.sleep(3)
        if pager_receiver.is_running:
            socketio.emit("pager_update", pager_receiver.get_pages())


def iq_pipeline_emit_loop():
    """Emit buffered IQ pipeline data via Socket.IO (runs in eventlet greenlet).

    The IQ capture callback runs in a real OS thread and cannot call
    socketio.emit directly. This loop polls for buffered data and emits
    it safely from the eventlet context.
    """
    global _pending_spectrogram_row, _pending_classification
    while not _signal_stop.is_set():
        eventlet.sleep(0.3)  # ~3 fps for spectrogram waterfall

        row = _pending_spectrogram_row
        if row is not None:
            _pending_spectrogram_row = None
            socketio.emit("spectrogram_row", row)

        clf = _pending_classification
        if clf is not None:
            _pending_classification = None
            socketio.emit("signal_classified", clf)


def meteor_stats_loop():
    """Push meteor stats update every 60s."""
    while not _signal_stop.is_set():
        eventlet.sleep(60)
        if meteor_detector and meteor_detector.is_running:
            events = meteor_detector.get_events(limit=10000)
            stats = meteor_analyzer.get_session_stats(events)
            hourly = meteor_analyzer.get_hourly_stats(events, hours=24)
            current_shower = meteor_analyzer.get_current_shower()
            stats["hourly"] = hourly
            stats["shower"] = current_shower["name"] if current_shower else None
            stats["baseline_dbm"] = round(meteor_detector.baseline_power_db, 1)
            stats["frequency_hz"] = meteor_detector.frequency_hz
            socketio.emit("meteor_stats_update", stats)


def sdr_health_loop():
    """Poll every 10s to detect SDR disconnect / process crash, with auto-recovery."""
    _crash_count = 0
    MAX_AUTO_RETRIES = 3

    while not _signal_stop.is_set():
        eventlet.sleep(10)
        if _signal_stop.is_set() or _shutdown_called:
            break

        # Check SDR hardware presence (only in SDR mode)
        if mode == "SDR":
            was_connected = input_source.sdr_connected
            is_connected = input_source.check_sdr_connected()

            # SDR just came back — auto-recover if we had a preset
            if not was_connected and is_connected and input_source.current_preset:
                log.info("SDR reconnected — auto-recovering")
                _crash_count = 0
                input_source.restart()
                _broadcast_status()
                continue

        # Check process health
        if not input_source.is_running:
            continue
        if not input_source.poll():
            _crash_count += 1
            log.warning("Input source process crashed (attempt %d/%d)",
                        _crash_count, MAX_AUTO_RETRIES)

            if _crash_count <= MAX_AUTO_RETRIES:
                socketio.emit("error", {
                    "message": "Audio source crashed — auto-restarting (attempt %d/%d)..." % (_crash_count, MAX_AUTO_RETRIES),
                    "type": "process_crash",
                    "recoverable": True,
                })
                eventlet.sleep(2)  # brief delay before restart
                if input_source.restart():
                    log.info("Auto-restart succeeded")
                    _broadcast_status()
                    continue

            socketio.emit("error", {
                "message": "Audio source crashed after %d retries. Use Retry to restart." % MAX_AUTO_RETRIES,
                "type": "process_crash",
                "recoverable": True,
            })
            _broadcast_status()


# ── Shutdown ──

_shutdown_called = False


def _do_shutdown(signum=None):
    """Actual shutdown work — runs in a greenlet, safe to call blocking functions."""
    sig_name = signal.Signals(signum).name if signum else "atexit"
    log.info("Shutting down (triggered by %s)...", sig_name)

    _signal_stop.set()
    ipc_server.stop()
    input_source.stop()
    transcriber.stop()
    if adsb_receiver:
        adsb_receiver.stop()
    if adsb_scheduler:
        adsb_scheduler.stop()
    ais_receiver.stop()
    ism_receiver.stop()
    acars_receiver.stop()
    pager_receiver.stop()
    apt_scheduler.stop()
    apt_decoder.stop()
    wefax_scheduler.stop()
    wefax_receiver.stop()
    meteor_detector.stop()
    signal_classifier.stop()
    sei_model.stop()
    iq_segmenter.reset()

    if signum == signal.SIGTERM:
        socketio.stop()


def shutdown(signum=None, frame=None):
    global _shutdown_called
    if _shutdown_called:
        return
    _shutdown_called = True

    if signum == signal.SIGINT:
        # Restore default handler so a second Ctrl+C force-kills immediately
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Run shutdown in a background greenlet to avoid blocking the mainloop
    socketio.start_background_task(_do_shutdown, signum)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
atexit.register(shutdown)


def _auto_tune_on_startup():
    """Tune the configured startup preset so the node collects unattended.

    Only plain audio presets are restored — dedicated modes (ADS-B, AIS,
    Science, WEFAX) hold the dongle outright and are left for the operator,
    so an unattended boot never blocks a scheduled satellite pass.
    """
    preset_id = get_startup_preset(_config)
    if not preset_id:
        log.info("No startup preset configured — starting idle")
        return

    preset = get_preset_by_id(preset_id)
    if not preset:
        log.warning("Startup preset %r not found — starting idle", preset_id)
        return

    if preset.get("mode") in ("adsb", "ais") or preset.get("category") in ("science", "wefax"):
        log.warning("Startup preset %r is a dedicated mode — starting idle", preset_id)
        return

    if input_source.tune(preset):
        transcriber.set_preset(preset)
        # Tell the arbiter what the hardware is actually doing, so the console's
        # ACTUAL field is populated from boot rather than after the first command.
        sdr_arbiter.adopt(preset)
        log.info("Auto-tuned startup preset: %s (%s)",
                 preset.get("label", ""), preset.get("freq", ""))
    else:
        log.error("Failed to auto-tune startup preset %r", preset_id)


# ── Main ──

if __name__ == "__main__":
    log.info("Starting ravenSDR v%s...", VERSION)
    _apply_settings(get_settings())  # push persisted thresholds into the modules
    transcriber.start()
    # Tune before the schedulers start, so a pass firing at boot preempts us
    # rather than us stealing the dongle out from under an active recording.
    _auto_tune_on_startup()
    # Drain real-thread events into the hub. Start this FIRST: until it runs,
    # anything the hardware threads emit only queues up.
    socketio.start_background_task(emit_bridge_loop)
    # Single worker that owns every SDR switch, serialized and coalescing.
    sdr_arbiter.start(spawn_fn=socketio.start_background_task)
    # Serve the radio half of the UI/radio boundary (phase 18).
    try:
        ipc_server.start()
    except OSError as e:
        log.error("Could not start IPC server on %s: %s — UI processes cannot "
                  "connect, built-in web UI still works", RADIO_SOCKET_PATH, e)
    socketio.start_background_task(signal_meter_loop)
    socketio.start_background_task(sdr_health_loop)
    socketio.start_background_task(stats_broadcast_loop)
    if ADSB_ENABLED:
        socketio.start_background_task(adsb_broadcast_loop)
    socketio.start_background_task(ais_broadcast_loop)
    socketio.start_background_task(ism_broadcast_loop)
    socketio.start_background_task(acars_broadcast_loop)
    socketio.start_background_task(pager_broadcast_loop)
    apt_scheduler.start()
    if WEFAX_ENABLED:
        wefax_scheduler.start()
    else:
        log.info("WEFAX disabled (WEFAX_ENABLED=false) — scheduler not started")
    socketio.start_background_task(meteor_stats_loop)
    socketio.start_background_task(iq_pipeline_emit_loop)
    # Start secondary dongle task from config (if configured)
    if _secondary_task and _secondary_task != "adsb":  # ADS-B already started above
        _start_secondary_task(_secondary_task)
        log.info("Secondary dongle task auto-started: %s", _secondary_task)
    if METEOR_ENABLED and METEOR_DUAL_DONGLE:
        meteor_detector.start()
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
