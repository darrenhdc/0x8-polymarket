"""
Polymarket Trading System Dashboard
Real-time web interface for monitoring
"""
from flask import Flask, render_template, jsonify
import json
import os
from datetime import datetime, timedelta
from src.core import config

app = Flask(__name__)

DATA_DIR = config.DATA_DIR


def load_json(filename):
    """Load JSON file safely"""
    filepath = os.path.join(DATA_DIR, filename)
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except:
        return {}


def get_position_details():
    """Get detailed position info with current prices"""
    portfolio = load_json("portfolio.json")
    positions = portfolio.get("positions", {})

    details = []
    for market_id, pos in positions.items():
        entry = pos.get("avg_price", 0)
        current = pos.get("current_price", entry)
        pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0

        # Determine stop loss threshold based on entry price
        if entry < 0.15:
            stop_loss = 0.10
        else:
            stop_loss = 0.15

        stop_price = entry * (1 - stop_loss)
        buffer = ((current - stop_price) / entry * 100) if entry > 0 else 0

        details.append({
            "market_id": market_id,
            "question": pos.get("market_question", ""),
            "outcome": pos.get("outcome", ""),
            "tokens": pos.get("tokens", 0),
            "entry_price": entry,
            "current_price": current,
            "cost_usd": pos.get("cost_usd", 0),
            "current_value": pos.get("tokens", 0) * current,
            "pnl_usd": (pos.get("tokens", 0) * current) - pos.get("cost_usd", 0),
            "pnl_pct": pnl_pct,
            "stop_loss_pct": stop_loss * 100,
            "stop_price": stop_price,
            "buffer_pct": buffer,
            "opened_at": pos.get("opened_at", ""),
            "last_updated": pos.get("last_updated", "")
        })

    return sorted(details, key=lambda x: x["buffer_pct"])


@app.route('/')
def dashboard():
    """Main dashboard page"""
    return render_template('dashboard.html')


@app.route('/api/status')
def api_status():
    """API endpoint for real-time data"""
    portfolio = load_json("portfolio.json")
    trades_all = load_json("trades.json")
    stopped_out = load_json("stopped_out.json")

    if not isinstance(trades_all, list):
        trades_all = []

    # In real mode, only show real trades to avoid legacy paper-history noise
    if not config.PAPER_TRADING:
        trades = [t for t in trades_all if t.get("mode") == "real"]
    else:
        trades = trades_all

    cash = portfolio.get("cash", 0)
    initial = portfolio.get("initial_capital", config.INITIAL_CAPITAL)

    # Calculate position value
    positions = portfolio.get("positions", {})
    position_value = sum(
        p.get("tokens", 0) * p.get("current_price", p.get("avg_price", 0))
        for p in positions.values()
    )

    total_value = cash + position_value
    total_pnl = total_value - initial
    total_pnl_pct = (total_pnl / initial * 100) if initial > 0 else 0

    # Calculate exposure
    exposure = sum(p.get("cost_usd", 0) for p in positions.values())

    # Trade stats
    buy_trades = [t for t in trades if t.get("action") == "BUY"]
    sell_trades = [t for t in trades if t.get("action") == "SELL"]

    # Recent trades: prefer last 7 days to avoid very old timestamps in UI
    cutoff = datetime.utcnow() - timedelta(days=7)
    recent_trades = []
    for t in trades:
        try:
            ts = datetime.fromisoformat(str(t.get("timestamp", "")).replace("Z", "+00:00").replace("+00:00", ""))
            if ts >= cutoff:
                recent_trades.append(t)
        except Exception:
            continue
    if not recent_trades:
        recent_trades = trades[-10:] if trades else []

    # Winning trades (sells with profit)
    winning = len([t for t in sell_trades if t.get("cost_usd", 0) > 0])

    # Cooldown info
    cooldowns = []
    now = datetime.utcnow()
    for market_id, timestamp in stopped_out.items():
        try:
            stopped_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            expires = stopped_time.timestamp() + 24 * 3600
            remaining = expires - now.timestamp()
            if remaining > 0:
                cooldowns.append({
                    "market_id": market_id,
                    "remaining_hours": round(remaining / 3600, 1)
                })
        except:
            pass

    max_exposure = max(float(config.MAX_TOTAL_EXPOSURE), float(total_value))

    return jsonify({
        "timestamp": datetime.utcnow().isoformat(),
        "portfolio": {
            "cash": round(cash, 2),
            "initial_capital": initial,
            "position_value": round(position_value, 2),
            "total_value": round(total_value, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "exposure": round(exposure, 2),
            "max_exposure": round(max_exposure, 2),
            "positions_count": len(positions)
        },
        "positions": get_position_details(),
        "trades": {
            "total": len(trades),
            "buys": len(buy_trades),
            "sells": len(sell_trades),
            "winning": winning,
            "recent": recent_trades[-10:] if recent_trades else []
        },
        "cooldowns": cooldowns
    })


if __name__ == '__main__':
    os.makedirs('templates', exist_ok=True)
    app.run(host='0.0.0.0', port=5001, debug=True)
