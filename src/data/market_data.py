"""
Market Data Module - Fetch live data from Polymarket API
"""
import json
import requests
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import config


class PolymarketAPI:
    """Interface to Polymarket APIs"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        })

    def _get(self, base_url: str, endpoint: str, params: Dict = None) -> Optional[Dict]:
        """Make GET request to API"""
        try:
            url = f"{base_url}/{endpoint}"
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"API Error ({endpoint}): {e}")
            return None

    def get_markets(self, limit: int = 100, offset: int = 0,
                    tag: str = None, active: bool = True) -> List[Dict]:
        """Get list of markets from Gamma API"""
        params = {
            'limit': limit,
            'offset': offset,
            'active': 'true' if active else 'false',
            'closed': 'false'
        }
        if tag:
            params['tag'] = tag

        result = self._get(config.GAMMA_API, "markets", params)
        return result if isinstance(result, list) else []

    def get_events(self, limit: int = 100, offset: int = 0,
                   tag: str = None, active: bool = True) -> List[Dict]:
        """Get list of events"""
        params = {
            'limit': limit,
            'offset': offset,
            'active': 'true' if active else 'false'
        }
        if tag:
            params['tag'] = tag

        result = self._get(config.GAMMA_API, "events", params)
        return result if isinstance(result, list) else []

    def get_market_by_id(self, market_id: str) -> Optional[Dict]:
        """Get specific market by ID"""
        return self._get(config.GAMMA_API, f"markets/{market_id}")

    def get_market_by_slug(self, slug: str) -> Optional[Dict]:
        """Get market by slug"""
        params = {'slug': slug}
        result = self._get(config.GAMMA_API, "markets", params)
        if result and isinstance(result, list) and len(result) > 0:
            return result[0]
        return None

    def get_orderbook(self, token_id: str) -> Optional[Dict]:
        """Get orderbook for a token from CLOB API"""
        params = {'token_id': token_id}
        return self._get(config.CLOB_API, "book", params)

    def get_price(self, token_id: str) -> Optional[Dict]:
        """Get current price for a token"""
        params = {'token_id': token_id}
        return self._get(config.CLOB_API, "price", params)

    def get_prices(self, token_ids: List[str]) -> List[Dict]:
        """Get prices for multiple tokens"""
        params = {'token_ids': ','.join(token_ids)}
        result = self._get(config.CLOB_API, "prices", params)
        return result if isinstance(result, list) else []

    def get_price_history(self, market_id: str, interval: str = "1d",
                          fudge: float = 1.0) -> Optional[Dict]:
        """Get price history for a market"""
        params = {
            'market': market_id,
            'interval': interval,
            'fudge': fudge
        }
        return self._get(config.CLOB_API, "prices-history", params)


class MarketScanner:
    """Scans and filters markets for trading opportunities"""

    def __init__(self):
        self.api = PolymarketAPI()

    def get_tradable_markets(self, limit: int = 50) -> List[Dict]:
        """Get markets that meet trading criteria"""
        markets = self.api.get_markets(limit=limit)

        filtered = []
        now = datetime.utcnow()
        max_end_date = now + timedelta(days=config.MAX_END_DATE_DAYS)

        for market in markets:
            try:
                # Skip closed/inactive markets
                if not market.get('active', False) or market.get('closed', False):
                    continue

                # Normalize field names (API uses camelCase, values are strings)
                end_date_str = market.get('endDateIso') or market.get('end_date_iso')

                # Parse numeric fields (they come as strings)
                liquidity = float(market.get('liquidity', 0) or 0)
                volume = float(market.get('volume', 0) or 0)

                # Check end date (optional - some markets don't have end dates)
                if end_date_str:
                    try:
                        # Handle both ISO and simple date formats
                        if 'T' in str(end_date_str):
                            end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00').replace('+00:00', ''))
                        else:
                            end_date = datetime.strptime(str(end_date_str), '%Y-%m-%d')
                        if end_date < now:
                            continue  # Market already ended
                        if end_date > max_end_date:
                            continue  # Market too far out
                    except Exception as e:
                        pass

                # Check liquidity
                if liquidity < config.MIN_LIQUIDITY:
                    continue

                # Check volume
                if volume < config.MIN_VOLUME_24H:
                    continue

                # Parse JSON string fields
                clob_token_ids = market.get('clobTokenIds', [])
                if isinstance(clob_token_ids, str):
                    clob_token_ids = json.loads(clob_token_ids)

                outcomes = market.get('outcomes', ['Yes', 'No'])
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)

                outcome_prices = market.get('outcomePrices', [])
                if isinstance(outcome_prices, str):
                    outcome_prices = json.loads(outcome_prices)

                # Must have CLOB token IDs
                if not clob_token_ids or len(clob_token_ids) < 2:
                    continue

                # Build token info
                tokens = []
                for i, token_id in enumerate(clob_token_ids):
                    if token_id:
                        price = 0
                        if i < len(outcome_prices) and outcome_prices[i]:
                            try:
                                price = float(outcome_prices[i])
                            except:
                                pass
                        tokens.append({
                            'token_id': token_id,
                            'outcome': outcomes[i] if i < len(outcomes) else f'Outcome{i}',
                            'price': price
                        })

                if len(tokens) < 2:
                    continue

                # Add computed fields
                market['tradable'] = True
                market['tokens'] = tokens
                market['yes_token'] = next((t for t in tokens if t.get('outcome', '').lower() == 'yes'), tokens[0])
                market['no_token'] = next((t for t in tokens if t.get('outcome', '').lower() == 'no'), tokens[1] if len(tokens) > 1 else None)

                # Normalize other fields
                market['end_date_iso'] = end_date_str
                market['liquidity'] = liquidity
                market['volume'] = volume

                filtered.append(market)

            except Exception as e:
                print(f"Error filtering market {market.get('id')}: {e}")
                continue

        return filtered

    def get_market_prices(self, market: Dict) -> Dict[str, float]:
        """Get current prices for a market's tokens"""
        prices = {}

        # First try to get from outcomePrices (already in market data)
        outcome_prices = market.get('outcomePrices', [])
        if isinstance(outcome_prices, str):
            outcome_prices = json.loads(outcome_prices)

        outcomes = market.get('outcomes', ['Yes', 'No'])
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        if outcome_prices and len(outcome_prices) >= 2:
            for i, price_str in enumerate(outcome_prices):
                if price_str and i < len(outcomes):
                    outcome = outcomes[i].lower()
                    try:
                        prices[outcome] = float(price_str)
                    except:
                        pass

        # If we have token IDs but no prices, fetch from API
        if not prices:
            yes_token = market.get('yes_token', {})
            no_token = market.get('no_token', {})

            if yes_token:
                token_id = yes_token.get('token_id')
                if token_id:
                    price_data = self.api.get_price(token_id)
                    if price_data:
                        prices['yes'] = float(price_data.get('price', 0) or 0)

            if no_token:
                token_id = no_token.get('token_id')
                if token_id:
                    price_data = self.api.get_price(token_id)
                    if price_data:
                        prices['no'] = float(price_data.get('price', 0) or 0)

        return prices

    def get_market_details(self, market_id: str) -> Optional[Dict]:
        """Get detailed info about a specific market"""
        market = self.api.get_market_by_id(market_id)
        if not market:
            return None

        # Enrich with price data
        prices = self.get_market_prices(market)
        market['current_prices'] = prices

        return market

    def get_top_markets_by_volume(self, limit: int = 20) -> List[Dict]:
        """Get top markets by volume"""
        markets = self.get_tradable_markets(limit=limit * 3)  # Get more to filter
        markets.sort(key=lambda x: float(x.get('volume', 0) or 0), reverse=True)
        return markets[:limit]


class MarketData:
    """Convenience class for market data operations"""

    def __init__(self):
        self.scanner = MarketScanner()
        self.api = PolymarketAPI()

    def scan_opportunities(self) -> List[Dict]:
        """Scan for trading opportunities"""
        print("Scanning Polymarket for opportunities...")
        markets = self.scanner.get_top_markets_by_volume(config.MARKET_ANALYSIS_COUNT)
        print(f"Found {len(markets)} tradable markets")
        return markets

    def get_price(self, token_id: str) -> float:
        """Get current price for a token"""
        data = self.api.get_price(token_id)
        if data:
            return float(data.get('price', 0) or 0)
        return 0.0

    def get_orderbook_summary(self, token_id: str) -> Dict:
        """Get orderbook summary (best bid/ask, spread)"""
        book = self.api.get_orderbook(token_id)
        if not book:
            return {'bid': 0, 'ask': 0, 'spread': 0}

        bids = book.get('bids', [])
        asks = book.get('asks', [])

        best_bid = float(bids[0].get('price', 0)) if bids else 0
        best_ask = float(asks[0].get('price', 0)) if asks else 0
        spread = (best_ask - best_bid) / best_ask if best_ask > 0 else 0

        return {
            'bid': best_bid,
            'ask': best_ask,
            'spread': spread,
            'bid_depth': sum(float(b.get('size', 0)) for b in bids[:5]),
            'ask_depth': sum(float(a.get('size', 0)) for a in asks[:5])
        }


if __name__ == "__main__":
    # Test market data
    md = MarketData()
    markets = md.scan_opportunities()

    print("\nTop Markets:")
    for m in markets[:5]:
        print(f"  - {m.get('question', 'N/A')[:60]}...")
        print(f"    Volume: ${float(m.get('volume', 0)):,.0f}, Liquidity: ${float(m.get('liquidity', 0)):,.0f}")
