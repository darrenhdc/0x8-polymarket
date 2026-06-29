#!/usr/bin/env python3
"""
Auto stop-loss watchdog v2 — resilient, with heartbeat and timeout protection.

Enhancements over v1:
  - Signal-based per-call timeout (kills hanging API requests)
  - Heartbeat file (external monitor can detect stuck process)
  - Short retry on error (30s, not 5 min)
  - Fixed cancel API (cancel_orders, not cancel_order)
  - Separated price-check from sell execution (sell won't block next cycle)

Usage:
  setsid python3 -m src.execution.stop_loss_watchdog > /tmp/stop_loss.log 2>&1 &
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import math
import signal
import traceback
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.core import config

TRADE_LOG_PATH = _PROJECT_ROOT / "data" / "trade_log.json"
HEARTBEAT_FILE = Path("/tmp/watchdog_heartbeat")
CHECK_INTERVAL_SEC = 300       # 5 minutes (normal)
RETRY_INTERVAL_SEC = 30        # 30 seconds (on error)
API_TIMEOUT_SEC = 10           # kill API call after 10s
HARD_STOP_LOSS_PCT = 0.20      # -20% → unconditional sell
EDGE_INVALIDATION = 0.03       # edge < 3% → sell
ABSOLUTE_TP_ROI = 0.08         # +8% ROI → immediate take-profit
BACKUP_KEY = "/home/darren/share/polymarket/config/.env.txt.backup"


# ── Heartbeat ────────────────────────────────────────────────────────────

def _write_heartbeat(status: str = "ok") -> None:
    """Write heartbeat timestamp so external monitors can detect stuck process."""
    try:
        HEARTBEAT_FILE.write_text(
            json.dumps({"ts": time.time(), "status": status})
        )
    except Exception:
        pass


def _heartbeat_age() -> float:
    """Return seconds since last heartbeat, or 9999 if no heartbeat."""
    try:
        data = json.loads(HEARTBEAT_FILE.read_text())
        return time.time() - data.get("ts", 0)
    except Exception:
        return 9999.0


# ── Timeout-protected API calls ──────────────────────────────────────────

class _TimeoutError(Exception):
    pass


def _timeout_handler(signum, frame):
    raise _TimeoutError("API call timed out")


def _api_call_with_timeout(func, *args, **kwargs):
    """Run a function with signal-based timeout. Kills hanging requests."""
    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(API_TIMEOUT_SEC)
    try:
        result = func(*args, **kwargs)
        return result
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


# ── Logging ──────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    # Also append to heartbeat log
    try:
        with open("/tmp/stop_loss_detail.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Trade log helpers ────────────────────────────────────────────────────

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


# ── Price fetching ───────────────────────────────────────────────────────

def _get_best_bid(token_id: str) -> float:
    """Fetch best bid with hard timeout protection."""
    import requests

    def _fetch():
        ob = requests.get(
            f"{config.CLOB_API}/book",
            params={"token_id": token_id},
            timeout=8,
        ).json()
        bids = ob.get("bids", [])
        return max((float(b["price"]) for b in bids), default=0.0)

    try:
        return _api_call_with_timeout(_fetch)
    except _TimeoutError:
        _log(f"  ⚠️ Price fetch timed out for {token_id[:16]}...")
        return -1.0  # sentinel: timeout, don't act
    except Exception as e:
        _log(f"  ⚠️ Price fetch error: {e}")
        return -1.0


# ── Sell execution ───────────────────────────────────────────────────────

_client_cache = None


def _get_client():
    """Create ClobClient (cached)."""
    global _client_cache
    if _client_cache is not None:
        return _client_cache

    with open(BACKUP_KEY) as f:
        m = re.search(r"^\s*sk\s*=\s*([0-9a-fA-FxX]+)", f.read(), re.MULTILINE)
    pk = m.group(1).strip()
    pk = pk if pk.startswith("0x") else "0x" + pk

    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds

    creds = ApiCreds(
        api_key="f785f79c-3119-1c24-3489-3ac27718b741",
        api_secret="uLbsEVrSw-wTNHC1X4wZ5tQuHzaeiy6xpuJrXAGbFX4=",
        api_passphrase="04949246bc4fe4326d25df889a4271c52299b7bea07a5b38ed8566e7566fb61a",
    )
    _client_cache = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137, key=pk, creds=creds,
        signature_type=2,
        funder="0x1270215141EA0a2CdA89272722B2ac47DF6751A1",
    )
    return _client_cache


def _execute_sell(trade: dict, bid_price: float) -> bool:
    """Place a SELL order and update log. Returns True on success."""
    order = trade.get("order", {})
    token_id = order.get("token_id", "")
    shares = order.get("shares", 0)
    if not token_id or not shares:
        return False

    def _do_sell():
        from py_clob_client_v2.clob_types import (
            OrderArgsV2, PartialCreateOrderOptions, OrderType,
        )

        client = _get_client()
        opts = PartialCreateOrderOptions(neg_risk=True, tick_size="0.01")

        # Cancel existing TP orders for this token
        try:
            open_orders = client.get_open_orders()
            for o in open_orders:
                if token_id in o.get("asset_id", ""):
                    client.cancel_orders([o["id"]])
                    _log(f"  Cancelled TP {o['id'][:16]}...")
                    time.sleep(1)
        except Exception as e:
            _log(f"  Cancel TP (non-critical): {e}")

        # Place SELL
        args = OrderArgsV2(
            token_id=token_id, price=bid_price, size=shares, side="SELL",
        )
        resp = client.create_and_post_order(args, opts, order_type=OrderType.GTC)
        oid = resp.get("orderID", "unknown")
        status = resp.get("status", "?")
        _log(f"  SELL {shares} @ {bid_price:.3f} → {status} oid={str(oid)[:16]}...")

        # Update trade log
        cost = order.get("cost_usd", 0)
        proceeds = shares * bid_price
        reason = trade.get("exit", {}).get("exit_reason", "auto")
        trade["status"] = "closed_early"
        trade["exit"] = {
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "exit_reason": reason,
            "exit_price_avg": bid_price,
            "exit_shares": shares,
            "exit_proceeds_usd": round(proceeds, 2),
            "net_pnl_usd": round(proceeds - cost, 2),
            "exit_order_id": str(oid),
        }
        _save_trade_update(trade)
        return True

    try:
        return _api_call_with_timeout(_do_sell)
    except _TimeoutError:
        _log(f"  🚨 SELL TIMED OUT — will retry next cycle")
        return False
    except Exception as e:
        _log(f"  🚨 SELL FAILED: {e}")
        _log(f"  {traceback.format_exc()[-200:]}")
        return False


# ── Check cycle ──────────────────────────────────────────────────────────

def check_once() -> int:
    """Run one check cycle. Returns number of actions triggered."""
    open_trades = _load_open_trades()
    if not open_trades:
        return 0

    triggered = 0
    for trade in open_trades:
        tid = trade["trade_id"]
        order = trade.get("order", {})
        edge = trade.get("edge", {})
        token_id = order.get("token_id", "")
        entry = order.get("price", 0)
        shares = order.get("shares", 0)
        model_p = edge.get("model_P_not") or edge.get("model_P_eq") or 0.5
        hard_stop = entry * (1 - HARD_STOP_LOSS_PCT)
        roi_tp = entry * (1 + ABSOLUTE_TP_ROI)

        label = trade.get("market", {}).get("question", "?")[:40]

        bid = _get_best_bid(token_id)

        if bid < 0:
            # Timeout or error — skip this one, retry next cycle
            _log(f"[{tid}] {label} — ⚠️ price fetch failed, skip")
            continue

        if bid == 0:
            _log(f"[{tid}] {label} — no bid (market may be settling)")
            continue

        pnl_pct = (bid - entry) / entry * 100 if entry > 0 else 0
        edge_now = model_p - bid

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


# ── Main loop ────────────────────────────────────────────────────────────

def main():
    _log("=" * 60)
    _log("  STOP-LOSS WATCHDOG v2 — resilient")
    _log(f"  Interval: {CHECK_INTERVAL_SEC}s | Retry: {RETRY_INTERVAL_SEC}s")
    _log(f"  Hard SL: -{HARD_STOP_LOSS_PCT*100:.0f}% | ROI TP: +{ABSOLUTE_TP_ROI*100:.0f}%")
    _log(f"  API timeout: {API_TIMEOUT_SEC}s | Heartbeat: {HEARTBEAT_FILE}")
    _log("=" * 60)

    cycle = 0
    while True:
        cycle += 1
        try:
            _write_heartbeat(f"cycle_{cycle}")
            n = check_once()
            if n:
                _log(f"Triggered {n} action(s) this cycle.")
            sleep_sec = CHECK_INTERVAL_SEC
        except Exception as e:
            _log(f"❌ CYCLE ERROR: {e}")
            _log(traceback.format_exc()[-300:])
            _write_heartbeat(f"error_cycle_{cycle}")
            sleep_sec = RETRY_INTERVAL_SEC  # retry sooner on error

        _log(f"Sleep {sleep_sec}s...")
        try:
            time.sleep(sleep_sec)
        except KeyboardInterrupt:
            _log("Shutdown.")
            break


if __name__ == "__main__":
    main()
