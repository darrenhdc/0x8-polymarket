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
# performance
# ---------------------------------------------------------------------------

def cmd_performance(args) -> int:
    """Read portfolio + trades and show real-time PnL summary."""
    portfolio_path = PROJECT_ROOT / "data" / "portfolio.json"
    trades_path = PROJECT_ROOT / "data" / "trades.json"

    pf = {}
    if portfolio_path.exists():
        import json as _json
        pf = _json.loads(portfolio_path.read_text())

    trades = []
    if trades_path.exists():
        import json as _json
        trades = _json.loads(trades_path.read_text())

    cash = pf.get("cash", 0.0)
    positions = pf.get("positions", {})
    total_trades = len(trades)
    realized_pnl = sum(t.get("pnl", 0) for t in trades if t.get("pnl") is not None)
    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)

    print(f"{'='*60}")
    print("PERFORMANCE SUMMARY")
    print(f"{'='*60}")
    print(f"  Cash:           ${cash:,.2f}")
    print(f"  Open positions: {len(positions)}")
    print(f"  Total trades:   {total_trades}")
    print(f"  Wins:           {wins}")
    print(f"  Realized PnL:   ${realized_pnl:+.2f}")
    if total_trades > 0:
        print(f"  Win rate:       {wins/total_trades:.1%}")
    print(f"{'='*60}")
    return 0


# ---------------------------------------------------------------------------
# sota
# ---------------------------------------------------------------------------

def cmd_sota(args) -> int:
    """Show current SOTA strategy brief."""
    sota_path = PROJECT_ROOT / "research" / "reports" / "SOTA.md"
    if not sota_path.exists():
        print("[sota] SOTA.md not found")
        return 1

    content = sota_path.read_text()
    # Print first sections only for brevity
    lines = content.splitlines()
    in_section = False
    printed = 0
    for line in lines:
        if line.startswith("## "):
            in_section = True
        if in_section and printed < 80:
            print(line)
            printed += 1
        elif printed >= 80:
            print("\n... (see full report in research/reports/SOTA.md)")
            break
    return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(args) -> int:
    """System status: DB counts, calibration, last signals."""
    from src.data.database import connect_markets, connect_gfs

    print(f"{'='*60}")
    print("SYSTEM STATUS")
    print(f"{'='*60}")

    # Market DB
    market_conn = connect_markets()
    mkt_count = market_conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    price_count = market_conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    resolved = market_conn.execute(
        "SELECT COUNT(*) FROM markets WHERE resolved_outcome IS NOT NULL"
    ).fetchone()[0]
    market_conn.close()
    print(f"  Markets:      {mkt_count}")
    print(f"  Price rows:   {price_count}")
    print(f"  Resolved:     {resolved}")

    # GFS DB
    gfs_conn = connect_gfs()
    gfs_count = gfs_conn.execute("SELECT COUNT(*) FROM gfs_forecasts").fetchone()[0]
    obs_count = gfs_conn.execute("SELECT COUNT(*) FROM observed_weather").fetchone()[0]
    gfs_conn.close()
    print(f"  GFS rows:     {gfs_count}")
    print(f"  Observed:     {obs_count}")

    # Calibration
    calib_path = PROJECT_ROOT / "data" / "calibration.json"
    if calib_path.exists():
        import json as _json
        calib = _json.loads(calib_path.read_text())
        print(f"  Calibrated:   {len(calib)} cities")
    else:
        print(f"  Calibrated:   none (run 'cli.py calibrate')")

    # Signal history
    sig_db = PROJECT_ROOT / "data" / "signal_history.db"
    if sig_db.exists():
        import sqlite3
        conn = sqlite3.connect(str(sig_db))
        today = date.today().isoformat()
        row = conn.execute(
            "SELECT COUNT(*) FROM signal_history WHERE run_date = ?", (today,)
        ).fetchone()
        print(f"  Signals today: {row[0]}")
        conn.close()
    else:
        print(f"  Signals today: N/A")

    print(f"{'='*60}")
    return 0


# ---------------------------------------------------------------------------
# walk-forward
# ---------------------------------------------------------------------------

def cmd_walkforward(args) -> int:
    """Delegate to research.walk_forward."""
    from research.walk_forward import main as wf_main
    end = args.end or date.today().isoformat()
    city = None if args.city and args.city.lower() == "all" else args.city
    wf_main(
        start=args.start,
        end=end,
        city=city,
        train_months=args.train_months,
        test_months=args.test_months,
        step_months=args.step_months,
        holdout_months=args.holdout_months,
        min_edge=args.min_edge,
        amount=args.amount,
        max_lead_hours=args.max_lead_hours,
        out_dir=Path(args.out_dir),
    )
    return 0


# ---------------------------------------------------------------------------
# factor-ic
# ---------------------------------------------------------------------------

def cmd_factor_ic(args) -> int:
    """Delegate to research.factor_ic."""
    from research.factor_ic import main as ic_main
    end = args.end or date.today().isoformat()
    ic_main(
        start=args.start,
        end=end,
        city=args.city,
        variable=args.variable,
        window_size=args.window_size,
        min_edge=args.min_edge,
        out_dir=Path(args.out_dir),
    )
    return 0


# ---------------------------------------------------------------------------
# paper-trading
# ---------------------------------------------------------------------------

def cmd_trade(args) -> int:
    """Run one paper trading cycle."""
    from src.execution.paper_trader import run_cycle
    city = _slug_to_display(args.city) if args.city else "Hong Kong"
    run_cycle(city_filter=city, min_edge=args.min_edge)
    return 0


def cmd_paper_status(args) -> int:
    """Show paper trading portfolio."""
    from src.execution.paper_trader import _load_portfolio, _load_trades
    s = _load_portfolio()
    trades = _load_trades()
    closed = [t for t in trades if t.get("closed")]
    open_trades = [t for t in trades if not t.get("closed")]
    won = [t for t in closed if t.get("outcome") == "won"]
    lost = [t for t in closed if t.get("outcome") == "lost"]
    total_pnl = sum(t.get("pnl", 0) for t in closed)
    open_cost = sum(t.get("cost_usd", 0) for t in open_trades)

    total_value = s["cash"] + open_cost + total_pnl

    print(f"{'='*60}")
    print("PAPER TRADING PORTFOLIO")
    print(f"{'='*60}")
    print(f"  Initial:      ${s.get('initial_capital', 1000):,.2f}")
    print(f"  Cash:         ${s['cash']:,.2f}")
    print(f"  Open trades:  {len(open_trades)} (${open_cost:,.2f} at risk)")
    print(f"  Closed:       {len(closed)} ({len(won)}W / {len(lost)}L)")
    print(f"  Realized PnL: ${total_pnl:+,.2f}")
    print(f"  Total value:  ${total_value:,.2f}")
    print(f"  Return:       {(total_value - s.get('initial_capital', 1000)) / max(s.get('initial_capital', 1000), 1):+.1%}")
    print(f"{'='*60}")
    return 0
    return 0


def cmd_paper_close(args) -> int:
    """Close positions for markets that have resolved."""
    from src.execution.paper_trader import _load_portfolio, _load_trades, _save_portfolio, _save_trades
    from src.data.database import connect_markets
    import sqlite3 as _sql

    trades = _load_trades()
    portfolio = _load_portfolio()
    market_conn = connect_markets()
    market_conn.row_factory = _sql.Row

    closed_count = 0
    for t in trades:
        if t.get("closed"):
            continue
        mkt = market_conn.execute(
            "SELECT resolved_outcome FROM markets WHERE id=?", (t["market_id"],)
        ).fetchone()
        if mkt is None:
            continue
        outcome = mkt["resolved_outcome"]
        if outcome not in ("Yes", "No"):
            continue

        # Resolve
        actual_yes = outcome == "Yes"
        if t["direction"] == "BUY_YES":
            won = actual_yes
        else:
            won = not actual_yes

        if won:
            t["pnl"] = round(t["tokens"] - t["cost_usd"], 2)
            t["outcome"] = "won"
        else:
            t["pnl"] = round(-t["cost_usd"], 2)
            t["outcome"] = "lost"

        t["closed"] = True
        t["close_date"] = date.today().isoformat()
        portfolio["cash"] += t["cost_usd"] + t["pnl"]
        closed_count += 1

    market_conn.close()

    if closed_count:
        _save_trades(trades)
        _save_portfolio(portfolio)
        print(f"[paper-close] Closed {closed_count} positions")
        cmd_paper_status(args)
    else:
        print("[paper-close] No resolved positions to close")
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

    # ---- performance -------------------------------------------------------
    sub.add_parser("performance", help="Show portfolio + trades PnL summary")

    # ---- sota --------------------------------------------------------------
    sub.add_parser("sota", help="Show current SOTA strategy brief")

    # ---- status ------------------------------------------------------------
    sub.add_parser("status", help="System status: DB counts, calibration, signals")

    # ---- walk-forward ------------------------------------------------------
    p_wf = sub.add_parser("walk-forward", help="Run walk-forward validation")
    p_wf.add_argument("--start", default="2024-01-01")
    p_wf.add_argument("--end", default=None)
    p_wf.add_argument("--city", default="all")
    p_wf.add_argument("--train-months", type=int, default=18)
    p_wf.add_argument("--test-months", type=int, default=3)
    p_wf.add_argument("--step-months", type=int, default=3)
    p_wf.add_argument("--holdout-months", type=int, default=12)
    p_wf.add_argument("--min-edge", type=float, default=0.05)
    p_wf.add_argument("--amount", type=float, default=5.0)
    p_wf.add_argument("--max-lead-hours", type=int, default=48)
    p_wf.add_argument("--out-dir", default="research/output")

    # ---- factor-ic ---------------------------------------------------------
    p_ic = sub.add_parser("factor-ic", help="Run factor IC analysis")
    p_ic.add_argument("--start", default="2024-01-01")
    p_ic.add_argument("--end", default=None)
    p_ic.add_argument("--city", default=None)
    p_ic.add_argument("--variable", default=None)
    p_ic.add_argument("--window-size", type=int, default=20)
    p_ic.add_argument("--min-edge", type=float, default=0.0)
    p_ic.add_argument("--out-dir", default="research/output")

    # ---- trade (paper) -----------------------------------------------------
    p_tr = sub.add_parser("trade", help="Run one paper trading cycle")
    p_tr.add_argument("--city", default="hong-kong")
    p_tr.add_argument("--min-edge", type=float, default=0.05)

    # ---- paper-status ------------------------------------------------------
    sub.add_parser("paper-status", help="Show paper trading portfolio")

    # ---- paper-close -------------------------------------------------------
    sub.add_parser("paper-close", help="Close resolved paper positions")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

COMMAND_HANDLERS = {
    "backfill":     cmd_backfill,
    "signals":      cmd_signals,
    "backtest":     cmd_backtest,
    "coverage":     cmd_coverage,
    "calibrate":    cmd_calibrate,
    "performance":  cmd_performance,
    "sota":         cmd_sota,
    "status":       cmd_status,
    "walk-forward": cmd_walkforward,
    "factor-ic":    cmd_factor_ic,
    "trade":        cmd_trade,
    "paper-status": cmd_paper_status,
    "paper-close":  cmd_paper_close,
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
