"""Persist, notify, and safely close an automatic Codex repair run."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess  # nosec B404 -- fixed systemctl path and unit
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ai_quant.copy_trading.health import PostgresHealthStore
from ai_quant.copy_trading.models import RuntimeControlState
from ai_quant.copy_trading.repository import CopyTradingRepository
from ai_quant.services.copy_trading import _private_text

_AUDIT_UNIT = "aiq-copy-codex-audit.service"
_SAFE_RESUME_ACTORS = frozenset({"codex-hourly-audit", "deterministic-watchdog"})
_BLOCKING_WARNING_CODES = frozenset({"COPY_ACCOUNT_WARNING_RISK_LINE"})


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Finalize automatic Codex repair evidence")
    parser.add_argument("--database-url-file", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    return parser.parse_args()


def _load(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("status") not in {
        "REPAIRED",
        "NO_CHANGE",
        "FAILED",
    }:
        raise ValueError("COPY_CODEX_REPAIR_EVIDENCE_INVALID")
    return document


def _latest_watchdog(database_url: str) -> tuple[str, list[dict[str, Any]]]:
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT state,findings FROM copytrading.health_check_runs
                 WHERE check_kind='WATCHDOG'
                 ORDER BY occurred_at DESC,health_run_id DESC LIMIT 1
                """,
                (),
            )
            row = cursor.fetchone()
    if row is None or not isinstance(row["findings"], list):
        return "FAILED", []
    return str(row["state"]), [item for item in row["findings"] if isinstance(item, dict)]


def _safe_to_resume(database_url: str, document: dict[str, Any]) -> bool:
    if document["status"] not in {"REPAIRED", "NO_CHANGE"}:
        return False
    if bool(document.get("follow_up_required", True)):
        return False
    watchdog_state, findings = _latest_watchdog(database_url)
    if watchdog_state == "FAILED":
        return False
    if any(
        item.get("severity") == "CRITICAL" or str(item.get("code")) in _BLOCKING_WARNING_CODES
        for item in findings
    ):
        return False
    facts = PostgresHealthStore(database_url).read_facts()
    return (
        facts.uncertain_signals == 0
        and facts.overdue_pending_entries == 0
        and facts.latest_poll_failures == 0
        and facts.history_gap_failures == 0
        and not facts.pending_entry_allowances
        and not _has_recoverable_signals(database_url)
    )


def _has_recoverable_signals(database_url: str) -> bool:
    return bool(CopyTradingRepository(database_url).recoverable_signals(limit=1))


def _persist(database_url: str, document: dict[str, Any], *, resumed: bool) -> str:
    occurred_at = datetime.now(UTC)
    status = str(document["status"])
    state = {"REPAIRED": "HEALTHY", "NO_CHANGE": "DEGRADED", "FAILED": "FAILED"}[status]
    evidence_hash = str(document.get("evidence_hash", ""))
    if len(evidence_hash) != 64:
        evidence_hash = hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    run_id = hashlib.sha256(
        f"CODEX_REPAIR:{evidence_hash}:{occurred_at.isoformat()}".encode()
    ).hexdigest()
    payload = {
        "event": "copy_codex_repair",
        "state": status,
        "summary": str(document.get("summary", ""))[:800],
        "root_cause": str(document.get("root_cause", ""))[:1000],
        "changed_files": document.get("changed_files", []),
        "verification": document.get("verification", []),
        "resumed": resumed,
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
            ) VALUES (%s,'CODEX_REPAIR',%s,%s,%s,%s)
            """,
            (run_id, state, Jsonb(document), evidence_hash, occurred_at),
        )
        cursor.execute(
            """
            INSERT INTO control.outbox(
              message_id,deduplication_key,topic,payload,payload_hash
            ) VALUES (%s,%s,'copy.telegram',%s,%s)
            ON CONFLICT (deduplication_key) DO NOTHING
            """,
            (
                hashlib.sha256(f"repair-notification:{run_id}".encode()).hexdigest(),
                f"copy-repair:{run_id}",
                Jsonb(payload),
                payload_hash,
            ),
        )
    return run_id


def _start_final_audit() -> None:
    # The repair's deterministic watchdog may already have launched an audit before the repair
    # result was persisted. Wait for that stale snapshot to finish, then request one final audit
    # that is guaranteed to see this CODEX_REPAIR row.
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            active = subprocess.run(  # noqa: S603  # nosec B603
                ["/usr/bin/systemctl", "is-active", "--quiet", _AUDIT_UNIT],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return
        if active.returncode != 0:
            break
        time.sleep(2)
    try:
        subprocess.run(  # noqa: S603  # nosec B603
            ["/usr/bin/systemctl", "start", "--no-block", _AUDIT_UNIT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return


def main() -> int:
    arguments = _arguments()
    database_url = _private_text(
        arguments.database_url_file,
        arguments.repository_root,
        reason="COPY_DATABASE_URL_FILE_UNSAFE",
    )
    document = _load(arguments.evidence_file)
    repository = CopyTradingRepository(database_url)
    current = repository.latest_runtime_control()
    resumed = False
    if (
        current.state is RuntimeControlState.PAUSED_NEW_ENTRIES
        and current.actor_id in _SAFE_RESUME_ACTORS
        and _safe_to_resume(database_url, document)
    ):
        repository.append_runtime_control(
            RuntimeControlState.RUNNING,
            actor_id="codex-auto-repair",
            reason_codes=("COPY_CODEX_REPAIR_VERIFIED",),
            occurred_at=datetime.now(UTC),
        )
        resumed = True
    run_id = _persist(database_url, document, resumed=resumed)
    _start_final_audit()
    print(
        json.dumps(
            {"event": "copy_codex_repair_finalized", "repair_run_id": run_id, "resumed": resumed},
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
