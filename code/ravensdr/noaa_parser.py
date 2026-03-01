# NOAA weather radio transcript parser
# Extracts structured weather fields from raw Whisper transcript text

import re
from datetime import datetime, timezone


# Alert keywords (case-insensitive matching)
_ALERT_PATTERNS = [
    (r"(winter storm)\s+(warning)", "warning"),
    (r"(blizzard)\s+(warning)", "warning"),
    (r"(tornado)\s+(warning)", "warning"),
    (r"(severe thunderstorm)\s+(warning)", "warning"),
    (r"(flood)\s+(warning)", "warning"),
    (r"(wind)\s+(advisory)", "advisory"),
    (r"(freeze)\s+(advisory)", "advisory"),
    (r"(frost)\s+(advisory)", "advisory"),
    (r"(dense fog)\s+(advisory)", "advisory"),
    (r"(heat)\s+(advisory)", "advisory"),
    (r"(winter weather)\s+(advisory)", "advisory"),
    (r"(winter storm)\s+(watch)", "watch"),
    (r"(tornado)\s+(watch)", "watch"),
    (r"(severe thunderstorm)\s+(watch)", "watch"),
    (r"(flood)\s+(watch)", "watch"),
    (r"(gale)\s+(warning)", "warning"),
    (r"(small craft)\s+(advisory)", "advisory"),
    (r"(hurricane)\s+(warning)", "warning"),
    (r"(tropical storm)\s+(warning)", "warning"),
]

# Area keywords to associate with alerts
_AREA_KEYWORDS = [
    "puget sound", "strait of juan de fuca", "seattle", "tacoma",
    "snoqualmie pass", "stevens pass", "cascades", "king county",
    "pierce county", "kitsap", "skagit", "whatcom", "coastal waters",
    "cape flattery", "point grenville", "admiralty inlet",
]

# Marine zone identifiers
_MARINE_ZONES = [
    "puget sound",
    "strait of juan de fuca",
    "coastal waters",
    "admiralty inlet",
]


def parse_weather_transcript(text):
    """Parse raw Whisper transcript of NOAA weather broadcast into structured data.

    Returns dict with temperature, wind, visibility, alerts, marine forecasts,
    forecast periods, raw transcript, timestamp, and confidence level.
    """
    if not text or not text.strip():
        return {
            "temperature": None,
            "wind": None,
            "visibility": None,
            "alerts": [],
            "marine": [],
            "forecast": [],
            "raw_transcript": "",
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "confidence": "low",
        }

    lower = text.lower()
    fields_parsed = 0
    total_fields = 3  # temperature, wind, visibility

    temperature = _parse_temperature(lower)
    if temperature:
        fields_parsed += 1

    wind = _parse_wind(lower)
    if wind:
        fields_parsed += 1

    visibility = _parse_visibility(lower)
    if visibility:
        fields_parsed += 1

    alerts = _parse_alerts(lower)
    marine = _parse_marine(lower)
    forecast = _parse_forecast(lower)

    if fields_parsed == total_fields:
        confidence = "full"
    elif fields_parsed >= 1:
        confidence = "partial"
    else:
        confidence = "low"

    return {
        "temperature": temperature,
        "wind": wind,
        "visibility": visibility,
        "alerts": alerts,
        "marine": marine,
        "forecast": forecast,
        "raw_transcript": text,
        "parsed_at": datetime.now(timezone.utc).isoformat(),
        "confidence": confidence,
    }


def detect_priority_alert(text):
    """Return True if transcript contains any warning, watch, or advisory keywords."""
    if not text:
        return False
    lower = text.lower()
    for pattern, _ in _ALERT_PATTERNS:
        if re.search(pattern, lower):
            return True
    # Fallback: loose keyword scan
    for keyword in ("warning", "watch", "advisory", "hazardous weather"):
        if keyword in lower:
            return True
    return False


def _parse_temperature(text):
    """Extract temperature in degrees F."""
    # "temperature 45 degrees" / "currently 52" / "temperature is 34 degrees"
    patterns = [
        r"temperature\s+(?:is\s+)?(\d+)\s*degrees",
        r"currently\s+(\d+)\s*degrees",
        r"currently\s+(\d+)",
        r"temperature\s+(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return {"value": int(m.group(1)), "unit": "F"}
    return None


def _parse_wind(text):
    """Extract wind speed, direction, and unit."""
    # "winds north at 15 miles per hour"
    # "winds south at 5 to 10 miles per hour"
    # "southwest winds 20 to 30 knots"
    # "winds light and variable"
    directions = (
        r"(?:north|south|east|west|northwest|northeast|southwest|southeast)"
    )

    # "winds [dir] at N [to N] mph/knots"
    m = re.search(
        r"winds?\s+(" + directions + r")\s+(?:at\s+)?(\d+)(?:\s+to\s+(\d+))?"
        r"\s*(miles per hour|mph|knots|knts)",
        text,
    )
    if m:
        speed = int(m.group(3)) if m.group(3) else int(m.group(2))
        unit = "knots" if "knot" in m.group(4) else "mph"
        return {"speed": speed, "direction": m.group(1), "unit": unit}

    # "[dir] winds N to N knots/mph"
    m = re.search(
        r"(" + directions + r")\s+winds?\s+(\d+)(?:\s+to\s+(\d+))?"
        r"\s*(miles per hour|mph|knots|knts)",
        text,
    )
    if m:
        speed = int(m.group(3)) if m.group(3) else int(m.group(2))
        unit = "knots" if "knot" in m.group(4) else "mph"
        return {"speed": speed, "direction": m.group(1), "unit": unit}

    # "winds light and variable"
    if re.search(r"winds?\s+light\s+and\s+variable", text):
        return {"speed": 0, "direction": "variable", "unit": "mph"}

    return None


def _parse_visibility(text):
    """Extract visibility in miles."""
    # "visibility 10 miles"
    # "visibility one quarter mile"
    # "visibility 2 to 4 miles"
    word_nums = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    }

    # "visibility N miles"
    m = re.search(r"visibility\s+(\d+)\s*(?:to\s+\d+\s+)?miles?", text)
    if m:
        return {"value": int(m.group(1)), "unit": "miles"}

    # "visibility one quarter mile"
    m = re.search(r"visibility\s+one\s+quarter\s+miles?", text)
    if m:
        return {"value": 0.25, "unit": "miles"}

    # "visibility one half mile"
    m = re.search(r"visibility\s+one\s+half\s+miles?", text)
    if m:
        return {"value": 0.5, "unit": "miles"}

    # "visibility [word] miles"
    m = re.search(r"visibility\s+(\w+)\s+miles?", text)
    if m and m.group(1) in word_nums:
        return {"value": word_nums[m.group(1)], "unit": "miles"}

    return None


def _parse_alerts(text):
    """Extract active warnings, watches, and advisories."""
    alerts = []
    seen = set()

    for pattern, alert_type in _ALERT_PATTERNS:
        m = re.search(pattern, text)
        if m:
            name = m.group(0).strip().title()
            if name.lower() in seen:
                continue
            seen.add(name.lower())

            area = _find_area(text)
            alerts.append({
                "type": alert_type,
                "name": name,
                "area": area,
            })

    return alerts


def _find_area(text):
    """Find the most relevant geographic area mentioned in text."""
    for area in _AREA_KEYWORDS:
        if area in text:
            return area.title()
    return ""


def _parse_marine(text):
    """Extract marine forecast segments."""
    segments = []

    for zone in _MARINE_ZONES:
        idx = text.find(zone)
        if idx == -1:
            continue

        # Grab text from zone name to the next zone or end
        after = text[idx:]
        # Find the end: next marine zone or end of string
        end = len(after)
        for other_zone in _MARINE_ZONES:
            if other_zone == zone:
                continue
            other_idx = after.find(other_zone, len(zone))
            if other_idx != -1 and other_idx < end:
                end = other_idx

        forecast_text = after[:end].strip()
        # Only include if there's meaningful content
        if len(forecast_text) > len(zone) + 5:
            segments.append({
                "zone": zone.title(),
                "forecast": forecast_text,
            })

    return segments


def _parse_forecast(text):
    """Extract forecast period segments (tonight, tomorrow, etc.)."""
    periods = []
    period_keywords = [
        "tonight", "tomorrow", "saturday", "sunday", "monday",
        "tuesday", "wednesday", "thursday", "friday",
        "this afternoon", "this evening",
    ]

    for keyword in period_keywords:
        idx = text.find(keyword)
        if idx == -1:
            continue

        # Grab from keyword to next period keyword or end
        after = text[idx:]
        end = len(after)
        for other_kw in period_keywords:
            if other_kw == keyword:
                continue
            other_idx = after.find(other_kw, len(keyword))
            if other_idx != -1 and other_idx < end:
                end = other_idx

        forecast_text = after[:end].strip()
        if len(forecast_text) > len(keyword) + 3:
            periods.append({
                "period": keyword.title(),
                "forecast": forecast_text,
            })

    return periods
