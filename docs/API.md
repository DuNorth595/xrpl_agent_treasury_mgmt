# The API

Every operational endpoint needs a bearer token with the right scope. The
sample endpoints do not, unless they move money.

Base URL in development: `http://127.0.0.1:8095`

---

## Authentication

```
Authorization: Bearer <token>
```

Mint one:

```bash
python3 mint_token.py --name operator --scopes treasury:read
```

| Scope | Allows |
|---|---|
| `treasury:read` | the position, the plan, the obligations, the log, the authorisations |
| `treasury:propose` | a dry run |
| `treasury:authorise` | recording a sign-off |
| `treasury:settle` | submitting a real payment |
| `treasury:admin` | everything |

### Tenant scoping

A credential may also be restricted to particular tenants:

```bash
python3 mint_token.py --name acme-only --scopes treasury:read --tenants acme
```

The restriction is enforced — the middleware reads the tenant id from the
route and calls `require_tenant`. A credential with tenants listed is
refused for any tenant not on its list. A credential with no tenants listed
is unrestricted.

A scope says *what* a caller may do. The tenant restriction says *whose*
money they may do it to.

**An unknown route is refused.** The scope map is explicit, and a route that
is not in it gets a 403 rather than an open door. Adding an endpoint without
declaring what it needs produces a broken endpoint, not an unprotected one.

### Responses

| Status | When |
|---|---|
| `401` | no token, or the token is wrong |
| `403` | the token is valid but lacks the scope — or the route has no declared scope |
| `200` | the request succeeded |
| `503` | a dependency is unavailable — for example, the signing service |

---

## Operational endpoints

### `GET /api/health`

No scope. The service, the ledger, and the rates.

```bash
curl -s http://127.0.0.1:8095/api/health
```

The rates are refreshed on each call, so this never reports a stale or empty
cache.

---

### `GET /api/tenants`

`treasury:read`. The tenants, their limits, and their approval matrices.

---

### `GET /api/tenant/<tenant_id>/position`

`treasury:read`. The balances the treasury holds, by currency, and the
domain perimeter it is confined to.

---

### `GET /api/tenant/<tenant_id>/obligations`

`treasury:read`. What the ERP says is due, before any reconciliation.

---

### `GET /api/tenant/<tenant_id>/plan`

`treasury:read`. The reconciled, policy-checked plan: which obligations
become transfers, what tier each one is, and what was refused.

**This endpoint can be blocked.** If the ERP or the ledger cannot be read,
the response carries `reconciliation: null` or a blocked report, an
`obligation_error`, and the plan is empty with a stated reason. That is
deliberate — an empty plan and a blocked plan are different, and only one
of them means "nothing is due".

---

### `POST /api/tenant/<tenant_id>/dry-run`

`treasury:propose`. Evaluate one transfer with no side effect. Nothing is
signed and nothing is submitted; the verdict comes back from a ledger
simulation.

```bash
curl -s -X POST http://127.0.0.1:8095/api/tenant/acme/dry-run \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"amount": 100, "currency": "RLUSD", "destination": "rDestination..."}'
```

The response carries the built payment (with its `DomainID`), the policy
decision, and the simulated `engine_result`.

**This is a `POST`.** A `GET` on this path returns 405.

---

### `POST /api/tenant/<tenant_id>/authorise/<obligation_id>`

`treasury:authorise`. Records a sign-off against an obligation.

```bash
curl -s -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"approver": "j.douglas", "role": "treasury_manager", "comment": "reviewed against the contract"}' \
  http://127.0.0.1:8095/api/tenant/acme/authorise/OBL-1042
```

| Field | Required | What it is |
|---|---|---|
| `approver` | **yes** | who is signing off. A sign-off with no approver is refused with 400 — an unattributable sign-off is not a sign-off |
| `role` | no | defaults to `treasury_manager` |
| `comment` | no | what the approver checked |

A sign-off is bound to the obligation **and** the transfer. It cannot
authorise a different transfer.

The obligation must be in the current plan. If it is not, the endpoint
refuses with 404: the policy has no tier for it, so the required number
of sign-offs cannot be determined.

---

### `GET /api/tenant/<tenant_id>/authorisations`

`treasury:read`. Who has signed off, on what, and how many sign-offs each
obligation still needs.

---

### `GET /api/tenant/<tenant_id>/log`

`treasury:read`. The hash-chained decision log.

Each entry carries the hash of the entry before it, so an altered entry
breaks the chain. The response includes the chain's verification state.

---

### `GET /api/connectors`

No scope beyond a valid token. The state of the connectors — the ERP, the
rate provider, and the signing service.

---

## The sample endpoints

Under `/api/sample/`. Sixteen of them, and they exist so the behaviour can
be explored without setting up a real obligation first.

**None of them submit a transaction, except `settle`.**

Twelve are `GET`. Four are `POST` — they take a body, so a `GET` returns
`405`.

| Endpoint | Method | Does |
|---|---|---|
| `/api/sample/capabilities` | GET | what this service can do — the best one to start with |
| `/api/sample/domain` | GET | the domain perimeter, and who is in it |
| `/api/sample/policy-matrix` | GET | how the tiers resolve for a range of amounts |
| `/api/sample/cross-currency` | GET | the conversion, and the `send_max` that goes with it |
| `/api/sample/rates` | GET | the current rates and their source |
| `/api/sample/source` | GET | where the obligations came from |
| `/api/sample/decision-log` | GET | the decision log, and whether its chain verifies |
| `/api/sample/scenario/approved` | GET | a Level 1 transfer |
| `/api/sample/scenario/single` | GET | a Level 2 transfer, needing one sign-off |
| `/api/sample/scenario/dual` | GET | a Level 3 transfer, needing two |
| `/api/sample/scenario/refused` | GET | a transfer the policy refuses |
| `/api/sample/scenario/blocked` | GET | what a blocked reconciliation looks like |
| `/api/sample/evaluate` | **POST** | policy evaluation without any side effect |
| `/api/sample/simulate` | **POST** | a ledger simulation — no side effect |
| `/api/sample/approval-walkthrough` | **POST** | the authorisation flow, end to end |
| `/api/sample/settle` | **POST** | **submits a real payment** — see below |

The POST ones take a JSON body:

```bash
curl -s -X POST http://127.0.0.1:8095/api/sample/evaluate \
  -H "Content-Type: application/json" \
  -d '{"amount": 5000, "currency": "CAD", "beneficiary": "cost-centre-a"}'

curl -s -X POST http://127.0.0.1:8095/api/sample/simulate \
  -H "Content-Type: application/json" \
  -d '{"amount": 100, "currency": "RLUSD"}'

curl -s -X POST http://127.0.0.1:8095/api/sample/approval-walkthrough \
  -H "Content-Type: application/json" -d '{}'
```

### The one that moves money

`POST /api/sample/settle` submits a real transaction to the ledger. It
requires `treasury:settle`, and it requires `"confirm": true` in the body.

```bash
curl -s -X POST http://127.0.0.1:8095/api/sample/settle \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"amount": 1, "currency": "RLUSD", "confirm": true}'
```

```json
{
  "engine_result": "tesSUCCESS",
  "tx_hash": "...",
  "validated": true,
  "money_moved": true
}
```

**Without `"confirm": true` it does not submit.** It builds the transaction
and simulates it.

The endpoint signs through the service's configured signer — the same signer
a deployment uses. It does not reach for a seed.

---

## The dashboard

`GET /` — the treasury dashboard. It shows the positions, the plan, the
policy tiers, the approvals, and the domain boundary.

In sample mode it carries a banner saying the sample data is local
demonstration data, not a real treasury. That banner is not decoration; it
is there so a screenshot of the dashboard cannot be mistaken for a live
treasury position.

---

## Errors

```json
{"error": "forbidden", "detail": "the token lacks the scope treasury:settle"}
```

```json
{"error": "reconciliation_blocked", "detail": "the ledger could not be read"}
```

```json
{"error": "refused", "reason": "would_breach_minimum_treasury_balance"}
```

The refusal reasons are named, not generic:

The six refusal reasons, and what each one means:

| Reason | Means |
|---|---|
| `amount_must_be_positive` | the transfer was for nothing |
| `destination_is_not_registered` | the destination is not one of the tenant's cost centres |
| `destination_is_not_an_authorised_participant` | the destination is a known cost centre but not a member of the domain — the ledger would refuse it too |
| `exceeds_maximum_single_transfer` | over the per-transfer limit |
| `exceeds_maximum_daily_disbursement` | over the daily cap, counting what is already committed |
| `would_breach_minimum_treasury_balance` | the payment would take the fee balance below the floor |

These are refusals, not escalations: no amount of authorisation makes an
over-limit transfer compliant. A transfer that is inside the envelope but
warrants human judgement is escalated instead, and it carries an escalation
reason rather than a refusal reason — see
[docs/DESIGN.md](DESIGN.md) for the five.

---

## The full verification

`api_test.py` exercises the whole contract against a live service:

```bash
python3 api_test.py
```

It writes `api-test-results/API_TEST.md` and a machine-readable JSON
beside it.
