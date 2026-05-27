"""
Portfolio Manager - Virtual funds and position tracking
"""
import json
import os
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
from . import config


@dataclass
class Position:
    """Represents a position in a market"""
    market_id: str
    market_question: str
    outcome: str  # "YES" or "NO"
    tokens: float  # Number of outcome tokens
    avg_price: float  # Average purchase price
    current_price: float  # Current market price
    cost_usd: float  # Total cost in USD
    opened_at: str  # ISO timestamp
    last_updated: str  # ISO timestamp

    @property
    def current_value(self) -> float:
        """Current value of position"""
        return self.tokens * self.current_price

    @property
    def pnl(self) -> float:
        """Unrealized P&L"""
        return self.current_value - self.cost_usd

    @property
    def pnl_percent(self) -> float:
        """Unrealized P&L percentage"""
        if self.cost_usd == 0:
            return 0
        return (self.pnl / self.cost_usd) * 100


@dataclass
class Portfolio:
    """Virtual portfolio state"""
    cash: float  # Available cash
    initial_capital: float  # Starting capital
    positions: Dict[str, Position]  # market_id -> Position
    total_trades: int
    winning_trades: int
    created_at: str
    last_updated: str

    @property
    def total_position_value(self) -> float:
        """Total value of all positions"""
        return sum(p.current_value for p in self.positions.values())

    @property
    def total_value(self) -> float:
        """Total portfolio value (cash + positions)"""
        return self.cash + self.total_position_value

    @property
    def total_pnl(self) -> float:
        """Total unrealized P&L"""
        return self.total_value - self.initial_capital

    @property
    def total_pnl_percent(self) -> float:
        """Total P&L percentage"""
        return (self.total_pnl / self.initial_capital) * 100

    @property
    def total_exposure(self) -> float:
        """Total capital at risk"""
        return sum(p.cost_usd for p in self.positions.values())


class PortfolioManager:
    """Manages virtual portfolio and positions"""

    def __init__(self):
        self.portfolio = self._load_or_create_portfolio()

    def _load_or_create_portfolio(self) -> Portfolio:
        """Load existing portfolio or create new one"""
        os.makedirs(config.DATA_DIR, exist_ok=True)

        if os.path.exists(config.PORTFOLIO_FILE):
            try:
                with open(config.PORTFOLIO_FILE, 'r') as f:
                    data = json.load(f)
                positions = {
                    k: Position(**v) for k, v in data.get('positions', {}).items()
                }
                return Portfolio(
                    cash=data['cash'],
                    initial_capital=data['initial_capital'],
                    positions=positions,
                    total_trades=data['total_trades'],
                    winning_trades=data['winning_trades'],
                    created_at=data['created_at'],
                    last_updated=data['last_updated']
                )
            except Exception as e:
                print(f"Error loading portfolio: {e}, creating new one")

        # Create new portfolio
        now = datetime.utcnow().isoformat()
        return Portfolio(
            cash=config.INITIAL_CAPITAL,
            initial_capital=config.INITIAL_CAPITAL,
            positions={},
            total_trades=0,
            winning_trades=0,
            created_at=now,
            last_updated=now
        )

    def save(self):
        """Save portfolio to disk"""
        os.makedirs(config.DATA_DIR, exist_ok=True)
        data = {
            'cash': self.portfolio.cash,
            'initial_capital': self.portfolio.initial_capital,
            'positions': {k: asdict(v) for k, v in self.portfolio.positions.items()},
            'total_trades': self.portfolio.total_trades,
            'winning_trades': self.portfolio.winning_trades,
            'created_at': self.portfolio.created_at,
            'last_updated': self.portfolio.last_updated
        }
        with open(config.PORTFOLIO_FILE, 'w') as f:
            json.dump(data, f, indent=2)

    def update_position_price(self, market_id: str, current_price: float):
        """Update the current price of a position"""
        if market_id in self.portfolio.positions:
            pos = self.portfolio.positions[market_id]
            pos.current_price = current_price
            pos.last_updated = datetime.utcnow().isoformat()
            self.portfolio.last_updated = datetime.utcnow().isoformat()
            self.save()

    def open_position(self, market_id: str, market_question: str, outcome: str,
                      tokens: float, price: float, cost: float) -> bool:
        """Open a new position or add to existing"""
        if cost > self.portfolio.cash:
            print(f"Insufficient cash: need ${cost}, have ${self.portfolio.cash}")
            return False

        if len(self.portfolio.positions) >= config.MAX_POSITIONS and market_id not in self.portfolio.positions:
            print(f"Maximum positions reached: {config.MAX_POSITIONS}")
            return False

        if cost > config.MAX_POSITION_SIZE:
            print(f"Position size ${cost} exceeds max ${config.MAX_POSITION_SIZE}")
            return False

        now = datetime.utcnow().isoformat()

        if market_id in self.portfolio.positions:
            # Add to existing position
            pos = self.portfolio.positions[market_id]
            total_cost = pos.cost_usd + cost
            total_tokens = pos.tokens + tokens
            pos.avg_price = total_cost / total_tokens if total_tokens > 0 else 0
            pos.tokens = total_tokens
            pos.cost_usd = total_cost
            pos.current_price = price
            pos.last_updated = now
        else:
            # Create new position
            self.portfolio.positions[market_id] = Position(
                market_id=market_id,
                market_question=market_question,
                outcome=outcome,
                tokens=tokens,
                avg_price=price,
                current_price=price,
                cost_usd=cost,
                opened_at=now,
                last_updated=now
            )

        self.portfolio.cash -= cost
        self.portfolio.total_trades += 1
        self.portfolio.last_updated = now
        self.save()

        print(f"Opened position: {outcome} on '{market_question[:50]}...' - {tokens:.2f} tokens @ ${price:.4f} = ${cost:.2f}")
        return True

    def close_position(self, market_id: str, price: float) -> Optional[Dict]:
        """Close a position and return trade result"""
        if market_id not in self.portfolio.positions:
            return None

        pos = self.portfolio.positions[market_id]
        proceeds = pos.tokens * price
        pnl = proceeds - pos.cost_usd

        if pnl > 0:
            self.portfolio.winning_trades += 1

        self.portfolio.cash += proceeds
        del self.portfolio.positions[market_id]
        self.portfolio.last_updated = datetime.utcnow().isoformat()
        self.save()

        result = {
            'market_id': market_id,
            'market_question': pos.market_question,
            'outcome': pos.outcome,
            'tokens': pos.tokens,
            'entry_price': pos.avg_price,
            'exit_price': price,
            'cost': pos.cost_usd,
            'proceeds': proceeds,
            'pnl': pnl,
            'pnl_percent': (pnl / pos.cost_usd) * 100 if pos.cost_usd > 0 else 0
        }

        print(f"Closed position: {pos.outcome} on '{pos.market_question[:50]}...'")
        print(f"  Entry: ${pos.avg_price:.4f}, Exit: ${price:.4f}, P&L: ${pnl:+.2f} ({result['pnl_percent']:+.1f}%)")

        return result

    def get_summary(self) -> Dict:
        """Get portfolio summary"""
        return {
            'cash': self.portfolio.cash,
            'positions_count': len(self.portfolio.positions),
            'total_position_value': self.total_position_value(),
            'total_value': self.total_value(),
            'total_pnl': self.total_pnl(),
            'total_pnl_percent': self.total_pnl_percent(),
            'total_exposure': self.portfolio.total_exposure,
            'total_trades': self.portfolio.total_trades,
            'winning_trades': self.portfolio.winning_trades,
            'win_rate': (self.portfolio.winning_trades / self.portfolio.total_trades * 100
                        if self.portfolio.total_trades > 0 else 0)
        }

    # Delegate properties to portfolio
    def total_position_value(self) -> float:
        return self.portfolio.total_position_value

    def total_value(self) -> float:
        return self.portfolio.total_value

    def total_pnl(self) -> float:
        return self.portfolio.total_pnl

    def total_pnl_percent(self) -> float:
        return self.portfolio.total_pnl_percent


if __name__ == "__main__":
    # Test portfolio manager
    pm = PortfolioManager()
    print("Portfolio Summary:")
    print(json.dumps(pm.get_summary(), indent=2))
