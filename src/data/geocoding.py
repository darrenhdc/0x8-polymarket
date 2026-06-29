"""City extraction and geocoding helpers for weather-market questions."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Optional

import requests


OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

KNOWN_LOCATIONS = {
    "beijing": ("Beijing", "China", 39.9042, 116.4074),
    "shanghai": ("Shanghai", "China", 31.2304, 121.4737),
    "tokyo": ("Tokyo", "Japan", 35.6762, 139.6503),
    "seoul": ("Seoul", "South Korea", 37.5665, 126.9780),
    "new york": ("New York", "United States", 40.7128, -74.0060),
    "nyc": ("New York", "United States", 40.7128, -74.0060),
    "london": ("London", "United Kingdom", 51.5072, -0.1276),
    "paris": ("Paris", "France", 48.8566, 2.3522),
    "berlin": ("Berlin", "Germany", 52.5200, 13.4050),
    "moscow": ("Moscow", "Russia", 55.7558, 37.6173),
    "dubai": ("Dubai", "United Arab Emirates", 25.2048, 55.2708),
    "singapore": ("Singapore", "Singapore", 1.3521, 103.8198),
    "sydney": ("Sydney", "Australia", -33.8688, 151.2093),
    "chicago": ("Chicago", "United States", 41.8781, -87.6298),
    "los angeles": ("Los Angeles", "United States", 34.0522, -118.2437),
    "miami": ("Miami", "United States", 25.7617, -80.1918),
    "houston": ("Houston", "United States", 29.7604, -95.3698),
    "hong kong": ("Hong Kong", "Hong Kong", 22.3193, 114.1694),
    "hongkong": ("Hong Kong", "Hong Kong", 22.3193, 114.1694),
    "hk": ("Hong Kong", "Hong Kong", 22.3193, 114.1694),
    "phoenix": ("Phoenix", "United States", 33.4484, -112.0740),
    "las vegas": ("Las Vegas", "United States", 36.1716, -115.1391),
    "dallas": ("Dallas", "United States", 32.7767, -96.7970),
    "san francisco": ("San Francisco", "United States", 37.7749, -122.4194),
    # New cities — Polymarket active markets
    "madrid": ("Madrid", "Spain", 40.4168, -3.7038),
    "munich": ("Munich", "Germany", 48.1351, 11.5820),
    "amsterdam": ("Amsterdam", "Netherlands", 52.3676, 4.9041),
    "helsinki": ("Helsinki", "Finland", 60.1699, 24.9384),
    "istanbul": ("Istanbul", "Turkey", 41.0082, 28.9784),
    "guangzhou": ("Guangzhou", "China", 23.1291, 113.2644),
    "shenzhen": ("Shenzhen", "China", 22.5431, 114.0579),
    "taipei": ("Taipei", "Taiwan", 25.0330, 121.5654),
    "chongqing": ("Chongqing", "China", 29.4316, 106.9123),
    "wuhan": ("Wuhan", "China", 30.5928, 114.3055),
    "chengdu": ("Chengdu", "China", 30.5728, 104.0668),
    "busan": ("Busan", "South Korea", 35.1796, 129.0756),
}


@dataclass(frozen=True)
class Location:
    id: str
    name: str
    country: Optional[str]
    latitude: float
    longitude: float
    timezone: Optional[str] = None
    source: str = "known"
    raw_json: Optional[str] = None


def normalize_location_id(name: str, country: Optional[str] = None) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if country:
        suffix = re.sub(r"[^a-z0-9]+", "_", country.lower()).strip("_")
        return f"{base}_{suffix}"
    return base


def extract_city(question: str) -> Optional[str]:
    q = question.lower()
    for key in sorted(KNOWN_LOCATIONS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", q):
            return KNOWN_LOCATIONS[key][0]

    patterns = [
        r"\bin\s+([A-Z][A-Za-z .'-]+?)(?:\s+(?:on|by|above|over|under|below|reach|exceed)|[?,]|$)",
        r"\bfor\s+([A-Z][A-Za-z .'-]+?)(?:\s+(?:on|by|above|over|under|below|reach|exceed)|[?,]|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, question)
        if match:
            candidate = match.group(1).strip()
            candidate = re.sub(r"\s+", " ", candidate)
            if len(candidate) >= 3:
                return candidate
    return None


def geocode_city(city: str, sleep_seconds: float = 0.1) -> Optional[Location]:
    key = city.lower().strip()
    if key in KNOWN_LOCATIONS:
        name, country, lat, lon = KNOWN_LOCATIONS[key]
        return Location(
            id=normalize_location_id(name, country),
            name=name,
            country=country,
            latitude=lat,
            longitude=lon,
            source="known",
        )

    try:
        resp = requests.get(
            OPEN_METEO_GEOCODING_URL,
            params={"name": city, "count": 1, "language": "en", "format": "json"},
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        results = data.get("results") or []
        if not results:
            return None
        row = results[0]
        time.sleep(sleep_seconds)
        name = row.get("name") or city
        country = row.get("country")
        return Location(
            id=normalize_location_id(name, country),
            name=name,
            country=country,
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
            timezone=row.get("timezone"),
            source="open-meteo-geocoding",
            raw_json=json.dumps(row, ensure_ascii=False),
        )
    except requests.RequestException:
        return None
