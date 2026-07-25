# APRS receiver — AX.25/AFSK1200 packet decode via `rtl_fm | multimon-ng -A`.
#
# APRS (144.390 MHz in North America) is the closest voice-band analogue to the
# ISM sensor table: stations beacon position, weather, telemetry and status on a
# schedule, so the channel carries structured data continuously whether or not a
# human is present. That makes it productive where the amateur voice repeaters
# are silent for hours at a time.
#
# multimon-ng's -A flag emits TNC2 monitor format, one packet per line:
#
#   KI7ABC-9>APU25N,WIDE1-1,WIDE2-1:!4738.00N/12210.00W>Testing
#   \_____/ \____/ \____________/  \_________________________/
#    source  dest      digipath                payload
#
# One line per packet is why -A is used rather than the default two-line AFSK1200
# output, which would need stateful reassembly across reads.

import logging
import re

from ravensdr.subprocess_decoder import SubprocessDecoder

log = logging.getLogger(__name__)

# North American APRS calling frequency.
DEFAULT_FREQUENCY = "144.390M"

# TNC2: SRC>DEST[,digi,digi]:payload   (multimon-ng may prefix "APRS: ")
_TNC2_RE = re.compile(
    r"^(?:APRS:\s*)?"
    r"(?P<source>[A-Z0-9\-]{3,9})>"
    r"(?P<dest>[A-Z0-9\-]{3,9})"
    r"(?P<path>(?:,[A-Z0-9\-*]{1,9})*)"
    r":(?P<payload>.*)$",
    re.IGNORECASE,
)

# Uncompressed position: DDMM.mmN/DDDMM.mmW plus symbol, after a data-type byte.
_POSITION_RE = re.compile(
    r"(?P<lat>\d{4}\.\d{2})(?P<ns>[NS])"
    r"(?P<symtable>.)"
    r"(?P<lon>\d{5}\.\d{2})(?P<ew>[EW])"
    r"(?P<symcode>.)"
)

# Weather fields embedded in a position/weather report, e.g. t072h45b10132
_WX_FIELDS = {
    "t": ("temperature_F", 3),
    "h": ("humidity_pct", 2),
    "b": ("pressure_dPa", 5),
    "g": ("gust_mph", 3),
    "r": ("rain_1h_hundredths_in", 3),
}

# Data-type identifiers that introduce a position report.
_POSITION_TYPES = "!=/@"


def parse_latlon(value, hemi, is_lon):
    """Convert APRS DDMM.mm / DDDMM.mm to signed decimal degrees."""
    split = 3 if is_lon else 2
    degrees = int(value[:split])
    minutes = float(value[split:])
    decimal = degrees + minutes / 60.0
    if hemi.upper() in ("S", "W"):
        decimal = -decimal
    return round(decimal, 5)


def parse_weather(payload):
    """Extract APRS weather fields from a report payload."""
    wx = {}
    for key, (name, width) in _WX_FIELDS.items():
        # Wind direction/speed use c/s and collide with symbol chars, so only the
        # unambiguous prefixed fields are parsed here.
        #
        # The field is a FIXED width that includes any minus sign — sub-zero
        # temperatures go out as "t-05", not "t-005" — so match `width`
        # sign-or-digit characters and let int() reject anything malformed.
        for m in re.finditer(key + r"([-\d]{" + str(width) + r"})", payload):
            try:
                wx[name] = int(m.group(1))
            except ValueError:
                continue
            break
    if "pressure_dPa" in wx:
        # APRS sends tenths of a millibar; hPa is the useful unit.
        wx["pressure_hPa"] = round(wx.pop("pressure_dPa") / 10.0, 1)
    return wx


class AprsReceiver(SubprocessDecoder):
    """Decode APRS packets via rtl_fm piped into multimon-ng."""

    PROC_NAME = "multimon-ng"     # killall guard targets multimon-ng only
    DEFAULT_TTL = 3600            # keep a station an hour after its last beacon

    def __init__(self, device_index=0, frequency=DEFAULT_FREQUENCY, ttl_sec=None):
        super().__init__(device_index=device_index, ttl_sec=ttl_sec)
        self.frequency = frequency

    def build_source_cmd(self):
        # 22050 Hz mono is what multimon-ng's AFSK1200 demodulator expects.
        return ["rtl_fm", "-f", self.frequency, "-M", "fm", "-s", "22050",
                "-d", str(self.device_index), "-"]

    def build_cmd(self):
        return ["multimon-ng", "-t", "raw", "-a", "AFSK1200", "-A", "-"]

    def parse_line(self, line):
        m = _TNC2_RE.match(line.strip())
        if not m:
            return None

        payload = m.group("payload")
        path = [hop for hop in m.group("path").split(",") if hop]
        rec = {
            "source": m.group("source").upper(),
            "dest": m.group("dest").upper(),
            "path": path,
            "payload": payload,
            "raw": line.strip(),
        }

        rec.update(self._parse_payload(payload))
        return rec

    @staticmethod
    def _parse_payload(payload):
        """Pull position, symbol, weather and comment out of a packet payload."""
        out = {}
        if not payload:
            return out

        dti = payload[0]
        out["type"] = _describe_type(dti)

        if dti in _POSITION_TYPES:
            pos = _POSITION_RE.search(payload)
            if pos:
                out["lat"] = parse_latlon(pos.group("lat"), pos.group("ns"), False)
                out["lon"] = parse_latlon(pos.group("lon"), pos.group("ew"), True)
                out["symbol"] = pos.group("symtable") + pos.group("symcode")
                comment = payload[pos.end():].strip()
                if comment:
                    out["comment"] = comment[:120]

        wx = parse_weather(payload)
        if wx:
            out["weather"] = wx

        if dti == ">":
            out["status"] = payload[1:].strip()[:120]
        elif dti == ":":
            # Addressed message: ":ADDRESSEE:text"
            parts = payload[1:].split(":", 1)
            if len(parts) == 2:
                out["addressee"] = parts[0].strip()
                out["message"] = parts[1].strip()[:120]
        return out

    def record_key(self, record):
        return record.get("source")

    def get_stations(self):
        return self.get_records()


def _describe_type(dti):
    return {
        "!": "position",
        "=": "position",
        "/": "position+time",
        "@": "position+time",
        ">": "status",
        ":": "message",
        ";": "object",
        ")": "item",
        "T": "telemetry",
        "_": "weather",
        "`": "mic-e",
        "'": "mic-e",
        "?": "query",
    }.get(dti, "other")
