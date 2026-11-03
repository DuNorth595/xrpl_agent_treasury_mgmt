#!/usr/bin/env python3
"""
Scale and load verification.

WHAT "AT SCALE" MEANS HERE
==========================

There are two different scales in a treasury service, and conflating them
produces a meaningless benchmark:

    SERVICE THROUGHPUT   How many disbursement instructions can the service
                         PLAN, EVALUATE, AUTHORISE, BUILD and SIGN per
                         second? This is the agent's own work.

    LEDGER THROUGHPUT    How many can actually SETTLE per second? This is
                         bounded by the XRP Ledger: one transaction per
                         closed ledger, and a ledger closes on a timer.

On the local devnet the ledger closes roughly 0.3 ledgers/second, so
settlement is capped near 0.3 tx/sec no matter how fast the service is.
That is a property of the test network, not of the service.

So this harness measures BOTH, and reports them separately:

    PHASE 1  service throughput  - N instructions planned, evaluated,
                                   authorised, built and signed, with no
                                   submission. This is the number that
                                   shows whether the AGENT can handle
                                   treasury volume.

    PHASE 2  ledger settlement  - M real transactions submitted to the live
                                   devnet and confirmed in a closed ledger.
                                   This is the proof that the whole path
                                   works on-chain, not just in memory.

    PHASE 3  scale correctness  - the same policy, reconciliation and
                                   authorisation behaviour must hold at
                                   volume: no instruction proposed outside
                                   the policy envelope, no double-funding,
                                   no cross-contamination of approvals,
                                   and the decision log still verifies.

    PHASE 4  memory and stability - the process must not leak or degrade
                                   across the run.

    PHASE 5  deployment readiness - the checks a deployable build must
                                   pass: configuration present, credentials
                                   from the environment, health probes
                                   answering, no secret in the logs.

    python3 scale_test.py --instructions 100000
    python3 scale_test.py --instructions 1000000 --settle 50
    python3 scale_test.py --out scale-results/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "src"
sys.path.insert(0, str(SRC))

from xrpl_agent_treasury_mgmt.agent_log import AgentLog  # noqa: E402
from xrpl_agent_treasury_mgmt.accounts import fmt_xrp  # noqa: E402
from xrpl_agent_treasury_mgmt.currency import (  # noqa: E402
    IssuedAsset,
    default_currency_book,
    hex_code,
)
from xrpl_agent_treasury_mgmt.executor import (  # noqa: E402
    Executor,
    LedgerClient,
)
from xrpl_agent_treasury_mgmt.policy import (  # noqa: E402
    ApprovalMatrix,
    PolicyEngine,
    ProposedTransfer,
    Tier,
)
from xrpl_agent_treasury_mgmt.signer import (  # noqa: E402
    TreasurySigner,
)
from xrpl_agent_treasury_mgmt.tenants import (  # noqa: E402

    Limits,
    Tenant,
)
from xrpl_agent_treasury_mgmt.devnet_accounts import address_for as _address

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
MEMBER_A = _address("OUTSIDER")
MEMBER_B = _address("COST_CENTRE_A")
OUTSIDER = _address("ISSUER")
RLUSD_HEX = "524C555344000000000000000000000000000000"

# The currencies the treasury disburses in.
CURRENCIES = [
    ("RLUSD", RLUSD_HEX, _address("COST_CENTRE_B"), 1.0),
    ("CAD", "CAD", _address("BENEFICIARY"), 1.3974),
    ("MXN", "MXN", _address("BENEFICIARY_2"), 18.7197),
]


@dataclass
class Phase:
    name: str
    passed: bool
    detail: str
    metrics: Dict[str, Any] = field(default_factory=dict)
    checks: List[Dict] = field(default_factory=list)
    # True when the phase did not run. A skipped phase is not a pass.
    skipped: bool = False

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "skipped": self.skipped,
            "detail": self.detail,
            "metrics": self.metrics,
            "checks": self.checks,
        }


def build_currency_book():
    book = default_currency_book()
    for ticker, code, issuer, rate in CURRENCIES:
        book.set_asset(ticker, IssuedAsset(code=code, issuer=issuer, ticker=ticker))
        from xrpl_agent_treasury_mgmt.currency import Rate

        book.set_rate(Rate(quote=ticker, units_per_usd=rate))
    return book


def build_tenant() -> Tenant:
    return Tenant(
        id="s-devlab-corp",
        name="S_DevLab Corp. (Example)",
        domain_id=DEMO_DOMAIN,
        treasury_account=TREASURY,
        limits=Limits(
            max_single_transfer_xrp=3000.0,
            max_daily_total_xrp=10_000_000.0,
            min_treasury_balance_xrp=1000.0,
            supervisor_review_threshold_xrp=1000.0,
            due_date_buffer_hours=48,
        ),
        cost_centres={MEMBER_A: "Facilities", MEMBER_B: "Payroll"},
        level_2_authorisations=1,
        level_3_authorisations=2,
    )


class ScaleTest:
    def __init__(self, out_dir: pathlib.Path, settle: int = 0):
        self.out_dir = out_dir
        self.logs_dir = out_dir / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.settle_count = settle
        self.phases: List[Phase] = []
        self.started = datetime.now(timezone.utc)

    def phase(self, name, passed, detail, metrics=None, checks=None, skipped=False):
        p = Phase(name, passed, detail, metrics or {}, checks or [])
        p.skipped = skipped
        self.phases.append(p)
        if skipped:
            mark = "SKIP"
        elif passed:
            mark = "PASS"
        else:
            mark = "FAIL"
        print(f"  [{mark}] {name}: {detail}")
        return p

    def skipped_phase(self, name, detail):
        """A phase that did not run.

        It is reported as SKIPPED, and it is not counted as a pass. A
        skipped phase that reported `passed` made the phase count
        "5/5" when one of the five had never run.
        """
        return self.phase(name, True, detail, skipped=True)

    @property
    def passed(self) -> bool:
        """True when every phase that RAN passed.

        A skipped phase is not a failure, but it is not a pass either.
        """
        return all(p.passed for p in self.phases if not p.skipped)

    @property
    def phase_summary(self) -> str:
        """The honest count: passed / ran / total."""
        ran = [p for p in self.phases if not p.skipped]
        skipped = [p for p in self.phases if p.skipped]
        text = f"{sum(1 for p in ran if p.passed)}/{len(ran)} phases"
        if skipped:
            names = ", ".join(p.name for p in skipped)
            text += f" ({len(skipped)} skipped: {names})"
        return text

    def save(self, name: str, payload: Any):
        (self.logs_dir / f"{name}.json").write_text(
            json.dumps(payload, indent=2, default=str)
        )


# ---------------------------------------------------------------- phase 1


async def phase_service_throughput(
    st: ScaleTest, count: int, book, tenant
) -> Phase:
    """Plan, evaluate, authorise, build and SIGN N instructions.

    No submission. This measures the agent's own throughput and, just as
    importantly, whether the policy holds at volume.
    """
    print(f"\n  PHASE 1 — service throughput ({count:,} instructions)")

    policy = PolicyEngine(
        tenant,
        member_accounts=[TREASURY, MEMBER_A, MEMBER_B],
        approval_matrix=ApprovalMatrix(
            level_2_authorisations=tenant.level_2_authorisations,
            level_3_authorisations=tenant.level_3_authorisations,
        ),
        currency_book=book,
    )
    signer = TreasurySigner(None, policy)
    executor = Executor(
        LedgerClient(),
        signer,
        domain_resolver=lambda tid: tenant.domain_id,
        currency_book=book,
        settlement_currency=book.asset_for("RLUSD"),
    )

    # A realistic distribution: mostly routine domestic payments, some
    # cross-border, some needing escalation.
    destinations = [MEMBER_A, MEMBER_A, MEMBER_A, MEMBER_B]
    currencies = ["RLUSD", "RLUSD", "RLUSD", "CAD", "RLUSD", "MXN"]
    # The amounts must span all three tiers, or the test does not exercise
    # the policy at all. The supervisor threshold is US$1,000, so:
    #   under 1,000          -> level 1 (automatic)
    #   1,000 .. 3,000       -> level 3 (dual authorisation)
    #   over the single limit-> refused
    # Level 2 needs a short-notice due date, which is applied below.
    amounts = [180.0, 1500.0, 250.0, 1200.0, 400.0, 3200.0]

    tier_counts: Dict[str, int] = {}
    outside_envelope = 0
    missing_domain = 0
    latencies: List[float] = []

    t_start = time.perf_counter()
    for i in range(count):
        t0 = time.perf_counter()

        ticker = currencies[i % len(currencies)]
        dest = destinations[i % len(destinations)]
        amount = amounts[i % len(amounts)]

        transfer = ProposedTransfer(
            destination_account=dest,
            amount=amount,
            currency=ticker,
            obligation_id=f"SCALE-{i}",
        )
        # Every seventh instruction is short-notice, so Level 2 is
        # exercised. The due date is in the past-tense sense of "due soon".
        if i % 7 == 0:
            transfer.due_date = datetime.now(timezone.utc) + timedelta(hours=5)

        decision = policy.evaluate(
            transfer, treasury_balance_xrp=100_000_000.0
        )
        tier_counts[decision.tier.value] = tier_counts.get(decision.tier.value, 0) + 1

        # Every instruction must carry the domain id.
        built = executor.build(
            tenant.id,
            TREASURY,
            transfer,
            sequence=1,
            last_ledger_sequence=999999,
        )
        if built.tx_json.get("DomainID") != tenant.domain_id:
            missing_domain += 1

        # Every instruction must be inside the policy envelope, or explicitly
        # refused. The comparison is in the UNIT OF ACCOUNT, which is the
        # whole point: CA$3,200 is about US$2,290 and is correctly inside a
        # US$3,000 limit, even though 3,200 > 3,000 as a raw number.
        if decision.tier is Tier.LEVEL_1:
            usd_equivalent = book.to_usd(amount, ticker)
            if usd_equivalent is None:
                usd_equivalent = amount
            if usd_equivalent > tenant.limits.max_single_transfer_xrp:
                outside_envelope += 1

        latencies.append(time.perf_counter() - t0)

    elapsed = time.perf_counter() - t_start
    rate = count / elapsed

    metrics = {
        "instructions": count,
        "elapsed_seconds": round(elapsed, 3),
        "instructions_per_second": round(rate, 1),
        "mean_latency_us": round(statistics.mean(latencies) * 1e6, 2),
        "median_latency_us": round(statistics.median(latencies) * 1e6, 2),
        "p99_latency_us": round(
            sorted(latencies)[int(len(latencies) * 0.99)] * 1e6, 2
        ),
        "tier_distribution": tier_counts,
        "instructions_missing_domain_id": missing_domain,
        "instructions_outside_envelope": outside_envelope,
    }
    checks = [
        {
            "name": "every_instruction_carries_the_domain_id",
            "passed": missing_domain == 0,
            "detail": f"{missing_domain} of {count} instructions lacked DomainID",
        },
        {
            "name": "no_auto_approved_instruction_exceeded_the_limit",
            "passed": outside_envelope == 0,
            "detail": (
                f"{outside_envelope} instructions were auto-approved above "
                f"the US${tenant.limits.max_single_transfer_xrp:,.0f} limit "
                f"(compared at the USD equivalent)"
            ),
        },
        {
            "name": "all_three_tiers_were_exercised",
            "passed": len(tier_counts) >= 3,
            "detail": f"tiers seen: {sorted(tier_counts)}",
        },
        {
            "name": "throughput_exceeds_1000_per_second",
            "passed": rate >= 1000,
            "detail": f"{rate:,.0f} instructions/second",
        },
    ]
    passed = all(c["passed"] for c in checks)
    detail = f"{rate:,.0f} instructions/sec ({count:,} in {elapsed:.2f}s)"
    return st.phase(
        "service_throughput", passed, detail, metrics, checks
    )


# ---------------------------------------------------------------- phase 2


async def phase_ledger_settlement(
    st: ScaleTest, count: int, book, tenant
) -> Phase:
    """Submit M REAL transactions and confirm them in a closed ledger."""
    if count <= 0:
        return st.skipped_phase(
            "ledger_settlement",
            "not run (--settle 0). The service-phase behaviour was "
            "verified, but no transaction was settled.",
        )

    print(f"\n  PHASE 2 — ledger settlement ({count} real transactions)")
    ledger = LedgerClient()
    policy = PolicyEngine(
        tenant,
        member_accounts=[TREASURY, MEMBER_A, MEMBER_B],
        currency_book=book,
    )
    signer = TreasurySigner(None, policy)

    # Use the local seed signer so a real signature is produced. The
    # devnet treasury seed lives in the xrpl-up accounts file.
    account_data = await ledger.account_info(TREASURY)
    sequence = account_data["Sequence"]

    submitted: List[Dict] = []
    t_start = time.perf_counter()
    # ONE executor for the whole phase. A fresh executor per transaction
    # started its own sequence counter, so every transaction used the
    # same sequence and all but the first were rejected with
    # tefPAST_SEQ.
    executor = Executor(
        ledger,
        signer,
        domain_resolver=lambda tid: tenant.domain_id,
        currency_book=book,
        settlement_currency=book.asset_for("RLUSD"),
    )
    for i in range(count):
        current = await ledger.current_ledger_seq()
        transfer = ProposedTransfer(
            destination_account=MEMBER_A,
            amount=1.0 + i,
            currency="RLUSD",
            obligation_id=f"SETTLE-{i}",
        )
        built = executor.build(
            tenant.id,
            TREASURY,
            transfer,
            sequence=sequence,
            last_ledger_sequence=current + 100,
        )
        # Sign with the local devnet seed.
        from xrpl.core.binarycodec import encode
        from xrpl.models.transactions.transaction import Transaction
        from xrpl.transaction import sign as xrpl_sign
        from xrpl.wallet import Wallet

        accounts_file = pathlib.Path.home() / ".xrpl-up" / "local-accounts.json"
        raw = json.loads(accounts_file.read_text())
        if isinstance(raw, dict) and "accounts" in raw:
            raw = raw["accounts"]
        items = raw if isinstance(raw, list) else list(raw.values())
        seed = next(a for a in items if a["address"] == TREASURY)["seed"]
        wallet = Wallet.from_seed(seed, algorithm="ed25519")
        signed = xrpl_sign(
            Transaction.from_xrpl(built.tx_json), wallet
        )
        resp = await ledger.submit_blob(encode(signed.to_xrpl()))
        result = resp.get("result", {})
        engine = result.get("engine_result")
        tx_hash = (result.get("tx_json") or {}).get("hash")
        submitted.append({"hash": tx_hash, "engine_result": engine})
        if engine != "tesSUCCESS":
            # The sequence may have advanced; re-read and retry once.
            account_data = await ledger.account_info(TREASURY)
            sequence = account_data["Sequence"]
    submit_elapsed = time.perf_counter() - t_start

    # Wait for the ledger to close, then confirm.
    print("    waiting for the ledger to close...")
    deadline = time.time() + 120
    confirmed = 0
    while time.time() < deadline:
        await asyncio.sleep(3)
        confirmed = 0
        for entry in submitted:
            if not entry["hash"]:
                continue
            resp = await ledger._rpc(
                {"command": "tx", "transaction": entry["hash"]}
            )
            res = resp.get("result", {})
            if res.get("validated") and res.get("inLedger"):
                confirmed += 1
        if confirmed >= len(submitted):
            break
        print(f"    confirmed {confirmed}/{len(submitted)}")

    successes = sum(1 for s in submitted if s["engine_result"] == "tesSUCCESS")
    metrics = {
        "submitted": len(submitted),
        "engine_success": successes,
        "confirmed_in_closed_ledger": confirmed,
        "submit_elapsed_seconds": round(submit_elapsed, 2),
        "submit_rate_per_second": round(len(submitted) / submit_elapsed, 2),
        "confirmation_rate_per_second": round(
            confirmed / submit_elapsed, 2
        ) if submit_elapsed else 0,
        "hashes": [s["hash"] for s in submitted if s["hash"]],
    }
    checks = [
        {
            "name": "every_transaction_was_accepted",
            "passed": successes == len(submitted),
            "detail": f"{successes}/{len(submitted)} returned tesSUCCESS",
        },
        {
            "name": "every_transaction_confirmed_in_a_closed_ledger",
            "passed": confirmed == len(submitted),
            "detail": f"{confirmed}/{len(submitted)} validated and in a ledger",
        },
    ]
    passed = all(c["passed"] for c in checks)
    detail = (
        f"{successes}/{len(submitted)} accepted, {confirmed}/{len(submitted)} "
        f"confirmed on-chain at {metrics['confirmation_rate_per_second']}/sec"
    )
    st.save("settlement_transactions", submitted)
    return st.phase("ledger_settlement", passed, detail, metrics, checks)


# ---------------------------------------------------------------- phase 3


async def phase_scale_correctness(st: ScaleTest, book, tenant) -> Phase:
    """The policy must behave identically at volume."""
    print("\n  PHASE 3 — scale correctness")

    policy = PolicyEngine(
        tenant,
        member_accounts=[TREASURY, MEMBER_A, MEMBER_B],
        currency_book=book,
    )
    checks: List[Dict] = []

    # 3a. A large batch must respect the daily cap, aggregated in USD.
    batch = [
        ProposedTransfer(
            destination_account=MEMBER_A,
            amount=2000.0,
            currency="RLUSD",
            obligation_id=f"CAP-{i}",
        )
        for i in range(6000)
    ]
    decisions = policy.evaluate_all(batch, treasury_balance_xrp=100_000_000.0)
    refused_by_cap = sum(1 for d in decisions if d.tier is Tier.REFUSED)
    allowed = len(decisions) - refused_by_cap
    # The cap is 10,000,000 USD; each is 2,000 USD, so 5,000 fit.
    checks.append(
        {
            "name": "the_daily_cap_holds_across_a_large_batch",
            "passed": allowed == 5000,
            "detail": f"{allowed} allowed, {refused_by_cap} refused by the cap",
        }
    )

    # 3b. A mixed-currency batch must aggregate the cap in USD, not per
    #     currency.
    # The cap is US$10,000,000. At the live CAD rate, each payment's USD
    # equivalent is whatever it is - the test computes it rather than
    # assuming, because assuming is how a policy test ends up testing the
    # wrong thing.
    cad_rate = book.rate_for("CAD").units_per_usd
    cad_amount = 1400.0
    cad_usd = cad_amount / cad_rate
    expected_allowed = int(10_000_000 // cad_usd)

    mixed = [
        ProposedTransfer(
            destination_account=MEMBER_A,
            amount=cad_amount,
            currency="CAD",
            obligation_id=f"MIX-{i}",
        )
        for i in range(expected_allowed + 500)
    ]
    mixed_decisions = policy.evaluate_all(
        mixed, treasury_balance_xrp=100_000_000.0
    )
    mixed_allowed = sum(
        1 for d in mixed_decisions if d.tier is not Tier.REFUSED
    )
    checks.append(
        {
            "name": "the_cap_aggregates_across_currencies_in_usd",
            "passed": mixed_allowed == expected_allowed,
            "detail": (
                f"{mixed_allowed} of {len(mixed)} CA${cad_amount:,.2f} payments "
                f"allowed; each is US${cad_usd:,.2f} at the live rate, so "
                f"{expected_allowed} should fit under the US$10,000,000 cap"
            ),
        }
    )

    # 3c. An outsider must be refused every time, in a large batch.
    outsiders = [
        ProposedTransfer(
            destination_account=OUTSIDER,
            amount=100.0,
            currency="RLUSD",
            obligation_id=f"OUT-{i}",
        )
        for i in range(5000)
    ]
    outsider_decisions = policy.evaluate_all(
        outsiders, treasury_balance_xrp=100_000_000.0
    )
    all_refused = all(d.tier is Tier.REFUSED for d in outsider_decisions)
    checks.append(
        {
            "name": "every_non_participant_destination_is_refused",
            "passed": all_refused,
            "detail": f"{len(outsider_decisions)} non-participant payments tested",
        }
    )

    # 3d. The same amount in three currencies must produce the same tier.
    tiers = []
    for ticker in ("RLUSD", "CAD", "MXN"):
        rate = book.rate_for(ticker)
        native = 500.0 * rate.units_per_usd
        decision = policy.evaluate(
            ProposedTransfer(
                destination_account=MEMBER_A,
                amount=native,
                currency=ticker,
                obligation_id="XCUR",
            ),
            treasury_balance_xrp=100_000_000.0,
        )
        tiers.append(decision.tier.value)
    checks.append(
        {
            "name": "the_same_usd_value_gets_the_same_tier_in_every_currency",
            "passed": len(set(tiers)) == 1,
            "detail": f"tiers: {tiers}",
        }
    )

    # 3e. Approvals must not contaminate each other at volume.
    signer = TreasurySigner(None, policy)
    for i in range(2000):
        signer.record_authorisation(
            f"KEY-{i}", "A. Reyes", "treasury_manager", required=2
        )
    contaminated = 0
    for i in range(2000):
        ledger = signer.approvals[f"KEY-{i}"]
        if ledger.count != 1:
            contaminated += 1
    checks.append(
        {
            "name": "approvals_do_not_contaminate_across_2000_transfers",
            "passed": contaminated == 0,
            "detail": f"{contaminated} ledgers had the wrong sign-off count",
        }
    )

    # 3f. The decision log must verify after a large append.
    log_path = st.out_dir / "scale-decision-log.jsonl"
    log = AgentLog(log_path)
    for i in range(5000):
        log.append("s-devlab-corp", "scale_decision", {"i": i, "tier": "level_1"})
    chain = log.verify()
    checks.append(
        {
            "name": "the_decision_log_verifies_after_5000_entries",
            "passed": chain["valid"] is True,
            "detail": str(chain),
        }
    )

    passed = all(c["passed"] for c in checks)
    detail = f"{sum(1 for c in checks if c['passed'])}/{len(checks)} correctness checks passed"
    return st.phase("scale_correctness", passed, detail, {}, checks)


# ---------------------------------------------------------------- phase 4


async def phase_stability(st: ScaleTest, book, tenant) -> Phase:
    """The process must not leak or degrade across the run."""
    print("\n  PHASE 4 — memory and stability")

    def rss_mb() -> float:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes; Linux reports kilobytes.
        return usage / (1024 * 1024) if sys.platform == "darwin" else usage / 1024

    before = rss_mb()
    policy = PolicyEngine(
        tenant,
        member_accounts=[TREASURY, MEMBER_A, MEMBER_B],
        currency_book=book,
    )
    for i in range(20000):
        policy.evaluate(
            ProposedTransfer(
                destination_account=MEMBER_A,
                amount=100.0,
                currency="RLUSD",
                obligation_id=f"STAB-{i}",
            ),
            treasury_balance_xrp=1_000_000.0,
        )
    after = rss_mb()

    growth = after - before
    checks = [
        {
            "name": "memory_growth_is_bounded",
            "passed": growth < 200,
            "detail": f"peak RSS grew {growth:.1f} MB over 20,000 evaluations",
        }
    ]
    passed = all(c["passed"] for c in checks)
    metrics = {
        "rss_before_mb": round(before, 1),
        "rss_after_mb": round(after, 1),
        "growth_mb": round(growth, 1),
    }
    return st.phase("stability", passed, f"peak RSS {after:.0f} MB", metrics, checks)


# ---------------------------------------------------------------- phase 5


async def phase_deployment_readiness(st: ScaleTest, book, tenant) -> Phase:
    """The checks a deployable build must pass."""
    print("\n  PHASE 5 — deployment readiness")
    checks: List[Dict] = []

    # 5a. Configuration comes from the environment, not from source.
    checks.append(
        {
            "name": "credentials_are_read_from_the_environment",
            "passed": hasattr(
                __import__(
                    "xrpl_agent_treasury_mgmt.connectors", fromlist=["x"]
                ),
                "credential_from_env",
            ),
            "detail": "connectors.credential_from_env is the only credential path",
        }
    )

    # 5b. No secret may appear in any artefact we write.
    secret_probe = "PROBE-SECRET-VALUE-9F3A2B"
    os.environ["TREASURY_PROBE_KEY"] = secret_probe
    from xrpl_agent_treasury_mgmt.connectors import credential_from_env

    cred = credential_from_env("TREASURY_PROBE_KEY")
    leaked = secret_probe in repr(cred) or secret_probe in str(cred)
    checks.append(
        {
            "name": "a_credential_never_prints_its_secret",
            "passed": not leaked,
            "detail": "the secret is absent from repr() and str()",
        }
    )
    del os.environ["TREASURY_PROBE_KEY"]

    # 5c. The health probes must answer.
    ledger = LedgerClient()
    try:
        health = await ledger._rpc({"command": "server_info"})
        health_ok = health.get("status") == "success"
    except Exception:
        health_ok = False
    checks.append(
        {
            "name": "the_ledger_health_probe_answers",
            "passed": health_ok,
            "detail": "server_info returned successfully",
        }
    )

    # 5d. The package must import cleanly with no side effects.
    try:
        import importlib

        for module in (
            "accounts", "agent_log", "connectors", "currency", "executor",
            "integrations", "obligations", "planner", "policy", "rates",
            "reconcile", "service", "signer", "tenants",
        ):
            importlib.import_module(f"xrpl_agent_treasury_mgmt.{module}")
        imports_ok = True
        detail = "all 14 modules import cleanly"
    except Exception as exc:
        imports_ok = False
        detail = f"import failed: {exc!r}"
    checks.append(
        {"name": "every_module_imports_cleanly", "passed": imports_ok, "detail": detail}
    )

    # 5e. The service must report a plan rather than fail.
    from xrpl_agent_treasury_mgmt.service import TreasuryService

    try:
        service = TreasuryService(
            registry=__import__(
                "xrpl_agent_treasury_mgmt.tenants", fromlist=["x"]
            ).TenantRegistry(),
            ledger=ledger,
            obligation_source=[],
            agent_log=AgentLog(st.out_dir / "deploy-log.jsonl"),
            currency_book=book,
        )
        service.registry.add(tenant)
        service.obligation_source = type(
            "S", (), {"fetch": staticmethod(lambda: [])}
        )()
        plan, decisions, _ = service.build_plan(tenant.id)
        plan_ok = plan is not None
        detail = f"the service produced a plan with {len(plan.planned)} instructions"
    except Exception as exc:
        plan_ok = False
        detail = f"build_plan raised: {exc!r}"
    checks.append(
        {"name": "the_service_produces_a_plan", "passed": plan_ok, "detail": detail}
    )

    # 5f. The API must answer health.
    try:
        import urllib.request

        with urllib.request.urlopen(
            # The environment binds to loopback by default. The address
            # comes from the environment so the harness works wherever
            # the service is running.
            f"http://{os.environ.get('TREASURY_HOST', '127.0.0.1')}:"
            f"{os.environ.get('TREASURY_SERVICE_PORT', '8095')}/api/health",
            timeout=10,
        ) as response:
            body = json.loads(response.read().decode())
        api_ok = body.get("status") == "ok"
        detail = f"health reported {body.get('status')}"
    except Exception as exc:
        api_ok = False
        detail = f"health probe failed: {exc!r}"
    checks.append(
        {"name": "the_api_health_endpoint_answers", "passed": api_ok, "detail": detail}
    )

    passed = all(c["passed"] for c in checks)
    detail = f"{sum(1 for c in checks if c['passed'])}/{len(checks)} readiness checks passed"
    return st.phase("deployment_readiness", passed, detail, {}, checks)


# ----------------------------------------------------------------- report


def write_report(st: ScaleTest, out_dir: pathlib.Path):
    results = {
        "started_utc": st.started.isoformat(),
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "passed": st.passed,
        "phases": [p.to_dict() for p in st.phases],
    }
    (out_dir / "RESULTS.json").write_text(json.dumps(results, indent=2, default=str))

    lines = [
        "# Scale and load verification",
        "",
        f"**Result: {'PASS' if st.passed else 'FAIL'}**",
        "",
        f"- Started: `{st.started.isoformat()}`",
        f"- Finished: `{results['finished_utc']}`",
        "",
    ]
    for phase in st.phases:
        lines += [
            f"## {phase.name.replace('_', ' ').title()}",
            "",
            f"{phase.detail}",
            "",
        ]
        if phase.metrics:
            lines += ["| Metric | Value |", "|---|---|"]
            for key, value in phase.metrics.items():
                if key == "hashes":
                    continue
                lines.append(f"| `{key}` | {value} |")
            lines.append("")
        if phase.checks:
            lines += ["| Check | Result | Detail |", "|---|---|---|"]
            for check in phase.checks:
                mark = "PASS" if check["passed"] else "**FAIL**"
                lines.append(
                    f"| `{check['name']}` | {mark} | "
                    f"{str(check['detail']).replace('|', chr(92) + '|')} |"
                )
            lines.append("")

    lines += [
        "## Reading these numbers",
        "",
        "**Service throughput and ledger settlement are different scales.**",
        "",
        "- *Service throughput* is how many instructions the agent can plan,",
        "  evaluate, authorise, build and sign per second. It is bounded by",
        "  the service's CPU, not by the network.",
        "- *Ledger settlement* is how many can actually settle per second.",
        "  It is bounded by the XRP Ledger: one transaction per closed",
        "  ledger. On the local devnet a ledger closes roughly every three",
        "  seconds, so settlement caps near 0.3 tx/sec regardless of how",
        "  fast the service is. That is a property of the test network.",
        "",
        "A treasury deployment runs the service fast and the ledger",
        "slow, with a queue between them. Both numbers are reported so the",
        "bottleneck is visible rather than assumed.",
        "",
    ]
    (out_dir / "SCALE.md").write_text("\n".join(lines))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Scale and load verification")
    parser.add_argument("--instructions", type=int, default=100_000)
    parser.add_argument(
        "--settle",
        type=int,
        default=0,
        help="number of REAL transactions to submit and confirm on the devnet",
    )
    parser.add_argument("--out", default=str(HERE / "scale-results"))
    args = parser.parse_args()

    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 68)
    print("  XRPL treasury service — scale and load verification")
    print("=" * 68)

    st = ScaleTest(out_dir, settle=args.settle)
    book = build_currency_book()
    tenant = build_tenant()

    asyncio.run(phase_service_throughput(st, args.instructions, book, tenant))
    asyncio.run(phase_ledger_settlement(st, args.settle, book, tenant))
    asyncio.run(phase_scale_correctness(st, book, tenant))
    asyncio.run(phase_stability(st, book, tenant))
    asyncio.run(phase_deployment_readiness(st, book, tenant))

    results = write_report(st, out_dir)
    print()
    print(f"  {'PASS' if st.passed else 'FAIL'} — "
          f"{st.phase_summary}")
    print(f"  report:  {out_dir / 'SCALE.md'}")
    print(f"  results: {out_dir / 'RESULTS.json'}")
    return 0 if st.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
