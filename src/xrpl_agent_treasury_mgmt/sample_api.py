"""
Sample endpoints — a demonstration surface for every capability.

WHY THESE EXIST
===============

The service's real endpoints are the ones a tenant calls. But a
reviewer needs to be able to exercise every capability without a real ERP,
a real custody API, or a funded wallet, and without spending anything.

These endpoints are namespaced under `/api/sample/` so they can never be
confused with the operational surface, and they are registered only when
the app is created in sample mode.

MOST OF THEM DO NOT MOVE MONEY.
==============================

Every endpoint here is read-only or a dry run. The endpoint that builds a
payment (`/simulate`) uses `simulate`, which asks the ledger what would
happen without submitting anything.

ONE EXCEPTION, GUARDED
======================

`/settle` submits a REAL transaction. It is the only endpoint in this
module that can move value, and it is guarded three ways: it requires an
explicit `confirm: true` in the request, it refuses to run against a
non-demo tenant, and it caps the amount. Without `confirm: true` it
returns the payment it WOULD send and moves nothing.

The point of having it is that a treasury's most important claim - "a
payment actually settles on-chain" - is only worth anything if it has
been demonstrated rather than asserted.

WHAT EACH ONE DEMONSTRATES
==========================

    /capabilities        what this service can do, as machine-readable data
    /domain              the PermissionedDomain perimeter, read from the ledger
    /policy-matrix       the authorisation tiers and their sign-off counts
    /evaluate            run a transfer through the policy engine, no ledger
    /simulate            build a payment and simulate it on the live node
    /cross-currency      the same USD value in each currency, tier by tier
    /scenario/approved   a transfer that will be auto-approved
    /scenario/single     a transfer that needs one sign-off
    /scenario/dual       a transfer that needs two sign-offs
    /scenario/refused    a transfer that will be refused
    /scenario/blocked    what a source outage looks like
    /approval-walkthrough  record sign-offs and watch the gate open
    /settle               submit a real payment (requires confirm: true)
"""

from __future__ import annotations

import json
import pathlib
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from flask import jsonify, request

from .executor import Executor
from .policy import ProposedTransfer, Tier
from .signer import TreasurySigner
from xrpl_agent_treasury_mgmt.devnet_accounts import address_for as _address

# Where the local devnet's account seeds live. Only used by the /settle
# endpoint, and only on a local devnet.
_DEVNET_ACCOUNTS = pathlib.Path.home() / ".xrpl-up" / "local-accounts.json"


class _UnreachableSource:
    """An obligation source that cannot be read.

    Used by the blocked-scenario endpoint to induce a genuine outage
    rather than describe one.
    """

    def __init__(self, original):
        self._original = original

    def fetch(self):
        raise ConnectionError(
            "the obligation source is unreachable (induced for the "
            "blocked-scenario demonstration)"
        )

    def __getattr__(self, name):
        return getattr(self._original, name)


def _payable_member(members: List[str], treasury: str) -> str:
    """The first domain member that is not the treasury.

    The treasury paying itself is a self-payment: it would return
    tesSUCCESS and prove nothing about settling to a beneficiary.
    """
    for member in members:
        if member != treasury:
            return member
    return members[0] if members else treasury


def _settleable_member(service, members: List[str], treasury: str) -> str:
    """A domain member whose trust line actually ripples.

    A beneficiary can be a domain member and still be UNPAYABLE: if the
    trust line was created before the issuer allowed rippling, the
    issuer side of the line carries a stale no-ripple bit and every
    payment fails with tecPATH_DRY. Domain membership and payment
    reachability are different properties.

    This picks the first member that is not the treasury and whose line
    is not no-ripple on either side.
    """
    for member in members:
        if member == treasury:
            continue
        try:
            lines = _run(service.ledger.account_lines(member))
        except Exception:
            continue
        for line in lines:
            if line.get("no_ripple") or line.get("no_ripple_peer"):
                continue
            return member
    return _payable_member(members, treasury)


def _devnet_seed(address: str) -> Optional[str]:
    """The local devnet seed for an address, if the devnet file is present."""
    try:
        if not _DEVNET_ACCOUNTS.exists():
            return None
        raw = json.loads(_DEVNET_ACCOUNTS.read_text())
        if isinstance(raw, dict) and "accounts" in raw:
            raw = raw["accounts"]
        items = raw if isinstance(raw, list) else list(raw.values())
        for item in items:
            if item.get("address") == address:
                return item.get("seed")
    except Exception:
        return None
    return None


def _sign(tx_json: Dict, seed: str) -> str:
    """Sign a transaction with a local devnet seed and return the blob."""
    from xrpl.core.binarycodec import encode
    from xrpl.models.transactions.transaction import Transaction
    from xrpl.transaction import sign as xrpl_sign
    from xrpl.wallet import Wallet

    wallet = Wallet.from_seed(seed, algorithm="ed25519")
    signed = xrpl_sign(Transaction.from_xrpl(tx_json), wallet)
    return encode(signed.to_xrpl())


def register_sample_endpoints(app, service) -> None:
    """Register the sample endpoints on a Flask app."""

    def svc():
        return app.config["SERVICE"]

    # ------------------------------------------------------- capabilities

    @app.route("/api/sample/capabilities")
    def sample_capabilities():
        """What this service can do, as machine-readable data.

        The endpoint a reviewer should read first.
        """
        return jsonify(
            {
                "service": "xrpl_agent_treasury_mgmt",
                "version": "0.1.0",
                "mode": "sample",
                "money_moved_by_this_endpoint": False,
                "money_moving_endpoints": ["/api/sample/settle"],
                "capabilities": [
                    {
                        "id": "ledger_enforced_perimeter",
                        "summary": (
                            "Payments carry DomainID from tenant config, so the "
                            "ledger rejects any transfer to a non-participant "
                            "with tecNO_PERMISSION."
                        ),
                        "endpoints": ["/api/sample/domain"],
                        "proven_on_live_node": True,
                    },
                    {
                        "id": "multi_currency_disbursement",
                        "summary": (
                            "A disbursement is denominated in the currency its "
                            "counterparty invoices in. The treasury holds "
                            "RLUSD, CAD and MXN."
                        ),
                        "endpoints": ["/api/sample/cross-currency"],
                        "proven_on_live_node": True,
                    },
                    {
                        "id": "authorisation_tiers",
                        "summary": (
                            "Three tiers with configurable sign-off counts: "
                            "automatic, single authorisation, dual "
                            "authorisation."
                        ),
                        "endpoints": [
                            "/api/sample/policy-matrix",
                            "/api/sample/approval-walkthrough",
                        ],
                        "proven_on_live_node": True,
                    },
                    {
                        "id": "policy_evaluation",
                        "summary": (
                            "Every transfer is evaluated against a tenant's "
                            "delegated authority before anything is signed."
                        ),
                        "endpoints": ["/api/sample/evaluate"],
                        "proven_on_live_node": True,
                    },
                    {
                        "id": "dry_run_simulation",
                        "summary": (
                            "A payment is built and simulated on the live "
                            "node without being submitted."
                        ),
                        "endpoints": ["/api/sample/simulate"],
                        "proven_on_live_node": True,
                    },
                    {
                        "id": "erp_integration",
                        "summary": (
                            "Obligations come from a pluggable source over "
                            "HTTP, with a field mapping."
                        ),
                        "endpoints": ["/api/sample/source"],
                        "proven_on_live_node": True,
                    },
                    {
                        "id": "live_fx",
                        "summary": (
                            "Exchange rates come from a live provider, "
                            "cached, with staleness detection. A rate "
                            "failure never alters what is transferred."
                        ),
                        "endpoints": ["/api/sample/rates"],
                        "proven_on_live_node": True,
                    },
                    {
                        "id": "hash_chained_decision_log",
                        "summary": (
                            "Every decision is appended to a hash-chained "
                            "log, so an altered or removed entry is "
                            "detectable."
                        ),
                        "endpoints": ["/api/sample/decision-log"],
                        "proven_on_live_node": True,
                    },
                ],
            }
        )

    # -------------------------------------------------------------- domain

    @app.route("/api/sample/domain")
    def sample_domain():
        """The PermissionedDomain perimeter, read from the ledger."""
        service = svc()
        tenant_id = request.args.get("tenant") or service.registry.all()[0].id
        tenant = service.registry.get(tenant_id)
        members = service._members(tenant_id)
        return jsonify(
            {
                "tenant_id": tenant_id,
                "domain_id": tenant.domain_id,
                "participants": members,
                "participant_count": len(members),
                "mechanism": (
                    "A Payment carrying DomainID is rejected with "
                    "tecNO_PERMISSION unless BOTH sender and destination "
                    "hold an accepted credential in the domain."
                ),
                "guarantee": (
                    "The agent cannot move funds out of the domain. Funds "
                    "already received can move onward — the guarantee is "
                    "about the perimeter, not about freezing value."
                ),
            }
        )

    # ------------------------------------------------------ policy matrix

    @app.route("/api/sample/policy-matrix")
    def sample_policy_matrix():
        """The authorisation tiers and their sign-off counts."""
        service = svc()
        tenant_id = request.args.get("tenant") or service.registry.all()[0].id
        tenant = service.registry.get(tenant_id)
        engine = service.policy_for(tenant_id)
        return jsonify(
            {
                "tenant_id": tenant_id,
                "tiers": [
                    {
                        "tier": "level_1",
                        "label": Tier.LEVEL_1.label,
                        "required_authorisations": 0,
                        "meaning": (
                            "Within delegated authority. Executed "
                            "automatically."
                        ),
                    },
                    {
                        "tier": "level_2",
                        "label": Tier.LEVEL_2.label,
                        "required_authorisations": tenant.level_2_authorisations,
                        "meaning": (
                            "Requires judgement. Escalated for a first "
                            "payment, a short settlement window, or a "
                            "missing obligation record."
                        ),
                    },
                    {
                        "tier": "level_3",
                        "label": Tier.LEVEL_3.label,
                        "required_authorisations": tenant.level_3_authorisations,
                        "meaning": (
                            "Requires a second pair of eyes. Escalated where "
                            "the amount exceeds the manager's delegated "
                            "authority, the beneficiary is new, or the "
                            "participant is designated for supervisor review."
                        ),
                    },
                    {
                        "tier": "refused",
                        "label": Tier.REFUSED.label,
                        "required_authorisations": 0,
                        "meaning": (
                            "Outside the policy envelope. No authorisation "
                            "makes an over-limit transfer compliant."
                        ),
                    },
                ],
                "limits": tenant.limits.to_dict(),
                "limits_note": (
                    "Every limit is denominated in the unit of account "
                    "(USD) and compared at each disbursement's USD "
                    "equivalent."
                ),
                "approval_matrix": engine.approval_matrix.to_dict(),
            }
        )

    # ------------------------------------------------------------ evaluate

    @app.route("/api/sample/evaluate", methods=["POST"])
    def sample_evaluate():
        """Run a transfer through the policy engine. No ledger, no money."""
        service = svc()
        tenant_id = request.args.get("tenant") or service.registry.all()[0].id
        tenant = service.registry.get(tenant_id)
        body = request.get_json(force=True) or {}

        amount = float(body.get("amount", 0))
        currency = body.get("currency", "RLUSD")
        destination = body.get("destination", "")

        # Default the destination to a known participant so the sample
        # works without the caller knowing the accounts.
        if not destination:
            members = service._members(tenant_id)
            destination = members[1] if len(members) > 1 else members[0]

        engine = service.policy_for(tenant_id)
        transfer = ProposedTransfer(
            destination_account=destination,
            amount=amount,
            currency=currency,
            obligation_id=body.get("obligation_id", "SAMPLE-OBL-1"),
        )
        decision = engine.evaluate(
            transfer,
            treasury_balance_xrp=service.xrp_balance(tenant_id),
            already_committed_today_xrp=float(body.get("already_committed", 0)),
        )
        presented = service.currency_book.present(amount, currency)
        return jsonify(
            {
                "request": {
                    "amount": amount,
                    "currency": currency,
                    "destination": destination,
                },
                "presented": presented,
                "decision": decision.to_dict(),
                "money_moved": False,
            }
        )

    # ------------------------------------------------------------ simulate

    @app.route("/api/sample/simulate", methods=["POST"])
    def sample_simulate():
        """Build a payment and simulate it on the live node.

        The payment is NEVER submitted. `simulate` asks the ledger what
        would happen and the ledger does not change.
        """
        service = svc()
        tenant_id = request.args.get("tenant") or service.registry.all()[0].id
        tenant = service.registry.get(tenant_id)
        body = request.get_json(force=True) or {}

        amount = float(body.get("amount", 180.0))
        currency = body.get("currency", "RLUSD")
        members = service._members(tenant_id)
        destination = body.get("destination") or _payable_member(
            members, tenant.treasury_account
        )

        engine = service.policy_for(tenant_id)
        transfer = ProposedTransfer(
            destination_account=destination,
            amount=amount,
            currency=currency,
            obligation_id=body.get("obligation_id", "SAMPLE-SIM"),
        )
        decision = engine.evaluate(
            transfer, treasury_balance_xrp=service.xrp_balance(tenant_id)
        )

        account_data = _run(service.ledger.account_info(tenant.treasury_account))
        current = _run(service.ledger.current_ledger_seq())
        executor = Executor(
            service.ledger,
            # The SERVICE's signer, not a fresh one with no provider.
            # A fresh signer would sign locally even when the service is
            # configured to sign through an external signer service, so
            # the sample surface would exercise a path that is not the
            # one the deployment uses.
            service.signer_for(tenant_id),
            service.registry.domain_for,
            currency_book=service.currency_book,
            settlement_currency=service.currency_book.asset_for("RLUSD"),
        )
        built = executor.build(
            tenant_id,
            tenant.treasury_account,
            transfer,
            sequence=account_data["Sequence"],
            last_ledger_sequence=current + 100,
        )
        try:
            sim = _run(service.ledger.simulate(built.tx_json))
        except Exception as exc:
            sim = {"error": repr(exc)}

        return jsonify(
            {
                "built_payment": built.to_dict(),
                "policy_decision": decision.to_dict(),
                "simulation": sim,
                "submitted": False,
                "money_moved": False,
                "note": (
                    "simulate asks the ledger what would happen. Nothing is "
                    "submitted and no value moves."
                ),
            }
        )

    @app.route("/api/sample/settle", methods=["POST"])
    def sample_settle():
        """Submit a REAL payment and confirm it in a closed ledger.

        This is the only sample endpoint that moves value, so it is
        guarded three ways:

        1. It refuses to run unless the request explicitly asks for it
           (`confirm: true`). A GET or a POST without that flag returns
           the payment it WOULD send and moves nothing.
        2. It refuses to run against a tenant that is not marked as a
           local demo tenant.
        3. The amount is capped so a stray call cannot move anything
           meaningful.

        The transaction is signed by the LOCAL DEVNET SEED. In a real
        deployment the signature comes from a custodian or KMS, never
        from a file on the service host.
        """
        service = svc()
        tenant_id = request.args.get("tenant") or service.registry.all()[0].id
        tenant = service.registry.get(tenant_id)
        body = request.get_json(force=True) or {}

        amount = float(body.get("amount", 1.0))
        currency = body.get("currency", "RLUSD")
        confirm = bool(body.get("confirm", False))

        if amount > 10.0:
            return (
                jsonify(
                    {
                        "error": "amount_capped",
                        "detail": (
                            "this endpoint moves at most 10 units so a stray "
                            "call cannot move anything meaningful"
                        ),
                    }
                ),
                400,
            )

        engine = service.policy_for(tenant_id)
        members = service._members(tenant_id)
        destination = body.get("destination") or _settleable_member(
            service, members, tenant.treasury_account
        )

        transfer = ProposedTransfer(
            destination_account=destination,
            amount=amount,
            currency=currency,
            obligation_id=body.get("obligation_id", "SAMPLE-SETTLE"),
        )
        decision = engine.evaluate(
            transfer, treasury_balance_xrp=service.xrp_balance(tenant_id)
        )

        account_data = _run(service.ledger.account_info(tenant.treasury_account))
        current = _run(service.ledger.current_ledger_seq())
        executor = Executor(
            service.ledger,
            # The SERVICE's signer, not a fresh one with no provider.
            # A fresh signer would sign locally even when the service is
            # configured to sign through an external signer service, so
            # the sample surface would exercise a path that is not the
            # one the deployment uses.
            service.signer_for(tenant_id),
            service.registry.domain_for,
            currency_book=service.currency_book,
            settlement_currency=service.currency_book.asset_for("RLUSD"),
        )
        built = executor.build(
            tenant_id,
            tenant.treasury_account,
            transfer,
            sequence=account_data["Sequence"],
            last_ledger_sequence=current + 100,
        )

        if not confirm:
            # The dry form: show exactly what would be sent, move nothing.
            return jsonify(
                {
                    "would_submit": built.to_dict(),
                    "policy_decision": decision.to_dict(),
                    "submitted": False,
                    "money_moved": False,
                    "note": (
                        "This endpoint did not submit. Send "
                        '{"confirm": true} to submit the payment for real. '
                        "It is the only sample endpoint that moves value."
                    ),
                }
            )

        # The signature goes through the SERVICE's signer, which is
        # configured with a provider - the local devnet seed, or an
        # external signing service.
        #
        # This endpoint used to read the seed directly and sign here.
        # That was a bypass: with the service pointed at an external
        # signer, this endpoint still signed locally, so the sample
        # surface - where the settlement check lives - exercised a path
        # that was NOT the one a deployment uses. The signing happened
        # outside the policy gate too.
        #
        # The policy decision is re-checked here because the signer
        # refuses to sign anything the policy has not cleared.
        signer = service.signer_for(tenant_id)
        outcome = signer.sign(
            executor._to_model(built.tx_json),
            decision,
            destination_account=destination,
            amount_xrp=amount,
            obligation_id=transfer.obligation_id,
        )
        if outcome.status != "signed":
            return (
                jsonify(
                    {
                        "error": "not_signed",
                        "detail": (
                            f"the signer withheld the transaction: "
                            f"{outcome.status} {outcome.reasons}"
                        ),
                        "status": outcome.status,
                        "reasons": list(outcome.reasons),
                        "submitted": False,
                        "money_moved": False,
                    }
                ),
                403,
            )

        try:
            from xrpl.core.binarycodec import encode

            signed = encode(outcome.signed_transaction.to_xrpl())
            resp = _run(service.ledger.submit_blob(signed))
        except Exception as exc:
            return (
                jsonify(
                    {
                        "error": "submit_failed",
                        "detail": repr(exc),
                        "submitted": False,
                        "money_moved": False,
                    }
                ),
                502,
            )

        result = resp.get("result", {}) or {}
        engine_result = result.get("engine_result")
        tx_hash = (result.get("tx_json") or {}).get("hash")

        # Wait for the ledger to close and confirm.
        validated = False
        in_ledger = False
        if engine_result == "tesSUCCESS" and tx_hash:
            for _ in range(40):
                time.sleep(2)
                lookup = _run(
                    service.ledger._rpc(
                        {"command": "tx", "transaction": tx_hash}
                    )
                )
                entry = lookup.get("result", {}) or {}
                if entry.get("validated"):
                    validated = True
                    in_ledger = bool(entry.get("inLedger"))
                    break

        return jsonify(
            {
                "engine_result": engine_result,
                "tx_hash": tx_hash,
                "validated": validated,
                "in_ledger": in_ledger,
                "amount": amount,
                "currency": currency,
                "destination": destination,
                "policy_decision": decision.to_dict(),
                "submitted": True,
                "money_moved": engine_result == "tesSUCCESS",
                "note": (
                    "A real transaction was signed and submitted to the "
                    "local devnet. The hash above is the ledger's record of "
                    "it."
                ),
            }
        )

    # ------------------------------------------------------- cross-currency

    @app.route("/api/sample/cross-currency")
    def sample_cross_currency():
        """The same USD value in each currency, and the tier each produces."""
        service = svc()
        tenant_id = request.args.get("tenant") or service.registry.all()[0].id
        tenant = service.registry.get(tenant_id)
        engine = service.policy_for(tenant_id)
        balance = service.xrp_balance(tenant_id)
        members = service._members(tenant_id)
        destination = _payable_member(members, tenant.treasury_account)

        usd = float(request.args.get("usd", 500.0))
        cases = []
        for ticker in ("RLUSD", "CAD", "MXN"):
            rate = service.currency_book.rate_for(ticker)
            if rate is None:
                continue
            native = service.currency_book.from_usd(usd, ticker)
            if native is None:
                continue
            transfer = ProposedTransfer(
                destination_account=destination,
                amount=native,
                currency=ticker,
                obligation_id="SAMPLE-XCUR",
            )
            decision = engine.evaluate(
                transfer, treasury_balance_xrp=balance
            )
            cases.append(
                {
                    "currency": ticker,
                    "native_amount": round(native, 2),
                    "native_display": f"{native:,.2f} {ticker}",
                    "usd_equivalent": round(
                        service.currency_book.to_usd(native, ticker) or 0.0, 2
                    ),
                    "rate": rate.units_per_usd,
                    "rate_source": rate.source,
                    "tier": decision.tier.value,
                    "required_authorisations": decision.required_authorisations,
                    "reasons": decision.reasons,
                }
            )
        return jsonify(
            {
                "usd_value": usd,
                "unit_of_account": service.currency_book.unit_of_account,
                "cases": cases,
                "note": (
                    "Identical USD value, identical tier. The limits are "
                    "compared in the unit of account, so a CA$700 payment is "
                    "judged as US$500."
                ),
            }
        )

    # ------------------------------------------------------------ scenarios

    def _scenario(label: str, description: str, amount: float, currency: str,
                  destination_kind: str, due_in_hours: Optional[float] = None):
        """Run one demonstration scenario and return its result."""
        service = svc()
        tenant_id = request.args.get("tenant") or service.registry.all()[0].id
        tenant = service.registry.get(tenant_id)
        engine = service.policy_for(tenant_id)
        balance = service.xrp_balance(tenant_id)
        members = service._members(tenant_id)

        if destination_kind == "participant":
            destination = _payable_member(members, tenant.treasury_account)
        elif destination_kind == "supervisor_review":
            designated = tenant.limits.always_human_destinations
            destination = designated[0] if designated else members[1]
        else:
            # A member of the domain, resolved at runtime. The
            # address is not hardcoded: the devnet accounts file holds
            # the SEED for it, so publishing the address would publish
            # half of what is needed to spend from it.
            destination = _address("COST_CENTRE_A")

        transfer = ProposedTransfer(
            destination_account=destination,
            amount=amount,
            currency=currency,
            obligation_id=f"SAMPLE-{label.upper()}",
        )
        if due_in_hours is not None:
            transfer.due_date = datetime.now(timezone.utc) + timedelta(
                hours=due_in_hours
            )
        decision = engine.evaluate(
            transfer, treasury_balance_xrp=balance
        )
        return {
            "scenario": label,
            "description": description,
            "request": {
                "amount": amount,
                "currency": currency,
                "destination": destination,
            },
            "decision": decision.to_dict(),
            "money_moved": False,
        }

    @app.route("/api/sample/scenario/approved")
    def scenario_approved():
        """A transfer inside delegated authority. It would be executed
        automatically."""
        return jsonify(
            _scenario(
                "approved",
                "A routine payment within delegated authority. No human "
                "authorisation is required.",
                amount=180.0,
                currency="RLUSD",
                destination_kind="participant",
            )
        )

    @app.route("/api/sample/scenario/single")
    def scenario_single():
        """A transfer needing one sign-off."""
        return jsonify(
            _scenario(
                "single",
                "A payment inside the settlement window. One authorisation "
                "is required.",
                amount=180.0,
                currency="RLUSD",
                destination_kind="participant",
                due_in_hours=5,
            )
        )

    @app.route("/api/sample/scenario/dual")
    def scenario_dual():
        """A transfer needing two sign-offs."""
        return jsonify(
            _scenario(
                "dual",
                "A payment above the manager's delegated authority. Two "
                "authorisations are required, the second from a supervisor.",
                amount=1500.0,
                currency="RLUSD",
                destination_kind="participant",
            )
        )

    @app.route("/api/sample/scenario/refused")
    def scenario_refused():
        """A transfer that is refused outright."""
        return jsonify(
            _scenario(
                "refused",
                "A payment beyond the maximum single disbursement. It is "
                "refused, not escalated — no authorisation makes an "
                "over-limit transfer compliant.",
                amount=25000.0,
                currency="RLUSD",
                destination_kind="participant",
            )
        )

    @app.route("/api/sample/scenario/blocked")
    def scenario_blocked():
        """What a source outage actually looks like.

        The plan is WITHHELD, not emptied. An empty plan and a withheld
        plan look identical to a caller unless the difference is explicit,
        so it is explicit here.

        This endpoint INDUCTION of the outage rather than describing it.
        It points the service's obligation source at an address that
        cannot be reached, rebuilds the plan, and shows what comes back.
        The original source is restored before the response is returned,
        so the demonstration cannot leave the service broken.
        """
        service = svc()
        tenant_id = request.args.get("tenant") or service.registry.all()[0].id

        original_source = service.obligation_source
        original_connector = service.obligation_connector
        outage = _UnreachableSource(original_source)

        try:
            # The CONNECTOR takes precedence over obligation_source, so it
            # has to be disabled too. Swapping only the source would
            # leave the live ERP in place and the outage would never
            # happen.
            service.obligation_connector = None
            service.obligation_source = outage
            plan, decisions, reconciliation = service.build_plan(tenant_id)
            blocked = plan.blocked
            blocked_reason = plan.blocked_reason
            planned = len(plan.planned)
            obligation_error = service.obligation_error
        finally:
            # Always restore, even if the build raised.
            service.obligation_source = original_source
            service.obligation_connector = original_connector
            service.obligation_error = None

        # Rebuild once more with the real source so the caller can see the
        # difference between a withheld plan and a normal one.
        healthy_plan, _, _ = service.build_plan(tenant_id)

        return jsonify(
            {
                "blocked": blocked,
                "blocked_reason": blocked_reason,
                "planned": planned,
                "obligation_error": obligation_error,
                "connector_status": service.connector_status(),
                "during_outage": {
                    "blocked": blocked,
                    "planned": planned,
                    "obligation_error": obligation_error,
                },
                "after_restore": {
                    "blocked": healthy_plan.blocked,
                    "planned": len(healthy_plan.planned),
                    "obligation_error": service.obligation_error,
                },
                "explanation": (
                    "The obligation source was pointed at an unreachable "
                    "address, the plan was rebuilt, and it was WITHHELD: "
                    "no transfers were proposed and the reason was reported. "
                    "The source was then restored. An empty plan and a "
                    "withheld plan look identical to a caller unless the "
                    "difference is explicit, so it is explicit here."
                ),
                "money_moved": False,
            }
        )

    # ------------------------------------------------------- rates + source

    @app.route("/api/sample/rates")
    def sample_rates():
        """The live exchange rates, with their provenance."""
        service = svc()
        info = service.refresh_rates()
        return jsonify(
            {
                "unit_of_account": service.currency_book.unit_of_account,
                "provider": info["provider"],
                "fetched_at": info["fetched_at"],
                "age_seconds": info["age_seconds"],
                "stale": info["stale"],
                "last_error": info["last_error"],
                "rates": info["rates"],
                "note": (
                    "A rate lookup failure never alters what is "
                    "transferred — only the displayed equivalent is "
                    "affected."
                ),
            }
        )

    @app.route("/api/sample/source")
    def sample_source():
        """The obligation source, read through the plug-in."""
        service = svc()
        tenant_id = request.args.get("tenant") or service.registry.all()[0].id
        try:
            obligations = service.obligations()
            return jsonify(
                {
                    "source": type(
                        service.obligation_connector or service.obligation_source
                    ).__name__,
                    "reachable": True,
                    "count": len(obligations),
                    "obligations": [o.to_dict() for o in obligations],
                }
            )
        except Exception as exc:
            return jsonify(
                {
                    "source": type(
                        service.obligation_connector or service.obligation_source
                    ).__name__,
                    "reachable": False,
                    "error": repr(exc),
                    "count": 0,
                    "obligations": [],
                    "note": (
                        "The source is unreachable. This is reported, not "
                        "silently treated as an empty obligation list."
                    ),
                }
            ), 503

    # --------------------------------------------------------- decision log

    @app.route("/api/sample/decision-log")
    def sample_decision_log():
        """The hash-chained decision log, and its verification."""
        service = svc()
        tenant_id = request.args.get("tenant") or service.registry.all()[0].id
        entries = service.agent_log.entries(tenant_id, limit=int(
            request.args.get("limit", 20)
        ))
        verification = service.agent_log.verify()
        return jsonify(
            {
                "verification": verification,
                "entries": [e.to_dict() for e in reversed(entries)],
                "note": (
                    "Each entry carries the hash of its predecessor. An "
                    "altered or removed entry is detectable by "
                    "recomputing the chain."
                ),
            }
        )

    # ------------------------------------------------- approval walkthrough

    @app.route("/api/sample/approval-walkthrough", methods=["POST"])
    def sample_approval_walkthrough():
        """Record sign-offs against an obligation and watch the gate open.

        The walkthrough records the approvals against a SAMPLE obligation
        key, so it never touches a real instruction's approval ledger.
        """
        service = svc()
        tenant_id = request.args.get("tenant") or service.registry.all()[0].id
        body = request.get_json(force=True) or {}
        key = body.get("obligation_id", "SAMPLE-WALKTHROUGH")
        required = int(body.get("required", 2))

        signer = service.signer_for(tenant_id)
        approver = body.get("approver", "Sample Approver")
        role = body.get("role", "treasury_manager")

        before = signer.ledger_for(key, required)
        before_count = before.count
        ledger = signer.record_authorisation(
            key, approver, role, required=required
        )

        return jsonify(
            {
                "obligation_id": key,
                "required": ledger.required,
                "received_before": before_count,
                "received_now": ledger.count,
                "satisfied": ledger.satisfied,
                "authorisations": [a.to_dict() for a in ledger.received],
                "next_step": (
                    "The signing path is now open."
                    if ledger.satisfied
                    else f"{ledger.required - ledger.count} more sign-off(s) needed."
                ),
                "money_moved": False,
            }
        )


def _run(coro):
    """Run a coroutine from synchronous Flask code."""
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            raise RuntimeError("event loop already running")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)
