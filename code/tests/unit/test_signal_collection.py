"""Unit tests for preset-labelled training-corpus collection.

The original trigger only saved a sample when the classifier's own output
already matched the preset — circular, since a trained classifier is needed to
collect the data required to train one. With no HEF the CPU fallback can only
emit WFM/FM/CW/AM/SSB, so ADSB/WEFAX/NOAA_APT/P25/DMR were unreachable. After
months of running the corpus held exactly one file.
"""

import os

import numpy as np
import pytest

from ravensdr import signal_classifier as sc
from ravensdr.signal_classifier import SignalClassifier


@pytest.fixture
def collector(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "COLLECTED_DIR", str(tmp_path))
    monkeypatch.setattr(sc, "COLLECT_MIN_INTERVAL_S", 0.0)   # no rate limit in tests
    return SignalClassifier(emit_fn=lambda *a, **k: None)


def _iq(n=1024):
    return (np.random.randn(n) + 1j * np.random.randn(n)).astype(np.complex64)


class TestLabelledCollection:
    def test_saves_under_the_preset_label(self, collector, tmp_path):
        path = collector.collect_sample(_iq(), "WFM", 94_900_000, snr_db=20)
        assert path is not None
        assert os.path.basename(os.path.dirname(path)) == "WFM"
        assert os.path.exists(path)

    def test_saved_array_round_trips(self, collector):
        iq = _iq()
        path = collector.collect_sample(iq, "FM", 146_960_000, snr_db=20)
        assert np.allclose(np.load(path), iq)

    def test_filename_carries_frequency(self, collector):
        path = collector.collect_sample(_iq(), "AM", 1_650_000, snr_db=20)
        assert path.endswith("_1650000.npy")

    def test_collects_classes_the_cpu_fallback_can_never_produce(self, collector):
        """The whole point: ADSB/WEFAX are unreachable without a trained model."""
        for label in ("ADSB", "WEFAX", "NOAA_APT", "P25", "DMR"):
            assert collector.collect_sample(_iq(), label, 1090_000_000,
                                            snr_db=20) is not None

    def test_no_label_collects_nothing(self, collector):
        assert collector.collect_sample(_iq(), None, 1, snr_db=20) is None
        assert collector.collect_sample(_iq(), "", 1, snr_db=20) is None

    def test_unknown_label_is_not_a_class(self, collector):
        assert collector.collect_sample(_iq(), "unknown", 1, snr_db=20) is None


class TestQualityGates:
    def test_low_snr_is_declined(self, collector):
        """Noise labelled as FM would poison the corpus."""
        assert collector.collect_sample(_iq(), "FM", 1, snr_db=2.0) is None
        assert collector.collection_stats()["skipped_low_snr"] == 1

    def test_snr_at_threshold_is_accepted(self, collector):
        assert collector.collect_sample(_iq(), "FM", 1,
                                        snr_db=sc.COLLECT_MIN_SNR_DB) is not None

    def test_missing_snr_is_allowed(self, collector):
        """Not every path measures SNR; absence must not block collection."""
        assert collector.collect_sample(_iq(), "FM", 1) is not None

    def test_rate_limit_prevents_flooding(self, tmp_path, monkeypatch):
        """A single long transmission must not fill the corpus with near-copies."""
        monkeypatch.setattr(sc, "COLLECTED_DIR", str(tmp_path))
        monkeypatch.setattr(sc, "COLLECT_MIN_INTERVAL_S", 60.0)
        c = SignalClassifier(emit_fn=lambda *a, **k: None)
        assert c.collect_sample(_iq(), "FM", 1, snr_db=20) is not None
        assert c.collect_sample(_iq(), "FM", 1, snr_db=20) is None

    def test_rate_limit_is_per_class(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "COLLECTED_DIR", str(tmp_path))
        monkeypatch.setattr(sc, "COLLECT_MIN_INTERVAL_S", 60.0)
        c = SignalClassifier(emit_fn=lambda *a, **k: None)
        assert c.collect_sample(_iq(), "FM", 1, snr_db=20) is not None
        assert c.collect_sample(_iq(), "AM", 1, snr_db=20) is not None

    def test_per_class_cap_is_enforced(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "COLLECTED_DIR", str(tmp_path))
        monkeypatch.setattr(sc, "COLLECT_MIN_INTERVAL_S", 0.0)
        monkeypatch.setattr(sc, "COLLECT_MAX_PER_CLASS", 3)
        c = SignalClassifier(emit_fn=lambda *a, **k: None)
        saved = [c.collect_sample(_iq(), "FM", 1, snr_db=20) for _ in range(6)]
        assert len([p for p in saved if p]) == 3

    def test_unwritable_dir_is_survivable(self, collector, monkeypatch):
        """Collection must never be able to break reception."""
        monkeypatch.setattr(sc.os, "makedirs",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
        assert collector.collect_sample(_iq(), "FM", 1, snr_db=20) is None


class TestCollectionStats:
    def test_counts_are_read_from_disk(self, collector):
        for label in ("FM", "FM", "AM"):
            collector.collect_sample(_iq(), label, 1, snr_db=20)
        stats = collector.collection_stats()
        assert stats["per_class"] == {"AM": 1, "FM": 2}
        assert stats["total"] == 3

    def test_stats_survive_a_new_instance(self, collector, tmp_path, monkeypatch):
        """Restarts must not reset the reported corpus size."""
        collector.collect_sample(_iq(), "FM", 1, snr_db=20)
        monkeypatch.setattr(sc, "COLLECTED_DIR", str(tmp_path))
        fresh = SignalClassifier(emit_fn=lambda *a, **k: None)
        assert fresh.collection_stats()["total"] == 1

    def test_empty_corpus_reports_zero(self, collector):
        stats = collector.collection_stats()
        assert stats["total"] == 0 and stats["per_class"] == {}
