"""Prediction source registry — global singleton for all active sources.

Usage::
    from src.data.prediction_registry import get_registry, register_defaults

    # One-time setup (call once at startup)
    register_defaults()

    # Get the registry anywhere
    registry = get_registry()
    sources = registry.list_sources()

    # Or use it directly with EdgeComposer
    from src.data.edge_composer import EdgeComposer
    composer = EdgeComposer(registry, min_edge=0.05)
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .prediction_interface import MarketContext, Prediction, PredictionSource


class PredictionRegistry:
    """Holds all registered PredictionSources.

    Typical usage:
        registry = PredictionRegistry()
        registry.register(GFSPredictionSource(mode="live"))
        registry.register(GFSPrecipSource(mode="live"))
        signals = EdgeComposer(registry).analyse_markets(markets)
    """

    def __init__(self) -> None:
        self._sources: Dict[str, PredictionSource] = {}

    def register(self, source: PredictionSource) -> None:
        self._sources[source.name] = source

    def unregister(self, name: str) -> None:
        source = self._sources.pop(name, None)
        if source is not None:
            source.close()

    def list_sources(self) -> List[str]:
        return list(self._sources.keys())

    def get(self, name: str) -> Optional[PredictionSource]:
        return self._sources.get(name)

    def predict_all(
        self, markets: List[MarketContext]
    ) -> Dict[str, List[Prediction]]:
        """Run every registered source against every market.
        Returns {source_name: [Predictions...]}."""
        results: Dict[str, List[Prediction]] = {}
        for name, source in self._sources.items():
            try:
                preds = source.batch_predict(markets)
                if preds:
                    results[name] = preds
            except Exception as exc:
                print(f"[PredictionRegistry] source '{name}' error: {exc}")
        return results

    def close(self) -> None:
        """Close all registered sources."""
        for source in self._sources.values():
            source.close()
        self._sources.clear()


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_registry: Optional[PredictionRegistry] = None


def get_registry() -> PredictionRegistry:
    """Return the process-global PredictionRegistry singleton."""
    global _registry
    if _registry is None:
        _registry = PredictionRegistry()
    return _registry


def register_defaults(
    mode: str = "live",
    gfs_db_path=None,
    market_db_path=None,
) -> PredictionRegistry:
    """Register GFSPredictionSource and GFSPrecipSource with the global registry.

    Call this once at application startup (e.g. in cli.py or signal generator).
    Subsequent calls with the same sources are idempotent (they replace the
    existing registration).

    Args:
        mode: "live" (API) or "historical" (DB lookup for backtesting)
        gfs_db_path: optional override for gfs_forecasts.db path
        market_db_path: optional override for weather_markets.db path
    """
    from .gfs_prediction import GFSPredictionSource, GFSPrecipSource

    registry = get_registry()

    # Close and replace any existing sources with same names
    for name in ("gfs-temperature", "gfs-precip"):
        registry.unregister(name)

    registry.register(GFSPredictionSource(
        gfs_db_path=gfs_db_path,
        market_db_path=market_db_path,
        mode=mode,
    ))
    registry.register(GFSPrecipSource(
        gfs_db_path=gfs_db_path,
        market_db_path=market_db_path,
        mode=mode,
    ))
    return registry
