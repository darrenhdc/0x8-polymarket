"""Open-Meteo GFS historical forecast and observed weather backfill."""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from typing import Optional

import requests

from .database import connect_gfs, connect_markets, init_gfs_db, init_weather_db
from .geocoding import Location, normalize_location_id


OPEN_METEO_HISTORICAL_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


MARKET_VARIABLES = {
    "temp_above": ("temperature_2m_max", "C"),
    "precip": ("precipitation_sum", "mm"),
    "snow": ("snowfall_sum", "cm"),
}


class GFSHistoryCollector:
    def __init__(self, gfs_db_path=None, market_db_path=None, session: Optional[requests.Session] = None):
        self.gfs_conn = connect_gfs(gfs_db_path)
        self.market_conn = connect_markets(market_db_path)
        init_gfs_db(self.gfs_conn)
        init_weather_db(self.market_conn)
        self.session = session or requests.Session()

    def close(self) -> None:
        self.gfs_conn.close()
        self.market_conn.close()

    def upsert_location(self, location: Location) -> None:
        self.gfs_conn.execute(
            """
            INSERT INTO locations (
                id, name, country, latitude, longitude, timezone, source, raw_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                country=excluded.country,
                latitude=excluded.latitude,
                longitude=excluded.longitude,
                timezone=excluded.timezone,
                source=excluded.source,
                raw_json=excluded.raw_json,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                location.id,
                location.name,
                location.country,
                location.latitude,
                location.longitude,
                location.timezone,
                location.source,
                location.raw_json,
            ),
        )

    def backfill_from_markets(
        self,
        *,
        start_date: str,
        end_date: str,
        limit_markets: Optional[int] = None,
        sleep_seconds: float = 0.2,
    ) -> dict[str, int]:
        market_query = """
            SELECT id, city, country, latitude, longitude, market_type, target_date
            FROM markets
            WHERE city IS NOT NULL
              AND latitude IS NOT NULL
              AND longitude IS NOT NULL
              AND target_date IS NOT NULL
              AND market_type IN ('temp_above', 'precip', 'snow')
            ORDER BY target_date
        """
        if limit_markets:
            market_query += f" LIMIT {int(limit_markets)}"

        forecast_rows = 0
        observed_rows = 0
        seen_forecasts: set[tuple[str, str, str, str]] = set()
        seen_observed: set[tuple[str, str, str]] = set()

        for market in self.market_conn.execute(market_query):
            location = Location(
                id=normalize_location_id(market["city"], market["country"]),
                name=market["city"],
                country=market["country"],
                latitude=float(market["latitude"]),
                longitude=float(market["longitude"]),
                source="polymarket-market",
            )
            self.upsert_location(location)

            variable, unit = MARKET_VARIABLES[market["market_type"]]
            target_date = market["target_date"]
            price_dates = self._price_dates(market["id"], start_date, end_date)
            if not price_dates:
                price_dates = [target_date]

            forecast = self.fetch_historical_forecast(
                location.latitude,
                location.longitude,
                target_date=target_date,
                variable=variable,
            )
            if forecast is not None:
                for issued_date in price_dates:
                    key = (location.id, target_date, issued_date, variable)
                    if key in seen_forecasts:
                        continue
                    seen_forecasts.add(key)
                    forecast_rows += self.insert_forecast(
                        location_id=location.id,
                        target_date=target_date,
                        forecast_issued=issued_date,
                        variable=variable,
                        value=forecast,
                        unit=unit,
                        raw_json=None,
                    )

            observed = self.fetch_observed_weather(
                location.latitude,
                location.longitude,
                target_date=target_date,
                variable=variable,
            )
            obs_key = (location.id, target_date, variable)
            if observed is not None and obs_key not in seen_observed:
                seen_observed.add(obs_key)
                observed_rows += self.insert_observed(
                    location_id=location.id,
                    target_date=target_date,
                    variable=variable,
                    value=observed,
                    unit=unit,
                    raw_json=None,
                )

            self.gfs_conn.commit()
            time.sleep(sleep_seconds)

        return {"forecasts": forecast_rows, "observed": observed_rows}

    def _price_dates(self, market_id: str, start_date: str, end_date: str) -> list[str]:
        return [
            row["date"]
            for row in self.market_conn.execute(
                """
                SELECT DISTINCT substr(timestamp, 1, 10) AS date
                FROM price_history
                WHERE market_id = ?
                  AND substr(timestamp, 1, 10) BETWEEN ? AND ?
                ORDER BY date
                """,
                (market_id, start_date, end_date),
            )
        ]

    def fetch_historical_forecast(
        self,
        latitude: float,
        longitude: float,
        *,
        target_date: str,
        variable: str,
    ) -> Optional[float]:
        return self._fetch_daily_value(
            OPEN_METEO_HISTORICAL_FORECAST_URL,
            latitude,
            longitude,
            target_date=target_date,
            variable=variable,
            extra_params={"models": "gfs_seamless"},
        )

    def fetch_historical_forecast_batch(
        self,
        latitude: float,
        longitude: float,
        *,
        start_date: str,
        end_date: str,
        variable: str,
    ) -> dict[str, float]:
        """Fetch GFS historical forecasts for an entire date range in one API call.

        Returns {date_str: value} for each date in [start_date, end_date].
        Each value is the best-available GFS forecast for that calendar date
        (approximately the prior-day model run, ~24h lead time).
        """
        return self._fetch_daily_range(
            OPEN_METEO_HISTORICAL_FORECAST_URL,
            latitude,
            longitude,
            start_date=start_date,
            end_date=end_date,
            variable=variable,
            extra_params={"models": "gfs_seamless"},
        )

    def fetch_observed_weather_batch(
        self,
        latitude: float,
        longitude: float,
        *,
        start_date: str,
        end_date: str,
        variable: str,
    ) -> dict[str, float]:
        """Fetch ERA5 observed weather for an entire date range in one API call."""
        return self._fetch_daily_range(
            OPEN_METEO_ARCHIVE_URL,
            latitude,
            longitude,
            start_date=start_date,
            end_date=end_date,
            variable=variable,
            extra_params={},
        )

    def backfill_batch(
        self,
        *,
        start_date: str,
        end_date: str,
        variables: Optional[list[str]] = None,
        sleep_seconds: float = 0.3,
    ) -> dict[str, int]:
        """Efficient batch backfill: one API call per location × variable covers all dates.

        Discovers all unique locations from the markets DB, then fetches the
        full date range in a single Open-Meteo call per location per variable.
        This is O(locations × variables) instead of O(markets × dates).
        """
        if variables is None:
            variables = list(MARKET_VARIABLES.values())  # [("temperature_2m_max","C"), ...]
            variables = [v for v, _ in MARKET_VARIABLES.values()]
            variables = list({v for v, _ in MARKET_VARIABLES.values()})
            if "temperature_2m_min" not in variables:
                variables.append("temperature_2m_min")

        locations_query = """
            SELECT DISTINCT city, country, latitude, longitude
            FROM markets
            WHERE city IS NOT NULL
              AND latitude IS NOT NULL
              AND longitude IS NOT NULL
              AND market_type IN ('temp_above', 'precip', 'snow')
        """
        locations = list(self.market_conn.execute(locations_query))
        if not locations:
            return {"forecasts": 0, "observed": 0}

        forecast_rows = 0
        observed_rows = 0

        for loc_row in locations:
            location = Location(
                id=normalize_location_id(loc_row["city"], loc_row["country"]),
                name=loc_row["city"],
                country=loc_row["country"],
                latitude=float(loc_row["latitude"]),
                longitude=float(loc_row["longitude"]),
                source="polymarket-market",
            )
            self.upsert_location(location)

            # Collect all (variable, unit) pairs from market types + extra variables
            var_units = list(MARKET_VARIABLES.values())
            for extra_var in variables:
                if extra_var not in [v for v, _ in var_units]:
                    var_units.append((extra_var, "C"))

            for variable, unit in var_units:
                # GFS historical forecasts
                forecast_values = self.fetch_historical_forecast_batch(
                    location.latitude,
                    location.longitude,
                    start_date=start_date,
                    end_date=end_date,
                    variable=variable,
                )
                for target_date, value in forecast_values.items():
                    forecast_rows += self.insert_forecast(
                        location_id=location.id,
                        target_date=target_date,
                        forecast_issued=target_date,  # same-day = ~1-day-ahead via API
                        variable=variable,
                        value=value,
                        unit=unit,
                        raw_json=None,
                    )
                time.sleep(sleep_seconds)

                # ERA5 observed weather (only for dates in the past)
                from datetime import date as _date
                today = _date.today().isoformat()
                obs_end = min(end_date, today)
                if obs_end >= start_date:
                    observed_values = self.fetch_observed_weather_batch(
                        location.latitude,
                        location.longitude,
                        start_date=start_date,
                        end_date=obs_end,
                        variable=variable,
                    )
                    for target_date, value in observed_values.items():
                        observed_rows += self.insert_observed(
                            location_id=location.id,
                            target_date=target_date,
                            variable=variable,
                            value=value,
                            unit=unit,
                            raw_json=None,
                        )
                time.sleep(sleep_seconds)

            self.gfs_conn.commit()

        return {"forecasts": forecast_rows, "observed": observed_rows}



    def fetch_observed_weather(
        self,
        latitude: float,
        longitude: float,
        *,
        target_date: str,
        variable: str,
    ) -> Optional[float]:
        return self._fetch_daily_value(
            OPEN_METEO_ARCHIVE_URL,
            latitude,
            longitude,
            target_date=target_date,
            variable=variable,
            extra_params={},
        )

    def _fetch_daily_range(
        self,
        url: str,
        latitude: float,
        longitude: float,
        *,
        start_date: str,
        end_date: str,
        variable: str,
        extra_params: dict,
    ) -> dict[str, float]:
        """Fetch a variable for every day in [start_date, end_date]. Returns {date: value}."""
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "daily": variable,
            "start_date": start_date,
            "end_date": end_date,
            "timezone": "auto",
        }
        params.update(extra_params)
        try:
            resp = self.session.get(url, params=params, timeout=60)
            if resp.status_code != 200:
                return {}
            data = resp.json()
            times = data.get("daily", {}).get("time", [])
            values = data.get("daily", {}).get(variable, [])
            return {
                t: float(v)
                for t, v in zip(times, values)
                if v is not None
            }
        except (requests.RequestException, ValueError, TypeError):
            return {}

    def _fetch_daily_value(
        self,
        url: str,
        latitude: float,
        longitude: float,
        *,
        target_date: str,
        variable: str,
        extra_params: dict,
    ) -> Optional[float]:
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "daily": variable,
            "start_date": target_date,
            "end_date": target_date,
            "timezone": "auto",
        }
        params.update(extra_params)
        try:
            resp = self.session.get(url, params=params, timeout=30)
            if resp.status_code != 200:
                return None
            data = resp.json()
            values = data.get("daily", {}).get(variable, [])
            if not values or values[0] is None:
                return None
            return float(values[0])
        except (requests.RequestException, ValueError, TypeError):
            return None

    def insert_forecast(
        self,
        *,
        location_id: str,
        target_date: str,
        forecast_issued: str,
        variable: str,
        value: float,
        unit: str,
        raw_json: Optional[dict],
    ) -> int:
        before = self.gfs_conn.total_changes
        lead_time_hours = _lead_time_hours(forecast_issued, target_date)
        self.gfs_conn.execute(
            """
            INSERT OR IGNORE INTO gfs_forecasts (
                location_id, target_date, forecast_issued, lead_time_hours,
                variable, value, unit, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                location_id,
                target_date,
                forecast_issued,
                lead_time_hours,
                variable,
                value,
                unit,
                json.dumps(raw_json, ensure_ascii=False) if raw_json else None,
            ),
        )
        return self.gfs_conn.total_changes - before

    def insert_observed(
        self,
        *,
        location_id: str,
        target_date: str,
        variable: str,
        value: float,
        unit: str,
        raw_json: Optional[dict],
    ) -> int:
        before = self.gfs_conn.total_changes
        self.gfs_conn.execute(
            """
            INSERT OR IGNORE INTO observed_weather (
                location_id, target_date, variable, value, unit, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                location_id,
                target_date,
                variable,
                value,
                unit,
                json.dumps(raw_json, ensure_ascii=False) if raw_json else None,
            ),
        )
        return self.gfs_conn.total_changes - before


def _lead_time_hours(forecast_issued: str, target_date: str) -> int:
    issued = date.fromisoformat(forecast_issued[:10])
    target = date.fromisoformat(target_date[:10])
    return int((target - issued).days * 24)
