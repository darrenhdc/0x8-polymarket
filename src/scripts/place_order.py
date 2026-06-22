#!/usr/bin/env python3
"""
Polyweather order placement — reusable template.
Reads private key from backup file, connects via v2 SDK, places order.

Usage:
  python3 src/scripts/place_order.py --token <TOKEN_ID> --side BUY --size 7.7 --price 0.66
  python3 src/scripts/place_order.py --token <TOKEN_ID> --side BUY --size 7.7 --price 0.66 --execute
"""
import os, re, json, sys, argparse

BACKUP = "/home/darren/share/polymarket/config/.env.txt.backup"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

# Load key
with open(BACKUP) as f:
    m = re.search(r"^\s*sk\s*=\s*([0-9a-fA-FxX]+)", f.read(), re.MULTILINE)
pk = m.group(1).strip()
pk = pk if pk.startswith("0x") else "0x" + pk
os.environ["POLYMARKET_PRIVATE_KEY"] = pk

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, OrderArgsV2, PartialCreateOrderOptions, OrderType

HOST = "https://clob.polymarket.com"
FUNDER = "0x1270215141EA0a2CdA89272722B2ac47DF6751A1"

creds = ApiCreds(
    api_key="f785f79c-3119-1c24-3489-3ac27718b741",
    api_secret="uLbsEVrSw-wTNHC1X4wZ5tQuHzaeiy6xpuJrXAGbFX4=",
    api_passphrase="04949246bc4fe4326d25df889a4271c52299b7bea07a5b38ed8566e7566fb61a",
)
client = ClobClient(host=HOST, chain_id=137, key=pk, creds=creds, signature_type=2, funder=FUNDER)

def place(token_id, side, size, price, dry_run=True):
    print(f"Order: {side} {size} @{price} on token {token_id[:20]}...")
    if dry_run:
        print("[DRY-RUN] not sent. Add --execute to place.")
        return
    args = OrderArgsV2(token_id=token_id, price=price, size=size, side=side)
    opts = PartialCreateOrderOptions(neg_risk=True, tick_size="0.01")
    try:
        resp = client.create_and_post_order(args, opts, order_type=OrderType.GTC)
        print(json.dumps(resp, default=str, indent=2)[:500])
        return resp
    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--token", required=True, help="Token ID (NO token for BUY_NO, YES token for BUY_YES)")
    p.add_argument("--side", required=True, choices=["BUY","SELL"])
    p.add_argument("--size", type=float, required=True)
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--execute", action="store_true")
    args = p.parse_args()
    place(args.token, args.side, args.size, args.price, dry_run=not args.execute)
