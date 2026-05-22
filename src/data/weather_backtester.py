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
    "temp_above": 1.5,   # calibrated from 65 HK dates: bias-corrected residual std 1.47°C
    "precip": 2.0,
    "snow": 2.0,
}

# GFS cold bias per location_id (GFS forecast - VHHH station, negative = GFS runs cold).
# Correction is *added* to the GFS forecast before computing probabilities.
# Calibrated in-sample from resolved HK markets Mar-May 2026.
GFS_BIAS_CORRECTION: dict[str, dict[str, float]] = {
    "hong-kong_hong-kong": {"temperature_2m_max": +0.89, "temperature_2m_min": +0.89},
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
              AND m.market_type IN ('temp_above', 'precip', 'snow')
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
            variable, _unit = MARKET_VARIABLES[row["market_type"]]
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
    ) -> tuple[float, float]:
        """Compute (bias, sigma) from the N resolved dates immediately before anchor_date.

        Returns (GFS_BIAS_CORRECTION[location_id][variable], DEFAULT_SIGMA[market_type])
        as fallback when there are fewer than 5 resolved pairs.
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

        # Resolved markets before anchor_date (YES = market resolved Yes → threshold IS the actual temp)
        resolved_rows = self.market_conn.execute(
            """
            SELECT target_date, threshold_value
            FROM markets
            WHERE city IS NOT NULL
              AND resolved_outcome = 'Yes'
              AND market_type = ?
              AND target_date < ?
            ORDER BY target_date DESC
            LIMIT ?
            """,
            (market_type, anchor_date, n_days),
        ).fetchall()

        if len(resolved_rows) < MIN_PAIRS:
            return fb_bias, fb_sigma

        errors: list[float] = []
        for d, vhhh in resolved_rows:
            vhhh_f = float(vhhh)
            gfs_row = self.gfs_conn.execute(
                """
                SELECT value FROM gfs_forecasts
                WHERE location_id = ? AND target_date = ? AND variable = ?
                ORDER BY forecast_issued DESC LIMIT 1
                """,
                (location_id, d, variable),
            ).fetchone()
            if gfs_row is not None:
                errors.append(float(gfs_row[0]) - vhhh_f)

        if len(errors) < MIN_PAIRS:
            return fb_bias, fb_sigma

        n = len(errors)
        bias = sum(errors) / n
        residuals = [e - bias for e in errors]
        sigma = max(math.sqrt(sum(r * r for r in residuals) / n), 0.3)
        return bias, sigma

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
        from .gfs_prediction import VARIABLE_MAP

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
                p.timestamp, p.price
            FROM markets m
            JOIN price_history p ON p.market_id = m.id
            WHERE substr(p.timestamp, 1, 10) BETWEEN ? AND ?
              AND m.market_type IN ('temp_above', 'precip', 'snow')
              AND m.threshold_value IS NOT NULL
              AND m.target_date IS NOT NULL
              AND m.city IS NOT NULL
              {city_clause}
            ORDER BY p.timestamp, m.id
            """.format(city_clause=city_clause),
            params,
        ).fetchall()

        results: list[dict] = []
        variable_map = VARIABLE_MAP

        for row in rows:
            location_id = normalize_location_id(row["city"], row["country"])
            variable = variable_map.get(row["market_type"], "temperature_2m_max")
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
            pnl = simulate_pnl(signal.direction, trade_price, amount, actual_yes)

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


def simulate_pnl(direction: str, price: float, amount: float, actual_yes: Optional[bool]) -> float:
    if actual_yes is None:
        return 0.0
    tokens = amount / min(max(price, 0.001), 0.999)
    won = actual_yes if direction == "BUY_YES" else not actual_yes
    return (tokens - amount) if won else -amount
