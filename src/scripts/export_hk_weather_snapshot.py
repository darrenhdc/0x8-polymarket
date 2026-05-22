"""Export a Hong Kong weather research snapshot for the A05 project page."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.data.database import connect_gfs, connect_markets
from src.data.geocoding import normalize_location_id
from src.data.gfs_history import MARKET_VARIABLES
from src.data.weather_backtester import convert_threshold


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WEBSITE_DATA_DIR = PROJECT_ROOT.parents[0] / "website" / "data"

SIGMA_BY_TYPE = {
    "temp_above": 0.7,
    "precip": 8.0,
    "snow": 2.0,
}


def parse_args():
    parser = argparse.ArgumentParser(description="Export Hong Kong weather coverage and backtest snapshot.")
    parser.add_argument("--city", default="Hong Kong")
    parser.add_argument("--country", default="Hong Kong")
    parser.add_argument("--out-dir", default=str(DEFAULT_WEBSITE_DATA_DIR))
    parser.add_argument("--json-name", default="a05_hk_weather.json")
    parser.add_argument("--js-name", default="a05_hk_weather.js")
    parser.add_argument("--sample-limit", type=int, default=8)
    parser.add_argument("--backtest-limit", type=int, default=16)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with connect_markets() as market_conn, connect_gfs() as gfs_conn:
        snapshot = build_snapshot(
            market_conn=market_conn,
            gfs_conn=gfs_conn,
            city=args.city,
            country=args.country,
            sample_limit=args.sample_limit,
            backtest_limit=args.backtest_limit,
        )

    json_path = out_dir / args.json_name
    js_path = out_dir / args.js_name
    payload = json.dumps(snapshot, ensure_ascii=False, indent=2)
    json_path.write_text(payload + "\n", encoding="utf-8")
    js_path.write_text("window.A05_HK_WEATHER_DATA = " + payload + ";\n", encoding="utf-8")

    print(f"json: {json_path}")
    print(f"js:   {js_path}")
    print(
        "summary: "
        f"markets={snapshot['summary']['markets']} "
        f"prices={snapshot['summary']['price_rows']} "
        f"gfs={snapshot['summary']['gfs_forecast_rows']} "
        f"observed={snapshot['summary']['observed_rows']} "
        f"backtest_rows={len(snapshot['backtest_rows'])}"
    )


def build_snapshot(*, market_conn, gfs_conn, city: str, country: str, sample_limit: int, backtest_limit: int) -> dict:
    location_id = normalize_location_id(city, country)
    summary = market_conn.execute(
        """
        SELECT
            COUNT(*) AS markets,
            MIN(target_date) AS min_target_date,
            MAX(target_date) AS max_target_date,
            COUNT(DISTINCT target_date) AS target_dates,
            SUM(CASE WHEN market_type = 'temp_above' THEN 1 ELSE 0 END) AS temperature_markets,
            SUM(CASE WHEN market_type = 'precip' THEN 1 ELSE 0 END) AS precipitation_markets,
            SUM(CASE WHEN market_type = 'snow' THEN 1 ELSE 0 END) AS snow_markets,
            SUM(CASE WHEN resolved_outcome IS NOT NULL AND resolved_outcome != '' THEN 1 ELSE 0 END) AS resolved_markets
        FROM markets
        WHERE lower(city) = lower(?)
        """,
        (city,),
    ).fetchone()

    price_summary = market_conn.execute(
        """
        SELECT
            COUNT(*) AS price_rows,
            COUNT(DISTINCT p.market_id) AS markets_with_prices,
            COUNT(DISTINCT substr(p.timestamp, 1, 10)) AS price_dates,
            MIN(substr(p.timestamp, 1, 10)) AS min_price_date,
            MAX(substr(p.timestamp, 1, 10)) AS max_price_date
        FROM price_history p
        JOIN markets m ON m.id = p.market_id
        WHERE lower(m.city) = lower(?)
        """,
        (city,),
    ).fetchone()

    gfs_summary = gfs_conn.execute(
        """
        SELECT
            COUNT(*) AS gfs_forecast_rows,
            COUNT(DISTINCT target_date) AS gfs_target_dates,
            MIN(target_date) AS min_gfs_target_date,
            MAX(target_date) AS max_gfs_target_date,
            MIN(forecast_issued) AS min_forecast_issued,
            MAX(forecast_issued) AS max_forecast_issued,
            COUNT(DISTINCT variable) AS gfs_variables
        FROM gfs_forecasts
        WHERE location_id = ?
        """,
        (location_id,),
    ).fetchone()

    observed_summary = gfs_conn.execute(
        """
        SELECT
            COUNT(*) AS observed_rows,
            COUNT(DISTINCT target_date) AS observed_target_dates,
            MIN(target_date) AS min_observed_date,
            MAX(target_date) AS max_observed_date
        FROM observed_weather
        WHERE location_id = ?
        """,
        (location_id,),
    ).fetchone()

    coverage_rows = [
        dict(row)
        for row in market_conn.execute(
            """
            SELECT
                target_date,
                market_type,
                COUNT(*) AS markets,
                SUM(CASE WHEN resolved_outcome IS NOT NULL AND resolved_outcome != '' THEN 1 ELSE 0 END) AS resolved_markets
            FROM markets
            WHERE lower(city) = lower(?)
              AND target_date IS NOT NULL
            GROUP BY target_date, market_type
            ORDER BY target_date, market_type
            """,
            (city,),
        )
    ]

    weather_status = load_weather_status(gfs_conn, location_id)
    coverage = [format_coverage_row(row, weather_status) for row in coverage_rows]
    samples = load_sample_markets(market_conn, city, sample_limit)
    backtest_rows = load_backtest_rows(market_conn, gfs_conn, city, country, location_id, backtest_limit)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "city": city,
        "country": country,
        "location_id": location_id,
        "summary": {
            "markets": _int(summary["markets"]),
            "price_rows": _int(price_summary["price_rows"]),
            "gfs_forecast_rows": _int(gfs_summary["gfs_forecast_rows"]),
            "observed_rows": _int(observed_summary["observed_rows"]),
            "target_dates": _int(summary["target_dates"]),
            "markets_with_prices": _int(price_summary["markets_with_prices"]),
            "price_dates": _int(price_summary["price_dates"]),
            "gfs_target_dates": _int(gfs_summary["gfs_target_dates"]),
            "observed_target_dates": _int(observed_summary["observed_target_dates"]),
            "resolved_markets": _int(summary["resolved_markets"]),
            "temperature_markets": _int(summary["temperature_markets"]),
            "precipitation_markets": _int(summary["precipitation_markets"]),
            "snow_markets": _int(summary["snow_markets"]),
        },
        "spans": {
            "market_targets": [_value(summary["min_target_date"]), _value(summary["max_target_date"])],
            "price_dates": [_value(price_summary["min_price_date"]), _value(price_summary["max_price_date"])],
            "gfs_targets": [_value(gfs_summary["min_gfs_target_date"]), _value(gfs_summary["max_gfs_target_date"])],
            "forecast_issued": [_value(gfs_summary["min_forecast_issued"]), _value(gfs_summary["max_forecast_issued"])],
            "observed_dates": [_value(observed_summary["min_observed_date"]), _value(observed_summary["max_observed_date"])],
        },
        "coverage": coverage,
        "sample_markets": samples,
        "backtest_rows": backtest_rows,
        "notes": [
            "页面数据由本地 SQLite 导出生成，不再手写核心统计数字。",
            "回测只使用历史市场价格、历史天气预报和已发生目标日期的实况天气。",
            "等更多目标日期完成后，实况天气和可结算市场数量会自动增加。",
        ],
    }


def load_weather_status(gfs_conn, location_id: str) -> dict[tuple[str, str], dict]:
    status = {}
    for row in gfs_conn.execute(
        """
        SELECT target_date, variable, COUNT(*) AS forecast_rows, MIN(forecast_issued) AS first_issue,
               MAX(forecast_issued) AS last_issue
        FROM gfs_forecasts
        WHERE location_id = ?
        GROUP BY target_date, variable
        """,
        (location_id,),
    ):
        status[(row["target_date"], row["variable"])] = dict(row)
    for row in gfs_conn.execute(
        """
        SELECT target_date, variable, value, unit
        FROM observed_weather
        WHERE location_id = ?
        """,
        (location_id,),
    ):
        item = status.setdefault((row["target_date"], row["variable"]), {})
        item["observed_value"] = row["value"]
        item["observed_unit"] = row["unit"]
    return status


def format_coverage_row(row: dict, weather_status: dict[tuple[str, str], dict]) -> dict:
    variable, _unit = MARKET_VARIABLES.get(row["market_type"], ("unknown", ""))
    status = weather_status.get((row["target_date"], variable), {})
    forecast_rows = _int(status.get("forecast_rows"))
    observed_value = status.get("observed_value")
    observed_unit = status.get("observed_unit")
    if observed_value is not None:
        weather_text = f"GFS {forecast_rows} 条，实况 {observed_value:g} {observed_unit}"
    elif forecast_rows:
        weather_text = f"GFS {forecast_rows} 条，等待实况"
    else:
        weather_text = "等待 GFS 和实况"
    return {
        "target_date": row["target_date"],
        "market_type": market_type_label(row["market_type"]),
        "markets": _int(row["markets"]),
        "resolved_markets": _int(row["resolved_markets"]),
        "weather_status": weather_text,
    }


def load_sample_markets(market_conn, city: str, limit: int) -> list[dict]:
    return [
        {
            "question": row["question"],
            "type": market_type_label(row["market_type"]),
            "threshold": format_threshold(row["threshold_value"], row["threshold_unit"]),
            "target_date": row["target_date"],
        }
        for row in market_conn.execute(
            """
            SELECT question, market_type, threshold_value, threshold_unit, target_date
            FROM markets
            WHERE lower(city) = lower(?)
            ORDER BY target_date, market_type, threshold_value
            LIMIT ?
            """,
            (city, limit),
        )
    ]


def load_backtest_rows(market_conn, gfs_conn, city: str, country: str, location_id: str, limit: int) -> list[dict]:
    rows = []
    for market in market_conn.execute(
        """
        SELECT id, question, market_type, threshold_value, threshold_unit, target_date, resolved_outcome
        FROM markets
        WHERE lower(city) = lower(?)
          AND market_type IN ('temp_above', 'precip', 'snow')
          AND threshold_value IS NOT NULL
          AND target_date IS NOT NULL
        ORDER BY
          CASE WHEN resolved_outcome IS NOT NULL AND resolved_outcome != '' THEN 0 ELSE 1 END,
          target_date,
          market_type,
          threshold_value
        LIMIT ?
        """,
        (city, limit),
    ):
        variable, unit = MARKET_VARIABLES[market["market_type"]]
        threshold = convert_threshold(market["threshold_value"], market["threshold_unit"], variable)
        latest_price = market_conn.execute(
            """
            SELECT substr(timestamp, 1, 10) AS price_date, price
            FROM price_history
            WHERE market_id = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (market["id"],),
        ).fetchone()
        forecast = latest_forecast(gfs_conn, location_id, market["target_date"], variable)
        observed = gfs_conn.execute(
            """
            SELECT value, unit
            FROM observed_weather
            WHERE location_id = ? AND target_date = ? AND variable = ?
            """,
            (location_id, market["target_date"], variable),
        ).fetchone()
        rule = infer_rule(market["question"], market["market_type"])
        forecast_value = float(forecast["value"]) if forecast and forecast["value"] is not None else None
        observed_value = float(observed["value"]) if observed and observed["value"] is not None else None
        market_yes_price = float(latest_price["price"]) if latest_price else None
        model_yes = (
            model_probability(forecast_value, threshold, market["market_type"], rule)
            if forecast_value is not None
            else None
        )
        actual_yes = actual_from_market_or_weather(market["resolved_outcome"], observed_value, threshold, rule)
        edge = model_yes - market_yes_price if model_yes is not None and market_yes_price is not None else None
        rows.append(
            {
                "question": market["question"],
                "target_date": market["target_date"],
                "type": market_type_label(market["market_type"]),
                "rule": rule_label(rule),
                "threshold": format_threshold(threshold, unit),
                "latest_price_date": latest_price["price_date"] if latest_price else None,
                "market_yes_price": _round(market_yes_price),
                "forecast_value": _round(forecast_value),
                "observed_value": _round(observed_value),
                "model_yes_probability": _round(model_yes),
                "edge": _round(edge),
                "actual_yes": actual_yes,
                "status": "已结算" if actual_yes is not None else "等待实况/结算",
            }
        )
    return rows


def latest_forecast(gfs_conn, location_id: str, target_date: str, variable: str):
    return gfs_conn.execute(
        """
        SELECT value, unit, forecast_issued
        FROM gfs_forecasts
        WHERE location_id = ? AND target_date = ? AND variable = ?
        ORDER BY forecast_issued DESC
        LIMIT 1
        """,
        (location_id, target_date, variable),
    ).fetchone()


def infer_rule(question: str, market_type: str) -> str:
    q = question.lower()
    if any(text in q for text in ("or below", "or less", "less than", "below")):
        return "lte"
    if any(text in q for text in ("or higher", "or above", "greater than", "more than", "above")):
        return "gte"
    if market_type in ("precip", "snow"):
        return "gte"
    return "eq"


def model_probability(forecast_value: float, threshold: float, market_type: str, rule: str) -> float:
    sigma = SIGMA_BY_TYPE.get(market_type, 1.0)
    if rule == "lte":
        return _normal_cdf((threshold - forecast_value) / sigma)
    if rule == "gte":
        return 1.0 - _normal_cdf((threshold - forecast_value) / sigma)
    lower = _normal_cdf(((threshold - 0.5) - forecast_value) / sigma)
    upper = _normal_cdf(((threshold + 0.5) - forecast_value) / sigma)
    return max(0.0, upper - lower)


def actual_from_market_or_weather(resolved_outcome, observed_value: Optional[float], threshold: float, rule: str):
    if resolved_outcome:
        value = str(resolved_outcome).strip().lower()
        if value in ("yes", "true", "1"):
            return True
        if value in ("no", "false", "0"):
            return False
    if observed_value is None:
        return None
    if rule == "lte":
        return observed_value <= threshold
    if rule == "gte":
        return observed_value >= threshold
    return math.floor(observed_value) == math.floor(threshold)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def market_type_label(value: Optional[str]) -> str:
    return {
        "temp_above": "气温",
        "precip": "降水",
        "snow": "降雪",
        "storm": "风暴",
    }.get(value or "", value or "未知")


def rule_label(value: str) -> str:
    return {
        "lte": "小于等于",
        "gte": "大于等于",
        "eq": "等于/区间",
    }.get(value, value)


def format_threshold(value, unit) -> str:
    if value is None:
        return ""
    try:
        numeric = float(value)
        text = f"{numeric:g}"
    except (TypeError, ValueError):
        text = str(value)
    return f"{text} {unit or ''}".strip()


def _int(value) -> int:
    return int(value or 0)


def _round(value: Optional[float]) -> Optional[float]:
    return round(value, 4) if value is not None else None


def _value(value):
    return value if value not in ("", None) else None


if __name__ == "__main__":
    main()
