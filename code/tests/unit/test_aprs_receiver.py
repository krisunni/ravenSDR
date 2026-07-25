"""Unit tests for the APRS (AFSK1200 / TNC2) receiver."""

from ravensdr.aprs_receiver import AprsReceiver, parse_latlon, parse_weather


def _rx():
    return AprsReceiver(device_index=0)


class TestLatLon:
    def test_north_west_seattle_area(self):
        # 4738.00N -> 47 deg 38.00 min
        assert parse_latlon("4738.00", "N", False) == 47.63333
        assert parse_latlon("12210.00", "W", True) == -122.16667

    def test_southern_and_eastern_hemispheres_are_signed(self):
        assert parse_latlon("3352.00", "S", False) < 0
        assert parse_latlon("15112.00", "E", True) > 0

    def test_longitude_uses_three_degree_digits(self):
        """DDDMM.mm for longitude vs DDMM.mm for latitude."""
        assert parse_latlon("00130.00", "E", True) == 1.5


class TestTnc2Parsing:
    def test_position_packet(self):
        line = "KI7ABC-9>APU25N,WIDE1-1,WIDE2-1:!4738.00N/12210.00W>Testing"
        rec = _rx().parse_line(line)
        assert rec["source"] == "KI7ABC-9"
        assert rec["dest"] == "APU25N"
        assert rec["path"] == ["WIDE1-1", "WIDE2-1"]
        assert rec["lat"] == 47.63333
        assert rec["lon"] == -122.16667
        assert rec["type"] == "position"
        assert rec["comment"] == "Testing"

    def test_multimon_prefix_is_tolerated(self):
        line = "APRS: N0CALL>APRS:!4738.00N/12210.00W-"
        rec = _rx().parse_line(line)
        assert rec["source"] == "N0CALL"

    def test_packet_with_no_digipath(self):
        rec = _rx().parse_line("W7ABC>APRS:!4738.00N/12210.00W-")
        assert rec["path"] == []

    def test_symbol_is_captured(self):
        rec = _rx().parse_line("W7ABC>APRS:!4738.00N/12210.00W-")
        assert rec["symbol"] == "/-"

    def test_status_packet(self):
        rec = _rx().parse_line("W7XYZ>APRS:>Net control tonight")
        assert rec["type"] == "status"
        assert rec["status"] == "Net control tonight"

    def test_addressed_message(self):
        rec = _rx().parse_line("W7AAA>APRS::W7BBB    :meet on 146.96")
        assert rec["type"] == "message"
        assert rec["addressee"] == "W7BBB"
        assert rec["message"] == "meet on 146.96"

    def test_raw_line_always_preserved(self):
        line = "W7ABC>APRS:`quirky mic-e payload"
        rec = _rx().parse_line(line)
        assert rec["raw"] == line
        assert rec["type"] == "mic-e"

    def test_non_aprs_line_ignored(self):
        assert _rx().parse_line("multimon-ng 1.3.1 starting") is None

    def test_empty_line_ignored(self):
        assert _rx().parse_line("") is None

    def test_position_without_valid_coords_still_records_station(self):
        rec = _rx().parse_line("W7ABC>APRS:!not a position")
        assert rec["source"] == "W7ABC"
        assert "lat" not in rec


class TestWeather:
    def test_temperature_and_humidity(self):
        wx = parse_weather("!4738.00N/12210.00W_c180s005g010t072h45b10132")
        assert wx["temperature_F"] == 72
        assert wx["humidity_pct"] == 45
        assert wx["gust_mph"] == 10

    def test_pressure_converted_to_hpa(self):
        """APRS sends tenths of a millibar."""
        wx = parse_weather("t072b10132")
        assert wx["pressure_hPa"] == 1013.2

    def test_negative_temperature(self):
        assert parse_weather("t-05")["temperature_F"] == -5

    def test_no_weather_fields(self):
        assert parse_weather("just a comment") == {}

    def test_weather_attached_to_record(self):
        rec = _rx().parse_line("W7WX>APRS:!4738.00N/12210.00W_t072h45")
        assert rec["weather"]["temperature_F"] == 72


class TestPipeline:
    def test_station_is_the_record_key(self):
        rx = _rx()
        rec = rx.parse_line("W7ABC-1>APRS:!4738.00N/12210.00W-")
        assert rx.record_key(rec) == "W7ABC-1"

    def test_source_command_targets_the_tuned_frequency(self):
        rx = AprsReceiver(device_index=1, frequency="144.390M")
        cmd = rx.build_source_cmd()
        assert "144.390M" in cmd
        assert cmd[cmd.index("-d") + 1] == "1"

    def test_decoder_uses_afsk1200_tnc2_mode(self):
        cmd = _rx().build_cmd()
        assert "AFSK1200" in cmd
        assert "-A" in cmd          # TNC2 = one line per packet
