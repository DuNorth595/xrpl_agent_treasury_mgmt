#!/usr/bin/env python3
"""
Stand up the treasury's currency holdings on the local devnet.

This is a LOCAL DEVNET operation. It establishes the trust lines and balances
the treasury needs to disburse in each currency it operates in:

    RLUSD   the treasury's holding and settlement currency
    CAD     disbursements to Canadian counterparties
    MXN     disbursements to Mexican counterparties

For each currency the script:
    1. Seeds an issuer's balance through a bootstrap chain (an account may
       not hold a trust line to itself, so an issuer's balance has to arrive
       from another account issuing the same currency code)
    2. Sets the treasury's and each cost centre's trust line
    3. Issues the treasury's opening balance

The issuer addresses are written to `var/local-currencies.json` so the
service can find them.

    python3 issue_currencies.py --rlusd 50000 --cad 20000 --mxn 300000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from decimal import Decimal

import websockets
from xrpl.core.binarycodec import encode
from xrpl.models import IssuedCurrencyAmount, Payment, TrustSet
from xrpl.transaction import sign
from xrpl.wallet import Wallet

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))
from xrpl_agent_treasury_mgmt.currency import (  # noqa: E402
    RLUSD_HEX,
    SCALE,
    hex_code,
    parse_xrpl_amount,
)

ACCOUNTS = pathlib.Path.home() / ".xrpl-up" / "local-accounts.json"
OUT = pathlib.Path(__file__).resolve().parent / "var" / "local-currencies.json"
WS_URL = "ws://127.0.0.1:6006"
NETWORK_ID = 15791

TREASURY_INDEX = 0
COST_CENTRE_INDEXES = [1, 2]

# Issuer accounts, one per currency, plus two accounts that bootstrap them.
ISSUER_INDEXES = {"RLUSD": 8, "CAD": 5, "MXN": 6}
BOOTSTRAP_INDEX = 7
SEED_INDEX = 9


def load_accounts():
    raw = json.loads(ACCOUNTS.read_text())
    if isinstance(raw, dict) and "accounts" in raw:
        raw = raw["accounts"]
    return raw if isinstance(raw, list) else list(raw.values())


def wallet_for(index: int) -> Wallet:
    accounts = load_accounts()
    a = next(x for x in accounts if x["index"] == index)
    return Wallet.from_seed(a["seed"], algorithm="ed25519")


def code_for(ticker: str) -> str:
    return RLUSD_HEX if ticker == "RLUSD" else hex_code(ticker)


def issued(issuer: str, code: str, value: str) -> IssuedCurrencyAmount:
    return IssuedCurrencyAmount(currency=code, issuer=issuer, value=value)


async def rpc(ws, command):
    await ws.send(json.dumps(command))
    return json.loads(await ws.recv())


async def sequence(ws, account: str) -> int:
    r = await rpc(ws, {"command": "account_info", "account": account})
    return r["result"]["account_data"]["Sequence"]


async def submit(ws, tx, wallet):
    resp = await rpc(
        ws, {"command": "submit", "tx_blob": encode(sign(tx, wallet).to_xrpl())}
    )
    result = resp.get("result", {})
    return result.get("engine_result"), (result.get("tx_json") or {}).get("hash")


async def trust(ws, wallet, issuer, code, limit="100000000000") -> str:
    tx = TrustSet(
        account=wallet.classic_address,
        limit_amount=issued(issuer, code, limit),
        fee="12",
        sequence=await sequence(ws, wallet.classic_address),
        network_id=NETWORK_ID,
    )
    engine, _ = await submit(ws, tx, wallet)
    return engine


async def issue(ws, wallet, issuer, code, destination, value) -> str:
    tx = Payment(
        account=wallet.classic_address,
        amount=issued(issuer, code, value),
        destination=destination,
        fee="12",
        sequence=await sequence(ws, wallet.classic_address),
        network_id=NETWORK_ID,
    )
    engine, _ = await submit(ws, tx, wallet)
    return engine


async def bootstrap_currency(ws, ticker: str, code: str, amount: float) -> str:
    """Seed the issuer's balance and return the issuer address."""
    issuer_index = ISSUER_INDEXES[ticker]
    issuer_wallet = wallet_for(issuer_index)
    issuer = issuer_wallet.classic_address

    seed_wallet = wallet_for(SEED_INDEX)
    boot_wallet = wallet_for(BOOTSTRAP_INDEX)
    seed = seed_wallet.classic_address
    boot = boot_wallet.classic_address

    # seed -> bootstrap -> issuer, each issuing the same currency code. The
    # ledger routes the value through the shared code.
    for source_wallet, target, target_index in [
        (seed_wallet, boot, BOOTSTRAP_INDEX),
        (boot_wallet, issuer, issuer_index),
    ]:
        source = source_wallet.classic_address
        await trust(ws, source_wallet, target, code)
        await trust(ws, wallet_for(target_index), source, code)
        engine = await issue(ws, source_wallet, source, code, target, "100000000000")
        if engine != "tesSUCCESS":
            print(f"    bootstrap {source[:8]} -> {target[:8]}: {engine}")
        await asyncio.sleep(0.6)

    return issuer


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rlusd", type=float, default=50000.0)
    parser.add_argument("--cad", type=float, default=20000.0)
    parser.add_argument("--mxn", type=float, default=300000.0)
    args = parser.parse_args()

    wanted = {"RLUSD": args.rlusd, "CAD": args.cad, "MXN": args.mxn}

    async with websockets.connect(WS_URL) as ws:
        issuers = {}
        for ticker, amount in wanted.items():
            code = code_for(ticker)
            print(f"  {ticker}:")
            issuer = await bootstrap_currency(ws, ticker, code, amount)
            issuers[ticker] = issuer
            print(f"    issuer {issuer}")

            # The treasury and each cost centre trust the issuer.
            for holder_index in [TREASURY_INDEX] + COST_CENTRE_INDEXES:
                holder = wallet_for(holder_index)
                engine = await trust(ws, holder, issuer, code)
                if engine != "tesSUCCESS":
                    print(f"    trust {holder.classic_address}: {engine}")

            # The issuer opens the treasury's position.
            value = str(int(Decimal(str(amount)) * SCALE))
            treasury_address = wallet_for(TREASURY_INDEX).classic_address
            engine = await issue(
                ws, wallet_for(ISSUER_INDEXES[ticker]), issuer, code,
                treasury_address, value,
            )
            print(f"    open position {amount:,.2f}: {engine}")
            await asyncio.sleep(1)

        # Verify the treasury's balances.
        print("  treasury balances:")
        for ticker, issuer in issuers.items():
            code = code_for(ticker)
            r = await rpc(
                ws,
                {"command": "account_lines", "account": wallet_for(TREASURY_INDEX).classic_address,
                 "peer": issuer},
            )
            for line in r.get("result", {}).get("lines", []):
                if line.get("currency") == code:
                    # The ledger returns balances in scientific
                    # notation ("1000000000000000e-4"), which int()
                    # cannot parse.
                    bal = parse_xrpl_amount(line.get("balance")) / SCALE
                    print(f"    {ticker:<6} {bal:>14,.2f}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "issuers": issuers,
                "codes": {t: code_for(t) for t in issuers},
                "network": "local-devnet",
                "note": (
                    "Locally generated issuers. Mainnet RLUSD issuer is "
                    "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De; testnet is "
                    "rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV."
                ),
            },
            indent=2,
        )
    )
    print(f"  wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
