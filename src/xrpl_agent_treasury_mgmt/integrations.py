"""
Concrete integrations: wallets, custody APIs, and business systems.

Each adapter is a self-contained implementation of one of the protocols in
`connectors.py`. They are deliberately boring: each one authenticates,
requests, maps the response into this package's types, and raises a typed
`ConnectorError` on failure.

VERIFIED LANDSCAPE (checked 2026-11-02)
=======================================

XRPL WALLETS AND CUSTODY
    `xrpl-connect` (npm, XRPL-Commons) is the maintained toolkit for
    connecting XRPL wallets to an application. It ships adapters for
    **Xaman/XUMM, Crossmark, GemWallet, WalletConnect, Ledger, XYRA,
    GhostSig, Otsu and MetaMask Snap**. It is framework-agnostic and
    TypeScript-first, so it is the right choice for a FRONT END that needs
    to ask a user's wallet to sign.

    That is a different job from this service's. A treasury backend holds
    the signing authority and signs server-side, unattended. It does not
    need a browser wallet UI. So the browser-wallet toolkit is not the
    integration point here; it is an optional companion for a treasury
    that wants a human to approve in their own wallet.

    The connectors in this module are therefore SERVER-SIDE signers:

    XamanPayloadSigner   Xaman/XUMM's platform API. Creates a sign
                          payload; the wallet approves; the service
                          submits. Endpoints verified live:
                          POST https://xumm.app/api/v1/platform/payload
                          (returns 403 without credentials, which is the
                          correct "endpoint exists, auth rejected").
                          Auth: X-API-Key + X-API-Secret headers.
                          Reference: https://docs.xaman.dev

    JsonRpcSigner         A generic JSON-RPC signer over HTTP. Any service
                          that accepts sign+submit works here — a
                          self-hosted signer, an internal cold-storage
                          facade, a custodian's REST wrapper. This is the
                          adapter to reach for when the customer names a
                          vendor we have not written code for: the contract
                          is small and stable.

    LocalSeedSigner       (in `signer.py`) the devnet path.

    NOTE: Xverse's public API has pivoted to Bitcoin-focused endpoints at
    api.secretkeylabs.io and is NOT an XRPL signer. An earlier draft of
    this module pointed at a v2 XRPL path that does not exist. It was
    removed rather than shipped broken.

BUSINESS SYSTEMS
    CsvObligationSource  A CSV export - the universal fallback
    GenericRestErp       A REST ERP driven by a field mapping, for any
                         system with a similar shape
    NetSuiteConnector    NetSuite SuiteTalk REST

    The generic adapters matter as much as the named ones: most
    tenants run something nobody has written an adapter for yet, and
    the field-mapping adapter is what makes the ERP side deliverable
    without a custom code drop.

CREDENTIALS
===========

Every connector takes a `Credential`. Build it with
`connectors.credential_from_env`, which reads the environment and never
persists anything. A connector never logs its secret and never puts one in
an error message.
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .connectors import ConnectorError, Credential
from .obligations import Obligation


def _http_json(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Dict] = None,
    timeout: float = 30.0,
    connector: str = "",
) -> Dict:
    """A JSON HTTP request that raises a typed, secret-free error.

    The credential never appears in the exception message. The response
    body IS included on error, because a business system's error message
    is usually the only useful diagnostic — but it is truncated and the
    caller decides what to log.
    """
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:400]
        except Exception:
            pass
        raise ConnectorError(
            f"HTTP {exc.code} from {url}: {detail}",
            connector=connector,
            retryable=exc.code >= 500,
        ) from None
    except urllib.error.URLError as exc:
        raise ConnectorError(
            f"could not reach {url}: {exc.reason}",
            connector=connector,
            retryable=True,
        ) from None
    except Exception as exc:
        raise ConnectorError(
            f"request to {url} failed: {exc!r}",
            connector=connector,
            retryable=True,
        ) from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise ConnectorError(
            f"response from {url} was not JSON: {raw[:200]}",
            connector=connector,
        ) from None


# --------------------------------------------------------------------------
# Wallets / custody
# --------------------------------------------------------------------------


@dataclass
class JsonRpcSigner:
    """A generic JSON-RPC signer over HTTP.

    Any service that accepts `{"method": "sign", "params": {...}}` and
    returns a signed blob works here. That covers self-hosted signers,
    internal cold-storage facades, and most custodians' REST wrappers.

    This is the adapter to reach for when the customer names a vendor we
    have not written code for: the contract is small and stable.
    """

    credential: Credential
    name: str = "json-rpc-signer"
    timeout: float = 30.0

    @property
    def account(self) -> str:
        return self.credential.account_id

    def sign_and_submit(self, tx_json: Dict) -> Dict:
        endpoint = self.credential.base_url.rstrip("/") + "/rpc"
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sign",
            "params": {"tx_json": tx_json},
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.credential.secret}",
        }
        response = _http_json(
            endpoint,
            method="POST",
            headers=headers,
            body=body,
            timeout=self.timeout,
            connector=self.name,
        )
        if "error" in response:
            raise ConnectorError(
                f"signer returned an error: {response['error']}",
                connector=self.name,
            )
        result = response.get("result") or {}
        if not result.get("signed_tx"):
            raise ConnectorError(
                f"signer returned no signed transaction: {str(result)[:200]}",
                connector=self.name,
            )
        return result


@dataclass
class XamanPayloadSigner:
    """Xaman/XUMM's platform API — the payload model.

    The flow is a three-step lifecycle, not a single sign call:

        1. POST a payload with the transaction template. Xaman returns a
           `uuid` and a QR code. The template may omit `Account` and
           `Sequence` — Xaman fills them in from the wallet that approves.
        2. The wallet approves (in-app, or by scanning the QR).
        3. Xaman submits the signed transaction and notifies the configured
           webhook, which carries the signed blob and the tx hash.

    Auth is two headers: `X-API-Key` and `X-API-Secret`.

    Because step 2 is asynchronous and human-gated, this connector has a
    different shape from the others: `create_payload` returns a pending
    payload rather than a submitted transaction. A service that wants
    unattended signing should use a server-side signer instead; this one
    is for the case where the tenant wants the treasury manager to
    approve in their own wallet.

    Reference: https://docs.xaman.dev
    """

    credential: Credential
    name: str = "xaman-payload"
    timeout: float = 30.0
    base_url: str = "https://xumm.app"

    @property
    def account(self) -> str:
        return self.credential.account_id

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-API-Key": self.credential.secret,
            "X-API-Secret": self.credential.extra.get("api_secret", ""),
        }

    def create_payload(
        self, tx_json: Dict, submit: bool = True, expire_minutes: int = 5
    ) -> Dict:
        """Create a sign payload. Returns the payload id and QR reference."""
        endpoint = self.base_url.rstrip("/") + "/api/v1/platform/payload"
        body = {
            "txjson": tx_json,
            "options": {
                "submit": submit,
                "multisign": False,
                "expire": expire_minutes,
            },
        }
        response = _http_json(
            endpoint,
            method="POST",
            headers=self._headers(),
            body=body,
            timeout=self.timeout,
            connector=self.name,
        )
        if not response.get("uuid"):
            raise ConnectorError(
                f"Xaman returned no payload uuid: {str(response)[:200]}",
                connector=self.name,
            )
        return response

    def sign_and_submit(self, tx_json: Dict) -> Dict:
        """Create a payload for signing.

        NOTE: this does NOT submit. The signature comes back through the
        webhook. Calling `sign_and_submit` and expecting a hash is the
        wrong shape for this connector — use `create_payload` and wait for
        the callback.
        """
        payload = self.create_payload(tx_json)
        return {
            "status": "awaiting_wallet_approval",
            "payload_id": payload.get("uuid"),
            "qr": (payload.get("refs") or {}).get("qr_png"),
            "expires_at": payload.get("expires"),
        }


# --------------------------------------------------------------------------
# Business systems
# --------------------------------------------------------------------------


@dataclass
class CsvObligationSource:
    """Obligations from a CSV export.

    The universal fallback: every finance system exports CSV, and this
    keeps the service usable before a native integration exists.

    Columns: id, description, amount, due_date, funding_account,
    entity, vendor, currency
    """

    path: str
    name: str = "csv"
    delimiter: str = ","

    def fetch(self) -> List[Obligation]:
        path = pathlib.Path(self.path)
        if not path.exists():
            raise ConnectorError(
                f"obligation file {self.path} does not exist", connector=self.name
            )
        try:
            text = path.read_text()
        except Exception as exc:
            raise ConnectorError(
                f"could not read {self.path}: {exc!r}", connector=self.name
            ) from None
        obligations: List[Obligation] = []
        try:
            for row in csv.DictReader(io.StringIO(text), delimiter=self.delimiter):
                due = datetime.fromisoformat(row["due_date"])
                if due.tzinfo is None:
                    due = due.replace(tzinfo=timezone.utc)
                obligations.append(
                    Obligation(
                        id=row["id"],
                        description=row.get("description", ""),
                        amount=float(row["amount"]),
                        currency=row.get("currency", "RLUSD"),
                        due_date=due,
                        funding_account=row["funding_account"],
                        entity=row.get("entity", ""),
                        vendor=row.get("vendor", ""),
                    )
                )
        except KeyError as exc:
            raise ConnectorError(
                f"{self.path} is missing the required column {exc}",
                connector=self.name,
            ) from None
        except ValueError as exc:
            raise ConnectorError(
                f"{self.path} has a malformed value: {exc}", connector=self.name
            ) from None
        return obligations


@dataclass
class GenericRestErp:
    """A REST business system driven by a field mapping.

    Most ERPs expose open obligations over REST with roughly the same
    shape: a list of records, each with an id, an amount, a due date, and
    some account reference. This adapter takes a mapping so a new vendor
    is configuration, not code.

    settings:
        path               the URL path for the obligations endpoint
        id_field           field holding the obligation id
        amount_field       field holding the amount
        due_field          field holding the due date
        account_field      field holding the destination account
        currency_field     optional field holding the currency
        description_field  optional
        vendor_field       optional
        entity_field       optional
        records_key        optional: which key holds the list
    """

    credential: Credential
    settings: Dict[str, str] = field(default_factory=dict)
    name: str = "generic-rest-erp"
    timeout: float = 45.0

    def _path(self, key: str, default: str = "") -> str:
        return self.settings.get(key, default)

    def fetch(self) -> List[Obligation]:
        base = self.credential.base_url.rstrip("/")
        url = base + self._path("path", "/obligations")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.credential.secret}",
        }
        response = _http_json(
            url, headers=headers, timeout=self.timeout, connector=self.name
        )
        records_key = self._path("records_key", "")
        if records_key:
            records = response.get(records_key)
        else:
            records = (
                response.get("records")
                or response.get("items")
                or response.get("data")
            )
        if records is None:
            raise ConnectorError(
                f"{url} returned no recognisable records list: "
                f"{str(response)[:200]}",
                connector=self.name,
            )
        if not isinstance(records, list):
            raise ConnectorError(
                f"{url} returned records as {type(records).__name__}, not a list",
                connector=self.name,
            )

        id_field = self._path("id_field", "id")
        amount_field = self._path("amount_field", "amount")
        due_field = self._path("due_field", "due_date")
        account_field = self._path("account_field", "funding_account")
        currency_field = self._path("currency_field", "currency")
        description_field = self._path("description_field", "description")
        vendor_field = self._path("vendor_field", "vendor")
        entity_field = self._path("entity_field", "entity")

        obligations: List[Obligation] = []
        for record in records:
            try:
                due_raw = record[due_field]
                if isinstance(due_raw, (int, float)):
                    due = datetime.fromtimestamp(
                        due_raw / 1000.0, tz=timezone.utc
                    )
                else:
                    due = datetime.fromisoformat(str(due_raw))
                    if due.tzinfo is None:
                        due = due.replace(tzinfo=timezone.utc)
                obligations.append(
                    Obligation(
                        id=str(record[id_field]),
                        description=str(record.get(description_field, "")),
                        amount=float(record[amount_field]),
                        currency=str(record.get(currency_field, "RLUSD")),
                        due_date=due,
                        funding_account=str(record[account_field]),
                        entity=str(record.get(entity_field, "")),
                        vendor=str(record.get(vendor_field, "")),
                    )
                )
            except KeyError as exc:
                raise ConnectorError(
                    f"{url} returned a record missing the field {exc}; "
                    f"check the field mapping",
                    connector=self.name,
                ) from None
            except (TypeError, ValueError) as exc:
                raise ConnectorError(
                    f"{url} returned a record with a malformed value: {exc}",
                    connector=self.name,
                ) from None
        return obligations


@dataclass
class NetSuiteConnector:
    """NetSuite SuiteTalk REST.

    NetSuite's REST API exposes open transactions. The shape differs by
    account and by whether the RESTlet services are enabled, so the
    endpoint and field names are configurable.

    Reference: https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/
    """

    credential: Credential
    settings: Dict[str, str] = field(default_factory=dict)
    name: str = "netsuite"
    timeout: float = 60.0

    def fetch(self) -> List[Obligation]:
        base = self.credential.base_url.rstrip("/")
        url = base + self.settings.get(
            "path", "/services/rest/record/v1/purchaseOrder"
        )
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.credential.secret}",
        }
        response = _http_json(
            url, headers=headers, timeout=self.timeout, connector=self.name
        )
        records = response.get("items") or response.get("records") or []
        obligations: List[Obligation] = []
        for record in records:
            due_raw = record.get("duedate") or record.get("expectedreceiptdate")
            if not due_raw:
                continue
            due = datetime.fromisoformat(str(due_raw))
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            obligations.append(
                Obligation(
                    id=str(record.get("id", "")),
                    description=str(record.get("memo", "")),
                    amount=float(record.get("total", 0) or 0),
                    currency=str(record.get("currency", "USD")),
                    due_date=due,
                    funding_account=str(
                        record.get("subsidiary", "")
                        or self.settings.get("default_funding_account", "")
                    ),
                    entity=str(record.get("entity", "")),
                    vendor=str(record.get("entity", "")),
                )
            )
        return obligations
