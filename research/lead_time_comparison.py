#!/usr/bin/env python3
"""Lead-time comparison backtest for GFS temperature markets.

Compares the strategy's P&L / win-rate / edge when restricted to trades at a
specific lead time (T+K = K days between price_date and target_date).

Hypothesis: shorter lead times (closer to event resolution) yield higher
forecast accuracy and therefore better risk-adjusted returns, even though
longer lead times surface more mispricings.

Output:
  - Per-lead-time summary table printed to stdout
  - CSV of all trades written to research/output/<run>/lead_time_<k>.csv

Usage::

    python3 -m research.lead_time_comparison --city hong-kong \
        --start 2026-03-01 --end 2026-05-31
    python3 -m research.lead_time_comparison --city all --leads 0 1 2 3
"""
from __future__ import annotations

import argparse
import csv
import statistics
from datetime import date
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).parent.parent


def _bucket_by_lead(results: list[dict], lead_days: int) -> list[dict]:
    out: list[dict] = []
    for r in results:
        d1 = date.fromisoformat(r["date"])
        d2 = date.fromisoformat(r["target_date"])
        if (d2 - d1).days == lead_days:
            out.append(r)
    return out


def _summarize(rows: list[dict]) -> dict:
    resolved = [r for r in rows if r["actual_outcome"] is not None]
    invested = 5.0 * len(resolved)
    pnl = sum(r["pnl"] for r in resolved)
    wins = sum(1 for r in resolved if r["pnl"] > 0)
    daily_pnl: dict[str, float] = {}
    for r in resolved:
        daily_pnl[r["date"]] = daily_pnl.get(r["date"], 0.0) + r["pnl"]
    rets = list(daily_pnl.values())
    if len(rets) > 1:
        mu = statistics.mean(rets)
        sd = statistics.pstdev(rets) or 1e-9
        sharpe = mu / sd * (len(rets) ** 0.5)
    else:
        sharpe = 0.0
    peak = 0.0
    cur = 0.0
    mdd = 0.0
    for v in rets:
        cur += v
        peak = max(peak, cur)
        mdd = min(mdd, cur - peak)
    return {
        "trades": len(rows),
        "resolved": len(resolved),
        "win_rate": wins / len(resolved) if resolved else 0.0,
        "pnl": pnl,
        "roi": pnl / invested if invested else 0.0,
        "avg_edge": statistics.mean(abs(r["edge"]) for r in rows) if rows else 0.0,
        "sharpe_daily": sharpe,
        "max_drawdown": mdd,
    }


def _write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def run(
    city: Optional[str],
    start: str,
    end: str,
    leads: list[int],
    min_edge: float,
    amount: float,
    min_price: float,
    out_dir: Optional[Path],
) -> list[dict]:
    import sys
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from cli import _slug_to_display
    from src.data.weather_backtester import WeatherBacktester

    if city and city.lower() != "all":
        city_display = _slug_to_display(city)
    else:
        city_display = None

    bt = WeatherBacktester()
    try:
        all_rows = bt.run_standard(
            start=start,
            end=end,
            city=city_display,
            min_edge=min_edge,
            amount=amount,
            min_price=min_price,
            max_lead_time_hours=None,
        )
    finally:
        bt.close()

    print(
        f"\nLead-time comparison — city={city_display or 'ALL'} "
        f"window={start}..{end} trades(any lead)={len(all_rows)}\n"
    )
    header = (
        f"{'Lead':>4} {'Trades':>7} {'Resolved':>9} {'Win%':>6} "
        f"{'PnL':>9} {'ROI%':>7} {'Avg|edge|%':>10} {'Sharpe':>7} {'MaxDD':>8}"
    )
    print(header)
    print("-" * len(header))

    summary_rows: list[dict] = []
    for k in leads:
        bucket = _bucket_by_lead(all_rows, k)
        s = _summarize(bucket)
        summary_rows.append({"lead": k, **s})
        print(
            f"T+{k:<3} {s['trades']:>7} {s['resolved']:>9} "
            f"{s['win_rate']*100:>5.1f}% {s['pnl']:>+9.2f} "
            f"{s['roi']*100:>6.1f}% {s['avg_edge']*100:>9.1f}% "
            f"{s['sharpe_daily']:>7.2f} {s['max_drawdown']:>+8.2f}"
        )
        if out_dir is not None:
            _write_csv(bucket, out_dir / f"lead_time_{k}.csv")

    if out_dir is not None:
        _write_csv(
            [{k: v for k, v in row.items()} for row in summary_rows],
            out_dir / "summary.csv",
        )
        print(f"\nCSVs written to {out_dir}/")
    return summary_rows


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--city", default=None, help="City name (default: all)")
    p.add_argument("--start", default="2026-03-01")
    p.add_argument("--end", default="2026-05-31")
    p.add_argument("--leads", type=int, nargs="+", default=[0, 1, 2, 3])
    p.add_argument("--min-edge", type=float, default=0.05)
    p.add_argument("--amount", type=float, default=5.0)
    p.add_argument("--min-price", type=float, default=0.03)
    p.add_argument(
        "--out-dir",
        default=None,
        help="Directory for CSV output (default: research/output/lead_time_<city>)",
    )
    args = p.parse_args()

    out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else PROJECT_ROOT
        / "research"
        / "output"
        / f"lead_time_{(args.city or 'all').lower().replace(' ', '-')}"
    )
    run(
        city=args.city,
        start=args.start,
        end=args.end,
        leads=args.leads,
        min_edge=args.min_edge,
        amount=args.amount,
        min_price=args.min_price,
        out_dir=out_dir,
    )


if __name__ == "__main__":
    main()
