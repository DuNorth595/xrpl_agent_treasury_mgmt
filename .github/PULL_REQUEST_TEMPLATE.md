## What this changes

## Checklist

- [ ] `pytest tests/ -q` — 187 passed (177 + 10 skipped without a devnet)
- [ ] `python3 dev_env.py verify-all --allow-move` — 4/4 layers
- [ ] `python3 build_release.py` — clean, no credential-shaped strings
- [ ] README / docs examples updated if any behaviour changed
- [ ] README quick-start still runs end-to-end
- [ ] Test counts re-measured, not remembered
- [ ] New files carry an SPDX header
- [ ] No seeds, keys, tokens, or private addresses in the diff
- [ ] The tenant boundary still refuses an unknown tenant

## Security impact

<!-- Does this touch signing, the policy, the tenant boundary, or the ledger? -->
