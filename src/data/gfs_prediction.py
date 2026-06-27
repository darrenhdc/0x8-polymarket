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

import json
import math
import time
from datetime import date
from pathlib import Path
from typing import Optional

import requests

from .database import connect_gfs, connect_markets, init_gfs_db, init_weather_db
from .prediction_interface import MarketContext, Prediction, PredictionSource


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Path to pre-computed calibration JSON (HKO × GFS, London × GFS).
# These values are preferred over DB-computed calibration when they
# have more pairs than the dynamically-computed DB estimates.
_CALIBRATION_JSON_PATH = (
    Path(__file__).parent.parent.parent / "data" / "calibration.json"
)

# Per-lead fallback calibration — used only when neither calibration.json
# nor DB have sufficient data.
BIAS_BY_LEAD_H = {0: -1.324, 24: -3.242, 48: -3.440, 72: -3.382}
SIGMA_BY_LEAD_H = {0: 1.200, 24: 1.409, 48: 1.599, 72: 1.496}

# Single-value fallbacks (T+0) for legacy code paths
DEFAULT_BIAS_TEMP   = -1.324   # = bias at lead 0h
DEFAULT_SIGMA_TEMP  =  1.200   # = sigma at lead 0h
DEFAULT_BIAS_PRECIP =  0.0     # mm
DEFAULT_SIGMA_PRECIP = 5.0     # mm

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
# Pre-computed calibration JSON
# ---------------------------------------------------------------------------

_calib_json_cache: Optional[dict] = None

def _load_calibration_json() -> dict:
    """Load calibration.json, caching in memory.

    Returns a dict keyed by city display name, with per-variable per-lead
    bias/sigma/n values.  Example::

        {"Hong Kong": {"temperature_2m_max": {
            "bias_by_lead_hours": {"0": -1.324, ...},
            "sigma_by_lead_hours": {"0": 1.200, ...},
            "pairs_by_lead_hours": {"0": 791, ...},
        }}}
    """
    global _calib_json_cache
    if _calib_json_cache is not None:
        return _calib_json_cache
    try:
        if _CALIBRATION_JSON_PATH.exists():
            _calib_json_cache = json.loads(_CALIBRATION_JSON_PATH.read_text())
        else:
            _calib_json_cache = {}
    except Exception:
        _calib_json_cache = {}
    return _calib_json_cache


def _lookup_json_calibration(
    city_display: str,
    variable: str,
    lead_time_hours: Optional[int],
) -> Optional[tuple[float, float, int]]:
    """Look up pre-computed calibration for a city/variable.

    Returns (bias, sigma, n) or None if not found.
    Supports both formats:
      - Simple:   {"bias": x, "sigma": y, "n": n}
      - Per-lead: {"bias_by_lead_hours": {"0": x}, "sigma_by_lead_hours": ..., "pairs_by_lead_hours": ...}
    """
    calib = _load_calibration_json()
    city_data = calib.get(city_display)
    if city_data is None:
        return None
    var_data = city_data.get(variable)
    if var_data is None:
        return None

    # Simple format: {"bias": x, "sigma": y, "n": n}
    if "bias" in var_data and "sigma" in var_data:
        try:
            return float(var_data["bias"]), float(var_data["sigma"]), int(var_data.get("n", 0))
        except (KeyError, ValueError, TypeError):
            return None

    # Per-lead format
    biases = var_data.get("bias_by_lead_hours", {})
    sigmas = var_data.get("sigma_by_lead_hours", {})
    pairs  = var_data.get("pairs_by_lead_hours", {})

    lead_key = "0"
    if lead_time_hours is not None:
        for bucket in ["72", "48", "24", "0"]:
            if lead_time_hours >= int(bucket):
                lead_key = bucket
                break

    if lead_key not in biases:
        return None
    try:
        bias = float(biases[lead_key])
        sigma = float(sigmas[lead_key])
        n = int(pairs[lead_key])
        return bias, sigma, n
    except (KeyError, ValueError, TypeError):
        return None


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
    lead_time_hours: Optional[int] = None,
) -> tuple[float, float, int]:
    """Compute (bias, sigma, n) from GFS forecast vs observed weather.

    Uses the observed_weather table for exact station/ERA5 data.
    Filters by location_id, variable, and lead_time_hours.
    bias = mean(GFS - observed); corrected = GFS_raw - bias.
    """
    lt = lead_time_hours if lead_time_hours is not None else 0

    pairs = gfs_conn.execute(
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
        (location_id, variable, anchor_date, lt, n_days),
    ).fetchall()

    if len(pairs) < MIN_CALIB_PAIRS:
        return default_bias, default_sigma, 0

    errors: list[float] = [float(p[0]) - float(p[1]) for p in pairs]
    n = len(errors)
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

        # Determine lead time bucket for per-lead calibration
        lead_h = None
        if market.target_date and anchor_date:
            try:
                td = date.fromisoformat(market.target_date[:10])
                pd = date.fromisoformat(anchor_date[:10])
                lead_h = max(0, (td - pd).days * 24)
            except ValueError:
                lead_h = None

        # Calibration (per-lead-time bucket when lead_h known)
        bias, sigma, calib_n = self._calibrate(
            location_id=market.location_id,
            variable=variable,
            anchor_date=anchor_date,
            lead_time_hours=lead_h,
        )

        # bias = mean(GFS - actual), so correction = GFS - bias = GFS - (GFS - actual) ≈ actual
        gfs_corrected = gfs_raw - bias
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
                f"Calibration n={calib_n} ({'pre-computed JSON' if calib_n >= 100 else 'from DB' if calib_n > 0 else 'fallback'})",
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
            return self._historical_forecast(target_date, variable, location_id, price_date)

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
        price_date: Optional[str] = None,
    ) -> Optional[float]:
        # Look-ahead fix: only use forecasts issued on or before price_date.
        # If price_date is None (live mode misuse), fall back to latest for safety.
        if price_date:
            row = self.gfs_conn.execute(
                """
                SELECT value FROM gfs_forecasts
                WHERE location_id = ? AND target_date = ? AND variable = ?
                  AND forecast_issued <= ?
                ORDER BY forecast_issued DESC LIMIT 1
                """,
                (location_id, target_date, variable, price_date),
            ).fetchone()
        else:
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
        lead_time_hours: Optional[int] = None,
    ) -> tuple[float, float, int]:
        cache_key = (location_id, variable, anchor_date, lead_time_hours)
        if cache_key not in self._calib_cache:
            # 1. Compute DB-based calibration (rolling window from resolved markets)
            db_bias, db_sigma, db_n = _compute_calibration(
                gfs_conn=self.gfs_conn,
                market_conn=self.market_conn,
                location_id=location_id,
                market_type="temp_above",
                variable=variable,
                anchor_date=anchor_date,
                n_days=self.calib_window,
                default_bias=DEFAULT_BIAS_TEMP,
                default_sigma=DEFAULT_SIGMA_TEMP,
                lead_time_hours=lead_time_hours,
            )

            # 2. Check calibration.json for a higher-quality pre-computed calibration
            #    The JSON values come from long-term station data (HKO, Wunderground)
            #    and are preferred when they have more pairs than DB.
            #    Map location_id → city display name.
            city_map = {
                "hong_kong_hong_kong": "Hong Kong",
                "london_united_kingdom": "London",
            }
            city_display = city_map.get(location_id)
            if city_display is not None:
                json_calib = _lookup_json_calibration(
                    city_display, variable, lead_time_hours
                )
                if json_calib is not None:
                    json_bias, json_sigma, json_n = json_calib
                    # Prefer JSON if it has more pairs than DB
                    if json_n > db_n:
                        bias, sigma, n = json_bias, json_sigma, json_n
                        self._calib_cache[cache_key] = (bias, sigma, n)
                        return self._calib_cache[cache_key]

            # 3. Fall back to DB computation
            self._calib_cache[cache_key] = (db_bias, db_sigma, db_n)
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
        return self._historical_forecast(target_date, variable, location_id, price_date)

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
        self, target_date, variable, location_id, price_date=None
    ) -> Optional[float]:
        # Look-ahead fix: only use forecasts issued on or before price_date.
        if price_date:
            row = self.gfs_conn.execute(
                """
                SELECT value FROM gfs_forecasts
                WHERE location_id = ? AND target_date = ? AND variable = ?
                  AND forecast_issued <= ?
                ORDER BY forecast_issued DESC LIMIT 1
                """,
                (location_id, target_date, variable, price_date),
            ).fetchone()
        else:
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
            db_bias, db_sigma, db_n = _compute_calibration(
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

            # Check calibration.json for better pre-computed values
            city_map = {
                "hong_kong_hong_kong": "Hong Kong",
                "london_united_kingdom": "London",
            }
            city_display = city_map.get(location_id)
            if city_display is not None:
                json_calib = _lookup_json_calibration(
                    city_display, variable, None
                )
                if json_calib is not None:
                    json_bias, json_sigma, json_n = json_calib
                    if json_n > db_n:
                        self._calib_cache[cache_key] = (json_bias, json_sigma, json_n)
                        return self._calib_cache[cache_key]

            self._calib_cache[cache_key] = (db_bias, db_sigma, db_n)
        return self._calib_cache[cache_key]
