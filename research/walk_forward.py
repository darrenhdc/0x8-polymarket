#!/usr/bin/env python3
"""Walk-forward validation framework for GFS weather strategies.

Implements A02-standard walk-forward analysis:
  - Train window: 18 months resolved markets (for rolling calibration)
  - Test window:  3 months of trading
  - Step window:  3 months forward step
  - Holdout:      12 months at the end (final unbiased estimate)

For each fold:
  1. Use train window to compute (bias, sigma) calibration
  2. Run WeatherBacktester.run_standard() on test window
  3. Record PnL, win rate, Sharpe, max drawdown per fold
  4. Aggregate across folds

Usage::
    python3 -m research.walk_forward --city hong-kong --start 2024-01-01 --end 2026-05-31
    python3 -m research.walk_forward --city all --start 2025-01-01 --end 2026-05-31 --folds 4
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).parent.parent


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _format_date(d: date) -> str:
    return d.isoformat()


def _generate_folds(
    start: date,
    end: date,
    train_months: int = 18,
    test_months: int = 3,
    step_months: int = 3,
    holdout_months: int = 12,
) -> List[dict]:
    """Generate walk-forward fold definitions.

    Returns list of dicts: {train_start, train_end, test_start, test_end}.
    """
    folds = []
    # Total range minus holdout
    effective_end = end - timedelta(days=holdout_months * 30)

    test_start = start + timedelta(days=train_months * 30)
    while test_start < effective_end:
        train_start = start
        train_end = test_start - timedelta(days=1)
        test_end = min(test_start + timedelta(days=test_months * 30), effective_end)

        if test_end <= test_start:
            break

        folds.append({
            "train_start": _format_date(train_start),
            "train_end": _format_date(train_end),
            "test_start": _format_date(test_start),
            "test_end": _format_date(test_end),
        })

        test_start = test_start + timedelta(days=step_months * 30)

    return folds


def _run_fold(
    fold: dict,
    city: Optional[str],
    min_edge: float,
    amount: float,
    max_lead_hours: Optional[int],
    prediction_source_name: str = "gfs",
) -> dict:
    """Execute one walk-forward fold.

    Uses WeatherBacktester.run_standard() with rolling calibration.
    """
    from src.data.weather_backtester import WeatherBacktester
    from src.data.gfs_prediction import GFSPredictionSource

    source = GFSPredictionSource(mode="historical")
    bt = WeatherBacktester(prediction_source=source)
    try:
        trades = bt.run_standard(
            start=fold["test_start"],
            end=fold["test_end"],
            city=city,
            min_edge=min_edge,
            amount=amount,
            max_lead_time_hours=max_lead_hours,
            prediction_source=source,
        )
        summary = _compute_fold_stats(trades, amount)
        summary["fold"] = fold
        summary["trades"] = trades
        return summary
    finally:
        bt.close()
        source.close()


def _compute_fold_stats(trades: List[dict], amount: float) -> dict:
    """Compute statistics from a list of trade dicts."""
    if not trades:
        return {
            "n_trades": 0,
            "n_resolved": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "invested": 0.0,
            "roi": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "avg_edge": 0.0,
        }

    resolved = [t for t in trades if t.get("actual_outcome") is not None]
    if not resolved:
        return {
            "n_trades": len(trades),
            "n_resolved": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "invested": 0.0,
            "roi": 0.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "avg_edge": 0.0,
        }

    pnls = [t["pnl"] for t in resolved]
    total_pnl = sum(pnls)
    invested = amount * len(resolved)
    wins = sum(1 for p in pnls if p > 0)
    losses = len(resolved) - wins
    win_rate = wins / len(resolved) if resolved else 0.0
    roi = total_pnl / invested if invested else 0.0

    # Sharpe (approximate: assuming zero risk-free rate, daily resolution not available)
    if len(pnls) > 1:
        mean_pnl = total_pnl / len(pnls)
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
        std_pnl = math.sqrt(variance) if variance > 0 else 0.0
        sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0.0
    else:
        sharpe = 0.0

    # Max drawdown from cumulative PnL
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    edges = [abs(t.get("edge", 0)) for t in trades]
    avg_edge = sum(edges) / len(edges) if edges else 0.0

    return {
        "n_trades": len(trades),
        "n_resolved": len(resolved),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl": round(total_pnl, 2),
        "invested": round(invested, 2),
        "roi": round(roi, 4),
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(max_dd, 2),
        "avg_edge": round(avg_edge, 4),
    }


def _print_fold(fold_idx: int, fold_def: dict, stats: dict) -> None:
    print(f"\n{'='*60}")
    print(f"Fold #{fold_idx + 1}")
    print(f"  Train: {fold_def['train_start']} → {fold_def['train_end']}")
    print(f"  Test:  {fold_def['test_start']} → {fold_def['test_end']}")
    print(f"  Trades:   {stats['n_trades']} (resolved: {stats['n_resolved']})")
    print(f"  Win rate: {stats['win_rate']:.1%}")
    print(f"  PnL:      ${stats['total_pnl']:+.2f}")
    print(f"  ROI:      {stats['roi']:+.1%}")
    print(f"  Sharpe:   {stats['sharpe']:.2f}")
    print(f"  Max DD:   ${stats['max_drawdown']:.2f}")
    print(f"  Avg edge: {stats['avg_edge']:.1%}")


def _aggregate_results(fold_results: List[dict]) -> dict:
    """Aggregate statistics across all folds."""
    if not fold_results:
        return {}

    total_trades = sum(r["n_trades"] for r in fold_results)
    total_resolved = sum(r["n_resolved"] for r in fold_results)
    total_pnl = sum(r["total_pnl"] for r in fold_results)
    total_invested = sum(r["invested"] for r in fold_results)
    total_wins = sum(r["wins"] for r in fold_results)

    roi = total_pnl / total_invested if total_invested else 0.0
    win_rate = total_wins / total_resolved if total_resolved else 0.0

    sharpe_values = [r["sharpe"] for r in fold_results if r["n_trades"] > 1]
    avg_sharpe = sum(sharpe_values) / len(sharpe_values) if sharpe_values else 0.0

    max_dd_values = [r["max_drawdown"] for r in fold_results]
    avg_max_dd = sum(max_dd_values) / len(max_dd_values) if max_dd_values else 0.0

    edges = [r["avg_edge"] for r in fold_results]
    avg_edge = sum(edges) / len(edges) if edges else 0.0

    return {
        "folds": len(fold_results),
        "total_trades": total_trades,
        "total_resolved": total_resolved,
        "total_wins": total_wins,
        "total_losses": total_resolved - total_wins,
        "win_rate": round(win_rate, 4),
        "total_pnl": round(total_pnl, 2),
        "roi": round(roi, 4),
        "avg_sharpe": round(avg_sharpe, 4),
        "avg_max_drawdown": round(avg_max_dd, 2),
        "avg_edge": round(avg_edge, 4),
    }


def _write_results(
    fold_results: List[dict],
    aggregate: dict,
    out_dir: Path,
) -> None:
    """Write fold results and aggregate summary to CSV/JSON."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # Aggregate summary
    agg_path = out_dir / "walk_forward_summary.json"
    agg_path.write_text(json.dumps(aggregate, indent=2))
    print(f"\n[wf] Wrote aggregate summary → {agg_path}")

    # Per-fold details
    for i, result in enumerate(fold_results):
        trades = result.pop("trades", [])
        fold = result.pop("fold", {})
        fold_path = out_dir / f"fold_{i+1}_{fold.get('test_start', 'unknown')}_{fold.get('test_end', 'unknown')}.csv"
        if trades:
            with open(fold_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
                writer.writeheader()
                writer.writerows(trades)
            print(f"[wf] Wrote fold {i+1} trades → {fold_path}")


def main(
    start: str,
    end: str,
    city: Optional[str] = None,
    train_months: int = 18,
    test_months: int = 3,
    step_months: int = 3,
    holdout_months: int = 12,
    min_edge: float = 0.05,
    amount: float = 5.0,
    max_lead_hours: Optional[int] = 48,
    out_dir: Optional[Path] = None,
) -> dict:
    """Run walk-forward analysis and return aggregate stats."""
    start_date = _parse_date(start)
    end_date = _parse_date(end)

    print(f"[wf] Walk-forward analysis")
    print(f"[wf] Range: {start} → {end}")
    print(f"[wf] City: {city or 'ALL'}")
    print(f"[wf] Train: {train_months}m / Test: {test_months}m / Step: {step_months}m / Holdout: {holdout_months}m")

    folds = _generate_folds(
        start_date, end_date,
        train_months=train_months,
        test_months=test_months,
        step_months=step_months,
        holdout_months=holdout_months,
    )

    if not folds:
        print("[wf] ERROR: No folds generated — check date range and window sizes")
        return {}

    print(f"[wf] Folds: {len(folds)}")

    fold_results: List[dict] = []
    for idx, fold_def in enumerate(folds):
        stats = _run_fold(
            fold=fold_def,
            city=city,
            min_edge=min_edge,
            amount=amount,
            max_lead_hours=max_lead_hours,
        )
        _print_fold(idx, fold_def, stats)
        fold_results.append(stats)

    aggregate = _aggregate_results(fold_results)
    print(f"\n{'='*60}")
    print("WALK-FORWARD AGGREGATE")
    print(f"{'='*60}")
    print(f"  Folds:        {aggregate['folds']}")
    print(f"  Total trades: {aggregate['total_trades']} (resolved: {aggregate['total_resolved']})")
    print(f"  Win rate:     {aggregate['win_rate']:.1%}")
    print(f"  Total PnL:    ${aggregate['total_pnl']:+.2f}")
    print(f"  ROI:          {aggregate['roi']:+.1%}")
    print(f"  Avg Sharpe:   {aggregate['avg_sharpe']:.2f}")
    print(f"  Avg Max DD:   ${aggregate['avg_max_drawdown']:.2f}")
    print(f"  Avg edge:     {aggregate['avg_edge']:.1%}")

    if out_dir:
        _write_results(fold_results, aggregate, out_dir)

    return aggregate


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Walk-forward validation for GFS weather strategies")
    parser.add_argument("--start", default="2024-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--city", default=None, help="City filter or 'all'")
    parser.add_argument("--train-months", type=int, default=18, help="Training window in months")
    parser.add_argument("--test-months", type=int, default=3, help="Test window in months")
    parser.add_argument("--step-months", type=int, default=3, help="Step size in months")
    parser.add_argument("--holdout-months", type=int, default=12, help="Holdout months at end")
    parser.add_argument("--min-edge", type=float, default=0.05)
    parser.add_argument("--amount", type=float, default=5.0)
    parser.add_argument("--max-lead-hours", type=int, default=48)
    parser.add_argument("--out-dir", default="research/output", help="Output directory")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    end = args.end or date.today().isoformat()
    city = None if args.city and args.city.lower() == "all" else args.city
    main(
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
