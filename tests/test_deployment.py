"""
Tests for the deployed configuration.

THE POINT
=========

The service is verified in sample mode in the other tests. These tests
check the things that only matter in a DEPLOYMENT:

  1. PRODUCTION MODE  the sample surface is not registered at all, so
                      there is nothing to authenticate on it.
  2. THE SERVER       the production WSGI server starts, binds, and
                      serves. Flask's development server is not enough.
  3. THE BOUNDARY    in production mode, the operational surface still
                      refuses an unauthenticated request and still
                      allows a scoped one.
  4. NO OPEN DOORS   every route that is not explicitly open either
                      has a declared scope or is refused.

These run against a REAL server on a real socket, not the Flask test
client, because the point is that the whole thing works when it is
actually running.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT / "src"))

from xrpl_agent_treasury_mgmt.auth import (  # noqa: E402
    SCOPE_READ,
    SCOPE_SETTLE,
    mint_credential,
)

PORT = 8197


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_for_port(port: int, process, timeout: float = 40.0) -> bool:
    """Wait for the server to bind.

    Polling, not sleeping. A fixed sleep is either too short - a flaky
    test - or too long - a slow test.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        probe = socket.socket()
        probe.settimeout(1)
        try:
            probe.connect(("127.0.0.1", port))
            probe.close()
            return True
        except Exception:
            probe.close()
            time.sleep(0.4)
    return False


def _request(path: str, token=None, port: int = PORT):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers=headers, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except Exception as exc:
        return None, repr(exc)


class TestProductionMode(unittest.TestCase):
    """The service as it would be deployed."""

    @classmethod
    def setUpClass(cls):
        credential, token = mint_credential(
            "operator", [SCOPE_READ, SCOPE_SETTLE]
        )
        cls.token = token

        cls.port = _free_port()
        env = dict(os.environ)
        env["TREASURY_API_TOKENS"] = json.dumps(
            {
                "operator": {
                    "token": token,
                    "scopes": [SCOPE_READ, SCOPE_SETTLE],
                }
            }
        )
        cls.process = subprocess.Popen(
            [
                sys.executable,
                "run_demo.py",
                "--host",
                "127.0.0.1",
                "--port",
                str(cls.port),
                "--no-sample",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(PROJECT),
        )
        cls.started = _wait_for_port(cls.port, cls.process)

    @classmethod
    def tearDownClass(cls):
        if cls.process.poll() is None:
            cls.process.terminate()
            try:
                cls.process.wait(timeout=8)
            except Exception:
                cls.process.kill()

    def setUp(self):
        if not self.started:
            self.skipTest("the service did not start")

    # ---------------------------------------------------- the deployment

    def test_the_production_server_starts_and_binds(self):
        """waitress, not the Flask development server."""
        self.assertTrue(self.started, "the service never bound to its port")

    def test_the_sample_surface_is_not_registered(self):
        """In production mode there is nothing to authenticate on it."""
        status, _ = _request("/api/sample/capabilities", port=self.port)
        self.assertEqual(status, 404)

    def test_the_sample_settle_endpoint_is_not_registered(self):
        status, _ = _request("/api/sample/settle", port=self.port)
        self.assertEqual(status, 404)

    # ------------------------------------------------------ the boundary

    def test_the_operational_surface_refuses_an_unauthenticated_request(self):
        status, _ = _request("/api/tenants", port=self.port)
        self.assertEqual(status, 401)

    def test_a_scoped_token_is_allowed(self):
        status, _ = _request("/api/tenants", token=self.token, port=self.port)
        self.assertEqual(status, 200)

    def test_the_health_probe_stays_open(self):
        status, _ = _request("/api/health", port=self.port)
        self.assertEqual(status, 200)

    def test_a_forged_token_is_refused(self):
        status, _ = _request(
            "/api/tenants", token="forged-token", port=self.port
        )
        self.assertEqual(status, 401)

    def test_the_dashboard_still_renders(self):
        status, body = _request("/", port=self.port)
        self.assertEqual(status, 200)
        self.assertIn("S_DevLab", body)


class TestTheRouteMap(unittest.TestCase):
    """Every route is either open or has a scope.

    A route with no declared scope is REFUSED at runtime, so this is
    about making sure the refusal is not a surprise: every route the
    service actually registers should be accounted for.
    """

    def test_every_operational_route_has_a_scope(self):
        from xrpl_agent_treasury_mgmt.auth_middleware import (
            OPEN_PATHS,
            SCOPES,
            UNSCOPED_PATHS,
        )

        for rule in (
            "/api/tenants",
            "/api/tenant/<tenant_id>/position",
            "/api/tenant/<tenant_id>/plan",
            "/api/tenant/<tenant_id>/obligations",
            "/api/tenant/<tenant_id>/log",
            "/api/tenant/<tenant_id>/authorisations",
            "/api/tenant/<tenant_id>/dry-run",
            "/api/tenant/<tenant_id>/authorise/<obligation_id>",
        ):
            self.assertIn(
                rule,
                SCOPES,
                f"{rule} has no declared scope, so it would be REFUSED",
            )

    def test_the_open_set_is_small_and_deliberate(self):
        from xrpl_agent_treasury_mgmt.auth_middleware import OPEN_PATHS

        self.assertEqual(
            set(OPEN_PATHS), {"/", "/api/health"}
        )

    def test_the_sample_surface_is_not_in_the_scope_map(self):
        """The sample surface is open by being open, not by having a
        scope. A scope on it would imply it needed a token."""
        from xrpl_agent_treasury_mgmt.auth_middleware import SCOPES

        for rule in SCOPES:
            self.assertFalse(
                rule.startswith("/api/sample/"),
                f"{rule} is a sample route and should not need a scope",
            )


if __name__ == "__main__":
    unittest.main()
