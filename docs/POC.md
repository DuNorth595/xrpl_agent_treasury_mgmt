# The proof

What is demonstrated, how to reproduce it, and — for each result — what it
does and does not establish.

The last part matters most. A number without a boundary is a marketing
claim.

---

## How to reproduce it

```bash
git clone <this repository>
cd xrpl-treasury

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# From an empty ledger: build the environment, run every layer.
python3 dev_env.py fresh --allow-move
```

That rebuilds the ledger, issues the currencies, creates the domain,
enrols the members, funds the treasury, starts the services, and runs the
suite. From nothing to a green result in about 130 seconds.

```bash
python3 dev_env.py verify-all --allow-move
```

```
  PASS  unit tests                   187 passed
  PASS  the API harness              PASS - 38/38 checks
  PASS  the verification harness     17/17 assertions passed
  PASS  the scale harness            PASS - 4/4 phases (1 skipped)

  4/4 layers pass
```

> In a clean checkout, without a local devnet, `pytest tests/ -q` reports
> **177 passed, 10 skipped**. The ten are the external-signer tests,
> which need a devnet account to sign against.

```
```

Each layer is independent. Any one can fail alone, and each writes its own
artifacts.

---

## The five things this demonstrates

### 1. The domain gate is enforced by the ledger

The core claim: a payment cannot leave the tenant's perimeter, because the
ledger refuses it — not because the application checks it.

Four cases, each a real submitted transaction:

| # | Case | `DomainID` | Result |
|---|---|---|---|
| 1 | member → outsider | yes | **`tecNO_PERMISSION`** |
| 2 | member → member | yes | **`tesSUCCESS`** |
| 3 | outsider → member | yes | **`tecNO_PERMISSION`** |
| 4 | member → outsider | **no** | **`tesSUCCESS`** |

Case 1 is the guarantee. Case 3 proves the check is not one-sided — the
sender is checked too. Case 4 is the hole: **without `DomainID` the ledger
enforces nothing.** That is why the service sets it on every payment from
tenant configuration, with no code path that can produce a Payment without
it.

**How it is verified.** Behaviourally, not by reading a list of amendment
flags. The harness simulates a payment to a non-participant and requires the
ledger's own `tecNO_PERMISSION`. A flag says the feature is compiled in; a
refusal says it works.

**What it establishes.** The perimeter is real and the ledger enforces it.

**What it does not establish.** The gate constrains the *agent*. An account
the agent funds may move that value onward. It is not a claim that funds
cannot leave the tenant's domain.

The evidence package — signed transactions, raw ledger objects, logs, and a
machine-readable `SUMMARY.json` — is published separately, as evidence about
the XRPL rather than as part of this software.

### 2. The service holds no key, and the payment still settles

The signing boundary. The treasury service sends the transaction to a
separate signing service, receives a signature, and submits it.

```
engine_result : tesSUCCESS
tx_hash       : 601E097D93E17A8FDF7F8DB47005FECDCDE99A25A57F51775849420EF37AD769
validated     : True
inLedger      : 39
```

That hash is on the local devnet right now. You can look it up yourself:

```bash
# the tx command reads the ledger
curl -s -N -H "Content-Type: application/json" \
  -d '{"command":"tx","transaction":"601E097D93E17A8FDF7F8DB47005FECDCDE99A25A57F51775849420EF37AD769"}' \
  ws://127.0.0.1:6006
```

A devnet is rebuilt from scratch between runs, so a hash from an earlier run
may no longer resolve. That is the devnet being disposable, not the claim
being wrong — reproduce it with `python3 dev_env.py fresh` and your own hash
will be in the artifact under `api-test-results/logs/`.

The signing service's own audit record shows exactly what it signed. The
addresses are redacted here: the local devnet accounts file holds the
seeds for them, so publishing them would publish half of what is needed
to spend from those accounts.

```
2026-11-03T02:33:09Z  account r9NioetqX...  (the treasury)
    destination : rGTaeK3jW...  (a domain member)
    amount      : {'currency': '524C555344000000000000000000000000000000',
                   'issuer': 'r31Rt235Y4...',
                   'value': '1000000'}
    fee/seq     : 12 / 38
    blob sha256 : 90366e1aa93601a695ff65840b8cd9d4df69d72b...
```

The signing service refuses a bad bearer token (401), refuses to sign for
an account it holds no key for (403), and keeps an append-only audit log.
It has no policy engine and no idea what a treasury obligation is.

**How it is verified.** Ten tests in `tests/test_external_signer.py`, one of
which is *"the signature is real"* — the ledger must accept what the signer
produced.

**What it establishes.** A compromise of the treasury service process does
not, by itself, move money.

**What it does not establish.** The signing service signs whatever it is
given. If the treasury service is compromised *before* it asks for a
signature, the signature will be granted. The boundary raises the cost of a
compromise; it does not make the policy unnecessary.

### 3. The policy holds at scale

1,000,000 instructions, planned, evaluated, authorised, built and signed.

| Metric | Value |
|---|---|
| Instructions | 1,000,000 |
| Elapsed | 263.1 s |
| Throughput | **3,801 instructions/sec** |
| Mean latency | 263.0 µs |
| Median latency | 256.3 µs |
| p99 latency | 331.5 µs |
| Peak RSS | **97 MB** |
| RSS growth | 0.0 MB |

And the two numbers that matter more than the throughput:

| Check | Result |
|---|---|
| Instructions missing a `DomainID` | **0** |
| Instructions outside the policy envelope | **0** |

A million instructions, and not one of them skipped the perimeter or the
policy. The envelope check is what makes the throughput number meaningful —
a fast system that skips its checks is not fast, it is wrong.

**What it establishes.** The service-side path — planning, policy
evaluation, authorisation, transaction building, signing — scales to a
million instructions with a flat memory profile and no policy escape.

**What it does not establish.** This is *not* a claim about settlement
rate. One transaction settles per closed ledger, and the local devnet
closes a ledger about every three seconds, so settlement caps near **0.3
transactions/second**. Real throughput is bounded by the ledger, not by the
service. The two numbers are reported separately throughout, because
combining them would be the easy way to make this look better than it is.

### 4. It fails closed

Not a feature that was added — a property that was tested.

| Failure | The behaviour |
|---|---|
| The ledger cannot be read | `blocked=True`; **every** transfer withheld; obligations marked `reconciliation_blocked` |
| The ERP cannot be read | `reconciliation: null` with a stated `obligation_error`; the plan is blocked |
| A balance cannot be read | `last_balance_error` is recorded — **not** silently treated as zero |
| The policy refuses a transfer | refused, with a named reason |
| The signer holds no key for the account | 403 |
| A route has no declared scope | 403 |

The blocked path was verified independently: a broken ledger produced a
blocked reconciliation, zero planned transfers, zero decisions, and no
crash. The ERP outage produced a blocked plan, zero transfers, an HTTP 200
with the reason stated, and normal planning resumed after restoration.

**Why the balance case is the one that matters.** A balance that fails to
read is not a balance of zero. Treating it as zero is the kind of bug that
stops a service from making a payment it should have made — or worse, makes
it believe a treasury is empty.

### 5. The decision log is tamper-evident

Every decision appends to a hash-chained log. Each entry carries the hash
of the entry before it, so an altered entry breaks the chain, and the log
reports that it no longer verifies.

The log is written to `var/decision-log.jsonl`, and the `/api/sample/decision-log`
endpoint reports the chain's state.

---

## The four layers

| Layer | What it covers | Result |
|---|---|---|
| Unit tests | the pure logic — policy, currency, planner, signer, log, rates, auth, external signing, tenant boundary, reviewer findings | **187 passed** (177 + 10 skipped without a devnet) |
| The API harness | the HTTP contract against a live service | **38/38 checks** |
| The verification harness | the service end to end, against a live ledger | **17/17 assertions** |
| The scale harness | throughput, correctness, stability, deployment readiness | **4/4 phases**, plus a settlement phase that is skipped unless `--settle N` is given |

The scale harness's phases are: service throughput, ledger settlement, scale
correctness, stability, and deployment readiness.

**The settlement phase is skipped by default** and is reported as skipped, not
as passed. It submits real transactions and confirms them in a closed ledger,
so it is opt-in: `--settle 3`. The phase count says so rather than counting a
phase that never ran.

**What the unit tests cannot show.** They do not touch a ledger. They prove
the logic; the harnesses prove the wiring.

---

## The defects the harnesses found

This is the part that says the harnesses are worth running. Every one of
these was found by the suite, not by reading the code.

| The defect | What it would have caused |
|---|---|
| The approval ledger keyed on an empty string | one sign-off could authorise an **unrelated** transfer |
| The sample endpoints built their own signer | the sample surface — where settlement is checked — exercised a path a deployment never runs |
| The settle endpoint signed from a seed directly | signing happened **outside the policy gate** |
| The client and the signer service disagreed on the wire format | the external signing path could never have worked |
| The minimum-balance floor compared an XRP limit against an RLUSD balance | **every** transfer on a well-funded treasury was refused |
| Limits compared in native currency against a USD cap | 4,000 CAD instructions passed under a USD limit |
| `send_max` missing on issued-currency payments | **every** issued-currency payment was invalid |
| `meta.engine_result` instead of `meta.TransactionResult` | the reconciliation read the wrong field |
| A balance exception swallowed and rendered as zero | a treasury with an unreadable balance looked empty |
| `int("1000000000000000e-4")` | an ordinary XRPL balance raised `ValueError` |
| The issuer's rippling flag set after the trust lines | a stale issuer-side no-ripple bit that **cannot** be repaired in place |
| A stale hardcoded service address | the scale harness could not find the service |
| The domain gate checked by amendment flag | a flag says the feature compiled in, not that it works |
| Approval-walkthrough keys colliding within a second | two runs could authorise each other |

The last one deserves a note: **the approval ledger keyed on an empty string
was found by the unit tests**, and it would have meant a single sign-off
could authorise any transfer on the tenant. That is the kind of defect that
survives a code review and reaches production.

### The ones a second reviewer found

Every one of the four layers was green - and 156 tests were passing - when
an independent reviewer read the documentation against the source. It found
six things the suites could not see. All are fixed and covered by tests.

| The defect | What it would have caused |
|---|---|
| The dry-run endpoint referenced an undefined name | it answered **every** request with `{"error": "NameError(...)"}` and HTTP 200 - completely non-functional, and looking healthy |
| `mint_token.py --scopes` kept only the last value | asking for read+settle silently produced a **settle-only** token - more privilege than requested, with no warning |
| The ledger-failure path did not set `plan.blocked` | a caller checking the flag got `false` on a ledger outage, **indistinguishable from a normal plan** |
| The external signer did not verify what it returned | a compromised signer could return a signature over a **different destination**, and the service would submit it |
| `/obligations` never resolved the tenant | it answered for a tenant that did not exist, **with the real obligations** |
| The executor never advanced the sequence | two transactions from one account carried the same sequence; the second was rejected with `tefPAST_SEQ` |

The last one was found only because the reviewer insisted that the scale
harness's settlement phase actually **run** rather than being skipped and
counted as a pass.

**The dry-run one is the cautionary tale.** It returned HTTP 200 for every
request and the API harness's dry-run check passed anyway - because that
check only asserted that nothing was submitted, which was true. An endpoint
that answers every time looks like a working endpoint even when every answer
is an error.

### The second reviewer's own limitation

The first version of the signer-verification fix compared CamelCase field
names (`DomainID`). The xrpl-py models use snake_case (`domain_id`). So it
compared `None` with `None` and verified nothing at all - the check was
theatre, and it would have looked like a working security control.

The tests caught it. A verification that has never rejected anything is
not a verification; it needs a test that proves it rejects.

---

## Reproducing each piece individually

```bash
# The unit tests
PYTHONPATH=src python3 -m pytest tests -q

# The API contract
python3 dev_env.py up && python3 api_test.py

# The end-to-end verification
python3 verify.py --allow-move

# The million-instruction scale run
python3 scale_test.py --instructions 1000000

# The external signing path on its own
PYTHONPATH=src python3 -m pytest tests/test_external_signer.py -v
```

`verify.py` and the scale harness **do not move money** unless
`--allow-move` is given.

---

## What this is not

Stated plainly, because the gaps are the thing a reader most needs.

- **Not a settlement benchmark.** The million instructions are processed by
  the service. They are not a million on-chain transfers. The devnet
  settles about 0.3 transactions/second - one per closed ledger, and it
  closes a ledger roughly every three seconds - and a production ledger
  has its own rate.
- **Not a mainnet result.** Everything here is a local devnet at network
  15791. The mainnet amendment state was never verified from this machine.
- **Not a production deployment.** There is no TLS, no token expiry, no rate
  limiting, and the signing service is a local stand-in. The list is in
  [SECURITY.md](../SECURITY.md).
- **Not a claim of audit readiness.** The log is tamper-*evident*, not
  tamper-proof. It detects alteration; it does not prevent it.
- **Not a custody solution.** The included signer holds devnet keys in a
  file. A deployment points at a real custodian or KMS.
