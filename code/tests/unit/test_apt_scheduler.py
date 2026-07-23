"""Unit tests for APT satellite pass scheduler."""

import datetime
from unittest.mock import patch, MagicMock

import pytest

from ravensdr.apt_scheduler import AptScheduler, NOAA_SATS, MIN_ELEVATION_DEG


# Sample TLE data (real-format but fictional epoch)
SAMPLE_TLE_TEXT = """NOAA 15
1 25338U 98030A   26060.50000000  .00000050  00000-0  40000-4 0  9999
2 25338  98.7200 120.0000 0010500  90.0000 270.0000 14.25900000100001
NOAA 18
1 28654U 05018A   26060.50000000  .00000050  00000-0  40000-4 0  9999
2 28654  99.0000 150.0000 0015000 100.0000 260.0000 14.12400000100002
NOAA 19
1 33591U 09005A   26060.50000000  .00000050  00000-0  40000-4 0  9999
2 33591  99.1900 060.0000 0014000  50.0000 310.0000 14.12300000100003
"""


class TestTleParsing:

    def test_parse_noaa15_and_noaa19(self):
        scheduler = AptScheduler()
        scheduler._parse_tles(SAMPLE_TLE_TEXT)
        assert "NOAA 15" in scheduler._tle_data
        assert "NOAA 19" in scheduler._tle_data

    def test_noaa18_excluded(self):
        scheduler = AptScheduler()
        scheduler._parse_tles(SAMPLE_TLE_TEXT)
        assert "NOAA 18" not in scheduler._tle_data

    def test_tle_has_two_lines(self):
        scheduler = AptScheduler()
        scheduler._parse_tles(SAMPLE_TLE_TEXT)
        for name, (line1, line2) in scheduler._tle_data.items():
            assert line1.startswith("1 ")
            assert line2.startswith("2 ")

    def test_empty_tle_text(self):
        scheduler = AptScheduler()
        scheduler._parse_tles("")
        assert len(scheduler._tle_data) == 0


class TestPassPrediction:

    @patch("ravensdr.apt_scheduler.AptScheduler._refresh_tles_if_stale")
    def test_get_next_passes_returns_list(self, mock_refresh):
        scheduler = AptScheduler()
        scheduler._parse_tles(SAMPLE_TLE_TEXT)
        scheduler._tle_last_fetch = datetime.datetime.utcnow()
        passes = scheduler.get_next_passes(hours=24)
        assert isinstance(passes, list)

    @patch("ravensdr.apt_scheduler.AptScheduler._refresh_tles_if_stale")
    def test_passes_have_required_fields(self, mock_refresh):
        scheduler = AptScheduler()
        scheduler._parse_tles(SAMPLE_TLE_TEXT)
        scheduler._tle_last_fetch = datetime.datetime.utcnow()
        passes = scheduler.get_next_passes(hours=24)
        for p in passes:
            assert "satellite" in p
            assert "frequency" in p
            assert "aos" in p
            assert "los" in p
            assert "max_elevation" in p
            assert "duration" in p

    @patch("ravensdr.apt_scheduler.AptScheduler._refresh_tles_if_stale")
    def test_passes_filtered_by_elevation(self, mock_refresh):
        scheduler = AptScheduler()
        scheduler._parse_tles(SAMPLE_TLE_TEXT)
        scheduler._tle_last_fetch = datetime.datetime.utcnow()
        passes = scheduler.get_next_passes(hours=24)
        for p in passes:
            assert p["max_elevation"] >= MIN_ELEVATION_DEG

    @patch("ravensdr.apt_scheduler.AptScheduler._refresh_tles_if_stale")
    def test_passes_sorted_by_aos(self, mock_refresh):
        scheduler = AptScheduler()
        scheduler._parse_tles(SAMPLE_TLE_TEXT)
        scheduler._tle_last_fetch = datetime.datetime.utcnow()
        passes = scheduler.get_next_passes(hours=24)
        for i in range(len(passes) - 1):
            assert passes[i]["aos"] <= passes[i + 1]["aos"]

    @patch("ravensdr.apt_scheduler.AptScheduler._refresh_tles_if_stale")
    def test_only_tracked_satellites(self, mock_refresh):
        scheduler = AptScheduler()
        scheduler._parse_tles(SAMPLE_TLE_TEXT)
        scheduler._tle_last_fetch = datetime.datetime.utcnow()
        passes = scheduler.get_next_passes(hours=24)
        for p in passes:
            assert p["satellite"] in NOAA_SATS

    def test_no_passes_without_tle(self):
        scheduler = AptScheduler()
        # Mark TLEs as freshly fetched but empty, so get_next_passes doesn't hit
        # the network — with no TLE data it must return no passes.
        scheduler._tle_last_fetch = datetime.datetime.utcnow()
        scheduler._tle_data = {}
        passes = scheduler.get_next_passes(hours=24)
        assert passes == []


class TestTleRefresh:

    def test_refresh_triggered_when_stale(self):
        scheduler = AptScheduler()
        scheduler._tle_last_fetch = datetime.datetime.utcnow() - datetime.timedelta(hours=25)
        with patch.object(scheduler, "_fetch_tles") as mock_fetch:
            scheduler._refresh_tles_if_stale()
            mock_fetch.assert_called_once()

    def test_no_refresh_when_fresh(self):
        scheduler = AptScheduler()
        scheduler._tle_last_fetch = datetime.datetime.utcnow() - datetime.timedelta(hours=1)
        with patch.object(scheduler, "_fetch_tles") as mock_fetch:
            scheduler._refresh_tles_if_stale()
            mock_fetch.assert_not_called()

    def test_refresh_triggered_when_never_fetched(self):
        scheduler = AptScheduler()
        with patch.object(scheduler, "_fetch_tles") as mock_fetch:
            scheduler._refresh_tles_if_stale()
            mock_fetch.assert_called_once()
