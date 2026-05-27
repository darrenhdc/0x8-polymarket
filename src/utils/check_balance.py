#!/usr/bin/env python3
"""Check Polymarket balance — no private key needed."""
import requests, sys

PROXY = "0x1270215141EA0a2CdA89272722B2ac47DF6751A1"
EOA = "0xa0F7CDAE61735523C69eF1E7974eb8007195B9Af"
RPC = "https://polygon-rpc.com"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"


def erc20_balance(token, addr):
    data = "0x70a08231" + addr[2:].lower().zfill(64)
    r = requests.post(RPC, json={"jsonrpc": "2.0", "method": "eth_call",
        "params": [{"to": token, "data": data}, "latest"], "id": 1})
    return int(r.json().get("result", "0x0"), 16) / 1e6


def pol_balance(addr):
    r = requests.post(RPC, json={"jsonrpc": "2.0", "method": "eth_getBalance",
        "params": [addr, "latest"], "id": 1})
    return int(r.json().get("result", "0x0"), 16) / 1e18


def polymarket_value(addr):
    r = requests.get(f"https://data-api.polymarket.com/value?user={addr.lower()}", timeout=10)
    data = r.json()
    return data[0].get("value", 0) if data else 0


print("=== Polymarket Balance Check (public, no key needed) ===\n")

for label, addr in [("EOA", EOA), ("Proxy", PROXY)]:
    print(f"{label}: {addr}")
    print(f"  USDC.e:  ${erc20_balance(USDC_E, addr):.6f}")
    print(f"  USDC:    ${erc20_balance(USDC, addr):.6f}")
    print(f"  POL:     {pol_balance(addr):.6f}")
    print(f"  Polymarket portfolio value: ${polymarket_value(addr):.2f}")
    print()

# If user provides an extra address as argument
if len(sys.argv) > 1:
    addr = sys.argv[1]
    print(f"Custom: {addr}")
    print(f"  USDC.e:  ${erc20_balance(USDC_E, addr):.6f}")
    print(f"  USDC:    ${erc20_balance(USDC, addr):.6f}")
    print(f"  POL:     {pol_balance(addr):.6f}")
    print(f"  Polymarket portfolio value: ${polymarket_value(addr):.2f}")
