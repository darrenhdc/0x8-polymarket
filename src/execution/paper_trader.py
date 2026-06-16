#!/usr/bin/env python3
"""Paper trading engine for weather markets — local simulation, no wallet needed.

Portfolio: data/paper_portfolio.json (auto-created)
Trades:    data/paper_trades.json   (append-only log)

Usage:
    python3 cli.py trade --city hong-kong            # run one cycle
    python3 cli.py paper-status                       # show portfolio
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import requests

PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
PORTFOLIO_FILE = DATA_DIR / "paper_portfolio.json"
TRADES_FILE = DATA_DIR / "paper_trades.json"

CLOB_BOOK_URL = "https://clob.polymarket.com/book"
INITIAL_CAPITAL = 1000.0
MIN_EDGE = 0.05
POSITION_SIZE = 10.0  # $10 per trade


@dataclass
class PaperTrade:
    trade_id: str
    timestamp: str
    market_id: str
    question: str
    target_date: str
    threshold_value: float
    direction: str  # BUY_YES / BUY_NO
    entry_price: float  # YES price at entry
    tokens: float
    cost_usd: float
    edge: float
    model_prob: float
    closed: bool = False
    close_date: Optional[str] = None
    outcome: Optional[str] = None  # "won" / "lost" / None
    pnl: float = 0.0


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _load_portfolio() -> dict:
    if PORTFOLIO_FILE.exists():
        return json.loads(PORTFOLIO_FILE.read_text())
    return {
        "cash": INITIAL_CAPITAL,
        "initial_capital": INITIAL_CAPITAL,
        "open_positions": {},
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def _save_portfolio(pf: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PORTFOLIO_FILE.write_text(json.dumps(pf, indent=2))


def _load_trades() -> list[dict]:
    if TRADES_FILE.exists():
        return json.loads(TRADES_FILE.read_text())
    return []


def _save_trades(trades: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TRADES_FILE.write_text(json.dumps(trades, indent=2))


def _fetch_yes_price(token_id: str) -> Optional[float]:
    """Get market YES price from best bid (most conservative/executable).

    For thin markets, uses best bid since that's the highest price someone
    is actively willing to pay. Returns None if no bids exist or market
    is completely dead.
    """
    try:
        r = requests.get(CLOB_BOOK_URL, params={"token_id": token_id}, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        bids = data.get("bids", [])
        
        if not bids:
            return None
        
        best_bid = float(bids[0]["price"])
        best_bid_size = float(bids[0].get("size", 0))
        
        # Skip dead markets (0.001 bids are just dust)
        if best_bid < 0.005 or best_bid_size < 10:
            return None
        
        return round(best_bid, 4)
    except Exception:
        return None


def run_cycle(city_filter: str = "Hong Kong", min_edge: float = MIN_EDGE) -> list[dict]:
    """Run one paper trading cycle. Returns list of trades executed this cycle."""

    from src.data.database import connect_markets, init_weather_db
    from src.data.gfs_prediction import GFSPredictionSource
    from src.data.prediction_interface import MarketContext
    from src.data.geocoding import normalize_location_id

    print(f"[paper-trader] {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"[paper-trader] Capital: ${INITIAL_CAPITAL} | Size: ${POSITION_SIZE}/trade\n")

    market_conn = connect_markets()
    market_conn.row_factory = __import__("sqlite3").Row
    init_weather_db(market_conn)

    today = date.today().isoformat()
    active = market_conn.execute(
        """
        SELECT id, question, city, country, market_type,
               threshold_value, threshold_unit, target_date,
               latitude, longitude, clob_token_ids, volume
        FROM markets
        WHERE city = ? AND market_type = 'temp_above'
          AND resolved_outcome IS NULL
        ORDER BY target_date, threshold_value
        """,
        (city_filter,),
    ).fetchall()
    print(f"[paper-trader] Active markets: {len(active)}")

    portfolio = _load_portfolio()
    trades = _load_trades()
    new_trades: list[dict] = []

    source = GFSPredictionSource(mode="live")
    skipped_no_token = 0
    skipped_no_price = 0

    for mkt in active:
        if portfolio["cash"] < POSITION_SIZE:
            print(f"[paper-trader] Cash depleted (${portfolio['cash']:.2f}) — stopping")
            break

        # Skip if already in a position for this market
        existing = [t for t in trades if t.get("market_id") == mkt["id"] and not t.get("closed")]
        if existing:
            continue

        # Extract YES token ID
        token_ids_str = mkt["clob_token_ids"] or ""
        token_ids = token_ids_str.replace('"', "").replace("'", "").strip("[]").split(",")
        if len(token_ids) < 1:
            skipped_no_token += 1
            continue

        yes_token_id = token_ids[0].strip()
        yes_price = _fetch_yes_price(yes_token_id)
        if yes_price is None:
            skipped_no_price += 1
            continue

        # Build market context
        q = mkt["question"] or ""
        ql = q.lower()
        variable = "temperature_2m_min" if ("lowest" in ql or "minimum" in ql) else "temperature_2m_max"
        rule = "eq"
        if "or below" in ql or "less than" in ql:
            rule = "lte"
        elif "or higher" in ql or "or above" in ql or "more than" in ql or "above" in ql:
            rule = "gte"

        ctx = MarketContext(
            market_id=mkt["id"],
            question=q,
            outcomes=["Yes", "No"],
            outcome_prices=[yes_price, round(1.0 - yes_price, 4)],
            city=mkt["city"] or "",
            country=mkt["country"] or "",
            target_date=mkt["target_date"],
            market_type=mkt["market_type"],
            threshold_value=float(mkt["threshold_value"]),
            threshold_unit=mkt["threshold_unit"] or "",
            variable=variable,
            rule=rule,
            latitude=float(mkt["latitude"] or 0),
            longitude=float(mkt["longitude"] or 0),
            location_id=normalize_location_id(mkt["city"] or "", mkt["country"] or ""),
            extra={"price_date": today},
        )

        if not source.can_predict(ctx):
            continue

        prediction = source.predict(ctx)
        if prediction is None:
            continue

        model_prob = prediction.estimated_probability
        edge = model_prob - yes_price
        abs_edge = abs(edge)

        if abs_edge < min_edge:
            continue

        # Determine direction
        if edge > 0:
            direction = "BUY_YES"
            trade_price = yes_price
        else:
            direction = "BUY_NO"
            trade_price = round(1.0 - yes_price, 4)

        tokens = POSITION_SIZE / min(max(trade_price, 0.001), 0.999)
        cost = POSITION_SIZE

        if cost > portfolio["cash"]:
            continue

        portfolio["cash"] -= cost

        trade = PaperTrade(
            trade_id=f"T{len(trades) + len(new_trades):06d}",
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            market_id=mkt["id"],
            question=q,
            target_date=mkt["target_date"],
            threshold_value=float(mkt["threshold_value"]),
            direction=direction,
            entry_price=yes_price,
            tokens=round(tokens, 4),
            cost_usd=round(cost, 2),
            edge=round(edge, 4),
            model_prob=round(model_prob, 4),
        )

        new_trades.append(asdict(trade))
        print(
            f"  [{direction}] {q[:50]:<50} "
            f"price={yes_price:.3f} prob={model_prob:.3f} edge={edge:+.1%} "
            f"cost=${cost:.2f}"
        )

        time.sleep(0.1)  # rate limit

    source.close()
    market_conn.close()

    if new_trades:
        trades.extend(new_trades)
        _save_trades(trades)
        _save_portfolio(portfolio)
        print(f"\n[paper-trader] Executed {len(new_trades)} trades")
        print(f"[paper-trader] Cash remaining: ${portfolio['cash']:.2f}")
    else:
        print(
            f"[paper-trader] No trades (skipped: {skipped_no_token} no-token, "
            f"{skipped_no_price} no-price)"
        )

    _print_summary(portfolio, trades)
    return new_trades


def _print_summary(portfolio: dict, trades: list[dict]) -> None:
    closed = [t for t in trades if t.get("closed")]
    open_positions = [t for t in trades if not t.get("closed")]
    won = [t for t in closed if t.get("outcome") == "won"]
    lost = [t for t in closed if t.get("outcome") == "lost"]
    total_pnl = sum(t.get("pnl", 0) for t in closed)
    total_cost = sum(t.get("cost_usd", 0) for t in open_positions)

    portfolio_value = portfolio["cash"] + total_cost + total_pnl

    print(f"\n{'='*60}")
    print(f"PAPER TRADING SUMMARY")
    print(f"{'='*60}")
    print(f"  Initial:     ${portfolio.get('initial_capital', INITIAL_CAPITAL):,.2f}")
    print(f"  Cash:        ${portfolio['cash']:,.2f}")
    print(f"  Positions:   {len(open_positions)} open (${total_cost:,.2f})")
    print(f"  Realized PnL: ${total_pnl:+,.2f}")
    print(f"  Total value:  ${portfolio_value:,.2f}")
    if closed:
        print(f"  Closed:      {len(closed)} ({len(won)}W / {len(lost)}L)")
    print(f"{'='*60}")


def status() -> dict:
    portfolio = _load_portfolio()
    trades = _load_trades()
    pf = dict(portfolio)
    pf["total_trades"] = len(trades)
    pf["open_trades"] = len([t for t in trades if not t.get("closed")])
    closed = [t for t in trades if t.get("closed")]
    pf["closed_trades"] = len(closed)
    pf["realized_pnl"] = round(sum(t.get("pnl", 0) for t in closed), 2)
    pf["won"] = len([t for t in closed if t.get("outcome") == "won"])
    pf["lost"] = len([t for t in closed if t.get("outcome") == "lost"])
    return pf
