"""
The signer: authorisation tier evaluation, then sign or withhold.

The agent process holds no key that can move funds without the policy
engine's evaluation. This module implements that separation:

    Level 1  Automatic          the policy engine approved it -> sign
    Level 2  Single Authorisation   one sign-off required -> withhold
    Level 3  Dual Authorisation    two sign-offs required -> withhold
    Refused                    outside policy -> refuse

For Levels 2 and 3, `ApprovalLedger` accumulates sign-offs against a
transfer. The signer signs only when the ledger holds the number of
authorisations the policy engine required for that tier. The second
sign-off at Level 3 may be provided by a supervisor — the ledger records
the role, so the audit trail shows who authorised what.

The local signer conforms to `xrpl_agent_id.wallet.WalletProvider` so the
existing signer abstraction can be reused rather than rebuilt. The external
signer path (a KMS, an HSM, a separate service) is the same protocol with a
different implementation.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Sequence

from .policy import PolicyDecision, PolicyEngine, ProposedTransfer, Tier

try:  # pragma: no cover - optional dependency, resolved at runtime
    from xrpl.models.transactions.transaction import Transaction
except Exception:  # pragma: no cover
    Transaction = object  # type: ignore


class Signer(Protocol):
    """Something that can identify an account and sign transactions for it.

    Mirrors `xrpl_agent_id.wallet.WalletProvider`.
    """

    @property
    def classic_address(self) -> str:
        ...

    @property
    def public_key(self) -> str:
        ...

    def sign(self, transaction: "Transaction") -> "Transaction":
        ...


@dataclass
class Authorisation:
    """One human sign-off against a transfer."""

    approver: str
    role: str  # e.g. "treasury_manager", "supervisor"
    at: str
    comment: str = ""

    def to_dict(self) -> Dict:
        return {
            "approver": self.approver,
            "role": self.role,
            "at": self.at,
            "comment": self.comment,
        }


@dataclass
class ApprovalLedger:
    """Tracks sign-offs against a transfer.

    Keyed by obligation id (or destination account when there is no
    obligation). Each entry holds the required count and the sign-offs
    received so far.
    """

    required: int = 0
    received: List[Authorisation] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.received)

    @property
    def satisfied(self) -> bool:
        return self.count >= self.required

    def add(self, authorisation: Authorisation) -> "ApprovalLedger":
        self.received.append(authorisation)
        return self

    def to_dict(self) -> Dict:
        return {
            "required": self.required,
            "received_count": self.count,
            "satisfied": self.satisfied,
            "authorisations": [a.to_dict() for a in self.received],
        }


@dataclass
class SignRequest:
    """A request to sign one transaction, with the policy decision attached."""

    decision: PolicyDecision
    transaction: object
    destination_account: str
    amount_xrp: float
    obligation_id: str = ""

    def to_dict(self) -> Dict:
        return {
            "destination_account": self.destination_account,
            "amount_xrp": self.amount_xrp,
            "obligation_id": self.obligation_id,
            "tier": self.decision.tier.value,
            "tier_label": self.decision.tier.label,
            "required_authorisations": self.decision.required_authorisations,
            "reasons": list(self.decision.reasons),
        }


@dataclass
class SignOutcome:
    """What the signer did with a request."""

    status: str  # "signed" | "awaiting_authorisation" | "refused"
    tier: Tier
    reasons: List[str]
    required_authorisations: int = 0
    received_authorisations: int = 0
    transaction_hash: Optional[str] = None
    signed_transaction: Optional[object] = None

    def to_dict(self) -> Dict:
        return {
            "status": self.status,
            "tier": self.tier.value,
            "tier_label": self.tier.label,
            "reasons": list(self.reasons),
            "required_authorisations": self.required_authorisations,
            "received_authorisations": self.received_authorisations,
            "transaction_hash": self.transaction_hash,
        }


class TreasurySigner:
    """Signs a transaction only when the policy engine and the approval
    ledger both permit it.

    This is the component that makes the agent safe to point at real funds:
    the agent cannot reach a signing key without passing through
    `PolicyEngine.evaluate` first, and an escalated transaction cannot be
    signed until the required authorisations have been recorded.
    """

    def __init__(
        self,
        provider: Optional[Signer],
        policy: PolicyEngine,
        approvals: Optional[Dict[str, ApprovalLedger]] = None,
    ):
        self._provider = provider
        self.policy = policy
        # obligation_id (or destination account) -> ApprovalLedger
        self.approvals: Dict[str, ApprovalLedger] = approvals or {}

    @property
    def has_provider(self) -> bool:
        return self._provider is not None

    @property
    def account(self) -> Optional[str]:
        return self._provider.classic_address if self._provider else None

    def ledger_for(self, key: str, required: int = 0) -> ApprovalLedger:
        if key not in self.approvals:
            self.approvals[key] = ApprovalLedger(required=required)
        elif required and not self.approvals[key].required:
            self.approvals[key].required = required
        return self.approvals[key]

    def record_authorisation(
        self,
        key: str,
        approver: str,
        role: str = "treasury_manager",
        comment: str = "",
        required: int = 0,
        at: Optional[str] = None,
    ) -> ApprovalLedger:
        """Record one sign-off against a transfer.

        `required` is the number of sign-offs the policy engine demands for
        this transfer's tier. Passing it in means the ledger knows its
        target from the moment the first sign-off lands, rather than
        learning it later - so `satisfied` is meaningful at every point.
        """
        from datetime import datetime, timezone

        ledger = self.ledger_for(key, required)
        ledger.add(
            Authorisation(
                approver=approver,
                role=role,
                at=at or datetime.now(timezone.utc).isoformat(),
                comment=comment,
            )
        )
        return ledger

    def sign(
        self,
        transaction: object,
        decision: PolicyDecision,
        destination_account: str = "",
        amount_xrp: float = 0.0,
        obligation_id: str = "",
    ) -> SignOutcome:
        """Sign only if the tier permits it and the approvals are in place."""
        if decision.tier is Tier.REFUSED:
            return SignOutcome(
                status="refused",
                tier=decision.tier,
                reasons=list(decision.reasons),
                required_authorisations=0,
            )

        # The approval ledger is keyed by the obligation id, falling back to
        # the destination account. An empty key would put EVERY transfer
        # that lacks both into one shared bucket - so one approval could
        # authorise an unrelated transfer. That is a fund-movement bug, so
        # an unkeyable transfer is refused rather than signed.
        key = obligation_id or destination_account
        if not key:
            return SignOutcome(
                status="refused",
                tier=decision.tier,
                reasons=["transfer_has_no_approval_key"],
                required_authorisations=decision.required_authorisations,
            )

        if decision.requires_authorisation:
            ledger = self.ledger_for(key, decision.required_authorisations)
            if not ledger.satisfied:
                return SignOutcome(
                    status="awaiting_authorisation",
                    tier=decision.tier,
                    reasons=list(decision.reasons),
                    required_authorisations=decision.required_authorisations,
                    received_authorisations=ledger.count,
                )

        if self._provider is None:
            # No signing provider. The authorisation requirement may well be
            # satisfied - report the ACTUAL count, not zero, or the
            # operator sees "0/2" on an instruction that has two
            # sign-offs against it.
            ledger = self.approvals.get(key)
            return SignOutcome(
                status="awaiting_authorisation",
                tier=decision.tier,
                reasons=["no_signing_provider_configured"],
                required_authorisations=decision.required_authorisations,
                received_authorisations=(ledger.count if ledger else 0),
            )

        signed = self._provider.sign(transaction)
        tx_hash = None
        try:
            tx_hash = signed.get_transaction_hash()
        except Exception:
            pass
        return SignOutcome(
            status="signed",
            tier=decision.tier,
            reasons=list(decision.reasons),
            required_authorisations=decision.required_authorisations,
            received_authorisations=(
                self.approvals[key].count if key in self.approvals else 0
            ),
            transaction_hash=tx_hash,
            signed_transaction=signed,
        )


class LocalSeedSigner:
    """A signer backed by a seed in the local sandbox.

    Mirrors `xrpl_agent_id.wallet.SeedWalletProvider`. Only used against the
    local devnet — a real deployment uses an HSM or KMS.
    """

    def __init__(self, seed: str, algorithm: str = "ed25519"):
        from xrpl.wallet import Wallet

        self._wallet = Wallet.from_seed(seed, algorithm=algorithm)

    @property
    def classic_address(self) -> str:
        return self._wallet.classic_address

    @property
    def public_key(self) -> str:
        return self._wallet.public_key

    def sign(self, transaction: "Transaction") -> "Transaction":
        from xrpl.transaction import sign as xrpl_sign

        return xrpl_sign(transaction, self._wallet)


class ExternalSignerSigner:
    """A signer that delegates to an external signing service.

    The transaction is serialised and posted to the signer service; the
    signature comes back. The key never touches this process. This is the
    shape a production deployment takes.
    """

    def __init__(self, endpoint: str, account: str, public_key: str, token: str):
        self.endpoint = endpoint
        self._classic_address = account
        self._public_key = public_key
        self._token = token

    @property
    def classic_address(self) -> str:
        return self._classic_address

    @property
    def public_key(self) -> str:
        return self._public_key

    def sign(self, transaction: "Transaction") -> "Transaction":
        """Send the transaction to the signer service and take the
        signature back.

        The wire format is a BLOB in both directions: the request
        carries `tx_blob` (the hex serialisation) and the response
        carries `signed_tx` (the hex serialisation of the signed
        transaction). A signer that returned JSON instead would be a
        different contract, and the two would have to agree.

        A service that returns the signed transaction as JSON is also
        accepted, because some do. The two are told apart by whether
        the value parses as JSON.
        """
        import json

        import httpx

        from xrpl.core.binarycodec import decode, encode
        from xrpl.models.transactions.transaction import Transaction as T

        payload = {
            "tx_blob": encode(transaction.to_xrpl()),
            "account": self._classic_address,
        }
        response = httpx.post(
            self.endpoint,
            json=payload,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=30.0,
        )
        response.raise_for_status()
        body = response.json()

        signed = body.get("signed_tx")
        if not signed:
            raise RuntimeError(
                f"the signer service returned no signature: {body}"
            )

        # A blob, or a JSON object. A hex blob starts with a hex digit
        # and is not valid JSON on its own; a JSON object starts with a
        # brace.
        text = str(signed).strip()
        if text.startswith("{"):
            returned = T.from_xrpl(json.loads(text))
        else:
            returned = T.from_xrpl(decode(text))

        # Verify that what came back is what was sent.
        #
        # The signer is a separate process, possibly a separate
        # organisation, possibly a third-party custody service. If it
        # returns a signature over a DIFFERENT transaction - a different
        # destination, a different amount - the service would submit
        # that one, because it has no reason to look.
        #
        # Signing does not change the fields it is supposed to change:
        # the account, the destination, the amount, the fee, the
        # sequence, and the domain all have to be identical. Only the
        # signature itself (and the signing flags) may differ.
        self._assert_same_transaction(transaction, returned)
        return returned

    def _assert_same_transaction(self, requested, returned) -> None:
        """Raise unless the returned transaction is the requested one.

        The fields checked are the ones that decide where money goes
        and how much. A signature over a different destination is a
        different payment, and it must not be submitted.
        """
        # The xrpl-py models use snake_case attribute names:
        # `account`, `destination`, `domain_id`. Comparing the
        # CamelCase names compared None with None, which verified
        # nothing at all.
        mismatches = []
        for field in (
            "account",
            "destination",
            "amount",
            "fee",
            "sequence",
            "destination_tag",
            # The domain is what confines the payment to the tenant's
            # perimeter. A signer that dropped it would take the payment
            # outside the domain, and the ledger would then enforce
            # nothing.
            "domain_id",
        ):
            want = getattr(requested, field, None)
            got = getattr(returned, field, None)
            if want != got:
                mismatches.append(f"{field}: requested {want!r}, got {got!r}")

        if mismatches:
            raise RuntimeError(
                "the signer returned a transaction that is not the one "
                "requested: " + "; ".join(mismatches)
            )
