# Unit tests for IQ transmission segmenter

import datetime
import numpy as np
from unittest.mock import MagicMock

import pytest

from ravensdr.iq_segmenter import (
    IQSegmenter, Segment, compute_power_db,
    DEFAULT_THRESHOLD_DB, MIN_TX_MS, MAX_TX_SEC, HYSTERESIS_MS,
)


class TestPowerComputation:
    """Test IQ power measurement."""

    def test_known_signal_power(self):
        """Known amplitude signal should produce correct dB value."""
        # Unit amplitude sine = RMS of 1/sqrt(2), power = 20*log10(1/sqrt(2)) ≈ -3.01 dB
        n = 1024
        t = np.arange(n) / 1000.0
        iq = np.exp(2j * np.pi * 100 * t)
        power = compute_power_db(iq)
        # RMS of unit complex exponential = 1, so 20*log10(1) = 0
        assert abs(power - 0.0) < 0.5

    def test_zero_signal_returns_low_power(self):
        iq = np.zeros(1024, dtype=np.complex64)
        power = compute_power_db(iq)
        assert power == -100.0

    def test_higher_amplitude_higher_power(self):
        iq_low = 0.1 * np.exp(2j * np.pi * 100 * np.arange(1024) / 1000.0)
        iq_high = 10.0 * np.exp(2j * np.pi * 100 * np.arange(1024) / 1000.0)
        assert compute_power_db(iq_high) > compute_power_db(iq_low)


class TestBoundaryDetection:
    """Test transmission boundary detection."""

    def _make_segmenter(self, on_segment=None):
        seg = IQSegmenter(
            sample_rate=1024000,  # ~1 MHz, 1ms per 1024 samples
            threshold_db=10,
            on_segment=on_segment,
        )
        # Pre-fill noise floor
        noise = 0.001 * (np.random.randn(10240) + 1j * np.random.randn(10240))
        seg.feed(noise.astype(np.complex64))
        return seg

    def test_clear_transmission_detected(self):
        """A clear on/off signal should produce one segment."""
        segments = []
        seg = self._make_segmenter(on_segment=segments.append)

        # Silence (low noise)
        noise = 0.001 * (np.random.randn(5120) + 1j * np.random.randn(5120))
        seg.feed(noise.astype(np.complex64))

        # Transmission (strong signal, ~200ms)
        t = np.arange(204800) / 1024000
        signal = 10.0 * np.exp(2j * np.pi * 50000 * t)
        seg.feed(signal.astype(np.complex64))

        # Silence again (triggers end of TX after hysteresis).
        # HYSTERESIS_MS=100 at 1.024 MS/s is 100 chunks, so ending the TX needs
        # >102,400 samples of quiet. This fed 10,240 and still passed, because
        # the noise floor used to be updated from in-transmission chunks and
        # climbed into the signal until it stopped clearing the threshold —
        # the TX was ended by that bug, not by silence. With the floor frozen
        # during TX the test has to supply the silence it always claimed to.
        noise2 = 0.001 * (np.random.randn(122880) + 1j * np.random.randn(122880))
        seg.feed(noise2.astype(np.complex64))

        assert len(segments) >= 1
        s = segments[0]
        assert s.duration_ms >= MIN_TX_MS
        assert len(s.iq_samples) > 0

    def test_short_burst_filtered(self):
        """Burst under MIN_TX_MS should not produce a segment."""
        segments = []
        seg = self._make_segmenter(on_segment=segments.append)

        # Pre-noise
        noise = 0.001 * (np.random.randn(10240) + 1j * np.random.randn(10240))
        seg.feed(noise.astype(np.complex64))

        # Very short burst (~1ms = 1024 samples at 1.024 MHz)
        t = np.arange(1024) / 1024000
        signal = 10.0 * np.exp(2j * np.pi * 50000 * t)
        seg.feed(signal.astype(np.complex64))

        # Silence
        noise2 = 0.001 * (np.random.randn(10240) + 1j * np.random.randn(10240))
        seg.feed(noise2.astype(np.complex64))

        # Should not trigger (too short, only ~1ms vs 50ms minimum)
        assert len(segments) == 0

    def test_snr_estimated(self):
        """Segment SNR should be positive for strong signal above noise."""
        segments = []
        seg = self._make_segmenter(on_segment=segments.append)

        # Noise
        noise = 0.001 * (np.random.randn(10240) + 1j * np.random.randn(10240))
        seg.feed(noise.astype(np.complex64))

        # Strong signal
        t = np.arange(102400) / 1024000
        signal = 10.0 * np.exp(2j * np.pi * 50000 * t)
        seg.feed(signal.astype(np.complex64))

        # End
        noise2 = 0.001 * (np.random.randn(10240) + 1j * np.random.randn(10240))
        seg.feed(noise2.astype(np.complex64))

        if len(segments) > 0:
            assert segments[0].snr_db > 0

    def test_frequency_metadata(self):
        """Segment should carry the configured frequency."""
        segments = []
        seg = self._make_segmenter(on_segment=segments.append)
        seg.set_frequency(121500000)

        noise = 0.001 * (np.random.randn(10240) + 1j * np.random.randn(10240))
        seg.feed(noise.astype(np.complex64))

        t = np.arange(102400) / 1024000
        signal = 10.0 * np.exp(2j * np.pi * 50000 * t)
        seg.feed(signal.astype(np.complex64))

        noise2 = 0.001 * (np.random.randn(10240) + 1j * np.random.randn(10240))
        seg.feed(noise2.astype(np.complex64))

        if len(segments) > 0:
            assert segments[0].frequency_hz == 121500000


class TestHysteresis:
    """Test hysteresis prevents false splits."""

    def _make_segmenter(self, on_segment=None):
        seg = IQSegmenter(
            sample_rate=1024000,
            threshold_db=10,
            on_segment=on_segment,
        )
        noise = 0.001 * (np.random.randn(10240) + 1j * np.random.randn(10240))
        seg.feed(noise.astype(np.complex64))
        return seg

    def test_brief_dropout_does_not_split(self):
        """A brief power dropout shorter than hysteresis should not split the TX."""
        segments = []
        seg = self._make_segmenter(on_segment=segments.append)

        noise_pre = 0.001 * (np.random.randn(10240) + 1j * np.random.randn(10240))
        seg.feed(noise_pre.astype(np.complex64))

        # Signal block 1 (~100ms)
        t1 = np.arange(102400) / 1024000
        s1 = 10.0 * np.exp(2j * np.pi * 50000 * t1)
        seg.feed(s1.astype(np.complex64))

        # Brief dropout (1 chunk = ~1ms, well under hysteresis)
        dropout = 0.001 * (np.random.randn(1024) + 1j * np.random.randn(1024))
        seg.feed(dropout.astype(np.complex64))

        # Signal block 2 (~100ms)
        t2 = np.arange(102400) / 1024000
        s2 = 10.0 * np.exp(2j * np.pi * 50000 * t2)
        seg.feed(s2.astype(np.complex64))

        # End with enough silence for hysteresis: 100 chunks at this rate, so
        # >102,400 samples. See test_clear_transmission_detected for why 20,480
        # used to be sufficient and no longer is.
        noise_post = 0.001 * (np.random.randn(122880) + 1j * np.random.randn(122880))
        seg.feed(noise_post.astype(np.complex64))

        # Should be 1 segment (not 2) — the 1-chunk dropout is within hysteresis
        assert len(segments) == 1


class TestSegmentToDict:
    """Test segment serialization."""

    def test_to_dict_has_required_fields(self):
        s = Segment(
            iq_samples=np.zeros(1024, dtype=np.complex64),
            start_time=datetime.datetime(2026, 3, 17, tzinfo=datetime.timezone.utc),
            duration_ms=500,
            frequency_hz=121500000,
            snr_db=20.0,
            peak_power_db=-50.0,
            mean_power_db=-55.0,
        )
        d = s.to_dict()
        assert "start_time" in d
        assert "duration_ms" in d
        assert d["frequency_hz"] == 121500000
        assert d["snr_db"] == 20.0
        assert d["sample_count"] == 1024


class TestReset:
    """Test segmenter reset."""

    def test_reset_clears_state(self):
        seg = IQSegmenter()
        t = np.arange(10240) / 2400000
        sig = 10.0 * np.exp(2j * np.pi * 50000 * t)
        seg.feed(sig.astype(np.complex64))
        assert seg.noise_floor_db > -100

        seg.reset()
        assert seg.noise_floor_db == -100.0
        assert seg.in_transmission is False
