# Handoff: Polymarket Weather Trading System

You are taking over a **GFS weather arbitrage trading system** for Polymarket prediction markets. Read `AGENTS.md` for full coding standards and project conventions before writing any code.

## What This Project Does

The system exploits a pricing inefficiency: Polymarket retail traders price weather markets by gut feel, while we have **GFS (Global Forecast System)** — a free, publicly available NOAA physics model. The edge comes from information asymmetry, not technical analysis.

**Core loop:** GFS forecast → adjust for known bias → compute probability via normal CDF → compare to Polymarket price → if |edge| > 5%, trade.

## Current State (as of 2026-06-14)

### What's Working
- **Data pipeline complete**: 21 cities, Jan–Jun 2026 data backfilled
- **Two databases** in `data/`:
  - `weather_markets.db` (70 MB) — markets, price_history, resolved outcomes
  - `gfs_forecasts.db` (18 MB) — GFS forecasts (T+0 through T+3), observed weather
- **CLI operational** — `python3 cli.py` with 13 subcommands (backfill, signals, backtest, calibrate, coverage, performance, sota, walk-forward, factor-ic, etc.)
- **Backtest engine** — `WeatherBacktester` in `src/data/weather_backtester.py` with two paths:
  - `run()` — legacy, fixed bias=+0.89°C, sigma=1.79°C
  - `run_standard()` — pluggable `PredictionSource`, rolling calibration per-trade
- **Research framework** — walk-forward validation (`research/walk_forward.py`), factor IC analysis (`research/factor_ic.py`)

### Backtest Results (Hong Kong, Mar–May 2026, 68 days)

T+0 vs T+1/T+2/T+3 lead-time comparison was run. **Conclusion: T+0 (same-day forecast) is optimal.** Earlier forecasts find more edge but lose accuracy, netting ~breakeven.

| Lead | Trades | Win Rate | PnL | Avg \|edge\| |
|------|--------|----------|-----|-------------|
| T+0  | 514    | 51%      | +$881 | 15.9% |
| T+1  | 637    | 44%      | +$19  | 17.7% |
| T+2  | 653    | 44%      | +$85  | 17.8% |
| T+3  | 648    | 43%      | -$2   | 17.9% |

The original `run()` backtest on the same window: 782 trades, +$1,623 (discrepancy due to price snapshot selection + rolling vs fixed calibration).

### Key Constants
- `DEFAULT_SIGMA_TEMP` = 1.79°C (GFS temperature residual std)
- `DEFAULT_BIAS_TEMP` = +0.89°C (GFS cold bias, Hong Kong)
- `MIN_EDGE` = 0.05, `MAX_SANE_EDGE` = 0.60
- `MIN_CALIB_PAIRS` = 5, `DEFAULT_CALIB_WINDOW` = 20 days

## Architecture

```
src/data/           ← ACTIVE: database, backtester, prediction sources, edge computation
  database.py         SQLite schema (WAL, FK enabled)
  prediction_interface.py  ABC: PredictionSource, MarketContext, Prediction, EdgeSignal
  prediction_registry.py   Singleton registry for prediction sources
  edge_composer.py         compute_edge() + fuse_predictions()
  gfs_prediction.py        GFSPredictionSource (temp) + GFSPrecipSource (precip), rolling calib
  gfs_history.py           GFSHistoryCollector (Open-Meteo historical backfill)
  polymarket_history.py    PolymarketHistoryCollector (Gamma + CLOB price backfill)
  weather_backtester.py    WeatherBacktester.run() / run_standard() / calibrate()
src/core/           ← DEPRECATED (generic event trading, 7.7% win rate, abandoned)
src/strategies/     ← DEPRECATED
src/execution/      ← Paper trading works; real trading needs py_clob_client
src/risk/           ← RiskManager (10-rule gate)
src/weather/        ← 1 broken import, partially superseded by src/data/
src/scripts/        ← 12 operational scripts (backfill, signals, coverage, etc.)
research/           ← Walk-forward + IC analysis, reports/SOTA.md
monitor/            ← Heartbeat health checks
cli.py              ← Unified entry point (13 subcommands)
```

**Two data directories exist** (by design):
- `data/` — weather research (used by `database.py`)
- `trading_system/data/` — generic agent runtime (used by `config.py`)

## Known Issues

1. **`src/weather/gfs_weather_pipeline.py:25`** — imports `PredictionRegistry`/`get_registry` from `prediction_interface`, but they're in `prediction_registry.py`. Broken import, easy fix.
2. **Missing deps** (not in venv): `flask` (dashboard), `xarray` (NOAA scripts), `eth_account` (keystore). `py-clob-client` declared in requirements.txt but not installed.
3. **`tests/test_llm.py`** — bare imports, won't run. Not a real test.
4. **`_archived/`** — empty; AGENTS.md references a file that doesn't exist.

## Build & Run

```bash
# Already done — data is backfilled. These are for reference.
pip install -r requirements.txt
python3 -c "from src.data.database import init_all; init_all()"
python3 cli.py backfill --city all --start 2026-01-01

# Daily operations
python3 cli.py signals                    # Generate today's trading signals
python3 cli.py backtest --city hong-kong --start 2026-03-01 --end 2026-05-31
python3 cli.py calibrate --city all       # Recompute bias/sigma
python3 cli.py coverage                   # Data coverage report
python3 cli.py performance                # Portfolio performance
python3 cli.py sota                       # View current best strategy

# Research
python3 -m research.walk_forward --city hong-kong --start 2026-01-01 --end 2026-05-31
python3 -m research.factor_ic --city all --start 2026-01-01 --end 2026-05-31
```

## Suggested Next Steps

1. **Fix the broken import** in `src/weather/gfs_weather_pipeline.py` (one line).
2. **Write the candidate report** — T+0 vs T+1/T+2/T+3 comparison → `research/reports/20260614_lead_time_comparison.md` (use `TEMPLATE.md`).
3. **Update SOTA.md** — register current best strategy (T+0, rolling calib, min_edge 5%).
4. **Multi-city backtest** — Hong Kong works, but verify the edge exists across other cities before live trading.
5. **Paper trade** — run `python3 cli.py signals` daily and track paper P&L for 2-4 weeks to validate live performance matches backtest.
6. **Consider real trading** only after paper validation + multi-city confirmation.

## Critical Conventions

- **Never** modify `weather_backtester.run()` — use `run_standard()` for new work.
- **Always** use `from __future__ import annotations` + type hints.
- **No scipy** — normal CDF via `math.erf`.
- **No comments** unless explicitly asked.
- All new strategies need walk-forward + IC validation before entering `SOTA.md`.
- Paper trading is default (`PAPER_TRADING=true`). Never commit private keys.
