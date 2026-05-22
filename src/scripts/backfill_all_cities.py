"""Multi-city weather market backfill.

Discovers Polymarket weather markets for any of the 21 supported cities,
then backfills CLOB price history and GFS/ERA5 weather data.

Usage::
    python3 -m src.scripts.backfill_all_cities --city all --start 2024-01-01
    python3 -m src.scripts.backfill_all_cities --city hong-kong --start 2026-03-01
    python3 -m src.scripts.backfill_all_cities --city "new york" --start 2026-01-01
    python3 -m src.scripts.backfill_all_cities --city all --stage gfs

Stages (run independently or together):
    all       — discover + prices + gfs (default)
    discover  — scan Polymarket for weather markets
    prices    — backfill CLOB price history
    gfs       — backfill GFS forecasts + ERA5 observed

Resumable: existing data is never deleted; DB upserts are idempotent.
Rate-limit: --sleep controls seconds between API requests (default 1.0).
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import requests

from src.data.database import (
    connect_gfs, connect_markets,
    init_gfs_db, init_weather_db,
)
from src.data.geocoding import KNOWN_LOCATIONS, Location, normalize_location_id
from src.data.gfs_history import GFSHistoryCollector
from src.data.polymarket_history import PolymarketHistoryCollector

# ---------------------------------------------------------------------------
# 21-city catalogue  (name → (display_name, country, lat, lon))
# ---------------------------------------------------------------------------

CITIES: dict[str, tuple[str, str, float, float]] = {
    "beijing":       ("Beijing",       "China",               39.90,  116.40),
    "shanghai":      ("Shanghai",      "China",               31.23,  121.47),
    "tokyo":         ("Tokyo",         "Japan",               35.68,  139.76),
    "seoul":         ("Seoul",         "South Korea",         37.57,  126.98),
    "new-york":      ("New York",      "United States",       40.71,  -74.01),
    "london":        ("London",        "United Kingdom",      51.51,   -0.13),
    "paris":         ("Paris",         "France",              48.85,    2.35),
    "berlin":        ("Berlin",        "Germany",             52.52,   13.41),
    "moscow":        ("Moscow",        "Russia",              55.75,   37.62),
    "dubai":         ("Dubai",         "United Arab Emirates",25.20,   55.27),
    "singapore":     ("Singapore",     "Singapore",            1.35,  103.82),
    "sydney":        ("Sydney",        "Australia",          -33.87,  151.21),
    "chicago":       ("Chicago",       "United States",       41.88,  -87.63),
    "los-angeles":   ("Los Angeles",   "United States",       34.05, -118.24),
    "miami":         ("Miami",         "United States",       25.76,  -80.19),
    "houston":       ("Houston",       "United States",       29.76,  -95.37),
    "phoenix":       ("Phoenix",       "United States",       33.45, -112.07),
    "las-vegas":     ("Las Vegas",     "United States",       36.17, -115.14),
    "dallas":        ("Dallas",        "United States",       32.78,  -96.80),
    "san-francisco": ("San Francisco", "United States",       37.77, -122.42),
    "hong-kong":     ("Hong Kong",     "Hong Kong",           22.32,  114.17),
}


def _city_slug_to_display(slug: str) -> str:
    """Convert 'new-york' → 'New York'."""
    return CITIES.get(slug, (slug.replace("-", " ").title(), "", 0, 0))[0]


def _resolve_city_list(city_arg: str) -> list[str]:
    """Return list of city slugs to process."""
    if city_arg.lower() == "all":
        return list(CITIES.keys())
    # Accept both slug ('new-york') and display name ('New York')
    slug = city_arg.lower().replace(" ", "-").replace("_", "-")
    if slug in CITIES:
        return [slug]
    # Try partial match
    matches = [k for k in CITIES if k.startswith(slug)]
    if matches:
        return [matches[0]]
    # Fall back — treat as a single city slug, may still work
    return [slug]


def _has_data_for_range(
    market_conn, city_display: str, start: str, end: str
) -> dict[str, bool]:
    """Quick check which stages already have data for a city/range."""
    n_markets = market_conn.execute(
        "SELECT COUNT(*) FROM markets WHERE city=? AND target_date BETWEEN ? AND ?",
        (city_display, start, end),
    ).fetchone()[0]
    n_prices = market_conn.execute(
        """SELECT COUNT(*) FROM price_history p
           JOIN markets m ON m.id = p.market_id
           WHERE m.city=? AND substr(p.timestamp,1,10) BETWEEN ? AND ?""",
        (city_display, start, end),
    ).fetchone()[0]
    return {"markets": n_markets > 0, "prices": n_prices > 0}


# ---------------------------------------------------------------------------
# Per-stage helpers
# ---------------------------------------------------------------------------

def discover_city(
    city_slug: str,
    start: str,
    end: str,
    sleep: float = 1.0,
    dry_run: bool = False,
) -> int:
    """Scan Polymarket for weather markets mentioning this city."""
    city_display = _city_slug_to_display(city_slug)
    print(f"  [discover] Searching for '{city_display}' weather markets …")

    if dry_run:
        print(f"  [discover] DRY-RUN — skipping API calls")
        return 0

    collector = PolymarketHistoryCollector()
    try:
        # Search events with city name as keyword query
        n = collector.collect_weather_markets(
            max_pages=20,
            page_size=100,
            include_closed=True,
            include_open=True,
            geocode=True,
            sleep_seconds=sleep,
            events_only=True,
            event_query=city_display,
        )
        print(f"  [discover] Found/updated {n} markets for {city_display}")
        return n
    finally:
        collector.close()


def backfill_prices(
    city_slug: str,
    start: str,
    end: str,
    sleep: float = 1.0,
    dry_run: bool = False,
) -> int:
    """Backfill CLOB price history for a city's markets."""
    city_display = _city_slug_to_display(city_slug)
    print(f"  [prices] Backfilling CLOB prices for '{city_display}' {start}→{end} …")

    if dry_run:
        print(f"  [prices] DRY-RUN — skipping")
        return 0

    market_conn = connect_markets()
    market_conn.row_factory = __import__("sqlite3").Row
    init_weather_db(market_conn)

    markets = market_conn.execute(
        """
        SELECT id, clob_token_ids FROM markets
        WHERE city = ?
          AND target_date BETWEEN ? AND ?
          AND market_type IN ('temp_above','precip','snow')
        """,
        (city_display, start, end),
    ).fetchall()
    market_conn.close()

    if not markets:
        print(f"  [prices] No markets in DB for {city_display} in range — run discover first")
        return 0

    CLOB_PRICES_HISTORY_URL = "https://clob.polymarket.com/prices-history"
    session = requests.Session()
    collector = PolymarketHistoryCollector()
    total = 0

    try:
        for mkt in markets:
            token_ids = mkt["clob_token_ids"]
            if isinstance(token_ids, str):
                try:
                    token_ids = json.loads(token_ids)
                except Exception:
                    continue
            if not token_ids:
                continue

            yes_token = token_ids[0]
            start_ts = int(datetime.fromisoformat(start).timestamp())
            end_ts   = int(datetime.fromisoformat(end).timestamp())

            # Fetch in 14-day windows
            cursor = start_ts
            WINDOW = 14 * 86400
            while cursor < end_ts:
                chunk_end = min(cursor + WINDOW, end_ts)
                try:
                    resp = session.get(
                        CLOB_PRICES_HISTORY_URL,
                        params={
                            "market": yes_token,
                            "startTs": cursor,
                            "endTs": chunk_end,
                            "fidelity": 1440,
                        },
                        timeout=20,
                    )
                    if resp.status_code == 200:
                        history = resp.json().get("history", [])
                        if history:
                            collector.ingest_price_history(mkt["id"], yes_token, history)
                            total += len(history)
                    time.sleep(sleep)
                except Exception as exc:
                    print(f"    [prices] WARN: {mkt['id']}: {exc}")
                cursor = chunk_end

        print(f"  [prices] Ingested {total} price rows for {city_display}")
        return total
    finally:
        collector.close()


def backfill_gfs(
    city_slug: str,
    start: str,
    end: str,
    sleep: float = 1.0,
    dry_run: bool = False,
) -> dict[str, int]:
    """Backfill GFS forecasts + ERA5 observed weather for a city."""
    info = CITIES.get(city_slug)
    if info is None:
        print(f"  [gfs] Unknown city slug '{city_slug}' — skipping")
        return {}

    city_display, country, lat, lon = info
    location_id = normalize_location_id(city_display, country)
    print(f"  [gfs] Backfilling GFS+ERA5 for '{city_display}' ({lat},{lon}) {start}→{end} …")

    if dry_run:
        print(f"  [gfs] DRY-RUN — skipping")
        return {}

    location = Location(
        id=location_id,
        name=city_display,
        country=country,
        latitude=lat,
        longitude=lon,
    )
    collector = GFSHistoryCollector()
    try:
        collector.upsert_location(location)
        stats = collector.backfill_from_markets(
            start_date=start,
            end_date=end,
            sleep_seconds=sleep,
        )
        print(
            f"  [gfs] Done. forecasts={stats.get('forecasts_inserted',0)}, "
            f"observed={stats.get('observed_inserted',0)}"
        )
        return stats
    finally:
        collector.close()


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def run_city(
    city_slug: str,
    start: str,
    end: str,
    stages: list[str],
    sleep: float = 1.0,
    dry_run: bool = False,
    skip_existing: bool = True,
) -> dict:
    city_display = _city_slug_to_display(city_slug)
    print(f"\n{'='*60}")
    print(f"  City: {city_display}  ({city_slug})")
    print(f"  Range: {start} → {end}  stages={stages}")
    print(f"{'='*60}")

    summary: dict = {"city": city_display, "city_slug": city_slug}

    if skip_existing:
        market_conn = connect_markets()
        init_weather_db(market_conn)
        existing = _has_data_for_range(market_conn, city_display, start, end)
        market_conn.close()
    else:
        existing = {"markets": False, "prices": False}

    if "discover" in stages or "all" in stages:
        if skip_existing and existing["markets"]:
            print(f"  [discover] Already have markets — skipping (use --no-skip to force)")
        else:
            summary["discover"] = discover_city(city_slug, start, end, sleep, dry_run)

    if "prices" in stages or "all" in stages:
        if skip_existing and existing["prices"]:
            print(f"  [prices] Already have prices — skipping (use --no-skip to force)")
        else:
            summary["prices"] = backfill_prices(city_slug, start, end, sleep, dry_run)

    if "gfs" in stages or "all" in stages:
        summary["gfs"] = backfill_gfs(city_slug, start, end, sleep, dry_run)

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Multi-city Polymarket weather backfill."
    )
    parser.add_argument(
        "--city", default="all",
        help="City slug (e.g. hong-kong, new-york) or 'all'. "
             "Use 'all' to process all 21 cities.",
    )
    parser.add_argument(
        "--start", default="2026-01-01",
        help="Start date YYYY-MM-DD (default: 2026-01-01)",
    )
    parser.add_argument(
        "--end", default=None,
        help="End date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--stage", default="all",
        choices=["all", "discover", "prices", "gfs"],
        help="Which stages to run (default: all)",
    )
    parser.add_argument(
        "--sleep", type=float, default=1.0,
        help="Seconds to wait between API requests (default: 1.0)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen without making API calls",
    )
    parser.add_argument(
        "--no-skip", action="store_true",
        help="Re-run stages even if data already exists",
    )
    args = parser.parse_args()

    end = args.end or date.today().isoformat()
    stages = [args.stage]
    city_list = _resolve_city_list(args.city)

    print(f"Backfilling {len(city_list)} cities: {city_list}")
    print(f"Date range: {args.start} → {end}")
    print(f"Stages: {stages}  sleep={args.sleep}s  dry_run={args.dry_run}")

    all_summaries = []
    for slug in city_list:
        summary = run_city(
            slug,
            start=args.start,
            end=end,
            stages=stages,
            sleep=args.sleep,
            dry_run=args.dry_run,
            skip_existing=not args.no_skip,
        )
        all_summaries.append(summary)

    print(f"\n{'='*60}")
    print(f"BACKFILL COMPLETE — {len(all_summaries)} cities processed")
    print(f"{'='*60}")
    for s in all_summaries:
        city_name = s.get("city", s.get("city_slug", "?"))
        parts = []
        if "discover" in s:
            parts.append(f"discover={s['discover']}")
        if "prices" in s:
            parts.append(f"prices={s['prices']}")
        if "gfs" in s:
            g = s["gfs"]
            if isinstance(g, dict):
                parts.append(
                    f"gfs_forecasts={g.get('forecasts_inserted',0)} "
                    f"era5={g.get('observed_inserted',0)}"
                )
        print(f"  {city_name:<20} {' | '.join(parts) or 'skipped'}")


if __name__ == "__main__":
    main()
