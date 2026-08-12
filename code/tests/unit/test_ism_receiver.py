"""Unit tests for the rtl_433 ISM receiver parsing + record table."""

import json
import time

from ravensdr.ism_receiver import IsmReceiver, _heard_on


def _rx():
    return IsmReceiver(device_index=0)


class TestParseLine:
    def test_parses_weather_station_json(self):
        line = json.dumps({
            "time": "2026-07-24 14:00:00", "model": "Acurite-Tower",
            "id": 1234, "channel": "A", "temperature_C": 21.5,
            "humidity": 55, "battery_ok": 1, "rssi": -8.2,
        })
        rec = _rx().parse_line(line)
        assert rec["model"] == "Acurite-Tower"
        assert rec["id"] == 1234
        assert rec["temperature_C"] == 21.5
        assert rec["humidity"] == 55
        assert rec["rssi"] == -8.2

    def test_tpms_pressure_fields(self):
        line = json.dumps({"model": "Toyota-TPMS", "id": "abc123",
                           "pressure_kPa": 230.0, "temperature_C": 30})
        rec = _rx().parse_line(line)
        assert rec["model"] == "Toyota-TPMS"
        assert rec["pressure_kPa"] == 230.0

    def test_non_json_line_ignored(self):
        assert _rx().parse_line("rtl_433 version 25.02 starting") is None

    def test_json_without_model_ignored(self):
        assert _rx().parse_line(json.dumps({"id": 5, "temperature_C": 10})) is None

    def test_record_key_is_model_and_id(self):
        rx = _rx()
        rec = rx.parse_line(json.dumps({"model": "Foo", "id": 7}))
        assert rx.record_key(rec) == "Foo/7"


class TestRecordTable:
    def test_store_and_get_devices(self):
        rx = _rx()
        rx._store(rx.parse_line(json.dumps({"model": "A", "id": 1, "temperature_C": 5})))
        rx._store(rx.parse_line(json.dumps({"model": "B", "id": 2, "temperature_C": 6})))
        devices = rx.get_devices()
        assert len(devices) == 2
        assert all("seen" in d for d in devices)

    def test_same_device_updates_not_duplicates(self):
        rx = _rx()
        rx._store(rx.parse_line(json.dumps({"model": "A", "id": 1, "temperature_C": 5})))
        rx._store(rx.parse_line(json.dumps({"model": "A", "id": 1, "temperature_C": 9})))
        devices = rx.get_devices()
        assert len(devices) == 1
        assert devices[0]["temperature_C"] == 9

    def test_ttl_expiry(self):
        rx = IsmReceiver(device_index=0, ttl_sec=0)
        rx._store(rx.parse_line(json.dumps({"model": "A", "id": 1})))
        # ttl_sec=0 -> anything older than "now" is stale
        time.sleep(0.01)
        assert rx.get_devices() == []

    def test_on_record_hook_fires(self):
        rx = _rx()
        hits = []
        rx.on_record = lambda rec, is_new: hits.append((rec["model"], is_new))
        rx._store(rx.parse_line(json.dumps({"model": "A", "id": 1})))
        rx._store(rx.parse_line(json.dumps({"model": "A", "id": 1})))  # update
        assert hits == [("A", True), ("A", False)]

    def test_build_cmd_has_device_and_json(self):
        cmd = IsmReceiver(device_index=1, frequency="915M").build_cmd()
        assert cmd[0] == "rtl_433"
        assert "-d" in cmd and "1" in cmd
        assert "json" in cmd
        assert "915M" in cmd


class TestNonWeatherPassthrough:
    """Utility meters/TPMS emit fields no weather whitelist anticipates.

    Regression: LandisGyr-GS smart meters rendered a blank READINGS column, which
    was indistinguishable from a failed decode.
    """

    def _rx(self):
        from ravensdr.ism_receiver import IsmReceiver
        return IsmReceiver(device_index=0)

    def test_unknown_fields_are_kept_as_extra(self):
        import json
        line = json.dumps({"model": "LandisGyr-GS", "id": "907b0418",
                           "rssi": -12.1, "snr": 11.8,
                           "packet_type": 2, "uptime": 41234})
        rec = self._rx().parse_line(line)
        assert rec["extra"]["packet_type"] == 2
        assert rec["extra"]["uptime"] == 41234

    def test_raw_frame_is_preserved_entirely(self):
        import json
        payload = {"model": "LandisGyr-GS", "id": "x", "mod": "FSK",
                   "freq": 915.2, "len": 92}
        rec = self._rx().parse_line(json.dumps(payload))
        assert rec["raw"] == payload

    def test_columns_are_not_duplicated_into_extra(self):
        import json
        rec = self._rx().parse_line(json.dumps(
            {"model": "M", "id": "1", "rssi": -3.0, "snr": 9.0, "time": "t"}))
        assert "extra" not in rec or not (
            {"model", "id", "rssi", "snr", "time"} & set(rec["extra"]))

    def test_weather_readings_still_normalised(self):
        import json
        rec = self._rx().parse_line(json.dumps(
            {"model": "Oregon-v1", "id": 15, "temperature_C": 18.8}))
        assert rec["temperature_C"] == 18.8


class TestHeardOnFrequency:
    """Which frequency a device was decoded at.

    The panel keeps history across retunes on purpose, so without this a
    315 MHz TPMS sits in the same table as a 912 MHz meter with nothing to
    distinguish them — stale TPMS rows read as though they had been heard on
    the ERT band.
    """

    def test_ook_reports_a_single_freq(self):
        assert _heard_on({"freq": 433.905}) == 433.905

    def test_fsk_uses_the_midpoint_of_the_tone_pair(self):
        # FSK reports the two tones; the carrier sits between them.
        assert _heard_on({"freq1": 912.489, "freq2": 912.497}) == 912.493

    def test_falls_back_to_freq1_alone(self):
        assert _heard_on({"freq1": 315.001}) == 315.001

    def test_absent_when_the_decoder_reports_neither(self):
        assert _heard_on({"model": "Acurite-Tower"}) is None

    def test_parse_line_carries_it_onto_the_record(self):
        rec = _rx().parse_line(json.dumps({
            "model": "LandisGyr-GS", "id": "907b1f6a",
            "freq1": 912.489, "freq2": 912.497}))
        assert rec["freq_mhz"] == 912.493


class TestClearRecords:
    def test_clear_drops_everything_and_reports_the_count(self):
        rx = _rx()
        for i in range(3):
            rec = rx.parse_line(json.dumps(
                {"model": "Acurite-Tower", "id": i, "temperature_C": 20}))
            rx._store(rec)
        assert len(rx.get_records()) == 3
        assert rx.clear_records() == 3
        assert rx.get_records() == []

    def test_clear_on_an_empty_store_is_harmless(self):
        assert _rx().clear_records() == 0
