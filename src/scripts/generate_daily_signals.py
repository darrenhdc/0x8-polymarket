"""Daily signal generator for Polymarket weather markets.

Queries active unresolved markets from weather_markets.db, runs them through
a pluggable PredictionSource (default: GFSPredictionSource(mode="live")),
computes edges via EdgeComposer, and writes:
  - data/daily_signals.csv      — today's full signal table
  - data/signal_history.db      — append-only signal log (SQLite)

Usage::
    python3 -m src.scripts.generate_daily_signals              # all cities, GFS default
    python3 -m src.scripts.generate_daily_signals --city "Hong Kong"

Or from cli.py::
    python3 cli.py signals
    python3 cli.py signals --city hong-kong
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from src.data.database import connect_markets, init_weather_db
from src.data.edge_composer import compute_edge
from src.data.geocoding import normalize_location_id
from src.data.gfs_history import MARKET_VARIABLES
from src.data.prediction_interface import MarketContext, PredictionSource

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent.parent
MARKET_DB    = PROJECT_ROOT / "data" / "weather_markets.db"
SIGNALS_CSV  = PROJECT_ROOT / "data" / "daily_signals.csv"
HISTORY_DB   = PROJECT_ROOT / "data" / "signal_history.db"

MIN_EDGE = 0.05
_INFER_RULE_MAP = {
    "or below": "lte", "or less": "lte", "less than": "lte", "below": "lte",
    "or higher": "gte", "or above": "gte", "greater than": "gte",
    "more than": "gte", "above": "gte",
}


def _infer_rule(question: str, market_type: str) -> str:
    q = question.lower()
    for phrase, rule in _INFER_RULE_MAP.items():
        if phrase in q:
            return rule
    if market_type in ("precip", "snow"):
        return "gte"
    return "eq"


def _infer_variable(question: str, market_type: str) -> str:
    if market_type in ("precip", "snow"):
        return MARKET_VARIABLES[market_type][0]
    q = question.lower()
    if "lowest" in q or "minimum" in q:
        return "temperature_2m_min"
    return "temperature_2m_max"


# ---------------------------------------------------------------------------
# Signal history DB
# ---------------------------------------------------------------------------

def _init_history_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_history (
            run_date        TEXT NOT NULL,
            target_date     TEXT NOT NULL,
            question        TEXT,
            threshold_value REAL,
            variable        TEXT,
            gfs_raw         REAL,
            gfs_corrected   REAL,
            calib_bias      REAL,
            calib_sigma     REAL,
            calib_n         INTEGER,
            model_prob      REAL,
            market_price    REAL,
            edge            REAL,
            direction       TEXT,
            flagged         INTEGER DEFAULT 0,
            PRIMARY KEY (run_date, target_date, question)
        )
        """
    )
    conn.commit()


def _upsert_signals(
    conn: sqlite3.Connection, run_date: str, signals: list[dict]
) -> None:
    conn.executemany(
        """
        INSERT OR REPLACE INTO signal_history
            (run_date, target_date, question, threshold_value, variable,
             gfs_raw, gfs_corrected, calib_bias, calib_sigma, calib_n,
             model_prob, market_price, edge, direction, flagged)
        VALUES
            (:run_date, :target_date, :question, :threshold_value, :variable,
             :gfs_raw, :gfs_corrected, :calib_bias, :calib_sigma, :calib_n,
             :model_prob, :market_price, :edge, :direction, :flagged)
        """,
        [{**s, "run_date": run_date} for s in signals],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Price lookup from local DB
# ---------------------------------------------------------------------------

def _get_current_prices(market_conn: sqlite3.Connection) -> dict[str, float]:
    rows = market_conn.execute(
        """
        SELECT p.market_id, p.price
        FROM price_history p
        INNER JOIN (
            SELECT market_id, MAX(timestamp) AS ts
            FROM price_history
            GROUP BY market_id
        ) latest ON p.market_id = latest.market_id AND p.timestamp = latest.ts
        """
    ).fetchall()
    return {r[0]: float(r[1]) for r in rows}


# ---------------------------------------------------------------------------
# Core run function
# ---------------------------------------------------------------------------

def run(
    source: Optional[PredictionSource] = None,
    city_filter: Optional[str] = None,
    min_edge: float = MIN_EDGE,
) -> list[dict]:
    """Generate daily signals.

    Args:
        source: PredictionSource to use.  If None, defaults to
                GFSPredictionSource(mode="live").
        city_filter: optional city name to restrict to (e.g. "Hong Kong").
                     None or "all" runs all active markets.
        min_edge: edge threshold for flagging trades.

    Returns:
        List of signal dicts (one per market).
    """
    if source is None:
        from src.data.gfs_prediction import GFSPredictionSource
        source = GFSPredictionSource(mode="live")

    today = date.today().isoformat()
    run_ts = datetime.now(tz=timezone.utc).isoformat()
    print(f"[signals] Run date: {today}  (UTC: {run_ts})")

    market_conn = connect_markets(MARKET_DB)
    market_conn.row_factory = sqlite3.Row
    init_weather_db(market_conn)

    city_clause = ""
    params: list = [today]
    if city_filter and city_filter.lower() != "all":
        city_clause = " AND lower(m.city) = lower(?)"
        params.append(city_filter)

    markets = market_conn.execute(
        """
        SELECT m.id, m.question, m.city, m.country,
               m.market_type, m.threshold_value, m.threshold_unit,
               m.target_date, m.latitude, m.longitude
        FROM markets m
        WHERE m.target_date > ?
          AND m.resolved_outcome IS NULL
          AND m.market_type IN ('temp_above', 'precip', 'snow')
          AND m.threshold_value IS NOT NULL
          AND m.city IS NOT NULL
          AND m.latitude IS NOT NULL
          {city_clause}
        ORDER BY m.target_date, m.city, m.threshold_value
        """.format(city_clause=city_clause),
        params,
    ).fetchall()
    print(f"[signals] Active markets: {len(markets)}")

    current_prices = _get_current_prices(market_conn)
    market_conn.close()

    signals: list[dict] = []
    skipped_no_pred = 0
    skipped_no_price = 0

    for mkt in markets:
        market_type  = mkt["market_type"]
        variable     = _infer_variable(mkt["question"], market_type)
        rule         = _infer_rule(mkt["question"], market_type)
        threshold    = float(mkt["threshold_value"])
        location_id  = normalize_location_id(mkt["city"], mkt["country"])
        market_price = current_prices.get(mkt["id"])

        ctx = MarketContext(
            market_id=mkt["id"],
            question=mkt["question"],
            outcomes=["Yes", "No"],
            outcome_prices=[market_price or 0.5, 1.0 - (market_price or 0.5)],
            city=mkt["city"] or "",
            country=mkt["country"] or "",
            target_date=mkt["target_date"],
            market_type=market_type,
            threshold_value=threshold,
            threshold_unit=mkt["threshold_unit"] or "",
            variable=variable,
            rule=rule,
            latitude=float(mkt["latitude"] or 0),
            longitude=float(mkt["longitude"] or 0),
            location_id=location_id,
            extra={"price_date": today},
        )

        if not source.can_predict(ctx):
            skipped_no_pred += 1
            continue

        prediction = source.predict(ctx)
        if prediction is None:
            skipped_no_pred += 1
            continue

        extra         = prediction.extra or {}
        gfs_raw       = extra.get("gfs_raw")
        gfs_corrected = extra.get("gfs_corrected")
        calib_bias    = extra.get("calib_bias", 0.0)
        calib_sigma   = extra.get("calib_sigma", 0.0)
        calib_n       = extra.get("calib_n", 0)

        if market_price is None:
            skipped_no_price += 1
            edge, direction, flagged = None, None, 0
        else:
            mp_clamped = min(max(market_price, 0.001), 0.999)
            ctx.outcome_prices = [mp_clamped]
            signal  = compute_edge(prediction, mp_clamped, ctx,
                                   min_edge=min_edge, min_confidence=0.0)
            edge      = signal.edge
            direction = signal.direction
            flagged   = 1 if signal.flagged else 0

        signals.append({
            "target_date":     mkt["target_date"],
            "city":            mkt["city"],
            "question":        mkt["question"],
            "threshold_value": threshold,
            "variable":        variable,
            "gfs_raw":         round(gfs_raw, 2) if gfs_raw is not None else None,
            "gfs_corrected":   round(gfs_corrected, 2) if gfs_corrected is not None else None,
            "calib_bias":      round(calib_bias, 4),
            "calib_sigma":     round(calib_sigma, 4),
            "calib_n":         calib_n,
            "model_prob":      round(prediction.estimated_probability, 4),
            "market_price":    round(market_price, 4) if market_price is not None else None,
            "edge":            round(edge, 4) if edge is not None else None,
            "direction":       direction,
            "flagged":         flagged,
        })

    print(
        f"[signals] Scored {len(signals)} markets "
        f"({skipped_no_pred} no-prediction, {skipped_no_price} no-price)"
    )
    return signals


def write_csv(signals: list[dict], path: Path = SIGNALS_CSV) -> None:
    if not signals:
        print("[signals] Nothing to write.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(signals[0].keys())
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(signals)
    print(f"[signals] Wrote {len(signals)} rows → {path}")


def write_history(signals: list[dict], today: str) -> None:
    hist_conn = sqlite3.connect(str(HISTORY_DB))
    _init_history_db(hist_conn)
    _upsert_signals(hist_conn, today, signals)
    hist_conn.close()
    print(f"[signals] Appended {len(signals)} rows to signal_history.db")


def print_summary(signals: list[dict], min_edge: float = MIN_EDGE) -> None:
    flagged = [s for s in signals if s.get("flagged")]
    if not flagged:
        print(f"\n[signals] No signals above edge threshold (|edge| ≥ {min_edge:.0%}).")
        return

    print(f"\n{'='*72}")
    print(f"ACTIONABLE SIGNALS  (|edge| ≥ {min_edge:.0%})  —  {len(flagged)} trades")
    print(f"{'='*72}")
    print(
        f"{'Date':<12} {'City':<14} {'Thresh':>7} {'GFS':>7} "
        f"{'Model':>7} {'Mkt':>7} {'Edge':>8} Dir"
    )
    print("-" * 72)
    for s in sorted(
        flagged,
        key=lambda x: (x["target_date"], x.get("city", ""), x["threshold_value"]),
    ):
        mp = s.get("market_price") or 0.0
        gc = s.get("gfs_corrected") or 0.0
        print(
            f"{s['target_date']:<12} "
            f"{(s.get('city') or ''):<14} "
            f"{s['threshold_value']:>6.1f}°  "
            f"{gc:>6.1f}°  "
            f"{s['model_prob']:>6.1%} "
            f"{mp:>6.1%} "
            f"{(s.get('edge') or 0.0):>+7.1%}  "
            f"{s.get('direction', '')}"
        )


def main(
    source: Optional[PredictionSource] = None,
    city_filter: Optional[str] = None,
    min_edge: float = MIN_EDGE,
) -> None:
    today = date.today().isoformat()
    signals = run(source=source, city_filter=city_filter, min_edge=min_edge)
    write_csv(signals)
    write_history(signals, today)
    print_summary(signals, min_edge=min_edge)


def _parse_args():
    parser = argparse.ArgumentParser(description="Generate daily weather market signals.")
    parser.add_argument("--city", default=None,
                        help="City filter (e.g. 'Hong Kong') or 'all'")
    parser.add_argument("--min-edge", type=float, default=MIN_EDGE)
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    main(city_filter=args.city, min_edge=args.min_edge)


