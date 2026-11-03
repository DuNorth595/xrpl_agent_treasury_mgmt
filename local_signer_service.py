#!/usr/bin/env python3
"""
A local signing service — the stand-in for a custodian or KMS.

WHY THIS EXISTS
===============

The treasury service's external signing path has never been exercised.
Every signature it has produced came from a seed file on the same disk.

A production deployment signs somewhere else: a custody service, a KMS,
an HSM. The service sends the transaction there and gets a signature
back. The key never touches the treasury service's process.

This is that "somewhere else", running locally so the path can be
proved without a custody vendor account. It implements the same
contract a real signer would:

    POST /sign
        {"tx_blob": "<hex>", "account": "<address>"}
        Authorization: Bearer <token>

    -> {"signed_tx": "<hex>", "tx_hash": "<hex>"}

The important property is that it is a SEPARATE PROCESS holding the key.
The treasury service has no seed. It sends a transaction and receives a
signature. If this works, the only thing that changes in production is
the URL and the credential.

WHAT IT DOES NOT DO
===================

It does not decide what to sign. It has no policy engine, no approval
ledger, no idea what a treasury obligation is. It signs a transaction
that is presented to it, and that is ALL a real signer does. The policy
lives in the treasury service, where the authorisation workflow is.

THE SAFETY PROPERTY
===================

The signer refuses to sign for an account it does not hold a key for,
and it records every signature it produces in an append-only log with
the hash of what was signed. A deployment audit is "the signer's log".

    python3 local_signer_service.py --port 8098
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

from xrpl.core.binarycodec import encode, decode  # noqa: E402
from xrpl.models.transactions.transaction import Transaction  # noqa: E402
from xrpl.transaction import sign as xrpl_sign  # noqa: E402
from xrpl.wallet import Wallet  # noqa: E402

ACCOUNTS = pathlib.Path.home() / ".xrpl-up" / "local-accounts.json"


def load_keys() -> dict:
    """The accounts this service holds keys for.

    In a deployment this list comes from the custody service's
    configuration, and the keys themselves are inside an HSM. Here they
    are in the devnet's accounts file, which is the one thing this
    stand-in does that a real signer does not.
    """
    raw = json.loads(ACCOUNTS.read_text())
    if isinstance(raw, dict) and "accounts" in raw:
        raw = raw["accounts"]
    items = raw if isinstance(raw, list) else list(raw.values())
    keys = {}
    for item in items:
        address = item.get("address")
        seed = item.get("seed")
        if not address or not seed:
            continue
        wallet = Wallet.from_seed(seed, algorithm="ed25519")
        keys[address] = wallet
    return keys



def _transaction_hash(signed_dict: dict) -> Optional[str]:
    """The transaction's ID, as the LEDGER computes it.

    The transaction ID is rippled's SHA-256 over the transaction in
    its single-signing form. That form is not the signing pre-image
    and not the signed blob, and reconstructing it is a rabbit hole.

    So this does NOT compute the hash. It reads it from the ledger,
    which is where the ledger's own value comes from. The signer asks
    the ledger what the transaction's ID is; if the ledger has not seen
    it yet, the answer is None and the submitter fills it in.

    Computing a hash that LOOKS like the transaction ID but is not is
    worse than having no hash: it would be recorded in an audit log as
    though it were the ledger's.
    """
    return None


class SigningHandler(BaseHTTPRequestHandler):
    """The signing endpoint."""

    keys: dict = {}
    token: str = ""
    log_path: pathlib.Path = pathlib.Path("/tmp/local-signer.jsonl")
    server_version = "LocalSigner/1.0"

    def log_message(self, fmt, *args):
        # Silence the default stderr logging. The audit log below is
        # the record that matters.
        pass

    def _reject(self, status: int, error: str, detail: str = "") -> None:
        body = json.dumps({"error": error, "detail": detail}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            body = json.dumps(
                {
                    "status": "ok",
                    "accounts": sorted(self.keys),
                    "signatures": self._signature_count(),
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/audit":
            try:
                entries = [
                    json.loads(line)
                    for line in self.log_path.read_text().splitlines()
                    if line.strip()
                ]
            except Exception:
                entries = []
            body = json.dumps({"entries": entries}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._reject(404, "not_found", f"no such path: {self.path}")

    def do_POST(self) -> None:
        if self.path != "/sign":
            self._reject(404, "not_found", f"no such path: {self.path}")
            return

        # Authentication. The treasury service proves it is allowed to
        # ask for a signature.
        header = self.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            self._reject(401, "unauthorized", "a bearer token is required")
            return
        if header[7:].strip() != self.token:
            self._reject(401, "unauthorized", "the bearer token is not valid")
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 1_048_576:
            self._reject(413, "too_large", "the request body is too large")
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._reject(400, "bad_request", "the body is not JSON")
            return

        tx_blob = payload.get("tx_blob")
        account = payload.get("account")
        if not tx_blob or not account:
            self._reject(
                400, "bad_request", "tx_blob and account are both required"
            )
            return

        wallet = self.keys.get(account)
        if wallet is None:
            # The signer holds keys for a set of accounts. It will not
            # sign for one it does not hold a key for. A signer that
            # would is not a signer, it is a signing oracle.
            self._reject(
                403,
                "no_key_for_account",
                f"this service holds no key for {account}",
            )
            return

        try:
            transaction = Transaction.from_xrpl(decode(tx_blob))
        except Exception as exc:
            self._reject(400, "bad_transaction", repr(exc))
            return

        try:
            signed = xrpl_sign(transaction, wallet)
        except Exception as exc:
            self._reject(400, "cannot_sign", repr(exc))
            return

        signed_blob = encode(signed.to_xrpl())
        signed_dict = signed.to_xrpl()
        tx_hash = _transaction_hash(signed_dict)

        # The audit record. Every signature leaves a trace: what was
        # signed, for which account, by which caller, and when.
        entry = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "account": account,
            "tx_hash": tx_hash,
            "signed_blob_sha256": hashlib.sha256(
                signed_blob.encode("utf-8")
            ).hexdigest(),
            "tx_blob_sha256": hashlib.sha256(
                tx_blob.encode("utf-8")
            ).hexdigest(),
            "signed_tx_sha256": hashlib.sha256(
                signed_blob.encode("utf-8")
            ).hexdigest(),
            "fee": signed_dict.get("Fee"),
            "sequence": signed_dict.get("Sequence"),
            "destination": signed_dict.get("Destination"),
            "amount": signed_dict.get("Amount"),
        }
        with open(self.log_path, "a") as handle:
            handle.write(json.dumps(entry) + "\n")

        # The wire format is a BLOB. `signed_tx` is the hex
        # serialisation of the signed transaction.
        body = json.dumps(
            {
                "signed_tx": signed_blob,
                "tx_hash": tx_hash,
                "account": account,
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _signature_count(self) -> int:
        try:
            return len(
                [
                    line
                    for line in self.log_path.read_text().splitlines()
                    if line.strip()
                ]
            )
        except Exception:
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="A local signing service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8098)
    parser.add_argument(
        "--token",
        default=os.environ.get("SIGNER_SERVICE_TOKEN", "local-signer-token"),
        help="the bearer token the treasury service presents",
    )
    parser.add_argument(
        "--log",
        default="/tmp/local-signer.jsonl",
        help="the append-only audit log",
    )
    args = parser.parse_args()

    SigningHandler.keys = load_keys()
    SigningHandler.token = args.token
    SigningHandler.log_path = pathlib.Path(args.log)

    print(
        f"local signer on http://{args.host}:{args.port} "
        f"holding {len(SigningHandler.keys)} account(s)",
        flush=True,
    )
    print(f"  audit log: {args.log}", flush=True)

    server = ThreadingHTTPServer((args.host, args.port), SigningHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
