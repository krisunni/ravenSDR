"""Unit tests for the POCSAG/FLEX pager receiver (multimon-ng parsing)."""

from ravensdr.pager_receiver import PagerReceiver


def _rx():
    return PagerReceiver(device_index=0)


class TestParseLine:
    def test_pocsag_alpha(self):
        line = "POCSAG1200: Address:  123456  Function: 3  Alpha:   HELLO WORLD"
        rec = _rx().parse_line(line)
        assert rec["protocol"] == "POCSAG1200"
        assert rec["address"] == "123456"
        assert rec["function"] == "3"
        assert rec["content_type"] == "Alpha"
        assert rec["text"] == "HELLO WORLD"

    def test_pocsag_numeric(self):
        line = "POCSAG512: Address: 7654321  Function: 0  Numeric: 12345"
        rec = _rx().parse_line(line)
        assert rec["protocol"] == "POCSAG512"
        assert rec["address"] == "7654321"
        assert rec["text"] == "12345"

    def test_pocsag_empty_message(self):
        line = "POCSAG1200: Address:  111111  Function: 0"
        rec = _rx().parse_line(line)
        assert rec["address"] == "111111"
        assert rec["text"] == ""

    def test_flex_capcode(self):
        line = "FLEX: 2026-07-24 15:00:00 1600/2/K/A 12.345 [001234567] ALN Test flex message"
        rec = _rx().parse_line(line)
        assert rec["protocol"] == "FLEX"
        assert rec["address"] == "1234567"   # leading zeros stripped
        assert "flex message" in rec["text"].lower()

    def test_non_pager_line_ignored(self):
        assert _rx().parse_line("multimon-ng 1.3.1") is None
        assert _rx().parse_line("Enabled demodulators: POCSAG1200") is None

    def test_record_key_protocol_and_address(self):
        rx = _rx()
        rec = rx.parse_line("POCSAG1200: Address: 42  Function: 1  Alpha: hi")
        assert rx.record_key(rec) == "POCSAG1200/42"


class TestPipeCommands:
    def test_source_is_rtl_fm_at_22050(self):
        src = PagerReceiver(device_index=1, frequency="152.007M").build_source_cmd()
        assert src[0] == "rtl_fm"
        assert "152.007M" in src
        assert "22050" in src
        assert "1" in src  # device index

    def test_main_is_multimon_raw(self):
        cmd = _rx().build_cmd()
        assert cmd[0] == "multimon-ng"
        assert "raw" in cmd
        assert "POCSAG1200" in cmd
        assert "FLEX" in cmd

    def test_records_accumulate_per_address(self):
        rx = _rx()
        rx._store(rx.parse_line("POCSAG1200: Address: 5  Function: 0  Alpha: first"))
        rx._store(rx.parse_line("POCSAG1200: Address: 5  Function: 0  Alpha: second"))
        pages = rx.get_pages()
        assert len(pages) == 1
        assert pages[0]["text"] == "second"
