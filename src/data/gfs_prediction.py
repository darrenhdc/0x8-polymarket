"""GFS Prediction Sources — temperature and precipitation.

Two PredictionSource implementations that wrap the Open-Meteo GFS model:

  GFSPredictionSource   — temperature markets (temp_above)
  GFSPrecipSource       — precipitation markets (precip)

Both support two modes:
  mode="live"       — fetch current GFS forecast from Open-Meteo API
  mode="historical" — read GFS forecast stored in gfs_forecasts.db
                      (set market.extra["price_date"] to control the
                       forecast cut-off date for backtesting)

Calibration (rolling window):
  For every predict() call the source computes a rolling (bias, sigma) from
  the last N resolved market pairs before the anchor date.  The result is
  cached per (location_id, variable, anchor_date) to avoid redundant queries.
  Falls back to DEFAULT_BIAS / DEFAULT_SIGMA when fewer than MIN_CALIB_PAIRS
  resolved pairs exist.
"""
from __future__ import annotations

import math
import time
from datetime import date
from typing import Optional

import requests

from .database import connect_gfs, connect_markets, init_gfs_db, init_weather_db
from .prediction_interface import MarketContext, Prediction, PredictionSource


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Fallback calibration — HK temperature (GFS runs cold vs VHHH station)
DEFAULT_BIAS_TEMP   = +0.89   # °C
DEFAULT_SIGMA_TEMP  =  1.79   # °C
DEFAULT_BIAS_PRECIP =  0.0    # mm
DEFAULT_SIGMA_PRECIP = 5.0    # mm

MIN_CALIB_PAIRS = 5
DEFAULT_CALIB_WINDOW = 20

# Market variables mapped from market_type
VARIABLE_MAP = {
    "temp_above": "temperature_2m_max",
    "precip":     "precipitation_sum",
    "snow":       "snowfall_sum",
}


# ---------------------------------------------------------------------------
# Math helpers (no scipy dependency)
# ---------------------------------------------------------------------------

def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _point_prob(forecast: float, threshold: float, sigma: float) -> float:
    """P(T rounds to threshold) = P(threshold-0.5 ≤ T < threshold+0.5)."""
    lo = _normal_cdf(((threshold - 0.5) - forecast) / sigma)
    hi = _normal_cdf(((threshold + 0.5) - forecast) / sigma)
    return min(max(hi - lo, 0.001), 0.999)


def _gte_prob(forecast: float, threshold: float, sigma: float) -> float:
    """P(T ≥ threshold)."""
    return min(max(1.0 - _normal_cdf((threshold - forecast) / sigma), 0.001), 0.999)


def _lte_prob(forecast: float, threshold: float, sigma: float) -> float:
    """P(T ≤ threshold)."""
    return min(max(_normal_cdf((threshold - forecast) / sigma), 0.001), 0.999)


def probability_for_rule(
    forecast: float, threshold: float, sigma: float, rule: str
) -> float:
    if rule == "gte":
        return _gte_prob(forecast, threshold, sigma)
    elif rule == "lte":
        return _lte_prob(forecast, threshold, sigma)
    else:  # "eq"
        return _point_prob(forecast, threshold, sigma)


# ---------------------------------------------------------------------------
# Shared calibration helper
# ---------------------------------------------------------------------------

def _compute_calibration(
    gfs_conn,
    market_conn,
    location_id: str,
    market_type: str,
    variable: str,
    anchor_date: str,
    n_days: int,
    default_bias: float,
    default_sigma: float,
) -> tuple[float, float, int]:
    """Rolling calibration from the last n_days resolved pairs.

    Returns (bias, sigma, n_pairs).  Falls back to (default_bias, default_sigma, 0).
    """
    resolved = market_conn.execute(
        """
        SELECT target_date, threshold_value
        FROM markets
        WHERE resolved_outcome = 'Yes'
          AND market_type = ?
          AND target_date < ?
        ORDER BY target_date DESC
        LIMIT ?
        """,
        (market_type, anchor_date, n_days),
    ).fetchall()

    if len(resolved) < MIN_CALIB_PAIRS:
        return default_bias, default_sigma, 0

    errors: list[float] = []
    for d, vhhh in resolved:
        row = gfs_conn.execute(
            """
            SELECT value FROM gfs_forecasts
            WHERE location_id = ? AND target_date = ? AND variable = ?
            ORDER BY forecast_issued DESC LIMIT 1
            """,
            (location_id, d, variable),
        ).fetchone()
        if row is not None:
            errors.append(float(row[0]) - float(vhhh))

    n = len(errors)
    if n < MIN_CALIB_PAIRS:
        return default_bias, default_sigma, 0

    bias = sum(errors) / n
    residuals = [e - bias for e in errors]
    sigma = max(math.sqrt(sum(r * r for r in residuals) / n), 0.3)
    return bias, sigma, n


# ---------------------------------------------------------------------------
# GFSPredictionSource — temperature markets
# ---------------------------------------------------------------------------

class GFSPredictionSource(PredictionSource):
    """GFS temperature prediction source.

    Supports two modes:
      mode="live"       — live Open-Meteo GFS forecast
      mode="historical" — reads from gfs_forecasts.db (for backtesting)
                          expects market.extra["price_date"]

    Example (live signals)::
        src = GFSPredictionSource(mode="live")
        pred = src.predict(market_ctx)

    Example (backtesting)::
        src = GFSPredictionSource(mode="historical")
        ctx.extra["price_date"] = "2026-04-05"
        pred = src.predict(ctx)
    """

    def __init__(
        self,
        gfs_db_path=None,
        market_db_path=None,
        mode: str = "live",
        calib_window: int = DEFAULT_CALIB_WINDOW,
    ):
        super().__init__("gfs-temperature")
        self.mode = mode
        self.calib_window = calib_window
        self.gfs_conn = connect_gfs(gfs_db_path)
        self.market_conn = connect_markets(market_db_path)
        init_gfs_db(self.gfs_conn)
        init_weather_db(self.market_conn)
        self._live_cache: dict[tuple, float] = {}     # (date, variable, location_id) → value
        self._calib_cache: dict[tuple, tuple] = {}    # (location_id, variable, anchor) → (b, s, n)

    def close(self) -> None:
        self.gfs_conn.close()
        self.market_conn.close()

    def can_predict(self, market: MarketContext) -> bool:
        return (
            market.market_type == "temp_above"
            and market.latitude != 0.0
            and market.longitude != 0.0
            and market.target_date != ""
            and market.threshold_value != 0.0
        )

    def predict(self, market: MarketContext) -> Optional[Prediction]:
        if not self.can_predict(market):
            return None

        variable = market.variable or VARIABLE_MAP.get(market.market_type, "temperature_2m_max")
        anchor_date = market.extra.get("price_date", date.today().isoformat())

        # Forecast
        gfs_raw = self._get_forecast(
            target_date=market.target_date,
            variable=variable,
            lat=market.latitude,
            lon=market.longitude,
            location_id=market.location_id,
            price_date=anchor_date,
        )
        if gfs_raw is None:
            return None

        # Calibration
        bias, sigma, calib_n = self._calibrate(
            location_id=market.location_id,
            variable=variable,
            anchor_date=anchor_date,
        )

        gfs_corrected = gfs_raw + bias
        prob = probability_for_rule(gfs_corrected, market.threshold_value, sigma, market.rule)

        return Prediction(
            market_id=market.market_id,
            source_name=self.name,
            estimated_probability=prob,
            confidence=1.0,
            reasoning=(
                f"GFS {variable}={gfs_corrected:.2f} "
                f"(raw={gfs_raw:.2f}, bias={bias:+.2f}), "
                f"sigma={sigma:.2f}, rule={market.rule}, "
                f"threshold={market.threshold_value}"
            ),
            key_factors=[
                f"GFS corrected: {gfs_corrected:.1f}°C",
                f"Calibration n={calib_n} ({'fallback' if calib_n == 0 else 'from DB'})",
            ],
            extra={
                "gfs_raw": round(gfs_raw, 2),
                "gfs_corrected": round(gfs_corrected, 2),
                "calib_bias": round(bias, 4),
                "calib_sigma": round(sigma, 4),
                "calib_n": calib_n,
                "variable": variable,
            },
        )

    # ------------------------------------------------------------------
    # Forecast lookup
    # ------------------------------------------------------------------

    def _get_forecast(
        self,
        target_date: str,
        variable: str,
        lat: float,
        lon: float,
        location_id: str,
        price_date: str,
    ) -> Optional[float]:
        if self.mode == "live":
            return self._live_forecast(target_date, variable, lat, lon, location_id)
        else:
            return self._historical_forecast(target_date, variable, location_id)

    def _live_forecast(
        self,
        target_date: str,
        variable: str,
        lat: float,
        lon: float,
        location_id: str,
    ) -> Optional[float]:
        """Fetch (and cache) the live Open-Meteo GFS forecast for an entire location."""
        # If we already have any key for this (location, variable), the full
        # batch was fetched; check the specific date.
        cache_key = (target_date, variable, location_id)
        if cache_key in self._live_cache:
            return self._live_cache[cache_key]

        # Check if any date for this location has been fetched already
        sentinel = (None, variable, location_id)
        if sentinel in self._live_cache:
            return self._live_cache.get(cache_key)

        # Fetch batch for all 16 forecast days
        try:
            resp = requests.get(
                OPEN_METEO_FORECAST_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": variable,
                    "forecast_days": 16,
                    "timezone": "auto",
                    "models": "gfs_seamless",
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            times  = data["daily"]["time"]
            values = data["daily"][variable]
            for t, v in zip(times, values):
                if v is not None:
                    self._live_cache[(t, variable, location_id)] = float(v)
            # Mark sentinel so we don't re-fetch on cache miss
            self._live_cache[sentinel] = True
            time.sleep(0.05)
        except Exception as exc:
            print(f"[GFSPredictionSource] live forecast fetch failed: {exc}")
            self._live_cache[sentinel] = True

        return self._live_cache.get(cache_key)

    def _historical_forecast(
        self,
        target_date: str,
        variable: str,
        location_id: str,
    ) -> Optional[float]:
        row = self.gfs_conn.execute(
            """
            SELECT value FROM gfs_forecasts
            WHERE location_id = ? AND target_date = ? AND variable = ?
            ORDER BY forecast_issued DESC LIMIT 1
            """,
            (location_id, target_date, variable),
        ).fetchone()
        return float(row[0]) if row else None

    # ------------------------------------------------------------------
    # Rolling calibration
    # ------------------------------------------------------------------

    def _calibrate(
        self,
        location_id: str,
        variable: str,
        anchor_date: str,
    ) -> tuple[float, float, int]:
        cache_key = (location_id, variable, anchor_date)
        if cache_key not in self._calib_cache:
            bias, sigma, n = _compute_calibration(
                gfs_conn=self.gfs_conn,
                market_conn=self.market_conn,
                location_id=location_id,
                market_type="temp_above",
                variable=variable,
                anchor_date=anchor_date,
                n_days=self.calib_window,
                default_bias=DEFAULT_BIAS_TEMP,
                default_sigma=DEFAULT_SIGMA_TEMP,
            )
            self._calib_cache[cache_key] = (bias, sigma, n)
        return self._calib_cache[cache_key]


# ---------------------------------------------------------------------------
# GFSPrecipSource — precipitation markets
# ---------------------------------------------------------------------------

class GFSPrecipSource(PredictionSource):
    """GFS precipitation prediction source.

    Uses a Gaussian ≥threshold model for cumulative precipitation.
    Typically the rule is "gte" (market resolves YES if precip ≥ threshold mm).

    Calibration: rolling bias/sigma from last N resolved precip pairs.
    Fallback: bias=0 mm, sigma=5 mm (wide uncertainty for rare events).
    """

    def __init__(
        self,
        gfs_db_path=None,
        market_db_path=None,
        mode: str = "live",
        calib_window: int = DEFAULT_CALIB_WINDOW,
    ):
        super().__init__("gfs-precip")
        self.mode = mode
        self.calib_window = calib_window
        self.gfs_conn = connect_gfs(gfs_db_path)
        self.market_conn = connect_markets(market_db_path)
        init_gfs_db(self.gfs_conn)
        init_weather_db(self.market_conn)
        self._live_cache: dict[tuple, float] = {}
        self._calib_cache: dict[tuple, tuple] = {}

    def close(self) -> None:
        self.gfs_conn.close()
        self.market_conn.close()

    def can_predict(self, market: MarketContext) -> bool:
        return (
            market.market_type in ("precip", "snow")
            and market.latitude != 0.0
            and market.longitude != 0.0
            and market.target_date != ""
            and market.threshold_value > 0.0
        )

    def predict(self, market: MarketContext) -> Optional[Prediction]:
        if not self.can_predict(market):
            return None

        variable = VARIABLE_MAP.get(market.market_type, "precipitation_sum")
        anchor_date = market.extra.get("price_date", date.today().isoformat())

        gfs_raw = self._get_forecast(
            target_date=market.target_date,
            variable=variable,
            lat=market.latitude,
            lon=market.longitude,
            location_id=market.location_id,
            price_date=anchor_date,
        )
        if gfs_raw is None:
            return None

        bias, sigma, calib_n = self._calibrate(
            location_id=market.location_id,
            market_type=market.market_type,
            variable=variable,
            anchor_date=anchor_date,
        )

        gfs_corrected = max(gfs_raw + bias, 0.0)  # precip cannot be negative
        # Precip markets are almost always "gte" or "lte"; default to rule
        rule = market.rule if market.rule in ("gte", "lte") else "gte"
        prob = probability_for_rule(gfs_corrected, market.threshold_value, sigma, rule)

        return Prediction(
            market_id=market.market_id,
            source_name=self.name,
            estimated_probability=prob,
            confidence=0.75,  # slightly lower: precip harder to forecast
            reasoning=(
                f"GFS {variable}={gfs_corrected:.1f}mm "
                f"(raw={gfs_raw:.1f}, bias={bias:+.1f}), "
                f"sigma={sigma:.1f}mm, threshold={market.threshold_value}mm, rule={rule}"
            ),
            key_factors=[
                f"GFS corrected: {gfs_corrected:.1f} mm",
                f"Calibration n={calib_n}",
            ],
            extra={
                "gfs_raw": round(gfs_raw, 2),
                "gfs_corrected": round(gfs_corrected, 2),
                "calib_bias": round(bias, 4),
                "calib_sigma": round(sigma, 4),
                "calib_n": calib_n,
                "variable": variable,
            },
        )

    def _get_forecast(
        self, target_date, variable, lat, lon, location_id, price_date
    ) -> Optional[float]:
        if self.mode == "live":
            return self._live_forecast(target_date, variable, lat, lon, location_id)
        return self._historical_forecast(target_date, variable, location_id)

    def _live_forecast(
        self, target_date, variable, lat, lon, location_id
    ) -> Optional[float]:
        cache_key = (target_date, variable, location_id)
        if cache_key in self._live_cache:
            return self._live_cache[cache_key]
        sentinel = (None, variable, location_id)
        if sentinel in self._live_cache:
            return self._live_cache.get(cache_key)
        try:
            resp = requests.get(
                OPEN_METEO_FORECAST_URL,
                params={
                    "latitude": lat, "longitude": lon,
                    "daily": variable,
                    "forecast_days": 16,
                    "timezone": "auto",
                    "models": "gfs_seamless",
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            for t, v in zip(data["daily"]["time"], data["daily"][variable]):
                if v is not None:
                    self._live_cache[(t, variable, location_id)] = float(v)
            self._live_cache[sentinel] = True
            time.sleep(0.05)
        except Exception as exc:
            print(f"[GFSPrecipSource] live forecast fetch failed: {exc}")
            self._live_cache[sentinel] = True
        return self._live_cache.get(cache_key)

    def _historical_forecast(
        self, target_date, variable, location_id
    ) -> Optional[float]:
        row = self.gfs_conn.execute(
            """
            SELECT value FROM gfs_forecasts
            WHERE location_id = ? AND target_date = ? AND variable = ?
            ORDER BY forecast_issued DESC LIMIT 1
            """,
            (location_id, target_date, variable),
        ).fetchone()
        return float(row[0]) if row else None

    def _calibrate(
        self, location_id, market_type, variable, anchor_date
    ) -> tuple[float, float, int]:
        cache_key = (location_id, variable, anchor_date)
        if cache_key not in self._calib_cache:
            bias, sigma, n = _compute_calibration(
                gfs_conn=self.gfs_conn,
                market_conn=self.market_conn,
                location_id=location_id,
                market_type=market_type,
                variable=variable,
                anchor_date=anchor_date,
                n_days=self.calib_window,
                default_bias=DEFAULT_BIAS_PRECIP,
                default_sigma=DEFAULT_SIGMA_PRECIP,
            )
            self._calib_cache[cache_key] = (bias, sigma, n)
        return self._calib_cache[cache_key]
