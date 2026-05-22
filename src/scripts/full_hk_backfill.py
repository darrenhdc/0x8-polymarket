"""Full backfill pipeline for Hong Kong Polymarket weather research.

Pipeline stages (run in order):
  1. discover  — Find all HK temperature/precipitation events by slug
  2. prices    — Backfill CLOB price history for all discovered markets
  3. gfs       — Batch-fetch GFS forecasts + ERA5 observed for all HK dates

Usage (full run):
    python -m src.scripts.full_hk_backfill

Usage (individual stages):
    python -m src.scripts.full_hk_backfill --stage discover
    python -m src.scripts.full_hk_backfill --stage prices
    python -m src.scripts.full_hk_backfill --stage gfs

Usage (custom date range):
    python -m src.scripts.full_hk_backfill --start 2026-03-01 --end 2026-06-30
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

from src.data.database import init_all
from src.data.gfs_history import GFSHistoryCollector
from src.data.polymarket_history import PolymarketHistoryCollector


# HK temperature markets started around March 13, 2026.
# Scan from a few days before to be safe.
DEFAULT_START = "2026-03-10"


def parse_args() -> argparse.Namespace:
    today = date.today().isoformat()
    # Look 7 days ahead for future events that may already be listed
    look_ahead = (date.today() + timedelta(days=7)).isoformat()
    parser = argparse.ArgumentParser(description="Full HK weather backfill pipeline.")
    parser.add_argument("--start", default=DEFAULT_START, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=look_ahead, help="End date YYYY-MM-DD (default: today+7)")
    parser.add_argument(
        "--stage",
        choices=["all", "discover", "prices", "gfs"],
        default="all",
        help="Which pipeline stage to run (default: all)",
    )
    parser.add_argument("--sleep", type=float, default=0.08, help="Seconds between API calls")
    parser.add_argument("--dry-run", action="store_true", help="Print plan but do not write to DB")
    return parser.parse_args()


def stage_discover(start: str, end: str, sleep: float, dry_run: bool) -> None:
    print(f"\n[discover] Scanning HK weather event slugs: {start} → {end}")
    if dry_run:
        # Count how many slug calls would be made
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
        days = (end_d - start_d).days + 1
        # Months for precipitation
        months: set[tuple[int, int]] = set()
        current = start_d
        while current <= end_d:
            months.add((current.year, current.month))
            current += timedelta(days=1)
        print(f"  [dry-run] Would query {days} temperature slugs + {len(months)} precipitation slugs")
        return

    poly = PolymarketHistoryCollector()
    try:
        count = poly.discover_hk_weather_by_slug(
            start_date=start,
            end_date=end,
            sleep_seconds=sleep,
            include_precip=True,
        )
        print(f"  [discover] upserted {count} market rows")
    finally:
        poly.close()


def stage_prices(start: str, end: str, sleep: float, dry_run: bool) -> None:
    print(f"\n[prices] Backfilling CLOB price history: {start} → {end}")
    if dry_run:
        from src.data.database import connect_markets
        conn = connect_markets()
        n = conn.execute("SELECT COUNT(*) FROM markets WHERE clob_token_ids IS NOT NULL AND clob_token_ids != '[]'").fetchone()[0]
        conn.close()
        print(f"  [dry-run] Would fetch prices for {n} markets")
        return

    poly = PolymarketHistoryCollector()
    try:
        count = poly.backfill_price_history(
            start_date=start,
            end_date=end,
            fidelity_minutes=1440,  # daily fidelity
        )
        print(f"  [prices] inserted {count} price rows")
    finally:
        poly.close()


def stage_gfs(start: str, end: str, sleep: float, dry_run: bool) -> None:
    print(f"\n[gfs] Batch-fetching GFS forecasts + ERA5 observed: {start} → {end}")
    if dry_run:
        from src.data.database import connect_markets
        conn = connect_markets()
        locs = conn.execute(
            "SELECT COUNT(DISTINCT city) FROM markets WHERE city IS NOT NULL AND latitude IS NOT NULL"
        ).fetchone()[0]
        conn.close()
        print(f"  [dry-run] Would fetch 2 variables × {locs} locations (temp + precip)")
        return

    gfs = GFSHistoryCollector()
    try:
        counts = gfs.backfill_batch(
            start_date=start,
            end_date=end,
            sleep_seconds=sleep,
        )
        print(f"  [gfs] inserted forecasts={counts['forecasts']}, observed={counts['observed']}")
    finally:
        gfs.close()


def print_summary() -> None:
    import sqlite3
    from src.data.database import WEATHER_MARKETS_DB, GFS_FORECASTS_DB

    print("\n" + "=" * 60)
    print("Database summary after backfill")
    print("=" * 60)

    conn1 = sqlite3.connect(WEATHER_MARKETS_DB)
    conn1.row_factory = sqlite3.Row
    total_markets = conn1.execute("SELECT COUNT(*) FROM markets WHERE city IS NOT NULL").fetchone()[0]
    total_prices = conn1.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    resolved = conn1.execute(
        "SELECT COUNT(*) FROM markets WHERE resolved_outcome IS NOT NULL AND resolved_outcome != ''"
    ).fetchone()[0]
    print(f"  markets (with city):  {total_markets}")
    print(f"  resolved markets:     {resolved}")
    print(f"  price_history rows:   {total_prices}")
    conn1.close()

    conn2 = sqlite3.connect(GFS_FORECASTS_DB)
    conn2.row_factory = sqlite3.Row
    gfs_rows = conn2.execute("SELECT COUNT(*) FROM gfs_forecasts").fetchone()[0]
    obs_rows = conn2.execute("SELECT COUNT(*) FROM observed_weather").fetchone()[0]
    locations = [r[0] for r in conn2.execute("SELECT name FROM locations")]
    print(f"  GFS forecast rows:    {gfs_rows}")
    print(f"  ERA5 observed rows:   {obs_rows}")
    print(f"  locations:            {locations}")
    conn2.close()

    # Backtest readiness: markets with both price AND observed data
    conn3 = sqlite3.connect(WEATHER_MARKETS_DB)
    conn3.row_factory = sqlite3.Row
    joinable = conn3.execute(
        """
        SELECT COUNT(DISTINCT m.id)
        FROM markets m
        JOIN price_history p ON p.market_id = m.id
        WHERE m.city IS NOT NULL
          AND m.market_type IN ('temp_above','precip','snow')
          AND m.threshold_value IS NOT NULL
          AND m.target_date IS NOT NULL
        """
    ).fetchone()[0]
    print(f"  markets with prices:  {joinable}")
    conn3.close()


def main() -> None:
    args = parse_args()
    init_all()

    run_all = args.stage == "all"

    if run_all or args.stage == "discover":
        stage_discover(args.start, args.end, args.sleep, args.dry_run)

    if run_all or args.stage == "prices":
        stage_prices(args.start, args.end, args.sleep, args.dry_run)

    if run_all or args.stage == "gfs":
        stage_gfs(args.start, args.end, args.sleep, args.dry_run)

    if not args.dry_run:
        print_summary()


if __name__ == "__main__":
    main()
