"""Unit tests for ObservationLog — durable emitter sighting history.

Decoder tables are in-memory with a TTL, so a sensor beaconing every 15 minutes
vanishes between transmissions and everything is lost on restart. This log is
what makes "track an ID over time" possible.
"""

import json

import pytest

from ravensdr.observation_log import ObservationLog


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def log_path(tmp_path):
    return str(tmp_path / "obs.json")


class TestObserving:
    def test_first_sighting_creates_entry(self, log_path):
        clock = _Clock()
        obs = ObservationLog(log_path, clock=clock)
        entry = obs.observe("ism", "907b0418", {"model": "LandisGyr-GS"})
        assert entry["count"] == 1
        assert entry["first_seen"] == entry["last_seen"] == 1000.0
        assert entry["model"] == "LandisGyr-GS"

    def test_repeat_sighting_keeps_first_seen_and_counts(self, log_path):
        clock = _Clock()
        obs = ObservationLog(log_path, clock=clock)
        obs.observe("ism", "abc")
        clock.advance(3600)
        entry = obs.observe("ism", "abc")
        assert entry["count"] == 2
        assert entry["first_seen"] == 1000.0      # preserved
        assert entry["last_seen"] == 4600.0       # advanced

    def test_sources_are_namespaced(self, log_path):
        obs = ObservationLog(log_path)
        obs.observe("ism", "1")
        obs.observe("aprs", "1")
        assert len(obs.entries()) == 2
        assert len(obs.entries(source="ism")) == 1

    def test_metadata_updates_without_losing_history(self, log_path):
        obs = ObservationLog(log_path)
        obs.observe("ism", "x", {"model": "old"})
        entry = obs.observe("ism", "x", {"model": "new"})
        assert entry["model"] == "new"
        assert entry["count"] == 2

    def test_none_metadata_values_do_not_overwrite(self, log_path):
        obs = ObservationLog(log_path)
        obs.observe("ism", "x", {"model": "Oregon-v1"})
        entry = obs.observe("ism", "x", {"model": None})
        assert entry["model"] == "Oregon-v1"

    def test_empty_key_ignored(self, log_path):
        obs = ObservationLog(log_path)
        assert obs.observe("ism", None) is None
        assert obs.observe("ism", "") is None
        assert obs.entries() == []

    def test_best_rssi_tracks_the_strongest(self, log_path):
        obs = ObservationLog(log_path)
        obs.observe("ism", "x", rssi=-12.0)
        obs.observe("ism", "x", rssi=-3.0)
        entry = obs.observe("ism", "x", rssi=-20.0)
        assert entry["best_rssi"] == -3.0
        assert entry["last_rssi"] == -20.0


class TestOrderingAndLimits:
    def test_entries_sorted_most_recent_first(self, log_path):
        clock = _Clock()
        obs = ObservationLog(log_path, clock=clock)
        obs.observe("ism", "old")
        clock.advance(100)
        obs.observe("ism", "new")
        assert [e["key"] for e in obs.entries()] == ["new", "old"]

    def test_cap_evicts_least_recently_seen(self, log_path):
        clock = _Clock()
        obs = ObservationLog(log_path, max_entries=2, clock=clock)
        obs.observe("ism", "a")
        clock.advance(10)
        obs.observe("ism", "b")
        clock.advance(10)
        obs.observe("ism", "c")
        keys = {e["key"] for e in obs.entries()}
        assert keys == {"b", "c"}      # "a" was stalest

    def test_limit_argument(self, log_path):
        obs = ObservationLog(log_path)
        for i in range(5):
            obs.observe("ism", str(i))
        assert len(obs.entries(limit=2)) == 2


class TestPersistence:
    def test_survives_a_restart(self, log_path):
        """The whole point: history outlives the process."""
        obs = ObservationLog(log_path)
        obs.observe("ism", "907b0418", {"model": "LandisGyr-GS"})
        obs.observe("ism", "907b0418")
        obs.save()

        reloaded = ObservationLog(log_path).load()
        entry = reloaded.get("ism", "907b0418")
        assert entry["count"] == 2
        assert entry["model"] == "LandisGyr-GS"

    def test_counts_continue_accumulating_after_reload(self, log_path):
        obs = ObservationLog(log_path)
        obs.observe("ism", "x")
        obs.save()
        reloaded = ObservationLog(log_path).load()
        assert reloaded.observe("ism", "x")["count"] == 2

    def test_writes_are_debounced(self, log_path):
        """A busy channel must not sync on every packet — SD card wear."""
        clock = _Clock()
        obs = ObservationLog(log_path, save_interval_s=30, clock=clock)
        obs.observe("ism", "a")           # triggers first save (last_save=0)
        writes_after_first = obs._last_save
        for _ in range(50):
            obs.observe("ism", "a")
        assert obs._last_save == writes_after_first    # no further writes
        clock.advance(31)
        obs.observe("ism", "a")
        assert obs._last_save > writes_after_first

    def test_force_save_ignores_debounce(self, log_path):
        clock = _Clock()
        obs = ObservationLog(log_path, save_interval_s=999, clock=clock)
        obs.observe("ism", "a")
        obs.observe("ism", "b")
        assert obs.maybe_save(force=True) is True

    def test_missing_file_loads_empty(self, log_path):
        assert ObservationLog(log_path).load().entries() == []

    def test_corrupt_file_does_not_raise(self, log_path):
        with open(log_path, "w") as fh:
            fh.write("{not json")
        assert ObservationLog(log_path).load().entries() == []

    def test_saved_file_is_valid_json(self, log_path):
        obs = ObservationLog(log_path)
        obs.observe("aprs", "KI7ABC-9", {"type": "position"})
        obs.save()
        with open(log_path) as fh:
            data = json.load(fh)
        assert data["entries"][0]["key"] == "KI7ABC-9"

    def test_stats_summarise_by_source(self, log_path):
        obs = ObservationLog(log_path)
        obs.observe("ism", "a")
        obs.observe("ism", "b")
        obs.observe("aprs", "c")
        stats = obs.stats()
        assert stats["total"] == 3
        assert stats["by_source"] == {"ism": 2, "aprs": 1}
