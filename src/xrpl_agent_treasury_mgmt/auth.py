"""
API authentication and authorisation.

WHY THIS EXISTS
===============

The transfer endpoint is a security boundary. Anyone who can reach it can
propose a disbursement, and anyone who can authorise can make one happen.
Authentication is the difference between a demo and a product, so it is
built the way a real service would build it rather than as a
middleware-shaped afterthought.

THE MODEL
=========

A caller presents a BEARER TOKEN. Tokens carry SCOPES, and a route
declares the scope it needs. A token with the wrong scope is refused
even if it is valid — a valid token for reading the position must not
be able to authorise a transfer.

    POST /api/tenant/<id>/authorise/<obligation_id>   scope: treasury:authorise
    GET  /api/tenant/<id>/position                    scope: treasury:read
    POST /api/sample/settle                           scope: treasury:settle

WHAT IS STORED
==============

NOT the token. A token is 32 bytes of entropy. What is stored is its
SHA-256 HASH, so a leaked store does not yield usable credentials. The
plaintext token is shown ONCE, when it is minted, and never again.

Comparison is CONSTANT TIME. A naive `==` on a secret leaks its length
and its prefix through timing. hmac.compare_digest does not.

WHERE THE SECRETS LIVE
======================

Tokens come from the ENVIRONMENT, never from source. A token in a file
in the repository is a token in every clone of the repository.

    TREASURY_API_TOKENS='{"<name>":{"token":"...","scopes":[...]}}'

A JSON object is used rather than a loose "one token, all scopes" string
because the second is the shape that accidentally becomes production
behaviour: a single credential that can read and authorise and settle.
Several named credentials with different scopes is what makes a
revocation meaningful.

WHAT IS NOT AUTHENTICATED
==========================

    /api/health          a liveness probe. It reports whether the
                         service is up. It does not report balances,
                         tenant names, or anything sensitive, because a
                         health probe that leaks is a health probe that
                         gets turned off.

    /api/sample/*        the demonstration surface. These endpoints are
                         namespaced, documented as demonstrations, and
                         most of them move nothing. They are open in
                         sample mode so a reviewer can look without
                         credentials.

    /                   the dashboard. It is a read-only view of a
                         demo tenant.

    EVERYTHING ELSE     requires a token, and the operational surface
                         requires one with the right scope.

THE FAILURE MODES ARE EXPLICIT
==============================

    401  no token, or a token that does not verify.
    403  a valid token without the scope the route requires.

Those are different problems and an operator needs to tell them apart.
401 gets a WWW-Authenticate header; 403 does not, because the caller
authenticated fine and re-authenticating will not help.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set

# The scopes the service understands.
SCOPE_READ = "treasury:read"
SCOPE_PROPOSE = "treasury:propose"
SCOPE_AUTHORISE = "treasury:authorise"
SCOPE_SETTLE = "treasury:settle"
SCOPE_ADMIN = "treasury:admin"

ALL_SCOPES = (
    SCOPE_READ,
    SCOPE_PROPOSE,
    SCOPE_AUTHORISE,
    SCOPE_SETTLE,
    SCOPE_ADMIN,
)


class AuthError(Exception):
    """Authentication or authorisation failed.

    `status` is 401 for "who are you" and 403 for "not you".
    """

    def __init__(self, message: str, status: int = 401, scope: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.scope = scope


@dataclass(frozen=True)
class Credential:
    """One named API credential.

    Only the HASH is kept. The plaintext is returned once, at mint time.
    """

    name: str
    token_hash: str
    scopes: Set[str] = field(default_factory=set)
    tenant_ids: Set[str] = field(default_factory=set)
    description: str = ""
    created_at: str = ""
    last_used_at: Optional[str] = None
    revoked: bool = False

    def has_scope(self, scope: str) -> bool:
        if SCOPE_ADMIN in self.scopes:
            return True
        return scope in self.scopes

    def may_access_tenant(self, tenant_id: str) -> bool:
        """Is this credential allowed to touch this tenant?

        A credential with no tenant restriction is not restricted. A
        credential with tenants listed is restricted to exactly those.
        """
        if not self.tenant_ids:
            return True
        return tenant_id in self.tenant_ids

    def to_dict(self) -> Dict:
        """The public description. Never the token, never the hash."""
        return {
            "name": self.name,
            "scopes": sorted(self.scopes),
            "tenants": sorted(self.tenant_ids) if self.tenant_ids else "all",
            "description": self.description,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "revoked": self.revoked,
        }


def hash_token(token: str) -> str:
    """The stored form of a token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def mint_token() -> str:
    """A fresh token. 32 bytes of entropy, URL-safe."""
    return secrets.token_urlsafe(32)


def _parse_scopes(value) -> Set[str]:
    if isinstance(value, str):
        return {part.strip() for part in value.split(",") if part.strip()}
    if isinstance(value, (list, tuple, set)):
        return {str(part).strip() for part in value if str(part).strip()}
    return set()


def _parse_tenants(value) -> Set[str]:
    if isinstance(value, str):
        return {part.strip() for part in value.split(",") if part.strip()}
    if isinstance(value, (list, tuple, set)):
        return {str(part).strip() for part in value if str(part).strip()}
    return set()


class CredentialStore:
    """The credentials the service will accept.

    Loaded from the environment at startup. Nothing is written to disk,
    because a store on disk is a store that can be read by anything
    that can read the disk.
    """

    def __init__(self, credentials: Optional[Iterable[Credential]] = None):
        self._credentials: Dict[str, Credential] = {}
        for credential in credentials or ():
            self._credentials[credential.name] = credential

    # ------------------------------------------------------------- loading

    @classmethod
    def from_environment(
        cls, variable: str = "TREASURY_API_TOKENS"
    ) -> "CredentialStore":
        """Load credentials from the environment.

        The variable holds a JSON object:

            {"treasury-ops": {"token": "...", "scopes": ["treasury:read"]}}

        A malformed value is IGNORED rather than fatal: a service that
        refuses to start because of a typo in a secret is harder to
        operate than one that starts with no credentials and says so.
        The caller checks `store.count` and warns.
        """
        raw = os.environ.get(variable, "").strip()
        if not raw:
            return cls()

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return cls()

        if not isinstance(parsed, dict):
            return cls()

        credentials = []
        now = datetime.now(timezone.utc).isoformat()
        for name, entry in parsed.items():
            if not isinstance(entry, dict):
                continue
            token = entry.get("token")
            if not token or not isinstance(token, str):
                continue
            scopes = _parse_scopes(entry.get("scopes"))
            # A credential with no scopes can do nothing. That is a
            # safe default: an over-broad credential is a mistake that
            # moves money, and a useless one is merely useless.
            if not scopes:
                scopes = {SCOPE_READ}
            credentials.append(
                Credential(
                    name=str(name),
                    token_hash=hash_token(token),
                    scopes=scopes,
                    tenant_ids=_parse_tenants(entry.get("tenants")),
                    description=str(entry.get("description", "")),
                    created_at=now,
                )
            )
        return cls(credentials)

    # ------------------------------------------------------------- lookups

    @property
    def count(self) -> int:
        return len([c for c in self._credentials.values() if not c.revoked])

    def describe(self) -> List[Dict]:
        return [
            credential.to_dict()
            for credential in sorted(
                self._credentials.values(), key=lambda c: c.name
            )
        ]

    def verify(self, token: str) -> Credential:
        """Verify a bearer token and return its credential.

        Every credential is compared, even after a match, so the time
        taken does not depend on WHICH credential matched. A caller who
        can time the response learns nothing about the token space.
        """
        if not token:
            raise AuthError("a bearer token is required", 401)

        candidate = hash_token(token)
        matched: Optional[Credential] = None

        for credential in self._credentials.values():
            if hmac.compare_digest(candidate, credential.token_hash):
                matched = credential

        if matched is None:
            raise AuthError("the bearer token is not valid", 401)
        if matched.revoked:
            raise AuthError("the bearer token has been revoked", 401)

        # Record the use. A credential that has never been used, or
        # whose last use is months old, is one worth looking at.
        self._credentials[matched.name] = Credential(
            name=matched.name,
            token_hash=matched.token_hash,
            scopes=matched.scopes,
            tenant_ids=matched.tenant_ids,
            description=matched.description,
            created_at=matched.created_at,
            last_used_at=datetime.now(timezone.utc).isoformat(),
            revoked=matched.revoked,
        )
        return self._credentials[matched.name]

    def require_scope(self, credential: Credential, scope: str) -> None:
        if not credential.has_scope(scope):
            raise AuthError(
                f"this credential lacks the {scope!r} scope",
                403,
                scope=scope,
            )

    def require_tenant(self, credential: Credential, tenant_id: str) -> None:
        if not credential.may_access_tenant(tenant_id):
            raise AuthError(
                f"this credential is not scoped to the tenant {tenant_id!r}",
                403,
            )

    def revoke(self, name: str) -> bool:
        credential = self._credentials.get(name)
        if credential is None:
            return False
        self._credentials[name] = Credential(
            name=credential.name,
            token_hash=credential.token_hash,
            scopes=credential.scopes,
            tenant_ids=credential.tenant_ids,
            description=credential.description,
            created_at=credential.created_at,
            last_used_at=credential.last_used_at,
            revoked=True,
        )
        return True


def mint_credential(
    name: str,
    scopes: Iterable[str],
    tenants: Optional[Iterable[str]] = None,
    description: str = "",
) -> tuple:
    """Mint a credential.

    Returns (credential, plaintext_token). The plaintext is shown ONCE.
    It is never stored and cannot be recovered - only its hash is.
    """
    token = mint_token()
    credential = Credential(
        name=name,
        token_hash=hash_token(token),
        scopes=set(scopes),
        tenant_ids=set(tenants or ()),
        description=description,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    return credential, token
