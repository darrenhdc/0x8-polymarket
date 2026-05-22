"""SQLite schema and helpers for Polymarket weather research data."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
WEATHER_MARKETS_DB = DATA_DIR / "weather_markets.db"
GFS_FORECASTS_DB = DATA_DIR / "gfs_forecasts.db"


def connect(path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with practical defaults for local analytics."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def connect_markets(db_path: Optional[Path] = None) -> sqlite3.Connection:
    return connect(db_path or WEATHER_MARKETS_DB)


def connect_gfs(db_path: Optional[Path] = None) -> sqlite3.Connection:
    return connect(db_path or GFS_FORECASTS_DB)


def init_weather_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS markets (
            id TEXT PRIMARY KEY,
            slug TEXT,
            question TEXT NOT NULL,
            city TEXT,
            country TEXT,
            latitude REAL,
            longitude REAL,
            market_type TEXT,
            threshold_value REAL,
            threshold_unit TEXT,
            target_date TEXT,
            start_date TEXT,
            end_date TEXT,
            active INTEGER,
            closed INTEGER,
            archived INTEGER,
            resolved_outcome TEXT,
            volume REAL,
            liquidity REAL,
            clob_token_ids TEXT,
            raw_json TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS price_history (
            market_id TEXT NOT NULL,
            token_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            price REAL NOT NULL,
            fidelity_minutes INTEGER,
            source TEXT DEFAULT 'clob-prices-history',
            PRIMARY KEY (market_id, token_id, timestamp),
            FOREIGN KEY (market_id) REFERENCES markets(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_markets_city_date
            ON markets(city, target_date);
        CREATE INDEX IF NOT EXISTS idx_markets_type
            ON markets(market_type);
        CREATE INDEX IF NOT EXISTS idx_price_history_market_time
            ON price_history(market_id, timestamp);
        """
    )
    conn.commit()


def init_gfs_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS locations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            country TEXT,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            timezone TEXT,
            source TEXT,
            raw_json TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS gfs_forecasts (
            location_id TEXT NOT NULL,
            target_date TEXT NOT NULL,
            forecast_issued TEXT NOT NULL,
            lead_time_hours INTEGER NOT NULL,
            variable TEXT NOT NULL,
            value REAL,
            unit TEXT,
            model TEXT DEFAULT 'gfs_seamless',
            source TEXT DEFAULT 'open-meteo-historical-forecast',
            raw_json TEXT,
            PRIMARY KEY (location_id, target_date, forecast_issued, variable),
            FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS observed_weather (
            location_id TEXT NOT NULL,
            target_date TEXT NOT NULL,
            variable TEXT NOT NULL,
            value REAL,
            unit TEXT,
            source TEXT DEFAULT 'open-meteo-archive',
            raw_json TEXT,
            PRIMARY KEY (location_id, target_date, variable),
            FOREIGN KEY (location_id) REFERENCES locations(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_gfs_location_date
            ON gfs_forecasts(location_id, target_date);
        CREATE INDEX IF NOT EXISTS idx_observed_location_date
            ON observed_weather(location_id, target_date);
        """
    )
    conn.commit()


def init_all(weather_db: Optional[Path] = None, gfs_db: Optional[Path] = None) -> None:
    with connect_markets(weather_db) as market_conn:
        init_weather_db(market_conn)
    with connect_gfs(gfs_db) as gfs_conn:
        init_gfs_db(gfs_conn)


def rows(conn: sqlite3.Connection, query: str, params: Iterable = ()) -> list[sqlite3.Row]:
    return list(conn.execute(query, tuple(params)))
