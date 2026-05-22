"""Run a weather-market backtest from the local SQLite databases."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.data.weather_backtester import WeatherBacktester


def parse_args():
    parser = argparse.ArgumentParser(description="Backtest GFS probabilities vs Polymarket weather odds.")
    parser.add_argument("--start", default="2024-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--city", default=None, help="Optional city filter, e.g. 'Hong Kong'")
    parser.add_argument("--min-edge", type=float, default=0.10, help="Minimum absolute edge")
    parser.add_argument("--amount", type=float, default=5.0, help="Simulated stake per trade")
    parser.add_argument("--max-lead-hours", type=int, default=None, help="Optional max forecast lead time")
    parser.add_argument("--max-lead", type=int, default=None, dest="max_lead_hours", help="Alias for --max-lead-hours")
    parser.add_argument("--min-price", type=float, default=0.0, help="Minimum YES-token price; filters illiquid near-zero trades (suggest 0.03)")
    parser.add_argument("--csv", default=None, help="Optional output CSV path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    from datetime import date

    end = args.end or date.today().isoformat()
    backtester = WeatherBacktester()
    try:
        trades = backtester.run(
            start_date=args.start,
            end_date=end,
            city=args.city,
            min_edge=args.min_edge,
            amount=args.amount,
            max_lead_time_hours=args.max_lead_hours,
            min_price=args.min_price,
        )
        summary = backtester.summary(trades)
        print("=" * 60)
        print("Weather Backtest")
        print("=" * 60)
        print(f"trades:    {summary['trades']}")
        print(f"resolved:  {summary['resolved']}")
        print(f"wins:      {summary['wins']}")
        print(f"losses:    {summary['losses']}")
        print(f"win_rate:  {summary['win_rate']:.1%}")
        print(f"pnl:       ${summary['total_pnl']:+.2f}")
        print(f"roi:       {summary['roi']:+.1%}")
        print(f"avg_edge:  {summary['avg_edge']:.1%}")
        if args.csv:
            backtester.write_csv(trades, Path(args.csv))
            print(f"csv:       {args.csv}")
    finally:
        backtester.close()


if __name__ == "__main__":
    main()
