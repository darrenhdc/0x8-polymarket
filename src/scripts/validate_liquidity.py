"""Validate CLOB orderbook liquidity for all active HK weather markets.

Fetches current bid/ask from CLOB, compares with last traded price, and
writes data/liquidity_report.csv.

Usage:
    python3 -m src.scripts.validate_liquidity
    python3 src/scripts/validate_liquidity.py
"""
from __future__ import annotations

import csv
import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"

DB_PATH = Path(__file__).parent.parent.parent / "data" / "weather_markets.db"
REPORT_PATH = Path(__file__).parent.parent.parent / "data" / "liquidity_report.csv"

SLEEP_BETWEEN_CALLS = 0.15  # seconds — stay well under rate limits
HISTORY_LOOKBACK_SECS = 86_400 * 7  # 7 days for last-traded-price lookup
MIN_TRADEABLE_DEPTH = 10.0  # tokens — below this, market is effectively illiquid


# ---------------------------------------------------------------------------
# CLOB helpers
# ---------------------------------------------------------------------------

def fetch_orderbook(token_id: str, session: requests.Session) -> Optional[dict]:
    """Fetch YES-token orderbook. Returns raw response dict or None on error."""
    try:
        resp = session.get(CLOB_BOOK_URL, params={"token_id": token_id}, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        return None
    except (requests.RequestException, ValueError):
        return None


def fetch_last_price(token_id: str, session: requests.Session) -> Optional[float]:
    """Return the most recent traded price for a YES token (last 7 days)."""
    now = int(time.time())
    try:
        resp = session.get(
            CLOB_HISTORY_URL,
            params={
                "market": token_id,
                "startTs": now - HISTORY_LOOKBACK_SECS,
                "endTs": now,
                "fidelity": 60,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        pts = resp.json().get("history", [])
        if pts:
            return float(pts[-1]["p"])
        return None
    except (requests.RequestException, ValueError, KeyError):
        return None


def parse_orderbook(book: dict) -> dict:
    """Extract best bid, best ask, depths, and spread from raw orderbook dict."""
    bids = book.get("bids", [])  # sorted descending by price
    asks = book.get("asks", [])  # sorted ascending by price

    best_bid = float(bids[0]["price"]) if bids else None
    best_ask = float(asks[0]["price"]) if asks else None

    # Depth = sum of size at best level only (top-of-book liquidity)
    bid_depth = sum(float(b["size"]) for b in bids[:3])   # top 3 levels
    ask_depth = sum(float(a["size"]) for a in asks[:3])

    mid = None
    spread_pct = None
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2.0
        spread_pct = (best_ask - best_bid) / mid * 100.0 if mid > 0 else None

    # Min trade size possible: how many tokens you can buy/sell at best level
    min_trade_tokens = min(
        float(bids[0]["size"]) if bids else 0.0,
        float(asks[0]["size"]) if asks else 0.0,
    )

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_pct": spread_pct,
        "bid_depth_tokens": bid_depth,
        "ask_depth_tokens": ask_depth,
        "min_trade_size_possible": min_trade_tokens,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> list[dict]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    markets = conn.execute(
        """
        SELECT id, question, target_date, threshold_value, clob_token_ids,
               active, closed
        FROM markets
        WHERE city = 'Hong Kong'
          AND target_date >= date('now')
          AND resolved_outcome IS NULL
          AND clob_token_ids IS NOT NULL
        ORDER BY target_date, threshold_value
        """
    ).fetchall()
    conn.close()

    print(f"Found {len(markets)} active unresolved HK markets to validate.")

    session = requests.Session()
    session.headers.update({"User-Agent": "polymarket-weather-bot/1.0"})

    results = []

    for i, mkt in enumerate(markets, 1):
        tokens = json.loads(mkt["clob_token_ids"])
        if not tokens:
            continue
        yes_token = tokens[0]

        # Fetch book + last price (two calls per market)
        book_raw = fetch_orderbook(yes_token, session)
        time.sleep(SLEEP_BETWEEN_CALLS)
        last_price = fetch_last_price(yes_token, session)
        time.sleep(SLEEP_BETWEEN_CALLS)

        if book_raw is None:
            ob = {
                "best_bid": None, "best_ask": None, "mid": None,
                "spread_pct": None, "bid_depth_tokens": 0.0,
                "ask_depth_tokens": 0.0, "min_trade_size_possible": 0.0,
            }
            status = "NO_BOOK"
        else:
            ob = parse_orderbook(book_raw)
            status = "OK"

        price_diff = None
        if ob["mid"] is not None and last_price is not None:
            price_diff = abs(ob["mid"] - last_price)

        row = {
            "market_id": mkt["id"],
            "question": mkt["question"],
            "target_date": mkt["target_date"],
            "threshold_value": mkt["threshold_value"],
            "yes_token": yes_token,
            "best_bid": ob["best_bid"],
            "best_ask": ob["best_ask"],
            "mid": ob["mid"],
            "spread_pct": ob["spread_pct"],
            "bid_depth_tokens": ob["bid_depth_tokens"],
            "ask_depth_tokens": ob["ask_depth_tokens"],
            "min_trade_size_possible": ob["min_trade_size_possible"],
            "last_traded_price": last_price,
            "price_diff_vs_clob_history": price_diff,
            "status": status,
        }
        results.append(row)

        spread_str = f"{ob['spread_pct']:.1f}%" if ob["spread_pct"] is not None else "N/A"
        print(
            f"  [{i:3d}/{len(markets)}] {mkt['target_date']} {mkt['threshold_value']:4.0f}°C | "
            f"bid={ob['best_bid']} ask={ob['best_ask']} spread={spread_str} "
            f"depth={ob['ask_depth_tokens']:.0f} last={last_price}"
        )

    return results


def write_report(results: list[dict]) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not results:
        print("No results to write.")
        return

    fieldnames = list(results[0].keys())
    with REPORT_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow(row)
    print(f"\nReport written: {REPORT_PATH}")


def print_summary(results: list[dict]) -> None:
    valid = [r for r in results if r["spread_pct"] is not None]
    if not valid:
        print("No markets with valid orderbooks.")
        return

    tight   = [r for r in valid if r["spread_pct"] < 2.0]
    medium  = [r for r in valid if 2.0 <= r["spread_pct"] < 5.0]
    wide    = [r for r in valid if r["spread_pct"] >= 5.0]
    no_book = [r for r in results if r["status"] == "NO_BOOK"]
    liquid  = [r for r in valid if r["min_trade_size_possible"] >= MIN_TRADEABLE_DEPTH]

    print("\n" + "=" * 60)
    print("Liquidity Summary")
    print("=" * 60)
    print(f"  Total markets scanned:   {len(results)}")
    print(f"  With valid orderbook:    {len(valid)}")
    print(f"  No book (stale/closed):  {len(no_book)}")
    print()
    print(f"  Spread < 2%  (tight):    {len(tight)}")
    print(f"  Spread 2–5%  (medium):   {len(medium)}")
    print(f"  Spread > 5%  (wide):     {len(wide)}")
    print()
    print(f"  Liquid (depth ≥ {MIN_TRADEABLE_DEPTH:.0f} tok): {len(liquid)}")

    if valid:
        avg_spread = sum(r["spread_pct"] for r in valid) / len(valid)
        avg_depth  = sum(r["ask_depth_tokens"] for r in valid) / len(valid)
        print(f"  Avg spread:              {avg_spread:.1f}%")
        print(f"  Avg ask depth (top 3):   {avg_depth:.0f} tokens")

    # Per-date breakdown
    from itertools import groupby
    sorted_valid = sorted(valid, key=lambda r: r["target_date"])
    print()
    print("  Per target-date:")
    for date, group in groupby(sorted_valid, key=lambda r: r["target_date"]):
        grp = list(group)
        avg_s = sum(r["spread_pct"] for r in grp) / len(grp)
        max_depth = max(r["ask_depth_tokens"] for r in grp)
        print(f"    {date}: {len(grp):2d} markets | avg_spread={avg_s:.1f}% | max_depth={max_depth:.0f} tok")


def main() -> None:
    results = run()
    write_report(results)
    print_summary(results)


if __name__ == "__main__":
    main()
