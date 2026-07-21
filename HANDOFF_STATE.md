# Handoff state

Updated: `2026-07-21`

## Read first

1. `README.md`
2. `IMPLEMENTATION_STATUS.md`
3. `docs/deployment/copy-trading-vps.md`
4. `docs/architecture/copy-trading-testnet.md`
5. `docs/architecture/copy-trading-failure-matrix.md`
6. `chat/CONTINUE_WITH_ANOTHER_AI.md`

## Repository purpose

This repository contains a reusable strategy-free execution framework plus an active Binance
lead-trader copy system. The copy system treats public lead-trader operations as the external
signal source; it does not restore or depend on the removed V4/V5 built-in strategy campaign.

The production boundary remains locked. Do not weaken environment binding, order idempotency,
history-gap detection, position attribution, two-step Telegram controls or production activation
checks merely to make a failed operation continue.

## Runtime model

- `aiq-copy-poller.service`: 30-second public history polling and Testnet execution.
- `aiq-copy-telegram.service`: authorized Telegram dashboard and notifications.
- `aiq-copy-watchdog.timer`: deterministic 30-minute health and reconciliation checks.
- `aiq-copy-codex-audit.timer`: hourly sanitized Codex review.
- short-term and long-term selector timers: daily and weekly leader selection.
- backup and incident replay timers: verified database backup and out-of-band reporting.

The exact startup order, secret files and acceptance checks are documented in
`docs/deployment/copy-trading-vps.md`.

## Non-negotiable data rules

- Each leader owns an independent virtual ledger even when exchange positions aggregate.
- Existing source history is baselined, never replayed as new trades.
- An uncertain submission is reconciled by the original client order ID, never blindly duplicated.
- Reductions affect only the corresponding leader-owned quantity.
- Account, line, leader and position PnL raw events remain append-only; presentation resets append a
  new baseline instead of deleting history.
- Runtime credentials, database dumps, logs, browser state, Codex auth and account evidence remain
  outside Git.

## Verification

```bash
uv run ruff check src tests tools scripts migrations
uv run mypy src
uv run pytest -q
```

On a deployed VPS also verify systemd active/enabled state, the current Alembic head, recent poll
success, Telegram outbox delivery and a fresh `HEALTHY` watchdog run.
