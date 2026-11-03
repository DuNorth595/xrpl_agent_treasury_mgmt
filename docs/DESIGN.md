# Design

The decisions behind the service, and the alternatives that were considered
and rejected. Each entry says what was chosen, why, and what it costs.

---

## 1. The ledger is the tenant boundary

**Chosen.** Each tenant has a `PermissionedDomain`. Every payment carries the
`DomainID` from that tenant's configuration. The XRPL refuses a payment
between a member and a non-participant with `tecNO_PERMISSION`.

**Why not an application check?** Because an application check is a promise.
A prompt injection, a bug, a misconfigured environment, a compromised
dependency — any of them can make the application skip its own check. The
ledger does not skip checks. The perimeter is enforced by the thing that
holds the money.

**Why not one domain for everything?** Then a tenant-mixing bug is a real
breach rather than a data leak. A multi-tenant service that shares one
perimeter has one perimeter, and every tenant is inside it.

**The cost.** The domain ID must never come from a caller. It is resolved
from tenant configuration, and there is no code path that can produce a
Payment without it. That is an invariant to maintain, and it is exactly the
kind of thing that erodes.

**The honest limit.** The gate constrains the *agent*. A cost-centre account
the agent funds may move that value onward. The domain gate constrains the
agent, not the recipient. Anyone who tells you the domain gate prevents
funds from leaving the tenant's domain is overstating it.

---

## 2. The service holds no key

**Chosen.** Signing happens in a separate service. The treasury service sends
the transaction and receives a signature.

**Why.** It separates the thing that *decides* from the thing that *can move
money*. A compromise of the decision layer does not directly yield money.

**Why not just keep the key in the service?** Because then the service is the
key, and every other control — the policy, the authorisation workflow, the
domain gate — only protects a process that can ignore all of them. The
boundary is what makes those controls worth having.

**The cost, stated plainly.** The signing service signs whatever it is
given. If the treasury service is compromised *before* it asks for a
signature, the signature will be granted. The boundary raises the cost of a
compromise. It does not replace the policy.

**The alternative.** A KMS or HSM directly would be better in production, but
it is not local-testable, and the point of the local stand-in is that the
*contract* is exercised — the same HTTP shape, the same refusals, the same
audit trail. Swapping the endpoint is the only change a deployment makes.

---

## 3. Reconciliation runs before the planner, and fails closed

**Chosen.** The ERP is authoritative for what is owed. The ledger is
authoritative for what exists. The reconciler answers "is this already
funded?" *before* the planner decides anything.

**Why.** A planner that trusts the ERP alone will re-fund obligations that
were already paid. The failure is silent and expensive.

**Why fail closed?** Because the alternative is worse than an error. If the
ledger cannot be read and the service falls back to an empty plan, then a
ledger outage and "nothing is due" look identical to the caller — and the
caller may well act on that. With `blocked=True`, no transfer is planned, the
obligations are marked `reconciliation_blocked`, and the reason is in the
response.

**The cost.** A ledger outage stops the service. That is the point, but it is
a real availability trade-off, and it is the right one for a system that moves
money.

---

## 4. Limits are compared at the USD equivalent

**Chosen.** The treasury is dollar-denominated. Limits, thresholds, and the
minimum-balance floor are evaluated at the USD equivalent of the obligation's
currency. The XRP fee floor is compared against the XRP balance; the spend
floor against the settlement balance.

**Why.** The alternative — comparing a raw native amount against a USD limit
— is silently wrong by the exchange rate. At 1.3974 CAD per USD, a
CA$1,000 limit is about US$715.61, and CA$1,400 is about US$1,001.86. The
test suite caught exactly this: a scale run allowed 4,000
CAD instructions under a USD cap.

**The subtlety.** The floor and the limit are different currencies and are
not compared to each other. An earlier version subtracted a USD amount from
the RLUSD balance and compared the result against an *XRP* limit — so every
transfer on a well-funded treasury was refused. The parameter was named
`treasury_balance_xrp` and did not hold XRP.

**A known wrinkle.** Floating-point boundaries can escalate by a hair.
`700 / 1.40 = 500.00000000000006` — a mathematically exact threshold lands
just above it. A production version should use scaled integers or an explicit
tolerance. This is recorded rather than hidden.

---

## 5. Three tiers, and the sign-offs are the gate

**Chosen.** Level 1 is automatic when nothing warrants human judgement.
Level 2 needs one sign-off. Level 3 needs two. The counts are per tenant.
Outside the policy envelope entirely, the transfer is refused with a named
reason.

The escalation reasons are specific, and each one says which tier it
implies:

| Reason | Tier |
|---|---|
| a first payment to this counterparty | Level 3 |
| beyond the treasury manager's delegated authority | Level 3 |
| the participant is designated for supervisor review | Level 3 |
| inside the due-date buffer (short notice) | Level 2 |
| no linked obligation | Level 2 |

**Hard limits are refusals, not escalations.** A transfer over the single
limit or the daily cap is refused outright — no amount of authorisation
makes an over-limit transfer compliant. That is a different thing from a
transfer that needs more human judgement, and conflating them would let
authorisation paper over a breach.

**Why tiers rather than a binary?** Because a binary — approve or refuse —
pushes the judgement to the caller and makes the routine case expensive. The
routine case should be cheap and automatic; the unusual case should be
expensive and require people.

**Why sign-offs are enforced in the signer and not in the API?** Because the
signer is the last thing that touches the transaction. A check in the API is
a check that can be skipped; a check in the signer is a check that cannot be
bypassed without going around the signer entirely.

**The bug this design caught.** The approval ledger was keyed on an empty
string, which meant a sign-off for one transfer could authorise an unrelated
one. It is now keyed on the obligation *and* the transfer, and there is a
test.

---

## 6. One `before_request` hook with an explicit route map

**Chosen.** A single middleware, and a map from route to required scope.

**Why a map rather than decorators?** A decorator on each route is easy to
forget. Forgetting means the route has no scope, and "no scope" has to
resolve to something. If it resolves to "allow", forgetting is a
vulnerability. If it resolves to "refuse", forgetting is a 403 on a new
endpoint — an inconvenience, not a hole.

So: **an unknown route is refused.** The failure mode of an incomplete map
is a broken endpoint, not an open one.

**Why a hash rather than plaintext?** A leaked configuration file then does
not yield a usable credential. Why constant-time comparison? So the
comparison does not leak the token through its timing.

---

## 7. The transaction hash comes from the ledger

**Chosen.** The service records the hash that rippled returns. It does not
compute one.

**Why.** The transaction ID is rippled's SHA-256 over the transaction's
canonical single-signing form. That is not the signing pre-image and not the
signed blob, and an implementation that gets it subtly wrong produces a
value that *looks* authoritative and is wrong.

A hash in an audit log that looks right but is not is worse than no hash. The
signing service returns the signed blob and no hash at all; the submitter
records the ledger's value.

---

## 8. The policy lives in the service; the signer does not know what an obligation is

**Chosen.** The signing service has no policy engine, no tenant
configuration, and no concept of an obligation. It signs transactions, and it
refuses to sign for an account it holds no key for.

**Why.** If the signer enforced policy, it would need to know about
obligations, tenants, limits, and authorisations — and it would be a second
implementation of the rules, which is a second thing to get wrong and a
second thing to keep in sync.

**Why the refusal for an unknown account, then?** Because a signer that signs
for anything is a signing oracle. That refusal is about the *signer's own*
authority — which keys it holds — not about treasury policy.

---

## 9. The sample surface is separate and does not move money

**Chosen.** 16 endpoints under `/api/sample/`, namespaced. Twelve are
`GET` and four are `POST`; none of them submits a transaction except
`/api/sample/settle`, which requires `"confirm": true`. The settlement check is behind an explicit
`confirm: true`.

**Why namespaced?** So that "is this endpoint safe to hit?" has one answer:
everything under `/api/sample/` is exploratory, and the operational endpoints
are everywhere else. A reviewer can check that claim in one grep.

**Why the settlement path exists at all if it moves money?** Because "it
works" is not the same as "it works in this configuration". The settlement
check is how the domain gate, the trust line, the rate conversion, and the
external signer are all exercised together against a real ledger. It is
opt-in, and the non-submitting harnesses never trigger it.

---

## 10. The service signs through the configured signer, always

**Chosen.** The sample endpoints use `service.signer_for(tenant_id)` — the
service's own signer, with whatever provider it is configured with.

**Why.** They used to build a fresh `TreasurySigner(None, engine)` — no
provider. And the settle endpoint read the devnet seed directly and signed
there.

Both meant the sample surface — the place where settlement is actually
checked — exercised a path that was *not* the one a deployment uses. Worse,
the direct signing happened **outside the policy gate**.

The symptom was subtle: the service banner said `signing via the external
signer`, the payment settled, and the signing service reported **zero
signatures**. Everything looked right except the one number that mattered.

**The rule this produced.** The path a demo exercises must be the path a
deployment runs. If a code path exists only for the demo, it is a path that
will rot.

---

## 11. Fail closed, everywhere

A pattern that runs through the whole design:

| When | Then |
|---|---|
| The ledger cannot be read | the plan is withheld, `blocked=True`, no transfers |
| The ERP cannot be read | `reconciliation: null` with a stated `obligation_error` |
| A balance cannot be read | `last_balance_error` is recorded — not silently zero |
| A route has no declared scope | 403 |
| The token is wrong | 401 |
| The signer holds no key for the account | 403 |
| The policy refuses the transfer | refused, with a named reason |

The one that matters most is the third. A balance that fails to read is
**not** a balance of zero, and treating it as zero is the kind of bug that
stops the service from making a payment it should have made, or worse, makes
it believe a treasury is empty.

---

## 12. The rate source is pluggable, with a static fallback

**Chosen.** `FrankfurterProvider` (live), `ExchangeRateApiProvider` (live),
`StaticRateProvider` (fallback), behind a `RateService` with a cache and a
refresh.

**Why a static fallback?** Because a rate provider being down should not stop
the service from *reporting* a position. It should, however, be visible. The
service refreshes rates on `/api/health` so the cache is never stale at the
moment someone looks at it.

**The risk, stated.** A fallback rate is a *stale* rate. A payment sized
against a stale rate can breach a limit. The mitigation is that the fallback
is visible in the response rather than silent — but a production version
should refuse to *size* a payment against a fallback rate and only allow it
for reporting.

---

## 13. Multi-currency is issued-currency on the XRPL

**Chosen.** RLUSD, CAD, and MXN as issued currencies on the devnet, with
trust lines, rippling enabled at the issuer, and `send_max` always set.

**The trap.** Every issued-currency payment is a *cross-issue* payment from
the treasury's point of view, because the treasury holds RLUSD and pays CAD.
An XRPL `Payment` without `send_max` is invalid in that case, and the
validation message is not obvious. Every issued-currency payment here sets
`send_max`.

**The second trap.** The issuer's `lsfDefaultRipple` must be set **before**
any trust line to that issuer is created. A trust line created earlier
carries a stale issuer-side no-ripple bit that **cannot be repaired in
place** — the fix is to create the trust line after the flag is set. This
cost a real debugging cycle and is now the documented seed order:

```
enable_issuer_rippling.py → issue_currencies.py → fund_treasury.py → prepare_beneficiaries.py
```

---

## 14. What was considered and rejected

| Considered | Rejected because |
|---|---|
| Keep the key in the service | makes every other control a promise rather than a barrier |
| An application-level tenant check | a bug or an injection skips it; the ledger does not |
| One domain for all tenants | a tenant-mixing bug becomes a real breach |
| Vault (XLS-65) | `SingleAssetVault` is vetoed on the node |
| Batch transfers | `BatchV1_1` is vetoed; and it was only wanted for onboarding |
| Computing the transaction hash locally | an subtly-wrong hash in an audit log is worse than none |
| A decorator-based scope per route | forgetting a decorator would open a route |
| A new audit log format | the existing hash-chained log was the right fit |
| Forecasting models | a buffer and a due date are what the obligation actually needs |
| An MCP surface | deferred; there is no consumer yet |
