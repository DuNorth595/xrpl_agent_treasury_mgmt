#!/usr/bin/env python3
"""
Fund the demo treasury account from a devnet account with spare XRP.

This is a LOCAL DEVNET operation only. It exists so the demo dashboard shows
a treasury with a realistic balance and a working minimum-balance floor
instead of the 1000 XRP the proof run happened to leave behind.

    python3 fund_treasury.py --amount 10000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

import websockets
from xrpl.models import Payment
from xrpl.transaction import sign
from xrpl.wallet import Wallet

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))
from xrpl_agent_treasury_mgmt.accounts import drops_to_xrp, fmt_xrp

from xrpl_agent_treasury_mgmt.devnet_accounts import address_for as _address

ACCOUNTS = pathlib.Path.home() / ".xrpl-up" / "local-accounts.json"
WS_URL = "ws://127.0.0.1:6006"
FAUCET_URL = "http://127.0.0.1:3001/faucet"
NETWORK_ID = 15791

TREASURY = _address("TREASURY")
async def rpc(ws, command):
    await ws.send(json.dumps(command))
    return json.loads(await ws.recv())


async def send_xrp(ws, sender, to: str, amount: float) -> str:
    """Send XRP from one account to another."""
    wallet = Wallet.from_seed(sender["seed"], algorithm="ed25519")
    info = await rpc(
        ws, {"command": "account_info", "account": wallet.classic_address}
    )
    seq = info["result"]["account_data"]["Sequence"]

    # last_ledger_sequence must be ahead of the CURRENT ledger, or the
    # transaction dies with tefMAX_LEDGER.
    cur = await rpc(ws, {"command": "ledger_current"})
    current = cur["result"]["ledger_current_index"]

    tx = Payment(
        account=wallet.classic_address,
        amount=fmt_xrp(amount),
        destination=to,
        fee="12",
        sequence=seq,
        network_id=NETWORK_ID,
        last_ledger_sequence=current + 100,
    )
    signed = sign(tx, wallet)
    from xrpl.core.binarycodec import encode

    resp = await rpc(
        ws, {"command": "submit", "tx_blob": encode(signed.to_xrpl())}
    )
    result = resp.get("result", {})
    engine = result.get("engine_result")
    if engine != "tesSUCCESS":
        raise RuntimeError(f"the transfer failed: {engine}")
    return (result.get("tx_json") or {}).get("hash", "")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target",
        type=float,
        default=3000.0,
        help="the treasury's XRP balance to reach, not the amount to send",
    )
    parser.add_argument("--from-index", type=int, default=7)
    parser.add_argument("--to", default=TREASURY)
    args = parser.parse_args()

    raw = json.loads(ACCOUNTS.read_text())
    if isinstance(raw, dict) and "accounts" in raw:
        raw = raw["accounts"]
    items = raw if isinstance(raw, list) else list(raw.values())

    async with websockets.connect(WS_URL) as ws:
        # The goal is a BALANCE, not a transfer. The treasury spends XRP on
        # the owner reserve for every object it holds - a trust line, a
        # credential, a domain - so a fixed transfer leaves it short of
        # its own floor by a few XRP. That is what put the treasury
        # 96 drops below the 1,000 XRP floor and made every transfer
        # look like a floor breach.
        balance = await xrp_balance(ws, args.to)
        print(f"  treasury holds {balance:,.4f} XRP, target {args.target:,.0f}")

        if balance >= args.target:
            print("  already at the target")
            return 0

        needed = args.target - balance
        # The faucet gives each account 1,000 XRP, so a single sender may
        # not have enough. Several senders are used in turn.
        senders = [
            a
            for a in sorted(items, key=lambda x: x["index"])
            if a["address"] != args.to and a["index"] != 0
        ]

        sent = 0.0

        # The FAUCET is the reliable source: it is unlimited, whereas the
        # devnet accounts hold 1,000 XRP each and cannot supply a large
        # treasury balance between them.
        try:
            import urllib.request

            request = urllib.request.Request(
                FAUCET_URL,
                data=json.dumps({"destination": args.to}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode())
            print(f"  the faucet granted {body.get('balance')} XRP")
            await asyncio.sleep(2)
        except Exception as exc:
            print(f"  the faucet could not be reached: {exc!r}")

        for sender in senders:
            if balance + sent >= args.target:
                break
            have = await xrp_balance(ws, sender["address"])
            # Leave the sender enough to keep paying fees.
            spare = have - 100.0
            if spare <= 0:
                continue
            chunk = min(spare, needed - sent)
            try:
                tx_hash = await send_xrp(ws, sender, args.to, chunk)
            except Exception as exc:
                print(f"  skipped {sender['address']}: {exc}")
                continue
            sent += chunk
            print(
                f"  sent {chunk:,.4f} XRP from {sender['address']} "
                f"({tx_hash[:16]}...)"
            )
            await asyncio.sleep(1.5)
            balance = await xrp_balance(ws, args.to)

        final = await xrp_balance(ws, args.to)
        print(f"  treasury balance now {final:,.4f} XRP")
        if final < args.target:
            print(f"  !! short of the target by {args.target - final:,.4f} XRP")
            return 1
        return 0


async def xrp_balance(ws, address: str) -> float:
    resp = await rpc(ws, {"command": "account_info", "account": address})
    data = resp["result"]["account_data"]
    return drops_to_xrp(int(data["Balance"]))


if __name__ == "__main__":
    asyncio.run(main())
