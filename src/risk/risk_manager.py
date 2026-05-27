"""
Risk Manager — unified gate KEEP in front of every single trade.

This module was created after -$14 Seoul weather arbitrage loss caused by
a module bypassing risk checks. ALL trades must pass through this module.

Rule index (all rules enforced on every trade):
  Rule 1: Max daily loss check (real mode only)
  Rule 2: Max positions check
  Rule 3: Single position size cap
  Rule 4: Total exposure cap
  Rule 5: Minimum edge > 5% (absolute |p_llm - p_market|)
  Rule 6: Edge sanity cap — if edge > 40%, treat as model error, NOT trade
  Rule 7: Weather market gate — config-driven (ALLOW_WEATHER_MARKETS env var)
  Rule 8: Stopped-out market cooldown (24h before re-entry)
  Rule 9: Min trade size
  Rule 10: Category whitelist (sports, politics, crypto, weather — config-dependent)
"""
import json
import os
from datetime import datetime, timedelta
from typing import Dict, Optional, List

import config

# ── Constants ─────────────────────────────────────────────────

# Permanently blocked categories/outcome strings
WEATHER_BLOCKLIST = [
    "weather", "temperature", "rainfall", "snowfall",
    "precipitation", "storm", "hurricane", "typhoon",
    "wind speed", "humidity", "fog", "frost",
    "sports-weather", "game weather",
]

# Minimum edge threshold (absolute |p_llm - p_market|)
MIN_EDGE = 0.05  # 5%

# Edge sanity — if |edge| > 40%, suspect model error
MAX_SANE_EDGE = 0.40

# Category whitelist for event-scanner style trading.
# If config.ALLOW_WEATHER_MARKETS is True, weather categories are also allowed.
ALLOWED_CATEGORIES = ["sports", "politics", "crypto"]
if config.ALLOW_WEATHER_MARKETS:
    ALLOWED_CATEGORIES.extend(["weather", "temperature", "climate"])


def is_blocked_category(category: str) -> bool:
    """Check if a category/tag is permanently blocked."""
    cat_lower = category.lower()
    for blocked in WEATHER_BLOCKLIST:
        if blocked in cat_lower:
            return True
    return False


def is_blocked_question(question: str) -> bool:
    """Check if a market question contains weather or other blocked content."""
    q_lower = question.lower()
    for blocked in WEATHER_BLOCKLIST:
        if blocked in q_lower:
            return True
    return False


def check_edge(edge: float) -> Dict:
    """
    Rule 5 + Rule 6: Validate edge.
    Returns {'allowed': bool, 'reason': str}
    """
    abs_edge = abs(edge)
    if abs_edge < MIN_EDGE:
        return {
            "allowed": False,
            "reason": f"Edge too small: {edge:+.3f} (need |edge| >= {MIN_EDGE:.0%})",
        }
    if abs_edge > MAX_SANE_EDGE:
        return {
            "allowed": False,
            "reason": f"Edge suspiciously large: {edge:+.3f} (|edge| > {MAX_SANE_EDGE:.0%}, suspected model error)",
        }
    return {"allowed": True, "reason": f"Edge {edge:+.3f} within acceptable range"}


class RiskManager:
    """
    Central risk gate. Every trade decision must call risk_manager.approve()
    before execution. If any rule fails, the trade is rejected.
    """

    def __init__(self, portfolio=None, trade_executor=None):
        self.portfolio = portfolio
        self.trade_executor = trade_executor
        self.daily_loss_usd = 0.0
        self.daily_loss_reset_date = datetime.utcnow().date()
        self.log: List[Dict] = []
        self._load_daily_loss()

    # ── persistence ───────────────────────────────────────────

    def _daily_loss_file(self) -> str:
        return os.path.join(config.DATA_DIR, "daily_loss.json")

    def _load_daily_loss(self):
        path = self._daily_loss_file()
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                if data.get("date") == datetime.utcnow().date().isoformat():
                    self.daily_loss_usd = data.get("loss", 0.0)
            except Exception:
                pass

    def _save_daily_loss(self):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(self._daily_loss_file(), "w") as f:
            json.dump({
                "date": datetime.utcnow().date().isoformat(),
                "loss": self.daily_loss_usd,
            }, f)

    def record_loss(self, loss_usd: float):
        """Called externally when a trade realises a loss."""
        self.daily_loss_usd += max(0, loss_usd)
        self._save_daily_loss()

    # ── approval gate ─────────────────────────────────────────

    def approve(
        self,
        decision_type: str,  # "BUY_YES" / "BUY_NO" / "SELL" / "HOLD"
        market_id: str,
        market_question: str,
        outcome: str,
        amount_usd: float,
        edge: float,
        confidence: float,
        category: str = "Unknown",
        is_new_position: bool = True,
    ) -> Dict:
        """
        Evaluate a trading decision against all rules.
        Returns {"approved": True/False, "reason": str, "rule": str}
        """
        checks = []

        # ── Rule 1: Daily loss limit (real mode only) ──
        if not config.PAPER_TRADING:
            self._reset_daily_loss_if_new_day()
            if self.daily_loss_usd >= config.MAX_DAILY_LOSS:
                checks.append({
                    "rule": "Rule 1",
                    "passed": False,
                    "reason": f"Daily loss limit (${config.MAX_DAILY_LOSS}) reached or exceeded",
                })

        # Rule 7: Weather market gate (config-driven)
        if not config.ALLOW_WEATHER_MARKETS:
            if is_blocked_category(category) or is_blocked_question(market_question):
                checks.append({
                    "rule": "Rule 7",
                    "passed": False,
                    "reason": "Weather / blocked market — disabled in config (ALLOW_WEATHER_MARKETS=false)",
                })

        # ── Rule 10: Category whitelist (new positions only) ──
        if is_new_position and decision_type.startswith("BUY"):
            cat_lower = category.lower()
            allowed = any(a in cat_lower for a in ALLOWED_CATEGORIES)
            if not allowed and cat_lower not in ["unknown", ""]:
                checks.append({
                    "rule": "Rule 10",
                    "passed": False,
                    "reason": f"Category '{category}' not in allowed: {ALLOWED_CATEGORIES}",
                })

        # ── Rule 2: Max positions ──
        if self.portfolio and is_new_position and decision_type.startswith("BUY"):
            current_positions = len(self.portfolio.portfolio.positions)
            if current_positions >= config.MAX_POSITIONS:
                checks.append({
                    "rule": "Rule 2",
                    "passed": False,
                    "reason": f"Max positions ({config.MAX_POSITIONS}) already open",
                })

        # ── Rule 3: Single position size cap ──
        if decision_type.startswith("BUY") and amount_usd > config.MAX_POSITION_SIZE:
            checks.append({
                "rule": "Rule 3",
                "passed": False,
                "reason": f"Amount ${amount_usd:.2f} exceeds max position ${config.MAX_POSITION_SIZE}",
            })

        # ── Rule 4: Total exposure cap ──
        if self.portfolio and decision_type.startswith("BUY"):
            current_exposure = self.portfolio.portfolio.total_exposure
            new_total = current_exposure + amount_usd
            max_exposure = max(float(config.MAX_TOTAL_EXPOSURE), float(self.portfolio.total_value()))
            if new_total > max_exposure:
                checks.append({
                    "rule": "Rule 4",
                    "passed": False,
                    "reason": f"Total exposure ${new_total:.2f} would exceed ${max_exposure:.2f}",
                })

        # ── Rule 5 + Rule 6: Edge validation ──
        if decision_type.startswith("BUY"):
            edge_check = check_edge(edge)
            if not edge_check["allowed"]:
                checks.append({"rule": "Rule 5/6", "passed": False, "reason": edge_check["reason"]})

        # ── Rule 8: Stopped-out cooldown ──
        if self.trade_executor and decision_type.startswith("BUY") and is_new_position:
            if not self.trade_executor.is_market_cooled_down(market_id):
                checks.append({
                    "rule": "Rule 8",
                    "passed": False,
                    "reason": f"Market {market_id} in stop-loss cooldown",
                })

        # ── Rule 9: Min trade size ──
        if decision_type.startswith("BUY") and amount_usd < config.MIN_TRADE_SIZE:
            checks.append({
                "rule": "Rule 9",
                "passed": False,
                "reason": f"Amount ${amount_usd:.2f} below minimum ${config.MIN_TRADE_SIZE}",
            })

        # ── Evaluate ──
        failures = [c for c in checks if not c["passed"]]
        if failures:
            reasons = "; ".join(f"{c['rule']}: {c['reason']}" for c in failures)
            self._log_decision(market_id, decision_type, False, reasons)
            return {"approved": False, "reason": reasons, "failures": failures}

        self._log_decision(market_id, decision_type, True, "All rules passed")
        return {"approved": True, "reason": "All rules passed"}

    # ── helpers ───────────────────────────────────────────────

    def _reset_daily_loss_if_new_day(self):
        today = datetime.utcnow().date()
        if today != self.daily_loss_reset_date:
            self.daily_loss_usd = 0.0
            self.daily_loss_reset_date = today
            self._save_daily_loss()

    def _log_decision(self, market_id: str, decision_type: str, approved: bool, reason: str):
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "market_id": market_id,
            "decision": decision_type,
            "approved": approved,
            "reason": reason,
        }
        self.log.append(entry)
        # Keep log manageable
        if len(self.log) > 1000:
            self.log = self.log[-500:]

    def summary(self) -> Dict:
        """Return risk manager status summary."""
        return {
            "daily_loss_usd": self.daily_loss_usd,
            "daily_loss_limit": config.MAX_DAILY_LOSS,
            "paused": self.daily_loss_usd >= config.MAX_DAILY_LOSS,
            "checks_today": len(self.log),
            "recent_rejections": [
                e for e in self.log[-20:] if not e["approved"]
            ],
        }
