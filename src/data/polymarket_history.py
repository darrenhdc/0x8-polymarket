"""Polymarket weather-market discovery and CLOB price-history backfill."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import requests

from .database import connect_markets, init_weather_db
from .geocoding import Location, extract_city, geocode_city


GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_PRICES_HISTORY_URL = "https://clob.polymarket.com/prices-history"
CLOB_MAX_WINDOW_DAYS = 14

# Slug templates for known HK event series (city, event_type → slug template)
# {month} = lowercase month name, {day} = int day, {year} = int year
HK_TEMP_SLUG_TEMPLATE = "highest-temperature-in-hong-kong-on-{month}-{day}-{year}"
HK_PRECIP_SLUG_TEMPLATE = "precipitation-in-hong-kong-in-{month}-{year}"

WEATHER_PATTERNS = (
    r"\bweather\b",
    r"\btemperature\b",
    r"\btemp\b",
    r"[°º]\s*[cf]\b",
    r"\bcelsius\b",
    r"\bfahrenheit\b",
    r"\brain\b",
    r"\brainfall\b",
    r"\bprecipitation\b",
    r"\bsnow\b",
    r"\bsnowfall\b",
    r"\bstorm\b",
    r"\bhurricane\b",
    r"\btyphoon\b",
    r"\btornado\b",
    r"\bwind speed\b",
    r"\bhumidity\b",
)


@dataclass
class ParsedMarket:
    market_type: Optional[str]
    threshold_value: Optional[float]
    threshold_unit: Optional[str]
    target_date: Optional[str]
    city: Optional[str]


def _loads_maybe(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def is_weather_market(question: str, tags: Iterable = ()) -> bool:
    combined = question.lower()
    for tag in tags or []:
        if isinstance(tag, dict):
            combined += " " + str(tag.get("name") or tag.get("label") or tag.get("slug") or "").lower()
        else:
            combined += " " + str(tag).lower()
    return any(re.search(pattern, combined) for pattern in WEATHER_PATTERNS)


def parse_threshold(question: str) -> tuple[Optional[str], Optional[float], Optional[str]]:
    q = question.lower()

    temp_f = re.search(r"(\d+(?:\.\d+)?)\s*(?:°|º|deg(?:rees)?\s*)?(?:f|fahrenheit|degf)\b", q)
    if temp_f:
        value_f = float(temp_f.group(1))
        return "temp_above", (value_f - 32.0) * 5.0 / 9.0, "C"

    temp_c = re.search(r"(\d+(?:\.\d+)?)\s*(?:°|º|deg(?:rees)?\s*)?(?:c|celsius|degc)\b", q)
    if temp_c:
        return "temp_above", float(temp_c.group(1)), "C"

    inches = re.search(r"(\d+(?:\.\d+)?)\s*(?:inches|inch|in)\b", q)
    if inches and any(word in q for word in ("rain", "snow", "precip")):
        unit = "inch"
        market_type = "snow" if "snow" in q else "precip"
        return market_type, float(inches.group(1)), unit

    millimeters = re.search(r"(\d+(?:\.\d+)?)\s*(?:mm|millimeters|millimetres)\b", q)
    if millimeters and any(word in q for word in ("rain", "snow", "precip")):
        market_type = "snow" if "snow" in q else "precip"
        return market_type, float(millimeters.group(1)), "mm"

    if any(word in q for word in ("hurricane", "storm", "typhoon")):
        return "storm", None, None

    return None, None, None


def parse_target_date(question: str, fallback: Optional[str] = None) -> Optional[str]:
    q = question.replace(",", "")
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", q)
    if match:
        return match.group(1)

    formats = [
        ("%B %d %Y", r"\b([A-Z][a-z]+ \d{1,2}(?:st|nd|rd|th)? 20\d{2})\b"),
        ("%b %d %Y", r"\b([A-Z][a-z]{2} \d{1,2}(?:st|nd|rd|th)? 20\d{2})\b"),
    ]
    for fmt, pattern in formats:
        match = re.search(pattern, q)
        if match:
            value = re.sub(r"(st|nd|rd|th)", "", match.group(1))
            try:
                return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass

    if fallback:
        try:
            return datetime.fromisoformat(fallback.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            return fallback[:10]
    return None


def parse_market(question: str, end_date: Optional[str]) -> ParsedMarket:
    market_type, threshold_value, threshold_unit = parse_threshold(question)
    return ParsedMarket(
        market_type=market_type,
        threshold_value=threshold_value,
        threshold_unit=threshold_unit,
        target_date=parse_target_date(question, end_date),
        city=extract_city(question),
    )


class PolymarketHistoryCollector:
    def __init__(self, db_path=None, session: Optional[requests.Session] = None):
        self.conn = connect_markets(db_path)
        init_weather_db(self.conn)
        self.session = session or requests.Session()

    def close(self) -> None:
        self.conn.close()

    def fetch_markets_page(self, *, limit: int, offset: int, closed: bool) -> list[dict]:
        resp = self.session.get(
            GAMMA_MARKETS_URL,
            params={
                "limit": limit,
                "offset": offset,
                "closed": str(closed).lower(),
                "order": "endDate",
                "ascending": "false",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("markets", [])

    def fetch_weather_events_page(self, *, limit: int, offset: int, closed: Optional[bool]) -> list[dict]:
        params = {
            "limit": limit,
            "offset": offset,
            "tag_slug": "weather",
        }
        if closed is not None:
            params["closed"] = str(closed).lower()
        resp = self.session.get(GAMMA_EVENTS_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("events", [])

    def collect_weather_markets(
        self,
        *,
        max_pages: int = 50,
        page_size: int = 200,
        include_closed: bool = True,
        include_open: bool = True,
        geocode: bool = True,
        sleep_seconds: float = 0.2,
        events_only: bool = False,
        event_query: Optional[str] = None,
    ) -> int:
        total = 0
        if not events_only:
            for closed in [False, True]:
                if closed and not include_closed:
                    continue
                if not closed and not include_open:
                    continue
                for page in range(max_pages):
                    markets = self.fetch_markets_page(
                        limit=min(page_size, 500),
                        offset=page * page_size,
                        closed=closed,
                    )
                    if not markets:
                        break
                    for market in markets:
                        if not is_weather_market(market.get("question", ""), market.get("tags", [])):
                            continue
                        self.upsert_market(market, geocode=geocode)
                        total += 1
                    time.sleep(sleep_seconds)

        total += self.collect_weather_event_markets(
            max_pages=max_pages,
            page_size=page_size,
            include_closed=include_closed,
            include_open=include_open,
            geocode=geocode,
            sleep_seconds=sleep_seconds,
            event_query=event_query,
        )
        self.conn.commit()
        return total

    def collect_weather_event_markets(
        self,
        *,
        max_pages: int,
        page_size: int,
        include_closed: bool,
        include_open: bool,
        geocode: bool,
        sleep_seconds: float,
        event_query: Optional[str] = None,
    ) -> int:
        total = 0
        query_text = event_query.lower() if event_query else None
        for closed in [False, True]:
            if closed and not include_closed:
                continue
            if not closed and not include_open:
                continue
            for page in range(max_pages):
                events = self.fetch_weather_events_page(
                    limit=min(page_size, 500),
                    offset=page * page_size,
                    closed=closed,
                )
                if not events:
                    break
                for event in events:
                    event_text = " ".join(
                        str(event.get(key) or "") for key in ("title", "description", "slug")
                    )
                    all_market_text = " ".join(str(m.get("question") or "") for m in event.get("markets", []))
                    if query_text and query_text not in f"{event_text} {all_market_text}".lower():
                        continue
                    for market in event.get("markets", []):
                        question = market.get("question") or ""
                        if not is_weather_market(f"{event_text} {question}", event.get("tags", [])):
                            continue
                        enriched = dict(market)
                        enriched.setdefault("startDate", event.get("startDate"))
                        enriched.setdefault("endDate", event.get("endDate"))
                        enriched["_event"] = {
                            "id": event.get("id"),
                            "slug": event.get("slug"),
                            "title": event.get("title"),
                            "closed": event.get("closed"),
                            "tags": event.get("tags", []),
                        }
                        self.upsert_market(enriched, geocode=geocode)
                        total += 1
                time.sleep(sleep_seconds)
        return total

    def upsert_market(self, market: dict, *, geocode: bool = True) -> None:
        question = market.get("question") or ""
        end_date = market.get("endDate") or market.get("end_date_iso") or market.get("end_date")
        parsed = parse_market(question, end_date)
        location: Optional[Location] = geocode_city(parsed.city) if geocode and parsed.city else None

        market_id = market.get("conditionId") or market.get("id")
        if not market_id:
            return

        clob_token_ids = _loads_maybe(market.get("clobTokenIds"), [])
        outcomes = _loads_maybe(market.get("outcomes"), [])
        outcome_prices = _loads_maybe(market.get("outcomePrices"), [])
        resolved_outcome = self._infer_resolved_outcome(outcomes, outcome_prices, market)

        self.conn.execute(
            """
            INSERT INTO markets (
                id, slug, question, city, country, latitude, longitude,
                market_type, threshold_value, threshold_unit, target_date,
                start_date, end_date, active, closed, archived, resolved_outcome,
                volume, liquidity, clob_token_ids, raw_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                slug=excluded.slug,
                question=excluded.question,
                city=excluded.city,
                country=excluded.country,
                latitude=excluded.latitude,
                longitude=excluded.longitude,
                market_type=excluded.market_type,
                threshold_value=excluded.threshold_value,
                threshold_unit=excluded.threshold_unit,
                target_date=excluded.target_date,
                start_date=excluded.start_date,
                end_date=excluded.end_date,
                active=excluded.active,
                closed=excluded.closed,
                archived=excluded.archived,
                resolved_outcome=excluded.resolved_outcome,
                volume=excluded.volume,
                liquidity=excluded.liquidity,
                clob_token_ids=excluded.clob_token_ids,
                raw_json=excluded.raw_json,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                market_id,
                market.get("slug"),
                question,
                location.name if location else parsed.city,
                location.country if location else None,
                location.latitude if location else None,
                location.longitude if location else None,
                parsed.market_type,
                parsed.threshold_value,
                parsed.threshold_unit,
                parsed.target_date,
                _date_only(market.get("startDate") or market.get("start_date_iso")),
                _date_only(end_date),
                int(bool(market.get("active"))),
                int(bool(market.get("closed"))),
                int(bool(market.get("archived"))),
                resolved_outcome,
                _float_or_none(market.get("volume")),
                _float_or_none(market.get("liquidity")),
                json.dumps(clob_token_ids),
                json.dumps(market, ensure_ascii=False),
            ),
        )

    def discover_hk_weather_by_slug(
        self,
        *,
        start_date: str,
        end_date: str,
        sleep_seconds: float = 0.08,
        include_precip: bool = True,
    ) -> int:
        """Discover HK weather events by iterating known slug patterns.

        Much faster than scanning all markets because it targets exactly the
        known event series without touching unrelated markets.
        """
        from datetime import date as _date, timedelta

        start = _date.fromisoformat(start_date)
        end = _date.fromisoformat(end_date)
        total = 0

        # Daily temperature events
        current = start
        while current <= end:
            slug = HK_TEMP_SLUG_TEMPLATE.format(
                month=current.strftime("%B").lower(),
                day=current.day,
                year=current.year,
            )
            total += self._ingest_event_by_slug(slug)
            current += timedelta(days=1)
            time.sleep(sleep_seconds)

        # Monthly precipitation events (one event per calendar month)
        if include_precip:
            seen_months: set[tuple[int, int]] = set()
            current = start
            while current <= end:
                key = (current.year, current.month)
                if key not in seen_months:
                    seen_months.add(key)
                    slug = HK_PRECIP_SLUG_TEMPLATE.format(
                        month=current.strftime("%B").lower(),
                        year=current.year,
                    )
                    total += self._ingest_event_by_slug(slug)
                    time.sleep(sleep_seconds)
                current += timedelta(days=1)

        self.conn.commit()
        return total

    def _ingest_event_by_slug(self, slug: str) -> int:
        """Fetch a single Gamma event by slug and upsert its markets. Returns count upserted."""
        try:
            resp = self.session.get(GAMMA_EVENTS_URL, params={"slug": slug}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError):
            return 0
        if not data or not isinstance(data, list):
            return 0
        event = data[0]
        total = 0
        event_meta = {
            "id": event.get("id"),
            "slug": event.get("slug"),
            "title": event.get("title"),
            "closed": event.get("closed"),
            "tags": event.get("tags", []),
        }
        for market in event.get("markets", []):
            enriched = dict(market)
            enriched.setdefault("startDate", event.get("startDate"))
            enriched.setdefault("endDate", event.get("endDate"))
            enriched["_event"] = event_meta
            self.upsert_market(enriched, geocode=True)
            total += 1
        return total

    def _infer_resolved_outcome(self, outcomes: list, prices: list, market: dict) -> Optional[str]:
        for key in ("outcome", "resolution", "resolvedOutcome", "winningOutcome"):
            if market.get(key):
                return str(market[key])
        numeric_prices = []
        for price in prices or []:
            try:
                numeric_prices.append(float(price))
            except (TypeError, ValueError):
                numeric_prices.append(None)
        if outcomes and numeric_prices and max(p or 0 for p in numeric_prices) >= 0.999:
            idx = max(range(len(numeric_prices)), key=lambda i: numeric_prices[i] or 0)
            if idx < len(outcomes):
                return str(outcomes[idx])
        return None

    def backfill_price_history(
        self,
        *,
        start_date: str,
        end_date: str,
        fidelity_minutes: int = 1440,
        limit_markets: Optional[int] = None,
        sleep_seconds: float = 0.15,
    ) -> int:
        query = """
            SELECT id, clob_token_ids, start_date, end_date, target_date
            FROM markets
            WHERE clob_token_ids IS NOT NULL AND clob_token_ids != '[]'
            ORDER BY end_date
        """
        if limit_markets:
            query += f" LIMIT {int(limit_markets)}"
        written = 0
        for row in self.conn.execute(query):
            token_ids = _loads_maybe(row["clob_token_ids"], [])
            if not token_ids:
                continue
            yes_token = str(token_ids[0])
            price_start, price_end = _market_price_window(row, start_date, end_date)
            points = self.fetch_price_history(
                yes_token,
                start_date=price_start,
                end_date=price_end,
                fidelity_minutes=fidelity_minutes,
            )
            written += self.insert_price_points(
                market_id=row["id"],
                token_id=yes_token,
                points=points,
                fidelity_minutes=fidelity_minutes,
            )
            time.sleep(sleep_seconds)
        self.conn.commit()
        return written

    def fetch_price_history(
        self,
        token_id: str,
        *,
        start_date: str,
        end_date: str,
        fidelity_minutes: int = 1440,
    ) -> list[dict]:
        start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc) + timedelta(days=1)
        points: dict[str, float] = {}
        window = timedelta(days=CLOB_MAX_WINDOW_DAYS)
        current = start
        while current < end:
            segment_end = min(current + window, end)
            resp = self.session.get(
                CLOB_PRICES_HISTORY_URL,
                params={
                    "market": token_id,
                    "startTs": int(current.timestamp()),
                    "endTs": int(segment_end.timestamp()),
                    "fidelity": fidelity_minutes,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                for item in resp.json().get("history", []):
                    ts = datetime.fromtimestamp(item["t"], tz=timezone.utc).isoformat()
                    points[ts] = float(item["p"])
            current = segment_end
        return [{"timestamp": ts, "price": price} for ts, price in sorted(points.items())]

    def insert_price_points(
        self,
        *,
        market_id: str,
        token_id: str,
        points: list[dict],
        fidelity_minutes: int,
    ) -> int:
        before = self.conn.total_changes
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO price_history (
                market_id, token_id, timestamp, price, fidelity_minutes
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (market_id, token_id, point["timestamp"], point["price"], fidelity_minutes)
                for point in points
            ],
        )
        return self.conn.total_changes - before


def _float_or_none(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date_only(value) -> Optional[str]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return str(value)[:10]


def _market_price_window(row, global_start: str, global_end: str) -> tuple[str, str]:
    starts = [global_start]
    if row["start_date"]:
        starts.append(row["start_date"])

    ends = [global_end]
    if row["end_date"]:
        ends.append(row["end_date"])
    elif row["target_date"]:
        ends.append(row["target_date"])

    start = max(starts)
    end = min(ends)

    if end < start and row["target_date"]:
        target = datetime.fromisoformat(row["target_date"])
        fallback_start = (target - timedelta(days=30)).date().isoformat()
        fallback_end = target.date().isoformat()
        start = max(global_start, fallback_start)
        end = min(global_end, fallback_end)

    if end < start:
        end = start
    return start, end
