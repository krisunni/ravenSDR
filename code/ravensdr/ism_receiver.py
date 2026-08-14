# ISM-band sensor receiver — rtl_433 JSON decoder (433.92 MHz and friends).
#
# rtl_433 decodes 200+ device protocols (weather stations, TPMS tire sensors,
# utility meters, doorbells, remotes). We run it with JSON-line output on stdout
# and surface each device as a record keyed by model+id.

import json
import logging

from ravensdr.subprocess_decoder import SubprocessDecoder

log = logging.getLogger(__name__)

# rtl_433 default frequency is 433.92 MHz; -F json emits one JSON object per line.
DEFAULT_FREQUENCY = "433.92M"
DEFAULT_HOP_S = 12     # seconds per frequency when covering several


# Sensor readings we render with units/labels in the UI.
KNOWN_READINGS = (
    "temperature_C", "humidity", "wind_avg_km_h", "wind_dir_deg",
    "rain_mm", "pressure_hPa", "battery_ok", "pressure_kPa",
    "moisture", "depth_cm",
)

# Only fields that already have their own column are withheld from the passthrough.
# Everything else is kept — including mod/freq/len, which are genuinely useful on
# an RF intelligence node (e.g. the hop channel a frequency-hopping meter used).
_NON_READING_KEYS = frozenset({
    "model", "id", "channel", "time", "rssi", "snr",
})


def _heard_on(obj):
    """Frequency in MHz this frame was decoded at, or None.

    FSK reports the two tones as freq1/freq2; their midpoint is the carrier.
    """
    if obj.get("freq") is not None:
        return round(float(obj["freq"]), 3)
    f1, f2 = obj.get("freq1"), obj.get("freq2")
    if f1 is not None and f2 is not None:
        return round((float(f1) + float(f2)) / 2.0, 3)
    if f1 is not None:
        return round(float(f1), 3)
    return None


class IsmReceiver(SubprocessDecoder):
    """Decode ISM-band sensor telemetry via rtl_433 -F json."""

    PROC_NAME = "rtl_433"
    DEFAULT_TTL = 900  # sensors report slowly; keep them ~15 min

    def __init__(self, device_index=0, frequency=DEFAULT_FREQUENCY, ttl_sec=None,
                 sample_rate=None, frequencies=None, hop_s=None):
        super().__init__(device_index=device_index, ttl_sec=ttl_sec)
        self.frequency = frequency
        self.sample_rate = sample_rate
        # Optional list. Garage remotes and car fobs are split across 315, 390
        # and 433.92 MHz depending on make and age, and you cannot know in
        # advance which one the thing in your hand uses — so cover them all and
        # let rtl_433 hop.
        self.frequencies = list(frequencies) if frequencies else None
        self.hop_s = hop_s

    def build_cmd(self):
        cmd = [
            "rtl_433",
            "-d", str(self.device_index),
        ]
        # One -f per frequency; rtl_433 hops between them on -H.
        #
        # Hopping is a real trade, not a free win: a remote transmits for about
        # 100 ms, so while the tuner is parked on 315 a press on 433 is simply
        # missed. A short interval covers more of the dial and drops more
        # individual bursts. rtl_433's own default is 600s, which is right for
        # sensors that report every few minutes and useless for something you
        # press once — hence a much shorter default here.
        freqs = self.frequencies or [self.frequency]
        for f in freqs:
            cmd += ["-f", f]
        if len(freqs) > 1:
            cmd += ["-H", str(self.hop_s or DEFAULT_HOP_S)]
        cmd += [
            "-F", "json",
            "-M", "level",   # include RSSI/SNR in output
        ]
        # rtl_433 defaults to 250 kHz, which is plenty for a doorbell parked on
        # one frequency but far too narrow for a frequency-hopping meter: at
        # 912.6 MHz it sees 912.475-912.725 and misses everything else the meter
        # hops to. Presets that need more say so; the rest keep the default,
        # since a wider window costs CPU on every sample for no benefit.
        if self.sample_rate:
            cmd += ["-s", self.sample_rate]
        return cmd

    def parse_line(self, line):
        if not line.startswith("{"):
            return None
        obj = json.loads(line)
        model = obj.get("model")
        if not model:
            return None
        rec = {
            "model": model,
            "id": obj.get("id", obj.get("channel", "")),
            "channel": obj.get("channel"),
            "time": obj.get("time"),
            "rssi": obj.get("rssi"),
            "snr": obj.get("snr"),
            # Which frequency this was actually heard on. rtl_433 reports "freq"
            # for OOK/ASK and a freq1/freq2 pair for FSK (the two tones). The
            # panel keeps history across retunes, so without this a 315 MHz TPMS
            # sits in the same table as a 912 MHz meter with nothing to tell
            # them apart — which is exactly how stale TPMS rows came to look
            # like they had been decoded on the ERT band.
            "freq_mhz": _heard_on(obj),
        }
        # Common sensor readings (present depending on device type)
        for k in KNOWN_READINGS:
            if k in obj:
                rec[k] = obj[k]

        # Anything else the decoder produced. rtl_433 supports ~250 protocols and
        # only weather sensors report the fields above — utility meters
        # (LandisGyr-GS, SCM, IDM), TPMS and remotes emit entirely different keys.
        # Whitelisting alone silently discarded them, so such devices showed a
        # blank READINGS column with no way to tell "nothing decoded" apart from
        # "decoded, then dropped".
        extra = {k: v for k, v in obj.items()
                 if k not in _NON_READING_KEYS and k not in KNOWN_READINGS
                 and not isinstance(v, (dict, list))}
        if extra:
            rec["extra"] = extra

        # Keep the decoder's complete output. Nothing is discarded: the UI shows
        # a summary inline and the full frame on click, so an unfamiliar device
        # is always inspectable rather than reduced to whatever keys we happened
        # to anticipate.
        rec["raw"] = obj
        return rec

    def record_key(self, record):
        return "%s/%s" % (record.get("model", "?"), record.get("id", ""))

    def get_devices(self):
        return self.get_records()
