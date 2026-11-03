# Deployment

How to run this for real.

---

## Before you start

Three things have to be true before this service is pointed at money:

1. **The signing service is a real custodian or KMS.** Not
   `local_signer_service.py`. That one holds devnet keys in a local file; it
   is for development.
2. **TLS is terminated.** The bearer token is a plaintext credential. It
   must not cross an untrusted network.
3. **The ledger is one you have checked.** The `PermissionedDomains`,
   `Credentials`, and `fixCleanup3_4_0` amendments must be enabled. The
   perimeter does not exist without them.

---

## Configuration

Every setting is an environment variable. Nothing is read from a config
file, so nothing has to be protected as a file.

The names below are the ones the code reads. They were verified against the
source, not chosen here.

### Required

| Variable | Read by | What it is |
|---|---|---|
| `TREASURY_API_TOKENS` | the service | the credentials, as a JSON object |
| `TREASURY_SIGNER_URL` | the service | the signing service's `/sign` endpoint. **Unset means the local seed is used** — which is not a deployment |
| `TREASURY_SIGNER_TOKEN` | the service | the signing service's bearer token |

### The ledger and the ERP

These are currently **command-line arguments**, not environment variables.
They are read by `run_demo.py` and `dev_env.py`:

| Argument | Environment variable | Default | What it is |
|---|---|---|---|
| `--url` | `TREASURY_RIPPLE_WS` | `ws://127.0.0.1:6006` | the node, as a websocket URL |
| `--network-id` | — | `15791` | the network id. **Must match the node.** 15791 is the local devnet |
| `--erp-url` | `S_DEVLAB_ERP_URL` | — | the ERP obligations API. Empty uses the in-memory source |
| — | `S_DEVLAB_ERP_TOKEN` | `demo-token` | the ERP's bearer token |
| `--host` | `TREASURY_HOST` | `127.0.0.1` | the bind address |
| `--port` | `TREASURY_SERVICE_PORT` | `8095` | the port |
| — | `TREASURY_ERP_PORT` | `8096` | the reference ERP's port |

### The service's own options

| Argument | Default | What it is |
|---|---|---|
| `--server` | `waitress` | `waitress` or `flask`. `waitress` is the production one |
| `--threads` | `8` | the waitress worker thread count |
| `--max-body` | `1048576` | the maximum request body, in bytes |
| `--log` | `var/decision-log.jsonl` | the hash-chained decision log |
| `--no-sample` | off | do not register the `/api/sample/*` endpoints |

### The signer service's options

`local_signer_service.py` reads `SIGNER_SERVICE_TOKEN` (default
`local-signer-token`) and takes `--port`, `--token`, and `--log`.

### A note on the split

The ledger URL and the network id are arguments rather than environment
variables. That is an inconsistency, and it is a real one: a deployment
that sets `TREASURY_RIPPLE_WS` and expects the service to pick it up will
not get it. `dev_env.py` bridges the two for the local environment — it
reads the environment variables and passes them as arguments.

**Before a deployment, decide whether these become environment variables.**
As it stands, a deployment sets them in the supervisor's command line,
which means they appear in the process list. That is acceptable for a URL
and a network id, and it would not be acceptable for anything secret.

See `.env.example` for the local set.

---

## The credentials

Tokens are minted with `mint_token.py`. The plaintext is shown **once** and
is not recoverable — only its hash is stored.

```bash
python3 mint_token.py --name operator --scopes treasury:read,treasury:propose
```

The scopes are comma-separated, or `all`. The credential can also be
restricted to particular tenants with `--tenants acme,other`, and the
restriction is enforced by the service.

The result is a JSON object. Put it in the environment:

```bash
export TREASURY_API_TOKENS='{"operator": {"token": "...", "scopes": ["treasury:read"]}}'
```

`mint_token.py --env` prints the value for the whole store, and `--out` writes
it to a file with mode 0600.

**In a deployment, this comes from a secret store** — Vault, AWS Secrets
Manager, a Kubernetes secret, whatever you already use. It is a credential;
treat it as one. It is not written to disk by the service, and it is not
logged.

### Scopes, and why you want more than one

Give the operator who watches the dashboard `treasury:read` and nothing else.
Give the one who runs a dry run `treasury:propose`. Give `treasury:settle`
only to whoever may actually cause a payment.

The scope is what the *route* requires, not a description of the person.
A token with `treasury:settle` can settle. There is no second check inside
the settlement path beyond the policy and the authorisations.

---

## Running it

### The entry point

The service is started by `run_demo.py`. It builds the registry, the ledger
client, the obligation source, the connectors, the signer, and the app, then
serves it.

```bash
python3 run_demo.py \
  --host 127.0.0.1 --port 8095 \
  --url wss://your-node.example --network-id 1 \
  --erp-url https://erp.internal/obligations \
  --server waitress --threads 8
```

`waitress` is the default and is what a deployment should use. It is
threaded, it enforces a request body limit, and it does not announce itself
as a development server. `flask` is Werkzeug's development server and is
for local poking only.

The Flask app is also a WSGI application, so a different server can host it:

```python
# app.py
from xrpl_agent_treasury_mgmt.service import create_app
application = create_app(service, sample_mode=False)
```

But **`create_app` needs a fully built `TreasuryService`**, and that is
assembled inside `run_demo.py`. There is no module-level `main()` to point
gunicorn at — `xrpl_agent_treasury_mgmt.service:main` does not exist. If you
want gunicorn, factor the assembly out of `run_demo.py` first. That is a
small change and is on the list.

### Under systemd

```ini
[Unit]
Description=XRPL Treasury Service
After=network-online.target

[Service]
Type=simple
User=treasury
WorkingDirectory=/opt/xrpl-treasury
EnvironmentFile=/etc/xrpl-treasury/env
ExecStart=/opt/xrpl-treasury/.venv/bin/python /opt/xrpl-treasury/run_demo.py --host 127.0.0.1 --port 8095 --server waitress --threads 8 --url wss://your-node.example --network-id 1 --erp-url https://erp.internal/obligations
Restart=on-failure
RestartSec=5

# The service needs no write access outside its own directory, and
# definitely no shell.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/xrpl-treasury/var

[Install]
WantedBy=multi-user.target
```

The `EnvironmentFile` carries the three secrets — `TREASURY_API_TOKENS`,
`TREASURY_SIGNER_URL`, `TREASURY_SIGNER_TOKEN`. The URL and the network id
are on the command line, where they are visible in the process list. That is
acceptable for those two; it would not be for a token.

**`--no-sample` in a deployment.** It removes the `/api/sample/*` endpoints.
They are deliberately open, so a deployment should not have them at all.

### Behind a TLS terminator

The service speaks plain HTTP. Put a terminator in front of it.

```nginx
server {
    listen 443 ssl http2;
    server_name treasury.example;

    ssl_certificate     /etc/letsencrypt/live/treasury.example/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/treasury.example/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8095;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # The settlement endpoints can take a while while a transaction
        # is submitted and confirmed.
        proxy_read_timeout 120s;
    }
}
```

**Do not expose the service directly.** It has no rate limiting and no
token expiry. If it must be reachable from outside, the terminator is
where both of those get added.

---

## The signing service

The contract the treasury service expects:

### `POST /sign`

**Request**

```json
{
  "tx_blob": "<hex>",
  "account": "<classic address>"
}
```

**Headers**

```
Authorization: Bearer <token>
Content-Type: application/json
```

**Response**

```json
{
  "signed_tx": "<hex>",
  "account": "<classic address>"
}
```

**Refusals**

| Status | Meaning |
|---|---|
| `401` | the bearer token is missing or wrong |
| `403` | the service holds no key for that account |

The signed transaction comes back as a **hex blob**, not JSON. A signer that
returns JSON is also accepted by the client, but the blob is the contract.

The signing service must **not** return a transaction hash. The transaction
ID is rippled's SHA-256 over the canonical single-signing form, and a
signer that computes it will get it subtly wrong. The ledger's response is
authoritative.

### Audit

The signing service keeps an append-only log of what it signed: the
account, the destination, the amount, the fee, the sequence, and the digest
of the blob. Ship it somewhere durable — it is the record of what was
authorised to be signed.

---

## The decision log

Written to `var/decision-log.jsonl`. Each entry carries the hash of the
entry before it, so an altered entry breaks the chain.

Ship it somewhere durable and append-only. It is the audit trail, and the
point of a hash chain is that someone else holds a copy.

---

## Checklist

**Before the first real payment**

- [ ] TLS terminated in front of the service
- [ ] `TREASURY_API_TOKENS` from a secret store, not a file
- [ ] The signing service is a real custodian or KMS
- [ ] The signing service's audit log is shipped somewhere durable
- [ ] The decision log is shipped somewhere durable and append-only
- [ ] Rate limiting in front of the service
- [ ] The service binds to a private interface
- [ ] The ledger's amendment state verified: `PermissionedDomains`,
      `Credentials`, `fixCleanup3_4_0`
- [ ] The `--network-id` argument matches the node
- [ ] The tenant configuration's domain IDs are the real ones
- [ ] The limits and the approval matrix reviewed against the policy
- [ ] The ERP connector pointed at the real ERP and the reconciliation
      confirmed against known payments
- [ ] The rates provider pointed at a real source, and the fallback
      behaviour understood
- [ ] A dry run reviewed against a hand-computed expectation
- [ ] The alert on reconciliation failures configured

**Ongoing**

- [ ] Rotate the API tokens
- [ ] Rotate the signing service's token
- [ ] Review the decision log
- [ ] Watch the reconciliation failure rate
- [ ] Re-check the ledger's amendment state after an upgrade

---

## Operating it

### Health

```bash
curl -s https://treasury.example/api/health | jq
```

Reports the service, the ledger, and the rates. It refreshes the rates on
each call, so it never reports a stale or empty cache.

### When the plan is blocked

A blocked plan is not an error to be retried blindly. Look at
`obligation_error` and `last_balance_error` in the response. A blocked
reconciliation means the ledger or the ERP could not be read, and the
service has deliberately withheld every transfer.

### When a payment fails on-chain

`tecNO_PERMISSION` on a payment that should have succeeded almost always
means the destination is not a member of the tenant's domain, or the
`DomainID` is wrong. It is the perimeter working. Check the credential
membership for the destination account.

`tecPATH_DRY` means the issuer's rippling is disabled, or the trust line
carries a stale issuer-side no-ripple bit. The bit cannot be repaired in
place — the trust line has to be recreated after the issuer's
`lsfDefaultRipple` is set.

---

## Scaling

The service is stateless apart from the decision log, so it scales
horizontally. Two things to know:

- **Ledger settlement is the bottleneck, not the service.** The service
  processes ~3,800 instructions/sec; a devnet settles ~0.3 transactions/sec.
  Scale the service for the API load and the planning, and expect the
  settlement rate to be whatever the ledger gives you.
- **The decision log is a single file.** With more than one worker, they
  will contend. Ship it to a durable store rather than writing it locally
  when you run more than one instance.
