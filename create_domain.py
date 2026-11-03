#!/usr/bin/env python3
"""
Create the treasury's PermissionedDomain on the devnet.

WHY
===

The domain is the ledger-enforced perimeter the whole service rests on:
a Payment carrying a DomainID is refused by the ledger unless both the
sender and the destination hold an accepted credential in that domain.
The domain is a ledger OBJECT, not configuration, so it has to exist on
the chain before the service can use it - and it does not survive a
ledger reset.

This creates it and writes its id to `var/local-domain.json` so the rest
of the project can find it.

The domain admits one credential type, `treasury.internal`, which is
what the service issues to its own members. The XRPL's separate
CredentialIDs mechanism (DepositPreauth) is NOT used - that is a
different gate for a different purpose.

    python3 create_domain.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import websockets
from xrpl.core.binarycodec import encode
from xrpl.models import PermissionedDomainSet
from xrpl.models.transactions.deposit_preauth import Credential
from xrpl.transaction import sign
from xrpl.wallet import Wallet

from xrpl_agent_treasury_mgmt.devnet_accounts import address_for as _address

ACCOUNTS = pathlib.Path.home() / ".xrpl-up" / "local-accounts.json"
WS_URL = "ws://127.0.0.1:6006"
NETWORK_ID = 15791
HERE = pathlib.Path(__file__).resolve().parent

# The treasury owns the domain.
OWNER = _address("TREASURY")
# The credential type the domain admits, as hex. This is what makes an
# account a member: it holds an ACCEPTED credential of this type.
CREDENTIAL_TYPE = "74726561737572792E696E7465726E616C"


def load_accounts():
    raw = json.loads(ACCOUNTS.read_text())
    if isinstance(raw, dict) and "accounts" in raw:
        raw = raw["accounts"]
    return raw if isinstance(raw, list) else list(raw.values())


def wallet_for(items, address):
    seed = next(a for a in items if a["address"] == address)["seed"]
    return Wallet.from_seed(seed, algorithm="ed25519")


async def rpc(ws, command):
    await ws.send(json.dumps(command))
    return json.loads(await ws.recv())


async def current_ledger(ws):
    resp = await rpc(ws, {"command": "ledger_current"})
    return int(resp["result"]["ledger_current_index"])


async def sequence(ws, account):
    resp = await rpc(ws, {"command": "account_info", "account": account})
    return int(resp["result"]["account_data"]["Sequence"])


async def main():
    items = load_accounts()
    wallet = wallet_for(items, OWNER)

    async with websockets.connect(WS_URL) as ws:
        # Does a domain already exist? Reusing it is better than creating
        # a second one, because the service's configuration names a
        # specific domain id.
        objects = await rpc(ws, {"command": "account_objects", "account": OWNER})
        existing = None
        for entry in objects.get("result", {}).get("account_objects", []):
            if entry.get("LedgerEntryType") == "PermissionedDomain":
                existing = entry.get("index")
                break

        if existing:
            print(f"  a domain already exists: {existing}")
            domain_id = existing
        else:
            current = await current_ledger(ws)
            seq = await sequence(ws, OWNER)
            tx = PermissionedDomainSet(
                account=OWNER,
                accepted_credentials=[
                    Credential(
                        issuer=OWNER,
                        credential_type=CREDENTIAL_TYPE,
                    )
                ],
                fee="12",
                sequence=seq,
                network_id=NETWORK_ID,
                last_ledger_sequence=current + 100,
            )
            signed = sign(tx, wallet)
            resp = await rpc(
                ws, {"command": "submit", "tx_blob": encode(signed.to_xrpl())}
            )
            result = resp.get("result", {}) or {}
            engine = result.get("engine_result")
            print(f"  PermissionedDomainSet: {engine}")
            if engine != "tesSUCCESS":
                print("  !! the domain was not created")
                return 1

            await asyncio.sleep(3)

            # The authoritative source for the domain id is the
            # transaction's own metadata, not account_objects.
            domain_id = None
            objects = await rpc(
                ws, {"command": "account_objects", "account": OWNER}
            )
            for entry in objects.get("result", {}).get("account_objects", []):
                if entry.get("LedgerEntryType") == "PermissionedDomain":
                    domain_id = entry.get("index")
                    break

            if not domain_id:
                print("  !! the domain was created but could not be read back")
                return 1

        print(f"  domain id: {domain_id}")

        out = HERE / "var" / "local-domain.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "domain_id": domain_id,
                    "owner": OWNER,
                    "credential_type": CREDENTIAL_TYPE,
                    "credential_type_name": "treasury.internal",
                },
                indent=2,
            )
        )
        print(f"  saved to {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
