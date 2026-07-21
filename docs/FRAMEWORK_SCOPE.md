# Strategy-free framework scope

## Purpose

Provide reusable, tested trading infrastructure and a strategy-agnostic automatic execution engine.
The repository now also contains a project-owned copy-trading consumer. That consumer follows
explicit public lead-trader operation deltas; it does not add a built-in predictive strategy to the
base framework.

## Authority boundary

The retained framework may ingest data, calculate features, validate risk, model/order intents,
automatically process complete intents, simulate or submit through an injected execution adapter,
reconcile exchange responses, plan native protection and observe Testnet account events. It must not
decide a symbol, direction or entry time without a new project-owned decision module.

## Retained extension points

- Market input: `ai_quant.market_data`, `ai_quant.orderbook`, `ai_quant.archive`.
- Optional observations: `ai_quant.features`. These have no order authority.
- Cost/risk: `ai_quant.cost`, `ai_quant.risk`, `ai_quant.rate_budget`.
- Execution primitives: `ai_quant.execution`, `ai_quant.binance_egress.testnet_probe`.
- Automatic orchestration: `ai_quant.automation`; it consumes decisions but never creates them.
- Operations: `ai_quant.control`, `ai_quant.notifications`, `ai_quant.monitoring`,
  `ai_quant.backup`, `ai_quant.validation`.
- New decision code: the intentionally empty `ai_quant.strategy` package or, preferably, a separate
  new project package with an explicit dependency on this framework.

## Removed authority

The repository has no old V4/V5 predictive strategy campaign, result reviewer, replay sweep or
executable strategy core. The generic automatic engine remains reusable. The separate
`ai_quant.copy_trading` project is an explicit decision consumer with its own Testnet executor,
Telegram dashboard, deployment units and tests; it must not be described as part of the empty
`ai_quant.strategy` extension package.

## Adding a new project

1. Define immutable input/output contracts for decisions.
2. Keep proposal/decision authority separate from exchange submission.
3. Add deterministic risk and net-cost rejection before any execution adapter.
4. Connect decisions to `ai_quant.automation.AutomaticTradeEngine` through explicit gate and
   protected-executor adapters.
5. Prove Paper behavior, then bounded Testnet behavior, before adding a disabled service unit.
6. Require explicit owner review before enabling unattended Testnet operation.
7. Keep production locked until independent production gates are complete.
