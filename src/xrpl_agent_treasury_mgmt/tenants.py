"""
Tenant configuration and domain resolution.

The PermissionedDomain IS the tenant boundary. That has one hard rule:

    The domain id comes from tenant config. It is NEVER caller-supplied.

A request names the tenant and the destination account. The service resolves
the domain. There is no request path by which a caller can choose a domain,
because a tenant-mixing bug here is a fund-movement bug, not a data leak.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class TenantError(Exception):
    """Raised when tenant configuration is missing or inconsistent."""


@dataclass(frozen=True)
class Limits:
    """The authorisation policy envelope for one tenant.

    `supervisor_review_threshold_xrp` is the treasury manager's delegated
    authority. A transfer above it requires a second sign-off even if every
    other control is satisfied.
    """

    max_single_transfer_xrp: float = 500.0
    max_daily_total_xrp: float = 5000.0
    min_treasury_balance_xrp: float = 1000.0
    # The treasury manager's delegated authority. Above this, a second
    # sign-off is required (Level 3).
    supervisor_review_threshold_xrp: float = 250.0
    # A transfer must land this many hours before the obligation is due.
    due_date_buffer_hours: int = 48
    # Participants designated for supervisor review. Transfers to these
    # destinations require dual authorisation regardless of amount.
    always_human_destinations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "max_single_transfer_xrp": self.max_single_transfer_xrp,
            "max_daily_total_xrp": self.max_daily_total_xrp,
            "min_treasury_balance_xrp": self.min_treasury_balance_xrp,
            "supervisor_review_threshold_xrp": self.supervisor_review_threshold_xrp,
            "due_date_buffer_hours": self.due_date_buffer_hours,
            "always_human_destinations": list(self.always_human_destinations),
        }


@dataclass(frozen=True)
class Tenant:
    """One tenant on the ledger.

    A tenant owns exactly one PermissionedDomain. Every Payment the service
    builds for this tenant carries that domain id, so the ledger rejects any
    transfer to a destination that is not a member.
    """

    id: str
    name: str
    domain_id: str
    treasury_account: str
    limits: Limits = field(default_factory=Limits)
    # Accounts this tenant may fund, mapped to the business entity they serve.
    cost_centres: Dict[str, str] = field(default_factory=dict)
    currency: str = "XRP"
    # How many authorisations each tier requires. Configurable per tenant.
    level_2_authorisations: int = 1
    level_3_authorisations: int = 2

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "domain_id": self.domain_id,
            "treasury_account": self.treasury_account,
            "currency": self.currency,
            "limits": self.limits.to_dict(),
            "cost_centres": dict(self.cost_centres),
            "approval_matrix": {
                "level_2_authorisations": self.level_2_authorisations,
                "level_3_authorisations": self.level_3_authorisations,
            },
        }


@dataclass
class TenantRegistry:
    """Holds the tenants this service instance serves."""

    _tenants: Dict[str, Tenant] = field(default_factory=dict)

    def add(self, tenant: Tenant) -> None:
        if tenant.id in self._tenants:
            raise TenantError(f"tenant {tenant.id!r} already registered")
        self._tenants[tenant.id] = tenant

    def get(self, tenant_id: str) -> Tenant:
        try:
            return self._tenants[tenant_id]
        except KeyError:
            raise TenantError(f"unknown tenant {tenant_id!r}")

    def domain_for(self, tenant_id: str) -> str:
        """Resolve the domain id for a tenant.

        This is the only place a domain id is read from. Callers pass a tenant
        id, never a domain id.
        """
        return self.get(tenant_id).domain_id

    def all(self) -> List[Tenant]:
        return list(self._tenants.values())

    def __len__(self) -> int:
        return len(self._tenants)

    # ---------------------------------------------------------------- loading

    @classmethod
    def from_file(cls, path: pathlib.Path) -> "TenantRegistry":
        raw = json.loads(pathlib.Path(path).read_text())
        registry = cls()
        for entry in raw.get("tenants", []):
            limits = Limits(**entry.get("limits", {}))
            registry.add(
                Tenant(
                    id=entry["id"],
                    name=entry.get("name", entry["id"]),
                    domain_id=entry["domain_id"],
                    treasury_account=entry["treasury_account"],
                    limits=limits,
                    cost_centres=entry.get("cost_centres", {}),
                    currency=entry.get("currency", "XRP"),
                )
            )
        return registry

    @classmethod
    def from_dict(cls, raw: Dict) -> "TenantRegistry":
        registry = cls()
        for entry in raw.get("tenants", []):
            registry.add(
                Tenant(
                    id=entry["id"],
                    name=entry.get("name", entry["id"]),
                    domain_id=entry["domain_id"],
                    treasury_account=entry["treasury_account"],
                    limits=Limits(**entry.get("limits", {})),
                    cost_centres=entry.get("cost_centres", {}),
                    currency=entry.get("currency", "XRP"),
                )
            )
        return registry
