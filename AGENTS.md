# Codex repository guidance

## Read first

Before changing or deploying this repository, read these files completely:

1. `README.md`
2. `docs/PROJECT_OVERVIEW.md`
3. `docs/deployment/codex-vps-handoff.md`
4. `docs/deployment/copy-trading-vps.md`
5. `docs/architecture/copy-trading-testnet.md`
6. `docs/architecture/copy-trading-failure-matrix.md`

## Project boundary

This repository is the source of truth for the deployed Binance lead-trader copy system. The
default environment is Binance USD-M Futures Testnet. Public lead-trader operations are external
signals; the system does not contain a built-in trading strategy.

Do not enable production by changing a URL, reusing the Testnet database, or weakening the
production activation gate. Production requires the dedicated procedure in
`docs/deployment/copy-production-cutover.md`.

## Non-negotiable invariants

- Preserve the independent virtual ledger for every leader, even when Binance aggregates matching
  symbol/side exposure.
- Preserve signal idempotency, client-order-ID reconciliation, history-gap detection and the
  initial history baseline. Never blindly replay old source operations.
- Never infer a missing reduction, position direction or exchange result.
- Never duplicate an order whose submission result is uncertain. Reconcile it by the original
  client order ID.
- Keep Telegram mutations authorized and destructive actions protected by confirmation.
- Keep Codex audit/repair unable to read exchange keys, Telegram tokens or production secrets.
- Keep runtime secrets, Codex authentication, PostgreSQL data, backups, browser profiles, logs and
  evidence outside Git.

## Supported deployment

- Verified host: Debian 12 Bookworm on aarch64.
- Fixed application path: `/root/quantify/ai-quant-system`.
- Python: `3.12.x`, installed from the committed `uv.lock`.
- Runtime supervision: systemd; database: the pinned PostgreSQL/TimescaleDB Compose service.
- Trading mode after a fresh install: Testnet and observe-only until the authorized Telegram user
  explicitly restores new entries.

Do not copy `/root/aiq-user-inputs`, `/run/ai-quant-secrets`, `/root/.codex`,
`/var/lib/ai-quant`, Docker volumes or a previous VPS database into Git.

## Working rules

- Inspect the current dirty worktree before editing and preserve unrelated operator changes.
- Use migrations for database shape changes. Never edit an applied migration.
- Update the example configuration, JSON Schema, systemd units and docs together when a runtime
  policy changes.
- Use reason codes internally and clear Chinese reason text in Telegram user-facing notifications.
- Keep Testnet API effects out of ordinary unit tests.

## Required verification

For a normal code change, run:

```bash
uv sync --frozen --all-groups
uv run ruff check src tests tools scripts migrations
uv run mypy src
uv run pytest -q
uv run bandit -q -r src
uv run python scripts/validate/secret_scan.py
```

For migrations or deployment changes, additionally run the relevant targets:

```bash
make test-migrations
make validate-deployment
systemd-analyze verify deploy/systemd/*.service deploy/systemd/*.timer
```

On a deployed VPS, finish with the acceptance checks in
`docs/deployment/copy-trading-vps.md`. Passing offline tests does not authorize production trading.
