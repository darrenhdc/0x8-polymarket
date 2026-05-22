"""Backfill Polymarket weather markets, prices, GFS forecasts, and observations."""

from __future__ import annotations

import argparse

from src.data.database import init_all
from src.data.gfs_history import GFSHistoryCollector
from src.data.polymarket_history import PolymarketHistoryCollector


def parse_args():
    parser = argparse.ArgumentParser(description="Backfill weather-market research databases.")
    parser.add_argument("--start", default="2024-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--market-pages", type=int, default=50, help="Gamma pages per open/closed state")
    parser.add_argument("--page-size", type=int, default=200, help="Gamma page size")
    parser.add_argument("--limit-markets", type=int, default=None, help="Limit markets for price/GFS backfill")
    parser.add_argument("--events-only", action="store_true", help="Discover markets only through Gamma weather events")
    parser.add_argument("--event-query", default=None, help="Only collect weather events whose title/markets contain this text")
    parser.add_argument("--skip-markets", action="store_true", help="Skip Gamma market discovery")
    parser.add_argument("--skip-prices", action="store_true", help="Skip CLOB price-history backfill")
    parser.add_argument("--skip-gfs", action="store_true", help="Skip GFS/observed weather backfill")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    from datetime import date

    end = args.end or date.today().isoformat()
    init_all()

    poly = PolymarketHistoryCollector()
    try:
        if not args.skip_markets:
            count = poly.collect_weather_markets(
                max_pages=args.market_pages,
                page_size=args.page_size,
                events_only=args.events_only,
                event_query=args.event_query,
            )
            print(f"[markets] upserted weather-like markets: {count}")
        if not args.skip_prices:
            count = poly.backfill_price_history(
                start_date=args.start,
                end_date=end,
                limit_markets=args.limit_markets,
            )
            print(f"[prices] inserted price rows: {count}")
    finally:
        poly.close()

    if not args.skip_gfs:
        gfs = GFSHistoryCollector()
        try:
            counts = gfs.backfill_from_markets(
                start_date=args.start,
                end_date=end,
                limit_markets=args.limit_markets,
            )
            print(f"[gfs] inserted forecasts={counts['forecasts']} observed={counts['observed']}")
        finally:
            gfs.close()


if __name__ == "__main__":
    main()
