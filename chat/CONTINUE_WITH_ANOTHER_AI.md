# Prompt for another AI/Codex

```text
Continue maintenance of this Binance lead-trader copy system.

Before editing:
1. Detect the repository root and inspect git status/history/remotes.
2. Read README.md, IMPLEMENTATION_STATUS.md, HANDOFF_STATE.md,
   docs/architecture/copy-trading-testnet.md,
   docs/architecture/copy-trading-failure-matrix.md and the relevant code/tests.
3. Preserve unrelated owner changes in a dirty worktree.
4. Verify every change with focused tests and then the complete suite when practical.

Current architecture:
- Debian 12 Bookworm/aarch64 and /root/quantify/ai-quant-system are the tested deployment.
- Binance public lead-trader operations are the external signal source.
- Binance USD-M Futures Testnet is the default execution environment.
- One long-term, two short-term and two manual custom slots are supported.
- Every leader has an independent virtual ledger, PnL attribution and follow multiplier.
- The exchange may aggregate same-symbol/same-side quantities, but leader ownership must not mix.
- Entries use source-price protected limits; reductions use market orders.
- PostgreSQL business facts are append-only and orders are idempotent.
- Telegram writes require an authorized user and two-step confirmation.
- Watchdog, Codex audit/repair, backup and incident reporting are systemd-managed.
- Production remains locked and uses a separate database, credentials and activation gate.

Do not:
- replay pre-baseline source history as new trades;
- blindly resubmit an uncertain order;
- mutate historical events to repair a display;
- merge one leader's position ownership into another leader;
- bypass history-gap, environment, authorization or production activation checks;
- commit credentials, database dumps, logs, /run secrets, /root/aiq-user-inputs,
  /root/.codex or runtime evidence.

Validation:
uv run ruff check src tests tools scripts migrations
uv run mypy src
uv run pytest -q
```
