"""
Personal Prediction Source — reads your own predictions from a file (CSV / JSON).

Use this to feed your own weather temperature predictions, or any other
personal forecast, into the trading system.

File formats supported:

  CSV (data/your_predictions.csv):
    market_id,question,outcome,estimated_probability,confidence,reasoning
    w1,Beijing temp >35C July 1?,Above 35C,0.65,0.80,my weather model says hot
    w2,Shanghai rainfall >50mm July 2?,Yes,0.40,0.60,50% chance of typhoon

  JSON (data/your_predictions.json):
    [
      {
        "market_id": "w1",
        "question": "Beijing temp >35C July 1?",
        "outcome": "Above 35C",
        "estimated_probability": 0.65,
        "confidence": 0.80,
        "reasoning": "my weather model says hot"
      }
    ]

Each row must match a scanned market by question (fuzzy) or market_id (exact).
"""

import csv
import json
import os
from datetime import datetime
from typing import Dict, List, Optional

from src.data.prediction_interface import (
    PredictionSource,
    Prediction,
    MarketContext,
)


class PersonalPredictionSource(PredictionSource):
    """
    Reads your personal predictions from a file and matches them to
    Polymarket markets so the system can compute edges.
    """

    def __init__(self, file_path: str, name: str = "personal", refresh_seconds: int = 300):
        super().__init__(name)
        self.file_path = file_path
        self.refresh_seconds = refresh_seconds
        self._predictions: Dict[str, Dict] = {}   # market_id → prediction row
        self._last_load = 0.0
        self._load_file()

    # ── PredictionSource interface ──────────────────────────────

    def can_predict(self, market: MarketContext) -> bool:
        self._maybe_refresh()
        key = self._match_key(market)
        return key is not None

    def predict(self, market: MarketContext) -> Optional[Prediction]:
        self._maybe_refresh()
        key = self._match_key(market)
        if key is None:
            return None

        row = self._predictions[key]
        prob = float(row.get("estimated_probability", 0.5))
        conf = float(row.get("confidence", 0.5))

        return Prediction(
            market_id=market.market_id,
            source_name=self.name,
            estimated_probability=prob,
            confidence=conf,
            reasoning=row.get("reasoning", f"Personal prediction from {os.path.basename(self.file_path)}"),
            key_factors=[],
            risks=[],
        )

    # ── Internal ─────────────────────────────────────────────────

    def _match_key(self, market: MarketContext) -> Optional[str]:
        """Match a market to one of our predictions, by ID or fuzzy question."""
        # Exact ID match
        if market.market_id and market.market_id in self._predictions:
            return market.market_id

        # Fuzzy question match
        q = market.question.lower().strip()
        for mid, row in self._predictions.items():
            row_q = row.get("question", "").lower().strip()
            if row_q and (row_q in q or q in row_q):
                return mid

        return None

    def _maybe_refresh(self):
        """Reload file if enough time has passed."""
        import time
        now = time.time()
        if now - self._last_load > self.refresh_seconds:
            self._load_file()

    def _load_file(self):
        """Load predictions from CSV or JSON file."""
        import time
        self._predictions.clear()

        if not os.path.exists(self.file_path):
            import time
            self._last_load = time.time()
            return

        ext = os.path.splitext(self.file_path)[1].lower()

        try:
            if ext == ".csv":
                self._load_csv()
            elif ext == ".json":
                self._load_json()
            else:
                print(f"[PersonalPrediction] unsupported format: {ext}")
        except Exception as e:
            print(f"[PersonalPrediction] error loading {self.file_path}: {e}")

        import time
        self._last_load = time.time()
        print(f"[PersonalPrediction] loaded {len(self._predictions)} predictions from {os.path.basename(self.file_path)}")

    def _load_csv(self):
        with open(self.file_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                mid = row.get("market_id", "").strip()
                if mid:
                    self._predictions[mid] = row

    def _load_json(self):
        with open(self.file_path) as f:
            data = json.load(f)
        for item in data:
            mid = item.get("market_id", "").strip()
            if mid:
                self._predictions[mid] = item

    def add_prediction(self, market_id: str, question: str, outcome: str,
                       estimated_probability: float, confidence: float,
                       reasoning: str = ""):
        """Programmatically add a prediction (for interactive use)."""
        self._predictions[market_id] = {
            "market_id": market_id,
            "question": question,
            "outcome": outcome,
            "estimated_probability": estimated_probability,
            "confidence": confidence,
            "reasoning": reasoning,
        }

    def save(self):
        """Persist back to file."""
        ext = os.path.splitext(self.file_path)[1].lower()
        if ext == ".json":
            with open(self.file_path, "w") as f:
                json.dump(list(self._predictions.values()), f, indent=2)
        elif ext == ".csv":
            if not self._predictions:
                return
            with open(self.file_path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(next(iter(self._predictions.values())).keys()))
                writer.writeheader()
                for row in self._predictions.values():
                    writer.writerow(row)


# ── Demo / test ─────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

    demo_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "weather_predictions.json")
    os.makedirs(os.path.dirname(demo_path), exist_ok=True)

    demo_predictions = [
        {
            "market_id": "weather_demo_1",
            "question": "Will the temperature in Beijing exceed 35C on July 1 2026?",
            "outcome": "Above 35C",
            "estimated_probability": 0.65,
            "confidence": 0.80,
            "reasoning": "Based on historical July data + 3-day GFS model forecast",
        },
        {
            "market_id": "weather_demo_2",
            "question": "Will Shanghai rainfall exceed 50mm on July 2 2026?",
            "outcome": "Yes",
            "estimated_probability": 0.40,
            "confidence": 0.65,
            "reasoning": "ECMWF ensemble shows 40% probability of typhoon approach",
        },
    ]

    with open(demo_path, "w") as f:
        json.dump(demo_predictions, f, indent=2)

    # Load it
    source = PersonalPredictionSource(demo_path)

    # Simulate a market
    market = MarketContext(
        market_id="weather_demo_1",
        question="Will the temperature in Beijing exceed 35C on July 1 2026?",
        outcomes=["Above 35C", "Below 35C"],
        outcome_prices=[0.35, 0.65],
        volume=5000,
        liquidity=2000,
        category="weather",
    )

    if source.can_predict(market):
        pred = source.predict(market)
        edge = pred.estimated_probability - market.outcome_prices[0]
        print(f"Market: {market.question}")
        print(f"  Market says: {market.outcome_prices[0]:.0%}")
        print(f"  You predict: {pred.estimated_probability:.0%} (confidence: {pred.confidence:.0%})")
        print(f"  Edge: {edge:+.0%}")
        if abs(edge) > 0.10:
            print(f"  → FLAGGED — significant edge!")
    else:
        print("No prediction available for this market.")
