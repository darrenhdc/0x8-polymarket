"""
GFS Weather Prediction Source — uses GFS forecast + sigma 0.7°C to estimate
the probability of temperature exceeding a threshold.

Model:  P(temp > threshold) = 1 - Φ((threshold - forecast) / sigma)

Where:
  - forecast = GFS T+1 temperature prediction (°C) from Open-Meteo API
  - sigma = 0.7°C (standard deviation of GFS T+1 forecast error)
  - Φ = standard normal CDF

This plugs directly into the PredictionSource framework.

Cities supported: any lat/lon.  Pre-configured for common Polymarket cities.
"""

import json
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

from prediction_source import PredictionSource, Prediction, MarketContext


# ── City coordinates (lat, lon) for common Polymarket weather cities ──

CITIES = {
    "beijing":     (39.90, 116.40),
    "shanghai":    (31.23, 121.47),
    "tokyo":       (35.68, 139.76),
    "seoul":       (37.57, 126.98),
    "new york":    (40.71, -74.01),
    "nyc":         (40.71, -74.01),
    "london":      (51.51, -0.13),
    "paris":       (48.85, 2.35),
    "berlin":      (52.52, 13.41),
    "moscow":      (55.75, 37.62),
    "dubai":       (25.20, 55.27),
    "singapore":   (1.35, 103.82),
    "sydney":      (-33.87, 151.21),
    "chicago":     (41.88, -87.63),
    "los angeles": (34.05, -118.24),
    "miami":       (25.76, -80.19),
    "houston":     (29.76, -95.37),
    "phoenix":     (33.45, -112.07),
    "las vegas":   (36.17, -115.14),
    "dallas":      (32.78, -96.80),
    "san francisco": (37.77, -122.42),
}


def find_city_coords(question: str) -> Optional[tuple]:
    """Extract city name from a Polymarket question and return (lat, lon)."""
    q = question.lower()
    for city, coords in CITIES.items():
        if city in q:
            return coords
    return None


def extract_threshold(question: str) -> Optional[float]:
    """
    Extract temperature threshold from a market question.
    E.g. "exceed 35C" → 35, "above 100°F" → 37.8 (converted to C)
    """
    import re
    q = question.lower()

    # Celsius patterns: "35c", "35°c", "35 degrees celsius"
    m = re.search(r'(\d+)\s*[°]?\s*c(?:elsius)?', q)
    if m:
        return float(m.group(1))

    # Fahrenheit patterns: "100f", "100°f", "100 degrees fahrenheit"
    m = re.search(r'(\d+)\s*[°]?\s*f(?:ahrenheit)?', q)
    if m:
        f_val = float(m.group(1))
        return (f_val - 32) * 5 / 9  # convert to Celsius

    return None


def extract_date(question: str) -> Optional[str]:
    """
    Extract target date from a market question.
    E.g. "on July 1 2026" → "2026-07-01"
    """
    import re
    from datetime import datetime

    q = question.lower()

    # Common patterns
    patterns = [
        r'(\d{4}-\d{2}-\d{2})',          # ISO format
        r'(\w+ \d{1,2}(?:st|nd|rd|th)?,? \d{4})',  # "July 1, 2026" or "July 1st 2026"
        r'(\d{1,2} \w+ \d{4})',           # "1 July 2026"
        r'(\w+ \d{1,2}(?:st|nd|rd|th)?)',  # "July 1st" (assume current year)
    ]

    for pat in patterns:
        m = re.search(pat, q)
        if m:
            date_str = m.group(1).replace(',', '').replace('st', '').replace('nd', '').replace('rd', '').replace('th', '')
            try:
                for fmt in ['%Y-%m-%d', '%B %d %Y', '%d %B %Y', '%B %d']:
                    try:
                        dt = datetime.strptime(date_str, fmt)
                        if dt.year == 1900:  # no year in pattern
                            dt = dt.replace(year=datetime.now().year)
                        return dt.strftime('%Y-%m-%d')
                    except ValueError:
                        continue
            except:
                pass
    return None


# ── Normal distribution helpers ──────────────────────────────────

def normal_cdf(x: float) -> float:
    """Standard normal CDF using the error function."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def prob_exceed_threshold(forecast: float, threshold: float, sigma: float) -> float:
    """
    Probability that actual temperature exceeds threshold,
    given GFS forecast and error sigma.
    P(temp > threshold) = 1 - Φ((threshold - forecast) / sigma)
    """
    z = (threshold - forecast) / sigma
    return 1.0 - normal_cdf(z)


# ── Open-Meteo API (free, no key, GFS-based) ─────────────────────

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def fetch_gfs_historical_forecast(
    lat: float,
    lon: float,
    target_date: str,
    variables: List[str] = None,
) -> Optional[Dict]:
    """
    Replay what GFS predicted on a past date using the Open-Meteo
    Historical Forecast Archive (coverage: ~2022 onward).

    Endpoint: historical-forecast-api.open-meteo.com/v1/forecast
      ?models=gfs_seamless
      &latitude=...&longitude=...
      &daily=temperature_2m_max,temperature_2m_min
      &start_date=YYYY-MM-DD&end_date=YYYY-MM-DD

    Returns the same dict shape as fetch_gfs_forecast():
      {
        "forecast_temp": float,   # max temperature (°C)
        "forecast_min":  float,
        "forecast_max":  float,
        "units": "°C",
        "source": "GFS historical via Open-Meteo",
      }
    Returns None on any error.
    """
    if variables is None:
        variables = ["temperature_2m_max", "temperature_2m_min"]

    params = {
        "latitude":   lat,
        "longitude":  lon,
        "daily":      variables,
        "models":     "gfs_seamless",
        "start_date": target_date,
        "end_date":   target_date,
        "timezone":   "UTC",
    }

    try:
        resp = requests.get(OPEN_METEO_HISTORICAL_FORECAST_URL, params=params, timeout=15)
        if resp.status_code != 200:
            print(f"[GFS-hist] API error {resp.status_code}: {resp.text[:200]}")
            return None

        data  = resp.json()
        daily = data.get("daily", {})
        dates     = daily.get("time", [])
        temps_max = daily.get("temperature_2m_max", [])
        temps_min = daily.get("temperature_2m_min", [])

        if not dates or not temps_max:
            print(f"[GFS-hist] No data for {target_date} at ({lat}, {lon})")
            return None

        # There should be exactly one row (start_date == end_date)
        if dates[0] != target_date:
            print(f"[GFS-hist] Unexpected date returned: {dates[0]} (wanted {target_date})")
            return None

        t_max = temps_max[0]
        t_min = temps_min[0] if temps_min else t_max

        if t_max is None:
            print(f"[GFS-hist] Null temperature for {target_date}")
            return None

        result: Dict = {
            "forecast_temp": t_max,
            "forecast_min":  t_min,
            "forecast_max":  t_max,
            "units":  "°C",
            "source": f"GFS historical via Open-Meteo (date={target_date})",
        }

        # Attach any extra daily variables the caller requested
        for var in variables:
            if var not in ("temperature_2m_max", "temperature_2m_min"):
                vals = daily.get(var, [None])
                result[var] = vals[0]

        return result

    except Exception as e:
        print(f"[GFS-hist] fetch error: {e}")
        return None


def fetch_observed_temperature(lat: float, lon: float, target_date: str) -> Optional[float]:
    """
    Fetch the ERA5 reanalysis (observed) max temperature for a past date.
    Used as ground truth for backtest calibration.

    Endpoint: archive-api.open-meteo.com/v1/archive
    Coverage: 1940 onward.
    Returns max temperature (°C) or None.
    """
    params = {
        "latitude":   lat,
        "longitude":  lon,
        "daily":      "temperature_2m_max",
        "start_date": target_date,
        "end_date":   target_date,
        "timezone":   "UTC",
    }
    try:
        resp = requests.get(OPEN_METEO_ARCHIVE_URL, params=params, timeout=15)
        if resp.status_code != 200:
            print(f"[ERA5] API error {resp.status_code}")
            return None
        data  = resp.json()
        daily = data.get("daily", {})
        temps = daily.get("temperature_2m_max", [None])
        return temps[0]
    except Exception as e:
        print(f"[ERA5] fetch error: {e}")
        return None


def fetch_gfs_forecast(lat: float, lon: float, target_date: str) -> Optional[Dict]:
    """
    Fetch GFS-based temperature forecast from Open-Meteo.

    Returns dict with:
      - forecast_temp: max temperature on target_date (°C)
      - forecast_min: min temperature
      - forecast_max: max temperature
      - units: '°C'
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": ["temperature_2m_max", "temperature_2m_min"],
        "timezone": "UTC",
        "forecast_days": 16,  # GFS goes out 16 days
    }

    try:
        resp = requests.get(OPEN_METEO_URL, params=params, timeout=10)
        if resp.status_code != 200:
            print(f"[GFS] API error: {resp.status_code}")
            return None

        data = resp.json()
        daily = data.get("daily", {})

        dates = daily.get("time", [])
        temps_max = daily.get("temperature_2m_max", [])
        temps_min = daily.get("temperature_2m_min", [])

        # Find the target date
        for i, d in enumerate(dates):
            if d == target_date:
                return {
                    "forecast_temp": temps_max[i],
                    "forecast_min": temps_min[i],
                    "forecast_max": temps_max[i],
                    "units": "°C",
                    "source": "GFS via Open-Meteo",
                }

        # If target date not found (too far in future), use the last available
        if dates and temps_max:
            return {
                "forecast_temp": temps_max[-1],
                "forecast_min": temps_min[-1],
                "forecast_max": temps_max[-1],
                "units": "°C",
                "source": f"GFS via Open-Meteo (last available: {dates[-1]}, requested: {target_date})",
            }

        print(f"[GFS] no forecast data for {target_date}")
        return None

    except Exception as e:
        print(f"[GFS] fetch error: {e}")
        return None


# ── GFS Weather Prediction Source ────────────────────────────────

class GFSWeatherSource(PredictionSource):
    """
    Prediction source that uses GFS temperature forecasts + sigma error model
    to estimate probabilities for temperature threshold markets.

    sigma = standard deviation of GFS T+1 forecast error (default 0.7°C)
    """

    def __init__(self, sigma: float = 0.7, name: str = "gfs_weather"):
        super().__init__(name)
        self.sigma = sigma
        self.cache: Dict[str, Dict] = {}  # market_id → cached prediction

    def can_predict(self, market: MarketContext) -> bool:
        """Only predict temperature-threshold markets with known cities."""
        q = market.question.lower()
        has_temp = any(kw in q for kw in ['temperature', 'temp', '°c', '°f', 'celsius', 'fahrenheit'])
        if not has_temp:
            return False

        coords = find_city_coords(market.question)
        if not coords:
            return False

        threshold = extract_threshold(market.question)
        if threshold is None:
            return False

        return True

    def predict(self, market: MarketContext) -> Optional[Prediction]:
        if market.market_id in self.cache:
            cached = self.cache[market.market_id]
            if cached["timestamp"] > datetime.utcnow().isoformat()[:10]:
                return cached["prediction"]

        coords = find_city_coords(market.question)
        threshold = extract_threshold(market.question)
        target_date = extract_date(market.question)

        if not coords or threshold is None:
            return None

        lat, lon = coords

        # Fetch GFS forecast
        forecast_data = None
        if target_date:
            forecast_data = fetch_gfs_forecast(lat, lon, target_date)
        else:
            # Try tomorrow
            tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime('%Y-%m-%d')
            forecast_data = fetch_gfs_forecast(lat, lon, tomorrow)

        if not forecast_data:
            # Fallback: if API fails, return None (no prediction)
            return None

        forecast_temp = forecast_data["forecast_temp"]

        # Compute probability
        prob = prob_exceed_threshold(forecast_temp, threshold, self.sigma)

        # Confidence: based on how far the forecast is from threshold relative to sigma
        z = abs(forecast_temp - threshold) / self.sigma
        if z > 2.0:
            confidence = 0.90
        elif z > 1.0:
            confidence = 0.75
        elif z > 0.5:
            confidence = 0.60
        else:
            confidence = 0.50

        # Clamp probability
        prob = max(0.001, min(0.999, prob))

        prediction = Prediction(
            market_id=market.market_id,
            source_name=self.name,
            estimated_probability=prob,
            confidence=confidence,
            reasoning=(
                f"GFS forecast: {forecast_temp:.1f}°C, threshold: {threshold:.0f}°C, "
                f"sigma={self.sigma:.1f}°C, z={(threshold - forecast_temp)/self.sigma:+.1f}"
            ),
            key_factors=[
                f"GFS T+1 forecast: {forecast_temp:.1f}°C",
                f"Threshold: {threshold:.0f}°C",
                f"Model sigma: {self.sigma:.1f}°C",
                f"Probability = 1 - Φ(({threshold:.0f} - {forecast_temp:.1f}) / {self.sigma:.1f})",
            ],
            risks=[
                "GFS forecast error may be larger than 0.7°C for some regions",
                "Model does not account for systematic GFS bias",
                "Threshold markets may have thin liquidity",
            ],
        )

        # Cache
        self.cache[market.market_id] = {
            "prediction": prediction,
            "timestamp": datetime.utcnow().isoformat()[:10],
        }

        self.prediction_count += 1
        return prediction


# ── Demo ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  GFS Weather Prediction Source — Demo")
    print(f"  sigma = 0.7°C")
    print("=" * 60)

    source = GFSWeatherSource(sigma=0.7)

    # Test with a hypothetical Beijing temperature market
    tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime('%Y-%m-%d')
    test_question = f"Will the temperature in Beijing exceed 35C on {tomorrow}?"

    market = MarketContext(
        market_id="test_beijing",
        question=test_question,
        outcomes=["Above 35C", "Below 35C"],
        outcome_prices=[0.30, 0.70],
        volume=5000, liquidity=2000, category="weather",
    )

    print(f"\nMarket: {market.question}")
    print(f"  Market price: {market.outcome_prices[0]:.0%} (YES probability)")

    if source.can_predict(market):
        pred = source.predict(market)
        if pred:
            edge = pred.estimated_probability - market.outcome_prices[0]
            print(f"\nGFS Prediction:")
            print(f"  Your estimate: {pred.estimated_probability:.1%}")
            print(f"  Confidence: {pred.confidence:.0%}")
            print(f"  Reasoning: {pred.reasoning}")
            print(f"\nEdge: {edge:+.1%}")
            if abs(edge) > 0.10:
                print(f"  → SIGNIFICANT EDGE — consider trading!")
            else:
                print(f"  → Edge too small to trade")

            print(f"\nKey factors:")
            for f in pred.key_factors:
                print(f"  • {f}")
        else:
            print("  Could not fetch GFS forecast")
    else:
        print("  Cannot predict this market")

    # Also show the model mechanics
    print(f"\n{'─' * 60}")
    print(f"Model: P(temp > threshold) = 1 - Φ((threshold - forecast) / σ)")
    print(f"σ = {source.sigma}°C")
    print(f"\nExample calculations:")
    for fc, th in [(36, 35), (35, 35), (34, 35), (33, 35), (32, 35)]:
        p = prob_exceed_threshold(fc, th, source.sigma)
        z = (th - fc) / source.sigma
        print(f"  forecast={fc}°C, threshold={th}°C → z={z:+.1f}σ → P(exceed)={p:.1%}")
