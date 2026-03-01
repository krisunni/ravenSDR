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
from ravensdr.adsb_correlator import extract_callsigns, match_flights
from ravensdr.noaa_parser import detect_priority_alert

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

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

# ── ADS-B Receiver ──
adsb_receiver = None
adsb_scheduler = None

if ADSB_ENABLED:
    device_idx = 1 if ADSB_DUAL_DONGLE else 0
    adsb_receiver = AdsbReceiver(device_index=device_idx, dual_dongle=ADSB_DUAL_DONGLE)

    if ADSB_DUAL_DONGLE:
        # Dual-dongle: start immediately and run continuously
        adsb_receiver.start()
        log.info("ADS-B receiver started (dual-dongle mode, device %d)", device_idx)
    else:
        # Single-dongle: time-share with rtl_fm via scan scheduler
        adsb_scheduler = AdsbScanScheduler(adsb_receiver, input_source)
        log.info("ADS-B scan scheduler configured (single-dongle mode)")

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

# ── Weather state ──
_latest_weather = None


def _on_weather_update(parsed_data):
    """Handle parsed NOAA weather data from the transcriber post-processor."""
    global _latest_weather
    _latest_weather = parsed_data
    socketio.emit("weather_update", parsed_data)

    if detect_priority_alert(parsed_data.get("raw_transcript", "")):
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
    """Sample RMS from the audio queue every 500ms and emit signal_level."""
    import numpy as np
    while not _signal_stop.is_set():
        eventlet.sleep(0.5)
        if not input_source.is_running:
            continue
        # Signal level is emitted from the transcriber's inference loop
        # This thread exists as a fallback / heartbeat
        preset = input_source.current_preset or {}
        socketio.emit("signal_level", {
            "rms": 0,
            "freq": preset.get("freq", ""),
        })


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

    # Check if web stream mode and no stream_url
    if mode == "WEBSTREAM" and not preset.get("stream_url"):
        return jsonify({"error": "No web stream available for this preset (SDR only)"}), 400

    success = input_source.tune(preset)
    if not success:
        return jsonify({"error": "Failed to tune"}), 500

    transcriber.set_preset(preset)
    _broadcast_status()

    return jsonify({"status": "tuned", "preset": preset})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    input_source.stop()
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


@app.route("/api/weather/current")
def api_weather_current():
    if _latest_weather is None:
        return jsonify({"error": "No weather data received yet"}), 404
    return jsonify(_latest_weather)


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
        "sdr_available": sdr_available,
        "sdr_connected": input_source.sdr_connected,
        "transcriber_backend": transcriber.backend,
        "adsb_enabled": ADSB_ENABLED,
        "adsb_scanning": adsb_scheduler.is_scanning if adsb_scheduler else False,
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
        if adsb_receiver and adsb_receiver.is_running:
            socketio.emit("adsb_update", adsb_receiver.get_flights())


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


def shutdown(signum=None, frame=None):
    global _shutdown_called
    if _shutdown_called:
        return
    _shutdown_called = True

    sig_name = signal.Signals(signum).name if signum else "atexit"
    log.info("Shutting down (triggered by %s)...", sig_name)

    _signal_stop.set()
    input_source.stop()
    transcriber.stop()
    if adsb_receiver:
        adsb_receiver.stop()
    if adsb_scheduler:
        adsb_scheduler.stop()

    if signum == signal.SIGTERM:
        socketio.stop()

    if signum == signal.SIGINT:
        # Restore default handler so a second Ctrl+C force-kills immediately
        signal.signal(signal.SIGINT, signal.SIG_DFL)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
atexit.register(shutdown)


# ── Main ──

if __name__ == "__main__":
    log.info("Starting ravenSDR...")
    transcriber.start()
    socketio.start_background_task(signal_meter_loop)
    socketio.start_background_task(sdr_health_loop)
    socketio.start_background_task(stats_broadcast_loop)
    if ADSB_ENABLED:
        socketio.start_background_task(adsb_broadcast_loop)
        if adsb_scheduler:
            adsb_scheduler.start()
    socketio.run(app, host="0.0.0.0", port=5000, debug=False)
