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
import config
from portfolio import PortfolioManager
from market_data import MarketData
from trade_executor import TradeExecutor
from decision_engine import DecisionEngine


class TradingAgent:
    """
    Autonomous trading agent for Polymarket (paper or real trading)
    """

    def __init__(self):
        self.portfolio = PortfolioManager()
        self.market_data = MarketData()
        self.executor = TradeExecutor(self.portfolio)
        self.decision_engine = DecisionEngine(self.portfolio)
        self.cycle_count = 0
        self.running = True
        self.daily_loss_usd = 0.0
        self.daily_loss_reset_date = datetime.utcnow().date()

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
        """Return True if we are still within the daily loss limit."""
        if config.PAPER_TRADING:
            return True
        today = datetime.utcnow().date()
        if today != self.daily_loss_reset_date:
            self.daily_loss_usd = 0.0
            self.daily_loss_reset_date = today
        return self.daily_loss_usd < config.MAX_DAILY_LOSS

    def execute_decision(self, decision) -> bool:
        """Execute a trading decision"""
        # Daily loss guard (real trading only)
        if not self._check_daily_loss_limit():
            self._log(f"⛔ Daily loss limit (${config.MAX_DAILY_LOSS}) reached – skipping")
            return False

        # Determine strategy name from reasoning
        strategy = "unknown"
        reasoning_lower = decision.reasoning.lower()
        for s in ["llm", "mispricing", "momentum", "liquidity", "contrarian", "value", "stop loss", "take profit"]:
            if s in reasoning_lower:
                strategy = s.replace(" ", "_")
                break

        if decision.decision == "BUY_YES":
            # Calculate position size based on confidence and risk
            base_size = config.MAX_POSITION_SIZE * decision.confidence

            # Adjust for risk tier based on price
            yes_price = decision.market_data.get('yes_price', 0.5)
            if yes_price >= 0.35:
                max_size = config.RISK_TIERS['safe']['max_position']
            elif yes_price >= 0.15:
                max_size = config.RISK_TIERS['medium']['max_position']
            elif yes_price >= 0.10:
                max_size = config.RISK_TIERS['risky']['max_position']
            else:
                max_size = 0  # Don't buy extremely low probability

            base_size = min(base_size, max_size)

            if base_size < config.MIN_TRADE_SIZE:
                return False

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
            base_size = config.MAX_POSITION_SIZE * decision.confidence

            # Adjust for risk tier based on price
            no_price = decision.market_data.get('no_price', 0.5)
            if no_price >= 0.35:
                max_size = config.RISK_TIERS['safe']['max_position']
            elif no_price >= 0.15:
                max_size = config.RISK_TIERS['medium']['max_position']
            elif no_price >= 0.10:
                max_size = config.RISK_TIERS['risky']['max_position']
            else:
                max_size = 0  # Don't buy extremely low probability

            base_size = min(base_size, max_size)

            if base_size < config.MIN_TRADE_SIZE:
                return False

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
    args = parser.parse_args()

    agent = TradingAgent()

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
