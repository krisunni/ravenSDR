# Vehicle departure/arrival detection from TPMS.
#
# What this can and cannot do, because the physics decides it:
#
# TPMS sensors are accelerometer-triggered and sleep when the wheel is not
# turning — typically below about 25 km/h. So a parked car is silent. You
# CANNOT ask "is the car in the driveway"; you CAN see it leave and see it come
# back, because it is moving both times. Departures and arrivals are the whole
# available signal, and happily that is the interesting part.
#
# Scope is deliberately limited to vehicles the operator registers. A TPMS id is
# a persistent unique identifier, so logging every id that passes would build a
# movement history of identifiable people who never agreed to it. Tracking your
# own car is a different thing from that, and this module only does the former.

import logging
import time

log = logging.getLogger(__name__)

# A burst of frames from the same vehicle is one event. Sensors fire every few
# seconds while rolling, so a gap much longer than that means the car has gone
# out of range rather than paused between frames.
EVENT_GAP_S = 180
# Below this many frames it is a fleeting catch at the edge of range, not a
# vehicle movement worth logging.
MIN_FRAMES = 2
# RSSI slope across an event, in dB, before it is called approaching/departing.
DIRECTION_MIN_SLOPE_DB = 1.5
MAX_EVENTS = 500

# rtl_433 reports a TPMS frame with type "TPMS" and a make as the model. Older
# observation-log entries predate the type being recorded, so the make is the
# only evidence left — hence this list, taken from rtl_433's own TPMS decoders.
# Without it, grouping by id prefix happily proposes 23 LandisGyr utility meters
# sharing a "907b" prefix as though they were a single 23-wheeled vehicle.
TPMS_MODEL_HINTS = frozenset({
    "tpms", "toyota", "schrader", "citroen", "steelmate", "ford", "renault",
    "nissan", "bmw", "audi", "kia", "hyundai", "elantra", "jansite", "abarth",
    "porsche", "ave", "tyreguard", "eeztire", "pmv-107j", "gm-aftermarket",
    "continental", "sensata", "huf", "beru", "infiniti", "subaru",
})


def _looks_like_tpms(record):
    rtype = str(record.get("type") or "").upper()
    if rtype:
        return rtype == "TPMS"          # authoritative when present
    model = str(record.get("model") or "").lower()
    return any(h in model for h in TPMS_MODEL_HINTS)


class VehicleTracker:
    """Group registered TPMS ids into vehicles and log their movements."""

    def __init__(self, emit_fn=None):
        self.emit_fn = emit_fn or (lambda *a, **k: None)
        self._vehicles = {}      # label -> set(sensor ids)
        self._open = {}          # label -> in-progress event
        self._events = []        # completed, newest last

    # ── registry ─────────────────────────────────────────────────────────

    def set_vehicles(self, mapping):
        """mapping: {label: [sensor_id, ...]}"""
        self._vehicles = {
            str(k): {str(s).lower() for s in (v or [])}
            for k, v in (mapping or {}).items()
        }
        return self.vehicles()

    def vehicles(self):
        return {k: sorted(v) for k, v in self._vehicles.items()}

    def _vehicle_for(self, sensor_id):
        sid = str(sensor_id or "").lower()
        for label, ids in self._vehicles.items():
            if sid in ids:
                return label
        return None

    # ── observation ──────────────────────────────────────────────────────

    def observe(self, record):
        """Feed one decoded TPMS record. Returns an event if one just closed."""
        sid = record.get("id")
        label = self._vehicle_for(sid)
        if label is None:
            return None          # not a registered vehicle: deliberately ignored

        now = time.time()
        rssi = record.get("rssi")
        ev = self._open.get(label)
        if ev is None or now - ev["last_at"] > EVENT_GAP_S:
            if ev is not None:
                self._close(label, ev)
            ev = {
                "vehicle": label,
                "started_at": now,
                "last_at": now,
                "frames": 0,
                "sensors": set(),
                "rssi": [],
                "pressure_psi": {},
                "temperature_c": {},
            }
            self._open[label] = ev

        ev["last_at"] = now
        ev["frames"] += 1
        ev["sensors"].add(str(sid).lower())
        if isinstance(rssi, (int, float)):
            ev["rssi"].append(float(rssi))
        if record.get("pressure_PSI") is not None:
            ev["pressure_psi"][str(sid).lower()] = record["pressure_PSI"]
        if record.get("temperature_C") is not None:
            ev["temperature_c"][str(sid).lower()] = record["temperature_C"]
        return None

    def tick(self, now=None):
        """Close any event whose vehicle has gone quiet. Call periodically."""
        now = now or time.time()
        closed = []
        for label, ev in list(self._open.items()):
            if now - ev["last_at"] > EVENT_GAP_S:
                self._open.pop(label, None)
                rec = self._close(label, ev)
                if rec:
                    closed.append(rec)
        return closed

    def _close(self, label, ev):
        if ev["frames"] < MIN_FRAMES:
            return None
        rec = {
            "vehicle": label,
            "at": ev["started_at"],
            "ended_at": ev["last_at"],
            "duration_s": round(ev["last_at"] - ev["started_at"], 1),
            "frames": ev["frames"],
            "sensors_heard": len(ev["sensors"]),
            "sensors_registered": len(self._vehicles.get(label, ())),
            "direction": self._direction(ev["rssi"]),
            "peak_rssi": round(max(ev["rssi"]), 2) if ev["rssi"] else None,
            "pressure_psi": dict(ev["pressure_psi"]),
            "temperature_c": dict(ev["temperature_c"]),
        }
        self._events.append(rec)
        if len(self._events) > MAX_EVENTS:
            self._events = self._events[-MAX_EVENTS:]
        log.info("VEHICLE %s | %s | %d frames, %d/%d sensors, peak %s dB",
                 label, rec["direction"], rec["frames"],
                 rec["sensors_heard"], rec["sensors_registered"], rec["peak_rssi"])
        self.emit_fn("vehicle_event", rec)
        return rec

    @staticmethod
    def _direction(rssi):
        """Approaching, departing, or passing, from how RSSI moved.

        A car driving toward the antenna gets louder and one driving away gets
        quieter, so the sign of the trend across the burst is the direction. A
        single frame says nothing, and a burst that rises then falls is a pass —
        which is what a car going by on the road looks like, as opposed to one
        arriving at the house and stopping.
        """
        if len(rssi) < 2:
            return "unknown"
        first, last = rssi[0], rssi[-1]
        peak = max(rssi)
        peak_i = rssi.index(peak)
        # Peak in the middle with a fall either side = went past.
        if 0 < peak_i < len(rssi) - 1 and (peak - first) > DIRECTION_MIN_SLOPE_DB \
                and (peak - last) > DIRECTION_MIN_SLOPE_DB:
            return "passing"
        delta = last - first
        if delta > DIRECTION_MIN_SLOPE_DB:
            return "arriving"
        if delta < -DIRECTION_MIN_SLOPE_DB:
            return "departing"
        return "nearby"

    # ── output ───────────────────────────────────────────────────────────

    def events(self, limit=50, vehicle=None):
        evs = self._events
        if vehicle:
            evs = [e for e in evs if e["vehicle"] == vehicle]
        return list(reversed(evs[-limit:]))

    def status(self):
        return {
            "vehicles": self.vehicles(),
            "active": sorted(self._open.keys()),
            "events": len(self._events),
        }


def suggest_vehicles(records, min_shared=3):
    """Guess which sensor ids belong together, for the registration UI.

    Four tyres on one car are fitted as a set, so their ids are usually
    consecutive from one manufacturing batch — the observed set de6d5816,
    de6d56e6, de6d6708, de6d66e5 shares a four-hex-digit prefix. Grouping by
    prefix is a starting suggestion for the operator to confirm, not a claim:
    two cars of the same make bought together could share one, and a replaced
    tyre will not match its siblings.
    """
    groups = {}
    for r in records or []:
        # Accepts both shapes: a live rtl_433 record (id + type) and a durable
        # observation-log entry (key, no type). The log is what makes this
        # useful — live records are cleared on restart, so suggesting only from
        # those would work exactly when a car happens to be driving past.
        sid = str(r.get("id") or r.get("key") or "").lower()
        if len(sid) < 6 or not _looks_like_tpms(r):
            continue
        groups.setdefault(sid[:4], set()).add(sid)
    return [
        {"prefix": p, "sensors": sorted(ids), "count": len(ids)}
        for p, ids in sorted(groups.items())
        if len(ids) >= min_shared
    ]
