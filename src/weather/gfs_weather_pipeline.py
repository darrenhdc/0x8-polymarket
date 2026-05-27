"""
GFS Weather Trading Pipeline — end-to-end weather prediction market strategy.

Flow:
  1. Scan Polymarket for temperature-threshold markets
  2. For each: fetch GFS T+1 forecast via Open-Meteo
  3. Compute P(temp > threshold) using sigma=0.7°C normal model
  4. Calculate edge = model_prob - market_price
  5. If |edge| > 10%, route through risk_manager
  6. Execute as paper trade (or real if PAPER_TRADING=false)

Usage:
  python gfs_weather_pipeline.py           # scan + predict + (paper) trade
  python gfs_weather_pipeline.py --backtest  # backtest with past scenarios
  python gfs_weather_pipeline.py --demo      # show model mechanics
"""

import json
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from src.core import config
from src.data.prediction_interface import PredictionRegistry, get_registry, MarketContext
from src.strategies.personal_source import PersonalPredictionSource
from src.weather.gfs_weather_source import (
    GFSWeatherSource,
    prob_exceed_threshold,
    fetch_gfs_forecast,
    find_city_coords,
    extract_threshold,
    extract_date,
)
from src.data.edge_composer import EdgeComposer, compute_edge
from src.risk.risk_manager import RiskManager
from src.backtest.backtester import Backtester


# ── Pipeline ────────────────────────────────────────────────────

class GFSWeatherPipeline:
    """
    Full pipeline: scan Polymarket → GFS forecast → probability → edge → trade
    """

    def __init__(self, sigma: float = 0.7, min_edge: float = 0.10,
                 min_confidence: float = 0.70):
        self.sigma = sigma
        self.min_edge = min_edge
        self.min_confidence = min_confidence

        # Init components
        self.gfs_source = GFSWeatherSource(sigma=sigma)
        self.risk_manager = RiskManager()

    def run_scan(self) -> List[Dict]:
        """
        Scan Polymarket for temperature markets, compute GFS probabilities,
        check for edge, gate through risk_manager.
        Returns list of actionable trades.
        """
        print("=" * 70)
        print(f"  GFS Weather Pipeline — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"  sigma={self.sigma}°C  min_edge={self.min_edge:.0%}  min_conf={self.min_confidence:.0%}")
        print("=" * 70)

        # Step 1: Scan Polymarket for weather markets
        print("\n[1/4] Scanning Polymarket for temperature markets...")
        try:
            from src.data.event_scanner import EventScanner
            scanner = EventScanner()
            all_markets = scanner.scan_markets(limit=500)
        except Exception as e:
            print(f"  Scan error: {e}")
            return []

        # Filter: only temperature threshold markets
        temp_markets = []
        for m in all_markets:
            q = m.question.lower()
            if not any(kw in q for kw in ['temperature', 'temp', '°c', '°f', 'celsius', 'fahrenheit']):
                continue
            if not find_city_coords(m.question):
                continue
            if not extract_threshold(m.question):
                continue
            temp_markets.append(m)

        print(f"  Found {len(temp_markets)} temperature threshold markets")

        if not temp_markets:
            print("  ⚠️ No temperature markets found. Try again when Polymarket has weather events.")
            self._show_model_demo()
            return []

        for m in temp_markets:
            print(f"  • {m.question[:70]}")

        # Step 2: GFS forecast + probability for each
        print(f"\n[2/4] Fetching GFS forecasts and computing probabilities...")
        predictions = []
        for m in temp_markets:
            context = MarketContext(
                market_id=m.market_id,
                question=m.question,
                outcomes=m.outcomes,
                outcome_prices=[m.yes_price, m.no_price],
                volume=m.volume,
                liquidity=m.liquidity,
                category="weather",
            )

            if self.gfs_source.can_predict(context):
                pred = self.gfs_source.predict(context)
                if pred:
                    predictions.append((m, context, pred))
                    print(f"  • {m.question[:50]}...")
                    print(f"    GFS prob={pred.estimated_probability:.1%} (conf={pred.confidence:.0%}) "
                          f"vs market={m.yes_price:.1%}")

        # Step 3: Compute edges
        print(f"\n[3/4] Computing edges...")
        trades = []
        for m, ctx, pred in predictions:
            signal = compute_edge(pred, ctx.outcome_prices[0], ctx,
                                 min_edge=self.min_edge,
                                 min_confidence=self.min_confidence,
                                 max_sane_edge=0.40)

            if signal.flagged:
                print(f"\n  🔥 FLAGGED: {m.question[:60]}")
                print(f"     Direction: {signal.direction}")
                print(f"     Edge: {signal.edge:+.1%}")
                print(f"     Reason: {signal.flag_reason}")

                # Step 4: Gate through risk_manager
                approval = self.risk_manager.approve(
                    decision_type=signal.direction,
                    market_id=m.market_id,
                    market_question=m.question,
                    outcome=m.outcomes[0],
                    amount_usd=min(5.0, config.MAX_POSITION_SIZE),
                    edge=signal.edge,
                    confidence=pred.confidence,
                    category="weather",
                    is_new_position=True,
                )

                if approval["approved"]:
                    print(f"     ✅ Risk Manager APPROVED")
                    trades.append({
                        "market": m,
                        "signal": signal,
                        "prediction": pred,
                        "approval": approval,
                    })
                else:
                    print(f"     ❌ Risk Manager: {approval['reason']}")
            else:
                pass  # No edge, skip

        if not trades:
            print(f"\n  No trades with sufficient edge found.")
            print(f"  This is normal — wait for temperature markets with uncertainty near thresholds.")

        # Summary
        print(f"\n{'─' * 70}")
        print(f"  SUMMARY: {len(temp_markets)} markets, {len(predictions)} predicted, "
              f"{len(trades)} tradeable")
        print(f"{'─' * 70}")

        return trades

    def _show_model_demo(self):
        """Show model mechanics when no live markets exist."""
        print()
        print("  ── Model Demo (no live temperature markets) ──")
        print()
        tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime('%Y-%m-%d')

        demo_cities = [
            ("Dubai", 25.20, 55.27, 35),
            ("Phoenix", 33.45, -112.07, 35),
            ("Singapore", 1.35, 103.82, 30),
            ("Beijing", 39.90, 116.40, 25),
        ]

        print(f"  GFS T+1 forecast for {tomorrow}:")
        print(f"  {'City':>12} {'Forecast':>8} {'Threshold':>10} {'z':>7} {'P(exceed)':>10}  Signal")
        print(f"  {'─'*65}")

        for name, lat, lon, th in demo_cities:
            fc = fetch_gfs_forecast(lat, lon, tomorrow)
            if fc:
                temp = fc['forecast_temp']
                p = prob_exceed_threshold(temp, th, self.sigma)
                z = (th - temp) / self.sigma
                if p > 0.90:
                    sig = "BUY_YES 🔴"
                elif p < 0.10:
                    sig = "BUY_NO  🔵"
                else:
                    sig = "HOLD    —"
                print(f"  {name:>12} {temp:>6.1f}°C {th:>7}°C {z:>+6.1f} {p:>10.1%}  {sig}")
            else:
                print(f"  {name:>12}  API error")

        print()
        print(f"  Model: P(temp > T) = 1 - Φ((T - forecast) / {self.sigma})")
        print(f"  Edge = P_model - P_market")
        print(f"  Trade if |edge| > {self.min_edge:.0%} and confidence > {self.min_confidence:.0%}")


# ── Backtest mode ────────────────────────────────────────────────

def run_backtest(sigma: float = 0.7, n_scenarios: int = 100):
    """Run backtest with GFS-model-generated scenarios."""
    import random
    random.seed(42)

    bt = Backtester(min_edge=0.10, min_confidence=0.70)
    scenarios = []

    cities_data = [
        ("Dubai", 39, 35), ("Phoenix", 34, 35), ("Singapore", 31, 30),
        ("Beijing", 23, 25), ("Tokyo", 28, 30), ("Miami", 30, 32),
        ("London", 22, 25), ("Paris", 24, 28), ("Berlin", 21, 25),
        ("Chicago", 25, 30), ("Dallas", 32, 35), ("Las Vegas", 35, 38),
    ]

    for i in range(n_scenarios):
        city, fc_temp, threshold = random.choice(cities_data)
        fc_temp += random.gauss(0, 0.3)

        prob = prob_exceed_threshold(fc_temp, threshold, sigma)

        # Market pricing with inefficiency (more realistic)
        if prob > 0.85:
            mkt = prob - random.uniform(0.02, 0.12)
        elif prob < 0.15:
            mkt = prob + random.uniform(0.02, 0.12)
        else:
            mkt = prob + random.uniform(-0.08, 0.08)
        mkt = max(0.02, min(0.98, mkt))

        actual_temp = fc_temp + random.gauss(0, sigma)
        actual_outcome = actual_temp > threshold

        scenarios.append({
            "market_id": f"sim_{i}",
            "question": f"{city} temp >{threshold}°C? (fc={fc_temp:.1f})",
            "market_price": mkt,
            "your_prediction": prob,
            "confidence": 0.85 if abs(prob - 0.5) > 0.25 else 0.70,
            "actual_outcome": actual_outcome,
            "amount": 5.0,
        })

    report = bt.simulate_batch(scenarios)
    bt.print_report(report)
    return report


# ── CLI ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="GFS Weather Trading Pipeline")
    parser.add_argument("--backtest", action="store_true", help="Run backtest simulation")
    parser.add_argument("--demo", action="store_true", help="Show model demo only")
    parser.add_argument("--sigma", type=float, default=0.7, help="GFS forecast sigma (default: 0.7)")
    parser.add_argument("--min-edge", type=float, default=0.10, help="Minimum edge to trade")
    args = parser.parse_args()

    pipeline = GFSWeatherPipeline(sigma=args.sigma, min_edge=args.min_edge)

    if args.backtest:
        run_backtest(sigma=args.sigma)
    elif args.demo:
        pipeline._show_model_demo()
    else:
        pipeline.run_scan()
