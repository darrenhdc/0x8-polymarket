"""Pluggable prediction interface for weather market edge computation.

Any prediction engine (GFS weather model, LLM analyzer, personal model, etc.)
implements PredictionSource and registers with PredictionRegistry.

Architecture:
    GFSPredictionSource   LLMPredictionSource   PersonalModelSource
              │                   │                       │
              └───────────────────┼───────────────────────┘
                                  │
                           PredictionRegistry
                                  │
                           EdgeComposer
                                  │
                    WeatherBacktester / SignalGenerator
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MarketContext:
    """Full context for a weather prediction market.

    Core fields (required):
        market_id, question, outcomes, outcome_prices

    Weather extension fields — populated by WeatherBacktester / signal generator:
        city, country, target_date, market_type, threshold_value,
        threshold_unit, variable, rule, latitude, longitude, location_id

    extra: arbitrary key-value bag for source-specific data.
        WeatherBacktester sets extra["price_date"] for historical mode.
    """
    market_id: str
    question: str
    outcomes: List[str] = field(default_factory=lambda: ["Yes", "No"])
    outcome_prices: List[float] = field(default_factory=lambda: [0.5, 0.5])

    # Weather fields
    city: str = ""
    country: str = ""
    target_date: str = ""         # YYYY-MM-DD
    market_type: str = ""         # temp_above | precip | snow
    threshold_value: float = 0.0
    threshold_unit: str = ""
    variable: str = ""            # temperature_2m_max | precipitation_sum | ...
    rule: str = "eq"              # eq | gte | lte
    latitude: float = 0.0
    longitude: float = 0.0
    location_id: str = ""

    # Aggregate metadata
    volume: float = 0.0
    liquidity: float = 0.0
    category: str = "weather"
    end_date_iso: Optional[str] = None

    # Arbitrary source-specific data (e.g. price_date for historical backtesting)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Prediction:
    """Output of one prediction source for one market.

    estimated_probability: P(YES outcome) — always in [0, 1].
    confidence: 0–1 quality score (1.0 = fully confident quantitative model).
    extra: structured numeric extras the source may populate for downstream
           display (e.g. {"gfs_raw": 29.1, "calib_bias": -0.78}).
    """
    market_id: str
    source_name: str
    estimated_probability: float
    confidence: float = 1.0
    reasoning: str = ""
    key_factors: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class EdgeSignal:
    """Computed edge: prediction vs market price.

    flagged == True means the signal meets the minimum edge + confidence
    thresholds and should be considered for trading.
    direction: "BUY_YES" | "BUY_NO" | "HOLD"
    """
    market_id: str
    question: str
    prediction: Prediction
    market_price: float
    edge: float
    abs_edge: float
    flagged: bool
    flag_reason: str
    direction: str


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------

class PredictionSource(ABC):
    """Every prediction engine implements this interface.

    Subclass, implement predict() and can_predict(), then register with
    PredictionRegistry.  No other module needs to change.

    Example (minimal GFS plug-in)::

        class MySource(PredictionSource):
            def __init__(self):
                super().__init__("my-source")

            def can_predict(self, market: MarketContext) -> bool:
                return market.market_type == "temp_above"

            def predict(self, market: MarketContext) -> Optional[Prediction]:
                prob = my_model(market.threshold_value, market.target_date)
                return Prediction(
                    market_id=market.market_id,
                    source_name=self.name,
                    estimated_probability=prob,
                    confidence=0.9,
                )
    """

    def __init__(self, name: str):
        self.name = name
        self.prediction_count = 0

    @abstractmethod
    def predict(self, market: MarketContext) -> Optional[Prediction]:
        """Produce a probability estimate for a single market.
        Return None if this source cannot predict this market."""
        ...

    @abstractmethod
    def can_predict(self, market: MarketContext) -> bool:
        """Return True if this source is applicable to the given market."""
        ...

    def batch_predict(self, markets: List[MarketContext]) -> List[Prediction]:
        """Predict multiple markets.  Override for batching (e.g. one API call).
        Default: calls predict() one by one and filters None results."""
        results = []
        for m in markets:
            p = self.predict(m)
            if p is not None:
                self.prediction_count += 1
                results.append(p)
        return results

    def close(self) -> None:
        """Release any held resources (DB connections, HTTP sessions, etc.)."""
        pass
