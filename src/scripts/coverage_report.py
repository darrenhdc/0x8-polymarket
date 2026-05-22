"""Data coverage report across weather_markets.db and gfs_forecasts.db.

Outputs data/coverage_report.json with per-month breakdowns of:
  - target_dates with price data only
  - target_dates with GFS forecast only
  - target_dates with both price + GFS (tradeable)
  - target_dates with all three: price + GFS + observed outcome (backtestable)

Usage:
    python3 -m src.scripts.coverage_report
    python3 src/scripts/coverage_report.py
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path

PROJECT_ROOT  = Path(__file__).parent.parent.parent
MARKET_DB     = PROJECT_ROOT / "data" / "weather_markets.db"
GFS_DB        = PROJECT_ROOT / "data" / "gfs_forecasts.db"
REPORT_PATH   = PROJECT_ROOT / "data" / "coverage_report.json"


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def dates_with_prices(market_conn: sqlite3.Connection, city: str = "Hong Kong") -> set[str]:
    """All target_dates that have at least one price_history row for this city."""
    rows = market_conn.execute(
        """
        SELECT DISTINCT m.target_date
        FROM markets m
        JOIN price_history p ON p.market_id = m.id
        WHERE m.city = ?
          AND m.target_date IS NOT NULL
        """,
        (city,),
    ).fetchall()
    return {r[0] for r in rows}


def dates_with_gfs(gfs_conn: sqlite3.Connection, location_id: str) -> set[str]:
    """All target_dates that have at least one GFS forecast row."""
    rows = gfs_conn.execute(
        """
        SELECT DISTINCT target_date
        FROM gfs_forecasts
        WHERE location_id = ?
        """,
        (location_id,),
    ).fetchall()
    return {r[0] for r in rows}


def dates_with_observed(gfs_conn: sqlite3.Connection, location_id: str) -> set[str]:
    """All target_dates that have at least one observed_weather row."""
    rows = gfs_conn.execute(
        """
        SELECT DISTINCT target_date
        FROM observed_weather
        WHERE location_id = ?
        """,
        (location_id,),
    ).fetchall()
    return {r[0] for r in rows}


def dates_with_resolution(market_conn: sqlite3.Connection, city: str = "Hong Kong") -> set[str]:
    """All target_dates where at least one market has a resolved_outcome."""
    rows = market_conn.execute(
        """
        SELECT DISTINCT target_date
        FROM markets
        WHERE city = ?
          AND resolved_outcome IS NOT NULL
          AND target_date IS NOT NULL
        """,
        (city,),
    ).fetchall()
    return {r[0] for r in rows}


def month_of(d: str) -> str:
    return d[:7]  # "YYYY-MM"


def per_month_breakdown(dates: set[str]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for d in dates:
        counts[month_of(d)] += 1
    return dict(sorted(counts.items()))


# ---------------------------------------------------------------------------
# Market-level counts
# ---------------------------------------------------------------------------

def market_counts(market_conn: sqlite3.Connection, city: str = "Hong Kong") -> dict:
    total = market_conn.execute(
        "SELECT COUNT(*) FROM markets WHERE city=?", (city,)
    ).fetchone()[0]

    with_prices = market_conn.execute(
        """
        SELECT COUNT(DISTINCT m.id)
        FROM markets m
        JOIN price_history p ON p.market_id = m.id
        WHERE m.city = ?
        """,
        (city,),
    ).fetchone()[0]

    resolved = market_conn.execute(
        """
        SELECT COUNT(*) FROM markets
        WHERE city=? AND resolved_outcome IS NOT NULL
        """,
        (city,),
    ).fetchone()[0]

    active_future = market_conn.execute(
        """
        SELECT COUNT(*) FROM markets
        WHERE city=? AND target_date > ? AND resolved_outcome IS NULL
        """,
        (city, date.today().isoformat()),
    ).fetchone()[0]

    price_rows = market_conn.execute(
        """
        SELECT COUNT(*) FROM price_history p
        JOIN markets m ON m.id = p.market_id
        WHERE m.city = ?
        """,
        (city,),
    ).fetchone()[0]

    return {
        "total_markets": total,
        "markets_with_prices": with_prices,
        "resolved_markets": resolved,
        "active_future_markets": active_future,
        "total_price_rows": price_rows,
    }


def gfs_counts(gfs_conn: sqlite3.Connection, location_id: str) -> dict:
    forecast_rows = gfs_conn.execute(
        "SELECT COUNT(*) FROM gfs_forecasts WHERE location_id=?", (location_id,)
    ).fetchone()[0]

    observed_rows = gfs_conn.execute(
        "SELECT COUNT(*) FROM observed_weather WHERE location_id=?", (location_id,)
    ).fetchone()[0]

    forecast_dates = gfs_conn.execute(
        "SELECT COUNT(DISTINCT target_date) FROM gfs_forecasts WHERE location_id=?",
        (location_id,),
    ).fetchone()[0]

    observed_dates = gfs_conn.execute(
        "SELECT COUNT(DISTINCT target_date) FROM observed_weather WHERE location_id=?",
        (location_id,),
    ).fetchone()[0]

    return {
        "gfs_forecast_rows": forecast_rows,
        "gfs_forecast_dates": forecast_dates,
        "era5_observed_rows": observed_rows,
        "era5_observed_dates": observed_dates,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_report() -> dict:
    market_conn = sqlite3.connect(str(MARKET_DB))
    market_conn.row_factory = sqlite3.Row
    gfs_conn    = sqlite3.connect(str(GFS_DB))
    gfs_conn.row_factory = sqlite3.Row

    city        = "Hong Kong"
    location_id = "hong_kong_hong_kong"

    # Date sets
    d_price    = dates_with_prices(market_conn, city)
    d_gfs      = dates_with_gfs(gfs_conn, location_id)
    d_observed = dates_with_observed(gfs_conn, location_id)
    d_resolved = dates_with_resolution(market_conn, city)

    d_price_and_gfs      = d_price & d_gfs
    d_all_three          = d_price & d_gfs & d_observed    # backtestable (ERA5)
    d_fully_resolved     = d_price & d_gfs & d_resolved    # backtestable (market)

    today = date.today().isoformat()
    d_past_price_and_gfs = {d for d in d_price_and_gfs if d <= today}

    report = {
        "generated_at": date.today().isoformat(),
        "city": city,
        "location_id": location_id,

        # Raw counts
        "unique_dates": {
            "with_price":                   len(d_price),
            "with_gfs_forecast":            len(d_gfs),
            "with_era5_observed":           len(d_observed),
            "with_market_resolution":       len(d_resolved),
            "with_price_and_gfs":           len(d_price_and_gfs),
            "with_price_gfs_era5":          len(d_all_three),
            "with_price_gfs_resolution":    len(d_fully_resolved),
            "past_dates_price_and_gfs":     len(d_past_price_and_gfs),
        },

        # Market and GFS row counts
        **market_counts(market_conn, city),
        **gfs_counts(gfs_conn, location_id),

        # Per-month breakdown
        "monthly": {
            "price":                 per_month_breakdown(d_price),
            "gfs_forecast":          per_month_breakdown(d_gfs),
            "era5_observed":         per_month_breakdown(d_observed),
            "market_resolution":     per_month_breakdown(d_resolved),
            "price_and_gfs":         per_month_breakdown(d_price_and_gfs),
            "backtestable_era5":     per_month_breakdown(d_all_three),
            "backtestable_resolved": per_month_breakdown(d_fully_resolved),
        },

        # Date lists for debugging
        "dates_without_gfs":   sorted(d_price - d_gfs),
        "dates_without_price": sorted(d_gfs - d_price),
    }

    market_conn.close()
    gfs_conn.close()
    return report


def print_summary(report: dict) -> None:
    ud = report["unique_dates"]
    print("=" * 60)
    print("Data Coverage Report")
    print("=" * 60)
    print(f"  City / location:          {report['city']} / {report['location_id']}")
    print()
    print(f"  Markets total:            {report['total_markets']}")
    print(f"  Markets with prices:      {report['markets_with_prices']}")
    print(f"  Markets resolved:         {report['resolved_markets']}")
    print(f"  Markets active (future):  {report['active_future_markets']}")
    print(f"  Price history rows:       {report['total_price_rows']}")
    print()
    print(f"  GFS forecast rows:        {report['gfs_forecast_rows']}")
    print(f"  GFS unique dates:         {report['gfs_forecast_dates']}")
    print(f"  ERA5 observed rows:       {report['era5_observed_rows']}")
    print(f"  ERA5 unique dates:        {report['era5_observed_dates']}")
    print()
    print("  Date coverage:")
    print(f"    With price data:        {ud['with_price']}")
    print(f"    With GFS forecast:      {ud['with_gfs_forecast']}")
    print(f"    With ERA5 observed:     {ud['with_era5_observed']}")
    print(f"    With market resolution: {ud['with_market_resolution']}")
    print(f"    Price + GFS (all):      {ud['with_price_and_gfs']}")
    print(f"    Past price + GFS:       {ud['past_dates_price_and_gfs']}")
    print(f"    Backtestable (ERA5):    {ud['with_price_gfs_era5']}")
    print(f"    Backtestable (market):  {ud['with_price_gfs_resolution']}")
    print()

    # Per-month summary
    monthly_bt = report["monthly"]["backtestable_resolved"]
    if monthly_bt:
        print("  Backtestable dates by month (price+GFS+resolution):")
        for month, cnt in sorted(monthly_bt.items()):
            p_cnt = report["monthly"]["price"].get(month, 0)
            g_cnt = report["monthly"]["gfs_forecast"].get(month, 0)
            r_cnt = report["monthly"]["market_resolution"].get(month, 0)
            print(f"    {month}:  price={p_cnt:3d}  gfs={g_cnt:3d}  resolved={r_cnt:3d}  bt={cnt:3d}")


def main() -> None:
    report = build_report()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with REPORT_PATH.open("w") as f:
        json.dump(report, f, indent=2)
    print_summary(report)
    print(f"Report written: {REPORT_PATH}")


if __name__ == "__main__":
    main()
