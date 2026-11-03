"""
Flask integration for API authentication.

THE ENFORCEMENT POINT
=====================

One `before_request` hook, plus a per-route scope map. A hook rather
than a decorator on each route, because a decorator is easy to forget:
one route without it is an open door, and nothing in the code says so.

The map is EXPLICIT. A route that is not in the map is not open - it is
refused, and the refusal says why. A new route added without a scope
fails closed. An open route has to be added to the OPEN list, which is
a deliberate act.

    OPEN       no token. Health probes, the dashboard, the sample
               demonstration surface.
    SCOPES     the scope each route needs.

The sample surface is open in SAMPLE MODE only. In production mode it
is not registered at all, so there is nothing to authenticate.
"""

from __future__ import annotations

import functools
from typing import Callable, Dict, Optional

from flask import jsonify, request

from .auth import (
    SCOPE_ADMIN,
    SCOPE_AUTHORISE,
    SCOPE_PROPOSE,
    SCOPE_READ,
    SCOPE_SETTLE,
    AuthError,
    CredentialStore,
)

# Routes that need no token. Deliberately short.
OPEN_PATHS = frozenset(
    {
        "/",
        "/api/health",
    }
)

# Routes that need a token but no particular scope: they reveal that a
# service is running and nothing about its tenants' money.
UNSCOPED_PATHS = frozenset(
    {
        "/api/connectors",
    }
)

# The scope each operational route requires.
SCOPES: Dict[str, str] = {
    "/api/tenants": SCOPE_READ,
    "/api/tenant/<tenant_id>/position": SCOPE_READ,
    "/api/tenant/<tenant_id>/plan": SCOPE_READ,
    "/api/tenant/<tenant_id>/obligations": SCOPE_READ,
    "/api/tenant/<tenant_id>/log": SCOPE_READ,
    "/api/tenant/<tenant_id>/authorisations": SCOPE_READ,
    "/api/tenant/<tenant_id>/dry-run": SCOPE_PROPOSE,
    "/api/tenant/<tenant_id>/authorise/<obligation_id>": SCOPE_AUTHORISE,
}

# The sample routes that can MOVE MONEY. The rest of the sample surface
# is read-only or a simulation, and stays open so a reviewer can look
# without credentials.
SAMPLE_SETTLE_PATH = "/api/sample/settle"


def _extract_token() -> Optional[str]:
    """The bearer token from the request, if there is one."""
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
        return token or None
    return None


def register_auth(app, store: CredentialStore, *, sample_mode: bool = False) -> None:
    """Install authentication on a Flask app.

    `store` holds the credentials. An EMPTY store means no token can
    verify, so every protected route is refused - which is the correct
    behaviour for a service that was started without credentials. It is
    NOT the same as having no authentication at all.
    """

    app.config["AUTH_STORE"] = store
    app.config["AUTH_SAMPLE_MODE"] = sample_mode

    @app.before_request
    def _enforce_authentication():
        path = request.path

        if path in OPEN_PATHS:
            return None

        if path in UNSCOPED_PATHS:
            # Still requires a valid token - it just does not narrow
            # what the token may do.
            token = _extract_token()
            if token is None:
                return _unauthorized("a bearer token is required")
            try:
                app.config["AUTH_STORE"].verify(token)
            except AuthError as exc:
                return _unauthorized(exc.message)
            return None

        # The sample surface is open in sample mode, EXCEPT the one
        # endpoint that can move money.
        if path.startswith("/api/sample/"):
            if not sample_mode:
                return _not_found("no such endpoint")
            if path == SAMPLE_SETTLE_PATH:
                # The dry form needs no token - it moves nothing. The
                # submit form does.
                if not _wants_to_submit():
                    return None
                token = _extract_token()
                if token is None:
                    return _unauthorized(
                        "submitting a payment requires a bearer token"
                    )
                try:
                    credential = app.config["AUTH_STORE"].verify(token)
                    app.config["AUTH_STORE"].require_scope(
                        credential, SCOPE_SETTLE
                    )
                except AuthError as exc:
                    return _error(exc)
                return None
            return None

        # Everything else is the operational surface.
        #
        # The scope map is keyed by the ROUTE RULE, not the concrete path.
        # request.path for /api/tenant/tenant-a/authorise/OB-1 is the
        # concrete path, which is in no map. request.url_rule.rule is the
        # rule, which is.
        rule = getattr(request.url_rule, "rule", None) or path
        scope = SCOPES.get(rule)
        if scope is None:
            # A route with no declared scope is REFUSED, not allowed.
            # Adding a route without saying what it needs must not
            # silently make it public.
            return _forbidden(
                f"no scope is declared for {rule}; this route is refused"
            )

        token = _extract_token()
        if token is None:
            return _unauthorized("a bearer token is required")

        try:
            credential = app.config["AUTH_STORE"].verify(token)
            app.config["AUTH_STORE"].require_scope(credential, scope)
            tenant_id = _tenant_from_view_args(rule)
            if tenant_id:
                app.config["AUTH_STORE"].require_tenant(
                    credential, tenant_id
                )
        except AuthError as exc:
            return _error(exc)

        # The verified credential is available to the route.
        app.config["CURRENT_CREDENTIAL"] = credential
        return None

    @app.after_request
    def _clear_credential(response):
        app.config.pop("CURRENT_CREDENTIAL", None)
        return response

    # A helper the routes can use to report who is calling.
    def current_credential():
        return app.config.get("CURRENT_CREDENTIAL")

    app.current_credential = current_credential  # type: ignore[attr-defined]


def _tenant_from_view_args(rule: str) -> Optional[str]:
    """The tenant a route rule names, if any.

    The tenant id is a VIEW ARGUMENT, so it is read from the request's
    view arguments rather than by splitting the path. Splitting the
    concrete path works too, but the rule is the thing that says which
    segment is the tenant.
    """
    if "<tenant_id>" not in rule:
        return None
    return (request.view_args or {}).get("tenant_id")


def _wants_to_submit() -> bool:
    """Is this request asking to actually submit?

    The sample settle endpoint returns the payment it WOULD send unless
    the request explicitly asks for the real thing. That distinction
    has to be made here, because the token is only needed for the real
    thing.
    """
    if request.method != "POST":
        return False
    body = request.get_json(silent=True) or {}
    return bool(body.get("confirm"))


def _unauthorized(message: str):
    response = jsonify(
        {
            "error": "unauthorized",
            "detail": message,
            "how_to_authenticate": (
                "Send the token in the Authorization header: "
                "'Authorization: Bearer <token>'"
            ),
        }
    )
    response.status_code = 401
    response.headers["WWW-Authenticate"] = 'Bearer realm="treasury"'
    return response


def _forbidden(message: str):
    response = jsonify(
        {
            "error": "forbidden",
            "detail": message,
        }
    )
    response.status_code = 403
    return response


def _not_found(message: str):
    response = jsonify({"error": "not_found", "detail": message})
    response.status_code = 404
    return response


def _error(exc: AuthError):
    if exc.status == 401:
        return _unauthorized(exc.message)
    payload = {"error": "forbidden", "detail": exc.message}
    if exc.scope:
        payload["required_scope"] = exc.scope
    response = jsonify(payload)
    response.status_code = exc.status
    return response
