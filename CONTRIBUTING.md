# Contributing

## Setup

```bash
uv sync
make check
```

## Requirements for every change

- `make check` passes: ruff, black, mypy `--strict`, bandit, contract drift,
  tests with at least 90% branch coverage.
- New behaviour has tests. Security controls need adversarial cases in
  `tests/security`; dependency failure modes need cases in `tests/chaos`.
- Integrity rules stay pure functions in `src/lab_result_integrity/domain/rules.py`
  so they remain deterministic and replayable from an evidence packet.
- Thresholds never go into code. Reference intervals, critical limits, delta
  limits, HIL thresholds, combination rules and PBRTQC parameters belong in the
  analyte catalogue with a citation, and catalogue changes follow
  [docs/runbooks/catalogue-change.md](docs/runbooks/catalogue-change.md).
- Nothing may give an agent the ability to release, verify or autoverify a
  result. The start-up policy self-test and `tests/security` enforce this;
  do not weaken either.
- Public API changes are recorded in `CHANGELOG.md` and the published
  contracts regenerated with `uv run python -m lab_result_integrity.contracts`.
- Architecturally significant decisions get an ADR in `docs/architecture/adr/`.
- No credentials, patient data or PHI in code, tests or fixtures. Synthetic
  data only.

## Commit messages

Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
`chore:`). Breaking changes use `!` and a `BREAKING CHANGE:` footer.

## Review

Changes to `src/lab_result_integrity/security`, `policies/`, `configs/analytes.yaml`
or `deployments/` need approval from a code owner (see `CODEOWNERS`).
Catalogue changes additionally need laboratory director sign-off recorded in
the catalogue's `governance` block.
