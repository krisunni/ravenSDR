"""Unit tests for the POCSAG/FLEX pager receiver (multimon-ng parsing)."""

from ravensdr.pager_receiver import PagerReceiver, numeric_quality


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


class TestNumericQuality:
    """POCSAG numeric mode is BCD through multimon-ng's "084 2.6]195-3U7[".

    Real numeric pages are callback numbers. Payloads littered with the
    unassigned codes "[" and "]" are alphanumeric or binary content forced
    through the numeric table, or noise that survived BCH correction — both
    reach the operator as line noise and must be labelled, not presented as
    a message.
    """

    def test_observed_noise_is_flagged(self):
        # Both captured off 152.0075 MHz on 2026-07-28.
        assert numeric_quality(
            "357]3371U89]040-0]1U87 025U80.561U8050509994U6114] 7.4U91462"
        ) == "low"
        assert numeric_quality(
            "1 43914-2051U-569[9U ]]17]77.2325383405U04135-]040.4] 7]1]87-3"
        ) == "low"

    def test_callback_numbers_pass(self):
        for text in ["5551234", "206-555-0142", "911", "1-800-555-0100",
                     "0000000000", "425 555 0199"]:
            assert numeric_quality(text) == "ok", text

    def test_single_urgency_marker_is_legitimate(self):
        # A trailing "U" is a normal urgency flag, not a decode failure.
        assert numeric_quality("4255550199U") == "ok"
        assert numeric_quality("18005550100 U") == "ok"

    def test_repeated_urgency_is_flagged(self):
        assert numeric_quality("1U2U3U4U5U6U") == "low"

    def test_digit_fraction_alone_would_not_catch_it(self):
        # The observed noise is ~75% digits; guard against a future rewrite
        # regressing to a digit-count heuristic.
        noise = "357]3371U89]040-0]1U87 025U80.561U8050509994U6114] 7.4U91462"
        body = [c for c in noise if not c.isspace()]
        digit_fraction = sum(c.isdigit() for c in body) / len(body)
        assert digit_fraction > 0.7
        assert numeric_quality(noise) == "low"

    def test_short_payloads_are_not_judged(self):
        assert numeric_quality("]") == "ok"
        assert numeric_quality("12") == "ok"


class TestParseLineQuality:
    def test_numeric_noise_gets_quality_low(self):
        rx = PagerReceiver()
        rec = rx.parse_line(
            "POCSAG1200: Address: 1310050  Function: 0  "
            "Numeric: 357]3371U89]040-0]1U87 025U80.561U8050509994U6114]")
        assert rec["quality"] == "low"
        assert rec["address"] == "1310050"
        # The payload is retained — an undecodable page is still evidence.
        assert "357]" in rec["text"]

    def test_alpha_pages_are_never_flagged(self):
        rx = PagerReceiver()
        rec = rx.parse_line(
            "POCSAG1200: Address: 1234567  Function: 3  Alpha:   CALL DISPATCH")
        assert rec["quality"] == "ok"
        assert rec["text"] == "CALL DISPATCH"
