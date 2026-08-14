# Flask app, routes, Socket.IO events
import eventlet
eventlet.monkey_patch()

import atexit
import logging
import signal
import sys
import threading
import time as _time

import numpy as np

from flask import (Flask, Response, jsonify, make_response, render_template,
                   request, send_from_directory, stream_with_context)
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
from ravensdr.spectrum_scanner import SpectrumScanner, BANDS
from ravensdr.ais_receiver import AisReceiver
from ravensdr.ism_receiver import IsmReceiver
from ravensdr.acars_receiver import AcarsReceiver, correlate_with_adsb
from ravensdr.pager_receiver import PagerReceiver
from ravensdr.aprs_receiver import AprsReceiver
from ravensdr.observation_log import ObservationLog
from ravensdr.iq_collector import IQCollector
from ravensdr.iq_collect_scheduler import IQCollectScheduler
from ravensdr.adsb_correlator import extract_callsigns, match_flights
from ravensdr.noaa_parser import WeatherAccumulator, detect_priority_alert
from ravensdr.apt_scheduler import AptScheduler
from ravensdr.apt_decoder import AptDecoder
from ravensdr.wefax_scheduler import WefaxScheduler, WEFAX_ENABLED
from ravensdr.wefax_receiver import WefaxReceiver
from ravensdr.meteor_detector import MeteorDetector, METEOR_ENABLED, METEOR_DUAL_DONGLE, METEOR_FREQUENCY
from ravensdr.meteor_analyzer import MeteorAnalyzer
from ravensdr.signal_classifier import (SignalClassifier, iq_to_spectrogram,
                                        spectrogram_to_image, MODULATION_CLASSES,
                                        CLASS_VALIDATION)
from ravensdr.sei_model import SEIModel
from ravensdr.iq_segmenter import IQSegmenter, compute_power_db
from ravensdr.config import (
    load_config, save_config, get_secondary_task, set_secondary_task,
    get_startup_preset, set_last_preset, get_settings, update_settings,
    get_automation, set_automation, is_automation_enabled,
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

VERSION = "1.3.0"

# Modes whose IQ never reaches the classifier: a separate dongle, or a decoder
# that consumes the stream itself. Their presets must not label the corpus.
NON_IQ_MODES = {"adsb", "ais"}

# ── Who gets the radio ──
#
# One dongle, three kinds of claimant, and they are NOT equally urgent:
#
#   operator    an explicit tune. Wins immediately — a person asked.
#   scheduled   a satellite pass or WEFAX slot. Happens now or not at all,
#               so it may pre-empt; _start_collect_slot already yields to it.
#   background  IQ corpus collection. Opportunistic, no deadline, and it has
#               no business taking the radio from someone who is listening.
#
# Lumping all three under one "Auto" toggle meant tuning to weather and getting
# silence, because the corpus rotation took the dongle on its next slot. Worse,
# it made the toggle modal: you had to know to switch automation off BEFORE
# tuning, or your click quietly did not stick.
#
# Collection now runs only on an idle radio. Any operator action refreshes the
# lease; when it lapses, collecting resumes on its own. Nobody has to know the
# toggle exists to use the radio.
OPERATOR_LEASE_S = 600          # 10 min, refreshed by any interaction
_last_operator_action = 0.0


def note_operator_action():
    """Mark the radio as in use by a person."""
    global _last_operator_action
    _last_operator_action = _time.time()


def operator_lease_remaining():
    """Seconds until background collection may resume; 0 when it is free."""
    if not _last_operator_action:
        return 0.0
    return max(0.0, OPERATOR_LEASE_S - (_time.time() - _last_operator_action))

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
# emit_safe, not _late_emit: the inference loop is a real OS thread now, and
# socketio.emit from there hits green locks in ipc_server.broadcast.
transcriber = Transcriber(input_source.pcm_queue, emit_fn=emit_safe)

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
        # resume_fn goes through the arbiter: the scan loop moving the radio
        # directly leaves the arbiter's 'actual' stale, so the console reports
        # LOCKED on a frequency the hardware is not on. Late-bound because
        # sdr_arbiter is constructed further down.
        adsb_scheduler = AdsbScanScheduler(
            adsb_receiver, input_source,
            resume_fn=lambda preset: sdr_arbiter.request(preset))
        log.info("ADS-B configured (on-demand via Aviation tab)")

# ── Spectrum scanner ──
# emit_safe: the sweep reader is a real OS thread (blocking readline on
# rtl_power's stdout), so it must not touch socketio directly.
spectrum_scanner = SpectrumScanner(emit_fn=emit_safe, device_index=0)

# ── AIS Receiver ──
ais_receiver = AisReceiver(device_index=0)

# ── Durable emitter history ──
# Decoder tables are in-memory with a TTL, so a meter that beacons every 15
# minutes disappears between transmissions and everything is lost on restart.
# This records first/last-seen and counts per ID so devices can be tracked over
# days rather than minutes.
observations = ObservationLog().load()

# ── ISM sensor receiver (rtl_433) ──
ism_receiver = IsmReceiver(device_index=0)


def _ism_on_record(record, is_new):
    """Emit each rtl_433 device update to the ISM panel.

    Called from rtl_433's REAL reader thread — emit via the bridge.
    """
    observations.observe("ism", record.get("id"),
                         meta={"model": record.get("model")},
                         rssi=record.get("rssi"))
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

# ── APRS receiver (rtl_fm | multimon-ng AFSK1200) ──
aprs_receiver = AprsReceiver(device_index=0)


def _aprs_on_record(record, is_new):
    """Emit each decoded APRS packet.

    Called from multimon-ng's REAL reader thread — emit via the bridge.
    """
    observations.observe("aprs", record.get("source"),
                         meta={"model": record.get("type"),
                               "dest": record.get("dest")})
    emit_safe("aprs_packet", record)


aprs_receiver.on_record = _aprs_on_record

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


def _dedicated_decoders():
    """Every decoder that opens the RTL-SDR directly, as (receiver, label).

    One list instead of the eleven hand-maintained tuples this replaced. Those
    had drifted: aprs_receiver was added later and never joined the APT, WEFAX,
    pager or /api/stop sets, so an APRS session held the dongle straight through
    a scheduled satellite pass. Several branches were only safe by accident of
    ordering — a fall-through stop below them happened to catch what their own
    tuple missed. Adding a decoder should mean editing one place.
    """
    return (
        (ism_receiver, "ISM"),
        (acars_receiver, "ACARS"),
        (pager_receiver, "Pager"),
        (aprs_receiver, "APRS"),
        (ais_receiver, "AIS"),
        (spectrum_scanner, "Spectrum sweep"),
    )


def _stop_dedicated(reason, keep=None, notice_type="preempt"):
    """Stop every dedicated decoder except `keep`. Returns the labels stopped."""
    stopped = []
    for rx, name in _dedicated_decoders():
        if rx is None or rx is keep or not rx.is_running:
            continue
        rx.stop()
        stopped.append(name)
        log.info("Stopped %s — %s", name, reason)
        emit_safe("notice", {
            "message": f"{name} paused — {reason}",
            "type": notice_type,
        })
    return stopped


def _on_apt_pass_start(pass_info):
    """Called by scheduler when a satellite pass begins — start recording."""
    satellite = pass_info.get("satellite", "")
    frequency = pass_info.get("frequency", "")

    # Check automation BEFORE taking anything. This used to sit below the
    # decoder stops, so with automation off a pass would preempt every decoder,
    # log "not preempting the SDR", and return — leaving the dongle unclaimed
    # and nothing restarted.
    if not is_automation_enabled("apt"):
        log.info("Automation off — not preempting the SDR for %s pass", satellite)
        emit_safe("notice", {
            "message": f"{satellite} pass in progress — not recorded (automation off)",
            "type": "automation_skipped",
        })
        return

    reason = f"SDR dedicated to {satellite} pass"

    # Corpus collection first: it is the only one that can already be holding
    # the device with no deadline of its own, and it is what cost 11 of 22
    # passes an empty WAV.
    if iq_collect_scheduler and iq_collect_scheduler.preempt():
        log.info("Preempted IQ collect slot for %s pass", satellite)
        emit_safe("notice", {"message": f"IQ collect paused — {reason}",
                             "type": "apt_preempt"})

    if meteor_detector and meteor_detector.is_running and not METEOR_DUAL_DONGLE:
        meteor_detector.stop()
        log.info("Stopped meteor detector for APT recording")
        emit_safe("notice", {"message": f"Meteor detector paused — {reason}",
                             "type": "apt_preempt"})

    _stop_dedicated(reason, notice_type="apt_preempt")

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



def _resume_adsb_scan():
    """Restart opportunistic ADS-B scanning, unless automation is paused."""
    if adsb_scheduler and is_automation_enabled("adsb_scan"):
        adsb_scheduler.start()


def _on_wefax_broadcast_start(broadcast_info):
    """Called by scheduler when a WEFAX broadcast begins — start recording."""
    frequency_khz = broadcast_info.get("frequency_khz", 0)

    if not is_automation_enabled("wefax"):
        log.info("Automation off — not preempting the SDR for WEFAX %s kHz",
                 frequency_khz)
        return

    reason = f"SDR dedicated to WEFAX {frequency_khz} kHz"

    # This handler used to preempt only meteor and ADS-B, so a WEFAX slot
    # firing during ISM/ACARS/pager/APRS would flip the mode flag (refusing all
    # tunes) and then write a zero-byte WAV. Same registry as the APT path.
    if iq_collect_scheduler and iq_collect_scheduler.preempt():
        log.info("Preempted IQ collect slot for WEFAX recording")
        emit_safe("notice", {"message": f"IQ collect paused — {reason}",
                             "type": "wefax_preempt"})

    if meteor_detector and meteor_detector.is_running and not METEOR_DUAL_DONGLE:
        meteor_detector.stop()
        log.info("Stopped meteor detector for WEFAX recording")

    _stop_dedicated(reason, notice_type="wefax_preempt")

    # Stop ADS-B if it's holding the device (single-dongle mode)
    if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
        adsb_receiver.stop()
        if adsb_scheduler:
            adsb_scheduler.stop()
        log.info("Stopped ADS-B for WEFAX recording")

    if input_source.enter_wefax_mode(frequency_khz):
        wefax_receiver.record_broadcast(broadcast_info)
        socketio.emit("status", _get_status())

        # Schedule exit from WEFAX mode once recording actually finishes.
        #
        # This used to blind-sleep duration+30s. record_broadcast() returns True
        # for merely spawning the capture, and the capture can die in ~4s (no HF
        # antenna, dongle busy) — after which the radio stayed pinned in WEFAX
        # mode for the full ten-plus minutes, refusing every tune, with nothing
        # being recorded. _exit_apt was rewritten to poll for exactly this
        # reason; the fix was never carried across.
        def _exit_wefax():
            import eventlet as _ev
            duration_min = broadcast_info.get("duration_minutes", 10)
            deadline = duration_min * 60 + 30
            grace = 20          # capture needs a moment to open the device
            waited = 0
            while waited < deadline:
                _ev.sleep(2)
                waited += 2
                if not input_source.wefax_mode:
                    return      # someone else already released it
                if waited > grace and not wefax_receiver.is_recording:
                    log.warning("WEFAX capture is not running after %ds — "
                                "releasing the SDR instead of holding it for "
                                "the whole %d min slot", waited, duration_min)
                    break
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
# Same defaulting as the ONNX below: a .hef dropped into models/ should be
# picked up without anyone having to set an environment variable. Without this
# the NPU path could only ever be reached by remembering CLASSIFIER_HEF_PATH.
_classifier_hef = _os.environ.get("CLASSIFIER_HEF_PATH") or _os.path.join(
    _os.path.dirname(__file__), "models", "signal_classifier_h8l.hef")
_classifier_classes = _os.environ.get("CLASSIFIER_CLASSES_PATH") or _os.path.join(
    _os.path.dirname(__file__), "models", "signal_classifier_classes.json")
# Default to the trained ONNX model shipped in models/ when no explicit path is
# given, so a model dropped there is picked up without touching the environment.
_classifier_onnx = _os.environ.get("CLASSIFIER_ONNX_PATH") or _os.path.join(
    _os.path.dirname(__file__), "models", "signal_classifier.onnx")
signal_classifier = SignalClassifier(
    emit_fn=_late_emit,
    hef_path=_classifier_hef,
    class_map_path=_classifier_classes,
    onnx_path=_classifier_onnx,
)
log.info("Signal classifier initialized (backend: %s)", signal_classifier.backend)

# ── Specific Emitter Identification ──
_sei_hef = _os.environ.get("SEI_HEF_PATH")
# emit_safe, not _late_emit: identify() is reached from the IQ collector's
# real OS thread (iq_collector._read_loop -> classify_iq -> _forward_to_sei),
# and socketio.emit from there touches green locks and a green socket in
# ipc_server.broadcast — the greenlet.error emit_bridge exists to prevent.
# The classifier was nulled and meteor was wrapped; SEI was missed.
sei_model = SEIModel(emit_fn=emit_safe, hef_path=_sei_hef)
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


def _classify_and_waterfall(iq_samples, frequency_hz, expected=None):
    """Classify a chunk and build a waterfall row, buffered for the hub.

    Called from REAL OS threads (pyrtlsdr's capture thread, or rtl_sdr's reader),
    so it must not touch socketio directly — results are buffered and
    iq_pipeline_emit_loop emits them.
    """
    global _iq_chunk_counter, _pending_spectrogram_row, _pending_classification
    _iq_chunk_counter += 1

    if _iq_chunk_counter % 5 == 0:
        try:
            result = signal_classifier.classify_iq(
                iq_samples, frequency_hz=frequency_hz,
                expected_modulation=expected,
            )
            if result:
                _pending_classification = result
        except Exception:
            log.debug("classify failed", exc_info=True)

    if _iq_chunk_counter % 3 == 0:
        try:
            spec = iq_to_spectrogram(iq_samples, fft_size=256, hop=128)
            img = spectrogram_to_image(spec, size=256)
            _pending_spectrogram_row = img[-1].tolist()
        except Exception:
            log.debug("waterfall row failed", exc_info=True)


def _on_iq_chunk(iq_samples, frequency_hz):
    """Called by pyrtlsdr IQCapture for each raw IQ chunk.

    NOTE: this never fires on this node — pyrtlsdr cannot load against the
    RTL-SDR Blog driver, so set_iq_callback is a no-op and the tuner runs
    rtl_fm, which yields audio and not IQ. The live IQ that DOES exist arrives
    through _on_collect_iq, which drives the same work.
    """
    iq_segmenter.set_frequency(frequency_hz)
    iq_segmenter.feed(iq_samples)
    preset = input_source.current_preset or {}
    _classify_and_waterfall(iq_samples, frequency_hz,
                            preset.get("expected_modulation"))


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


def _register_asset_helper(flask_app, fallback_version):
    """Expose asset() to templates: /static/x.js?v=<mtime>.

    Keyed on the file's modification time rather than the app version, so ANY
    edit to a JS/CSS file invalidates the browser copy automatically. Relying on
    a manual version bump is what let a stale ravensdr.js keep running after a
    fix shipped — and a stale one is not cosmetic here: an old build force-tuned
    the radio on every page load.
    """
    import os as _os

    @flask_app.template_global("asset")
    def _asset(filename):
        path = _os.path.join(flask_app.static_folder, filename)
        try:
            stamp = str(int(_os.path.getmtime(path)))
        except OSError:
            stamp = fallback_version
        return f"/static/{filename}?v={stamp}"


_register_asset_helper(app, VERSION)

@app.route("/")
def index():
    # Version-stamp static URLs: Flask caches /static for 12h, so a shipped
    # JS/CSS fix would otherwise not reach an already-open console until a hard
    # refresh. A stale ravensdr.js is not cosmetic — the old one force-tuned the
    # radio on every page load.
    # The page must never be cached: it carries the asset URLs, so a cached
    # copy would keep pointing at old JS/CSS no matter how well those are
    # versioned. The assets themselves stay cacheable — their URLs change.
    resp = make_response(render_template("index.html", version=VERSION))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


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
    note_operator_action()   # a person asked for this frequency
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
    # Remember the target for resume-on-restart BEFORE the mode branches, every
    # one of which returns early. Doing it per-branch is what let dedicated modes
    # (ISM/APRS/pager/ACARS/AIS/ADS-B) go unrecorded, so a restart came back on a
    # stale audio preset instead of where the operator left it.
    set_last_preset(preset.get("id"))

    # The preset's declared modulation is ground truth for corpus collection —
    # the operator tuned here deliberately. Set before any mode branch, all of
    # which return early.
    #
    # ...but only for presets whose IQ actually reaches the classifier. ADS-B is
    # map-only on the second dongle and AIS runs its own decoder, so neither
    # feeds this path. Labelling from them left a stale collect_label in place
    # while the main receiver sat on something else entirely: ten windows of
    # 162.550 MHz NOAA weather were filed under "ADSB", a class the model does
    # not even have. Clear it instead, so collection pauses rather than lies.
    # _collect_bands() already applies the same exclusion to the rotation.
    if preset.get("mode") in NON_IQ_MODES:
        signal_classifier.collect_label = None
    else:
        signal_classifier.collect_label = preset.get("expected_modulation")
    # Start weather accumulation fresh when the station changes
    if input_source.current_preset is None or \
            input_source.current_preset.get("id") != preset.get("id"):
        _weather_accumulator.reset()

    # Science tab: display-only, start meteor detector if not running
    if preset.get("category") == "science":
        input_source.stop()
        input_source.current_preset = preset
        _stop_dedicated("switching to Science", notice_type="mode_switch")
        if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
            adsb_receiver.stop()
            _resume_adsb_scan()
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
            _resume_adsb_scan()
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
        _stop_dedicated("switching to Pager", keep=pager_receiver,
                        notice_type="mode_switch")
        if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
            adsb_receiver.stop()
            _resume_adsb_scan()
        pager_receiver.frequency = preset.get("freq", pager_receiver.frequency)
        pager_receiver.start()
        if not pager_receiver.is_running:
            reason = pager_receiver.last_error or "unknown error"
            log.error("Failed to start multimon-ng pager decoder: %s", reason)
            return False, f"Failed to start pager decoder — {reason}"
        log.info("Pager dedicated mode — multimon-ng on %s", pager_receiver.frequency)
        _broadcast_status()
        return True, None

    # APRS dedicated mode: stop audio pipeline, run rtl_fm|multimon-ng continuously
    if preset.get("mode") == "aprs":
        input_source.stop()
        input_source.current_preset = preset
        _stop_dedicated("switching to APRS", keep=aprs_receiver,
                        notice_type="mode_switch")
        if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
            adsb_receiver.stop()
            _resume_adsb_scan()
        aprs_receiver.frequency = preset.get("freq", aprs_receiver.frequency)
        aprs_receiver.start()
        if not aprs_receiver.is_running:
            reason = aprs_receiver.last_error or "unknown error"
            log.error("Failed to start APRS decoder: %s", reason)
            return False, f"Failed to start APRS decoder — {reason}"
        log.info("APRS dedicated mode — multimon-ng on %s", aprs_receiver.frequency)
        _broadcast_status()
        return True, None

    # Switching away from APRS: stop multimon-ng
    if aprs_receiver.is_running:
        aprs_receiver.stop()

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
            _resume_adsb_scan()
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
            _resume_adsb_scan()
        ism_receiver.frequency = preset.get("freq", ism_receiver.frequency)
        ism_receiver.sample_rate = preset.get("sample_rate")
        # A preset may cover several frequencies; rtl_433 hops between them.
        ism_receiver.frequencies = preset.get("freqs")
        ism_receiver.hop_s = preset.get("hop_s")
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
            _resume_adsb_scan()
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
        _resume_adsb_scan()

    success = input_source.tune(preset)
    if not success:
        return False, "Failed to tune — SDR busy or unavailable"

    transcriber.set_preset(preset)
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



# ── Background IQ collection (training corpus) ──
# The IQ pipeline never ran: pyrtlsdr cannot load against the RTL-SDR Blog
# driver, and rtl_fm gives audio, not IQ. rtl_sdr(1) streams raw IQ, so a
# rotation across bands can build a labelled corpus while the node is otherwise
# idle. Gated behind automation ("iq_collect", off by default) because it takes
# the dongle — no audio is produced for the band being collected.
# Rejects only a dead/disconnected receiver — a quiet band still collects,
# because the preset is the label and we deliberately tuned here.
COLLECT_MIN_POWER_DB = -60.0
# Duty of the band currently being collected — see presets.py.
_collect_duty = "burst"
# Above this crest factor the channel is idle between bursts, so direct
# collection would store silence. Continuous carriers measured 1.5-3.6.
COLLECT_BURSTY_CREST = 5.0
IQ_COLLECT_DWELL_S = 60

_iq_collect_segmenter = IQSegmenter(
    sample_rate=2400000,
    on_segment=lambda seg: signal_classifier.classify_segment(seg),
)


def _on_collect_iq(iq_samples, frequency_hz):
    """Feed captured IQ to the segmenter AND collect directly.

    Runs on the collector's real thread.

    Two paths, because they fail in opposite conditions:

    - The segmenter finds bursts against a noise floor, which is right for a
      quiet voice channel but useless on a CONTINUOUS carrier: NOAA weather
      radio never stops, so it becomes the floor itself and its SNR reads ~0.
      Measured: 5 collection slots produced 2 candidates, both rejected as low
      SNR, and the corpus stayed empty.
    - Direct collection ignores detection entirely. It is sound here because the
      LABEL comes from the preset, not from detecting anything — we already know
      what modulation this band carries. The per-class rate limit throttles it.

    The power check only rejects a dead receiver, not a quiet band. Band choice
    is what determines label quality: rotate onto a dead frequency and you
    collect noise filed under that modulation.
    """
    _iq_collect_segmenter.set_frequency(frequency_hz)
    _iq_collect_segmenter.feed(iq_samples)

    label = signal_classifier.collect_label
    if not label:
        return
    if compute_power_db(iq_samples) < COLLECT_MIN_POWER_DB:
        return          # receiver dead or disconnected, not merely quiet

    # Only a CONTINUOUS band may be sampled directly. Crest factor was used for
    # this and it was the wrong test: it measures burstiness, so an idle channel
    # and a steady carrier are indistinguishable. That produced 1921 "OOK"
    # samples containing zero bursts, and "AFSK1200" that was a steady carrier
    # sitting on 144.390 rather than an APRS packet.
    #
    # For a bursty protocol the label is only true DURING a transmission, so
    # those bands collect solely through classify_segment, which fires when the
    # segmenter has actually detected one.
    # Drive the live classifier panel from here too. This is the ONLY source of
    # real IQ on this node, so without it the Signal Classification panel and
    # its spectrogram waterfall stay permanently blank while the model is
    # loaded, working and idle.
    _classify_and_waterfall(iq_samples, frequency_hz, label)

    # A manual burst is an explicit request for THIS frequency, so the
    # continuous-only rule does not apply: the operator has said what the band
    # carries, which is the same guarantee a preset gives.
    if _collect_duty != "continuous" and \
            signal_classifier.burst_status()["remaining"] <= 0:
        return
    signal_classifier.collect_sample(iq_samples, label, frequency_hz)


iq_collector = IQCollector(device_index=0, on_iq=_on_collect_iq)


# Below this the node cannot hear anything: the antenna is a VHF dipole, so HF
# reception needs hardware that is not attached. Collecting there yields receiver
# noise filed under a real modulation — measured: 30 "WEFAX" samples from
# 8.682 MHz and 30 "AM" from 1.65 MHz, all noise. Training on that teaches the
# model that WEFAX looks like static, which is worse than having no samples.
HF_CUTOFF_HZ = 30_000_000


def _collect_bands(include_hf=False):
    """Bands worth collecting: presets that declare a modulation to label with."""
    seen = {}
    skipped_hf = []
    ambiguous = set()
    for preset in get_presets():
        label = preset.get("expected_modulation")
        freq = preset.get("freq", "")
        if not label or label == "unknown" or preset.get("mode") in NON_IQ_MODES:
            continue
        hz = _freq_to_hz(freq)
        if not hz:
            continue
        if hz < HF_CUTOFF_HZ and not include_hf:
            skipped_hf.append(f"{preset['id']} ({hz/1e6:.3f} MHz)")
            continue
        # Keep EVERY frequency that carries this modulation, not one per label.
        #
        # Deduplicating by label gave each class exactly one frequency, which
        # makes the training set unable to distinguish "this modulation" from
        # "this band": a model can score 99% by learning each band's noise floor
        # and filter shape and never learn modulation at all. Collecting FM from
        # three NOAA stations and several ham repeaters, WFM from two broadcast
        # stations, OOK from 345 and 433 MHz, forces the model to find what those
        # captures have in common — which is the modulation.
        if label not in MODULATION_CLASSES:
            ambiguous.add(label)
            continue
        seen.setdefault(label, []).append(
            {"id": preset["id"], "freq_hz": hz, "label": label,
             "duty": preset.get("duty", "burst")})
    if skipped_hf:
        log.info("IQ collect: skipping HF bands (no HF antenna): %s",
                 ", ".join(skipped_hf))

    if ambiguous:
        log.info("IQ collect: skipping ambiguous modulation labels %s — a sample "
                 "cannot be two classes, so they cannot be ground truth",
                 ", ".join(sorted(ambiguous)))

    bands = [b for group in seen.values() for b in group]
    for label, group in sorted(seen.items()):
        if len(group) == 1:
            log.warning("IQ collect: %s has only one frequency (%s) — the model "
                        "cannot separate modulation from band for this class",
                        label, group[0]["id"])
    return bands


def _freq_to_hz(freq):
    """Parse a preset frequency string ('162.550M', '8682.0k') into Hz."""
    try:
        text = str(freq).strip()
        if text.upper().endswith("M"):
            return int(float(text[:-1]) * 1e6)
        if text.lower().endswith("k"):
            return int(float(text[:-1]) * 1e3)
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _start_collect_slot(band):
    """Take the dongle and start capturing IQ for one band."""
    global _collect_duty
    _collect_duty = band.get("duty", "burst")
    if input_source.apt_mode or input_source.wefax_mode:
        return False        # a scheduled pass outranks corpus building
    if operator_lease_remaining() > 0:
        # Somebody is listening. Corpus building has no deadline; waiting costs
        # nothing, and taking the radio here is what made the transcript go
        # silently empty.
        return False
    input_source.stop()
    signal_classifier.collect_label = band["label"]
    ok = iq_collector.start(band["freq_hz"])
    if ok:
        log.info("IQ collect: %s @ %.3f MHz (label %s)",
                 band["id"], band["freq_hz"] / 1e6, band["label"])
        # Tell the arbiter what the hardware is REALLY doing. This path takes
        # the dongle directly, and _stop_collect_slot hands it back through the
        # arbiter — so without this the console kept reporting LOCKED on the
        # operator's preset while the radio was off collecting on another band
        # entirely. The transcript then sat empty with nothing to explain why.
        # adopt() exists for exactly this: state set outside the arbiter.
        sdr_arbiter.adopt(_collect_preset_view(band))
        emit_safe("iq_collect", iq_collect_scheduler.snapshot())
    return ok


def _collect_preset_view(band):
    """A preset-shaped view of a collection band, for the C2 display.

    Marked with collecting=True so the console can say the corpus builder has
    the radio rather than implying the operator's preset is live.
    """
    hz = band.get("freq_hz") or 0
    return {
        "id": band.get("id"),
        "label": "%s (collecting %s)" % (band.get("id"), band.get("label")),
        "freq": "%.4fM" % (hz / 1e6) if hz else "",
        "mode": "iq-collect",
        "category": band.get("category"),
        "collecting": True,
    }


def _stop_collect_slot(band):
    iq_collector.stop()
    emit_safe("iq_collect", iq_collect_scheduler.snapshot())
    # Hand the radio back to whatever the operator had tuned.
    preset = input_source.current_preset
    if preset:
        sdr_arbiter.request(preset)


def _dwell_for_band(band):
    """Give under-represented classes more time on air.

    Bursty channels only yield a sample when somebody transmits, so equal dwell
    produced a ~10:1 imbalance (WFM 376 vs APRS 38 after 2.6h). Weighting by how
    far a class trails the leader lets the slow ones catch up overnight without
    any hand-tuned per-band table, and it self-corrects as counts change.
    """
    counts = signal_classifier.collection_stats().get("per_class", {})
    if not counts:
        return IQ_COLLECT_DWELL_S
    leader = max(counts.values())
    mine = counts.get(band.get("label"), 0)
    if leader <= 0 or mine >= leader * 0.5:
        return IQ_COLLECT_DWELL_S
    # Up to 4x for a class far behind the leader.
    return min(IQ_COLLECT_DWELL_S * 4, int(IQ_COLLECT_DWELL_S * leader / max(mine, 1)))


iq_collect_scheduler = IQCollectScheduler(
    bands=_collect_bands(),
    dwell_fn=_dwell_for_band,
    start_slot=_start_collect_slot,
    stop_slot=_stop_collect_slot,
    is_enabled=lambda: is_automation_enabled("iq_collect"),
    # A person tuning, OR a scheduled pass holding the radio. The second half
    # was missing, which is how a dwell could run straight through an AOS.
    should_yield=lambda: (operator_lease_remaining() > 0
                          or input_source.apt_mode
                          or input_source.wefax_mode),
    sleep_fn=eventlet.sleep,
    on_change=lambda snap: emit_safe("iq_collect", snap),
)


@app.route("/api/collect-here", methods=["POST"])
def api_collect_here():
    """Collect a burst of training samples on the CURRENT frequency.

    The background rotation can only visit frequencies someone thought to add
    as a preset, and its per-class cap means a class that is already full
    collects nothing more — including on a frequency it has never seen. That is
    backwards: a second frequency for OOK is worth more than a twelve-thousandth
    sample from the first, because held-out-frequency validation needs one.

    So this is operator-driven: tune anywhere, say what it is, collect a burst.
    """
    data = request.get_json(force=True) or {}
    label = (data.get("label") or "").strip()
    count = max(1, min(int(data.get("count", 300)), 2000))

    if label not in MODULATION_CLASSES:
        return jsonify({"error": "unknown modulation '%s'" % label,
                        "classes": MODULATION_CLASSES}), 400

    preset = input_source.current_preset or {}
    if not input_source.is_running:
        return jsonify({"error": "Tune to a frequency first — "
                                 "there is nothing to collect."}), 409

    hz = _freq_to_hz(preset.get("freq") or "")
    if not hz:
        return jsonify({"error": "cannot parse the tuned frequency"}), 409

    note_operator_action()          # this is a person; hold off the rotation
    signal_classifier.collect_label = label
    armed = signal_classifier.collect_burst(count, label)

    # rtl_fm gives demodulated audio, not IQ, so the burst needs the dongle on
    # rtl_sdr. Take it, capture, hand it straight back to whatever was tuned.
    eventlet.spawn_n(_run_manual_burst, preset, hz, label, count)

    log.info("Manual collect: %d samples as %s on %s",
             armed, label, preset.get("freq") or "?")
    return jsonify({
        "armed": armed,
        "label": label,
        "freq": preset.get("freq"),
        "preset": preset.get("id"),
    })


# Collection is rate-limited to one sample per class every
# COLLECT_MIN_INTERVAL_S (2s), and gated windows are skipped on top of that — a
# measured burst ran ~7.8s per sample, not 2s. A flat 180s therefore truncated
# any request over ~25 samples, which is how a 30-sample burst finished with 7
# unfilled. Budget from the request instead, with a ceiling so a large count
# cannot hold the dongle indefinitely.
MANUAL_BURST_PER_SAMPLE_S = 8
MANUAL_BURST_MIN_S = 60
MANUAL_BURST_MAX_S = 900


def _manual_burst_timeout(count):
    return max(MANUAL_BURST_MIN_S,
               min(count * MANUAL_BURST_PER_SAMPLE_S, MANUAL_BURST_MAX_S))


def _run_manual_burst(preset, hz, label, count):
    """Hold the dongle on rtl_sdr until the burst fills, then restore audio."""
    global _collect_duty
    was = _collect_duty
    _collect_duty = "continuous"    # the operator vouched for this band
    input_source.stop()
    if not iq_collector.start(hz):
        _collect_duty = was
        log.warning("Manual collect: could not start IQ capture on %.4f MHz",
                    hz / 1e6)
        signal_classifier.collect_burst(0)
        sdr_arbiter.request(preset)
        return

    sdr_arbiter.adopt(_collect_preset_view(
        {"id": preset.get("id"), "freq_hz": hz, "label": label,
         "category": preset.get("category")}))

    budget = _manual_burst_timeout(count)
    waited = 0
    while waited < budget:
        if signal_classifier.burst_status()["remaining"] <= 0:
            break
        eventlet.sleep(1)
        waited += 1

    remaining = signal_classifier.burst_status()["remaining"]
    signal_classifier.collect_burst(0)
    iq_collector.stop()
    _collect_duty = was
    log.info("Manual collect finished on %.4f MHz (%s): %d/%d collected in %ds",
             hz / 1e6, label, count - remaining, count, waited)
    sdr_arbiter.request(preset)     # give the operator their frequency back


@app.route("/api/radio-activity")
def api_radio_activity():
    """What the radio is doing right now, and why — in one call.

    The console had the pieces (arbiter state, collector snapshot, scheduler
    modes) but nothing joined them, so an idle audio path looked like a fault
    instead of a node quietly doing its other job.
    """
    lease = operator_lease_remaining()
    collecting = iq_collector.is_running
    band = (iq_collect_scheduler.snapshot() or {}).get("current_band") or {}

    if input_source.apt_mode:
        who, detail = "scheduled", "Satellite pass recording"
    elif input_source.wefax_mode:
        who, detail = "scheduled", "WEFAX slot recording"
    elif collecting:
        burst = signal_classifier.burst_status()
        hz = band.get("freq_hz") or 0
        if burst.get("remaining"):
            # Requested by a person, so do not file it under "background" —
            # that reads as the node having taken the radio on its own.
            who = "operator"
            detail = "Collecting %d more %s samples at your request" % (
                burst["remaining"], burst.get("label") or "")
        else:
            who = "background"
            detail = "Building the training corpus"
            if hz:
                detail += " on %.4f MHz" % (hz / 1e6)
    elif lease > 0:
        who = "operator"
        detail = "You have the radio"
    else:
        who = "idle"
        detail = "Radio free"

    return jsonify({
        "who": who,
        "detail": detail,
        "lease_remaining_s": round(lease),
        "collecting": collecting,
        "collect_band": band.get("id"),
        "collect_enabled": is_automation_enabled("iq_collect"),
        # Collection is opportunistic: it waits for an idle radio rather than
        # competing for one. Surfaced so the console can say when it resumes.
        "collect_blocked_by": ("operator" if lease > 0 and not collecting
                               else None),
    })


@app.route("/api/iq-collect")
def api_iq_collect():
    """Status of the background corpus rotation."""
    snap = iq_collect_scheduler.snapshot()
    snap["corpus"] = signal_classifier.collection_stats()
    snap["capturing"] = iq_collector.is_running
    snap["bytes_read"] = iq_collector.bytes_read
    return jsonify(snap)


@app.route("/favicon.ico")
def favicon():
    """Serve the app icon. Browsers request this unconditionally, and a 404 on
    every page load is noise that hides real errors in the console."""
    return send_from_directory(app.static_folder, "favicon.svg",
                               mimetype="image/svg+xml")


@app.route("/learn")
def learn():
    """Static explainer: how RF becomes a spectrogram becomes a classification.

    Served from the node itself on the same port as the console — D3 is vendored
    under static/vendor rather than pulled from a CDN, because the node is meant
    to work air-gapped and a field kit with no uplink should still render this.
    """
    resp = make_response(render_template("learn.html", version=VERSION))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@app.route("/api/sweep/bands")
def api_sweep_bands():
    """The band table the survey UI offers."""
    return jsonify({"bands": BANDS})


@app.route("/api/sweep")
def api_sweep_status():
    """Current or most recent sweep. ?full=1 includes the spectrum itself."""
    full = request.args.get("full") in ("1", "true", "yes")
    return jsonify(spectrum_scanner.snapshot(include_bins=full))


@app.route("/api/sweep/start", methods=["POST"])
def api_sweep_start():
    """Take the radio and survey a band.

    A sweep is an operator action with a deadline of its own, so it preempts
    the decoders the same way tuning does — but unlike a tune it is temporary,
    and the operator expects their frequency back afterwards. The previous
    preset is captured BEFORE input_source.stop() clears it (the IQ collector
    had this exact bug: it read current_preset after stopping and always got
    None) and restored by a watcher when the sweep ends.
    """
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "expected a JSON object"}), 400

    note_operator_action()
    previous = input_source.current_preset

    def _as_int(key):
        v = data.get(key)
        if v in (None, ""):
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    input_source.stop()
    _stop_dedicated("spectrum sweep", keep=spectrum_scanner,
                    notice_type="mode_switch")
    if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
        adsb_receiver.stop()
        _resume_adsb_scan()

    ok, err = spectrum_scanner.start(
        band_id=data.get("band"),
        low=_as_int("low"), high=_as_int("high"), bin_hz=_as_int("bin"),
        gain=_as_int("gain"), integration_s=_as_int("integration") or 4,
    )
    if not ok:
        # Put the radio back rather than leaving it unclaimed on a failed start.
        if previous:
            sdr_arbiter.request(previous)
        return jsonify({"error": err or "could not start sweep"}), 500

    def _restore_after_sweep():
        while spectrum_scanner.is_running:
            eventlet.sleep(1)
        if previous and not input_source.is_running:
            log.info("Sweep finished — restoring %s", previous.get("label"))
            sdr_arbiter.request(previous)
        _broadcast_status()

    socketio.start_background_task(_restore_after_sweep)
    return jsonify(spectrum_scanner.snapshot()), 202


@app.route("/api/sweep/history")
def api_sweep_history():
    band = request.args.get("band") or ""
    return jsonify({"band": band, "surveys": spectrum_scanner.history(band)})


@app.route("/api/sweep/identify", methods=["POST"])
def api_sweep_identify():
    """Ask the NPU what each peak from the last sweep actually is.

    Kept separate from the sweep because it costs a retune and an IQ grab per
    peak — about a second each — and most of the time the map is all you want.
    """
    data = request.get_json(force=True, silent=True) or {}
    note_operator_action()
    try:
        limit = int(data.get("limit", 12))
    except (TypeError, ValueError):
        limit = 12

    # It needs the radio back, same as a sweep does.
    input_source.stop()
    _stop_dedicated("identifying survey peaks", keep=spectrum_scanner,
                    notice_type="mode_switch")
    ok, err = spectrum_scanner.start_identify(
        signal_classifier, limit=max(1, min(limit, 40)),
        gain=data.get("gain"))
    if not ok:
        return jsonify({"error": err or "could not start"}), 409
    return jsonify(spectrum_scanner.snapshot()), 202


@app.route("/api/sweep/stop", methods=["POST"])
def api_sweep_stop():
    spectrum_scanner.stop()
    spectrum_scanner.stop_identify()
    return jsonify({"status": "stopped"})


@app.route("/api/languages")
def api_languages():
    """Source languages offered for translation, plus the current setting.

    Only the source is selectable. Whisper translates INTO English and cannot
    do the reverse, so exposing a target would advertise something the model
    cannot do.
    """
    from ravensdr.transcriber import LANGUAGE_NAMES
    settings = get_settings()
    return jsonify({
        "languages": [{"code": c, "name": n} for c, n in LANGUAGE_NAMES.items()],
        "source_language": settings.get("source_language", "auto"),
        "translate_enabled": bool(settings.get("translate_enabled", False)),
        "target": "en",
        "backend": transcriber.backend,
    })


@app.route("/learn/code")
def learn_code():
    """Companion to /learn, aimed at engineers rather than at the curious.

    /learn explains the model — what a spectrogram is, why a CNN, how it was
    trained. This one explains the program around it: the process model, the
    device contention, and the path one transmission takes through the source.
    Split rather than appended because the two have different readers, and a
    single page trying to serve both was already 18,000px long.
    """
    resp = make_response(render_template("learn_code.html", version=VERSION))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@app.route("/api/automation", methods=["GET", "POST"])
def api_automation():
    """Read or update which automation may seize the SDR."""
    if request.method == "GET":
        return jsonify(get_automation())
    patch = request.get_json(force=True) or {}
    auto = set_automation(patch)
    log.info("Automation updated: %s", auto)
    socketio.emit("automation", auto)
    _broadcast_status()
    return jsonify(auto)


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
    # Every dedicated decoder, not the four this used to list. "Stop" that left
    # rtl_fm | multimon-ng running for APRS was not a stop.
    _stop_dedicated("operator stopped the radio", notice_type="mode_switch")
    # Stop dedicated ADS-B mode if active
    if adsb_receiver and adsb_receiver.is_running and not ADSB_DUAL_DONGLE:
        adsb_receiver.stop()
        _resume_adsb_scan()
    _broadcast_status()
    return jsonify({"status": "stopped"})


@app.route("/api/squelch", methods=["POST"])
def api_squelch():
    note_operator_action()
    data = request.get_json(force=True)
    level = data.get("level", 0)
    input_source.set_squelch(int(level))
    _broadcast_status()
    return jsonify({"status": "ok", "squelch": input_source.squelch})


@app.route("/api/gain", methods=["POST"])
def api_gain():
    note_operator_action()
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


@app.route("/api/ism/clear", methods=["POST"])
def api_ism_clear():
    """Drop every remembered ISM device.

    Useful after a decoder fix or a change of band: the table keeps history
    across retunes, so stale entries can outlive the conditions that produced
    them and read as though they were heard on the current frequency.
    """
    n = ism_receiver.clear_records()
    log.info("ISM device list cleared (%d dropped)", n)
    emit_safe("ism_update", ism_receiver.get_records())
    return jsonify({"cleared": n})


@app.route("/api/ism/devices")
def api_ism_devices():
    return jsonify(_with_history("ism", ism_receiver.get_devices(), "id"))


@app.route("/api/acars/messages")
def api_acars_messages():
    return jsonify(acars_receiver.get_messages())



def _with_history(source, records, key_field):
    """Attach durable first-seen/count to live decoder records.

    The live table only knows about the current TTL window; the history is what
    shows a device has been around for days.
    """
    out = []
    for rec in records:
        entry = observations.get(source, rec.get(key_field))
        merged = dict(rec)
        if entry:
            merged["first_seen"] = entry.get("first_seen")
            merged["count"] = entry.get("count")
            merged["best_rssi"] = entry.get("best_rssi")
        out.append(merged)
    return out


@app.route("/api/observations")
def api_observations():
    """Durable sighting history: first/last seen and counts per emitter."""
    source = request.args.get("source")
    limit = request.args.get("limit", type=int)
    return jsonify({
        "stats": observations.stats(),
        "entries": observations.entries(source=source, limit=limit),
    })


@app.route("/api/aprs/stations")
def api_aprs_stations():
    return jsonify(_with_history("aprs", aprs_receiver.get_stations(), "source"))


@app.route("/api/pager/pages")
def api_pager_pages():
    return jsonify(pager_receiver.get_pages())


@app.route("/api/weather/current")
def api_weather_current():
    """Latest decoded NOAA weather, or an explicit 'nothing yet'.

    Returns 200 even with no data. 404 means "this resource does not exist",
    but the endpoint exists and having decoded no weather yet is an ordinary
    state — the node may simply not have been tuned to NOAA. Returning 404 made
    every console log a red error on a healthy node.
    """
    if _latest_weather is None:
        return jsonify({
            "available": False,
            "message": "No weather decoded yet — tune to a NOAA preset and "
                       "wait for a transcript",
        })
    payload = dict(_latest_weather)
    payload["available"] = True
    return jsonify(payload)


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
    # Use the classifier's own COLLECTED_DIR. This route used to look under
    # code/ravensdr/data/collected while the classifier wrote to
    # code/ml/signal_classifier/data/collected, so the panel always read 0.
    from ravensdr.signal_classifier import COLLECTED_DIR as _collected
    collected_dir = _collected
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
        "collection_available": True,
        "classifier_corpus": signal_classifier.collection_stats(),
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
        "aprs_running": aprs_receiver.is_running,
        "automation": get_automation(),
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
            socketio.emit("ism_update",
                          _with_history("ism", ism_receiver.get_devices(), "id"))


def acars_broadcast_loop():
    """Push the ACARS aircraft table (with TTL expiry) to clients every 3s."""
    while not _signal_stop.is_set():
        eventlet.sleep(3)
        if acars_receiver.is_running:
            socketio.emit("acars_update", acars_receiver.get_messages())


def aprs_broadcast_loop():
    """Push the APRS station table (with TTL expiry) to clients every 3s."""
    while not _signal_stop.is_set():
        eventlet.sleep(3)
        if aprs_receiver.is_running:
            socketio.emit("aprs_update",
                          _with_history("aprs", aprs_receiver.get_stations(), "source"))


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

        # socketio.emit is wrapped by _emit_with_ipc_fanout, so these already
        # reach every IPC peer (the LCD driver among them) as well as the
        # browser. Do NOT add an explicit ipc_server.broadcast here: it sends a
        # second copy of every row under the same event name, and the LCD then
        # sees two payload shapes for one event.
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
    iq_collect_scheduler.stop()
    observations.maybe_save(force=True)
    ipc_server.stop()
    aprs_receiver.stop()
    input_source.stop()
    transcriber.stop()
    if adsb_receiver:
        adsb_receiver.stop()
    if adsb_scheduler:
        adsb_scheduler.stop()
    spectrum_scanner.stop()
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

    # socketio.stop() is `raise SystemExit` under eventlet, which only unwinds
    # the greenthread it is raised in. Raised here — inside a background task,
    # not the WSGI handler — it killed this greenthread and left socketio.run()
    # serving, so systemd waited out TimeoutStopSec=45 and SIGKILLed us. Two
    # outcomes were observed in the journal, never a clean exit:
    #   status=9/KILL  result 'timeout'   (the 45s hang)
    #   status=7/BUS   result 'signal'    (torn down mid-DMA instead)
    # Teardown above is finished and flushed by this point, so leave directly
    # rather than unwinding through an interpreter shutdown that races HailoRT
    # for the mapped buffers.
    if signum == signal.SIGTERM:
        import os as _exit_os
        logging.shutdown()
        _exit_os._exit(0)


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

    # Dedicated modes hold the dongle outright. Normally we refuse to resume one
    # unattended so a scheduled satellite pass isn't blocked at boot — but with
    # automation paused nothing is going to preempt us, so honouring the
    # operator's last choice is both safe and what they asked for.
    is_dedicated = (preset.get("mode") in ("adsb", "ais", "ism", "aprs", "pager", "acars")
                    or preset.get("category") in ("science", "wefax"))
    if is_dedicated and is_automation_enabled("apt"):
        log.warning("Startup preset %r is a dedicated mode and automation is on "
                    "— starting idle so a scheduled pass isn't blocked", preset_id)
        return

    if is_dedicated:
        ok, err = _apply_tune(preset)
        if ok:
            sdr_arbiter.adopt(preset)
            log.info("Resumed dedicated startup preset: %s", preset.get("label", ""))
        else:
            log.error("Failed to resume startup preset %r: %s", preset_id, err)
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
    iq_collect_scheduler.start(spawn_fn=socketio.start_background_task)
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
    socketio.start_background_task(aprs_broadcast_loop)
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
