from __future__ import annotations

from datetime import timedelta

from ai_quant.validation.gates import GateObservation, evaluate_continuous_gate
from tests.market_fixtures import BASE_TIME


def observation(hours: int, **changes: object) -> GateObservation:
    values: dict[str, object] = {
        "observed_at": BASE_TIME + timedelta(hours=hours),
        "release_hash": "a" * 64,
        "runtime_state": "SHADOW",
        "open_p0_p1": 0,
        "order_discrepancies": 0,
        "duplicate_orders": 0,
        "unprotected_positions": 0,
    }
    values.update(changes)
    return GateObservation(**values)  # type: ignore[arg-type]


def test_72h_gate_passes_only_one_immutable_clean_release() -> None:
    result = evaluate_continuous_gate(
        [observation(hour) for hour in range(73)],
        required_duration=timedelta(hours=72),
        maximum_gap=timedelta(hours=1),
        allowed_runtime_states={"SHADOW", "TESTNET"},
    )
    assert result.passed


def test_release_change_and_protection_gap_restart_gate() -> None:
    observations = [observation(hour) for hour in range(73)]
    observations[10] = observation(10, release_hash="b" * 64, unprotected_positions=1)
    result = evaluate_continuous_gate(
        observations,
        required_duration=timedelta(hours=72),
        maximum_gap=timedelta(hours=1),
        allowed_runtime_states={"SHADOW"},
    )
    assert not result.passed
    assert "RELEASE_CHANGED_DURING_GATE" in result.reason_codes
    assert "UNPROTECTED_POSITION" in result.reason_codes
