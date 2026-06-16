#!/usr/bin/env python3
"""Backfill multi-step GFS from NOAA NOMADS filter (11KB per file).

For HK target dates in recent 10-day window, downloads T+1/T+2/T+3
2m temperature forecasts using NOMADS grib filter (much smaller files).

Usage: python3 -m src.scripts.noaa_multi_step_backfill
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import requests
import xarray as xr

PROJECT_ROOT = Path(__file__).parent.parent.parent
GFS_DB = PROJECT_ROOT / "data" / "gfs_forecasts.db"

HK_LAT = 22.32
HK_LON = 114.17

# NOMADS grib filter (subregion extraction → ~11KB per file)
FILTER_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"

# Lead days → forecast hours (00Z run, targeting 06Z valid time)
LEAD_TO_FHOUR = {1: 30, 2: 54, 3: 78}
TARGET_VARIABLE = "temperature_2m_max"


def _fetch_temp(run_date_str: str, forecast_hour: int) -> Optional[float]:
    """Download filtered GFS 2m temperature at HK coordinates."""
    params = {
        "dir": f"/gfs.{run_date_str}/00/atmos",
        "file": f"gfs.t00z.pgrb2.0p25.f{forecast_hour:03d}",
        "var_TMP": "on",
        "subregion": "",
        "toplat": "23",
        "bottomlat": "22",
        "leftlon": "114",
        "rightlon": "115",
    }
    for attempt in range(2):
        try:
            r = requests.get(FILTER_URL, params=params, timeout=120)
            if r.status_code != 200:
                continue
            with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as f:
                f.write(r.content)
                tmp = f.name
            ds = xr.open_dataset(
                tmp,
                engine="cfgrib",
                filter_by_keys={"typeOfLevel": "heightAboveGround", "level": 2},
            )
            t_k = float(ds.t2m.sel(latitude=HK_LAT, longitude=HK_LON, method="nearest").values)
            ds.close()
            os.unlink(tmp)
            return round(t_k - 273.15, 2)
        except Exception:
            time.sleep(1)
    return None


def backfill_recent(
    location_id: str = "hong_kong_hong_kong",
    lookback_days: int = 10,
    lead_days: list[int] | None = None,
    sleep: float = 0.3,
) -> int:
    """Backfill T+1/T+2/T+3 for recent dates available on NOMADS."""
    if lead_days is None:
        lead_days = [1, 2, 3]

    today = date.today()
    conn = sqlite3.connect(str(GFS_DB))
    conn.execute("PRAGMA journal_mode=WAL")

    # Get target dates from DB that fall within NOMADS window
    min_date = (today - timedelta(days=lookback_days)).isoformat()
    max_date = today.isoformat()
    targets = conn.execute(
        """
        SELECT DISTINCT target_date FROM gfs_forecasts
        WHERE location_id = ? AND variable = 'temperature_2m_max'
          AND target_date BETWEEN ? AND ?
        ORDER BY target_date
        """,
        (location_id, min_date, max_date),
    ).fetchall()

    dates = [r[0] for r in targets]
    print(f"[noaa] Target dates in window: {len(dates)} ({dates[0]} → {dates[-1]})")

    inserted = 0
    for target_str in dates:
        target = date.fromisoformat(target_str)
        for lead in lead_days:
            run_date = target - timedelta(days=lead)
            run_str = run_date.strftime("%Y%m%d")
            fhour = LEAD_TO_FHOUR[lead]

            # Check if already exists
            exists = conn.execute(
                """
                SELECT 1 FROM gfs_forecasts
                WHERE location_id=? AND target_date=? AND forecast_issued=? AND variable=?
                """,
                (location_id, target_str, run_date.isoformat(), TARGET_VARIABLE),
            ).fetchone()

            if exists:
                continue

            print(f"  {target_str} T+{lead}d (run={run_date.isoformat()}, f{fhour:03d})", end=" ")
            value = _fetch_temp(run_str, fhour)
            if value is not None:
                conn.execute(
                    """
                    INSERT INTO gfs_forecasts
                        (location_id, target_date, forecast_issued, lead_time_hours,
                         variable, value, unit, model, source)
                    VALUES (?, ?, ?, ?, ?, ?, 'C', 'gfs_seamless', 'noaa-nomads-filter')
                    """,
                    (location_id, target_str, run_date.isoformat(), lead * 24, TARGET_VARIABLE, value),
                )
                conn.commit()
                print(f"{value}C ✓")
                inserted += 1
            else:
                print("FAIL")
            time.sleep(sleep)

    conn.close()
    print(f"\n[noaa] Inserted {inserted} T+1/T+2/T+3 rows")
    return inserted


if __name__ == "__main__":
    backfill_recent()
