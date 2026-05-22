"""Task 1 — City Discovery Scanner.

Fetches ALL weather events from Polymarket Gamma API, then for each of the
21 target cities determines how many markets exist (by substring match in
question text). Does NOT write to weather_markets.db.

Saves results to data/city_discovery.json.

Usage:
    python3 -m src.scripts.city_discovery
    python3 -m src.scripts.city_discovery --include-db  # also scan NULL-city rows
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

import requests

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

PROJECT_ROOT = Path(__file__).parent.parent.parent
DISCOVERY_JSON = PROJECT_ROOT / "data" / "city_discovery.json"
MARKET_DB = PROJECT_ROOT / "data" / "weather_markets.db"

# City slug → (display_name, country, list of match strings for question text)
CITY_ALIASES: dict[str, tuple[str, str, list[str]]] = {
    "beijing":       ("Beijing",       "China",                ["beijing"]),
    "shanghai":      ("Shanghai",      "China",                ["shanghai"]),
    "tokyo":         ("Tokyo",         "Japan",                ["tokyo"]),
    "seoul":         ("Seoul",         "South Korea",          ["seoul"]),
    "new-york":      ("New York",      "United States",        ["new york", "nyc"]),
    "london":        ("London",        "United Kingdom",       ["london"]),
    "paris":         ("Paris",         "France",               ["paris"]),
    "berlin":        ("Berlin",        "Germany",              ["berlin"]),
    "moscow":        ("Moscow",        "Russia",               ["moscow"]),
    "dubai":         ("Dubai",         "United Arab Emirates", ["dubai"]),
    "singapore":     ("Singapore",     "Singapore",            ["singapore"]),
    "sydney":        ("Sydney",        "Australia",            ["sydney"]),
    "chicago":       ("Chicago",       "United States",        ["chicago"]),
    "los-angeles":   ("Los Angeles",   "United States",        ["los angeles", "los angeles", "la california"]),
    "miami":         ("Miami",         "United States",        ["miami"]),
    "houston":       ("Houston",       "United States",        ["houston"]),
    "phoenix":       ("Phoenix",       "United States",        ["phoenix"]),
    "las-vegas":     ("Las Vegas",     "United States",        ["las vegas"]),
    "dallas":        ("Dallas",        "United States",        ["dallas"]),
    "san-francisco": ("San Francisco", "United States",        ["san francisco", "san francisco"]),
    "hong-kong":     ("Hong Kong",     "Hong Kong",            ["hong kong", "hongkong"]),
}


def _is_weather_question(q: str) -> bool:
    """Lightweight weather market filter."""
    q = q.lower()
    return any(w in q for w in [
        "temperature", "°c", "°f", "celsius", "fahrenheit",
        "precipitation", "rainfall", "rain", "snow", "snowfall",
        "hottest", "highest temp", "lowest temp", "warmest", "coldest",
        "heat", "degrees",
    ])


def _city_matches(question: str, aliases: list[str]) -> bool:
    q = question.lower()
    return any(alias in q for alias in aliases)


def _parse_market_type(question: str) -> str:
    q = question.lower()
    if any(w in q for w in ["precipitation", "rainfall", "rain", "mm"]):
        return "precip"
    if any(w in q for w in ["snow", "snowfall"]):
        return "snow"
    return "temp_above"


def _parse_target_date(market: dict) -> Optional[str]:
    for key in ("endDate", "end_date", "startDate", "start_date"):
        val = market.get(key)
        if val:
            return val[:10]
    return None


def fetch_all_weather_events(session: requests.Session, sleep: float = 0.5) -> list[dict]:
    """Fetch all weather-tagged events (all pages, open + closed)."""
    all_events: list[dict] = []
    for closed in [False, True]:
        offset = 0
        page_size = 500
        while True:
            params = {
                "tag_slug": "weather",
                "limit": page_size,
                "offset": offset,
                "closed": str(closed).lower(),
            }
            try:
                resp = session.get(GAMMA_EVENTS_URL, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                events = data if isinstance(data, list) else data.get("events", [])
                if not events:
                    break
                all_events.extend(events)
                print(f"  Fetched {len(events)} events (closed={closed}, offset={offset}) — total {len(all_events)}")
                if len(events) < page_size:
                    break
                offset += page_size
                time.sleep(sleep)
            except Exception as exc:
                print(f"  WARN: fetch error at offset={offset}: {exc}")
                break
    return all_events


def fetch_weather_markets_direct(session: requests.Session, sleep: float = 0.5) -> list[dict]:
    """Directly fetch markets with weather tag."""
    all_markets: list[dict] = []
    for closed in [False, True]:
        offset = 0
        page_size = 500
        while True:
            params = {
                "tag_slug": "weather",
                "limit": page_size,
                "offset": offset,
                "closed": str(closed).lower(),
            }
            try:
                resp = session.get(GAMMA_MARKETS_URL, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                markets = data if isinstance(data, list) else data.get("markets", [])
                if not markets:
                    break
                all_markets.extend(markets)
                if len(markets) < page_size:
                    break
                offset += page_size
                time.sleep(sleep)
            except Exception as exc:
                print(f"  WARN: direct markets fetch error: {exc}")
                break
    return all_markets


def scan_db_null_city(db_path: Path) -> list[dict]:
    """Return rows from weather_markets.db where city IS NULL."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, question, target_date FROM markets WHERE city IS NULL"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def run(include_db: bool = False) -> dict:
    session = requests.Session()

    print("=== PHASE 1: Fetch all weather events from Polymarket ===")
    events = fetch_all_weather_events(session)
    print(f"Total events fetched: {len(events)}")

    # Extract all markets from events
    event_markets: list[dict] = []
    for event in events:
        for mkt in event.get("markets", []):
            enriched = dict(mkt)
            enriched["_event_title"] = event.get("title", "")
            enriched["_event_slug"] = event.get("slug", "")
            event_markets.append(enriched)

    print(f"Total markets from events: {len(event_markets)}")

    print("\n=== PHASE 2: Fetch direct weather markets ===")
    direct_markets = fetch_weather_markets_direct(session)
    print(f"Total direct weather markets: {len(direct_markets)}")

    # Deduplicate by conditionId / id
    all_market_map: dict[str, dict] = {}
    for m in event_markets + direct_markets:
        mid = m.get("conditionId") or m.get("id") or ""
        if mid:
            all_market_map[mid] = m

    all_markets = list(all_market_map.values())
    print(f"Unique markets total: {len(all_markets)}")

    # Filter to weather-question markets only
    weather_markets = [
        m for m in all_markets
        if _is_weather_question(m.get("question", ""))
        or _is_weather_question(m.get("_event_title", ""))
    ]
    print(f"Weather question markets: {len(weather_markets)}")

    print("\n=== PHASE 3: Match cities ===")
    discovery: dict[str, dict] = {}

    for slug, (display, country, aliases) in CITY_ALIASES.items():
        matched: list[dict] = []
        for m in weather_markets:
            q = m.get("question", "")
            title = m.get("_event_title", "")
            combined = f"{q} {title}"
            if _city_matches(combined, aliases):
                matched.append(m)

        if not matched:
            discovery[slug] = {
                "display_name": display,
                "country": country,
                "has_markets": False,
                "markets": 0,
                "types": {},
                "date_range": [None, None],
                "sample_questions": [],
            }
            print(f"  {slug:<20} → 0 markets")
            continue

        # Count by type
        types: dict[str, int] = {}
        dates: list[str] = []
        samples: list[str] = []
        for m in matched:
            mt = _parse_market_type(m.get("question", ""))
            types[mt] = types.get(mt, 0) + 1
            d = _parse_target_date(m)
            if d:
                dates.append(d)
            if len(samples) < 3:
                samples.append(m.get("question", "")[:100])

        dates.sort()
        discovery[slug] = {
            "display_name": display,
            "country": country,
            "has_markets": True,
            "markets": len(matched),
            "types": types,
            "date_range": [dates[0] if dates else None, dates[-1] if dates else None],
            "sample_questions": samples,
        }
        print(f"  {slug:<20} → {len(matched):4d} markets  types={types}  dates={dates[0] if dates else '?'}–{dates[-1] if dates else '?'}")

    if include_db:
        print("\n=== PHASE 4: Scan NULL-city rows in weather_markets.db ===")
        null_rows = scan_db_null_city(MARKET_DB)
        print(f"  NULL-city rows in DB: {len(null_rows)}")
        # Try to match these to any city
        db_unmatched: list[str] = []
        for row in null_rows:
            matched_city = None
            for slug, (_, _, aliases) in CITY_ALIASES.items():
                if _city_matches(row["question"], aliases):
                    matched_city = slug
                    break
            if matched_city:
                print(f"  DB NULL → {matched_city}: {row['question'][:80]}")
            else:
                db_unmatched.append(row["question"][:80])
        discovery["_db_null_unmatched"] = {
            "count": len(db_unmatched),
            "samples": db_unmatched[:5],
        }

    # Summary stats
    cities_with_markets = [s for s, d in discovery.items()
                           if not s.startswith("_") and d.get("has_markets")]
    print(f"\n=== SUMMARY ===")
    print(f"Cities WITH markets: {len(cities_with_markets)}")
    for slug in cities_with_markets:
        d = discovery[slug]
        print(f"  {slug:<20} {d['markets']:5d} markets  {d['date_range'][0]}–{d['date_range'][1]}")
    print(f"Cities WITHOUT markets: {21 - len(cities_with_markets)}")

    return discovery


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan Polymarket for city weather markets")
    parser.add_argument("--include-db", action="store_true",
                        help="Also scan NULL-city rows in weather_markets.db")
    parser.add_argument("--sleep", type=float, default=0.5,
                        help="Seconds between API pages (default 0.5)")
    args = parser.parse_args()

    discovery = run(include_db=args.include_db)

    DISCOVERY_JSON.parent.mkdir(parents=True, exist_ok=True)
    DISCOVERY_JSON.write_text(json.dumps(discovery, indent=2, ensure_ascii=False))
    print(f"\nSaved → {DISCOVERY_JSON}")


if __name__ == "__main__":
    main()
