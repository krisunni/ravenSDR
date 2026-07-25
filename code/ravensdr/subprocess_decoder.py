# Base class for stdout/line-oriented subprocess RF decoders.
#
# Factors out the lifecycle shared by dongle-seizing decoders (rtl_433, acarsdec,
# multimon-ng, ...): launch a subprocess, read its stdout line by line in a
# thread, parse each line into a record, keep a TTL-expiring dict of records.
# Modelled on ais_receiver.py / adsb_receiver.py. The caller must release the
# RTL-SDR (input_source.stop()) before start(), since these decoders open the
# device directly.

import logging
import os
import time

# Use REAL stdlib modules, not eventlet's green versions, for blocking stdout reads.
try:
    from eventlet.patcher import original
    subprocess = original("subprocess")
    threading = original("threading")
except ImportError:
    import subprocess
    import threading

log = logging.getLogger(__name__)


class SubprocessDecoder:
    """Manage a line-emitting decoder subprocess and a TTL record table.

    Subclasses set PROC_NAME and implement build_cmd(), parse_line(), record_key().
    """

    PROC_NAME = None      # basename for the killall guard, e.g. "rtl_433"
    DEFAULT_TTL = 600     # seconds a record survives without an update

    def __init__(self, device_index=0, ttl_sec=None):
        self.device_index = device_index
        self.ttl_sec = ttl_sec if ttl_sec is not None else self.DEFAULT_TTL
        self.process = None
        self._src_process = None   # optional upstream pipe (e.g. rtl_fm)
        self._records = {}   # key -> record dict (always carries "seen" epoch)
        self._reader_thread = None
        self._running = False
        self._lock = threading.Lock()
        self.last_error = None   # human-readable reason the last start() failed

    @property
    def is_running(self):
        return self._running

    # ── Subclass hooks ──
    def build_cmd(self):
        """Return the argv list for the decoder subprocess."""
        raise NotImplementedError

    def build_source_cmd(self):
        """Optional upstream process whose stdout pipes into build_cmd()'s stdin.

        Return None (default) for direct-device decoders. Override to return an
        argv list (e.g. rtl_fm) for pipe decoders like `rtl_fm | multimon-ng`.
        """
        return None

    def parse_line(self, line):
        """Parse one stdout line into a record dict, or return None to skip."""
        raise NotImplementedError

    def record_key(self, record):
        """Return a stable dict key identifying the record's emitter/device."""
        raise NotImplementedError

    def on_record(self, record, is_new):
        """Optional hook after a record is stored (for emits/correlation)."""

    # ── Lifecycle ──
    def start(self):
        if self._running:
            return
        self.last_error = None
        self._kill_lingering()
        src_cmd = self.build_source_cmd()
        cmd = self.build_cmd()
        try:
            if src_cmd:
                # source | main : source stdout feeds main stdin.
                # Keep the source's stderr so a failure to open the dongle
                # ("usb_claim_interface error -6") is reportable — otherwise the
                # only symptom is the *downstream* decoder exiting 0 on EOF,
                # which looks like a missing/broken decoder binary.
                self._src_process = subprocess.Popen(
                    src_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.process = subprocess.Popen(
                    cmd, stdin=self._src_process.stdout,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, bufsize=1)
                # Let the source receive SIGPIPE when main exits.
                self._src_process.stdout.close()
            else:
                self.process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, bufsize=1)
        except FileNotFoundError as e:
            self.last_error = f"{e.filename} not found — is it installed?"
            log.error("decoder binary not found (%s) — is it installed?", e.filename)
            self._terminate_procs()
            return

        time.sleep(1)
        # Check the SOURCE first. When it dies (usually because something else
        # still holds the RTL-SDR), the decoder downstream just sees EOF on stdin
        # and exits 0 — so blaming the decoder here is actively misleading.
        if self._src_process is not None and self._src_process.poll() is not None:
            detail = self._read_source_stderr()
            if "usb_claim_interface" in detail or "device busy" in detail.lower():
                reason = (f"{src_cmd[0]} could not open the RTL-SDR — the device is "
                          f"in use by another decoder or a leftover process")
            else:
                reason = (f"{src_cmd[0]} exited immediately "
                          f"(code {self._src_process.returncode})")
            self.last_error = reason + (f": {detail}" if detail else "")
            log.error("%s pipeline failed — %s", cmd[0], self.last_error)
            self._terminate_procs()
            return

        if self.process.poll() is not None:
            self.last_error = (f"{cmd[0]} exited immediately "
                               f"(code {self.process.returncode})")
            log.error("%s exited immediately (code %s)", cmd[0], self.process.returncode)
            self._terminate_procs()
            return

        self._running = True
        self._reader_thread = threading.Thread(target=self._reader, daemon=True)
        self._reader_thread.start()
        log.info("%s started on device %d", cmd[0], self.device_index)

    def stop(self):
        self._running = False
        self._terminate_procs()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=3)
            self._reader_thread = None
        log.info("%s stopped", self.PROC_NAME or "decoder")

    def _read_source_stderr(self, max_chars=300):
        """Return the source process's stderr tail. Only safe once it has exited."""
        proc = self._src_process
        if proc is None or proc.stderr is None:
            return ""
        try:
            raw = proc.stderr.read() or b""
        except Exception:
            return ""
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        # Keep the last, most specific lines (the error, not the banner).
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return " | ".join(lines[-3:])[:max_chars]

    def _terminate_procs(self):
        """Terminate the main process and any upstream source pipe."""
        for proc in (self.process, self._src_process):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        self.process = None
        self._src_process = None

    def _kill_lingering(self):
        if not self.PROC_NAME:
            return
        try:
            subprocess.run(["killall", "-q", self.PROC_NAME],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.5)
        except Exception:
            pass

    def _reader(self):
        """Read decoder stdout line by line and store parsed records."""
        try:
            for line in self.process.stdout:
                if not self._running:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    record = self.parse_line(line)
                except Exception as e:
                    log.debug("%s parse error: %s — line: %.100s",
                              self.PROC_NAME, e, line)
                    continue
                if not record:
                    continue
                self._store(record)
        except Exception as e:
            if self._running:
                log.debug("%s reader stopped: %s", self.PROC_NAME, e)

    def _store(self, record):
        key = self.record_key(record)
        if key is None:
            return
        record["seen"] = time.time()
        with self._lock:
            is_new = key not in self._records
            existing = self._records.get(key, {})
            existing.update(record)
            self._records[key] = existing
            stored = existing
        self.on_record(stored, is_new)

    def _expire(self):
        cutoff = time.time() - self.ttl_sec
        with self._lock:
            for k in [k for k, v in self._records.items() if v.get("seen", 0) < cutoff]:
                del self._records[k]

    def get_records(self):
        """Return current (non-stale) records as a list, newest activity first."""
        self._expire()
        with self._lock:
            return sorted(self._records.values(),
                          key=lambda r: r.get("seen", 0), reverse=True)
