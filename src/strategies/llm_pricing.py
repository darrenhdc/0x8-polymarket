"""
LLM Pricing Engine — feeds event/market details to DeepSeek (via existing
config.py) to estimate true probabilities, then calculates edge vs market.

Architecture:
  1. Takes an EventMarket (from event_scanner) with question, outcomes, prices
  2. Formats a structured prompt for DeepSeek with the market question & context
  3. DeepSeek returns: estimated probability, confidence level, reasoning
  4. Edge = |p_LLM - p_market| — only flags when edge > 10% AND confidence > 70%
  5. Results are logged to data/llm_pricing_log.json for accuracy tracking

Safety:
  - Edge > 40% → suspected model error, overridden to 0
  - Low confidence → HOLD even if edge is large
  - All results cached to avoid redundant API calls (6h TTL)
"""
import json
import os
import hashlib
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from dotenv import load_dotenv

from openai import OpenAI

from src.core.config import *
import src.core.config as config

# ── Ensure .env loaded ────────────────────────────────────────
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "trading_system", ".env"))

# ── Constants ─────────────────────────────────────────────────

# Minimum edge to flag (10 percentage points)
MIN_EDGE_TO_FLAG = 0.10

# Minimum LLM confidence to flag (70%)
MIN_CONFIDENCE_TO_FLAG = 0.70

# Edge sanity — if |p_LLM - p_market| > 40%, suspect model error
MAX_SANE_EDGE = 0.40

# Cache TTL
CACHE_TTL_HOURS = 6
CACHE_DIR = os.path.join(config.DATA_DIR, "llm_pricing_cache")

# Log file for accuracy tracking
PRICING_LOG_FILE = os.path.join(config.DATA_DIR, "llm_pricing_log.json")


@dataclass
class PricingResult:
    """Output of LLM pricing engine for one market."""
    market_id: str
    question: str
    timestamp: str

    # Market prices
    market_yes_price: float
    market_no_price: float

    # LLM estimates
    llm_estimated_probability: float  # probability of YES (0-1)
    llm_confidence: float  # 0-1 numeric
    llm_confidence_label: str  # "high", "medium", "low"
    llm_reasoning: str
    llm_key_factors: List[str]
    llm_risks: List[str]

    # Derived
    edge: float  # llm_estimate - market_yes_price (could be negative)
    abs_edge: float
    flagged: bool  # edge > 10% AND confidence > 70%
    flag_reason: str

    # Meta
    model: str
    cache_hit: bool = False

    def to_dict(self) -> Dict:
        return asdict(self)


def _cache_path(market_id: str) -> str:
    return os.path.join(CACHE_DIR, f"{market_id}.json")


def _load_cache(market_id: str) -> Optional[PricingResult]:
    path = _cache_path(market_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        ts = datetime.fromisoformat(data["timestamp"])
        if (datetime.utcnow() - ts).total_seconds() > CACHE_TTL_HOURS * 3600:
            return None
        return PricingResult(**data)
    except Exception:
        return None


def _save_cache(result: PricingResult):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_cache_path(result.market_id), "w") as f:
        json.dump(result.to_dict(), f, indent=2)


# ── Prompt templates ──────────────────────────────────────────

SYSTEM_PROMPT = """You are a prediction market pricing expert. Your job is to estimate the true probability of event outcomes based on your knowledge.

Rules:
1. Be calibrated: when uncertain, stay close to market price; only deviate with clear reasoning.
2. Use base rates, historical precedents, and logical reasoning.
3. Distinguish between what you know and what you don't know.
4. Your training data has a knowledge cutoff — you may lack recent information.
5. Do NOT fabricate facts. Say "I don't know" if uncertain.
6. Only suggest BUY_YES or BUY_NO when |your_estimate - market_price| >= 10 percentage points.

Output ONLY valid JSON (no markdown code blocks). Use this exact schema:
{
  "estimated_probability": <float 0-1, probability of YES>,
  "confidence": <"high" | "medium" | "low">,
  "reasoning": "<2-3 sentence analysis>",
  "key_factors": ["<factor1>", "<factor2>", ...],
  "risks": ["<risk1>", "<risk2>", ...]
}

Confidence mapping:
- "high" = you have strong evidence and high certainty (>= 80% confident)
- "medium" = you have reasonable evidence but some uncertainty (60-80% confident)
- "low" = you're guessing or have very little information (< 60% confident)"""


def _build_prompt(market_question: str, outcomes: List[str],
                  yes_price: float, no_price: float,
                  volume: float, liquidity: float,
                  days_to_resolution: Optional[int],
                  category: str = "Unknown") -> str:
    """Build the user prompt for the LLM."""
    return f"""Please analyze this Polymarket prediction market:

Question: {market_question}
Category: {category}
Outcomes: {outcomes}
Current prices: {outcomes[0] if outcomes else 'Yes'} = {yes_price:.1%} | {outcomes[1] if len(outcomes) > 1 else 'No'} = {no_price:.1%}
Volume: ${volume:,.0f}
Liquidity: ${liquidity:,.0f}
Days to resolution: {days_to_resolution if days_to_resolution else 'Unknown'}
Today: {datetime.utcnow().strftime('%Y-%m-%d')}

What is your estimated probability of the YES/{outcomes[0] if outcomes else 'Outcome'} outcome?

Only flag a trading opportunity (recommend BUY_YES or BUY_NO) if your estimate differs from the market price by at least 10 percentage points and you have high confidence."""


def _days_to_resolution(end_date_iso: Optional[str]) -> Optional[int]:
    """Calculate days until market resolution."""
    if not end_date_iso:
        return None
    try:
        end = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        delta = end - datetime.utcnow().replace(tzinfo=end.tzinfo)
        return max(0, delta.days)
    except Exception:
        return None


# ── Numeric confidence mapping ────────────────────────────────

CONFIDENCE_MAP = {
    "high": 0.85,
    "medium": 0.65,
    "low": 0.40,
}


def _numeric_confidence(label: str) -> float:
    """Convert confidence label to numeric value."""
    return CONFIDENCE_MAP.get(label.lower(), 0.40)


# ── Core engine ───────────────────────────────────────────────

class LLMPricingEngine:
    """
    Uses DeepSeek to price prediction markets and find edges.

    This is an independent pricing engine (not the same as LLMMarketAnalyzer).
    It is designed specifically for the Phase 2 "information-edge" strategy:
    scanning newly created events and pricing them from first principles.
    """

    def __init__(self):
        api_key = config.DEEPSEEK_API_KEY
        base_url = config.LLM_BASE_URL

        if not api_key:
            raise RuntimeError(
                "No API key available. Set DEEPSEEK_API_KEY in .env"
            )

        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = os.getenv("LLM_MODEL", config.LLM_MODEL or "deepseek-v4-pro")
        self.call_count = 0

        # Load existing log for accuracy tracking
        self.pricing_log: List[Dict] = self._load_log()

    # ── Main pricing method ────────────────────────────────────

    def price_event_market(
        self,
        market_question: str,
        outcomes: List[str],
        yes_price: float,
        no_price: float,
        market_id: str = "",
        volume: float = 0,
        liquidity: float = 0,
        end_date_iso: Optional[str] = None,
        category: str = "Unknown",
        force_refresh: bool = False,
    ) -> PricingResult:
        """
        Price a single event market.

        Returns a PricingResult with LLM estimate, edge, and flag status.
        Uses cache (6h TTL) unless force_refresh=True.
        """
        # Check cache
        if not force_refresh and market_id:
            cached = _load_cache(market_id)
            if cached:
                return cached

        days = _days_to_resolution(end_date_iso)

        prompt = _build_prompt(
            market_question=market_question,
            outcomes=outcomes,
            yes_price=yes_price,
            no_price=no_price,
            volume=volume,
            liquidity=liquidity,
            days_to_resolution=days,
            category=category,
        )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=512,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )

            text = response.choices[0].message.content.strip()
            # Strip markdown fences if present
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
                if text.endswith("```"):
                    text = text[: text.rfind("```")]
                text = text.strip()

            data = json.loads(text)
            self.call_count += 1

            # Parse LLM response
            llm_prob = float(data["estimated_probability"])
            conf_label = data.get("confidence", "low").lower()
            conf_numeric = _numeric_confidence(conf_label)

            # Calculate edge
            edge = llm_prob - yes_price
            abs_edge = abs(edge)

            # Safety: flag if edge exceeds sanity threshold
            flag_reason_parts = []
            flagged = False

            if abs_edge >= MAX_SANE_EDGE:
                flag_reason_parts.append(
                    f"Edge {edge:+.1%} exceeds sanity limit ({MAX_SANE_EDGE:.0%}) — suspected model error"
                )
                # Override — don't trade on insane edges
                llm_prob = yes_price  # fall back to market price
                edge = 0.0
                abs_edge = 0.0
            else:
                if abs_edge >= MIN_EDGE_TO_FLAG and conf_numeric >= MIN_CONFIDENCE_TO_FLAG:
                    flagged = True
                    direction = "BUY_YES" if edge > 0 else "BUY_NO"
                    flag_reason_parts.append(
                        f"Edge {edge:+.1%} >= {MIN_EDGE_TO_FLAG:.0%}, "
                        f"confidence {conf_label} ({conf_numeric:.0%}) >= {MIN_CONFIDENCE_TO_FLAG:.0%} — "
                        f"FLAGGED for {direction}"
                    )
                elif abs_edge < MIN_EDGE_TO_FLAG:
                    flag_reason_parts.append(
                        f"Edge {edge:+.1%} below threshold ({MIN_EDGE_TO_FLAG:.0%})"
                    )
                else:
                    flag_reason_parts.append(
                        f"Edge {edge:+.1%} meets threshold but confidence {conf_label} ({conf_numeric:.0%}) "
                        f"below {MIN_CONFIDENCE_TO_FLAG:.0%}"
                    )

            result = PricingResult(
                market_id=market_id,
                question=market_question,
                timestamp=datetime.utcnow().isoformat(),
                market_yes_price=yes_price,
                market_no_price=no_price,
                llm_estimated_probability=llm_prob,
                llm_confidence=conf_numeric,
                llm_confidence_label=conf_label,
                llm_reasoning=data.get("reasoning", ""),
                llm_key_factors=data.get("key_factors", []),
                llm_risks=data.get("risks", []),
                edge=round(edge, 4),
                abs_edge=round(abs_edge, 4),
                flagged=flagged,
                flag_reason="; ".join(flag_reason_parts),
                model=self.model,
            )

            # Cache and log
            if market_id:
                _save_cache(result)
            self._log_result(result)

            return result

        except json.JSONDecodeError as e:
            print(f"[LLMPricing] Invalid JSON from LLM for '{market_question[:40]}': {e}")
            return self._fallback_result(market_id, market_question, yes_price, no_price,
                                         f"JSON parse error: {e}")
        except Exception as e:
            print(f"[LLMPricing] API error for '{market_question[:40]}': {e}")
            return self._fallback_result(market_id, market_question, yes_price, no_price,
                                         f"API error: {e}")

    def _fallback_result(self, market_id: str, question: str,
                         yes_price: float, no_price: float,
                         error: str) -> PricingResult:
        """Return a safe fallback when LLM is unavailable."""
        return PricingResult(
            market_id=market_id,
            question=question,
            timestamp=datetime.utcnow().isoformat(),
            market_yes_price=yes_price,
            market_no_price=no_price,
            llm_estimated_probability=yes_price,
            llm_confidence=0.0,
            llm_confidence_label="low",
            llm_reasoning=f"LLM unavailable: {error}",
            llm_key_factors=[],
            llm_risks=[],
            edge=0.0,
            abs_edge=0.0,
            flagged=False,
            flag_reason=f"LLM unavailable: {error}",
            model="N/A",
        )

    # ── Batch pricing ─────────────────────────────────────────

    def price_markets(self, markets: List, batch_size: int = 5) -> List[PricingResult]:
        """
        Price multiple markets. Returns results with flags.
        'markets' can be EventMarket objects or plain dicts.

        Args:
            markets: List of market objects with question, outcomes, yes_price, no_price, etc.
            batch_size: Max markets to price in one call.

        Returns:
            List of PricingResult, sorted with flagged markets first.
        """
        results: List[PricingResult] = []
        for market in markets[:batch_size]:
            # Handle both EventMarket and dict interfaces
            if hasattr(market, "question"):
                question = market.question
                outcomes = market.outcomes
                yes_price = market.yes_price
                no_price = market.no_price
                market_id = market.market_id
                volume = market.volume
                liquidity = market.liquidity
                end_date_iso = getattr(market, "end_date_iso", None)
            else:
                question = market.get("question", "")
                outcomes = market.get("outcomes", ["Yes", "No"])
                prices = market.get("outcome_prices", [0, 0])
                yes_price = prices[0] if len(prices) > 0 else 0
                no_price = prices[1] if len(prices) > 1 else 0
                market_id = market.get("market_id", "")
                volume = float(market.get("volume", 0))
                liquidity = float(market.get("liquidity", 0))
                end_date_iso = market.get("end_date_iso")

            result = self.price_event_market(
                market_question=question,
                outcomes=outcomes,
                yes_price=yes_price,
                no_price=no_price,
                market_id=market_id,
                volume=volume,
                liquidity=liquidity,
                end_date_iso=end_date_iso,
            )
            results.append(result)
            # Brief pause to avoid rate limits
            import time
            time.sleep(0.5)

        # Sort: flagged first, then by abs_edge descending
        results.sort(key=lambda r: (not r.flagged, -r.abs_edge))
        return results

    # ── Logging / accuracy tracking ───────────────────────────

    def _load_log(self) -> List[Dict]:
        if os.path.exists(PRICING_LOG_FILE):
            try:
                with open(PRICING_LOG_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _log_result(self, result: PricingResult):
        """Log pricing result for accuracy tracking."""
        entry = result.to_dict()
        entry["logged_at"] = datetime.utcnow().isoformat()
        self.pricing_log.append(entry)
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(PRICING_LOG_FILE, "w") as f:
            json.dump(self.pricing_log, f, indent=2)

    def get_accuracy_stats(self) -> Dict:
        """Get accuracy statistics for this pricing engine."""
        if not self.pricing_log:
            return {"status": "no_data", "total_analyses": 0}

        total = len(self.pricing_log)
        flagged = [e for e in self.pricing_log if e.get("flagged")]
        high_conf = [e for e in self.pricing_log if e.get("llm_confidence", 0) >= 0.70]

        return {
            "status": "ok",
            "total_analyses": total,
            "flagged_opportunities": len(flagged),
            "high_confidence_analyses": len(high_conf),
            "avg_edge_flagged": (
                sum(e.get("abs_edge", 0) for e in flagged) / len(flagged)
                if flagged else 0
            ),
            "model": self.model,
        }


# ── CLI test ──────────────────────────────────────────────────

if __name__ == "__main__":
    from src.data.event_scanner import EventScanner

    print("LLM Pricing Engine Test")
    print("=" * 60)

    scanner = EventScanner()
    engine = LLMPricingEngine()

    # Get top markets
    markets = scanner.get_top_markets(limit=5)

    if not markets:
        print("No markets found. Testing with fallback data...")
        # Use dummy data for testing
        from src.data.event_scanner import EventMarket
        markets = [
            EventMarket(
                market_id="test_1",
                question="Will Bitcoin reach $100k by end of 2025?",
                outcomes=["Yes", "No"],
                outcome_prices=[0.35, 0.65],
                clob_token_ids=["0x1", "0x2"],
                volume=100000,
                liquidity=50000,
                end_date_iso="2025-12-31T23:59:59Z",
            ),
            EventMarket(
                market_id="test_2",
                question="Will the Lakers win the NBA Finals 2025?",
                outcomes=["Yes", "No"],
                outcome_prices=[0.08, 0.92],
                clob_token_ids=["0x3", "0x4"],
                volume=5000000,
                liquidity=200000,
                end_date_iso="2025-06-30T23:59:59Z",
            ),
        ]

    print(f"Analyzing {len(markets)} markets...\n")
    results = engine.price_markets(markets)

    for r in results:
        flag = "🔥 FLAGGED" if r.flagged else "      "
        print(f"{flag} [{r.llm_confidence_label.upper():>6}] {r.question[:55]}")
        print(f"      Market: {r.market_yes_price:.1%} | LLM: {r.llm_estimated_probability:.1%} | Edge: {r.edge:+.1%}")
        if r.flagged:
            print(f"      Reason: {r.flag_reason[:80]}")
        print()

    print("Accuracy stats:")
    print(json.dumps(engine.get_accuracy_stats(), indent=2))
