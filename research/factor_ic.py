#!/usr/bin/env python3
"""Factor IC analysis for GFS weather prediction signals.

Computes the Information Coefficient (IC) between GFS model probability
and actual market outcomes, measuring the predictive power of the GFS signal.

IC = corr(GFS_prob, actual_outcome)  over a rolling window.

Outputs:
  - IC_mean, IC_std, IC_sharpe (IR) per city / variable
  - IC decay by lead time (how fast does edge decay as forecast horizon grows)
  - IC stability (proportion of windows where IC > 0)

Usage::
    python3 -m research.factor_ic --city all --start 2024-01-01 --end 2026-05-31
    python3 -m research.factor_ic --city hong-kong --variable temperature_2m_max
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import date
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).parent.parent


def _pearson_corr(x: List[float], y: List[float]) -> Optional[float]:
    """Compute Pearson correlation coefficient."""
    n = len(x)
    if n < 3 or len(y) != n:
        return None

    mean_x = sum(x) / n
    mean_y = sum(y) / n

    cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    var_x = sum((xi - mean_x) ** 2 for xi in x)
    var_y = sum((yi - mean_y) ** 2 for yi in y)

    if var_x == 0 or var_y == 0:
        return None

    return cov / math.sqrt(var_x * var_y)


def _compute_ic_window(
    trades: List[dict],
    window_size: int = 20,
    min_pairs: int = 5,
) -> List[dict]:
    """Compute rolling IC over a sequence of trades.

    Each trade dict must have: model_prob, actual_outcome (bool/0/1).
    Returns list of {window_start, window_end, n, ic_mean, ic_std, ic_sharpe}.
    """
    resolved = [t for t in trades if t.get("actual_outcome") is not None]
    if len(resolved) < min_pairs:
        return []

    # Sort by date
    resolved.sort(key=lambda t: t.get("date", ""))

    results = []
    for i in range(len(resolved)):
        start_idx = max(0, i - window_size + 1)
        window = resolved[start_idx : i + 1]
        if len(window) < min_pairs:
            continue

        probs = [t["model_prob"] for t in window]
        outcomes = [1.0 if t["actual_outcome"] else 0.0 for t in window]

        ic = _pearson_corr(probs, outcomes)
        if ic is not None:
            results.append({
                "window_end": window[-1].get("date", ""),
                "n": len(window),
                "ic": round(ic, 4),
            })

    return results


def _compute_ic_summary(ic_series: List[dict]) -> dict:
    """Compute summary stats from an IC series."""
    if not ic_series:
        return {
            "ic_mean": 0.0,
            "ic_std": 0.0,
            "ic_sharpe": 0.0,
            "ic_positive_ratio": 0.0,
            "n_windows": 0,
        }

    ics = [d["ic"] for d in ic_series]
    n = len(ics)
    mean_ic = sum(ics) / n
    variance = sum((ic - mean_ic) ** 2 for ic in ics) / n
    std_ic = math.sqrt(variance) if variance > 0 else 0.0
    sharpe_ic = mean_ic / std_ic if std_ic > 0 else 0.0
    pos_ratio = sum(1 for ic in ics if ic > 0) / n

    return {
        "ic_mean": round(mean_ic, 4),
        "ic_std": round(std_ic, 4),
        "ic_sharpe": round(sharpe_ic, 4),
        "ic_positive_ratio": round(pos_ratio, 4),
        "n_windows": n,
    }


def _compute_ic_by_lead_time(
    trades: List[dict],
    lead_bins: Optional[List[tuple]] = None,
) -> dict:
    """Compute IC segmented by forecast lead time.

    lead_bins: list of (label, min_hours, max_hours).  Default bins provided.
    """
    if lead_bins is None:
        lead_bins = [
            ("<=24h", 0, 24),
            ("24-48h", 24, 48),
            ("48-72h", 48, 72),
            ("72-120h", 72, 120),
            (">120h", 120, 9999),
        ]

    results = {}
    for label, min_h, max_h in lead_bins:
        subset = [
            t for t in trades
            if t.get("lead_hours") is not None
            and min_h <= t["lead_hours"] < max_h
            and t.get("actual_outcome") is not None
        ]
        if len(subset) < 5:
            results[label] = {"ic": None, "n": len(subset)}
            continue

        probs = [t["model_prob"] for t in subset]
        outcomes = [1.0 if t["actual_outcome"] else 0.0 for t in subset]
        ic = _pearson_corr(probs, outcomes)
        results[label] = {"ic": round(ic, 4) if ic is not None else None, "n": len(subset)}

    return results


def _fetch_trades(
    start: str,
    end: str,
    city: Optional[str] = None,
    variable: Optional[str] = None,
    min_edge: float = 0.0,
) -> List[dict]:
    """Fetch backtest trade records from WeatherBacktester.run_standard()."""
    from src.data.weather_backtester import WeatherBacktester
    from src.data.gfs_prediction import GFSPredictionSource

    source = GFSPredictionSource(mode="historical")
    bt = WeatherBacktester(prediction_source=source)
    try:
        trades = bt.run_standard(
            start=start,
            end=end,
            city=city,
            min_edge=min_edge,
            amount=1.0,  # IC does not depend on amount
            prediction_source=source,
        )
        # Enrich with lead_hours for decay analysis
        for t in trades:
            t["lead_hours"] = _lead_hours(t.get("date", ""), t.get("target_date", ""))
        return trades
    finally:
        bt.close()
        source.close()


def _lead_hours(price_date: str, target_date: str) -> int:
    from datetime import date as _date
    try:
        issued = _date.fromisoformat(price_date[:10])
        target = _date.fromisoformat(target_date[:10])
        return max(0, (target - issued).days * 24)
    except ValueError:
        return 0


def _print_summary(summary: dict, label: str = "Overall") -> None:
    print(f"\n{'='*60}")
    print(f"IC Summary — {label}")
    print(f"{'='*60}")
    print(f"  IC mean:            {summary['ic_mean']:+.3f}")
    print(f"  IC std:             {summary['ic_std']:.3f}")
    print(f"  IC Sharpe (IR):     {summary['ic_sharpe']:+.3f}")
    print(f"  IC > 0 ratio:       {summary['ic_positive_ratio']:.1%}")
    print(f"  Rolling windows:    {summary['n_windows']}")


def main(
    start: str,
    end: str,
    city: Optional[str] = None,
    variable: Optional[str] = None,
    window_size: int = 20,
    min_edge: float = 0.0,
    out_dir: Optional[Path] = None,
) -> dict:
    """Run factor IC analysis and return summary."""
    print(f"[ic] Factor IC analysis")
    print(f"[ic] Range: {start} → {end}")
    print(f"[ic] City: {city or 'ALL'}  Variable: {variable or 'ALL'}")
    print(f"[ic] Rolling window: {window_size} trades")

    trades = _fetch_trades(start, end, city=city, variable=variable, min_edge=min_edge)
    print(f"[ic] Fetched {len(trades)} trades ({sum(1 for t in trades if t.get('actual_outcome') is not None)} resolved)")

    if not trades:
        print("[ic] No trades found — check date range and city filter")
        return {}

    # Overall IC
    ic_series = _compute_ic_window(trades, window_size=window_size)
    overall_summary = _compute_ic_summary(ic_series)
    _print_summary(overall_summary, label="Overall")

    # IC by lead time
    lead_ic = _compute_ic_by_lead_time(trades)
    print(f"\n{'='*60}")
    print("IC by Lead Time")
    print(f"{'='*60}")
    for label, stats in lead_ic.items():
        ic_str = f"{stats['ic']:+.3f}" if stats['ic'] is not None else "N/A"
        print(f"  {label:<12} IC={ic_str}  n={stats['n']}")

    # Per-variable breakdown
    variables = {}
    for t in trades:
        var = t.get("variable", "unknown")
        variables.setdefault(var, []).append(t)

    var_summaries = {}
    print(f"\n{'='*60}")
    print("IC by Variable")
    print(f"{'='*60}")
    for var, var_trades in variables.items():
        ic_series_var = _compute_ic_window(var_trades, window_size=window_size)
        summary_var = _compute_ic_summary(ic_series_var)
        var_summaries[var] = summary_var
        print(f"  {var:<25} IC_mean={summary_var['ic_mean']:+.3f}  IR={summary_var['ic_sharpe']:+.3f}  n_win={summary_var['n_windows']}")

    result = {
        "overall": overall_summary,
        "by_lead_time": lead_ic,
        "by_variable": var_summaries,
    }

    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "factor_ic_summary.json"
        out_path.write_text(json.dumps(result, indent=2))
        print(f"\n[ic] Wrote summary → {out_path}")

        # Write IC series CSV
        if ic_series:
            csv_path = out_dir / "factor_ic_series.csv"
            with open(csv_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["window_end", "n", "ic"])
                writer.writeheader()
                writer.writerows(ic_series)
            print(f"[ic] Wrote IC series → {csv_path}")

    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Factor IC analysis for GFS weather signals")
    parser.add_argument("--start", default="2024-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--city", default=None, help="City filter")
    parser.add_argument("--variable", default=None, help="Variable filter (e.g. temperature_2m_max)")
    parser.add_argument("--window-size", type=int, default=20, help="Rolling window size (default: 20)")
    parser.add_argument("--min-edge", type=float, default=0.0, help="Minimum edge filter")
    parser.add_argument("--out-dir", default="research/output", help="Output directory")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    end = args.end or date.today().isoformat()
    main(
        start=args.start,
        end=end,
        city=args.city,
        variable=args.variable,
        window_size=args.window_size,
        min_edge=args.min_edge,
        out_dir=Path(args.out_dir),
    )
