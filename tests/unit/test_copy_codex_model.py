from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ai_quant.copy_trading.codex_audit import CodexSystemAuditor
from ai_quant.copy_trading.codex_model import (
    CODEX_INTERVENTION_MODEL,
    CODEX_INTERVENTION_REASONING_EFFORT,
    codex_model_arguments,
)
from ai_quant.copy_trading.codex_repair import CodexSystemRepairer
from ai_quant.services.copy_codex_audit import (
    _RECENT_SIGNAL_ERRORS_SQL,
    _newly_resolved_incident_ids,
    _pause_new_entries_justified,
    _reconciliation_status,
)

NOW = datetime(2026, 7, 19, 1, 39, tzinfo=UTC)


def test_completed_codex_audit_prevents_repeat_signal_error_alerts() -> None:
    assert "audit.findings->'reviewed_signal_ids'" in _RECENT_SIGNAL_ERRORS_SQL
    assert "audit.findings ? 'codex'" in _RECENT_SIGNAL_ERRORS_SQL
    assert "NOT (audit.findings ? 'reviewed_signal_ids')" in _RECENT_SIGNAL_ERRORS_SQL


def test_audit_detects_incident_recovery_during_model_review() -> None:
    incident_id = "a" * 64

    assert _newly_resolved_incident_ids(
        [{"incident_id": incident_id, "resolved": False}],
        [{"incident_id": incident_id, "resolved": True}],
    ) == frozenset({incident_id})
    assert not _newly_resolved_incident_ids(
        [{"incident_id": incident_id, "resolved": True}],
        [{"incident_id": incident_id, "resolved": True}],
    )


def test_codex_intervention_uses_explicit_frontier_model_policy() -> None:
    assert CODEX_INTERVENTION_MODEL == "gpt-5.6-sol"
    assert CODEX_INTERVENTION_REASONING_EFFORT == "high"
    assert codex_model_arguments() == (
        "--model",
        "gpt-5.6-sol",
        "--config",
        'model_reasoning_effort="high"',
    )


def test_fresh_partial_fill_is_inside_reconciliation_grace() -> None:
    requires_reconciliation, grace_active = _reconciliation_status(
        decision_state="UNCERTAIN",
        submission_state="PARTIALLY_FILLED",
        decision_occurred_at=NOW - timedelta(seconds=20),
        submission_occurred_at=NOW - timedelta(seconds=20),
        now=NOW,
    )

    assert not requires_reconciliation
    assert grace_active


def test_overdue_partial_fill_requires_reconciliation() -> None:
    requires_reconciliation, grace_active = _reconciliation_status(
        decision_state="UNCERTAIN",
        submission_state="PARTIALLY_FILLED",
        decision_occurred_at=NOW - timedelta(minutes=3),
        submission_occurred_at=NOW - timedelta(minutes=3),
        now=NOW,
    )

    assert requires_reconciliation
    assert not grace_active


def test_codex_pause_gate_rejects_model_pause_for_fresh_partial_only() -> None:
    facts = SimpleNamespace(
        stale_poll_seconds=10,
        oldest_pending_notification_seconds=0,
        history_gap_failures=0,
        uncertain_signals=0,
        failed_signals_last_hour=0,
        overdue_pending_entries=0,
        latest_watchdog_state="HEALTHY",
    )

    justified = _pause_new_entries_justified(
        facts=facts,
        service_states={"aiq-copy-poller.service": "active"},
        recent_signal_errors=[
            {
                "requires_reconciliation": False,
                "reconciliation_grace_active": True,
            }
        ],
    )

    assert not justified


def test_hourly_audit_passes_model_policy_despite_ignoring_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run(command: list[str], **kwargs: Any) -> None:
        captured.extend(command)
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text(
            json.dumps(
                {
                    "status": "HEALTHY",
                    "summary": "healthy",
                    "findings": [],
                    "recommended_actions": ["NO_ACTION"],
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    auditor = CodexSystemAuditor(
        schema_path=Path("contracts/copy-system-audit.schema.json"),
        work_root=tmp_path,
    )

    auditor.audit({"state": "HEALTHY"})

    assert "--ignore-user-config" in captured
    assert captured[captured.index("--model") + 1] == "gpt-5.6-sol"
    assert captured[captured.index("--config") + 1] == 'model_reasoning_effort="high"'
    assert "Simplified Chinese" in captured[-1]


def test_automatic_repair_uses_high_model_and_workspace_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run(command: list[str], **kwargs: Any) -> None:
        captured.extend(command)
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text(
            json.dumps(
                {
                    "status": "NO_CHANGE",
                    "summary": "operational incident already resolved",
                    "root_cause": "no persistent code defect",
                    "changed_files": [],
                    "tests_run": ["inspection only"],
                    "follow_up_required": False,
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    repairer = CodexSystemRepairer(
        schema_path=Path("contracts/copy-system-repair.schema.json"),
        repository_root=Path.cwd(),
        work_root=tmp_path,
    )

    repairer.repair({"incident": "sanitized"})

    assert "--ignore-user-config" in captured
    assert captured[captured.index("--model") + 1] == "gpt-5.6-sol"
    assert captured[captured.index("--sandbox") + 1] == "workspace-write"
    assert "Simplified Chinese" in captured[-1]
