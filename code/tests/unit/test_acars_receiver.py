"""Unit tests for the acarsdec ACARS receiver + ADS-B correlation."""

import json

from ravensdr.acars_receiver import (
    AcarsReceiver, flight_digits, correlate_with_adsb,
)


def _rx():
    return AcarsReceiver(device_index=0)


class TestParseLine:
    def test_parses_msg_json(self):
        line = json.dumps({
            "timestamp": 1721835600, "freq": 131.55, "level": -18.0,
            "mode": "2", "label": "H1", "tail": "N827NN", "flight": "AA1234",
            "msgno": "M01A", "text": "HELLO FROM THE COCKPIT",
        })
        rec = _rx().parse_line(line)
        assert rec["tail"] == "N827NN"
        assert rec["flight"] == "AA1234"
        assert rec["label"] == "H1"
        assert rec["text"] == "HELLO FROM THE COCKPIT"

    def test_nested_acars_key(self):
        line = json.dumps({"timestamp": 1, "acars": {"tail": "N1", "flight": "UA9", "text": "x"}})
        rec = _rx().parse_line(line)
        assert rec["tail"] == "N1"
        assert rec["flight"] == "UA9"

    def test_non_json_ignored(self):
        assert _rx().parse_line("Acarsdec 3.7 starting") is None

    def test_record_key_prefers_tail(self):
        rx = _rx()
        assert rx.record_key({"tail": "N5", "flight": "AA1"}) == "N5"
        assert rx.record_key({"flight": "AA1"}) == "AA1"
        assert rx.record_key({"msgno": "M2"}) == "msg-M2"

    def test_build_cmd_has_channels(self):
        cmd = AcarsReceiver(device_index=2, channels=["131.550", "130.025"]).build_cmd()
        assert cmd[0] == "acarsdec"
        assert "-o" in cmd and "4" in cmd
        assert "2" in cmd
        assert "131.550" in cmd and "130.025" in cmd


class TestFlightDigits:
    def test_trailing_digits(self):
        assert flight_digits("AAL1234") == "1234"
        assert flight_digits("AA1234") == "1234"
        assert flight_digits("N827NN") == ""   # reg has no trailing digits
        assert flight_digits("") == ""

    def test_stops_at_gap(self):
        assert flight_digits("AB12CD34") == "34"


class TestCorrelation:
    FLIGHTS = [
        {"hex": "a1b2c3", "flight": "AAL1234"},
        {"hex": "d4e5f6", "flight": "UAL0987"},
    ]

    def test_matches_on_flight_digits(self):
        m = correlate_with_adsb({"flight": "AA1234"}, self.FLIGHTS)
        assert m is not None and m["hex"] == "a1b2c3"

    def test_no_match_when_digits_differ(self):
        assert correlate_with_adsb({"flight": "AA5555"}, self.FLIGHTS) is None

    def test_short_flight_number_not_matched(self):
        # <3 digits is too ambiguous to correlate
        assert correlate_with_adsb({"flight": "AA12"}, [{"hex": "x", "flight": "ABC12"}]) is None

    def test_no_flight_field(self):
        assert correlate_with_adsb({"tail": "N1"}, self.FLIGHTS) is None

    def test_empty_flight_list(self):
        assert correlate_with_adsb({"flight": "AA1234"}, []) is None
