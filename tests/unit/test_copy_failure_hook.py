from __future__ import annotations

import subprocess
import sys

from ai_quant.services import copy_failure_hook


def test_failure_hook_starts_reporter_on_first_abnormal_exit(monkeypatch) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        del kwargs
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setenv("SERVICE_RESULT", "exit-code")
    monkeypatch.setattr(
        sys, "argv", ["copy_failure_hook", "--source-unit", "aiq-copy-telegram.service"]
    )
    monkeypatch.setattr(copy_failure_hook.subprocess, "run", run)

    assert copy_failure_hook.main() == 0
    assert commands == [
        [
            "/usr/bin/systemctl",
            "start",
            "--no-block",
            "aiq-copy-incident-reporter@aiq-copy-telegram.service.service",
        ]
    ]


def test_failure_hook_ignores_normal_stop(monkeypatch) -> None:
    monkeypatch.setenv("SERVICE_RESULT", "success")
    monkeypatch.setattr(
        sys, "argv", ["copy_failure_hook", "--source-unit", "aiq-copy-poller.service"]
    )
    monkeypatch.setattr(
        copy_failure_hook.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    assert copy_failure_hook.main() == 0
