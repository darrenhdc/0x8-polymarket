from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.data.polymarket_history import PolymarketHistoryCollector


class FakeResponse:
    status_code = 200

    def __init__(self, history: list[dict]):
        self._history = history

    def json(self) -> dict:
        return {"history": self._history}


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def get(self, _url: str, *, params: dict, timeout: int) -> FakeResponse:
        self.calls.append(dict(params))
        return FakeResponse([{"t": params["startTs"], "p": 0.25}])


class PolymarketHistoryCollectorPriceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "weather_markets.db"
        self.collector = PolymarketHistoryCollector(db_path=self.db_path)

    def tearDown(self) -> None:
        self.collector.close()
        self.tmpdir.cleanup()

    def insert_market(
        self,
        market_id: str,
        *,
        city: str = "Hong Kong",
        token: str = "yes-token",
        target_date: str = "2026-06-10",
        start_date: str = "2026-06-08",
        end_date: str = "2026-06-10",
        active: int = 1,
        market_type: str = "temp_above",
    ) -> None:
        self.collector.conn.execute(
            """
            INSERT INTO markets (
                id, question, city, market_type, target_date, start_date,
                end_date, active, clob_token_ids
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market_id,
                "Will the highest temperature in Hong Kong be 24°C or below?",
                city,
                market_type,
                target_date,
                start_date,
                end_date,
                active,
                json.dumps([token, "no-token"]),
            ),
        )
        self.collector.conn.commit()

    def test_ingest_price_history_normalizes_raw_clob_rows_and_dedupes(self) -> None:
        self.insert_market("m1")

        inserted = self.collector.ingest_price_history(
            "m1",
            "yes-token",
            [
                {"t": 0, "p": "0.42"},
                {"timestamp": "1970-01-02T00:00:00+00:00", "price": 0.5},
                {"t": 0, "p": 0.42},
                {"bad": "row"},
            ],
        )

        self.assertEqual(inserted, 2)
        rows = self.collector.conn.execute(
            "SELECT timestamp, price FROM price_history ORDER BY timestamp"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("1970-01-01T00:00:00+00:00", 0.42),
                ("1970-01-02T00:00:00+00:00", 0.5),
            ],
        )

    def test_backfill_price_history_includes_active_overlapping_future_markets(self) -> None:
        fake_session = FakeSession()
        self.collector.close()
        self.collector = PolymarketHistoryCollector(
            db_path=self.db_path,
            session=fake_session,
        )
        self.insert_market("in-range", token="token-in-range")
        self.insert_market(
            "active-future",
            token="token-active-future",
            target_date="2026-06-12",
            start_date="2026-06-10",
            end_date="2026-06-12",
            active=1,
        )
        self.insert_market(
            "inactive-future",
            token="token-inactive-future",
            target_date="2026-06-12",
            start_date="2026-06-10",
            end_date="2026-06-12",
            active=0,
        )

        inserted = self.collector.backfill_price_history(
            start_date="2026-06-10",
            end_date="2026-06-11",
            city="Hong Kong",
            market_type="temp_above",
            target_start_date="2026-06-10",
            target_end_date="2026-06-11",
            include_active_overlap=True,
            sleep_seconds=0,
        )

        called_tokens = {call["market"] for call in fake_session.calls}
        self.assertEqual(inserted, 2)
        self.assertEqual(called_tokens, {"token-in-range", "token-active-future"})
        rows = self.collector.conn.execute(
            "SELECT COUNT(*) FROM price_history"
        ).fetchone()
        self.assertEqual(rows[0], 2)


if __name__ == "__main__":
    unittest.main()
