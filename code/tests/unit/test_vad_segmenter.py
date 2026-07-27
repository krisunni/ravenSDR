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


def prime_floor(vad, rms_level=200.0, frames=20):
    """Establish a noise floor without emitting segments.

    The segmenter now gates on level ABOVE the local floor rather than an
    absolute RMS, so a test that feeds a bare constant tone with no floor is not
    a realistic signal — on air there is always hiss between transmissions.
    Priming the tracker directly gives the floor without the noise itself being
    segmented, keeping each test focused on the behaviour it names.
    """
    for _ in range(frames):
        vad.noise.update(rms_level)
    return vad


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
        vad = prime_floor(VoiceActivitySegmenter())
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
        vad = prime_floor(VoiceActivitySegmenter())
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


class TestAdaptiveNoiseGate:
    """The bug this replaced: unsquelched FM hiss sailed through a fixed gate.

    Measured on a silent 2m repeater: RMS 1342-1523 against a 500 threshold, so
    0% of frames were rejected and the NPU transcribed static for 75 minutes,
    discarding 97.5% of its own output as hallucinations.
    """

    def test_steady_hiss_is_rejected(self):
        from ravensdr.transcriber import NoiseFloorTracker
        tracker = NoiseFloorTracker()
        for _ in range(50):
            tracker.update(1440.0)          # the measured floor
        assert tracker.ready
        assert tracker.is_signal(1440.0) is False, "hiss must not count as speech"

    def test_speech_above_hiss_is_accepted(self):
        from ravensdr.transcriber import NoiseFloorTracker
        tracker = NoiseFloorTracker()
        for _ in range(50):
            tracker.update(1440.0)
        # ~11 dB over the floor — a normal speech margin.
        assert tracker.is_signal(5000.0) is True

    def test_floor_follows_a_quieter_channel(self):
        """A fixed threshold cannot adapt to gain/frequency changes; this must."""
        from ravensdr.transcriber import NoiseFloorTracker
        tracker = NoiseFloorTracker()
        for _ in range(50):
            tracker.update(1440.0)
        loud_floor = tracker.noise_floor_db
        for _ in range(200):
            tracker.update(120.0)           # squelch closed / quieter band
        assert tracker.noise_floor_db < loud_floor - 15
        # Speech that would have been rejected against the loud floor now passes.
        assert tracker.is_signal(400.0) is True

    def test_low_percentile_survives_a_busy_channel(self):
        """A median would become the SPEECH level once the channel is busy."""
        from ravensdr.transcriber import NoiseFloorTracker
        tracker = NoiseFloorTracker()
        for i in range(100):
            tracker.update(1440.0 if i % 5 == 0 else 6000.0)   # 80% speech
        # Floor must still sit near the hiss, not near the speech.
        assert tracker.noise_floor_db < 70.0
        assert tracker.is_signal(6000.0) is True

    def test_digital_silence_never_passes(self):
        from ravensdr.transcriber import NoiseFloorTracker
        tracker = NoiseFloorTracker()
        for _ in range(50):
            tracker.update(2.0)             # near-zero floor
        assert tracker.is_signal(5.0) is False, "must not chase an absolute-zero floor"

    def test_falls_back_to_absolute_gate_before_floor_settles(self):
        from ravensdr.transcriber import NoiseFloorTracker
        tracker = NoiseFloorTracker()
        assert not tracker.ready
        assert tracker.is_signal(5000.0) is True     # loud -> pass
        assert tracker.is_signal(100.0) is False     # quiet -> reject

    def test_segmenter_exposes_the_gate(self):
        vad = prime_floor(VoiceActivitySegmenter(), rms_level=1440.0, frames=50)
        assert vad.should_transcribe(make_pcm(1.0, amplitude=8000)) is True
        assert vad.should_transcribe(make_pcm(1.0, amplitude=1400)) is False

    def test_continuous_segmenter_keeps_the_absolute_gate(self):
        """NOAA/TIS loops are meant to be transcribed wholesale."""
        from ravensdr.transcriber import ContinuousSegmenter
        seg = ContinuousSegmenter()
        assert seg.should_transcribe(make_pcm(1.0, amplitude=5000)) is True


class TestFloorFreezeDuringSignal:
    """A long transmission must not teach the floor its own speech level.

    Without freezing, a net or ragchew fills the whole window with speech, the
    percentile climbs into the speech, and the gate shuts mid-over.
    """

    def test_long_transmission_does_not_close_the_gate(self):
        from ravensdr.transcriber import NoiseFloorTracker
        tracker = NoiseFloorTracker(window=50)
        for _ in range(50):
            tracker.update(1440.0)              # establish hiss floor
        floor = tracker.noise_floor_db
        # 40s of unbroken speech (400 frames) well above the floor.
        for _ in range(400):
            tracker.update(6000.0)
        assert tracker.noise_floor_db == pytest.approx(floor, abs=1.0), \
            "speech taught the floor its own level"
        assert tracker.is_signal(6000.0) is True, "gate closed mid-transmission"

    def test_stale_floor_is_relearned(self):
        """Freezing must not be permanent — a wrong floor has to recover."""
        from ravensdr.transcriber import NoiseFloorTracker
        tracker = NoiseFloorTracker(window=20, stale_frames=50)
        for _ in range(20):
            tracker.update(50.0)               # very quiet floor
        # Band changes: everything is now loud, and stays loud far past a window.
        for _ in range(200):
            tracker.update(4000.0)
        assert tracker.noise_floor_db > 60.0, "floor never relearned after a change"

    def test_quiet_frames_still_teach_the_floor(self):
        from ravensdr.transcriber import NoiseFloorTracker
        tracker = NoiseFloorTracker(window=30)
        for _ in range(30):
            tracker.update(2000.0)
        loud = tracker.noise_floor_db
        for _ in range(60):
            tracker.update(100.0)              # channel goes quiet
        assert tracker.noise_floor_db < loud - 10


class TestSegmenterFollowsPreset:
    """The loops build a segmenter once, so a preset change must force a rebuild.

    Without this, tuning from a voice preset to a continuous broadcast kept the
    VAD segmenter and its floor-relative gate rejected the programme audio.
    """

    def _transcriber(self):
        from ravensdr.transcriber import Transcriber
        return Transcriber(pcm_queue=None, emit_fn=lambda *a, **k: None)

    def test_set_preset_marks_segmenter_dirty(self):
        t = self._transcriber()
        t._segmenter_dirty = False
        t.set_preset({"id": "kuow-fm", "continuous": True})
        assert t._segmenter_dirty is True

    def test_continuous_preset_selects_continuous_segmenter(self):
        from ravensdr.transcriber import ContinuousSegmenter
        t = self._transcriber()
        t.set_preset({"id": "kuow-fm", "label": "KUOW", "continuous": True})
        assert isinstance(t._make_segmenter(), ContinuousSegmenter)

    def test_noaa_preset_still_selects_continuous_segmenter(self):
        from ravensdr.transcriber import ContinuousSegmenter
        t = self._transcriber()
        t.set_preset({"id": "noaa-seattle", "parser": "noaa", "squelch": 0})
        assert isinstance(t._make_segmenter(), ContinuousSegmenter)

    def test_voice_preset_selects_vad_segmenter(self):
        t = self._transcriber()
        t.set_preset({"id": "redmond-ares", "mode": "fm", "squelch": 0})
        assert isinstance(t._make_segmenter(), VoiceActivitySegmenter)
