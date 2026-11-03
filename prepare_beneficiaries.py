#!/usr/bin/env python3
"""
Prepare the devnet's cost centres so the treasury can actually settle.

THE PROBLEM
===========

A payment of an issued currency reaches its beneficiary by RIPPLING through
the issuer. Two independent settings decide whether that works:

  1. The trust LINE's own no-ripple bit. The holder clears this with a
     TrustSet carrying `tfClearNoRipple` (0x00040000).

  2. The ISSUER's account-level `lsfDefaultRipple` (0x00800000, bit 23).
     This is the issuer's "I will ripple by default" switch. It is set with
     an AccountSet carrying `SetFlag: 8` (ASF_DEFAULT_RIPPLE) and the
     transaction flag `tfSetFlag` (0x00010000).

If the issuer did not have `lsfDefaultRipple` set when a trust line was
created, the issuer's side of that line carries a stale no-ripple bit. The
HOLDER cannot clear it - only the issuer can, and the issuer cannot submit
a TrustSet for its own currency (`temDST_IS_SRC`). There is no transaction
that repairs it in place.

So the fix is to set `lsfDefaultRipple` on the issuer FIRST, and then have
each cost centre create its trust line. A line created after that point
ripples on both sides.

The trap in all of this: the asf enum value (8) and the lsf bit position
(bit 23) are DIFFERENT numbering spaces. Checking `flags & 8` after
setting `SetFlag: 8` always reads False, and it looks like the transaction
did nothing. The transaction did work. Check `flags & 0x00800000`.

    python3 prepare_beneficiaries.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

import websockets
from xrpl.core.binarycodec import encode
from xrpl.models import AccountSet, CredentialAccept, CredentialCreate, Payment
from xrpl.models.transactions.deposit_preauth import Credential
from xrpl.models.transactions.transaction import Transaction
from xrpl.transaction import sign
from xrpl.wallet import Wallet

from xrpl_agent_treasury_mgmt.devnet_accounts import address_for as _address

ACCOUNTS = pathlib.Path.home() / ".xrpl-up" / "local-accounts.json"
WS_URL = "ws://127.0.0.1:6006"
FAUCET_URL = "http://127.0.0.1:3001/faucet"
NETWORK_ID = 15791
CODE = "524C555344000000000000000000000000000000"
ISSUER = _address("COST_CENTRE_B")
TREASURY = _address("TREASURY")
# The tenant's configured cost centres. These are the accounts the
# treasury disburses to, so they have to be domain members.
CONFIGURED_COST_CENTRES = [
    _address("OUTSIDER"),
    _address("COST_CENTRE_A"),
]

# The DOMAIN OWNER is the treasury. A Credential lives in its SUBJECT's
# owner directory, and domain membership is read from the owner's
# directory, so the domain credential must be issued BY the domain owner.
DOMAIN_OWNER = TREASURY

# The treasury's PermissionedDomain. Membership is carried by Credential
# objects the owner issues, so a new beneficiary needs a credential whose
# type the domain accepts.
def _load_domain_id() -> str:
    """The PermissionedDomain id this environment is using.

    The domain is a ledger object, so it does not survive a ledger
    reset - a fresh environment has a different id. It is written to
    var/local-domain.json by create_domain.py and read from here, so the
    service always names the domain that actually exists. The fallback
    is the id the first proof run created.
    """
    path = pathlib.Path(__file__).resolve().parent / "var" / "local-domain.json"
    try:
        data = json.loads(path.read_text())
        domain_id = data.get("domain_id")
        if domain_id:
            return domain_id
    except Exception:
        pass
    return "F56F42C55216A32803EE83DE806B47B377243BC0C78342E8D828CC6D84E5C3DB"


DOMAIN_ID = _load_domain_id()
# The credential type is a HEX STRING, not bytes: xrpl-py validates it
# against a hex regex and rejects bytes outright.
#
# It is read from the DOMAIN itself, because the domain names the
# credential types it admits. Hardcoding it means a credential of the
# wrong type is issued, the credential is accepted, and the account is
# still not a member - a failure that looks like a working credential.
DOMAIN_CREDENTIAL_TYPE = "74726561737572792E696E7465726E616C"


async def read_domain_credential_type(ws) -> str:
    """The credential type the live domain admits."""
    entry = await rpc(ws, {"command": "ledger_entry", "index": DOMAIN_ID})
    node = entry.get("result", {}).get("node", {}) or {}
    for accepted in node.get("AcceptedCredentials", []) or []:
        credential = accepted.get("Credential", {}) or {}
        credential_type = credential.get("CredentialType")
        if credential_type:
            return credential_type
    return DOMAIN_CREDENTIAL_TYPE

TF_SET_FLAG = 0x00010000
TF_CLEAR_NO_RIPPLE = 0x00040000
ASF_DEFAULT_RIPPLE = 8
LSF_DEFAULT_RIPPLE = 0x00800000


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


async def submit(ws, model, wallet):
    signed = sign(model, wallet)
    resp = await rpc(
        ws, {"command": "submit", "tx_blob": encode(signed.to_xrpl())}
    )
    result = resp.get("result", {})
    return result.get("engine_result"), (result.get("tx_json") or {}).get("hash")


async def ensure_issuer_allows_rippling(ws, items) -> bool:
    """Set ASF_DEFAULT_RIPPLE on the issuer account.

    Changing an account flag requires authority. The master key works as
    long as `lsfDisableMaster` is not set, which is the case on a fresh
    devnet account.
    """
    info = await rpc(ws, {"command": "account_info", "account": ISSUER})
    flags = int(info["result"]["account_data"].get("Flags", 0))
    if flags & LSF_DEFAULT_RIPPLE:
        print(f"    issuer already allows rippling (flags={flags})")
        return True

    wallet = wallet_for(items, ISSUER)
    current = await current_ledger(ws)
    seq = await sequence(ws, ISSUER)
    tx = AccountSet(
        account=ISSUER,
        set_flag=ASF_DEFAULT_RIPPLE,
        flags=TF_SET_FLAG,
        fee="12",
        sequence=seq,
        network_id=NETWORK_ID,
        last_ledger_sequence=current + 100,
    )
    engine, tx_hash = await submit(ws, tx, wallet)
    print(f"    AccountSet ASF_DEFAULT_RIPPLE: {engine} {tx_hash}")
    await asyncio.sleep(2)

    info = await rpc(ws, {"command": "account_info", "account": ISSUER})
    flags = int(info["result"]["account_data"].get("Flags", 0))
    ok = bool(flags & LSF_DEFAULT_RIPPLE)
    print(f"    issuer flags now {flags} - rippling allowed: {ok}")
    return ok


async def create_beneficiary(ws, items) -> dict:
    """Create a beneficiary account whose RLUSD line ripples on both sides."""
    wallet = Wallet.create()
    address = wallet.classic_address

    # Fund it so the account exists on the ledger.
    import urllib.request

    request = urllib.request.Request(
        FAUCET_URL,
        data=json.dumps({"destination": address}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        body = json.loads(urllib.request.urlopen(request, timeout=25).read())
        print(f"    funded {address}: {body.get('balance')} XRP")
    except Exception as exc:
        print(f"    !! faucet failed for {address}: {exc!r}")
        return {}
    await asyncio.sleep(3)

    # The trust line is created now, with the issuer already set to ripple.
    current = await current_ledger(ws)
    seq = await sequence(ws, address)
    raw_trust_set = {
        "TransactionType": "TrustSet",
        "Account": address,
        "LimitAmount": {
            "currency": CODE,
            "issuer": ISSUER,
            "value": "1000000000000000e-4",
        },
        "Flags": TF_CLEAR_NO_RIPPLE,
        "Fee": "12",
        "Sequence": seq,
        "SigningPubKey": wallet.public_key,
        "NetworkID": NETWORK_ID,
        "LastLedgerSequence": current + 100,
    }
    signed = sign(Transaction.from_xrpl(raw_trust_set), wallet)
    resp = await rpc(
        ws, {"command": "submit", "tx_blob": encode(signed.to_xrpl())}
    )
    print(f"    TrustSet: {resp.get('result', {}).get('engine_result')}")
    await asyncio.sleep(2)

    # Verify the line ripples on both sides.
    lines = await rpc(
        ws, {"command": "account_lines", "account": address, "peer": ISSUER}
    )
    for line in lines.get("result", {}).get("lines", []):
        if line.get("currency") == CODE:
            print(
                f"    no_ripple={line.get('no_ripple')} "
                f"no_ripple_peer={line.get('no_ripple_peer')}"
            )

    # The beneficiary must hold an ACCEPTED credential whose type the
    # domain accepts, otherwise it is not a domain member and a payment
    # to it with DomainID is refused by the ledger.
    await issue_domain_credential(ws, items, address, wallet)

    # The tenant's CONFIGURED cost centres must be members too. They are
    # the accounts the treasury actually disburses to, and a domain
    # member that does not hold an accepted credential is not a member
    # of anything - every payment to them is refused with
    # tecNO_PERMISSION.
    for cost_centre in CONFIGURED_COST_CENTRES:
        if cost_centre == address:
            continue
        print(f"    enrolling the configured cost centre {cost_centre}")
        await issue_domain_credential(
            ws, items, cost_centre, wallet_for(items, cost_centre)
        )
        await ensure_cost_centre_can_receive(
            ws, items, cost_centre
        )

    return {
        "address": address,
        "seed": wallet.seed,
        "public_key": wallet.public_key,
        "role": "cost_centre",
    }


async def ensure_cost_centre_can_receive(
    ws, items, cost_centre: str
) -> None:
    """Make sure a cost centre's trust line can actually ripple.

    A cost centre can be a domain member and still be UNPAYABLE. If its
    trust line was created before the issuer allowed rippling, the
    issuer's side of the line carries a stale no-ripple bit, and every
    payment to it fails with tecPATH_DRY.

    The stale bit cannot be repaired in place: only the issuer can clear
    it, and the issuer cannot submit a TrustSet for its own currency
    (temDST_IS_SRC). What CAN be done is to set the HOLDER's account
    default to allow rippling (ASF_DEFAULT_RIPPLE) and then re-create
    the trust line, which picks up the new default.

    This is a no-op for a cost centre that already ripples.
    """
    code = CODE
    issuer = ISSUER

    # Is it already payable?
    try:
        lines = await rpc(
            ws, {"command": "account_lines", "account": cost_centre}
        )
        for line in lines.get("result", {}).get("lines", []):
            if line.get("currency") != code:
                continue
            if not line.get("no_ripple") and not line.get("no_ripple_peer"):
                print("    its trust line already ripples")
                return
    except Exception:
        pass

    wallet = wallet_for(items, cost_centre)

    # 1. Set the account's default to allow rippling.
    info = await rpc(
        ws, {"command": "account_info", "account": cost_centre}
    )
    flags = int(info["result"]["account_data"].get("Flags", 0))
    if not flags & LSF_DEFAULT_RIPPLE:
        current = await current_ledger(ws)
        seq = await sequence(ws, cost_centre)
        account_set = AccountSet(
            account=cost_centre,
            set_flag=ASF_DEFAULT_RIPPLE,
            flags=TF_SET_FLAG,
            fee="12",
            sequence=seq,
            network_id=NETWORK_ID,
            last_ledger_sequence=current + 100,
        )
        engine, tx_hash = await submit(ws, account_set, wallet)
        print(f"    AccountSet ASF_DEFAULT_RIPPLE: {engine} {tx_hash}")
        await asyncio.sleep(2)

    # 2. Re-create the trust line so it picks up the new default.
    current = await current_ledger(ws)
    seq = await sequence(ws, cost_centre)
    raw_trust_set = {
        "TransactionType": "TrustSet",
        "Account": cost_centre,
        "LimitAmount": {
            "currency": code,
            "issuer": issuer,
            "value": "1000000000000000e-4",
        },
        "Flags": TF_CLEAR_NO_RIPPLE,
        "Fee": "12",
        "Sequence": seq,
        "SigningPubKey": wallet.public_key,
        "NetworkID": NETWORK_ID,
        "LastLedgerSequence": current + 100,
    }
    signed = sign(Transaction.from_xrpl(raw_trust_set), wallet)
    resp = await rpc(
        ws, {"command": "submit", "tx_blob": encode(signed.to_xrpl())}
    )
    engine = (resp.get("result", {}) or {}).get("engine_result")
    print(f"    TrustSet re-create: {engine}")
    await asyncio.sleep(2)

    # 3. Verify.
    lines = await rpc(
        ws, {"command": "account_lines", "account": cost_centre}
    )
    for line in lines.get("result", {}).get("lines", []):
        if line.get("currency") == code:
            print(
                f"    -> no_ripple={line.get('no_ripple')} "
                f"no_ripple_peer={line.get('no_ripple_peer')}"
            )


async def issue_domain_credential(ws, items, subject: str, subject_wallet) -> None:
    """Issue and accept a domain credential for a beneficiary.

    The owner issues the credential (CredentialCreate) and the subject
    accepts it (CredentialAccept). Only an ACCEPTED credential counts as
    domain membership.
    """
    owner_wallet = wallet_for(items, DOMAIN_OWNER)

    # The type the LIVE domain admits, not a hardcoded guess.
    credential_type = await read_domain_credential_type(ws)
    print(f"    the domain admits credential type {credential_type}")

    # CredentialCreate, signed by the domain owner.
    current = await current_ledger(ws)
    seq = await sequence(ws, DOMAIN_OWNER)
    create = CredentialCreate(
        account=DOMAIN_OWNER,
        subject=subject,
        credential_type=credential_type,
        fee="12",
        sequence=seq,
        network_id=NETWORK_ID,
        last_ledger_sequence=current + 100,
    )
    engine, tx_hash = await submit(ws, create, owner_wallet)
    print(f"    CredentialCreate: {engine} {tx_hash}")
    await asyncio.sleep(2)

    # CredentialAccept, signed by the subject.
    current = await current_ledger(ws)
    seq = await sequence(ws, subject)
    accept = CredentialAccept(
        account=subject,
        issuer=DOMAIN_OWNER,
        credential_type=credential_type,
        fee="12",
        sequence=seq,
        network_id=NETWORK_ID,
        last_ledger_sequence=current + 100,
    )
    engine, tx_hash = await submit(ws, accept, subject_wallet)
    print(f"    CredentialAccept: {engine} {tx_hash}")
    await asyncio.sleep(2)

    # Verify the credential is accepted.
    objects = await rpc(
        ws, {"command": "account_objects", "account": DOMAIN_OWNER}
    )
    for entry in objects.get("result", {}).get("account_objects", []):
        if (
            entry.get("LedgerEntryType") == "Credential"
            and entry.get("Subject") == subject
            and entry.get("Flags", 0) & 0x00010000
        ):
            print(
                "    the credential is ACCEPTED - the beneficiary is a "
                "domain member"
            )
            return
    print(
        "    !! the credential is not accepted; the beneficiary is NOT a "
        "domain member"
    )


async def main():
    items = load_accounts()
    async with websockets.connect(WS_URL) as ws:
        # The issuer's rippling default is set by enable_issuer_rippling.py,
        # which MUST run before any trust line to that issuer is created.
        # A line created while the issuer refused to ripple carries a
        # stale issuer-side no-ripple bit that cannot be repaired in
        # place - only the issuer can clear it, and the issuer cannot
        # submit a TrustSet for its own currency.
        print("  1. checking the issuer's rippling default")
        if not await ensure_issuer_allows_rippling(ws, items):
            print(
                "  !! the issuer will not ripple; no settlement is possible. "
                "Run enable_issuer_rippling.py BEFORE issuing the "
                "currencies."
            )
            return 1

        print("  2. beneficiary with a rippling trust line")
        beneficiary = await create_beneficiary(ws, items)

        if beneficiary:
            out = HERE / "var" / "devnet-beneficiaries.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            existing = {}
            if out.exists():
                existing = json.loads(out.read_text())
            existing[beneficiary["address"]] = beneficiary
            out.write_text(json.dumps(existing, indent=2))
            print(f"    saved to {out}")

    return 0


HERE = pathlib.Path(__file__).resolve().parent

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
