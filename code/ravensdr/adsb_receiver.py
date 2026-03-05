# ADS-B receiver — dump1090 process manager + SBS BaseStation TCP reader

import logging
import os
import socket
import threading
import time

try:
    from eventlet.patcher import original
    subprocess = original("subprocess")
except ImportError:
    import subprocess

try:
    import eventlet
    _HAS_EVENTLET = True
except ImportError:
    _HAS_EVENTLET = False

log = logging.getLogger(__name__)

SBS_HOST = "localhost"
SBS_PORT = 30003  # dump1090 SBS BaseStation output

# Config from environment
ADSB_ENABLED = os.environ.get("ADSB_ENABLED", "true").lower() == "true"
ADSB_DUAL_DONGLE = os.environ.get("ADSB_DUAL_DONGLE", "false").lower() == "true"
ADSB_SCAN_INTERVAL = int(os.environ.get("ADSB_SCAN_INTERVAL", "60"))
ADSB_SCAN_DURATION = int(os.environ.get("ADSB_SCAN_DURATION", "30"))

# How long before an aircraft is considered stale (seconds)
AIRCRAFT_TTL = 600  # 10 minutes


class AdsbReceiver:
    """Manages dump1090 process and reads SBS TCP stream."""

    def __init__(self, device_index=0, dual_dongle=False):
        self.device_index = device_index
        self.dual_dongle = dual_dongle
        self.process = None
        self._aircraft = {}  # hex -> aircraft dict
        self._poll_thread = None
        self._running = False

    @property
    def is_running(self):
        return self._running

    def start(self):
        """Start dump1090 subprocess on configured device index."""
        if self._running:
            return

        # Kill any lingering dump1090 and allow USB device to be released
        try:
            subprocess.run(["killall", "-q", "dump1090-mutability"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        time.sleep(2)

        cmd = [
            "dump1090-mutability",
            "--device-index", str(self.device_index),
            "--net",
            "--quiet",
        ]
        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            log.error("dump1090-mutability not found — is it installed?")
            return

        # Wait for dump1090 to start and check it didn't crash
        time.sleep(3)
        if self.process is None or self.process.poll() is not None:
            rc = self.process.returncode if self.process else "?"
            log.error("dump1090 exited immediately (code %s)", rc)
            self.process = None
            return

        self._running = True
        self._poll_thread = threading.Thread(target=self._sbs_reader, daemon=True)
        self._poll_thread.start()
        log.info("dump1090 started on device %d", self.device_index)

    def stop(self):
        """Stop dump1090 and SBS reader."""
        self._running = False
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process = None
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=3)
            self._poll_thread = None
        log.info("dump1090 stopped")

    def _sbs_reader(self):
        """Connect to dump1090 SBS port and parse BaseStation messages."""
        while self._running:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect((SBS_HOST, SBS_PORT))
                log.info("Connected to dump1090 SBS stream on port %d", SBS_PORT)
                buf = ""
                while self._running:
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        self._expire_stale()
                        continue
                    if not data:
                        break  # connection closed
                    buf += data.decode("ascii", errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if line:
                            self._parse_sbs(line)
                    self._expire_stale()
            except (ConnectionRefusedError, OSError) as e:
                if self._running:
                    log.debug("SBS connect failed: %s — retrying in 2s", e)
                    time.sleep(2)
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

    def _parse_sbs(self, line):
        """Parse a single SBS BaseStation format line.

        Format: MSG,type,session,aircraft,hex,flight,date,time,date,time,
                callsign,altitude,speed,track,lat,lon,vertrate,squawk,...
        """
        fields = line.split(",")
        if len(fields) < 11 or fields[0] != "MSG":
            return

        hex_id = fields[4].strip().upper()
        if not hex_id:
            return

        ac = self._aircraft.get(hex_id, {"hex": hex_id})
        ac["seen"] = time.time()

        callsign = fields[10].strip()
        if callsign:
            ac["flight"] = callsign

        # MSG types 1-8 have different field positions
        try:
            if fields[11].strip():
                ac["altitude"] = int(fields[11].strip())
        except (IndexError, ValueError):
            pass
        try:
            if fields[12].strip():
                ac["speed"] = float(fields[12].strip())
        except (IndexError, ValueError):
            pass
        try:
            if fields[13].strip():
                ac["track"] = float(fields[13].strip())
        except (IndexError, ValueError):
            pass
        try:
            lat = fields[14].strip()
            lon = fields[15].strip()
            if lat and lon:
                ac["lat"] = float(lat)
                ac["lon"] = float(lon)
        except (IndexError, ValueError):
            pass
        try:
            if fields[16].strip():
                ac["vert_rate"] = int(fields[16].strip())
        except (IndexError, ValueError):
            pass
        try:
            if fields[17].strip():
                ac["squawk"] = fields[17].strip()
        except (IndexError, ValueError):
            pass

        self._aircraft[hex_id] = ac

    def _expire_stale(self):
        """Remove aircraft not seen for AIRCRAFT_TTL seconds."""
        now = time.time()
        stale = [k for k, v in self._aircraft.items()
                 if now - v.get("seen", 0) > AIRCRAFT_TTL]
        for k in stale:
            del self._aircraft[k]

    def get_flights(self):
        """Return current aircraft list."""
        return list(self._aircraft.values())


class AdsbScanScheduler:
    """Single-dongle time-sharing: pauses rtl_fm for ADS-B scan windows."""

    def __init__(self, receiver, input_source,
                 scan_interval=ADSB_SCAN_INTERVAL,
                 scan_duration=ADSB_SCAN_DURATION):
        self.receiver = receiver
        self.input_source = input_source
        self.scan_interval = scan_interval
        self.scan_duration = scan_duration
        self._running = False
        self._thread = None
        self._scanning = False
        self._status_callback = None

    @property
    def is_scanning(self):
        return self._scanning

    def set_status_callback(self, callback):
        """Set callback(scanning: bool) for UI status updates."""
        self._status_callback = callback

    def _sleep(self, seconds):
        """Sleep using eventlet if available, else time.sleep."""
        if _HAS_EVENTLET:
            eventlet.sleep(seconds)
        else:
            time.sleep(seconds)

    def start(self):
        if self._running:
            return
        self._running = True
        if _HAS_EVENTLET:
            self._thread = eventlet.spawn(self._scan_loop)
        else:
            self._thread = threading.Thread(target=self._scan_loop, daemon=True)
            self._thread.start()
        log.info("ADS-B scan scheduler started (interval=%ds, duration=%ds)",
                 self.scan_interval, self.scan_duration)

    def stop(self):
        self._running = False
        if self._scanning:
            self.receiver.stop()
            self._scanning = False
        if self._thread is not None:
            if _HAS_EVENTLET and hasattr(self._thread, 'wait'):
                try:
                    self._thread.wait()
                except Exception:
                    pass
            elif hasattr(self._thread, 'join'):
                self._thread.join(timeout=5)
            self._thread = None

    def _scan_loop(self):
        while self._running:
            # Wait for scan interval
            for _ in range(self.scan_interval):
                if not self._running:
                    return
                self._sleep(1)

            if not self._running:
                return

            # Only scan when tuned to an aviation preset
            preset = self.input_source.current_preset
            if not preset or preset.get("category") != "aviation":
                continue

            # Pause rtl_fm, start ADS-B scan
            log.info("ADS-B scan window starting — pausing rtl_fm")
            self._scanning = True
            if self._status_callback:
                self._status_callback(True)

            was_running = self.input_source.is_running
            preset = self.input_source.current_preset
            if was_running:
                self.input_source.stop()
                # Restore preset reference so we can resume
                self.input_source.current_preset = preset

            self.receiver.start()

            # Scan for configured duration
            for _ in range(self.scan_duration):
                if not self._running:
                    self.receiver.stop()
                    return
                self._sleep(1)

            # Stop ADS-B, resume rtl_fm
            self.receiver.stop()
            self._scanning = False
            if self._status_callback:
                self._status_callback(False)

            if was_running and preset:
                log.info("ADS-B scan complete — resuming rtl_fm")
                self.input_source.tune(preset)
