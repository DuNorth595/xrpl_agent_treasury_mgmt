# Architecture

This document describes how the service is put together and, where a boundary
was chosen, why it is where it is.

---

## The shape of it

```
                          ┌──────────────────────────┐
                          │   The reference ERP       │
                          │   (or the real one)       │
                          └────────────┬─────────────┘
                                       │ read-only
                                       ▼
  ┌─────────────────────────────────────────────────────────────┐
  │  THE TREASURY SERVICE                       holds no key    │
  │                                                             │
  │   ┌────────────┐   ┌──────────────┐   ┌────────────────┐  │
  │   │obligations │──►│  reconcile   │──►│    planner     │  │
  │   └────────────┘   │ (fail closed)│   │ (multi-currency│  │
  │                     └──────────────┘   │   obligations) │  │
  │                                          └───────┬────────┘  │
  │                                                  │           │
  │                                          ┌───────▼────────┐  │
  │                                          │    policy      │  │
  │                                          │ 3 tiers, USD-  │  │
  │                                          │ normalised    │  │
  │                                          └───────┬────────┘  │
  │                                                  │           │
  │                                    ┌─────────────▼─────────┐ │
  │                                    │  signer + approvals  │ │
  │                                    │ (the gate: nothing   │ │
  │                                    │  is signed without   │ │
  │                                    │  the authorisations) │ │
  │                                    └─────────────┬─────────┘ │
  │                                                  │           │
  │                                    ┌─────────────▼─────────┐ │
  │                                    │  executor            │ │
  │                                    │ Payment(DomainID=…)  │ │
  │                                    └─────────────┬─────────┘ │
  │                                                  │           │
  │                                    ┌─────────────▼─────────┐ │
  │                                    │  agent log           │ │
  │                                    │ (hash-chained)       │ │
  │                                    └──────────────────────┘ │
  └──────────────────────────────────────────────┬──────────────┘
                                                 │ signed tx
                                                 ▼
                          ┌──────────────────────────┐
                          │  THE SIGNING SERVICE      │
                          │  holds the key            │
                          │  no policy, no obligations│
                          └────────────┬─────────────┘
                                       │ signed blob
                                       ▼
                          ┌──────────────────────────┐
                          │  THE XRP LEDGER           │
                          │  enforces the perimeter   │
                          └──────────────────────────┘
```

---

## The components

Every module, what it owns, and what it does not.

| Module | Owns | Deliberately does not own |
|---|---|---|
| `service.py` | The HTTP API, the wiring, the app lifecycle | Any business rule |
| `sample_api.py` | The 16 exploration endpoints (12 GET, 4 POST) | Any path that moves money |
| `policy.py` | The three tiers, the limits, the refusal reasons | The key, the ledger, the ERP |
| `signer.py` | The signer protocol, the approval ledger, the two signer implementations | The policy rules themselves |
| `executor.py` | The ledger client, building the Payment, simulating, submitting, reading history | Whether it *should* be submitted |
| `planner.py` | Turning obligations into transfers; the minimum-balance logic | The policy decision |
| `reconcile.py` | ERP against the ledger; the blocked state | The plan |
| `currency.py` | Issued assets, rates, positions, the multi-currency model | The rate *source* |
| `rates.py` | The rate providers, the cache, the refresh | The rate *values* — they come from a provider |
| `auth.py` | Credentials, hashing, scopes, minting | The route map |
| `auth_middleware.py` | The fail-closed route/scope enforcement | The token store |
| `agent_log.py` | The hash-chained decision log | The log's content decisions |
| `tenants.py` | Tenant configuration, the domain ID, the limits | Anything a caller supplies |
| `obligations.py` | The obligation model and the sources | The ERP protocol |
| `connectors.py` | The connector registry and the statuses | The connector implementations |
| `integrations.py` | Alternative signers, ERP connectors, obligation sources | The policy |
| `accounts.py` | The drops/XRP helpers | The treasury model |

---

## The boundaries, and why

### The service holds no key

The most important boundary in the system. The service can *ask* for a
signature; it cannot produce one.

A signer is anything that implements the `Signer` protocol. Two ship:

- `LocalSeedSigner` — reads a seed from disk. For the devnet only.
- `ExternalSignerSigner` — sends the transaction to an HTTP signing service
  and receives a signature back.

The signing service is deliberately ignorant. It has no policy engine and no
idea what a treasury obligation is. It signs what it is given, and it refuses
to sign for an account it holds no key for. That refusal is what makes it a
signer rather than a signing oracle.

The consequence: a compromise of the treasury service process does not, by
itself, move money. The attacker still needs the signing service, and the
signing service will happily sign anything it is handed — so the *policy*
has to be the thing that holds, which is why it lives in the service.

**What this does not give you.** The signing service signs whatever it is
given. If the treasury service is compromised *before* it asks for a
signature, the signature will be given. The boundary raises the cost of a
compromise; it does not make the policy unnecessary.

### The perimeter is the ledger's

Every payment carries a `DomainID`, resolved from tenant configuration. It is
never taken from a request. The check is performed by rippled: a payment to a
non-participant is refused with `tecNO_PERMISSION`.

This is verified behaviourally, not by inspecting a list of amendment flags —
the harness simulates a payment to a non-participant and requires the
ledger's own refusal.

**The honest scope.** The domain gate constrains the *agent*. A cost-centre
account the agent funds may move that value onward under whoever holds that
account's keys. The domain gate constrains the agent, not the recipient.

### Reconciliation runs before the planner

The ERP is authoritative for *what is owed*. The ledger is authoritative for
*what exists*. The reconciler answers "has this already been funded?" before
the planner decides anything.

And it **fails closed**. If the ledger cannot be read, the report carries
`blocked=True`, the planner withholds every transfer, and the obligations are
marked `reconciliation_blocked`. It does not fall back to an empty plan,
because an empty plan is indistinguishable from nothing being due — and the
distinction is exactly the one that matters.

### Policy is USD-normalised

The treasury is dollar-denominated. The obligations are not: a bill may be in
CAD, a supplier's invoice in MXN.

Limits, thresholds, and the minimum-balance floor are all evaluated at the
**USD equivalent**. A CA$1,000 limit is not a US$1,000 limit — at the
rate in use (1.3974 CAD per USD) it is about US$715.61, and a comparison
against the raw native amount would let the limit be exceeded by 40%.

The XRP fee floor is compared against the XRP balance, and the spend floor
against the settlement balance. They are different currencies and they are
not compared to each other.

### The tier model

| Tier | Trigger | Sign-offs |
|---|---|---|
| Level 1 | nothing warrants human judgement | 0 — automatic |
| Level 2 | inside the due-date buffer (short notice), or no linked obligation | 1 |
| Level 3 | a first payment to this counterparty, beyond the treasury manager's delegated authority, or a participant designated for supervisor review | 2 |
| Refused | outside the policy envelope entirely | — |

The counts are configurable per tenant through the approval matrix.

The approval ledger is keyed on the **obligation and the transfer**, not on
an empty string. An earlier version keyed it on an empty string, which meant
one sign-off could authorise an unrelated transfer. That was found by the
test suite and is now covered by a test.

### The decision log is hash-chained

Every decision appends to a log where each entry carries the hash of the
entry before it. Altering an entry breaks the chain, and the log stops
verifying. The log is written to `var/decision-log.jsonl`.

---

## The data flow of one payment

1. **Obligation** — the ERP says a cost centre owes a supplier CA$3,750 on a
   date.
2. **Reconcile** — has this already been funded? The ledger is read via
   `account_tx`, and the on-chain payments are flattened — XRP payments and
   issued-currency payments alike — into a comparable shape.
3. **Plan** — the obligation becomes a transfer, with the currency it is
   denominated in. If the reconciliation was blocked, no transfer is produced.
4. **Policy** — the transfer is evaluated. The limits are checked at the USD
   equivalent. The tier is decided. An out-of-policy transfer is refused with
   a named reason.
5. **Authorise** — the required sign-offs are recorded against the transfer.
6. **Build** — a `Payment` is built with `DomainID` from the tenant
   configuration. For an issued currency, `send_max` is always set — a
   cross-issue payment without it is invalid.
7. **Sign** — the signer is asked. With an external signer, the transaction
   goes over HTTP and a signed blob comes back. Without the required
   authorisations, the signer refuses.
8. **Submit** — the signed transaction is submitted. The ledger's own
   response is the authoritative transaction hash; the service does not
   compute one.
9. **Log** — the decision, the tier, the authorisations, and the hash are
   appended to the chain.

---

## Authentication

One `before_request` hook, plus an explicit route-to-scope map.

| Scope | Allows |
|---|---|
| `treasury:read` | the position, the plan, the obligations, the log, the authorisations |
| `treasury:propose` | a dry run |
| `treasury:authorise` | recording a sign-off |
| `treasury:settle` | submitting a real payment |
| `treasury:admin` | everything |

Tokens are stored as SHA-256 hashes and compared in constant time, so a leaked
configuration file does not yield a usable credential.

**A credential can also be restricted to particular tenants.** The middleware
reads the tenant id from the route and calls `require_tenant` on the
credential. A credential with tenants listed is refused for any tenant not
on its list; a credential with no tenants listed is unrestricted.

That makes two independent axes. A scope says *what* a caller may do; the
tenant restriction says *whose* money they may do it to. A credential with
`treasury:settle` scoped to one tenant can move that tenant's money and no
other tenant's.

**The route map is a gate, not a warning.** A route with no declared scope is
**refused**, not allowed. Adding a route without saying what it needs cannot
silently make it public — the failure mode is a 403 on a new endpoint, not an
unprotected one.

Health, the dashboard, and the `/api/sample/*` endpoints are deliberately
open in sample mode so the behaviour can be explored without a credential.

---

## Where the numbers come from

| Number | What it measures | How it was measured |
|---|---|---|
| 187 unit tests | the pure logic (177 + 10 skipped without a devnet) | `pytest tests/` |
| 38/38 API checks | the HTTP contract | `api_test.py` against a live service |
| 17/17 assertions | the service end to end | `verify.py` against a live devnet |
| 4/4 scale phases | the scale profile, plus an opt-in settlement phase | `scale_test.py` |
| 3,801 instr/sec | **service-side** throughput | 1,000,000 instructions planned, evaluated, authorised, built and signed |
| 97 MB peak | the memory ceiling | the same run |
| ~0.3 tx/sec | **ledger** settlement | one transaction per closed ledger; the local devnet closes a ledger about every 3 seconds |

The last two rows are different things and are never combined. The first is
what the service can process; the second is what the devnet can settle.
Real throughput is bounded by the second.

---

## What is deliberately not here

- **No vault logic.** `SingleAssetVault` is vetoed on the node.
- **No batch transfers.** `BatchV1_1` is vetoed, and it was only ever wanted
  for atomic credential onboarding, which is two steps here.
- **No external payments by the agent.** The agent pre-funds internal
  accounts. Making the external payment is a separate actor's job.
- **No forecasting model.** A buffer and a due date, not a prediction.
- **No MCP surface.** Deferred until there is a reason.
