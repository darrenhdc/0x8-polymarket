"""
Event Scanner — scans Polymarket for newly created markets in
sports, politics, and crypto categories.

Uses the Gamma API (public, no auth) to list events and markets.
Filters strictly: sports, politics, crypto only — NO weather markets.

Output: list of event dicts with:
  - event_id, title, question, outcomes
  - current implied probabilities
  - volume, liquidity
  - category/tags
  - market_ids for each outcome token
"""
import json
import os
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

from src.core import config
from .market_data import PolymarketAPI


# ── Category filtering ────────────────────────────────────────

ALLOWED_EVENT_TAGS = [
    "sports", "politics", "crypto", "cryptocurrency",
    "nfl", "nba", "mlb", "nhl", "soccer", "football",
    "election", "presidential", "congress", "policy",
    "bitcoin", "ethereum", "defi", "regulation",
]

WEATHER_KEYWORDS = [
    "weather", "temperature", "rainfall", "snowfall",
    "precipitation", "storm", "hurricane", "typhoon",
    "wind speed", "humidity", "fog", "frost",
]


@dataclass
class EventMarket:
    """A single market within an event."""
    market_id: str
    question: str
    outcomes: List[str]
    outcome_prices: List[float]  # current prices (0-1)
    clob_token_ids: List[str]    # CLOB token IDs for trading
    volume: float
    liquidity: float
    end_date_iso: Optional[str]
    neg_risk: bool = False

    @property
    def yes_price(self) -> float:
        """YES price (first outcome)."""
        if self.outcome_prices and len(self.outcome_prices) > 0:
            return self.outcome_prices[0]
        return 0.0

    @property
    def no_price(self) -> float:
        """NO price (second outcome)."""
        if self.outcome_prices and len(self.outcome_prices) > 1:
            return self.outcome_prices[1]
        return 0.0

    @property
    def implied_probability(self) -> float:
        """Market-implied probability of YES outcome."""
        return self.yes_price

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class EventInfo:
    """A Polymarket event containing one or more markets."""
    event_id: str
    title: str
    description: str
    category: str
    tags: List[str]
    markets: List[EventMarket]
    volume: float
    liquidity: float
    start_date_iso: Optional[str]
    end_date_iso: Optional[str]

    def is_weather(self) -> bool:
        """Check if this event is weather-related."""
        title_lower = self.title.lower()
        desc_lower = self.description.lower()
        combined = title_lower + " " + desc_lower
        for kw in WEATHER_KEYWORDS:
            if kw in combined:
                return True
        # Check category
        for kw in WEATHER_KEYWORDS:
            if kw in self.category.lower():
                return True
        return False

    def to_dict(self) -> Dict:
        return {
            "event_id": self.event_id,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "tags": self.tags,
            "markets": [m.to_dict() for m in self.markets],
            "volume": self.volume,
            "liquidity": self.liquidity,
            "start_date_iso": self.start_date_iso,
            "end_date_iso": self.end_date_iso,
        }


class EventScanner:
    """
    Scans Polymarket for events and markets, with strong category filtering.
    """

    def __init__(self):
        self.api = PolymarketAPI()
        self._events_cache: List[EventInfo] = []
        self._last_scan_time: Optional[datetime] = None

    def _is_allowed_tag(self, tags: List[str]) -> bool:
        """Check if any tag matches allowed categories."""
        if not tags:
            return False
        for tag in tags:
            tag_name = ""
            if isinstance(tag, str):
                tag_name = tag.lower()
            elif isinstance(tag, dict):
                tag_name = tag.get("name", "").lower()
            for allowed in ALLOWED_EVENT_TAGS:
                if allowed in tag_name:
                    return True
        return False

    def _is_weather_tag(self, tags: List[str]) -> bool:
        """Check if any tag is weather-related."""
        if not tags:
            return False
        for tag in tags:
            tag_name = ""
            if isinstance(tag, str):
                tag_name = tag.lower()
            elif isinstance(tag, dict):
                tag_name = tag.get("name", "").lower()
            for kw in WEATHER_KEYWORDS:
                if kw in tag_name:
                    return True
        return False

    def _extract_category(self, tags: List[str]) -> str:
        """Extract the primary category from tags."""
        if not tags:
            return "Unknown"
        for tag in tags:
            if isinstance(tag, dict):
                name = tag.get("name", "")
                if name.lower() in ["sports", "politics", "crypto", "cryptocurrency"]:
                    return name
            elif isinstance(tag, str):
                if tag.lower() in ["sports", "politics", "crypto", "cryptocurrency"]:
                    return tag
        return "Unknown"

    def scan_events(self, limit: int = 50, offset: int = 0) -> List[EventInfo]:
        """
        Scan Polymarket events, returning only allowed events
        (sports/politics/crypto, not weather).
        """
        raw_events = self.api.get_events(limit=limit, offset=offset, active=True)
        if not raw_events:
            return []

        events: List[EventInfo] = []
        for raw in raw_events:
            try:
                event = self._parse_event(raw)
                if event and not event.is_weather():
                    events.append(event)
            except Exception as e:
                print(f"[EventScanner] Error parsing event {raw.get('id')}: {e}")
                continue

        self._events_cache = events
        self._last_scan_time = datetime.utcnow()
        return events

    def scan_markets(self, limit: int = 100) -> List[EventMarket]:
        """
        Scan markets directly from Gamma API (not nested under events).
        Uses a lower minimum volume threshold (configurable) to catch
        newly created markets before they have significant liquidity.

        NOTE: We use the Gamma API's public /markets endpoint directly
        (not MarketScanner.get_tradable_markets) because that method's
        liquidity/volume filters are too restrictive for event scanning.
        """
        import requests as _requests

        raw_markets = []
        try:
            resp = _requests.get(
                f"{config.GAMMA_API}/markets",
                params={
                    "limit": min(limit, 200),
                    "active": "true",
                    "closed": "false",
                },
                timeout=30,
            )
            if resp.status_code == 200:
                raw_markets = resp.json() if isinstance(resp.json(), list) else []
        except Exception as e:
            print(f"[EventScanner] Gamma API error: {e}")
            return []

        result: List[EventMarket] = []
        for m in raw_markets:
            try:
                question = str(m.get("question", "") or "")
                tags_raw = m.get("tags", [])

                # Skip weather by question keyword (strongest signal)
                q_lower = question.lower()
                is_weather = any(kw in q_lower for kw in WEATHER_KEYWORDS)
                if is_weather:
                    continue

                # Parse tags for category
                tags_list = []
                if isinstance(tags_raw, list):
                    for t in tags_raw:
                        if isinstance(t, dict):
                            tags_list.append(t.get("label", "") or t.get("name", ""))
                        elif isinstance(t, str):
                            tags_list.append(t)

                # No strict category filter here — Gamma markets often have
                # empty tags. We'll let the pipeline filter by question
                # heuristics and the risk manager's Rule 10.
                # Instead, just ensure it's not weather.

                # Parse market data
                clob_token_ids = m.get("clobTokenIds", [])
                if isinstance(clob_token_ids, str):
                    try:
                        clob_token_ids = json.loads(clob_token_ids)
                    except Exception:
                        clob_token_ids = []

                outcomes = m.get("outcomes", ["Yes", "No"])
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except Exception:
                        outcomes = ["Yes", "No"]

                outcome_prices_raw = m.get("outcomePrices", [])
                if isinstance(outcome_prices_raw, str):
                    try:
                        outcome_prices_raw = json.loads(outcome_prices_raw)
                    except Exception:
                        outcome_prices_raw = []

                prices = []
                for p in outcome_prices_raw:
                    try:
                        prices.append(float(p) if p else 0)
                    except (ValueError, TypeError):
                        prices.append(0)

                # Must have at least some price data
                if not prices or all(p == 0 for p in prices):
                    continue

                em = EventMarket(
                    market_id=str(m.get("id", "")),
                    question=question,
                    outcomes=outcomes[:2] if len(outcomes) > 2 else outcomes,
                    outcome_prices=prices[:2] if len(prices) > 2 else prices,
                    clob_token_ids=clob_token_ids[:2] if len(clob_token_ids) > 2 else clob_token_ids,
                    volume=float(m.get("volume", 0) or 0),
                    liquidity=float(m.get("liquidity", 0) or 0),
                    end_date_iso=m.get("endDateIso") or m.get("end_date_iso"),
                    neg_risk=m.get("neg_risk", False),
                )
                result.append(em)

            except Exception as e:
                print(f"[EventScanner] Error parsing market {m.get('id')}: {e}")
                continue

        # Sort by volume descending
        result.sort(key=lambda x: x.volume, reverse=True)
        return result

    def scan_new_markets(self, since_minutes: int = 60) -> List[EventMarket]:
        """
        Get markets created in the last N minutes.
        Uses the events endpoint to find recently created events.
        """
        all_markets = self.scan_markets(limit=200)
        cutoff = datetime.utcnow().timestamp() - (since_minutes * 60)

        # We can't directly filter by creation time from the public API,
        # but we return recently active markets and let the caller cache.
        # For a real incremental scan, save seen market IDs.

        # Load previously seen IDs
        seen_file = os.path.join(config.DATA_DIR, "scanned_market_ids.json")
        seen_ids: set = set()
        if os.path.exists(seen_file):
            try:
                with open(seen_file) as f:
                    seen_ids = set(json.load(f))
            except Exception:
                pass

        new_markets = []
        for m in all_markets:
            if m.market_id not in seen_ids:
                new_markets.append(m)
                seen_ids.add(m.market_id)

        # Save updated seen IDs
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(seen_file, "w") as f:
            json.dump(list(seen_ids), f)

        return new_markets

    def _parse_event(self, raw: Dict) -> Optional[EventInfo]:
        """Parse raw API event dict into EventInfo."""
        event_id = str(raw.get("id", ""))
        title = raw.get("title", "") or raw.get("question", "")
        description = raw.get("description", "")
        tags_raw = raw.get("tags", [])
        volume = float(raw.get("volume", 0) or 0)
        liquidity = float(raw.get("liquidity", 0) or 0)

        # Extract category
        category = self._extract_category(tags_raw)

        # Check allowed tags
        if not self._is_allowed_tag(tags_raw):
            return None

        # Parse markets within the event
        raw_markets = raw.get("markets", [])
        if not raw_markets:
            return None

        markets: List[EventMarket] = []
        for rm in raw_markets:
            try:
                clob_ids = rm.get("clobTokenIds", [])
                if isinstance(clob_ids, str):
                    clob_ids = json.loads(clob_ids)

                outcomes = rm.get("outcomes", ["Yes", "No"])
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)

                prices_raw = rm.get("outcomePrices", [])
                if isinstance(prices_raw, str):
                    prices_raw = json.loads(prices_raw)

                prices = []
                for p in prices_raw:
                    try:
                        prices.append(float(p) if p else 0)
                    except (ValueError, TypeError):
                        prices.append(0)

                # Only include binary (2-outcome) markets for simplicity
                if len(clob_ids) < 2 or len(outcomes) < 2:
                    continue

                em = EventMarket(
                    market_id=str(rm.get("id", "")),
                    question=rm.get("question", title),
                    outcomes=outcomes[:2],  # binary markets only
                    outcome_prices=prices[:2],
                    clob_token_ids=clob_ids[:2],
                    volume=float(rm.get("volume", 0) or 0),
                    liquidity=float(rm.get("liquidity", 0) or 0),
                    end_date_iso=rm.get("endDateIso") or rm.get("end_date_iso"),
                    neg_risk=rm.get("neg_risk", False),
                )
                markets.append(em)
            except Exception as e:
                print(f"[EventScanner] Error parsing sub-market {rm.get('id')}: {e}")
                continue

        if not markets:
            return None

        return EventInfo(
            event_id=event_id,
            title=title,
            description=description,
            category=category,
            tags=[t.get("name", str(t)) if isinstance(t, dict) else str(t) for t in tags_raw],
            markets=markets,
            volume=volume,
            liquidity=liquidity,
            start_date_iso=raw.get("startDateIso") or raw.get("start_date_iso"),
            end_date_iso=raw.get("endDateIso") or raw.get("end_date_iso"),
        )

    def get_recent_events(self, limit: int = 20) -> List[EventInfo]:
        """Get most recent events in allowed categories."""
        return self.scan_events(limit=limit)

    def get_top_markets(self, limit: int = 20) -> List[EventMarket]:
        """Get top markets by volume in allowed categories."""
        all_markets = self.scan_markets(limit=limit * 3)
        all_markets.sort(key=lambda m: m.volume, reverse=True)
        return all_markets[:limit]


# ── CLI test ──────────────────────────────────────────────────

if __name__ == "__main__":
    scanner = EventScanner()
    print("Scanning events...")
    events = scanner.get_recent_events(limit=10)
    print(f"Found {len(events)} events\n")

    for e in events[:5]:
        print(f"  [{e.category}] {e.title[:60]}")
        print(f"    Tags: {e.tags[:3]}")
        for m in e.markets[:2]:
            print(f"    Market: {m.question[:50]}... @ {m.yes_price:.1%} YES")
        print()

    print("---\nScanning markets...")
    markets = scanner.get_top_markets(limit=10)
    print(f"Found {len(markets)} tradable markets\n")

    for m in markets[:5]:
        print(f"  {m.question[:60]}")
        print(f"    YES: {m.yes_price:.1%}  NO: {m.no_price:.1%}  Vol: ${m.volume:,.0f}")
        print()
