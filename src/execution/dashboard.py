#!/usr/bin/env python3
"""
Live position dashboard — visual convergence tracker.
Updates every 60 seconds. Shows where current price sits on the edge scale.

Usage:
  python3 -m src.execution.dashboard
  python3 -m src.execution.dashboard --once   # single snapshot
"""
from __future__ import annotations

import json
import os
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


def _load_open_trades():
    with open(TRADE_LOG_PATH) as f:
        data = json.load(f)
    return [t for t in data.get("trades", []) if t.get("status") == "open"]


def _get_best_bid_ask(token_id):
    import requests
    try:
        ob = requests.get(
            f"{config.CLOB_API}/book",
            params={"token_id": token_id},
            timeout=10,
        ).json()
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        bid = max((float(b["price"]) for b in bids), default=0.0)
        ask = min((float(a["price"]) for a in asks), default=1.0)
        return bid, ask
    except Exception:
        return 0.0, 1.0


def _convergence_bar(entry, model_p, current, width=40):
    """Build a visual bar showing where current price sits on the edge scale."""
    gap = model_p - entry
    if gap <= 0:
        return "[invalid: gap<=0]"

    # Positions on the bar (0 to width)
    # entry = position 0, model_p = position width
    pct = (current - entry) / gap
    pos = max(0, min(int(pct * width), width))

    # Markers for key levels
    tp50_pos = int(0.50 * width)
    tp80_pos = int(0.80 * width)

    bar = list("·" * (width + 1))

    # Fill from entry to current
    if pct >= 0:
        for i in range(min(pos, width + 1)):
            bar[i] = "■"
    else:
        # Price went below entry — show red zone
        for i in range(width + 1):
            bar[i] = "─"

    # Place markers
    bar[0] = "E"           # Entry
    bar[tp50_pos] = "5"    # 50% TP
    bar[tp80_pos] = "8"    # 80% TP (old)
    bar[width] = "M"       # Model

    # Current position marker
    if 0 <= pos <= width:
        bar[pos] = "◆"

    return "".join(bar)


def render_once():
    trades = _load_open_trades()
    if not trades:
        print("  No open positions.")
        return

    now = datetime.now(timezone.utc)
    print(f"\033[2J\033[H")  # clear screen
    print(f"{'='*90}")
    print(f"  📊 LIVE POSITION DASHBOARD    {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*90}")
    print()
    print(f"  Legend: E=入场  5=50%止盈  8=80%止盈  M=模型概率  ◆=当前价  ■=已收敛")
    print(f"  Scale:  E────5────────8────────M   (entry → model probability)")
    print()

    for t in trades:
        tid = t["trade_id"]
        o = t.get("order", {})
        e = t.get("edge", {})
        tp = t.get("take_profit", {})

        token = o.get("token_id", "")
        entry = o.get("price", 0)
        shares = o.get("shares", 0)
        cost = o.get("cost_usd", 0)
        model_p = e.get("model_P_not") or e.get("model_P_eq") or 0.5
        tp_price = tp.get("price", 0)
        tp_status = tp.get("status", "?")
        question = t.get("market", {}).get("question", "?")[:55]
        city = t.get("market", {}).get("city", "?")
        target_date = t.get("market", {}).get("date", "?")

        # ROI take-profit price (8% above entry)
        roi_tp_price = entry * (1 + config.ABSOLUTE_TP_ROI)

        bid, ask = _get_best_bid_ask(token)
        mid = (bid + ask) / 2

        pnl_per = (bid - entry) * shares
        pnl_pct = (bid - entry) / entry * 100 if entry else 0

        gap = model_p - entry
        conv_pct = (bid - entry) / gap * 100 if gap > 0 else 0

        # Convergence ratio (where convergence TP sits)
        tp_ratio = (tp_price - entry) / gap if gap > 0 else 0
        # Which TP triggers first?
        effective_tp = min(tp_price, roi_tp_price)

        # Color coding
        if conv_pct >= tp_ratio * 100:
            color = "\033[92m"  # green — TP should trigger
            status_icon = "✅"
        elif conv_pct >= 0:
            color = "\033[93m"  # yellow — converging
            status_icon = "⏳"
        else:
            color = "\033[91m"  # red — adverse
            status_icon = "❌"

        reset = "\033[0m"

        # Build visual bar
        bar = _convergence_bar(entry, model_p, bid)

        # PnL color
        pnl_color = "\033[92m" if pnl_per >= 0 else "\033[91m"

        print(f"  ┌─ {color}[{tid}]{reset} {question}")
        print(f"  │  {city} | {target_date} | BUY_NO {shares}sh")
        print(f"  │")
        print(f"  │  入场: {entry:.3f}  │  bid: {bid:.3f}  │  模型: {model_p:.3f}")
        print(f"  │  收敛TP: {tp_price:.3f} ({tp_status})  │  ROI TP: {roi_tp_price:.3f} (+{config.ABSOLUTE_TP_ROI*100:.0f}%)  │  生效: {effective_tp:.3f}")
        print(f"  │  PnL: {pnl_color}" + f"${pnl_per:+.2f} ({pnl_pct:+.1f}%)" + f"{reset}  │  收敛: {conv_pct:+.1f}%  {status_icon}")
        print(f"  │")
        print(f"  │  {entry:.2f} [{bar}] {model_p:.2f}")
        print(f"  │       ↑Entry   ROI{int(config.ABSOLUTE_TP_ROI*100)}%↑  50%↑         80%↑     ↑Model")
        if bid >= effective_tp:
            print(f"  │  {color}⚡ 止盈触发! bid {bid:.3f} >= {effective_tp:.3f}{reset}")
        elif bid > 0:
            to_tp = effective_tp - bid
            print(f"  │  距止盈: {to_tp:+.3f}")
        print(f"  └{'─'*80}")
        print()

    # Summary line
    total_val = sum(
        _get_best_bid_ask(t["order"]["token_id"])[0] * t["order"]["shares"]
        for t in trades
    )
    total_cost = sum(t["order"]["cost_usd"] for t in trades)
    total_pnl = total_val - total_cost
    print(f"  持仓总值: ${total_val:.2f}  │  成本: ${total_cost:.2f}  │  浮盈: ${total_pnl:+.2f}")
    print(f"  更新时间: {now.strftime('%H:%M:%S')}  │  Ctrl+C 退出")
    print()


def main():
    import argparse
    p = argparse.ArgumentParser(description="Live position dashboard")
    p.add_argument("--once", action="store_true", help="Single snapshot, no loop")
    p.add_argument("--interval", type=int, default=60, help="Refresh interval (seconds)")
    args = p.parse_args()

    if args.once:
        render_once()
        return

    try:
        while True:
            render_once()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n退出。")


if __name__ == "__main__":
    main()
