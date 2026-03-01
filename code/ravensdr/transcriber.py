# Hailo Whisper wrapper + audio chunking

import logging
import os
import threading
import time
from datetime import datetime

import numpy as np

log = logging.getLogger(__name__)

SILENCE_THRESHOLD = 500    # RMS value — below this, skip inference
CHUNK_SAMPLES = 160000     # 10 seconds at 16kHz (matches Hailo encoder input)
SAMPLE_RATE = 16000

# Voice-activity segmentation constants
VAD_SILENCE_THRESHOLD = 400   # RMS below this = silence
VAD_HOLDOFF_MS = 300          # silence must last this long to trigger a split
VAD_MIN_SEGMENT_S = 1.0       # don't send segments shorter than this
VAD_MAX_SEGMENT_S = 15.0      # force-split if speech runs longer than this
VAD_FRAME_SIZE = 1600          # 100ms frames at 16kHz

# Continuous capture constants (NOAA weather radio)
CONTINUOUS_SEGMENT_S = 30.0    # fixed segment duration for continuous broadcasts
CONTINUOUS_OVERLAP_S = 2.0     # overlap between segments to avoid cutting words

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
DECODER_SEQUENCE_LENGTH = 32  # max tokens for whisper-tiny
START_TOKEN_ID = 50258
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


def is_signal_present(pcm_bytes):
    return compute_rms(pcm_bytes) > SILENCE_THRESHOLD


def _apply_repetition_penalty(logits, generated_tokens, penalty=REPETITION_PENALTY,
                              last_window=REPETITION_WINDOW):
    """Discourage repeated tokens by dividing their logits by the penalty."""
    logits = np.squeeze(logits, axis=0)
    recent = set(generated_tokens[-last_window:])
    for token in recent:
        if token not in EXCLUDED_TOKENS:
            logits[token] /= penalty
    return logits


class VoiceActivitySegmenter:
    """Accumulates PCM and splits on silence boundaries instead of fixed time."""

    def __init__(self):
        self._pending = b""
        self._silence_frames = 0
        self._holdoff_frames = int(VAD_HOLDOFF_MS / 100)

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

            if rms < VAD_SILENCE_THRESHOLD:
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

    def _flush(self) -> bytes:
        seg = self._pending
        self._pending = b""
        self._silence_frames = 0
        return seg

    def reset(self):
        self._pending = b""
        self._silence_frames = 0


class ContinuousSegmenter:
    """Time-based segmentation for continuous broadcasts (e.g. NOAA weather radio).

    Splits audio into fixed-duration chunks with overlap to avoid cutting words.
    Used instead of VAD when the broadcast has no silence gaps.
    """

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


class Transcriber:
    """Accumulates PCM chunks, detects silence, runs Whisper inference."""

    def __init__(self, pcm_queue, emit_fn):
        self.pcm_queue = pcm_queue
        self.emit_fn = emit_fn        # callback to emit transcript + signal_level
        self._stop_event = threading.Event()
        self._thread = None
        self._current_preset = None
        self._whisper_model = None
        self._transcript_callback = None  # called with text on each transcript
        self._weather_callback = None     # called with parsed NOAA data

        # Inference stats
        self._stats = {
            "backend": "none",
            "chunks_processed": 0,
            "chunks_skipped_silence": 0,
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

        # Initialize inference backend
        if HAILO_AVAILABLE:
            self._backend = "hailo"
            self._init_hailo()
        elif FASTER_WHISPER_AVAILABLE:
            self._backend = "cpu"
            self._init_faster_whisper()
        else:
            self._backend = "none"
            log.warning("No Whisper backend available — transcription disabled")

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
            log.warning("Hailo init failed (%s), falling back to CPU", e)
            if FASTER_WHISPER_AVAILABLE:
                self._backend = "cpu"
                self._init_faster_whisper()
            else:
                self._backend = "none"

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
        self._current_preset = preset

    def set_transcript_callback(self, callback):
        """Set callback(text) called on each non-empty transcript."""
        self._transcript_callback = callback

    def set_weather_callback(self, callback):
        """Set callback(parsed_data) called when NOAA parser produces results."""
        self._weather_callback = callback

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
            self._thread.join(timeout=5)
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

    def _make_segmenter(self):
        """Choose segmenter based on current preset configuration."""
        preset = self._current_preset or {}
        if preset.get("parser") == "noaa" and preset.get("squelch", -1) == 0:
            log.info("Using continuous segmenter (%.0fs chunks) for NOAA broadcast",
                     CONTINUOUS_SEGMENT_S)
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

            # Signal level from raw data
            if len(data) >= 4096:
                rms = compute_rms(data[-4096:])
                preset = self._current_preset or {}
                self.emit_fn("signal_level", {
                    "rms": round(rms, 1),
                    "freq": preset.get("freq", ""),
                })

            # Feed into segmenter — get back segments (VAD or time-based)
            segments = segmenter.feed(data)
            for chunk in segments:
                if not is_signal_present(chunk):
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

                if text and text.strip():
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

        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN

        decoder_hef = HEF(self._decoder_path)
        sorted_output_names = decoder_hef.get_sorted_output_names()
        decoder_model_name = decoder_hef.get_network_group_names()[0]

        try:
            with VDevice(params) as vdevice:
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

                            # Signal level from raw data
                            if len(data) >= 4096:
                                rms = compute_rms(data[-4096:])
                                preset = self._current_preset or {}
                                self.emit_fn("signal_level", {
                                    "rms": round(rms, 1),
                                    "freq": preset.get("freq", ""),
                                })

                            # Feed into segmenter (VAD or continuous)
                            vad_segments = segmenter.feed(data)
                            for chunk in vad_segments:
                                if not is_signal_present(chunk):
                                    self._stats["chunks_skipped_silence"] += 1
                                    continue

                                audio_s = len(chunk) / (SAMPLE_RATE * 2)

                            # --- Mel spectrogram ---
                            samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
                            samples = pad_or_trim(samples, CHUNK_SAMPLES)
                            mel = log_mel_spectrogram(samples)

                            mel_np = mel.cpu().numpy()
                            mel_np = np.expand_dims(mel_np, axis=0)
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
                                decoder_input_ids[0][0] = START_TOKEN_ID
                                generated_tokens = []

                                for i in range(DECODER_SEQUENCE_LENGTH - 1):
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
                                        decoder_outputs[:, i], generated_tokens
                                    )
                                    next_token = int(np.argmax(logits))
                                    generated_tokens.append(next_token)
                                    decoder_input_ids[0][i + 1] = next_token

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
                                if text and text.strip():
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
            log.error("Hailo device/configure failed: %s", e)
            log.info("Falling back to CPU for this session")
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
