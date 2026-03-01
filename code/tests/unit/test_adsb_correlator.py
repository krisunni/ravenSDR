"""Unit tests for ADS-B callsign extraction and flight matching."""

import pytest
from ravensdr.adsb_correlator import extract_callsigns, match_flights


class TestExtractCallsigns:

    def test_extract_airline_callsign(self):
        result = extract_callsigns("Alaska 412 cleared to land")
        assert "ASA412" in result

    def test_extract_icao_callsign(self):
        result = extract_callsigns("UAL 732 turn left heading 270")
        assert "UAL732" in result

    def test_extract_n_number(self):
        result = extract_callsigns("N12345 squawk 1200")
        assert "N12345" in result

    def test_extract_n_number_with_suffix(self):
        result = extract_callsigns("N1234A contact tower")
        assert "N1234A" in result

    def test_no_match(self):
        assert extract_callsigns("wind calm altimeter 30.12") == []

    def test_multiple_callsigns(self):
        result = extract_callsigns("Alaska 412 follow Delta 89")
        assert "ASA412" in result
        assert "DAL89" in result

    def test_delta_callsign(self):
        result = extract_callsigns("Delta 1492 descend and maintain flight level 240")
        assert "DAL1492" in result

    def test_southwest_callsign(self):
        result = extract_callsigns("Southwest 237 cleared for takeoff")
        assert "SWA237" in result

    def test_case_insensitive_airline(self):
        result = extract_callsigns("ALASKA 100 runway 34 left")
        assert "ASA100" in result

    def test_no_duplicates(self):
        result = extract_callsigns("UAL 732 UAL 732")
        assert result.count("UAL732") == 1

    def test_empty_string(self):
        assert extract_callsigns("") == []


class TestMatchFlights:

    def test_match_single_flight(self):
        flights = [{"flight": "ASA412 ", "lat": 47.4, "lon": -122.3, "hex": "a1b2c3"}]
        matched = match_flights(["ASA412"], flights)
        assert len(matched) == 1
        assert matched[0]["matched_callsign"] == "ASA412"

    def test_no_match(self):
        flights = [{"flight": "DAL100 ", "lat": 47.4, "lon": -122.3}]
        matched = match_flights(["ASA412"], flights)
        assert len(matched) == 0

    def test_partial_match(self):
        flights = [{"flight": "ASA412  ", "lat": 47.4, "lon": -122.3}]
        matched = match_flights(["ASA412"], flights)
        assert len(matched) == 1

    def test_empty_flight_field(self):
        flights = [{"flight": "", "lat": 47.4, "lon": -122.3}]
        matched = match_flights(["ASA412"], flights)
        assert len(matched) == 0

    def test_missing_flight_field(self):
        flights = [{"lat": 47.4, "lon": -122.3}]
        matched = match_flights(["ASA412"], flights)
        assert len(matched) == 0

    def test_multiple_matches(self):
        flights = [
            {"flight": "ASA412 ", "lat": 47.4, "lon": -122.3},
            {"flight": "DAL89  ", "lat": 47.5, "lon": -122.2},
        ]
        matched = match_flights(["ASA412", "DAL89"], flights)
        assert len(matched) == 2
