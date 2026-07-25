"""Unit tests for the rtl_433 ISM receiver parsing + record table."""

import json
import time

from ravensdr.ism_receiver import IsmReceiver


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
