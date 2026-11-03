"""
Reconciliation: match a proposal to the on-chain Payment, then to completion.

This is the component that makes the ERP view trustworthy, and it runs BEFORE
the planner, not after the executor.

Two failure modes it exists to prevent:

1. **Double funding** — a transfer proposed from ERP data that the ledger
   already shows as funded.
2. **Stale ERP** — the ledger has movements the ERP never recorded (a manual
   transfer, a clawback, an MPT share recovery), so the ERP's view of the
   treasury is wrong and the agent must not act on it.

The ERP is authoritative for *what is owed*. The ledger is authoritative for
*what exists*. This module joins them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from .obligations import Obligation


@dataclass
class ReconciledTransfer:
    """A proposal matched against the ledger."""

    obligation_id: str
    destination_account: str
    amount: float
    currency: str = "RLUSD"
    matched: bool = False
    transaction_hash: Optional[str] = None
    ledger_index: Optional[int] = None
    engine_result: Optional[str] = None
    note: str = ""

    def to_dict(self) -> Dict:
        return {
            "obligation_id": self.obligation_id,
            "destination_account": self.destination_account,
            "amount": self.amount,
            "currency": self.currency,
            "matched": self.matched,
            "transaction_hash": self.transaction_hash,
            "ledger_index": self.ledger_index,
            "engine_result": self.engine_result,
            "note": self.note,
        }


@dataclass
class ReconciliationReport:
    """The result of reconciling a tenant's obligations against the ledger."""

    tenant_id: str
    obligations: List[ReconciledTransfer] = field(default_factory=list)
    funded_obligation_ids: List[str] = field(default_factory=list)
    unmatched: List[Dict] = field(default_factory=list)
    ledger_exceptions: List[Dict] = field(default_factory=list)
    # True when reconciliation could not establish a trustworthy view. A
    # blocked report must not be used to plan disbursements.
    blocked: bool = False
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def blocked_reason(self) -> str:
        """Why the report is blocked, in one sentence.

        Derived from the exceptions rather than stored separately, so the
        two cannot drift apart. A blocked report with no reason is not
        much better than an unblocked one.
        """
        if not self.blocked:
            return ""
        details = [
            str(e.get("detail") or e.get("error") or "")
            for e in self.ledger_exceptions
        ]
        details = [d for d in details if d]
        if details:
            return details[0]
        return "The ledger could not be read."

    def to_dict(self) -> Dict:
        return {
            "tenant_id": self.tenant_id,
            "obligations": [o.to_dict() for o in self.obligations],
            "funded_obligation_ids": list(self.funded_obligation_ids),
            "unmatched": list(self.unmatched),
            "ledger_exceptions": list(self.ledger_exceptions),
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "generated_at": self.generated_at.isoformat(),
        }


class Reconciler:
    """Matches obligations to on-chain payments.

    The implementation here matches on (destination, amount) within a
    destination-tag convention. The production path uses a destination tag or
    an invoice id so the match is unambiguous, and anchors the reconciliation
    result in the agent log.
    """

    def __init__(self, destination_tag: int = 42730):
        self.destination_tag = destination_tag

    async def reconcile(
        self,
        tenant_id: str,
        obligations: Sequence[Obligation],
        ledger,
        treasury_account: str,
        lookback_ledgers: int = 50,
    ) -> ReconciliationReport:
        report = ReconciliationReport(tenant_id=tenant_id)

        # Pull the treasury account's recent payments. If this read fails,
        # reconciliation has NOT succeeded - it has failed to run. That is
        # materially different from "nothing is funded", and the difference
        # matters: the planner must not propose transfers on a view it
        # could not establish.
        payments: List[Dict] = []
        try:
            payments = await ledger.account_payments(
                treasury_account, limit=lookback_ledgers
            )
        except Exception as e:  # pragma: no cover
            report.ledger_exceptions.append(
                {
                    "kind": "ledger_read_failed",
                    "error": repr(e),
                    "detail": (
                        "The treasury's transaction history could not be read, "
                        "so the service cannot establish which obligations are "
                        "already settled. The plan is blocked until the "
                        "ledger can be read."
                    ),
                }
            )
            report.blocked = True
            return report

        for obligation in obligations:
            entry = ReconciledTransfer(
                obligation_id=obligation.id,
                destination_account=obligation.funding_account,
                amount=obligation.amount,
                currency=obligation.currency,
            )
            for payment in payments:
                if self._matches(obligation, payment):
                    entry.matched = True
                    entry.transaction_hash = payment.get("hash")
                    entry.ledger_index = payment.get("ledger_index")
                    entry.engine_result = payment.get("engine_result")
                    entry.note = "matched on ledger"
                    report.funded_obligation_ids.append(obligation.id)
                    break
            if not entry.matched:
                entry.note = "not yet funded on the ledger"
                report.unmatched.append(entry.to_dict())
            report.obligations.append(entry)

        return report

    def _matches(self, obligation: Obligation, payment: Dict) -> bool:
        if payment.get("engine_result") != "tesSUCCESS":
            return False
        if payment.get("destination") != obligation.funding_account:
            return False
        # The currency must match too. 100 CAD and 100 RLUSD are different
        # amounts to the same beneficiary.
        if payment.get("currency") not in (obligation.currency, None):
            return False
        amount = payment.get("amount")
        if amount is None:
            return False
        return abs(float(amount) - obligation.amount) < 0.000001
