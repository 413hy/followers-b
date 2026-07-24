"""Transactional PostgreSQL repository for normalized copy-trading events."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ai_quant.copy_trading.allocation import DEFAULT_ENTRY_MARGIN_LIMIT_USDT, PortfolioUsage
from ai_quant.copy_trading.leader_slots import (
    CandidateActivity,
    LeaderSlot,
    SelectionStrategy,
    slot_replacement_wait,
)
from ai_quant.copy_trading.leader_symbol_stop import (
    LEADER_SYMBOL_STOP_COOLDOWN,
    LEADER_SYMBOL_STOP_LOSS_USDT,
    LeaderSymbolPositionPnl,
    aggregate_leader_symbol_pnl,
)
from ai_quant.copy_trading.ledger import VirtualPosition, VirtualPositionKey, VirtualPositionLedger
from ai_quant.copy_trading.models import (
    LeaderLifecycle,
    LeaderSnapshot,
    NormalizedSignal,
    OrderSide,
    PositionSide,
    PublicLeaderOrder,
    RuntimeControlState,
    SignalKind,
    SourcePositionSide,
)
from ai_quant.copy_trading.risk import logical_available_balance
from ai_quant.copy_trading.selection import CandidateAssessment
from ai_quant.copy_trading.selection_quality import LeaderPerformanceTrend


class CopyRepositoryError(RuntimeError):
    """Copy-trading persistence failed closed."""


@dataclass(frozen=True, slots=True)
class LeaderAssignment:
    lead_portfolio_id: str
    nickname: str
    lifecycle: LeaderLifecycle
    source_aum_usdt: Decimal
    portfolio_weight: Decimal
    slot: LeaderSlot | None = None
    follow_multiplier: int = 1


@dataclass(frozen=True, slots=True)
class RuntimeControl:
    event_id: str | None
    state: RuntimeControlState
    actor_id: str
    occurred_at: datetime | None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AccountPositionMark:
    symbol: str
    position_side: PositionSide
    exchange_quantity: Decimal
    mark_price: Decimal

    def __post_init__(self) -> None:
        if (
            not self.symbol
            or self.exchange_quantity <= 0
            or self.mark_price <= 0
            or not self.exchange_quantity.is_finite()
            or not self.mark_price.is_finite()
        ):
            raise ValueError("copy account position mark is invalid")


@dataclass(frozen=True, slots=True)
class LeaderSymbolStop:
    stop_event_id: str
    lead_portfolio_id: str
    leader_nickname: str
    symbol: str
    net_position_pnl_usdt: Decimal
    loss_limit_usdt: Decimal
    triggered_at: datetime
    blocked_until: datetime
    newly_triggered: bool = False


class CopyTradingRepository:
    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError("copy repository DSN is required")
        self._dsn = dsn

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def reset_pnl_baseline(
        self,
        *,
        actor_id: str,
        occurred_at: datetime,
    ) -> str:
        """Rebase logical capital and PnL without mutating orders or positions."""

        _require_utc(occurred_at)
        if not actor_id or len(actor_id) > 128 or "\n" in actor_id or "\r" in actor_id:
            raise ValueError("copy PnL reset actor is invalid")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("copy-pnl-reset",),
                )
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("copy-account-envelope",),
                )
                cursor.execute(
                    """
                    SELECT valuation_event_id,observed_at,
                           exchange_margin_balance_usdt,exchange_available_balance_usdt,
                           operating_envelope_usdt,total_initial_margin_usdt
                      FROM copytrading.account_valuation_events
                     ORDER BY observed_at DESC,valuation_event_id DESC LIMIT 1
                     FOR SHARE
                    """,
                    (),
                )
                anchor = cursor.fetchone()
                if anchor is None:
                    raise CopyRepositoryError("COPY_PNL_RESET_VALUATION_MISSING")
                anchor_observed_at = anchor["observed_at"]
                if not isinstance(anchor_observed_at, datetime) or (
                    occurred_at - anchor_observed_at
                ) > timedelta(minutes=2):
                    raise CopyRepositoryError("COPY_PNL_RESET_VALUATION_STALE")
                valuation_event_id = str(anchor["valuation_event_id"])
                exchange_margin_balance_usdt = Decimal(str(anchor["exchange_margin_balance_usdt"]))
                exchange_available_balance_usdt = Decimal(
                    str(anchor["exchange_available_balance_usdt"])
                )
                operating_envelope_usdt = Decimal(str(anchor["operating_envelope_usdt"]))
                total_initial_margin_usdt = Decimal(str(anchor["total_initial_margin_usdt"]))
                if (
                    not exchange_margin_balance_usdt.is_finite()
                    or exchange_margin_balance_usdt < 0
                    or not exchange_available_balance_usdt.is_finite()
                    or exchange_available_balance_usdt < 0
                    or not operating_envelope_usdt.is_finite()
                    or operating_envelope_usdt <= 0
                    or not total_initial_margin_usdt.is_finite()
                    or total_initial_margin_usdt < 0
                ):
                    raise CopyRepositoryError("COPY_PNL_RESET_VALUATION_INVALID")
                cursor.execute(
                    """
                    WITH latest AS (
                      SELECT DISTINCT ON (lead_portfolio_id,symbol,position_side)
                             lead_portfolio_id,symbol,position_side,resulting_quantity
                        FROM copytrading.leader_pnl_events
                       WHERE observed_at<=%s
                       ORDER BY lead_portfolio_id,symbol,position_side,
                                observed_at DESC,pnl_event_id DESC
                    )
                    SELECT count(*) AS missing_marks
                      FROM latest
                      LEFT JOIN copytrading.account_position_mark_events AS mark
                        ON mark.valuation_event_id=%s
                       AND mark.symbol=latest.symbol
                       AND mark.position_side=latest.position_side
                     WHERE latest.resulting_quantity>0 AND mark.mark_event_id IS NULL
                    """,
                    (anchor_observed_at, valuation_event_id),
                )
                missing = cursor.fetchone()
                if missing is None or int(missing["missing_marks"]) != 0:
                    raise CopyRepositoryError("COPY_PNL_RESET_MARKS_INCOMPLETE")
                reset_event_id = _digest(
                    {
                        "actor_id": actor_id,
                        "occurred_at": occurred_at.isoformat(),
                        "type": "copy-pnl-reset",
                        "valuation_event_id": valuation_event_id,
                    }
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.pnl_reset_events(
                      reset_event_id,valuation_event_id,actor_id,reason_codes,occurred_at
                    ) VALUES (%s,%s,%s,%s,%s)
                    """,
                    (
                        reset_event_id,
                        valuation_event_id,
                        actor_id,
                        Jsonb(["COPY_PNL_PRESENTATION_RESET"]),
                        occurred_at,
                    ),
                )
                envelope_event_id = _digest(
                    {
                        "exchange_margin_balance_usdt": str(exchange_margin_balance_usdt),
                        "operating_envelope_usdt": str(operating_envelope_usdt),
                        "reset_event_id": reset_event_id,
                        "type": "copy-account-envelope-reset",
                    }
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.account_envelope_events(
                      envelope_event_id,event_type,operating_envelope_usdt,
                      exchange_margin_balance_usdt,reason_codes,occurred_at
                    ) VALUES (%s,'RESET',%s,%s,%s,%s)
                    """,
                    (
                        envelope_event_id,
                        operating_envelope_usdt,
                        exchange_margin_balance_usdt,
                        Jsonb(["COPY_ACCOUNT_ENVELOPE_RESET"]),
                        occurred_at,
                    ),
                )
                cursor.execute(
                    """
                    WITH latest AS (
                      SELECT DISTINCT ON (lead_portfolio_id,symbol,position_side)
                             pnl_event_id,lead_portfolio_id,symbol,position_side,
                             resulting_quantity,resulting_average_entry_price,
                             observed_at
                        FROM copytrading.leader_pnl_events
                       WHERE observed_at<=%s
                       ORDER BY lead_portfolio_id,symbol,position_side,
                                observed_at DESC,pnl_event_id DESC
                    )
                    INSERT INTO copytrading.pnl_position_reset_anchors(
                      reset_event_id,lead_portfolio_id,symbol,position_side,
                      cycle_realized_pnl_usdt,unrealized_pnl_usdt
                    )
                    SELECT %s,latest.lead_portfolio_id,latest.symbol,latest.position_side,
                           cycle.realized_pnl_usdt,
                           (mark.mark_price-latest.resulting_average_entry_price)
                             *latest.resulting_quantity
                             *CASE latest.position_side WHEN 'LONG' THEN 1 ELSE -1 END
                      FROM latest
                      JOIN copytrading.account_position_mark_events AS mark
                        ON mark.valuation_event_id=%s
                       AND mark.symbol=latest.symbol
                       AND mark.position_side=latest.position_side
                      JOIN LATERAL (
                        SELECT started.observed_at,started.pnl_event_id
                          FROM copytrading.leader_pnl_events AS started
                         WHERE started.lead_portfolio_id=latest.lead_portfolio_id
                           AND started.symbol=latest.symbol
                           AND started.position_side=latest.position_side
                           AND started.previous_quantity=0
                           AND started.resulting_quantity>0
                           AND (started.observed_at,started.pnl_event_id)
                               <= (latest.observed_at,latest.pnl_event_id)
                         ORDER BY started.observed_at DESC,started.pnl_event_id DESC LIMIT 1
                      ) AS cycle_start ON true
                      JOIN LATERAL (
                        SELECT coalesce(sum(event.realized_pnl_delta_usdt),0)
                                 AS realized_pnl_usdt
                          FROM copytrading.leader_pnl_events AS event
                         WHERE event.lead_portfolio_id=latest.lead_portfolio_id
                           AND event.symbol=latest.symbol
                           AND event.position_side=latest.position_side
                           AND (event.observed_at,event.pnl_event_id)
                               >= (cycle_start.observed_at,cycle_start.pnl_event_id)
                           AND (event.observed_at,event.pnl_event_id)
                               <= (latest.observed_at,latest.pnl_event_id)
                      ) AS cycle ON true
                     WHERE latest.resulting_quantity>0
                    """,
                    (
                        anchor_observed_at,
                        reset_event_id,
                        valuation_event_id,
                    ),
                )
                payload = {
                    "event": "copy_pnl_reset",
                    "state": "RESET",
                    "occurred_at": occurred_at.isoformat(),
                    "operating_envelope_usdt": str(operating_envelope_usdt),
                    "logical_available_usdt": str(
                        logical_available_balance(
                            exchange_available_balance_usdt=(exchange_available_balance_usdt),
                            logical_equity_usdt=operating_envelope_usdt,
                            total_initial_margin_usdt=total_initial_margin_usdt,
                        )
                    ),
                    "total_initial_margin_usdt": str(total_initial_margin_usdt),
                    "reason_codes": [
                        "COPY_PNL_PRESENTATION_RESET",
                        "COPY_ACCOUNT_ENVELOPE_RESET",
                    ],
                    "summary": (
                        "交易资金净值已按初始额度重新计算; 账户未占用资金已扣除"
                        "当前真实占用后重新计算; 可用开仓保证金余额继续受共享开仓"
                        "上限约束; 系统总盈亏、各条线、"
                        "各带单员及当前仓位的盈亏统计已从现在重新计为 0; "
                        "当前仓位、订单、带单员配置和历史审计记录均未修改, "
                        "已有仓位仍会正常占用保证金额度"
                    ),
                }
                payload_hash = _digest(payload)
                cursor.execute(
                    """
                    INSERT INTO control.outbox(
                      message_id,deduplication_key,topic,payload,payload_hash
                    ) VALUES (%s,%s,'copy.telegram',%s,%s)
                    """,
                    (
                        _digest({"pnl_reset": reset_event_id}),
                        f"copy-pnl-reset:{reset_event_id}",
                        Jsonb(payload),
                        payload_hash,
                    ),
                )
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_PNL_RESET_WRITE_FAILED") from error
        return reset_event_id

    def record_leader_snapshot(
        self,
        snapshot: LeaderSnapshot,
        *,
        observed_at: datetime,
    ) -> None:
        _require_utc(observed_at)
        snapshot_id = _digest(
            {
                "lead_portfolio_id": snapshot.lead_portfolio_id,
                "observed_at": observed_at.isoformat(),
                "payload_hash": snapshot.raw_payload_hash,
            }
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO copytrading.leader_snapshots(
                      snapshot_id,lead_portfolio_id,nickname,roi_pct,pnl_usdt,aum_usdt,
                      maximum_drawdown_pct,win_rate_pct,current_copy_count,
                      maximum_copy_count,source_payload_hash,observed_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        snapshot_id,
                        snapshot.lead_portfolio_id,
                        snapshot.nickname,
                        snapshot.roi_pct,
                        snapshot.pnl_usdt,
                        snapshot.aum_usdt,
                        snapshot.maximum_drawdown_pct,
                        snapshot.win_rate_pct,
                        snapshot.current_copy_count,
                        snapshot.maximum_copy_count,
                        snapshot.raw_payload_hash,
                        observed_at,
                    ),
                )
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_LEADER_SNAPSHOT_WRITE_FAILED") from error

    def leader_selection_trend(
        self,
        leader: LeaderSnapshot,
        *,
        observed_at: datetime,
        minimum_baseline_age: timedelta = timedelta(hours=12),
        maximum_baseline_age: timedelta = timedelta(days=7),
    ) -> LeaderPerformanceTrend | None:
        """Compare a candidate with its latest sufficiently old durable snapshot."""

        _require_utc(observed_at)
        if not timedelta(hours=1) <= minimum_baseline_age < maximum_baseline_age:
            raise ValueError("copy leader trend window is invalid")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT roi_pct,pnl_usdt,aum_usdt,maximum_drawdown_pct,observed_at
                      FROM copytrading.leader_snapshots
                     WHERE lead_portfolio_id=%s
                       AND observed_at<=%s
                       AND observed_at>=%s
                     ORDER BY observed_at DESC,snapshot_id DESC LIMIT 1
                    """,
                    (
                        leader.lead_portfolio_id,
                        observed_at - minimum_baseline_age,
                        observed_at - maximum_baseline_age,
                    ),
                )
                row = cursor.fetchone()
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_LEADER_TREND_READ_FAILED") from error
        if row is None:
            return None
        baseline_at = row["observed_at"]
        if not isinstance(baseline_at, datetime):
            raise CopyRepositoryError("COPY_LEADER_TREND_INVALID")
        return LeaderPerformanceTrend(
            baseline_age_hours=(
                Decimal(str((observed_at - baseline_at).total_seconds())) / Decimal("3600")
            ).quantize(Decimal("0.000001")),
            roi_change_pct=_relative_change_pct(
                leader.roi_pct,
                Decimal(str(row["roi_pct"])),
            ),
            pnl_change_pct=_relative_change_pct(
                leader.pnl_usdt,
                Decimal(str(row["pnl_usdt"])),
            ),
            aum_change_pct=_relative_change_pct(
                leader.aum_usdt,
                Decimal(str(row["aum_usdt"])),
            ),
            maximum_drawdown_change_points=(
                leader.maximum_drawdown_pct - Decimal(str(row["maximum_drawdown_pct"]))
            ).quantize(Decimal("0.000001")),
        )

    def recently_manually_cleared_leader_ids(
        self,
        *,
        since: datetime,
    ) -> frozenset[str]:
        """Leaders explicitly cleared by the operator inside a selection cooldown."""

        _require_utc(since)
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT challenge.lead_portfolio_id
                      FROM copytrading.telegram_leader_position_close_consumptions AS used
                      JOIN copytrading.telegram_leader_position_close_challenges AS challenge
                        USING(challenge_id)
                     WHERE used.consumed_at>=%s
                    """,
                    (since,),
                )
                rows = cursor.fetchall()
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_MANUAL_CLEAR_COOLDOWN_READ_FAILED") from error
        return frozenset(str(row["lead_portfolio_id"]) for row in rows)

    def append_lifecycle(
        self,
        lead_portfolio_id: str,
        lifecycle: LeaderLifecycle,
        *,
        occurred_at: datetime,
        reason_codes: tuple[str, ...],
        selection_run_id: str | None = None,
    ) -> None:
        _require_utc(occurred_at)
        event_id = _digest(
            {
                "lead_portfolio_id": lead_portfolio_id,
                "occurred_at": occurred_at.isoformat(),
                "selection_run_id": selection_run_id,
                "state": lifecycle.value,
            }
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO copytrading.leader_lifecycle_events(
                      event_id,lead_portfolio_id,state,selection_run_id,reason_codes,occurred_at
                    ) VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        event_id,
                        lead_portfolio_id,
                        lifecycle.value,
                        selection_run_id,
                        Jsonb(list(reason_codes)),
                        occurred_at,
                    ),
                )
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_LIFECYCLE_WRITE_FAILED") from error

    def record_candidate_activity(self, activity: CandidateActivity) -> None:
        _require_utc(activity.observed_at)
        evidence = {
            "active_days_7d": activity.active_days_7d,
            "latest_operation_time_ms": activity.latest_operation_time_ms,
            "lead_portfolio_id": activity.lead_portfolio_id,
            "losing_close_count": activity.losing_close_count,
            "observed_at": activity.observed_at.isoformat(),
            "orders_1d": activity.orders_1d,
            "orders_3d": activity.orders_3d,
            "orders_7d": activity.orders_7d,
            "profitable_close_count": activity.profitable_close_count,
            "sample_order_count": activity.sample_order_count,
            "testnet_symbol_compatibility_pct": (activity.testnet_symbol_compatibility_pct),
        }
        activity_snapshot_id = _digest(evidence)
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO copytrading.leader_activity_snapshots(
                      activity_snapshot_id,lead_portfolio_id,sample_order_count,
                      orders_1d,orders_3d,orders_7d,active_days_7d,
                      latest_operation_time_ms,profitable_close_count,
                      losing_close_count,testnet_symbol_compatibility_pct,
                      evidence_hash,observed_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        activity_snapshot_id,
                        activity.lead_portfolio_id,
                        activity.sample_order_count,
                        activity.orders_1d,
                        activity.orders_3d,
                        activity.orders_7d,
                        activity.active_days_7d,
                        activity.latest_operation_time_ms,
                        activity.profitable_close_count,
                        activity.losing_close_count,
                        activity.testnet_symbol_compatibility_pct,
                        _digest(evidence),
                        activity.observed_at,
                    ),
                )
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_LEADER_ACTIVITY_WRITE_FAILED") from error

    def current_slot_assignments(self) -> Mapping[LeaderSlot, str]:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT ON (slot) slot,action,lead_portfolio_id
                      FROM copytrading.leader_slot_events
                     ORDER BY slot,occurred_at DESC,slot_event_id DESC
                    """,
                    (),
                )
                rows = list(cursor.fetchall())
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_LEADER_SLOT_READ_FAILED") from error
        return {
            LeaderSlot(str(row["slot"])): str(row["lead_portfolio_id"])
            for row in rows
            if row["action"] == "ASSIGNED" and row["lead_portfolio_id"] is not None
        }

    def record_leader_availability(
        self,
        *,
        slot: LeaderSlot,
        lead_portfolio_id: str,
        state: str,
        public_directory_total: int,
        valid_directory_total: int,
        invalid_row_count: int,
        observed_at: datetime,
    ) -> bool:
        """Record one status observation and enqueue only the first alert in a missing episode."""

        _require_utc(observed_at)
        if (
            not re.fullmatch(r"[0-9]{10,24}", lead_portfolio_id)
            or state not in {"AVAILABLE", "MISSING"}
            or public_directory_total <= 0
            or valid_directory_total < 0
            or invalid_row_count < 0
        ):
            raise ValueError("copy leader availability observation is invalid")
        reason_codes = (
            ["COPY_LEADER_PUBLIC_PROJECT_AVAILABLE"]
            if state == "AVAILABLE"
            else ["COPY_LEADER_PUBLIC_PROJECT_MISSING"]
        )
        evidence = {
            "slot": slot.value,
            "lead_portfolio_id": lead_portfolio_id,
            "state": state,
            "public_directory_total": public_directory_total,
            "valid_directory_total": valid_directory_total,
            "invalid_row_count": invalid_row_count,
            "reason_codes": reason_codes,
            "observed_at": observed_at.isoformat(),
        }
        event_id = _digest(evidence)
        alert_created = False
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                # Serialize against Telegram and selector slot changes. A leader that was
                # manually replaced while the directory was being read must not generate
                # a stale disappearance alert for its former slot.
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("copy-leader-slots",),
                )
                cursor.execute(
                    """
                    SELECT action,lead_portfolio_id,occurred_at
                      FROM copytrading.leader_slot_events
                     WHERE slot=%s
                     ORDER BY occurred_at DESC,slot_event_id DESC LIMIT 1
                    """,
                    (slot.value,),
                )
                assignment = cursor.fetchone()
                if (
                    assignment is None
                    or assignment["action"] != "ASSIGNED"
                    or str(assignment["lead_portfolio_id"]) != lead_portfolio_id
                ):
                    return False
                cursor.execute(
                    """
                    SELECT state FROM copytrading.leader_availability_events
                     WHERE slot=%s AND lead_portfolio_id=%s AND observed_at >= %s
                     ORDER BY observed_at DESC,availability_event_id DESC LIMIT 1
                    """,
                    (slot.value, lead_portfolio_id, assignment["occurred_at"]),
                )
                previous = cursor.fetchone()
                previous_state = None if previous is None else str(previous["state"])
                cursor.execute(
                    """
                    INSERT INTO copytrading.leader_availability_events(
                      availability_event_id,slot,lead_portfolio_id,state,
                      public_directory_total,valid_directory_total,invalid_row_count,
                      reason_codes,evidence_hash,observed_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        event_id,
                        slot.value,
                        lead_portfolio_id,
                        state,
                        public_directory_total,
                        valid_directory_total,
                        invalid_row_count,
                        Jsonb(reason_codes),
                        _digest(evidence),
                        observed_at,
                    ),
                )
                if state != "MISSING" or previous_state == "MISSING":
                    return False
                payload = {
                    "event": "copy_leader_availability_alert",
                    "state": "MISSING",
                    "slot": slot.value,
                    "lead_portfolio_id": lead_portfolio_id,
                    "nickname": _leader_nickname(cursor, lead_portfolio_id),
                    "checked_at": observed_at.isoformat(),
                    "reason_codes": reason_codes,
                }
                cursor.execute(
                    """
                    INSERT INTO control.outbox(
                      message_id,deduplication_key,topic,payload,payload_hash
                    ) VALUES (%s,%s,'copy.telegram',%s,%s)
                    ON CONFLICT (deduplication_key) DO NOTHING
                    """,
                    (
                        _digest({"leader_availability_alert": event_id}),
                        f"copy-leader-availability-alert:{event_id}",
                        Jsonb(payload),
                        _digest(payload),
                    ),
                )
                alert_created = cursor.rowcount == 1
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_LEADER_AVAILABILITY_WRITE_FAILED") from error
        return alert_created

    def current_locked_leader_ids(self) -> frozenset[str]:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                return _locked_leader_ids(cursor)
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_LEADER_LOCK_READ_FAILED") from error

    def apply_slot_selection(
        self,
        candidates: tuple[LeaderSnapshot, ...],
        assessments: Mapping[str, CandidateAssessment],
        selected_leader_ids: tuple[str, ...],
        *,
        strategy: SelectionStrategy,
        scheduled_for: datetime,
        data_cutoff: datetime,
        candidate_digest: str,
        policy_digest: str,
        codex_report_digest: str,
        occurred_at: datetime,
        backup_leader_ids: Mapping[LeaderSlot, str] | None = None,
    ) -> str:
        for value in (scheduled_for, data_cutoff, occurred_at):
            _require_utc(value)
        target_slots = strategy.slots
        candidate_by_id = {item.lead_portfolio_id: item for item in candidates}
        backups = dict(backup_leader_ids or {})
        if (
            len(selected_leader_ids) != len(target_slots)
            or len(set(selected_leader_ids)) != len(selected_leader_ids)
            or not set(backups) <= set(target_slots)
            or any(not leader_id for leader_id in backups.values())
        ):
            raise ValueError("copy slot selection leader IDs are invalid")
        selection_run_id = _digest(
            {
                "candidate_digest": candidate_digest,
                "policy_digest": policy_digest,
                "scheduled_for": scheduled_for.isoformat(),
                "strategy": strategy.value,
                "type": "slot-copy-leader-selection",
            }
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("copy-leader-slots",),
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.selection_runs(
                      selection_run_id,scheduled_for,data_cutoff,candidate_digest,
                      policy_digest,codex_report_digest,state,reason_codes,
                      occurred_at,selection_kind
                    ) VALUES (%s,%s,%s,%s,%s,%s,'COMPLETED',%s,%s,%s)
                    ON CONFLICT (selection_run_id) DO NOTHING
                    RETURNING selection_run_id
                    """,
                    (
                        selection_run_id,
                        scheduled_for,
                        data_cutoff,
                        candidate_digest,
                        policy_digest,
                        codex_report_digest,
                        Jsonb([f"COPY_{strategy.value}_SELECTION_COMPLETED"]),
                        occurred_at,
                        strategy.value,
                    ),
                )
                if cursor.fetchone() is None:
                    return selection_run_id
                cursor.execute(
                    """
                    SELECT DISTINCT ON (lead_portfolio_id) lead_portfolio_id,state
                      FROM copytrading.leader_lifecycle_events
                     ORDER BY lead_portfolio_id,occurred_at DESC,event_id DESC
                    """,
                    (),
                )
                current_states = {
                    str(row["lead_portfolio_id"]): LeaderLifecycle(str(row["state"]))
                    for row in cursor.fetchall()
                }
                leaders_with_positions = _leaders_with_owned_exposure(cursor)
                if any(
                    current_states.get(leader_id) is LeaderLifecycle.DRAINING
                    and leader_id in leaders_with_positions
                    for leader_id in selected_leader_ids
                ):
                    raise ValueError("copy draining leader with a position cannot be reassigned")
                cursor.execute(
                    """
                    SELECT DISTINCT ON (slot) slot,action,lead_portfolio_id
                      FROM copytrading.leader_slot_events
                     ORDER BY slot,occurred_at DESC,slot_event_id DESC
                    """,
                    (),
                )
                current_slots = {
                    LeaderSlot(str(row["slot"])): str(row["lead_portfolio_id"])
                    for row in cursor.fetchall()
                    if row["action"] == "ASSIGNED" and row["lead_portfolio_id"] is not None
                }
                locked_leader_ids = _locked_leader_ids(cursor)
                locked_incumbents = {
                    leader_id
                    for slot, leader_id in current_slots.items()
                    if slot in target_slots and leader_id in locked_leader_ids
                }
                if set(selected_leader_ids) - set(candidate_by_id) - locked_incumbents:
                    raise ValueError("copy slot selection leader IDs are invalid")
                for slot, backup_id in backups.items():
                    incumbent = current_slots.get(slot)
                    if (
                        incumbent is None
                        or incumbent not in locked_leader_ids
                        or backup_id == incumbent
                        or backup_id not in candidate_by_id
                    ):
                        raise ValueError("copy locked slot backup is invalid")
                leaders_in_other_slots = {
                    leader_id
                    for slot, leader_id in current_slots.items()
                    if slot not in target_slots
                }
                if set(selected_leader_ids) & leaders_in_other_slots:
                    raise ValueError("copy leader cannot occupy multiple slots")
                slot_assignments = tuple(zip(target_slots, selected_leader_ids, strict=True))
                if any(
                    current_slots.get(other_slot) == leader_id
                    for slot, leader_id in slot_assignments
                    for other_slot in target_slots
                    if other_slot is not slot
                ):
                    raise ValueError("copy leader cannot rotate between target slots")
                final_slots = dict(current_slots)
                direct_assignments: list[tuple[LeaderSlot, str]] = []
                selected_direct: set[str] = set()
                displaced: set[str] = set()
                replacement_results: list[dict[str, Any]] = []
                selected_status: dict[str, str] = {}
                blocked_by_lock: set[str] = set()
                for slot, leader_id in slot_assignments:
                    _supersede_open_slot_replacements(
                        cursor,
                        slot=slot,
                        superseding_selection_run_id=selection_run_id,
                        occurred_at=occurred_at,
                    )
                    incumbent = current_slots.get(slot)
                    if incumbent == leader_id:
                        locked_backup_id = backups.get(slot)
                        if locked_backup_id is not None:
                            cursor.execute(
                                """
                                INSERT INTO copytrading.leader_slot_backup_events(
                                  backup_event_id,selection_run_id,slot,
                                  incumbent_lead_portfolio_id,backup_lead_portfolio_id,
                                  actor_id,reason_codes,occurred_at
                                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                                """,
                                (
                                    _digest(
                                        {
                                            "backup_leader_id": locked_backup_id,
                                            "selection_run_id": selection_run_id,
                                            "slot": slot.value,
                                        }
                                    ),
                                    selection_run_id,
                                    slot.value,
                                    incumbent,
                                    locked_backup_id,
                                    f"selector:{strategy.value.lower()}",
                                    Jsonb(["COPY_SELECTION_LOCKED_SLOT_BACKUP_SELECTED"]),
                                    occurred_at,
                                ),
                            )
                        selected_direct.add(leader_id)
                        selected_status[leader_id] = (
                            "LOCKED_UNCHANGED" if leader_id in locked_leader_ids else "UNCHANGED"
                        )
                        replacement_results.append(
                            {
                                "slot": slot.value,
                                "status": selected_status[leader_id],
                                "incumbent_lead_portfolio_id": incumbent,
                                "incumbent_nickname": _leader_nickname(cursor, leader_id),
                                "candidate_lead_portfolio_id": leader_id,
                                "candidate_nickname": _leader_nickname(cursor, leader_id),
                                "backup_lead_portfolio_id": locked_backup_id,
                                "backup_nickname": (
                                    None
                                    if locked_backup_id is None
                                    else _leader_nickname(cursor, locked_backup_id)
                                ),
                                "expires_at": None,
                            }
                        )
                        continue
                    if incumbent is not None and incumbent in locked_leader_ids:
                        blocked_by_lock.add(leader_id)
                        selected_status[leader_id] = "BLOCKED_BY_LEADER_LOCK"
                        replacement_results.append(
                            {
                                "slot": slot.value,
                                "status": "BLOCKED_BY_LEADER_LOCK",
                                "incumbent_lead_portfolio_id": incumbent,
                                "incumbent_nickname": _leader_nickname(cursor, incumbent),
                                "candidate_lead_portfolio_id": leader_id,
                                "candidate_nickname": candidate_by_id[leader_id].nickname,
                                "expires_at": None,
                            }
                        )
                        continue
                    if incumbent is not None and incumbent in leaders_with_positions:
                        wait = slot_replacement_wait(slot)
                        expires_at = occurred_at + wait
                        replacement_id = _digest(
                            {
                                "candidate": leader_id,
                                "incumbent": incumbent,
                                "selection_run_id": selection_run_id,
                                "slot": slot.value,
                            }
                        )
                        cursor.execute(
                            """
                            INSERT INTO copytrading.slot_replacement_events(
                              replacement_event_id,replacement_id,selection_run_id,slot,
                              incumbent_lead_portfolio_id,candidate_lead_portfolio_id,
                              state,requested_at,expires_at,actor_id,reason_codes,occurred_at
                            ) VALUES (%s,%s,%s,%s,%s,%s,'REQUESTED',%s,%s,%s,%s,%s)
                            """,
                            (
                                _digest({"replacement_id": replacement_id, "state": "REQUESTED"}),
                                replacement_id,
                                selection_run_id,
                                slot.value,
                                incumbent,
                                leader_id,
                                occurred_at,
                                expires_at,
                                f"selector:{strategy.value.lower()}",
                                Jsonb(["COPY_SLOT_REPLACEMENT_WAITING_FOR_EXPOSURE_CLOSE"]),
                                occurred_at,
                            ),
                        )
                        selected_status[leader_id] = "WAITING_FOR_POSITION_CLOSE"
                        replacement_results.append(
                            {
                                "slot": slot.value,
                                "status": "WAITING_FOR_POSITION_CLOSE",
                                "incumbent_lead_portfolio_id": incumbent,
                                "incumbent_nickname": _leader_nickname(cursor, incumbent),
                                "candidate_lead_portfolio_id": leader_id,
                                "candidate_nickname": candidate_by_id[leader_id].nickname,
                                "expires_at": expires_at.isoformat(),
                            }
                        )
                        continue
                    final_slots[slot] = leader_id
                    direct_assignments.append((slot, leader_id))
                    selected_direct.add(leader_id)
                    selected_status[leader_id] = "ASSIGNED" if incumbent is None else "REPLACED"
                    if incumbent is not None:
                        displaced.add(incumbent)
                    replacement_results.append(
                        {
                            "slot": slot.value,
                            "status": selected_status[leader_id],
                            "incumbent_lead_portfolio_id": incumbent,
                            "incumbent_nickname": (
                                None if incumbent is None else _leader_nickname(cursor, incumbent)
                            ),
                            "candidate_lead_portfolio_id": leader_id,
                            "candidate_nickname": candidate_by_id[leader_id].nickname,
                            "expires_at": None,
                        }
                    )
                final_assigned = set(final_slots.values())
                selected_rank = {
                    leader_id: index
                    for index, leader_id in enumerate(
                        (value for value in selected_leader_ids if value not in blocked_by_lock),
                        start=1,
                    )
                }
                backup_ids = set(backups.values())
                decision_ids = (
                    set(candidate_by_id) | displaced | set(selected_leader_ids) | backup_ids
                )
                for leader_id in sorted(decision_ids):
                    assessment = assessments.get(leader_id)
                    decision_reasons: tuple[str, ...]
                    if leader_id in blocked_by_lock:
                        outcome = "REJECTED"
                        rank = None
                        decision_reasons = ("COPY_SLOT_REPLACEMENT_BLOCKED_BY_LEADER_LOCK",)
                    elif leader_id in selected_rank:
                        outcome = "SELECTED"
                        rank = selected_rank[leader_id]
                        decision_reasons = (
                            f"COPY_{strategy.value}_SELECTED_{selected_status[leader_id]}",
                        )
                    elif leader_id in backup_ids:
                        outcome = "REJECTED"
                        rank = None
                        decision_reasons = ("COPY_SELECTION_LOCKED_SLOT_BACKUP_SELECTED",)
                    else:
                        outcome = "REJECTED"
                        rank = None
                        decision_reasons = (
                            assessment.reason_codes
                            if assessment and assessment.reason_codes
                            else (f"COPY_{strategy.value}_NOT_SELECTED",)
                        )
                    evidence_hash = _digest(
                        {
                            "leader_id": leader_id,
                            "outcome": outcome,
                            "rank": rank,
                            "reasons": list(decision_reasons),
                            "selection_run_id": selection_run_id,
                        }
                    )
                    cursor.execute(
                        """
                        INSERT INTO copytrading.selection_decisions(
                          decision_id,selection_run_id,lead_portfolio_id,outcome,rank,
                          score,reason_codes,evidence_hash,occurred_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            _digest(
                                {
                                    "evidence_hash": evidence_hash,
                                    "selection_run_id": selection_run_id,
                                }
                            ),
                            selection_run_id,
                            leader_id,
                            outcome,
                            rank,
                            assessment.deterministic_score if assessment else None,
                            Jsonb(list(decision_reasons)),
                            evidence_hash,
                            occurred_at,
                        ),
                    )
                for slot, leader_id in direct_assignments:
                    if current_slots.get(slot) == leader_id:
                        continue
                    cursor.execute(
                        """
                        INSERT INTO copytrading.leader_slot_events(
                          slot_event_id,slot,action,lead_portfolio_id,actor_id,
                          reason_codes,occurred_at
                        ) VALUES (%s,%s,'ASSIGNED',%s,%s,%s,%s)
                        """,
                        (
                            _digest(
                                {
                                    "leader_id": leader_id,
                                    "selection_run_id": selection_run_id,
                                    "slot": slot.value,
                                }
                            ),
                            slot.value,
                            leader_id,
                            f"selector:{strategy.value.lower()}",
                            Jsonb([f"COPY_{strategy.value}_SLOT_ASSIGNED"]),
                            occurred_at,
                        ),
                    )
                lifecycle_changes: dict[str, tuple[LeaderLifecycle, tuple[str, ...]]] = {}
                for leader_id in selected_direct:
                    current = current_states.get(leader_id)
                    lifecycle_changes[leader_id] = (
                        (
                            LeaderLifecycle.ACTIVE
                            if current in {LeaderLifecycle.OBSERVE_ONLY, LeaderLifecycle.ACTIVE}
                            else LeaderLifecycle.OBSERVE_ONLY
                        ),
                        (f"COPY_{strategy.value}_SLOT_SELECTED",),
                    )
                for leader_id in displaced - final_assigned:
                    lifecycle_changes[leader_id] = (
                        (LeaderLifecycle.RETIRED),
                        ("COPY_SLOT_ROTATION_RETIRED",),
                    )
                for leader_id, (lifecycle, reasons) in lifecycle_changes.items():
                    cursor.execute(
                        """
                        INSERT INTO copytrading.leader_lifecycle_events(
                          event_id,lead_portfolio_id,state,selection_run_id,
                          reason_codes,occurred_at
                        ) VALUES (%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            _digest(
                                {
                                    "lead_portfolio_id": leader_id,
                                    "selection_run_id": selection_run_id,
                                    "state": lifecycle.value,
                                }
                            ),
                            leader_id,
                            lifecycle.value,
                            selection_run_id,
                            Jsonb(list(reasons)),
                            occurred_at,
                        ),
                    )
                payload = {
                    "event": "copy_slot_selection",
                    "leader_ids": list(selected_leader_ids),
                    "leaders": [
                        {
                            "lead_portfolio_id": leader_id,
                            "nickname": _leader_nickname(cursor, leader_id),
                            "slot": slot.value,
                        }
                        for slot, leader_id in slot_assignments
                    ],
                    "results": replacement_results,
                    "state": "SUCCEEDED",
                    "strategy": strategy.value,
                }
                cursor.execute(
                    """
                    INSERT INTO control.outbox(
                      message_id,deduplication_key,topic,payload,payload_hash
                    ) VALUES (%s,%s,'copy.telegram',%s,%s)
                    ON CONFLICT (deduplication_key) DO NOTHING
                    """,
                    (
                        _digest({"slot_selection": selection_run_id}),
                        f"copy-slot-selection:{selection_run_id}",
                        Jsonb(payload),
                        _digest(payload),
                    ),
                )
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_SLOT_SELECTION_WRITE_FAILED") from error
        return selection_run_id

    def reconcile_pending_slot_replacements(self, *, occurred_at: datetime) -> int:
        """Apply or expire automatic replacements without ever forcing an incumbent exit."""

        _require_utc(occurred_at)
        terminal_count = 0
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("copy-leader-slots",),
                )
                cursor.execute(
                    """
                    WITH latest AS (
                      SELECT DISTINCT ON (replacement_id) *
                        FROM copytrading.slot_replacement_events
                       ORDER BY replacement_id,occurred_at DESC,replacement_event_id DESC
                    )
                    SELECT * FROM latest WHERE state='REQUESTED'
                     ORDER BY requested_at,replacement_id
                    """,
                    (),
                )
                pending = list(cursor.fetchall())
                if not pending:
                    return 0
                cursor.execute(
                    """
                    SELECT DISTINCT ON (slot) slot,action,lead_portfolio_id
                      FROM copytrading.leader_slot_events
                     ORDER BY slot,occurred_at DESC,slot_event_id DESC
                    """,
                    (),
                )
                current_slots = {
                    LeaderSlot(str(row["slot"])): str(row["lead_portfolio_id"])
                    for row in cursor.fetchall()
                    if row["action"] == "ASSIGNED" and row["lead_portfolio_id"] is not None
                }
                locked_leader_ids = _locked_leader_ids(cursor)
                for row in pending:
                    slot = LeaderSlot(str(row["slot"]))
                    incumbent = str(row["incumbent_lead_portfolio_id"])
                    candidate = str(row["candidate_lead_portfolio_id"])
                    if current_slots.get(slot) != incumbent:
                        _append_slot_replacement_terminal(
                            cursor,
                            row,
                            state="SUPERSEDED",
                            reason_code="COPY_SLOT_REPLACEMENT_INCUMBENT_CHANGED",
                            occurred_at=occurred_at,
                        )
                        terminal_count += 1
                        continue
                    if incumbent in locked_leader_ids:
                        _append_slot_replacement_terminal(
                            cursor,
                            row,
                            state="SUPERSEDED",
                            reason_code="COPY_SLOT_REPLACEMENT_CANCELLED_BY_LEADER_LOCK",
                            occurred_at=occurred_at,
                        )
                        terminal_count += 1
                        continue
                    if any(
                        other_slot is not slot and leader_id == candidate
                        for other_slot, leader_id in current_slots.items()
                    ):
                        _append_slot_replacement_terminal(
                            cursor,
                            row,
                            state="SUPERSEDED",
                            reason_code="COPY_SLOT_REPLACEMENT_CANDIDATE_ASSIGNED_ELSEWHERE",
                            occurred_at=occurred_at,
                        )
                        terminal_count += 1
                        continue
                    has_exposure, cleared_at = _leader_owned_exposure_state(
                        cursor,
                        incumbent,
                        requested_at=row["requested_at"],
                    )
                    expires_at = row["expires_at"]
                    closed_within_window = not has_exposure and cleared_at <= expires_at
                    if closed_within_window:
                        replacement_id = str(row["replacement_id"])
                        cursor.execute(
                            """
                            INSERT INTO copytrading.leader_slot_events(
                              slot_event_id,slot,action,lead_portfolio_id,actor_id,
                              reason_codes,occurred_at
                            ) VALUES (%s,%s,'ASSIGNED',%s,%s,%s,%s)
                            """,
                            (
                                _digest(
                                    {
                                        "replacement_id": replacement_id,
                                        "slot": slot.value,
                                        "state": "APPLIED",
                                    }
                                ),
                                slot.value,
                                candidate,
                                "slot-replacement-reconciler",
                                Jsonb(["COPY_SLOT_REPLACEMENT_EXPOSURE_CLOSED"]),
                                occurred_at,
                            ),
                        )
                        candidate_lifecycle = _latest_leader_lifecycle(cursor, candidate)
                        next_candidate_lifecycle = (
                            LeaderLifecycle.ACTIVE
                            if candidate_lifecycle
                            in {LeaderLifecycle.OBSERVE_ONLY, LeaderLifecycle.ACTIVE}
                            else LeaderLifecycle.OBSERVE_ONLY
                        )
                        _append_replacement_lifecycle(
                            cursor,
                            candidate,
                            next_candidate_lifecycle,
                            replacement_id,
                            str(row["selection_run_id"]),
                            occurred_at,
                        )
                        _append_replacement_lifecycle(
                            cursor,
                            incumbent,
                            LeaderLifecycle.RETIRED,
                            replacement_id,
                            str(row["selection_run_id"]),
                            occurred_at,
                        )
                        _append_slot_replacement_terminal(
                            cursor,
                            row,
                            state="APPLIED",
                            reason_code="COPY_SLOT_REPLACEMENT_APPLIED_AFTER_EXPOSURE_CLOSE",
                            occurred_at=occurred_at,
                        )
                        current_slots[slot] = candidate
                        terminal_count += 1
                        continue
                    if occurred_at >= expires_at:
                        _append_slot_replacement_terminal(
                            cursor,
                            row,
                            state="EXPIRED",
                            reason_code="COPY_SLOT_REPLACEMENT_EXPIRED_WITH_EXPOSURE",
                            occurred_at=occurred_at,
                        )
                        terminal_count += 1
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_SLOT_REPLACEMENT_RECONCILE_FAILED") from error
        return terminal_count

    def apply_daily_selection(
        self,
        candidates: tuple[LeaderSnapshot, ...],
        assessments: Mapping[str, CandidateAssessment],
        selected_leader_ids: tuple[str, ...],
        *,
        scheduled_for: datetime,
        data_cutoff: datetime,
        candidate_digest: str,
        policy_digest: str,
        codex_report_digest: str,
        occurred_at: datetime,
    ) -> str:
        for value in (scheduled_for, data_cutoff, occurred_at):
            _require_utc(value)
        candidate_by_id = {item.lead_portfolio_id: item for item in candidates}
        if (
            not selected_leader_ids
            or len(set(selected_leader_ids)) != len(selected_leader_ids)
            or not set(selected_leader_ids) <= set(candidate_by_id)
        ):
            raise ValueError("copy selected leader IDs are invalid")
        selection_run_id = _digest(
            {
                "scheduled_for": scheduled_for.isoformat(),
                "type": "daily-copy-leader-selection",
            }
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("copy-daily-selection",),
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.selection_runs(
                      selection_run_id,scheduled_for,data_cutoff,candidate_digest,
                      policy_digest,codex_report_digest,state,reason_codes,occurred_at,
                      selection_kind
                    ) VALUES (%s,%s,%s,%s,%s,%s,'COMPLETED',%s,%s,'LEGACY')
                    ON CONFLICT (selection_run_id) DO NOTHING
                    RETURNING selection_run_id
                    """,
                    (
                        selection_run_id,
                        scheduled_for,
                        data_cutoff,
                        candidate_digest,
                        policy_digest,
                        codex_report_digest,
                        Jsonb(["COPY_CODEX_SELECTION_COMPLETED"]),
                        occurred_at,
                    ),
                )
                if cursor.fetchone() is None:
                    return selection_run_id
                cursor.execute(
                    """
                    SELECT DISTINCT ON (lead_portfolio_id) lead_portfolio_id,state
                      FROM copytrading.leader_lifecycle_events
                     ORDER BY lead_portfolio_id,occurred_at DESC,event_id DESC
                    """,
                    (),
                )
                current_states = {
                    str(row["lead_portfolio_id"]): LeaderLifecycle(str(row["state"]))
                    for row in cursor.fetchall()
                }
                cursor.execute(
                    """
                    WITH latest AS (
                      SELECT DISTINCT ON (lead_portfolio_id,symbol,position_side)
                             lead_portfolio_id,resulting_local_quantity
                        FROM copytrading.virtual_position_events
                       ORDER BY lead_portfolio_id,symbol,position_side,
                                occurred_at DESC,position_event_id DESC
                    )
                    SELECT DISTINCT lead_portfolio_id FROM latest
                     WHERE resulting_local_quantity > 0
                    """,
                    (),
                )
                leaders_with_positions = {
                    str(row["lead_portfolio_id"]) for row in cursor.fetchall()
                }
                selected_rank = {
                    leader_id: index for index, leader_id in enumerate(selected_leader_ids, start=1)
                }
                decision_ids = set(candidate_by_id) | {
                    leader_id
                    for leader_id, state in current_states.items()
                    if state
                    in {
                        LeaderLifecycle.OBSERVE_ONLY,
                        LeaderLifecycle.ACTIVE,
                        LeaderLifecycle.DRAINING,
                    }
                }
                for leader_id in sorted(decision_ids):
                    assessment = assessments.get(leader_id)
                    decision_reasons: tuple[str, ...]
                    if leader_id in selected_rank:
                        outcome = "SELECTED"
                        rank: int | None = selected_rank[leader_id]
                        decision_reasons = ("COPY_CODEX_SELECTED",)
                    elif leader_id in current_states and leader_id in leaders_with_positions:
                        outcome = "DRAINING"
                        rank = None
                        decision_reasons = ("COPY_ROTATION_POSITION_DRAINING",)
                    else:
                        outcome = "REJECTED"
                        rank = None
                        decision_reasons = (
                            assessment.reason_codes
                            if assessment and assessment.reason_codes
                            else ("COPY_CODEX_NOT_SELECTED",)
                        )
                    evidence_hash = _digest(
                        {
                            "leader_id": leader_id,
                            "outcome": outcome,
                            "rank": rank,
                            "reasons": list(decision_reasons),
                            "selection_run_id": selection_run_id,
                        }
                    )
                    decision_id = _digest(
                        {
                            "evidence_hash": evidence_hash,
                            "selection_run_id": selection_run_id,
                        }
                    )
                    cursor.execute(
                        """
                        INSERT INTO copytrading.selection_decisions(
                          decision_id,selection_run_id,lead_portfolio_id,outcome,rank,
                          score,reason_codes,evidence_hash,occurred_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            decision_id,
                            selection_run_id,
                            leader_id,
                            outcome,
                            rank,
                            assessment.deterministic_score if assessment else None,
                            Jsonb(list(decision_reasons)),
                            evidence_hash,
                            occurred_at,
                        ),
                    )
                lifecycle_changes: dict[str, tuple[LeaderLifecycle, tuple[str, ...]]] = {}
                for leader_id in selected_leader_ids:
                    current = current_states.get(leader_id)
                    lifecycle = (
                        LeaderLifecycle.OBSERVE_ONLY
                        if current in {None, LeaderLifecycle.RETIRED}
                        else LeaderLifecycle.ACTIVE
                    )
                    lifecycle_changes[leader_id] = (
                        lifecycle,
                        ("COPY_DAILY_SELECTED",),
                    )
                for leader_id, current in current_states.items():
                    if leader_id not in selected_rank and current in {
                        LeaderLifecycle.OBSERVE_ONLY,
                        LeaderLifecycle.ACTIVE,
                        LeaderLifecycle.DRAINING,
                    }:
                        if leader_id in leaders_with_positions:
                            lifecycle_changes[leader_id] = (
                                LeaderLifecycle.DRAINING,
                                ("COPY_DAILY_ROTATION_DRAINING",),
                            )
                        else:
                            lifecycle_changes[leader_id] = (
                                LeaderLifecycle.RETIRED,
                                ("COPY_DAILY_ROTATION_RETIRED",),
                            )
                for leader_id, (lifecycle, lifecycle_reasons) in lifecycle_changes.items():
                    event_id = _digest(
                        {
                            "lead_portfolio_id": leader_id,
                            "selection_run_id": selection_run_id,
                            "state": lifecycle.value,
                        }
                    )
                    cursor.execute(
                        """
                        INSERT INTO copytrading.leader_lifecycle_events(
                          event_id,lead_portfolio_id,state,selection_run_id,
                          reason_codes,occurred_at
                        ) VALUES (%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            event_id,
                            leader_id,
                            lifecycle.value,
                            selection_run_id,
                            Jsonb(list(lifecycle_reasons)),
                            occurred_at,
                        ),
                    )
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_DAILY_SELECTION_WRITE_FAILED") from error
        return selection_run_id

    def record_selection_failure(
        self,
        *,
        scheduled_for: datetime,
        reason_code: str,
        occurred_at: datetime,
        strategy: SelectionStrategy | None = None,
    ) -> str:
        for value in (scheduled_for, occurred_at):
            _require_utc(value)
        if not reason_code or len(reason_code) > 120:
            raise ValueError("copy selection failure reason is invalid")
        selection_kind = strategy.value if strategy is not None else "LEGACY"
        digest = _digest(
            {
                "reason_code": reason_code,
                "scheduled_for": scheduled_for.isoformat(),
                "strategy": selection_kind,
                "type": "daily-copy-leader-selection-failure",
            }
        )
        payload = {
            "event": "copy_system",
            "state": f"{selection_kind}_SELECTION_FAILED",
            "strategy": selection_kind,
            "reason_codes": [reason_code],
            "summary": f"{selection_kind} 带单员选择失败并将自动重试: {reason_code}",
            "occurred_at": occurred_at.isoformat(),
        }
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO copytrading.selection_runs(
                      selection_run_id,scheduled_for,data_cutoff,candidate_digest,
                      policy_digest,codex_report_digest,state,reason_codes,occurred_at,
                      selection_kind
                    ) VALUES (%s,%s,%s,%s,%s,NULL,'FAILED',%s,%s,%s)
                    ON CONFLICT (selection_run_id) DO NOTHING
                    """,
                    (
                        digest,
                        scheduled_for,
                        occurred_at,
                        _digest({"failure": reason_code}),
                        _digest({"policy": "selection-failure"}),
                        Jsonb([reason_code]),
                        occurred_at,
                        selection_kind,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO control.outbox(
                      message_id,deduplication_key,topic,payload,payload_hash
                    ) VALUES (%s,%s,'copy.telegram',%s,%s)
                    ON CONFLICT (deduplication_key) DO NOTHING
                    """,
                    (
                        _digest({"selection_failure_notification": digest}),
                        f"copy-selection-failure:{digest}",
                        Jsonb(payload),
                        _digest(payload),
                    ),
                )
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_SELECTION_FAILURE_WRITE_FAILED") from error
        return digest

    def record_selection_unchanged(
        self,
        *,
        scheduled_for: datetime,
        reason_code: str,
        occurred_at: datetime,
        strategy: SelectionStrategy,
    ) -> str:
        """Complete a scheduled selection safely when no replacement is admissible."""

        for value in (scheduled_for, occurred_at):
            _require_utc(value)
        if not reason_code or len(reason_code) > 120:
            raise ValueError("copy unchanged selection reason is invalid")
        digest = _digest(
            {
                "reason_code": reason_code,
                "scheduled_for": scheduled_for.isoformat(),
                "strategy": strategy.value,
                "type": "copy-leader-selection-unchanged",
            }
        )
        payload = {
            "event": "copy_system",
            "state": f"{strategy.value}_SELECTION_UNCHANGED",
            "strategy": strategy.value,
            "reason_codes": [reason_code],
            "summary": (
                f"{strategy.value} 本轮没有足够的合适候选, 已保留当前带单员, "
                f"不会强行替换。原因: {reason_code}"
            ),
            "occurred_at": occurred_at.isoformat(),
        }
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO copytrading.selection_runs(
                      selection_run_id,scheduled_for,data_cutoff,candidate_digest,
                      policy_digest,codex_report_digest,state,reason_codes,occurred_at,
                      selection_kind
                    ) VALUES (%s,%s,%s,%s,%s,NULL,'COMPLETED',%s,%s,%s)
                    ON CONFLICT (selection_run_id) DO NOTHING
                    """,
                    (
                        digest,
                        scheduled_for,
                        occurred_at,
                        _digest({"no_change": reason_code}),
                        _digest({"policy": "retain-current-on-insufficient-candidates"}),
                        Jsonb(["COPY_SELECTION_RETAINED_CURRENT", reason_code]),
                        occurred_at,
                        strategy.value,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO control.outbox(
                      message_id,deduplication_key,topic,payload,payload_hash
                    ) VALUES (%s,%s,'copy.telegram',%s,%s)
                    ON CONFLICT (deduplication_key) DO NOTHING
                    """,
                    (
                        _digest({"selection_unchanged_notification": digest}),
                        f"copy-selection-unchanged:{digest}",
                        Jsonb(payload),
                        _digest(payload),
                    ),
                )
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_SELECTION_UNCHANGED_WRITE_FAILED") from error
        return digest

    def active_assignments(self) -> tuple[LeaderAssignment, ...]:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH current_lifecycle AS (
                      SELECT DISTINCT ON (lead_portfolio_id)
                             lead_portfolio_id,state,occurred_at
                        FROM copytrading.leader_lifecycle_events
                       ORDER BY lead_portfolio_id,occurred_at DESC,event_id DESC
                    ), current_snapshot AS (
                      SELECT DISTINCT ON (lead_portfolio_id)
                             lead_portfolio_id,nickname,aum_usdt
                        FROM copytrading.leader_snapshots
                       ORDER BY lead_portfolio_id,observed_at DESC,snapshot_id DESC
                    ), current_slots AS (
                      SELECT DISTINCT ON (slot) slot,action,lead_portfolio_id
                        FROM copytrading.leader_slot_events
                       ORDER BY slot,occurred_at DESC,slot_event_id DESC
                    ), slot_weights AS (
                      SELECT lead_portfolio_id,min(slot) AS slot,
                             sum(CASE slot
                                   WHEN 'LONG_TERM' THEN 0.25
                                   WHEN 'SHORT_TERM_1' THEN 0.375
                                   WHEN 'SHORT_TERM_2' THEN 0.375
                                   ELSE 0.25
                                 END)
                               AS slot_weight
                        FROM current_slots WHERE action='ASSIGNED'
                       GROUP BY lead_portfolio_id
                    ), current_multipliers AS (
                      SELECT DISTINCT ON (lead_portfolio_id)
                             lead_portfolio_id,multiplier
                        FROM copytrading.leader_follow_multiplier_events
                       ORDER BY lead_portfolio_id,occurred_at DESC,
                                multiplier_event_id DESC
                    )
                    SELECT lifecycle.lead_portfolio_id,lifecycle.state,
                           snapshot.nickname,snapshot.aum_usdt,
                           slot_weights.slot_weight,slot_weights.slot,
                           coalesce(current_multipliers.multiplier,1) AS follow_multiplier
                      FROM current_lifecycle AS lifecycle
                      JOIN current_snapshot AS snapshot USING (lead_portfolio_id)
                      LEFT JOIN slot_weights USING (lead_portfolio_id)
                      LEFT JOIN current_multipliers USING (lead_portfolio_id)
                     WHERE lifecycle.state IN ('OBSERVE_ONLY','ACTIVE','DRAINING')
                       AND (
                         lifecycle.state='DRAINING' OR slot_weights.slot_weight IS NOT NULL OR
                         NOT EXISTS (SELECT 1 FROM copytrading.leader_slot_events)
                       )
                     ORDER BY lifecycle.lead_portfolio_id
                    """,
                    (),
                )
                rows = list(cursor.fetchall())
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_ASSIGNMENT_READ_FAILED") from error
        active_rows = [row for row in rows if row["state"] != "DRAINING"]
        configured_weight = sum(
            (Decimal(str(row["slot_weight"] or "0")) for row in active_rows),
            start=Decimal("0"),
        )
        fallback_weight = Decimal("1") / Decimal(len(active_rows)) if active_rows else Decimal("0")
        return tuple(
            LeaderAssignment(
                lead_portfolio_id=str(row["lead_portfolio_id"]),
                nickname=str(row["nickname"]),
                lifecycle=LeaderLifecycle(str(row["state"])),
                source_aum_usdt=Decimal(str(row["aum_usdt"])),
                portfolio_weight=(
                    Decimal("0")
                    if row["state"] == "DRAINING"
                    else (
                        Decimal(str(row["slot_weight"])) / configured_weight
                        if configured_weight > 0 and row["slot_weight"] is not None
                        else fallback_weight
                    )
                ),
                slot=(LeaderSlot(str(row["slot"])) if row["slot"] is not None else None),
                follow_multiplier=int(row["follow_multiplier"]),
            )
            for row in rows
        )

    def retire_drained_leaders(self, *, occurred_at: datetime) -> tuple[str, ...]:
        """Retire unassigned draining leaders once all owned work is complete.

        A replaced leader remains DRAINING while it has a local position or an
        unfinished signal so its eventual source reduction is never missed.  Once
        both are absent, continuing to poll it serves no recovery purpose and makes
        the runtime leader count exceed the ten configured slots.
        """

        _require_utc(occurred_at)
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("copy-retire-drained-leaders",),
                )
                cursor.execute(
                    """
                    WITH current_lifecycle AS (
                      SELECT DISTINCT ON (lead_portfolio_id)
                             lead_portfolio_id,state
                        FROM copytrading.leader_lifecycle_events
                       ORDER BY lead_portfolio_id,occurred_at DESC,event_id DESC
                    ), current_slots AS (
                      SELECT DISTINCT ON (slot) slot,action,lead_portfolio_id
                        FROM copytrading.leader_slot_events
                       ORDER BY slot,occurred_at DESC,slot_event_id DESC
                    ), latest_positions AS (
                      SELECT DISTINCT ON (lead_portfolio_id,symbol,position_side)
                             lead_portfolio_id,resulting_local_quantity
                        FROM copytrading.virtual_position_events
                       ORDER BY lead_portfolio_id,symbol,position_side,
                                occurred_at DESC,position_event_id DESC
                    ), position_totals AS (
                      SELECT lead_portfolio_id,sum(resulting_local_quantity) AS quantity
                        FROM latest_positions GROUP BY lead_portfolio_id
                    ), latest_decisions AS (
                      SELECT DISTINCT ON (signal_id) signal_id,state
                        FROM copytrading.signal_decision_events
                       ORDER BY signal_id,occurred_at DESC,decision_event_id DESC
                    ), unfinished AS (
                      SELECT DISTINCT signal.lead_portfolio_id
                        FROM copytrading.signals AS signal
                        LEFT JOIN latest_decisions AS decision USING(signal_id)
                       WHERE decision.state IS NULL OR decision.state IN (
                         'RECEIVED','APPROVED','SUBMITTED','UNCERTAIN'
                       )
                    )
                    SELECT lifecycle.lead_portfolio_id
                      FROM current_lifecycle AS lifecycle
                      LEFT JOIN position_totals AS positions USING(lead_portfolio_id)
                     WHERE lifecycle.state='DRAINING'
                       AND coalesce(positions.quantity,0)=0
                       AND NOT EXISTS (
                         SELECT 1 FROM current_slots AS slot
                          WHERE slot.action='ASSIGNED'
                            AND slot.lead_portfolio_id=lifecycle.lead_portfolio_id
                       )
                       AND NOT EXISTS (
                         SELECT 1 FROM unfinished
                          WHERE unfinished.lead_portfolio_id=lifecycle.lead_portfolio_id
                       )
                     ORDER BY lifecycle.lead_portfolio_id
                    """,
                    (),
                )
                leader_ids = tuple(str(row["lead_portfolio_id"]) for row in cursor.fetchall())
                for leader_id in leader_ids:
                    cursor.execute(
                        """
                        INSERT INTO copytrading.leader_lifecycle_events(
                          event_id,lead_portfolio_id,state,selection_run_id,
                          reason_codes,occurred_at
                        ) VALUES (%s,%s,'RETIRED',NULL,%s,%s)
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            _digest(
                                {
                                    "lead_portfolio_id": leader_id,
                                    "occurred_at": occurred_at.isoformat(),
                                    "reason": "COPY_DRAINING_COMPLETED_RETIRED",
                                }
                            ),
                            leader_id,
                            Jsonb(["COPY_DRAINING_COMPLETED_RETIRED"]),
                            occurred_at,
                        ),
                    )
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_DRAINED_LEADER_RETIRE_FAILED") from error
        return leader_ids

    def latest_runtime_control(self) -> RuntimeControl:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT control_event_id,state,actor_id,reason_codes,occurred_at
                      FROM copytrading.runtime_control_events
                     ORDER BY occurred_at DESC,control_event_id DESC LIMIT 1
                    """,
                    (),
                )
                row = cursor.fetchone()
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_RUNTIME_CONTROL_READ_FAILED") from error
        if row is None:
            return RuntimeControl(
                event_id=None,
                state=RuntimeControlState.PAUSED_NEW_ENTRIES,
                actor_id="fail-closed-default",
                occurred_at=None,
                reason_codes=("COPY_RUNTIME_CONTROL_MISSING",),
            )
        raw_reasons = row["reason_codes"]
        reason_codes = (
            tuple(str(value) for value in raw_reasons)
            if isinstance(raw_reasons, list)
            else ("COPY_RUNTIME_CONTROL_REASONS_INVALID",)
        )
        return RuntimeControl(
            event_id=str(row["control_event_id"]),
            state=RuntimeControlState(str(row["state"])),
            actor_id=str(row["actor_id"]),
            occurred_at=row["occurred_at"],
            reason_codes=reason_codes,
        )

    def bind_execution_environment(
        self,
        environment: str,
        *,
        occurred_at: datetime,
    ) -> bool:
        """Permanently bind this database lane to Testnet or production."""

        _require_utc(occurred_at)
        if environment not in {"TESTNET", "PRODUCTION"}:
            raise ValueError("copy execution environment is invalid")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("copy-execution-environment",),
                )
                cursor.execute(
                    """
                    SELECT environment FROM copytrading.execution_environment_bindings
                     WHERE singleton=true
                    """,
                    (),
                )
                row = cursor.fetchone()
                if row is not None:
                    if str(row["environment"]) != environment:
                        raise CopyRepositoryError("COPY_EXECUTION_ENVIRONMENT_DATABASE_MISMATCH")
                    return False
                if environment == "PRODUCTION":
                    cursor.execute(
                        """
                        SELECT
                          EXISTS(SELECT 1 FROM copytrading.source_order_events) OR
                          EXISTS(SELECT 1 FROM copytrading.signals) OR
                          EXISTS(SELECT 1 FROM copytrading.submission_claims) OR
                          EXISTS(SELECT 1 FROM copytrading.virtual_position_events) OR
                          EXISTS(SELECT 1 FROM copytrading.account_valuation_events)
                            AS has_test_or_trading_facts
                        """,
                        (),
                    )
                    facts = cursor.fetchone()
                    if facts is None or bool(facts["has_test_or_trading_facts"]):
                        raise CopyRepositoryError("COPY_PRODUCTION_DATABASE_MUST_BE_FRESH")
                cursor.execute(
                    """
                    INSERT INTO copytrading.execution_environment_bindings(
                      singleton,environment,bound_at
                    ) VALUES (true,%s,%s)
                    """,
                    (environment, occurred_at),
                )
        except CopyRepositoryError:
            raise
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_EXECUTION_ENVIRONMENT_BINDING_FAILED") from error
        return True

    def execution_environment_binding(self) -> str | None:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT environment FROM copytrading.execution_environment_bindings
                     WHERE singleton=true
                    """,
                    (),
                )
                row = cursor.fetchone()
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_EXECUTION_ENVIRONMENT_BINDING_READ_FAILED") from error
        return None if row is None else str(row["environment"])

    def append_runtime_control(
        self,
        state: RuntimeControlState,
        *,
        actor_id: str,
        reason_codes: tuple[str, ...],
        occurred_at: datetime,
        notify: bool = False,
    ) -> str:
        _require_utc(occurred_at)
        if not actor_id or len(actor_id) > 64 or not reason_codes:
            raise ValueError("copy runtime control event is invalid")
        event_id = _digest(
            {
                "actor_id": actor_id,
                "occurred_at": occurred_at.isoformat(),
                "reason_codes": list(reason_codes),
                "state": state.value,
            }
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO copytrading.runtime_control_events(
                      control_event_id,state,actor_id,reason_codes,occurred_at
                    ) VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    RETURNING control_event_id
                    """,
                    (
                        event_id,
                        state.value,
                        actor_id,
                        Jsonb(list(reason_codes)),
                        occurred_at,
                    ),
                )
                inserted = cursor.fetchone() is not None
                if notify and inserted:
                    payload = {
                        "event": "copy_runtime_control",
                        "state": state.value,
                        "actor_id": actor_id,
                        "reason_codes": list(reason_codes),
                        "occurred_at": occurred_at.isoformat(),
                    }
                    cursor.execute(
                        """
                        INSERT INTO control.outbox(
                          message_id,deduplication_key,topic,payload,payload_hash
                        ) VALUES (%s,%s,'copy.telegram',%s,%s)
                        ON CONFLICT (deduplication_key) DO NOTHING
                        """,
                        (
                            _digest({"runtime_control": event_id}),
                            f"copy-runtime-control:{event_id}",
                            Jsonb(payload),
                            _digest(payload),
                        ),
                    )
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_RUNTIME_CONTROL_WRITE_FAILED") from error
        return event_id

    def ensure_control_reduction_signals(
        self,
        control_event_id: str,
        *,
        occurred_at: datetime,
    ) -> tuple[NormalizedSignal, ...]:
        _require_utc(occurred_at)
        if len(control_event_id) != 64:
            raise ValueError("copy control event ID is invalid")
        created: list[NormalizedSignal] = []
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"copy-reduce-all:{control_event_id}",),
                )
                cursor.execute(
                    """
                    WITH latest AS (
                      SELECT DISTINCT ON (lead_portfolio_id,symbol,position_side)
                             lead_portfolio_id,symbol,position_side,
                             resulting_local_quantity,resulting_source_quantity
                        FROM copytrading.virtual_position_events
                       ORDER BY lead_portfolio_id,symbol,position_side,
                                occurred_at DESC,position_event_id DESC
                    )
                    SELECT * FROM latest WHERE resulting_local_quantity>0
                    ORDER BY lead_portfolio_id,symbol,position_side
                    """,
                    (),
                )
                for row in cursor.fetchall():
                    source_quantity = max(
                        Decimal(str(row["resulting_source_quantity"])),
                        Decimal(str(row["resulting_local_quantity"])),
                    )
                    identity = _digest(
                        {
                            "control_event_id": control_event_id,
                            "lead_portfolio_id": str(row["lead_portfolio_id"]),
                            "position_side": str(row["position_side"]),
                            "symbol": str(row["symbol"]),
                        }
                    )
                    signal_id = _digest({"control_reduction": identity})
                    cursor.execute(
                        """
                        INSERT INTO copytrading.signals(
                          signal_id,delta_event_id,lead_portfolio_id,symbol,position_side,
                          signal_kind,source_delta_quantity,reference_price,occurred_at,
                          signal_origin
                        ) VALUES (%s,NULL,%s,%s,%s,'REDUCE',%s,1,%s,'CONTROL')
                        ON CONFLICT (signal_id) DO NOTHING
                        RETURNING signal_id
                        """,
                        (
                            signal_id,
                            str(row["lead_portfolio_id"]),
                            str(row["symbol"]),
                            str(row["position_side"]),
                            source_quantity,
                            occurred_at,
                        ),
                    )
                    if cursor.fetchone() is None:
                        continue
                    created.append(
                        NormalizedSignal(
                            signal_id=signal_id,
                            source_event_key=identity,
                            source_identity_key=identity,
                            lead_portfolio_id=str(row["lead_portfolio_id"]),
                            symbol=str(row["symbol"]),
                            position_side=PositionSide(str(row["position_side"])),
                            kind=SignalKind.REDUCE,
                            source_delta_quantity=source_quantity,
                            source_cumulative_quantity=source_quantity,
                            reference_price=Decimal("1"),
                            occurred_at_ms=int(occurred_at.timestamp() * 1000),
                        )
                    )
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_CONTROL_REDUCTION_SIGNAL_FAILED") from error
        return tuple(created)

    def enforce_leader_symbol_stops(
        self,
        *,
        valuation_event_id: str,
        position_marks: tuple[AccountPositionMark, ...],
        occurred_at: datetime,
        loss_limit_usdt: Decimal = LEADER_SYMBOL_STOP_LOSS_USDT,
        cooldown: timedelta = LEADER_SYMBOL_STOP_COOLDOWN,
    ) -> tuple[LeaderSymbolStop, ...]:
        """Activate and recover isolated leader/symbol stops from current position PnL.

        Only position sides whose latest quantity remains positive participate.
        Already realized reductions and fully closed historical positions are
        excluded. Current LONG and SHORT unrealized PnL are netted only inside
        the same leader and symbol.
        Every active cooldown continuously derives close signals from the latest
        append-only virtual-position events, so a late entry fill or process restart
        cannot leave risk behind.
        """

        _require_utc(occurred_at)
        if len(valuation_event_id) != 64:
            raise ValueError("copy valuation event ID is invalid")
        if (
            not loss_limit_usdt.is_finite()
            or loss_limit_usdt <= 0
            or cooldown <= timedelta(0)
        ):
            raise ValueError("copy leader symbol stop policy is invalid")
        mark_keys = {(mark.symbol, mark.position_side) for mark in position_marks}
        if len(mark_keys) != len(position_marks):
            raise ValueError("copy account position marks contain duplicates")
        marks_by_key = {
            (mark.symbol, mark.position_side): mark.mark_price for mark in position_marks
        }
        newly_triggered_ids: set[str] = set()
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("copy-leader-symbol-stop",),
                )
                cursor.execute(
                    """
                    WITH latest AS (
                      SELECT DISTINCT ON (lead_portfolio_id,symbol,position_side)
                             pnl_event_id,position_event_id,lead_portfolio_id,symbol,
                             position_side,resulting_quantity,
                             resulting_average_entry_price,observed_at
                        FROM copytrading.leader_pnl_events
                       ORDER BY lead_portfolio_id,symbol,position_side,
                                observed_at DESC,pnl_event_id DESC
                    )
                    SELECT latest.*
                      FROM latest
                     WHERE latest.resulting_quantity>0
                     ORDER BY latest.lead_portfolio_id,latest.symbol,latest.position_side
                    """,
                    (),
                )
                pnl_positions: list[LeaderSymbolPositionPnl] = []
                incomplete_keys: set[tuple[str, str]] = set()
                for row in cursor.fetchall():
                    leader_id = str(row["lead_portfolio_id"])
                    symbol = str(row["symbol"])
                    position_side = PositionSide(str(row["position_side"]))
                    mark_price = marks_by_key.get((symbol, position_side))
                    if mark_price is None:
                        incomplete_keys.add((leader_id, symbol))
                        continue
                    pnl_positions.append(
                        LeaderSymbolPositionPnl(
                            lead_portfolio_id=leader_id,
                            symbol=symbol,
                            position_side=position_side,
                            position_event_id=str(row["position_event_id"]),
                            quantity=Decimal(str(row["resulting_quantity"])),
                            average_entry_price=Decimal(
                                str(row["resulting_average_entry_price"])
                            ),
                            mark_price=mark_price,
                        )
                    )
                positions_by_key: dict[
                    tuple[str, str], list[LeaderSymbolPositionPnl]
                ] = {}
                for position in pnl_positions:
                    positions_by_key.setdefault(
                        (position.lead_portfolio_id, position.symbol), []
                    ).append(position)
                totals = aggregate_leader_symbol_pnl(tuple(pnl_positions))
                cursor.execute(
                    """
                    SELECT DISTINCT ON (lead_portfolio_id,symbol)
                           stop_event_id,lead_portfolio_id,symbol,
                           net_position_pnl_usdt,loss_limit_usdt,
                           triggered_at,blocked_until
                      FROM copytrading.leader_symbol_stop_events
                     WHERE blocked_until>%s
                     ORDER BY lead_portfolio_id,symbol,
                              blocked_until DESC,triggered_at DESC,stop_event_id DESC
                    """,
                    (occurred_at,),
                )
                active_keys = {
                    (str(row["lead_portfolio_id"]), str(row["symbol"]))
                    for row in cursor.fetchall()
                }
                for (leader_id, symbol), net_pnl in sorted(totals.items()):
                    key = (leader_id, symbol)
                    if (
                        key in incomplete_keys
                        or key in active_keys
                        or net_pnl > -loss_limit_usdt
                    ):
                        continue
                    stop_event_id = _digest(
                        {
                            "lead_portfolio_id": leader_id,
                            "loss_limit_usdt": str(loss_limit_usdt),
                            "symbol": symbol,
                            "type": "leader-symbol-stop",
                            "valuation_event_id": valuation_event_id,
                        }
                    )
                    blocked_until = occurred_at + cooldown
                    breakdown = [
                        {
                            "average_entry_price": str(position.average_entry_price),
                            "mark_price": str(position.mark_price),
                            "position_event_id": position.position_event_id,
                            "position_side": position.position_side.value,
                            "quantity": str(position.quantity),
                            "unrealized_pnl_usdt": str(position.unrealized_pnl_usdt),
                        }
                        for position in positions_by_key[key]
                    ]
                    reason_codes = (
                        "COPY_LEADER_SYMBOL_NET_LOSS_LIMIT_REACHED",
                        "COPY_LEADER_SYMBOL_ENTRY_COOLDOWN_24H",
                    )
                    cursor.execute(
                        """
                        INSERT INTO copytrading.leader_symbol_stop_events(
                          stop_event_id,valuation_event_id,lead_portfolio_id,symbol,
                          loss_limit_usdt,net_position_pnl_usdt,
                          position_pnl_breakdown,blocked_until,reason_codes,triggered_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT DO NOTHING
                        RETURNING stop_event_id
                        """,
                        (
                            stop_event_id,
                            valuation_event_id,
                            leader_id,
                            symbol,
                            loss_limit_usdt,
                            net_pnl,
                            Jsonb(breakdown),
                            blocked_until,
                            Jsonb(list(reason_codes)),
                            occurred_at,
                        ),
                    )
                    if cursor.fetchone() is None:
                        continue
                    newly_triggered_ids.add(stop_event_id)
                    active_keys.add(key)
                    cursor.execute(
                        """
                        SELECT nickname FROM copytrading.leader_snapshots
                         WHERE lead_portfolio_id=%s
                         ORDER BY observed_at DESC,snapshot_id DESC LIMIT 1
                        """,
                        (leader_id,),
                    )
                    nickname_row = cursor.fetchone()
                    nickname = (
                        str(nickname_row["nickname"])
                        if nickname_row is not None
                        else "名称未知"
                    )
                    payload = {
                        "event": "copy_leader_symbol_stop_triggered",
                        "lead_portfolio_id": leader_id,
                        "leader_nickname": nickname,
                        "symbol": symbol,
                        "net_position_pnl_usdt": str(net_pnl),
                        "loss_limit_usdt": str(loss_limit_usdt),
                        "position_pnl_breakdown": breakdown,
                        "blocked_until": blocked_until.isoformat(),
                        "reason_codes": list(reason_codes),
                        "occurred_at": occurred_at.isoformat(),
                    }
                    cursor.execute(
                        """
                        INSERT INTO control.outbox(
                          message_id,deduplication_key,topic,payload,payload_hash
                        ) VALUES (%s,%s,'copy.telegram',%s,%s)
                        ON CONFLICT (deduplication_key) DO NOTHING
                        """,
                        (
                            _digest({"leader_symbol_stop": stop_event_id}),
                            f"copy-leader-symbol-stop:{stop_event_id}",
                            Jsonb(payload),
                            _digest(payload),
                        ),
                    )
                cursor.execute(
                    """
                    WITH active AS (
                      SELECT DISTINCT ON (lead_portfolio_id,symbol)
                             stop_event_id,lead_portfolio_id,symbol,
                             net_position_pnl_usdt,loss_limit_usdt,
                             triggered_at,blocked_until
                        FROM copytrading.leader_symbol_stop_events
                       WHERE blocked_until>%s
                       ORDER BY lead_portfolio_id,symbol,
                                blocked_until DESC,triggered_at DESC,stop_event_id DESC
                    ), snapshot AS (
                      SELECT DISTINCT ON (lead_portfolio_id)
                             lead_portfolio_id,nickname
                        FROM copytrading.leader_snapshots
                       ORDER BY lead_portfolio_id,observed_at DESC,snapshot_id DESC
                    )
                    SELECT active.*,coalesce(snapshot.nickname,'名称未知') AS nickname
                      FROM active LEFT JOIN snapshot USING(lead_portfolio_id)
                     ORDER BY active.lead_portfolio_id,active.symbol
                    """,
                    (occurred_at,),
                )
                active_rows = list(cursor.fetchall())
                for stop_row in active_rows:
                    leader_id = str(stop_row["lead_portfolio_id"])
                    symbol = str(stop_row["symbol"])
                    stop_event_id = str(stop_row["stop_event_id"])
                    cursor.execute(
                        """
                        WITH latest AS (
                          SELECT DISTINCT ON (position_side)
                                 position_event_id,position_side,
                                 resulting_local_quantity,resulting_source_quantity,
                                 reference_price
                            FROM copytrading.virtual_position_events
                           WHERE lead_portfolio_id=%s AND symbol=%s
                           ORDER BY position_side,occurred_at DESC,position_event_id DESC
                        )
                        SELECT * FROM latest
                         WHERE resulting_local_quantity>0
                         ORDER BY position_side
                        """,
                        (leader_id, symbol),
                    )
                    for position_row in cursor.fetchall():
                        position_event_id = str(position_row["position_event_id"])
                        position_side = PositionSide(str(position_row["position_side"]))
                        source_quantity = max(
                            Decimal(str(position_row["resulting_source_quantity"])),
                            Decimal(str(position_row["resulting_local_quantity"])),
                        )
                        identity = _digest(
                            {
                                "position_event_id": position_event_id,
                                "stop_event_id": stop_event_id,
                            }
                        )
                        signal_id = _digest({"leader_symbol_stop_reduction": identity})
                        reference_price = marks_by_key.get(
                            (symbol, position_side),
                            Decimal(str(position_row["reference_price"])),
                        )
                        cursor.execute(
                            """
                            INSERT INTO copytrading.signals(
                              signal_id,delta_event_id,lead_portfolio_id,symbol,
                              position_side,signal_kind,source_delta_quantity,
                              reference_price,occurred_at,signal_origin
                            ) VALUES (%s,NULL,%s,%s,%s,'REDUCE',%s,%s,%s,'CONTROL')
                            ON CONFLICT (signal_id) DO NOTHING
                            RETURNING signal_id
                            """,
                            (
                                signal_id,
                                leader_id,
                                symbol,
                                position_side.value,
                                source_quantity,
                                reference_price,
                                occurred_at,
                            ),
                        )
                        if cursor.fetchone() is None:
                            continue
                        cursor.execute(
                            """
                            INSERT INTO copytrading.leader_symbol_stop_signal_events(
                              stop_signal_event_id,stop_event_id,position_event_id,
                              signal_id,occurred_at
                            ) VALUES (%s,%s,%s,%s,%s)
                            """,
                            (
                                _digest(
                                    {
                                        "position_event_id": position_event_id,
                                        "signal_id": signal_id,
                                        "stop_event_id": stop_event_id,
                                    }
                                ),
                                stop_event_id,
                                position_event_id,
                                signal_id,
                                occurred_at,
                            ),
                        )
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_LEADER_SYMBOL_STOP_ENFORCEMENT_FAILED") from error
        return tuple(
            LeaderSymbolStop(
                stop_event_id=str(row["stop_event_id"]),
                lead_portfolio_id=str(row["lead_portfolio_id"]),
                leader_nickname=str(row["nickname"]),
                symbol=str(row["symbol"]),
                net_position_pnl_usdt=Decimal(str(row["net_position_pnl_usdt"])),
                loss_limit_usdt=Decimal(str(row["loss_limit_usdt"])),
                triggered_at=row["triggered_at"],
                blocked_until=row["blocked_until"],
                newly_triggered=str(row["stop_event_id"]) in newly_triggered_ids,
            )
            for row in active_rows
        )

    def active_leader_symbol_stop(
        self,
        *,
        lead_portfolio_id: str,
        symbol: str,
        occurred_at: datetime,
    ) -> LeaderSymbolStop | None:
        """Return the durable cooldown that blocks only this leader/symbol pair."""

        _require_utc(occurred_at)
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH snapshot AS (
                      SELECT nickname FROM copytrading.leader_snapshots
                       WHERE lead_portfolio_id=%s
                       ORDER BY observed_at DESC,snapshot_id DESC LIMIT 1
                    )
                    SELECT stop_event_id,lead_portfolio_id,symbol,
                           net_position_pnl_usdt,loss_limit_usdt,
                           triggered_at,blocked_until,
                           coalesce((SELECT nickname FROM snapshot),'名称未知') AS nickname
                      FROM copytrading.leader_symbol_stop_events
                     WHERE lead_portfolio_id=%s AND symbol=%s AND blocked_until>%s
                     ORDER BY blocked_until DESC,triggered_at DESC,stop_event_id DESC
                     LIMIT 1
                    """,
                    (
                        lead_portfolio_id,
                        lead_portfolio_id,
                        symbol,
                        occurred_at,
                    ),
                )
                row = cursor.fetchone()
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_LEADER_SYMBOL_STOP_READ_FAILED") from error
        if row is None:
            return None
        return LeaderSymbolStop(
            stop_event_id=str(row["stop_event_id"]),
            lead_portfolio_id=str(row["lead_portfolio_id"]),
            leader_nickname=str(row["nickname"]),
            symbol=str(row["symbol"]),
            net_position_pnl_usdt=Decimal(str(row["net_position_pnl_usdt"])),
            loss_limit_usdt=Decimal(str(row["loss_limit_usdt"])),
            triggered_at=row["triggered_at"],
            blocked_until=row["blocked_until"],
        )

    def recoverable_leader_symbol_stop_signals(
        self,
        stop_event_id: str,
    ) -> tuple[NormalizedSignal, ...]:
        """Prioritize close signals for one active stop ahead of the general queue."""

        if len(stop_event_id) != 64:
            raise ValueError("copy leader symbol stop event ID is invalid")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH latest_decision AS (
                      SELECT DISTINCT ON (signal_id) signal_id,state
                        FROM copytrading.signal_decision_events
                       ORDER BY signal_id,occurred_at DESC,decision_event_id DESC
                    ), latest_submission AS (
                      SELECT DISTINCT ON (signal_id) signal_id,state
                        FROM copytrading.submission_events
                       ORDER BY signal_id,occurred_at DESC,submission_event_id DESC
                    ), attributed AS (
                      SELECT DISTINCT signal_id
                        FROM copytrading.virtual_position_events
                    )
                    SELECT signal.signal_id,signal.lead_portfolio_id,signal.symbol,
                           signal.position_side,signal.signal_kind,
                           signal.source_delta_quantity,signal.reference_price,
                           extract(epoch FROM signal.occurred_at)*1000 AS occurred_at_ms
                      FROM copytrading.leader_symbol_stop_signal_events AS stop_signal
                      JOIN copytrading.signals AS signal USING(signal_id)
                      LEFT JOIN latest_decision AS decision USING(signal_id)
                      LEFT JOIN copytrading.submission_claims AS claim USING(signal_id)
                      LEFT JOIN latest_submission AS submission USING(signal_id)
                      LEFT JOIN attributed USING(signal_id)
                     WHERE stop_signal.stop_event_id=%s
                       AND (
                         decision.state IS NULL OR decision.state IN (
                           'RECEIVED','APPROVED','SUBMITTED','UNCERTAIN'
                         ) OR (
                           claim.signal_id IS NOT NULL
                           AND attributed.signal_id IS NULL
                           AND submission.state IN (
                             'SUBMITTING','ACKNOWLEDGED','PARTIALLY_FILLED',
                             'UNKNOWN'
                           )
                         )
                       )
                     ORDER BY signal.occurred_at,signal.signal_id
                    """,
                    (stop_event_id,),
                )
                rows = list(cursor.fetchall())
        except psycopg.Error as error:
            raise CopyRepositoryError(
                "COPY_LEADER_SYMBOL_STOP_SIGNAL_READ_FAILED"
            ) from error
        return tuple(
            NormalizedSignal(
                signal_id=str(row["signal_id"]),
                source_event_key=str(row["signal_id"]),
                source_identity_key=str(row["signal_id"]),
                lead_portfolio_id=str(row["lead_portfolio_id"]),
                symbol=str(row["symbol"]),
                position_side=PositionSide(str(row["position_side"])),
                kind=SignalKind(str(row["signal_kind"])),
                source_delta_quantity=Decimal(str(row["source_delta_quantity"])),
                source_cumulative_quantity=Decimal(str(row["source_delta_quantity"])),
                reference_price=Decimal(str(row["reference_price"])),
                occurred_at_ms=int(row["occurred_at_ms"]),
            )
            for row in rows
        )

    def ingest_orders(
        self,
        lead_portfolio_id: str,
        orders: tuple[PublicLeaderOrder, ...],
        *,
        baseline: bool,
        observed_at: datetime,
    ) -> tuple[NormalizedSignal, ...]:
        _require_utc(observed_at)
        signals: list[NormalizedSignal] = []
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"copy-leader:{lead_portfolio_id}",),
                )
                cursor.execute(
                    """
                    SELECT reset_event_id,occurred_at
                      FROM copytrading.source_resolution_reset_events
                     WHERE lead_portfolio_id=%s
                     ORDER BY occurred_at DESC,reset_event_id DESC LIMIT 1
                    """,
                    (lead_portfolio_id,),
                )
                reset_row = cursor.fetchone()
                reset_event_id = str(reset_row["reset_event_id"]) if reset_row is not None else None
                reset_at = reset_row["occurred_at"] if reset_row is not None else None
                ordered = sorted(
                    (_source_epoch_order(order, reset_event_id=reset_event_id) for order in orders),
                    key=lambda order: (order.update_time_ms, order.event_key),
                )
                cursor.execute(
                    """
                    SELECT max(update_time_ms) AS maximum_update_time_ms
                      FROM copytrading.source_order_events
                     WHERE lead_portfolio_id=%s
                       AND (%s::timestamptz IS NULL OR observed_at>%s)
                    """,
                    (lead_portfolio_id, reset_at, reset_at),
                )
                watermark_row = cursor.fetchone()
                maximum_update_time_ms = (
                    int(watermark_row["maximum_update_time_ms"])
                    if watermark_row and watermark_row["maximum_update_time_ms"] is not None
                    else 0
                )
                for order in ordered:
                    cursor.execute(
                        """
                        SELECT max(executed_quantity) AS executed_quantity
                          FROM copytrading.source_order_events
                         WHERE lead_portfolio_id=%s AND identity_key=%s
                           AND (%s::timestamptz IS NULL OR observed_at>%s)
                        """,
                        (lead_portfolio_id, order.identity_key, reset_at, reset_at),
                    )
                    previous_row = cursor.fetchone()
                    if previous_row is None or previous_row["executed_quantity"] is None:
                        has_previous = False
                        previous = Decimal("0")
                    else:
                        has_previous = True
                        previous = Decimal(str(previous_row["executed_quantity"]))
                    matches_ambiguous_baseline = False
                    if (
                        not baseline
                        and not has_previous
                        and order.position_side is not SourcePositionSide.BOTH
                    ):
                        cursor.execute(
                            """
                            SELECT EXISTS(
                              SELECT 1
                                FROM copytrading.source_order_events
                               WHERE lead_portfolio_id=%s
                                 AND position_side='BOTH'
                                 AND is_baseline
                                 AND source_payload_hash=%s
                                 AND (%s::timestamptz IS NULL OR observed_at>%s)
                            ) AS matched
                            """,
                            (
                                lead_portfolio_id,
                                order.raw_payload_hash,
                                reset_at,
                                reset_at,
                            ),
                        )
                        ambiguous_baseline_row = cursor.fetchone()
                        matches_ambiguous_baseline = bool(
                            ambiguous_baseline_row and ambiguous_baseline_row["matched"]
                        )
                    is_baseline = _is_source_order_baseline(
                        baseline=baseline,
                        has_previous=has_previous,
                        update_time_ms=order.update_time_ms,
                        maximum_update_time_ms=maximum_update_time_ms,
                        matches_ambiguous_baseline=matches_ambiguous_baseline,
                    )
                    cursor.execute(
                        """
                        INSERT INTO copytrading.source_order_events(
                          event_key,identity_key,lead_portfolio_id,symbol,position_side,
                          order_side,order_type,executed_quantity,average_price,total_pnl,
                          order_time_ms,update_time_ms,is_baseline,source_payload_hash,observed_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT DO NOTHING
                        RETURNING event_key
                        """,
                        (
                            order.event_key,
                            order.identity_key,
                            order.lead_portfolio_id,
                            order.symbol,
                            order.position_side.value,
                            order.order_side.value,
                            order.order_type,
                            order.executed_quantity,
                            order.average_price,
                            order.total_pnl,
                            order.order_time_ms,
                            order.update_time_ms,
                            is_baseline,
                            order.raw_payload_hash,
                            observed_at,
                        ),
                    )
                    inserted = cursor.fetchone() is not None
                    delta = order.executed_quantity - previous
                    maximum_update_time_ms = max(
                        maximum_update_time_ms,
                        order.update_time_ms,
                    )
                    if not inserted or is_baseline or delta <= 0:
                        continue
                    if order.position_side is SourcePositionSide.BOTH:
                        continue
                    signal = NormalizedSignal.from_order(order, delta_quantity=delta)
                    delta_event_id = _digest({"kind": "fill-delta", "signal_id": signal.signal_id})
                    occurred_at = datetime.fromtimestamp(order.update_time_ms / 1000, tz=UTC)
                    cursor.execute(
                        """
                        INSERT INTO copytrading.source_fill_delta_events(
                          delta_event_id,source_event_key,identity_key,lead_portfolio_id,
                          previous_executed_quantity,delta_quantity,
                          cumulative_executed_quantity,occurred_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            delta_event_id,
                            signal.source_event_key,
                            signal.source_identity_key,
                            signal.lead_portfolio_id,
                            previous,
                            signal.source_delta_quantity,
                            signal.source_cumulative_quantity,
                            occurred_at,
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO copytrading.signals(
                          signal_id,delta_event_id,lead_portfolio_id,symbol,position_side,
                          signal_kind,source_delta_quantity,reference_price,occurred_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            signal.signal_id,
                            delta_event_id,
                            signal.lead_portfolio_id,
                            signal.symbol,
                            signal.position_side.value,
                            signal.kind.value,
                            signal.source_delta_quantity,
                            signal.reference_price,
                            occurred_at,
                        ),
                    )
                    signals.append(signal)
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_SOURCE_ORDER_WRITE_FAILED") from error
        return tuple(signals)

    def source_orders_for_resolution(
        self,
        lead_portfolio_id: str,
    ) -> tuple[PublicLeaderOrder, ...]:
        """Return the latest cumulative view of each persisted source order."""

        if not lead_portfolio_id:
            raise ValueError("copy leader ID is required")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH latest_reset AS (
                        SELECT occurred_at
                          FROM copytrading.source_resolution_reset_events
                         WHERE lead_portfolio_id=%s
                         ORDER BY occurred_at DESC,reset_event_id DESC LIMIT 1
                    )
                    SELECT DISTINCT ON (identity_key)
                           lead_portfolio_id,symbol,position_side,order_side,
                           order_type,executed_quantity,average_price,total_pnl,
                           order_time_ms,update_time_ms,identity_key,event_key,
                           source_payload_hash
                     FROM copytrading.source_order_events
                     WHERE lead_portfolio_id=%s
                       AND position_side<>'BOTH'
                       AND observed_at>coalesce(
                           (SELECT occurred_at FROM latest_reset),'-infinity'::timestamptz
                       )
                     ORDER BY identity_key,update_time_ms DESC,
                              executed_quantity DESC,event_key DESC
                    """,
                    (lead_portfolio_id, lead_portfolio_id),
                )
                rows = list(cursor.fetchall())
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_SOURCE_ORDER_RESOLUTION_READ_FAILED") from error
        return tuple(
            PublicLeaderOrder(
                lead_portfolio_id=str(row["lead_portfolio_id"]),
                symbol=str(row["symbol"]),
                position_side=SourcePositionSide(str(row["position_side"])),
                order_side=OrderSide(str(row["order_side"])),
                order_type=str(row["order_type"]),
                executed_quantity=Decimal(str(row["executed_quantity"])),
                average_price=Decimal(str(row["average_price"])),
                total_pnl=Decimal(str(row["total_pnl"])),
                order_time_ms=int(row["order_time_ms"]),
                update_time_ms=int(row["update_time_ms"]),
                identity_key=str(row["identity_key"]),
                event_key=str(row["event_key"]),
                raw_payload_hash=str(row["source_payload_hash"]),
            )
            for row in rows
        )

    def source_watermark(self, lead_portfolio_id: str) -> int | None:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH latest_reset AS (
                        SELECT occurred_at
                          FROM copytrading.source_resolution_reset_events
                         WHERE lead_portfolio_id=%s
                         ORDER BY occurred_at DESC,reset_event_id DESC LIMIT 1
                    ), source_watermark AS (
                        SELECT max(update_time_ms) AS maximum_update_time_ms
                          FROM copytrading.source_order_events
                         WHERE lead_portfolio_id=%s
                           AND observed_at>coalesce(
                               (SELECT occurred_at FROM latest_reset),
                               '-infinity'::timestamptz
                           )
                    ), baseline_fence AS (
                        SELECT max(maximum_update_time_ms) AS maximum_update_time_ms
                          FROM copytrading.poll_events
                         WHERE lead_portfolio_id=%s
                           AND state='SUCCEEDED'
                           AND reason_codes
                               ? 'COPY_BASELINE_ORDER_IDENTITY_AMBIGUITY_FENCED'
                           AND occurred_at>coalesce(
                               (SELECT occurred_at FROM latest_reset),
                               '-infinity'::timestamptz
                           )
                    )
                    SELECT greatest(
                             (SELECT maximum_update_time_ms FROM source_watermark),
                             (SELECT maximum_update_time_ms FROM baseline_fence)
                           ) AS maximum_update_time_ms
                    """,
                    (
                        lead_portfolio_id,
                        lead_portfolio_id,
                        lead_portfolio_id,
                    ),
                )
                row = cursor.fetchone()
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_SOURCE_WATERMARK_READ_FAILED") from error
        return (
            int(row["maximum_update_time_ms"])
            if row is not None and row["maximum_update_time_ms"] is not None
            else None
        )

    def reset_source_resolution(
        self,
        lead_portfolio_id: str,
        *,
        reason_codes: tuple[str, ...],
        occurred_at: datetime,
    ) -> str:
        """Start a new append-only direction-resolution epoch for one leader."""

        if not lead_portfolio_id or not reason_codes:
            raise ValueError("copy source resolution reset evidence is required")
        _require_utc(occurred_at)
        reset_event_id = _digest(
            {
                "lead_portfolio_id": lead_portfolio_id,
                "occurred_at": occurred_at.isoformat(),
                "reason_codes": list(reason_codes),
                "type": "source-resolution-reset",
            }
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"copy-leader:{lead_portfolio_id}",),
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.source_resolution_reset_events(
                      reset_event_id,lead_portfolio_id,reason_codes,occurred_at
                    ) VALUES (%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        reset_event_id,
                        lead_portfolio_id,
                        Jsonb(list(reason_codes)),
                        occurred_at,
                    ),
                )
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_SOURCE_RESOLUTION_RESET_FAILED") from error
        return reset_event_id

    def record_poll(
        self,
        lead_portfolio_id: str,
        *,
        state: str,
        row_count: int,
        maximum_update_time_ms: int | None,
        reason_codes: tuple[str, ...],
        occurred_at: datetime,
    ) -> None:
        _require_utc(occurred_at)
        event_id = _digest(
            {
                "lead_portfolio_id": lead_portfolio_id,
                "occurred_at": occurred_at.isoformat(),
                "state": state,
            }
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO copytrading.poll_events(
                      poll_event_id,lead_portfolio_id,state,row_count,
                      maximum_update_time_ms,response_hash,reason_codes,occurred_at
                    ) VALUES (%s,%s,%s,%s,%s,NULL,%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        event_id,
                        lead_portfolio_id,
                        state,
                        row_count,
                        maximum_update_time_ms,
                        Jsonb(list(reason_codes)),
                        occurred_at,
                    ),
                )
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_POLL_EVENT_WRITE_FAILED") from error

    def load_virtual_ledger(self) -> VirtualPositionLedger:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT DISTINCT ON (lead_portfolio_id,symbol,position_side)
                           lead_portfolio_id,symbol,position_side,
                           resulting_local_quantity,resulting_source_quantity
                      FROM copytrading.virtual_position_events
                     ORDER BY lead_portfolio_id,symbol,position_side,
                              occurred_at DESC,position_event_id DESC
                    """,
                    (),
                )
                rows = list(cursor.fetchall())
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_VIRTUAL_LEDGER_READ_FAILED") from error
        return VirtualPositionLedger(
            tuple(
                VirtualPosition(
                    key=VirtualPositionKey(
                        lead_portfolio_id=str(row["lead_portfolio_id"]),
                        symbol=str(row["symbol"]),
                        position_side=PositionSide(str(row["position_side"])),
                    ),
                    local_quantity=Decimal(str(row["resulting_local_quantity"])),
                    observed_source_quantity=Decimal(str(row["resulting_source_quantity"])),
                )
                for row in rows
            )
        )

    def source_position_quantity_before(self, signal: NormalizedSignal) -> Decimal | None:
        """Return tracked public source exposure immediately before one source signal.

        This ledger follows every durable public increase/reduction even when the local
        protected limit did not fill.  Control reductions have no public source position
        and intentionally return ``None`` so they retain full-local-close semantics.
        """

        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH latest_reset AS (
                      SELECT occurred_at
                        FROM copytrading.source_resolution_reset_events
                       WHERE lead_portfolio_id=%s
                       ORDER BY occurred_at DESC,reset_event_id DESC LIMIT 1
                    ), current_source AS (
                      SELECT persisted.signal_origin,source.update_time_ms,source.event_key,
                             source.observed_at
                        FROM copytrading.signals AS persisted
                        LEFT JOIN copytrading.source_fill_delta_events AS delta
                          ON delta.delta_event_id=persisted.delta_event_id
                        LEFT JOIN copytrading.source_order_events AS source
                          ON source.event_key=delta.source_event_key
                       WHERE persisted.signal_id=%s
                    )
                    SELECT current_source.signal_origin,
                           greatest(
                             coalesce(sum(
                               CASE historical.signal_kind
                                 WHEN 'INCREASE' THEN historical.source_delta_quantity
                                 ELSE -historical.source_delta_quantity
                               END
                             ),0),
                             0
                           ) AS source_quantity
                      FROM current_source
                      LEFT JOIN copytrading.source_order_events AS source
                        ON current_source.signal_origin='PUBLIC'
                       AND source.lead_portfolio_id=%s
                       AND source.symbol=%s
                       AND source.position_side=%s
                       AND source.observed_at>coalesce(
                         (SELECT occurred_at FROM latest_reset),'-infinity'::timestamptz
                       )
                       AND current_source.observed_at>coalesce(
                         (SELECT occurred_at FROM latest_reset),'-infinity'::timestamptz
                       )
                       AND (source.update_time_ms,source.event_key)
                           < (current_source.update_time_ms,current_source.event_key)
                      LEFT JOIN copytrading.source_fill_delta_events AS delta
                        ON delta.source_event_key=source.event_key
                      LEFT JOIN copytrading.signals AS historical
                        ON historical.delta_event_id=delta.delta_event_id
                       AND historical.signal_origin='PUBLIC'
                     GROUP BY current_source.signal_origin
                    """,
                    (
                        signal.lead_portfolio_id,
                        signal.signal_id,
                        signal.lead_portfolio_id,
                        signal.symbol,
                        signal.position_side.value,
                    ),
                )
                row = cursor.fetchone()
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_SOURCE_POSITION_READ_FAILED") from error
        if row is None or str(row["signal_origin"]) != "PUBLIC":
            return None
        quantity = Decimal(str(row["source_quantity"]))
        if not quantity.is_finite() or quantity < 0:
            raise CopyRepositoryError("COPY_SOURCE_POSITION_INVALID")
        return quantity

    def attributed_fill_quantity(self, signal_id: str) -> Decimal | None:
        """Return an already committed local fill for crash-safe decision recovery.

        Virtual position and leader-PnL events are committed in one transaction.  A process can
        still exit after that transaction and before the terminal signal decision/outbox write.
        Recovery must recognize that durable attribution instead of applying the same reduction
        to the already-updated ledger a second time.
        """
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT abs(local_quantity_delta) AS filled_quantity
                      FROM copytrading.virtual_position_events
                     WHERE signal_id=%s
                    """,
                    (signal_id,),
                )
                row = cursor.fetchone()
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_ATTRIBUTED_FILL_READ_FAILED") from error
        if row is None:
            return None
        quantity = Decimal(str(row["filled_quantity"]))
        if not quantity.is_finite() or quantity <= 0:
            raise CopyRepositoryError("COPY_ATTRIBUTED_FILL_INVALID")
        return quantity

    def recoverable_signals(self, *, limit: int = 100) -> tuple[NormalizedSignal, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("copy recovery signal limit is invalid")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH latest_decision AS (
                      SELECT DISTINCT ON (signal_id) signal_id,state
                        FROM copytrading.signal_decision_events
                       ORDER BY signal_id,occurred_at DESC,decision_event_id DESC
                    ), latest_submission AS (
                      SELECT DISTINCT ON (signal_id) signal_id,state
                        FROM copytrading.submission_events
                       ORDER BY signal_id,occurred_at DESC,submission_event_id DESC
                    ), attributed AS (
                      SELECT DISTINCT signal_id
                        FROM copytrading.virtual_position_events
                    )
                    SELECT signal.signal_id,signal.lead_portfolio_id,signal.symbol,
                           signal.position_side,signal.signal_kind,
                           signal.source_delta_quantity,signal.reference_price,
                           extract(epoch FROM signal.occurred_at) * 1000 AS occurred_at_ms,
                           coalesce(delta.cumulative_executed_quantity,
                                    signal.source_delta_quantity)
                             AS cumulative_executed_quantity,
                           coalesce(delta.identity_key,signal.signal_id) AS identity_key,
                           coalesce(delta.source_event_key,signal.signal_id)
                             AS source_event_key
                      FROM copytrading.signals AS signal
                      LEFT JOIN copytrading.source_fill_delta_events AS delta
                        ON delta.delta_event_id=signal.delta_event_id
                      LEFT JOIN latest_decision AS decision USING (signal_id)
                      LEFT JOIN copytrading.submission_claims AS claim USING (signal_id)
                      LEFT JOIN latest_submission AS submission USING (signal_id)
                      LEFT JOIN attributed USING (signal_id)
                     WHERE (decision.state IS NULL OR decision.state IN (
                              'RECEIVED','APPROVED','SUBMITTED','UNCERTAIN'
                            ))
                        OR (claim.signal_id IS NOT NULL
                            AND attributed.signal_id IS NULL
                            AND submission.state IN (
                              'SUBMITTING','ACKNOWLEDGED','PARTIALLY_FILLED','UNKNOWN'
                            ))
                     ORDER BY signal.occurred_at,signal.signal_id
                     LIMIT %s
                    """,
                    (limit,),
                )
                rows = list(cursor.fetchall())
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_RECOVERY_SIGNAL_READ_FAILED") from error
        return tuple(
            NormalizedSignal(
                signal_id=str(row["signal_id"]),
                source_event_key=str(row["source_event_key"]),
                source_identity_key=str(row["identity_key"]),
                lead_portfolio_id=str(row["lead_portfolio_id"]),
                symbol=str(row["symbol"]),
                position_side=PositionSide(str(row["position_side"])),
                kind=SignalKind(str(row["signal_kind"])),
                source_delta_quantity=Decimal(str(row["source_delta_quantity"])),
                source_cumulative_quantity=Decimal(str(row["cumulative_executed_quantity"])),
                reference_price=Decimal(str(row["reference_price"])),
                occurred_at_ms=int(row["occurred_at_ms"]),
            )
            for row in rows
        )

    def pending_increase_signals(
        self,
        *,
        lead_portfolio_id: str,
        symbol: str,
        position_side: PositionSide,
        limit: int = 100,
    ) -> tuple[NormalizedSignal, ...]:
        """Return protected entries that may still fill and can be superseded by a reduction."""
        if not 1 <= limit <= 1000:
            raise ValueError("copy pending entry limit is invalid")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH latest_decision AS (
                      SELECT DISTINCT ON (signal_id) signal_id,state
                        FROM copytrading.signal_decision_events
                       ORDER BY signal_id,occurred_at DESC,decision_event_id DESC
                    )
                    SELECT signal.signal_id,signal.lead_portfolio_id,signal.symbol,
                           signal.position_side,signal.signal_kind,
                           signal.source_delta_quantity,signal.reference_price,
                           extract(epoch FROM signal.occurred_at)*1000 AS occurred_at_ms,
                           delta.cumulative_executed_quantity,delta.identity_key,
                           delta.source_event_key
                      FROM copytrading.signals AS signal
                      JOIN copytrading.source_fill_delta_events AS delta
                        ON delta.delta_event_id=signal.delta_event_id
                      JOIN copytrading.submission_claims AS claim USING(signal_id)
                      JOIN latest_decision AS decision USING(signal_id)
                     WHERE signal.lead_portfolio_id=%s AND signal.symbol=%s
                       AND signal.position_side=%s AND signal.signal_kind='INCREASE'
                       AND claim.order_type='LIMIT'
                       AND decision.state IN ('SUBMITTED','UNCERTAIN')
                     ORDER BY signal.occurred_at,signal.signal_id
                     LIMIT %s
                    """,
                    (lead_portfolio_id, symbol, position_side.value, limit),
                )
                rows = list(cursor.fetchall())
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_PENDING_ENTRY_READ_FAILED") from error
        return tuple(
            NormalizedSignal(
                signal_id=str(row["signal_id"]),
                source_event_key=str(row["source_event_key"]),
                source_identity_key=str(row["identity_key"]),
                lead_portfolio_id=str(row["lead_portfolio_id"]),
                symbol=str(row["symbol"]),
                position_side=PositionSide(str(row["position_side"])),
                kind=SignalKind(str(row["signal_kind"])),
                source_delta_quantity=Decimal(str(row["source_delta_quantity"])),
                source_cumulative_quantity=Decimal(str(row["cumulative_executed_quantity"])),
                reference_price=Decimal(str(row["reference_price"])),
                occurred_at_ms=int(row["occurred_at_ms"]),
            )
            for row in rows
        )

    def portfolio_usage(
        self,
        *,
        lead_portfolio_id: str,
        symbol: str,
        account_equity_usdt: Decimal,
        account_available_balance_usdt: Decimal,
        current_symbol_leverage: int,
    ) -> PortfolioUsage:
        if not 1 <= current_symbol_leverage <= 125:
            raise ValueError("copy current symbol leverage is invalid")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH latest AS (
                      SELECT DISTINCT ON (lead_portfolio_id,symbol,position_side)
                             lead_portfolio_id,symbol,committed_margin_usdt,
                             committed_margin_usdt*leverage AS committed_notional_usdt
                        FROM copytrading.virtual_position_events
                       ORDER BY lead_portfolio_id,symbol,position_side,
                                occurred_at DESC,position_event_id DESC
                    ), latest_decision AS (
                      SELECT DISTINCT ON (signal_id) signal_id,state
                        FROM copytrading.signal_decision_events
                       ORDER BY signal_id,occurred_at DESC,decision_event_id DESC
                    ), pending AS (
                      SELECT signal.lead_portfolio_id,signal.symbol,
                             claim.requested_quantity*claim.limit_price/claim.leverage
                               AS committed_margin_usdt,
                             claim.requested_quantity*claim.limit_price
                               AS committed_notional_usdt
                        FROM copytrading.submission_claims AS claim
                        JOIN copytrading.signals AS signal USING(signal_id)
                        JOIN latest_decision AS decision USING(signal_id)
                       WHERE claim.order_type='LIMIT'
                         AND decision.state IN ('SUBMITTED','UNCERTAIN')
                    ), raw_usage AS (
                      SELECT lead_portfolio_id,symbol,committed_margin_usdt,
                             committed_notional_usdt FROM latest
                      UNION ALL
                      SELECT lead_portfolio_id,symbol,committed_margin_usdt,
                             committed_notional_usdt FROM pending
                    ), usage AS (
                      SELECT lead_portfolio_id,symbol,
                             CASE WHEN symbol=%s
                                  THEN committed_notional_usdt/%s
                                  ELSE committed_margin_usdt END
                               AS committed_margin_usdt
                        FROM raw_usage
                    )
                    SELECT coalesce(sum(committed_margin_usdt),0) AS total_margin,
                           coalesce(sum(committed_margin_usdt) FILTER (
                             WHERE lead_portfolio_id=%s
                           ),0) AS leader_margin,
                           coalesce(sum(committed_margin_usdt) FILTER (
                             WHERE symbol=%s
                           ),0) AS symbol_margin,
                           coalesce((
                             SELECT limit_usdt
                               FROM copytrading.entry_margin_limit_events
                              ORDER BY occurred_at DESC,limit_event_id DESC LIMIT 1
                           ),%s) AS configured_entry_margin
                      FROM usage
                    """,
                    (
                        symbol,
                        current_symbol_leverage,
                        lead_portfolio_id,
                        symbol,
                        DEFAULT_ENTRY_MARGIN_LIMIT_USDT,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise CopyRepositoryError("COPY_PORTFOLIO_USAGE_INVALID")
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_PORTFOLIO_USAGE_READ_FAILED") from error
        return PortfolioUsage(
            account_equity_usdt=account_equity_usdt,
            total_committed_margin_usdt=Decimal(str(row["total_margin"])),
            leader_committed_margin_usdt=Decimal(str(row["leader_margin"])),
            symbol_committed_margin_usdt=Decimal(str(row["symbol_margin"])),
            account_available_balance_usdt=account_available_balance_usdt,
            configured_entry_margin_usdt=Decimal(str(row["configured_entry_margin"])),
        )

    def ensure_envelope_baseline(
        self,
        *,
        exchange_margin_balance_usdt: Decimal,
        operating_envelope_usdt: Decimal,
        occurred_at: datetime,
    ) -> Decimal:
        _require_utc(occurred_at)
        if min(exchange_margin_balance_usdt, operating_envelope_usdt) <= 0:
            raise ValueError("copy envelope baseline values must be positive")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("copy-account-envelope",),
                )
                cursor.execute(
                    """
                    SELECT exchange_margin_balance_usdt
                      FROM copytrading.account_envelope_events
                     ORDER BY occurred_at DESC,envelope_event_id DESC
                     LIMIT 1
                    """,
                    (),
                )
                row = cursor.fetchone()
                if row is not None:
                    return Decimal(str(row["exchange_margin_balance_usdt"]))
                event_id = _digest(
                    {
                        "event_type": "BASELINE",
                        "exchange_margin_balance_usdt": str(exchange_margin_balance_usdt),
                        "occurred_at": occurred_at.isoformat(),
                        "operating_envelope_usdt": str(operating_envelope_usdt),
                    }
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.account_envelope_events(
                      envelope_event_id,event_type,operating_envelope_usdt,
                      exchange_margin_balance_usdt,reason_codes,occurred_at
                    ) VALUES (%s,'BASELINE',%s,%s,%s,%s)
                    """,
                    (
                        event_id,
                        operating_envelope_usdt,
                        exchange_margin_balance_usdt,
                        Jsonb(["COPY_ENVELOPE_INITIALIZED"]),
                        occurred_at,
                    ),
                )
                return exchange_margin_balance_usdt
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_ENVELOPE_BASELINE_FAILED") from error

    def record_signal_decision(
        self,
        signal: NormalizedSignal,
        *,
        state: str,
        local_quantity: Decimal,
        reason_codes: tuple[str, ...],
        occurred_at: datetime,
    ) -> None:
        _require_utc(occurred_at)
        evidence_hash = _digest(
            {
                "local_quantity": str(local_quantity),
                "reason_codes": list(reason_codes),
                "signal_id": signal.signal_id,
                "state": state,
            }
        )
        event_id = _digest(
            {
                "evidence_hash": evidence_hash,
                "occurred_at": occurred_at.isoformat(),
            }
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO copytrading.signal_decision_events(
                      decision_event_id,signal_id,state,local_quantity,
                      reason_codes,evidence_hash,occurred_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        event_id,
                        signal.signal_id,
                        state,
                        local_quantity,
                        Jsonb(list(reason_codes)),
                        evidence_hash,
                        occurred_at,
                    ),
                )
                cursor.execute(
                    """
                    SELECT nickname FROM copytrading.leader_snapshots
                     WHERE lead_portfolio_id=%s
                     ORDER BY observed_at DESC,snapshot_id DESC LIMIT 1
                    """,
                    (signal.lead_portfolio_id,),
                )
                leader_row = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT persisted_signal.signal_origin,
                           claim.order_type,claim.limit_price,
                           CASE WHEN upgrade.signal_id IS NULL
                                THEN claim.expires_at ELSE NULL END AS expires_at,
                           claim.requested_quantity,claim.leverage,
                           pnl.fill_price,pnl.resulting_average_entry_price,
                           pnl.realized_pnl_delta_usdt,
                           risk_stop.stop_event_id AS leader_symbol_stop_event_id,
                           risk_stop.net_position_pnl_usdt AS stop_net_position_pnl_usdt,
                           risk_stop.loss_limit_usdt AS stop_loss_limit_usdt,
                           risk_stop.blocked_until AS stop_blocked_until,
                           source.total_pnl-coalesce(prior.total_pnl,0)
                             AS leader_realized_pnl_delta
                      FROM copytrading.signals AS persisted_signal
                      LEFT JOIN copytrading.submission_claims AS claim USING(signal_id)
                      LEFT JOIN copytrading.submission_policy_upgrade_events AS upgrade
                        USING(signal_id)
                      LEFT JOIN copytrading.leader_pnl_events AS pnl USING(signal_id)
                      LEFT JOIN copytrading.leader_symbol_stop_signal_events AS risk_link
                        USING(signal_id)
                      LEFT JOIN copytrading.leader_symbol_stop_events AS risk_stop
                        USING(stop_event_id)
                      LEFT JOIN copytrading.source_fill_delta_events AS delta
                        ON delta.delta_event_id=persisted_signal.delta_event_id
                      LEFT JOIN copytrading.source_order_events AS source
                        ON source.event_key=delta.source_event_key
                      LEFT JOIN LATERAL (
                        SELECT previous.total_pnl
                          FROM copytrading.source_order_events AS previous
                         WHERE previous.identity_key=source.identity_key
                           AND (previous.update_time_ms,previous.event_key)
                               < (source.update_time_ms,source.event_key)
                         ORDER BY previous.update_time_ms DESC,previous.event_key DESC
                         LIMIT 1
                      ) AS prior ON true
                     WHERE persisted_signal.signal_id=%s
                    """,
                    (signal.signal_id,),
                )
                claim_row = cursor.fetchone()
                notification_payload: dict[str, Any] = {
                    "event": "copy_signal_decision",
                    "lead_portfolio_id": signal.lead_portfolio_id,
                    "leader_nickname": (
                        str(leader_row["nickname"]) if leader_row is not None else "名称未知"
                    ),
                    "symbol": signal.symbol,
                    "position_side": signal.position_side.value,
                    "signal_kind": signal.kind.value,
                    # Keep the leader operation distinct from the locally allocated
                    # quantity.  A reduction can legitimately have no owned local
                    # position; Telegram must still present the complete source
                    # signal instead of reducing it to a misleading zero-quantity
                    # order outcome.
                    "source_quantity": str(signal.source_delta_quantity),
                    "leader_reference_price": str(signal.reference_price),
                    "state": state,
                    "local_quantity": str(local_quantity),
                    "reason_codes": list(reason_codes),
                    "source_occurred_at": datetime.fromtimestamp(
                        signal.occurred_at_ms / 1000,
                        tz=UTC,
                    ).isoformat(),
                    "occurred_at": occurred_at.isoformat(),
                }
                if claim_row is not None:
                    notification_payload["signal_origin"] = str(claim_row["signal_origin"])
                    notification_payload["order_type"] = (
                        None if claim_row["order_type"] is None else str(claim_row["order_type"])
                    )
                    notification_payload["limit_price"] = (
                        None if claim_row["limit_price"] is None else str(claim_row["limit_price"])
                    )
                    notification_payload["expires_at"] = (
                        claim_row["expires_at"].isoformat()
                        if claim_row["expires_at"] is not None
                        else None
                    )
                    notification_payload["requested_quantity"] = (
                        None
                        if claim_row["requested_quantity"] is None
                        else str(claim_row["requested_quantity"])
                    )
                    notification_payload["leverage"] = (
                        None if claim_row["leverage"] is None else int(claim_row["leverage"])
                    )
                    notification_payload["system_fill_price"] = (
                        None if claim_row["fill_price"] is None else str(claim_row["fill_price"])
                    )
                    notification_payload["system_average_entry_price"] = (
                        None
                        if claim_row["resulting_average_entry_price"] is None
                        else str(claim_row["resulting_average_entry_price"])
                    )
                    notification_payload["leader_realized_pnl_delta"] = (
                        None
                        if claim_row["leader_realized_pnl_delta"] is None
                        else str(claim_row["leader_realized_pnl_delta"])
                    )
                    notification_payload["system_realized_pnl_delta_usdt"] = (
                        None
                        if claim_row["realized_pnl_delta_usdt"] is None
                        else str(claim_row["realized_pnl_delta_usdt"])
                    )
                    if claim_row["leader_symbol_stop_event_id"] is not None:
                        notification_payload["leader_symbol_stop_event_id"] = str(
                            claim_row["leader_symbol_stop_event_id"]
                        )
                        notification_payload["stop_net_position_pnl_usdt"] = str(
                            claim_row["stop_net_position_pnl_usdt"]
                        )
                        notification_payload["stop_loss_limit_usdt"] = str(
                            claim_row["stop_loss_limit_usdt"]
                        )
                        notification_payload["stop_blocked_until"] = claim_row[
                            "stop_blocked_until"
                        ].isoformat()
                    if signal.kind is SignalKind.INCREASE and claim_row["leverage"]:
                        margin_quantity = local_quantity
                        margin_price = (
                            claim_row["fill_price"]
                            if state == "FILLED" and claim_row["fill_price"] is not None
                            else claim_row["limit_price"]
                        )
                        if margin_quantity > 0 and margin_price is not None:
                            order_margin = (
                                margin_quantity
                                * Decimal(str(margin_price))
                                / Decimal(str(claim_row["leverage"]))
                            )
                            notification_payload["order_margin_usdt"] = str(order_margin)
                payload_hash = _digest(notification_payload)
                cursor.execute(
                    """
                    INSERT INTO control.outbox(
                      message_id,deduplication_key,topic,payload,payload_hash
                    ) VALUES (%s,%s,'copy.telegram',%s,%s)
                    ON CONFLICT (deduplication_key) DO NOTHING
                    """,
                    (
                        _digest({"notification": event_id}),
                        f"copy-signal:{signal.signal_id}:{state}",
                        Jsonb(notification_payload),
                        payload_hash,
                    ),
                )
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_SIGNAL_DECISION_WRITE_FAILED") from error

    def record_account_valuation(
        self,
        *,
        exchange_wallet_balance_usdt: Decimal,
        exchange_margin_balance_usdt: Decimal,
        exchange_available_balance_usdt: Decimal,
        envelope_baseline_usdt: Decimal,
        operating_envelope_usdt: Decimal,
        total_initial_margin_usdt: Decimal,
        total_maintenance_margin_usdt: Decimal,
        position_marks: tuple[AccountPositionMark, ...] = (),
        observed_at: datetime,
    ) -> str:
        _require_utc(observed_at)
        nonnegative = (
            exchange_wallet_balance_usdt,
            exchange_margin_balance_usdt,
            exchange_available_balance_usdt,
            total_initial_margin_usdt,
            total_maintenance_margin_usdt,
        )
        if (
            any(not value.is_finite() or value < 0 for value in nonnegative)
            or not envelope_baseline_usdt.is_finite()
            or envelope_baseline_usdt <= 0
            or not operating_envelope_usdt.is_finite()
            or operating_envelope_usdt <= 0
        ):
            raise ValueError("copy account valuation values are invalid")
        mark_keys = {(mark.symbol, mark.position_side) for mark in position_marks}
        if len(mark_keys) != len(position_marks):
            raise ValueError("copy account position marks contain duplicates")
        realized_net_pnl = exchange_wallet_balance_usdt - envelope_baseline_usdt
        unrealized_pnl = exchange_margin_balance_usdt - exchange_wallet_balance_usdt
        total_pnl = realized_net_pnl + unrealized_pnl
        logical_equity = max(Decimal("0"), operating_envelope_usdt + total_pnl)
        logical_available = logical_available_balance(
            exchange_available_balance_usdt=exchange_available_balance_usdt,
            logical_equity_usdt=logical_equity,
            total_initial_margin_usdt=total_initial_margin_usdt,
        )
        evidence = {
            "available": str(exchange_available_balance_usdt),
            "baseline": str(envelope_baseline_usdt),
            "initial_margin": str(total_initial_margin_usdt),
            "logical_available": str(logical_available),
            "logical_equity": str(logical_equity),
            "maintenance_margin": str(total_maintenance_margin_usdt),
            "margin": str(exchange_margin_balance_usdt),
            "position_marks": [
                {
                    "exchange_quantity": str(mark.exchange_quantity),
                    "mark_price": str(mark.mark_price),
                    "position_side": mark.position_side.value,
                    "symbol": mark.symbol,
                }
                for mark in sorted(
                    position_marks,
                    key=lambda item: (item.symbol, item.position_side.value),
                )
            ],
            "operating_envelope": str(operating_envelope_usdt),
            "realized_net_pnl": str(realized_net_pnl),
            "total_pnl": str(total_pnl),
            "unrealized_pnl": str(unrealized_pnl),
            "wallet": str(exchange_wallet_balance_usdt),
        }
        evidence_hash = _digest(evidence)
        event_id = _digest(
            {
                "evidence_hash": evidence_hash,
                "observed_at": observed_at.isoformat(),
            }
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("copy-leader-pnl-ledger",),
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.account_valuation_events(
                      valuation_event_id,exchange_wallet_balance_usdt,
                      exchange_margin_balance_usdt,exchange_available_balance_usdt,
                      envelope_baseline_usdt,operating_envelope_usdt,
                      logical_equity_usdt,logical_available_usdt,
                      realized_net_pnl_usdt,unrealized_pnl_usdt,total_pnl_usdt,
                      total_initial_margin_usdt,total_maintenance_margin_usdt,
                      evidence_hash,observed_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (valuation_event_id) DO NOTHING
                    """,
                    (
                        event_id,
                        exchange_wallet_balance_usdt,
                        exchange_margin_balance_usdt,
                        exchange_available_balance_usdt,
                        envelope_baseline_usdt,
                        operating_envelope_usdt,
                        logical_equity,
                        logical_available,
                        realized_net_pnl,
                        unrealized_pnl,
                        total_pnl,
                        total_initial_margin_usdt,
                        total_maintenance_margin_usdt,
                        evidence_hash,
                        observed_at,
                    ),
                )
                for mark in position_marks:
                    mark_evidence = {
                        "exchange_quantity": str(mark.exchange_quantity),
                        "mark_price": str(mark.mark_price),
                        "position_side": mark.position_side.value,
                        "symbol": mark.symbol,
                        "valuation_event_id": event_id,
                    }
                    cursor.execute(
                        """
                        INSERT INTO copytrading.account_position_mark_events(
                          mark_event_id,valuation_event_id,symbol,position_side,
                          exchange_quantity,mark_price,observed_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (valuation_event_id,symbol,position_side) DO NOTHING
                        """,
                        (
                            _digest(mark_evidence),
                            event_id,
                            mark.symbol,
                            mark.position_side.value,
                            mark.exchange_quantity,
                            mark.mark_price,
                            observed_at,
                        ),
                    )
                marks_by_key = {
                    (mark.symbol, mark.position_side.value): mark.mark_price
                    for mark in position_marks
                }
                by_leader: dict[str, dict[str, Decimal | bool]] = {}
                by_line: dict[str, dict[str, Decimal | bool]] = {
                    slot.value: {
                        "realized": Decimal("0"),
                        "unrealized": Decimal("0"),
                        "mark_complete": True,
                    }
                    for slot in LeaderSlot
                }
                cursor.execute(
                    """
                    SELECT coalesce(correction.corrected_slot,pnl.slot) AS slot,
                           coalesce(sum(pnl.realized_pnl_delta_usdt),0) AS realized
                      FROM copytrading.leader_pnl_events AS pnl
                      LEFT JOIN copytrading.leader_pnl_slot_correction_events AS correction
                        USING(pnl_event_id)
                     GROUP BY coalesce(correction.corrected_slot,pnl.slot)
                    """,
                    (),
                )
                for row in cursor.fetchall():
                    by_line[str(row["slot"])]["realized"] = Decimal(str(row["realized"]))
                cursor.execute(
                    """
                    SELECT DISTINCT ON (lead_portfolio_id,symbol,position_side)
                           pnl.lead_portfolio_id,pnl.symbol,pnl.position_side,
                           coalesce(correction.corrected_slot,pnl.slot) AS slot,
                           pnl.resulting_quantity,pnl.resulting_average_entry_price,
                           pnl.cumulative_realized_pnl_usdt
                      FROM copytrading.leader_pnl_events AS pnl
                      LEFT JOIN copytrading.leader_pnl_slot_correction_events AS correction
                        USING(pnl_event_id)
                     ORDER BY lead_portfolio_id,symbol,position_side,
                              pnl.observed_at DESC,pnl.pnl_event_id DESC
                    """,
                    (),
                )
                for row in cursor.fetchall():
                    leader_id = str(row["lead_portfolio_id"])
                    totals = by_leader.setdefault(
                        leader_id,
                        {
                            "realized": Decimal("0"),
                            "unrealized": Decimal("0"),
                            "mark_complete": True,
                        },
                    )
                    totals["realized"] = Decimal(str(totals["realized"])) + Decimal(
                        str(row["cumulative_realized_pnl_usdt"])
                    )
                    quantity = Decimal(str(row["resulting_quantity"]))
                    if quantity <= 0:
                        continue
                    line_totals = by_line[str(row["slot"])]
                    mark_price = marks_by_key.get((str(row["symbol"]), str(row["position_side"])))
                    if mark_price is None:
                        totals["mark_complete"] = False
                        line_totals["mark_complete"] = False
                        continue
                    entry_price = Decimal(str(row["resulting_average_entry_price"]))
                    multiplier = (
                        Decimal("1")
                        if str(row["position_side"]) == PositionSide.LONG.value
                        else Decimal("-1")
                    )
                    totals["unrealized"] = Decimal(str(totals["unrealized"])) + (
                        (mark_price - entry_price) * quantity * multiplier
                    )
                    line_totals["unrealized"] = Decimal(str(line_totals["unrealized"])) + (
                        (mark_price - entry_price) * quantity * multiplier
                    )
                for leader_id, totals in by_leader.items():
                    realized = Decimal(str(totals["realized"]))
                    unrealized = Decimal(str(totals["unrealized"]))
                    complete = bool(totals["mark_complete"])
                    leader_event_id = _digest(
                        {
                            "lead_portfolio_id": leader_id,
                            "valuation_event_id": event_id,
                        }
                    )
                    cursor.execute(
                        """
                        INSERT INTO copytrading.leader_valuation_events(
                          leader_valuation_event_id,valuation_event_id,
                          lead_portfolio_id,realized_pnl_usdt,unrealized_pnl_usdt,
                          total_pnl_usdt,mark_complete,observed_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (valuation_event_id,lead_portfolio_id) DO NOTHING
                        """,
                        (
                            leader_event_id,
                            event_id,
                            leader_id,
                            realized,
                            unrealized,
                            realized + unrealized,
                            complete,
                            observed_at,
                        ),
                    )
                for slot, totals in by_line.items():
                    realized = Decimal(str(totals["realized"]))
                    unrealized = Decimal(str(totals["unrealized"]))
                    complete = bool(totals["mark_complete"])
                    line_event_id = _digest({"slot": slot, "valuation_event_id": event_id})
                    cursor.execute(
                        """
                        INSERT INTO copytrading.line_valuation_events(
                          line_valuation_event_id,valuation_event_id,slot,
                          realized_pnl_usdt,unrealized_pnl_usdt,total_pnl_usdt,
                          mark_complete,observed_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (valuation_event_id,slot) DO NOTHING
                        """,
                        (
                            line_event_id,
                            event_id,
                            slot,
                            realized,
                            unrealized,
                            realized + unrealized,
                            complete,
                            observed_at,
                        ),
                    )
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_ACCOUNT_VALUATION_WRITE_FAILED") from error
        return event_id

    def record_virtual_position(
        self,
        signal: NormalizedSignal,
        *,
        previous: VirtualPosition,
        updated: VirtualPosition,
        reference_price: Decimal,
        leverage: int,
        occurred_at: datetime,
    ) -> None:
        _require_utc(occurred_at)
        local_delta = updated.local_quantity - previous.local_quantity
        source_delta = updated.observed_source_quantity - previous.observed_source_quantity
        expected_key = VirtualPositionKey(
            signal.lead_portfolio_id,
            signal.symbol,
            signal.position_side,
        )
        if (
            previous.key != expected_key
            or updated.key != expected_key
            or local_delta == 0
            or (signal.kind is SignalKind.INCREASE) != (local_delta > 0)
            or not reference_price.is_finite()
            or reference_price <= 0
            or not 1 <= leverage <= 125
        ):
            raise ValueError("copy virtual position fill is invalid")
        event_type = "INCREASE" if local_delta > 0 else "REDUCE"
        margin = (updated.local_quantity * reference_price) / Decimal(leverage)
        evidence_hash = _digest(
            {
                "local_delta": str(local_delta),
                "local_result": str(updated.local_quantity),
                "signal_id": signal.signal_id,
                "source_delta": str(source_delta),
                "source_result": str(updated.observed_source_quantity),
            }
        )
        event_id = _digest(
            {
                "evidence_hash": evidence_hash,
                "occurred_at": occurred_at.isoformat(),
            }
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("copy-leader-pnl-ledger",),
                )
                cursor.execute(
                    "SELECT 1 FROM copytrading.virtual_position_events WHERE signal_id=%s",
                    (signal.signal_id,),
                )
                if cursor.fetchone() is not None:
                    return
                cursor.execute(
                    """
                    SELECT coalesce(correction.corrected_slot,pnl.slot) AS slot,
                           pnl.resulting_quantity,pnl.resulting_average_entry_price,
                           pnl.cumulative_realized_pnl_usdt
                      FROM copytrading.leader_pnl_events AS pnl
                      LEFT JOIN copytrading.leader_pnl_slot_correction_events AS correction
                        USING(pnl_event_id)
                     WHERE pnl.lead_portfolio_id=%s AND pnl.symbol=%s
                       AND pnl.position_side=%s
                     ORDER BY pnl.observed_at DESC,pnl.pnl_event_id DESC LIMIT 1
                    """,
                    (
                        signal.lead_portfolio_id,
                        signal.symbol,
                        signal.position_side.value,
                    ),
                )
                previous_pnl = cursor.fetchone()
                if previous_pnl is None:
                    if previous.local_quantity != 0:
                        raise CopyRepositoryError("COPY_LEADER_PNL_BASELINE_MISSING")
                    previous_average = Decimal("0")
                    cumulative_realized = Decimal("0")
                else:
                    recorded_quantity = Decimal(str(previous_pnl["resulting_quantity"]))
                    if recorded_quantity != previous.local_quantity:
                        raise CopyRepositoryError("COPY_LEADER_PNL_QUANTITY_MISMATCH")
                    previous_average = Decimal(str(previous_pnl["resulting_average_entry_price"]))
                    cumulative_realized = Decimal(str(previous_pnl["cumulative_realized_pnl_usdt"]))
                if previous.local_quantity > 0:
                    if previous_pnl is None:
                        raise CopyRepositoryError("COPY_LEADER_PNL_BASELINE_MISSING")
                    # Increases and reductions within one open-position lifecycle must
                    # remain on the line that owned the opening fill, even if a bad slot
                    # rotation temporarily moved the leader elsewhere.
                    slot = LeaderSlot(str(previous_pnl["slot"]))
                else:
                    cursor.execute(
                        """
                        SELECT slot FROM copytrading.leader_slot_events
                         WHERE action='ASSIGNED' AND lead_portfolio_id=%s
                           AND occurred_at<=%s
                         ORDER BY occurred_at DESC,slot_event_id DESC LIMIT 1
                        """,
                        (signal.lead_portfolio_id, occurred_at),
                    )
                    slot_row = cursor.fetchone()
                    if slot_row is None:
                        raise CopyRepositoryError("COPY_LEADER_PNL_SLOT_MISSING")
                    slot = LeaderSlot(str(slot_row["slot"]))
                if local_delta > 0:
                    resulting_average = (
                        (previous.local_quantity * previous_average)
                        + (local_delta * reference_price)
                    ) / updated.local_quantity
                    realized_delta = Decimal("0")
                else:
                    reduced_quantity = -local_delta
                    if reduced_quantity > previous.local_quantity or previous_average <= 0:
                        raise CopyRepositoryError("COPY_LEADER_PNL_REDUCTION_INVALID")
                    direction = (
                        Decimal("1") if signal.position_side is PositionSide.LONG else Decimal("-1")
                    )
                    realized_delta = (
                        (reference_price - previous_average) * reduced_quantity * direction
                    )
                    resulting_average = (
                        previous_average if updated.local_quantity > 0 else Decimal("0")
                    )
                cumulative_result = cumulative_realized + realized_delta
                cursor.execute(
                    """
                    INSERT INTO copytrading.virtual_position_events(
                      position_event_id,lead_portfolio_id,symbol,position_side,event_type,
                      local_quantity_delta,resulting_local_quantity,source_quantity_delta,
                      resulting_source_quantity,reference_price,leverage,
                      committed_margin_usdt,signal_id,evidence_hash,occurred_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (signal_id) DO NOTHING
                    """,
                    (
                        event_id,
                        signal.lead_portfolio_id,
                        signal.symbol,
                        signal.position_side.value,
                        event_type,
                        local_delta,
                        updated.local_quantity,
                        source_delta,
                        updated.observed_source_quantity,
                        reference_price,
                        leverage,
                        margin,
                        signal.signal_id,
                        evidence_hash,
                        occurred_at,
                    ),
                )
                pnl_evidence = {
                    "fill_price": str(reference_price),
                    "local_delta": str(local_delta),
                    "previous_average": str(previous_average),
                    "previous_quantity": str(previous.local_quantity),
                    "realized_delta": str(realized_delta),
                    "resulting_average": str(resulting_average),
                    "resulting_quantity": str(updated.local_quantity),
                    "signal_id": signal.signal_id,
                    "slot": slot.value,
                }
                cursor.execute(
                    """
                    INSERT INTO copytrading.leader_pnl_events(
                      pnl_event_id,position_event_id,signal_id,lead_portfolio_id,slot,
                      symbol,position_side,event_type,local_quantity_delta,
                      fill_price,previous_quantity,resulting_quantity,
                      previous_average_entry_price,resulting_average_entry_price,
                      realized_pnl_delta_usdt,cumulative_realized_pnl_usdt,
                      evidence_hash,observed_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        _digest(pnl_evidence),
                        event_id,
                        signal.signal_id,
                        signal.lead_portfolio_id,
                        slot.value,
                        signal.symbol,
                        signal.position_side.value,
                        event_type,
                        local_delta,
                        reference_price,
                        previous.local_quantity,
                        updated.local_quantity,
                        previous_average,
                        resulting_average,
                        realized_delta,
                        cumulative_result,
                        _digest(pnl_evidence),
                        occurred_at,
                    ),
                )
        except psycopg.Error as error:
            raise CopyRepositoryError("COPY_VIRTUAL_POSITION_WRITE_FAILED") from error


def _leaders_with_owned_exposure(
    cursor: psycopg.Cursor[dict[str, Any]],
) -> set[str]:
    cursor.execute(
        """
        WITH latest_position AS (
          SELECT DISTINCT ON (lead_portfolio_id,symbol,position_side)
                 lead_portfolio_id,resulting_local_quantity
            FROM copytrading.virtual_position_events
           ORDER BY lead_portfolio_id,symbol,position_side,
                    occurred_at DESC,position_event_id DESC
        ), latest_decision AS (
          SELECT DISTINCT ON (signal_id) signal_id,state
            FROM copytrading.signal_decision_events
           ORDER BY signal_id,occurred_at DESC,decision_event_id DESC
        )
        SELECT lead_portfolio_id FROM latest_position
         WHERE resulting_local_quantity>0
        UNION
        SELECT signal.lead_portfolio_id
          FROM latest_decision AS decision
          JOIN copytrading.submission_claims AS claim USING(signal_id)
          JOIN copytrading.signals AS signal USING(signal_id)
         WHERE signal.signal_kind='INCREASE'
           AND decision.state IN ('APPROVED','SUBMITTED','UNCERTAIN')
        """,
        (),
    )
    return {str(row["lead_portfolio_id"]) for row in cursor.fetchall()}


def _leader_owned_exposure_state(
    cursor: psycopg.Cursor[dict[str, Any]],
    lead_portfolio_id: str,
    *,
    requested_at: datetime,
) -> tuple[bool, datetime]:
    cursor.execute(
        """
        WITH latest_position AS (
          SELECT DISTINCT ON (position.symbol,position.position_side)
                 position.resulting_local_quantity,position.occurred_at,
                 coalesce(signal.occurred_at,position.occurred_at) AS source_occurred_at
            FROM copytrading.virtual_position_events AS position
            LEFT JOIN copytrading.signals AS signal USING(signal_id)
           WHERE position.lead_portfolio_id=%s
           ORDER BY position.symbol,position.position_side,position.occurred_at DESC,
                    position.position_event_id DESC
        ), latest_decision AS (
          SELECT DISTINCT ON (decision.signal_id)
                 decision.signal_id,decision.state,decision.occurred_at
            FROM copytrading.signal_decision_events AS decision
            JOIN copytrading.signals AS signal USING(signal_id)
            JOIN copytrading.submission_claims AS claim USING(signal_id)
           WHERE signal.lead_portfolio_id=%s AND signal.signal_kind='INCREASE'
           ORDER BY decision.signal_id,decision.occurred_at DESC,
                    decision.decision_event_id DESC
        )
        SELECT
          coalesce((SELECT bool_or(resulting_local_quantity>0) FROM latest_position),false)
          OR coalesce((SELECT bool_or(state IN ('APPROVED','SUBMITTED','UNCERTAIN'))
                         FROM latest_decision),false) AS has_exposure,
          greatest(
            %s,
            coalesce((SELECT max(source_occurred_at) FROM latest_position),%s),
            coalesce((SELECT max(occurred_at) FROM latest_decision),%s)
          ) AS cleared_at
        """,
        (
            lead_portfolio_id,
            lead_portfolio_id,
            requested_at,
            requested_at,
            requested_at,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        return False, requested_at
    return bool(row["has_exposure"]), row["cleared_at"]


def _leader_nickname(
    cursor: psycopg.Cursor[dict[str, Any]],
    lead_portfolio_id: str,
) -> str:
    cursor.execute(
        """
        SELECT nickname FROM copytrading.leader_snapshots
         WHERE lead_portfolio_id=%s
         ORDER BY observed_at DESC,snapshot_id DESC LIMIT 1
        """,
        (lead_portfolio_id,),
    )
    row = cursor.fetchone()
    return str(row["nickname"]) if row is not None else "名称未知"


def _latest_leader_lifecycle(
    cursor: psycopg.Cursor[dict[str, Any]],
    lead_portfolio_id: str,
) -> LeaderLifecycle | None:
    cursor.execute(
        """
        SELECT state FROM copytrading.leader_lifecycle_events
         WHERE lead_portfolio_id=%s
         ORDER BY occurred_at DESC,event_id DESC LIMIT 1
        """,
        (lead_portfolio_id,),
    )
    row = cursor.fetchone()
    return LeaderLifecycle(str(row["state"])) if row is not None else None


def _append_replacement_lifecycle(
    cursor: psycopg.Cursor[dict[str, Any]],
    lead_portfolio_id: str,
    lifecycle: LeaderLifecycle,
    replacement_id: str,
    selection_run_id: str,
    occurred_at: datetime,
) -> None:
    cursor.execute(
        """
        INSERT INTO copytrading.leader_lifecycle_events(
          event_id,lead_portfolio_id,state,selection_run_id,reason_codes,occurred_at
        ) VALUES (%s,%s,%s,%s,%s,%s)
        """,
        (
            _digest(
                {
                    "lead_portfolio_id": lead_portfolio_id,
                    "replacement_id": replacement_id,
                    "state": lifecycle.value,
                }
            ),
            lead_portfolio_id,
            lifecycle.value,
            selection_run_id,
            Jsonb(["COPY_DEFERRED_SLOT_REPLACEMENT"]),
            occurred_at,
        ),
    )


def _append_slot_replacement_terminal(
    cursor: psycopg.Cursor[dict[str, Any]],
    replacement: Mapping[str, Any],
    *,
    state: str,
    reason_code: str,
    occurred_at: datetime,
) -> None:
    replacement_id = str(replacement["replacement_id"])
    slot = str(replacement["slot"])
    incumbent = str(replacement["incumbent_lead_portfolio_id"])
    candidate = str(replacement["candidate_lead_portfolio_id"])
    cursor.execute(
        """
        INSERT INTO copytrading.slot_replacement_events(
          replacement_event_id,replacement_id,selection_run_id,slot,
          incumbent_lead_portfolio_id,candidate_lead_portfolio_id,state,
          requested_at,expires_at,actor_id,reason_codes,occurred_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            _digest({"replacement_id": replacement_id, "state": state}),
            replacement_id,
            str(replacement["selection_run_id"]),
            slot,
            incumbent,
            candidate,
            state,
            replacement["requested_at"],
            replacement["expires_at"],
            "slot-replacement-reconciler",
            Jsonb([reason_code]),
            occurred_at,
        ),
    )
    payload = {
        "event": "copy_slot_replacement",
        "state": state,
        "slot": slot,
        "incumbent_lead_portfolio_id": incumbent,
        "incumbent_nickname": _leader_nickname(cursor, incumbent),
        "candidate_lead_portfolio_id": candidate,
        "candidate_nickname": _leader_nickname(cursor, candidate),
        "expires_at": replacement["expires_at"].isoformat(),
        "reason_codes": [reason_code],
    }
    cursor.execute(
        """
        INSERT INTO control.outbox(
          message_id,deduplication_key,topic,payload,payload_hash
        ) VALUES (%s,%s,'copy.telegram',%s,%s)
        ON CONFLICT (deduplication_key) DO NOTHING
        """,
        (
            _digest({"replacement_notification": replacement_id, "state": state}),
            f"copy-slot-replacement:{replacement_id}:{state.lower()}",
            Jsonb(payload),
            _digest(payload),
        ),
    )


def _supersede_open_slot_replacements(
    cursor: psycopg.Cursor[dict[str, Any]],
    *,
    slot: LeaderSlot,
    superseding_selection_run_id: str,
    occurred_at: datetime,
) -> None:
    cursor.execute(
        """
        WITH latest AS (
          SELECT DISTINCT ON (replacement_id) *
            FROM copytrading.slot_replacement_events
           WHERE slot=%s
           ORDER BY replacement_id,occurred_at DESC,replacement_event_id DESC
        )
        SELECT * FROM latest
         WHERE state='REQUESTED' AND selection_run_id<>%s
        """,
        (slot.value, superseding_selection_run_id),
    )
    for row in cursor.fetchall():
        _append_slot_replacement_terminal(
            cursor,
            row,
            state="SUPERSEDED",
            reason_code="COPY_SLOT_REPLACEMENT_SUPERSEDED_BY_NEW_SELECTION",
            occurred_at=occurred_at,
        )


def _locked_leader_ids(
    cursor: psycopg.Cursor[dict[str, Any]],
) -> frozenset[str]:
    cursor.execute(
        """
        SELECT lead_portfolio_id FROM (
          SELECT DISTINCT ON (lead_portfolio_id)
                 lead_portfolio_id,state
            FROM copytrading.leader_lock_events
           ORDER BY lead_portfolio_id,occurred_at DESC,lock_event_id DESC
        ) AS latest
         WHERE state='LOCKED'
        """,
        (),
    )
    return frozenset(str(row["lead_portfolio_id"]) for row in cursor.fetchall())


def _digest(document: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _relative_change_pct(current: Decimal, baseline: Decimal) -> Decimal:
    if baseline == 0:
        return Decimal("0") if current == 0 else Decimal("100")
    return ((current - baseline) / abs(baseline) * Decimal("100")).quantize(Decimal("0.000001"))


def _is_source_order_baseline(
    *,
    baseline: bool,
    has_previous: bool,
    update_time_ms: int,
    maximum_update_time_ms: int,
    matches_ambiguous_baseline: bool,
) -> bool:
    """Suppress a resolved replay of an already stored ambiguous baseline row."""

    return (
        baseline
        or matches_ambiguous_baseline
        or (not has_previous and update_time_ms < maximum_update_time_ms)
    )


def _source_epoch_order(
    order: PublicLeaderOrder,
    *,
    reset_event_id: str | None,
) -> PublicLeaderOrder:
    if reset_event_id is None:
        return order
    identity_key = _digest(
        {
            "reset_event_id": reset_event_id,
            "source_identity_key": order.identity_key,
        }
    )
    event_key = _digest(
        {
            "reset_event_id": reset_event_id,
            "source_event_key": order.event_key,
        }
    )
    return replace(order, identity_key=identity_key, event_key=event_key)


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("copy repository time must be timezone-aware UTC")
