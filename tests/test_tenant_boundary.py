"""
Tests for the tenant boundary at the API edge.

WHY THIS EXISTS
===============

Three defects were found by reviewing the routes one at a time rather
than by the existing suite:

  1. An unknown tenant produced a 500 with an HTML stack trace, because
     `registry.get()` raises TenantError and nothing handled it.
  2. `/obligations` never resolved the tenant, so it answered for a
     tenant that is not configured - with the REAL obligations, which
     belong to whichever tenant does exist.
  3. `/log` had the same problem.

Defect 2 is the one that mattered. The response looked correct. It
contained obligations. They were just not that tenant's.

A route that names a tenant must resolve that tenant before it does
anything else. That is the rule these tests enforce.
"""

from __future__ import annotations

import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT / "src"))

from xrpl_agent_treasury_mgmt.auth import (  # noqa: E402
    SCOPE_READ,
    CredentialStore,
    mint_credential,
)
from xrpl_agent_treasury_mgmt.service import TreasuryService, create_app  # noqa: E402


TENANT_ROUTES = [
    "/api/tenant/{tenant}/position",
    "/api/tenant/{tenant}/plan",
    "/api/tenant/{tenant}/obligations",
    "/api/tenant/{tenant}/log",
    "/api/tenant/{tenant}/authorisations",
]

REAL_TENANT = "s-devlab-corp"


class TestTheTenantBoundary(unittest.TestCase):
    """Every tenant route resolves the tenant, or it answers 404."""

    def setUp(self):
        self.app, self.token = _build_app()
        self.client = self.app.test_client()
        self.auth = {"Authorization": f"Bearer {self.token}"}

    def _get(self, path):
        return self.client.get(path, headers=self.auth)

    def test_an_unknown_tenant_is_a_404_not_a_500(self):
        """The original defect: an unhandled TenantError became a 500."""
        response = self._get("/api/tenant/no-such-tenant/position")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "unknown_tenant")

    def test_every_tenant_route_rejects_an_unknown_tenant(self):
        """Including the two that used to answer with real data."""
        for route in TENANT_ROUTES:
            path = route.format(tenant="no-such-tenant")
            response = self._get(path)
            self.assertEqual(
                response.status_code,
                404,
                f"{path} answered {response.status_code} for an unknown tenant",
            )

    def test_the_error_is_json_not_html(self):
        """A 500 with an HTML stack trace leaks internals. A 404 does not."""
        response = self._get("/api/tenant/no-such-tenant/position")
        self.assertIn("application/json", response.headers.get("Content-Type", ""))
        body = response.get_data(as_text=True)
        self.assertNotIn("Traceback", body)
        self.assertNotIn('File "', body)

    def test_an_unknown_route_is_json(self):
        response = self._get("/api/definitely-not-a-route")
        # The scope gate refuses it before a handler is reached, so it is
        # a 403 rather than a 404. Either way it is JSON, not HTML.
        self.assertIn(response.status_code, (403, 404))
        self.assertIn("application/json", response.headers.get("Content-Type", ""))

    def test_a_known_tenant_still_works(self):
        """The fix must not break the normal path."""
        response = self._get(f"/api/tenant/{REAL_TENANT}/position")
        self.assertEqual(response.status_code, 200)
        self.assertIn("tenant", response.get_json())

    def test_obligations_for_an_unknown_tenant_returns_nothing(self):
        """The data-leak case.

        Before the fix this returned the real obligations under a
        tenant id that does not exist. It must return nothing.
        """
        response = self._get("/api/tenant/no-such-tenant/obligations")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("obligations", response.get_json())

    def test_the_log_for_an_unknown_tenant_returns_nothing(self):
        response = self._get("/api/tenant/no-such-tenant/log")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("entries", response.get_json())

    def test_the_tenant_is_resolved_before_the_ledger_is_touched(self):
        """The boundary holds even when the ledger is unreachable.

        The ledger in this test refuses everything. If the route
        reached the ledger before resolving the tenant, the failure
        would be a 500 from the ledger rather than a clean 404.
        """
        response = self._get("/api/tenant/no-such-tenant/position")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("unavailable", response.get_data(as_text=True))


def _build_app():
    """A minimal app with one tenant and no ledger.

    The tenant boundary is a configuration concern - it must hold even
    when the ledger is unreachable. That is the point of the test.
    """
    from xrpl_agent_treasury_mgmt.agent_log import AgentLog
    from xrpl_agent_treasury_mgmt.obligations import InMemoryObligationSource
    from xrpl_agent_treasury_mgmt.tenants import Limits, Tenant, TenantRegistry

    registry = TenantRegistry()
    registry.add(
        Tenant(
            id=REAL_TENANT,
            name="S_DevLabs",
            domain_id="B9263342B5E4C1F2BB8A691647B94FEEF53995DABFF6B88B",
            # A syntactically valid classic address is enough here. The
            # tenant boundary is a configuration concern; it must hold on a
            # clean checkout with no local devnet account file.
            treasury_account="rTestTreasuryAccountForBoundaryChecks",
            currency="RLUSD",
            cost_centres={},
            limits=Limits(),
        )
    )

    class _NoLedger:
        """A ledger that refuses everything.

        The tenant boundary has to hold when the ledger is down. If a
        route can be reached by making the ledger fail, the test is
        testing the wrong thing.
        """

        def __getattr__(self, name):
            def _refuse(*args, **kwargs):
                raise RuntimeError("the ledger is unavailable")

            return _refuse

    credential, token = mint_credential("ops", [SCOPE_READ])
    store = CredentialStore([credential])

    service = TreasuryService(
        registry=registry,
        ledger=_NoLedger(),
        obligation_source=InMemoryObligationSource(obligations=[]),
        agent_log=AgentLog(),
    )
    return create_app(service, sample_mode=False, auth_store=store), token


if __name__ == "__main__":
    unittest.main()
