#!/usr/bin/env python3
"""
Auto edge scanner — runs continuously, detects GFS update windows,
high-frequency scan when edge likely appears.

Usage:
  nohup python3 -m src.execution.edge_scanner > /tmp/edge_scanner.log 2>&1 &
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import requests
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from src.core import config

SCAN_LOG = Path("/tmp/edge_scanner.log")
ALERT_FILE = Path("/tmp/edge_alerts.txt")

# GFS update times (UTC hours)
GFS_HOURS = [0, 6, 12, 18]
# High-freq scan window: first N minutes after GFS release
SCAN_WINDOW_MINUTES = 60
HIGH_FREQ_INTERVAL = 60      # 60 seconds during window
LOW_FREQ_INTERVAL = 1800     # 30 minutes outside window
MIN_EXEC_EDGE = 0.08         # 8% minimum to alert
MIN_EXEC_EDGE_AUTO = 0.15    # 15% minimum to auto-place

# Cities to scan
CITIES = [
    ('Hong Kong', 22.3, 114.2, 'hong_kong_hong_kong'),
    ('London', 51.5, -0.1, 'london_united_kingdom'),
    ('Amsterdam', 52.37, 4.90, 'amsterdam_netherlands'),
    ('Paris', 48.86, 2.35, 'paris_france'),
    ('Istanbul', 41.01, 28.98, 'istanbul_turkey'),
    ('Manila', 14.60, 120.98, 'manila_philippines'),
    ('Madrid', 40.42, -3.70, 'madrid_spain'),
]


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(SCAN_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _alert(msg: str) -> None:
    """Write alert to file for dashboard to pick up."""
    try:
        with open(ALERT_FILE, "a") as f:
            f.write(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _is_edge_window() -> bool:
    """Check if we're in the high-frequency scan window after GFS release."""
    now = datetime.now(timezone.utc)
    current_hour = now.hour
    current_minute = now.minute
    
    for gfs_hour in GFS_HOURS:
        # GFS data usually available ~30 min after nominal time
        data_available = (gfs_hour * 60 + 30)
        current_total_min = current_hour * 60 + current_minute
        
        # Window: from (gfs_hour + 30 min) to (gfs_hour + 30 min + SCAN_WINDOW)
        window_start = data_available
        window_end = data_available + SCAN_WINDOW_MINUTES
        
        if window_start <= current_total_min < window_end:
            return True
    return False


def _get_best_bid_ask(token_id):
    try:
        ob = requests.get(
            f"{config.CLOB_API}/book",
            params={"token_id": token_id}, timeout=8,
        ).json()
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])
        bid = max((float(b["price"]) for b in bids), default=0)
        ask = min((float(a["price"]) for a in asks), default=1)
        depth = sum(float(b["size"]) for b in bids[:3])
        return bid, ask, depth
    except Exception:
        return 0, 1, 0


def scan_once() -> list[dict]:
    """Scan all cities, return list of tradeable signals."""
    try:
        sys.path.insert(0, str(_PROJECT_ROOT))
        import src.data.gfs_prediction as gp
        gp._calib_json_cache = None
        from src.data.gfs_prediction import GFSPredictionSource
        from src.data.prediction_interface import MarketContext

        source = GFSPredictionSource(mode="live")
        signals = []

        for city, lat, lon, loc in CITIES:
            cslug = city.lower().replace(" ", "-")
            for offset in [1, 2]:
                d = date.today() + timedelta(days=offset)
                td = d.isoformat()
                mn = d.strftime("%B").lower()
                base = f"highest-temperature-in-{cslug}-on-{mn}-{d.day}-2026"

                ctx = MarketContext(
                    market_id="x", question="x",
                    outcomes=["Yes", "No"], outcome_prices=[0.5, 0.5],
                    city=city, target_date=td,
                    variable="temperature_2m_max", rule="eq",
                    threshold_value=30.0, latitude=lat, longitude=lon,
                    market_type="temp_above", location_id=loc,
                )
                p = source.predict(ctx)
                if not p:
                    continue
                e = p.extra
                gc = e.get("gfs_corrected", 0)
                sg = e.get("calib_sigma", 1.2)

                for t, sf, lb in [
                    (21, "21c", "21"), (22, "22c", "22"), (23, "23c", "23"),
                    (24, "24c", "24"), (25, "25c", "25"), (26, "26c", "26"),
                    (27, "27c", "27"), (28, "28c", "28"), (29, "29c", "29"),
                    (30, "30c", "30"), (31, "31c", "31"), (32, "32c", "32"),
                ]:
                    slug = f"{base}-{sf}"
                    r = requests.get(
                        "https://gamma-api.polymarket.com/markets",
                        params={"slug": slug}, timeout=5,
                    )
                    data = r.json()
                    if not data or not isinstance(data, list) or len(data) == 0:
                        continue
                    m = data[0]
                    if m.get("closed"):
                        continue
                    cids = m.get("clobTokenIds", "[]")
                    if isinstance(cids, str):
                        cids = json.loads(cids)
                    pr = m.get("outcomePrices", "[]")
                    if isinstance(pr, str):
                        pr = json.loads(pr)
                    if not pr:
                        continue
                    mk = float(pr[0])

                    z1 = (t - gc) / sg
                    z2 = (t + 1 - gc) / sg
                    mp = 0.5 * (math.erf(z2 / math.sqrt(2)) - math.erf(z1 / math.sqrt(2)))
                    edge = mp - mk
                    dr = abs(gc - t) / sg

                    nt = cids[1] if len(cids) > 1 else ""
                    nb, na, bd = _get_best_bid_ask(nt) if nt else (0, 1, 0)

                    dd = (1 - mp) - na if edge < 0 else mp - na

                    # Rule A
                    ok = False
                    if dr >= 1.5 and abs(edge) >= 0.08:
                        ok = True
                    elif dr >= 1.0 and abs(edge) >= 0.15:
                        ok = True
                    elif dr >= 0.5 and abs(edge) >= 0.25:
                        ok = True

                    if ok and dd >= 0.05:  # Only positive exec edge (we trade BUY_NO)
                        signals.append({
                            "exec_edge": dd, "city": city, "date": td,
                            "temp": f"{lb}°C", "edge": edge, "dist": dr,
                            "no_ask": na, "depth": bd, "token": nt,
                            "model_p": mp, "market_p": mk,
                            "gfs": gc, "sigma": sg,
                        })

        source.close()
        return signals
    except Exception as e:
        _log(f"Scan error: {e}")
        return []


def main():
    _log("=" * 60)
    _log("  EDGE SCANNER — auto monitoring")
    _log(f"  Cities: {len(CITIES)} | High-freq: {HIGH_FREQ_INTERVAL}s | Low-freq: {LOW_FREQ_INTERVAL}s")
    _log(f"  Alert threshold: {MIN_EXEC_EDGE*100:.0f}% | Auto-place: {MIN_EXEC_EDGE_AUTO*100:.0f}%")
    _log("=" * 60)

    cycle = 0
    while True:
        cycle += 1
        in_window = _is_edge_window()

        try:
            signals = scan_once()

            if signals:
                top = sorted(signals, key=lambda x: -abs(x["exec_edge"]))
                count = sum(1 for s in top if s["exec_edge"] >= MIN_EXEC_EDGE)
                
                if count > 0:
                    msg = f"🔔 {count} signals found (top: {top[0]['city']} {top[0]['date']} {top[0]['temp']} exec={top[0]['exec_edge']:+.1%})"
                    _log(msg)
                    _alert(msg)

                    for s in top:
                        if s["exec_edge"] >= MIN_EXEC_EDGE:
                            _log(f"  ⚡ {s['city']:>10s} {s['date']} {s['temp']:>3s} exec={s['exec_edge']:+.1%} edge={s['edge']:+.1%} {s['dist']:.1f}σ NoAsk={s['no_ask']:.2f} d={s['depth']:.0f} GFS={s['gfs']:.1f} tok={s['token'][:20]}...")
                            
                            # Auto-place if very strong
                            if s["exec_edge"] >= MIN_EXEC_EDGE_AUTO:
                                _log(f"  🤖 AUTO-PLACE: {s['city']} {s['date']} {s['temp']}")
                                # TODO: integrate auto-order placement
                else:
                    _log(f"Scanned {len(signals)} items, none above threshold")
            else:
                _log(f"No signals found")

        except Exception as e:
            _log(f"Cycle error: {e}")

        # Determine sleep time
        if in_window:
            sleep_sec = HIGH_FREQ_INTERVAL
        else:
            sleep_sec = LOW_FREQ_INTERVAL

        _log(f"Sleep {sleep_sec}s (window={in_window})...")
        time.sleep(sleep_sec)


if __name__ == "__main__":
    main()
