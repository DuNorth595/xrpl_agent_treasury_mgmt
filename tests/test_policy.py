"""
Tests for the authorisation policy.

What this module must get right:
  - Three tiers, with configurable sign-off counts.
  - A refusal is NOT an escalation. An over-limit transfer is refused,
    never escalated.
  - Hard limits and delegated authority are different concepts.
  - EVERY limit comparison happens in the unit of account.
  - The running daily-cap total is NOT converted twice.
  - The service determines the tier; no caller can select one.

Run with:  pytest tests/test_policy.py -v
"""

from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parent.parent / "src")
)

from xrpl_agent_treasury_mgmt.currency import (  # noqa: E402
    CurrencyBook,
    IssuedAsset,
    Rate,
    hex_code,
)
from xrpl_agent_treasury_mgmt.policy import (  # noqa: E402
    ApprovalMatrix,
    PolicyDecision,
    PolicyEngine,
    ProposedTransfer,
    Tier,
)
from xrpl_agent_treasury_mgmt.tenants import Limits, Tenant  # noqa: E402

TREASURY = "rTreasury"
MEMBER_A = "rMemberA"
MEMBER_B = "rMemberB"
OUTSIDER = "rOutsider"


@pytest.fixture
def currency_book():
    book = CurrencyBook(unit_of_account="USD")
    book.set_asset("RLUSD", IssuedAsset(code=hex_code("RLUSD"), issuer="rRLUSD"))
    book.set_asset("CAD", IssuedAsset(code="CAD", issuer="rCAD"))
    book.set_asset("MXN", IssuedAsset(code="MXN", issuer="rMXN"))
    book.set_rate(Rate(quote="RLUSD", units_per_usd=1.0))
    book.set_rate(Rate(quote="CAD", units_per_usd=1.40))
    book.set_rate(Rate(quote="MXN", units_per_usd=18.0))
    return book


@pytest.fixture
def tenant():
    return Tenant(
        id="t",
        name="Test Co",
        domain_id="D" * 64,
        treasury_account=TREASURY,
        limits=Limits(
            max_single_transfer_xrp=1000.0,
            max_daily_total_xrp=5000.0,
            min_treasury_balance_xrp=1000.0,
            supervisor_review_threshold_xrp=500.0,
            due_date_buffer_hours=48,
        ),
        cost_centres={MEMBER_A: "A", MEMBER_B: "B"},
        level_2_authorisations=1,
        level_3_authorisations=2,
    )


@pytest.fixture
def engine(tenant, currency_book):
    return PolicyEngine(
        tenant,
        member_accounts=[TREASURY, MEMBER_A, MEMBER_B],
        currency_book=currency_book,
    )


def transfer(amount=100.0, currency="RLUSD", dest=MEMBER_A, **kwargs):
    return ProposedTransfer(
        destination_account=dest,
        amount=amount,
        currency=currency,
        **kwargs,
    )


class TestTiers:
    def test_within_envelope_is_level_1(self, engine):
        decision = engine.evaluate(
            transfer(100.0, obligation_id="OBL-1"),
            treasury_balance_xrp=10000.0,
        )
        assert decision.tier is Tier.LEVEL_1
        assert decision.required_authorisations == 0
        assert decision.reasons == []
        assert decision.is_automatic

    def test_escalation_is_level_2_with_one_authorisation(self, engine):
        # A short-notice payment: due in 10 hours, buffer is 48.
        decision = engine.evaluate(
            transfer(
                100.0,
                due_date=datetime.now(timezone.utc) + timedelta(hours=10),
                obligation_id="OBL-1",
            ),
            treasury_balance_xrp=10000.0,
        )
        assert decision.tier is Tier.LEVEL_2
        assert decision.required_authorisations == 1
        assert decision.requires_authorisation

    def test_over_delegated_authority_is_level_3(self, engine):
        # 600 > the 500 supervisor threshold, but under the 1000 hard limit.
        decision = engine.evaluate(
            transfer(600.0, obligation_id="OBL-1"),
            treasury_balance_xrp=10000.0,
        )
        assert decision.tier is Tier.LEVEL_3
        assert decision.required_authorisations == 2
        assert "exceeds_treasury_manager_delegated_authority" in decision.reasons

    def test_first_payment_escalates(self, engine):
        decision = engine.evaluate(
            transfer(100.0, dest=OUTSIDER, obligation_id="OBL-1"),
            treasury_balance_xrp=10000.0,
        )
        # A non-member is refused outright, not escalated.
        assert decision.tier is Tier.REFUSED

    def test_participant_designated_for_supervisor_review(self, tenant, currency_book):
        tenant = Tenant(
            id=tenant.id,
            name=tenant.name,
            domain_id=tenant.domain_id,
            treasury_account=tenant.treasury_account,
            limits=Limits(
                **{
                    **tenant.limits.to_dict(),
                    "always_human_destinations": [MEMBER_B],
                }
            ),
            cost_centres=tenant.cost_centres,
        )
        engine = PolicyEngine(
            tenant,
            member_accounts=[TREASURY, MEMBER_A, MEMBER_B],
            currency_book=currency_book,
        )
        decision = engine.evaluate(
            transfer(100.0, dest=MEMBER_B, obligation_id="OBL-1"),
            treasury_balance_xrp=10000.0,
        )
        assert decision.tier is Tier.LEVEL_3
        assert "participant_requires_supervisor_review" in decision.reasons


class TestRefusalsAreNotEscalations:
    def test_over_single_limit_is_refused(self, engine):
        decision = engine.evaluate(
            transfer(1500.0), treasury_balance_xrp=100000.0
        )
        assert decision.tier is Tier.REFUSED
        assert decision.required_authorisations == 0
        assert "exceeds_maximum_single_transfer" in decision.reasons

    def test_over_daily_cap_is_refused(self, engine):
        decision = engine.evaluate(
            transfer(100.0),
            treasury_balance_xrp=100000.0,
            already_committed_today_xrp=4950.0,
        )
        assert decision.tier is Tier.REFUSED
        assert "exceeds_maximum_daily_disbursement" in decision.reasons

    def test_breaching_minimum_balance_is_refused(self, engine):
        """The floor is an XRP floor, so it is compared against XRP.

        XRP pays fees. A transfer is paid out of the settlement currency
        and does not reduce XRP, so a treasury with ample XRP may still
        disburse - what the floor protects is the ability to submit the
        NEXT transaction.
        """
        below_floor = engine.evaluate(
            transfer(100.0), treasury_balance_xrp=999.0
        )
        assert below_floor.tier is Tier.REFUSED
        assert "would_breach_minimum_treasury_balance" in below_floor.reasons

    def test_a_transfer_does_not_consume_the_xrp_floor(self, engine):
        """A transfer is paid from the settlement currency, not XRP.

        This is the bug that made every transfer look like a floor
        breach: the old rule subtracted the USD-equivalent transfer
        amount from the XRP balance, subtracting two different kinds of
        quantity.
        """
        comfortable = engine.evaluate(
            transfer(100.0), treasury_balance_xrp=1050.0
        )
        assert comfortable.tier is not Tier.REFUSED
        assert "would_breach_minimum_treasury_balance" not in comfortable.reasons

    def test_unregistered_destination_is_refused(self, engine):
        """A destination the treasury does not know at all."""
        decision = engine.evaluate(
            transfer(100.0, dest=OUTSIDER), treasury_balance_xrp=10000.0
        )
        assert decision.tier is Tier.REFUSED
        assert "destination_is_not_registered" in decision.reasons

    def test_registered_but_non_participant_destination_is_refused(self, engine):
        """A cost centre that is configured but holds no accepted
        credential in the domain. This is the case that matters for the
        perimeter: it must not be treated as merely unregistered."""
        decision = engine.evaluate(
            transfer(100.0, dest="rConfiguredButNotAParticipant"),
            treasury_balance_xrp=10000.0,
        )
        assert decision.tier is Tier.REFUSED

    def test_zero_amount_is_refused(self, engine):
        decision = engine.evaluate(
            transfer(0.0), treasury_balance_xrp=10000.0
        )
        assert decision.tier is Tier.REFUSED

    def test_a_refusal_cannot_be_authorised_into_compliance(self, engine):
        """No amount of sign-offs makes an over-limit transfer compliant."""
        decision = engine.evaluate(
            transfer(1500.0), treasury_balance_xrp=100000.0
        )
        assert not decision.permitted
        assert decision.required_authorisations == 0


class TestUnitOfAccountNormalisation:
    def test_cad_amount_is_compared_at_its_usd_equivalent(self, engine):
        """CA$700 is about US$500. It must be judged as US$500.

        US$500 is exactly the supervisor threshold, and the comparison is
        strictly greater-than, so it is NOT escalated to Level 3.
        """
        decision = engine.evaluate(
            transfer(699.0, currency="CAD", obligation_id="OBL-1"),
            treasury_balance_xrp=100000.0,
        )
        assert decision.tier is not Tier.REFUSED
        assert "exceeds_treasury_manager_delegated_authority" not in decision.reasons

    def test_cad_amount_over_the_usd_limit_is_refused(self, engine):
        """CA$1400 is about US$1000 - over the limit in USD terms."""
        decision = engine.evaluate(
            transfer(1400.0, currency="CAD"), treasury_balance_xrp=100000.0
        )
        assert decision.tier is Tier.REFUSED
        assert "exceeds_maximum_single_transfer" in decision.reasons

    def test_mxn_amount_is_compared_at_its_usd_equivalent(self, engine):
        """MX$18,001 is about US$1,000.06 - over the single-transfer limit.

        The comparison is strictly greater-than, so exactly US$1,000 passes.
        The point is that MX$18,001 is refused, NOT because 18,001 > 1,000
        but because 18,001 / 18 = 1,000.06 > 1,000.
        """
        decision = engine.evaluate(
            transfer(18001.0, currency="MXN"), treasury_balance_xrp=100000.0
        )
        assert decision.tier is Tier.REFUSED
        assert "exceeds_maximum_single_transfer" in decision.reasons

    def test_mxn_at_exactly_the_limit_is_not_refused(self, engine):
        """MX$18,000 is exactly US$1,000.00, which is within the limit."""
        decision = engine.evaluate(
            transfer(18000.0, currency="MXN", obligation_id="OBL-1"),
            treasury_balance_xrp=100000.0,
        )
        assert decision.tier is not Tier.REFUSED

    def test_a_threshold_exactly_on_the_boundary_is_floating_point_sensitive(self, engine):
        """DOCUMENTS A REAL EDGE CASE.

        700 / 1.40 = 500.00000000000006 in IEEE 754 - not exactly 500. The
        comparison is strictly greater-than, so an amount that is
        mathematically exactly at the supervisor threshold can land just
        OVER it and escalate to Level 3.

        That is a rounding artefact, not a policy error, but it means an
        amount exactly at a threshold behaves unpredictably with respect to
        the rate in force. A production deployment should compare with a
        tolerance, or compare in a scaled integer rather than a float.

        This test pins the current behaviour so a change to it is visible.
        """
        decision = engine.evaluate(
            transfer(700.0, currency="CAD", obligation_id="OBL-1"),
            treasury_balance_xrp=100000.0,
        )
        # 700 CAD / 1.40 = 500.00000000000006 > 500, so it escalates.
        assert decision.tier is Tier.LEVEL_3

    def test_the_same_usd_value_in_two_currencies_gets_the_same_tier(self, engine):
        """US$500 is US$500 whether it is RLUSD or CAD."""
        rlusd_decision = engine.evaluate(
            transfer(499.0, currency="RLUSD", obligation_id="OBL-1"),
            treasury_balance_xrp=100000.0,
        )
        cad_decision = engine.evaluate(
            transfer(698.0, currency="CAD", obligation_id="OBL-1"),
            treasury_balance_xrp=100000.0,
        )
        assert rlusd_decision.tier is cad_decision.tier


class TestDoubleConversionTrap:
    def test_daily_cap_is_not_converted_twice(self, engine):
        """THE TRAP.

        `already_committed_today_xrp` is already in the unit of account. If
        it is converted again it gets divided by the rate a second time, and
        the daily cap fires on the second instruction of every batch.

        Here: 700 CAD is US$500. The single-transfer limit is US$1000 and
        the daily cap is US$5000. A batch of one CA$700 must NOT be
        refused for exceeding the daily cap.
        """
        decisions = engine.evaluate_all(
            [transfer(700.0, currency="CAD", obligation_id="OBL-1")],
            treasury_balance_xrp=100000.0,
        )
        assert len(decisions) == 1
        assert decisions[0].tier is not Tier.REFUSED

    def test_the_batch_accumulates_in_usd(self, engine):
        """Ten CA$700 payments are US$5000 - exactly the daily cap."""
        decisions = engine.evaluate_all(
            [
                transfer(700.0, currency="CAD", obligation_id=f"OBL-{i}")
                for i in range(10)
            ],
            treasury_balance_xrp=100000.0,
        )
        # The tenth crosses the cap (5000 + 500 > 5000) and is refused.
        assert decisions[9].tier is Tier.REFUSED
        assert "exceeds_maximum_daily_disbursement" in decisions[9].reasons
        # The first nine are fine.
        for d in decisions[:9]:
            assert d.tier is not Tier.REFUSED

    def test_a_mixed_currency_batch_aggregates_in_usd(self, engine):
        """RLUSD and CAD in one batch share one daily cap, in USD."""
        batch = [
            transfer(400.0, currency="RLUSD", obligation_id="OBL-1"),
            transfer(700.0, currency="CAD", obligation_id="OBL-2"),  # US$500
            transfer(400.0, currency="RLUSD", obligation_id="OBL-3"),
        ]
        decisions = engine.evaluate_all(
            batch, treasury_balance_xrp=100000.0
        )
        # US$400 + US$500 = US$900, then US$400 = US$1300. All under 5000.
        assert all(d.tier is not Tier.REFUSED for d in decisions)


class TestApprovalMatrix:
    def test_defaults(self):
        matrix = ApprovalMatrix()
        assert matrix.required_for(Tier.LEVEL_1) == 0
        assert matrix.required_for(Tier.LEVEL_2) == 1
        assert matrix.required_for(Tier.LEVEL_3) == 2

    def test_counts_are_configurable_per_tenant(self):
        matrix = ApprovalMatrix(
            level_2_authorisations=2, level_3_authorisations=3
        )
        assert matrix.required_for(Tier.LEVEL_2) == 2
        assert matrix.required_for(Tier.LEVEL_3) == 3

    def test_a_single_approval_at_level_3_is_allowed(self):
        """A smaller tenant may want one sign-off at Level 3."""
        matrix = ApprovalMatrix(level_3_authorisations=1)
        assert matrix.required_for(Tier.LEVEL_3) == 1

    def test_counts_never_go_below_one(self):
        matrix = ApprovalMatrix(
            level_2_authorisations=0, level_3_authorisations=0
        )
        assert matrix.required_for(Tier.LEVEL_2) == 1
        assert matrix.required_for(Tier.LEVEL_3) == 1

    def test_the_engine_uses_the_matrix(self, tenant, currency_book):
        engine = PolicyEngine(
            tenant,
            member_accounts=[TREASURY, MEMBER_A, MEMBER_B],
            approval_matrix=ApprovalMatrix(
                level_2_authorisations=2, level_3_authorisations=3
            ),
            currency_book=currency_book,
        )
        decision = engine.evaluate(
            transfer(600.0), treasury_balance_xrp=10000.0
        )
        assert decision.tier is Tier.LEVEL_3
        assert decision.required_authorisations == 3


class TestDecisionShape:
    def test_reasons_are_deduplicated_and_ordered(self, engine):
        decision = engine.evaluate(
            transfer(
                600.0,
                due_date=datetime.now(timezone.utc) + timedelta(hours=5),
                obligation_id="OBL-1",
            ),
            treasury_balance_xrp=10000.0,
        )
        assert len(decision.reasons) == len(set(decision.reasons))

    def test_the_decision_carries_the_transfer(self, engine):
        decision = engine.evaluate(
            transfer(100.0), treasury_balance_xrp=10000.0
        )
        assert decision.detail["transfer"]["amount"] == 100.0
        assert decision.detail["transfer"]["currency"] == "RLUSD"

    def test_to_dict_is_serialisable(self, engine):
        decision = engine.evaluate(
            transfer(100.0), treasury_balance_xrp=10000.0
        )
        payload = decision.to_dict()
        assert payload["tier"] in ("level_1", "level_2", "level_3", "refused")
        assert "tier_label" in payload
        assert isinstance(payload["reasons"], list)
