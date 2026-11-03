"""
The treasury service: HTTP API + dashboard.

The API surface is a security boundary. Whoever can call the transfer
endpoint can cause money movement inside the domain, so the endpoint is
authenticated and every transfer still passes the policy engine.

The dashboard is the operator view: treasury position, obligations, the
policy envelope, the proposed plan, and the decision log.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
from datetime import datetime, timezone
from typing import Dict, List, Optional

from flask import Flask, jsonify, render_template, request

from .agent_log import AgentLog
from .connectors import ConnectionStatus, ConnectorError, ConnectorRegistry
from .currency import CurrencyBook, default_currency_book
from .executor import Executor, LedgerClient
from .obligations import Obligation
from .planner import Plan, Planner
from .policy import ApprovalMatrix, PolicyEngine, Tier
from .rates import RateService, default_rate_service
from .signer import TreasurySigner
from .tenants import TenantRegistry

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent.parent


def _run(coro):
    """Run a coroutine from synchronous Flask code."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            raise RuntimeError("event loop already running")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


class TreasuryService:
    """Holds the service's state and the components wired together."""

    def __init__(
        self,
        registry: TenantRegistry,
        ledger: LedgerClient,
        obligation_source,
        agent_log: AgentLog,
        signer_provider=None,
        state_dir: Optional[pathlib.Path] = None,
        currency_book: Optional[CurrencyBook] = None,
        rate_service: Optional[RateService] = None,
        connectors: Optional[ConnectorRegistry] = None,
    ):
        self.registry = registry
        self.ledger = ledger
        self.currency_book = currency_book or default_currency_book()
        # The live rate feed. A rate lookup never blocks a payment; it only
        # affects the displayed equivalent.
        self.rate_service = rate_service or default_rate_service()
        # The configured external connectors: ERP, wallets, custody APIs.
        self.connectors = connectors or ConnectorRegistry()
        # The ERP connector, when one is configured. A failure here BLOCKS
        # the plan rather than looking like an empty obligation list.
        self.obligation_connector = None
        self.obligation_error: Optional[str] = None
        self.obligation_source = obligation_source
        self.agent_log = agent_log
        self.state_dir = state_dir or (PROJECT / "var")
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self._signer_provider = signer_provider
        self._engines: Dict[str, PolicyEngine] = {}
        self._member_cache: Dict[str, List[str]] = {}
        # tenant_id -> TreasurySigner (holds the approval ledgers)
        self._signers: Dict[str, TreasurySigner] = {}

    def refresh_rates(self, force: bool = False) -> Dict:
        """Pull live exchange rates into the currency book.

        The currency book is the thing the policy engine and the UI read, so
        the rates land there rather than in a parallel structure that could
        drift from it.
        """
        tickers = list(self.currency_book.assets.keys())
        cached = self.rate_service.get(tickers, force_refresh=force)
        for ticker, rate in cached.rates.items():
            self.currency_book.set_rate(rate)
        return {
            "provider": cached.provider,
            "fetched_at": cached.fetched_at,
            "age_seconds": round(cached.age_seconds(), 1),
            "stale": cached.is_stale(self.rate_service.max_age_seconds),
            "last_error": self.rate_service.last_error,
            "rates": {k: v.to_dict() for k, v in cached.rates.items()},
        }

    def signer_for(self, tenant_id: str) -> TreasurySigner:
        """The signer and approval ledger for a tenant."""
        if tenant_id not in self._signers:
            self._signers[tenant_id] = TreasurySigner(
                self._signer_provider, self.policy_for(tenant_id)
            )
        return self._signers[tenant_id]

    # ------------------------------------------------------------- helpers

    def policy_for(self, tenant_id: str) -> PolicyEngine:
        if tenant_id not in self._engines:
            tenant = self.registry.get(tenant_id)
            self._engines[tenant_id] = PolicyEngine(
                tenant,
                self._members(tenant_id),
                ApprovalMatrix(
                    level_2_authorisations=tenant.level_2_authorisations,
                    level_3_authorisations=tenant.level_3_authorisations,
                ),
                currency_book=self.currency_book,
            )
        return self._engines[tenant_id]

    def _members(self, tenant_id: str) -> List[str]:
        """Domain members, read from the ledger."""
        if tenant_id in self._member_cache:
            return self._member_cache[tenant_id]
        tenant = self.registry.get(tenant_id)
        try:
            members = _run(
                self.ledger.domain_members(tenant.domain_id, tenant.treasury_account)
            )
        except Exception:
            members = list(tenant.cost_centres.keys())
        self._member_cache[tenant_id] = members
        return members

    def refresh_members(self, tenant_id: str) -> List[str]:
        self._member_cache.pop(tenant_id, None)
        self._engines.pop(tenant_id, None)
        return self._members(tenant_id)

    def settlement_balance(self, tenant_id: str) -> float:
        """The treasury's holding of its settlement currency (RLUSD).

        This is what the treasury SPENDS. It is not the XRP balance:
        XRP pays fees, and the two are tracked separately.
        """
        tenant = self.registry.get(tenant_id)
        asset = self.currency_book.asset_for("RLUSD")
        if asset is None:
            return 0.0
        try:
            return _run(self.ledger.issued_balance(tenant.treasury_account, asset))
        except Exception as exc:
            # A balance that cannot be read is NOT a zero balance. Report it,
            # because a zero would silently disable the policy floor.
            self.last_balance_error = repr(exc)
            return 0.0

    def xrp_balance(self, tenant_id: str) -> float:
        """The treasury's XRP balance.

        XRP pays transaction fees. The minimum-balance floor is on XRP,
        because a treasury below its XRP floor cannot reliably submit
        the next transaction at all.
        """
        tenant = self.registry.get(tenant_id)
        try:
            return _run(self.ledger.balance_xrp(tenant.treasury_account))
        except Exception as exc:
            self.last_balance_error = repr(exc)
            return 0.0

    def positions(self, tenant_id: str) -> List[Dict]:
        """Every currency position the treasury holds, read from the ledger."""
        tenant = self.registry.get(tenant_id)
        positions = []
        for ticker, asset in self.currency_book.assets.items():
            try:
                amount = _run(
                    self.ledger.issued_balance(tenant.treasury_account, asset)
                )
            except Exception:
                amount = 0.0
            presented = self.currency_book.present(amount, ticker)
            positions.append(
                {
                    "ticker": ticker,
                    "asset": asset.to_dict(),
                    "amount": amount,
                    "display": presented["display"],
                    "usd_equivalent": presented.get("unit_of_account_display"),
                    "usd_amount": presented.get("unit_of_account_amount"),
                    "is_estimate": presented.get("is_estimate", False),
                }
            )
        return positions

    def consolidated_value_xrp(self, tenant_id: str) -> float:
        """The treasury's total position, expressed in the unit of account.

        This is what the operator actually reads: one number. It is the sum
        of each currency position converted at the quoted rate, and it is
        marked as an estimate because the rates can move.
        """
        total = 0.0
        for position in self.positions(tenant_id):
            if position.get("usd_amount") is not None:
                total += position["usd_amount"]
        return total

    def obligations(self) -> List[Obligation]:
        """The obligations, from the ERP connector if one is configured.

        A connector failure is recorded and raised. It must NOT degrade to
        an empty list: a treasury that sees "no obligations" because the
        ERP was down will plan nothing and look perfectly healthy.
        """
        if self.obligation_connector is not None:
            try:
                result = self.obligation_connector.fetch()
                self.obligation_error = None
                return result
            except Exception as exc:
                self.obligation_error = repr(exc)
                raise
        # The SOURCE path must record its failure the same way the connector
        # path does. Otherwise obligation_error stays None whenever the
        # source - rather than the connector - is what failed, and a blocked
        # plan reports no reason for being blocked.
        try:
            result = self.obligation_source.fetch()
            self.obligation_error = None
            return result
        except Exception as exc:
            self.obligation_error = repr(exc)
            raise

    def connector_status(self) -> List[Dict]:
        """The health of every configured connector.

        Each connector is probed if it has a `health` method; otherwise it
        is reported as configured-but-unprobed. A connector that cannot be
        reached is reported with the reason, never omitted — an absent
        connector and a broken one look identical otherwise.
        """
        status: List[Dict] = []
        for entry in self.connectors.describe():
            connector = self.connectors.get(entry["name"])
            health = getattr(connector, "health", None)
            if callable(health):
                try:
                    result = health()
                    status.append(
                        ConnectionStatus(
                            name=entry["name"],
                            kind=entry["kind"],
                            reachable=bool(result.get("reachable", True)),
                            detail=str(result.get("detail", "")),
                        ).to_dict()
                    )
                except Exception as exc:
                    status.append(
                        ConnectionStatus(
                            name=entry["name"],
                            kind=entry["kind"],
                            reachable=False,
                            detail=repr(exc)[:200],
                        ).to_dict()
                    )
            else:
                status.append(
                    ConnectionStatus(
                        name=entry["name"],
                        kind=entry["kind"],
                        reachable=True,
                        detail="configured (no health probe implemented)",
                    ).to_dict()
                )
        return status

    # ----------------------------------------------------------- the plan

    def build_plan(self, tenant_id: str):
        tenant = self.registry.get(tenant_id)
        # Refresh rates so the displayed equivalents are current. This is
        # cheap (cached for an hour) and a failure leaves the last good
        # rates in place.
        self.refresh_rates()
        xrp_balance = self.xrp_balance(tenant_id)
        settlement_balance = self.settlement_balance(tenant_id)
        try:
            obligations = self.obligations()
        except Exception as exc:
            # The ERP is unreachable. The plan is withheld, because an empty
            # obligation list looks identical to "nothing is due" and would
            # let the service look healthy while blind.
            plan = Plan(
                tenant_id=tenant_id,
                treasury_balance_xrp=xrp_balance,
                already_committed_today_xrp=0.0,
            )
            plan.blocked = True
            plan.blocked_reason = (
                f"The obligation source could not be reached: {exc!r}. "
                f"No disbursement instructions are proposed while the "
                f"source of obligations is unavailable."
            )
            return plan, [], None
        engine = self.policy_for(tenant_id)

        planner = Planner(
            buffer_hours=tenant.limits.due_date_buffer_hours,
            min_balance_xrp=tenant.limits.min_treasury_balance_xrp,
            currency_book=self.currency_book,
        )
        # Reconciliation runs first: the planner must not propose a transfer
        # for something the ledger already shows as funded.
        reconciler_report = self._reconcile(tenant_id, obligations)

        if reconciler_report.blocked:
            # The ledger view could not be established. Proposing anything
            # on an unknown view risks double-funding, so the plan is
            # withheld and the exception is surfaced to the operator.
            plan = Plan(
                tenant_id=tenant_id,
                treasury_balance_xrp=xrp_balance,
                already_committed_today_xrp=0.0,
            )
            # The plan carries the blocked signal. Without it, a caller
            # checking `plan.blocked` got False on a ledger outage -
            # indistinguishable from a normal plan with nothing due.
            #
            # The ERP branch below sets the same two fields. Both failure
            # modes must be visible the same way, or "blocked" means
            # two different things depending on which dependency failed.
            plan.blocked = True
            plan.blocked_reason = (
                f"{reconciler_report.blocked_reason} "
                f"No disbursement instructions are proposed while the "
                f"ledger view is unavailable."
            )
            for obligation in obligations:
                plan.skipped.append(
                    {
                        "obligation_id": obligation.id,
                        "description": obligation.description,
                        "reason": "reconciliation_blocked",
                        "amount": obligation.amount,
                        "currency": obligation.currency,
                    }
                )
            return plan, [], reconciler_report

        planner.set_already_funded(reconciler_report.funded_obligation_ids)

        plan = planner.plan(
            tenant_id=tenant_id,
            obligations=obligations,
            treasury_balance_xrp=xrp_balance,
            settlement_balance=settlement_balance,
        )

        decisions = engine.evaluate_all(
            [p.transfer for p in plan.planned],
            treasury_balance_xrp=xrp_balance,
        )
        for planned, decision in zip(plan.planned, decisions):
            planned.notes.extend(decision.reasons)
            planned.funded_already = decision.tier is Tier.REFUSED
            planned.transfer.__dict__["_tier"] = decision.tier.value
            planned.transfer.__dict__["_required_authorisations"] = (
                decision.required_authorisations
            )

        return plan, decisions, reconciler_report

    def _reconcile(self, tenant_id: str, obligations: List[Obligation]):
        from .reconcile import ReconciliationReport, Reconciler

        tenant = self.registry.get(tenant_id)
        reconciler = Reconciler()
        try:
            return _run(
                reconciler.reconcile(
                    tenant_id, obligations, self.ledger, tenant.treasury_account
                )
            )
        except Exception:
            return ReconciliationReport(tenant_id=tenant_id)


def create_app(
    service: TreasuryService,
    sample_mode: bool = False,
    auth_store=None,
) -> Flask:
    """Build the Flask application.

    `sample_mode` registers the demonstration endpoints under
    `/api/sample/`. They are namespaced and clearly separated from the
    operational surface.

    `auth_store` holds the API credentials. If it is None, the
    credentials are loaded from the environment. An empty store means
    every protected route is refused - a service started without
    credentials is not a service with open doors, it is a service that
    says no.
    """
    app = Flask(
        __name__,
        template_folder=str(HERE / "templates"),
        static_folder=str(HERE / "static"),
    )
    app.config["SERVICE"] = service

    # ------------------------------------------------------------- auth
    from .auth import CredentialStore
    from .auth_middleware import register_auth
    from .tenants import TenantError

    store = auth_store if auth_store is not None else CredentialStore.from_environment()
    register_auth(app, store, sample_mode=sample_mode)

    # ------------------------------------------------- error handlers
    #
    # An unknown tenant is a 404, not a 500.
    #
    # Every route that takes a tenant id calls registry.get(), which
    # raises TenantError for a tenant that is not configured. Without a
    # handler that exception became a 500 with an HTML body - which
    # says "the server is broken" when the truth is "that tenant does
    # not exist". It also leaked a stack trace.
    @app.errorhandler(TenantError)
    def _tenant_not_found(exc):
        return (
            jsonify(
                {
                    "error": "unknown_tenant",
                    "detail": str(exc),
                }
            ),
            404,
        )

    # A 404 for a route that does not exist, in the same shape as every
    # other error the API returns. The default is HTML.
    @app.errorhandler(404)
    def _route_not_found(exc):
        return (
            jsonify(
                {
                    "error": "not_found",
                    "detail": "no such endpoint",
                }
            ),
            404,
        )

    # A 405 for a wrong method, in the same shape.
    @app.errorhandler(405)
    def _method_not_allowed(exc):
        return (
            jsonify(
                {
                    "error": "method_not_allowed",
                    "detail": "that method is not allowed on this endpoint",
                }
            ),
            405,
        )

    @app.template_filter("xrp")
    def xrp_filter(value):
        """Format an XRP amount with thousands separators.

        `25,000.01` reads correctly; `25000.01` does not in a financial
        financial interface.
        """
        try:
            return "{:,.2f}".format(float(value))
        except (TypeError, ValueError):
            return value

    @app.template_filter("count")
    def count_filter(value, singular="item", plural=None):
        """Label a count with its unit: `1 request` / `2 requests`."""
        try:
            n = int(value)
        except (TypeError, ValueError):
            return value
        if plural is None:
            plural = singular + "s"
        return f"{n:,} {singular if n == 1 else plural}"

    def svc() -> TreasuryService:
        return app.config["SERVICE"]

    # --------------------------------------------------------------- pages

    @app.route("/")
    def dashboard():
        service = svc()
        tenant_id = request.args.get("tenant") or service.registry.all()[0].id
        tenant = service.registry.get(tenant_id)

        plan, decisions, reconciliation = service.build_plan(tenant_id)
        members = service._members(tenant_id)
        signer = service.signer_for(tenant_id)
        approvals = {k: v.to_dict() for k, v in signer.approvals.items()}

        # A blocked plan has no reconciliation report. The template still
        # needs one to render, so an empty report stands in - and the
        # blocked banner explains the real situation.
        if reconciliation is None:
            from .reconcile import ReconciliationReport

            reconciliation = ReconciliationReport(tenant_id=tenant_id)
            reconciliation.blocked = True
            reconciliation.ledger_exceptions.append(
                {
                    "kind": "obligation_source_unavailable",
                    "detail": service.obligation_error or "obligation source unavailable",
                }
            )

        # Currency positions, read from the ledger.
        positions = service.positions(tenant_id)
        consolidated_usd = service.consolidated_value_xrp(tenant_id)

        # Per-transfer USD equivalents, and the plan's USD total.
        plan_usd_total = 0.0
        currencies_in_plan = set()
        for planned in plan.planned:
            ticker = planned.transfer.currency
            currencies_in_plan.add(ticker)
            usd = service.currency_book.to_usd(
                planned.transfer.amount, ticker
            )
            planned.usd_equivalent = usd
            if usd is not None:
                plan_usd_total += usd
        # The obligations come from the ERP connector, which may be down.
        # The plan already fetched them (or recorded why it could not), so
        # reuse that rather than making a second call that could fail
        # differently. A dashboard that 500s when the ERP is down is a
        # dashboard that cannot tell the operator the ERP is down.
        obligations = []
        if not plan.blocked:
            try:
                obligations = service.obligations()
            except Exception:
                obligations = []
        else:
            obligations = []
        log_entries = service.agent_log.entries(tenant_id, limit=40)

        # Tier counts across the plan.
        tier_counts = {"level_1": 0, "level_2": 0, "level_3": 0, "refused": 0}
        for decision in decisions:
            tier_counts[decision.tier.value] = tier_counts.get(decision.tier.value, 0) + 1

        recent = []
        for entry in reversed(log_entries):
            recent.append(entry.to_dict())

        return render_template(
            "dashboard.html",
            tenant=tenant,
            tenants=service.registry.all(),
            tenants_count=len(service.registry),
            plan=plan,
            decisions=decisions,
            reconciliation=reconciliation,
            members=members,
            obligations=obligations,
            tier_counts=tier_counts,
            approvals=approvals,
            positions=positions,
            consolidated_usd=consolidated_usd,
            connectors=service.connector_status(),
            rate_status=service.rate_service.to_dict(),
            plan_usd_total=plan_usd_total,
            currencies_in_plan=len(currencies_in_plan),
            log_entries=recent,
            log_verified=service.agent_log.verify(),
            balance=service.settlement_balance(tenant_id),
            now=datetime.now(timezone.utc),
        )

    # ----------------------------------------------------------------- API

    @app.route("/api/health")
    def api_health():
        service = svc()
        # Refresh the rates so health never reports a stale or empty cache.
        # The refresh is cached for an hour, so this is cheap.
        service.refresh_rates()
        return jsonify(
            {
                "status": "ok",
                "version": "0.1.0",
                "tenants": len(service.registry),
                "time": datetime.now(timezone.utc).isoformat(),
                "connectors": service.connector_status(),
                "rates": service.rate_service.to_dict(),
            }
        )

    @app.route("/api/connectors")
    def api_connectors():
        return jsonify(
            {
                "connectors": svc().connector_status(),
                "obligation_source": (
                    type(svc().obligation_connector).__name__
                    if svc().obligation_connector is not None
                    else type(svc().obligation_source).__name__
                ),
                "obligation_error": svc().obligation_error,
            }
        )

    @app.route("/api/tenants")
    def api_tenants():
        return jsonify(
            {"tenants": [t.to_dict() for t in svc().registry.all()]}
        )

    @app.route("/api/tenant/<tenant_id>/position")
    def api_position(tenant_id: str):
        service = svc()
        tenant = service.registry.get(tenant_id)
        return jsonify(
            {
                "tenant": tenant.to_dict(),
                "settlement_currency": "RLUSD",
                "treasury_balance_xrp": service.xrp_balance(tenant_id),
                "treasury_settlement_balance": service.settlement_balance(
                    tenant_id
                ),
                "consolidated_value_usd": service.consolidated_value_xrp(tenant_id),
                "positions": service.positions(tenant_id),
                "currency_book": service.currency_book.to_dict(),
                "rates": service.refresh_rates(),
                "domain_members": service._members(tenant_id),
                "limits": tenant.limits.to_dict(),
            }
        )

    @app.route("/api/tenant/<tenant_id>/plan")
    def api_plan(tenant_id: str):
        service = svc()
        plan, decisions, reconciliation = service.build_plan(tenant_id)
        return jsonify(
            {
                "plan": plan.to_dict(),
                "decisions": [d.to_dict() for d in decisions],
                # A blocked plan has no reconciliation report - the ledger or
                # the ERP could not be read, so there is nothing to report.
                "reconciliation": (
                    reconciliation.to_dict() if reconciliation is not None else None
                ),
                "obligation_error": service.obligation_error,
            }
        )

    @app.route("/api/tenant/<tenant_id>/obligations")
    def api_obligations(tenant_id: str):
        # Resolve the tenant FIRST. Without this, the route answered for a
        # tenant that is not configured - and answered with the real
        # obligations, which belong to whichever tenant does exist.
        service = svc()
        service.registry.get(tenant_id)
        return jsonify(
            {"obligations": [o.to_dict() for o in service.obligations()]}
        )

    @app.route("/api/tenant/<tenant_id>/log")
    def api_log(tenant_id: str):
        service = svc()
        # Resolve the tenant FIRST, for the same reason.
        service.registry.get(tenant_id)
        return jsonify(
            {
                "entries": [e.to_dict() for e in service.agent_log.entries(tenant_id)],
                # The chain verification is for the whole log, not for one
                # tenant - the chain is the chain. It reports no entry
                # content.
                "verification": service.agent_log.verify(),
            }
        )

    @app.route("/api/tenant/<tenant_id>/dry-run", methods=["POST"])
    def api_dry_run(tenant_id: str):
        """Dry-run one transfer. Never submits anything."""
        service = svc()
        tenant = service.registry.get(tenant_id)
        body = request.get_json(force=True) or {}

        from .policy import ProposedTransfer

        transfer = ProposedTransfer(
            destination_account=body.get("destination", ""),
            amount=float(body.get("amount", 0)),
            currency=body.get("currency", "RLUSD"),
            due_date=None,
            entity=body.get("entity", ""),
            obligation_id=body.get("obligation_id", ""),
            reference=body.get("reference", "manual dry run"),
        )

        engine = service.policy_for(tenant_id)
        xrp_balance = service.xrp_balance(tenant_id)
        decision = engine.evaluate(
            transfer, treasury_balance_xrp=xrp_balance
        )

        executor = Executor(
            service.ledger,
            # The SERVICE's signer, not a fresh one with no provider. A
            # fresh signer would sign locally even when the service is
            # configured to sign through an external signer, so this
            # endpoint would exercise a path that is not the one a
            # deployment uses.
            service.signer_for(tenant_id),
            service.registry.domain_for,
            currency_book=service.currency_book,
            settlement_currency=service.currency_book.asset_for("RLUSD"),
        )
        try:
            seq = _run(service.ledger.account_info(tenant.treasury_account))["Sequence"]
            current = _run(service.ledger.current_ledger_seq())
            result = _run(
                executor.dry_run(
                    tenant_id,
                    tenant.treasury_account,
                    transfer,
                    sequence=seq,
                    last_ledger_sequence=current + 100,
                    balance_before_xrp=xrp_balance,
                )
            )
            payload = result.to_dict()
        except Exception as e:
            # A dry run that cannot be performed is a FAILURE, not a
            # result. Returning 200 with an error body made this
            # endpoint silently non-functional: it answered every
            # request, and every answer was an error. The status has to
            # say so.
            payload = {"error": repr(e)}
            payload["dry_run_failed"] = True

        payload["policy_decision"] = decision.to_dict()
        service.agent_log.append(
            tenant_id,
            "dry_run",
            {
                "destination": transfer.destination_account,
                "amount_xrp": transfer.amount_xrp,
                "tier": decision.tier.value,
                "engine_result": payload.get("engine_result"),
            },
        )
        return jsonify(payload)

    @app.route("/api/tenant/<tenant_id>/authorisations")
    def api_authorisations(tenant_id: str):
        """The sign-offs recorded against transfers requiring authorisation."""
        service = svc()
        signer = service.signer_for(tenant_id)
        return jsonify(
            {
                "authorisations": {
                    key: ledger.to_dict() for key, ledger in signer.approvals.items()
                }
            }
        )

    @app.route(
        "/api/tenant/<tenant_id>/authorise/<obligation_id>", methods=["POST"]
    )
    def api_authorise(tenant_id: str, obligation_id: str):
        """Record one sign-off against a transfer.

        The required number of sign-offs is determined by the policy engine
        for that transfer's tier. The caller supplies WHO and WHAT ROLE; the
        service decides how many are still needed.
        """
        service = svc()
        body = request.get_json(force=True) or {}
        signer = service.signer_for(tenant_id)
        engine = service.policy_for(tenant_id)

        # The approver check comes FIRST. It is a check on the request,
        # and it needs no ledger and no plan - so a malformed request is
        # answered with a 400 immediately, rather than after the service
        # has tried to read the ledger and possibly failed.
        approver = str(body.get("approver", "") or "").strip()
        if not approver:
            # A sign-off with no named approver is unattributable. It
            # would sit in the audit log as a sign-off by nobody, and
            # the approval ledger counts it as a real sign-off.
            return (
                jsonify(
                    {
                        "error": "approver_required",
                        "detail": (
                            "an authorisation must name the person making "
                            'it. send {"approver": "name"}.'
                        ),
                    }
                ),
                400,
            )

        # Find the transfer and its tier so we know how many sign-offs are
        # required before we accept this one.
        plan, decisions, _ = service.build_plan(tenant_id)
        required = 0
        tier = None
        found = False
        for planned, decision in zip(plan.planned, decisions):
            if planned.transfer.obligation_id == obligation_id:
                required = decision.required_authorisations
                tier = decision.tier.value
                found = True
                break

        if not found:
            # The obligation is not in the plan, so the policy has no tier
            # for it and the required number of sign-offs is unknown.
            #
            # Recording it anyway created a ledger with required=0, which
            # reported `satisfied: true` on the FIRST sign-off - for an
            # obligation that was never in any plan. The sign path
            # upgrades `required` to the policy's count later, so this
            # was a misleading report rather than a money bug. But a
            # sign-off against nothing is still not a sign-off.
            return (
                jsonify(
                    {
                        "error": "obligation_not_in_plan",
                        "detail": (
                            f"obligation {obligation_id!r} is not in the "
                            f"current plan for tenant {tenant_id!r}, so the "
                            f"policy has no tier for it and no required "
                            f"number of sign-offs can be determined."
                        ),
                        "tenant_id": tenant_id,
                        "obligation_id": obligation_id,
                    }
                ),
                404,
            )

        ledger = signer.record_authorisation(
            obligation_id,
            approver=approver,
            role=body.get("role", "treasury_manager"),
            comment=body.get("comment", ""),
            required=required,
        )
        service.agent_log.append(
            tenant_id,
            "authorisation_recorded",
            {
                "obligation_id": obligation_id,
                "approver": approver,
                "role": body.get("role", "treasury_manager"),
                "tier": tier,
                "received": ledger.count,
                "required": ledger.required,
            },
        )
        return jsonify(
            {
                "obligation_id": obligation_id,
                "tier": tier,
                "ledger": ledger.to_dict(),
                "satisfied": ledger.satisfied,
            }
        )

    if sample_mode:
        from .sample_api import register_sample_endpoints

        register_sample_endpoints(app, service)

    return app
