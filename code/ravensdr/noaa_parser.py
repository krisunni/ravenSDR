# NOAA weather radio transcript parser
# Extracts structured weather fields from raw Whisper transcript text

import re
import time
from collections import Counter, deque
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

# Sky/condition phrases, most-specific first so "mostly cloudy" wins over "cloudy".
_CONDITION_PATTERNS = [
    (r"partly sunny", "Partly Sunny"),
    (r"mostly sunny", "Mostly Sunny"),
    (r"partly cloudy", "Partly Cloudy"),
    (r"mostly cloudy", "Mostly Cloudy"),
    (r"mostly clear", "Mostly Clear"),
    (r"freezing rain", "Freezing Rain"),
    (r"thunderstorms?", "Thunderstorms"),
    (r"scattered showers?", "Scattered Showers"),
    (r"showers?", "Showers"),
    (r"drizzle", "Drizzle"),
    (r"overcast", "Overcast"),
    (r"sunny", "Sunny"),
    (r"clear", "Clear"),
    (r"cloudy", "Cloudy"),
    (r"rain", "Rain"),
    (r"snow", "Snow"),
    (r"sleet", "Sleet"),
    (r"\bfog", "Fog"),
    (r"haze", "Haze"),
    (r"windy", "Windy"),
    (r"breezy", "Breezy"),
]

# Words that look like place-name candidates but aren't — filtered out of the
# location vote (whisper capitalizes these too).
_LOCATION_STOPWORDS = {
    "noaa", "weather", "radio", "forecast", "warning", "watch", "advisory",
    "north", "south", "east", "west", "northwest", "northeast", "southwest",
    "southeast", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "tonight", "today", "tomorrow", "morning", "afternoon",
    "evening", "night", "degrees", "sunny", "cloudy", "clear", "rain", "wind",
    "winds", "the", "and", "for", "high", "low", "highs", "lows", "mostly",
    "partly", "point", "county", "national", "service", "zone", "coast",
    "coastal", "waters", "sound", "bay", "pass", "area", "mph", "knots",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    # common Whisper garble / filler that gets capitalized mid-sentence
    "loves", "love", "lows", "well", "yeah", "okay", "thanks", "sea", "seas",
    "there", "here", "then", "this", "that", "when", "with", "from", "will",
    "they", "them", "their", "these", "those", "your", "ours", "hers", "into",
    "been", "have", "were", "what", "which", "some", "more", "over", "rest",
    "root", "roost", "even", "like", "just", "much", "next", "past", "chance",
}


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
    total_fields = 4  # conditions, temperature, wind, visibility

    conditions = _parse_conditions(lower)
    if conditions:
        fields_parsed += 1

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

    if fields_parsed >= 3:
        confidence = "full"
    elif fields_parsed >= 1:
        confidence = "partial"
    else:
        confidence = "low"

    return {
        "conditions": conditions,
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


def _valid_temp(n):
    return isinstance(n, int) and -30 <= n <= 130


def _parse_conditions(text):
    """Return the sky/condition phrase (e.g. 'Mostly Cloudy'), or None."""
    for pattern, label in _CONDITION_PATTERNS:
        if re.search(pattern, text):
            return label
    return None


def _parse_temperature(text):
    """Extract a current temperature in degrees F."""
    # "temperature 45 degrees" / "currently 52" / "52 degrees"
    patterns = [
        r"temperature\s+(?:is\s+)?(\d+)\s*degrees",
        r"currently\s+(\d+)\s*degrees",
        r"currently\s+(\d+)",
        r"temperature\s+(?:is\s+)?(\d+)",
        r"(\d+)\s*degrees",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m and _valid_temp(int(m.group(1))):
            return {"value": int(m.group(1)), "unit": "F"}
    return None


_WORD_TENS = {
    "thirties": 30, "forties": 40, "fifties": 50, "sixties": 60,
    "seventies": 70, "eighties": 80, "nineties": 90,
}


def _vote_temperature(texts):
    """Median of all plausible temperature mentions across the window.

    Robust to single garbled readings — e.g. Whisper splitting "72 degrees" into
    "temperature 7, degrees" can't drag the current temp down to 7 when other
    samples report 65-73.
    """
    vals = []
    for t in texts:
        low = t.lower()
        for pat in (r"temperature\s+(?:is\s+)?(\d+)", r"currently\s+(\d+)", r"(\d+)\s*degrees"):
            for m in re.finditer(pat, low):
                n = int(m.group(1))
                if _valid_temp(n):
                    vals.append(n)
    if not vals:
        return None
    vals.sort()
    return {"value": vals[len(vals) // 2], "unit": "F"}


def _parse_temp_extremes(text):
    """Extract forecast high/low temps. Handles 'high near 89', 'highs in the
    upper 80s', 'lows in the lower sixties', etc. Returns (high, low) or None."""
    adj = {"mid": 5, "middle": 5, "upper": 7, "lower": 2, "low": 2}
    tens_re = "|".join(_WORD_TENS)

    def find(kind):  # kind = "high" or "low"
        # "high near/around/of 89"
        m = re.search(kind + r"s?\s+(?:near|around|of|is|about)\s+(\d+)", text)
        if m and _valid_temp(int(m.group(1))):
            return int(m.group(1))
        # "highs in the upper 80s" / "lows in the 60s"
        m = re.search(kind + r"s?\s+(?:in the\s+)?(mid|middle|upper|lower|low)?\s*(\d0)s", text)
        if m and _valid_temp(int(m.group(2))):
            return int(m.group(2)) + adj.get(m.group(1), 0)
        # word form: "lows in the lower sixties" / "highs in the eighties"
        m = re.search(kind + r"s?\s+(?:in the\s+)?(mid|middle|upper|lower|low)?\s*(" + tens_re + r")", text)
        if m:
            return _WORD_TENS[m.group(2)] + adj.get(m.group(1), 0)
        # bare "high 72"
        m = re.search(kind + r"s?\s+(\d+)\b", text)
        if m and _valid_temp(int(m.group(1))):
            return int(m.group(1))
        return None

    return find("high"), find("low")


def _extract_location(texts):
    """Vote for the most-mentioned place name across accumulated segments.

    Counts each candidate once PER SEGMENT and requires it to appear in at
    least two distinct segments, so one-off Whisper garble (e.g. "Loves" for
    "lows") can never surface as a location — only names that actually recur
    across NOAA's broadcast loop do. Needs a few samples before voting.
    """
    if len(texts) < 3:
        return []
    seg_counts = Counter()
    for text in texts:
        names = {tok for tok in re.findall(r"\b([A-Z][a-z]{3,})\b", text)
                 if tok.lower() not in _LOCATION_STOPWORDS}
        seg_counts.update(names)
    return [name for name, n in seg_counts.most_common(8) if n >= 2][:3]


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

    # "winds 10 to 15 mph" / "10 to 15 miles per hour" (no direction stated)
    m = re.search(
        r"winds?\s+(\d+)(?:\s+to\s+(\d+))?\s*(miles per hour|mph|knots|knts)",
        text,
    ) or re.search(
        r"(\d+)\s+to\s+(\d+)\s*(miles per hour|mph|knots|knts)",
        text,
    )
    if m:
        speed = int(m.group(2)) if m.group(2) else int(m.group(1))
        unit = "knots" if "knot" in m.group(3) else "mph"
        if 0 < speed <= 150:
            return {"speed": speed, "direction": None, "unit": unit}

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
                "forecast": forecast_text[:200],
            })

    return periods


def build_summary(texts):
    """Build a voted weather summary from a list of recent transcript strings.

    Votes conditions across segments and extracts location/temp/high/low/wind
    from the combined text, so a garbled single chunk doesn't dominate.
    """
    if not texts:
        return {
            "location": [], "conditions": None, "temperature": None,
            "high": None, "low": None, "wind": None, "visibility": None,
            "alerts": [], "forecast": [], "raw_transcript": "",
            "sample_count": 0, "parsed_at": datetime.now(timezone.utc).isoformat(),
            "confidence": "low",
        }

    combined = " . ".join(texts)
    lower = combined.lower()

    # Vote conditions across segments — the real sky state recurs on the loop.
    cond_votes = Counter()
    for t in texts:
        c = _parse_conditions(t.lower())
        if c:
            cond_votes[c] += 1
    conditions = cond_votes.most_common(1)[0][0] if cond_votes else None

    temperature = _vote_temperature(texts)
    high, low = _parse_temp_extremes(lower)
    location = _extract_location(texts)
    wind = _parse_wind(lower)
    visibility = _parse_visibility(lower)
    alerts = _parse_alerts(lower)
    forecast = _parse_forecast(lower)

    filled = sum(bool(x) for x in
                 (conditions, temperature, high, low, wind, visibility, location))
    confidence = "full" if filled >= 3 else "partial" if filled >= 1 else "low"

    return {
        "location": location,
        "conditions": conditions,
        "temperature": temperature,
        "high": high,
        "low": low,
        "wind": wind,
        "visibility": visibility,
        "alerts": alerts,
        "forecast": forecast,
        "raw_transcript": texts[-1],
        "sample_count": len(texts),
        "parsed_at": datetime.now(timezone.utc).isoformat(),
        "confidence": confidence,
    }


class WeatherAccumulator:
    """Rolling window of NOAA transcripts → a voted, richer weather summary.

    NOAA weather radio repeats on a few-minute loop; each Whisper chunk is
    garbled, but across many chunks the true content recurs. Accumulate a
    time+count-bounded window and re-summarize on every new segment.
    """

    def __init__(self, window_secs=900, max_segments=40):
        self.window_secs = window_secs
        self.max_segments = max_segments
        self._segments = deque()  # (timestamp, text)

    def _evict(self, now):
        while self._segments and (
            now - self._segments[0][0] > self.window_secs
            or len(self._segments) > self.max_segments
        ):
            self._segments.popleft()

    def add(self, text):
        """Add a transcript segment; returns the updated summary."""
        if text and text.strip():
            now = time.time()
            self._segments.append((now, text.strip()))
            self._evict(now)
        return self.summary()

    def summary(self):
        self._evict(time.time())
        return build_summary([t for _, t in self._segments])

    def reset(self):
        self._segments.clear()
