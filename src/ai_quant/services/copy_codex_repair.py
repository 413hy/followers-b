"""Run a bounded Codex code repair, verify it, then recheck the active environment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess  # nosec B404 -- all executable paths and units are fixed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_quant.copy_trading.codex_repair import CodexRepairError, CodexSystemRepairer
from ai_quant.copy_trading.reason_text import reason_code_text

_WRITABLE_ROOTS = (
    "src",
    "tests",
    "docs",
)
_IGNORED_NAMES = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
_RUNTIME_UNITS = ("aiq-copy-poller.service", "aiq-copy-telegram.service")


@dataclass(frozen=True, slots=True)
class _SavedFile:
    data: bytes
    mode: int


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run automatic bounded Codex repair")
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--schema-file", type=Path, required=True)
    parser.add_argument("--request-file", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    return parser.parse_args()


def _files(repository_root: Path) -> dict[str, _SavedFile]:
    snapshot: dict[str, _SavedFile] = {}
    for relative_root in _WRITABLE_ROOTS:
        root = repository_root / relative_root
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if any(part in _IGNORED_NAMES for part in path.parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            relative = str(path.relative_to(repository_root))
            snapshot[relative] = _SavedFile(path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    return snapshot


def _changed_files(repository_root: Path, before: dict[str, _SavedFile]) -> list[str]:
    after = _files(repository_root)
    changed = [
        path for path in sorted(set(before) | set(after)) if before.get(path) != after.get(path)
    ]
    return changed


def _restore(repository_root: Path, before: dict[str, _SavedFile]) -> None:
    after = _files(repository_root)
    for relative in sorted(set(after) - set(before), reverse=True):
        (repository_root / relative).unlink(missing_ok=True)
    for relative, saved in before.items():
        path = repository_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(saved.data)
        os.chmod(path, saved.mode)


def _run(command: list[str], *, cwd: Path, timeout: int) -> tuple[bool, str]:
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603
            command,
            cwd=cwd,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, type(error).__name__
    detail = (result.stdout or result.stderr).strip().splitlines()
    succeeded = result.returncode == 0
    if succeeded and command[-1:] == ["heads"]:
        heads = [line for line in detail if line.rstrip().endswith("(head)")]
        if len(heads) != 1:
            return False, f"expected one Alembic head, observed {len(heads)}"
    return succeeded, (detail[-1] if detail else f"exit={result.returncode}")[:300]


def _verify(repository_root: Path) -> tuple[bool, list[str]]:
    checks = (
        ([str(repository_root / ".venv/bin/python"), "-m", "ruff", "check", "."], 300),
        (
            [
                str(repository_root / ".venv/bin/python"),
                "-m",
                "ruff",
                "format",
                "--check",
                "src",
                "tests",
                "migrations/business/versions",
            ],
            300,
        ),
        ([str(repository_root / ".venv/bin/python"), "-m", "mypy", "src"], 600),
        ([str(repository_root / ".venv/bin/python"), "-m", "pytest", "-q"], 1200),
        (
            [
                str(repository_root / ".venv/bin/alembic"),
                "-c",
                "migrations/business/alembic.ini",
                "heads",
            ],
            120,
        ),
    )
    evidence: list[str] = []
    for command, timeout in checks:
        succeeded, detail = _run(command, cwd=repository_root, timeout=timeout)
        evidence.append(f"{'PASS' if succeeded else 'FAIL'} {' '.join(command[2:])}: {detail}")
        if not succeeded:
            return False, evidence
    return True, evidence


def _restart_runtime(repository_root: Path) -> tuple[bool, list[str]]:
    actions: list[str] = []
    for unit in _RUNTIME_UNITS:
        succeeded, detail = _run(
            ["/usr/bin/systemctl", "restart", unit],
            cwd=repository_root,
            timeout=120,
        )
        actions.append(f"{unit}:{detail}")
        if not succeeded:
            return False, actions
    succeeded, detail = _run(
        ["/usr/bin/systemctl", "start", "aiq-copy-watchdog.service"],
        cwd=repository_root,
        timeout=180,
    )
    actions.append(f"watchdog:{detail}")
    return succeeded, actions


def _recheck_runtime(repository_root: Path) -> tuple[bool, list[str]]:
    succeeded, detail = _run(
        ["/usr/bin/systemctl", "start", "aiq-copy-watchdog.service"],
        cwd=repository_root,
        timeout=180,
    )
    return succeeded, [f"watchdog:{detail}"]


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def main() -> int:
    arguments = _arguments()
    repository_root = arguments.repository_root.resolve()
    before = _files(repository_root)
    occurred_at = datetime.now(UTC)
    try:
        request = json.loads(arguments.request_file.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ValueError("COPY_CODEX_REPAIR_REQUEST_INVALID")
        result = CodexSystemRepairer(
            schema_path=arguments.schema_file,
            repository_root=repository_root,
            work_root=arguments.work_root,
        ).repair(request)
        model_document = dict(result.document)
        input_digest = result.input_digest
        report_digest = result.report_digest
    except (CodexRepairError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        error_code = str(error)
        reason = (
            reason_code_text(error_code)
            if error_code.startswith("COPY_")
            else f"Codex 自动修复进程发生 {type(error).__name__} 异常"
        )
        model_document = {
            "status": "FAILED",
            "summary": "Codex 自动修复没有完成, 未应用任何代码修改",
            "root_cause": reason,
            "changed_files": [],
            "tests_run": [],
            "follow_up_required": True,
        }
        input_digest = ""
        report_digest = ""
    changed = _changed_files(repository_root, before)
    verification: list[str] = []
    service_actions: list[str] = []
    status = str(model_document.get("status", "FAILED"))
    if status != "FAILED" and changed:
        verified, verification = _verify(repository_root)
        if verified:
            runtime_changed = any(path.startswith("src/") for path in changed)
            action_succeeded, service_actions = (
                _restart_runtime(repository_root)
                if runtime_changed
                else _recheck_runtime(repository_root)
            )
            if action_succeeded:
                status = "REPAIRED"
            else:
                _restore(repository_root, before)
                status = "FAILED"
                changed = []
        else:
            _restore(repository_root, before)
            status = "FAILED"
            changed = []
    elif status != "FAILED":
        status = "NO_CHANGE"
        rechecked, service_actions = _recheck_runtime(repository_root)
        if not rechecked:
            status = "FAILED"
    elif changed:
        _restore(repository_root, before)
        changed = []
    evidence = {
        "schema_version": "1.0.0",
        "occurred_at": occurred_at.isoformat(),
        "status": status,
        "summary": str(model_document.get("summary", "Codex 自动修复没有完成"))[:800],
        "root_cause": str(model_document.get("root_cause", "尚未取得明确根因"))[:1000],
        "changed_files": changed,
        "model_tests_run": model_document.get("tests_run", []),
        "verification": verification,
        "service_actions": service_actions,
        "follow_up_required": (
            status == "FAILED" or bool(model_document.get("follow_up_required", False))
        ),
        "input_digest": input_digest,
        "report_digest": report_digest,
    }
    evidence["evidence_hash"] = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    _write(arguments.evidence_file, evidence)
    print(json.dumps({"event": "copy_codex_repair", "status": status}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
