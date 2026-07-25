# ACARS receiver — acarsdec JSON decoder (VHF aircraft text messaging, ~131 MHz).
#
# acarsdec demodulates several ACARS channels from one dongle and emits one JSON
# object per message (-o 4). We key the record table by aircraft registration so
# the panel shows one row per aircraft with its latest message, and emit each
# message individually for a scrolling feed + ADS-B correlation.

import json
import logging

from ravensdr.subprocess_decoder import SubprocessDecoder

log = logging.getLogger(__name__)

# US ACARS VHF channels (MHz). All fit inside one ~2 MHz capture window so a
# single dongle can decode them together (130.025 .. 131.825 spans 1.8 MHz).
DEFAULT_CHANNELS = ["131.550", "130.025", "130.425", "131.725", "131.825"]


class AcarsReceiver(SubprocessDecoder):
    """Decode VHF ACARS aircraft messages via acarsdec -o 4 (JSON)."""

    PROC_NAME = "acarsdec"
    DEFAULT_TTL = 1200  # aircraft messages are sparse; keep ~20 min

    def __init__(self, device_index=0, channels=None, ttl_sec=None):
        super().__init__(device_index=device_index, ttl_sec=ttl_sec)
        self.channels = channels or list(DEFAULT_CHANNELS)

    def build_cmd(self):
        # -o 4: msg JSON to stdout, -e: drop empty ack-only messages.
        return ["acarsdec", "-o", "4", "-e",
                "-r", str(self.device_index)] + self.channels

    def parse_line(self, line):
        if not line.startswith("{"):
            return None
        obj = json.loads(line)
        # acarsdec nests the message under "vdl2"/"acars" in some builds; 3.7 -o 4
        # emits the ACARS fields at top level.
        msg = obj.get("acars", obj)
        tail = (msg.get("tail") or "").strip()
        flight = (msg.get("flight") or "").strip()
        text = msg.get("text") or ""
        rec = {
            "tail": tail,
            "flight": flight,
            "label": msg.get("label"),
            "freq": msg.get("freq"),
            "level": msg.get("level"),
            "mode": msg.get("mode"),
            "msgno": msg.get("msgno"),
            "text": text.strip(),
            "timestamp": obj.get("timestamp") or msg.get("timestamp"),
        }
        return rec

    def record_key(self, record):
        # One row per aircraft; fall back to flight, then message number.
        return (record.get("tail") or record.get("flight")
                or ("msg-" + str(record.get("msgno", "?"))))

    def get_messages(self):
        return self.get_records()


def flight_digits(callsign):
    """Trailing digit-run of a callsign/flight (e.g. 'AAL1234'->'1234')."""
    if not callsign:
        return ""
    digits = ""
    for ch in reversed(callsign):
        if ch.isdigit():
            digits = ch + digits
        else:
            break
    return digits


def correlate_with_adsb(acars_rec, flights):
    """Best-effort match of an ACARS message to a tracked ADS-B flight.

    ADS-B carries an ICAO callsign (e.g. 'AAL1234') and no registration; ACARS
    carries an IATA-style flight ('AA1234') and a tail. We match on the trailing
    flight-number digits (>=3), which is heuristic but usually unambiguous within
    the small set of aircraft overhead. Returns the matching flight dict or None.
    """
    want = flight_digits(acars_rec.get("flight", ""))
    if len(want) < 3:
        return None
    for f in flights or []:
        if flight_digits(f.get("flight", "")) == want:
            return f
    return None
