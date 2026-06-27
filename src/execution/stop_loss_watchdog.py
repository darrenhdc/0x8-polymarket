#!/usr/bin/env python3
"""
Auto stop-loss watchdog — runs continuously, checks every N seconds.
Triggers SELL when position hits hard stop (-50%) or edge invalidation.

Usage:
  nohup python3 -m src.execution.stop_loss_watchdog > /tmp/stop_loss.log 2>&1 &
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import math
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.core import config

TRADE_LOG_PATH = _PROJECT_ROOT / "data" / "trade_log.json"
CHECK_INTERVAL_SEC = 300       # 5 minutes
HARD_STOP_LOSS_PCT = 0.50      # -50% → unconditional sell
EDGE_INVALIDATION = 0.03       # edge < 3% → sell
ABSOLUTE_TP_ROI = 0.08         # +8% ROI → immediate take-profit
BACKUP_KEY = "/home/darren/share/polymarket/config/.env.txt.backup"


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _load_open_trades() -> list[dict]:
    if not TRADE_LOG_PATH.exists():
        return []
    with open(TRADE_LOG_PATH) as f:
        data = json.load(f)
    return [t for t in data.get("trades", []) if t.get("status") == "open"]


def _save_trade_update(trade: dict) -> None:
    with open(TRADE_LOG_PATH) as f:
        data = json.load(f)
    for i, t in enumerate(data["trades"]):
        if t["trade_id"] == trade["trade_id"]:
            data["trades"][i] = trade
            break
    with open(TRADE_LOG_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _get_best_bid(token_id: str) -> float:
    """Fetch best (highest) bid from CLOB order book."""
    import requests
    try:
        ob = requests.get(
            f"{config.CLOB_API}/book",
            params={"token_id": token_id},
            timeout=15,
        ).json()
        bids = ob.get("bids", [])
        return max((float(b["price"]) for b in bids), default=0.0)
    except Exception:
        return 0.0


def _execute_sell(trade: dict, bid_price: float) -> bool:
    """Place a SELL order at the bid (market-taker style) and update log."""
    order = trade.get("order", {})
    token_id = order.get("token_id", "")
    shares = order.get("shares", 0)
    if not token_id or not shares:
        return False

    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import (
            ApiCreds, OrderArgsV2, PartialCreateOrderOptions, OrderType,
        )

        with open(BACKUP_KEY) as f:
            m = re.search(r"^\s*sk\s*=\s*([0-9a-fA-FxX]+)", f.read(), re.MULTILINE)
        pk = m.group(1).strip()
        pk = pk if pk.startswith("0x") else "0x" + pk

        creds = ApiCreds(
            api_key="f785f79c-3119-1c24-3489-3ac27718b741",
            api_secret="uLbsEVrSw-wTNHC1X4wZ5tQuHzaeiy6xpuJrXAGbFX4=",
            api_passphrase="04949246bc4fe4326d25df889a4271c52299b7bea07a5b38ed8566e7566fb61a",
        )
        client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137, key=pk, creds=creds,
            signature_type=2,
            funder="0x1270215141EA0a2CdA89272722B2ac47DF6751A1",
        )

        # Cancel existing TP order first (free up the shares)
        tp = trade.get("take_profit", {})
        tp_oid = tp.get("order_id", "")
        if tp_oid:
            try:
                client.cancel_order(tp_oid)
                _log(f"  Cancelled TP order {tp_oid[:16]}...")
                time.sleep(2)
            except Exception:
                pass  # TP might have already filled or cancelled

        # Place SELL at bid (taker)
        args = OrderArgsV2(
            token_id=token_id, price=bid_price, size=shares, side="SELL",
        )
        opts = PartialCreateOrderOptions(neg_risk=True, tick_size="0.01")
        resp = client.create_and_post_order(args, opts, order_type=OrderType.GTC)
        oid = resp.get("orderID", "unknown")
        _log(f"  SELL {shares} @ {bid_price:.3f} → {resp.get('status','?')} oid={str(oid)[:16]}...")

        # Update trade log
        cost = order.get("cost_usd", 0)
        proceeds = shares * bid_price
        trade["status"] = "closed_early"
        trade["exit"] = {
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "exit_reason": f"Auto stop-loss: bid {bid_price:.3f} hit threshold",
            "exit_price_avg": bid_price,
            "exit_shares": shares,
            "exit_proceeds_usd": round(proceeds, 2),
            "net_pnl_usd": round(proceeds - cost, 2),
            "exit_order_id": str(oid),
        }
        _save_trade_update(trade)
        return True

    except Exception as e:
        _log(f"  SELL FAILED: {e}")
        return False


def check_once() -> int:
    """Run one check cycle. Returns number of actions triggered."""
    open_trades = _load_open_trades()
    if not open_trades:
        _log("No open positions.")
        return 0

    triggered = 0
    for trade in open_trades:
        tid = trade["trade_id"]
        order = trade.get("order", {})
        edge = trade.get("edge", {})
        token_id = order.get("token_id", "")
        entry = order.get("price", 0)
        shares = order.get("shares", 0)
        cost = order.get("cost_usd", 0)
        model_p = edge.get("model_P_not") or edge.get("model_P_eq") or 0.5
        hard_stop = entry * (1 - HARD_STOP_LOSS_PCT)
        roi_tp = entry * (1 + ABSOLUTE_TP_ROI)

        label = trade.get("market", {}).get("question", "?")[:40]
        bid = _get_best_bid(token_id)

        if bid <= 0:
            _log(f"[{tid}] {label} — no bid, skip")
            continue

        pnl_pct = (bid - entry) / entry * 100 if entry > 0 else 0
        edge_now = model_p - bid

        # Check conditions (priority: ROI TP > hard stop > edge invalidation)
        hit_roi_tp = bid >= roi_tp
        hit_hard = bid <= hard_stop
        hit_edge = (edge_now < EDGE_INVALIDATION) and (pnl_pct < -10)

        if hit_roi_tp:
            _log(f"[{tid}] {label} — 💰 ROI TP bid={bid:.3f} ≥ {roi_tp:.3f} | ROI={pnl_pct:+.1f}%")
            trade["exit"] = {"exit_reason": f"ROI take-profit {pnl_pct:+.1f}%"}
            if _execute_sell(trade, bid):
                triggered += 1
        elif hit_hard:
            _log(f"[{tid}] {label} — 🚨 HARD STOP bid={bid:.3f} ≤ {hard_stop:.3f} | PnL={pnl_pct:+.1f}%")
            trade["exit"] = {"exit_reason": f"hard stop-loss {pnl_pct:+.1f}%"}
            if _execute_sell(trade, bid):
                triggered += 1
        elif hit_edge:
            _log(f"[{tid}] {label} — ⚠️ EDGE GONE bid={bid:.3f} edge={edge_now:+.3f} | PnL={pnl_pct:+.1f}%")
            trade["exit"] = {"exit_reason": f"edge invalidation {pnl_pct:+.1f}%"}
            if _execute_sell(trade, bid):
                triggered += 1
        else:
            _log(f"[{tid}] {label} — ✅ bid={bid:.3f} ROI={pnl_pct:+.1f}% TP@{roi_tp:.3f} SL@{hard_stop:.3f}")

    return triggered


def main():
    _log("=" * 60)
    _log("  STOP-LOSS WATCHDOG — auto monitoring")
    _log(f"  Interval: {CHECK_INTERVAL_SEC}s | Hard SL: -{HARD_STOP_LOSS_PCT*100:.0f}%")
    _log("=" * 60)

    while True:
        try:
            n = check_once()
            if n:
                _log(f"Triggered {n} stop-loss(es) this cycle.")
        except Exception as e:
            _log(f"ERROR in check cycle: {e}")

        _log(f"Sleep {CHECK_INTERVAL_SEC}s...")
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
