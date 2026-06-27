"""Backtest GFS weather probabilities against Polymarket odds.

Two public backtest entry-points:

  run()          — original hardcoded-GFS path, unchanged for backward compat.
  run_standard() — pluggable path: accepts any PredictionSource, uses EdgeComposer.
                   Defaults to GFSPredictionSource(mode="historical") when no
                   prediction_source is supplied to __init__.

calibrate()      — standalone rolling bias/sigma computation; used by cli.py calibrate.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from .database import connect_gfs, connect_markets, init_gfs_db, init_weather_db
from .geocoding import normalize_location_id
from .gfs_history import MARKET_VARIABLES
from .prediction_interface import MarketContext, PredictionSource


DEFAULT_SIGMA = {
    "temp_above": 1.20,   # HKO-calibrated T+0 residual std (791 pairs, 2024-2026)
    "precip": 2.0,
    "snow": 2.0,
}

# GFS cold bias per location_id (GFS forecast - HKO actual, negative = GFS runs cold).
# Correction is *added* to the GFS forecast before computing probabilities.
# Calibrated against HKO Observatory HQ (Polymarket settlement source),
# 791+ pairs 2024-2026. See data/calibration.json for per-lead breakdown.
GFS_BIAS_CORRECTION: dict[str, dict[str, float]] = {
    "hong-kong_hong-kong": {"temperature_2m_max": -1.324, "temperature_2m_min": 0.0},
}


@dataclass
class BacktestTrade:
    market_id: str
    question: str
    city: str
    target_date: str
    price_date: str
    days_to_target: int
    market_type: str
    market_rule: str
    threshold_value: float
    threshold_unit: str
    market_yes_price: float
    forecast_value: float
    observed_value: Optional[float]
    model_yes_probability: float
    direction: str
    edge: float
    amount: float
    actual_yes: Optional[bool]
    pnl: float


class WeatherBacktester:
    def __init__(
        self,
        market_db_path=None,
        gfs_db_path=None,
        prediction_source: Optional[PredictionSource] = None,
    ):
        """Create a backtester.

        Args:
            market_db_path: override path for weather_markets.db
            gfs_db_path:    override path for gfs_forecasts.db
            prediction_source: optional PredictionSource for run_standard().
                Defaults to GFSPredictionSource(mode="historical") when None.
        """
        self.market_conn = connect_markets(market_db_path)
        self.gfs_conn = connect_gfs(gfs_db_path)
        init_weather_db(self.market_conn)
        init_gfs_db(self.gfs_conn)
        self._prediction_source = prediction_source   # lazy-init in run_standard()

    def close(self) -> None:
        self.market_conn.close()
        self.gfs_conn.close()
        if self._prediction_source is not None:
            self._prediction_source.close()

    def _get_or_create_prediction_source(self) -> PredictionSource:
        """Return the configured prediction source; create default if not set."""
        if self._prediction_source is None:
            from .gfs_prediction import GFSPredictionSource
            self._prediction_source = GFSPredictionSource(mode="historical")
        return self._prediction_source

    def run(
        self,
        *,
        start_date: str,
        end_date: str,
        city: Optional[str] = None,
        min_edge: float = 0.10,
        amount: float = 5.0,
        max_lead_time_hours: Optional[int] = None,
        min_price: float = 0.0,
    ) -> list[BacktestTrade]:
        trades: list[BacktestTrade] = []
        params: list[str] = [start_date, end_date]
        city_clause = ""
        if city:
            city_clause = " AND lower(m.city) = lower(?)"
            params.append(city)

        rows = self.market_conn.execute(
            """
            SELECT
                m.id, m.question, m.city, m.country, m.market_type,
                m.threshold_value, m.threshold_unit, m.target_date,
                m.resolved_outcome,
                p.timestamp, p.price
            FROM markets m
            JOIN price_history p ON p.market_id = m.id
              WHERE substr(p.timestamp, 1, 10) BETWEEN ? AND ?
              AND m.market_type IN ('temp_above')
              AND m.threshold_value IS NOT NULL
              AND m.target_date IS NOT NULL
              AND m.city IS NOT NULL
              {city_clause}
            ORDER BY p.timestamp, m.id
            """.format(city_clause=city_clause),
            params,
        )

        for row in rows:
            location_id = normalize_location_id(row["city"], row["country"])
            variable = infer_market_variable(row["question"], row["market_type"])
            price_date = row["timestamp"][:10]

            forecast = self._forecast(location_id, row["target_date"], variable)
            if not forecast:
                continue
            if max_lead_time_hours is not None:
                lead = self._lead_hours(price_date, row["target_date"])
                if lead > max_lead_time_hours:
                    continue

            observed = self._observed(location_id, row["target_date"], variable)
            threshold = convert_threshold(row["threshold_value"], row["threshold_unit"], variable)
            rule = infer_market_rule(row["question"], row["market_type"])
            bias = GFS_BIAS_CORRECTION.get(location_id, {}).get(variable, 0.0)
            pred_yes = probability_for_rule(
                forecast_value=float(forecast["value"]) + bias,
                threshold=threshold,
                sigma=DEFAULT_SIGMA.get(row["market_type"], 1.0),
                rule=rule,
            )
            market_yes = min(max(float(row["price"]), 0.001), 0.999)
            if min_price > 0 and (market_yes < min_price or market_yes > 1.0 - min_price):
                continue
            yes_edge = pred_yes - market_yes
            no_edge = (1.0 - pred_yes) - (1.0 - market_yes)

            if abs(yes_edge) < min_edge:
                continue

            if yes_edge > 0:
                direction = "BUY_YES"
                edge = yes_edge
                trade_price = market_yes
            else:
                direction = "BUY_NO"
                edge = no_edge
                trade_price = 1.0 - market_yes

            actual_yes = self._actual_yes(row["resolved_outcome"], observed, threshold, rule)
            pnl = simulate_pnl(direction, trade_price, amount, actual_yes)
            days_to_target = self._lead_hours(price_date, row["target_date"]) // 24
            trades.append(
                BacktestTrade(
                    market_id=row["id"],
                    question=row["question"],
                    city=row["city"],
                    target_date=row["target_date"],
                    price_date=price_date,
                    days_to_target=days_to_target,
                    market_type=row["market_type"],
                    market_rule=rule,
                    threshold_value=threshold,
                    threshold_unit=forecast["unit"],
                    market_yes_price=market_yes,
                    forecast_value=float(forecast["value"]) + bias,
                    observed_value=float(observed["value"]) if observed and observed["value"] is not None else None,
                    model_yes_probability=pred_yes,
                    direction=direction,
                    edge=edge,
                    amount=amount,
                    actual_yes=actual_yes,
                    pnl=pnl,
                )
            )
        return trades

    # ------------------------------------------------------------------
    # Rolling-calibration helpers
    # ------------------------------------------------------------------

    def calibrate(
        self,
        location_id: str,
        variable: str,
        anchor_date: str,
        n_days: int = 20,
        lead_time_hours: int = 0,
    ) -> tuple[float, float, int]:
        """Compute (bias, sigma, n) from GFS forecast vs observed weather.

        Uses the observed_weather table (exact station/ERA5 data) for the
        given location_id and variable, comparing against the GFS forecast
        at the specified lead_time_hours (default T+0).

        bias = mean(GFS - observed)
        sigma = std(GFS - observed)
        Corrected value = GFS_raw - bias  (see gfs_prediction.py line 358)

        Returns (GFS_BIAS_CORRECTION[location_id][variable], DEFAULT_SIGMA[market_type])
        as fallback when there are fewer than 5 pairs.
        """
        MIN_PAIRS = 5
        # Determine market_type from variable so we can pick the right fallback sigma
        mt_by_var = {
            "temperature_2m_max": "temp_above",
            "temperature_2m_min": "temp_above",
            "precipitation_sum": "precip",
            "snowfall_sum": "snow",
        }
        market_type = mt_by_var.get(variable, "temp_above")

        # Fallback values
        fb_bias  = GFS_BIAS_CORRECTION.get(location_id, {}).get(variable, 0.0)
        fb_sigma = DEFAULT_SIGMA.get(market_type, 1.0)

        # Join GFS forecasts with observed weather for this location/variable.
        # Filter to the requested lead_time_hours (default 0 = T+0).
        pairs = self.gfs_conn.execute(
            """
            SELECT g.value AS gfs_value, o.value AS obs_value
            FROM gfs_forecasts g
            JOIN observed_weather o
              ON  g.location_id = o.location_id
              AND g.target_date = o.target_date
              AND g.variable    = o.variable
            WHERE g.location_id = ?
              AND g.variable    = ?
              AND g.target_date < ?
              AND g.lead_time_hours = ?
              AND g.value IS NOT NULL
              AND o.value IS NOT NULL
            ORDER BY g.target_date DESC
            LIMIT ?
            """,
            (location_id, variable, anchor_date, lead_time_hours, n_days),
        ).fetchall()

        if len(pairs) < MIN_PAIRS:
            return fb_bias, fb_sigma, 0

        errors: list[float] = [float(p[0]) - float(p[1]) for p in pairs]

        n = len(errors)
        bias = sum(errors) / n
        residuals = [e - bias for e in errors]
        sigma = max(math.sqrt(sum(r * r for r in residuals) / n), 0.3)
        return bias, sigma, n

    def run_standard(
        self,
        start: str,
        end: str,
        train_days: int = 20,
        city: Optional[str] = None,
        min_edge: float = 0.05,
        amount: float = 5.0,
        max_lead_time_hours: Optional[int] = 48,
        min_price: float = 0.03,
        prediction_source: Optional[PredictionSource] = None,
        min_liquidity: float = 50.0,
        dedup_same_day: bool = True,
    ) -> list[dict]:
        """Per-trade rolling calibration backtest using a pluggable PredictionSource.

        For each (market, price_snapshot) row:
          1. Builds a MarketContext (with price_date in extra for historical lookup).
          2. Calls prediction_source.predict(ctx).
          3. Uses edge_composer.compute_edge() to decide trade direction.

        Args:
            start, end: price_date range (YYYY-MM-DD)
            train_days: rolling calibration window size (passed via MarketContext.extra)
            city: optional city name filter
            min_edge: minimum absolute edge to include trade
            amount: simulated stake per trade (for P&L)
            max_lead_time_hours: skip forecasts beyond this lead time
            min_price: skip YES-token prices below this (and above 1-this)
            prediction_source: override; if None uses self._prediction_source or
                               lazy-creates GFSPredictionSource(mode="historical")

        Returns:
            List of dicts with keys:
                date, target_date, question, market_price, model_prob, edge,
                direction, actual_outcome, pnl, calib_bias, calib_sigma, calib_n
            (pandas.DataFrame-compatible)
        """
        from .edge_composer import compute_edge as _compute_edge

        source = prediction_source or self._get_or_create_prediction_source()

        params: list = [start, end]
        city_clause = ""
        if city:
            city_clause = " AND lower(m.city) = lower(?)"
            params.append(city)

        rows = self.market_conn.execute(
            """
            SELECT
                m.id, m.question, m.city, m.country, m.market_type,
                m.threshold_value, m.threshold_unit, m.target_date,
                m.resolved_outcome, m.latitude, m.longitude,
                p.timestamp, p.price,
                m.volume, m.liquidity
            FROM markets m
            JOIN price_history p ON p.market_id = m.id
            WHERE substr(p.timestamp, 1, 10) BETWEEN ? AND ?
              AND m.market_type IN ('temp_above')
              AND m.threshold_value IS NOT NULL
              AND m.target_date IS NOT NULL
              AND m.city IS NOT NULL
              {city_clause}
            ORDER BY p.timestamp, m.id
            """.format(city_clause=city_clause),
            params,
        ).fetchall()

        results: list[dict] = []

        for row in rows:
            location_id = normalize_location_id(row["city"], row["country"])
            variable = infer_market_variable(row["question"], row["market_type"])
            price_date = row["timestamp"][:10]

            if max_lead_time_hours is not None:
                lead = self._lead_hours(price_date, row["target_date"])
                if lead > max_lead_time_hours:
                    continue

            threshold = convert_threshold(
                row["threshold_value"], row["threshold_unit"], variable
            )
            rule = infer_market_rule(row["question"], row["market_type"])

            ctx = MarketContext(
                market_id=row["id"],
                question=row["question"],
                outcomes=["Yes", "No"],
                outcome_prices=[min(max(float(row["price"]), 0.001), 0.999)],
                city=row["city"] or "",
                country=row["country"] or "",
                target_date=row["target_date"],
                market_type=row["market_type"],
                threshold_value=threshold,
                threshold_unit=row["threshold_unit"] or "",
                variable=variable,
                rule=rule,
                latitude=float(row["latitude"] or 0),
                longitude=float(row["longitude"] or 0),
                location_id=location_id,
                extra={
                    "price_date": price_date,
                    "train_days": train_days,
                },
            )

            prediction = source.predict(ctx)
            if prediction is None:
                continue

            market_yes = ctx.outcome_prices[0]
            if min_price > 0 and (market_yes < min_price or market_yes > 1.0 - min_price):
                continue

            # Liquidity filter (缺陷6): skip markets with insufficient volume
            if min_liquidity > 0:
                vol = float(row["volume"] or 0)
                liq = float(row["liquidity"] or 0)
                if vol < min_liquidity and liq < min_liquidity * 50:
                    continue

            signal = _compute_edge(
                prediction, market_yes, ctx,
                min_edge=min_edge,
                min_confidence=0.0,
            )
            if not signal.flagged:
                continue

            # Resolve outcome from DB
            _, _unit = MARKET_VARIABLES[row["market_type"]]
            observed = self._observed(location_id, row["target_date"], variable)
            actual_yes = self._actual_yes(row["resolved_outcome"], observed, threshold, rule)
            trade_price = market_yes if signal.direction == "BUY_YES" else 1.0 - market_yes
            # PnL with realistic costs: ~2% half-spread + 5% taker fee on wins
            pnl = simulate_pnl(
                signal.direction, trade_price, amount, actual_yes,
                half_spread=0.02, taker_fee_rate=0.05,
            )

            extra = prediction.extra or {}
            results.append({
                "date":           price_date,
                "target_date":    row["target_date"],
                "question":       row["question"],
                "market_price":   market_yes,
                "model_prob":     round(prediction.estimated_probability, 4),
                "edge":           round(signal.edge, 4),
                "direction":      signal.direction,
                "actual_outcome": actual_yes,
                "pnl":            round(pnl, 4),
                "calib_bias":     extra.get("calib_bias", 0.0),
                "calib_sigma":    extra.get("calib_sigma", 0.0),
                "calib_n":        extra.get("calib_n", 0),
            })

        # Defect 4 fix: discrete markets have multiple temperature buckets per
        # day (24/25/26/27/28/...) but only ONE wins.  Treating each bucket as
        # an independent trade inflates trade count and Sharpe.  When
        # dedup_same_day is True, keep only the highest-|edge| bucket per
        # (date, target_date) — i.e. one concentrated bet per day.
        if dedup_same_day and results:
            best_per_day: dict[tuple, dict] = {}
            for r in results:
                key = (r["date"], r["target_date"])
                cur = best_per_day.get(key)
                if cur is None or abs(r["edge"]) > abs(cur["edge"]):
                    best_per_day[key] = r
            results = list(best_per_day.values())
            # Re-sort by date for stable output
            results.sort(key=lambda r: (r["date"], r["target_date"]))

        return results

    def summary(self, trades: list[BacktestTrade]) -> dict:
        resolved = [trade for trade in trades if trade.actual_yes is not None]
        total_pnl = sum(trade.pnl for trade in resolved)
        invested = sum(trade.amount for trade in resolved)
        wins = sum(1 for trade in resolved if trade.pnl > 0)
        lead_days = [trade.days_to_target for trade in trades] if trades else []
        return {
            "trades": len(trades),
            "resolved": len(resolved),
            "wins": wins,
            "losses": len(resolved) - wins,
            "win_rate": wins / len(resolved) if resolved else 0.0,
            "total_pnl": total_pnl,
            "invested": invested,
            "roi": total_pnl / invested if invested else 0.0,
            "avg_edge": sum(abs(trade.edge) for trade in trades) / len(trades) if trades else 0.0,
            "avg_days_to_target": sum(lead_days) / len(lead_days) if lead_days else 0.0,
        }

    def write_csv(self, trades: list[BacktestTrade], path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(asdict(trades[0]).keys()) if trades else [])
            if trades:
                writer.writeheader()
                for trade in trades:
                    writer.writerow(asdict(trade))

    def _forecast(self, location_id: str, target_date: str, variable: str):
        """Look up the best available GFS forecast for a target date (any issued date)."""
        return self.gfs_conn.execute(
            """
            SELECT *
            FROM gfs_forecasts
            WHERE location_id = ?
              AND target_date = ?
              AND variable = ?
            ORDER BY forecast_issued DESC
            LIMIT 1
            """,
            (location_id, target_date, variable),
        ).fetchone()

    def _observed(self, location_id: str, target_date: str, variable: str):
        return self.gfs_conn.execute(
            """
            SELECT *
            FROM observed_weather
            WHERE location_id = ?
              AND target_date = ?
              AND variable = ?
            """,
            (location_id, target_date, variable),
        ).fetchone()

    @staticmethod
    def _lead_hours(price_date: str, target_date: str) -> int:
        from datetime import date as _date
        try:
            issued = _date.fromisoformat(price_date[:10])
            target = _date.fromisoformat(target_date[:10])
            return max(0, (target - issued).days * 24)
        except ValueError:
            return 0

    def _actual_yes(self, resolved_outcome, observed, threshold: float, rule: str) -> Optional[bool]:
        if resolved_outcome:
            value = str(resolved_outcome).strip().lower()
            if value in ("yes", "true", "1"):
                return True
            if value in ("no", "false", "0"):
                return False
        if observed and observed["value"] is not None:
            observed_value = float(observed["value"])
            if rule == "lte":
                return observed_value <= threshold
            if rule == "gte":
                return observed_value >= threshold
            return math.floor(observed_value) == math.floor(threshold)
        return None


def infer_market_rule(question: str, market_type: str) -> str:
    q = question.lower()
    if any(text in q for text in ("or below", "or less", "less than", "below")):
        return "lte"
    if any(text in q for text in ("or higher", "or above", "greater than", "more than", "above")):
        return "gte"
    if market_type in ("precip", "snow"):
        return "gte"
    return "eq"


def infer_market_variable(question: str, market_type: str) -> str:
    """Infer GFS variable from market question text.
    
    'lowest'/'minimum' temperature → temperature_2m_min, else temperature_2m_max.
    """
    if market_type == "temp_above":
        q = question.lower()
        if "lowest" in q or "minimum" in q:
            return "temperature_2m_min"
        return "temperature_2m_max"
    return MARKET_VARIABLES.get(market_type, (None, ""))[0] or ""


def probability_for_rule(forecast_value: float, threshold: float, sigma: float, rule: str) -> float:
    if rule == "lte":
        probability = _normal_cdf((threshold - forecast_value) / sigma)
    elif rule == "gte":
        probability = 1.0 - _normal_cdf((threshold - forecast_value) / sigma)
    else:
        lower = _normal_cdf(((threshold - 0.5) - forecast_value) / sigma)
        upper = _normal_cdf(((threshold + 0.5) - forecast_value) / sigma)
        probability = upper - lower
    return min(max(probability, 0.001), 0.999)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def convert_threshold(value: float, unit: Optional[str], variable: str) -> float:
    if unit == "inch" and variable == "precipitation_sum":
        return value * 25.4
    if unit == "inch" and variable == "snowfall_sum":
        return value * 2.54
    return float(value)


def simulate_pnl(
    direction: str,
    price: float,
    amount: float,
    actual_yes: Optional[bool],
    half_spread: float = 0.01,
    taker_fee_rate: float = 0.05,
) -> float:
    """Simulate trade PnL with realistic execution costs.

    Args:
        direction: "BUY_YES" | "BUY_NO"
        price: mid price of the YES token (0-1)
        amount: USD notional staked
        actual_yes: resolved outcome; None = unresolved (PnL=0)
        half_spread: half bid-ask spread (taker crosses the book).
                     We pay (mid + half_spread) when buying.
        taker_fee_rate: Polymarket weather markets charge ~5% taker fee on
                        winning proceeds (rounded). 0 to disable.
    """
    if actual_yes is None:
        return 0.0
    # Taker buys at the ask side of the book
    buy_price = min(max(price + half_spread, 0.001), 0.999)
    tokens = amount / buy_price
    won = actual_yes if direction == "BUY_YES" else not actual_yes
    if won:
        gross = tokens - amount  # win: tokens pay $1 each, minus stake
        fee = gross * taker_fee_rate
        return gross - fee
    return -amount
