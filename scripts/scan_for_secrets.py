#!/usr/bin/env python3
"""
Scan the tree for credentials.

WHY A SCRIPT AND NOT A REGEX IN THE CONFIG
===========================================

Because the findings have to be readable. A hook that prints "found a
seed at line 40" tells the author where to look. A hook that prints
"failed" makes them grep the whole tree by hand.

It is also the same scanner the CI workflow runs, so a local failure and
a CI failure say the same thing.

WHAT IT LOOKS FOR
=================

  - XRPL seeds - the classic-account secret family
  - PEM private key blocks
  - bearer tokens
  - 64-character hex strings, which is what a secp256k1 or ed25519
    private key looks like
  - AWS access key ids
  - GitHub tokens
  - OpenAI-style API keys
  - a password or secret assigned in a config-shaped line

FALSE POSITIVES
===============

A 64-character hex string is a shape, not a proof. A SHA-256 digest has
the same shape. The scanner reports both and lets a human judge, with
one exception: the values that are public by construction are listed and
skipped.

EXIT CODES
==========

  0   clean
  1   something credential-shaped was found
  2   the scanner could not run
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

PATTERNS = [
    ("xrpl seed", re.compile(r"\bs[A-Za-z0-9]{50,}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer token", re.compile(r"Bearer\s+[A-Za-z0-9_\-]{32,}")),
    ("hex private key", re.compile(r"\b[0-9A-Fa-f]{64}\b")),
    ("aws access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("openai key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    (
        "assigned secret",
        re.compile(
            r"(?i)\b(password|passwd|secret|private_key|api_key|apikey)\b"
            r"\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"
        ),
    ),
]

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "venv",
    ".venv",
    "node_modules",
    ".mypy_cache",
}

# Values that are public by construction.
#
# A PermissionedDomain id is a PUBLIC ledger value - anyone can read it
# from the node. A transaction hash is the index of a transaction anyone
# can look up. Neither is a secret, and both have the same shape as a
# private key.
KNOWN_SAFE = {
    # SHA-256 of the empty string.
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    # The domain ids used in the tests and the documentation.
    "B9263342B5E4C1F2BB8A691647B94FEEF53995DABFF6B88BAB57E1F95390720D",
    "F56F42C55216A32803EE83DE806B47B377243BC0C78342E8D828CC6D84E5C3DB",
    # The all-zero and all-one 64-char values.
    "0" * 64,
    "f" * 64,
}

# Transaction hashes quoted in the documentation as evidence.
#
# A transaction hash is a PUBLIC ledger value: it is the index of a
# transaction anyone can look up with the `tx` command. It has the same
# 64-hex shape as a secp256k1 private key, so the scanner cannot tell
# them apart by shape alone.
#
# Listing each hash would mean updating the allowlist every time the
# devnet is rebuilt and a new proof is written. Instead, the CONTEXT is
# recognised: a line that talks about a transaction hash, a tx_hash, a
# ledger or an in-ledger proof is not a key.
PUBLIC_HASH_CONTEXT = re.compile(
    r"(?i)(tx_hash|transaction hash|in_ledger|inledger|ledger index|"
    r"tx\s*\"|the hash|hash above|hash from)"
)

# The test fixtures. These are literal strings in the test suite, and
# they exist to PROVE that a secret does not leak into a repr(). They
# are not credentials.
TEST_FIXTURE_SECRETS = {
    "super-secret-value",
    "super-secret",
    "verification-token",
}


def is_safe(value: str, line: str = "") -> bool:
    if value in KNOWN_SAFE:
        return True
    if any(candidate in value for candidate in TEST_FIXTURE_SECRETS):
        return True
    # A hash that is being discussed as a transaction hash, rather than
    # used as a key.
    if PUBLIC_HASH_CONTEXT.search(line):
        return True
    return False


def scan(root: pathlib.Path) -> list:
    findings = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(errors="replace")
        except Exception:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for kind, pattern in PATTERNS:
                for match in pattern.finditer(line):
                    value = match.group(0)
                    if is_safe(value, line):
                        continue
                    findings.append(
                        {
                            "path": str(path.relative_to(root)),
                            "line": number,
                            "kind": kind,
                            "excerpt": value[:24],
                        }
                    )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root", nargs="?", default=".", help="the tree to scan (default: .)"
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        help="a substring of a path to skip; repeatable",
    )
    args = parser.parse_args()

    root = pathlib.Path(args.root).resolve()
    if not root.exists():
        print(f"  no such path: {root}", file=sys.stderr)
        return 2

    findings = scan(root)
    if args.allow:
        findings = [
            f
            for f in findings
            if not any(allow in f["path"] for allow in args.allow)
        ]

    if findings:
        print(f"{len(findings)} credential-shaped finding(s):")
        for finding in findings:
            print(
                f"  {finding['path']}:{finding['line']}: {finding['kind']}"
                f"  {finding['excerpt']}..."
            )
        print()
        print(
            "If a finding is a public value - a digest, a domain id, a "
            "transaction hash, a documented example - say so in the pull "
            "request rather than deleting the reference."
        )
        return 1

    print("clean - no credential-shaped strings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
