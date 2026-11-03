"""
Obligations: what is owed, from plug-ins.

The ERP is authoritative for *what is owed* (vendor, amount, due date). The
ledger is authoritative for *what exists*. This module models the first side
and leaves the second to `reconcile`.

An obligation fans out to N transfers. That is not an optimisation — payroll
has many beneficiaries and a hard deadline, so the fan-out has to be
first-class from day one rather than retrofitted later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Protocol, Sequence


@dataclass
class Obligation:
    """One thing the tenant owes, and the internal account that funds it."""

    id: str
    description: str
    amount: float
    due_date: datetime
    # The ledger-side account that receives the pre-fund. This is the unit of
    # operation, not the business entity.
    funding_account: str
    entity: str = ""
    vendor: str = ""
    currency: str = "RLUSD"
    # Extra destinations for a fan-out (payroll: many beneficiaries).
    allocations: List[Dict] = field(default_factory=list)
    source: str = "erp"

    @property
    def hours_until_due(self) -> float:
        now = datetime.now(timezone.utc)
        return (self.due_date - now).total_seconds() / 3600.0

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "description": self.description,
            "amount": self.amount,
            "due_date": self.due_date.isoformat(),
            "funding_account": self.funding_account,
            "entity": self.entity,
            "vendor": self.vendor,
            "currency": self.currency,
            "allocations": list(self.allocations),
            "source": self.source,
            "hours_until_due": round(self.hours_until_due, 1),
        }


class ObligationSource(Protocol):
    """A plug-in that supplies obligations."""

    def fetch(self) -> List[Obligation]:
        ...


@dataclass
class CsvObligationSource:
    """A reference adapter: obligations from a CSV file.

    Columns: id, description, amount, due_date, funding_account,
    entity, vendor, currency
    """

    path: str
    name: str = "reference-csv"

    def fetch(self) -> List[Obligation]:
        import csv
        import pathlib

        rows = pathlib.Path(self.path).read_text().strip().split("\n")
        obligations: List[Obligation] = []
        for row in csv.DictReader(rows):
            due = datetime.fromisoformat(row["due_date"])
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            obligations.append(
                Obligation(
                    id=row["id"],
                    description=row.get("description", ""),
                    amount=float(row["amount"]),
                    due_date=due,
                    funding_account=row["funding_account"],
                    entity=row.get("entity", ""),
                    vendor=row.get("vendor", ""),
                    currency=row.get("currency", "RLUSD"),
                )
            )
        return obligations


@dataclass
class InMemoryObligationSource:
    """Obligations held in memory. Used by the demo and the tests."""

    obligations: List[Obligation]
    name: str = "in-memory"

    def fetch(self) -> List[Obligation]:
        return list(self.obligations)
