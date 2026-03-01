"""Unit tests for VoiceActivitySegmenter."""

import struct
import numpy as np
import pytest
from ravensdr.transcriber import (
    VoiceActivitySegmenter,
    VAD_SILENCE_THRESHOLD,
    VAD_MIN_SEGMENT_S,
    VAD_MAX_SEGMENT_S,
    VAD_FRAME_SIZE,
    SAMPLE_RATE,
)


def make_pcm(duration_s, amplitude=5000):
    """Generate PCM bytes of a sine wave at given amplitude and duration."""
    n_samples = int(SAMPLE_RATE * duration_s)
    t = np.linspace(0, duration_s, n_samples, dtype=np.float32)
    samples = (amplitude * np.sin(2 * np.pi * 440 * t)).astype(np.int16)
    return samples.tobytes()


def make_silence(duration_s):
    """Generate silent PCM bytes."""
    n_samples = int(SAMPLE_RATE * duration_s)
    return b'\x00' * (n_samples * 2)


class TestVoiceActivitySegmenter:

    def test_no_segments_from_short_audio(self):
        """Audio shorter than min segment should not produce segments."""
        vad = VoiceActivitySegmenter()
        pcm = make_pcm(0.5)
        segments = vad.feed(pcm)
        assert len(segments) == 0

    def test_silence_triggers_split(self):
        """Speech followed by silence should produce a segment."""
        vad = VoiceActivitySegmenter()
        speech = make_pcm(2.0, amplitude=5000)
        silence = make_silence(0.5)
        segments = vad.feed(speech + silence)
        assert len(segments) >= 1

    def test_short_pause_no_split(self):
        """Brief silence (<holdoff) within speech should not split."""
        vad = VoiceActivitySegmenter()
        speech1 = make_pcm(1.5, amplitude=5000)
        brief_pause = make_silence(0.1)  # 100ms < 300ms holdoff
        speech2 = make_pcm(1.5, amplitude=5000)
        segments = vad.feed(speech1 + brief_pause + speech2)
        # Should not have split the two speech parts
        # May have 0 or 1 segments depending on max duration
        # The key is no split happened at the 100ms pause
        total_seg_bytes = sum(len(s) for s in segments)
        total_input = len(speech1) + len(brief_pause) + len(speech2)
        # Either no segments yet, or one big segment
        assert len(segments) <= 1

    def test_max_duration_force_split(self):
        """Continuous speech beyond max duration should force-split."""
        vad = VoiceActivitySegmenter()
        # 20s of continuous speech should force split at 15s
        long_speech = make_pcm(20.0, amplitude=5000)
        segments = vad.feed(long_speech)
        assert len(segments) >= 1
        # First segment should be around max duration
        first_duration = len(segments[0]) / (SAMPLE_RATE * 2)
        assert first_duration >= VAD_MAX_SEGMENT_S - 0.5
        assert first_duration <= VAD_MAX_SEGMENT_S + 0.5

    def test_silence_only_no_segments(self):
        """Pure silence should not produce meaningful segments."""
        vad = VoiceActivitySegmenter()
        silence = make_silence(5.0)
        segments = vad.feed(silence)
        # May produce segments but they should all be silence
        # The is_signal_present check in the transcriber will skip them
        # VAD itself splits on silence, so it may produce segments
        # that the caller filters via is_signal_present
        for seg in segments:
            rms = np.sqrt(np.mean(np.frombuffer(seg, dtype=np.int16).astype(np.float32) ** 2))
            assert rms < VAD_SILENCE_THRESHOLD

    def test_reset_clears_state(self):
        """Reset should clear internal buffers."""
        vad = VoiceActivitySegmenter()
        speech = make_pcm(2.0)
        vad.feed(speech)
        vad.reset()
        assert vad._pending == b""
        assert vad._silence_frames == 0

    def test_feed_incremental(self):
        """Feeding data in small chunks should work the same as one big feed."""
        vad = VoiceActivitySegmenter()
        speech = make_pcm(3.0, amplitude=5000)
        silence = make_silence(0.5)
        full_data = speech + silence

        # Feed in 4096-byte chunks
        all_segments = []
        chunk_size = 4096
        for i in range(0, len(full_data), chunk_size):
            chunk = full_data[i:i + chunk_size]
            segs = vad.feed(chunk)
            all_segments.extend(segs)

        assert len(all_segments) >= 1
