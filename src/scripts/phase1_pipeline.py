"""Phase 1 pipeline — Tasks 2-4.

Orchestrates:
  1. Market ingestion for each discovered city (writes to weather_markets.db)
  2. GFS + ERA5 backfill using backfill_batch() (efficient: 1 call/location/var)
  3. Multi-city backtest with run_standard()
  4. Summary JSON + per-city CSVs

Usage:
    python3 -m src.scripts.phase1_pipeline --stage all
    python3 -m src.scripts.phase1_pipeline --stage ingest
    python3 -m src.scripts.phase1_pipeline --stage gfs
    python3 -m src.scripts.phase1_pipeline --stage backtest
    python3 -m src.scripts.phase1_pipeline --city new-york --stage gfs
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent.parent.parent
DISCOVERY_JSON = PROJECT_ROOT / "data" / "city_discovery.json"
MARKET_DB = PROJECT_ROOT / "data" / "weather_markets.db"
GFS_DB = PROJECT_ROOT / "data" / "gfs_forecasts.db"
RESULTS_DIR = PROJECT_ROOT / "data" / "backtest_results"
SUMMARY_JSON = RESULTS_DIR / "summary.json"

# Backfill start dates per city (based on earliest known market data)
BACKFILL_STARTS: dict[str, str] = {
    "new-york":      "2024-12-01",
    "london":        "2025-01-01",
    "hong-kong":     "2024-04-01",
    "seoul":         "2025-12-01",
    "dallas":        "2025-12-01",
    "miami":         "2026-01-01",
    "paris":         "2026-01-01",
    "chicago":       "2026-01-01",
    "tokyo":         "2026-03-01",
    "shanghai":      "2026-03-01",
    "singapore":     "2026-03-01",
    # All other cities: start with ~3 weeks buffer before first market
    "_default":      "2026-03-15",
}

# City slug → (display_name, country)
CITY_META: dict[str, tuple[str, str]] = {
    "beijing":       ("Beijing",       "China"),
    "shanghai":      ("Shanghai",      "China"),
    "tokyo":         ("Tokyo",         "Japan"),
    "seoul":         ("Seoul",         "South Korea"),
    "new-york":      ("New York",      "United States"),
    "london":        ("London",        "United Kingdom"),
    "paris":         ("Paris",         "France"),
    "berlin":        ("Berlin",        "Germany"),
    "moscow":        ("Moscow",        "Russia"),
    "dubai":         ("Dubai",         "United Arab Emirates"),
    "singapore":     ("Singapore",     "Singapore"),
    "sydney":        ("Sydney",        "Australia"),
    "chicago":       ("Chicago",       "United States"),
    "los-angeles":   ("Los Angeles",   "United States"),
    "miami":         ("Miami",         "United States"),
    "houston":       ("Houston",       "United States"),
    "phoenix":       ("Phoenix",       "United States"),
    "las-vegas":     ("Las Vegas",     "United States"),
    "dallas":        ("Dallas",        "United States"),
    "san-francisco": ("San Francisco", "United States"),
    "hong-kong":     ("Hong Kong",     "Hong Kong"),
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_discovery() -> dict:
    if not DISCOVERY_JSON.exists():
        raise FileNotFoundError(
            f"city_discovery.json not found at {DISCOVERY_JSON}. "
            "Run: python3 -m src.scripts.city_discovery"
        )
    return json.loads(DISCOVERY_JSON.read_text())


def _cities_with_markets(discovery: dict) -> list[str]:
    return [
        slug for slug, info in discovery.items()
        if not slug.startswith("_") and info.get("has_markets")
    ]


def _backfill_start(slug: str) -> str:
    return BACKFILL_STARTS.get(slug, BACKFILL_STARTS["_default"])


def _db_market_count(city_display: str) -> int:
    conn = sqlite3.connect(str(MARKET_DB))
    n = conn.execute(
        "SELECT COUNT(*) FROM markets WHERE lower(city) = lower(?)",
        (city_display,)
    ).fetchone()[0]
    conn.close()
    return n


def _db_resolved_count(city_display: str) -> int:
    conn = sqlite3.connect(str(MARKET_DB))
    n = conn.execute(
        "SELECT COUNT(*) FROM markets WHERE lower(city) = lower(?) "
        "AND resolved_outcome IS NOT NULL",
        (city_display,)
    ).fetchone()[0]
    conn.close()
    return n


def _gfs_count_for_city(location_id: str) -> tuple[int, int]:
    """Return (forecast_rows, observed_rows) for a location."""
    conn = sqlite3.connect(str(GFS_DB))
    f = conn.execute(
        "SELECT COUNT(*) FROM gfs_forecasts WHERE location_id = ?",
        (location_id,)
    ).fetchone()[0]
    o = conn.execute(
        "SELECT COUNT(*) FROM observed_weather WHERE location_id = ?",
        (location_id,)
    ).fetchone()[0]
    conn.close()
    return f, o


def _slug_to_location_id(slug: str) -> str:
    """Convert 'new-york' → 'new_york_new_york' via normalize_location_id."""
    from src.data.geocoding import normalize_location_id
    if slug not in CITY_META:
        return slug.replace("-", "_")
    display, country = CITY_META[slug]
    return normalize_location_id(display, country)


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: Market Ingestion
# ─────────────────────────────────────────────────────────────────────────────

def stage_ingest(city_slugs: list[str], sleep: float = 0.3) -> dict[str, int]:
    """Ingest all weather markets for each city into weather_markets.db."""
    from src.data.polymarket_history import PolymarketHistoryCollector

    print(f"\n{'='*60}")
    print(f"STAGE: MARKET INGESTION  ({len(city_slugs)} cities)")
    print(f"{'='*60}")

    results: dict[str, int] = {}

    for slug in city_slugs:
        if slug not in CITY_META:
            print(f"  [{slug}] Unknown city — skipping")
            continue

        display, country = CITY_META[slug]
        pre_count = _db_market_count(display)
        print(f"\n  [{slug}] Pre-existing: {pre_count} markets in DB")
        print(f"  [{slug}] Ingesting '{display}' markets from Polymarket …")

        collector = PolymarketHistoryCollector()
        try:
            n = collector.collect_weather_markets(
                max_pages=50,
                page_size=500,
                include_closed=True,
                include_open=True,
                geocode=True,
                sleep_seconds=sleep,
                events_only=True,
                event_query=display,
            )
            collector.conn.commit()
        finally:
            collector.close()

        post_count = _db_market_count(display)
        new = post_count - pre_count
        print(f"  [{slug}] Ingested: {n} API events matched, {new} new DB rows (total: {post_count})")
        results[slug] = post_count

        time.sleep(sleep)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1.5: Per-market price history (final 3-day window)
# ─────────────────────────────────────────────────────────────────────────────

def stage_price_history(
    city_slugs: list[str],
    sleep: float = 0.15,
    days_before: int = 3,
    skip_if_exists: bool = True,
) -> dict[str, int]:
    """Fetch the final `days_before` days of price history for every resolved market.

    Uses a single CLOB API call per market (the 3-day window is well within the
    14-day CLOB max window), giving us a "day-before-resolution" price snapshot
    that the backtester can use for edge calculations.

    Args:
        city_slugs: only process these cities
        sleep: seconds between API calls
        days_before: how many days before target_date to start fetching
        skip_if_exists: skip markets that already have price_history rows
    """
    from datetime import datetime, timedelta, timezone
    import requests as _requests
    from src.data.polymarket_history import PolymarketHistoryCollector

    CLOB_URL = "https://clob.polymarket.com/prices-history"
    CLOB_MAX_DAYS = 14  # API limit

    results: dict[str, int] = {}

    # Build city display-name set
    target_displays = {
        CITY_META[s][0].lower()
        for s in city_slugs
        if s in CITY_META
    }

    print(f"\n{'='*60}")
    print(f"STAGE: PRICE HISTORY (days_before={days_before})")
    print(f"{'='*60}")

    collector = PolymarketHistoryCollector()
    session = collector.session

    # Find all resolved markets with CLOB token IDs for our target cities
    placeholders = ",".join("?" for _ in target_displays)
    rows = collector.conn.execute(
        f"""
        SELECT m.id, m.city, m.target_date, m.clob_token_ids
        FROM markets m
        WHERE lower(m.city) IN ({placeholders})
          AND m.clob_token_ids IS NOT NULL
          AND m.clob_token_ids != '[]'
          AND m.resolved_outcome IS NOT NULL
          AND m.target_date IS NOT NULL
        ORDER BY m.city, m.target_date
        """,
        list(target_displays),
    ).fetchall()

    if skip_if_exists:
        # Get set of market_ids already in price_history
        existing = set(
            r[0] for r in collector.conn.execute(
                "SELECT DISTINCT market_id FROM price_history"
            ).fetchall()
        )
        rows = [r for r in rows if r[0] not in existing]

    print(f"  Markets to process: {len(rows)}")
    total_written = 0

    city_counts: dict[str, int] = {}
    for row in rows:
        market_id = row["id"]
        city = row["city"]
        target_date_str = row["target_date"]
        import json as _json
        token_ids = _json.loads(row["clob_token_ids"]) if isinstance(row["clob_token_ids"], str) else row["clob_token_ids"]
        if not token_ids:
            continue
        yes_token = str(token_ids[0])

        # Price window: [target_date - days_before, target_date]
        target = datetime.fromisoformat(target_date_str)
        price_start = (target - timedelta(days=days_before)).date().isoformat()
        price_end = target.date().isoformat()

        # One API call covers the whole window (< 14 days)
        start_ts = int(datetime.fromisoformat(price_start).replace(tzinfo=timezone.utc).timestamp())
        end_ts = int((datetime.fromisoformat(price_end).replace(tzinfo=timezone.utc) + timedelta(days=1)).timestamp())

        try:
            resp = session.get(
                CLOB_URL,
                params={
                    "market": yes_token,
                    "startTs": start_ts,
                    "endTs": end_ts,
                    "fidelity": 1440,
                },
                timeout=30,
            )
            if resp.status_code != 200:
                time.sleep(sleep)
                continue
            history = resp.json().get("history", [])
        except Exception:
            time.sleep(sleep)
            continue

        points = []
        for item in history:
            ts = datetime.fromtimestamp(item["t"], tz=timezone.utc).isoformat()
            points.append((market_id, yes_token, ts, float(item["p"]), 1440))

        if points:
            collector.conn.executemany(
                """INSERT OR IGNORE INTO price_history
                   (market_id, token_id, timestamp, price, fidelity_minutes)
                   VALUES (?, ?, ?, ?, ?)""",
                points,
            )
            written = len(points)
            total_written += written
            city_counts[city] = city_counts.get(city, 0) + written

        time.sleep(sleep)

    collector.conn.commit()
    collector.close()

    print(f"  Total price points written: {total_written}")
    for city_disp, cnt in sorted(city_counts.items(), key=lambda x: -x[1]):
        results[city_disp.lower()] = cnt
        print(f"    {city_disp:<20} {cnt:>6} price points")

    return results

def stage_gfs_backfill(
    city_slugs: list[str],
    end_date: str,
    sleep: float = 0.5,
) -> dict[str, dict]:
    """Batch-backfill GFS forecasts + ERA5 for all ingested cities.

    Calls backfill_batch() ONCE for each date-range bucket rather than once
    per city, since backfill_batch() already iterates all locations from the DB.
    """
    from src.data.gfs_history import GFSHistoryCollector
    from src.data.geocoding import normalize_location_id, KNOWN_LOCATIONS
    from src.data.geocoding import Location
    import sqlite3 as _sqlite3

    print(f"\n{'='*60}")
    print(f"STAGE: GFS + ERA5 BACKFILL  ({len(city_slugs)} cities)")
    print(f"{'='*60}")

    # First: upsert locations for any city that isn't yet in gfs_forecasts.db
    collector = GFSHistoryCollector()
    try:
        conn = _sqlite3.connect(str(MARKET_DB))
        conn.row_factory = _sqlite3.Row
        for slug in city_slugs:
            if slug not in CITY_META:
                continue
            display, country = CITY_META[slug]
            row = conn.execute(
                "SELECT latitude, longitude FROM markets "
                "WHERE lower(city)=lower(?) AND latitude IS NOT NULL LIMIT 1",
                (display,)
            ).fetchone()
            if row:
                lat, lon = float(row["latitude"]), float(row["longitude"])
            else:
                key = display.lower()
                info = KNOWN_LOCATIONS.get(key)
                if info is None:
                    print(f"  [{slug}] No lat/lon — skipping")
                    continue
                lat, lon = float(info[2]), float(info[3])
            location_id = normalize_location_id(display, country)
            loc = Location(id=location_id, name=display, country=country,
                           latitude=lat, longitude=lon)
            collector.upsert_location(loc)
        collector.gfs_conn.commit()
        conn.close()
    finally:
        pass  # keep collector open for batch calls below

    # Use the BROADEST start date across all requested cities so we make ONE
    # batch call rather than one per city.  backfill_batch() is idempotent
    # (INSERT OR IGNORE) so we'll just skip dates that already exist.
    start_dates = [_backfill_start(s) for s in city_slugs if s in CITY_META]
    if not start_dates:
        collector.close()
        return {}
    global_start = min(start_dates)

    # DB total before
    gfs_before = collector.gfs_conn.execute("SELECT COUNT(*) FROM gfs_forecasts").fetchone()[0]
    era5_before = collector.gfs_conn.execute("SELECT COUNT(*) FROM observed_weather").fetchone()[0]
    print(f"\n  Batch range: {global_start} → {end_date}")
    print(f"  Pre-existing: {gfs_before} GFS rows, {era5_before} ERA5 rows")
    print(f"  Running backfill_batch() for all {len(city_slugs)} cities in one pass …")

    try:
        stats = collector.backfill_batch(
            start_date=global_start,
            end_date=end_date,
            sleep_seconds=sleep,
        )
    finally:
        collector.close()

    gfs_after = sqlite3.connect(str(GFS_DB)).execute("SELECT COUNT(*) FROM gfs_forecasts").fetchone()[0]
    era5_after = sqlite3.connect(str(GFS_DB)).execute("SELECT COUNT(*) FROM observed_weather").fetchone()[0]

    print(
        f"  Done: +{stats.get('forecasts', 0)} GFS, +{stats.get('observed', 0)} ERA5  "
        f"(total: {gfs_after} GFS, {era5_after} ERA5)"
    )

    # Build per-city result dict
    results: dict[str, dict] = {}
    for slug in city_slugs:
        if slug not in CITY_META:
            continue
        display, country = CITY_META[slug]
        location_id = normalize_location_id(display, country)
        f, o = _gfs_count_for_city(location_id)
        results[slug] = {
            "start_date": _backfill_start(slug),
            "end_date": end_date,
            "total_forecasts": f,
            "total_observed": o,
        }

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Stage 3: Multi-city backtest
# ─────────────────────────────────────────────────────────────────────────────

def stage_backtest(
    city_slugs: list[str],
    end_date: str,
    min_resolved: int = 10,
    min_edge: float = 0.10,
    amount: float = 5.0,
) -> dict[str, dict]:
    """Run run_standard() for each city with sufficient data."""
    from src.data.weather_backtester import WeatherBacktester
    from src.data.gfs_prediction import GFSPredictionSource

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"STAGE: MULTI-CITY BACKTEST  ({len(city_slugs)} cities)")
    print(f"{'='*60}")

    summary: dict[str, dict] = {}

    for slug in city_slugs:
        if slug not in CITY_META:
            continue

        display, country = CITY_META[slug]
        start_date = _backfill_start(slug)

        resolved = _db_resolved_count(display)
        n_markets = _db_market_count(display)
        location_id = _slug_to_location_id(slug)
        gfs_f, gfs_o = _gfs_count_for_city(location_id)

        print(f"\n  [{slug}] '{display}'  markets={n_markets}  resolved={resolved}  gfs_forecasts={gfs_f}  era5={gfs_o}")

        if resolved < min_resolved:
            print(f"  [{slug}] SKIP: only {resolved} resolved markets (need ≥{min_resolved})")
            summary[slug] = {
                "display_name": display,
                "markets": n_markets,
                "resolved": resolved,
                "skipped": True,
                "skip_reason": f"resolved_markets={resolved} < {min_resolved}",
                "gfs_forecasts": gfs_f,
                "era5_observed": gfs_o,
            }
            continue

        if gfs_f == 0:
            print(f"  [{slug}] SKIP: no GFS forecasts in DB")
            summary[slug] = {
                "display_name": display,
                "markets": n_markets,
                "resolved": resolved,
                "skipped": True,
                "skip_reason": "no_gfs_data",
                "gfs_forecasts": 0,
                "era5_observed": gfs_o,
            }
            continue

        print(f"  [{slug}] Running backtest {start_date}→{end_date} …")
        bt = WeatherBacktester()
        source = GFSPredictionSource(mode="historical")
        try:
            trades = bt.run_standard(
                start=start_date,
                end=end_date,
                city=display,
                min_edge=min_edge,
                amount=amount,
                max_lead_time_hours=48,
                min_price=0.03,
                prediction_source=source,
            )
        finally:
            source.close()
            bt.close()

        if not trades:
            print(f"  [{slug}] 0 trades — no signals generated")
            summary[slug] = {
                "display_name": display,
                "markets": n_markets,
                "resolved": resolved,
                "skipped": False,
                "trades": 0,
                "gfs_forecasts": gfs_f,
                "era5_observed": gfs_o,
            }
            continue

        # Compute stats
        resolved_trades = [t for t in trades if t.get("actual_outcome") is not None]
        wins = sum(1 for t in resolved_trades if t.get("pnl", 0) > 0)
        total_pnl = sum(t.get("pnl", 0) for t in resolved_trades)
        invested = amount * len(resolved_trades)
        roi = total_pnl / invested if invested > 0 else 0.0
        win_rate = wins / len(resolved_trades) if resolved_trades else 0.0
        avg_edge = sum(abs(t.get("edge", 0)) for t in trades) / len(trades) if trades else 0.0

        # Calibration info (from first trade)
        first = trades[0] if trades else {}
        bias = first.get("calib_bias", 0.0)
        sigma = first.get("calib_sigma", 0.0)
        calib_n = first.get("calib_n", 0)

        # Sharpe: PnL per trade / std(PnL per trade) × sqrt(252/avg_hold_days)
        pnls = [t.get("pnl", 0) for t in resolved_trades]
        if len(pnls) > 1:
            mean_p = sum(pnls) / len(pnls)
            var = sum((p - mean_p) ** 2 for p in pnls) / len(pnls)
            std = math.sqrt(var) if var > 0 else 1e-9
            sharpe = (mean_p / std) * math.sqrt(252) if std > 0 else 0.0
        else:
            sharpe = 0.0

        print(
            f"  [{slug}] trades={len(trades)}  resolved={len(resolved_trades)}  "
            f"wins={wins}  win_rate={win_rate:.1%}  roi={roi:+.1%}  sharpe={sharpe:.2f}"
        )

        # Write per-city CSV
        csv_path = RESULTS_DIR / f"{slug}_backtest.csv"
        if trades:
            with csv_path.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
                writer.writeheader()
                writer.writerows(trades)
            print(f"  [{slug}] CSV → {csv_path.name}")

        summary[slug] = {
            "display_name": display,
            "markets": n_markets,
            "resolved": resolved,
            "skipped": False,
            "trades": len(trades),
            "resolved_trades": len(resolved_trades),
            "wins": wins,
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 2),
            "invested": round(invested, 2),
            "roi": round(roi, 4),
            "avg_edge": round(avg_edge, 4),
            "sharpe": round(sharpe, 3),
            "gfs_forecasts": gfs_f,
            "era5_observed": gfs_o,
            "start_date": start_date,
            "end_date": end_date,
            "bias_correction": {"temperature_2m_max": round(bias, 4)},
            "sigma": {"temperature_2m_max": round(sigma, 4)},
            "calib_n": calib_n,
        }

    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Stage 4: Summary report
# ─────────────────────────────────────────────────────────────────────────────

def stage_report(
    city_summary: dict[str, dict],
    discovery: dict,
    ingest_results: Optional[dict] = None,
    gfs_results: Optional[dict] = None,
) -> None:
    """Write summary.json."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # DB totals
    conn = sqlite3.connect(str(GFS_DB))
    total_gfs = conn.execute("SELECT COUNT(*) FROM gfs_forecasts").fetchone()[0]
    total_era5 = conn.execute("SELECT COUNT(*) FROM observed_weather").fetchone()[0]
    conn.close()

    backtestable = sum(
        1 for v in city_summary.values()
        if not v.get("skipped") and v.get("trades", 0) > 0
    )

    report = {
        "generated": date.today().isoformat(),
        "db_stats": {
            "total_gfs_forecasts": total_gfs,
            "total_era5_observed": total_era5,
        },
        "total_cities_with_markets": sum(
            1 for s, d in discovery.items()
            if not s.startswith("_") and isinstance(d, dict) and d.get("has_markets")
        ),
        "total_cities_backtestable": backtestable,
        "cities": city_summary,
    }

    SUMMARY_JSON.write_text(json.dumps(report, indent=2))
    print(f"\n{'='*60}")
    print(f"SUMMARY REPORT → {SUMMARY_JSON}")
    print(f"{'='*60}")
    print(f"  DB:  {total_gfs} GFS forecasts,  {total_era5} ERA5 observed")
    print(f"  Cities with markets: {report['total_cities_with_markets']}")
    print(f"  Backtestable cities: {backtestable}")

    # Top 5 by ROI
    ranked = sorted(
        [(slug, d) for slug, d in city_summary.items() if not d.get("skipped") and d.get("trades", 0) > 0],
        key=lambda x: x[1].get("roi", 0),
        reverse=True,
    )[:5]
    if ranked:
        print(f"\n  Top cities by ROI:")
        print(f"  {'City':<20} {'Trades':>7} {'Win%':>7} {'ROI':>8} {'Sharpe':>7}")
        print(f"  {'-'*55}")
        for slug, d in ranked:
            print(
                f"  {slug:<20} {d.get('trades', 0):>7} "
                f"{d.get('win_rate', 0):>6.1%} "
                f"{d.get('roi', 0):>+7.1%} "
                f"{d.get('sharpe', 0):>7.2f}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 1 pipeline — ingest, backfill, backtest")
    parser.add_argument("--stage", default="all",
                        choices=["all", "ingest", "price", "gfs", "backtest", "report"],
                        help="Which stage to run (default: all)")
    parser.add_argument("--city", default=None,
                        help="Restrict to a single city slug")
    parser.add_argument("--end", default=date.today().isoformat(),
                        help="End date for backfill/backtest (default: today)")
    parser.add_argument("--sleep", type=float, default=0.5,
                        help="Sleep between API calls (default: 0.5)")
    parser.add_argument("--min-resolved", type=int, default=10,
                        help="Min resolved markets to run backtest (default: 10)")
    parser.add_argument("--skip-existing-gfs", action="store_true",
                        help="Skip GFS backfill if city already has data")
    args = parser.parse_args()

    discovery = _load_discovery()
    all_cities = _cities_with_markets(discovery)

    if args.city:
        slug_filter = args.city.lower()
        city_slugs = [s for s in all_cities if s == slug_filter]
        if not city_slugs:
            # Try to run for a city not in discovery (e.g. if we know it has markets)
            city_slugs = [slug_filter]
        print(f"City filter: {city_slugs}")
    else:
        city_slugs = all_cities

    print(f"Processing {len(city_slugs)} cities: {city_slugs}")
    print(f"End date: {args.end}")

    ingest_results: dict[str, int] = {}
    gfs_results: dict[str, dict] = {}
    city_summary: dict[str, dict] = {}

    run_all = args.stage == "all"

    if run_all or args.stage == "ingest":
        print("\n>>> STAGE: INGEST")
        ingest_results = stage_ingest(city_slugs, sleep=args.sleep)

    if run_all or args.stage == "price":
        print("\n>>> STAGE: PRICE HISTORY")
        stage_price_history(city_slugs, sleep=0.12, days_before=3)

    if run_all or args.stage == "gfs":
        print("\n>>> STAGE: GFS BACKFILL")
        gfs_cities = city_slugs
        if args.skip_existing_gfs:
            gfs_cities = []
            for slug in city_slugs:
                lid = _slug_to_location_id(slug)
                f, _ = _gfs_count_for_city(lid)
                if f > 0:
                    print(f"  [{slug}] Skipping GFS (already has {f} rows)")
                else:
                    gfs_cities.append(slug)
        gfs_results = stage_gfs_backfill(gfs_cities, end_date=args.end, sleep=args.sleep)

    if run_all or args.stage == "backtest":
        print("\n>>> STAGE: BACKTEST")
        city_summary = stage_backtest(
            city_slugs,
            end_date=args.end,
            min_resolved=args.min_resolved,
        )

    if run_all or args.stage == "report":
        # Build a minimal city_summary if we skipped the backtest stage
        if not city_summary:
            for slug in city_slugs:
                if slug not in CITY_META:
                    continue
                display, _ = CITY_META[slug]
                lid = _slug_to_location_id(slug)
                f, o = _gfs_count_for_city(lid)
                r = _db_resolved_count(display)
                city_summary[slug] = {
                    "display_name": display,
                    "markets": _db_market_count(display),
                    "resolved": r,
                    "gfs_forecasts": f,
                    "era5_observed": o,
                    "skipped": True,
                    "skip_reason": "stage=report only",
                }
        stage_report(city_summary, discovery, ingest_results, gfs_results)


if __name__ == "__main__":
    main()
