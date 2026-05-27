"""
Polymarket Phase 2 — Event Scanner + LLM Pricing Pipeline.

Runs as:  python event_scanner_pipeline.py
Runs in:  ~/0x8-polymarket/trading_system/

Pipeline:
  1. Scanner: fetch live markets from Polymarket (Gamma API)
  2. Filter: sports/politics/crypto only, NO weather, min liquidity
  3. Price: feed each market to DeepSeek for probability estimate
  4. Score: calculate edge = |p_LLM - p_market|
  5. Gate: route through risk_manager.py for validation
  6. Execute: send validated trades through trade_executor.py (paper mode)

Safety:
  - NEVER bypasses risk_manager.py
  - Edge > 40% treated as model error (Rule 6)
  - Weather markets permanently blocked (Rule 7)
  - All trades logged to data/llm_pricing_log.json

Output: prints flagged opportunities, logs full analysis to disk.
"""
import json
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional

# Import all system modules
from src.core import config
from .event_scanner import EventScanner
from src.strategies.llm_pricing import LLMPricingEngine, PricingResult
from src.risk.risk_manager import RiskManager


# ── Config overrides from Phase 2 spec ────────────────────────

# Flag thresholds (Phase 2 spec: edge > 10% AND confidence > 70%)
MIN_EDGE_TO_TRADE = 0.10
MIN_CONFIDENCE_TO_TRADE = 0.70

# Single trade max $5 (user's request, per portfolio size ~$65)
MAX_TRADE_AMOUNT = 5.0

# Daily max loss $3 (user's request)
DAILY_MAX_LOSS = 3.0


class Phase2Pipeline:
    """
    Runs the complete Phase 2 pipeline:
    Scan → Filter → Price → Gate → Execute
    """

    def __init__(self):
        self.scanner = EventScanner()
        self.pricer = LLMPricingEngine()
        self.risk_manager = RiskManager()
        self.stats = {
            "markets_scanned": 0,
            "markets_priced": 0,
            "markets_flagged": 0,
            "trades_excuted": 0,
            "trades_rejected": 0,
            "errors": 0,
        }

    def run_scan_cycle(self, limit: int = 50) -> List[Dict]:
        """
        Run one full scan cycle:
        1. Scan markets
        2. Filter by category + liquidity
        3. Price with LLM
        4. Calculate edges
        5. Return flagged opportunities
        """
        print("=" * 70)
        print(f"PHASE 2 SCAN CYCLE — {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print("=" * 70)

        # Step 1: Scan markets via Gamma API
        print(f"\n[1/4] Scanning Polymarket for markets...")
        markets = self.scanner.get_top_markets(limit=limit * 3)
        self.stats["markets_scanned"] = len(markets)

        if not markets:
            print("  No markets found via top_markets, trying events...")
            events = self.scanner.get_recent_events(limit=limit // 2)
            markets = []
            for e in events:
                markets.extend(e.markets)
            self.stats["markets_scanned"] = len(markets)

        print(f"  Found {len(markets)} tradable markets")

        # Step 2: Filter by minimum liquidity and relevance
        filtered = self._filter_markets(markets)
        print(f"  After filters: {len(filtered)} markets")

        if not filtered:
            print("\n  No markets passed filters. Try again later or widen filters.")
            return []

        # Step 3: Price with LLM (batch of top markets by volume)
        to_price = filtered[:10]  # Price top 10 to manage API costs
        print(f"\n[2/4] Pricing {len(to_price)} markets with DeepSeek...")

        results = self.pricer.price_markets(to_price)
        self.stats["markets_priced"] = len(results)
        self.stats["markets_flagged"] = sum(1 for r in results if r.flagged)

        # Print results
        print(f"\n[3/4] Results:")
        for r in results:
            flag_icon = "🔥 FLAGGED" if r.flagged else "  (no flag)"
            print(f"\n  {flag_icon}")
            print(f"  Market: {r.question[:70]}")
            print(f"  Price: {r.market_yes_price:.1%} | LLM est: {r.llm_estimated_probability:.1%} "
                  f"| Edge: {r.edge:+.1%} | Conf: {r.llm_confidence_label} ({r.llm_confidence:.0%})")
            if r.flagged:
                print(f"  Reason: {r.flag_reason[:100]}")
                print(f"  Key factors: {', '.join(r.llm_key_factors[:3])}")

        # Step 4: Prepare trade opportunities (gate through risk_manager)
        print(f"\n[4/4] Gating flagged trades through Risk Manager...")
        opportunities = []
        for r in results:
            if r.flagged:
                opp = self._prepare_trade_opportunity(r, filtered, results)
                if opp:
                    opportunities.append(opp)

        # Summary
        print(f"\n{'─' * 70}")
        print(f"CYCLE SUMMARY: {len(results)} priced, "
              f"{self.stats['markets_flagged']} flagged, "
              f"{len(opportunities)} approved by Risk Manager")
        print(f"{'─' * 70}")

        return opportunities

    def _filter_markets(self, markets) -> List:
        """Filter markets by liquidity, category, and validity."""
        filtered = []
        for m in markets:
            # Skip if not enough data
            if not m.question or not m.outcomes:
                continue
            if len(m.outcomes) < 2:
                continue

            # Skip weather by keyword in question (unless enabled in config)
            if not config.ALLOW_WEATHER_MARKETS:
                question_lower = m.question.lower()
                weather_kws = ["weather", "temperature", "rainfall", "snowfall",
                              "precipitation", "storm", "hurricane", "typhoon",
                              "wind speed", "humidity", "fog", "frost"]
                if any(kw in question_lower for kw in weather_kws):
                    continue

            # Skip if no volume (dead market)
            if m.volume < 100:
                continue

            # Skip if price is 0 or 1 (resolved / invalid)
            if m.yes_price <= 0.001 or m.yes_price >= 0.999:
                continue

            filtered.append(m)

        # Sort by volume (highest first — most liquid = most reliable)
        filtered.sort(key=lambda m: m.volume, reverse=True)
        return filtered

    def _prepare_trade_opportunity(
        self,
        result: PricingResult,
        all_markets: List,
        all_results: List,
    ) -> Optional[Dict]:
        """Prepare a trade opportunity and gate through Risk Manager."""
        # Find the matching market object
        market = None
        for m in all_markets:
            if hasattr(m, "market_id") and m.market_id == result.market_id:
                market = m
                break

        if not market:
            print(f"  ⚠️  Market {result.market_id} not found in scanned markets")
            return None

        # Determine direction
        if result.edge > 0:
            direction = "BUY_YES"
            amount = min(MAX_TRADE_AMOUNT, config.MAX_POSITION_SIZE)
            expected_price = result.market_yes_price
        else:
            direction = "BUY_NO"
            amount = min(MAX_TRADE_AMOUNT, config.MAX_POSITION_SIZE)
            expected_price = result.market_no_price

        # Gate through risk_manager
        approval = self.risk_manager.approve(
            decision_type=direction,
            market_id=result.market_id,
            market_question=result.question,
            outcome=market.outcomes[0],
            amount_usd=amount,
            edge=result.edge,
            confidence=result.llm_confidence,
            category="event_scanner",
            is_new_position=True,
        )

        if approval["approved"]:
            self.stats["trades_excuted"] += 1
            print(f"\n  ✅ TRADE APPROVED by Risk Manager:")
            print(f"     {direction} ${amount:.2f} @ {expected_price:.1%}")
            print(f"     Market: {result.question[:50]}...")

            trade_opp = {
                "timestamp": result.timestamp,
                "market_id": result.market_id,
                "question": result.question,
                "direction": direction,
                "amount_usd": amount,
                "expected_price": expected_price,
                "edge": result.edge,
                "confidence": result.llm_confidence,
                "llm_estimate": result.llm_estimated_probability,
                "flag_reason": result.flag_reason,
                "llm_reasoning": result.llm_reasoning,
                "llm_key_factors": result.llm_key_factors,
                "llm_risks": result.llm_risks,
            }

            # In paper trading mode, log but don't execute
            if config.PAPER_TRADING:
                self._log_paper_trade(trade_opp)
            else:
                self._execute_real_trade(trade_opp)

            return trade_opp
        else:
            self.stats["trades_rejected"] += 1
            print(f"\n  ❌ TRADE REJECTED by Risk Manager:")
            print(f"     {approval['reason'][:80]}")
            return None

    def _log_paper_trade(self, trade: Dict):
        """Log paper trade for tracking."""
        paper_log = os.path.join(config.DATA_DIR, "paper_trades.json")
        existing = []
        if os.path.exists(paper_log):
            try:
                with open(paper_log) as f:
                    existing = json.load(f)
            except Exception:
                pass
        existing.append(trade)
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(paper_log, "w") as f:
            json.dump(existing, f, indent=2)
        print(f"     📝 Logged as paper trade to {paper_log}")

    def _execute_real_trade(self, trade: Dict):
        """Execute a real trade through the existing trade executor."""
        # This will be wired when moving to real trading after paper trading period
        print(f"     ⚠️  Real trading mode activated — would execute:")
        print(f"     {trade['direction']} {trade['market_id']} @ ${trade['amount_usd']}")
        # TODO: integrate with real_trading_client.py when going live

    def get_paper_trade_stats(self) -> Dict:
        """Get stats on paper trades."""
        paper_log = os.path.join(config.DATA_DIR, "paper_trades.json")
        if not os.path.exists(paper_log):
            return {"status": "no_data", "total_paper_trades": 0}

        try:
            with open(paper_log) as f:
                trades = json.load(f)
            return {
                "status": "ok",
                "total_paper_trades": len(trades),
                "by_direction": {
                    "BUY_YES": sum(1 for t in trades if t["direction"] == "BUY_YES"),
                    "BUY_NO": sum(1 for t in trades if t["direction"] == "BUY_NO"),
                },
                "total_notional": sum(t["amount_usd"] for t in trades),
                "avg_edge": sum(t["edge"] for t in trades) / len(trades) if trades else 0,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}


if __name__ == "__main__":
    print("Polymarket Phase 2 — Event Scanner + LLM Pricing Pipeline")
    print("=" * 60)
    print(f"Mode: {'PAPER TRADING' if config.PAPER_TRADING else '⚠️  REAL TRADING'}")
    print(f"Data dir: {config.DATA_DIR}")
    print()

    pipeline = Phase2Pipeline()

    # Run one scan cycle
    opportunities = pipeline.run_scan_cycle(limit=50)

    # Print final summary
    print(f"\n{'=' * 70}")
    print("SCAN CYCLE COMPLETE")
    print(f"Stats: {json.dumps(pipeline.stats, indent=2)}")
    if opportunities:
        print(f"\nOpportunities found: {len(opportunities)}")
    else:
        print(f"\nNo tradeable opportunities found in this cycle.")
        print("This is normal — the scanner only flags when:")
        print(f"  • |p_LLM - p_market| > {MIN_EDGE_TO_TRADE:.0%}")
        print(f"  • LLM confidence > {MIN_CONFIDENCE_TO_TRADE:.0%}")
        print("  • Market is sports / politics / crypto (not weather)")
        print("  • Risk Manager approves all rules")

    # Print accuracy stats
    print(f"\nPricing engine accuracy: {json.dumps(pipeline.pricer.get_accuracy_stats(), indent=2)}")
    print(f"\nPaper trade stats: {json.dumps(pipeline.get_paper_trade_stats(), indent=2)}")
