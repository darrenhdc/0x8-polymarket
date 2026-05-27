#!/usr/bin/env python3
"""Cross-check balance via CLOB authenticated request (no private-key prompt) + public Data API."""
import os
import time

import requests
from dotenv import load_dotenv
from py_clob_client.signing.hmac import build_hmac_signature

import config


def _clob_balance_via_api_creds() -> dict:
    api_key = config.POLYMARKET_API_KEY
    api_secret = config.POLYMARKET_API_SECRET
    api_passphrase = config.POLYMARKET_PASSPHRASE
    poly_address = os.getenv("POLYMARKET_EOA", "").strip()

    if not (api_key and api_secret and api_passphrase):
        raise RuntimeError("Missing POLYMARKET_API_KEY/SECRET/PASSPHRASE in .env")
    if not poly_address:
        raise RuntimeError("Missing POLYMARKET_EOA in .env (needed for POLY_ADDRESS header)")

    path = "/balance-allowance"
    query = "?asset_type=COLLATERAL&signature_type=2"
    url = f"{config.CLOB_API}{path}{query}"

    ts = int(time.time())
    sig = build_hmac_signature(api_secret, ts, "GET", path, None)
    headers = {
        "POLY_ADDRESS": poly_address,
        "POLY_SIGNATURE": sig,
        "POLY_TIMESTAMP": str(ts),
        "POLY_API_KEY": api_key,
        "POLY_PASSPHRASE": api_passphrase,
    }

    r = requests.get(url, headers=headers, timeout=20)
    return {"status": r.status_code, "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text}


def main():
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    print("=== SDK-style CLOB balance check (no private key input) ===")

    try:
        sdk_balance = _clob_balance_via_api_creds()
        print(f"Authenticated CLOB /balance-allowance: {sdk_balance}")
    except Exception as e:
        print(f"Authenticated CLOB check failed: {e}")

    funder = (config.POLYMARKET_FUNDER or "").lower()
    if funder:
        try:
            r = requests.get(
                f"https://data-api.polymarket.com/value?user={funder}",
                timeout=10,
            )
            data = r.json() or [{}]
            value = data[0].get("value", 0)
            print(f"Public data-api value({funder}): {value}")
        except Exception as e:
            print(f"Public data-api check failed: {e}")


if __name__ == "__main__":
    main()
