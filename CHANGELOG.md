# Changelog

All notable changes to `xrpl_agent_treasury_mgmt` are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-11-02

First release. A treasury tool for the XRP Ledger: it
reconciles ERP obligations against on-chain payments, applies a three-tier
authorisation policy, signs through a separate signing service, and confines
every payment to a ledger-enforced `PermissionedDomain`.

### Added

- **A treasury service** that plans disbursements in the currency each
  counterparty invoices in, evaluates them against per-tenant policy limits,
  and submits the authorised ones.
- **A reconciler** that matches ERP obligations against on-chain payments so
  nothing is funded twice. It fails closed: if the ERP or the ledger cannot
  be read, the plan is withheld with a stated reason rather than presented as
  empty.
- **A three-tier policy engine** — automatic, one sign-off, two sign-offs —
  with limits evaluated at the USD equivalent of the obligation's currency.
  Hard limits are refusals, not escalations.
- **An external signing path.** The service holds no key. It sends the
  transaction to a separate signing service and receives a signature. The
  returned signature is verified against the transaction that was requested,
  so a signer that returned a signature over a different destination is
  rejected.
- **A ledger-enforced tenant perimeter.** Every payment carries the
  `DomainID` from the tenant's configuration, resolved at runtime and never
  taken from a request. rippled refuses a payment outside the domain with
  `tecNO_PERMISSION`.
- **Scoped bearer-token authentication.** `treasury:read`, `treasury:propose`,
  `treasury:authorise`, `treasury:settle`, `treasury:admin`. Tokens are
  stored as SHA-256 hashes, compared in constant time, and a credential may
  be restricted to particular tenants. A route with no declared scope is
  refused, not allowed.
- **A hash-chained decision log.** Each entry commits to the one before it, so
  an altered entry breaks the chain and the log stops verifying.
- **A self-building test environment.** `dev_env.py fresh` wipes the ledger,
  rebuilds the currencies, the domain, the treasury's funding and the domain
  membership, starts the services, and runs the suite — about 130 seconds
  from an empty ledger.
- **Four verification layers.** 187 unit tests (177 plus 10 that need a
  devnet), 38 API contract checks, 17
  end-to-end assertions against a live devnet, and a scale harness.
- **A scale harness.** 1,000,000 instructions planned, evaluated, authorised,
  built and signed at 3,801/sec with a 97 MB peak and the policy holding
  throughout — zero instructions missing a `DomainID`, zero outside the
  envelope. Service-side throughput; ledger settlement is a separate number.
- **Sixteen sample endpoints** under `/api/sample/` for exploring the
  behaviour without a credential. Twelve are `GET` and four are `POST`; none
  submits a transaction except `/api/sample/settle`, which requires
  `"confirm": true`.
- **A credential scanner** that ships with the package and runs in CI and in
  pre-commit. It refuses to write a package containing a credential.

### Security

- The tenant boundary is enforced at the API edge: every route that names a
  tenant resolves that tenant before it does anything else, so a route can
  never answer for a tenant that does not exist.
- An authorisation must name its approver, and must name an obligation that
  is in the current plan. A sign-off by nobody, or against nothing, is
  refused.
- The external signer verifies the transaction it is given. The account,
  destination, amount, fee, sequence, and domain id must all match.
- The executor advances the account sequence, so two transactions from one
  account cannot carry the same sequence.
- `mint_token.py --scopes` is repeatable and combines every occurrence.
  Repeating it previously kept only the last value, which silently produced a
  token with more privilege than was asked for.
- The service's state lives outside the source tree. Running the service or
  the tests from a checkout no longer writes a live decision log into it.

### Known limitations

Stated plainly rather than left to be discovered. See [TODO.md](TODO.md) and
[SECURITY.md](SECURITY.md).

- No TLS. The service speaks plain HTTP and must sit behind a terminator.
- No token expiry and no rotation path.
- No rate limiting.
- No request signing, so there is a replay window.
- The included signing service holds devnet keys in a local file. It is a
  stand-in for a custodian or a KMS, and a deployment points elsewhere.
- Everything was verified against a local devnet at network 15791. The
  mainnet amendment state was not verified.

[0.1.0]: https://github.com/DuNorth595/xrpl_agent_treasury_mgmt/releases/tag/v0.1.0
