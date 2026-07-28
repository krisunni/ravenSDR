# Pager receiver — POCSAG/FLEX decode via `rtl_fm | multimon-ng`.
#
# Unlike the direct-device decoders (rtl_433, acarsdec), multimon-ng takes raw
# 22050 Hz mono audio on stdin, so this is a piped decoder: rtl_fm FM-demodulates
# the paging channel and multimon-ng decodes POCSAG/FLEX from that audio. Output
# lines are parsed into per-address records.

import logging
import re

from ravensdr.subprocess_decoder import SubprocessDecoder

log = logging.getLogger(__name__)

# Paging allocations are highly regional; this is a common commercial POCSAG
# channel and is meant to be overridden per preset.
DEFAULT_FREQUENCY = "152.0075M"

# multimon-ng POCSAG line, e.g.:
#   POCSAG1200: Address:  123456  Function: 3  Alpha:   HELLO WORLD
_POCSAG_RE = re.compile(
    r"POCSAG(\d+):\s*Address:\s*(\d+)\s*Function:\s*(\d+)"
    r"(?:\s*(Alpha|Numeric|Skyper):\s*(.*))?"
)
# multimon-ng FLEX line carries the capcode in brackets, e.g.:
#   FLEX: 2026-07-24 ... [001234567] ALN message text
_FLEX_RE = re.compile(r"FLEX[:|].*?\[0*(\d+)\]\s*\S*\s*(.*)")

# POCSAG numeric mode is 4-bit BCD read through multimon-ng's conversion table
# "084 2.6]195-3U7[". Only the digits and a couple of separators carry meaning;
# "]", "[" and "U" are the reserved and urgency codes. A genuine numeric page is
# a callback number, so it is nearly all digits. A payload that is a third
# reserved codes is not numeric data at all — it is alphanumeric or binary
# content forced through the numeric table, or noise that survived BCH
# correction on a weak signal. Both look like line noise to an operator.
#
# We label rather than drop: a page that decoded badly is still evidence the
# channel is active, and the raw payload stays available.
# "[" and "]" are the two BCD codes with no assigned meaning in numeric paging.
# A callback number never contains one, so a single bracket already says the
# payload is not numeric data. "U" (urgency) IS legitimate — a trailing "U" on a
# real page is common — so it only counts against the message when it recurs.
#
# Counting digits does not work here: the observed noise is ~75% digits and
# sails through any digit-fraction test.
_UNASSIGNED = set("][")
_MAX_URGENCY_FRACTION = 0.15


def numeric_quality(text):
    """Return "ok" or "low" for a numeric-mode POCSAG payload."""
    body = [c for c in text if not c.isspace()]
    if len(body) < 6:
        return "ok"
    if any(c in _UNASSIGNED for c in body):
        return "low"
    urgency = sum(1 for c in body if c == "U")
    return "low" if urgency / len(body) > _MAX_URGENCY_FRACTION else "ok"


class PagerReceiver(SubprocessDecoder):
    """Decode POCSAG/FLEX pager traffic via rtl_fm piped into multimon-ng."""

    PROC_NAME = "multimon-ng"     # killall guard targets multimon-ng only (never rtl_fm)
    DEFAULT_TTL = 1800            # keep an address ~30 min after its last page

    def __init__(self, device_index=0, frequency=DEFAULT_FREQUENCY, ttl_sec=None):
        super().__init__(device_index=device_index, ttl_sec=ttl_sec)
        self.frequency = frequency

    def build_source_cmd(self):
        # rtl_fm demodulates the paging channel to 22050 Hz mono for multimon-ng.
        return ["rtl_fm", "-f", self.frequency, "-M", "fm", "-s", "22050",
                "-d", str(self.device_index), "-"]

    def build_cmd(self):
        return ["multimon-ng", "-t", "raw",
                "-a", "POCSAG512", "-a", "POCSAG1200", "-a", "POCSAG2400",
                "-a", "FLEX", "-e", "-u", "-"]

    def parse_line(self, line):
        m = _POCSAG_RE.search(line)
        if m:
            protocol = "POCSAG" + m.group(1)
            content_type = m.group(4) or ""
            text = (m.group(5) or "").strip()
            quality = ("ok" if content_type != "Numeric"
                       else numeric_quality(text))
            return {
                "protocol": protocol,
                "address": m.group(2),
                "function": m.group(3),
                "content_type": content_type,
                "text": text,
                "quality": quality,
            }
        f = _FLEX_RE.search(line)
        if f:
            return {
                "protocol": "FLEX",
                "address": f.group(1),
                "function": "",
                "content_type": "",
                "text": (f.group(2) or "").strip(),
                "quality": "ok",
            }
        return None

    def record_key(self, record):
        return "%s/%s" % (record.get("protocol", "?"), record.get("address", ""))

    def get_pages(self):
        return self.get_records()
