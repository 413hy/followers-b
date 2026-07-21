"""Hourly Codex audit with deterministic action gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os

# Only the fixed systemctl path and the two allowlisted unit names are accepted.
import subprocess  # nosec B404
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ai_quant.copy_trading.codex_audit import CodexAuditError, CodexSystemAuditor
from ai_quant.copy_trading.health import HealthState, PostgresHealthStore
from ai_quant.copy_trading.models import RuntimeControlState
from ai_quant.copy_trading.repository import CopyTradingRepository
from ai_quant.services.copy_incident_reporter import _ALLOWED_SOURCE_UNITS, _unit_facts
from ai_quant.services.copy_trading import _private_text

_POLLER_UNIT = "aiq-copy-poller.service"
_TELEGRAM_UNIT = "aiq-copy-telegram.service"
_REPAIR_UNIT = "aiq-copy-codex-repair.service"
_INCIDENT_ROOT = Path("/var/lib/ai-quant/evidence/copy-trading/incidents")
_REPAIR_REQUEST = Path("/var/lib/ai-quant/evidence/copy-trading/audit/repair-request.json")
_RECONCILIATION_GRACE = timedelta(minutes=2)
_IN_FLIGHT_SUBMISSION_STATES = frozenset(
    {"SUBMITTING", "ACKNOWLEDGED", "PARTIALLY_FILLED", "UNKNOWN"}
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run hourly Codex copy-system audit")
    parser.add_argument(
        "--environment",
        choices=("TESTNET", "PRODUCTION"),
        default="TESTNET",
    )
    parser.add_argument("--database-url-file", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--schema-file", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    return parser.parse_args()


def _unit_state(unit: str) -> str:
    result = subprocess.run(  # noqa: S603  # nosec B603
        ["/usr/bin/systemctl", "is-active", unit],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    value = result.stdout.strip()
    return value if value in {"active", "inactive", "failed", "activating"} else "unknown"


def _restart(unit: str) -> bool:
    if unit not in {_POLLER_UNIT, _TELEGRAM_UNIT}:
        return False
    result = subprocess.run(  # noqa: S603  # nosec B603
        ["/usr/bin/systemctl", "restart", unit],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=60,
    )
    return result.returncode == 0


def _start_repair(request: dict[str, Any]) -> bool:
    _REPAIR_REQUEST.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = _REPAIR_REQUEST.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(request, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(_REPAIR_REQUEST)
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603
            ["/usr/bin/systemctl", "start", "--no-block", _REPAIR_UNIT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _recent_incidents(
    root: Path = _INCIDENT_ROOT,
    *,
    now: datetime,
    unit_facts_loader: Callable[[str], dict[str, str]] = _unit_facts,
) -> list[dict[str, Any]]:
    incidents: list[dict[str, Any]] = []
    if not root.is_dir():
        return incidents
    candidates: list[tuple[float, Path]] = []
    for path in root.glob("*.json"):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(candidates, key=lambda item: item[0], reverse=True):
        if len(incidents) >= 5:
            break
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            occurred_at = datetime.fromisoformat(str(document["last_occurred_at"]))
        except (KeyError, OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            continue
        if occurred_at.tzinfo is None or now - occurred_at.astimezone(UTC) > timedelta(hours=2):
            continue
        historical_unit_facts = document.get("unit_facts")
        historical_facts = historical_unit_facts if isinstance(historical_unit_facts, dict) else {}
        source_unit = str(document.get("source_unit", "unknown"))[:80]
        current_facts = (
            unit_facts_loader(source_unit) if source_unit in _ALLOWED_SOURCE_UNITS else {}
        )
        current_active_state = str(current_facts.get("ActiveState", "unknown"))[:24]
        current_result = str(current_facts.get("Result", "unknown"))[:40]
        long_running = source_unit in {
            _POLLER_UNIT,
            _TELEGRAM_UNIT,
            "aiq-testnet-user-stream.service",
        }
        resolved = current_result == "success" and (
            current_active_state == "active"
            if long_running
            else current_active_state in {"active", "inactive", "activating"}
        )
        journal_lines = str(document.get("journal_tail", "")).splitlines()
        error_lines = [
            line[-500:]
            for line in journal_lines
            if any(
                marker in line.casefold()
                for marker in ("error", "failed", "exception", "traceback", "critical")
            )
        ][-5:]
        try:
            occurrence_count = max(1, int(document.get("occurrence_count", 1)))
        except (TypeError, ValueError):
            occurrence_count = 1
        incidents.append(
            {
                "incident_id": str(document.get("incident_id", ""))[:64],
                "source_unit": source_unit,
                "last_occurred_at": occurred_at.astimezone(UTC).isoformat(),
                "occurrence_count": occurrence_count,
                "historical_active_state": str(historical_facts.get("ActiveState", "unknown"))[:24],
                "historical_result": str(historical_facts.get("Result", "unknown"))[:40],
                "current_active_state": current_active_state,
                "current_result": current_result,
                "resolved": resolved,
                "notification_status": str(document.get("notification_status", "unknown"))[:16],
                "error_evidence": error_lines,
                "last_log_line": (journal_lines[-1] if journal_lines else "unavailable")[-500:],
            }
        )
    return incidents


def _persist_resolved_incidents(
    database_url: str,
    incidents: list[dict[str, Any]],
    *,
    occurred_at: datetime,
) -> int:
    """Notify once after a successful Codex audit confirms service recovery."""

    recovered = [
        incident
        for incident in incidents
        if incident.get("resolved") is True and len(str(incident.get("incident_id", ""))) == 64
    ]
    if not recovered:
        return 0
    inserted = 0
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        for incident in recovered:
            incident_id = str(incident["incident_id"])
            source_unit = str(incident.get("source_unit", "unknown"))[:80]
            payload = {
                "event": "copy_system",
                "state": "RECOVERED",
                "summary": (
                    f"Codex 复核确认 {source_unit} 已恢复; 当前 systemd 结果为 success。"
                    "原故障证据仍保留; 后续同类故障会重新生成独立报告。"
                ),
                "incident_id": incident_id,
                "occurred_at": occurred_at.isoformat(),
            }
            payload_hash = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            cursor.execute(
                """
                INSERT INTO control.outbox(
                  message_id,deduplication_key,topic,payload,payload_hash
                ) VALUES (%s,%s,'copy.telegram',%s,%s)
                ON CONFLICT (deduplication_key) DO NOTHING
                """,
                (
                    hashlib.sha256(f"incident-recovered:{incident_id}".encode()).hexdigest(),
                    f"copy-incident-recovered:{incident_id}",
                    Jsonb(payload),
                    payload_hash,
                ),
            )
            inserted += cursor.rowcount
    return inserted


def _newly_resolved_incident_ids(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> frozenset[str]:
    """Identify failures that recovered while the Codex review itself was running."""

    unresolved = {
        str(incident.get("incident_id", ""))
        for incident in before
        if incident.get("resolved") is not True
    }
    resolved_after = {
        str(incident.get("incident_id", ""))
        for incident in after
        if incident.get("resolved") is True
    }
    return frozenset(identifier for identifier in unresolved & resolved_after if identifier)


def _recent_signal_errors(
    database_url: str,
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """Supply Codex with exact persisted trading errors, never credentials or raw payloads."""
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH latest_decision AS (
                  SELECT DISTINCT ON (signal_id)
                         signal_id,state,reason_codes,occurred_at
                    FROM copytrading.signal_decision_events
                   ORDER BY signal_id,occurred_at DESC,decision_event_id DESC
                ), latest_submission AS (
                  SELECT DISTINCT ON (signal_id)
                         signal_id,state,reason_codes,occurred_at
                    FROM copytrading.submission_events
                   ORDER BY signal_id,occurred_at DESC,submission_event_id DESC
                )
                SELECT signal.signal_id,signal.lead_portfolio_id,signal.symbol,
                       signal.position_side,signal.signal_kind,
                       decision.state AS decision_state,
                       decision.reason_codes AS decision_reason_codes,
                       decision.occurred_at AS decision_occurred_at,
                       claim.client_order_id,claim.order_type,claim.limit_price,
                       submission.state AS submission_state,
                       submission.reason_codes AS submission_reason_codes,
                       submission.occurred_at AS submission_occurred_at,
                       snapshot.nickname
                  FROM latest_decision AS decision
                  JOIN copytrading.signals AS signal USING (signal_id)
                  LEFT JOIN copytrading.submission_claims AS claim USING (signal_id)
                  LEFT JOIN latest_submission AS submission USING (signal_id)
                  LEFT JOIN LATERAL (
                    SELECT nickname
                      FROM copytrading.leader_snapshots
                     WHERE lead_portfolio_id=signal.lead_portfolio_id
                     ORDER BY observed_at DESC,snapshot_id DESC LIMIT 1
                  ) AS snapshot ON TRUE
                 WHERE decision.state IN ('FAILED','UNCERTAIN')
                   AND decision.occurred_at >= %s
                 ORDER BY decision.occurred_at DESC,signal.signal_id
                 LIMIT 8
                """,
                (now - timedelta(hours=2),),
            )
            rows = list(cursor.fetchall())

    def reasons(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item)[:100] for item in value[:6]]

    errors: list[dict[str, Any]] = []
    for row in rows:
        decision_state = str(row["decision_state"])
        submission_state = None if row["submission_state"] is None else str(row["submission_state"])
        decision_occurred_at = row["decision_occurred_at"].astimezone(UTC)
        submission_occurred_at = (
            None
            if row["submission_occurred_at"] is None
            else row["submission_occurred_at"].astimezone(UTC)
        )
        requires_reconciliation, reconciliation_grace_active = _reconciliation_status(
            decision_state=decision_state,
            submission_state=submission_state,
            decision_occurred_at=decision_occurred_at,
            submission_occurred_at=submission_occurred_at,
            now=now,
        )
        errors.append(
            {
                "signal_id": str(row["signal_id"]),
                "leader_id": str(row["lead_portfolio_id"]),
                "leader_name": str(row["nickname"] or "unknown")[:80],
                "symbol": str(row["symbol"]),
                "position_side": str(row["position_side"]),
                "signal_kind": str(row["signal_kind"]),
                "decision_state": decision_state,
                "decision_reason_codes": reasons(row["decision_reason_codes"]),
                "decision_occurred_at": decision_occurred_at.isoformat(),
                "client_order_id": (
                    None if row["client_order_id"] is None else str(row["client_order_id"])
                ),
                "order_type": None if row["order_type"] is None else str(row["order_type"]),
                "limit_price": (None if row["limit_price"] is None else str(row["limit_price"])),
                "submission_state": submission_state,
                "submission_reason_codes": reasons(row["submission_reason_codes"]),
                "submission_occurred_at": (
                    None if submission_occurred_at is None else submission_occurred_at.isoformat()
                ),
                "requires_reconciliation": requires_reconciliation,
                "reconciliation_grace_active": reconciliation_grace_active,
            }
        )
    return errors


def _reconciliation_status(
    *,
    decision_state: str,
    submission_state: str | None,
    decision_occurred_at: datetime,
    submission_occurred_at: datetime | None,
    now: datetime,
) -> tuple[bool, bool]:
    """Return (requires_reconciliation, grace_active) for one latest signal state."""

    in_flight = submission_state in _IN_FLIGHT_SUBMISSION_STATES
    latest_observation = max(
        value for value in (decision_occurred_at, submission_occurred_at) if value is not None
    )
    grace_active = (
        decision_state == "UNCERTAIN"
        and in_flight
        and latest_observation > now - _RECONCILIATION_GRACE
    )
    return (not grace_active and (decision_state == "UNCERTAIN" or in_flight), grace_active)


def _pause_new_entries_justified(
    *,
    facts: Any,
    service_states: dict[str, str],
    recent_signal_errors: list[dict[str, Any]],
) -> bool:
    """Gate model-requested pauses on deterministic, currently unsafe evidence."""

    stale_poll = facts.stale_poll_seconds is None or facts.stale_poll_seconds > 120
    notification_stalled = (
        facts.oldest_pending_notification_seconds is not None
        and facts.oldest_pending_notification_seconds > 600
    )
    return any(
        (
            facts.history_gap_failures > 0,
            facts.uncertain_signals > 0,
            facts.failed_signals_last_hour >= 3,
            facts.overdue_pending_entries > 0,
            stale_poll,
            notification_stalled,
            service_states.get(_POLLER_UNIT) != "active",
            facts.latest_watchdog_state == "FAILED",
            any(bool(error.get("requires_reconciliation")) for error in recent_signal_errors),
        )
    )


def _recent_selection_failures(
    database_url: str,
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """Expose only the latest unresolved scheduled selection failure per strategy."""

    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH latest AS (
                  SELECT DISTINCT ON (selection_kind)
                         selection_kind,state,reason_codes,occurred_at
                    FROM copytrading.selection_runs
                   ORDER BY selection_kind,occurred_at DESC,selection_run_id DESC
                )
                SELECT selection_kind,reason_codes,occurred_at
                  FROM latest
                 WHERE state='FAILED' AND occurred_at >= %s
                 ORDER BY occurred_at DESC,selection_kind
                """,
                (now - timedelta(hours=2),),
            )
            rows = list(cursor.fetchall())
    return [
        {
            "selection_kind": str(row["selection_kind"])[:24],
            "reason_codes": (
                [str(value)[:120] for value in row["reason_codes"][:6]]
                if isinstance(row["reason_codes"], list)
                else []
            ),
            "occurred_at": row["occurred_at"].astimezone(UTC).isoformat(),
        }
        for row in rows
    ]


def _latest_repair(database_url: str) -> dict[str, Any] | None:
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT state,findings,occurred_at
                  FROM copytrading.health_check_runs
                 WHERE check_kind='CODEX_REPAIR'
                 ORDER BY occurred_at DESC,health_run_id DESC LIMIT 1
                """,
                (),
            )
            row = cursor.fetchone()
    if row is None or not isinstance(row["findings"], dict):
        return None
    findings = row["findings"]
    changed = findings.get("changed_files")
    return {
        "state": str(row["state"]),
        "status": str(findings.get("status", "UNKNOWN"))[:16],
        "summary": str(findings.get("summary", ""))[:800],
        "root_cause": str(findings.get("root_cause", ""))[:1000],
        "changed_files": (
            [str(item)[:240] for item in changed[:20]] if isinstance(changed, list) else []
        ),
        "follow_up_required": bool(findings.get("follow_up_required", True)),
        "occurred_at": row["occurred_at"].astimezone(UTC).isoformat(),
    }


def _persist(
    database_url: str,
    document: dict[str, Any],
    *,
    report_digest: str,
    occurred_at: datetime,
    applied_actions: list[str],
) -> str:
    status = str(document["status"])
    state = {
        "HEALTHY": HealthState.HEALTHY.value,
        "DEGRADED": HealthState.DEGRADED.value,
        "CRITICAL": HealthState.FAILED.value,
    }[status]
    evidence = {"codex": document, "applied_actions": applied_actions}
    run_id = hashlib.sha256(
        f"CODEX_AUDIT:{report_digest}:{occurred_at.isoformat()}".encode()
    ).hexdigest()
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO copytrading.health_check_runs(
              health_run_id,check_kind,state,findings,evidence_hash,occurred_at
            ) VALUES (%s,'CODEX_AUDIT',%s,%s,%s,%s)
            """,
            (run_id, state, Jsonb(evidence), report_digest, occurred_at),
        )
        if status != "HEALTHY" or applied_actions:
            payload = {
                "event": "copy_codex_audit",
                "state": status,
                "summary": str(document["summary"]),
                "applied_actions": applied_actions,
                "occurred_at": occurred_at.isoformat(),
            }
            payload_hash = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            cursor.execute(
                """
                INSERT INTO control.outbox(
                  message_id,deduplication_key,topic,payload,payload_hash
                ) VALUES (%s,%s,'copy.telegram',%s,%s)
                ON CONFLICT (deduplication_key) DO NOTHING
                """,
                (
                    hashlib.sha256(f"audit-notification:{run_id}".encode()).hexdigest(),
                    f"copy-audit:{run_id}",
                    Jsonb(payload),
                    payload_hash,
                ),
            )
    return run_id


def _persist_failure(
    database_url: str,
    reason_code: str,
    *,
    occurred_at: datetime,
) -> str:
    """Persist one deduplicated alert while systemd retries the failed Codex invocation."""
    hour = occurred_at.replace(minute=0, second=0, microsecond=0)
    evidence = {
        "code": reason_code,
        "severity": "CRITICAL",
        "detail": "Codex CLI audit did not produce a valid structured report",
    }
    evidence_hash = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    run_id = hashlib.sha256(
        f"CODEX_AUDIT_FAILURE:{reason_code}:{hour.isoformat()}".encode()
    ).hexdigest()
    payload = {
        "event": "copy_codex_audit",
        "state": "CRITICAL",
        "summary": (
            f"Codex 自动审查执行失败 ({reason_code}); systemd 正在按 5 分钟间隔重试, "
            "确定性巡检与交易风控仍独立运行。"
        ),
        "applied_actions": [],
        "occurred_at": occurred_at.isoformat(),
    }
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO copytrading.health_check_runs(
              health_run_id,check_kind,state,findings,evidence_hash,occurred_at
            ) VALUES (%s,'CODEX_AUDIT','FAILED',%s,%s,%s)
            ON CONFLICT (health_run_id) DO NOTHING
            """,
            (run_id, Jsonb({"codex_failure": evidence}), evidence_hash, occurred_at),
        )
        cursor.execute(
            """
            INSERT INTO control.outbox(
              message_id,deduplication_key,topic,payload,payload_hash
            ) VALUES (%s,%s,'copy.telegram',%s,%s)
            ON CONFLICT (deduplication_key) DO NOTHING
            """,
            (
                hashlib.sha256(f"audit-failure-notification:{run_id}".encode()).hexdigest(),
                f"copy-audit-failure:{run_id}",
                Jsonb(payload),
                payload_hash,
            ),
        )
    return run_id


def main() -> int:
    arguments = _arguments()
    database_url = _private_text(
        arguments.database_url_file,
        arguments.repository_root,
        reason="COPY_DATABASE_URL_FILE_UNSAFE",
    )
    facts = PostgresHealthStore(database_url).read_facts()
    unit_states = {
        _POLLER_UNIT: _unit_state(_POLLER_UNIT),
        _TELEGRAM_UNIT: _unit_state(_TELEGRAM_UNIT),
    }
    recent_signal_errors = _recent_signal_errors(
        database_url,
        now=datetime.now(UTC),
    )
    recent_service_incidents = _recent_incidents(now=datetime.now(UTC))
    sanitized = {
        "schema_version": "1.0.0",
        "environment": f"BINANCE_USDM_{arguments.environment}",
        "production": "ACTIVE" if arguments.environment == "PRODUCTION" else "LOCKED",
        "active_leaders": facts.active_leaders,
        "assigned_leader_slots": facts.assigned_slots,
        "maximum_poll_age_seconds": facts.stale_poll_seconds,
        "latest_poll_failures": facts.latest_poll_failures,
        "latest_history_gap_failures": facts.history_gap_failures,
        "uncertain_signals": facts.uncertain_signals,
        "failed_signals_last_hour": facts.failed_signals_last_hour,
        "overdue_pending_entries": facts.overdue_pending_entries,
        "overdue_slot_replacements": facts.overdue_slot_replacements,
        "dead_notifications": facts.dead_notifications,
        "pending_notifications": facts.pending_notifications,
        "oldest_pending_notification_seconds": facts.oldest_pending_notification_seconds,
        "daily_selection_age_hours": facts.selection_age_hours,
        "short_selection_age_hours": facts.short_selection_age_hours,
        "long_selection_age_hours": facts.long_selection_age_hours,
        "runtime_control": facts.control_state.value,
        "virtual_position_groups": len(facts.virtual_positions),
        "latest_watchdog_age_seconds": facts.latest_watchdog_age_seconds,
        "latest_watchdog_state": facts.latest_watchdog_state,
        "latest_watchdog_finding_codes": list(facts.latest_watchdog_finding_codes),
        "service_states": unit_states,
        "recent_service_incidents": recent_service_incidents,
        "recent_signal_errors": recent_signal_errors,
        "recent_selection_failures": _recent_selection_failures(
            database_url,
            now=datetime.now(UTC),
        ),
        "latest_code_repair": _latest_repair(database_url),
    }
    try:
        auditor = CodexSystemAuditor(
            schema_path=arguments.schema_file,
            work_root=arguments.work_root,
        )
        result = auditor.audit(sanitized)
        refreshed_incidents = _recent_incidents(now=datetime.now(UTC))
        if _newly_resolved_incident_ids(recent_service_incidents, refreshed_incidents):
            # A one-shot recovery may finish during the model call. Re-run once with current
            # service evidence so an already-resolved fault is not published as a fresh alert.
            recent_service_incidents = refreshed_incidents
            sanitized["recent_service_incidents"] = recent_service_incidents
            sanitized["service_states"] = {
                _POLLER_UNIT: _unit_state(_POLLER_UNIT),
                _TELEGRAM_UNIT: _unit_state(_TELEGRAM_UNIT),
            }
            result = auditor.audit(sanitized)
    except CodexAuditError as error:
        now = datetime.now(UTC)
        reason_code = str(error)
        run_id = _persist_failure(database_url, reason_code, occurred_at=now)
        failure_evidence = {
            "schema_version": "1.0.0",
            "occurred_at": now.isoformat(),
            "audit_run_id": run_id,
            "state": "FAILED",
            "reason_code": reason_code,
        }
        arguments.evidence_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = arguments.evidence_file.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(failure_evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(arguments.evidence_file)
        print(
            json.dumps(
                {
                    "event": "copy_codex_audit_failure",
                    "audit_run_id": run_id,
                    "reason": reason_code,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        raise
    document = dict(result.document)
    recommended = document.get("recommended_actions")
    actions = [str(value) for value in recommended] if isinstance(recommended, list) else []
    applied: list[str] = []
    pause_requested = "PAUSE_NEW_ENTRIES" in actions or document.get("status") == "CRITICAL"
    if pause_requested and _pause_new_entries_justified(
        facts=facts,
        service_states=unit_states,
        recent_signal_errors=recent_signal_errors,
    ):
        repository = CopyTradingRepository(database_url)
        if repository.latest_runtime_control().state is RuntimeControlState.RUNNING:
            repository.append_runtime_control(
                RuntimeControlState.PAUSED_NEW_ENTRIES,
                actor_id="codex-hourly-audit",
                reason_codes=("COPY_CODEX_AUDIT_PAUSE",),
                occurred_at=datetime.now(UTC),
            )
            applied.append("PAUSE_NEW_ENTRIES")
    poll_stale = facts.stale_poll_seconds is None or facts.stale_poll_seconds > 120
    if (
        "RESTART_COPY_POLLER" in actions
        and (poll_stale or unit_states[_POLLER_UNIT] != "active")
        and _restart(_POLLER_UNIT)
    ):
        applied.append("RESTART_COPY_POLLER")
    if (
        "RESTART_TELEGRAM" in actions
        and unit_states[_TELEGRAM_UNIT] != "active"
        and _restart(_TELEGRAM_UNIT)
    ):
        applied.append("RESTART_TELEGRAM")
    if "RUN_CODE_REPAIR" in actions and _start_repair(
        {
            "schema_version": "1.0.0",
            "requested_at": datetime.now(UTC).isoformat(),
            "audit": document,
            "facts": sanitized,
        }
    ):
        applied.append("RUN_CODE_REPAIR")
    now = datetime.now(UTC)
    run_id = _persist(
        database_url,
        document,
        report_digest=result.report_digest,
        occurred_at=now,
        applied_actions=applied,
    )
    _persist_resolved_incidents(
        database_url,
        recent_service_incidents,
        occurred_at=now,
    )
    evidence = {
        "schema_version": "1.0.0",
        "occurred_at": now.isoformat(),
        "audit_run_id": run_id,
        "input_digest": result.input_digest,
        "report_digest": result.report_digest,
        "codex_decision": document,
        "applied_actions": applied,
    }
    arguments.evidence_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = arguments.evidence_file.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(arguments.evidence_file)
    print(json.dumps({"event": "copy_codex_audit", "audit_run_id": run_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
