"""
Trade Journal – detailed reasoning log for every trade decision.

Every BUY / SELL / SKIP is recorded with:
  • market context (prices, volume, liquidity, spread)
  • strategy that triggered the signal
  • full reasoning text
  • portfolio state at decision time
  • outcome (filled price, order id, P&L on close)

Use `python3 trade_journal.py` to print a human-readable review.
"""
import json
import os
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, field, asdict

from src.core import config


@dataclass
class JournalEntry:
    """One row in the trade journal."""
    entry_id: str
    timestamp: str
    # ── market ──
    market_id: str
    market_question: str
    category: str
    # ── signal ──
    action: str          # BUY_YES, BUY_NO, SELL, SKIP
    strategy: str        # which strategy produced the signal
    confidence: float
    reasoning: str       # AI-generated explanation
    # ── market snapshot ──
    yes_price: float
    no_price: float
    spread: float        # |1 - yes - no|
    volume: float
    liquidity: float
    # ── order details (filled after execution) ──
    executed: bool = False
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    fill_size_usd: Optional[float] = None
    tokens: Optional[float] = None
    # ── portfolio at decision time ──
    portfolio_cash: float = 0.0
    portfolio_value: float = 0.0
    portfolio_exposure: float = 0.0
    open_positions: int = 0
    # ── post-trade review (filled on close) ──
    close_price: Optional[float] = None
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None
    close_reason: Optional[str] = None  # stop_loss, take_profit, manual
    closed_at: Optional[str] = None
    # ── mode ──
    paper_trade: bool = True

    def to_dict(self) -> Dict:
        return asdict(self)


class TradeJournal:
    """Persist and query the trade journal."""

    def __init__(self):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        self.entries: List[JournalEntry] = self._load()

    # ── persistence ──────────────────────────────────────────

    def _load(self) -> List[JournalEntry]:
        if os.path.exists(config.TRADE_JOURNAL_FILE):
            try:
                with open(config.TRADE_JOURNAL_FILE, "r") as f:
                    return [JournalEntry(**e) for e in json.load(f)]
            except Exception as e:
                print(f"Error loading trade journal: {e}")
        return []

    def save(self):
        with open(config.TRADE_JOURNAL_FILE, "w") as f:
            json.dump([e.to_dict() for e in self.entries], f, indent=2)

    # ── write ────────────────────────────────────────────────

    def _next_id(self) -> str:
        return f"J{datetime.utcnow().strftime('%Y%m%d%H%M%S')}{len(self.entries):04d}"

    def log_decision(
        self,
        market_id: str,
        market_question: str,
        category: str,
        action: str,
        strategy: str,
        confidence: float,
        reasoning: str,
        market_snapshot: Dict,
        portfolio_summary: Dict,
        paper_trade: bool = True,
    ) -> JournalEntry:
        """Record a new decision (before execution)."""
        entry = JournalEntry(
            entry_id=self._next_id(),
            timestamp=datetime.utcnow().isoformat(),
            market_id=market_id,
            market_question=market_question,
            category=category,
            action=action,
            strategy=strategy,
            confidence=confidence,
            reasoning=reasoning,
            yes_price=market_snapshot.get("yes_price", 0),
            no_price=market_snapshot.get("no_price", 0),
            spread=market_snapshot.get("spread", 0),
            volume=market_snapshot.get("volume", 0),
            liquidity=market_snapshot.get("liquidity", 0),
            portfolio_cash=portfolio_summary.get("cash", 0),
            portfolio_value=portfolio_summary.get("total_value", 0),
            portfolio_exposure=portfolio_summary.get("total_exposure", 0),
            open_positions=portfolio_summary.get("positions_count", 0),
            paper_trade=paper_trade,
        )
        self.entries.append(entry)
        self.save()
        return entry

    def mark_executed(
        self,
        entry_id: str,
        order_id: str,
        fill_price: float,
        fill_size_usd: float,
        tokens: float,
    ):
        """Update a journal entry after order fills."""
        for e in self.entries:
            if e.entry_id == entry_id:
                e.executed = True
                e.order_id = order_id
                e.fill_price = fill_price
                e.fill_size_usd = fill_size_usd
                e.tokens = tokens
                break
        self.save()

    def mark_closed(
        self,
        market_id: str,
        close_price: float,
        pnl_usd: float,
        pnl_pct: float,
        close_reason: str,
    ):
        """Update the most recent BUY entry for this market with close info."""
        for e in reversed(self.entries):
            if e.market_id == market_id and e.action.startswith("BUY") and e.executed:
                e.close_price = close_price
                e.pnl_usd = pnl_usd
                e.pnl_pct = pnl_pct
                e.close_reason = close_reason
                e.closed_at = datetime.utcnow().isoformat()
                break
        self.save()

    # ── read / review ────────────────────────────────────────

    def get_open_entries(self) -> List[JournalEntry]:
        """Entries that were executed but not yet closed."""
        return [e for e in self.entries if e.executed and e.closed_at is None]

    def get_closed_entries(self) -> List[JournalEntry]:
        """Entries that have been closed with P&L."""
        return [e for e in self.entries if e.closed_at is not None]

    def get_recent(self, n: int = 20) -> List[JournalEntry]:
        return self.entries[-n:]

    def summary_stats(self) -> Dict:
        closed = self.get_closed_entries()
        wins = [e for e in closed if (e.pnl_usd or 0) > 0]
        losses = [e for e in closed if (e.pnl_usd or 0) < 0]
        total_pnl = sum(e.pnl_usd or 0 for e in closed)
        return {
            "total_entries": len(self.entries),
            "executed": sum(1 for e in self.entries if e.executed),
            "open": len(self.get_open_entries()),
            "closed": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(closed) * 100 if closed else 0,
            "total_pnl": round(total_pnl, 2),
        }


# ── CLI review ───────────────────────────────────────────────

def print_journal():
    """Pretty-print the journal for human review."""
    journal = TradeJournal()
    entries = journal.get_recent(30)

    if not entries:
        print("No journal entries yet.")
        return

    stats = journal.summary_stats()
    mode = "PAPER" if entries[-1].paper_trade else "REAL"
    print(f"\n{'='*70}")
    print(f"  TRADE JOURNAL  [{mode} MODE]")
    print(f"  {stats['total_entries']} entries | {stats['executed']} executed | "
          f"{stats['open']} open | {stats['closed']} closed")
    print(f"  Win rate: {stats['win_rate']:.1f}% | Total P&L: ${stats['total_pnl']:+,.2f}")
    print(f"{'='*70}\n")

    for e in entries:
        status = ""
        if e.closed_at:
            emoji = "✅" if (e.pnl_usd or 0) > 0 else "❌"
            status = f"{emoji} CLOSED  P&L: ${e.pnl_usd:+,.2f} ({e.pnl_pct:+.1f}%)  [{e.close_reason}]"
        elif e.executed:
            status = "📊 OPEN"
        else:
            status = "⏭️  NOT EXECUTED"

        print(f"[{e.timestamp[:16]}] {e.action:8s} | {e.market_question[:50]}")
        print(f"  Strategy : {e.strategy}")
        print(f"  Reasoning: {e.reasoning}")
        print(f"  Price    : YES={e.yes_price:.3f}  NO={e.no_price:.3f}  "
              f"Vol=${e.volume:,.0f}  Liq=${e.liquidity:,.0f}")
        if e.executed:
            print(f"  Filled   : {e.tokens:.1f} tokens @ ${e.fill_price:.4f}  "
                  f"(${e.fill_size_usd:.2f})")
        print(f"  Status   : {status}")
        print(f"  Portfolio: cash=${e.portfolio_cash:,.2f}  "
              f"value=${e.portfolio_value:,.2f}  "
              f"exposure=${e.portfolio_exposure:,.2f}  "
              f"positions={e.open_positions}")
        print()


if __name__ == "__main__":
    print_journal()
