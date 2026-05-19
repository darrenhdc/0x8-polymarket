"""
Edge Composer — computes the edge between prediction(s) and market price.

Takes predictions from one or more PredictionSources, fuses them (simple
average, weighted by confidence, or Bayesian), and produces EdgeSignals.

Default strategy: single source → direct edge.  Multi-source → confidence-weighted.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from prediction_source import Prediction, MarketContext, PredictionSource, PredictionRegistry


# ── Data classes ────────────────────────────────────────────────

@dataclass
class EdgeSignal:
    """Result of edge computation: prediction vs market price."""
    market_id: str
    question: str
    prediction: Prediction
    market_price: float
    edge: float
    abs_edge: float
    flagged: bool
    flag_reason: str
    direction: str    # "BUY_YES", "BUY_NO", or "HOLD"


# ── Edge calculation ────────────────────────────────────────────

def compute_edge(
    prediction: Prediction,
    market_price: float,
    market: MarketContext,
    min_edge: float = 0.10,          # minimum |edge| to flag
    min_confidence: float = 0.70,    # minimum confidence to flag
    max_sane_edge: float = 0.40,     # over this = model error
) -> EdgeSignal:
    """
    Compute edge for a single prediction source vs market.
    Returns an EdgeSignal with flag status.
    """
    edge = prediction.estimated_probability - market_price
    abs_edge = abs(edge)

    # –– sanity cap ––
    if abs_edge > max_sane_edge:
        return EdgeSignal(
            market_id=market.market_id,
            question=market.question,
            prediction=prediction,
            market_price=market_price,
            edge=0.0,
            abs_edge=0.0,
            flagged=False,
            flag_reason=f"Edge {edge:+.1%} exceeds sanity limit ({max_sane_edge:.0%}) — suspected model error",
            direction="HOLD",
        )

    # –– flag check ––
    if abs_edge >= min_edge and prediction.confidence >= min_confidence:
        direction = "BUY_YES" if edge > 0 else "BUY_NO"
        return EdgeSignal(
            market_id=market.market_id,
            question=market.question,
            prediction=prediction,
            market_price=market_price,
            edge=round(edge, 4),
            abs_edge=round(abs_edge, 4),
            flagged=True,
            flag_reason=(
                f"Edge {edge:+.1%} >= {min_edge:.0%}, "
                f"confidence {prediction.confidence:.0%} >= {min_confidence:.0%} "
                f"→ FLAGGED for {direction}"
            ),
            direction=direction,
        )

    # –– not flagged ––
    reason_parts = []
    if abs_edge < min_edge:
        reason_parts.append(f"Edge {edge:+.1%} below threshold ({min_edge:.0%})")
    if prediction.confidence < min_confidence:
        reason_parts.append(f"Confidence {prediction.confidence:.0%} below {min_confidence:.0%}")

    return EdgeSignal(
        market_id=market.market_id,
        question=market.question,
        prediction=prediction,
        market_price=market_price,
        edge=round(edge, 4),
        abs_edge=round(abs_edge, 4),
        flagged=False,
        flag_reason="; ".join(reason_parts),
        direction="HOLD",
    )


def fuse_predictions(
    predictions: List[Prediction],
    method: str = "confidence_weighted",
) -> Tuple[float, float, str]:
    """
    Fuse multiple predictions into one estimate.

    Args:
        predictions: list of Prediction from different sources
        method:
            "confidence_weighted" — weighted average by confidence (default)
            "simple_average"      — arithmetic mean
            "best_confidence"     — take the prediction with highest confidence

    Returns:
        (fused_probability, fused_confidence, method_used)
    """
    if not predictions:
        return 0.5, 0.0, "no_predictions"

    if len(predictions) == 1:
        p = predictions[0]
        return p.estimated_probability, p.confidence, "single_source"

    if method == "best_confidence":
        best = max(predictions, key=lambda p: p.confidence)
        return best.estimated_probability, best.confidence, "best_confidence"

    if method == "confidence_weighted":
        total_conf = sum(p.confidence for p in predictions)
        if total_conf == 0:
            return sum(p.estimated_probability for p in predictions) / len(predictions), 0.0, "simple_average"
        weighted = sum(p.estimated_probability * p.confidence for p in predictions) / total_conf
        avg_conf = sum(p.confidence for p in predictions) / len(predictions)
        return weighted, avg_conf, "confidence_weighted"

    # simple average
    avg = sum(p.estimated_probability for p in predictions) / len(predictions)
    avg_conf = sum(p.confidence for p in predictions) / len(predictions)
    return avg, avg_conf, "simple_average"


# ── Composer — ties sources to market context ───────────────────

class EdgeComposer:
    """
    Runs registered prediction sources against markets, computes edges,
    and returns flagged signals sorted by |edge| descending.
    """

    def __init__(
        self,
        registry: PredictionRegistry,
        min_edge: float = 0.10,
        min_confidence: float = 0.70,
        fusion_method: str = "confidence_weighted",
    ):
        self.registry = registry
        self.min_edge = min_edge
        self.min_confidence = min_confidence
        self.fusion_method = fusion_method

    def analyse_market(self, market: MarketContext) -> Optional[EdgeSignal]:
        """
        Run all registered sources on one market, fuse predictions,
        compute edge, return signal.
        """
        all_preds: List[Prediction] = []
        for name, source in self.registry._sources.items():
            if source.can_predict(market):
                p = source.predict(market)
                if p:
                    all_preds.append(p)

        if not all_preds:
            return None

        prob, conf, method = fuse_predictions(all_preds, method=self.fusion_method)

        # Create a synthetic "fused" prediction
        fused = Prediction(
            market_id=market.market_id,
            source_name=f"fused({method})",
            estimated_probability=prob,
            confidence=conf,
            reasoning=f"Fused {len(all_preds)} sources via {method}",
            key_factors=[],
            risks=[],
        )

        # Edge is computed against outcome_prices[0] (the "YES" equivalent)
        market_price = market.outcome_prices[0] if market.outcome_prices else 0.5
        return compute_edge(
            fused, market_price, market,
            min_edge=self.min_edge,
            min_confidence=self.min_confidence,
        )

    def analyse_markets(self, markets: List[MarketContext]) -> List[EdgeSignal]:
        """Batch analysis.  Returns flagged first, then sorted by abs_edge."""
        results = []
        for m in markets:
            signal = self.analyse_market(m)
            if signal:
                results.append(signal)

        results.sort(key=lambda s: (not s.flagged, -s.abs_edge))
        return results

    def get_flagged(self, markets: List[MarketContext]) -> List[EdgeSignal]:
        """Return only the flagged (tradeable) signals."""
        return [s for s in self.analyse_markets(markets) if s.flagged]
