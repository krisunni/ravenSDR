# Unit tests for NOAA weather radio transcript parser

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ravensdr.noaa_parser import parse_weather_transcript, detect_priority_alert
from tests.fixtures.noaa_transcripts import (
    CLEAR_CONDITIONS,
    WIND_ADVISORY,
    WINTER_STORM_WARNING,
    MARINE_FORECAST,
)


class TestParseTemperature:
    def test_extracts_temperature_clear(self):
        result = parse_weather_transcript(CLEAR_CONDITIONS)
        assert result["temperature"] is not None
        assert result["temperature"]["value"] == 45
        assert result["temperature"]["unit"] == "F"

    def test_extracts_temperature_advisory(self):
        result = parse_weather_transcript(WIND_ADVISORY)
        assert result["temperature"]["value"] == 48

    def test_extracts_temperature_warning(self):
        result = parse_weather_transcript(WINTER_STORM_WARNING)
        assert result["temperature"]["value"] == 34


class TestParseWind:
    def test_extracts_wind_speed_and_direction(self):
        result = parse_weather_transcript(CLEAR_CONDITIONS)
        assert result["wind"] is not None
        assert result["wind"]["speed"] == 8
        assert result["wind"]["direction"] == "north"
        assert result["wind"]["unit"] == "mph"

    def test_extracts_wind_advisory(self):
        result = parse_weather_transcript(WIND_ADVISORY)
        assert result["wind"] is not None
        assert result["wind"]["direction"] == "south"
        assert result["wind"]["unit"] == "mph"

    def test_extracts_wind_knots_marine(self):
        result = parse_weather_transcript(MARINE_FORECAST)
        assert result["wind"] is not None
        assert result["wind"]["unit"] == "knots"


class TestParseVisibility:
    def test_extracts_visibility_clear(self):
        result = parse_weather_transcript(CLEAR_CONDITIONS)
        assert result["visibility"] is not None
        assert result["visibility"]["value"] == 10
        assert result["visibility"]["unit"] == "miles"

    def test_extracts_visibility_advisory(self):
        result = parse_weather_transcript(WIND_ADVISORY)
        assert result["visibility"]["value"] == 8

    def test_extracts_reduced_visibility(self):
        result = parse_weather_transcript(WINTER_STORM_WARNING)
        assert result["visibility"] is not None
        assert result["visibility"]["value"] == 0.25


class TestDetectPriorityAlert:
    def test_no_alert_clear_conditions(self):
        assert detect_priority_alert(CLEAR_CONDITIONS) is False

    def test_detects_wind_advisory(self):
        assert detect_priority_alert(WIND_ADVISORY) is True

    def test_detects_winter_storm_warning(self):
        assert detect_priority_alert(WINTER_STORM_WARNING) is True

    def test_detects_marine_advisory(self):
        assert detect_priority_alert(MARINE_FORECAST) is True


class TestParseAlerts:
    def test_wind_advisory_alert(self):
        result = parse_weather_transcript(WIND_ADVISORY)
        assert len(result["alerts"]) > 0
        alert_names = [a["name"].lower() for a in result["alerts"]]
        assert any("wind" in n and "advisory" in n for n in alert_names)

    def test_winter_storm_warning_alert(self):
        result = parse_weather_transcript(WINTER_STORM_WARNING)
        assert len(result["alerts"]) > 0
        alert_types = [a["type"] for a in result["alerts"]]
        assert "warning" in alert_types

    def test_clear_no_alerts(self):
        result = parse_weather_transcript(CLEAR_CONDITIONS)
        assert result["alerts"] == []


class TestParseMarineForecast:
    def test_extracts_marine_segments(self):
        result = parse_weather_transcript(MARINE_FORECAST)
        assert len(result["marine"]) > 0
        zones = [s["zone"] for s in result["marine"]]
        assert any("Puget Sound" in z for z in zones)

    def test_extracts_strait_segment(self):
        result = parse_weather_transcript(MARINE_FORECAST)
        zones = [s["zone"] for s in result["marine"]]
        assert any("Strait" in z for z in zones)


class TestConfidence:
    def test_full_confidence_all_fields(self):
        result = parse_weather_transcript(CLEAR_CONDITIONS)
        assert result["confidence"] == "full"

    def test_partial_confidence_some_fields(self):
        result = parse_weather_transcript("temperature 55 degrees some other text")
        assert result["confidence"] == "partial"

    def test_low_confidence_no_fields(self):
        result = parse_weather_transcript("this is just some random text nothing useful")
        assert result["confidence"] == "low"


class TestEdgeCases:
    def test_empty_string(self):
        result = parse_weather_transcript("")
        assert result["confidence"] == "low"
        assert result["temperature"] is None
        assert result["wind"] is None
        assert result["visibility"] is None
        assert result["alerts"] == []

    def test_none_input(self):
        result = parse_weather_transcript(None)
        assert result["confidence"] == "low"

    def test_detect_priority_alert_empty(self):
        assert detect_priority_alert("") is False
        assert detect_priority_alert(None) is False
