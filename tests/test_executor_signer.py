"""
Tests for the executor and signer.

The two invariants that matter most:

  1. **Every Payment the executor builds carries `domain_id`, resolved
     from tenant config.** There must be no code path that produces a
     Payment without it. This is what makes the ledger-enforced perimeter
     a structural property rather than a promise.

  2. **The signer will not sign an escalated transfer until the required
     number of authorisations has been recorded.** The policy engine
     decides HOW MANY; the approval ledger records WHO; the signer checks
     that the count is met.

Run with:  pytest tests/test_executor_signer.py -v
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parent.parent / "src")
)

from xrpl_agent_treasury_mgmt.accounts import fmt_xrp  # noqa: E402
from xrpl_agent_treasury_mgmt.currency import (  # noqa: E402
    IssuedAsset,
    hex_code,
)
from xrpl_agent_treasury_mgmt.executor import Executor  # noqa: E402
from xrpl_agent_treasury_mgmt.policy import (  # noqa: E402
    ApprovalMatrix,
    PolicyDecision,
    PolicyEngine,
    ProposedTransfer,
    Tier,
)
from xrpl_agent_treasury_mgmt.signer import (  # noqa: E402
    ApprovalLedger,
    Authorisation,
    TreasurySigner,
)
from xrpl_agent_treasury_mgmt.tenants import Limits, Tenant  # noqa: E402

TREASURY = "rTreasury"
MEMBER = "rMember"
DOMAIN = "A" * 64
RLUSD_HEX = "524C555344000000000000000000000000000000"


class FakeLedger:
    """A ledger stub. No network, no signing."""

    network_id = 15791

    def __init__(self):
        self.simulate_calls = []

    async def balance_xrp(self, address):
        return 50_000.0

    async def issued_balance(self, address, asset):
        return 70_900.0

    async def simulate(self, tx_json):
        self.simulate_calls.append(tx_json)
        return {
            "engine_result": "tesSUCCESS",
            "engine_result_code": 0,
            "engine_result_message": "The transaction was applied.",
            "applied": True,
        }


class RecordingSigner:
    """Records what it was asked to sign."""

    def __init__(self):
        self.signed = []

    @property
    def classic_address(self):
        return TREASURY

    @property
    def public_key(self):
        return "ED" + "0" * 64

    def sign(self, transaction):
        self.signed.append(transaction)
        return transaction


@pytest.fixture
def tenant():
    return Tenant(
        id="t",
        name="Test Co",
        domain_id=DOMAIN,
        treasury_account=TREASURY,
        limits=Limits(),
        cost_centres={MEMBER: "Member"},
    )


@pytest.fixture
def currency_book():
    from xrpl_agent_treasury_mgmt.currency import CurrencyBook, Rate

    book = CurrencyBook(unit_of_account="USD")
    book.set_asset(
        "RLUSD", IssuedAsset(code=RLUSD_HEX, issuer="rRLUSDIssuer")
    )
    book.set_asset("CAD", IssuedAsset(code="CAD", issuer="rCADIssuer"))
    book.set_rate(Rate(quote="RLUSD", units_per_usd=1.0))
    book.set_rate(Rate(quote="CAD", units_per_usd=1.4))
    return book


@pytest.fixture
def executor(tenant, currency_book):
    policy = PolicyEngine(
        tenant, member_accounts=[TREASURY, MEMBER], currency_book=currency_book
    )
    return Executor(
        FakeLedger(),
        TreasurySigner(RecordingSigner(), policy),
        domain_resolver=lambda tenant_id: DOMAIN,
        currency_book=currency_book,
    )


class TestDomainGateIsStructural:
    """The perimeter. These tests exist so it cannot regress."""

    def test_every_built_payment_carries_the_domain_id(self, executor):
        for currency in ("RLUSD", "CAD"):
            built = executor.build(
                "t",
                TREASURY,
                ProposedTransfer(
                    destination_account=MEMBER,
                    amount=100.0,
                    currency=currency,
                ),
                sequence=1,
                last_ledger_sequence=100,
            )
            assert built.tx_json["DomainID"] == DOMAIN

    def test_the_domain_id_comes_from_the_resolver_not_the_transfer(self, executor):
        """A caller cannot smuggle a domain in through the transfer."""
        built = executor.build(
            "t",
            TREASURY,
            ProposedTransfer(
                destination_account=MEMBER,
                amount=100.0,
                currency="RLUSD",
            ),
            sequence=1,
            last_ledger_sequence=100,
        )
        # The transfer carries no domain field at all.
        assert "domain_id" not in built.tx_json
        assert built.tx_json["DomainID"] == DOMAIN

    def test_a_different_tenant_resolves_a_different_domain(self, tenant, currency_book):
        other = Tenant(
            id="other",
            name="Other Co",
            domain_id="B" * 64,
            treasury_account="rOtherTreasury",
            limits=Limits(),
            cost_centres={MEMBER: "Member"},
        )
        policy = PolicyEngine(other, member_accounts=[MEMBER], currency_book=currency_book)
        executor = Executor(
            FakeLedger(),
            TreasurySigner(RecordingSigner(), policy),
            domain_resolver=lambda tenant_id: (
                DOMAIN if tenant_id == "t" else "B" * 64
            ),
            currency_book=currency_book,
        )
        built = executor.build(
            "other",
            "rOtherTreasury",
            ProposedTransfer(destination_account=MEMBER, amount=100.0),
            sequence=1,
            last_ledger_sequence=100,
        )
        assert built.tx_json["DomainID"] == "B" * 64

    def test_a_same_currency_payment_carries_send_max_in_that_currency(self, executor):
        """An issued-currency Payment is ALWAYS a cross-issue payment.

        Sending RLUSD means the source is XRP (the fee) and the issued
        asset is the destination, so the XRPL requires `send_max` even
        though the treasury holds that exact asset. Without it the model
        is rejected with "A currency conversion requires a `send_max`
        value."

        For a same-currency payment send_max is the amount itself, in the
        same asset.
        """
        built = executor.build(
            "t",
            TREASURY,
            ProposedTransfer(
                destination_account=MEMBER, amount=100.0, currency="RLUSD"
            ),
            sequence=1,
            last_ledger_sequence=100,
        )
        assert built.tx_json["SendMax"]["currency"] == RLUSD_HEX
        assert built.tx_json["SendMax"]["value"] == "100000000"
        assert built.tx_json["SendMax"]["issuer"] == "rRLUSDIssuer"

    def test_a_cross_currency_payment_carries_send_max(self, executor):
        """A CAD payment from an RLUSD treasury is a CROSS-CURRENCY payment.

        The XRPL rejects it without send_max:
        "A currency conversion requires a `send_max` value."

        Amount is CAD (what the beneficiary receives); send_max is RLUSD
        (what the treasury actually spends), capped at the USD equivalent.
        """
        built = executor.build(
            "t",
            TREASURY,
            ProposedTransfer(
                destination_account=MEMBER, amount=700.0, currency="CAD"
            ),
            sequence=1,
            last_ledger_sequence=100,
        )
        # Amount is in CAD.
        assert built.tx_json["Amount"]["currency"] == "CAD"
        assert built.tx_json["Amount"]["value"] == "700000000"
        # send_max is in the treasury's settlement currency, RLUSD.
        assert built.tx_json["SendMax"]["currency"] == RLUSD_HEX
        # CA$700 at 1.40 = US$500, so the cap is 500 RLUSD.
        assert built.tx_json["SendMax"]["value"] == "500000000"

    def test_the_domain_id_is_present_on_a_cross_currency_payment(self, executor):
        """The perimeter applies to cross-currency payments too."""
        built = executor.build(
            "t",
            TREASURY,
            ProposedTransfer(
                destination_account=MEMBER, amount=700.0, currency="CAD"
            ),
            sequence=1,
            last_ledger_sequence=100,
        )
        assert built.tx_json["DomainID"] == DOMAIN

    def test_the_payment_carries_the_issued_currency_not_xrp(self, executor):
        built = executor.build(
            "t",
            TREASURY,
            ProposedTransfer(
                destination_account=MEMBER, amount=100.0, currency="RLUSD"
            ),
            sequence=1,
            last_ledger_sequence=100,
        )
        amount = built.tx_json["Amount"]
        assert isinstance(amount, dict)
        assert amount["currency"] == RLUSD_HEX
        assert amount["value"] == "100000000"   # 100 RLUSD in smallest units
        assert amount["issuer"] == "rRLUSDIssuer"


class TestApprovalLedger:
    def test_a_new_ledger_is_not_satisfied(self):
        ledger = ApprovalLedger(required=2)
        assert not ledger.satisfied
        assert ledger.count == 0

    def test_one_of_two_is_not_satisfied(self):
        ledger = ApprovalLedger(required=2)
        ledger.add(Authorisation("A", "treasury_manager", "2026-01-01T00:00:00Z"))
        assert ledger.count == 1
        assert not ledger.satisfied

    def test_two_of_two_is_satisfied(self):
        ledger = ApprovalLedger(required=2)
        ledger.add(Authorisation("A", "treasury_manager", "2026-01-01T00:00:00Z"))
        ledger.add(Authorisation("M", "supervisor", "2026-01-01T00:01:00Z"))
        assert ledger.count == 2
        assert ledger.satisfied

    def test_the_role_is_recorded(self):
        ledger = ApprovalLedger(required=2)
        ledger.add(Authorisation("M", "supervisor", "2026-01-01T00:01:00Z"))
        assert ledger.received[0].role == "supervisor"


class TestSignerGate:
    def _decision(self, tier, required):
        return PolicyDecision(
            tier=tier,
            reasons=[],
            required_authorisations=required,
            detail={},
        )

    def test_level_1_signs_without_authorisation(self, tenant, currency_book):
        policy = PolicyEngine(tenant, member_accounts=[MEMBER], currency_book=currency_book)
        recorder = RecordingSigner()
        signer = TreasurySigner(recorder, policy)
        outcome = signer.sign({}, self._decision(Tier.LEVEL_1, 0), obligation_id="OBL-1")
        assert outcome.status == "signed"
        assert len(recorder.signed) == 1

    def test_level_2_withholds_until_one_approval(self, tenant, currency_book):
        policy = PolicyEngine(tenant, member_accounts=[MEMBER], currency_book=currency_book)
        recorder = RecordingSigner()
        signer = TreasurySigner(recorder, policy)

        outcome = signer.sign({}, self._decision(Tier.LEVEL_2, 1), obligation_id="OBL-1")
        assert outcome.status == "awaiting_authorisation"
        assert outcome.received_authorisations == 0
        assert len(recorder.signed) == 0

        signer.record_authorisation("OBL-1", "A. Reyes", "treasury_manager")
        outcome = signer.sign({}, self._decision(Tier.LEVEL_2, 1), obligation_id="OBL-1")
        assert outcome.status == "signed"
        assert len(recorder.signed) == 1

    def test_level_3_withholds_until_two_approvals(self, tenant, currency_book):
        policy = PolicyEngine(tenant, member_accounts=[MEMBER], currency_book=currency_book)
        recorder = RecordingSigner()
        signer = TreasurySigner(recorder, policy)

        signer.record_authorisation("OBL-1", "A. Reyes", "treasury_manager")
        outcome = signer.sign({}, self._decision(Tier.LEVEL_3, 2), obligation_id="OBL-1")
        assert outcome.status == "awaiting_authorisation"
        assert outcome.received_authorisations == 1
        assert len(recorder.signed) == 0

        signer.record_authorisation("OBL-1", "M. Okonkwo", "supervisor")
        outcome = signer.sign({}, self._decision(Tier.LEVEL_3, 2), obligation_id="OBL-1")
        assert outcome.status == "signed"
        assert outcome.received_authorisations == 2
        assert len(recorder.signed) == 1

    def test_the_signer_never_signs_a_refusal(self, tenant, currency_book):
        policy = PolicyEngine(tenant, member_accounts=[MEMBER], currency_book=currency_book)
        recorder = RecordingSigner()
        signer = TreasurySigner(recorder, policy)
        outcome = signer.sign({}, self._decision(Tier.REFUSED, 0), obligation_id="OBL-1")
        assert outcome.status == "refused"
        assert len(recorder.signed) == 0

    def test_approvals_do_not_leak_between_transfers(self, tenant, currency_book):
        """Signing off on one transfer must not authorise another."""
        policy = PolicyEngine(tenant, member_accounts=[MEMBER], currency_book=currency_book)
        recorder = RecordingSigner()
        signer = TreasurySigner(recorder, policy)

        # Sign off on OBL-1 only.
        signer.record_authorisation("OBL-1", "A. Reyes", "treasury_manager")
        # OBL-2 has no sign-offs of its own, so it must not be signed.
        outcome = signer.sign({}, self._decision(Tier.LEVEL_2, 1), obligation_id="OBL-2")
        assert outcome.status == "awaiting_authorisation"
        assert outcome.received_authorisations == 0
        assert len(recorder.signed) == 0

        # OBL-1, which was signed off, is signed.
        outcome = signer.sign({}, self._decision(Tier.LEVEL_2, 1), obligation_id="OBL-1")
        assert outcome.status == "signed"

    def test_the_received_count_is_reported_when_no_provider_is_configured(self, tenant, currency_book):
        """REGRESSION.

        When the signer has no signing provider, the outcome used to report
        `received_authorisations: 0` even when two sign-offs had been
        recorded against the transfer. An operator would see "0/2" on an
        instruction that had both approvals, and would go looking for a
        missing sign-off that was actually there.

        The count must reflect reality even when the sign itself is
        withheld.
        """
        policy = PolicyEngine(tenant, member_accounts=[MEMBER], currency_book=currency_book)
        signer = TreasurySigner(None, policy)  # no provider

        signer.record_authorisation(
            "OBL-1", "A. Reyes", "treasury_manager", required=2
        )
        signer.record_authorisation(
            "OBL-1", "M. Okonkwo", "supervisor", required=2
        )
        decision = self._decision(Tier.LEVEL_3, 2)
        outcome = signer.sign({}, decision, obligation_id="OBL-1")
        assert outcome.received_authorisations == 2
        assert outcome.required_authorisations == 2

    def test_the_ledger_knows_its_target_from_the_first_sign_off(self, tenant, currency_book):
        """The required count is supplied when the sign-off is recorded, so
        `satisfied` is meaningful at every point rather than only after a
        sign attempt."""
        policy = PolicyEngine(tenant, member_accounts=[MEMBER], currency_book=currency_book)
        signer = TreasurySigner(None, policy)
        ledger = signer.record_authorisation(
            "OBL-1", "A. Reyes", "treasury_manager", required=2
        )
        assert ledger.required == 2
        assert ledger.count == 1
        assert not ledger.satisfied

    def test_a_transfer_with_no_approval_key_is_refused(self, tenant, currency_book):
        """A transfer with neither an obligation id nor a destination has
        no key.

        Every such transfer would land in the same shared approval bucket,
        so one sign-off could authorise an unrelated transfer. That is a
        fund-movement bug, so the signer refuses instead.
        """
        policy = PolicyEngine(tenant, member_accounts=[MEMBER], currency_book=currency_book)
        recorder = RecordingSigner()
        signer = TreasurySigner(recorder, policy)
        outcome = signer.sign({}, self._decision(Tier.LEVEL_1, 0))
        assert outcome.status == "refused"
        assert "transfer_has_no_approval_key" in outcome.reasons
        assert len(recorder.signed) == 0

    def test_a_single_approval_at_level_3_can_be_configured(self, tenant, currency_book):
        policy = PolicyEngine(
            tenant,
            member_accounts=[MEMBER],
            approval_matrix=ApprovalMatrix(level_3_authorisations=1),
            currency_book=currency_book,
        )
        recorder = RecordingSigner()
        signer = TreasurySigner(recorder, policy)
        signer.record_authorisation("OBL-1", "A. Reyes", "treasury_manager")
        outcome = signer.sign({}, self._decision(Tier.LEVEL_3, 1), obligation_id="OBL-1")
        assert outcome.status == "signed"


class TestSecretHygiene:
    def test_a_credential_never_prints_its_secret(self):
        from xrpl_agent_treasury_mgmt.connectors import Credential

        cred = Credential(key="ERP", secret="super-secret-value", account_id="a")
        assert "super-secret-value" not in repr(cred)
        assert "super-secret-value" not in str(cred)
        assert "ERP" in repr(cred)

    def test_a_missing_env_credential_names_the_variable_not_the_value(self):
        import os

        from xrpl_agent_treasury_mgmt.connectors import (
            ConnectorError,
            credential_from_env,
        )

        os.environ.pop("TOTALLY_ABSENT_KEY", None)
        with pytest.raises(ConnectorError) as exc:
            credential_from_env("TOTALLY_ABSENT_KEY")
        assert "TOTALLY_ABSENT_KEY" in str(exc.value)
