"""Unit tests for the UI<->radio NDJSON IPC protocol."""

import json

import pytest

from ravensdr.ipc import (
    CommandRegistry, FrameBuffer, ProtocolError, encode, make_event,
    make_request, make_response,
)


class TestCodec:
    def test_encode_is_one_newline_terminated_line(self):
        raw = encode(make_event("status", {"a": 1}))
        assert raw.endswith(b"\n")
        assert raw.count(b"\n") == 1
        assert json.loads(raw)["name"] == "status"

    def test_request_shape(self):
        msg = make_request(7, "tune", {"preset_id": "noaa-seattle"})
        assert msg == {"t": "req", "id": 7, "cmd": "tune",
                       "args": {"preset_id": "noaa-seattle"}}

    def test_ok_response_carries_data_not_error(self):
        msg = make_response(7, True, data={"state": "LOCKED"})
        assert msg["ok"] is True and msg["data"] == {"state": "LOCKED"}
        assert "error" not in msg

    def test_failed_response_carries_error_not_data(self):
        msg = make_response(7, False, error="device busy")
        assert msg["ok"] is False and msg["error"] == "device busy"
        assert "data" not in msg

    def test_encode_survives_non_serializable_values(self):
        """default=str keeps a stray object from killing the link."""
        raw = encode(make_event("x", {"obj": object()}))
        assert b"object object" in raw


class TestFrameBuffer:
    def test_single_complete_frame(self):
        fb = FrameBuffer()
        msgs = fb.feed(encode(make_event("status", {"n": 1})))
        assert len(msgs) == 1 and msgs[0]["name"] == "status"

    def test_multiple_frames_in_one_chunk(self):
        fb = FrameBuffer()
        chunk = encode(make_event("a")) + encode(make_event("b"))
        assert [m["name"] for m in fb.feed(chunk)] == ["a", "b"]

    def test_partial_frame_yields_nothing_until_newline(self):
        """Stream sockets split writes anywhere — the reader must buffer."""
        fb = FrameBuffer()
        raw = encode(make_event("status", {"n": 1}))
        assert fb.feed(raw[:5]) == []
        assert fb.pending_bytes == 5
        msgs = fb.feed(raw[5:])
        assert len(msgs) == 1 and msgs[0]["name"] == "status"
        assert fb.pending_bytes == 0

    def test_frame_split_across_many_chunks(self):
        fb = FrameBuffer()
        raw = encode(make_request(1, "tune", {"preset_id": "x" * 200}))
        out = []
        for i in range(0, len(raw), 7):
            out.extend(fb.feed(raw[i:i + 7]))
        assert len(out) == 1 and out[0]["cmd"] == "tune"

    def test_blank_lines_ignored(self):
        fb = FrameBuffer()
        assert fb.feed(b"\n\n") == []

    def test_empty_chunk_is_noop(self):
        assert FrameBuffer().feed(b"") == []

    def test_bad_json_raises(self):
        with pytest.raises(ProtocolError, match="bad JSON"):
            FrameBuffer().feed(b"{not json}\n")

    def test_non_object_frame_raises(self):
        with pytest.raises(ProtocolError, match="not an object"):
            FrameBuffer().feed(b"[1,2]\n")

    def test_unknown_kind_raises(self):
        with pytest.raises(ProtocolError, match="unknown message kind"):
            FrameBuffer().feed(b'{"t":"nope"}\n')

    def test_oversized_frame_raises_and_clears(self):
        """A desynced stream must not grow the buffer without bound."""
        fb = FrameBuffer(max_frame_bytes=64)
        with pytest.raises(ProtocolError, match="desynced"):
            fb.feed(b"x" * 65)
        assert fb.pending_bytes == 0


class TestCommandRegistry:
    def test_dispatch_returns_handler_payload(self):
        reg = CommandRegistry()
        reg.register("ping", lambda args: {"pong": args.get("n")})
        res = reg.dispatch(make_request(3, "ping", {"n": 5}))
        assert res == {"t": "res", "id": 3, "ok": True, "data": {"pong": 5}}

    def test_decorator_registration(self):
        reg = CommandRegistry()

        @reg.command("status")
        def _status(args):
            return {"state": "LOCKED"}

        assert reg.names == ["status"]
        assert reg.dispatch(make_request(1, "status"))["data"]["state"] == "LOCKED"

    def test_unknown_command_is_an_error_response(self):
        res = CommandRegistry().dispatch(make_request(1, "nope"))
        assert res["ok"] is False and "unknown command" in res["error"]

    def test_handler_exception_becomes_error_response(self):
        """One bad command must not drop the connection."""
        reg = CommandRegistry()

        @reg.command("boom")
        def _boom(args):
            raise RuntimeError("device busy")

        res = reg.dispatch(make_request(9, "boom"))
        assert res["ok"] is False
        assert res["id"] == 9
        assert "RuntimeError: device busy" in res["error"]

    def test_duplicate_registration_rejected(self):
        reg = CommandRegistry()
        reg.register("x", lambda a: {})
        with pytest.raises(ValueError, match="already registered"):
            reg.register("x", lambda a: {})

    def test_handler_receives_empty_dict_when_args_missing(self):
        reg = CommandRegistry()
        seen = {}
        reg.register("x", lambda args: seen.setdefault("args", args))
        reg.dispatch({"t": "req", "id": 1, "cmd": "x"})
        assert seen["args"] == {}


class TestSocketPathResolution:
    """Both processes must reach the same path by identical reasoning.

    Regression: an XDG_RUNTIME_DIR candidate made the daemon (no XDG under
    systemd) and an interactive shell resolve different sockets, so the client
    reported LINK DOWN against a healthy radio.
    """

    def test_explicit_env_override_wins(self, monkeypatch):
        from ravensdr.ipc import SOCKET_ENV_VAR, resolve_socket_path
        monkeypatch.setenv(SOCKET_ENV_VAR, "/custom/radio.sock")
        assert resolve_socket_path() == "/custom/radio.sock"

    def test_resolution_ignores_xdg_runtime_dir(self, monkeypatch):
        from ravensdr.ipc import SOCKET_ENV_VAR, resolve_socket_path
        monkeypatch.delenv(SOCKET_ENV_VAR, raising=False)
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        with_xdg = resolve_socket_path()
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        without_xdg = resolve_socket_path()
        assert with_xdg == without_xdg
        assert "/run/user" not in with_xdg

    def test_filename_is_honoured(self, monkeypatch):
        from ravensdr.ipc import SOCKET_ENV_VAR, resolve_socket_path
        monkeypatch.delenv(SOCKET_ENV_VAR, raising=False)
        assert resolve_socket_path("audio.sock").endswith("audio.sock")


class TestValidationErrorsAreQuiet:
    def test_value_error_still_returns_error_response(self, caplog):
        """Client input errors report cleanly, without a stack trace."""
        import logging
        reg = CommandRegistry()

        @reg.command("tune")
        def _tune(args):
            raise ValueError("unknown preset: 'nope'")

        with caplog.at_level(logging.WARNING, logger="ravensdr.ipc"):
            res = reg.dispatch(make_request(1, "tune", {"preset_id": "nope"}))

        assert res["ok"] is False
        assert "unknown preset" in res["error"]
        assert any(r.levelno == logging.WARNING and not r.exc_info
                   for r in caplog.records)

    def test_unexpected_error_still_logs_traceback(self, caplog):
        import logging
        reg = CommandRegistry()

        @reg.command("boom")
        def _boom(args):
            raise RuntimeError("hardware exploded")

        with caplog.at_level(logging.ERROR, logger="ravensdr.ipc"):
            res = reg.dispatch(make_request(1, "boom"))

        assert res["ok"] is False
        assert any(r.exc_info for r in caplog.records)
