# Contributing

## Getting set up

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

## The environment

The service needs a local XRPL node. `dev_env.py` builds one:

```bash
python3 dev_env.py fresh          # empty ledger -> a working environment
python3 dev_env.py up             # bring it up without wiping
python3 dev_env.py down           # stop everything
python3 dev_env.py status         # what is running
```

It needs Docker. The node is `rippleci/xrpld:3.4.1` on network 15791.

## Before you open a pull request

```bash
pytest tests/ -q                              # 187 passed (177 + 10 skipped without a devnet)
python3 dev_env.py verify-all --allow-move    # 4/4 layers
python3 build_release.py                      # clean, no credentials
```

**Re-measure the test counts. Do not remember them.** The PR template
carries the current numbers; if your change moves them, update the
template too.

## Rules that are not negotiable

- **The tenant boundary.** Every route that names a tenant resolves that
  tenant before it does anything else. A route that answers for a tenant
  that does not exist is a data leak.
- **The policy gate.** Nothing is signed without the policy clearing it
  and the required authorisations being recorded.
- **The domain.** Every payment carries the `DomainID` from the tenant's
  configuration. It is never taken from a request.
- **Fail closed.** An unreadable ledger or ERP withholds the plan and
  states the reason. It is never presented as an empty plan.
- **No secrets.** No seeds, keys, tokens, or private addresses in the
  source, the docs, the tests, or the diff. `build_release.py` scans the
  staged package and refuses to write one that contains a credential.

## Tests

- `tests/test_policy.py` — the tiers, the limits, the USD normalisation
- `tests/test_currency.py` — issued assets, amounts, the scientific-notation balances
- `tests/test_executor_signer.py` — building and signing
- `tests/test_external_signer.py` — the external signing path, including that the signature is real
- `tests/test_auth.py` — credentials, scopes, the tenant restriction
- `tests/test_tenant_boundary.py` — the tenant boundary at the API edge
- `tests/test_reviewer_findings.py` — regressions for the defects a second reviewer found
- `tests/test_log_rates_connectors.py` — the decision log, the rates, the connectors
- `tests/test_deployment.py` — the deployment surface

## Style

- SPDX headers on new files:
  `# SPDX-FileCopyrightText: 2026 Justin Douglas` and
  `SPDX-License-Identifier: MIT`

  Put the header on its own line at the top of the file, with no
  trailing punctuation after the license identifier.
- Line length 88, `black` formatting, `isort` with the black profile.
- Docstrings explain **why**, not what. The code already says what.

## License

Contributions are accepted under the MIT license. See `LICENSES/MIT.txt`.
