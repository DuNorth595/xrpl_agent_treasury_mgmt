"""
Tests for API authentication and authorisation.

WHAT IS BEING TESTED
====================

The properties that matter, in order of how much they matter:

  1. An unauthenticated request to the operational surface is REFUSED.
  2. A request with an invalid token is REFUSED.
  3. A valid token WITHOUT the right scope is refused - a read token
     must not be able to authorise a transfer.
  4. A valid token with the right scope is ALLOWED.
  5. The health probe stays open, because a health probe that needs a
     credential is a health probe that gets turned off.
  6. A route with no declared scope is REFUSED, not allowed. Adding a
     route must not silently make it public.
  7. The sample surface is open in sample mode, EXCEPT the one endpoint
     that can move money.
  8. A credential scoped to one tenant cannot touch another.
  9. Tokens are stored as hashes. The plaintext is not recoverable.
 10. The token comparison is constant time.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

from xrpl_agent_treasury_mgmt.auth import (  # noqa: E402
    SCOPE_ADMIN,
    SCOPE_AUTHORISE,
    SCOPE_PROPOSE,
    SCOPE_READ,
    SCOPE_SETTLE,
    AuthError,
    Credential,
    CredentialStore,
    hash_token,
    mint_credential,
    mint_token,
)


class TestCredentialMinting(unittest.TestCase):
    """The properties of a minted credential."""

    def test_a_minted_token_is_usable(self):
        credential, token = mint_credential("ops", [SCOPE_READ])
        self.assertTrue(token)
        self.assertEqual(credential.name, "ops")
        self.assertEqual(credential.scopes, {SCOPE_READ})

    def test_tokens_are_long_and_unpredictable(self):
        tokens = {mint_token() for _ in range(200)}
        self.assertEqual(len(tokens), 200, "a token repeated itself")
        for token in tokens:
            # 32 bytes of entropy, URL-safe base64.
            self.assertGreaterEqual(len(token), 40)

    def test_the_plaintext_is_not_stored(self):
        credential, token = mint_credential("ops", [SCOPE_READ])
        self.assertNotIn(token, credential.token_hash)
        self.assertEqual(credential.token_hash, hashlib.sha256(
            token.encode("utf-8")
        ).hexdigest())

    def test_the_public_description_leaks_nothing(self):
        credential, token = mint_credential("ops", [SCOPE_READ])
        described = json.dumps(credential.to_dict())
        self.assertNotIn(token, described)
        self.assertNotIn(credential.token_hash, described)
        self.assertIn("ops", described)


class TestVerification(unittest.TestCase):
    """Verifying a presented token."""

    def setUp(self):
        self.credential, self.token = mint_credential(
            "ops", [SCOPE_READ, SCOPE_PROPOSE]
        )
        self.store = CredentialStore([self.credential])

    def test_a_valid_token_verifies(self):
        found = self.store.verify(self.token)
        self.assertEqual(found.name, "ops")
        self.assertEqual(found.scopes, {SCOPE_READ, SCOPE_PROPOSE})

    def test_an_invalid_token_is_refused(self):
        with self.assertRaises(AuthError) as caught:
            self.store.verify("not-the-token")
        self.assertEqual(caught.exception.status, 401)

    def test_an_empty_token_is_refused(self):
        with self.assertRaises(AuthError) as caught:
            self.store.verify("")
        self.assertEqual(caught.exception.status, 401)

    def test_a_revoked_token_is_refused(self):
        self.store.revoke("ops")
        with self.assertRaises(AuthError) as caught:
            self.store.verify(self.token)
        self.assertEqual(caught.exception.status, 401)

    def test_a_revoked_credential_is_not_counted(self):
        self.assertEqual(self.store.count, 1)
        self.store.revoke("ops")
        self.assertEqual(self.store.count, 0)

    def test_the_use_is_recorded(self):
        self.store.verify(self.token)
        self.assertIsNotNone(self.store.verify(self.token).last_used_at)

    def test_the_comparison_is_constant_time(self):
        """The comparison must not be a plain `==`.

        A plain `==` on a secret leaks its length and its prefix
        through timing. hmac.compare_digest does not.
        """
        import inspect

        from xrpl_agent_treasury_mgmt import auth

        source = inspect.getsource(auth.CredentialStore.verify)
        self.assertIn(
            "compare_digest",
            source,
            "the token comparison is not constant time",
        )
        self.assertNotIn(
            "token_hash ==",
            source,
            "the token comparison uses a plain equality",
        )


class TestScopes(unittest.TestCase):
    """Scope enforcement."""

    def setUp(self):
        self.credential, self.token = mint_credential("reader", [SCOPE_READ])
        self.store = CredentialStore([self.credential])

    def test_a_scope_the_credential_has_is_allowed(self):
        self.store.require_scope(self.credential, SCOPE_READ)

    def test_a_scope_the_credential_lacks_is_refused(self):
        with self.assertRaises(AuthError) as caught:
            self.store.require_scope(self.credential, SCOPE_AUTHORISE)
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(caught.exception.scope, SCOPE_AUTHORISE)

    def test_admin_covers_every_scope(self):
        admin, _ = mint_credential("admin", [SCOPE_ADMIN])
        for scope in (SCOPE_READ, SCOPE_PROPOSE, SCOPE_AUTHORISE, SCOPE_SETTLE):
            self.store.require_scope(admin, scope)

    def test_a_read_credential_cannot_settle(self):
        """The property that matters most."""
        with self.assertRaises(AuthError):
            self.store.require_scope(self.credential, SCOPE_SETTLE)


class TestTenantScoping(unittest.TestCase):
    """A credential may be restricted to particular tenants."""

    def setUp(self):
        self.credential, self.token = mint_credential(
            "tenant-a-only", [SCOPE_READ], tenants=["tenant-a"]
        )
        self.store = CredentialStore([self.credential])

    def test_its_own_tenant_is_allowed(self):
        self.store.require_tenant(self.credential, "tenant-a")

    def test_another_tenant_is_refused(self):
        with self.assertRaises(AuthError) as caught:
            self.store.require_tenant(self.credential, "tenant-b")
        self.assertEqual(caught.exception.status, 403)

    def test_a_credential_with_no_tenants_may_touch_any(self):
        wide, _ = mint_credential("wide", [SCOPE_READ])
        self.store.require_tenant(wide, "any-tenant-at-all")


class TestEnvironmentLoading(unittest.TestCase):
    """Loading credentials from the environment."""

    def setUp(self):
        self.variable = "TREASURY_API_TOKENS_TEST"
        self.previous = os.environ.get(self.variable)

    def tearDown(self):
        if self.previous is None:
            os.environ.pop(self.variable, None)
        else:
            os.environ[self.variable] = self.previous

    def test_nothing_configured_means_no_credentials(self):
        os.environ.pop(self.variable, None)
        self.assertEqual(CredentialStore.from_environment(self.variable).count, 0)

    def test_a_malformed_value_is_ignored_not_fatal(self):
        """A typo in a secret must not stop the service starting."""
        os.environ[self.variable] = "{not json"
        self.assertEqual(CredentialStore.from_environment(self.variable).count, 0)

    def test_credentials_are_loaded(self):
        credential, token = mint_credential("ops", [SCOPE_READ])
        os.environ[self.variable] = json.dumps(
            {"ops": {"token": token, "scopes": [SCOPE_READ]}}
        )
        store = CredentialStore.from_environment(self.variable)
        self.assertEqual(store.count, 1)
        self.assertEqual(store.verify(token).name, "ops")

    def test_a_credential_with_no_scopes_gets_read_only(self):
        """An over-broad credential is worse than a useless one."""
        _, token = mint_credential("ops", [SCOPE_READ])
        os.environ[self.variable] = json.dumps({"ops": {"token": token}})
        store = CredentialStore.from_environment(self.variable)
        credential = store.verify(token)
        self.assertEqual(credential.scopes, {SCOPE_READ})

    def test_a_token_is_never_echoed_back(self):
        _, token = mint_credential("ops", [SCOPE_READ])
        os.environ[self.variable] = json.dumps(
            {"ops": {"token": token, "scopes": [SCOPE_READ]}}
        )
        store = CredentialStore.from_environment(self.variable)
        self.assertNotIn(token, json.dumps(store.describe()))


class TestTheMiddleware(unittest.TestCase):
    """The enforcement, against a real Flask app."""

    def setUp(self):
        from flask import Flask

        from xrpl_agent_treasury_mgmt.auth_middleware import register_auth

        self.app = Flask(__name__)
        self.read_credential, self.read_token = mint_credential(
            "reader", [SCOPE_READ]
        )
        self.settle_credential, self.settle_token = mint_credential(
            "operator", [SCOPE_READ, SCOPE_SETTLE, SCOPE_AUTHORISE]
        )
        self.store = CredentialStore(
            [self.read_credential, self.settle_credential]
        )
        register_auth(self.app, self.store, sample_mode=True)

        @self.app.route("/api/health")
        def health():
            return {"status": "ok"}

        @self.app.route("/api/tenants")
        def tenants():
            return {"tenants": []}

        @self.app.route("/api/tenant/<tenant_id>/position")
        def position(tenant_id):
            return {"tenant": tenant_id}

        @self.app.route("/api/tenant/<tenant_id>/authorise/<obligation_id>",
                         methods=["POST"])
        def authorise(tenant_id, obligation_id):
            return {"ok": True}

        @self.app.route("/api/sample/capabilities")
        def capabilities():
            return {"capabilities": []}

        @self.app.route("/api/sample/settle", methods=["POST"])
        def settle():
            from flask import request

            body = request.get_json(silent=True) or {}
            if not body.get("confirm"):
                return {"submitted": False}
            return {"submitted": True}

        @self.app.route("/api/undeclared")
        def undeclared():
            return {"ok": True}

        self.client = self.app.test_client()

    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    # -------------------------------------------------------- the refusals

    def test_an_unauthenticated_request_is_refused(self):
        response = self.client.get("/api/tenants")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "unauthorized")

    def test_a_refusal_carries_a_www_authenticate_header(self):
        response = self.client.get("/api/tenants")
        self.assertIn("Bearer", response.headers.get("WWW-Authenticate", ""))

    def test_an_invalid_token_is_refused(self):
        response = self.client.get(
            "/api/tenants", headers=self._auth("not-the-token")
        )
        self.assertEqual(response.status_code, 401)

    def test_a_malformed_authorization_header_is_refused(self):
        response = self.client.get(
            "/api/tenants", headers={"Authorization": "Basic abc123"}
        )
        self.assertEqual(response.status_code, 401)

    def test_a_valid_token_with_the_wrong_scope_is_refused(self):
        """A read token must not be able to authorise."""
        response = self.client.post(
            "/api/tenant/tenant-a/authorise/OB-1",
            headers=self._auth(self.read_token),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "forbidden")

    def test_a_refusal_names_the_scope_it_wanted(self):
        response = self.client.post(
            "/api/tenant/tenant-a/authorise/OB-1",
            headers=self._auth(self.read_token),
        )
        self.assertEqual(
            response.get_json().get("required_scope"), SCOPE_AUTHORISE
        )

    def test_a_route_with_no_declared_scope_is_refused(self):
        """Adding a route must not silently make it public."""
        response = self.client.get(
            "/api/undeclared", headers=self._auth(self.settle_token)
        )
        self.assertEqual(response.status_code, 403)

    # ------------------------------------------------------ the allowances

    def test_the_health_probe_stays_open(self):
        """A health probe that needs a credential gets turned off."""
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)

    def test_a_valid_token_with_the_right_scope_is_allowed(self):
        response = self.client.get(
            "/api/tenants", headers=self._auth(self.read_token)
        )
        self.assertEqual(response.status_code, 200)

    def test_a_token_with_the_authorise_scope_may_authorise(self):
        response = self.client.post(
            "/api/tenant/tenant-a/authorise/OB-1",
            headers=self._auth(self.settle_token),
        )
        self.assertEqual(response.status_code, 200)

    # ------------------------------------------------------ the sample set

    def test_the_sample_surface_is_open_in_sample_mode(self):
        response = self.client.get("/api/sample/capabilities")
        self.assertEqual(response.status_code, 200)

    def test_the_settle_dry_form_needs_no_token(self):
        response = self.client.post("/api/sample/settle", json={"amount": 1})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["submitted"])

    def test_the_settle_submit_form_needs_a_token(self):
        response = self.client.post(
            "/api/sample/settle", json={"amount": 1, "confirm": True}
        )
        self.assertEqual(response.status_code, 401)

    def test_the_settle_submit_form_needs_the_settle_scope(self):
        response = self.client.post(
            "/api/sample/settle",
            json={"amount": 1, "confirm": True},
            headers=self._auth(self.read_token),
        )
        self.assertEqual(response.status_code, 403)

    def test_the_settle_submit_form_succeeds_with_the_right_scope(self):
        response = self.client.post(
            "/api/sample/settle",
            json={"amount": 1, "confirm": True},
            headers=self._auth(self.settle_token),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["submitted"])

    # -------------------------------------------------- sample mode off

    def test_the_sample_surface_is_gone_when_sample_mode_is_off(self):
        from flask import Flask

        from xrpl_agent_treasury_mgmt.auth_middleware import register_auth

        app = Flask(__name__)
        register_auth(app, self.store, sample_mode=False)
        client = app.test_client()
        response = client.get("/api/sample/capabilities")
        self.assertEqual(response.status_code, 404)


class TestAnEmptyStoreIsNotAnOpenDoor(unittest.TestCase):
    """A service started without credentials says no."""

    def setUp(self):
        from flask import Flask

        from xrpl_agent_treasury_mgmt.auth_middleware import register_auth

        self.app = Flask(__name__)
        register_auth(self.app, CredentialStore(), sample_mode=True)

        @self.app.route("/api/tenants")
        def tenants():
            return {"tenants": []}

        self.client = self.app.test_client()

    def test_the_operational_surface_is_refused(self):
        response = self.client.get("/api/tenants")
        self.assertEqual(response.status_code, 401)

    def test_no_token_can_verify(self):
        with self.assertRaises(AuthError):
            CredentialStore().verify("anything at all")


if __name__ == "__main__":
    unittest.main()
