from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_quant.automation import (
    AutomaticTradeEngine,
    AutomaticTradeIntent,
    AutomaticTradeOutcome,
    AutomaticTradeRunner,
    AutomationEnvironment,
    AutomationLimits,
    AutomationSnapshot,
    ExecutionReceipt,
    GateDecision,
    IntentAction,
    TradeSide,
)


class AllowGate:
    def evaluate(self, intent: AutomaticTradeIntent, snapshot: AutomationSnapshot) -> GateDecision:
        return GateDecision(True)


class RecordingExecutor:
    def __init__(self) -> None:
        self.ids: list[str] = []

    def submit_with_native_protection(self, intent: AutomaticTradeIntent) -> ExecutionReceipt:
        self.ids.append(intent.intent_id)
        return ExecutionReceipt(True, f"order-{intent.intent_id}")


NOW = datetime(2026, 7, 16, 4, 0, tzinfo=UTC)


def intent(intent_id: str = "decision-1") -> AutomaticTradeIntent:
    return AutomaticTradeIntent(
        intent_id=intent_id,
        decision_version="new-project-v1",
        evidence_hash="a" * 64,
        created_at=NOW - timedelta(seconds=5),
        expires_at=NOW + timedelta(seconds=30),
        environment=AutomationEnvironment.TESTNET,
        symbol="BTCUSDT",
        action=IntentAction.OPEN,
        side=TradeSide.LONG,
        quantity=Decimal("0.001"),
        entry_assumption=Decimal("65000"),
        stop_trigger=Decimal("64800"),
        target_trigger=Decimal("65200"),
        gross_edge_bps=Decimal("20"),
    )


def snapshot(**changes: object) -> AutomationSnapshot:
    values: dict[str, object] = {
        "observed_at": NOW,
        "open_positions": 0,
        "daily_net_pnl": Decimal("0"),
    }
    values.update(changes)
    return AutomationSnapshot(**values)  # type: ignore[arg-type]


def test_approved_intent_is_submitted_with_native_protection() -> None:
    executor = RecordingExecutor()
    engine = AutomaticTradeEngine(gate=AllowGate(), executor=executor)

    outcomes = engine.process_cycle(intents=(intent(),), snapshot=snapshot(), now=NOW)

    assert outcomes[0].submitted
    assert outcomes[0].reference_id == "order-decision-1"
    assert executor.ids == ["decision-1"]


def test_duplicate_intent_is_never_submitted_twice() -> None:
    executor = RecordingExecutor()
    engine = AutomaticTradeEngine(gate=AllowGate(), executor=executor)
    engine.process_cycle(intents=(intent(),), snapshot=snapshot(), now=NOW)

    second = engine.process_cycle(intents=(intent(),), snapshot=snapshot(), now=NOW)

    assert not second[0].submitted
    assert second[0].reason_codes == ("AUTOMATION_INTENT_DUPLICATE",)
    assert executor.ids == ["decision-1"]


def test_stale_or_unprotected_intent_fails_closed() -> None:
    executor = RecordingExecutor()
    engine = AutomaticTradeEngine(gate=AllowGate(), executor=executor)
    invalid = replace(
        intent(),
        stop_trigger=Decimal("65100"),
        expires_at=NOW - timedelta(seconds=1),
    )

    outcome = engine.process_cycle(intents=(invalid,), snapshot=snapshot(), now=NOW)[0]

    assert not outcome.submitted
    assert "AUTOMATION_NATIVE_PROTECTION_INVALID" in outcome.reason_codes
    assert "AUTOMATION_INTENT_EXPIRED" in outcome.reason_codes
    assert not executor.ids


def test_emergency_and_position_limits_block_new_entries() -> None:
    executor = RecordingExecutor()
    engine = AutomaticTradeEngine(
        gate=AllowGate(),
        executor=executor,
        limits=AutomationLimits(maximum_parallel_positions=5),
    )

    emergency = engine.process_cycle(
        intents=(intent("emergency"),),
        snapshot=snapshot(emergency_stop=True),
        now=NOW,
    )[0]
    full = engine.process_cycle(
        intents=(intent("full"),),
        snapshot=snapshot(open_positions=5),
        now=NOW,
    )[0]

    assert emergency.reason_codes == ("AUTOMATION_EMERGENCY_STOP",)
    assert full.reason_codes == ("AUTOMATION_POSITION_LIMIT",)
    assert not executor.ids


def test_gate_denial_prevents_submission() -> None:
    class DenyGate:
        def evaluate(
            self, intent: AutomaticTradeIntent, snapshot: AutomationSnapshot
        ) -> GateDecision:
            return GateDecision(False, ("NET_EDGE_INSUFFICIENT",))

    executor = RecordingExecutor()
    engine = AutomaticTradeEngine(gate=DenyGate(), executor=executor)

    outcome = engine.process_cycle(intents=(intent(),), snapshot=snapshot(), now=NOW)[0]

    assert outcome.reason_codes == ("NET_EDGE_INSUFFICIENT",)
    assert not executor.ids


def test_runner_polls_decisions_and_records_outcomes() -> None:
    executor = RecordingExecutor()
    engine = AutomaticTradeEngine(gate=AllowGate(), executor=executor)

    class SnapshotSource:
        def capture(self, *, now: datetime) -> AutomationSnapshot:
            return snapshot(observed_at=now)

    class IntentSource:
        def fetch(
            self, *, snapshot: AutomationSnapshot, now: datetime
        ) -> tuple[AutomaticTradeIntent, ...]:
            return (intent(),)

    class Sink:
        outcomes: tuple[AutomaticTradeOutcome, ...] = ()

        def record(
            self,
            *,
            snapshot: AutomationSnapshot,
            outcomes: tuple[AutomaticTradeOutcome, ...],
            completed_at: datetime,
        ) -> None:
            self.outcomes = outcomes

    sink = Sink()
    runner = AutomaticTradeRunner(
        engine=engine,
        snapshot_source=SnapshotSource(),
        intent_source=IntentSource(),
        outcome_sink=sink,
    )

    outcomes = runner.run_cycle(now=NOW)

    assert outcomes[0].submitted
    assert sink.outcomes == outcomes


def test_runner_continues_until_shutdown_without_embedded_strategy() -> None:
    engine = AutomaticTradeEngine(gate=AllowGate(), executor=RecordingExecutor())

    class SnapshotSource:
        def capture(self, *, now: datetime) -> AutomationSnapshot:
            return snapshot(observed_at=now)

    class EmptyIntentSource:
        def fetch(
            self, *, snapshot: AutomationSnapshot, now: datetime
        ) -> tuple[AutomaticTradeIntent, ...]:
            return ()

    class Sink:
        cycles = 0

        def record(
            self,
            *,
            snapshot: AutomationSnapshot,
            outcomes: tuple[AutomaticTradeOutcome, ...],
            completed_at: datetime,
        ) -> None:
            self.cycles += 1

    sink = Sink()
    runner = AutomaticTradeRunner(
        engine=engine,
        snapshot_source=SnapshotSource(),
        intent_source=EmptyIntentSource(),
        outcome_sink=sink,
    )
    sleeps: list[float] = []

    runner.run_forever(
        interval_seconds=60,
        stop_requested=lambda: sink.cycles >= 2,
        sleep=sleeps.append,
        utc_now=lambda: NOW,
    )

    assert sink.cycles == 2
    assert sleeps == [60.0]
