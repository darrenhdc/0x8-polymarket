"""
Historical Data Collector — captures Polymarket market snapshots for backtesting.

Runs in the background, saves periodic snapshots to data/historical/.
Also tracks resolved markets to compute prediction accuracy.

Usage:
  collector = HistoricalCollector()
  collector.capture_snapshot()              # one snapshot of top markets
  collector.run_collector(interval_min=15)   # run every 15 min
"""

import json
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests
import config


HISTORICAL_DIR = os.path.join(config.DATA_DIR, "historical")
RESOLVED_FILE = os.path.join(config.DATA_DIR, "resolved_markets.json")


class MarketSnapshot:
    """A single point-in-time capture of market state."""

    def __init__(self, market_id: str, question: str, outcomes: List[str],
                 outcome_prices: List[float], volume: float, liquidity: float,
                 category: str = "", end_date_iso: str = "",
                 timestamp: str = ""):
        self.market_id = market_id
        self.question = question
        self.outcomes = outcomes
        self.outcome_prices = outcome_prices
        self.volume = volume
        self.liquidity = liquidity
        self.category = category
        self.end_date_iso = end_date_iso
        self.timestamp = timestamp or datetime.utcnow().isoformat()

    def to_dict(self) -> Dict:
        return self.__dict__

    @classmethod
    def from_dict(cls, d: Dict) -> "MarketSnapshot":
        return cls(**d)


def _snapshot_path(date_str: str) -> str:
    return os.path.join(HISTORICAL_DIR, f"snapshot_{date_str}.json")


def _price_file(market_id: str) -> str:
    return os.path.join(HISTORICAL_DIR, "prices", f"{market_id}.jsonl")


class HistoricalCollector:
    """
    Collects and stores historical market data.
    """

    def __init__(self):
        os.makedirs(HISTORICAL_DIR, exist_ok=True)
        os.makedirs(os.path.join(HISTORICAL_DIR, "prices"), exist_ok=True)

    # ── Snapshot capture ──────────────────────────────────────

    def capture_snapshot(self, markets: List[Dict] = None) -> int:
        """
        Capture current prices for a list of markets.
        If `markets` is None, scans from Polymarket API.
        Returns number of markets captured.
        """
        if markets is None:
            try:
                from market_data import MarketData
                md = MarketData()
                markets = md.scan_opportunities()
            except Exception as e:
                print(f"[HistoricalCollector] scan error: {e}")
                return 0

        today = datetime.utcnow().strftime("%Y-%m-%d")
        snapshots = []

        for m in markets:
            try:
                # Parse outcomes and prices
                outcomes_raw = m.get("outcomes", ["Yes", "No"])
                if isinstance(outcomes_raw, str):
                    outcomes_raw = json.loads(outcomes_raw)

                prices_raw = m.get("outcomePrices", [])
                if isinstance(prices_raw, str):
                    prices_raw = json.loads(prices_raw)

                prices = []
                for p in prices_raw:
                    try:
                        prices.append(float(p))
                    except (ValueError, TypeError):
                        prices.append(0.0)

                tags = m.get("tags", [])
                category = tags[0].get("name", "") if tags else ""

                snap = MarketSnapshot(
                    market_id=m.get("id", m.get("conditionId", "")),
                    question=m.get("question", ""),
                    outcomes=outcomes_raw,
                    outcome_prices=prices,
                    volume=float(m.get("volume", 0) or 0),
                    liquidity=float(m.get("liquidity", 0) or 0),
                    category=category,
                    end_date_iso=m.get("end_date_iso", ""),
                )
                snapshots.append(snap.to_dict())

                # Also append to per-market price history
                self._append_price_line(snap)

            except Exception as e:
                print(f"[HistoricalCollector] error on {m.get('id','?')}: {e}")
                continue

        # Save daily snapshot
        path = _snapshot_path(today)
        with open(path, "w") as f:
            json.dump(snapshots, f, indent=2)

        print(f"[HistoricalCollector] captured {len(snapshots)} markets → {path}")
        return len(snapshots)

    def _append_price_line(self, snap: MarketSnapshot):
        """Append one price line to the per-market JSONL file."""
        os.makedirs(os.path.dirname(_price_file(snap.market_id)), exist_ok=True)
        line = {
            "ts": snap.timestamp,
            "yes": snap.outcome_prices[0] if len(snap.outcome_prices) > 0 else None,
            "no": snap.outcome_prices[1] if len(snap.outcome_prices) > 1 else None,
            "volume": snap.volume,
        }
        with open(_price_file(snap.market_id), "a") as f:
            f.write(json.dumps(line) + "\n")

    # ── Price history retrieval ────────────────────────────────

    def get_price_history(self, market_id: str) -> List[Dict]:
        """Return all recorded price points for a market."""
        path = _price_file(market_id)
        if not os.path.exists(path):
            return []
        history = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    history.append(json.loads(line))
        return history

    def get_snapshot(self, date_str: str) -> List[Dict]:
        """Load a historical daily snapshot."""
        path = _snapshot_path(date_str)
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return json.load(f)

    def list_snapshots(self) -> List[str]:
        """List available snapshot dates."""
        files = os.listdir(HISTORICAL_DIR)
        return sorted([
            f.replace("snapshot_", "").replace(".json", "")
            for f in files if f.startswith("snapshot_") and f.endswith(".json")
        ])

    # ── Resolution tracking ────────────────────────────────────

    def check_resolutions(self) -> List[Dict]:
        """
        Check if any tracked markets have resolved.
        Returns list of resolutions found.
        """
        # We track resolved markets by periodically checking the Polymarket API
        # for markets where outcomePrices show 0 or 1.
        resolved = []

        for date_str in self.list_snapshots()[-7:]:  # last 7 days
            snapshots = self.get_snapshot(date_str)
            for s in snapshots:
                prices = s.get("outcome_prices", [])
                if not prices:
                    continue
                # A resolved market has one outcome at ~1.0 and others ~0.0
                if any(abs(p - 1.0) < 0.001 for p in prices) or all(p < 0.001 for p in prices):
                    resolved.append({
                        "market_id": s["market_id"],
                        "question": s["question"],
                        "final_prices": prices,
                        "resolved_date": date_str,
                    })

        # Save resolved markets
        self._save_resolved(resolved)
        return resolved

    def _save_resolved(self, resolved: List[Dict]):
        existing = self._load_resolved()
        known_ids = {r["market_id"] for r in existing}
        new = [r for r in resolved if r["market_id"] not in known_ids]
        existing.extend(new)
        with open(RESOLVED_FILE, "w") as f:
            json.dump(existing, f, indent=2)

    def _load_resolved(self) -> List[Dict]:
        if os.path.exists(RESOLVED_FILE):
            with open(RESOLVED_FILE) as f:
                return json.load(f)
        return []

    def get_resolved_markets(self) -> List[Dict]:
        return self._load_resolved()

    # ── CLOB prices-history (补充历史市价) ───────────────────────

    CLOB_PRICES_HISTORY_URL = "https://clob.polymarket.com/prices-history"
    GAMMA_EVENTS_URL        = "https://gamma-api.polymarket.com/events"
    CLOB_MAX_WINDOW_DAYS    = 14   # API 最多支持 14 天单次查询

    def fetch_clob_price_history(
        self,
        token_id: str,
        start_date: str,
        end_date: str,
        fidelity: int = 1440,
    ) -> List[Dict]:
        """
        从 CLOB API 拉指定 token 的历史价格序列。
        自动分段处理超过14天的区间（API 限制）。

        Args:
            token_id:   CLOB token ID（来自 clobTokenIds[0]，代表 YES token）
            start_date: "YYYY-MM-DD"
            end_date:   "YYYY-MM-DD"
            fidelity:   分钟粒度（60=每小时, 1440=每天）

        Returns:
            list of {"ts": "YYYY-MM-DD", "yes": float}
        """
        try:
            start_dt = datetime.fromisoformat(start_date)
            end_dt   = datetime.fromisoformat(end_date) + timedelta(days=1)
            result: List[Dict] = []
            seen_dates: set = set()

            # 分段：每次最多 CLOB_MAX_WINDOW_DAYS 天
            window = timedelta(days=self.CLOB_MAX_WINDOW_DAYS)
            seg_start = start_dt
            while seg_start < end_dt:
                seg_end = min(seg_start + window, end_dt)
                start_ts = int(seg_start.timestamp())
                end_ts   = int(seg_end.timestamp())

                resp = requests.get(
                    self.CLOB_PRICES_HISTORY_URL,
                    params={
                        "market":   token_id,
                        "startTs":  start_ts,
                        "endTs":    end_ts,
                        "fidelity": fidelity,
                    },
                    timeout=15,
                )
                if resp.status_code != 200:
                    print(f"[CLOB-hist] API error {resp.status_code}: {resp.text[:100]}")
                    seg_start = seg_end
                    continue

                for pt in resp.json().get("history", []):
                    ts_str = datetime.utcfromtimestamp(pt["t"]).strftime("%Y-%m-%d")
                    if ts_str not in seen_dates:
                        seen_dates.add(ts_str)
                        result.append({"ts": ts_str, "yes": float(pt["p"])})

                seg_start = seg_end

            return sorted(result, key=lambda x: x["ts"])

        except Exception as e:
            print(f"[CLOB-hist] fetch error: {e}")
            return []

    def backfill_market(
        self,
        condition_id: str,
        token_id: str,
        question: str,
        start_date: str,
        end_date: str = None,
        outcomes: List[str] = None,
        category: str = "",
        fidelity: int = 1440,
    ) -> int:
        """
        用 CLOB prices-history 补充一个市场的历史价格到本地 JSONL 文件。
        相当于"时光机快照"——填补你没有存快照的那段历史。

        Args:
            condition_id: Polymarket conditionId（用作 market_id）
            token_id:     clobTokenIds[0]（YES token）
            question:     市场问题文本
            start_date:   "YYYY-MM-DD"
            end_date:     "YYYY-MM-DD"（默认：今天）
            outcomes:     ["Yes", "No"]（默认）
            category:     市场类别标签
            fidelity:     分钟粒度

        Returns:
            写入的数据点数量。
        """
        if end_date is None:
            end_date = datetime.utcnow().strftime("%Y-%m-%d")
        if outcomes is None:
            outcomes = ["Yes", "No"]

        history = self.fetch_clob_price_history(token_id, start_date, end_date, fidelity)
        if not history:
            print(f"[CLOB-hist] 无数据: {question[:50]}")
            return 0

        os.makedirs(os.path.join(HISTORICAL_DIR, "prices"), exist_ok=True)

        written = 0
        for pt in history:
            yes_price = pt["yes"]
            no_price  = round(1.0 - yes_price, 6)

            snap = MarketSnapshot(
                market_id     = condition_id,
                question      = question,
                outcomes      = outcomes,
                outcome_prices= [yes_price, no_price],
                volume        = 0.0,    # CLOB prices-history 不含成交量
                liquidity     = 0.0,
                category      = category,
                timestamp     = pt["ts"] + "T00:00:00",
            )

            # 写入 per-market JSONL
            self._append_price_line(snap)

            # 同时写入日快照文件（让 backtester.run() 能读到）
            snap_path = _snapshot_path(pt["ts"])
            existing: List[Dict] = []
            if os.path.exists(snap_path):
                with open(snap_path) as f:
                    try:
                        existing = json.load(f)
                    except json.JSONDecodeError:
                        existing = []

            # 避免重复写入同一市场同一天
            known_ids = {s["market_id"] for s in existing}
            if condition_id not in known_ids:
                existing.append(snap.to_dict())
                with open(snap_path, "w") as f:
                    json.dump(existing, f, indent=2)
                written += 1

        print(f"[CLOB-hist] backfill完成: {question[:50]} | {written} 天写入 → {start_date}~{end_date}")
        return written

    def backfill_weather_markets(
        self,
        start_date: str,
        end_date: str = None,
        limit: int = 50,
        fidelity: int = 1440,
    ) -> Dict[str, int]:
        """
        自动扫描所有天气类市场并批量补充历史价格。

        Returns:
            {"market_id": written_count, ...}
        """
        if end_date is None:
            end_date = datetime.utcnow().strftime("%Y-%m-%d")

        results: Dict[str, int] = {}

        try:
            resp = requests.get(
                self.GAMMA_EVENTS_URL,
                params={"limit": limit, "closed": "false", "tag_slug": "weather"},
                timeout=15,
            )
            if resp.status_code != 200:
                print(f"[CLOB-hist] Gamma API error {resp.status_code}")
                return results

            events = resp.json()
        except Exception as e:
            print(f"[CLOB-hist] Gamma fetch error: {e}")
            return results

        total_markets = 0
        for event in events:
            for market in event.get("markets", []):
                condition_id = market.get("conditionId", "")
                token_ids_raw = market.get("clobTokenIds", "[]")
                if isinstance(token_ids_raw, str):
                    try:
                        token_ids = json.loads(token_ids_raw)
                    except json.JSONDecodeError:
                        continue
                else:
                    token_ids = token_ids_raw

                if not token_ids or not condition_id:
                    continue

                yes_token = token_ids[0]
                question  = market.get("question", "")

                tags = event.get("tags", [])
                category = tags[0].get("slug", "weather") if tags else "weather"

                outcomes_raw = market.get("outcomes", '["Yes","No"]')
                if isinstance(outcomes_raw, str):
                    try:
                        outcomes = json.loads(outcomes_raw)
                    except json.JSONDecodeError:
                        outcomes = ["Yes", "No"]
                else:
                    outcomes = outcomes_raw

                n = self.backfill_market(
                    condition_id = condition_id,
                    token_id     = yes_token,
                    question     = question,
                    start_date   = start_date,
                    end_date     = end_date,
                    outcomes     = outcomes,
                    category     = category,
                    fidelity     = fidelity,
                )
                results[condition_id] = n
                total_markets += 1

        total_written = sum(results.values())
        print(f"[CLOB-hist] 批量补充完成: {total_markets} 市场, {total_written} 条价格记录写入")
        return results

if __name__ == "__main__":
    collector = HistoricalCollector()

    # Capture one snapshot
    print("Capturing current market snapshot...")
    count = collector.capture_snapshot()
    print(f"Done: {count} markets\n")

    # Show available snapshots
    dates = collector.list_snapshots()
    print(f"Available snapshots: {dates}")

    if dates:
        latest = collector.get_snapshot(dates[-1])
        print(f"\nLatest snapshot ({dates[-1]}): {len(latest)} markets")
        for m in latest[:3]:
            print(f"  {m['question'][:50]}... prices={m['outcome_prices']}")
