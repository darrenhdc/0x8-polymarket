# Polymarket AI Trading Agent

Autonomous AI trading agent for Polymarket prediction markets using paper trading (virtual funds).

## Overview

This system implements an autonomous trading agent that:
- Monitors Polymarket for trading opportunities
- Analyzes markets using multiple strategies
- Makes trading decisions autonomously
- Tracks portfolio performance in real-time

## Initial Setup

- **Virtual Capital**: $5,000 USD
- **Max Position Size**: $500 per position
- **Max Total Exposure**: $2,500
- **Confidence Threshold**: 50%

## Quick Start

```bash
# Check current status
python3 status.py

# Run a single trading cycle
python3 agent.py --once

# Run continuously (every 5 minutes)
python3 agent.py --interval 300

# Or use the runner script
./run.sh --once     # Single cycle
./run.sh --status   # Status only
./run.sh            # Continuous
```

## Architecture

```
trading_system/
├── config.py          # Configuration parameters
├── portfolio.py       # Portfolio & position management
├── market_data.py     # Polymarket API integration
├── decision_engine.py # AI decision making
├── trade_executor.py  # Trade execution
├── agent.py           # Main trading loop
├── status.py          # Status display utility
├── data/              # Persistent data storage
│   ├── portfolio.json
│   ├── trades.json
│   └── decisions.json
└── logs/              # Log files
```

## Trading Strategies

The agent uses multiple strategies to find opportunities:

1. **Mispricing Detection** - Finds markets where YES + NO ≠ 1.0
2. **Momentum Strategy** - Buys low-priced outcomes with good liquidity
3. **Liquidity Arbitrage** - Looks for tight spreads at attractive prices
4. **Contrarian Strategy** - Bets against extreme market confidence (>90%)
5. **Asymmetric Value** - Identifies potentially undervalued outcomes

## Risk Management

- Stop Loss: 15% per position
- Take Profit: 30% per position
- Maximum 10 concurrent positions
- Position sizing based on confidence level

## Data Sources

- **Gamma API**: Market data, events, prices
- **CLOB API**: Orderbook, price history
- All data from official Polymarket APIs

## Current Positions

Run `python3 status.py` to see current positions and P&L.

## Important Notes

- This is **paper trading** - no real money is at risk
- Decisions are logged for transparency and analysis
- Strategies can be adjusted in `config.py` and `decision_engine.py`
- The agent runs autonomously but can be monitored via status checks

## Monitoring

The system is designed to run continuously. Check progress with:

```bash
# Quick status check
python3 status.py

# View recent trades
cat data/trades.json | python3 -m json.tool

# View decision log
cat data/decisions.json | python3 -m json.tool
```

## Disclaimer

This is an experimental system for research purposes. Paper trading only - no real funds are used or at risk.
