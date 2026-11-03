"""
Wallet and external-service connectivity.

WHAT THIS SOLVES
================

A treasury service that can only talk to one hardcoded endpoint is a demo.
A deliverable has to connect to whatever the tenant actually runs:

    WALLETS / CUSTODY      XAMM, Xverse, a hardware wallet, an exchange,
                           a custodian's API, an internal cold-storage
                           service — each with its own auth model, its own
                           transaction format, its own failure modes.

    BUSINESS SYSTEMS       NetSuite, SAP, Dynamics, QuickBooks, an internal
                           ERP — each with its own auth model and its own
                           idea of what an obligation looks like.

    PRICING                A live FX feed.

This module defines the SEAMS for all three. The concrete adapters live in
`integrations/`, so adding a vendor does not mean touching the service.

THE CONTRACT
============

Every adapter implements one of three protocols:

    WalletConnector    sign and submit transactions
    ObligationProvider supply obligations from a business system
    RateProvider       supply exchange rates (see `rates.py`)

Two rules apply to every adapter:

1. **A connector failure is reportable, never silent.** An adapter that
   cannot be reached returns a typed error with the reason. A treasury that
   quietly sees "no obligations" because the ERP was down will plan
   nothing and look healthy.

2. **Credentials come from the environment, never from configuration
   files.** The connector takes a credential object; the adapter decides
   where it came from. Nothing in this package writes a secret to disk.

CONNECTOR REGISTRY
==================

`ConnectorRegistry` holds configured connectors by name, so a deployment
describes its environment in configuration and the service resolves
connectors by name at runtime.

    registry = ConnectorRegistry()
    registry.register("custody", XAMMConnector(env=env))
    registry.register("erp", NetSuiteConnector(env=env))

    connector = registry.get("erp")
    obligations = connector.fetch()
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Protocol

from .obligations import Obligation


class ConnectorError(Exception):
    """Raised when a connector cannot fulfil a request.

    Carries enough detail for the decision log to explain WHY the treasury
    is blind, without leaking a credential.
    """

    def __init__(self, message: str, connector: str = "", retryable: bool = False):
        super().__init__(message)
        self.connector = connector
        self.retryable = retryable


class WalletConnector(Protocol):
    """Something that can sign and submit transactions for an account.

    Mirrors the `Signer` protocol in `signer.py`, but expressed as a
    connector so an XAMM integration and an HSM integration are
    interchangeable from the service's point of view.
    """

    name: str

    @property
    def account(self) -> str:
        ...

    def sign_and_submit(self, tx_json: Dict) -> Dict:
        ...


class ObligationProvider(Protocol):
    """Something that can supply obligations from a business system."""

    name: str

    def fetch(self) -> List[Obligation]:
        ...


@dataclass
class Credential:
    """A credential for an external service.

    `secret` is never logged, never persisted by this package, and never
    included in an error message.
    """

    key: str
    secret: str
    account_id: str = ""
    base_url: str = ""
    extra: Dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        # Never let a secret reach a log line or a traceback.
        return f"Credential(key={self.key!r}, account_id={self.account_id!r})"

    def __str__(self) -> str:
        return self.__repr__()


@dataclass
class ConnectorConfig:
    """How a connector is configured for one deployment."""

    name: str
    kind: str  # "wallet" | "erp" | "rates"
    adapter: str  # the adapter class path or a registered key
    enabled: bool = True
    credential_key: str = ""
    settings: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "adapter": self.adapter,
            "enabled": self.enabled,
            "credential_key": self.credential_key,
            "settings": dict(self.settings),
        }


class ConnectorRegistry:
    """Holds the connectors a deployment has configured.

    Resolved by name at runtime so the service code never hardcodes a
    vendor. A missing connector is a typed error, not an `AttributeError`
    three frames deep.
    """

    def __init__(self):
        self._connectors: Dict[str, object] = {}
        self._configs: Dict[str, ConnectorConfig] = {}

    def register(self, name: str, connector: object) -> None:
        if name in self._connectors:
            raise ConnectorError(
                f"connector {name!r} is already registered", connector=name
            )
        self._connectors[name] = connector

    def configure(self, config: ConnectorConfig) -> None:
        self._configs[config.name] = config

    def get(self, name: str) -> object:
        try:
            return self._connectors[name]
        except KeyError:
            raise ConnectorError(
                f"no connector named {name!r} is registered; "
                f"registered: {sorted(self._connectors)}",
                connector=name,
            )

    def get_optional(self, name: str) -> Optional[object]:
        return self._connectors.get(name)

    def names(self) -> List[str]:
        return sorted(self._connectors)

    def describe(self) -> List[Dict]:
        """A safe description of the configured connectors.

        No credentials, no secrets — just enough for an operator to see
        what is wired up.
        """
        out = []
        for name, connector in sorted(self._connectors.items()):
            config = self._configs.get(name)
            out.append(
                {
                    "name": name,
                    "kind": config.kind if config else "unknown",
                    "adapter": config.adapter if config else type(connector).__name__,
                    "enabled": config.enabled if config else True,
                    "connector_class": type(connector).__name__,
                }
            )
        return out


def credential_from_env(
    key: str,
    account_env: str = "",
    base_url_env: str = "",
    required: bool = True,
) -> Optional[Credential]:
    """Build a credential from environment variables.

    The convention:
        <KEY>            the secret / API key
        <KEY>_ACCOUNT    the account or wallet identifier
        <KEY>_URL        the service base URL

    A missing required credential raises a `ConnectorError` naming the
    VARIABLE, never its value.
    """
    secret = os.environ.get(key)
    account = os.environ.get(account_env or f"{key}_ACCOUNT", "")
    base_url = os.environ.get(base_url_env or f"{key}_URL", "")
    if not secret:
        if required:
            raise ConnectorError(
                f"environment variable {key} is not set; this connector cannot "
                f"be configured",
                connector=key,
            )
        return None
    return Credential(
        key=key, secret=secret, account_id=account, base_url=base_url
    )


@dataclass
class ConnectionStatus:
    """The health of one connector."""

    name: str
    kind: str
    reachable: bool
    detail: str = ""
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "reachable": self.reachable,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }
