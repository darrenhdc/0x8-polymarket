"""
Backtester — replays predictions against historical market data.

Core flow:
  1. Load historical snapshots (from HistoricalCollector)
  2. Load your predictions (from PersonalPredictionSource)
  3. For each matching date: run prediction → edge check → simulated trade
  4. Track PnL against actual resolutions
  5. Report: win rate, total PnL, Sharpe ratio, calibration

Usage:
  backtester = Backtester()
  report = backtester.run(
      predictions=personal_prediction_source,
      date_range=("2026-05-01", "2026-05-18")
  )
  backtester.print_report(report)

Or quick-test a single market with known resolution:
  backtester.simulate_one(
      market_id="...",
      market_price=0.35,
      your_prediction=0.65,
      actual_outcome=True,   # did the event happen?
  )
"""

import json
import os
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import config
from historical_data import HistoricalCollector, HISTORICAL_DIR
from gfs_weather_source import (
    find_city_coords, extract_threshold, extract_date,
    fetch_gfs_historical_forecast, fetch_observed_temperature,
    prob_exceed_threshold, CITIES,
)


# ── Data types ──────────────────────────────────────────────────

class SimulatedTrade:
    """One simulated backtest trade."""
    def __init__(self, market_id: str, question: str, direction: str,
                 market_price: float, prediction_prob: float, confidence: float,
                 amount: float, date: str, actual_outcome: Optional[bool] = None):
        self.market_id = market_id
        self.question = question
        self.direction = direction
        self.market_price = market_price
        self.prediction_prob = prediction_prob
        self.confidence = confidence
        self.amount = amount
        self.date = date
        self.actual_outcome = actual_outcome  # True/False/None (unresolved)
        self.pnl = 0.0
        self.won = False
        self._calc_pnl()

    def _calc_pnl(self):
        if self.actual_outcome is None:
            return
        # If we bought YES and it happened, we win (get tokens * 1.0)
        # If we bought YES and it didn't, we lose (tokens worth 0)
        # If we bought NO and it didn't happen, we win
        tokens = self.amount / self.market_price
        if self.direction == "BUY_YES":
            self.won = self.actual_outcome
            self.pnl = (tokens * 1.0 - self.amount) if self.actual_outcome else -self.amount
        else:  # BUY_NO
            self.won = not self.actual_outcome
            self.pnl = (tokens * 1.0 - self.amount) if not self.actual_outcome else -self.amount

    def to_dict(self) -> Dict:
        return {
            "market_id": self.market_id,
            "question": self.question,
            "direction": self.direction,
            "market_price": self.market_price,
            "prediction": self.prediction_prob,
            "confidence": self.confidence,
            "amount": self.amount,
            "date": self.date,
            "actual_outcome": self.actual_outcome,
            "pnl": self.pnl,
            "won": self.won,
        }


class BacktestReport:
    """Performance report from a backtest run."""
    def __init__(self):
        self.trades: List[SimulatedTrade] = []
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.unresolved = 0
        self.total_pnl = 0.0
        self.total_invested = 0.0
        self.win_rate = 0.0
        self.sharpe_ratio = 0.0
        self.max_drawdown = 0.0
        self.max_drawdown_pct = 0.0
        self.avg_edge = 0.0
        self.calibration_score = 0.0   # how well predictions match actual outcomes
        self.by_market = []

    def compute(self):
        self.total_trades = len(self.trades)
        resolved = [t for t in self.trades if t.actual_outcome is not None]
        self.unresolved = self.total_trades - len(resolved)

        if resolved:
            self.winning_trades = sum(1 for t in resolved if t.won)
            self.losing_trades = len(resolved) - self.winning_trades
            self.win_rate = self.winning_trades / len(resolved)
            self.total_pnl = sum(t.pnl for t in resolved)
            self.total_invested = sum(t.amount for t in resolved)
            self.avg_edge = sum(abs(t.prediction_prob - t.market_price) for t in resolved) / len(resolved)

            # Sharpe ratio (simplified — assumes risk-free rate = 0)
            if len(resolved) >= 2:
                returns = [t.pnl / t.amount for t in resolved]
                mean_ret = sum(returns) / len(returns)
                variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
                if variance > 0:
                    self.sharpe_ratio = mean_ret / math.sqrt(variance) * math.sqrt(252)  # annualized
                else:
                    self.sharpe_ratio = 0.0

            # Max drawdown
            cumulative = 0.0
            peak = 0.0
            max_dd = 0.0
            for t in resolved:
                cumulative += t.pnl
                peak = max(peak, cumulative)
                dd = peak - cumulative
                max_dd = max(max_dd, dd)
            self.max_drawdown = max_dd
            self.max_drawdown_pct = max_dd / self.total_invested if self.total_invested > 0 else 0

            # Calibration: compare prediction probability to actual frequency
            # Group predictions into buckets and compare
            buckets = {0.1: [], 0.3: [], 0.5: [], 0.7: [], 0.9: []}
            for t in resolved:
                for b in sorted(buckets.keys()):
                    if t.prediction_prob <= b + 0.1:
                        buckets[b].append(t)
                        break
            cal_errors = []
            for b, trades in buckets.items():
                if trades:
                    actual_freq = sum(1 for t in trades if (t.direction == "BUY_YES" and t.actual_outcome) or
                                     (t.direction == "BUY_NO" and not t.actual_outcome)) / len(trades)
                    cal_errors.append(abs(b - actual_freq))
            if cal_errors:
                self.calibration_score = 1.0 - sum(cal_errors) / len(cal_errors)

    def to_dict(self) -> Dict:
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "unresolved": self.unresolved,
            "win_rate": self.win_rate,
            "total_pnl": round(self.total_pnl, 4),
            "total_invested": round(self.total_invested, 4),
            "roi": round(self.total_pnl / self.total_invested * 100, 2) if self.total_invested > 0 else 0,
            "sharpe_ratio": round(self.sharpe_ratio, 2),
            "max_drawdown": round(self.max_drawdown, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct * 100, 2),
            "avg_edge": round(self.avg_edge, 4),
            "calibration_score": round(self.calibration_score, 2),
        }


# ── Backtester ──────────────────────────────────────────────────

class Backtester:
    """
    Runs backtesting: replays predictions against historical market data.
    """

    def __init__(self, min_edge: float = 0.10, min_confidence: float = 0.70,
                 max_trade: float = 5.0):
        self.min_edge = min_edge
        self.min_confidence = min_confidence
        self.max_trade = max_trade
        self.collector = HistoricalCollector()

    # ── Single-market simulation ────────────────────────────────

    def simulate_one(
        self,
        market_id: str,
        question: str,
        market_price: float,
        your_prediction: float,
        confidence: float,
        actual_outcome: Optional[bool],
        amount: float = 5.0,
        date: str = "",
    ) -> SimulatedTrade:
        """
        Simulate a single trade.  The simplest entry point.

        Args:
            market_price: current market probability (0-1) for YES
            your_prediction: your probability estimate (0-1) for YES
            confidence: how confident you are (0-1)
            actual_outcome: True if YES won, False if NO won, None if unresolved

        Returns SimulatedTrade.
        """
        edge = your_prediction - market_price
        abs_edge = abs(edge)

        # Check if trade would have been made
        if abs_edge < self.min_edge or confidence < self.min_confidence:
            return SimulatedTrade(
                market_id=market_id, question=question,
                direction="HOLD", market_price=market_price,
                prediction_prob=your_prediction, confidence=confidence,
                amount=0, date=date, actual_outcome=actual_outcome,
            )

        direction = "BUY_YES" if edge > 0 else "BUY_NO"
        return SimulatedTrade(
            market_id=market_id, question=question,
            direction=direction, market_price=market_price,
            prediction_prob=your_prediction, confidence=confidence,
            amount=min(amount, self.max_trade), date=date,
            actual_outcome=actual_outcome,
        )

    def simulate_batch(self, scenarios: List[Dict]) -> BacktestReport:
        """
        Simulate multiple trades from scenarios list.

        Each scenario dict:
          {
            market_id, question, market_price, your_prediction,
            confidence, actual_outcome (True/False/None), amount (default 5),
            date (optional)
          }

        Returns BacktestReport.
        """
        report = BacktestReport()
        for s in scenarios:
            trade = self.simulate_one(
                market_id=s.get("market_id", ""),
                question=s.get("question", ""),
                market_price=s["market_price"],
                your_prediction=s["your_prediction"],
                confidence=s.get("confidence", 0.7),
                actual_outcome=s.get("actual_outcome"),
                amount=s.get("amount", 5.0),
                date=s.get("date", ""),
            )
            if trade.direction != "HOLD":
                report.trades.append(trade)

        report.compute()
        return report

    # ── GFS-driven historical backtest ─────────────────────────

    def run_gfs(
        self,
        date_range: tuple,
        sigma: float = 0.7,
        cities: List[str] = None,
        questions: List[Dict] = None,
    ) -> "BacktestReport":
        """
        Real GFS historical backtest — NO Monte Carlo.

        For each date in date_range, fetches the actual GFS forecast that
        was available that day (via Open-Meteo Historical Forecast Archive),
        computes P(temp > threshold), compares to a hypothetical market price,
        and checks outcome against ERA5 observed temperature.

        Two input modes:
          1. questions list — explicit list of {question, market_price, market_id}
             dicts.  Each question is analysed for every date in the range.
          2. cities + auto-generate — if questions is None, generates test
             scenarios for each city × threshold combination.

        Args:
            date_range: ("YYYY-MM-DD", "YYYY-MM-DD")
            sigma:      GFS error std-dev (default 0.7°C)
            cities:     list of city names (keys in CITIES dict); None → all cities
            questions:  explicit list of dicts with keys:
                          market_id, question, market_price (0-1),
                          threshold (°C), lat, lon
                        If provided, overrides cities.

        Returns BacktestReport.
        """
        from datetime import datetime, timedelta

        start_str, end_str = date_range
        start = datetime.fromisoformat(start_str)
        end   = datetime.fromisoformat(end_str)

        report   = BacktestReport()
        skipped  = 0
        fetched  = 0

        # Build city list
        if questions is not None:
            city_specs = questions  # user-supplied
        else:
            target_cities = cities if cities else list(CITIES.keys())
            # Default: one threshold per city (35°C for northern, 30°C for others)
            city_specs = []
            for city in target_cities:
                lat, lon = CITIES[city]
                threshold = 35.0 if lat > 30 else 30.0
                city_specs.append({
                    "market_id":    f"gfs_bt_{city.replace(' ', '_')}",
                    "question":     f"Will temperature in {city.title()} exceed {threshold:.0f}C?",
                    "market_price": 0.50,   # assume 50/50 baseline for auto-generated
                    "threshold":    threshold,
                    "lat":          lat,
                    "lon":          lon,
                })

        current = start
        while current <= end:
            date_str = current.strftime("%Y-%m-%d")

            for spec in city_specs:
                lat       = spec.get("lat")
                lon       = spec.get("lon")
                threshold = spec.get("threshold")

                # Auto-extract if not provided
                if lat is None or lon is None:
                    coords = find_city_coords(spec.get("question", ""))
                    if not coords:
                        skipped += 1
                        current += timedelta(days=1)
                        continue
                    lat, lon = coords
                if threshold is None:
                    threshold = extract_threshold(spec.get("question", ""))
                    if threshold is None:
                        skipped += 1
                        current += timedelta(days=1)
                        continue

                # 1. Fetch what GFS said on that date
                gfs_data = fetch_gfs_historical_forecast(lat, lon, date_str)
                if not gfs_data:
                    skipped += 1
                    current += timedelta(days=1)
                    continue

                # 2. Compute model probability
                forecast_temp = gfs_data["forecast_temp"]
                pred_prob = prob_exceed_threshold(forecast_temp, threshold, sigma)
                pred_prob = max(0.001, min(0.999, pred_prob))

                # Confidence: how far from threshold in sigma units
                z = abs(forecast_temp - threshold) / sigma
                confidence = 0.90 if z > 2.0 else 0.75 if z > 1.0 else 0.60 if z > 0.5 else 0.50

                # 3. Fetch ERA5 observed temperature (ground truth)
                observed = fetch_observed_temperature(lat, lon, date_str)
                actual_outcome: Optional[bool] = None
                if observed is not None:
                    actual_outcome = observed > threshold

                fetched += 1

                trade = self.simulate_one(
                    market_id       = spec["market_id"],
                    question        = spec.get("question", ""),
                    market_price    = spec.get("market_price", 0.50),
                    your_prediction = pred_prob,
                    confidence      = confidence,
                    actual_outcome  = actual_outcome,
                    date            = date_str,
                )
                if trade.direction != "HOLD":
                    report.trades.append(trade)

            current += timedelta(days=1)

        report.compute()
        print(
            f"[Backtester-GFS] {date_range[0]} → {date_range[1]} | "
            f"fetched={fetched} skipped={skipped} trades={len(report.trades)}"
        )
        return report

    # ── Full backtest against historical data ───────────────────

    def run(
        self,
        predictions_source,  # PersonalPredictionSource or compatible
        date_range: Tuple[str, str] = None,
    ) -> BacktestReport:
        """
        Full backtest: for each historical snapshot date, check if any
        of your predictions match a market, compute edge, simulate trade,
        and track resolution.

        This requires:
          - Historical snapshots (data/historical/snapshot_*.json)
          - Predictions from a PersonalPredictionSource
          - Resolved market outcomes (tracked automatically or manually provided)
        """
        report = BacktestReport()

        # Determine date range
        if date_range:
            start_date, end_date = date_range
        else:
            all_dates = self.collector.list_snapshots()
            if not all_dates:
                print("[Backtester] No historical snapshots found. Run HistoricalCollector first.")
                return report
            start_date = all_dates[0]
            end_date = all_dates[-1]

        # Collect resolved market outcomes
        resolved = self.collector.get_resolved_markets()
        resolved_map = {r["market_id"]: r for r in resolved}

        # Iterate through each snapshot date
        current = datetime.fromisoformat(start_date)
        end = datetime.fromisoformat(end_date)
        processed = 0

        while current <= end:
            date_str = current.strftime("%Y-%m-%d")
            snapshots = self.collector.get_snapshot(date_str)

            if not snapshots:
                current += timedelta(days=1)
                continue

            for snap in snapshots:
                # Check if any prediction matches this market
                pred = self._find_prediction(snap, predictions_source)
                if not pred:
                    continue

                market_price = snap["outcome_prices"][0] if snap["outcome_prices"] else 0.5
                pred_prob = pred["estimated_probability"]
                confidence = pred.get("confidence", 0.7)

                # Actual outcome from resolution tracking
                actual = None
                resolution = resolved_map.get(snap["market_id"])
                if resolution:
                    final_prices = resolution.get("final_prices", [])
                    if final_prices:
                        # Index 0 = YES, if 1.0 → YES won; if 0.0 → NO won
                        actual = final_prices[0] > 0.5

                trade = self.simulate_one(
                    market_id=snap["market_id"],
                    question=snap["question"],
                    market_price=market_price,
                    your_prediction=pred_prob,
                    confidence=confidence,
                    actual_outcome=actual,
                    date=date_str,
                )

                if trade.direction != "HOLD":
                    report.trades.append(trade)
                    processed += 1

            current += timedelta(days=1)

        report.compute()
        print(f"[Backtester] Processed {processed} trades across {date_range or 'all snapshots'}")
        return report

    def _find_prediction(self, snap: Dict, source) -> Optional[Dict]:
        """Find a prediction matching a market snapshot."""
        # Try exact market_id match
        for mid, row in source._predictions.items():
            if mid == snap["market_id"]:
                return row
            # Fuzzy question match
            q = snap.get("question", "").lower()
            row_q = row.get("question", "").lower()
            if row_q and (row_q in q or q in row_q):
                return row
        return None

    # ── Report printing ──────────────────────────────────────────

    def print_report(self, report: BacktestReport):
        """Pretty-print a backtest report."""
        d = report.to_dict()
        print()
        print("=" * 60)
        print("  BACKTEST REPORT")
        print("=" * 60)
        print(f"  Trades:      {d['total_trades']} total")
        print(f"               {d['winning_trades']} won / {d['losing_trades']} lost / {d['unresolved']} unresolved")
        print(f"  Win rate:    {d['win_rate']:.1%} (resolved only)")
        print(f"  Total PnL:   ${d['total_pnl']:+.4f}")
        print(f"  Invested:    ${d['total_invested']:.2f}")
        print(f"  ROI:         {d['roi']:+.2f}%")
        print(f"  Sharpe:      {d['sharpe_ratio']:.2f}")
        print(f"  Max DD:      ${d['max_drawdown']:.4f} ({d['max_drawdown_pct']:.1f}%)")
        print(f"  Avg Edge:    {d['avg_edge']:.1%}")
        print(f"  Calibration: {d['calibration_score']:.2f} (1.0 = perfect)")

        if d['calibration_score'] < 0.5:
            print(f"  ⚠️ LOW CALIBRATION — predictions are not well-calibrated")
        elif d['calibration_score'] < 0.8:
            print(f"  ⚡ Moderate calibration — room for improvement")
        else:
            print(f"  ✅ Good calibration")

        if d['sharpe_ratio'] > 1.0:
            print(f"  ✅ Sharpe > 1.0 — strategy has edge")
        elif d['sharpe_ratio'] > 0:
            print(f"  ⚡ Sharpe positive but low")
        else:
            print(f"  ❌ Negative Sharpe — strategy loses money")

        print("=" * 60)

        # Trade details
        if report.trades:
            print(f"\n  Trade details:")
            for i, t in enumerate(report.trades[:10]):
                resolved_str = ""
                if t.actual_outcome is not None:
                    resolved_str = f" → {'✅ WON' if t.won else '❌ LOST'} ${t.pnl:+.2f}"
                else:
                    resolved_str = " → (unresolved)"
                print(f"  [{i+1}] {t.direction} {t.question[:40]}...")
                print(f"       mkt={t.market_price:.0%} pred={t.prediction_prob:.0%} conf={t.confidence:.0%}{resolved_str}")


# ── Demo / quick test ───────────────────────────────────────────

if __name__ == "__main__":
    bt = Backtester(min_edge=0.10, min_confidence=0.70)

    # Quick test: simulate 5 weather market scenarios with known outcomes
    scenarios = [
        {
            "market_id": "w1", "question": "Beijing temp >35C July 1?",
            "market_price": 0.35, "your_prediction": 0.65,
            "confidence": 0.80, "actual_outcome": True,   # your prediction was right!
        },
        {
            "market_id": "w2", "question": "Shanghai rain >50mm July 2?",
            "market_price": 0.25, "your_prediction": 0.40,
            "confidence": 0.65, "actual_outcome": False,   # edge OK but confidence below threshold → no trade
        },
        {
            "market_id": "w3", "question": "Tokyo temp >30C July 3?",
            "market_price": 0.42, "your_prediction": 0.55,
            "confidence": 0.75, "actual_outcome": True,
        },
        {
            "market_id": "w4", "question": "Seoul rain >10mm July 4?",
            "market_price": 0.60, "your_prediction": 0.30,  # you're bearish on rain
            "confidence": 0.80, "actual_outcome": False,     # you were right — it didn't rain
        },
        {
            "market_id": "w5", "question": "NYC temp >30C July 5?",
            "market_price": 0.50, "your_prediction": 0.55,
            "confidence": 0.72, "actual_outcome": False,     # you were wrong
        },
    ]

    report = bt.simulate_batch(scenarios)
    bt.print_report(report)
