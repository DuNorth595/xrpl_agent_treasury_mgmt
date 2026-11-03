"""
The planner: obligations -> proposed internal transfers.

The planner consumes reconciled ledger state, not just the ERP's obligations.
"Has this already been funded?" has to be answerable from the ledger before
anything is proposed, or the agent double-funds.

There is no amount prediction here (decision D7). The valuable, defensible part
is the policy + reconciliation + audit layer, not a forecasting model any ERP
already has. The forecast is a due-date buffer and a minimum-balance rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

from .obligations import Obligation
from .policy import ProposedTransfer


@dataclass
class PlannedTransfer:
    """A proposed transfer plus the obligation it came from."""

    transfer: ProposedTransfer
    obligation: Optional[Obligation]
    funded_already: bool = False
    notes: List[str] = field(default_factory=list)
    # The USD equivalent, when a rate is known. Presentation only.
    usd_equivalent: Optional[float] = None

    def to_dict(self) -> Dict:
        return {
            "transfer": self.transfer.to_dict(),
            "obligation": self.obligation.to_dict() if self.obligation else None,
            "funded_already": self.funded_already,
            "notes": list(self.notes),
            "usd_equivalent": self.usd_equivalent,
        }


@dataclass
class Plan:
    """A batch of proposed transfers for one tenant."""

    tenant_id: str
    planned: List[PlannedTransfer] = field(default_factory=list)
    skipped: List[Dict] = field(default_factory=list)
    treasury_balance_xrp: float = 0.0
    already_committed_today_xrp: float = 0.0
    # True when the plan could not be built on a trustworthy view — the
    # obligation source was unreachable, or reconciliation could not
    # establish what is already funded. A blocked plan proposes nothing.
    blocked: bool = False
    blocked_reason: str = ""
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict:
        return {
            "tenant_id": self.tenant_id,
            "planned": [p.to_dict() for p in self.planned],
            "skipped": list(self.skipped),
            "treasury_balance_xrp": self.treasury_balance_xrp,
            "already_committed_today_xrp": self.already_committed_today_xrp,
            "planned_total_xrp": round(
                sum(p.transfer.amount for p in self.planned), 6
            ),
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "generated_at": self.generated_at.isoformat(),
        }


class Planner:
    """Turns obligations into proposed transfers.

    `already_funded` comes from the reconciler: a set of obligation ids the
    ledger already shows as funded.
    """

    def __init__(
        self,
        buffer_hours: int = 48,
        min_balance_xrp: float = 1000.0,
        already_funded: Optional[Sequence[str]] = None,
        currency_book=None,
    ):
        self.buffer_hours = buffer_hours
        self.min_balance_xrp = min_balance_xrp
        self.already_funded = set(already_funded or [])
        # The minimum balance is a USD figure; each obligation is compared
        # at its USD equivalent so the floor means the same in every
        # currency.
        self.currency_book = currency_book

    def _in_unit_of_account(self, amount: float, currency: str) -> float:
        if self.currency_book is None:
            return amount
        usd = self.currency_book.to_usd(amount, currency)
        return amount if usd is None else usd

    def set_already_funded(self, obligation_ids: Sequence[str]) -> None:
        self.already_funded = set(obligation_ids)

    def plan(
        self,
        tenant_id: str,
        obligations: Sequence[Obligation],
        treasury_balance_xrp: float,
        already_committed_today_xrp: float = 0.0,
        now: Optional[datetime] = None,
        settlement_balance: Optional[float] = None,
    ) -> Plan:
        """Build the plan.

        TWO BALANCES, TWO JOBS
        =====================

        `treasury_balance_xrp`   The treasury's XRP. XRP pays FEES. The
                                  floor exists so the treasury can always
                                  still submit a transaction, so this is
                                  what the floor is compared against.

        `settlement_balance`     The treasury's holding of the settlement
                                  currency (RLUSD), in the unit of
                                  account. This is what the treasury
                                  actually spends, so this is what the
                                  proposed amounts come out of.

        Earlier code passed the RLUSD balance in the parameter named
        `treasury_balance_xrp` and compared a USD-equivalent amount
        against an XRP-denominated floor. The numbers were of different
        kinds, so the floor fired on a treasury that had plenty of XRP,
        and every transfer was skipped as
        `would_breach_min_treasury_balance`. The names were the tell.
        """
        now = now or datetime.now(timezone.utc)
        if settlement_balance is None:
            settlement_balance = treasury_balance_xrp
        plan = Plan(
            tenant_id=tenant_id,
            treasury_balance_xrp=treasury_balance_xrp,
            already_committed_today_xrp=already_committed_today_xrp,
        )

        running_total = 0.0
        for obligation in sorted(obligations, key=lambda o: o.due_date):
            # Reconciliation gate: never propose a transfer for something the
            # ledger already shows as funded.
            if obligation.id in self.already_funded:
                plan.skipped.append(
                    {
                        "obligation_id": obligation.id,
                        "description": obligation.description,
                        "reason": "already_funded_on_ledger",
                        "amount": obligation.amount,
                    }
                )
                continue

            # Too soon: the due-date buffer means we wait until closer to the
            # date. Forecasting is a buffer, not a prediction.
            earliest = obligation.due_date - timedelta(hours=self.buffer_hours)
            if now < earliest:
                plan.skipped.append(
                    {
                        "obligation_id": obligation.id,
                        "description": obligation.description,
                        "reason": "outside_due_date_buffer",
                        "amount": obligation.amount,
                    }
                )
                continue

            # The minimum-balance rule has two halves, because the
            # treasury holds two balances that serve two purposes.
            #
            #   The FLOOR is on XRP. XRP pays fees, so a treasury below its
            #   XRP floor cannot reliably submit a transaction at all. The
            #   fee is small and fixed, so the check is that XRP remains
            #   above the floor.
            #
            #   The SPENDABLE amount is the settlement currency. A
            #   transfer cannot be proposed from money the treasury does
            #   not hold, so the proposed amounts come out of the RLUSD
            #   balance, compared at the USD equivalent.
            remaining_settlement = settlement_balance - running_total
            amount_usd = self._in_unit_of_account(
                obligation.amount, obligation.currency
            )

            if remaining_settlement - amount_usd < 0:
                plan.skipped.append(
                    {
                        "obligation_id": obligation.id,
                        "description": obligation.description,
                        "reason": "insufficient_settlement_balance",
                        "amount": obligation.amount,
                    }
                )
                continue

            if treasury_balance_xrp < self.min_balance_xrp:
                plan.skipped.append(
                    {
                        "obligation_id": obligation.id,
                        "description": obligation.description,
                        "reason": "would_breach_min_treasury_balance",
                        "amount": obligation.amount,
                    }
                )
                continue

            transfer = ProposedTransfer(
                destination_account=obligation.funding_account,
                amount=obligation.amount,
                currency=obligation.currency,
                due_date=obligation.due_date,
                entity=obligation.entity,
                obligation_id=obligation.id,
                reference=obligation.description,
            )
            plan.planned.append(
                PlannedTransfer(
                    transfer=transfer,
                    obligation=obligation,
                )
            )
            running_total += self._in_unit_of_account(
                obligation.amount, obligation.currency
            )

        return plan
