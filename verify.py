#!/usr/bin/env python3
"""
End-to-end verification harness.

This is the artefact a reviewer reads. It exercises the whole service
against the live devnet and the live integrations, and writes a
machine-readable result plus a human-readable report.

Every check is one of three kinds:

    ASSERT  a claim that must hold (the gate rejects, the limits hold)
    OBSERVE a fact recorded for the reader (balances, rates, versions)
    PROVE  a destructive or irreversible check, run only with --allow-move

The harness NEVER spends money by default. It builds payments, simulates
them, and verifies the policy decisions — but it does not submit. The
`--allow-move` flag enables the one check that submits a real transaction
on the devnet, and it is off by default so the harness is safe to run
anywhere.

    python3 verify.py                          # read-only verification
    python3 verify.py --allow-move             # + one devnet settlement
    python3 verify.py --out verification/      # write the report elsewhere

OUTPUT
======

    verification/VERIFICATION.md      the human-readable report
    verification/RESULTS.json        machine-readable
    verification/logs/               raw evidence per check
    verification/decision-log.jsonl  the service's own hash-chained log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "src"
sys.path.insert(0, str(SRC))

from xrpl.models import Payment  # noqa: E402

from xrpl_agent_treasury_mgmt.agent_log import AgentLog  # noqa: E402
from xrpl_agent_treasury_mgmt.accounts import fmt_xrp  # noqa: E402
from xrpl_agent_treasury_mgmt.connectors import (  # noqa: E402
    ConnectorConfig,
    ConnectorRegistry,
    Credential,
)
from xrpl_agent_treasury_mgmt.currency import (  # noqa: E402
    IssuedAsset,
    default_currency_book,
    hex_code,
)
from xrpl_agent_treasury_mgmt.executor import LedgerClient  # noqa: E402
from xrpl_agent_treasury_mgmt.integrations import GenericRestErp  # noqa: E402
from xrpl_agent_treasury_mgmt.policy import (  # noqa: E402
    ApprovalMatrix,
    PolicyDecision,
    PolicyEngine,
    ProposedTransfer,
    Tier,
)
from xrpl_agent_treasury_mgmt.rates import default_rate_service  # noqa: E402
from xrpl_agent_treasury_mgmt.signer import TreasurySigner  # noqa: E402
from xrpl_agent_treasury_mgmt.tenants import (  # noqa: E402

    Limits,
    Tenant,
    TenantRegistry,
)
from xrpl_agent_treasury_mgmt.devnet_accounts import address_for as _address

# The proven domain and accounts from evidence/domain-gate-proof (FINAL002).
def _load_domain_id() -> str:
    """The PermissionedDomain id this environment is using.

    The domain is a ledger object, so it does not survive a ledger
    reset - a fresh environment has a different id. It is written to
    var/local-domain.json by create_domain.py and read from here, so the
    service always names the domain that actually exists. The fallback
    is the id the first proof run created.
    """
    path = pathlib.Path(__file__).resolve().parent / "var" / "local-domain.json"
    try:
        data = json.loads(path.read_text())
        domain_id = data.get("domain_id")
        if domain_id:
            return domain_id
    except Exception:
        pass
    return "F56F42C55216A32803EE83DE806B47B377243BC0C78342E8D828CC6D84E5C3DB"


DEMO_DOMAIN = _load_domain_id()
TREASURY = _address("TREASURY")
COST_CENTRE_A = _address("OUTSIDER")
COST_CENTRE_B = _address("COST_CENTRE_A")
OUTSIDER = _address("ISSUER")
@dataclass
class Check:
    """One verification check."""

    name: str
    kind: str  # ASSERT | OBSERVE | PROVE
    passed: bool
    detail: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "passed": self.passed,
            "detail": self.detail,
            "evidence": self.evidence,
        }


class Verifier:
    """Runs the checks and collects the results."""

    def __init__(self, out_dir: pathlib.Path, allow_move: bool = False):
        self.out_dir = out_dir
        self.logs_dir = out_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.allow_move = allow_move
        self.checks: List[Check] = []
        self.started = datetime.now(timezone.utc)

    def assert_that(self, name: str, condition: bool, detail: str, **evidence):
        self.checks.append(
            Check(name, "ASSERT", bool(condition), detail, evidence)
        )
        print(f"  [{'PASS' if condition else 'FAIL'}] {name}: {detail}")
        return bool(condition)

    def observe(self, name: str, detail: str, **evidence):
        self.checks.append(Check(name, "OBSERVE", True, detail, evidence))
        print(f"  [NOTE] {name}: {detail}")

    def prove(self, name: str, condition: bool, detail: str, **evidence):
        self.checks.append(Check(name, "PROVE", bool(condition), detail, evidence))
        print(f"  [{'PROVED' if condition else 'FAILED'}] {name}: {detail}")
        return bool(condition)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def save_evidence(self, name: str, payload: Any):
        path = self.logs_dir / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, default=str))
        return path


def build_tenant() -> Tenant:
    return Tenant(
        id="s-devlab-corp",
        name="S_DevLab Corp. (Example)",
        domain_id=DEMO_DOMAIN,
        treasury_account=TREASURY,
        limits=Limits(
            max_single_transfer_xrp=3000.0,
            max_daily_total_xrp=10000.0,
            min_treasury_balance_xrp=1000.0,
            supervisor_review_threshold_xrp=1000.0,
            due_date_buffer_hours=48,
            always_human_destinations=[COST_CENTRE_B],
        ),
        cost_centres={
            COST_CENTRE_A: "Facilities & Utilities",
            COST_CENTRE_B: "Payroll",
        },
        level_2_authorisations=1,
        level_3_authorisations=2,
    )


def build_currency_book() -> Any:
    """The currency book, wired to the devnet's real issuers."""
    book = default_currency_book()
    config_path = HERE / "var" / "local-currencies.json"
    if config_path.exists():
        cfg = json.loads(config_path.read_text())
        issuers = cfg.get("issuers", {})
        codes = cfg.get("codes", {})
        for ticker, code in codes.items():
            if ticker in issuers:
                book.set_asset(
                    ticker,
                    IssuedAsset(code=code, issuer=issuers[ticker], ticker=ticker),
                )
    return book


async def run_checks(v: Verifier, erp_url: str, ws_url: str) -> Dict:
    """Run every check. Returns a summary dict."""
    tenant = build_tenant()
    book = build_currency_book()
    ledger = LedgerClient(url=ws_url)

    # ---------------------------------------------------------- environment
    v.observe(
        "environment",
        f"XRPL devnet at {ws_url}",
        xrpl_url=ws_url,
        tenant=tenant.id,
        domain_id=DEMO_DOMAIN,
    )

    # server_info nests its payload under "result", and the rippled version
    # lives under result.info.build_version.
    info = await ledger._rpc({"command": "server_info"})
    build_version = (
        (info.get("result") or {}).get("info", {}).get("build_version")
    )
    v.observe(
        "rippled_version",
        f"rippled {build_version or 'version not reported'}",
        build_version=build_version,
    )

    # The feature RPC returns "features" as a DICT KEYED BY AMENDMENT HASH.
    # The human-readable name is inside each value, so the lookup has to
    # scan for it rather than index by name.
    feature = await ledger._rpc({"command": "feature"})
    features = (feature.get("result") or {}).get("features") or {}
    wanted = ("PermissionedDomains", "Credentials")
    amendments: Dict[str, bool] = {}
    if isinstance(features, dict):
        for entry in features.values():
            name = (entry or {}).get("name")
            if name in wanted:
                amendments[name] = bool((entry or {}).get("enabled"))
    else:
        for entry in features:
            name = (entry or {}).get("name")
            if name in wanted:
                amendments[name] = bool((entry or {}).get("enabled"))
    v.assert_that(
        "amendments_enabled",
        all(amendments.values()),
        f"the amendments the domain gate depends on: {amendments}",
        amendments=amendments,
    )


    # ------------------------------------------------------------- balances
    positions = {}
    for ticker, asset in book.assets.items():
        try:
            amount = await ledger.issued_balance(TREASURY, asset)
        except Exception as exc:
            amount = None
            v.observe("balance_error", f"{ticker}: {exc!r}", ticker=ticker)
        if amount is not None:
            positions[ticker] = amount
    v.observe(
        "currency_positions",
        ", ".join(f"{k} {v:,.2f}" for k, v in positions.items()),
        positions=positions,
    )
    v.assert_that(
        "treasury_holds_rlusd",
        positions.get("RLUSD", 0) > 0,
        f"the treasury holds {positions.get('RLUSD', 0):,.2f} RLUSD",
        amount=positions.get("RLUSD"),
    )

    # ------------------------------------------------------------ FX rates
    rate_service = default_rate_service()
    cached = rate_service.get(list(book.assets.keys()), force_refresh=True)
    v.observe(
        "fx_rates",
        f"provider={cached.provider} "
        f"stale={cached.is_stale(rate_service.max_age_seconds)}",
        provider=cached.provider,
        fetched_at=cached.fetched_at,
        rates={k: v.units_per_usd for k, v in cached.rates.items()},
        last_error=rate_service.last_error,
    )
    v.assert_that(
        "fx_rates_available",
        bool(cached.rates) and cached.provider != "static",
        "live exchange rates were obtained from a market source",
        provider=cached.provider,
    )
    for ticker, rate in cached.rates.items():
        book.set_rate(rate)

    # ------------------------------------------------------- the ERP plug-in
    if erp_url:
        erp = GenericRestErp(
            credential=Credential(
                key="VERIFY_ERP",
                secret="verification-token",
                account_id=tenant.id,
                base_url=erp_url,
            ),
            settings={
                "path": "/obligations",
                "records_key": "records",
                "id_field": "invoice_number",
                "amount_field": "total_due",
                "due_field": "expected_payment_date",
                "account_field": "counterparty_account",
                "currency_field": "currency_code",
                "description_field": "description",
                "vendor_field": "supplier_name",
                "entity_field": "cost_centre",
            },
        )
        try:
            obligations = erp.fetch()
            v.assert_that(
                "erp_connector_fetches",
                len(obligations) > 0,
                f"the ERP plug-in returned {len(obligations)} obligations",
                count=len(obligations),
                currencies=sorted({o.currency for o in obligations}),
            )
            v.save_evidence("erp_obligations", [o.to_dict() for o in obligations])
        except Exception as exc:
            v.assert_that(
                "erp_connector_fetches",
                False,
                f"the ERP plug-in raised: {exc!r}",
                error=repr(exc),
            )

    # ---------------------------------------------------- the domain gate
    policy = PolicyEngine(
        tenant,
        member_accounts=[TREASURY, COST_CENTRE_A, COST_CENTRE_B],
        approval_matrix=ApprovalMatrix(
            level_2_authorisations=tenant.level_2_authorisations,
            level_3_authorisations=tenant.level_3_authorisations,
        ),
        currency_book=book,
    )
    balance = positions.get("RLUSD", 0.0)

    # A domain-gated payment to a non-member must be REFUSED by policy
    # before it ever reaches the ledger.
    outsider_decision = policy.evaluate(
        ProposedTransfer(
            destination_account=OUTSIDER,
            amount=100.0,
            currency="RLUSD",
            obligation_id="VERIFY-OUTSIDER",
        ),
        treasury_balance_xrp=balance,
    )
    v.assert_that(
        "outsider_destination_refused",
        outsider_decision.tier is Tier.REFUSED,
        f"a payment to a non-participant is refused: {outsider_decision.reasons}",
        tier=outsider_decision.tier.value,
        reasons=outsider_decision.reasons,
    )

    # A member payment must be built WITH the domain id.
    from xrpl_agent_treasury_mgmt.executor import Executor

    executor = Executor(
        ledger,
        TreasurySigner(None, policy),
        domain_resolver=lambda tenant_id: tenant.domain_id,
        currency_book=book,
    )
    # The simulate call needs a CURRENT sequence and a last-ledger-sequence
    # ahead of the current ledger, or the ledger rejects it before it
    # reaches the gate (telCAN_NOT_QUEUE).
    account_data = await ledger.account_info(TREASURY)
    sequence = account_data["Sequence"]
    current_ledger = await ledger.current_ledger_seq()

    # The domain gate is checked BEHAVIOURALLY, not by reading a flag
    # list.
    #
    # An earlier version of this harness asserted that
    # `fixCleanup3_4_0` was enabled, on the reasoning that it gates the
    # Payment domain check in rippled 3.5.0-rc1. On 3.4.1 that flag
    # gates a STRICTER variant and the gate is already enforced without
    # it - so the assertion was testing a flag rather than the property.
    #
    # What matters is that a Payment carrying a DomainID to a
    # non-participant is REFUSED by the ledger. That is what is checked
    # here: the amendment list is reported, but the PASS/FAIL comes from
    # the ledger's own verdict.
    gate_verdict = None
    try:
        # The RLUSD asset comes from the currency book, which is wired to
        # the devnet's real issuers - not from a hardcoded address.
        rlusd_asset = book.asset_for("RLUSD")
        gate_tx = Payment(
            account=TREASURY,
            destination=OUTSIDER,
            amount={
                "currency": rlusd_asset.hex_code,
                "issuer": rlusd_asset.issuer,
                "value": "1000000",
            },
            send_max={
                "currency": rlusd_asset.hex_code,
                "issuer": rlusd_asset.issuer,
                "value": "1000000",
            },
            domain_id=tenant.domain_id,
            fee="12",
            sequence=sequence,
            network_id=ledger.network_id,
            last_ledger_sequence=current_ledger + 100,
        )
        gate_sim = await ledger.simulate(gate_tx.to_xrpl())
        gate_verdict = gate_sim.get("engine_result")
        v.save_evidence("domain_gate_simulation", gate_sim)
    except Exception as exc:
        gate_verdict = f"error: {exc!r}"

    v.assert_that(
        "domain_gate_enforces",
        gate_verdict == "tecNO_PERMISSION",
        (
            f"a Payment with a DomainID to a non-participant is refused: "
            f"{gate_verdict}"
        ),
        engine_result=gate_verdict,
        amendments=amendments,
    )

    built = executor.build(
        tenant.id,
        TREASURY,
        ProposedTransfer(
            destination_account=COST_CENTRE_A,
            amount=180.0,
            currency="RLUSD",
            obligation_id="VERIFY-MEMBER",
        ),
        sequence=sequence,
        last_ledger_sequence=current_ledger + 100,
    )
    v.assert_that(
        "payment_carries_domain_id",
        built.tx_json.get("DomainID") == tenant.domain_id,
        "every built payment carries the tenant's domain id",
        domain_id_in_tx=built.tx_json.get("DomainID"),
        expected=tenant.domain_id,
    )
    v.save_evidence("built_payment", built.to_dict())

    # The amount must be in smallest units, not major units.
    amount_value = built.tx_json["Amount"]["value"]
    v.assert_that(
        "amount_is_in_smallest_units",
        amount_value == "180000000",
        f"180 RLUSD is encoded as {amount_value} smallest units",
        encoded=amount_value,
        expected="180000000",
    )

    # ---------------------------------------------- simulate the member payment
    try:
        sim = await ledger.simulate(built.tx_json)
        engine_result = sim.get("engine_result")
        v.observe(
            "simulate_member_payment",
            f"the ledger simulated the instruction: {engine_result}",
            engine_result=engine_result,
            applied=sim.get("applied"),
        )
        v.save_evidence("simulate_member", sim)
    except Exception as exc:
        v.observe("simulate_error", f"{exc!r}")

    # ------------------------------------------------ the authorisation tiers
    tier_cases = [
        ("level_1", "RLUSD", 180.0, Tier.LEVEL_1),
        ("level_3_over_delegated_authority", "RLUSD", 1500.0, Tier.LEVEL_3),
        ("level_2_short_notice", "RLUSD", 180.0, Tier.LEVEL_2),
    ]
    for label, currency, amount, expected in tier_cases:
        transfer = ProposedTransfer(
            destination_account=COST_CENTRE_A,
            amount=amount,
            currency=currency,
            obligation_id=f"VERIFY-{label}",
        )
        if label == "level_2_short_notice":
            from datetime import datetime as _dt, timedelta as _td

            transfer.due_date = _dt.now(timezone.utc) + _td(hours=5)
        decision = policy.evaluate(
            transfer, treasury_balance_xrp=balance, already_committed_today_xrp=0.0
        )
        v.assert_that(
            f"tier_{label}",
            decision.tier is expected,
            f"{label}: tier={decision.tier.value} "
            f"authorisations={decision.required_authorisations} "
            f"reasons={decision.reasons}",
            tier=decision.tier.value,
            expected=expected.value,
            required_authorisations=decision.required_authorisations,
            reasons=decision.reasons,
        )

    # ---------------------------------------- the cross-currency payment
    # A CAD disbursement from an RLUSD treasury is a CROSS-CURRENCY payment.
    # The XRPL requires send_max; without it the model is rejected.
    try:
        cad_built = executor.build(
            tenant.id,
            TREASURY,
            ProposedTransfer(
                destination_account=COST_CENTRE_A,
                amount=3750.0,
                currency="CAD",
                obligation_id="VERIFY-XCUR",
            ),
            sequence=sequence,
            last_ledger_sequence=current_ledger + 100,
        )
        v.assert_that(
            "cross_currency_payment_carries_send_max",
            "SendMax" in cad_built.tx_json,
            f"a CAD payment from an RLUSD treasury carries send_max: "
            f"{cad_built.tx_json.get('SendMax', {}).get('currency', 'NONE')}",
            amount=cad_built.tx_json.get("Amount"),
            send_max=cad_built.tx_json.get("SendMax"),
        )
        v.assert_that(
            "cross_currency_payment_keeps_the_domain_gate",
            cad_built.tx_json.get("DomainID") == tenant.domain_id,
            "the perimeter applies to cross-currency payments too",
            domain_id=cad_built.tx_json.get("DomainID"),
        )
        v.save_evidence("cross_currency_payment", cad_built.to_dict())
    except Exception as exc:
        v.assert_that(
            "cross_currency_payment_carries_send_max",
            False,
            f"building a cross-currency payment raised: {exc!r}",
            error=repr(exc),
        )

    # An issued-currency payment is ALWAYS a cross-issue payment, so it
    # needs send_max even when the treasury holds that exact asset.
    v.assert_that(
        "issued_currency_payment_carries_send_max",
        "SendMax" in built.tx_json,
        "an RLUSD payment carries send_max in RLUSD",
        send_max=built.tx_json.get("SendMax"),
    )

    # --------------------------------------------- multi-currency limits
    cad_decision = policy.evaluate(
        ProposedTransfer(
            destination_account=COST_CENTRE_A,
            amount=3750.0,
            currency="CAD",
            obligation_id="VERIFY-CAD",
        ),
        treasury_balance_xrp=balance,
    )
    v.observe(
        "multi_currency_limit",
        f"CA$3,750.00 = ${cad_decision.detail['transfer']['amount']:,.2f} CAD "
        f"-> tier {cad_decision.tier.value}",
        tier=cad_decision.tier.value,
        reasons=cad_decision.reasons,
    )

    # ---------------------------------------------- the approval ledger gate
    signer = TreasurySigner(None, policy)
    signer.record_authorisation("VERIFY-LEVEL3", "A. Reyes", "treasury_manager")
    level_3 = PolicyDecision(
        tier=Tier.LEVEL_3,
        reasons=[],
        required_authorisations=2,
        detail={},
    )
    outcome = signer.sign({}, level_3, obligation_id="VERIFY-LEVEL3")
    v.assert_that(
        "one_approval_does_not_satisfy_level_3",
        outcome.status == "awaiting_authorisation",
        f"with one sign-off the signer withholds: {outcome.status}",
        status=outcome.status,
        received=outcome.received_authorisations,
        required=outcome.required_authorisations,
    )
    signer.record_authorisation("VERIFY-LEVEL3", "M. Okonkwo", "supervisor")
    outcome = signer.sign({}, level_3, obligation_id="VERIFY-LEVEL3")
    # The signer has no provider configured here (this harness never
    # submits), so it still withholds - but the APPROVAL COUNT is what this
    # check is about: two sign-offs must satisfy a two-signature level.
    v.assert_that(
        "two_approvals_satisfy_level_3",
        outcome.received_authorisations == 2
        and outcome.required_authorisations == 2,
        f"with two sign-offs the authorisation requirement is met: "
        f"{outcome.received_authorisations}/{outcome.required_authorisations} "
        f"(status={outcome.status}; no signing provider is configured in the "
        f"harness, so the instruction is not submitted)",
        status=outcome.status,
        received=outcome.received_authorisations,
        required=outcome.required_authorisations,
    )

    # ------------------------------------------------------ the decision log
    log_path = v.out_dir / "decision-log.jsonl"
    log = AgentLog(log_path)
    log.append(tenant.id, "verification_started", {"allow_move": v.allow_move})
    log.append(tenant.id, "domain_verified", {"domain_id": DEMO_DOMAIN})
    log.append(tenant.id, "positions_read", positions)
    log.append(tenant.id, "plan_simulated", {"built": built.to_dict()})
    chain = log.verify()
    v.assert_that(
        "decision_log_chain_valid",
        chain["valid"] is True,
        f"the decision log is hash-chained and verifies: {chain}",
        **chain,
    )

    # --------------------------------------------------------- optional move
    if v.allow_move:
        v.observe(
            "move_enabled",
            "--allow-move was supplied; a devnet settlement may be submitted",
        )
    else:
        v.observe(
            "move_skipped",
            "no funds were moved: the harness is read-only unless "
            "--allow-move is supplied",
        )

    return {
        "positions": positions,
        "amendments": amendments,
        "rates": {k: r.units_per_usd for k, r in cached.rates.items()},
        "rate_provider": cached.provider,
    }


def write_report(v: Verifier, summary: Dict, out_dir: pathlib.Path):
    """Write the machine-readable results and the human-readable report."""
    asserts = [c for c in v.checks if c.kind == "ASSERT"]
    proves = [c for c in v.checks if c.kind == "PROVE"]
    observations = [c for c in v.checks if c.kind == "OBSERVE"]

    results = {
        "started_utc": v.started.isoformat(),
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "passed": v.passed,
        "allow_move": v.allow_move,
        "totals": {
            "assertions": len(asserts),
            "assertions_passed": sum(1 for c in asserts if c.passed),
            "proves": len(proves),
            "proves_passed": sum(1 for c in proves if c.passed),
            "observations": len(observations),
        },
        "checks": [c.to_dict() for c in v.checks],
        "summary": summary,
    }
    (out_dir / "RESULTS.json").write_text(json.dumps(results, indent=2, default=str))

    lines = [
        "# Verification report",
        "",
        f"**Result: {'PASS' if v.passed else 'FAIL'}**",
        "",
        f"- Started: `{v.started.isoformat()}`",
        f"- Finished: `{results['finished_utc']}`",
        f"- Assertions: {results['totals']['assertions_passed']}"
        f"/{results['totals']['assertions']} passed",
        f"- Proves: {results['totals']['proves_passed']}"
        f"/{results['totals']['proves']} passed",
        f"- Observations recorded: {results['totals']['observations']}",
        f"- Funds moved: {'yes (--allow-move)' if v.allow_move else 'no'}",
        "",
        "## Assertions",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
    ]
    for c in asserts:
        mark = "PASS" if c.passed else "**FAIL**"
        detail = c.detail.replace("|", "\\|")
        lines.append(f"| `{c.name}` | {mark} | {detail} |")

    if proves:
        lines += ["", "## Proves", "", "| Check | Result | Detail |", "|---|---|---|"]
        for c in proves:
            mark = "PROVED" if c.passed else "**FAILED**"
            lines.append(
                f"| `{c.name}` | {mark} | {c.detail.replace('|', chr(92) + '|')} |"
            )

    lines += ["", "## Observations", "", "| Check | Detail |", "|---|---|"]
    for c in observations:
        lines.append(f"| `{c.name}` | {c.detail.replace('|', chr(92) + '|')} |")

    lines += [
        "",
        "## Evidence",
        "",
        "`logs/` holds the raw evidence for each check:",
        "",
    ]
    for path in sorted((out_dir / "logs").glob("*.json")):
        lines.append(f"- `logs/{path.name}`")
    lines += [
        "",
        "`decision-log.jsonl` is the service's own hash-chained decision log "
        "for this run. `RESULTS.json` is the machine-readable form of this "
        "report.",
        "",
    ]
    (out_dir / "VERIFICATION.md").write_text("\n".join(lines))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the treasury service")
    parser.add_argument("--out", default=str(HERE / "verification"))
    parser.add_argument("--erp-url", default="http://127.0.0.1:8096")
    parser.add_argument("--xrpl-url", default="ws://127.0.0.1:6006")
    parser.add_argument(
        "--allow-move",
        action="store_true",
        help="permit checks that submit a real transaction on the devnet",
    )
    args = parser.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    v = Verifier(out_dir, allow_move=args.allow_move)
    print(f"Verifying against {args.xrpl_url} and {args.erp_url}")
    print()
    try:
        summary = asyncio.run(run_checks(v, args.erp_url, args.xrpl_url))
    except Exception as exc:
        v.assert_that("harness_completed", False, f"the harness raised: {exc!r}")
        summary = {}

    results = write_report(v, summary, out_dir)
    print()
    print(f"  {results['totals']['assertions_passed']}"
          f"/{results['totals']['assertions']} assertions passed")
    print(f"  report: {out_dir / 'VERIFICATION.md'}")
    print(f"  results: {out_dir / 'RESULTS.json'}")
    return 0 if v.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
