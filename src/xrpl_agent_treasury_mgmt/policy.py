"""
The policy layer: authorisation tiers, limits, and the authorisation decision.

This component determines whether a proposed transfer may be executed
automatically, requires human authorisation, or must be refused outright.

AUTHORISATION TIERS
===================

    Level 1  Within delegated authority       Executed automatically
    Level 2  Requires single authorisation    One human sign-off
    Level 3  Requires dual authorisation     Two human sign-offs

Level 3 exists because a transfer can satisfy every automated control and
still warrant additional scrutiny — for example, a first payment to a
counterparty, or an amount exceeding the treasury manager's own delegated
authority. The second sign-off may be provided by a supervisor.

The number of required authorisations is configurable per tenant and per
tier (see `ApprovalMatrix`), so a tenant may require dual
authorisation at Level 3 while a smaller organisation requires only a single
sign-off, or may require dual authorisation at Level 2 as well.

The service determines the applicable tier. A caller cannot request a tier,
and cannot influence how many authorisations it requires.

REFUSAL VS ESCALATION
=====================

A transfer that exceeds a hard limit is REFUSED, not escalated. No amount of
human authorisation makes an over-limit transfer compliant. Refusals are
distinct from the tier progression and are reported separately.

A transfer outside the PermissionedDomain is likewise refused. The ledger
would reject it with tecNO_PERMISSION; surfacing it here makes it visible to
the operator rather than appearing later as a failed transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Sequence

from .tenants import Tenant


class Tier(str, Enum):
    """Authorisation tier for a proposed transfer."""

    LEVEL_1 = "level_1"
    LEVEL_2 = "level_2"
    LEVEL_3 = "level_3"
    REFUSED = "refused"

    @property
    def label(self) -> str:
        return {
            Tier.LEVEL_1: "Level 1 — Automatic",
            Tier.LEVEL_2: "Level 2 — Single Authorisation",
            Tier.LEVEL_3: "Level 3 — Dual Authorisation",
            Tier.REFUSED: "Refused",
        }[self]


class RefusalReason(str, Enum):
    """Reasons a transfer is refused rather than escalated."""

    DESTINATION_NOT_MEMBER = "destination_is_not_an_authorised_participant"
    BELOW_MIN_BALANCE = "would_breach_minimum_treasury_balance"
    OVER_SINGLE_LIMIT = "exceeds_maximum_single_transfer"
    OVER_DAILY_CAP = "exceeds_maximum_daily_disbursement"
    UNKNOWN_DESTINATION = "destination_is_not_registered"
    ZERO_AMOUNT = "amount_must_be_positive"


class EscalationReason(str, Enum):
    """Reasons a transfer is escalated for human authorisation."""

    NEW_DESTINATION = "first_payment_to_this_participant"
    ALWAYS_HUMAN = "participant_requires_supervisor_review"
    OVER_DELEGATED_AUTHORITY = "exceeds_treasury_manager_delegated_authority"
    SHORT_NOTICE = "payment_is_inside_the_due_date_buffer"
    NO_OBLIGATION = "no_linked_obligation_record"


@dataclass
class PolicyDecision:
    """The authorisation decision for one proposed transfer."""

    tier: Tier
    reasons: List[str]
    required_authorisations: int = 0
    detail: Dict = field(default_factory=dict)

    @property
    def permitted(self) -> bool:
        """Whether the transfer may proceed (automatically or with sign-off)."""
        return self.tier in (Tier.LEVEL_1, Tier.LEVEL_2, Tier.LEVEL_3)

    @property
    def requires_authorisation(self) -> bool:
        return self.tier in (Tier.LEVEL_2, Tier.LEVEL_3)

    @property
    def is_automatic(self) -> bool:
        return self.tier is Tier.LEVEL_1

    def to_dict(self) -> Dict:
        return {
            "tier": self.tier.value,
            "tier_label": self.tier.label,
            "reasons": list(self.reasons),
            "required_authorisations": self.required_authorisations,
            "detail": self.detail or {},
        }


@dataclass
class ProposedTransfer:
    """One proposed movement of treasury funds.

    `destination_account` is the ledger-side account. `entity` is the
    business entity that account serves — they are not the same thing,
    because a cost centre may be an off-ledger entity.
    """

    destination_account: str
    amount: float
    currency: str = "RLUSD"
    due_date: Optional[datetime] = None
    entity: str = ""
    obligation_id: str = ""
    reference: str = ""

    @property
    def amount_xrp(self) -> float:
        """Backwards-compatible alias. Amounts are now denominated in the
        own currency of the instruction, not in XRP."""
        return self.amount

    def to_dict(self) -> Dict:
        return {
            "destination_account": self.destination_account,
            "amount": self.amount,
            "currency": self.currency,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "entity": self.entity,
            "obligation_id": self.obligation_id,
            "reference": self.reference,
        }


@dataclass
class ApprovalMatrix:
    """The authorisation requirement for each tier.

    `level_2_authorisations` and `level_3_authorisations` are configurable.
    Default: one sign-off at Level 2, two at Level 3.
    """

    level_2_authorisations: int = 1
    level_3_authorisations: int = 2

    def required_for(self, tier: Tier) -> int:
        if tier is Tier.LEVEL_1:
            return 0
        if tier is Tier.LEVEL_2:
            return max(1, self.level_2_authorisations)
        if tier is Tier.LEVEL_3:
            return max(1, self.level_3_authorisations)
        return 0

    def to_dict(self) -> Dict:
        return {
            "level_2_authorisations": self.level_2_authorisations,
            "level_3_authorisations": self.level_3_authorisations,
        }


class PolicyEngine:
    """Evaluates proposed transfers against a tenant's authorisation policy.

    The engine holds no key. It produces a decision; the signer consumes it.
    That separation is what makes the agent safe to point at real funds: the
    agent process cannot move funds without the policy engine's evaluation.
    """

    def __init__(
        self,
        tenant: Tenant,
        member_accounts: Optional[Sequence[str]] = None,
        approval_matrix: Optional[ApprovalMatrix] = None,
        currency_book=None,
    ):
        self.tenant = tenant
        self.approval_matrix = approval_matrix or ApprovalMatrix()
        # The limits are denominated in the unit of account (USD). A
        # disbursement in CAD or MXN is compared against them at its USD
        # equivalent, so the limit means the same thing in every currency.
        self.currency_book = currency_book
        # Accounts that hold an accepted credential in the tenant's domain.
        # Derived from the ledger, not from configuration, when available.
        self._members = set(member_accounts or [])

    def set_members(self, member_accounts: Sequence[str]) -> None:
        """Refresh the set of domain participants from the ledger."""
        self._members = set(member_accounts)

    def in_unit_of_account(self, amount: float, currency: str) -> float:
        """The amount expressed in the unit of account.

        Falls back to the amount itself when no rate is known, which is the
        conservative choice: it keeps the limit meaningful rather than
        silently passing an unbounded value through.
        """
        if self.currency_book is None:
            return amount
        usd = self.currency_book.to_usd(amount, currency)
        if usd is None:
            return amount
        return usd

    def is_member(self, account: str) -> bool:
        if account == self.tenant.treasury_account:
            return True  # the domain owner is always a participant
        if self._members:
            return account in self._members
        # No ledger data yet: fall back to the configured registry.
        return account in self.tenant.cost_centres

    def evaluate(
        self,
        transfer: ProposedTransfer,
        treasury_balance_xrp: float,
        already_committed_today_xrp: float = 0.0,
        now: Optional[datetime] = None,
        is_first_payment: bool = False,
    ) -> PolicyDecision:
        """Determine the authorisation tier for one transfer."""
        now = now or datetime.now(timezone.utc)
        limits = self.tenant.limits
        reasons: List[str] = []

        if transfer.amount_xrp <= 0:
            return self._decide(
                Tier.REFUSED,
                [RefusalReason.ZERO_AMOUNT.value],
                transfer,
            )

        # A destination outside the domain is refused. The ledger would reject
        # it too; surfacing it here makes it visible to the operator rather
        # than appearing later as a failed transaction.
        if not self.is_member(transfer.destination_account):
            reason = (
                RefusalReason.UNKNOWN_DESTINATION.value
                if transfer.destination_account not in self.tenant.cost_centres
                else RefusalReason.DESTINATION_NOT_MEMBER.value
            )
            return self._decide(Tier.REFUSED, [reason], transfer)

        # Hard limits are refusals, not escalations. No amount of authorisation
        # makes an over-limit transfer compliant. The comparison is made in
        # the unit of account, so the limit means the same thing whether the
        # disbursement is in RLUSD, CAD or MXN.
        amount_usd = self.in_unit_of_account(transfer.amount, transfer.currency)
        # `already_committed_today_xrp` is already expressed in the unit of
        # account, so it is NOT converted again.
        committed_usd = already_committed_today_xrp

        if amount_usd > limits.max_single_transfer_xrp:
            reasons.append(RefusalReason.OVER_SINGLE_LIMIT.value)
        if committed_usd + amount_usd > limits.max_daily_total_xrp:
            reasons.append(RefusalReason.OVER_DAILY_CAP.value)
        # The floor is an XRP floor, so it is compared against XRP. XRP
        # pays FEES; the transfer itself is paid out of the settlement
        # currency and does not reduce XRP at all. What the floor
        # protects is the treasury's ability to submit the NEXT
        # transaction, which costs a fee in XRP.
        #
        # This used to subtract the USD-equivalent transfer amount from
        # the XRP balance, which subtracted two different kinds of
        # quantity. On a treasury holding plenty of XRP it refused
        # every transfer as `would_breach_minimum_treasury_balance`.
        if treasury_balance_xrp < limits.min_treasury_balance_xrp:
            reasons.append(RefusalReason.BELOW_MIN_BALANCE.value)
        if reasons:
            return self._decide(Tier.REFUSED, reasons, transfer)

        # The transfer is within the policy envelope. The remaining question
        # is how much human judgement it warrants.
        if transfer.destination_account in limits.always_human_destinations:
            reasons.append(EscalationReason.ALWAYS_HUMAN.value)
        if is_first_payment:
            reasons.append(EscalationReason.NEW_DESTINATION.value)
        if not transfer.obligation_id:
            reasons.append(EscalationReason.NO_OBLIGATION.value)
        if amount_usd > limits.supervisor_review_threshold_xrp:
            reasons.append(EscalationReason.OVER_DELEGATED_AUTHORITY.value)
        if transfer.due_date is not None:
            buffer = limits.due_date_buffer_hours
            earliest = transfer.due_date - timedelta(hours=buffer)
            if now > earliest:
                reasons.append(EscalationReason.SHORT_NOTICE.value)

        if not reasons:
            return self._decide(Tier.LEVEL_1, [], transfer)

        # Level 3 where a second pair of eyes is warranted: a first payment to
        # a counterparty, an amount beyond the treasury manager's delegated
        # authority, or a participant the tenant has designated for
        # supervisor review.
        dual = {
            EscalationReason.NEW_DESTINATION.value,
            EscalationReason.OVER_DELEGATED_AUTHORITY.value,
            EscalationReason.ALWAYS_HUMAN.value,
        }
        if dual.intersection(reasons):
            return self._decide(Tier.LEVEL_3, reasons, transfer)

        return self._decide(Tier.LEVEL_2, reasons, transfer)

    def evaluate_all(
        self,
        transfers: Sequence[ProposedTransfer],
        treasury_balance_xrp: float,
        already_committed_today_xrp: float = 0.0,
        now: Optional[datetime] = None,
        first_payment_accounts: Optional[Sequence[str]] = None,
    ) -> List[PolicyDecision]:
        """Evaluate a batch. The daily cap aggregates across the batch.

        Payroll requires this: many beneficiary transfers against one daily
        disbursement limit.
        """
        firsts = set(first_payment_accounts or [])
        decisions: List[PolicyDecision] = []
        # The running total is in the unit of account, so the daily cap
        # aggregates correctly across disbursements in different currencies.
        committed = already_committed_today_xrp
        for transfer in transfers:
            decision = self.evaluate(
                transfer,
                treasury_balance_xrp=treasury_balance_xrp,
                already_committed_today_xrp=committed,
                now=now,
                is_first_payment=transfer.destination_account in firsts,
            )
            decisions.append(decision)
            if decision.tier is not Tier.REFUSED:
                committed += self.in_unit_of_account(
                    transfer.amount, transfer.currency
                )
        return decisions

    def _decide(
        self, tier: Tier, reasons: List[str], transfer: ProposedTransfer
    ) -> PolicyDecision:
        # Deduplicate while preserving order.
        seen = set()
        ordered = []
        for reason in reasons:
            if reason not in seen:
                seen.add(reason)
                ordered.append(reason)
        return PolicyDecision(
            tier=tier,
            reasons=ordered,
            required_authorisations=self.approval_matrix.required_for(tier),
            detail={"transfer": transfer.to_dict()},
        )
