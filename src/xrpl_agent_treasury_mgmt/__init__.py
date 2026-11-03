"""
xrpl_agent_treasury_mgmt — a treasury tool for the XRP Ledger.

A treasury tool for the XRP Ledger.

The service holds no signing key: it asks a separate signing service
for a signature, and the ledger enforces a PermissionedDomain perimeter
on every payment it builds.
The product premise, proven on the live devnet on 2026-11-02:

    A Payment carrying `DomainID` is rejected with tecNO_PERMISSION unless
    BOTH the sender and the destination hold an accepted credential in that
    PermissionedDomain.

So "no external movement" can be enforced by the ledger rather than promised
by the agent. This package composes that primitive into a treasury service:

    tenants -> accounts -> obligations -> forecast -> policy -> planner
            -> signer -> executor -> reconcile -> agent_log
"""

__version__ = "0.1.0"

__all__ = [
    "accounts",
    "agent_log",
    "executor",
    "obligations",
    "planner",
    "policy",
    "reconcile",
    "service",
    "signer",
    "tenants",
]
