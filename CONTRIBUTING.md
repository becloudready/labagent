# Contributing

- Fork, branch, open a PR. CI runs unit tests against mocked AWS (`moto`). It has no secrets and never touches real AWS.
- Add a deleter: implement it in `labagent/deleters.py` (idempotent, returns `Result`), register it in `REGISTRY` with an order,
  add an ARN parse case in `labagent/discovery.py` and a moto test.
- Do not add organisation-specific values (account IDs, names, domains) to code or examples. Use `example.com` and `123456789012`.
- Run `python scripts/check_no_pii.py` and `pytest` before pushing.
- Changes under `labagent/`, `scripts/` and `.github/` need a maintainer review (see CODEOWNERS).

## Releasing (maintainers)

1. Bump `__version__` in `labagent/__init__.py` and update the changelog notes in the PR.
2. Merge, then tag `vX.Y.Z` on `main`. The `release` workflow builds, runs `twine check --strict`, verifies the tag matches
   the version and publishes to PyPI via Trusted Publishing (no tokens). Use the manual run with `testpypi` first for a dry run.
3. One-time: on pypi.org add a (pending) publisher for `becloudready/labagent`, workflow `release.yml`, environment `pypi`.
