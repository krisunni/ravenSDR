# ADS-B correlator — callsign extraction + flight matching

import logging
import re

log = logging.getLogger(__name__)

# Common ICAO airline designators -> callsign prefixes
AIRLINE_CODES = {
    "alaska": "ASA", "united": "UAL", "delta": "DAL",
    "american": "AAL", "southwest": "SWA", "jetblue": "JBU",
    "horizon": "QXE", "skywest": "SKW", "frontier": "FFT",
    "spirit": "NKS", "hawaiian": "HAL",
    # Common Whisper misheard variants
    "a last car": "ASA", "you knighted": "UAL",
}

# Patterns: "Alaska 412", "UAL 732", "Delta 89", "N12345"
CALLSIGN_PATTERNS = [
    # Airline name + flight number: "Alaska four twelve"
    re.compile(
        r'\b(' + '|'.join(re.escape(k) for k in AIRLINE_CODES.keys()) + r')\s+(\d{1,4})\b',
        re.IGNORECASE
    ),
    # ICAO code + flight number: "UAL 732"
    re.compile(
        r'\b([A-Z]{3})\s*(\d{1,4})\b'
    ),
    # N-number: "N12345" or "N1234A"
    re.compile(
        r'\b(N\d{1,5}[A-Z]{0,2})\b', re.IGNORECASE
    ),
]


def extract_callsigns(transcript: str) -> list[str]:
    """Extract potential callsigns from a Whisper transcript line.

    Returns normalized ICAO-style callsigns (e.g. "ASA412", "N12345").
    """
    matches = []
    seen = set()
    for pattern in CALLSIGN_PATTERNS:
        for m in pattern.finditer(transcript):
            groups = m.groups()
            if len(groups) == 2:
                airline, number = groups
                code = AIRLINE_CODES.get(airline.lower(), airline.upper())
                cs = f"{code}{number}"
            else:
                cs = groups[0].upper()
            if cs not in seen:
                seen.add(cs)
                matches.append(cs)
    return matches


def match_flights(callsigns: list[str], flights: list[dict]) -> list[dict]:
    """Match extracted callsigns against dump1090 flight list.

    Returns list of matched flight dicts with added 'matched_callsign' key.
    """
    matched = []
    for flight in flights:
        flight_cs = flight.get("flight", "").strip().upper()
        if not flight_cs:
            continue
        for cs in callsigns:
            if cs in flight_cs or flight_cs in cs:
                matched.append({**flight, "matched_callsign": cs})
                break
    return matched
