"""
AI Decision Engine - Analyze markets and make trading decisions
This module logs decisions for Claude to review and refine strategies
"""
import json
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
import config
from portfolio import PortfolioManager
from market_data import MarketData, MarketScanner


@dataclass
class Decision:
    """Represents an AI trading decision"""
    decision_id: str
    timestamp: str
    market_id: str
    market_question: str
    decision: str  # "BUY_YES", "BUY_NO", "SELL", "HOLD"
    confidence: float
    reasoning: str
    market_data: Dict
    portfolio_state: Dict
    executed: bool = False
    result: Optional[str] = None

    def to_dict(self) -> Dict:
        return asdict(self)


class MarketAnalyzer:
    """Analyzes individual markets for trading opportunities"""

    def __init__(self, market_data: MarketData):
        self.market_data = market_data

    def analyze_market(self, market: Dict) -> Dict:
        """
        Analyze a market and return analysis data
        This provides raw data for decision making
        """
        market_id = market.get('id')
        question = market.get('question', '')

        # Get current prices
        prices = self.market_data.scanner.get_market_prices(market)
        yes_price = prices.get('yes', 0)
        no_price = prices.get('no', 0)

        # Market metrics
        volume = float(market.get('volume', 0) or 0)
        liquidity = float(market.get('liquidity', 0) or 0)

        # Get orderbook data for better price discovery
        yes_token = market.get('yes_token', {})
        no_token = market.get('no_token', {})

        yes_book = {}
        no_book = {}
        if yes_token:
            yes_book = self.market_data.get_orderbook_summary(yes_token.get('token_id'))
        if no_token:
            no_book = self.market_data.get_orderbook_summary(no_token.get('token_id'))

        # Calculate implied probability and edge
        # In efficient markets, yes + no should ~= 1.0
        total_prob = yes_price + no_price
        spread = abs(1.0 - total_prob) if total_prob > 0 else 1.0

        # Time to resolution
        end_date_str = market.get('end_date_iso')
        days_to_resolution = 999
        if end_date_str:
            try:
                end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                days_to_resolution = (end_date - datetime.utcnow().replace(tzinfo=end_date.tzinfo)).days
            except:
                pass

        return {
            'market_id': market_id,
            'question': question,
            'yes_price': yes_price,
            'no_price': no_price,
            'spread': spread,
            'volume': volume,
            'liquidity': liquidity,
            'days_to_resolution': days_to_resolution,
            'yes_orderbook': yes_book,
            'no_orderbook': no_book,
            'category': market.get('tags', [{}])[0].get('name', 'Unknown') if market.get('tags') else 'Unknown'
        }


class StrategyEngine:
    """
    Core strategy engine that generates trading signals
    Uses multiple strategies and combines them
    """

    def __init__(self):
        self.strategies = [
            self._strategy_mispricing,
            self._strategy_momentum,
            self._strategy_liquidity_arb,
            self._strategy_sentiment_contrarian,
            self._strategy_asymmetric_value  # New strategy
        ]

    def _strategy_mispricing(self, analysis: Dict) -> Tuple[str, float, str]:
        """
        Look for mispriced markets where yes + no != 1.0
        This is a basic arbitrage strategy
        """
        yes_price = analysis.get('yes_price', 0)
        no_price = analysis.get('no_price', 0)

        if yes_price <= 0 or no_price <= 0:
            return "HOLD", 0, "Invalid prices"

        total = yes_price + no_price
        spread = abs(1.0 - total)

        # If spread > 5%, there might be an opportunity
        if spread > 0.05:
            if yes_price < no_price and yes_price < 0.45:
                return "BUY_YES", 0.60, f"Mispricing detected: YES underpriced at {yes_price:.3f} (sum={total:.3f})"
            elif no_price < yes_price and no_price < 0.45:
                return "BUY_NO", 0.60, f"Mispricing detected: NO underpriced at {no_price:.3f} (sum={total:.3f})"

        return "HOLD", 0, "No mispricing opportunity"

    def _strategy_momentum(self, analysis: Dict) -> Tuple[str, float, str]:
        """
        Follow momentum - buy YES on low prices that might rise
        OPTIMIZED: Avoid extremely low probability markets (<10%) as they often
        result in stop losses
        """
        yes_price = analysis.get('yes_price', 0)
        no_price = analysis.get('no_price', 0)
        liquidity = analysis.get('liquidity', 0)
        volume = analysis.get('volume', 0)

        # Minimum price threshold to avoid high stop-loss risk
        MIN_PRICE = 0.10  # Only buy if price >= 10%

        # High liquidity + low price = potential asymmetric upside
        if liquidity > 5000 and volume > 5000:
            if MIN_PRICE <= yes_price < 0.35:
                return "BUY_YES", 0.55, f"Momentum: Low YES price {yes_price:.3f} with liquidity ${liquidity:,.0f}"
            if MIN_PRICE <= no_price < 0.35:
                return "BUY_NO", 0.55, f"Momentum: Low NO price {no_price:.3f} with liquidity ${liquidity:,.0f}"

        return "HOLD", 0, "No momentum opportunity"

    def _strategy_liquidity_arb(self, analysis: Dict) -> Tuple[str, float, str]:
        """
        Look for orderbook imbalances
        """
        yes_book = analysis.get('yes_orderbook', {})
        no_book = analysis.get('no_orderbook', {})

        yes_bid = yes_book.get('bid', 0)
        yes_ask = yes_book.get('ask', 0)
        no_bid = no_book.get('bid', 0)
        no_ask = no_book.get('ask', 0)

        # Look for tight spreads and good depth
        if yes_ask > 0 and yes_bid > 0:
            yes_spread_pct = (yes_ask - yes_bid) / yes_ask
            if yes_spread_pct < 0.03 and yes_ask < 0.40:
                return "BUY_YES", 0.52, f"Liquidity arb: Tight spread, low ask price {yes_ask:.3f}"

        if no_ask > 0 and no_bid > 0:
            no_spread_pct = (no_ask - no_bid) / no_ask
            if no_spread_pct < 0.03 and no_ask < 0.40:
                return "BUY_NO", 0.52, f"Liquidity arb: Tight spread, low ask price {no_ask:.3f}"

        return "HOLD", 0, "No liquidity arb opportunity"

    def _strategy_sentiment_contrarian(self, analysis: Dict) -> Tuple[str, float, str]:
        """
        Contrarian strategy - when everyone is on one side, consider the other
        OPTIMIZED: Avoid extremely low probability markets (<10%) as they often
        result in stop losses
        """
        yes_price = analysis.get('yes_price', 0)
        no_price = analysis.get('no_price', 0)

        # AVOID extremely low probability - learned from OpenAI/GTA VI losses
        MIN_CONTRARIAN_PRICE = 0.10  # Only play contrarian if price >= 10%

        # If market is extremely confident (>90%), consider contrarian position
        # BUT only if the underdog has at least 10% probability
        if yes_price > 0.90 and no_price >= MIN_CONTRARIAN_PRICE:
            return "BUY_NO", 0.50, f"Contrarian: Market overconfident on YES at {yes_price:.3f}"
        if no_price > 0.90 and yes_price >= MIN_CONTRARIAN_PRICE:
            return "BUY_YES", 0.50, f"Contrarian: Market overconfident on NO at {no_price:.3f}"

        # Skip extremely low probability markets
        if no_price > 0.90 and yes_price < MIN_CONTRARIAN_PRICE:
            return "HOLD", 0, f"Skip: Price too low ({yes_price:.1%}), high stop-loss risk"
        if yes_price > 0.90 and no_price < MIN_CONTRARIAN_PRICE:
            return "HOLD", 0, f"Skip: Price too low ({no_price:.1%}), high stop-loss risk"

        return "HOLD", 0, "No contrarian opportunity"

    def _strategy_asymmetric_value(self, analysis: Dict) -> Tuple[str, float, str]:
        """
        Look for asymmetric value - low priced outcomes with decent liquidity
        This is a simple value strategy
        OPTIMIZED: Focus on mid-range probability (15-35%) for better risk/reward
        """
        yes_price = analysis.get('yes_price', 0)
        no_price = analysis.get('no_price', 0)
        liquidity = analysis.get('liquidity', 0)
        volume = analysis.get('volume', 0)

        # Skip if not enough liquidity
        if liquidity < 5000 or volume < 1000:
            return "HOLD", 0, "Insufficient liquidity for value play"

        # Minimum price threshold to avoid high stop-loss risk
        MIN_PRICE = 0.15  # Only buy if price >= 15%

        # Look for asymmetric opportunities
        # If YES is in the sweet spot (15-35%), it might be undervalued
        if MIN_PRICE <= yes_price < 0.35:
            return "BUY_YES", 0.55, f"Value: YES at {yes_price:.3f} appears undervalued"

        # If NO is in the sweet spot (15-35%), it might be undervalued
        if MIN_PRICE <= no_price < 0.35:
            return "BUY_NO", 0.55, f"Value: NO at {no_price:.3f} appears undervalued"

        return "HOLD", 0, "No value opportunity"

    def evaluate(self, analysis: Dict) -> Tuple[str, float, str]:
        """
        Evaluate all strategies and return the best signal
        """
        best_decision = "HOLD"
        best_confidence = 0
        best_reasoning = "No actionable signals"

        for strategy in self.strategies:
            try:
                decision, confidence, reasoning = strategy(analysis)
                if confidence > best_confidence:
                    best_decision = decision
                    best_confidence = confidence
                    best_reasoning = reasoning
            except Exception as e:
                print(f"Strategy error: {e}")
                continue

        return best_decision, best_confidence, best_reasoning


class DecisionEngine:
    """
    Main decision engine that coordinates analysis and decision making.
    Uses LLM (Claude) as the primary signal source when available,
    with heuristic strategies as fallback.
    """

    def __init__(self, portfolio: PortfolioManager):
        self.portfolio = portfolio
        self.market_data = MarketData()
        self.analyzer = MarketAnalyzer(self.market_data)
        self.strategy = StrategyEngine()  # fallback heuristics
        self.decisions: List[Decision] = self._load_decisions()

    def _load_decisions(self) -> List[Decision]:
        """Load decision history from disk"""
        os.makedirs(config.DATA_DIR, exist_ok=True)

        if os.path.exists(config.DECISIONS_FILE):
            try:
                with open(config.DECISIONS_FILE, 'r') as f:
                    data = json.load(f)
                return [Decision(**d) for d in data]
            except Exception as e:
                print(f"Error loading decisions: {e}")
        return []

    def _save_decisions(self):
        """Save decision history to disk"""
        os.makedirs(config.DATA_DIR, exist_ok=True)
        data = [d.to_dict() for d in self.decisions]
        with open(config.DECISIONS_FILE, 'w') as f:
            json.dump(data, f, indent=2)

    def _generate_decision_id(self) -> str:
        """Generate unique decision ID"""
        return f"D{datetime.utcnow().strftime('%Y%m%d%H%M%S')}{len(self.decisions):04d}"

    def analyze_and_decide(self, markets: List[Dict]) -> List[Decision]:
        """
        Analyze markets and generate trading decisions
        """
        decisions = []

        # Sensitive topics to filter out
        SENSITIVE_KEYWORDS = [
            "xi jinping", "xi", "jinping",
            "china", "chinese",
            "taiwan",
            "ccp", "communist",
            "politburo",
            "xijinping"
        ]

        # Exclude noisy / non-actionable markets
        EXCLUDED_KEYWORDS = [
            "jesus christ",
            "second coming",
        ]

        for market in markets:
            market_id = market.get('id')
            has_position = market_id in self.portfolio.portfolio.positions

            # Filter out sensitive topics
            question = market.get('question', '').lower()
            is_sensitive = any(keyword in question for keyword in SENSITIVE_KEYWORDS)
            is_excluded = any(keyword in question for keyword in EXCLUDED_KEYWORDS)
            # Still allow risk-management logic for already-open positions
            if (is_sensitive or is_excluded) and not has_position:
                continue
            try:
                # Analyze the market
                analysis = self.analyzer.analyze_market(market)

                # Get strategy evaluation — heuristic strategies
                decision_type, confidence, reasoning = self.strategy.evaluate(analysis)

                # Global entry floor for new positions (avoid ultra-low-probability noise)
                min_entry_price = getattr(config, 'MIN_CONTRARIAN_PRICE', 0.10)
                if not has_position and decision_type == "BUY_YES":
                    yes_price = analysis.get('yes_price', 0)
                    if yes_price < min_entry_price:
                        decision_type = "HOLD"
                        confidence = 0
                        reasoning = f"跳过：YES 入场价过低（{yes_price:.1%} < {min_entry_price:.0%}）"
                elif not has_position and decision_type == "BUY_NO":
                    no_price = analysis.get('no_price', 0)
                    if no_price < min_entry_price:
                        decision_type = "HOLD"
                        confidence = 0
                        reasoning = f"跳过：NO 入场价过低（{no_price:.1%} < {min_entry_price:.0%}）"

                # Adjust decision based on existing position
                if has_position:
                    position = self.portfolio.portfolio.positions[market_id]
                    # Check stop loss / take profit
                    current_price = analysis.get(f"{position.outcome.lower()}_price", 0)
                    pnl_pct = ((current_price - position.avg_price) / position.avg_price) * 100

                    # Dynamic stop loss based on entry price
                    if position.avg_price < config.LOW_PROB_THRESHOLD:
                        stop_loss = config.LOW_PROB_STOP_LOSS  # 10% for low prob
                    else:
                        stop_loss = config.STOP_LOSS_PERCENT  # 15% for normal

                    if pnl_pct <= -stop_loss * 100:
                        decision_type = "SELL"
                        confidence = 0.85
                        reasoning = f"触发止损：亏损 {pnl_pct:.1f}%"
                    elif pnl_pct >= config.TAKE_PROFIT_PERCENT * 100:
                        decision_type = "SELL"
                        confidence = 0.80
                        reasoning = f"触发止盈：盈利 {pnl_pct:.1f}%"
                    elif decision_type.startswith("BUY"):
                        decision_type = "HOLD"
                        reasoning = "已有持仓，继续持有"

                # Only create decision if confidence meets threshold
                if confidence >= config.CONFIDENCE_THRESHOLD:
                    decision = Decision(
                        decision_id=self._generate_decision_id(),
                        timestamp=datetime.utcnow().isoformat(),
                        market_id=market_id,
                        market_question=market.get('question', ''),
                        decision=decision_type,
                        confidence=confidence,
                        reasoning=reasoning,
                        market_data=analysis,
                        portfolio_state=self.portfolio.get_summary()
                    )
                    decisions.append(decision)
                    self.decisions.append(decision)

            except Exception as e:
                print(f"Error analyzing market {market.get('id')}: {e}")
                continue

        self._save_decisions()
        return decisions

    def get_pending_decisions(self) -> List[Decision]:
        """Get decisions that haven't been executed"""
        return [d for d in self.decisions if not d.executed]

    def mark_executed(self, decision_id: str, result: str):
        """Mark a decision as executed"""
        for d in self.decisions:
            if d.decision_id == decision_id:
                d.executed = True
                d.result = result
                break
        self._save_decisions()

    def get_decision_summary(self) -> Dict:
        """Get summary of recent decisions"""
        recent = self.decisions[-20:]  # Last 20 decisions
        executed = [d for d in recent if d.executed]

        return {
            'total_decisions': len(self.decisions),
            'recent_decisions': len(recent),
            'executed_decisions': len(executed),
            'pending_decisions': len(self.decisions) - len(executed),
            'avg_confidence': sum(d.confidence for d in recent) / len(recent) if recent else 0,
            'decision_types': {
                'BUY_YES': len([d for d in recent if d.decision == 'BUY_YES']),
                'BUY_NO': len([d for d in recent if d.decision == 'BUY_NO']),
                'SELL': len([d for d in recent if d.decision == 'SELL']),
                'HOLD': len([d for d in recent if d.decision == 'HOLD'])
            }
        }


if __name__ == "__main__":
    # Test decision engine
    pm = PortfolioManager()
    engine = DecisionEngine(pm)

    # Scan markets
    md = MarketData()
    markets = md.scan_opportunities()

    # Make decisions
    decisions = engine.analyze_and_decide(markets[:10])

    print(f"\nGenerated {len(decisions)} decisions:")
    for d in decisions[:5]:
        print(f"  {d.decision} on '{d.market_question[:40]}...'")
        print(f"    Confidence: {d.confidence:.2f}, Reason: {d.reasoning[:60]}...")
