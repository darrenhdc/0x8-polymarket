"""
Polymarket Real Trading Client
Wraps the official py-clob-client SDK for real order execution.
"""
import json
from typing import Dict, Optional
from datetime import datetime

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    MarketOrderArgs,
    OrderArgs,
    PartialCreateOrderOptions,
    BalanceAllowanceParams,
    AssetType,
    OrderType,
)

import config


class RealTradingClient:
    """Manages authenticated connection to Polymarket CLOB API."""

    def __init__(self):
        private_key = config._resolve_private_key()
        if not private_key:
            raise RuntimeError(
                "Private key not available. Options:\n"
                "  1) export POLYMARKET_PRIVATE_KEY=0x...  (env var, not saved to disk)\n"
                "  2) Set KEYSTORE_FILE in .env\n"
                "  3) Run in a TTY terminal to get an interactive prompt"
            )

        creds = None
        if config.POLYMARKET_API_KEY:
            creds = ApiCreds(
                api_key=config.POLYMARKET_API_KEY,
                api_secret=config.POLYMARKET_API_SECRET,
                api_passphrase=config.POLYMARKET_PASSPHRASE,
            )

        # signature_type: 0=EOA, 1=POLY_PROXY, 2=POLY_GNOSIS_SAFE
        # When using a funder/proxy wallet, must use POLY_GNOSIS_SAFE (2)
        funder = config.POLYMARKET_FUNDER or None
        sig_type = 2 if funder else 0

        self.client = ClobClient(
            host=config.CLOB_API,
            chain_id=config.CHAIN_ID,
            key=private_key,
            creds=creds,
            signature_type=sig_type,
            funder=funder,
        )

        # Derive API creds if not provided
        if creds is None:
            self._derive_and_set_creds()

    # ── bootstrap ────────────────────────────────────────────

    def _derive_and_set_creds(self):
        """One-time derive of API creds from the private key."""
        derived = self.client.create_or_derive_api_creds()
        self.client.set_api_creds(derived)
        print(
            "API credentials derived. Save these to .env so you don't "
            "re-derive every restart:\n"
            f"  POLYMARKET_API_KEY={derived.api_key}\n"
            f"  POLYMARKET_API_SECRET={derived.api_secret}\n"
            f"  POLYMARKET_PASSPHRASE={derived.api_passphrase}"
        )

    # ── queries ──────────────────────────────────────────────

    def get_balance(self) -> Optional[Dict]:
        """Get USDC balance/allowance on the exchange."""
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            return self.client.get_balance_allowance(params)
        except Exception as e:
            print(f"Error fetching balance: {e}")
            return None

    def get_collateral_balance_usdc(self) -> Optional[float]:
        """Return available collateral balance in USDC units."""
        data = self.get_balance()
        if not data:
            return None
        try:
            raw = data.get("balance", 0)
            # CLOB returns micro-USDC integer string (e.g. "6460671")
            return float(raw) / 1_000_000
        except Exception:
            return None

    def get_open_orders(self) -> list:
        """Return current open orders."""
        try:
            return self.client.get_orders()
        except Exception as e:
            print(f"Error fetching orders: {e}")
            return []

    def get_order_book(self, token_id: str) -> Optional[Dict]:
        """Get the full order book for a token."""
        try:
            return self.client.get_order_book(token_id)
        except Exception as e:
            print(f"Error fetching order book: {e}")
            return None

    # ── order placement ──────────────────────────────────────

    def place_market_order(
        self,
        token_id: str,
        side: str,
        amount: float,
        neg_risk: bool = False,
    ) -> Optional[Dict]:
        """
        Place a fill-or-kill market order.

        Args:
            token_id: The CLOB token ID for the outcome.
            side: "BUY" or "SELL".
            amount: USD amount to spend (BUY) or tokens to sell (SELL).
            neg_risk: True for neg-risk markets (multi-outcome events).
        """
        try:
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=side,
            )
            options = PartialCreateOrderOptions(neg_risk=neg_risk)
            resp = self.client.create_and_post_order(order_args, options)
            return resp
        except Exception as e:
            print(f"Market order error: {e}")
            return None

    def place_limit_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        neg_risk: bool = False,
    ) -> Optional[Dict]:
        """
        Place a GTC limit order.

        Args:
            token_id: The CLOB token ID for the outcome.
            side: "BUY" or "SELL".
            size: Number of outcome tokens.
            price: Limit price (0‑1).
            neg_risk: True for neg-risk markets.
        """
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
            )
            options = PartialCreateOrderOptions(neg_risk=neg_risk)
            resp = self.client.create_and_post_order(order_args, options)
            return resp
        except Exception as e:
            print(f"Limit order error: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order by ID."""
        try:
            self.client.cancel(order_id)
            return True
        except Exception as e:
            print(f"Cancel error: {e}")
            return False

    def cancel_all(self) -> bool:
        """Cancel all open orders."""
        try:
            self.client.cancel_all()
            return True
        except Exception as e:
            print(f"Cancel-all error: {e}")
            return False
