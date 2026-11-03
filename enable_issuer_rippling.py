#!/usr/bin/env python3
"""
Set the RLUSD issuer's account-level rippling default.

WHY THIS IS A SEPARATE STEP
===========================

An issued currency reaches its beneficiary by RIPPLING through the issuer.
Two settings decide whether that works:

  1. The trust LINE's own no-ripple bit, cleared by the holder with a
     TrustSet carrying `tfClearNoRipple` (0x00040000).

  2. The ISSUER's account-level `lsfDefaultRipple` (0x00800000, bit 23)
     - the issuer's "I will ripple by default" switch.

The second one is the trap. If the issuer does NOT have `lsfDefaultRipple`
set at the moment a trust line is created, that line carries a stale
issuer-side no-ripple bit. The stale bit cannot be repaired in place:
only the issuer can clear it, and the issuer cannot submit a TrustSet for
its own currency (`temDST_IS_SRC`).

So the issuer's default must be set BEFORE any trust line to that issuer
is created. This script therefore has to run before the currencies are
issued, not after.

THE BIT NUMBERING TRAP
=======================

The asf enum value (8, ASF_DEFAULT_RIPPLE) and the lsf bit position
(bit 23, 0x00800000) are DIFFERENT numbering spaces. After setting
`SetFlag: 8`, checking `flags & 8` always reads False and it looks like
the transaction did nothing. It did work. Check `flags & 0x00800000`.

    python3 enable_issuer_rippling.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import websockets
from xrpl.core.binarycodec import encode
from xrpl.models import AccountSet
from xrpl.transaction import sign
from xrpl.wallet import Wallet

from xrpl_agent_treasury_mgmt.devnet_accounts import address_for as _address

ACCOUNTS = pathlib.Path.home() / ".xrpl-up" / "local-accounts.json"
WS_URL = "ws://127.0.0.1:6006"
NETWORK_ID = 15791

# The RLUSD issuer account.
ISSUER = _address("COST_CENTRE_B")
ASF_DEFAULT_RIPPLE = 8
LSF_DEFAULT_RIPPLE = 0x00800000
TF_SET_FLAG = 0x00010000


def load_accounts():
    raw = json.loads(ACCOUNTS.read_text())
    if isinstance(raw, dict) and "accounts" in raw:
        raw = raw["accounts"]
    return raw if isinstance(raw, list) else list(raw.values())


async def rpc(ws, command):
    await ws.send(json.dumps(command))
    return json.loads(await ws.recv())


async def main():
    items = load_accounts()
    seed = next(a for a in items if a["address"] == ISSUER)["seed"]
    wallet = Wallet.from_seed(seed, algorithm="ed25519")

    async with websockets.connect(WS_URL) as ws:
        # RETRY. A freshly reset ledger is still settling its first
        # ledgers, and a transaction submitted in that window can be
        # rejected for a reason that has nothing to do with the flag -
        # a stale sequence, a last-ledger-sequence that is no longer
        # ahead of the current ledger. The failure is transient and the
        # fix is to try again with fresh state.
        last_error = ""
        for attempt in range(1, 6):
            info = await rpc(
                ws, {"command": "account_info", "account": ISSUER}
            )
            account_data = info["result"]["account_data"]
            flags = int(account_data.get("Flags", 0))

            if flags & LSF_DEFAULT_RIPPLE:
                print(
                    f"  the issuer allows rippling (flags={flags})"
                    + ("" if attempt == 1 else f" on attempt {attempt}")
                )
                return 0

            cur = await rpc(ws, {"command": "ledger_current"})
            current = int(cur["result"]["ledger_current_index"])
            seq = int(account_data["Sequence"])

            tx = AccountSet(
                account=ISSUER,
                set_flag=ASF_DEFAULT_RIPPLE,
                flags=TF_SET_FLAG,
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
            print(f"  AccountSet ASF_DEFAULT_RIPPLE: {engine}")
            last_error = str(engine)

            if engine == "tesSUCCESS":
                # Confirm against a FRESH read. The account_info
                # immediately after a submit can still show the old
                # flags, because the transaction has not been included
                # in a closed ledger yet.
                for _ in range(15):
                    await asyncio.sleep(1)
                    check = await rpc(
                        ws, {"command": "account_info", "account": ISSUER}
                    )
                    new_flags = int(
                        check["result"]["account_data"].get("Flags", 0)
                    )
                    if new_flags & LSF_DEFAULT_RIPPLE:
                        print(
                            f"  issuer flags now {new_flags} - "
                            "rippling allowed: True"
                        )
                        return 0
                print(
                    f"  the flag was set but has not been confirmed "
                    f"(flags={new_flags})"
                )
                last_error = "not_confirmed"
            else:
                await asyncio.sleep(2)

        print(f"  !! the issuer's rippling default could not be set: {last_error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
