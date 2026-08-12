# Hailo Whisper wrapper + audio chunking

import contextlib
import logging
import os
import threading
import time

# The inference thread must be a REAL OS thread, not a greenthread. Its body is
# a sequence of blocking C calls — encoder_configured.run(), then one
# decoder_configured.run() per token, each with timeout_ms=10000 — and a
# greenthread running those never yields, so the eventlet hub stops entirely:
# no HTTP, no Socket.IO, no arbiter tick, no broadcast loop, for as long as the
# transcript takes. Measured at up to ~2.5s per segment. The codebase already
# blamed this without naming it — see apt_scheduler's note about the poll loop
# drifting when "starved by ... Hailo inference".
#
# Only Thread and Event are taken real. Nothing in the loop calls time.sleep(),
# so green time is not a hazard here; timing uses time.monotonic(), which is
# not patched in a way that matters.
try:
    from eventlet.patcher import original
    threading = original("threading")
except ImportError:
    pass
from datetime import datetime

import numpy as np

log = logging.getLogger(__name__)

# Under eventlet's monkey-patched threading, Thread.join(timeout=...) raises
# eventlet.timeout.Timeout (a BaseException) instead of returning on expiry.
try:
    from eventlet.timeout import Timeout as _EventletTimeout
    _JOIN_TIMEOUT = (_EventletTimeout,)
except ImportError:
    _JOIN_TIMEOUT = ()

SILENCE_THRESHOLD = 500    # RMS value — below this, skip inference
CHUNK_SAMPLES = 160000     # 10 seconds at 16kHz (matches Hailo encoder input)
SAMPLE_RATE = 16000

# Voice-activity segmentation constants
VAD_SILENCE_THRESHOLD = 400   # RMS below this = silence
VAD_HOLDOFF_MS = 300          # silence must last this long to trigger a split
VAD_MIN_SEGMENT_S = 1.0       # don't send segments shorter than this
VAD_MAX_SEGMENT_S = 10.0      # force-split if speech runs longer than this
VAD_FRAME_SIZE = 1600          # 100ms frames at 16kHz

# Continuous capture constants (NOAA weather radio)
# Both segment ceilings are pinned to the encoder window on purpose. The Hailo
# encoder accepts exactly CHUNK_SAMPLES (10s) and pad_or_trim() silently drops
# anything past it — so a 30s segment did not give Whisper more context, it threw
# 20s of speech away before the NPU ever saw it. Continuous broadcasts were
# losing roughly two thirds of their audio, which read as a flaky transcriber
# rather than as the truncation it was. Anything raised above 10.0 here is
# discarded downstream, so raise CHUNK_SAMPLES (and recompile the HEF) instead.
CONTINUOUS_SEGMENT_S = 10.0    # fixed segment duration for continuous broadcasts
CONTINUOUS_OVERLAP_S = 2.0     # overlap between segments to avoid cutting words

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
DECODER_SEQUENCE_LENGTH = 32  # max tokens for whisper-tiny
START_TOKEN_ID = 50258        # <|startoftranscript|>
LANG_TOKEN_ID = 50259         # <|en|>
TRANSCRIBE_TOKEN_ID = 50359   # <|transcribe|>
TRANSLATE_TOKEN_ID = 50358    # <|translate|> — same model, X -> English
NO_TIMESTAMPS_TOKEN_ID = 50363  # <|notimestamps|>
DECODE_PREFIX = [START_TOKEN_ID, LANG_TOKEN_ID, TRANSCRIBE_TOKEN_ID, NO_TIMESTAMPS_TOKEN_ID]

# The 99 language tokens sit contiguously between <|en|> and <|translate|>, which
# is what makes auto-detect cheap: run one decoder step off a bare
# <|startoftranscript|> and take the argmax over this slice.
LANG_TOKEN_FIRST = 50259      # <|en|>
LANG_TOKEN_LAST = 50357       # last language token, immediately before <|translate|>

# Whisper is multilingual in one direction only: it can turn 99 languages INTO
# English, and cannot go the other way. Anything English->X needs a separate
# NMT model, so the UI must never offer a non-English target.
_translate_enabled = False
_source_language = "auto"
REPETITION_PENALTY = 1.5
REPETITION_WINDOW = 8
EXCLUDED_TOKENS = {11, 13}  # punctuation tokens excluded from penalty

# Try to import Hailo SDK
HAILO_AVAILABLE = False
try:
    from hailo_platform import HEF, VDevice, HailoSchedulingAlgorithm, FormatType
    HAILO_AVAILABLE = True
    log.info("Hailo SDK available — using NPU inference")
except ImportError:
    log.info("Hailo SDK not available — will try faster-whisper CPU fallback")

# Try to import faster-whisper for CPU fallback
FASTER_WHISPER_AVAILABLE = False
try:
    from faster_whisper import WhisperModel
    FASTER_WHISPER_AVAILABLE = True
    log.info("faster-whisper available for CPU fallback")
except ImportError:
    log.info("faster-whisper not available")


def compute_rms(pcm_bytes):
    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    if len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


# Runtime-tunable copy of SILENCE_THRESHOLD (updated via Transcriber.apply_settings).
_silence_threshold = SILENCE_THRESHOLD


def is_signal_present(pcm_bytes):
    return compute_rms(pcm_bytes) > _silence_threshold


# Adaptive VAD: gate on level ABOVE the noise floor, not an absolute RMS.
#
# Why the absolute threshold does not work
# ----------------------------------------
# rtl_fm with squelch disabled (-l 0) emits full-scale FM hiss when nobody is
# transmitting. Measured on a silent 2m repeater: RMS 1342-1523 (median 1439),
# i.e. ~3x the 500 gate, so 0% of frames were ever rejected. The NPU therefore
# transcribed static continuously and Whisper — asked to transcribe a roaring
# noise — emitted "(roaring)" and "(Groans)", which the hallucination filter then
# discarded. Measured over 75 minutes: 280 chunks processed, 273 filtered (97.5%
# waste), 1764 tokens spent on nothing.
#
# The noise floor also moves with gain, frequency and antenna, so no fixed number
# can be right everywhere. What IS stable is the *margin* between speech and the
# local floor, so this tracks the floor and gates on dB above it — mirroring the
# approach iq_segmenter.IQSegmenter already uses successfully in the IQ domain.

VAD_THRESHOLD_DB = 8.0          # speech must exceed the floor by this much
VAD_NOISE_WINDOW = 200          # frames of history for the floor estimate (~20s)
VAD_MIN_FLOOR_FRAMES = 10       # need this many before the estimate is trusted
VAD_ABSOLUTE_FLOOR_RMS = 30     # below this it is digital silence, never speech
# How long the floor may stay frozen before we assume the estimate is wrong.
# Must be much longer than any plausible transmission — a net or a ragchew can
# run for minutes, and relearning mid-over would teach the floor the speech
# level and shut the gate. 3 minutes at 100ms frames.
VAD_STALE_FLOOR_FRAMES = 1800

_vad_threshold_db = VAD_THRESHOLD_DB


def rms_to_db(rms):
    """Convert a linear 16-bit RMS to dB. Floored to avoid log(0)."""
    return float(20.0 * np.log10(max(float(rms), 1e-6)))


class NoiseFloorTracker:
    """Rolling noise floor over frame RMS, in dB, by low-percentile statistics.

    A LOW PERCENTILE, not a mean or a median. Each alternative fails:

      mean   — speech drags it upward, raising the floor until nothing exceeds
               it and the gate goes deaf.
      median — fine when transmissions are rare (which is why IQSegmenter can
               use one), but during an over the channel is busy more than half
               the time, so the median becomes the SPEECH level and every frame
               then reads as "at the floor". Caught by test_short_pause_no_split.

    The 10th percentile over a ~20s window lands on the quiet gaps that real
    audio always has — between words, between overs — so the floor tracks hiss
    even mid-conversation. A perfectly constant tone has no gaps and cannot be
    distinguished from noise by level alone; that is inherent to level-based VAD,
    not a defect here.
    """

    FLOOR_PERCENTILE = 10

    def __init__(self, window=VAD_NOISE_WINDOW, min_frames=VAD_MIN_FLOOR_FRAMES,
                 stale_frames=VAD_STALE_FLOOR_FRAMES):
        self._window = window
        self._min_frames = min_frames
        self._stale_frames = stale_frames
        self._history = []
        self._floor_db = None
        self._frozen_frames = 0

    def update(self, rms):
        """Feed one frame's RMS; returns the current floor in dB (or None).

        Frames already judged to be signal do NOT teach the floor. Without this,
        a long transmission (a net, a ragchew) fills the whole window with
        speech, the percentile climbs into the speech itself, and the gate closes
        mid-over — the same way it swallowed continuous broadcast audio.

        The stale-guard keeps that from becoming permanent: if the floor is never
        re-learned for a full window, the estimate is stale (the band changed, or
        it locked on during a transmission), so learning resumes unconditionally.
        """
        if self._floor_db is not None and self.is_signal(rms):
            self._frozen_frames += 1
            if self._frozen_frames <= self._stale_frames:
                return self._floor_db
            # Stale — fall through and relearn rather than stay deaf forever.
        else:
            self._frozen_frames = 0

        self._history.append(rms_to_db(rms))
        if len(self._history) > self._window:
            self._history = self._history[-self._window:]
        if len(self._history) >= self._min_frames:
            self._floor_db = float(np.percentile(self._history,
                                                 self.FLOOR_PERCENTILE))
        return self._floor_db

    def excess_db(self, rms):
        """How far this frame sits above the floor. 0.0 while still learning."""
        if self._floor_db is None:
            return 0.0
        # float() throughout: numpy scalars leak np.float64/np.bool_ into the
        # public API, and np.bool_(False) is not False, which silently breaks
        # identity checks in callers and tests.
        return float(rms_to_db(rms) - self._floor_db)

    def is_signal(self, rms, threshold_db=None):
        """True if this frame is loud enough, relative to the local floor."""
        rms = float(rms)
        if rms < VAD_ABSOLUTE_FLOOR_RMS:
            return False        # digital silence — no floor estimate can save it
        if self._floor_db is None:
            # Until the floor settles, fall back to the absolute gate rather than
            # passing everything through to the NPU.
            return bool(rms > _silence_threshold)
        threshold = _vad_threshold_db if threshold_db is None else threshold_db
        return bool(self.excess_db(rms) >= threshold)

    @property
    def noise_floor_db(self):
        return self._floor_db

    @property
    def ready(self):
        return self._floor_db is not None

    def reset(self):
        self._history = []
        self._floor_db = None
        self._frozen_frames = 0


import re

# Whisper hallucinates these on noise/static — filter them out.
# Two-tier approach: specific known phrases + structural patterns.

# Tier 1: specific known Whisper hallucination phrases
_HALLUCINATION_PHRASES = re.compile(
    r"^\s*("
    r"thank you|thanks for watching|subscribe|like and subscribe|"
    r"you|I don'?t know.*|okay|oh|ah|uh|hmm|"
    r"blank audio|no speech|inaudible|unintelligible"
    r")\s*\.?\s*$",
    re.IGNORECASE,
)

# Tier 2: any text that is ENTIRELY inside brackets/parens — these are
# Whisper's sound descriptions, never real speech transcriptions.
# e.g. [Music], (roaring), [Birds], [Scream], [Grooing]
_BRACKETED_RE = re.compile(r"^\s*[\[\(].+[\]\)]\s*$")


def hallucination_reason(text):
    """Return why `text` looks like a Whisper hallucination, or None to keep it."""
    t = text.strip()
    if len(t) <= 1:
        return "too_short"
    if _HALLUCINATION_PHRASES.match(t):
        return "known_phrase"
    if _BRACKETED_RE.match(t):
        return "bracketed"
    if re.match(r"^(.{1,4}[-–])\1{2,}$", t, re.IGNORECASE):
        return "repetitive"
    words = t.split()
    if len(words) <= 2 and len(t) < 15:
        return "short_fragment"
    return None


def _is_hallucination(text):
    """Return True if text is a known Whisper hallucination on noise."""
    reason = hallucination_reason(text)
    if reason:
        log.debug("Hallucination filtered [%s]: %r", reason, text.strip())
        return True
    return False


def _apply_repetition_penalty(logits, generated_tokens, penalty=REPETITION_PENALTY,
                              last_window=REPETITION_WINDOW):
    """Discourage repeated tokens by pushing their logits toward -inf.

    The sign matters and dividing unconditionally gets it backwards. A logit is
    a log-probability, so it is usually NEGATIVE, and dividing a negative by 1.5
    moves it UP: -8.0 becomes -5.333, i.e. the token we meant to suppress
    becomes more likely. Whisper's logits are overwhelmingly negative, so the
    guard added to stop repetition loops was in fact rewarding them ~1.5x across
    most of the vocabulary. Match HuggingFace's RepetitionPenaltyLogitsProcessor:
    multiply when negative, divide when positive — both move the token down.
    """
    logits = np.squeeze(logits, axis=0)
    recent = set(generated_tokens[-last_window:])
    for token in recent:
        if token not in EXCLUDED_TOKENS:
            score = logits[token]
            logits[token] = score * penalty if score < 0 else score / penalty
    return logits


class VoiceActivitySegmenter:
    """Accumulates PCM and splits on silence boundaries instead of fixed time."""

    def __init__(self):
        self._pending = b""
        self._silence_frames = 0
        self._holdoff_frames = int(VAD_HOLDOFF_MS / 100)
        # Shared floor estimate: the same measurement decides both where to split
        # and whether a segment is worth transcribing. With a fixed threshold on
        # an unsquelched channel, hiss never counted as silence, so nothing ever
        # split on silence and every segment ran to the 15s maximum.
        self.noise = NoiseFloorTracker()

    def should_transcribe(self, chunk):
        """True if this segment stands far enough above the local noise floor."""
        return self.noise.is_signal(compute_rms(chunk))

    def feed(self, pcm: bytes) -> list[bytes]:
        """Feed PCM data, return list of complete segments (may be empty)."""
        self._pending += pcm
        segments = []
        frame_bytes = VAD_FRAME_SIZE * 2  # 16-bit samples

        while len(self._pending) >= frame_bytes:
            # Peek at next frame for RMS check without consuming yet
            frame_start = len(self._pending) - (len(self._pending) % frame_bytes)
            # Process frames from the start
            break

        # Process all complete frames in pending buffer
        pos = 0
        new_pending = b""
        # We need to track position through the buffer
        buf = self._pending
        self._pending = b""

        while len(buf) >= frame_bytes:
            frame = buf[:frame_bytes]
            buf = buf[frame_bytes:]

            rms = compute_rms(frame)
            self._pending += frame
            buf_seconds = len(self._pending) / (SAMPLE_RATE * 2)

            self.noise.update(rms)
            # "Silence" means near the floor, not below a fixed number — on a
            # noisy channel the floor itself sits well above VAD_SILENCE_THRESHOLD.
            if self.noise.ready:
                is_quiet = self.noise.excess_db(rms) < _vad_threshold_db
            else:
                is_quiet = rms < VAD_SILENCE_THRESHOLD
            if is_quiet:
                self._silence_frames += 1
            else:
                self._silence_frames = 0

            # Split on silence boundary (if enough silence and min length met)
            if (self._silence_frames >= self._holdoff_frames
                    and buf_seconds >= VAD_MIN_SEGMENT_S):
                segments.append(self._flush())

            # Force-split at max duration to avoid unbounded buffers
            elif buf_seconds >= VAD_MAX_SEGMENT_S:
                segments.append(self._flush())

        # Put remaining partial frame back
        self._pending += buf
        return segments

    def progress(self):
        """How much audio has accumulated toward the next segment.

        Surfaced in the console because a segmenter that is quietly filling its
        buffer looks exactly like a dead transcriber. There is no fixed target
        here — a segment ends when the talker stops — so target_s is the hard
        ceiling and the bar reads "at most this long", not "this far along".
        """
        return {
            "mode": "vad",
            "buffered_s": round(len(self._pending) / (SAMPLE_RATE * 2), 1),
            "target_s": VAD_MAX_SEGMENT_S,
        }

    def _flush(self) -> bytes:
        seg = self._pending
        self._pending = b""
        self._silence_frames = 0
        return seg

    def reset(self):
        self._pending = b""
        self._silence_frames = 0


def _segmenter_progress(segmenter):
    """Segment fill state for the console, or None if the segmenter lacks it.

    Tolerant by design: a segmenter without progress() must not break the
    signal meter, which is load-bearing for judging whether a channel is live.
    """
    fn = getattr(segmenter, "progress", None)
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        return None


class ContinuousSegmenter:
    """Time-based segmentation for continuous broadcasts (e.g. NOAA weather radio).

    Splits audio into fixed-duration chunks with overlap to avoid cutting words.
    Used instead of VAD when the broadcast has no silence gaps.
    """

    def should_transcribe(self, chunk):
        """Continuous broadcasts are transcribed wholesale — absolute gate only.

        These presets (NOAA weather radio, a TIS loop) carry speech by design and
        have no silence gaps, so a floor-relative test would reject the very
        content we tuned in for.
        """
        return is_signal_present(chunk)

    def __init__(self, segment_s=CONTINUOUS_SEGMENT_S, overlap_s=CONTINUOUS_OVERLAP_S):
        self._segment_bytes = int(segment_s * SAMPLE_RATE * 2)  # 16-bit PCM
        self._overlap_bytes = int(overlap_s * SAMPLE_RATE * 2)
        self._pending = b""

    def feed(self, pcm: bytes) -> list[bytes]:
        """Feed PCM data, return list of complete segments."""
        self._pending += pcm
        segments = []
        while len(self._pending) >= self._segment_bytes:
            seg = self._pending[:self._segment_bytes]
            segments.append(seg)
            # Advance by segment minus overlap
            advance = self._segment_bytes - self._overlap_bytes
            self._pending = self._pending[advance:]
        return segments

    def reset(self):
        self._pending = b""

    def progress(self):
        """How much of the next fixed segment has accumulated.

        This is the one the console actually needs: a 30s segment means up to
        30s of blank transcript panel after tuning, which is indistinguishable
        from a broken pipeline unless the UI says otherwise.
        """
        return {
            "mode": "continuous",
            "buffered_s": round(len(self._pending) / (SAMPLE_RATE * 2), 1),
            "target_s": round(self._segment_bytes / (SAMPLE_RATE * 2), 1),
        }


def _shared_vdevice():
    """The process-wide VDevice. Never closed here — the classifier and SEI
    hold the same object, and closing it would take the NPU from them."""
    from ravensdr.hailo_device import get_vdevice
    vd = get_vdevice()
    if vd is None:
        raise RuntimeError("no Hailo VDevice")
    return vd


class Transcriber:
    """Accumulates PCM chunks, detects silence, runs Whisper inference."""

    def __init__(self, pcm_queue, emit_fn):
        self.pcm_queue = pcm_queue
        self.emit_fn = emit_fn        # callback to emit transcript + signal_level
        self._stop_event = threading.Event()
        self._thread = None
        self._current_preset = None
        self._whisper_model = None
        self._transcript_callback = None   # called with text on each transcript
        self._weather_callback = None      # called with parsed NOAA data
        # Rebuild the segmenter on the next loop pass (set by set_preset).
        self._segmenter_dirty = True

        # Inference stats
        self._stats = {
            "backend": "none",
            "chunks_processed": 0,
            "chunks_skipped_silence": 0,
            "chunks_filtered": 0,
            "noise_floor_db": None,
            "vad_threshold_db": VAD_THRESHOLD_DB,
            "last_filtered_text": None,
            "last_filtered_reason": None,
            "total_tokens": 0,
            "last_encoder_ms": 0,
            "last_decoder_ms": 0,
            "last_total_ms": 0,
            "last_tokens": 0,
            "last_tokens_per_sec": 0.0,
            "last_rtf": 0.0,
            "last_decoder_steps": 0,
            "max_decoder_steps": DECODER_SEQUENCE_LENGTH,
            "audio_duration_s": 0.0,
        }

        # Hailo decoder assets (pure data, no device handles)
        self._encoder_path = None
        self._decoder_path = None
        self._token_embedding_weight = None
        self._onnx_add_input = None
        self._tokenizer = None

        # Initialize inference backend. RAVENSDR_FORCE_BACKEND (hailo|cpu|none) pins
        # the backend and turns a silent downgrade into a hard error — useful during
        # bring-up/debugging so a broken Hailo env fails loudly instead of quietly
        # running on CPU.
        forced = (os.environ.get("RAVENSDR_FORCE_BACKEND") or "").strip().lower() or None
        if forced not in (None, "hailo", "cpu", "none"):
            log.warning("Ignoring invalid RAVENSDR_FORCE_BACKEND=%r (expected hailo|cpu|none)", forced)
            forced = None
        self._forced_backend = forced

        if forced == "hailo" or (forced is None and HAILO_AVAILABLE):
            if forced == "hailo" and not HAILO_AVAILABLE:
                raise RuntimeError(
                    "RAVENSDR_FORCE_BACKEND=hailo but hailo_platform is not importable — "
                    "check the venv provides hailo_platform (see: python3 code/scripts/debug.py)"
                )
            self._backend = "hailo"
            self._init_hailo()
        elif forced == "cpu" or (forced is None and FASTER_WHISPER_AVAILABLE):
            if forced == "cpu" and not FASTER_WHISPER_AVAILABLE:
                raise RuntimeError("RAVENSDR_FORCE_BACKEND=cpu but faster-whisper is not installed")
            self._backend = "cpu"
            self._init_faster_whisper()
        else:
            self._backend = "none"
            log.warning("No Whisper backend available — transcription disabled")

        log.info("Transcriber backend resolved: %s%s",
                 self._backend, " (forced)" if forced else "")

    @property
    def backend(self):
        return self._backend

    @property
    def stats(self):
        return dict(self._stats)

    def _init_hailo(self):
        """Load decoder assets and validate model files. No device handles created here."""
        try:
            self._encoder_path = os.path.join(MODELS_DIR, "h8l", "tiny-whisper-encoder-10s_15dB_h8l.hef")
            self._decoder_path = os.path.join(MODELS_DIR, "h8l", "tiny-whisper-decoder-fixed-sequence-matmul-split_h8l.hef")

            for path in (self._encoder_path, self._decoder_path):
                if not os.path.exists(path):
                    raise FileNotFoundError(f"Model file not found: {path}. Run scripts/download_models.sh")

            # Load decoder assets
            assets_dir = os.path.join(MODELS_DIR, "decoder_assets")
            self._token_embedding_weight = np.load(
                os.path.join(assets_dir, "token_embedding_weight_tiny.npy")
            )
            self._onnx_add_input = np.load(
                os.path.join(assets_dir, "onnx_add_input_tiny.npy")
            )

            # Load tokenizer
            from transformers import AutoTokenizer
            self._tokenizer = AutoTokenizer.from_pretrained("openai/whisper-tiny")

            log.info("Hailo model files validated, decoder assets loaded")

        except Exception as e:
            reason = self._hailo_failure_reason(e)
            if self._forced_backend == "hailo":
                log.error("Hailo init failed [%s]: %s (%s) — backend forced to hailo, not degrading",
                          reason, e, type(e).__name__)
                raise
            log.warning("Hailo init failed [%s]: %s (%s) — falling back to CPU",
                        reason, e, type(e).__name__)
            if FASTER_WHISPER_AVAILABLE:
                self._backend = "cpu"
                self._init_faster_whisper()
            else:
                self._backend = "none"
                log.warning("faster-whisper unavailable too — transcription disabled")

    @staticmethod
    def _hailo_failure_reason(exc):
        """Map an init exception to a short, actionable reason string."""
        if isinstance(exc, FileNotFoundError):
            return "model file missing — run scripts/download_models.sh"
        if type(exc).__name__ == "ModuleNotFoundError":
            return "missing python dependency (e.g. transformers)"
        return type(exc).__name__

    def _init_faster_whisper(self):
        try:
            self._whisper_model = WhisperModel(
                "tiny", device="cpu", compute_type="int8"
            )
            log.info("faster-whisper CPU model loaded (tiny)")
        except Exception as e:
            log.error("Failed to load faster-whisper: %s", e)
            self._backend = "none"

    def set_preset(self, preset):
        """Change the active preset and force the segmenter to be rebuilt.

        The inference loops build their segmenter ONCE before looping, so without
        this flag a preset change could never switch strategy: tuning from a
        voice preset to a continuous broadcast (NOAA, KUOW, a TIS loop) kept the
        VAD segmenter, whose floor-relative gate has no quiet gap to measure
        against and so rejected the programme audio wholesale.
        """
        self._current_preset = preset
        self._segmenter_dirty = True

    def set_transcript_callback(self, callback):
        """Set callback(text) called on each non-empty transcript."""
        self._transcript_callback = callback

    def set_weather_callback(self, callback):
        """Set callback(parsed_data) called when NOAA parser produces results."""
        self._weather_callback = callback

    def apply_settings(self, settings):
        """Apply runtime settings from the Settings tab (see config.py)."""
        global _silence_threshold, _vad_threshold_db
        try:
            _silence_threshold = float(settings.get(
                "silence_threshold", _silence_threshold))
            _vad_threshold_db = float(settings.get(
                "vad_threshold_db", _vad_threshold_db))
        except (TypeError, ValueError):
            pass

    def _post_process(self, text):
        """Route transcript through category-specific post-processors.

        Returns (text, parsed_data) where parsed_data is None if no parser applies.
        """
        preset = self._current_preset or {}
        parser_type = preset.get("parser")

        if parser_type == "noaa":
            from ravensdr.noaa_parser import parse_weather_transcript
            parsed = parse_weather_transcript(text)
            return text, parsed

        return text, None

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._inference_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            try:
                self._thread.join(timeout=5)
            except _JOIN_TIMEOUT:
                pass
            if self._thread.is_alive():
                log.warning("Transcriber thread did not exit within 5s timeout")
            self._thread = None
        self._whisper_model = None

    def _inference_loop(self):
        if self._backend == "hailo":
            self._inference_loop_hailo()
        elif self._backend == "cpu":
            self._inference_loop_cpu()
        elif self._backend == "none":
            self._inference_loop_passthrough()

    def _inference_loop_passthrough(self):
        """Drain the queue without transcription when no backend is available."""
        while not self._stop_event.is_set():
            try:
                self.pcm_queue.get(timeout=1)
            except Exception:
                continue

    def _note_filtered(self, text, reason):
        """Record a transcript that was produced but withheld from the UI.

        Whisper genuinely decodes noise into plausible-looking text, so the
        filter is necessary — but dropping it silently made a working NPU look
        like a dead one: stats showed chunks processed and tokens generated
        while the operator saw an empty feed and no way to tell why. Counting
        drops (and keeping the last one) distinguishes "nothing was heard" from
        "something was heard and rejected".
        """
        self._stats["chunks_filtered"] = self._stats.get("chunks_filtered", 0) + 1
        self._stats["last_filtered_text"] = (text or "").strip()[:200]
        self._stats["last_filtered_reason"] = reason
        log.info("Transcript filtered [%s]: %r", reason, (text or "").strip()[:120])

    def _make_segmenter(self):
        """Choose segmenter based on current preset configuration."""
        preset = self._current_preset or {}
        # Continuous broadcasts have no silence gaps for VAD to split on, so they
        # need time-based segmentation. NOAA weather radio qualifies by its
        # parser; any other always-on loop (e.g. a TIS/HAR community station)
        # opts in explicitly with "continuous": True.
        is_noaa_loop = preset.get("parser") == "noaa" and preset.get("squelch", -1) == 0
        if is_noaa_loop or preset.get("continuous"):
            log.info("Using continuous segmenter (%.0fs chunks) for %s",
                     CONTINUOUS_SEGMENT_S, preset.get("label", "broadcast"))
            return ContinuousSegmenter()
        return VoiceActivitySegmenter()

    def _inference_loop_cpu(self):
        """CPU fallback inference loop using faster-whisper with VAD/continuous segmentation."""
        segmenter = self._make_segmenter()

        while not self._stop_event.is_set():
            try:
                data = self.pcm_queue.get(timeout=1)
            except Exception:
                continue

            if self._segmenter_dirty:
                segmenter = self._make_segmenter()
                self._segmenter_dirty = False

            # Signal level from raw data
            if len(data) >= 4096:
                rms = compute_rms(data[-4096:])
                preset = self._current_preset or {}
                noise = getattr(segmenter, "noise", None)
                # Excess over the measured floor, not raw level. On FM an ABSENT
                # carrier demodulates to full-scale hiss, so a loud meter can
                # mean nothing is there — which is exactly how a dead NOAA
                # channel read at 85% of scale.
                self.emit_fn("signal_level", {
                    "rms": round(rms, 1),
                    "freq": preset.get("freq", ""),
                    "noise_floor_db": (round(noise.noise_floor_db, 1)
                                       if noise and noise.ready else None),
                    "excess_db": (round(noise.excess_db(rms), 1)
                                  if noise and noise.ready else None),
                    "segment": _segmenter_progress(segmenter),
                })

            # Feed into segmenter — get back segments (VAD or time-based)
            segments = segmenter.feed(data)
            for chunk in segments:
                if not segmenter.should_transcribe(chunk):
                    self._stats["chunks_skipped_silence"] += 1
                    continue

                audio_s = len(chunk) / (SAMPLE_RATE * 2)
                t_start = time.monotonic()
                text = self._transcribe_cpu(chunk)
                t_end = time.monotonic()
                total_ms = (t_end - t_start) * 1000

                self._stats.update({
                    "backend": "cpu",
                    "chunks_processed": self._stats["chunks_processed"] + 1,
                    "last_encoder_ms": 0,
                    "last_decoder_ms": 0,
                    "last_total_ms": round(total_ms, 1),
                    "last_tokens": 0,
                    "last_tokens_per_sec": 0.0,
                    "last_rtf": round((total_ms / 1000) / audio_s, 3) if audio_s > 0 else 0,
                    "last_decoder_steps": 0,
                    "audio_duration_s": round(audio_s, 1),
                })
                self.emit_fn("inference_stats", self._stats)

                filtered = hallucination_reason(text) if text and text.strip() else "empty"
                if filtered:
                    self._note_filtered(text, filtered)
                else:
                    preset = self._current_preset or {}
                    text_clean = text.strip()
                    text_clean, parsed_data = self._post_process(text_clean)
                    segment = {
                        "timestamp": datetime.now().strftime("%H:%M:%S"),
                        "freq": preset.get("freq", ""),
                        "label": preset.get("label", ""),
                        "text": text_clean,
                        "rms": round(compute_rms(chunk), 1),
                    }
                    self.emit_fn("transcript", segment)
                    if parsed_data and self._weather_callback:
                        self._weather_callback(parsed_data)
                    if self._transcript_callback:
                        self._transcript_callback(text_clean)

    def _inference_loop_hailo(self):
        """Hailo NPU inference loop — VDevice and configure() scoped by context managers."""
        from ravensdr.mel import log_mel_spectrogram, pad_or_trim

        decoder_hef = HEF(self._decoder_path)
        sorted_output_names = decoder_hef.get_sorted_output_names()
        decoder_model_name = decoder_hef.get_network_group_names()[0]

        try:
            with contextlib.nullcontext(_shared_vdevice()) as vdevice:
                encoder_model = vdevice.create_infer_model(self._encoder_path)
                decoder_model = vdevice.create_infer_model(self._decoder_path)

                encoder_model.input().set_format_type(FormatType.FLOAT32)
                encoder_model.output().set_format_type(FormatType.FLOAT32)
                decoder_model.input(f"{decoder_model_name}/input_layer1").set_format_type(FormatType.FLOAT32)
                decoder_model.input(f"{decoder_model_name}/input_layer2").set_format_type(FormatType.FLOAT32)
                for name in sorted_output_names:
                    decoder_model.output(name).set_format_type(FormatType.FLOAT32)

                with encoder_model.configure() as encoder_configured:
                    with decoder_model.configure() as decoder_configured:
                        encoder_bindings = encoder_configured.create_bindings()
                        decoder_bindings = decoder_configured.create_bindings()

                        log.info("Hailo NPU ready — entering inference loop")

                        segmenter = self._make_segmenter()
                        timeout_ms = 10000

                        while not self._stop_event.is_set():
                            try:
                                data = self.pcm_queue.get(timeout=1)
                            except Exception:
                                continue

                            if self._segmenter_dirty:
                                segmenter = self._make_segmenter()
                                self._segmenter_dirty = False

                            # Signal level from raw data
                            if len(data) >= 4096:
                                rms = compute_rms(data[-4096:])
                                preset = self._current_preset or {}
                                self.emit_fn("signal_level", {
                                    "rms": round(rms, 1),
                                    "freq": preset.get("freq", ""),
                                    "noise_floor_db": (
                                        round(segmenter.noise.noise_floor_db, 1)
                                        if getattr(segmenter, "noise", None)
                                        and segmenter.noise.ready else None),
                                    "excess_db": (
                                        round(segmenter.noise.excess_db(rms), 1)
                                        if getattr(segmenter, "noise", None)
                                        and segmenter.noise.ready else None),
                                    "segment": _segmenter_progress(segmenter),
                                })
                                self._stats["noise_floor_db"] = (
                                    round(segmenter.noise.noise_floor_db, 1)
                                    if getattr(segmenter, "noise", None)
                                    and segmenter.noise.ready else None)

                            # Feed into segmenter (VAD or continuous)
                            vad_segments = segmenter.feed(data)
                            for chunk in vad_segments:
                                if not segmenter.should_transcribe(chunk):
                                    self._stats["chunks_skipped_silence"] += 1
                                    continue

                                audio_s = len(chunk) / (SAMPLE_RATE * 2)

                                # --- Mel spectrogram ---
                                samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
                                samples = pad_or_trim(samples, CHUNK_SAMPLES)
                                mel = log_mel_spectrogram(samples)  # numpy (n_mels, n_frames)

                                mel_np = np.expand_dims(mel, axis=0)
                                mel_np = np.expand_dims(mel_np, axis=2)
                                mel_np = np.transpose(mel_np, (0, 2, 3, 1))  # NHWC
                                input_mel = np.ascontiguousarray(mel_np)

                                expected_size = int(np.prod(encoder_model.input().shape)) * 4
                                if input_mel.nbytes != expected_size:
                                    log.warning("Mel buffer size %d != expected %d, skipping",
                                                input_mel.nbytes, expected_size)
                                    continue

                                try:
                                    # --- Encoder ---
                                    t_enc_start = time.monotonic()
                                    encoder_bindings.input().set_buffer(input_mel)
                                    enc_out_buf = np.zeros(encoder_model.output().shape, dtype=np.float32)
                                    encoder_bindings.output().set_buffer(enc_out_buf)
                                    encoder_configured.run([encoder_bindings], timeout_ms)
                                    encoded_features = encoder_bindings.output().get_buffer()
                                    t_enc_end = time.monotonic()

                                    # --- Decoder (iterative) ---
                                    t_dec_start = time.monotonic()
                                    decoder_input_ids = np.zeros((1, DECODER_SEQUENCE_LENGTH), dtype=np.int64)
                                    # Seed with Whisper decode prefix:
                                    # <|startoftranscript|> <|en|> <|transcribe|> <|notimestamps|>
                                    prefix_len = len(DECODE_PREFIX)
                                    for pi, tok in enumerate(DECODE_PREFIX):
                                        decoder_input_ids[0][pi] = tok
                                    generated_tokens = []

                                    for i in range(prefix_len, DECODER_SEQUENCE_LENGTH):
                                        tokenized_ids = self._tokenization(decoder_input_ids)

                                        decoder_bindings.input(f"{decoder_model_name}/input_layer1").set_buffer(encoded_features)
                                        decoder_bindings.input(f"{decoder_model_name}/input_layer2").set_buffer(tokenized_ids)

                                        buffers = [
                                            np.zeros(decoder_model.output(name).shape, dtype=np.float32)
                                            for name in sorted_output_names
                                        ]
                                        for name, buf in zip(sorted_output_names, buffers):
                                            decoder_bindings.output(name).set_buffer(buf)

                                        decoder_configured.run([decoder_bindings], timeout_ms)

                                        decoder_outputs = np.concatenate(
                                            [decoder_bindings.output(name).get_buffer() for name in sorted_output_names],
                                            axis=2,
                                        )

                                        logits = _apply_repetition_penalty(
                                            decoder_outputs[:, i - 1], generated_tokens
                                        )
                                        next_token = int(np.argmax(logits))
                                        generated_tokens.append(next_token)
                                        decoder_input_ids[0][i] = next_token

                                        if next_token == self._tokenizer.eos_token_id:
                                            break

                                    t_dec_end = time.monotonic()

                                    # --- Stats ---
                                    encoder_ms = (t_enc_end - t_enc_start) * 1000
                                    decoder_ms = (t_dec_end - t_dec_start) * 1000
                                    total_ms = encoder_ms + decoder_ms
                                    n_tokens = len(generated_tokens)

                                    self._stats.update({
                                        "backend": "hailo",
                                        "chunks_processed": self._stats["chunks_processed"] + 1,
                                        "total_tokens": self._stats["total_tokens"] + n_tokens,
                                        "last_encoder_ms": round(encoder_ms, 1),
                                        "last_decoder_ms": round(decoder_ms, 1),
                                        "last_total_ms": round(total_ms, 1),
                                        "last_tokens": n_tokens,
                                        "last_tokens_per_sec": round(n_tokens / (decoder_ms / 1000), 1) if decoder_ms > 0 else 0,
                                        "last_rtf": round((total_ms / 1000) / audio_s, 3) if audio_s > 0 else 0,
                                        "last_decoder_steps": n_tokens,
                                        "audio_duration_s": round(audio_s, 1),
                                    })
                                    self.emit_fn("inference_stats", self._stats)

                                    text = self._tokenizer.decode(generated_tokens, skip_special_tokens=True)
                                    filtered = (hallucination_reason(text)
                                                if text and text.strip() else "empty")
                                    if filtered:
                                        self._note_filtered(text, filtered)
                                    else:
                                        preset = self._current_preset or {}
                                        text_clean = text.strip()
                                        text_clean, parsed_data = self._post_process(text_clean)
                                        segment = {
                                            "timestamp": datetime.now().strftime("%H:%M:%S"),
                                            "freq": preset.get("freq", ""),
                                            "label": preset.get("label", ""),
                                            "text": text_clean,
                                            "rms": round(compute_rms(chunk), 1),
                                        }
                                        self.emit_fn("transcript", segment)
                                        if parsed_data and self._weather_callback:
                                            self._weather_callback(parsed_data)
                                        if self._transcript_callback:
                                            self._transcript_callback(text_clean)

                                except Exception as e:
                                    log.error("Hailo inference error: %s", e)

        except Exception as e:
            log.error("Hailo device/configure failed (%s): %s", type(e).__name__, e, exc_info=True)
            if self._forced_backend == "hailo":
                log.error("Backend forced to hailo — not falling back; transcription halted")
                return
            log.warning("Falling back to CPU for this session")
            if FASTER_WHISPER_AVAILABLE:
                self._backend = "cpu"
                self._init_faster_whisper()
                self._inference_loop_cpu()

    def _tokenization(self, decoder_input_ids):
        """Token embedding lookup → add positional bias → reshape to NHWC."""
        gather_output = self._token_embedding_weight[decoder_input_ids]
        add_output = gather_output + self._onnx_add_input
        unsqueeze_output = np.expand_dims(add_output, axis=1)
        transpose_output = np.transpose(unsqueeze_output, (0, 2, 1, 3))
        return transpose_output

    def _transcribe_cpu(self, pcm_bytes):
        if not self._whisper_model:
            return None
        try:
            samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            segments, _ = self._whisper_model.transcribe(
                samples, language="en", beam_size=1, vad_filter=True
            )
            text = " ".join(seg.text for seg in segments)
            return text
        except Exception as e:
            log.error("CPU transcription error: %s", e)
            return None
