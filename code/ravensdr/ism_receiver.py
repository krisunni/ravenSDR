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


class IsmReceiver(SubprocessDecoder):
    """Decode ISM-band sensor telemetry via rtl_433 -F json."""

    PROC_NAME = "rtl_433"
    DEFAULT_TTL = 900  # sensors report slowly; keep them ~15 min

    def __init__(self, device_index=0, frequency=DEFAULT_FREQUENCY, ttl_sec=None):
        super().__init__(device_index=device_index, ttl_sec=ttl_sec)
        self.frequency = frequency

    def build_cmd(self):
        return [
            "rtl_433",
            "-d", str(self.device_index),
            "-f", self.frequency,
            "-F", "json",
            "-M", "level",   # include RSSI/SNR in output
        ]

    def parse_line(self, line):
        if not line.startswith("{"):
            return None
        obj = json.loads(line)
        model = obj.get("model")
        if not model:
            return None
        # Normalise the fields we display; keep the raw payload too.
        rec = {
            "model": model,
            "id": obj.get("id", obj.get("channel", "")),
            "channel": obj.get("channel"),
            "time": obj.get("time"),
            "rssi": obj.get("rssi"),
            "snr": obj.get("snr"),
        }
        # Common sensor readings (present depending on device type)
        for k in ("temperature_C", "humidity", "wind_avg_km_h", "wind_dir_deg",
                  "rain_mm", "pressure_hPa", "battery_ok", "pressure_kPa",
                  "moisture", "depth_cm"):
            if k in obj:
                rec[k] = obj[k]
        return rec

    def record_key(self, record):
        return "%s/%s" % (record.get("model", "?"), record.get("id", ""))

    def get_devices(self):
        return self.get_records()
