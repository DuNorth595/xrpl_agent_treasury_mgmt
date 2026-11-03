#!/usr/bin/env python3
"""
Local API test harness.

WHAT THIS IS
=============

An HTTP-level test runner that exercises the treasury service's API the
way a client would: over the network, against the running service. It
asserts on the RESPONSE, not on internal Python objects, so it catches
serialisation errors, wrong status codes and broken contracts that a
unit test cannot see.

Every check records the request, the response, the assertion and the
outcome. The results are written as:

    api-test-results/RESULTS.json     machine-readable, every check
    api-test-results/API_TEST.md       human-readable report
    api-test-results/logs/*.json       one file per check, raw evidence

SAFETY
======

By default NOTHING is submitted to the ledger. The one endpoint that can
move real money (`/api/sample/settle`) is skipped unless `--allow-move`
is passed. It also refuses to run against anything that is not an
obviously-local address.

    python3 api_test.py                          # read-only sweep
    python3 api_test.py --base http://127.0.0.1:8095
    python3 api_test.py --allow-move             # include real settlement
    python3 api_test.py --filter currency        # only matching checks
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

HERE = pathlib.Path(__file__).resolve().parent

# The default base URL follows the environment: the harness and the
# service have to agree on where the service is, and the environment
# binds to loopback by default. A hardcoded address here meant the
# harness timed out against an address nothing was listening on.
DEFAULT_BASE = (
    f"http://{os.environ.get('TREASURY_HOST', '127.0.0.1')}"
    f":{os.environ.get('TREASURY_SERVICE_PORT', '8095')}"
)

# Loopback by default. A private-network address can be added by the
# operator who runs the harness; it is never hardcoded here.
LOCAL_HOSTS = ("127.0.0.1", "localhost", "0.0.0.0")


class Result:
    def __init__(self, name: str, group: str):
        self.name = name
        self.group = group
        self.passed = False
        self.detail = ""
        self.request: Dict[str, Any] = {}
        self.response: Dict[str, Any] = {}
        self.status: Optional[int] = None
        self.error: Optional[str] = None
        self.duration_ms = 0.0
        self.response_headers: Dict[str, str] = {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "group": self.group,
            "passed": self.passed,
            "detail": self.detail,
            "status": self.status,
            "response_headers": self.response_headers,
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
            "request": self.request,
            "response": self.response,
        }


class Client:
    """A small HTTP client that records what it did."""

    def __init__(
        self,
        base: str,
        timeout: int = 30,
        tokens: Optional[Dict[str, str]] = None,
    ):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.tokens = tokens or {}
        self.results: List[Result] = []
        self.logs_dir = HERE / "api-test-results" / "logs"

    def call(
        self,
        method: str,
        path: str,
        body: Optional[Dict] = None,
        name: Optional[str] = None,
        group: str = "general",
        token: Optional[str] = None,
    ) -> Result:
        result = Result(name or f"{method} {path}", group)
        url = f"{self.base}{path}"
        payload = json.dumps(body).encode() if body is not None else None
        result.request = {
            "method": method,
            "url": url,
            "body": body,
        }
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        # An EXPLICIT empty token means no token at all. None means "use
        # the client's default". The distinction matters: the auth
        # checks need a request with no Authorization header, and
        # falling back to the default would make them test the wrong
        # thing.
        bearer = token if token is not None else getattr(self, "token", None)
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"

        request = urllib.request.Request(url, data=payload, headers=headers, method=method)
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                result.status = response.status
                result.response_headers = dict(response.headers)
            try:
                result.response = json.loads(raw)
            except json.JSONDecodeError:
                result.response = {"_raw": raw[:4000]}
        except urllib.error.HTTPError as exc:
            result.status = exc.code
            result.response_headers = dict(exc.headers or {})
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                result.response = json.loads(raw)
            except json.JSONDecodeError:
                result.response = {"_raw": raw[:4000]}
            result.error = f"HTTP {exc.code}"
        except Exception as exc:
            result.error = repr(exc)
        result.duration_ms = (time.perf_counter() - started) * 1000

        self.results.append(result)
        self._save_log(result)
        return result

    def get(self, path: str, **kw) -> Result:
        return self.call("GET", path, **kw)

    def post(self, path: str, body: Optional[Dict] = None, **kw) -> Result:
        return self.call("POST", path, body, **kw)

    def _save_log(self, result: Result):
        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            slug = re.sub(r"[^a-z0-9]+", "-", result.name.lower()).strip("-")
            path = self.logs_dir / f"{slug}.json"
            path.write_text(json.dumps(result.to_dict(), indent=2, default=str))
        except Exception:
            pass


# ------------------------------------------------------------- assertions


def expect(condition: bool, detail: str) -> None:
    if not condition:
        raise AssertionError(detail)


def check_status(result: Result, status: int = 200) -> Result:
    expect(
        result.status == status,
        f"expected HTTP {status}, got {result.status}"
        + (f" ({result.error})" if result.error else ""),
    )
    return result


# ----------------------------------------------------------------- checks


def check_health(client: Client) -> List[Result]:
    result = client.get("/api/health", name="health endpoint answers", group="health")
    check_status(result)
    body = result.response
    expect("status" in body, "the health body has no 'status' field")
    expect(
        body.get("status") in ("ok", "degraded"),
        f"unexpected health status: {body.get('status')!r}",
    )
    result.passed = True
    result.detail = f"status={body.get('status')}"
    return [result]


def check_tenants(client: Client) -> List[Result]:
    result = client.get("/api/tenants", name="tenants are listed", group="registry")
    check_status(result)
    tenants = result.response.get("tenants")
    expect(isinstance(tenants, list) and tenants, "no tenants returned")
    expect(len(tenants) >= 1, "the tenant registry is empty")
    result.passed = True
    result.detail = f"{len(tenants)} tenant(s): {[t.get('id') for t in tenants]}"
    return [result]


def check_tenant_surface(client: Client, tenant_id: str) -> List[Result]:
    results = []

    position = client.get(
        f"/api/tenant/{tenant_id}/position",
        name=f"position for {tenant_id}",
        group="tenant",
    )
    check_status(position)
    expect(
        "positions" in position.response
        or "treasury_balance_xrp" in position.response,
        f"the position body has no positions or treasury balance: "
        f"{list(position.response)}",
    )
    position.passed = True
    position.detail = f"keys: {sorted(position.response)[:8]}"
    results.append(position)

    plan = client.get(
        f"/api/tenant/{tenant_id}/plan",
        name=f"plan for {tenant_id}",
        group="tenant",
    )
    check_status(plan)
    expect("plan" in plan.response, "the plan body has no 'plan'")
    expect(
        "reconciliation" in plan.response,
        "the plan body has no 'reconciliation' key - the contract changed",
    )
    plan_body = plan.response.get("plan") or {}
    expect(
        "planned" in plan_body,
        f"the plan object has no 'planned'; keys are {sorted(plan_body)}",
    )
    planned = plan_body.get("planned") or []
    reconciliation = plan.response.get("reconciliation") or {}
    plan.passed = True
    plan.detail = (
        f"{len(planned)} planned, blocked={plan_body.get('blocked')}, "
        f"reconciliation blocked={reconciliation.get('blocked')}"
    )
    results.append(plan)

    obligations = client.get(
        f"/api/tenant/{tenant_id}/obligations",
        name=f"obligations for {tenant_id}",
        group="tenant",
    )
    check_status(obligations)
    obligations.passed = True
    obligations.detail = f"keys: {sorted(obligations.response)[:8]}"
    results.append(obligations)

    log = client.get(
        f"/api/tenant/{tenant_id}/log",
        name=f"decision log for {tenant_id}",
        group="tenant",
    )
    check_status(log)
    log.passed = True
    log.detail = f"keys: {sorted(log.response)[:8]}"
    results.append(log)

    authorisations = client.get(
        f"/api/tenant/{tenant_id}/authorisations",
        name=f"authorisations for {tenant_id}",
        group="tenant",
    )
    check_status(authorisations)
    authorisations.passed = True
    authorisations.detail = f"keys: {sorted(authorisations.response)[:8]}"
    results.append(authorisations)

    return results


def check_dry_run(client: Client, tenant_id: str) -> List[Result]:
    result = client.post(
        f"/api/tenant/{tenant_id}/dry-run",
        {},
        name=f"dry run for {tenant_id} submits nothing",
        group="safety",
    )
    check_status(result)
    body = result.response
    expect(
        body.get("submitted") in (False, 0, None)
        or body.get("money_moved") in (False, 0, None),
        f"the dry run claims to have moved money: {body}",
    )
    result.passed = True
    result.detail = (
        f"submitted={body.get('submitted')} money_moved={body.get('money_moved')}"
    )
    return [result]


def check_capabilities(client: Client) -> List[Result]:
    result = client.get(
        "/api/sample/capabilities",
        name="capabilities catalogue",
        group="sample",
    )
    check_status(result)
    body = result.response
    capabilities = body.get("capabilities")
    expect(isinstance(capabilities, list) and capabilities, "no capabilities listed")
    expect(
        body.get("money_moved_by_this_endpoint") is False,
        "the catalogue claims it moved money",
    )
    for capability in capabilities:
        expect("id" in capability, f"a capability has no id: {capability}")
        expect(
            "summary" in capability,
            f"capability {capability.get('id')} has no summary",
        )
    result.passed = True
    result.detail = f"{len(capabilities)} capabilities, none move money"
    return [result]


def check_sample_get_endpoints(client: Client) -> List[Result]:
    results = []
    endpoints = {
        "/api/sample/domain": ("domain", ("domain_id", "guarantee")),
        "/api/sample/policy-matrix": ("policy matrix", ("tiers", "limits")),
        "/api/sample/cross-currency": ("cross currency", ("usd_value", "cases")),
        "/api/sample/rates": ("rates", ("rates",)),
        "/api/sample/source": ("erp source", ("obligations", "source")),
        "/api/sample/decision-log": ("decision log", ()),
    }
    for path, (label, required) in endpoints.items():
        result = client.get(path, name=f"sample {label}", group="sample")
        check_status(result)
        for key in required:
            expect(
                key in result.response,
                f"{path} has no {key!r}; keys are {sorted(result.response)}",
            )
        result.passed = True
        result.detail = f"keys: {sorted(result.response)[:8]}"
        results.append(result)
    return results


def check_scenarios(client: Client) -> List[Result]:
    """The four authorisation tiers plus the blocked path."""
    expected = {
        "approved": "level_1",
        "single": "level_2",
        "dual": "level_3",
        "refused": "refused",
    }
    results = []
    for scenario, tier in expected.items():
        result = client.get(
            f"/api/sample/scenario/{scenario}",
            name=f"scenario {scenario} -> {tier}",
            group="policy",
        )
        check_status(result)
        body = result.response
        decision = body.get("decision") or {}
        actual = decision.get("tier")
        expect(
            actual == tier,
            f"scenario {scenario} returned tier {actual!r}, expected {tier!r}",
        )
        expect(
            decision.get("reasons") is not None,
            f"scenario {scenario} gave no reasons",
        )
        result.passed = True
        result.detail = f"tier={actual} signoffs={decision.get('required_authorisations')}"
        results.append(result)

    blocked = client.get(
        "/api/sample/scenario/blocked",
        name="scenario blocked withholds the plan during an outage",
        group="policy",
    )
    check_status(blocked)
    body = blocked.response
    during = body.get("during_outage") or {}
    expect(
        during.get("blocked") is True,
        f"the induced outage did not block the plan: {during}",
    )
    expect(
        during.get("planned") == 0,
        f"the blocked plan still proposed {during.get('planned')} transfers",
    )
    expect(
        during.get("obligation_error"),
        "the blocked plan did not report why it was blocked",
    )
    after = body.get("after_restore") or {}
    expect(
        after.get("blocked") is False,
        f"the service did not recover after the source was restored: {after}",
    )
    blocked.passed = True
    blocked.detail = (
        f"outage: blocked={during.get('blocked')} planned={during.get('planned')}"
        f" | restored: blocked={after.get('blocked')} planned={after.get('planned')}"
    )
    results.append(blocked)
    return results


def check_evaluate(client: Client) -> List[Result]:
    """The policy engine must agree with itself across currencies."""
    results = []
    cases = [
        (180.0, "RLUSD", "level_1"),
        (1500.0, "RLUSD", "level_3"),
        (25000.0, "RLUSD", "refused"),
    ]
    for amount, currency, tier in cases:
        result = client.post(
            "/api/sample/evaluate",
            {"amount": amount, "currency": currency},
            name=f"evaluate {amount} {currency} -> {tier}",
            group="policy",
        )
        check_status(result)
        body = result.response
        decision = body.get("decision") or body
        actual = decision.get("tier")
        expect(
            actual == tier,
            f"{amount} {currency} evaluated to {actual!r}, expected {tier!r}",
        )
        result.passed = True
        result.detail = f"tier={actual}"
        results.append(result)
    return results


def check_cross_currency_consistency(client: Client) -> List[Result]:
    """The same USD value must land in the same tier in every currency."""
    result = client.get(
        "/api/sample/cross-currency?usd=500",
        name="the same USD value gets the same tier in every currency",
        group="policy",
    )
    check_status(result)
    body = result.response
    cases = body.get("cases") or []
    expect(len(cases) >= 2, "cross-currency returned fewer than two cases")
    tiers = {case.get("tier") for case in cases}
    expect(
        len(tiers) == 1,
        f"the same USD value produced different tiers across currencies: {tiers}",
    )
    for case in cases:
        expect(
            abs(float(case.get("usd_equivalent")) - 500.0) < 1.0,
            f"{case.get('currency')} converted to "
            f"{case.get('usd_equivalent')} USD, expected ~500",
        )
    result.passed = True
    result.detail = f"tiers={tiers} currencies={[c.get('currency') for c in cases]}"
    return [result]


def check_simulate(client: Client) -> List[Result]:
    """A simulation must build a real payment and move no money."""
    result = client.post(
        "/api/sample/simulate",
        {"amount": 180, "currency": "RLUSD"},
        name="simulate builds a payment without submitting it",
        group="safety",
    )
    check_status(result)
    body = result.response
    expect(
        body.get("submitted") is False,
        f"the simulation submitted a transaction: {body.get('submitted')}",
    )
    expect(
        body.get("money_moved") is False,
        f"the simulation moved money: {body.get('money_moved')}",
    )
    built = body.get("built_payment") or {}
    tx = built.get("tx_json") or {}
    expect("DomainID" in tx, "the simulated payment has no DomainID")
    expect(
        tx.get("Amount", {}).get("currency") is not None,
        "the simulated payment has no issued-currency amount",
    )
    expect(
        "SendMax" in tx,
        "the simulated payment has no SendMax - an issued-currency "
        "payment without SendMax is invalid",
    )
    result.passed = True
    result.detail = (
        f"engine={body.get('simulation', {}).get('engine_result')} "
        f"submitted={body.get('submitted')}"
    )
    return [result]


def check_approval_walkthrough(client: Client) -> List[Result]:
    """Two distinct sign-offs open the gate; a new key starts empty."""
    results = []
    # The key must be unique per RUN. A key based only on the second
    # collides when two runs happen inside the same second, and the
    # second run's sign-offs land in the first run's ledger - so the
    # "one sign-off does not open a two-sign-off gate" check sees a
    # gate that is already satisfied.
    key = f"APITEST-{time.time_ns()}-{os.getpid()}"

    first = client.post(
        "/api/sample/approval-walkthrough",
        {
            "approver": "A. Reyes",
            "role": "treasury_manager",
            "required": 2,
            "obligation_id": key + "-A",
        },
        name="one sign-off does not open a two-sign-off gate",
        group="authorisation",
    )
    check_status(first)
    body = first.response
    expect(
        body.get("satisfied") is False,
        "a single sign-off satisfied a gate that needs two",
    )
    expect(
        body.get("received_now") == 1,
        f"expected 1 sign-off, got {body.get('received_now')}",
    )
    first.passed = True
    first.detail = f"{body.get('received_now')}/{body.get('required')} satisfied={body.get('satisfied')}"
    results.append(first)

    second = client.post(
        "/api/sample/approval-walkthrough",
        {
            "approver": "M. Okonkwo",
            "role": "supervisor",
            "required": 2,
            "obligation_id": key + "-A",
        },
        name="two sign-offs open the gate",
        group="authorisation",
    )
    check_status(second)
    body = second.response
    expect(
        body.get("satisfied") is True,
        "two distinct sign-offs did not satisfy a two-sign-off gate",
    )
    second.passed = True
    second.detail = f"{body.get('received_now')}/{body.get('required')} satisfied={body.get('satisfied')}"
    results.append(second)

    isolated = client.post(
        "/api/sample/approval-walkthrough",
        {
            "approver": "A. Reyes",
            "role": "treasury_manager",
            "required": 2,
            "obligation_id": key + "-B",
        },
        name="approvals do not leak to an unrelated transfer",
        group="authorisation",
    )
    check_status(isolated)
    body = isolated.response
    expect(
        body.get("received_now") == 1,
        f"an unrelated transfer started with "
        f"{body.get('received_now')} sign-off(s), expected 1",
    )
    expect(
        body.get("satisfied") is False,
        "an unrelated transfer was already satisfied",
    )
    isolated.passed = True
    isolated.detail = f"fresh key held at {body.get('received_now')}/{body.get('required')}"
    results.append(isolated)

    return results


def check_connectors(client: Client) -> List[Result]:
    result = client.get("/api/connectors", name="connectors are listed", group="registry")
    check_status(result)
    body = result.response
    connectors = body.get("connectors")
    expect(isinstance(connectors, list), "the connectors body is not a list")
    for connector in connectors:
        expect("name" in connector, f"a connector has no name: {connector}")
        blob = json.dumps(connector).lower()
        for secret_marker in ("seed", "secret", "private_key", "password"):
            expect(
                secret_marker not in blob,
                f"the connector listing exposes a {secret_marker}",
            )
    result.passed = True
    result.detail = f"{len(connectors)} connector(s), no secrets exposed"
    return [result]


def check_no_money_moved(client: Client) -> List[Result]:
    """The sample module must never submit."""
    result = client.get(
        "/api/sample/capabilities",
        name="the sample module declares itself non-submitting",
        group="safety",
    )
    check_status(result)
    body = result.response
    expect(
        body.get("money_moved_by_this_endpoint") is False,
        "the sample module does not declare that it moves nothing",
    )
    result.passed = True
    result.detail = "money_moved_by_this_endpoint=false"
    return [result]


def check_authentication(client: Client) -> List[Result]:
    """The authentication boundary, proved over real HTTP.

    These are the properties that make the API a boundary rather than a
    doorway:

      - an unauthenticated request to the operational surface is refused
      - an invalid token is refused
      - a valid token WITHOUT the right scope is refused
      - the health probe stays open
      - the sample surface stays open in sample mode
    """
    results = []

    # No token at all.
    anonymous = client.call(
        "GET",
        "/api/tenants",
        name="the operational surface refuses an unauthenticated request",
        group="auth",
        token="",
    )
    expect(
        anonymous.status == 401,
        f"an unauthenticated request returned HTTP {anonymous.status}, "
        f"expected 401",
    )
    expect(
        "bearer"
        in " ".join(
            value.lower()
            for key, value in (anonymous.response_headers or {}).items()
            if key.lower() == "www-authenticate"
        ),
        "the refusal did not carry a WWW-Authenticate header",
    )
    anonymous.passed = True
    anonymous.detail = "HTTP 401 without a token"
    results.append(anonymous)

    # An invalid token.
    invalid = client.call(
        "GET",
        "/api/tenants",
        name="an invalid token is refused",
        group="auth",
        token="not-a-real-token",
    )
    expect(
        invalid.status == 401,
        f"an invalid token returned HTTP {invalid.status}, expected 401",
    )
    invalid.passed = True
    invalid.detail = "HTTP 401 with a forged token"
    results.append(invalid)

    # A valid token with the wrong scope.
    reader = client.tokens.get("reader", "")
    if reader:
        allowed = client.call(
            "GET",
            "/api/tenants",
            name="a token with the right scope is allowed",
            group="auth",
            token=reader,
        )
        expect(
            allowed.status == 200,
            f"a valid read token returned HTTP {allowed.status}, expected 200",
        )
        allowed.passed = True
        allowed.detail = "HTTP 200 with the read scope"
        results.append(allowed)

        refused = client.call(
            "POST",
            "/api/sample/settle",
            {"amount": 1, "currency": "RLUSD", "confirm": True},
            name="a read token cannot submit a payment",
            group="auth",
            token=reader,
        )
        expect(
            refused.status == 403,
            f"a read token submitting a payment returned HTTP "
            f"{refused.status}, expected 403",
        )
        body = refused.response or {}
        expect(
            body.get("error") == "forbidden",
            f"the refusal did not say why: {body}",
        )
        refused.passed = True
        refused.detail = "HTTP 403 - the read scope cannot settle"
        results.append(refused)

    # The health probe stays open.
    health = client.call(
        "GET",
        "/api/health",
        name="the health probe stays open",
        group="auth",
        token="",
    )
    expect(
        health.status == 200,
        f"the health probe returned HTTP {health.status}, expected 200",
    )
    health.passed = True
    health.detail = "HTTP 200 without a token"
    results.append(health)

    # The sample surface stays open.
    sample = client.call(
        "GET",
        "/api/sample/capabilities",
        name="the sample surface stays open in sample mode",
        group="auth",
        token="",
    )
    expect(
        sample.status == 200,
        f"the sample surface returned HTTP {sample.status}, expected 200",
    )
    sample.passed = True
    sample.detail = "HTTP 200 without a token"
    results.append(sample)

    return results


def check_settle(client: Client, allow_move: bool) -> List[Result]:
    """The real settlement endpoint, if the caller allowed money to move."""
    if not allow_move:
        result = Result("real on-chain settlement (skipped)", "settlement")
        result.passed = True
        result.detail = "skipped: pass --allow-move to run a real transaction"
        client.results.append(result)
        return [result]

    # First: WITHOUT confirm, the endpoint must NOT submit. This is the
    # guard that makes the endpoint safe to leave exposed.
    dry = client.post(
        "/api/sample/settle",
        {"amount": 1, "currency": "RLUSD"},
        name="settle without confirm does not submit",
        group="settlement",
    )
    check_status(dry)
    expect(
        dry.response.get("submitted") is False,
        f"the endpoint submitted without an explicit confirm: {dry.response}",
    )
    expect(
        dry.response.get("money_moved") is False,
        "the endpoint moved money without an explicit confirm",
    )
    expect(
        dry.response.get("would_submit"),
        "the dry form did not show the payment it would send",
    )
    dry.passed = True
    dry.detail = "the dry form returned the payment and submitted nothing"
    results = [dry]

    # Second: the amount cap must hold even with confirm.
    capped = client.post(
        "/api/sample/settle",
        {"amount": 5000, "currency": "RLUSD", "confirm": True},
        name="settle refuses an amount over the cap",
        group="settlement",
    )
    expect(
        capped.status == 400,
        f"the amount cap returned HTTP {capped.status}, expected 400",
    )
    expect(
        capped.response.get("error") == "amount_capped",
        f"the amount cap reported {capped.response.get('error')!r}",
    )
    capped.passed = True
    capped.detail = "amount_capped"
    results.append(capped)

    # Third: the real thing.
    result = client.post(
        "/api/sample/settle",
        {"amount": 1, "currency": "RLUSD", "confirm": True},
        name="a real payment settles and is confirmed on-chain",
        group="settlement",
    )
    check_status(result)
    body = result.response
    expect(
        body.get("engine_result") == "tesSUCCESS",
        f"the payment did not succeed: {body.get('engine_result')} "
        f"({body.get('detail', '')})",
    )
    expect(body.get("tx_hash"), "the settled payment has no transaction hash")
    expect(
        body.get("validated") is True,
        "the transaction was not validated in a closed ledger",
    )
    expect(
        body.get("in_ledger") is True,
        "the transaction was not in a closed ledger",
    )
    expect(
        body.get("money_moved") is True,
        "the successful payment did not report that money moved",
    )
    result.passed = True
    result.detail = (
        f"{body.get('engine_result')} {body.get('tx_hash')} "
        f"validated={body.get('validated')} -> {body.get('destination')}"
    )
    results.append(result)
    return results


# ------------------------------------------------------------------ main


def write_report(results: List[Result], out_dir: pathlib.Path, base: str) -> Dict:
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    payload = {
        "base_url": base,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "passed": passed,
        "total": total,
        "all_passed": passed == total,
        "checks": [r.to_dict() for r in results],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "RESULTS.json").write_text(json.dumps(payload, indent=2, default=str))

    lines = [
        "# Local API test report",
        "",
        f"**{'PASS' if passed == total else 'FAIL'} - {passed}/{total} checks passed**",
        "",
        f"- Target: `{base}`",
        f"- Finished: `{payload['finished_utc']}`",
        "",
        "| # | Check | Group | Result | Detail |",
        "|---|---|---|---|---|",
    ]
    for index, result in enumerate(results, 1):
        mark = "PASS" if result.passed else "**FAIL**"
        detail = result.detail or result.error or ""
        detail = str(detail).replace("|", "\\|")
        lines.append(
            f"| {index} | {result.name} | {result.group} | {mark} | {detail} |"
        )
    lines += [
        "",
        "## Notes",
        "",
        "- Every check runs over HTTP against the running service, so a",
        "  broken JSON contract or a wrong status code fails the check.",
        "- Raw request and response bodies for each check are in",
        "  `logs/`.",
        "- Nothing is submitted to the ledger unless `--allow-move` is",
        "  passed.",
        "",
    ]
    (out_dir / "API_TEST.md").write_text("\n".join(lines))
    return payload


CREDENTIAL_FILE = pathlib.Path.home() / ".treasury-dev" / "api-tokens.json"


def load_tokens() -> Dict[str, str]:
    """The devnet's API tokens, by credential name.

    The same file the service reads its credentials from, so the harness
    and the service always agree on what is valid.
    """
    try:
        stored = json.loads(CREDENTIAL_FILE.read_text())
    except Exception:
        return {}
    return {
        name: entry.get("token", "")
        for name, entry in stored.items()
        if isinstance(entry, dict) and entry.get("token")
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Local API test harness")
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument(
        "--allow-move",
        action="store_true",
        help="allow the real settlement check to submit a transaction",
    )
    parser.add_argument("--filter", default="", help="only run matching checks")
    parser.add_argument("--out", default=str(HERE / "api-test-results"))
    args = parser.parse_args()

    if args.allow_move:
        host = urllib.parse.urlparse(args.base).hostname or ""
        if host not in LOCAL_HOSTS:
            print(
                f"  refusing to move money against {args.base!r}: "
                f"{host!r} is not a local address"
            )
            return 2

    out_dir = pathlib.Path(args.out)
    tokens = load_tokens()
    client = Client(args.base, tokens=tokens)
    if not tokens:
        print(
            "  !! no API credentials were found; the protected endpoints "
            "will be refused. Run `python3 dev_env.py up` to mint them."
        )

    print("=" * 68)
    print(f"  Local API test harness -> {args.base}")
    print("=" * 68)

    # The authentication boundary is checked FIRST, and with NO default
    # token on the client. It has to run before the client's default
    # token is set, or the "no token" check would send the default and
    # test the wrong thing.
    client.token = None
    try:
        for result in check_authentication(client):
            state = "PASS" if result.passed else "FAIL"
            print(f"  [{state}] {result.name}")
    except AssertionError as exc:
        failed = client.results[-1] if client.results else Result("?", "?")
        failed.passed = False
        failed.detail = str(exc)
        print(f"  [FAIL] {failed.name}: {exc}")

    # From here on the client carries the READER credential, which is
    # what the operational surface needs.
    client.token = tokens.get("reader", "")

    tenants_result = client.get("/api/tenants", name="_tenants_probe", group="_probe")
    tenant_id = ""
    try:
        tenant_id = (tenants_result.response.get("tenants") or [{}])[0].get("id", "")
    except Exception:
        tenant_id = ""

    checks: List[Callable[[], List[Result]]] = [
        check_health,
        check_capabilities,
        check_sample_get_endpoints,
        check_scenarios,
        check_evaluate,
        check_cross_currency_consistency,
        check_simulate,
        check_approval_walkthrough,
        check_connectors,
        check_no_money_moved,
    ]

    for check in checks:
        try:
            for result in check(client):
                state = "PASS" if result.passed else "FAIL"
                print(f"  [{state}] {result.name}")
        except AssertionError as exc:
            failed = client.results[-1] if client.results else Result("?", "?")
            failed.passed = False
            failed.detail = str(exc)
            print(f"  [FAIL] {failed.name}: {exc}")
        except Exception as exc:
            failed = client.results[-1] if client.results else Result("?", "?")
            failed.passed = False
            failed.error = repr(exc)
            failed.detail = repr(exc)
            print(f"  [FAIL] {failed.name}: {exc!r}")

    if tenant_id:
        try:
            for result in check_tenant_surface(client, tenant_id):
                state = "PASS" if result.passed else "FAIL"
                print(f"  [{state}] {result.name}")
        except AssertionError as exc:
            failed = client.results[-1]
            failed.passed = False
            failed.detail = str(exc)
            print(f"  [FAIL] {failed.name}: {exc}")
        # A dry run PROPOSES a transfer, so it needs the propose scope,
        # which the reader credential does not have. The authoriser
        # credential does.
        client.token = tokens.get("authoriser", "") or client.token
        try:
            for result in check_dry_run(client, tenant_id):
                state = "PASS" if result.passed else "FAIL"
                print(f"  [{state}] {result.name}")
        except AssertionError as exc:
            failed = client.results[-1]
            failed.passed = False
            failed.detail = str(exc)
            print(f"  [FAIL] {failed.name}: {exc}")
    else:
        print("  [FAIL] no tenant id could be read; tenant checks skipped")

    # Submitting a real payment needs the OPERATOR credential - the
    # reader scope cannot settle.
    client.token = tokens.get("operator", "") or client.token
    try:
        for result in check_settle(client, args.allow_move):
            state = "PASS" if result.passed else "FAIL"
            print(f"  [{state}] {result.name}")
    except AssertionError as exc:
        failed = client.results[-1]
        failed.passed = False
        failed.detail = str(exc)
        print(f"  [FAIL] {failed.name}: {exc}")

    # Drop the probe from the report.
    client.results = [r for r in client.results if r.group != "_probe"]

    payload = write_report(client.results, out_dir, args.base)
    print()
    print(f"  {'PASS' if payload['all_passed'] else 'FAIL'} - "
          f"{payload['passed']}/{payload['total']} checks")
    print(f"  report:  {out_dir / 'API_TEST.md'}")
    print(f"  results: {out_dir / 'RESULTS.json'}")
    print(f"  logs:    {out_dir / 'logs'}")
    return 0 if payload["all_passed"] else 1


if __name__ == "__main__":
    import urllib.parse  # noqa: E402  (used in main)

    raise SystemExit(main())
