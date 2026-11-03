"""
The local devnet accounts, resolved by ROLE.

WHY THIS EXISTS
===============

The devnet account addresses used to be hardcoded across the project:
in `run_demo.py`, in the setup scripts, in the harnesses, in the tests,
and in the documentation.

That is a publication hazard, and a subtle one. The addresses are not
secrets on their own - they are public ledger values. But the devnet
accounts file on disk holds the SEEDS for those same addresses, so
anyone who has both can sign for them. Publishing the addresses
publishes half of what is needed to spend from them, and the other half
sits in a well-known file.

So the addresses are resolved at runtime, by role, from the local
accounts file. A clone has no accounts file, and the code says so
clearly instead of pointing at an address whose key somebody else
holds.

THE ROLES
==========

    0  TREASURY         the tenant's own account
    1  OUTSIDER         deliberately outside the domain, for the gate test
    2  COST_CENTRE_A    a member of the domain, a cost centre
    3  ISSUER           the issued-currency issuer
    5  BENEFICIARY      a payment destination
    6  BENEFICIARY_2    a second payment destination
    8  COST_CENTRE_B    a member of the domain, a cost centre

THE ROLES ARE A LOCAL CONVENTION
=================================

They are this project's devnet convention, not an XRPL standard. The
mapping is in ROLE_INDEX below and in the accounts file that xrpl-up
writes.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Dict, List, Optional

ACCOUNTS_PATH = pathlib.Path(
    os.environ.get(
        "TREASURY_DEVNET_ACCOUNTS",
        str(pathlib.Path.home() / ".xrpl-up" / "local-accounts.json"),
    )
)

# The role each devnet account index plays.
ROLE_INDEX: Dict[str, int] = {
    "TREASURY": 0,
    "OUTSIDER": 1,
    "COST_CENTRE_A": 2,
    "ISSUER": 3,
    "BENEFICIARY": 5,
    "BENEFICIARY_2": 6,
    "COST_CENTRE_B": 8,
}

# The roles, in the order the setup scripts use them.
ROLES: List[str] = list(ROLE_INDEX)


class DevnetAccountsError(RuntimeError):
    """The local devnet accounts could not be read."""


def _load() -> List[dict]:
    if not ACCOUNTS_PATH.exists():
        raise DevnetAccountsError(
            f"the local devnet accounts file is not there: {ACCOUNTS_PATH}\n"
            f"run `python3 dev_env.py fresh` to build the environment, or set "
            f"TREASURY_DEVNET_ACCOUNTS to the file."
        )
    raw = json.loads(ACCOUNTS_PATH.read_text())
    if isinstance(raw, dict) and "accounts" in raw:
        raw = raw["accounts"]
    items = raw if isinstance(raw, list) else list(raw.values())
    return [i for i in items if isinstance(i, dict) and "address" in i]


def account_for(role: str) -> dict:
    """The devnet account that plays a role.

    Returns the whole entry, so the caller can take the address or the
    seed.
    """
    if role not in ROLE_INDEX:
        raise DevnetAccountsError(
            f"unknown devnet role {role!r}; the roles are: "
            f"{', '.join(sorted(ROLE_INDEX))}"
        )
    wanted = ROLE_INDEX[role]
    for item in _load():
        if item.get("index") == wanted:
            return item
    raise DevnetAccountsError(
        f"the devnet accounts file has no account at index {wanted} "
        f"(the role {role!r})"
    )


def address_for(role: str) -> str:
    """The classic address of the account that plays a role."""
    return account_for(role)["address"]


def seed_for(role: str) -> str:
    """The seed of the account that plays a role.

    Only the setup scripts and the local signer need this. Nothing that
    reads a role should ever need it.
    """
    return account_for(role).get("seed", "")


def all_addresses() -> Dict[str, str]:
    """Every role and its address."""
    return {role: address_for(role) for role in ROLES}


def public_key_for(role: str) -> str:
    return account_for(role).get("publicKey", "")


# ------------------------------------------------------------------
# The generated beneficiaries
# ------------------------------------------------------------------
#
# prepare_beneficiaries.py creates a set of cost-centre accounts on the
# devnet and writes their addresses AND their seeds to
# var/devnet-beneficiaries.json. So an address from that file is as
# sensitive as one from the accounts file: publishing it publishes half
# of what is needed to spend from it.
#
# The file is local state and is not published, so the addresses are
# resolved from it at runtime.

def _default_beneficiaries_path() -> pathlib.Path:
    """Where the generated beneficiaries are kept, by default.

    The state directory sits beside the source tree - the same place the
    builder puts it - so a checkout finds it without configuration. The
    path is DERIVED, never hardcoded: a published package must not
    contain a path from the machine that built it.
    """
    here = pathlib.Path(__file__).resolve()
    # src/xrpl_agent_treasury_mgmt/devnet_accounts.py -> the project root
    root = here.parent.parent.parent
    return root / "var" / "devnet-beneficiaries.json"


BENEFICIARIES_PATH = pathlib.Path(
    os.environ.get("TREASURY_DEVNET_BENEFICIARIES")
    or _default_beneficiaries_path()
)


def beneficiaries() -> List[dict]:
    """The generated beneficiary accounts."""
    if not BENEFICIARIES_PATH.exists():
        raise DevnetAccountsError(
            f"the local devnet beneficiaries file is not there: "
            f"{BENEFICIARIES_PATH}\nrun `python3 dev_env.py seed` to build "
            f"the environment."
        )
    raw = json.loads(BENEFICIARIES_PATH.read_text())
    items = raw if isinstance(raw, list) else list(raw.values())
    return [i for i in items if isinstance(i, dict) and "address" in i]


def beneficiary_address(index: int = 0) -> str:
    """The address of a generated beneficiary.

    The index picks one of the generated cost centres. It is not a
    role, because they are all the same role - they exist to be paid.
    """
    accounts = beneficiaries()
    if not accounts:
        raise DevnetAccountsError("there are no beneficiaries in the file")
    if index < 0 or index >= len(accounts):
        raise DevnetAccountsError(
            f"there are {len(accounts)} beneficiaries; index {index} is out "
            f"of range"
        )
    return accounts[index]["address"]


def beneficiary_seed(index: int = 0) -> str:
    """The seed of a generated beneficiary. Local setup only."""
    accounts = beneficiaries()
    if not accounts:
        raise DevnetAccountsError("there are no beneficiaries in the file")
    return accounts[index].get("seed", "")


if __name__ == "__main__":
    for role, address in all_addresses().items():
        print(f"{role:<14} {address}")
