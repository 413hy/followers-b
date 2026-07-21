import subprocess
from pathlib import Path

from ai_quant.services import copy_codex_repair


def test_automatic_repair_runs_complete_verification_gate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], *, cwd: Path, timeout: int) -> tuple[bool, str]:
        del cwd, timeout
        commands.append(command)
        return True, "ok"

    monkeypatch.setattr(copy_codex_repair, "_run", fake_run)

    succeeded, evidence = copy_codex_repair._verify(tmp_path)

    assert succeeded
    assert len(evidence) == 5
    rendered = [" ".join(command) for command in commands]
    assert any("ruff check ." in command for command in rendered)
    assert any("ruff format --check" in command for command in rendered)
    assert any("mypy src" in command for command in rendered)
    assert any("pytest -q" in command for command in rendered)
    assert any(
        "alembic -c migrations/business/alembic.ini heads" in command for command in rendered
    )


def test_no_change_rechecks_watchdog_without_restarting_trading_services(
    tmp_path: Path,
    monkeypatch,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], *, cwd: Path, timeout: int) -> tuple[bool, str]:
        del cwd, timeout
        commands.append(command)
        return True, "ok"

    monkeypatch.setattr(copy_codex_repair, "_run", fake_run)

    succeeded, actions = copy_codex_repair._recheck_runtime(tmp_path)

    assert succeeded
    assert actions == ["watchdog:ok"]
    assert commands == [["/usr/bin/systemctl", "start", "aiq-copy-watchdog.service"]]


def test_alembic_verification_rejects_multiple_heads(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        copy_codex_repair.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout="first (head)\nsecond (head)\n",
            stderr="",
        ),
    )

    succeeded, detail = copy_codex_repair._run(
        ["alembic", "heads"],
        cwd=tmp_path,
        timeout=10,
    )

    assert not succeeded
    assert detail == "expected one Alembic head, observed 2"
