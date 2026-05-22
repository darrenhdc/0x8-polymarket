"""Edge Composer — computes edge between prediction(s) and market price.

Standalone functions compute_edge() and fuse_predictions() can be imported
directly.  EdgeComposer wraps a PredictionRegistry for batch analysis.

Typical usage::

    from src.data.edge_composer import EdgeComposer, compute_edge
    from src.data.prediction_registry import get_registry

    # Single prediction
    signal = compute_edge(prediction, market_price=0.30, market=ctx, min_edge=0.05)

    # Full batch via registry
    composer = EdgeComposer(registry, min_edge=0.05)
    signals  = composer.analyse_markets(all_market_contexts)
    flagged  = [s for s in signals if s.flagged]
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from .prediction_interface import (
    EdgeSignal,
    MarketContext,
    Prediction,
    PredictionSource,
)
from .prediction_registry import PredictionRegistry


# ---------------------------------------------------------------------------
# Core edge function
# ---------------------------------------------------------------------------

def compute_edge(
    prediction: Prediction,
    market_price: float,
    market: MarketContext,
    min_edge: float = 0.05,
    min_confidence: float = 0.0,
    max_sane_edge: float = 0.60,
) -> EdgeSignal:
    """Compute the YES-side edge for a single prediction.

    Args:
        prediction: output of a PredictionSource.predict() call
        market_price: current YES-token price (0–1), already clamped
        market: original market context
        min_edge: flag threshold on |edge|
        min_confidence: flag threshold on prediction.confidence (0 = ignore)
        max_sane_edge: edges above this are capped as suspected model error

    Returns:
        EdgeSignal with flagged=True when |edge| >= min_edge AND
        confidence >= min_confidence.
    """
    edge = prediction.estimated_probability - market_price
    abs_edge = abs(edge)

    if abs_edge > max_sane_edge:
        return EdgeSignal(
            market_id=market.market_id,
            question=market.question,
            prediction=prediction,
            market_price=market_price,
            edge=0.0,
            abs_edge=0.0,
            flagged=False,
            flag_reason=(
                f"Edge {edge:+.1%} exceeds sanity cap "
                f"({max_sane_edge:.0%}) — suspected model error"
            ),
            direction="HOLD",
        )

    passes_edge       = abs_edge >= min_edge
    passes_confidence = prediction.confidence >= min_confidence

    if passes_edge and passes_confidence:
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
                f"→ {direction}"
            ),
            direction=direction,
        )

    # Not flagged — explain why
    parts: list[str] = []
    if not passes_edge:
        parts.append(f"Edge {edge:+.1%} < {min_edge:.0%}")
    if not passes_confidence:
        parts.append(
            f"Confidence {prediction.confidence:.0%} < {min_confidence:.0%}"
        )

    return EdgeSignal(
        market_id=market.market_id,
        question=market.question,
        prediction=prediction,
        market_price=market_price,
        edge=round(edge, 4),
        abs_edge=round(abs_edge, 4),
        flagged=False,
        flag_reason="; ".join(parts),
        direction="HOLD",
    )


# ---------------------------------------------------------------------------
# Multi-source fusion
# ---------------------------------------------------------------------------

def fuse_predictions(
    predictions: List[Prediction],
    method: str = "confidence_weighted",
) -> Tuple[float, float, str]:
    """Fuse multiple predictions into a single (probability, confidence, method).

    Methods:
        "confidence_weighted" — weighted average by confidence (default)
        "simple_average"      — arithmetic mean
        "best_confidence"     — take the single highest-confidence prediction
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
            avg = sum(p.estimated_probability for p in predictions) / len(predictions)
            return avg, 0.0, "simple_average"
        weighted = (
            sum(p.estimated_probability * p.confidence for p in predictions) / total_conf
        )
        avg_conf = sum(p.confidence for p in predictions) / len(predictions)
        return weighted, avg_conf, "confidence_weighted"

    # simple_average
    avg = sum(p.estimated_probability for p in predictions) / len(predictions)
    avg_conf = sum(p.confidence for p in predictions) / len(predictions)
    return avg, avg_conf, "simple_average"


# ---------------------------------------------------------------------------
# EdgeComposer — batch analysis via registry
# ---------------------------------------------------------------------------

class EdgeComposer:
    """Runs all registered prediction sources against markets and produces edges.

    Usage::
        composer = EdgeComposer(registry, min_edge=0.05, min_confidence=0.0)
        signals  = composer.analyse_markets(market_list)
        for sig in signals:
            if sig.flagged:
                print(sig.direction, sig.edge)
    """

    def __init__(
        self,
        registry: "PredictionRegistry",
        min_edge: float = 0.05,
        min_confidence: float = 0.0,
        fusion_method: str = "confidence_weighted",
        max_sane_edge: float = 0.60,
    ):
        self.registry = registry
        self.min_edge = min_edge
        self.min_confidence = min_confidence
        self.fusion_method = fusion_method
        self.max_sane_edge = max_sane_edge

    def analyse_market(self, market: MarketContext) -> Optional[EdgeSignal]:
        """Run all applicable sources on one market, fuse, compute edge."""
        all_preds: List[Prediction] = []
        for _name, source in self.registry._sources.items():
            if source.can_predict(market):
                p = source.predict(market)
                if p is not None:
                    all_preds.append(p)

        if not all_preds:
            return None

        prob, conf, method = fuse_predictions(all_preds, method=self.fusion_method)

        # Build a fused prediction (carries extras from first source for display)
        fused_extra = all_preds[0].extra if all_preds else {}
        fused = Prediction(
            market_id=market.market_id,
            source_name=f"fused({method})[{'+'.join(p.source_name for p in all_preds)}]",
            estimated_probability=prob,
            confidence=conf,
            reasoning=f"Fused {len(all_preds)} source(s) via {method}",
            extra=fused_extra,
        )

        market_price = market.outcome_prices[0] if market.outcome_prices else 0.5
        return compute_edge(
            fused,
            market_price,
            market,
            min_edge=self.min_edge,
            min_confidence=self.min_confidence,
            max_sane_edge=self.max_sane_edge,
        )

    def analyse_markets(self, markets: List[MarketContext]) -> List[EdgeSignal]:
        """Batch analysis.  Flagged signals returned first, sorted by |edge|."""
        results: List[EdgeSignal] = []
        for m in markets:
            sig = self.analyse_market(m)
            if sig is not None:
                results.append(sig)
        results.sort(key=lambda s: (not s.flagged, -s.abs_edge))
        return results

    def get_flagged(self, markets: List[MarketContext]) -> List[EdgeSignal]:
        """Return only flagged (tradeable) signals."""
        return [s for s in self.analyse_markets(markets) if s.flagged]
