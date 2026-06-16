#!/usr/bin/env python3
"""AWS multi-step GFS backfill — downloads 524MB files, extracts HK temp.

Runs ~3 hours for 204 files. Use nohup to background.

Usage:
    nohup python3 -m src.scripts.noaa_aws_backfill &
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import requests
import xarray as xr

PROJECT_ROOT = Path(__file__).parent.parent.parent
GFS_DB = PROJECT_ROOT / "data" / "gfs_forecasts.db"
AWS_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"

HK_LAT, HK_LON = 22.25, 114.25
LEAD_TO_FHOUR = {1: 30, 2: 54, 3: 78}


def download_one(run_str: str, fhour: int) -> float | None:
    url = f"{AWS_BASE}/gfs.{run_str}/00/atmos/gfs.t00z.pgrb2.0p25.f{fhour:03d}"
    r = requests.get(url, timeout=180)
    if r.status_code != 200:
        return None
    tmp = tempfile.NamedTemporaryFile(suffix=".grib2", delete=False)
    tmp.write(r.content)
    tmp.close()
    try:
        ds = xr.open_dataset(
            tmp.name, engine="cfgrib",
            filter_by_keys={"typeOfLevel": "heightAboveGround", "level": 2},
        )
        result = float(ds.t2m.sel(latitude=HK_LAT, longitude=HK_LON, method="nearest").values) - 273.15
        ds.close()
        return round(result, 2)
    finally:
        os.unlink(tmp.name)


def main():
    conn = sqlite3.connect(str(GFS_DB))
    conn.execute("PRAGMA journal_mode=WAL")

    targets = conn.execute(
        """
        SELECT DISTINCT target_date FROM gfs_forecasts
        WHERE location_id='hong_kong_hong_kong' AND variable='temperature_2m_max'
          AND target_date BETWEEN '2026-03-13' AND '2026-05-24'
        ORDER BY target_date
        """
    ).fetchall()
    dates = [r[0] for r in targets]
    total = len(dates) * 3
    print(f"[aws] {len(dates)} target dates, {total} files to fetch")
    print(f"[aws] Estimated: ~{total * 55 / 60:.0f} minutes")

    inserted = 0
    for i, target_str in enumerate(dates):
        target = date.fromisoformat(target_str)
        for lead, fhour in LEAD_TO_FHOUR.items():
            run_date = target - timedelta(days=lead)
            run_str = run_date.strftime("%Y%m%d")

            exists = conn.execute(
                "SELECT 1 FROM gfs_forecasts WHERE location_id=?"
                " AND target_date=? AND forecast_issued=? AND variable=?",
                ("hong_kong_hong_kong", target_str, run_date.isoformat(), "temperature_2m_max"),
            ).fetchone()
            if exists:
                continue

            progress = f"[{i*3 + lead}/{total}]" if lead == 1 else ""
            print(f"  {progress} {target_str} T+{lead}d (run={run_date.isoformat()})", end=" ", flush=True)
            t0 = time.time()
            value = download_one(run_str, fhour)
            elapsed = time.time() - t0

            if value is not None:
                conn.execute(
                    """INSERT INTO gfs_forecasts
                       (location_id, target_date, forecast_issued, lead_time_hours,
                        variable, value, unit, model, source)
                       VALUES (?,?,?,?,?,?,'C','gfs_seamless','noaa-aws')""",
                    ("hong_kong_hong_kong", target_str, run_date.isoformat(), lead * 24,
                     "temperature_2m_max", value),
                )
                conn.commit()
                print(f"{value}C ({elapsed:.0f}s)")
                inserted += 1
            else:
                print(f"FAIL ({elapsed:.0f}s)")

    conn.close()
    print(f"\n[aws] Done. Inserted {inserted} rows.")


if __name__ == "__main__":
    main()
