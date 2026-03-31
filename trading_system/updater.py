"""
Background price updater for dashboard
Updates portfolio prices every 60 seconds
"""
import time
import sys
import os

# Add parent dir to path
sys.path.insert(0, os.path.dirname(__file__))

from portfolio import PortfolioManager
from market_data import MarketData

def update_prices():
    """Update prices for all positions"""
    portfolio = PortfolioManager()
    market_data = MarketData()

    for market_id, pos in portfolio.portfolio.positions.items():
        try:
            market = market_data.scanner.api.get_market_by_id(market_id)
            if market:
                prices = market_data.scanner.get_market_prices(market)
                price = prices.get(pos.outcome.lower(), pos.current_price)
                portfolio.update_position_price(market_id, price)
                print(f"Updated {market_id}: {pos.outcome} @ {price:.4f}")
        except Exception as e:
            print(f"Error updating {market_id}: {e}")

    portfolio.save()
    return portfolio.get_summary()

if __name__ == "__main__":
    print("Starting price updater (every 60s)...")
    while True:
        try:
            summary = update_prices()
            print(f"Total Value: ${summary['total_value']:.2f} | P&L: ${summary['total_pnl']:+.2f}")
        except Exception as e:
            print(f"Update error: {e}")

        time.sleep(60)
