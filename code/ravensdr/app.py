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
from ravensdr.input_source import InputSource, detect_sdr
from ravensdr.presets import get_presets, get_preset_by_id, CATEGORY_LABELS
from ravensdr.transcriber import Transcriber
from ravensdr.adsb_receiver import (
    AdsbReceiver, AdsbScanScheduler,
    ADSB_ENABLED, ADSB_DUAL_DONGLE,
)
from ravensdr.ais_receiver import AisReceiver
from ravensdr.adsb_correlator import extract_callsigns, match_flights
from ravensdr.noaa_parser import WeatherAccumulator, detect_priority_alert
from ravensdr.apt_scheduler import AptScheduler
from ravensdr.apt_decoder import AptDecoder
from ravensdr.wefax_scheduler import WefaxScheduler
from ravensdr.wefax_receiver import WefaxReceiver
from ravensdr.meteor_detector import MeteorDetector, METEOR_ENABLED, METEOR_DUAL_DONGLE, METEOR_FREQUENCY
from ravensdr.meteor_analyzer import MeteorAnalyzer
from ravensdr.signal_classifier import SignalClassifier, iq_to_spectrogram, spectrogram_to_image
from ravensdr.sei_model import SEIModel
from ravensdr.iq_segmenter import IQSegmenter
from ravensdr.config import load_config, save_config, get_secondary_task, set_secondary_task

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

VERSION = "1.1.0"

# ── Flask + Socket.IO ──
app = Flask(
    __name__,
    template_folder="../templates",
    static_folder="../static",
)
app.config["SECRET_KEY"] = "ravensdr-dev"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ── Detect mode ──
sdr_available = detect_sdr()
mode = "SDR" if sdr_available else "WEBSTREAM"
log.info("Mode: %s (SDR detected: %s)", mode, sdr_available)

# ── Core components ──
input_source = InputSource(mode)
transcriber = Transcriber(input_source.pcm_queue, emit_fn=socketio.emit)

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

    # Wire transcript callback for callsign correlation
    def _on_transcript(text):
        if not adsb_receiver:
            return
        callsigns = extract_callsigns(text)
        if callsigns:
            matches = match_flights(callsigns, adsb_receiver.get_flights())
            if matches:
                socketio.emit("callsign_match", {
                    "transcript": text,
                    "matches": matches,
                })

    transcriber.set_transcript_callback(_on_transcript)

# ── AIS Receiver ──
ais_receiver = AisReceiver(device_index=0)

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

# ── APT Satellite Imaging ──
apt_decoder = AptDecoder(emit_fn=socketio.emit)


def _on_apt_pass_start(pass_info):
    """Called by scheduler when a satellite pass begins — start recording."""
    satellite = pass_info.get("satellite", "")
    frequency = pass_info.get("frequency", "")

    if input_source.enter_apt_mode(frequency):
        apt_decoder.record_pass(pass_info)
        socketio.emit("status", _get_status())

        # Schedule exit from APT mode after recording duration
        def _exit_apt():
            import eventlet as _ev
            _ev.sleep(pass_info.get("duration", 900) + 30)
            if input_source.apt_mode:
                input_source.exit_apt_mode()
                socketio.emit("status", _get_status())

        socketio.start_background_task(_exit_apt)
    else:
        log.warning("Could not enter APT mode for %s", satellite)


apt_scheduler = AptScheduler(emit_fn=socketio.emit, on_pass_start=_on_apt_pass_start)

# ── WEFAX Weather Fax ──
wefax_receiver = WefaxReceiver(emit_fn=socketio.emit)


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


wefax_scheduler = WefaxScheduler(emit_fn=socketio.emit, on_broadcast_start=_on_wefax_broadcast_start)

# ── Meteor Scatter Detection ──
meteor_analyzer = MeteorAnalyzer()

_meteor_is_secondary = (_secondary_task == "meteor")
_meteor_device_idx = _secondary_device if _meteor_is_secondary else 0
meteor_detector = MeteorDetector(
    emit_fn=socketio.emit,
    frequency_hz=METEOR_FREQUENCY,
    device_index=_meteor_device_idx,
)
meteor_detector.load_events_from_log()

# ── Signal Classifier ──
import os as _os
_classifier_hef = _os.environ.get("CLASSIFIER_HEF_PATH")
_classifier_classes = _os.environ.get("CLASSIFIER_CLASSES_PATH")
signal_classifier = SignalClassifier(
    emit_fn=socketio.emit,
    hef_path=_classifier_hef,
    class_map_path=_classifier_classes,
)
log.info("Signal classifier initialized (backend: %s)", signal_classifier.backend)

# ── Specific Emitter Identification ──
_sei_hef = _os.environ.get("SEI_HEF_PATH")
sei_model = SEIModel(emit_fn=socketio.emit, hef_path=_sei_hef)
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

# Wire analyzer to tag shower info on each detection
_original_meteor_emit = socketio.emit


def _meteor_emit_wrapper(event, data, **kw):
    if event == "meteor_detection" and isinstance(data, dict):
        meteor_analyzer.tag_event_shower(data)
    _original_meteor_emit(event, data, **kw)


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
    data = request.get_json(force=True)
    preset_id = data.get("preset_id")
    preset = get_preset_by_id(preset_id)
    if not preset:
        return jsonify({"error": "Unknown preset"}), 400

    # Start weather accumulation fresh when the station changes
    if input_source.current_preset is None or \
            input_source.current_preset.get("id") != preset.get("id"):
        _weather_accumulator.reset()

    # Check if web stream mode and no stream_url
    if mode == "WEBSTREAM" and not preset.get("stream_url"):
        return jsonify({"error": "No web stream available for this preset (SDR only)"}), 400

    # Science tab: display-only, start meteor detector if not running
    if preset.get("category") == "science":
        input_source.stop()
        input_source.current_preset = preset
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
        return jsonify({"status": "tuned", "preset": preset})

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
        # Stop AIS if active
        if ais_receiver.is_running:
            ais_receiver.stop()
        _broadcast_status()
        return jsonify({"status": "tuned", "preset": preset})

    is_adsb = preset.get("mode") == "adsb"
    is_ais = preset.get("mode") == "ais"

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
            return jsonify({"error": "Failed to start rtl_ais"}), 500
        log.info("AIS dedicated mode — rtl_ais running continuously")
        _broadcast_status()
        return jsonify({"status": "tuned", "preset": preset})

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
            return jsonify({"error": "Failed to start dump1090"}), 500
        log.info("ADS-B dedicated mode — dump1090 running continuously")
        _broadcast_status()
        return jsonify({"status": "tuned", "preset": preset})

    # Switching away from ADS-B: stop dedicated dump1090, restart scheduler
    if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
        adsb_receiver.stop()
        if adsb_scheduler:
            adsb_scheduler.start()

    success = input_source.tune(preset)
    if not success:
        return jsonify({"error": "Failed to tune"}), 500

    transcriber.set_preset(preset)
    _broadcast_status()

    return jsonify({"status": "tuned", "preset": preset})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    input_source.stop()
    # Stop dedicated AIS mode if active
    if ais_receiver.is_running:
        ais_receiver.stop()
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


@app.route("/api/sei/status")
def api_sei_status():
    return jsonify(sei_model.get_status())


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
        "apt_mode": input_source.apt_mode,
        "apt_recording": apt_decoder.is_recording,
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
    input_source.stop()
    transcriber.stop()
    if adsb_receiver:
        adsb_receiver.stop()
    if adsb_scheduler:
        adsb_scheduler.stop()
    ais_receiver.stop()
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


# ── Main ──

if __name__ == "__main__":
    log.info("Starting ravenSDR v%s...", VERSION)
    transcriber.start()
    socketio.start_background_task(signal_meter_loop)
    socketio.start_background_task(sdr_health_loop)
    socketio.start_background_task(stats_broadcast_loop)
    if ADSB_ENABLED:
        socketio.start_background_task(adsb_broadcast_loop)
    socketio.start_background_task(ais_broadcast_loop)
    apt_scheduler.start()
    wefax_scheduler.start()
    socketio.start_background_task(meteor_stats_loop)
    socketio.start_background_task(iq_pipeline_emit_loop)
    # Start secondary dongle task from config (if configured)
    if _secondary_task and _secondary_task != "adsb":  # ADS-B already started above
        _start_secondary_task(_secondary_task)
        log.info("Secondary dongle task auto-started: %s", _secondary_task)
    if METEOR_ENABLED and METEOR_DUAL_DONGLE:
        meteor_detector.start()
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
