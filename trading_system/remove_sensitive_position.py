#!/usr/bin/env python3
"""
Remove sensitive position from portfolio
"""
import sys
import os

# Add parent dir to path
sys.path.insert(0, os.path.dirname(__file__))

from portfolio import PortfolioManager

def main():
    pm = PortfolioManager()

    # Market ID for Xi Jinping position
    sensitive_market_id = "559651"

    if sensitive_market_id in pm.portfolio.positions:
        pos = pm.portfolio.positions[sensitive_market_id]
        print(f"Closing sensitive position: {pos.market_question}")

        # Close the position at current price
        result = pm.close_position(sensitive_market_id, pos.current_price)

        if result:
            print(f"Successfully closed position")
            print(f"  Cash returned: ${result['proceeds']:.2f}")
            print(f"  P&L: ${result['pnl']:.2f}")
        else:
            print("Failed to close position")
    else:
        print(f"No position found for market {sensitive_market_id}")

    print("\nPortfolio Summary:")
    print(f"  Cash: ${pm.portfolio.cash:.2f}")
    print(f"  Positions: {len(pm.portfolio.positions)}")

if __name__ == "__main__":
    main()
