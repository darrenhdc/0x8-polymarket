"""
Trade Executor - Execute trades (paper or real) and track history
"""
import json
import os
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
import config
from portfolio import PortfolioManager
from market_data import MarketData
from trade_journal import TradeJournal


@dataclass
class Trade:
    """Represents a trade record"""
    trade_id: str
    timestamp: str
    market_id: str
    market_question: str
    action: str  # "BUY" or "SELL"
    outcome: str  # "YES" or "NO"
    tokens: float
    price: float
    cost_usd: float
    reasoning: str
    confidence: float
    mode: str = "paper"  # "paper" or "real"
    order_id: Optional[str] = None  # real order ID from CLOB

    def to_dict(self) -> Dict:
        return asdict(self)


class TradeExecutor:
    """Executes and tracks trades (paper or real)"""

    def __init__(self, portfolio: PortfolioManager):
        self.portfolio = portfolio
        self.market_data = MarketData()
        self.trades: List[Trade] = self._load_trades()
        self.stopped_out: Dict[str, str] = self._load_stopped_out()  # market_id -> timestamp
        self.journal = TradeJournal()
        self.real_client = None

        if not config.PAPER_TRADING:
            from real_trading_client import RealTradingClient
            self.real_client = RealTradingClient()
            self._sync_cash_from_exchange()
            print("🔴 REAL TRADING MODE – orders will be sent to Polymarket")

    def _sync_cash_from_exchange(self):
        """Sync local cash tracker with real CLOB collateral balance."""
        if not self.real_client:
            return
        balance_usdc = self.real_client.get_collateral_balance_usdc()
        if balance_usdc is None:
            print("⚠️ Could not sync cash from CLOB balance")
            return

        now = datetime.utcnow().isoformat()
        local = self.portfolio.portfolio
        position_value = sum(p.current_value for p in local.positions.values())
        old_total = local.cash + position_value
        old_cash = local.cash
        local.cash = round(balance_usdc, 6)
        new_total = local.cash + position_value

        # Keep PnL baseline stable when external cashflow (deposit/withdraw) changes cash.
        cash_delta = new_total - old_total
        if abs(cash_delta) >= 0.01:
            local.initial_capital = round(local.initial_capital + cash_delta, 6)
            print(f"🧭 Adjusted PnL baseline by {cash_delta:+.6f} due to cashflow sync")

        local.last_updated = now
        self.portfolio.save()
        print(f"💰 Synced cash from CLOB: ${old_cash:.2f} -> ${local.cash:.6f}")

    def _load_stopped_out(self) -> Dict[str, str]:
        """Load stopped-out markets from disk"""
        if os.path.exists(config.STOPPED_OUT_FILE):
            try:
                with open(config.STOPPED_OUT_FILE, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {}

    def _save_stopped_out(self):
        """Save stopped-out markets to disk"""
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(config.STOPPED_OUT_FILE, 'w') as f:
            json.dump(self.stopped_out, f, indent=2)

    def is_market_cooled_down(self, market_id: str) -> bool:
        """Check if enough time has passed since stop loss"""
        if market_id not in self.stopped_out:
            return True

        try:
            stopped_time = datetime.fromisoformat(self.stopped_out[market_id])
            cooldown_hours = getattr(config, 'STOPPED_OUT_COOLDOWN_HOURS', 24)
            elapsed = datetime.utcnow() - stopped_time
            return elapsed.total_seconds() > cooldown_hours * 3600
        except:
            return True  # If error, allow trading

    def _load_trades(self) -> List[Trade]:
        """Load trade history from disk"""
        os.makedirs(config.DATA_DIR, exist_ok=True)

        if os.path.exists(config.TRADES_FILE):
            try:
                with open(config.TRADES_FILE, 'r') as f:
                    data = json.load(f)
                return [Trade(**t) for t in data]
            except Exception as e:
                print(f"Error loading trades: {e}")
        return []

    def _save_trades(self):
        """Save trade history to disk"""
        os.makedirs(config.DATA_DIR, exist_ok=True)
        data = [t.to_dict() for t in self.trades]
        with open(config.TRADES_FILE, 'w') as f:
            json.dump(data, f, indent=2)

    def _max_total_exposure(self) -> float:
        """Dynamic max exposure: configured floor vs current portfolio equity."""
        equity = float(self.portfolio.total_value())
        return max(float(config.MAX_TOTAL_EXPOSURE), equity)

    def _generate_trade_id(self) -> str:
        """Generate unique trade ID"""
        return f"T{datetime.utcnow().strftime('%Y%m%d%H%M%S')}{len(self.trades):04d}"

    def execute_buy(self, market_id: str, market_question: str,
                    outcome: str, amount_usd: float,
                    reasoning: str, confidence: float,
                    strategy: str = "unknown",
                    market_snapshot: Dict = None) -> Optional[Trade]:
        """Execute a buy order (paper or real)"""

        # Check cooldown for stopped-out markets
        if not self.is_market_cooled_down(market_id):
            print(f"Market {market_id} in cooldown (recently stopped out)")
            return None

        # Get current market data
        market = self.market_data.scanner.api.get_market_by_id(market_id)
        if not market:
            print(f"Market not found: {market_id}")
            return None

        # Parse token data
        import json
        clob_token_ids = market.get('clobTokenIds', [])
        if isinstance(clob_token_ids, str):
            clob_token_ids = json.loads(clob_token_ids)

        outcomes = market.get('outcomes', ['Yes', 'No'])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        outcome_prices = market.get('outcomePrices', [])
        if isinstance(outcome_prices, str):
            outcome_prices = json.loads(outcome_prices)

        # Find the token for the outcome
        token_id = None
        price = 0
        for i, o in enumerate(outcomes):
            if o.lower() == outcome.lower() and i < len(clob_token_ids):
                token_id = clob_token_ids[i]
                if i < len(outcome_prices):
                    try:
                        price = float(outcome_prices[i])
                    except:
                        pass
                break

        if not token_id:
            print(f"Token not found for outcome: {outcome}")
            return None

        if price <= 0 or price > 1:
            print(f"Invalid price: {price}")
            return None

        # Check risk limits
        if amount_usd > config.MAX_POSITION_SIZE:
            amount_usd = config.MAX_POSITION_SIZE
            print(f"Capped position size to ${amount_usd}")

        max_exposure = self._max_total_exposure()
        total_exposure = self.portfolio.portfolio.total_exposure + amount_usd
        if total_exposure > max_exposure:
            max_allowed = max_exposure - self.portfolio.portfolio.total_exposure
            if max_allowed < config.MIN_TRADE_SIZE:
                print(f"Would exceed max exposure. Max allowed: ${max_allowed:.2f}")
                return None
            amount_usd = max_allowed
            print(f"Reduced position to ${amount_usd:.2f} due to exposure limits")

        if amount_usd < config.MIN_TRADE_SIZE:
            print(f"Trade size ${amount_usd:.2f} below minimum ${config.MIN_TRADE_SIZE}")
            return None

        # Calculate tokens
        tokens_amount = amount_usd / price

        # ── Journal: record the decision before execution ──
        journal_entry = self.journal.log_decision(
            market_id=market_id,
            market_question=market_question,
            category=market_snapshot.get("category", "Unknown") if market_snapshot else "Unknown",
            action=f"BUY_{outcome.upper()}",
            strategy=strategy,
            confidence=confidence,
            reasoning=reasoning,
            market_snapshot=market_snapshot or {"yes_price": price, "no_price": 1 - price},
            portfolio_summary=self.portfolio.get_summary(),
            paper_trade=config.PAPER_TRADING,
        )

        # ── Execute: real or paper ──
        order_id = None
        fill_price = price

        if not config.PAPER_TRADING and self.real_client:
            # Real order via CLOB
            neg_risk = market.get("neg_risk", False)
            if isinstance(neg_risk, str):
                neg_risk = neg_risk.lower() == "true"
            resp = self.real_client.place_limit_order(
                token_id=token_id,
                side="BUY",
                size=tokens_amount,
                price=price,
                neg_risk=neg_risk,
            )
            if not resp or resp.get("success") is False:
                print(f"❌ Real order FAILED: {resp}")
                return None
            order_id = resp.get("orderID") or resp.get("id")
            print(f"✅ Real order placed: {order_id}")

        # Update local portfolio tracker
        success = self.portfolio.open_position(
            market_id=market_id,
            market_question=market_question,
            outcome=outcome.upper(),
            tokens=tokens_amount,
            price=price,
            cost=amount_usd
        )

        if not success:
            return None

        # ── Journal: mark executed ──
        self.journal.mark_executed(
            entry_id=journal_entry.entry_id,
            order_id=order_id or self._generate_trade_id(),
            fill_price=fill_price,
            fill_size_usd=amount_usd,
            tokens=tokens_amount,
        )

        # Record the trade
        mode = "paper" if config.PAPER_TRADING else "real"
        trade = Trade(
            trade_id=self._generate_trade_id(),
            timestamp=datetime.utcnow().isoformat(),
            market_id=market_id,
            market_question=market_question,
            action="BUY",
            outcome=outcome.upper(),
            tokens=tokens_amount,
            price=price,
            cost_usd=amount_usd,
            reasoning=reasoning,
            confidence=confidence,
            mode=mode,
            order_id=order_id,
        )

        self.trades.append(trade)
        self._save_trades()

        return trade

    def execute_sell(self, market_id: str, reasoning: str,
                     confidence: float,
                     strategy: str = "risk_management") -> Optional[Trade]:
        """Execute a sell order (close position) – paper or real"""

        if market_id not in self.portfolio.portfolio.positions:
            print(f"No position found for market: {market_id}")
            return None

        position = self.portfolio.portfolio.positions[market_id]

        # Get current market data
        market = self.market_data.scanner.api.get_market_by_id(market_id)
        if not market:
            print(f"Market not found: {market_id}")
            return None

        # Parse token data
        import json
        clob_token_ids = market.get('clobTokenIds', [])
        if isinstance(clob_token_ids, str):
            clob_token_ids = json.loads(clob_token_ids)

        outcomes = market.get('outcomes', ['Yes', 'No'])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        outcome_prices = market.get('outcomePrices', [])
        if isinstance(outcome_prices, str):
            outcome_prices = json.loads(outcome_prices)

        # Find the token for the position's outcome
        token_id = None
        price = 0
        for i, o in enumerate(outcomes):
            if o.lower() == position.outcome.lower():
                if i < len(outcome_prices):
                    try:
                        price = float(outcome_prices[i])
                    except:
                        pass
                if i < len(clob_token_ids):
                    token_id = clob_token_ids[i]
                break

        if price <= 0:
            print(f"Invalid price: {price}")
            return None

        # ── Journal: record sell decision ──
        pnl_pct = ((price - position.avg_price) / position.avg_price) * 100
        close_reason = "manual"
        if "stop loss" in reasoning.lower():
            close_reason = "stop_loss"
        elif "take profit" in reasoning.lower():
            close_reason = "take_profit"

        journal_entry = self.journal.log_decision(
            market_id=market_id,
            market_question=position.market_question,
            category="",
            action="SELL",
            strategy=strategy,
            confidence=confidence,
            reasoning=reasoning,
            market_snapshot={
                "yes_price": price if position.outcome == "YES" else 1 - price,
                "no_price": 1 - price if position.outcome == "YES" else price,
                "spread": 0, "volume": 0, "liquidity": 0,
            },
            portfolio_summary=self.portfolio.get_summary(),
            paper_trade=config.PAPER_TRADING,
        )

        # ── Execute real sell ──
        order_id = None
        if not config.PAPER_TRADING and self.real_client and token_id:
            neg_risk = market.get("neg_risk", False)
            if isinstance(neg_risk, str):
                neg_risk = neg_risk.lower() == "true"
            resp = self.real_client.place_limit_order(
                token_id=token_id,
                side="SELL",
                size=position.tokens,
                price=price,
                neg_risk=neg_risk,
            )
            if not resp or resp.get("success") is False:
                print(f"❌ Real sell order FAILED: {resp}")
                return None
            order_id = resp.get("orderID") or resp.get("id")
            print(f"✅ Real sell order placed: {order_id}")

        # Close the position in local tracker
        result = self.portfolio.close_position(market_id, price)
        if not result:
            return None

        # Journal: mark executed + closed
        self.journal.mark_executed(
            entry_id=journal_entry.entry_id,
            order_id=order_id or self._generate_trade_id(),
            fill_price=price,
            fill_size_usd=result['proceeds'],
            tokens=position.tokens,
        )
        self.journal.mark_closed(
            market_id=market_id,
            close_price=price,
            pnl_usd=result.get('pnl', 0),
            pnl_pct=pnl_pct,
            close_reason=close_reason,
        )

        # Record the trade
        mode = "paper" if config.PAPER_TRADING else "real"
        trade = Trade(
            trade_id=self._generate_trade_id(),
            timestamp=datetime.utcnow().isoformat(),
            market_id=market_id,
            market_question=position.market_question,
            action="SELL",
            outcome=position.outcome,
            tokens=position.tokens,
            price=price,
            cost_usd=result['proceeds'],
            reasoning=reasoning,
            confidence=confidence,
            mode=mode,
            order_id=order_id,
        )

        self.trades.append(trade)
        self._save_trades()

        # Track stopped-out markets (loss >= stop loss threshold)
        pnl_percent = result.get('pnl_percent', 0)
        if pnl_percent <= -config.STOP_LOSS_PERCENT * 100:
            self.stopped_out[market_id] = datetime.utcnow().isoformat()
            self._save_stopped_out()
            print(f"⚠️ Market {market_id} added to stop-loss cooldown list")

        return trade

    def get_trade_history(self, limit: int = 50) -> List[Trade]:
        """Get recent trade history"""
        return self.trades[-limit:]

    def get_trade_stats(self) -> Dict:
        """Get trading statistics"""
        if not self.trades:
            return {
                'total_trades': 0,
                'buy_trades': 0,
                'sell_trades': 0,
                'total_volume': 0
            }

        buys = [t for t in self.trades if t.action == "BUY"]
        sells = [t for t in self.trades if t.action == "SELL"]

        return {
            'total_trades': len(self.trades),
            'buy_trades': len(buys),
            'sell_trades': len(sells),
            'total_volume': sum(t.cost_usd for t in self.trades),
            'avg_trade_size': sum(t.cost_usd for t in self.trades) / len(self.trades)
        }


if __name__ == "__main__":
    # Test trade executor
    pm = PortfolioManager()
    executor = TradeExecutor(pm)

    print("Trade Stats:")
    print(json.dumps(executor.get_trade_stats(), indent=2))
