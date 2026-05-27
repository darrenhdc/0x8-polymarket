#!/usr/bin/env python3
"""
Quick status check script
"""
import sys
import os

from datetime import datetime
from .portfolio import PortfolioManager
from src.data.market_data import MarketData
from src.execution.trade_executor import TradeExecutor
from src.strategies.decision_engine import DecisionEngine
import json


def main():
    print("=" * 60)
    print("   POLYMARKET AI AGENT - STATUS CHECK")
    print(f"   {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    # Load portfolio
    pm = PortfolioManager()
    summary = pm.get_summary()

    print("\n📊 PORTFOLIO SUMMARY")
    print("-" * 40)
    print(f"  💵 Cash:           ${summary['cash']:,.2f}")
    print(f"  📈 Positions:      {summary['positions_count']}")
    print(f"  💰 Position Value: ${summary['total_position_value']:,.2f}")
    print(f"  🏦 Total Value:    ${summary['total_value']:,.2f}")

    pnl_color = "🟢" if summary['total_pnl'] >= 0 else "🔴"
    print(f"  {pnl_color} Total P&L:      ${summary['total_pnl']:+,.2f} ({summary['total_pnl_percent']:+.2f}%)")

    print(f"  ⚠️  Exposure:        ${summary['total_exposure']:,.2f} / $2500 max")
    print(f"  📊 Trades:         {summary['total_trades']} (Win rate: {summary['win_rate']:.1f}%)")

    # Show positions
    if pm.portfolio.positions:
        print("\n📂 OPEN POSITIONS")
        print("-" * 40)
        for market_id, pos in pm.portfolio.positions.items():
            pnl_str = f"${pos.pnl:+,.2f} ({pos.pnl_percent:+.1f}%)"
            print(f"\n  [{pos.outcome}] {pos.market_question[:50]}...")
            print(f"       🪙 Tokens: {pos.tokens:.2f} @ ${pos.avg_price:.4f}")
            print(f"       💵 Cost: ${pos.cost_usd:.2f} → Value: ${pos.current_value:.2f}")
            pnl_emoji = "🟢" if pos.pnl >= 0 else "🔴"
            print(f"       {pnl_emoji} P&L: {pnl_str}")

    # Load trade history
    executor = TradeExecutor(pm)
    stats = executor.get_trade_stats()

    print("\n📈 TRADING STATS")
    print("-" * 40)
    print(f"  Total trades: {stats['total_trades']}")
    print(f"  Buy orders:   {stats['buy_trades']}")
    print(f"  Sell orders:  {stats['sell_trades']}")
    print(f"  Volume:       ${stats.get('total_volume', 0):,.2f}")

    # Load decision history
    engine = DecisionEngine(pm)
    decision_summary = engine.get_decision_summary()

    print("\n🧠 DECISION ENGINE")
    print("-" * 40)
    print(f"  Total decisions:    {decision_summary['total_decisions']}")
    print(f"  Avg confidence:     {decision_summary['avg_confidence']:.2f}")
    print(f"  Decision breakdown:")
    for dtype, count in decision_summary['decision_types'].items():
        print(f"    - {dtype}: {count}")

    print("\n" + "=" * 60)

    # Return summary for scripting
    return summary


if __name__ == "__main__":
    main()
