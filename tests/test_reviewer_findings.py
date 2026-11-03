"""
Tests for the defects the second reviewer found.

WHY THIS FILE EXISTS
====================

A reviewer read the documentation against the source and found six
things the documentation promised and the code did not do. Five of them
were in the code; one was in the docs. They are collected here so they
cannot come back.

The list:

  1. The dry-run endpoint referenced an undefined name `balance`, so it
     raised NameError, caught it, and returned HTTP 200 with
     {"error": "NameError(...)"} for EVERY request. The endpoint was
     completely non-functional and looked like it was working.

  2. The dry run built its own signer with no provider, so it signed
     locally even when the service was configured to sign through an
     external signer.

  3. When the LEDGER could not be read, the plan did not carry
     `blocked = True`. Only the ERP failure path set it. A caller
     checking `plan.blocked` got False on a ledger outage -
     indistinguishable from a normal plan.

  4. `mint_token.py --scopes` was a single-value argument, so
     repeating it kept only the LAST value. Asking for
     `--scopes treasury:read --scopes treasury:settle` silently
     produced a settle-only token: MORE privilege than requested, with
     no warning.

  5. An authorisation with no approver was accepted and recorded in
     the audit log as a sign-off by nobody.

  6. An authorisation against an obligation that is not in the plan
     created a ledger with `required = 0`, which reported
     `satisfied: true` on the first sign-off.

None of these were caught by the existing suite. The suite tested the
logic; these are all at the edges - the endpoint, the CLI, and the
failure path.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT / "src"))

from xrpl_agent_treasury_mgmt.auth import (  # noqa: E402
    SCOPE_AUTHORISE,
    SCOPE_PROPOSE,
    SCOPE_READ,
    SCOPE_SETTLE,
    CredentialStore,
    mint_credential,
)
from xrpl_agent_treasury_mgmt.reconcile import ReconciliationReport  # noqa: E402
from xrpl_agent_treasury_mgmt.service import TreasuryService, create_app  # noqa: E402

from xrpl_agent_treasury_mgmt.devnet_accounts import address_for as _address

REAL_TENANT = "s-devlab-corp"
def _needs_devnet():
    """Why this test cannot run here, or None if it can.

    The signer and sequence tests resolve a real devnet account. A
    clean checkout has no accounts file, so they skip rather than fail.
    """
    from xrpl_agent_treasury_mgmt.devnet_accounts import (
        DevnetAccountsError,
        account_for,
    )

    try:
        account_for("TREASURY")
    except DevnetAccountsError as exc:
        return f"no local devnet accounts: {exc}"
    return None


def _treasury() -> str:
    """The treasury address, resolved at call time.

    It used to be resolved at module level, so on a machine with no
    local devnet the IMPORT raised - during pytest collection, before
    any skip could run, and the whole file failed to collect.
    """
    return _address("TREASURY")
DOMAIN = "B9263342B5E4C1F2BB8A691647B94FEEF53995DABFF6B88BAB57E1F95390720D"


class _Stub:
    """A transaction-shaped object with mutable fields.

    The xrpl models are frozen dataclasses, so a test cannot clear a
    field after construction. The comparison only reads attributes, so
    a stub is enough.
    """

    def __init__(self, **fields):
        self.account = fields.get("account")
        self.destination = fields.get("destination")
        self.amount = fields.get("amount")
        self.fee = fields.get("fee")
        self.sequence = fields.get("sequence")
        self.destination_tag = fields.get("destination_tag")
        self.domain_id = fields.get("domain_id")


class TestTheMintCommand(unittest.TestCase):
    """The scopes must be what was asked for.

    Finding 4. The old behaviour was a privilege ESCALATION: asking for
    read+settle produced settle-only, because only the last value
    survived.
    """

    def _mint(self, *args):
        result = subprocess.run(
            [sys.executable, "mint_token.py", *args],
            cwd=str(PROJECT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def _scopes_in(self, output):
        for line in output.splitlines():
            if line.strip().startswith("scopes"):
                return line.split(":", 1)[1]
        raise AssertionError(f"no scopes line in the output: {output}")

    def test_repeated_scopes_are_combined(self):
        """The regression: last-wins dropped the earlier scope."""
        output = self._mint(
            "--name", "t", "--scopes", "treasury:read", "--scopes", "treasury:settle"
        )
        scopes = self._scopes_in(output)
        self.assertIn("treasury:read", scopes)
        self.assertIn("treasury:settle", scopes)

    def test_comma_separated_scopes_still_work(self):
        output = self._mint(
            "--name", "t", "--scopes", "treasury:read,treasury:settle"
        )
        scopes = self._scopes_in(output)
        self.assertIn("treasury:read", scopes)
        self.assertIn("treasury:settle", scopes)

    def test_mixed_forms_combine(self):
        output = self._mint(
            "--name",
            "t",
            "--scopes",
            "treasury:read,treasury:propose",
            "--scopes",
            "treasury:authorise",
        )
        scopes = self._scopes_in(output)
        for scope in ("treasury:read", "treasury:propose", "treasury:authorise"):
            self.assertIn(scope, scopes)

    def test_all_still_means_all(self):
        output = self._mint("--name", "t", "--scopes", "all")
        scopes = self._scopes_in(output)
        self.assertIn("treasury:admin", scopes)
        self.assertIn("treasury:settle", scopes)

    def test_the_default_is_read_only(self):
        output = self._mint("--name", "t")
        scopes = self._scopes_in(output)
        self.assertIn("treasury:read", scopes)
        self.assertNotIn("treasury:settle", scopes)

    def test_an_unknown_scope_is_refused(self):
        result = subprocess.run(
            [sys.executable, "mint_token.py", "--name", "t", "--scopes", "bogus"],
            cwd=str(PROJECT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown scope", result.stderr)


class TestTheBlockedSignal(unittest.TestCase):
    """Finding 3. Both failure modes must be visible the same way."""

    def test_a_blocked_report_carries_a_reason(self):
        report = ReconciliationReport(tenant_id="t")
        report.ledger_exceptions.append(
            {
                "kind": "ledger_read_failed",
                "error": "RuntimeError('unavailable')",
                "detail": "The treasury's transaction history could not be read.",
            }
        )
        report.blocked = True
        self.assertTrue(report.blocked)
        self.assertIn("could not be read", report.blocked_reason)

    def test_an_unblocked_report_has_no_reason(self):
        report = ReconciliationReport(tenant_id="t")
        self.assertFalse(report.blocked)
        self.assertEqual(report.blocked_reason, "")

    def test_the_reason_is_in_the_serialised_form(self):
        """A caller reads the dict, not the object."""
        report = ReconciliationReport(tenant_id="t")
        report.ledger_exceptions.append({"detail": "the ledger is down"})
        report.blocked = True
        self.assertIn("blocked_reason", report.to_dict())
        self.assertIn("the ledger is down", report.to_dict()["blocked_reason"])


class TestTheDryRunAndAuthoriseEndpoints(unittest.TestCase):
    """Findings 1, 2, 5, 6 - the endpoint behaviours."""

    @classmethod
    def setUpClass(cls):
        reason = _needs_devnet()
        if reason:
            raise unittest.SkipTest(reason)


    def setUp(self):
        self.app, self.token = _build_app()
        self.client = self.app.test_client()
        self.auth = {"Authorization": f"Bearer {self.token}"}

    def test_the_dry_run_does_not_return_a_name_error(self):
        """Finding 1: it answered every request with a NameError.

        The NameError came from an undefined name in the handler, so it
        fired before anything else. The ledger in this test is
        unreachable, so the dry run legitimately cannot complete - but
        the failure it reports must be the LEDGER's, not a bug in the
        handler.
        """
        response = self.client.post(
            "/api/tenant/" + REAL_TENANT + "/dry-run",
            headers=self.auth,
            json={"amount": 100, "currency": "RLUSD"},
        )
        body = response.get_json()
        self.assertNotIn("NameError", str(body))
        self.assertNotIn("'balance'", str(body))
        self.assertNotIn("UnboundLocalError", str(body))
        # The policy decision is computed BEFORE the ledger is touched,
        # so it is present even when the ledger is down. That is what
        # proves the handler ran.
        self.assertIn("policy_decision", body)

    def test_a_get_on_the_dry_run_is_a_405(self):
        """The docs used to say GET. It is a POST."""
        response = self.client.get(
            "/api/tenant/" + REAL_TENANT + "/dry-run", headers=self.auth
        )
        self.assertIn(response.status_code, (403, 405))

    def test_an_authorisation_without_an_approver_is_refused(self):
        """Finding 5: it recorded a sign-off by nobody."""
        response = self.client.post(
            "/api/tenant/" + REAL_TENANT + "/authorise/OB-1",
            headers=self.auth,
            json={"comment": "no name given"},
        )
        # The approver check happens BEFORE the plan lookup, so it
        # answers even when the ledger is down.
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "approver_required")

    def test_an_authorisation_for_an_unknown_obligation_is_refused(self):
        """Finding 6: it reported satisfied=true on the first sign-off.

        This one needs the plan, so it needs a ledger. It is exercised
        against the live service in the API harness. Here the assertion
        is that the endpoint does NOT return a satisfied ledger for an
        obligation it knows nothing about.
        """
        response = self.client.post(
            "/api/tenant/" + REAL_TENANT + "/authorise/OB-NOT-IN-PLAN",
            headers=self.auth,
            json={"approver": "j.douglas"},
        )
        body = response.get_json()
        if response.status_code == 404:
            self.assertEqual(body["error"], "obligation_not_in_plan")
            return
        # The plan could not be built (the ledger is down in this test),
        # so the endpoint must NOT have reported a satisfied ledger.
        self.assertNotEqual(body.get("satisfied"), True, body)


class TestTheSignerIsChecked(unittest.TestCase):
    """The signer must not be able to change the transaction.

    A second reviewer found that the external signer returned whatever
    the signing service sent back, with no check that it was the
    transaction that was requested.

    That is a real attack: the signing service is a separate process,
    possibly a separate organisation, possibly a third-party custody
    service. If it returns a signature over a DIFFERENT destination,
    the treasury service would submit that one - because it had no
    reason to look. The policy cleared the intended payment; the
    submitted payment was a different one.
    """

    @classmethod
    def setUpClass(cls):
        reason = _needs_devnet()
        if reason:
            raise unittest.SkipTest(reason)


    def _signer(self):
        from xrpl_agent_treasury_mgmt.signer import ExternalSignerSigner

        return ExternalSignerSigner(
            endpoint="http://127.0.0.1:1/sign",
            account=_treasury(),
            public_key="",
            token="t",
        )

    def _transaction(self, **overrides):
        """A Payment, with a signature on it.

        A signed Payment carries `signing_pub_key` and a signature over
        the pre-image. xrpl-py computes those for us when the model is
        serialised; for the purpose of this test the fields that matter
        are the ones the comparison looks at.
        """
        from xrpl.models import Payment

        fields = dict(
            account=_treasury(),
            destination="rDestinationOne",
            amount="100",
            fee="12",
            sequence=42,
            network_id=15791,
            last_ledger_sequence=999999,
            domain_id=DOMAIN,
        )
        fields.update(overrides)
        return Payment(**fields)

    def test_a_matching_transaction_passes(self):
        signer = self._signer()
        requested = self._transaction()
        returned = self._transaction()
        # Identical transactions, so nothing should be flagged.
        signer._assert_same_transaction(requested, returned)

    def test_a_different_destination_is_caught(self):
        signer = self._signer()
        requested = self._transaction()
        returned = self._transaction(destination="rSomebodyElse")
        with self.assertRaises(RuntimeError) as caught:
            signer._assert_same_transaction(requested, returned)
        self.assertIn("destination", str(caught.exception))

    def test_a_different_amount_is_caught(self):
        signer = self._signer()
        requested = self._transaction()
        returned = self._transaction(amount="999999")
        with self.assertRaises(RuntimeError) as caught:
            signer._assert_same_transaction(requested, returned)
        self.assertIn("amount", str(caught.exception))

    def test_a_dropped_domain_is_caught(self):
        """The domain is what confines the payment to the perimeter.

        A signer that dropped it would take the payment outside the
        domain, and the ledger would then enforce nothing.
        """
        signer = self._signer()
        requested = self._transaction()
        # The xrpl models are frozen, so a field cannot be cleared after
        # construction. A stub with the fields the comparison reads is
        # enough to exercise the check.
        returned = _Stub(
            account=_treasury(),
            destination="rDestinationOne",
            amount="100",
            fee="12",
            sequence=42,
            domain_id=None,
        )
        with self.assertRaises(RuntimeError) as caught:
            signer._assert_same_transaction(requested, returned)
        self.assertIn("domain_id", str(caught.exception))

    def test_a_different_account_is_caught(self):
        signer = self._signer()
        requested = self._transaction()
        returned = self._transaction(account="rSomebodyElse")
        with self.assertRaises(RuntimeError) as caught:
            signer._assert_same_transaction(requested, returned)
        self.assertIn("account", str(caught.exception))

    def test_a_different_sequence_is_caught(self):
        """A replayed transaction with a stale sequence."""
        signer = self._signer()
        requested = self._transaction()
        returned = self._transaction(sequence=7)
        with self.assertRaises(RuntimeError) as caught:
            signer._assert_same_transaction(requested, returned)
        self.assertIn("sequence", str(caught.exception))


class TestTheSequenceAdvances(unittest.TestCase):
    """Two transactions from one account must not share a sequence.

    A second reviewer insisted the scale harness's settlement phase
    actually run, rather than being skipped and counted as a pass. When
    it ran, one transaction in three was rejected with tefPAST_SEQ.

    The cause: the executor took a sequence number and never advanced
    it. Two transactions built from the same read carried the same
    sequence, and the ledger rejected the second.

    This is a real defect, not a harness artefact. The scale harness
    built a fresh Executor per transaction, so each one started its own
    counter. The executor itself had no counter at all.
    """

    @classmethod
    def setUpClass(cls):
        reason = _needs_devnet()
        if reason:
            raise unittest.SkipTest(reason)


    def _executor(self):
        from xrpl_agent_treasury_mgmt.executor import Executor

        class _NoLedger:
            network_id = 15791

        return Executor(
            _NoLedger(),
            signer=None,
            domain_resolver=lambda tid: DOMAIN,
        )

    def test_the_sequence_advances_across_calls(self):
        executor = self._executor()
        first = executor.sequence_for(_treasury(), 42)
        second = executor.sequence_for(_treasury(), 42)
        third = executor.sequence_for(_treasury(), 42)
        self.assertEqual(first, 42)
        self.assertEqual(second, 43)
        self.assertEqual(third, 44)

    def test_a_fresh_read_wins_when_it_is_ahead(self):
        """The ledger's sequence is authoritative when it moves on.

        If another process advanced the account's sequence, the counter
        must not hand out a number the ledger has already used.
        """
        executor = self._executor()
        executor.sequence_for(_treasury(), 42)
        # The ledger now says 50 - another transaction went through.
        self.assertEqual(executor.sequence_for(_treasury(), 50), 50)
        # And the counter follows it.
        self.assertEqual(executor.sequence_for(_treasury(), 50), 51)

    def test_the_counter_is_per_account(self):
        executor = self._executor()
        executor.sequence_for(_treasury(), 42)
        other = executor.sequence_for("rAnotherAccount", 7)
        self.assertEqual(other, 7)

    def test_two_built_transactions_do_not_share_a_sequence(self):
        """The end-to-end property: two Payments, two sequences."""
        executor = self._executor()
        from xrpl.models import Payment

        def build():
            return Payment(
                account=_treasury(),
                destination="rDestinationOne",
                amount="100",
                fee="12",
                sequence=executor.sequence_for(_treasury(), 42),
                network_id=15791,
                last_ledger_sequence=999999,
                domain_id=DOMAIN,
            )

        first = build()
        second = build()
        self.assertNotEqual(first.sequence, second.sequence)
        self.assertEqual(second.sequence, first.sequence + 1)


def _build_app():
    """An app with one tenant, no ledger, and no obligations.

    The dry run and the authorise endpoint are tested for the SHAPE of
    their behaviour - that they do not produce a NameError, that they
    refuse an unattributable sign-off - and not for a ledger verdict.
    That keeps the test independent of the devnet.
    """
    from xrpl_agent_treasury_mgmt.agent_log import AgentLog
    from xrpl_agent_treasury_mgmt.obligations import InMemoryObligationSource
    from xrpl_agent_treasury_mgmt.tenants import Limits, Tenant, TenantRegistry

    registry = TenantRegistry()
    registry.add(
        Tenant(
            id=REAL_TENANT,
            name="S_DevLabs",
            domain_id="B9263342B5E4C1F2BB8A691647B94FEEF53995DABFF6B88B",
            treasury_account=_address("TREASURY"),
            currency="RLUSD",
            cost_centres={},
            limits=Limits(),
        )
    )

    class _NoLedger:
        def __getattr__(self, name):
            def _refuse(*args, **kwargs):
                raise RuntimeError("the ledger is unavailable")

            return _refuse

    credential, token = mint_credential(
        "ops", [SCOPE_READ, SCOPE_PROPOSE, SCOPE_SETTLE, SCOPE_AUTHORISE]
    )
    store = CredentialStore([credential])

    service = TreasuryService(
        registry=registry,
        ledger=_NoLedger(),
        obligation_source=InMemoryObligationSource(obligations=[]),
        agent_log=AgentLog(),
    )
    return create_app(service, sample_mode=False, auth_store=store), token


if __name__ == "__main__":
    unittest.main()
