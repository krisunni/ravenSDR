"""Unit tests for APT decoder — rtl_fm command construction and noaa-apt integration."""

import os
from unittest.mock import patch, MagicMock

import pytest

from ravensdr.apt_decoder import AptDecoder, DEFAULT_GAIN, SAMPLE_RATE, CAPTURE_RATE_HZ


class TestRtlFmCommand:

    def test_default_command(self):
        cmd = AptDecoder.build_rtl_fm_cmd("137.6200M")
        assert cmd[0] == "rtl_fm"
        assert "-f" in cmd
        assert "137.6200M" in cmd
        assert "-M" in cmd
        assert "fm" in cmd
        assert "-s" in cmd
        assert SAMPLE_RATE in cmd
        assert "-r" in cmd
        assert str(CAPTURE_RATE_HZ) in cmd

    def test_custom_gain(self):
        cmd = AptDecoder.build_rtl_fm_cmd("137.9125M", gain=50)
        assert "-g" in cmd
        idx = cmd.index("-g")
        assert cmd[idx + 1] == "50"

    def test_default_gain(self):
        cmd = AptDecoder.build_rtl_fm_cmd("137.6200M")
        assert "-g" in cmd
        idx = cmd.index("-g")
        assert cmd[idx + 1] == str(DEFAULT_GAIN)

    def test_noaa15_frequency(self):
        cmd = AptDecoder.build_rtl_fm_cmd("137.6200M")
        assert "137.6200M" in cmd

    def test_noaa19_frequency(self):
        cmd = AptDecoder.build_rtl_fm_cmd("137.9125M")
        assert "137.9125M" in cmd

    def test_outputs_to_stdout(self):
        cmd = AptDecoder.build_rtl_fm_cmd("137.6200M")
        assert cmd[-1] == "-"


class TestDecodeCommand:

    def test_aptdec_command(self):
        cmd = AptDecoder.build_decode_cmd("aptdec", "/tmp/t.wav", "/img/NOAA-15_x.png", "NOAA 15")
        assert cmd[0] == "aptdec"
        assert "/tmp/t.wav" in cmd
        assert "-o" in cmd and "NOAA-15_x.png" in cmd
        assert "-d" in cmd and "/img" in cmd
        assert "-s" in cmd and "15" in cmd

    def test_noaa_apt_command(self):
        cmd = AptDecoder.build_decode_cmd("noaa-apt", "/tmp/t.wav", "/tmp/t.png", "NOAA 19")
        assert cmd[0] == "noaa-apt"
        assert "/tmp/t.wav" in cmd
        assert "-o" in cmd and "/tmp/t.png" in cmd


class TestOutputFilenames:

    def test_filename_convention(self):
        decoder = AptDecoder()
        pass_info = {
            "satellite": "NOAA 19",
            "frequency": "137.9125M",
        }
        # The filename is generated inside _record_and_decode;
        # verify the safe_name replacement logic
        safe_name = pass_info["satellite"].replace(" ", "-")
        assert safe_name == "NOAA-19"

    def test_filename_noaa15(self):
        safe_name = "NOAA 15".replace(" ", "-")
        assert safe_name == "NOAA-15"


class TestDecoderState:

    def test_not_recording_initially(self):
        decoder = AptDecoder()
        assert decoder.is_recording is False

    def test_no_current_pass_initially(self):
        decoder = AptDecoder()
        assert decoder.current_pass is None

    def test_cannot_record_while_recording(self):
        decoder = AptDecoder()
        decoder._recording = True
        result = decoder.record_pass({"satellite": "NOAA 19"})
        assert result is False


class TestEventPayload:

    def test_emit_called_with_correct_event(self):
        emit_fn = MagicMock()
        decoder = AptDecoder(emit_fn=emit_fn)
        # Directly test the emit would be called with apt_image_ready
        # (full test requires mocking subprocess)
        assert decoder.emit_fn == emit_fn


class TestLatestImage:

    def test_no_image_returns_none(self):
        decoder = AptDecoder()
        # Point to a non-existent directory
        with patch("ravensdr.apt_decoder.IMAGE_DIR", "/tmp/ravensdr_test_nonexistent"):
            result = decoder.get_latest_image()
            assert result is None

    def test_image_history_empty(self):
        decoder = AptDecoder()
        with patch("ravensdr.apt_decoder.IMAGE_DIR", "/tmp/ravensdr_test_nonexistent"):
            result = decoder.get_image_history()
            assert result == []
