"""Deterministic copy-trading health checks and fail-closed actions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ai_quant.copy_trading.models import PositionSide, RuntimeControlState, SignalKind
from ai_quant.copy_trading.risk import CopyAccountSnapshot, evaluate_account_risk


class HealthSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class HealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class HealthFinding:
    code: str
    severity: HealthSeverity
    detail: str


@dataclass(frozen=True, slots=True)
class DatabaseHealthFacts:
    active_leaders: int
    assigned_slots: int
    stale_poll_seconds: Decimal | None
    latest_poll_failures: int
    history_gap_failures: int
    uncertain_signals: int
    failed_signals_last_hour: int
    overdue_pending_entries: int
    overdue_slot_replacements: int
    dead_notifications: int
    pending_notifications: int
    oldest_pending_notification_seconds: Decimal | None
    selection_age_hours: Decimal | None
    short_selection_age_hours: Decimal | None
    long_selection_age_hours: Decimal | None
    latest_watchdog_age_seconds: Decimal | None
    latest_watchdog_state: str | None
    latest_watchdog_finding_codes: tuple[str, ...]
    latest_codex_audit_age_hours: Decimal | None
    latest_codex_audit_state: str | None
    control_state: RuntimeControlState
    envelope_baseline_usdt: Decimal | None
    virtual_positions: dict[tuple[str, PositionSide], Decimal]
    pending_entry_allowances: dict[tuple[str, PositionSide], Decimal] = field(default_factory=dict)
    repeated_minimum_rejections: int = 0


@dataclass(frozen=True, slots=True)
class HealthReport:
    state: HealthState
    findings: tuple[HealthFinding, ...]
    requested_control: RuntimeControlState | None


@dataclass(frozen=True, slots=True)
class HostHealthFacts:
    service_states: Mapping[str, str]
    root_free_bytes: int
    root_free_percent: Decimal
    memory_available_bytes: int
    backup_age_hours: Decimal | None = None
    user_stream_unit: str | None = "aiq-testnet-user-stream.service"

    def __post_init__(self) -> None:
        if (
            self.root_free_bytes < 0
            or not self.root_free_percent.is_finite()
            or not Decimal("0") <= self.root_free_percent <= Decimal("100")
            or self.memory_available_bytes < 0
            or (
                self.backup_age_hours is not None
                and (not self.backup_age_hours.is_finite() or self.backup_age_hours < 0)
            )
        ):
            raise ValueError("copy host health facts are invalid")


def evaluate_health(
    facts: DatabaseHealthFacts,
    account: CopyAccountSnapshot,
    exchange_positions: dict[tuple[str, PositionSide], Decimal],
    *,
    now: datetime,
    host: HostHealthFacts | None = None,
) -> HealthReport:
    _require_utc(now)
    findings: list[HealthFinding] = []
    requested: RuntimeControlState | None = None
    if facts.active_leaders == 0:
        findings.append(HealthFinding("COPY_NO_ACTIVE_LEADERS", HealthSeverity.WARNING, "active=0"))
    elif facts.stale_poll_seconds is None or facts.stale_poll_seconds > 120:
        findings.append(
            HealthFinding(
                "COPY_POLL_STALE",
                HealthSeverity.CRITICAL,
                f"max_age_seconds={facts.stale_poll_seconds}",
            )
        )
        requested = _stronger_control(requested, RuntimeControlState.PAUSED_NEW_ENTRIES)
    elif facts.stale_poll_seconds > 75:
        findings.append(
            HealthFinding(
                "COPY_POLL_DELAYED",
                HealthSeverity.WARNING,
                f"max_age_seconds={facts.stale_poll_seconds}",
            )
        )
    if facts.assigned_slots != 3:
        findings.append(
            HealthFinding(
                "COPY_LEADER_SLOTS_INCOMPLETE",
                HealthSeverity.WARNING,
                f"assigned={facts.assigned_slots}:expected=3",
            )
        )
    if facts.latest_poll_failures >= max(1, facts.active_leaders):
        findings.append(
            HealthFinding(
                "COPY_PUBLIC_POLL_FAILURES",
                HealthSeverity.CRITICAL,
                f"latest_failures={facts.latest_poll_failures}",
            )
        )
        requested = _stronger_control(requested, RuntimeControlState.PAUSED_NEW_ENTRIES)
    elif facts.latest_poll_failures:
        findings.append(
            HealthFinding(
                "COPY_PUBLIC_POLL_PARTIAL_FAILURE",
                HealthSeverity.WARNING,
                f"latest_failures={facts.latest_poll_failures}",
            )
        )
    if facts.history_gap_failures:
        findings.append(
            HealthFinding(
                "COPY_PUBLIC_HISTORY_GAP",
                HealthSeverity.CRITICAL,
                f"latest_gaps={facts.history_gap_failures}",
            )
        )
        requested = _stronger_control(requested, RuntimeControlState.PAUSED_NEW_ENTRIES)
    if facts.uncertain_signals:
        findings.append(
            HealthFinding(
                "COPY_UNCERTAIN_SUBMISSIONS",
                HealthSeverity.CRITICAL,
                f"count={facts.uncertain_signals}",
            )
        )
        requested = _stronger_control(requested, RuntimeControlState.PAUSED_NEW_ENTRIES)
    if facts.failed_signals_last_hour:
        repeated = facts.failed_signals_last_hour >= 3
        findings.append(
            HealthFinding(
                "COPY_RECENT_EXECUTION_FAILURES",
                HealthSeverity.CRITICAL if repeated else HealthSeverity.WARNING,
                f"last_hour={facts.failed_signals_last_hour}",
            )
        )
        if repeated:
            requested = _stronger_control(requested, RuntimeControlState.PAUSED_NEW_ENTRIES)
    if facts.repeated_minimum_rejections >= 3:
        findings.append(
            HealthFinding(
                "COPY_REPEATED_MINIMUM_REJECTIONS",
                HealthSeverity.WARNING,
                f"last_10_minutes={facts.repeated_minimum_rejections}",
            )
        )
    if facts.overdue_pending_entries:
        findings.append(
            HealthFinding(
                "COPY_PROTECTED_ENTRY_OVERDUE",
                HealthSeverity.CRITICAL,
                f"count={facts.overdue_pending_entries}",
            )
        )
        requested = _stronger_control(requested, RuntimeControlState.PAUSED_NEW_ENTRIES)
    if facts.overdue_slot_replacements:
        findings.append(
            HealthFinding(
                "COPY_SLOT_REPLACEMENT_RECONCILE_OVERDUE",
                HealthSeverity.WARNING,
                f"count={facts.overdue_slot_replacements}",
            )
        )
    if facts.dead_notifications:
        findings.append(
            HealthFinding(
                "COPY_DEAD_TELEGRAM_NOTIFICATIONS",
                HealthSeverity.WARNING,
                f"count={facts.dead_notifications}",
            )
        )
    notification_age = facts.oldest_pending_notification_seconds
    if facts.pending_notifications and notification_age is not None and notification_age > 120:
        blocked = notification_age > 600
        findings.append(
            HealthFinding(
                "COPY_TELEGRAM_OUTBOX_STALLED",
                HealthSeverity.CRITICAL if blocked else HealthSeverity.WARNING,
                f"pending={facts.pending_notifications}:oldest_seconds={notification_age}",
            )
        )
        if blocked:
            requested = _stronger_control(requested, RuntimeControlState.PAUSED_NEW_ENTRIES)
    if facts.short_selection_age_hours is None or facts.short_selection_age_hours > 36:
        findings.append(
            HealthFinding(
                "COPY_SHORT_SELECTION_STALE",
                HealthSeverity.WARNING,
                f"age_hours={facts.short_selection_age_hours}",
            )
        )
    if facts.long_selection_age_hours is None or facts.long_selection_age_hours > 192:
        findings.append(
            HealthFinding(
                "COPY_LONG_SELECTION_STALE",
                HealthSeverity.WARNING,
                f"age_hours={facts.long_selection_age_hours}",
            )
        )
    if facts.latest_codex_audit_age_hours is None or facts.latest_codex_audit_age_hours > 2:
        findings.append(
            HealthFinding(
                "COPY_CODEX_AUDIT_STALE",
                HealthSeverity.WARNING,
                f"age_hours={facts.latest_codex_audit_age_hours}",
            )
        )
    elif facts.latest_codex_audit_state == HealthState.FAILED.value:
        findings.append(
            HealthFinding(
                "COPY_CODEX_AUDIT_REPORTED_FAILURE",
                HealthSeverity.WARNING,
                "latest_state=FAILED",
            )
        )
    if not account.can_trade:
        findings.append(
            HealthFinding(
                "COPY_ACCOUNT_TRADING_DISABLED",
                HealthSeverity.CRITICAL,
                "can_trade=false",
            )
        )
        requested = _stronger_control(requested, RuntimeControlState.PAUSED_NEW_ENTRIES)
    if not account.hedge_mode:
        severity = (
            HealthSeverity.CRITICAL
            if facts.control_state is RuntimeControlState.RUNNING
            else HealthSeverity.WARNING
        )
        findings.append(HealthFinding("COPY_HEDGE_MODE_NOT_READY", severity, "hedge_mode=false"))
        if severity is HealthSeverity.CRITICAL:
            requested = _stronger_control(requested, RuntimeControlState.PAUSED_NEW_ENTRIES)
    risk = evaluate_account_risk(account, signal_kind=SignalKind.REDUCE, now=now)
    if risk.reduce_all_required:
        findings.append(
            HealthFinding(
                "COPY_ACCOUNT_EMERGENCY_RISK_LINE",
                HealthSeverity.CRITICAL,
                f"logical_margin={account.margin_balance_usdt}",
            )
        )
        requested = _stronger_control(requested, RuntimeControlState.REDUCE_ALL)
    elif risk.pause_new_entries and account.hedge_mode and account.can_trade:
        findings.append(
            HealthFinding(
                "COPY_ACCOUNT_WARNING_RISK_LINE",
                HealthSeverity.WARNING,
                f"logical_margin={account.margin_balance_usdt}",
            )
        )
        requested = _stronger_control(requested, RuntimeControlState.PAUSED_NEW_ENTRIES)
    keys = set(facts.virtual_positions) | set(exchange_positions)
    for key in sorted(keys, key=lambda value: (value[0], value[1].value)):
        virtual = facts.virtual_positions.get(key, Decimal("0"))
        exchange = exchange_positions.get(key, Decimal("0"))
        difference = exchange - virtual
        pending_allowance = facts.pending_entry_allowances.get(key, Decimal("0"))
        # Binance can fill an acknowledged protected LIMIT before the poller makes
        # that fill terminal and attributes it to the virtual ledgers.  The order's
        # durable expiry, rather than an arbitrary claim-age window, bounds this
        # expected in-flight state (short entries live for one hour, long entries
        # for one day).
        explained_by_pending_entry = (
            difference > 0
            and pending_allowance > 0
            and difference <= pending_allowance + Decimal("0.00000001")
        )
        if abs(difference) > Decimal("0.00000001") and not explained_by_pending_entry:
            findings.append(
                HealthFinding(
                    "COPY_POSITION_RECONCILIATION_MISMATCH",
                    HealthSeverity.CRITICAL,
                    f"{key[0]}:{key[1].value}:virtual={virtual}:exchange={exchange}",
                )
            )
            requested = _stronger_control(requested, RuntimeControlState.PAUSED_NEW_ENTRIES)
    if host is not None:
        for unit in ("aiq-copy-poller.service", "aiq-copy-telegram.service"):
            state = host.service_states.get(unit, "unknown")
            if state != "active":
                findings.append(
                    HealthFinding(
                        "COPY_REQUIRED_SERVICE_INACTIVE",
                        HealthSeverity.CRITICAL,
                        f"unit={unit}:state={state}",
                    )
                )
                requested = _stronger_control(
                    requested,
                    RuntimeControlState.PAUSED_NEW_ENTRIES,
                )
        stream_unit = host.user_stream_unit
        stream_state = (
            None if stream_unit is None else host.service_states.get(stream_unit, "unknown")
        )
        if stream_unit is not None and stream_state != "active":
            findings.append(
                HealthFinding(
                    "COPY_TESTNET_USER_STREAM_INACTIVE",
                    HealthSeverity.WARNING,
                    f"unit={stream_unit}:state={stream_state}",
                )
            )
        gibibyte = 1024**3
        if host.root_free_bytes < 2 * gibibyte or host.root_free_percent < Decimal("2"):
            findings.append(
                HealthFinding(
                    "COPY_HOST_DISK_CRITICAL",
                    HealthSeverity.CRITICAL,
                    f"free_bytes={host.root_free_bytes}:free_pct={host.root_free_percent}",
                )
            )
            requested = _stronger_control(requested, RuntimeControlState.PAUSED_NEW_ENTRIES)
        elif host.root_free_bytes < 10 * gibibyte or host.root_free_percent < Decimal("10"):
            findings.append(
                HealthFinding(
                    "COPY_HOST_DISK_LOW",
                    HealthSeverity.WARNING,
                    f"free_bytes={host.root_free_bytes}:free_pct={host.root_free_percent}",
                )
            )
        if host.memory_available_bytes < 256 * 1024**2:
            findings.append(
                HealthFinding(
                    "COPY_HOST_MEMORY_CRITICAL",
                    HealthSeverity.CRITICAL,
                    f"available_bytes={host.memory_available_bytes}",
                )
            )
            requested = _stronger_control(requested, RuntimeControlState.PAUSED_NEW_ENTRIES)
        elif host.memory_available_bytes < 512 * 1024**2:
            findings.append(
                HealthFinding(
                    "COPY_HOST_MEMORY_LOW",
                    HealthSeverity.WARNING,
                    f"available_bytes={host.memory_available_bytes}",
                )
            )
        if host.backup_age_hours is None:
            findings.append(
                HealthFinding(
                    "COPY_DATABASE_BACKUP_MISSING",
                    HealthSeverity.WARNING,
                    "latest_verified_backup=missing",
                )
            )
        elif host.backup_age_hours > 72:
            findings.append(
                HealthFinding(
                    "COPY_DATABASE_BACKUP_CRITICAL",
                    HealthSeverity.CRITICAL,
                    f"age_hours={host.backup_age_hours}",
                )
            )
            requested = _stronger_control(requested, RuntimeControlState.PAUSED_NEW_ENTRIES)
        elif host.backup_age_hours > 36:
            findings.append(
                HealthFinding(
                    "COPY_DATABASE_BACKUP_STALE",
                    HealthSeverity.WARNING,
                    f"age_hours={host.backup_age_hours}",
                )
            )
    state = (
        HealthState.FAILED
        if any(item.severity is HealthSeverity.CRITICAL for item in findings)
        else HealthState.DEGRADED
        if findings
        else HealthState.HEALTHY
    )
    return HealthReport(state=state, findings=tuple(findings), requested_control=requested)


def _stronger_control(
    current: RuntimeControlState | None,
    candidate: RuntimeControlState,
) -> RuntimeControlState:
    priority = {
        RuntimeControlState.RUNNING: 0,
        RuntimeControlState.PAUSED_NEW_ENTRIES: 1,
        RuntimeControlState.REDUCE_ALL: 2,
    }
    if current is None or priority[candidate] > priority[current]:
        return candidate
    return current


class PostgresHealthStore:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("copy health database DSN is required")
        self._dsn = dsn

    def read_facts(self) -> DatabaseHealthFacts:
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(_FACTS_SQL, ())
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("COPY_HEALTH_FACTS_MISSING")
                cursor.execute(_VIRTUAL_SQL, ())
                positions = {
                    (str(item["symbol"]), PositionSide(str(item["position_side"]))): Decimal(
                        str(item["quantity"])
                    )
                    for item in cursor.fetchall()
                    if Decimal(str(item["quantity"])) > 0
                }
                cursor.execute(_PENDING_ENTRY_ALLOWANCE_SQL, ())
                pending_entry_allowances = {
                    (str(item["symbol"]), PositionSide(str(item["position_side"]))): Decimal(
                        str(item["quantity"])
                    )
                    for item in cursor.fetchall()
                    if Decimal(str(item["quantity"])) > 0
                }
        return DatabaseHealthFacts(
            active_leaders=int(row["active_leaders"]),
            assigned_slots=int(row["assigned_slots"]),
            stale_poll_seconds=(
                None
                if row["stale_poll_seconds"] is None
                else Decimal(str(row["stale_poll_seconds"]))
            ),
            latest_poll_failures=int(row["latest_poll_failures"]),
            history_gap_failures=int(row["history_gap_failures"]),
            uncertain_signals=int(row["uncertain_signals"]),
            failed_signals_last_hour=int(row["failed_signals_last_hour"]),
            overdue_pending_entries=int(row["overdue_pending_entries"]),
            overdue_slot_replacements=int(row["overdue_slot_replacements"]),
            dead_notifications=int(row["dead_notifications"]),
            pending_notifications=int(row["pending_notifications"]),
            oldest_pending_notification_seconds=(
                None
                if row["oldest_pending_notification_seconds"] is None
                else Decimal(str(row["oldest_pending_notification_seconds"]))
            ),
            selection_age_hours=(
                None
                if row["selection_age_hours"] is None
                else Decimal(str(row["selection_age_hours"]))
            ),
            short_selection_age_hours=(
                None
                if row["short_selection_age_hours"] is None
                else Decimal(str(row["short_selection_age_hours"]))
            ),
            long_selection_age_hours=(
                None
                if row["long_selection_age_hours"] is None
                else Decimal(str(row["long_selection_age_hours"]))
            ),
            latest_watchdog_age_seconds=(
                None
                if row["latest_watchdog_age_seconds"] is None
                else Decimal(str(row["latest_watchdog_age_seconds"]))
            ),
            latest_watchdog_state=(
                None if row["latest_watchdog_state"] is None else str(row["latest_watchdog_state"])
            ),
            latest_watchdog_finding_codes=_finding_codes(row["latest_watchdog_findings"]),
            latest_codex_audit_age_hours=(
                None
                if row["latest_codex_audit_age_hours"] is None
                else Decimal(str(row["latest_codex_audit_age_hours"]))
            ),
            latest_codex_audit_state=(
                None
                if row["latest_codex_audit_state"] is None
                else str(row["latest_codex_audit_state"])
            ),
            control_state=RuntimeControlState(
                str(row["control_state"] or RuntimeControlState.PAUSED_NEW_ENTRIES.value)
            ),
            envelope_baseline_usdt=(
                None
                if row["envelope_baseline_usdt"] is None
                else Decimal(str(row["envelope_baseline_usdt"]))
            ),
            virtual_positions=positions,
            pending_entry_allowances=pending_entry_allowances,
            repeated_minimum_rejections=int(row["repeated_minimum_rejections"]),
        )

    def persist(
        self,
        report: HealthReport,
        *,
        occurred_at: datetime,
        codex_wakeup_requested: bool = False,
    ) -> str:
        _require_utc(occurred_at)
        findings = [
            {"code": item.code, "severity": item.severity.value, "detail": item.detail}
            for item in report.findings
        ]
        evidence_hash = _digest({"findings": findings, "state": report.state.value})
        run_id = _digest(
            {"check_kind": "WATCHDOG", "evidence_hash": evidence_hash, "occurred_at": occurred_at}
        )
        payload = {
            "event": "copy_health",
            "state": report.state.value,
            "findings": findings,
            "requested_control": (
                None if report.requested_control is None else report.requested_control.value
            ),
            "codex_wakeup_requested": codex_wakeup_requested,
            "occurred_at": occurred_at.isoformat(),
        }
        with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO copytrading.health_check_runs(
                      health_run_id,check_kind,state,findings,evidence_hash,occurred_at
                    ) VALUES (%s,'WATCHDOG',%s,%s,%s,%s)
                    """,
                    (run_id, report.state.value, Jsonb(findings), evidence_hash, occurred_at),
                )
                if report.state is not HealthState.HEALTHY:
                    cursor.execute(
                        """
                        INSERT INTO control.outbox(
                          message_id,deduplication_key,topic,payload,payload_hash
                        ) VALUES (%s,%s,'copy.telegram',%s,%s)
                        ON CONFLICT (deduplication_key) DO NOTHING
                        """,
                        (
                            _digest({"health_notification": run_id}),
                            f"copy-health:{run_id}",
                            Jsonb(payload),
                            _digest(payload),
                        ),
                    )
        return run_id


def logical_account_snapshot(
    account_v3: dict[str, Any],
    account_v2: dict[str, Any],
    position_mode: dict[str, Any],
    *,
    baseline_usdt: Decimal | None,
    observed_at: datetime,
) -> CopyAccountSnapshot:
    combined = dict(account_v3)
    combined["canTrade"] = account_v2.get("canTrade")
    raw = CopyAccountSnapshot.from_api(combined, position_mode, observed_at=observed_at)
    baseline = baseline_usdt or raw.margin_balance_usdt
    logical = max(Decimal("0"), Decimal("150") + raw.margin_balance_usdt - baseline)
    return CopyAccountSnapshot(
        observed_at=observed_at,
        hedge_mode=raw.hedge_mode,
        can_trade=raw.can_trade,
        wallet_balance_usdt=logical,
        margin_balance_usdt=logical,
        available_balance_usdt=min(raw.available_balance_usdt, logical),
        total_initial_margin_usdt=raw.total_initial_margin_usdt,
        total_maintenance_margin_usdt=raw.total_maintenance_margin_usdt,
    )


def exchange_positions_from_account(
    account_v2: dict[str, Any],
) -> dict[tuple[str, PositionSide], Decimal]:
    raw_positions = account_v2.get("positions")
    if not isinstance(raw_positions, list):
        raise ValueError("COPY_ACCOUNT_POSITIONS_INVALID")
    positions: dict[tuple[str, PositionSide], Decimal] = {}
    try:
        for item in raw_positions:
            if not isinstance(item, dict) or item.get("positionSide") not in {"LONG", "SHORT"}:
                continue
            quantity = abs(Decimal(str(item.get("positionAmt", "0"))))
            if not quantity.is_finite():
                raise ValueError
            if quantity > 0:
                positions[(str(item["symbol"]), PositionSide(str(item["positionSide"])))] = quantity
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ValueError("COPY_ACCOUNT_POSITIONS_INVALID") from error
    return positions


_FACTS_SQL = """
WITH lifecycle AS (
  SELECT DISTINCT ON (lead_portfolio_id) lead_portfolio_id,state
    FROM copytrading.leader_lifecycle_events
   ORDER BY lead_portfolio_id,occurred_at DESC,event_id DESC
), active AS (
  SELECT lead_portfolio_id FROM lifecycle
   WHERE state IN ('OBSERVE_ONLY','ACTIVE','DRAINING')
), latest_poll AS (
  SELECT active.lead_portfolio_id,poll.state,poll.occurred_at
    FROM active LEFT JOIN LATERAL (
      SELECT state,occurred_at FROM copytrading.poll_events
       WHERE lead_portfolio_id=active.lead_portfolio_id
       ORDER BY occurred_at DESC,poll_event_id DESC LIMIT 1
    ) AS poll ON true
), current_slots AS (
  SELECT DISTINCT ON (slot) slot,action,occurred_at
    FROM copytrading.leader_slot_events
   ORDER BY slot,occurred_at DESC,slot_event_id DESC
), latest_decision AS (
  SELECT DISTINCT ON (signal_id) signal_id,state,occurred_at
    FROM copytrading.signal_decision_events
   ORDER BY signal_id,occurred_at DESC,decision_event_id DESC
), latest_replacement AS (
  SELECT DISTINCT ON (replacement_id) replacement_id,state,expires_at
    FROM copytrading.slot_replacement_events
   ORDER BY replacement_id,occurred_at DESC,replacement_event_id DESC
), latest_watchdog AS (
  SELECT state,findings,occurred_at FROM copytrading.health_check_runs
   WHERE check_kind='WATCHDOG'
   ORDER BY occurred_at DESC,health_run_id DESC LIMIT 1
), latest_codex_audit AS (
  SELECT state,occurred_at FROM copytrading.health_check_runs
   WHERE check_kind='CODEX_AUDIT'
   ORDER BY occurred_at DESC,health_run_id DESC LIMIT 1
)
SELECT (SELECT count(*) FROM active) AS active_leaders,
       (SELECT count(*) FROM current_slots
         WHERE action='ASSIGNED'
           AND slot IN ('LONG_TERM','SHORT_TERM_1','SHORT_TERM_2')) AS assigned_slots,
       (SELECT max(extract(epoch FROM now()-occurred_at)) FROM latest_poll)
         AS stale_poll_seconds,
       (SELECT count(*) FROM latest_poll WHERE state IS DISTINCT FROM 'SUCCEEDED')
         AS latest_poll_failures,
       (SELECT count(*) FROM latest_poll WHERE state='HISTORY_GAP')
         AS history_gap_failures,
       (SELECT count(*) FROM latest_decision
         WHERE state='UNCERTAIN' AND occurred_at<now()-interval '2 minutes')
         AS uncertain_signals,
       (SELECT count(*) FROM latest_decision
         WHERE state='FAILED' AND occurred_at>=now()-interval '1 hour')
         AS failed_signals_last_hour,
       (SELECT count(*) FROM latest_decision
         WHERE state='IGNORED_MINIMUM'
           AND occurred_at>=now()-interval '10 minutes')
         AS repeated_minimum_rejections,
       (SELECT count(*) FROM copytrading.submission_claims AS claim
         JOIN latest_decision AS decision USING(signal_id)
         WHERE claim.order_type='LIMIT'
           AND decision.state IN ('SUBMITTED','UNCERTAIN')
           AND claim.expires_at<now()-interval '2 minutes')
         AS overdue_pending_entries,
       (SELECT count(*) FROM latest_replacement
         WHERE state='REQUESTED' AND expires_at<now()-interval '2 minutes')
         AS overdue_slot_replacements,
       (SELECT count(*) FROM control.outbox
         WHERE topic='copy.telegram' AND status='DEAD') AS dead_notifications,
       (SELECT count(*) FROM control.outbox
         WHERE topic='copy.telegram' AND status IN ('PENDING','CLAIMED'))
         AS pending_notifications,
       (SELECT max(extract(epoch FROM now()-created_at)) FROM control.outbox
         WHERE topic='copy.telegram' AND status IN ('PENDING','CLAIMED'))
         AS oldest_pending_notification_seconds,
       (SELECT extract(epoch FROM now()-max(occurred_at))/3600
          FROM copytrading.selection_runs WHERE state='COMPLETED') AS selection_age_hours,
       (SELECT extract(epoch FROM now()-greatest(
                 (SELECT max(occurred_at) FROM copytrading.selection_runs
                   WHERE state='COMPLETED' AND selection_kind IN ('SHORT_TERM','LEGACY')),
                 (SELECT min(occurred_at) FROM current_slots
                   WHERE action='ASSIGNED' AND slot IN ('SHORT_TERM_1','SHORT_TERM_2'))
               ))/3600) AS short_selection_age_hours,
       (SELECT extract(epoch FROM now()-greatest(
                 (SELECT max(occurred_at) FROM copytrading.selection_runs
                   WHERE state='COMPLETED' AND selection_kind='LONG_TERM'),
                 (SELECT max(occurred_at) FROM current_slots
                   WHERE action='ASSIGNED' AND slot='LONG_TERM')
               ))/3600) AS long_selection_age_hours,
       (SELECT extract(epoch FROM now()-occurred_at) FROM latest_watchdog)
         AS latest_watchdog_age_seconds,
       (SELECT state FROM latest_watchdog) AS latest_watchdog_state,
       (SELECT findings FROM latest_watchdog) AS latest_watchdog_findings,
       (SELECT extract(epoch FROM now()-occurred_at)/3600 FROM latest_codex_audit)
         AS latest_codex_audit_age_hours,
       (SELECT state FROM latest_codex_audit) AS latest_codex_audit_state,
       (SELECT state FROM copytrading.runtime_control_events
         ORDER BY occurred_at DESC,control_event_id DESC LIMIT 1) AS control_state,
       (SELECT exchange_margin_balance_usdt
          FROM copytrading.account_envelope_events
         ORDER BY occurred_at DESC,envelope_event_id DESC LIMIT 1)
         AS envelope_baseline_usdt
"""

_VIRTUAL_SQL = """
WITH latest AS (
  SELECT DISTINCT ON (lead_portfolio_id,symbol,position_side)
         lead_portfolio_id,symbol,position_side,resulting_local_quantity
    FROM copytrading.virtual_position_events
   ORDER BY lead_portfolio_id,symbol,position_side,occurred_at DESC,position_event_id DESC
)
SELECT symbol,position_side,sum(resulting_local_quantity) AS quantity
  FROM latest GROUP BY symbol,position_side
"""

_PENDING_ENTRY_ALLOWANCE_SQL = """
WITH latest_decision AS (
  SELECT DISTINCT ON (signal_id) signal_id,state
    FROM copytrading.signal_decision_events
   ORDER BY signal_id,occurred_at DESC,decision_event_id DESC
)
SELECT signal.symbol,signal.position_side,
       sum(claim.requested_quantity) AS quantity
  FROM copytrading.submission_claims AS claim
  JOIN copytrading.signals AS signal USING(signal_id)
  JOIN latest_decision AS decision USING(signal_id)
 WHERE signal.signal_kind='INCREASE'
   AND claim.order_type='LIMIT'
   AND decision.state IN ('SUBMITTED','UNCERTAIN')
   AND claim.expires_at>now()-interval '2 minutes'
 GROUP BY signal.symbol,signal.position_side
"""


def _digest(document: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _finding_codes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        str(item["code"])
        for item in value
        if isinstance(item, dict) and isinstance(item.get("code"), str)
    )


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("copy health time must be timezone-aware UTC")
