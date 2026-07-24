"""Database-independent systemd failure reports with a durable Telegram spool."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess  # nosec B404 -- fixed binaries and an exact unit allowlist
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_quant.notifications.telegram_bot import (
    TelegramBotClient,
    TelegramBotError,
    TelegramBotFileConfig,
    contextual_inline_keyboard,
)

_CODEX_AUDIT_UNIT = "aiq-copy-codex-audit.service"
_ALLOWED_SOURCE_UNITS = frozenset(
    {
        "aiq-copy-codex-audit.service",
        "aiq-copy-codex-repair.service",
        "aiq-copy-codex-repair-finalize.service",
        "aiq-copy-database-backup.service",
        "aiq-copy-infra.service",
        "aiq-copy-leader-selector.service",
        "aiq-copy-leader-status-check.service",
        "aiq-copy-long-leader-selector.service",
        "aiq-copy-migrations.service",
        "aiq-copy-poller.service",
        "aiq-copy-telegram.service",
        "aiq-copy-watchdog.service",
        "aiq-testnet-user-stream.service",
    }
)
_EVIDENCE_ROOT = Path("/var/lib/ai-quant/evidence/copy-trading/incidents")
_REDACTIONS = (
    re.compile(r"(?i)(api[_-]?key|secret|token|password)=([^\s]+)"),
    re.compile(r"(?i)(postgres(?:ql)?://)[^@\s]+@"),
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report copy-system service failures")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--source-unit", choices=sorted(_ALLOWED_SOURCE_UNITS))
    action.add_argument("--replay-pending", action="store_true")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--chat-ids-file", type=Path, required=True)
    parser.add_argument("--authorized-user-ids-file", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, default=_EVIDENCE_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _redact(value: str, *, maximum: int = 12_000) -> str:
    text = value
    text = _REDACTIONS[0].sub(r"\1=<redacted>", text)
    text = _REDACTIONS[1].sub(r"\1<redacted>@", text)
    return text[-maximum:]


def _command(command: list[str], *, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603  # nosec B603
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return subprocess.CompletedProcess(
            command,
            124,
            stdout="",
            stderr=type(error).__name__,
        )


def _unit_facts(unit: str) -> dict[str, str]:
    if unit not in _ALLOWED_SOURCE_UNITS:
        raise ValueError("COPY_INCIDENT_SOURCE_UNIT_DENIED")
    result = _command(
        [
            "/usr/bin/systemctl",
            "show",
            unit,
            "--property=ActiveState,SubState,Result,NRestarts,ExecMainStatus,StateChangeTimestamp",
        ]
    )
    facts: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key:
            facts[key] = _redact(value, maximum=300)
    facts["systemctl_returncode"] = str(result.returncode)
    return facts


def _journal_tail(unit: str) -> str:
    if unit not in _ALLOWED_SOURCE_UNITS:
        raise ValueError("COPY_INCIDENT_SOURCE_UNIT_DENIED")
    result = _command(
        [
            "/usr/bin/journalctl",
            "--unit",
            unit,
            "--lines",
            "40",
            "--no-pager",
            "--output=short-iso",
        ]
    )
    return _redact(result.stdout or result.stderr)


def _bucket(now: datetime) -> datetime:
    minute = now.minute - (now.minute % 15)
    return now.replace(minute=minute, second=0, microsecond=0)


def _incident_id(unit: str, now: datetime) -> str:
    if unit not in _ALLOWED_SOURCE_UNITS:
        raise ValueError("COPY_INCIDENT_SOURCE_UNIT_DENIED")
    return hashlib.sha256(f"{unit}:{_bucket(now).isoformat()}".encode()).hexdigest()


def _report_path(root: Path, unit: str, now: datetime, incident_id: str) -> Path:
    safe_unit = unit.removesuffix(".service").replace(".", "-")
    stamp = _bucket(now).strftime("%Y%m%dT%H%MZ")
    return root / f"{stamp}-{safe_unit}-{incident_id[:12]}.json"


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(dict(report), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _load_report(path: Path) -> dict[str, Any] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _latest_error(journal: str) -> str:
    lines = [line.strip() for line in journal.splitlines() if line.strip()]
    preferred = [
        line
        for line in lines
        if any(word in line.casefold() for word in ("error", "failed", "exception", "traceback"))
    ]
    return _redact((preferred or lines or ["没有可用日志"])[-1], maximum=500)


def _failure_explanation(facts: Mapping[str, Any]) -> str:
    result = str(facts.get("Result", "unknown"))
    if result == "start-limit-hit":
        return (
            "systemd 在短时间内收到过多启动请求, 本次请求尚未进入业务程序; "
            "这不是 Binance、交易订单或数据库故障"
        )
    if result == "exit-code":
        return "业务程序本次以非零状态退出, 已保留精确日志并请求自动排查"
    if result in {"signal", "timeout"}:
        return "业务程序被信号终止或执行超时, systemd 将按策略恢复并由 Codex 复核"
    return "systemd 检测到服务未按预期完成, 已保留运行状态和精确日志"


def _render_telegram(report: Mapping[str, Any]) -> str:
    unit = str(report.get("source_unit", "unknown"))
    facts = report.get("unit_facts")
    state = "unknown"
    result = "unknown"
    restarts = "unknown"
    if isinstance(facts, dict):
        state = f"{facts.get('ActiveState', 'unknown')}/{facts.get('SubState', 'unknown')}"
        result = str(facts.get("Result", "unknown"))
        restarts = str(facts.get("NRestarts", "unknown"))
    last_error = _latest_error(str(report.get("journal_tail", "")))
    report_path = str(report.get("report_path", "本地报告路径未知"))
    wakeup = report.get("codex_wakeup_requested") is True
    action = "systemd 将按单元策略重试"
    if wakeup:
        action += "; 已请求 Codex 即时审查"
    explanation = _failure_explanation(facts if isinstance(facts, dict) else {})
    return (
        "🚨 跟单系统服务故障\n"
        f"组件: {unit}\n"
        f"状态: {state} | result={result} | restarts={restarts}\n"
        f"原因: {explanation}\n"
        f"最近错误: {last_error}\n"
        f"自动处置: {action}\n"
        "交易保护: 未确认状态不会盲目重下; 数据库提交声明和虚拟仓位保持幂等恢复。\n"
        f"排查报告: {report_path}"
    )[:3900]


def _telegram_config(arguments: argparse.Namespace) -> TelegramBotFileConfig:
    return TelegramBotFileConfig.load(
        arguments.token_file,
        arguments.chat_ids_file,
        arguments.authorized_user_ids_file,
    )


def _deliver(report: Mapping[str, Any], config: TelegramBotFileConfig) -> bool:
    client = TelegramBotClient(config)
    try:
        for chat_id in config.allowed_chat_ids:
            client.send_message(
                chat_id,
                _render_telegram(report),
                reply_markup=contextual_inline_keyboard("health"),
            )
    except TelegramBotError:
        return False
    return True


def _wake_codex(source_unit: str) -> bool:
    if source_unit == _CODEX_AUDIT_UNIT:
        return False
    result = _command(
        ["/usr/bin/systemctl", "start", "--no-block", _CODEX_AUDIT_UNIT],
        timeout=15,
    )
    return result.returncode == 0


def _record_failure(arguments: argparse.Namespace, source_unit: str) -> Path:
    now = datetime.now(UTC)
    incident_id = _incident_id(source_unit, now)
    path = _report_path(arguments.evidence_root, source_unit, now, incident_id)
    previous = _load_report(path)
    occurrence_count = int(previous.get("occurrence_count", 0)) + 1 if previous else 1
    already_sent = previous is not None and previous.get("notification_status") == "SENT"
    wakeup_requested = source_unit != _CODEX_AUDIT_UNIT
    report: dict[str, Any] = {
        "schema_version": "1.0.0",
        "incident_id": incident_id,
        "source_unit": source_unit,
        "first_occurred_at": (
            previous.get("first_occurred_at", now.isoformat()) if previous else now.isoformat()
        ),
        "last_occurred_at": now.isoformat(),
        "occurrence_count": occurrence_count,
        "unit_facts": _unit_facts(source_unit),
        "journal_tail": _journal_tail(source_unit),
        "codex_wakeup_requested": wakeup_requested,
        "codex_wakeup_started": False,
        "notification_status": "SENT" if already_sent else "PENDING",
        "report_path": str(path),
    }
    _write_report(path, report)
    if wakeup_requested and not arguments.dry_run:
        report["codex_wakeup_started"] = _wake_codex(source_unit)
    if arguments.dry_run:
        report["notification_status"] = "DRY_RUN"
    elif not already_sent:
        try:
            delivered = _deliver(report, _telegram_config(arguments))
        except (OSError, ValueError):
            delivered = False
        report["notification_status"] = "SENT" if delivered else "PENDING"
        report["last_delivery_attempt_at"] = now.isoformat()
    _write_report(path, report)
    return path


def _replay(arguments: argparse.Namespace) -> int:
    try:
        config = _telegram_config(arguments)
    except (OSError, ValueError):
        return 0
    delivered = 0
    for path in sorted(arguments.evidence_root.glob("*.json")):
        report = _load_report(path)
        if report is None or report.get("notification_status") != "PENDING":
            continue
        now = datetime.now(UTC)
        if arguments.dry_run:
            continue
        if _deliver(report, config):
            report["notification_status"] = "SENT"
            report["last_delivery_attempt_at"] = now.isoformat()
            _write_report(path, report)
            delivered += 1
            if delivered >= 20:
                break
    return delivered


def main() -> int:
    arguments = _arguments()
    arguments.evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = arguments.evidence_root / ".reporter.lock"
    with lock_path.open("a", encoding="ascii") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if arguments.replay_pending:
            count = _replay(arguments)
            print(json.dumps({"event": "copy_incident_replay", "delivered": count}))
        else:
            path = _record_failure(arguments, str(arguments.source_unit))
            print(json.dumps({"event": "copy_incident_report", "path": str(path)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
