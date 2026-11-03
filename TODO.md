# TODO — xrpl_agent_treasury_mgmt

**Rule:** every item says what "done" means and how it is proved. An item
without a proof is not done.

---

## NEXT — point the signer at a real custodian

The external signing path is **proved**: the treasury service holds no key,
sends the transaction to a separate signing service, receives a signature, and
the payment settles on the devnet — confirmed in a closed ledger, with the
signer's own audit record.

What remains is pointing it at a real custody service instead of the local
stand-in.

**What "done" looks like.**

1. The `Signer` protocol is implemented against a real custody interface. The
   XRPL Agent Wallet's external-signer path is the obvious candidate — its
   mechanism has already been proven elsewhere in this project family.
2. The signing service's contract is documented as a contract, not as a
   description of one implementation. The local stand-in and the custodian both
   satisfy it.
3. The treasury service's signer is configured by URL and credential, and
   nothing about the service changes when the endpoint changes.

**How it is proved.** A transaction signed by the custody service, confirmed in
a closed ledger.

**What is already done, so this is smaller than it sounds.**

- The `Signer` protocol exists, with a local-seed implementation and an
  external-service implementation.
- The external implementation is exercised: a separate process holds the key,
  the treasury service has none, the signature comes back over HTTP, and the
  payment settles.
- The local stand-in (`local_signer_service.py`) is a real HTTP service with
  its own bearer token, its own audit log, and the same refusals a custody
  service would make — a bad token, an account it holds no key for.
- The returned signature is **verified against the transaction that was
  requested**. A signer that returned a signature over a different destination
  would be rejected.
- The settlement path signs through the service's signer, so the sample surface
  exercises the path a deployment uses.

**Watch for.** The custodian becomes the thing that must be authorised. Scope
enforcement does not travel with the transaction automatically — a dry run
decided under one credential must not be submittable under another without
re-checking.

---

## The security gaps

These are the reasons this is not production-ready. They are listed in
[SECURITY.md](SECURITY.md) as well.

### TLS

The service speaks plain HTTP. It must sit behind a TLS terminator, or
terminate TLS itself.

**Done when:** a deployment terminates TLS and the bearer token never crosses
an untrusted network. **Proved by:** a request over TLS succeeds and the token
is not visible on the wire.

### Token expiry and rotation

A token is valid until revoked, and revocation is in-process. There is no
expiry.

**Done when:** a credential carries an expiry, the service refuses an expired
one, and there is a rotation path that does not require a restart.
**Proved by:** a test that mints a token with a past expiry and is refused.

### Rate limiting

A caller with a valid token can make as many requests as they like.

**Done when:** the service, or something in front of it, limits request rate
per credential. **Proved by:** a test that exceeds the limit and is refused.

### Request signing

The bearer token alone authenticates a request. There is a replay window.

**Done when:** a request is bound to its body, or the replay window is
documented and accepted. **Proved by:** a replayed request is refused.

---

## The hardening items

### A production WSGI server as the default

`waitress` is already the default in `run_demo.py`, and `--server flask` is
available for local poking. What is missing is a `main()` the console script
could point at, so the service could be started by a supervisor without the
demo entry point.

**Done when:** `xrpl_agent_treasury_mgmt.service:main` exists and starts the
service. **Proved by:** running it and hitting `/api/health`.

### The decision log at scale

The log is a single file. More than one worker will contend on it.

**Done when:** the log can be written to a durable store, or the service
documents that it is single-instance only.

### A CI job for the ledger-dependent tests

The unit suite is ledger-free and runs on a GitHub runner. The API harness,
the verification harness, and the scale harness need a node.

**Done when:** a CI job stands up a local node and runs the other three
layers. **Proved by:** the job is green on a pull request.

### Rate-limited reconciliation

The reconciler reads the whole account history on every plan. That is fine
for a devnet and a small treasury, and it will not scale to a long history.

**Done when:** the reconciler reads an incremental window, or the history is
bounded by an explicit range.

---

## The things deliberately not being done

These are vetoed or out of scope. They are recorded so the reasoning is not
re-litigated.

- **Vault (XLS-65).** `SingleAssetVault` is vetoed on the node.
- **Batch transfers.** `BatchV1_1` is vetoed, and it was only ever wanted for
  atomic credential onboarding, which is two steps here.
- **External payments by the agent.** The agent pre-funds internal accounts.
  Making the external payment is a separate actor's job, under a separately
  credentialed authorisation.
- **Forecasting models.** A buffer and a due date, not a prediction.
- **An MCP surface.** Deferred until there is a consumer.
