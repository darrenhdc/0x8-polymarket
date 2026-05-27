#!/usr/bin/env python3
"""
One-time keystore generator.
Encrypts your private key with a password and saves it to data/keystore.json.
After this, all scripts only need the password — private key is never stored in plaintext.
"""
import os
import json
import getpass
import sys

from eth_account import Account

KEYSTORE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "trading_system", "data", "keystore.json")


def main():
    if os.path.exists(KEYSTORE_PATH):
        overwrite = input(f"Keystore already exists at {KEYSTORE_PATH}. Overwrite? (y/N): ")
        if overwrite.lower() != "y":
            print("Aborted.")
            sys.exit(0)

    print("=== Polymarket Keystore Generator ===")
    print("Your private key will be encrypted and saved. You'll only need a password after this.\n")

    pk = getpass.getpass("Enter private key (hidden): ")
    if not pk:
        print("No key provided.")
        sys.exit(1)

    raw = pk if pk.startswith("0x") else "0x" + pk
    try:
        acct = Account.from_key(raw)
        print(f"EOA: {acct.address}")
    except Exception as e:
        print(f"Invalid key: {e}")
        sys.exit(1)

    password = getpass.getpass("Choose keystore password (hidden): ")
    confirm = getpass.getpass("Confirm password (hidden): ")
    if password != confirm:
        print("Passwords don't match.")
        sys.exit(1)
    if len(password) < 4:
        print("Password too short (min 4 chars).")
        sys.exit(1)

    print("Encrypting (this takes a few seconds)...")
    keystore = Account.encrypt(raw, password)

    os.makedirs(os.path.dirname(KEYSTORE_PATH), exist_ok=True)
    with open(KEYSTORE_PATH, "w") as f:
        json.dump(keystore, f, indent=2)

    print(f"\nKeystore saved to: {KEYSTORE_PATH}")
    print(f"EOA address: {acct.address}")
    print("From now on, just enter your password to unlock.")


if __name__ == "__main__":
    main()
