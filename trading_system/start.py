#!/usr/bin/env python3
"""
Secure launcher — prompts for private key via getpass (no echo),
sets it in process memory only, then runs the trading agent.

Usage:
    python3 start.py              # continuous mode
    python3 start.py --once       # single cycle
    python3 start.py --status     # check status only
"""
import os
import sys
import getpass


def main():
    import config

    # Resolve private key: env var → keystore (password) → interactive prompt
    pk = config._resolve_private_key()
    if not pk:
        print("No private key available. Run create_keystore.py first, or export POLYMARKET_PRIVATE_KEY=0x...")
        sys.exit(1)
    os.environ["POLYMARKET_PRIVATE_KEY"] = pk

    print(f"   Mode: {'PAPER' if config.PAPER_TRADING else '🔴 REAL'}")
    print(f"   Funder: {config.POLYMARKET_FUNDER}")

    from agent import main as agent_main
    agent_main()


if __name__ == "__main__":
    main()
