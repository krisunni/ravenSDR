"""Integration tests for ADS-B receiver with mocked dump1090."""

import json
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from unittest.mock import patch, MagicMock

import pytest
from ravensdr.adsb_receiver import AdsbReceiver


SAMPLE_AIRCRAFT = {
    "now": 1700000000,
    "messages": 100,
    "aircraft": [
        {
            "hex": "a1b2c3",
            "flight": "ASA412  ",
            "lat": 47.45,
            "lon": -122.31,
            "altitude": 3000,
            "track": 180,
            "speed": 150,
        },
        {
            "hex": "d4e5f6",
            "flight": "DAL89   ",
            "lat": 47.50,
            "lon": -122.25,
            "altitude": 5000,
            "track": 270,
            "speed": 200,
        },
    ],
}


class MockDump1090Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/data/aircraft.json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(SAMPLE_AIRCRAFT).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress logs


@pytest.fixture
def mock_server():
    """Start a mock dump1090 HTTP server."""
    server = HTTPServer(("127.0.0.1", 8080), MockDump1090Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


class TestAdsbReceiver:

    @patch("ravensdr.adsb_receiver.subprocess.Popen")
    def test_start_stop(self, mock_popen):
        """Receiver starts and stops without errors."""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        receiver = AdsbReceiver(device_index=0)
        receiver.start()
        assert receiver.is_running

        receiver.stop()
        assert not receiver.is_running
        mock_proc.terminate.assert_called_once()

    @patch("ravensdr.adsb_receiver.subprocess.Popen")
    def test_poll_flights(self, mock_popen, mock_server):
        """Poller retrieves flight data from mock server."""
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        receiver = AdsbReceiver(device_index=0)
        # Manually set running and start poll thread
        receiver._running = True
        receiver._poll_thread = threading.Thread(
            target=receiver._poll_loop, daemon=True
        )
        receiver._poll_thread.start()

        # Wait for at least one poll
        time.sleep(3)

        flights = receiver.get_flights()
        assert len(flights) == 2
        assert flights[0]["flight"].strip() == "ASA412"

        receiver._running = False
        receiver._poll_thread.join(timeout=5)

    @patch("ravensdr.adsb_receiver.subprocess.Popen")
    def test_get_flights_returns_copy(self, mock_popen):
        """get_flights returns a copy, not a reference."""
        receiver = AdsbReceiver()
        receiver.flights = [{"flight": "TEST"}]
        flights = receiver.get_flights()
        flights.append({"flight": "EXTRA"})
        assert len(receiver.flights) == 1

    @patch("ravensdr.adsb_receiver.subprocess.Popen")
    def test_stop_without_start(self, mock_popen):
        """Stopping a receiver that was never started should not error."""
        receiver = AdsbReceiver()
        receiver.stop()  # Should not raise

    @patch("ravensdr.adsb_receiver.subprocess.Popen", side_effect=FileNotFoundError)
    def test_start_missing_binary(self, mock_popen):
        """Starting with missing dump1090 binary should log error and not crash."""
        receiver = AdsbReceiver()
        receiver.start()
        assert not receiver.is_running
