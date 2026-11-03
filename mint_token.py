#!/usr/bin/env python3
"""
Mint API credentials for the treasury service.

WHY A CLI
=========

A token has to be created somewhere, and the answer cannot be "in the
code" or "in a file in the repository". This CLI mints a token, prints
it ONCE, and produces the environment variable that the service reads.

    python3 mint_token.py --name treasury-ops --scopes treasury:read
    python3 mint_token.py --name authoriser --scopes treasury:read,treasury:authorise

The printed token is shown once and cannot be recovered. Only its
SHA-256 hash is kept by the service, so a leaked configuration file does
not yield a usable credential.

USING IT
========

    export TREASURY_API_TOKENS="$(python3 mint_token.py --name ops --scopes treasury:read --env)"
    python3 run_demo.py --host 127.0.0.1 --port 8095

    curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8095/api/tenants

`--env` prints the whole `TREASURY_API_TOKENS` value, ready to be
exported. Without it, the token is printed on its own.

THE SCOPES
===========

    treasury:read       see the position, the plan, the obligations, the
                        decision log, the authorisations
    treasury:propose    build a dry run - propose a transfer without
                        committing to it
    treasury:authorise  record a sign-off on a transfer
    treasury:settle     submit a real payment to the ledger
    treasury:admin      everything, including administering credentials

A token should have the SMALLEST scope that does the job. A read token
that can authorise is a read token that can authorise.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

from xrpl_agent_treasury_mgmt.auth import (  # noqa: E402
    ALL_SCOPES,
    mint_credential,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mint an API credential for the treasury service"
    )
    parser.add_argument("--name", required=True, help="the credential's name")
    parser.add_argument(
        "--scopes",
        action="append",
        default=None,
        help=(
            "a scope, or a comma-separated list of scopes, or 'all'. "
            "This may be repeated: --scopes treasury:read "
            "--scopes treasury:settle. Repeating it used to keep only "
            "the last value, which silently produced a token with MORE "
            "privilege than was asked for."
        ),
    )
    parser.add_argument(
        "--tenants",
        default="",
        help="comma-separated tenant ids this credential may touch; "
        "empty means all tenants",
    )
    parser.add_argument(
        "--description", default="", help="what this credential is for"
    )
    parser.add_argument(
        "--env",
        action="store_true",
        help="print a TREASURY_API_TOKENS value for the whole store",
    )
    parser.add_argument(
        "--out",
        default="",
        help="write the TREASURY_API_TOKENS value to this file "
        "(mode 0600) instead of stdout",
    )
    args = parser.parse_args()

    # Every occurrence is collected, and every occurrence may itself be
    # a comma-separated list. Both forms work, and they combine.
    raw_scopes = args.scopes if args.scopes else ["treasury:read"]
    scopes = []
    for chunk in raw_scopes:
        for piece in str(chunk).split(","):
            piece = piece.strip()
            if not piece:
                continue
            if piece.lower() == "all":
                scopes.extend(ALL_SCOPES)
            else:
                scopes.append(piece)

    # De-duplicate, preserving order.
    seen = set()
    scopes = [s for s in scopes if not (s in seen or seen.add(s))]

    unknown = [s for s in scopes if s not in ALL_SCOPES]
    if unknown:
        print(f"unknown scope(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"the scopes are: {', '.join(ALL_SCOPES)}", file=sys.stderr)
        return 2

    tenants = [t.strip() for t in args.tenants.split(",") if t.strip()]

    credential, token = mint_credential(
        name=args.name,
        scopes=scopes,
        tenants=tenants,
        description=args.description,
    )

    store = {
        args.name: {
            "token": token,
            "scopes": sorted(scopes),
            "tenants": tenants,
            "description": args.description,
        }
    }

    if args.env or args.out:
        # Merge with any existing store, so several credentials can be
        # minted and combined.
        existing = {}
        if args.out and pathlib.Path(args.out).exists():
            try:
                existing = json.loads(pathlib.Path(args.out).read_text())
            except Exception:
                existing = {}
        existing.update(store)
        value = json.dumps(existing)

        if args.out:
            path = pathlib.Path(args.out)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
            path.chmod(0o600)
            print(f"  wrote {len(existing)} credential(s) to {path} (mode 0600)")
            print(f"  token for {args.name}: {token}")
            print()
            print("  This file holds usable credentials. Keep it out of any")
            print("  repository, and out of any shared directory.")
            return 0

        print(value)
        return 0

    print(f"  name       : {credential.name}")
    print(f"  scopes     : {', '.join(sorted(scopes))}")
    print(f"  tenants    : {', '.join(tenants) if tenants else 'all'}")
    print(f"  token      : {token}")
    print()
    print("  This token is shown ONCE. The service stores only its hash,")
    print("  so it cannot be recovered if it is lost. Mint a new one.")
    print()
    print("  export TREASURY_API_TOKENS='"
          + json.dumps(store)
          + "'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
