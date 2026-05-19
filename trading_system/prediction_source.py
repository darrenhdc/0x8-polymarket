"""
Prediction Source — abstract interface for pluggable prediction engines.

Any prediction source (DeepSeek LLM, heuristic strategies, your personal weather
model, an external API, a CSV file of manual predictions, etc.) implements this
interface.  The system reads predictions from one or more sources, computes the
edge vs the market, and feeds the result through risk_manager.

Architecture:

  YourPersonalPrediction   DeepSeekLLMPricer   HeuristicStrategyEngine
           │                      │                      │
           └──────────────────────┼──────────────────────┘
                                  │
                          EdgeComposer
                                  │
                          RiskManager
                                  │
                          TradeExecutor
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Optional, Any


# ── Data classes ────────────────────────────────────────────────

@dataclass
class MarketContext:
    """Minimal market snapshot that every prediction source receives."""
    market_id: str
    question: str
    outcomes: List[str]              # e.g. ["Yes", "No"] or ["Above 30C", "Below 30C"]
    outcome_prices: List[float]      # current market prices, same order as outcomes
    volume: float = 0.0
    liquidity: float = 0.0
    category: str = "Unknown"
    end_date_iso: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)  # any source-specific data


@dataclass
class Prediction:
    """Output of one prediction source for one market."""
    market_id: str
    source_name: str                 # "deepseek-v4-pro", "personal_weather", "heuristic", ...
    estimated_probability: float     # probability of YES (or outcome[0]) — always 0-1
    confidence: float                # 0-1, how confident the source is
    reasoning: str = ""
    key_factors: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class EdgeSignal:
    """Computed edge: difference between a prediction and the market price."""
    market_id: str
    question: str
    prediction: Prediction
    market_price: float              # current market price for the relevant outcome
    edge: float                      # prediction.estimated_probability - market_price
    abs_edge: float                  # |edge|
    flagged: bool                    # edge > threshold AND confidence > threshold
    flag_reason: str
    direction: str                   # "BUY_YES" or "BUY_NO"


# ── Abstract base class ─────────────────────────────────────────

class PredictionSource(ABC):
    """
    Every prediction engine implements this interface.

    Subclass and register with PredictionRegistry to plug into the system.
    No changes to agent.py, risk_manager, or any other module required.
    """

    def __init__(self, name: str):
        self.name = name
        self.prediction_count = 0

    @abstractmethod
    def predict(self, market: MarketContext) -> Optional[Prediction]:
        """
        Produce a probability estimate for a single market.
        Return None if this source cannot / should not predict this market.
        """
        ...

    @abstractmethod
    def can_predict(self, market: MarketContext) -> bool:
        """Return True if this source is applicable to the given market."""
        ...

    def batch_predict(self, markets: List[MarketContext]) -> List[Prediction]:
        """
        Predict multiple markets.  Override for batching (e.g. one LLM call).
        Default: calls predict() one by one.
        """
        results = []
        for m in markets:
            p = self.predict(m)
            if p:
                self.prediction_count += 1
                results.append(p)
        return results


# ── Registry — discovers and manages all registered sources ─────

class PredictionRegistry:
    """
    Holds all registered PredictionSources.  The agent queries this
    to get predictions from every active source, then fuses them through
    EdgeComposer.
    """

    def __init__(self):
        self._sources: Dict[str, PredictionSource] = {}

    def register(self, source: PredictionSource):
        self._sources[source.name] = source

    def unregister(self, name: str):
        self._sources.pop(name, None)

    def list_sources(self) -> List[str]:
        return list(self._sources.keys())

    def get(self, name: str) -> Optional[PredictionSource]:
        return self._sources.get(name)

    def predict_all(self, markets: List[MarketContext]) -> Dict[str, List[Prediction]]:
        """
        Run every registered source against every market.
        Returns {source_name: [Predictions...]}
        """
        results: Dict[str, List[Prediction]] = {}
        for name, source in self._sources.items():
            try:
                preds = source.batch_predict(markets)
                if preds:
                    results[name] = preds
            except Exception as e:
                print(f"[PredictionRegistry] source '{name}' error: {e}")
        return results


# ── Global singleton ────────────────────────────────────────────

_registry: Optional[PredictionRegistry] = None


def get_registry() -> PredictionRegistry:
    global _registry
    if _registry is None:
        _registry = PredictionRegistry()
    return _registry
