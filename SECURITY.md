# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | yes |

This is a pre-1.0 project. The API may change between minor versions.

## Reporting a vulnerability

Report security issues privately to the maintainer at
S_DevLabs@outlook.com. Do not open a public issue for a vulnerability — an
open issue is a public disclosure.

Include: what an attacker can do, how to reproduce it, and which version or
commit you tested.

## Threat model

This service handles treasury disbursements for the tenants it is configured
to serve. It is designed so that a compromise of the service process does not,
by itself, move money.

**The service holds no signing key.** Transactions are signed by a separate
signing service. The service can ask for a signature; it cannot produce one.
The signature that comes back is verified against the transaction that was
requested — the account, destination, amount, fee, sequence, and domain id
must all match — so a signer that returns a signature over a different
destination is rejected rather than submitted.

**The ledger enforces the perimeter.** Every payment carries a
`PermissionedDomain`. The XRPL refuses a payment to a non-participant with
`tecNO_PERMISSION`. This is not an application-level check — it is the
ledger's.

**The policy is the gate.** A transaction is only signed when the policy
engine clears it AND the required human authorisations are recorded. An
escalated transaction cannot be signed early. A sign-off must name its
approver, and must name an obligation that is in the current plan.

**It fails closed.** If the ERP or the ledger cannot be read, the plan is
withheld with a stated reason. It is never presented as an empty plan,
because an empty plan looks identical to nothing being due.

**The log is tamper-evident.** Every decision is appended to a hash-chained
log. A modified entry breaks the chain and the log stops verifying.

### What the boundary does not buy you

The signing service signs whatever it is given. If the treasury service is
compromised *before* it asks for a signature, the signature will be granted.
The boundary raises the cost of a compromise; it does not make the policy
unnecessary.

The domain gate constrains the *agent*. An account the agent funds may move
that value onward under whoever holds that account's keys. It is not a claim
that funds cannot leave the tenant's domain.

## Seed-handling rules (for contributors)

**No seed, private key, or token goes in the repository. Ever.** Not in the
source, not in a test, not in a fixture, not in a comment.

The devnet account *addresses* are equally sensitive in a way that is easy to
miss: the local devnet accounts file holds the seeds for those same
addresses, so publishing an address publishes half of what is needed to
spend from it. Accounts are therefore resolved **by role**, at runtime, from
the local accounts file — see `src/xrpl_agent_treasury_mgmt/devnet_accounts.py`.

What to do:

- Use `devnet_accounts.address_for("TREASURY")`, not a literal.
- A credential you need at runtime comes from the environment, not from the
  source.
- A test that needs a "secret" uses an obviously fake value — `super-secret`
  is fine; a real seed is not.
- The value `sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r` is the documented xrpl.js
  example seed. It is public and appears in the XRPL documentation. It is the
  only seed allowed in the tree, and only in the scanner's allow-list.

Run the scanner before you open a pull request:

```bash
python3 scripts/scan_for_secrets.py .
```

It exits non-zero on anything credential-shaped, and it runs in CI and in
pre-commit.

## Cryptographic choices

- **Token storage.** API tokens are stored as SHA-256 hashes, so a leaked
  configuration file does not yield a usable credential. Comparison is
  constant-time, so it does not leak the token through its timing.
- **The decision log.** Each entry commits to the SHA-256 hash of the entry
  before it. The chain detects alteration and truncation from the tail; it
  does not prevent either. Ship the log somewhere durable and append-only.
- **The transaction hash.** It comes from rippled, not from this service. The
  transaction ID is rippled's SHA-256 over the transaction's canonical
  single-signing form, which is neither the signing pre-image nor the signed
  blob. A hash computed locally would look authoritative and be wrong.
- **The domain id.** It is resolved from tenant configuration and never
  accepted from a request.

## Dependencies

| Package | Why | Version |
|---|---|---|
| `xrpl-py` | the XRPL client | >= 4.5 |
| `flask` | the HTTP surface | >= 3.0 |
| `httpx` | the client for the external signer | >= 0.27 |
| `websockets` | the ledger connection | >= 13 |
| `waitress` | the production WSGI server | >= 3.0 |

`waitress` is the default server. Flask's development server is available
behind `--server flask` and is for local poking only.

## Known limitations (honest disclosures from live testing)

These are stated plainly rather than left to be discovered.

- **No TLS.** The service speaks plain HTTP. It must sit behind a TLS
  terminator, or terminate TLS itself. The bearer token is a plaintext
  credential and must not cross an untrusted network.
- **No token expiry or rotation.** A token is valid until revoked, and
  revocation is in-process. A deployment needs an expiry and a rotation path.
- **No rate limiting.** A caller with a valid token can make as many
  requests as they like.
- **No request signing.** The bearer token alone authenticates a request. A
  replay window exists.
- **The included signing service is a stand-in.** It holds devnet keys in a
  local file. A real deployment points the service at a custody service or
  KMS instead.
- **The decision log is a single file.** More than one worker will contend
  on it.
- **Everything was verified against a local devnet** at network 15791. The
  mainnet amendment state was not verified from this machine.
- **The scale numbers are service-side.** 3,801 instructions/sec is how fast
  the service plans, evaluates, authorises, builds and signs. It is not a
  settlement rate. One transaction settles per closed ledger, and the devnet
  closes a ledger about every three seconds, so settlement caps near 0.3
  transactions/second.

## Deployment checklist

- [ ] TLS terminated in front of the service, or by the service
- [ ] `TREASURY_API_TOKENS` supplied from a secret store, not a file
- [ ] The signing service is a real custodian or KMS, not the local stand-in
- [ ] The signing service's audit log is shipped somewhere durable
- [ ] Rate limiting is in front of the service
- [ ] The service binds to a private interface or a service mesh
- [ ] The decision log is shipped somewhere durable and append-only
- [ ] The ledger's amendment state verified: `PermissionedDomains`,
      `Credentials`, `fixCleanup3_4_0`
- [ ] The `--network-id` argument matches the node
- [ ] The tenant configuration's domain ids are the real ones
- [ ] The limits and the approval matrix reviewed against the policy
- [ ] The ERP connector pointed at the real ERP and the reconciliation
      confirmed against known payments
- [ ] The rates provider pointed at a real source, and the fallback
      behaviour understood
- [ ] A dry run reviewed against a hand-computed expectation
- [ ] The alert on reconciliation failures configured
