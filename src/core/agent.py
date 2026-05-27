"""
Polymarket AI Trading Agent
Main autonomous trading loop
"""
import json
import time
import os
import sys
from datetime import datetime
from typing import Optional
from . import config
from .portfolio import PortfolioManager
from src.data.market_data import MarketData
from src.execution.trade_executor import TradeExecutor
from src.strategies.decision_engine import DecisionEngine
from src.risk.risk_manager import RiskManager


class TradingAgent:
    """
    Autonomous trading agent for Polymarket (paper or real trading)
    """

    def __init__(self):
        self.portfolio = PortfolioManager()
        self.market_data = MarketData()
        self.executor = TradeExecutor(self.portfolio)
        self.decision_engine = DecisionEngine(self.portfolio)
        self.risk_manager = RiskManager(portfolio=self.portfolio, trade_executor=self.executor)
        self.cycle_count = 0
        self.running = True
        self.daily_loss_usd = 0.0
        self.daily_loss_reset_date = datetime.utcnow().date()

        # Optional Phase 2 pipeline for information-edge event scanning
        self.phase2_pipeline = None
        self._init_phase2_pipeline()

    def _init_phase2_pipeline(self):
        """Lazy-init the Phase 2 event scanner pipeline if modules are available."""
        try:
            from src.data.event_scanner import EventScanner
            from src.strategies.llm_pricing import LLMPricingEngine
            self.phase2_scanner = EventScanner()
            self.phase2_pricer = LLMPricingEngine()
            self._log("[Phase 2] Event scanner + LLM pricing engine loaded")
        except Exception as e:
            self.phase2_scanner = None
            self.phase2_pricer = None
            self._log(f"[Phase 2] Not loaded: {e}")

    def _log(self, message: str):
        """Log with timestamp"""
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{timestamp}] {message}")

    def _print_banner(self):
        """Print startup banner"""
        mode = "PAPER" if config.PAPER_TRADING else "🔴 REAL"
        print("=" * 60)
        print("   POLYMARKET AI TRADING AGENT")
        print(f"   Mode: {mode}")
        print("=" * 60)
        if not config.PAPER_TRADING:
            print("   ⚠️  REAL MONEY – orders are sent to Polymarket CLOB")
            print(f"   Daily loss limit: ${config.MAX_DAILY_LOSS:.2f}")
            print("=" * 60)

    def _print_status(self):
        """Print current portfolio status"""
        summary = self.portfolio.get_summary()
        print("\n" + "-" * 40)
        print("PORTFOLIO STATUS")
        print("-" * 40)
        print(f"  Cash:           ${summary['cash']:,.2f}")
        print(f"  Positions:      {summary['positions_count']}")
        print(f"  Position Value: ${summary['total_position_value']:,.2f}")
        print(f"  Total Value:    ${summary['total_value']:,.2f}")
        print(f"  Total P&L:      ${summary['total_pnl']:+,.2f} ({summary['total_pnl_percent']:+.2f}%)")
        print(f"  Total Exposure: ${summary['total_exposure']:,.2f}")
        print(f"  Trades:         {summary['total_trades']} (Win rate: {summary['win_rate']:.1f}%)")
        print("-" * 40)

    def _print_positions(self):
        """Print current positions"""
        positions = self.portfolio.portfolio.positions
        if not positions:
            print("\nNo open positions")
            return

        print("\n" + "-" * 40)
        print("OPEN POSITIONS")
        print("-" * 40)
        for market_id, pos in positions.items():
            pnl_str = f"${pos.pnl:+,.2f} ({pos.pnl_percent:+.1f}%)"
            print(f"  [{pos.outcome}] {pos.market_question[:40]}...")
            print(f"       Tokens: {pos.tokens:.2f} @ ${pos.avg_price:.4f}")
            print(f"       Cost: ${pos.cost_usd:.2f}, Value: ${pos.current_value:.2f}")
            print(f"       P&L: {pnl_str}")
        print("-" * 40)

    def update_position_prices(self):
        """Update current prices for all open positions"""
        for market_id, pos in self.portfolio.portfolio.positions.items():
            try:
                market = self.market_data.scanner.api.get_market_by_id(market_id)
                if market:
                    prices = self.market_data.scanner.get_market_prices(market)
                    price = prices.get(pos.outcome.lower(), pos.current_price)
                    self.portfolio.update_position_price(market_id, price)
            except Exception as e:
                self._log(f"Error updating price for {market_id}: {e}")

    def _check_daily_loss_limit(self) -> bool:
        """Return True if we are still within the daily loss limit.
        Delegates to risk_manager for unified tracking."""
        if config.PAPER_TRADING:
            return True
        return self.risk_manager.daily_loss_usd < config.MAX_DAILY_LOSS

    def execute_decision(self, decision) -> bool:
        """Execute a trading decision, gated by risk_manager.approve()."""
        # ── Risk gate: route every trade through risk_manager ──
        # Determine price direction for the amount
        yes_price = decision.market_data.get('yes_price', 0.5)
        no_price = decision.market_data.get('no_price', 0.5)
        decision_type = decision.decision

        # Calculate proposed amount (same logic as before)
        if decision_type.startswith("BUY"):
            base_size = config.MAX_POSITION_SIZE * decision.confidence

            # Adjust for risk tier based on price
            price = yes_price if decision_type == "BUY_YES" else no_price
            if price >= 0.35:
                max_size = config.RISK_TIERS['safe']['max_position']
            elif price >= 0.15:
                max_size = config.RISK_TIERS['medium']['max_position']
            elif price >= 0.10:
                max_size = config.RISK_TIERS['risky']['max_position']
            else:
                max_size = 0
            base_size = min(base_size, max_size)

            if base_size < config.MIN_TRADE_SIZE:
                self.decision_engine.mark_executed(
                    decision.decision_id, "Rejected by size filter (below MIN_TRADE_SIZE)")
                return False

            # Gate through risk_manager
            edge = abs(yes_price - 0.5)  # Conservative edge estimate for existing strategies
            approval = self.risk_manager.approve(
                decision_type=decision_type,
                market_id=decision.market_id,
                market_question=decision.market_question,
                outcome="YES" if decision_type == "BUY_YES" else "NO",
                amount_usd=base_size,
                edge=edge,
                confidence=decision.confidence,
                category=decision.market_data.get('category', 'Unknown'),
                is_new_position=decision.market_id not in self.portfolio.portfolio.positions,
            )

            if not approval["approved"]:
                self._log(f"⛔ Risk manager blocked: {approval['reason']}")
                self.decision_engine.mark_executed(
                    decision.decision_id, f"Rejected by risk manager: {approval['reason']}")
                return False

        # ── Daily loss limit check (real mode) ──
        if not self._check_daily_loss_limit():
            self._log(f"⛔ Daily loss limit (${config.MAX_DAILY_LOSS}) reached – skipping")
            return False

        # ── Execute ──

        # Determine strategy name from reasoning
        strategy = "unknown"
        reasoning_lower = decision.reasoning.lower()
        for s in ["llm", "mispricing", "momentum", "liquidity", "contrarian", "value", "stop loss", "take profit"]:
            if s in reasoning_lower:
                strategy = s.replace(" ", "_")
                break

        if decision.decision == "BUY_YES":
            trade = self.executor.execute_buy(
                market_id=decision.market_id,
                market_question=decision.market_question,
                outcome="YES",
                amount_usd=base_size,
                reasoning=decision.reasoning,
                confidence=decision.confidence,
                strategy=strategy,
                market_snapshot=decision.market_data,
            )
            if trade:
                self.decision_engine.mark_executed(decision.decision_id, f"Executed: {trade.trade_id}")
                return True

        elif decision.decision == "BUY_NO":
            trade = self.executor.execute_buy(
                market_id=decision.market_id,
                market_question=decision.market_question,
                outcome="NO",
                amount_usd=base_size,
                reasoning=decision.reasoning,
                confidence=decision.confidence,
                strategy=strategy,
                market_snapshot=decision.market_data,
            )
            if trade:
                self.decision_engine.mark_executed(decision.decision_id, f"Executed: {trade.trade_id}")
                return True

        elif decision.decision == "SELL":
            trade = self.executor.execute_sell(
                market_id=decision.market_id,
                reasoning=decision.reasoning,
                confidence=decision.confidence,
                strategy=strategy,
            )
            if trade:
                self.decision_engine.mark_executed(decision.decision_id, f"Executed: {trade.trade_id}")
                # Track daily losses for real mode
                if trade.cost_usd < trade.price * trade.tokens:
                    loss = (trade.price * trade.tokens) - trade.cost_usd
                    self.daily_loss_usd += max(0, loss)
                return True

        return False

    def run_cycle(self):
        """Run one trading cycle"""
        self.cycle_count += 1
        self._log(f"=== Cycle #{self.cycle_count} ===")

        # Update position prices first
        self._log("Updating position prices...")
        self.update_position_prices()

        # Print current status
        self._print_status()
        self._print_positions()

        # Scan for market opportunities
        self._log("Scanning markets...")
        try:
            markets = self.market_data.scan_opportunities()
        except Exception as e:
            self._log(f"Error scanning markets: {e}")
            return

        if not markets:
            self._log("No markets found, waiting...")
            return

        # Analyze and make decisions
        self._log(f"Analyzing {len(markets)} markets...")
        decisions = self.decision_engine.analyze_and_decide(markets)

        # Execute decisions
        executed_count = 0
        for decision in decisions:
            if decision.confidence >= config.CONFIDENCE_THRESHOLD:
                self._log(f"Decision: {decision.decision} on '{decision.market_question[:40]}...'")
                self._log(f"  Confidence: {decision.confidence:.2f}")
                self._log(f"  Reasoning: {decision.reasoning}")

                if self.execute_decision(decision):
                    executed_count += 1

        self._log(f"Executed {executed_count} trades this cycle")

        # Save final state
        self.portfolio.save()

        # ── Phase 2: Event-scanning cycle (every 6 cycles = ~30 min) ──
        if self.phase2_scanner and self.cycle_count % 6 == 0:
            try:
                self._run_phase2_scan()
            except Exception as e:
                self._log(f"[Phase 2] Scan error: {e}")

    def _run_phase2_scan(self):
        """Run the Phase 2 information-edge event scanner and log findings."""
        self._log("[Phase 2] Scanning Polymarket for information-edge opportunities...")

        # 1. Scan live events from Gamma API
        events = self.phase2_scanner.scan_markets(limit=50)
        if not events:
            self._log("[Phase 2] No events found")
            return

        self._log(f"[Phase 2] Scanning {len(events)} events for LLM pricing edge...")

        # 2. Price events through LLM (handle missing API key gracefully)
        try:
            results = self.phase2_pricer.price_markets(events)
        except Exception as e:
            self._log(f"[Phase 2] LLM pricing failed: {e}")
            return

        # 3. Apply risk manager screen + log candidates
        flagged_count = 0
        for r in results:
            if r.flagged:
                category = r.category if hasattr(r, 'category') else "Unknown"
                edge = abs(r.edge)
                confidence = r.llm_confidence
                question = r.question[:60]

                self._log(f"[Phase 2] ⚡ CANDIDATE: {question}")
                self._log(f"           Edge={edge:.1%}  Confidence={confidence:.0%}  Category={category}")
                self._log(f"           Reasoning: {r.llm_reasoning[:120]}")

                # Check if risk_manager would approve (log only — paper trading)
                if not config.PAPER_TRADING:
                    approval = self.risk_manager.approve(
                        decision_type="BUY_YES" if r.edge > 0 else "BUY_NO",
                        market_id=r.market_id,
                        market_question=r.question,
                        outcome="YES" if r.edge > 0 else "NO",
                        amount_usd=config.MAX_POSITION_SIZE,
                        edge=abs(r.edge),
                        confidence=confidence,
                        category=category,
                        is_new_position=True,
                    )
                    if approval["approved"]:
                        self._log(f"[Phase 2] ✅ Risk manager APPROVED")
                    else:
                        self._log(f"[Phase 2] ⛔ Risk manager: {approval['reason']}")

                flagged_count += 1

        self._log(f"[Phase 2] Complete — {flagged_count} flagged candidates out of {len(results)} events")

        # 4. Log accuracy stats if available
        try:
            stats = self.phase2_pricer.get_accuracy_stats()
            if stats and stats.get("total_markets", 0) > 0:
                self._log(f"[Phase 2] Accuracy: {stats['avg_accuracy']:.1%} over {stats['total_markets']} markets")
        except Exception:
            pass

    def run(self, interval_seconds: int = 300):
        """
        Run the trading agent continuously
        Default: run every 5 minutes
        """
        self._print_banner()
        self._log(f"Starting agent with {interval_seconds}s interval")
        self._log(f"Initial capital: ${config.INITIAL_CAPITAL}")
        self._log(f"Max position size: ${config.MAX_POSITION_SIZE}")
        dynamic_max_exposure = max(float(config.MAX_TOTAL_EXPOSURE), float(self.portfolio.total_value()))
        self._log(f"Max total exposure: ${dynamic_max_exposure:.2f}")
        self._log(f"Confidence threshold: {config.CONFIDENCE_THRESHOLD}")

        while self.running:
            try:
                self.run_cycle()
            except Exception as e:
                self._log(f"Cycle error: {e}")
                import traceback
                traceback.print_exc()

            self._log(f"Sleeping for {interval_seconds}s...")
            time.sleep(interval_seconds)

    def run_once(self):
        """Run a single trading cycle"""
        self._print_banner()
        self.run_cycle()
        return self.portfolio.get_summary()


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description='Polymarket AI Trading Agent')
    parser.add_argument('--once', action='store_true', help='Run once and exit')
    parser.add_argument('--interval', type=int, default=300, help='Interval between cycles in seconds')
    parser.add_argument('--status', action='store_true', help='Just show current status')
    parser.add_argument('--phase2', action='store_true', help='Run Phase 2 event scan once and exit')
    args = parser.parse_args()

    agent = TradingAgent()

    if args.phase2:
        agent._print_banner()
        agent._run_phase2_scan()
        return

    if args.status:
        agent._print_banner()
        agent.update_position_prices()
        agent._print_status()
        agent._print_positions()
        return

    if args.once:
        summary = agent.run_once()
        print("\nFinal Summary:")
        print(json.dumps(summary, indent=2))
    else:
        agent.run(interval_seconds=args.interval)


if __name__ == "__main__":
    main()
