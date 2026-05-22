#!/usr/bin/env python3
"""Unified CLI for the Polymarket weather prediction trading system.

Subcommands
-----------
  backfill  — discover markets & backfill prices/GFS for one or all cities
  signals   — generate today's live trading signals
  backtest  — run historical backtest (legacy run() method, guaranteed parity)
  coverage  — data coverage report (markets, prices, GFS, ERA5)
  calibrate — compute GFS bias/sigma for resolved dates

Examples
--------
  python3 cli.py backfill --city all --start 2026-01-01
  python3 cli.py backfill --city hong-kong --start 2026-03-01
  python3 cli.py signals
  python3 cli.py signals --city new-york
  python3 cli.py backtest --city hong-kong --start 2026-03-10 --end 2026-05-22
  python3 cli.py backtest --city all --start 2026-01-01
  python3 cli.py coverage
  python3 cli.py calibrate
  python3 cli.py calibrate --city hong-kong
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root on sys.path so `from src.data…` imports work even when running
# this script directly (python3 cli.py) from the project root.
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# City slug → display name lookup (compatible with KNOWN_LOCATIONS)
# ---------------------------------------------------------------------------
SLUG_TO_DISPLAY: dict[str, str] = {
    "beijing":       "Beijing",
    "shanghai":      "Shanghai",
    "tokyo":         "Tokyo",
    "seoul":         "Seoul",
    "new-york":      "New York",
    "london":        "London",
    "paris":         "Paris",
    "berlin":        "Berlin",
    "moscow":        "Moscow",
    "dubai":         "Dubai",
    "singapore":     "Singapore",
    "sydney":        "Sydney",
    "chicago":       "Chicago",
    "los-angeles":   "Los Angeles",
    "miami":         "Miami",
    "houston":       "Houston",
    "phoenix":       "Phoenix",
    "las-vegas":     "Las Vegas",
    "dallas":        "Dallas",
    "san-francisco": "San Francisco",
    "hong-kong":     "Hong Kong",
}


def _slug_to_display(slug: str) -> str:
    """Convert CLI slug to DB display name.  'hong-kong' → 'Hong Kong'."""
    if slug.lower() == "all":
        return "all"
    return SLUG_TO_DISPLAY.get(slug.lower(), slug.replace("-", " ").title())


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------

def cmd_backfill(args) -> int:
    from src.scripts.backfill_all_cities import run_city, _resolve_city_list, CITIES

    end = args.end or date.today().isoformat()
    city_list = _resolve_city_list(args.city)
    stages = [args.stage] if args.stage else ["all"]

    print(f"[backfill] Cities: {city_list}")
    print(f"[backfill] Range:  {args.start} → {end}")
    print(f"[backfill] Stages: {stages}")

    results = []
    for slug in city_list:
        s = run_city(
            slug,
            start=args.start,
            end=end,
            stages=stages,
            sleep=args.sleep,
            dry_run=args.dry_run,
            skip_existing=not args.no_skip,
        )
        results.append(s)

    print(f"\n[backfill] Done — {len(results)} cities")
    return 0


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------

def cmd_signals(args) -> int:
    from src.scripts.generate_daily_signals import main as signals_main

    city_filter = _slug_to_display(args.city) if args.city else None
    signals_main(city_filter=city_filter, min_edge=args.min_edge)
    return 0


# ---------------------------------------------------------------------------
# backtest — MUST use WeatherBacktester.run() for backward compatibility
# ---------------------------------------------------------------------------

def cmd_backtest(args) -> int:
    from src.data.weather_backtester import WeatherBacktester

    end = args.end or date.today().isoformat()

    # Resolve city filter: "all" → None (no filter), slug → display name
    city = None
    if args.city and args.city.lower() != "all":
        city = _slug_to_display(args.city)

    print(f"[backtest] city={city or 'ALL'}  {args.start} → {end}")
    print(f"[backtest] min_edge={args.min_edge}  amount=${args.amount:.2f}")

    bt = WeatherBacktester()
    try:
        trades = bt.run(
            start_date=args.start,
            end_date=end,
            city=city,
            min_edge=args.min_edge,
            amount=args.amount,
        )
        summary = bt.summary(trades)
        print(
            f"\n{'='*50}\nBacktest Results\n{'='*50}\n"
            f"Trades:    {summary['trades']}\n"
            f"Resolved:  {summary['resolved']}\n"
            f"Wins:      {summary['wins']}  Losses: {summary['losses']}\n"
            f"Win rate:  {summary['win_rate']:.1%}\n"
            f"Total PnL: ${summary['total_pnl']:.2f}\n"
            f"Invested:  ${summary['invested']:.2f}\n"
            f"ROI:       {summary['roi']:+.1%}\n"
            f"Avg edge:  {summary['avg_edge']:.1%}\n"
        )

        if args.csv:
            out = Path(args.csv)
            bt.write_csv(trades, out)
            print(f"[backtest] Wrote {len(trades)} trades → {out}")
    finally:
        bt.close()

    return 0


# ---------------------------------------------------------------------------
# coverage
# ---------------------------------------------------------------------------

def cmd_coverage(args) -> int:
    from src.scripts.coverage_report import main as coverage_main

    coverage_main()
    return 0


# ---------------------------------------------------------------------------
# calibrate
# ---------------------------------------------------------------------------

def cmd_calibrate(args) -> int:
    import math
    from src.data.weather_backtester import WeatherBacktester

    end = args.end or date.today().isoformat()

    # Which cities to calibrate?
    if args.city and args.city.lower() != "all":
        city_display_list = [_slug_to_display(args.city)]
    else:
        city_display_list = list(SLUG_TO_DISPLAY.values())

    bt = WeatherBacktester()
    results: dict[str, dict] = {}

    try:
        for city_display in city_display_list:
            # Derive location_id the same way as the backtester
            from src.data.geocoding import normalize_location_id, KNOWN_LOCATIONS
            key = city_display.lower()
            info = KNOWN_LOCATIONS.get(key)
            if info is None:
                print(f"[calibrate] {city_display}: not in KNOWN_LOCATIONS — skipping")
                continue
            country = info[1]
            location_id = normalize_location_id(city_display, country)

            variables = [
                ("temperature_2m_max", "temp_above"),
                ("temperature_2m_min", "temp_above"),
            ]
            city_results: dict[str, dict] = {}
            for variable, _ in variables:
                try:
                    bias, sigma = bt.calibrate(
                        location_id=location_id,
                        variable=variable,
                        anchor_date=end,
                        n_days=args.n_days,
                    )
                    city_results[variable] = {"bias": bias, "sigma": sigma}
                    print(
                        f"[calibrate] {city_display}/{variable}: "
                        f"bias={bias:+.3f}°C  sigma={sigma:.3f}°C"
                    )
                except Exception as exc:
                    print(f"[calibrate] {city_display}/{variable}: ERROR — {exc}")

            if city_results:
                results[city_display] = city_results
    finally:
        bt.close()

    # Write calibration.json
    out_path = PROJECT_ROOT / "data" / "calibration.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\n[calibrate] Wrote {out_path}")
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli",
        description="Polymarket weather trading system — unified CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- backfill ----------------------------------------------------------
    p_bf = sub.add_parser("backfill", help="Discover + backfill markets/prices/GFS for cities")
    p_bf.add_argument("--city", default="all",
                      help="City slug or 'all' (default: all)")
    p_bf.add_argument("--start", default="2026-01-01",
                      help="Start date YYYY-MM-DD (default: 2026-01-01)")
    p_bf.add_argument("--end", default=None,
                      help="End date YYYY-MM-DD (default: today)")
    p_bf.add_argument("--stage", default="all",
                      choices=["all", "discover", "prices", "gfs"],
                      help="Which stages to run (default: all)")
    p_bf.add_argument("--sleep", type=float, default=1.0,
                      help="Seconds between API requests (default: 1.0)")
    p_bf.add_argument("--dry-run", action="store_true",
                      help="Print what would be done, no API calls")
    p_bf.add_argument("--no-skip", action="store_true",
                      help="Re-run even if data already exists")

    # ---- signals -----------------------------------------------------------
    p_sig = sub.add_parser("signals", help="Generate today's trading signals")
    p_sig.add_argument("--city", default=None,
                       help="City slug (e.g. hong-kong) or omit for all")
    p_sig.add_argument("--min-edge", type=float, default=0.05,
                       help="Min edge threshold (default: 0.05)")

    # ---- backtest ----------------------------------------------------------
    p_bt = sub.add_parser("backtest", help="Run historical backtest")
    p_bt.add_argument("--city", default="all",
                      help="City slug or 'all' (default: all)")
    p_bt.add_argument("--start", default="2026-01-01",
                      help="Start date YYYY-MM-DD (default: 2026-01-01)")
    p_bt.add_argument("--end", default=None,
                      help="End date YYYY-MM-DD (default: today)")
    p_bt.add_argument("--min-edge", type=float, default=0.10,
                      help="Min edge to trigger a trade (default: 0.10)")
    p_bt.add_argument("--amount", type=float, default=5.0,
                      help="USD per trade (default: 5.0)")
    p_bt.add_argument("--csv", default=None,
                      help="Optional path to write trade CSV")

    # ---- coverage ----------------------------------------------------------
    sub.add_parser("coverage", help="Data coverage report")

    # ---- calibrate ---------------------------------------------------------
    p_cal = sub.add_parser("calibrate", help="Compute GFS bias/sigma from resolved dates")
    p_cal.add_argument("--city", default="all",
                       help="City slug or 'all' (default: all)")
    p_cal.add_argument("--end", default=None,
                       help="Anchor date (default: today)")
    p_cal.add_argument("--n-days", type=int, default=20,
                       help="Look-back window for calibration (default: 20)")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

COMMAND_HANDLERS = {
    "backfill":  cmd_backfill,
    "signals":   cmd_signals,
    "backtest":  cmd_backtest,
    "coverage":  cmd_coverage,
    "calibrate": cmd_calibrate,
}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    handler = COMMAND_HANDLERS.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)
    sys.exit(handler(args) or 0)


if __name__ == "__main__":
    main()
