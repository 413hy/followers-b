"""Immediate Codex wakeups remain queued while an audit is already active."""

from __future__ import annotations

import subprocess

from ai_quant.services import copy_trading


def test_incident_is_retried_after_active_audit_finishes(monkeypatch) -> None:
    commands: list[tuple[str, ...]] = []
    active_results = iter((0, 3))

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        commands.append(tuple(command))
        if "is-active" in command:
            return subprocess.CompletedProcess(command, next(active_results))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(copy_trading.subprocess, "run", fake_run)
    trigger = copy_trading._CodexIncidentTrigger()

    trigger("signal:failed")
    assert not any("start" in command for command in commands)

    trigger.flush()
    assert any("start" in command for command in commands)

    command_count = len(commands)
    trigger("signal:failed")
    assert len(commands) == command_count


def test_persistent_incident_reaudits_after_bounded_cooldown(monkeypatch) -> None:
    commands: list[tuple[str, ...]] = []
    now = [100.0]

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        del kwargs
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 3 if "is-active" in command else 0)

    monkeypatch.setattr(copy_trading.subprocess, "run", fake_run)
    trigger = copy_trading._CodexIncidentTrigger(
        clock=lambda: now[0],
        reaudit_seconds=300,
    )

    trigger("leader-poll:contract-drift")
    assert sum("start" in command for command in commands) == 1

    now[0] += 299
    trigger("leader-poll:contract-drift")
    assert sum("start" in command for command in commands) == 1

    now[0] += 1
    trigger("leader-poll:contract-drift")
    assert sum("start" in command for command in commands) == 2
