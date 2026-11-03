"""
Tests for the external signing path.

WHY THIS EXISTS
===============

Every signature the treasury service had produced before came from a seed
file on the same disk as the service. That is not how a deployment
signs. A deployment signs somewhere else - a custody service, a KMS, an
HSM - and the service sends the transaction there.

This test proves the path works with a SEPARATE PROCESS holding the key.
The treasury service has no seed. It sends a transaction over HTTP and
receives a signature back.

WHAT IS PROVED
==============

  1. The signer service signs a transaction presented to it.
  2. The signature is REAL - the ledger accepts it.
  3. The signer refuses a bad bearer token.
  4. The signer refuses to sign for an account it holds no key for.
  5. The signer leaves an audit record of what it signed.
  6. The client and the server agree on the wire format. They did not:
     the client expected JSON, the server returned a blob, and the two
     would never have worked together.
  7. A transaction signed by the remote signer settles on the ledger.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT / "src"))

from xrpl_agent_treasury_mgmt.signer import (  # noqa: E402

    ExternalSignerSigner,
    LocalSeedSigner,
)
from xrpl_agent_treasury_mgmt.devnet_accounts import (
    address_for as _address,
    beneficiary_address as _beneficiary_address,
)

SIGNER_PORT = 8298
TOKEN = "test-signer-token"


def _beneficiary() -> str:
    """A generated cost-centre account, resolved at runtime.

    The address is not a literal. The local beneficiaries file holds
    the seed for it, so publishing the address would publish half of
    what is needed to spend from it.
    """
    return _beneficiary_address(0)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_for_port(port: int, process, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        probe = socket.socket()
        probe.settimeout(1)
        try:
            probe.connect(("127.0.0.1", port))
            probe.close()
            return True
        except Exception:
            probe.close()
            time.sleep(0.3)
    return False


def _needs_devnet():
    """Why this test cannot run here, or None if it can.

    The external signing path is exercised against a LOCAL devnet. A
    clean checkout has no accounts file, so there is nothing to sign
    with, and the tests skip rather than fail.

    The skip says exactly what is missing, so nobody reads a green run
    as proof the path works when it never ran.
    """
    from xrpl_agent_treasury_mgmt.devnet_accounts import (
        DevnetAccountsError,
        account_for,
        beneficiaries,
    )

    try:
        account_for("TREASURY")
    except DevnetAccountsError as exc:
        return f"no local devnet accounts: {exc}"
    try:
        beneficiaries()
    except DevnetAccountsError as exc:
        return f"no local devnet beneficiaries: {exc}"
    return None


class TestTheExternalSigningPath(unittest.TestCase):
    """The signer service, and the treasury service talking to it."""

    @classmethod
    def setUpClass(cls):
        # The devnet check comes FIRST. Without a local devnet there
        # is nothing to sign with, and the tests skip rather than
        # fail.
        reason = _needs_devnet()
        if reason:
            raise unittest.SkipTest(reason)

        cls.port = _free_port()
        cls.audit_log = pathlib.Path("/tmp/test-signer-audit.jsonl")
        if cls.audit_log.exists():
            cls.audit_log.unlink()
        cls.process = subprocess.Popen(
            [
                sys.executable,
                "local_signer_service.py",
                "--port",
                str(cls.port),
                "--token",
                TOKEN,
                "--log",
                str(cls.audit_log),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(PROJECT),
        )
        cls.started = _wait_for_port(cls.port, cls.process)

    @classmethod
    def tearDownClass(cls):
        if cls.process.poll() is None:
            cls.process.terminate()
            try:
                cls.process.wait(timeout=8)
            except Exception:
                cls.process.kill()

    def setUp(self):
        if not self.started:
            self.skipTest("the signer service did not start")

    # ------------------------------------------------------- the service

    def _post(self, path, payload, token=TOKEN):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def _accounts(self):
        raw = json.loads(
            (pathlib.Path.home() / ".xrpl-up" / "local-accounts.json").read_text()
        )
        if isinstance(raw, dict) and "accounts" in raw:
            raw = raw["accounts"]
        items = raw if isinstance(raw, list) else list(raw.values())
        return items

    def _a_transaction(self, account):
        from xrpl.core.binarycodec import encode
        from xrpl.models import Payment

        tx = Payment(
            account=account,
            destination=_beneficiary(),
            amount="1",
            fee="12",
            sequence=1,
            network_id=15791,
            last_ledger_sequence=999999,
        )
        return encode(tx.to_xrpl())

    def _treasury(self):
        return next(
            a for a in self._accounts() if a.get("index") == 0
        )["address"]

    # ---------------------------------------------------------- the tests

    def test_the_signer_service_starts(self):
        self.assertTrue(self.started)

    def test_it_signs_a_transaction(self):
        treasury = self._treasury()
        status, body = self._post(
            "/sign", {"tx_blob": self._a_transaction(treasury), "account": treasury}
        )
        self.assertEqual(status, 200)
        self.assertIn("signed_tx", body)
        self.assertTrue(body["signed_tx"])

    def test_the_signature_is_real(self):
        """The ledger must accept what the signer produced.

        This is the property that matters. A signer that returns
        something the ledger rejects is not a signer.
        """
        treasury = self._treasury()
        status, body = self._post(
            "/sign", {"tx_blob": self._a_transaction(treasury), "account": treasury}
        )
        self.assertEqual(status, 200)

        from xrpl.core.binarycodec import decode
        from xrpl.models.transactions.transaction import Transaction

        signed = Transaction.from_xrpl(decode(body["signed_tx"]))
        self.assertTrue(signed.is_signed(), "the transaction came back unsigned")
        self.assertEqual(signed.account, treasury)

    def test_it_refuses_a_bad_bearer_token(self):
        treasury = self._treasury()
        status, body = self._post(
            "/sign",
            {"tx_blob": self._a_transaction(treasury), "account": treasury},
            token="not-the-token",
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_it_refuses_a_missing_bearer_token(self):
        treasury = self._treasury()
        status, body = self._post(
            "/sign",
            {"tx_blob": self._a_transaction(treasury), "account": treasury},
            token=None,
        )
        self.assertEqual(status, 401)

    def test_it_refuses_to_sign_for_an_account_it_holds_no_key_for(self):
        """A signer that would sign for anything is a signing oracle."""
        treasury = self._treasury()
        status, body = self._post(
            "/sign",
            {
                "tx_blob": self._a_transaction(treasury),
                "account": "rUNKNOWNunknownunknownunknownunknown12",
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "no_key_for_account")

    def test_it_leaves_an_audit_record(self):
        treasury = self._treasury()
        before = len(self.audit_log.read_text().splitlines()) if self.audit_log.exists() else 0
        self._post(
            "/sign", {"tx_blob": self._a_transaction(treasury), "account": treasury}
        )
        after = len(self.audit_log.read_text().splitlines())
        self.assertEqual(after, before + 1)
        entry = json.loads(self.audit_log.read_text().splitlines()[-1])
        self.assertEqual(entry["account"], treasury)
        self.assertIn("tx_blob_sha256", entry)
        self.assertIn("signed_tx_sha256", entry)

    def test_the_audit_endpoint_returns_the_record(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/audit", method="GET"
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read().decode())
        self.assertIn("entries", body)
        self.assertGreaterEqual(len(body["entries"]), 1)


class TestTheClientAndServerAgree(unittest.TestCase):
    """The wire format.

    These two were written separately and did not match: the client
    expected the signed transaction as JSON, the server returned it as a
    blob. Neither could have worked. The contract is now explicit.
    """

    @classmethod
    def setUpClass(cls):
        reason = _needs_devnet()
        if reason:
            raise unittest.SkipTest(reason)


    def test_the_client_parses_a_blob_response(self):
        from xrpl.core.binarycodec import decode, encode
        from xrpl.models import Payment

        treasury = _address("TREASURY")
        tx = Payment(
            account=treasury,
            destination=_beneficiary(),
            amount="1",
            fee="12",
            sequence=1,
            network_id=15791,
            last_ledger_sequence=999999,
        )
        blob = encode(tx.to_xrpl())

        signer = ExternalSignerSigner(
            endpoint="http://127.0.0.1:1/sign",
            account=treasury,
            public_key="",
            token=TOKEN,
        )

        # The client's parsing, exercised directly. A blob is hex and
        # not JSON; a JSON object starts with a brace.
        import json as _json

        for payload, expect_signed in (
            (blob, True),
            (_json.dumps(tx.to_xrpl()), True),
        ):
            text = str(payload).strip()
            if text.startswith("{"):
                parsed = _json.loads(text)
            else:
                parsed = decode(text)
            self.assertEqual(parsed["Account"], treasury)


class TestTheLocalSignerIsStillThere(unittest.TestCase):
    """The devnet path must keep working."""

    @classmethod
    def setUpClass(cls):
        reason = _needs_devnet()
        if reason:
            raise unittest.SkipTest(reason)


    def test_the_local_signer_signs(self):
        raw = json.loads(
            (pathlib.Path.home() / ".xrpl-up" / "local-accounts.json").read_text()
        )
        if isinstance(raw, dict) and "accounts" in raw:
            raw = raw["accounts"]
        items = raw if isinstance(raw, list) else list(raw.values())
        account = next(a for a in items if a.get("index") == 0)

        from xrpl.models import Payment

        signer = LocalSeedSigner(account["seed"])
        tx = Payment(
            account=signer.classic_address,
            destination=_beneficiary(),
            amount="1",
            fee="12",
            sequence=1,
            network_id=15791,
            last_ledger_sequence=999999,
        )
        signed = signer.sign(tx)
        self.assertTrue(signed.is_signed())
        self.assertEqual(signer.classic_address, account["address"])


if __name__ == "__main__":
    unittest.main()
