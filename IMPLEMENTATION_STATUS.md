# Implementation status

Updated: `2026-07-21`

Overall state: `COPY_TESTNET_IMPLEMENTED / AUTOMATION_ENABLED / PRODUCTION_LOCKED`

## Current product

The repository retains the strategy-free trading framework and now includes a project-owned
copy-trading subsystem under `ai_quant.copy_trading`. The subsystem does not invent a market
strategy: its external decision source is the incremental public operation history of selected
Binance lead traders. It normalizes those operations into explicit increase/reduce signals and
executes them through a bounded Testnet adapter.

Implemented runtime components include:

- public Binance lead-trader discovery and 30-second incremental history polling;
- one long-term, two short-term and two owner-managed custom slots;
- deterministic candidate filtering plus structured Codex selection review;
- isolated per-leader virtual position, order, multiplier and PnL attribution;
- protected-limit entries, market reductions and idempotent exchange reconciliation;
- a shared 150-USDT logical envelope with reserve, order and symbol margin limits;
- account, line, leader and position PnL baselines and Telegram presentation;
- Telegram operations with authorization and two-step confirmation;
- PostgreSQL migrations, append-only business events and a transactional outbox;
- systemd startup, daily/weekly selection, watchdog, Codex audit/repair, backup and incident replay.

## Deployment boundary

The checked-in default deployment is Binance USD-M Futures Testnet. Production code paths and
separate production deployment units exist, but production remains locked behind a dedicated
database, credentials, activation document, environment binding and clean-account checks. A
Testnet database must never be reused for production.

All systemd units use `/root/quantify/ai-quant-system`. Runtime credentials are materialized from
root-only files outside the repository into `/run/ai-quant-secrets`. Database volumes, backups,
Codex authentication and runtime evidence also remain outside Git.

## Verification

The current source snapshot passes 609 pytest tests on Debian 12/aarch64, together with focused
Ruff validation. Before deployment or upgrade, run:

```bash
uv sync --frozen --all-groups
uv run ruff check src tests tools scripts migrations
uv run mypy src
uv run pytest -q
```

Database migrations must report the single current Alembic head before services start. Live
Testnet acceptance additionally requires successful leader polls, a healthy watchdog, Telegram
delivery and explicit operator confirmation before new entries are allowed.

## Documentation

- Product overview: `README.md`
- Complete Testnet deployment: `docs/deployment/copy-trading-vps.md`
- Copy-trading behavior: `docs/architecture/copy-trading-testnet.md`
- Production cutover boundary: `docs/deployment/copy-production-cutover.md`
- Failure matrix: `docs/architecture/copy-trading-failure-matrix.md`

Passing offline tests is not evidence that production trading is approved or safe.
