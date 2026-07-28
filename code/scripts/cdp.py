"""CDP driver that never waits on a navigation-complete signal.

On this Pi (Chromium 150, 16 KB-page kernel) the HTTP request is issued and
answered — the access log proves it — but Page.navigate's response and
--dump-dom's completion never arrive. Everything else about the browser works.

So: open the target straight at the URL, then poll document.readyState over
Runtime.evaluate until the document is there. That sidesteps the one broken
signal and leaves the rest of CDP usable.
"""
import base64
import json
import os
import socket
import struct
import time
import urllib.parse
import urllib.request

PORT = int(os.environ.get("CDP_PORT", "9444"))


def _ws(ws_url):
    _, _, rest = ws_url.partition("://")
    hostport, _, path = rest.partition("/")
    host, _, port = hostport.partition(":")
    s = socket.create_connection((host, int(port or 80)), timeout=60)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(("GET /%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n"
               % (path, hostport, key)).encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    assert b"101" in buf.split(b"\r\n")[0], buf[:150]
    return s, buf.split(b"\r\n\r\n", 1)[1]


class Session:
    def __init__(self, url, port=PORT, settle=6.0):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/json/new?%s"
            % (port, urllib.parse.quote(url, safe="")), method="PUT")
        self.target = json.load(urllib.request.urlopen(req))
        self.sock, initial = _ws(self.target["webSocketDebuggerUrl"])
        self.buf = initial
        self.id = 0
        self.events = []
        self.call("Runtime.enable")
        self.call("Log.enable")
        self.wait_ready(settle)

    # ── websocket plumbing ──
    def _need(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("browser closed the connection")
            self.buf += chunk

    def _frame(self):
        while True:
            self._need(2)
            opcode = self.buf[0] & 0x0F
            ln = self.buf[1] & 0x7F
            off = 2
            if ln == 126:
                self._need(4)
                ln = struct.unpack("!H", self.buf[2:4])[0]
                off = 4
            elif ln == 127:
                self._need(10)
                ln = struct.unpack("!Q", self.buf[2:10])[0]
                off = 10
            self._need(off + ln)
            payload = self.buf[off:off + ln]
            self.buf = self.buf[off + ln:]
            if opcode == 0x1:
                return json.loads(payload)

    def _send(self, obj):
        payload = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            hdr = struct.pack("!BB", 0x81, 0x80 | n)
        elif n < 65536:
            hdr = struct.pack("!BBH", 0x81, 0x80 | 126, n)
        else:
            hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
        self.sock.sendall(
            hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def call(self, method, **params):
        self.id += 1
        self._send({"id": self.id, "method": method, "params": params})
        deadline = time.time() + 60
        while time.time() < deadline:
            msg = self._frame()
            if msg.get("id") == self.id:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})
            self.events.append(msg)
        raise TimeoutError(method)

    # ── page control ──
    def js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr,
                      returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            desc = r["exceptionDetails"].get("exception", {}).get("description", "")
            raise RuntimeError(desc or json.dumps(r["exceptionDetails"])[:300])
        return r["result"].get("value")

    def wait_ready(self, settle=6.0, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.js("document.readyState") in ("interactive", "complete") \
                        and self.js("!!document.querySelector('body *')"):
                    break
            except Exception:
                pass
            time.sleep(0.5)
        time.sleep(settle)          # let XHR-driven panels populate

    def viewport(self, w, h, mobile=False, scale=1):
        self.call("Emulation.setDeviceMetricsOverride", width=w, height=h,
                  deviceScaleFactor=scale, mobile=mobile,
                  screenWidth=w, screenHeight=h)
        if mobile:
            self.call("Emulation.setTouchEmulationEnabled", enabled=True,
                      maxTouchPoints=5)
        time.sleep(1.2)

    def shot(self, path, full=False):
        args = {"format": "png"}
        if full:
            args["captureBeyondViewport"] = True
        r = self.call("Page.captureScreenshot", **args)
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        return path

    def console(self):
        out = []
        for e in self.events:
            if e.get("method") == "Log.entryAdded":
                en = e["params"]["entry"]
                if en.get("level") in ("error", "warning"):
                    out.append("%s: %s" % (en["level"], en.get("text", "")[:180]))
            elif e.get("method") == "Runtime.exceptionThrown":
                d = e["params"]["exceptionDetails"]
                out.append("exception: %s" % (
                    d.get("exception", {}).get("description")
                    or d.get("text", ""))[:180])
        return out

    def close(self):
        try:
            urllib.request.urlopen(
                "http://127.0.0.1:%d/json/close/%s" % (PORT, self.target["id"]))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
