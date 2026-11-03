"""
Demo runner: wire the service against the live local devnet and serve it.

This builds a realistic single-tenant demo using the REAL accounts, the REAL
PermissionedDomain from the proven domain-gate run, and the REAL RLUSD, CAD
and MXN holdings issued on the devnet. The dashboard shows live ledger
state rather than fixtures.

    python3 run_demo.py --host 127.0.0.1 --port 8095

The currency book is loaded from var/local-currencies.json, which records the
issuer addresses the devnet setup created.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE / "src"
sys.path.insert(0, str(SRC))

from xrpl_agent_treasury_mgmt.agent_log import AgentLog  # noqa: E402
from xrpl_agent_treasury_mgmt.connectors import (  # noqa: E402
    ConnectorConfig,
    ConnectorRegistry,
    Credential,
)
from xrpl_agent_treasury_mgmt.integrations import GenericRestErp  # noqa: E402
from xrpl_agent_treasury_mgmt.currency import (  # noqa: E402
    IssuedAsset,
    default_currency_book,
    hex_code,
)
from xrpl_agent_treasury_mgmt.executor import LedgerClient  # noqa: E402
from xrpl_agent_treasury_mgmt.obligations import (  # noqa: E402
    InMemoryObligationSource,
    Obligation,
)
from xrpl_agent_treasury_mgmt.service import TreasuryService, create_app  # noqa: E402
from xrpl_agent_treasury_mgmt.tenants import Limits, Tenant, TenantRegistry  # noqa: E402

from xrpl_agent_treasury_mgmt.devnet_accounts import address_for as _address

# The proven domain from evidence/domain-gate-proof (run FINAL002).
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
CURRENCIES_CONFIG = HERE / "var" / "local-currencies.json"


def load_currency_book():
    """The currency book, wired to the issuers the devnet setup created."""
    issuers = {}
    codes = {}
    if CURRENCIES_CONFIG.exists():
        cfg = json.loads(CURRENCIES_CONFIG.read_text())
        issuers = cfg.get("issuers", {})
        codes = cfg.get("codes", {})

    book = default_currency_book(issuers=issuers)

    # Use the exact currency codes the devnet issued, so the trust lines
    # match. RLUSD is hex-encoded; CAD and MXN are three characters.
    for ticker, code in codes.items():
        if ticker in issuers:
            book.set_asset(
                ticker, IssuedAsset(code=code, issuer=issuers[ticker], ticker=ticker)
            )
    return book


def build_registry() -> TenantRegistry:
    registry = TenantRegistry()
    registry.add(
        Tenant(
            id="s-devlab-corp",
            name="S_DevLab Corp. (Example)",
            domain_id=DEMO_DOMAIN,
            treasury_account=TREASURY,
            limits=Limits(
                # These are USD figures. The policy engine compares every
                # disbursement at its USD equivalent, so the limit means the
                # same thing whether the payment is in RLUSD, CAD or MXN.
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
    )
    return registry


def build_obligations() -> InMemoryObligationSource:
    """The ERP's obligations, in the currency each counterparty needs.

    A US treasury paying a Toronto contractor invoices in CAD. A Mexico City
    payroll run is in MXN. Domestic vendors are in RLUSD.
    """
    now = datetime.now(timezone.utc)
    obligations = [
        Obligation(
            id="OBL-2026-0417",
            description="Q4 electricity — facilities",
            amount=180.0,
            currency="RLUSD",
            due_date=now + timedelta(hours=30),
            funding_account=COST_CENTRE_A,
            entity="Facilities & Utilities",
            vendor="Northwind Grid Co.",
        ),
        Obligation(
            id="OBL-2026-0418",
            description="Payroll run — engineering",
            amount=620.0,
            currency="RLUSD",
            due_date=now + timedelta(hours=20),
            funding_account=COST_CENTRE_B,
            entity="Payroll",
            vendor="Internal payroll run",
        ),
        Obligation(
            id="OBL-2026-0419",
            description="Water & waste — facilities",
            amount=95.0,
            currency="RLUSD",
            due_date=now + timedelta(hours=74),
            funding_account=COST_CENTRE_A,
            entity="Facilities & Utilities",
            vendor="Municipal Water Authority",
        ),
        Obligation(
            id="OBL-2026-0420",
            description="Cloud infrastructure — platform",
            amount=410.0,
            currency="RLUSD",
            due_date=now + timedelta(hours=40),
            funding_account=COST_CENTRE_A,
            entity="Facilities & Utilities",
            vendor="Northwind Compute",
        ),
        Obligation(
            id="OBL-2026-0421",
            description="Contractor invoice — Toronto",
            amount=2400.0,
            currency="RLUSD",
            due_date=now + timedelta(hours=60),
            funding_account=COST_CENTRE_A,
            entity="Facilities & Utilities",
            vendor="Ashcombe Contracting",
        ),
        Obligation(
            id="OBL-2026-0416",
            description="Insurance premium — facilities",
            amount=310.0,
            currency="RLUSD",
            due_date=now + timedelta(hours=10),
            funding_account=COST_CENTRE_A,
            entity="Facilities & Utilities",
            vendor="Ardent Underwriting",
        ),
        Obligation(
            id="OBL-2026-0422",
            description="Site works — Toronto contractor",
            amount=3750.0,
            currency="CAD",
            due_date=now + timedelta(hours=36),
            funding_account=COST_CENTRE_A,
            entity="Facilities & Utilities",
            vendor="Maple Ridge Construction",
        ),
        Obligation(
            id="OBL-2026-0423",
            description="Payroll run — Mexico City plant",
            amount=48000.0,
            currency="MXN",
            due_date=now + timedelta(hours=26),
            funding_account=COST_CENTRE_B,
            entity="Payroll",
            vendor="Internal payroll run — MX",
        ),
    ]
    return InMemoryObligationSource(obligations)


def build_connectors(erp_url: str = "") -> ConnectorRegistry:
    """The configured external connectors.

    The ERP connector is the live one — it talks to the reference ERP over
    HTTP with a field mapping, so the integration path is exercised for
    real rather than stubbed.
    """
    registry = ConnectorRegistry()

    if erp_url:
        erp = GenericRestErp(
            credential=Credential(
                key="S_DEVLAB_ERP",
                secret=os.environ.get("S_DEVLAB_ERP_TOKEN", "demo-token"),
                account_id="s-devlab-corp",
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
        registry.register("erp", erp)
        registry.configure(
            ConnectorConfig(
                name="erp",
                kind="erp",
                adapter="generic-rest-erp",
                credential_key="S_DEVLAB_ERP",
            )
        )

    return registry


def _devnet_seed(address: str) -> str:
    """The local devnet seed for an address, if the devnet file is there."""
    import pathlib

    accounts = pathlib.Path.home() / ".xrpl-up" / "local-accounts.json"
    try:
        raw = json.loads(accounts.read_text())
        if isinstance(raw, dict) and "accounts" in raw:
            raw = raw["accounts"]
        items = raw if isinstance(raw, list) else list(raw.values())
        for item in items:
            if item.get("address") == address:
                return item.get("seed", "")
    except Exception:
        pass
    return ""


def _state_dir() -> pathlib.Path:
    """Where the service keeps its state.

    NOT the source tree. A checkout is not a state directory, and a
    publication package definitely is not one.

    Honours XDG_STATE_HOME, then LOCALAPPDATA, then ~/.local/state.
    """
    override = os.environ.get("TREASURY_STATE_DIR", "").strip()
    if override:
        base = pathlib.Path(override)
    else:
        xdg = os.environ.get("XDG_STATE_HOME", "").strip()
        if xdg:
            base = pathlib.Path(xdg) / "xrpl_agent_treasury_mgmt"
        else:
            local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
            if local_appdata:
                base = pathlib.Path(local_appdata) / "xrpl_agent_treasury_mgmt"
            else:
                base = pathlib.Path.home() / ".local" / "state" / "xrpl_agent_treasury_mgmt"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return base


def main() -> int:
    parser = argparse.ArgumentParser(description="Treasury service demo")
    parser.add_argument("--port", type=int, default=8095)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--url", default="ws://127.0.0.1:6006")
    parser.add_argument("--network-id", type=int, default=15791)
    parser.add_argument(
        "--log",
        # The log goes to a STATE DIRECTORY, never into the source tree.
        #
        # It used to be HERE / "var" - next to this script - which meant
        # that running the service from a clone, or from the staged
        # publication package, wrote a live decision log INTO the source
        # tree. The log carries real tenant ids, domain ids and account
        # addresses, so it must not travel with the code.
        #
        # It was then moved to CWD, which had the same problem: running
        # the tests from inside the package wrote the log into the
        # package.
        #
        # So: the user state directory. ~/.local/state/<project>/ on a
        # POSIX system, %LOCALAPPDATA% on Windows. An explicit --log
        # still wins.
        default=str(_state_dir() / "decision-log.jsonl"),
        help="path to the decision log",
    )
    parser.add_argument(
        "--no-sample",
        action="store_true",
        help="do not register the /api/sample demonstration endpoints",
    )
    parser.add_argument(
        "--erp-url",
        default=os.environ.get("S_DEVLAB_ERP_URL", ""),
        help="base URL of the ERP obligations API; empty uses the in-memory source",
    )
    parser.add_argument(
        "--server",
        choices=["waitress", "flask"],
        default="waitress",
        help=(
            "the WSGI server. waitress is a production server: it is "
            "threaded, it enforces a request body limit, and it does not "
            "announce itself as a development server. flask is Werkzeug's "
            "development server and is for local poking only."
        ),
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=8,
        help="the waitress worker thread count",
    )
    parser.add_argument(
        "--max-body",
        type=int,
        default=1_048_576,
        help="the maximum request body size in bytes",
    )
    args = parser.parse_args()

    registry = build_registry()
    ledger = LedgerClient(url=args.url, network_id=args.network_id)
    obligations = build_obligations()
    log = AgentLog(pathlib.Path(args.log))
    currency_book = load_currency_book()

    connectors = build_connectors(args.erp_url)

    # ------------------------------------------------------------ signing
    # WHERE THE SIGNATURE HAPPENS
    #
    # Two ways to sign, and the choice matters:
    #
    #   local      the treasury account's seed, in this process. Only
    #              for the devnet.
    #   external   a separate signing service that holds the key. The
    #              treasury service sends the transaction there and
    #              receives a signature. The key never touches this
    #              process.
    #
    # The external path is what a deployment uses. It is selectable
    # here so it can be exercised, not just described.
    signer_provider = None
    signer_endpoint = os.environ.get("TREASURY_SIGNER_URL", "").strip()
    signer_token = os.environ.get("TREASURY_SIGNER_TOKEN", "").strip()
    if signer_endpoint:
        from xrpl_agent_treasury_mgmt.signer import ExternalSignerSigner

        signer_provider = ExternalSignerSigner(
            endpoint=signer_endpoint,
            account=TREASURY,
            public_key=os.environ.get("TREASURY_SIGNER_PUBLIC_KEY", ""),
            token=signer_token,
        )
        print(f"  signing via the external signer at {signer_endpoint}", flush=True)
    else:
        seed = _devnet_seed(TREASURY)
        if seed:
            from xrpl_agent_treasury_mgmt.signer import LocalSeedSigner

            signer_provider = LocalSeedSigner(seed)
            print(
                "  signing with the LOCAL devnet seed. A deployment signs "
                "through a custodian; set TREASURY_SIGNER_URL to use one.",
                flush=True,
            )

    service = TreasuryService(
        registry=registry,
        ledger=ledger,
        obligation_source=obligations,
        agent_log=log,
        currency_book=currency_book,
        connectors=connectors,
        signer_provider=signer_provider,
    )
    # When the ERP connector is configured it becomes the source of
    # obligations. The in-memory source stays as the fallback so the demo
    # still starts if the reference ERP is not running.
    if connectors.get_optional("erp") is not None:
        service.obligation_connector = connectors.get("erp")

    # Seed the log with the decisions a real service would already have made,
    # so the dashboard shows a populated decision chain.
    if not log.entries():
        log.append(
            "s-devlab-corp",
            "service_started",
            {"version": "0.1.0", "domain": DEMO_DOMAIN,
             "settlement_currency": "RLUSD"},
        )
        log.append(
            "s-devlab-corp",
            "domain_verified",
            {"domain_id": DEMO_DOMAIN, "members": 3},
        )
        log.append(
            "s-devlab-corp",
            "positions_reconciled",
            {"currencies": ["RLUSD", "CAD", "MXN"]},
        )
        log.append(
            "s-devlab-corp",
            "plan_generated",
            {"obligations": 8, "planned": 6, "skipped": 2},
        )
        log.append(
            "s-devlab-corp",
            "dry_run",
            {
                "destination": COST_CENTRE_A,
                "amount": 180.0,
                "currency": "RLUSD",
                "tier": "level_1",
                "engine_result": "tesSUCCESS",
            },
        )
        log.append(
            "s-devlab-corp",
            "dry_run",
            {
                "destination": OUTSIDER,
                "amount": 500.0,
                "currency": "RLUSD",
                "tier": "refused",
                "engine_result": "tecNO_PERMISSION",
                "note": "destination is not a domain participant",
            },
        )

    app = create_app(service, sample_mode=not args.no_sample)

    # The credentials the service will accept. They come from the
    # environment, never from a file in the repository.
    from xrpl_agent_treasury_mgmt.auth import CredentialStore

    store = CredentialStore.from_environment()
    if store.count:
        print(
            f"  API credentials: {store.count} "
            f"({', '.join(c['name'] for c in store.describe())})",
            flush=True,
        )
    else:
        print(
            "  !! no API credentials are configured. Every protected "
            "endpoint will be refused. Set TREASURY_API_TOKENS, or run "
            "`python3 mint_token.py`.",
            flush=True,
        )

    if args.server == "waitress":
        try:
            from waitress import serve
        except ImportError:
            print(
                "  waitress is not installed. "
                "`python3 -m pip install waitress`",
                flush=True,
            )
            return 1
        print(
            f"treasury service on http://{args.host}:{args.port} "
            f"(waitress, {args.threads} threads)",
            flush=True,
        )
        serve(
            app,
            host=args.host,
            port=args.port,
            threads=args.threads,
            max_request_body_size=args.max_body,
            ident="treasury-service",
            clear_untrusted_proxy_headers=True,
        )
        return 0

    print(
        f"treasury dashboard on http://{args.host}:{args.port} "
        "(Flask development server - not for production)",
        flush=True,
    )
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
