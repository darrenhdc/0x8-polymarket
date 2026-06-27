"""
Convergence Take-Profit Monitor — close positions when market price captures
a configurable ratio of the edge gap, avoiding binary settlement risk.

Logic
-----
  Entry:   market_price = entry_price,  model_prob = GFS-implied P
  Edge gap = model_prob - entry_price  (positive for correct-direction trades)
  Exit:    when current_price reaches entry_price + RATIO * edge_gap
           default RATIO = 0.80 (capture 80% of edge, skip settlement risk)

Usage
-----
  python3 -m src.execution.convergence_monitor        # check only
  python3 -m src.execution.convergence_monitor --auto  # auto-exit converged positions
  python3 cli.py monitor                               # via CLI
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── project root on path ────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.core import config

TRADE_LOG_PATH = _PROJECT_ROOT / "data" / "trade_log.json"


def _load_trades() -> list[dict[str, Any]]:
    if not TRADE_LOG_PATH.exists():
        return []
    with open(TRADE_LOG_PATH) as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data.get("trades", [])
    return data


def _save_trades(trades: list[dict[str, Any]]) -> None:
    TRADE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TRADE_LOG_PATH, "w") as f:
        json.dump({"trades": trades}, f, indent=2, ensure_ascii=False)


def _fetch_order_book(token_id: str) -> dict[str, Any] | None:
    """Fetch CLOB order book for a token, return {bid, ask} or None."""
    try:
        import requests
        resp = requests.get(
            f"{config.CLOB_API}/book",
            params={"token_id": token_id},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        best_bid = max((float(b["price"]) for b in bids), default=0.0)
        best_ask = min((float(a["price"]) for a in asks), default=0.0)
        return {
            "bid": best_bid,
            "ask": best_ask,
        }
    except Exception as e:
        print(f"  ⚠️  Order book fetch failed for {token_id[:16]}...: {e}")
        return None


def _model_prob_for_trade(trade: dict[str, Any]) -> float | None:
    """Extract model-implied probability for the trade's direction.

    Prefers explicit model_P_not / model_P_eq fields from edge data.
    Falls back to: entry_price + edge (since edge = model_prob - entry_price).
    """
    edge = trade.get("edge", {})
    direction = edge.get("edge_direction", "")

    if direction == "BUY_NO":
        prob = edge.get("model_P_not")
        if prob is not None:
            return float(prob)
    elif direction == "BUY_YES":
        prob = edge.get("model_P_eq")
        if prob is not None:
            return float(prob)

    # Fallback: derive from entry_price + edge
    entry = _entry_price_for_trade(trade)
    edge_val = edge.get("edge")
    if entry is not None and edge_val is not None:
        return entry + float(edge_val)

    return None


def _entry_price_for_trade(trade: dict[str, Any]) -> float | None:
    """Extract entry price from trade's order or edge data."""
    order = trade.get("order", {})
    price = order.get("price")
    if price is not None:
        return float(price)
    edge = trade.get("edge", {})
    direction = edge.get("edge_direction", "")
    if direction == "BUY_NO":
        return edge.get("market_no_price_ask") or edge.get("market_no_ask")
    elif direction == "BUY_YES":
        return edge.get("market_yes_price_ask") or edge.get("market_yes_ask")
    return None


def _current_price_for_exit(token_id: str, direction: str) -> float | None:
    """Get the current sellable price (bid) for the token we hold."""
    ob = _fetch_order_book(token_id)
    if ob is None:
        return None
    # We SELL the token → we care about the bid
    return ob["bid"]


def _calc_convergence(entry_price: float, model_prob: float,
                      current_price: float) -> dict[str, Any]:
    """
    Calculate how much of the edge gap has been captured.

    Returns dict with:
      edge_gap      — model_prob - entry_price (the total available edge)
      captured_pct  — how much of the gap the price has moved through
      converged     — True if captured_pct >= CONVERGENCE_TAKE_PROFIT_RATIO
    """
    edge_gap = model_prob - entry_price
    if edge_gap <= 0:
        return {"edge_gap": edge_gap, "captured_pct": 0.0, "converged": False}

    captured_pct = (current_price - entry_price) / edge_gap
    converged = captured_pct >= config.CONVERGENCE_TAKE_PROFIT_RATIO
    return {
        "edge_gap": edge_gap,
        "captured_pct": captured_pct,
        "converged": converged,
    }


def _execute_exit(trade: dict[str, Any], current_price: float) -> bool:
    """Place SELL order and update trade_log.json. Returns True on success."""
    order = trade.get("order", {})
    token_id = order.get("token_id", "")
    shares = order.get("shares", 0)

    if not token_id or not shares:
        print(f"  ❌ Missing token_id or shares")
        return False

    try:
        # ── Load key & build client (same pattern as place_order.py) ──
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import (
            ApiCreds, OrderArgsV2, PartialCreateOrderOptions, OrderType,
        )

        backup_path = os.path.expanduser(
            "/home/darren/share/polymarket/config/.env.txt.backup"
        )
        import re
        with open(backup_path) as f:
            m = re.search(
                r"^\s*sk\s*=\s*([0-9a-fA-FxX]+)", f.read(), re.MULTILINE
            )
        pk = m.group(1).strip()
        pk = pk if pk.startswith("0x") else "0x" + pk

        creds = ApiCreds(
            api_key="f785f79c-3119-1c24-3489-3ac27718b741",
            api_secret="uLbsEVrSw-wTNHC1X4wZ5tQuHzaeiy6xpuJrXAGbFX4=",
            api_passphrase="04949246bc4fe4326d25df889a4271c52299b7bea07a5b38ed8566e7566fb61a",
        )
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=pk,
            creds=creds,
            signature_type=2,
            funder="0x1270215141EA0a2CdA89272722B2ac47DF6751A1",
        )

        args = OrderArgsV2(
            token_id=token_id, price=current_price, size=shares, side="SELL"
        )
        opts = PartialCreateOrderOptions(neg_risk=True, tick_size="0.01")
        resp = client.create_and_post_order(args, opts, order_type=OrderType.GTC)
        order_id = resp.get("orderID") or resp.get("id", "unknown")
        print(f"  ✅ SELL order placed: {order_id}")

        # ── Update trade_log.json ──
        trade["status"] = "closed_early"
        trade["exit"] = {
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "exit_reason": (
                f"Convergence take-profit: market price captured "
                f"{config.CONVERGENCE_TAKE_PROFIT_RATIO*100:.0f}% of edge gap"
            ),
            "exit_price_avg": current_price,
            "exit_shares": shares,
            "exit_proceeds_usd": round(shares * current_price, 2),
            "net_pnl_usd": round(
                shares * current_price - order.get("cost_usd", 0), 2
            ),
            "exit_order_id": str(order_id),
        }
        # Save all trades
        trades = _load_trades()
        for i, t in enumerate(trades):
            if t.get("trade_id") == trade.get("trade_id"):
                trades[i] = trade
                break
        _save_trades(trades)
        return True

    except Exception as e:
        print(f"  ❌ Exit failed: {e}")
        return False


# ── Stop-loss thresholds ─────────────────────────────────────────────────
HARD_STOP_LOSS_PCT = 0.50    # -50% → unconditional sell
STOP_WARNING_PCT = 0.20      # -20% → warning zone
STOP_CONFIRM_HOURS = 2.0     # must persist this long before triggering


# ── Main runner ──────────────────────────────────────────────────────────

def run(auto_exit: bool = False) -> int:
    """
    Monitor open positions for:
      1. Convergence take-profit (if no GTC limit order on book)
      2. Hard stop-loss (-50%)
      3. Edge invalidation (edge < 0.03)

    Parameters
    ----------
    auto_exit : bool
        If True, automatically place SELL orders when triggered.

    Returns
    -------
    int
        0 on success, 1 if errors encountered.
    """
    trades = _load_trades()
    open_trades = [t for t in trades if t.get("status") == "open"]

    print(f"\n{'='*60}")
    print(f"  POSITION MONITOR (TP + SL)")
    print(f"  TP: {config.CONVERGENCE_TAKE_PROFIT_RATIO*100:.0f}% convergence | "
          f"SL: -{HARD_STOP_LOSS_PCT*100:.0f}% hard")
    print(f"  Time: {datetime.now(timezone.utc).isoformat(timespec='seconds')}Z")
    print(f"  Open positions: {len(open_trades)}")
    print(f"{'='*60}\n")

    if not open_trades:
        print("  No open positions to monitor.")
        return 0

    errors = 0
    actions = 0

    for trade in open_trades:
        tid = trade.get("trade_id", "?")
        market = trade.get("market", {})
        question = market.get("question", "?")
        order = trade.get("order", {})
        token_id = order.get("token_id", "")
        direction = order.get("direction", "?")
        shares = order.get("shares", 0)
        cost = order.get("cost_usd", 0)

        print(f"─ [{tid}] {question}")
        print(f"  方向: {direction} | {shares} shares | cost ${cost:.2f}")

        model_prob = _model_prob_for_trade(trade)
        entry_price = _entry_price_for_trade(trade)

        if model_prob is None or entry_price is None:
            print(f"  ⚠️  Missing model_prob or entry_price")
            errors += 1
            continue

        current_price = _current_price_for_exit(token_id, direction)
        if current_price is None or current_price <= 0:
            print(f"  ⚠️  Cannot fetch current bid price")
            errors += 1
            continue

        # ── PnL calculation ──
        pnl_per_share = current_price - entry_price
        pnl_total = pnl_per_share * shares
        pnl_pct = (pnl_per_share / entry_price) * 100 if entry_price > 0 else 0

        # ── Convergence (take-profit) ──
        conv = _calc_convergence(entry_price, model_prob, current_price)
        target_tp = entry_price + config.CONVERGENCE_TAKE_PROFIT_RATIO * conv["edge_gap"]

        # ── Stop-loss checks ──
        hard_stop_price = entry_price * (1 - HARD_STOP_LOSS_PCT)
        stop_triggered = current_price <= hard_stop_price

        # Edge invalidation: edge reversed (market now disagrees with model)
        edge_now = model_prob - current_price
        edge_invalidated = edge_now < 0.03

        # Status icons
        print(f"  入场: {entry_price:.3f}  |  当前 bid: {current_price:.3f}  "
              f"|  模型: {model_prob:.3f}")
        print(f"  PnL: ${pnl_total:+.2f} ({pnl_pct:+.1f}%)")

        bar_len = 20
        capped_pct = max(0, min(conv["captured_pct"], 1.0))
        filled = int(capped_pct * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"  收敛: [{bar}] {conv['captured_pct']*100:.1f}%  "
              f"(TP 目标: {target_tp:.3f})")
        print(f"  止损: hard @{hard_stop_price:.3f}  "
              f"({'❌ 触发!' if stop_triggered else '✅ 安全'})")

        # ── Decide action ──
        if stop_triggered:
            print(f"  🚨 硬止损触发! 当前价 {current_price:.3f} ≤ {hard_stop_price:.3f}")
            if auto_exit:
                print(f"  🔄 执行止损平仓...")
                trade["exit"] = {"exit_reason": "hard_stop_loss"}
                success = _execute_exit(trade, current_price)
                actions += 1 if success else 0
                errors += 0 if success else 1
            else:
                print(f"  💡 加 --auto 自动止损")

        elif edge_invalidated and pnl_pct < 0:
            print(f"  ⚠️  Edge 已失效 ({edge_now:+.3f} < 0.03), 当前亏损 {pnl_pct:.1f}%")
            if auto_exit:
                print(f"  🔄 执行 edge 止损平仓...")
                trade["exit"] = {"exit_reason": "edge_invalidation"}
                success = _execute_exit(trade, current_price)
                actions += 1 if success else 0
                errors += 0 if success else 1
            else:
                print(f"  💡 加 --auto 自动止损")

        elif conv["converged"]:
            print(f"  ⚡ 收敛达标 ({config.CONVERGENCE_TAKE_PROFIT_RATIO*100:.0f}%)! "
                  f"(GTC 止盈单应在链上自动成交)")

        print()

    print(f"{'='*60}")
    print(f"  已检查: {len(open_trades)}  |  执行操作: {actions}  |  错误: {errors}")
    print(f"{'='*60}\n")

    return 1 if errors > 0 else 0


# ── CLI entry ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(
        description="Convergence take-profit monitor for Polymarket weather positions"
    )
    p.add_argument(
        "--auto", action="store_true",
        help="Automatically SELL converged positions",
    )
    args = p.parse_args()
    sys.exit(run(auto_exit=args.auto))
