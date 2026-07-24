"""PostgreSQL state, dashboard views, and control challenges for Telegram."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ai_quant.copy_trading.allocation import (
    DEFAULT_ENTRY_MARGIN_LIMIT_USDT,
    MINIMUM_ENTRY_MARGIN_LIMIT_USDT,
)
from ai_quant.copy_trading.leader_slots import LeaderSlot, is_custom_slot, leader_slot_label
from ai_quant.copy_trading.models import LeaderLifecycle
from ai_quant.copy_trading.reason_text import (
    reason_code_text,
    translate_reason_codes_in_text,
)
from ai_quant.copy_trading.repository import CopyRepositoryError, CopyTradingRepository
from ai_quant.copy_trading.risk import (
    available_entry_margin_balance,
    logical_available_balance,
)
from ai_quant.copy_trading.telegram_format import (
    compact_decimal,
    compact_money,
    signed_money,
    signed_percent,
)
from ai_quant.notifications.telegram_bot import (
    ControlAction,
    EntryMarginLimitProposal,
    FollowMultiplierProposal,
    LeaderCandidateChoice,
    LeaderChangeProposal,
    LeaderLockChoice,
    LeaderLockProposal,
    LeaderMultiplierChoice,
    LeaderPnlChoice,
    PositionCloseChoice,
    PositionCloseProposal,
    PositionLeaderChoice,
    bounded_telegram_text,
)


class TelegramStateError(RuntimeError):
    """Telegram state could not be persisted or rendered."""


_CARD_DIVIDER = "────────"


@dataclass(frozen=True, slots=True)
class OutboundTelegramMessage:
    message_id: str
    text: str
    contextual_view: str | None
    restore_navigation_keyboard: bool = False


class PostgresTelegramState:
    def __init__(self, dsn: str, *, execution_environment: str = "TESTNET") -> None:
        if not dsn:
            raise ValueError("Telegram state database DSN is required")
        if execution_environment not in {"TESTNET", "PRODUCTION"}:
            raise ValueError("Telegram execution environment is invalid")
        self._dsn = dsn
        self._execution_environment = execution_environment
        self._environment_label = "正式盘" if execution_environment == "PRODUCTION" else "测试盘"

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def next_offset(self) -> int:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT next_offset FROM copytrading.telegram_offset_events
                     ORDER BY occurred_at DESC,offset_event_id DESC LIMIT 1
                    """,
                    (),
                )
                row = cursor.fetchone()
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_OFFSET_READ_FAILED") from error
        return int(row["next_offset"]) if row else 0

    def claim_notifications(self, *, limit: int = 20) -> tuple[OutboundTelegramMessage, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Telegram notification claim limit is invalid")
        messages: list[OutboundTelegramMessage] = []
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE control.outbox SET status='PENDING',available_at=now()
                     WHERE topic='copy.telegram' AND status='CLAIMED'
                       AND available_at < now() - interval '2 minutes'
                    """,
                    (),
                )
                cursor.execute(
                    """
                    WITH selected AS (
                      SELECT message_id FROM control.outbox
                       WHERE topic='copy.telegram' AND status='PENDING'
                         AND available_at <= now()
                       ORDER BY created_at
                       FOR UPDATE SKIP LOCKED LIMIT %s
                    )
                    UPDATE control.outbox AS outbox
                       SET status='CLAIMED',attempts=attempts+1,available_at=now()
                      FROM selected
                     WHERE outbox.message_id=selected.message_id
                    RETURNING outbox.message_id,outbox.payload
                    """,
                    (limit,),
                )
                for row in cursor.fetchall():
                    payload = row["payload"]
                    if not isinstance(payload, dict):
                        raise TelegramStateError("TELEGRAM_OUTBOX_PAYLOAD_INVALID")
                    messages.append(
                        OutboundTelegramMessage(
                            message_id=str(row["message_id"]),
                            text=_notification_text(
                                payload,
                                environment_label=self._environment_label,
                            ),
                            contextual_view=_notification_contextual_view(payload),
                            restore_navigation_keyboard=(
                                _notification_restores_navigation_keyboard(payload)
                            ),
                        )
                    )
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_OUTBOX_CLAIM_FAILED") from error
        return tuple(messages)

    def notification_message(self, token: str) -> tuple[str, str] | None:
        """Recover the original notification after its Telegram message was edited."""

        if not re.fullmatch(r"[0-9a-f]{16}", token):
            return None
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT payload FROM control.outbox
                     WHERE topic='copy.telegram' AND left(message_id,16)=%s
                     ORDER BY created_at DESC LIMIT 2
                    """,
                    (token,),
                )
                rows = cursor.fetchall()
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_NOTIFICATION_READ_FAILED") from error
        if len(rows) != 1:
            return None
        payload = rows[0]["payload"]
        if not isinstance(payload, dict):
            return None
        contextual_view = _notification_contextual_view(payload)
        if contextual_view is None:
            return None
        return (
            _notification_text(payload, environment_label=self._environment_label),
            contextual_view,
        )

    def complete_notification(self, message_id: str, *, delivered: bool) -> None:
        if len(message_id) != 64:
            raise ValueError("Telegram outbox message ID is invalid")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                if delivered:
                    cursor.execute(
                        """
                        UPDATE control.outbox SET status='PUBLISHED',published_at=now()
                         WHERE message_id=%s AND status='CLAIMED'
                        """,
                        (message_id,),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE control.outbox
                           SET status=CASE WHEN attempts>=5 THEN 'DEAD' ELSE 'PENDING' END,
                               available_at=now()+interval '30 seconds'
                         WHERE message_id=%s AND status='CLAIMED'
                        """,
                        (message_id,),
                    )
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_OUTBOX_COMPLETE_FAILED") from error

    def record_update(
        self,
        update: Mapping[str, Any],
        *,
        chat_id: int | None,
        user_id: int | None,
        authorized: bool,
        processed_at: datetime,
    ) -> None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or update_id < 0:
            raise ValueError("Telegram update ID is invalid")
        _require_utc(processed_at)
        kind = "callback_query" if "callback_query" in update else "message"
        payload_hash = _digest(update)
        next_offset = update_id + 1
        offset_event_id = _digest(
            {
                "next_offset": next_offset,
                "processed_at": processed_at.isoformat(),
                "update_id": update_id,
            }
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO copytrading.telegram_update_events(
                      update_id,chat_id,user_id,update_kind,authorized,
                      payload_hash,processed_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        update_id,
                        chat_id,
                        user_id,
                        kind,
                        authorized,
                        payload_hash,
                        processed_at,
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.telegram_offset_events(
                      offset_event_id,next_offset,occurred_at
                    ) VALUES (%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (offset_event_id, next_offset, processed_at),
                )
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_UPDATE_WRITE_FAILED") from error

    def create(self, *, user_id: int, action: ControlAction) -> str:
        if user_id <= 0:
            raise ValueError("Telegram control user ID is invalid")
        now = datetime.now(UTC)
        nonce = secrets.token_urlsafe(12)
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        challenge_id = _digest(
            {
                "action": action.value,
                "created_at": now.isoformat(),
                "nonce_hash": nonce_hash,
                "user_id": user_id,
            }
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO copytrading.telegram_control_challenges(
                      challenge_id,user_id,action,nonce_hash,expires_at,created_at
                    ) VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        challenge_id,
                        user_id,
                        action.value,
                        nonce_hash,
                        now + timedelta(minutes=2),
                        now,
                    ),
                )
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_CHALLENGE_WRITE_FAILED") from error
        return nonce

    def consume(self, *, user_id: int, nonce: str) -> ControlAction | None:
        if user_id <= 0 or not 8 <= len(nonce) <= 32 or not nonce.isascii():
            return None
        now = datetime.now(UTC)
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"telegram-challenge:{nonce_hash}",),
                )
                cursor.execute(
                    """
                    SELECT challenge.challenge_id,challenge.action
                      FROM copytrading.telegram_control_challenges AS challenge
                      LEFT JOIN copytrading.telegram_challenge_consumptions AS consumption
                        USING (challenge_id)
                     WHERE challenge.nonce_hash=%s AND challenge.user_id=%s
                       AND challenge.expires_at>%s AND consumption.challenge_id IS NULL
                    """,
                    (nonce_hash, user_id, now),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                consumption_id = _digest(
                    {
                        "challenge_id": row["challenge_id"],
                        "consumed_at": now.isoformat(),
                    }
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.telegram_challenge_consumptions(
                      consumption_id,challenge_id,user_id,consumed_at
                    ) VALUES (%s,%s,%s,%s)
                    ON CONFLICT (challenge_id) DO NOTHING
                    RETURNING consumption_id
                    """,
                    (consumption_id, row["challenge_id"], user_id, now),
                )
                if cursor.fetchone() is None:
                    return None
                return ControlAction(str(row["action"]))
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_CHALLENGE_CONSUME_FAILED") from error

    def execute(self, *, user_id: int, action: ControlAction) -> str:
        if action is ControlAction.RESET_ACCOUNT_SUMMARY:
            return self._reset_account_summary(user_id=user_id, occurred_at=datetime.now(UTC))
        state = _control_state(action)
        now = datetime.now(UTC)
        event_id = _digest(
            {
                "actor_id": f"telegram:{user_id}",
                "occurred_at": now.isoformat(),
                "state": state,
            }
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO copytrading.runtime_control_events(
                      control_event_id,state,actor_id,reason_codes,occurred_at
                    ) VALUES (%s,%s,%s,%s,%s)
                    """,
                    (
                        event_id,
                        state,
                        f"telegram:{user_id}",
                        Jsonb([f"TELEGRAM_{action.value.upper()}"]),
                        now,
                    ),
                )
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_CONTROL_WRITE_FAILED") from error
        return _control_message(action, environment_label=self._environment_label)

    def execute_confirmed(self, *, user_id: int, nonce: str) -> str | None:
        """Consume a challenge and append its control event in one transaction."""
        if user_id <= 0 or not 8 <= len(nonce) <= 32 or not nonce.isascii():
            return None
        now = datetime.now(UTC)
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        result_message: str | None = None
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"telegram-challenge:{nonce_hash}",),
                )
                cursor.execute(
                    """
                    SELECT challenge.challenge_id,challenge.action
                      FROM copytrading.telegram_control_challenges AS challenge
                      LEFT JOIN copytrading.telegram_challenge_consumptions AS consumption
                        USING (challenge_id)
                     WHERE challenge.nonce_hash=%s AND challenge.user_id=%s
                       AND challenge.expires_at>%s AND consumption.challenge_id IS NULL
                    """,
                    (nonce_hash, user_id, now),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                action = ControlAction(str(row["action"]))
                challenge_id = str(row["challenge_id"])
                if action is ControlAction.RESET_ACCOUNT_SUMMARY:
                    result_message = self._reset_account_summary(
                        user_id=user_id,
                        occurred_at=now,
                    )
                else:
                    state = _control_state(action)
                cursor.execute(
                    """
                    INSERT INTO copytrading.telegram_challenge_consumptions(
                      consumption_id,challenge_id,user_id,consumed_at
                    ) VALUES (%s,%s,%s,%s)
                    ON CONFLICT (challenge_id) DO NOTHING
                    RETURNING consumption_id
                    """,
                    (
                        _digest({"challenge_id": challenge_id, "consumed_at": now.isoformat()}),
                        challenge_id,
                        user_id,
                        now,
                    ),
                )
                if cursor.fetchone() is None:
                    return None
                if action is not ControlAction.RESET_ACCOUNT_SUMMARY:
                    event_id = _digest(
                        {
                            "actor_id": f"telegram:{user_id}",
                            "challenge_id": challenge_id,
                            "state": state,
                        }
                    )
                    cursor.execute(
                        """
                        INSERT INTO copytrading.runtime_control_events(
                          control_event_id,state,actor_id,reason_codes,occurred_at
                        ) VALUES (%s,%s,%s,%s,%s)
                        """,
                        (
                            event_id,
                            state,
                            f"telegram:{user_id}",
                            Jsonb([f"TELEGRAM_{action.value.upper()}"]),
                            now,
                        ),
                    )
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_CONFIRMED_CONTROL_WRITE_FAILED") from error
        return result_message or _control_message(
            action,
            environment_label=self._environment_label,
        )

    def _reset_account_summary(self, *, user_id: int, occurred_at: datetime) -> str:
        try:
            CopyTradingRepository(self._dsn).reset_pnl_baseline(
                actor_id=f"telegram:{user_id}",
                occurred_at=occurred_at,
            )
        except CopyRepositoryError as error:
            raise TelegramStateError("TELEGRAM_ACCOUNT_SUMMARY_RESET_FAILED") from error
        reset_time = occurred_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M:%S")
        return (
            "✅ 账户汇总已初始化\n"
            "当前净值已重新以 150 U 为起点; 今日、本月、累计、各条线、"
            "各带单员和各仓位盈亏均从现在重新计为 0。\n"
            f"统计起点: {reset_time}\n"
            "可用开仓保证金余额已按“共享上限减已成交占用和待入场预留”重算; "
            "仓位、订单、带单员与额度配置均未修改。\n"
            "说明: 归零后现有仓位仍在交易, 后续浮动盈亏会从 0 继续实时变化。"
        )

    def position_leader_choices(self) -> tuple[PositionLeaderChoice, ...]:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH latest_slots AS (
                      SELECT DISTINCT ON (slot) slot,action,lead_portfolio_id
                        FROM copytrading.leader_slot_events
                       ORDER BY slot,occurred_at DESC,slot_event_id DESC
                    ), assigned AS (
                      SELECT DISTINCT ON (lead_portfolio_id) lead_portfolio_id,slot
                        FROM latest_slots
                       WHERE action='ASSIGNED' AND lead_portfolio_id IS NOT NULL
                       ORDER BY lead_portfolio_id,
                         CASE slot
                           WHEN 'LONG_TERM' THEN 0
                           WHEN 'SHORT_TERM_1' THEN 1
                           WHEN 'SHORT_TERM_2' THEN 2
                           WHEN 'CUSTOM_1' THEN 3
                           WHEN 'CUSTOM_2' THEN 4
                           WHEN 'CUSTOM_3' THEN 5
                           WHEN 'CUSTOM_4' THEN 6
                           WHEN 'CUSTOM_5' THEN 7
                           WHEN 'CUSTOM_6' THEN 8
                           WHEN 'CUSTOM_7' THEN 9
                           ELSE 10
                         END
                    ), latest_positions AS (
                      SELECT DISTINCT ON (lead_portfolio_id,symbol,position_side)
                             lead_portfolio_id,symbol,position_side,
                             resulting_local_quantity
                        FROM copytrading.virtual_position_events
                       ORDER BY lead_portfolio_id,symbol,position_side,
                                occurred_at DESC,position_event_id DESC
                    ), position_counts AS (
                      SELECT lead_portfolio_id,count(*) AS position_count
                        FROM latest_positions
                       WHERE resulting_local_quantity>0
                       GROUP BY lead_portfolio_id
                    ), leaders AS (
                      SELECT lead_portfolio_id FROM assigned
                      UNION
                      SELECT lead_portfolio_id FROM position_counts
                    ), snapshot AS (
                      SELECT DISTINCT ON (lead_portfolio_id) lead_portfolio_id,nickname
                        FROM copytrading.leader_snapshots
                       ORDER BY lead_portfolio_id,observed_at DESC,snapshot_id DESC
                    )
                    SELECT leaders.lead_portfolio_id,assigned.slot,snapshot.nickname,
                           coalesce(position_counts.position_count,0) AS position_count
                      FROM leaders
                      LEFT JOIN assigned USING(lead_portfolio_id)
                      LEFT JOIN snapshot USING(lead_portfolio_id)
                      LEFT JOIN position_counts USING(lead_portfolio_id)
                     ORDER BY
                       CASE assigned.slot
                         WHEN 'LONG_TERM' THEN 0
                         WHEN 'SHORT_TERM_1' THEN 1
                         WHEN 'SHORT_TERM_2' THEN 2
                         WHEN 'CUSTOM_1' THEN 3
                         WHEN 'CUSTOM_2' THEN 4
                         WHEN 'CUSTOM_3' THEN 5
                         WHEN 'CUSTOM_4' THEN 6
                         WHEN 'CUSTOM_5' THEN 7
                         WHEN 'CUSTOM_6' THEN 8
                         WHEN 'CUSTOM_7' THEN 9
                         ELSE 10
                       END,
                       leaders.lead_portfolio_id
                    """,
                    (),
                )
                rows = list(cursor.fetchall())
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_POSITION_LEADERS_READ_FAILED") from error
        choices: list[PositionLeaderChoice] = []
        for row in rows:
            slot = (
                leader_slot_label(LeaderSlot(str(row["slot"])))
                if row["slot"] is not None
                else "排空"
            )
            nickname = _safe_text(str(row["nickname"] or "名称未知"), 16)
            count = int(row["position_count"])
            choices.append(
                PositionLeaderChoice(
                    lead_portfolio_id=str(row["lead_portfolio_id"]),
                    button_label=f"{slot} · {nickname} · {count} 仓",
                    open_position_count=count,
                )
            )
        return tuple(choices)

    def position_close_choices(
        self,
        *,
        lead_portfolio_id: str | None = None,
        page: int = 1,
    ) -> tuple[PositionCloseChoice, ...]:
        if not 1 <= page <= 99 or (
            lead_portfolio_id is not None and not re.fullmatch(r"[0-9]{10,24}", lead_portfolio_id)
        ):
            raise ValueError("Telegram position page is invalid")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH latest AS (
                      SELECT DISTINCT ON (lead_portfolio_id,symbol,position_side)
                             lead_portfolio_id,symbol,position_side,
                             resulting_local_quantity
                        FROM copytrading.virtual_position_events
                       ORDER BY lead_portfolio_id,symbol,position_side,
                                occurred_at DESC,position_event_id DESC
                    ), snapshot AS (
                      SELECT DISTINCT ON (lead_portfolio_id) lead_portfolio_id,nickname
                        FROM copytrading.leader_snapshots
                       ORDER BY lead_portfolio_id,observed_at DESC,snapshot_id DESC
                    )
                    SELECT latest.*,snapshot.nickname
                      FROM latest LEFT JOIN snapshot USING(lead_portfolio_id)
                     WHERE latest.resulting_local_quantity>0
                       AND (%s::text IS NULL OR latest.lead_portfolio_id=%s)
                     ORDER BY latest.lead_portfolio_id,latest.symbol,latest.position_side
                    """,
                    (lead_portfolio_id, lead_portfolio_id),
                )
                rows = list(cursor.fetchall())
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_POSITION_CHOICES_READ_FAILED") from error
        return tuple(
            PositionCloseChoice(
                lead_portfolio_id=str(row["lead_portfolio_id"]),
                symbol=str(row["symbol"]),
                position_side=str(row["position_side"]),
                button_label=(
                    f"{row['symbol']} · "
                    f"{'多' if row['position_side'] == 'LONG' else '空'}"
                    + (
                        ""
                        if lead_portfolio_id is not None
                        else f" · {_safe_text(str(row['nickname'] or '名称未知'), 12)}"
                    )
                ),
            )
            for row in rows[(page - 1) * 8 : page * 8 + 1]
        )

    def create_position_close(
        self,
        *,
        user_id: int,
        lead_portfolio_id: str,
        symbol: str,
        position_side: str,
    ) -> PositionCloseProposal:
        if (
            user_id <= 0
            or not re.fullmatch(r"[0-9]{10,24}", lead_portfolio_id)
            or not re.fullmatch(r"[A-Z0-9]{3,24}", symbol)
            or position_side not in {"LONG", "SHORT"}
        ):
            raise ValueError("Telegram position close target is invalid")
        now = datetime.now(UTC)
        nonce = secrets.token_urlsafe(12)
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        challenge_id = _digest(
            {
                "created_at": now.isoformat(),
                "lead_portfolio_id": lead_portfolio_id,
                "nonce_hash": nonce_hash,
                "position_side": position_side,
                "symbol": symbol,
                "user_id": user_id,
            }
        )
        expires_at = now + timedelta(minutes=2)
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                position = _current_virtual_position(
                    cursor,
                    lead_portfolio_id=lead_portfolio_id,
                    symbol=symbol,
                    position_side=position_side,
                )
                if position is None or Decimal(str(position["local_quantity"])) <= 0:
                    raise ValueError("Telegram position is already flat")
                cursor.execute(
                    """
                    INSERT INTO copytrading.telegram_position_close_challenges(
                      challenge_id,user_id,lead_portfolio_id,symbol,position_side,
                      nonce_hash,expires_at,created_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        challenge_id,
                        user_id,
                        lead_portfolio_id,
                        symbol,
                        position_side,
                        nonce_hash,
                        expires_at,
                        now,
                    ),
                )
                nickname = _leader_identity(cursor, lead_portfolio_id)
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_POSITION_CLOSE_CREATE_FAILED") from error
        side_label = "多单" if position_side == "LONG" else "空单"
        quantity = compact_decimal(position["local_quantity"])
        return PositionCloseProposal(
            nonce=nonce,
            confirmation_text=(
                "⚠️ 确认只清除此仓\n"
                f"{symbol} · {side_label} · 数量 {quantity}\n"
                f"带单员: {nickname}\n"
                "确认后按市价减仓/平仓; 其他带单员和其他仓位不受影响。"
            ),
        )

    def execute_position_close_confirmed(self, *, user_id: int, nonce: str) -> str | None:
        if user_id <= 0 or not 8 <= len(nonce) <= 32 or not nonce.isascii():
            return None
        now = datetime.now(UTC)
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"position-close:{nonce_hash}",),
                )
                cursor.execute(
                    """
                    SELECT challenge.*
                      FROM copytrading.telegram_position_close_challenges AS challenge
                      LEFT JOIN copytrading.telegram_position_close_consumptions AS consumption
                        USING(challenge_id)
                     WHERE challenge.nonce_hash=%s AND challenge.user_id=%s
                       AND challenge.expires_at>%s AND consumption.challenge_id IS NULL
                    """,
                    (nonce_hash, user_id, now),
                )
                challenge = cursor.fetchone()
                if challenge is None:
                    return None
                leader_id = str(challenge["lead_portfolio_id"])
                symbol = str(challenge["symbol"])
                position_side = str(challenge["position_side"])
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"position-close:{leader_id}:{symbol}:{position_side}",),
                )
                position = _current_virtual_position(
                    cursor,
                    lead_portfolio_id=leader_id,
                    symbol=symbol,
                    position_side=position_side,
                )
                signal_id: str | None = None
                if position is not None and Decimal(str(position["local_quantity"])) > 0:
                    if _position_close_pending(
                        cursor,
                        lead_portfolio_id=leader_id,
                        symbol=symbol,
                        position_side=position_side,
                    ):
                        result = "🕒 此仓已有清理任务, 请等待成交。"
                    else:
                        source_quantity = max(
                            Decimal(str(position["source_quantity"])),
                            Decimal(str(position["local_quantity"])),
                        )
                        identity = _digest(
                            {
                                "challenge_id": str(challenge["challenge_id"]),
                                "lead_portfolio_id": leader_id,
                                "position_side": position_side,
                                "symbol": symbol,
                            }
                        )
                        signal_id = _digest({"position_close": identity})
                        cursor.execute(
                            """
                            INSERT INTO copytrading.signals(
                              signal_id,delta_event_id,lead_portfolio_id,symbol,
                              position_side,signal_kind,source_delta_quantity,
                              reference_price,occurred_at,signal_origin
                            ) VALUES (%s,NULL,%s,%s,%s,'REDUCE',%s,1,%s,'CONTROL')
                            ON CONFLICT (signal_id) DO NOTHING
                            """,
                            (
                                signal_id,
                                leader_id,
                                symbol,
                                position_side,
                                source_quantity,
                                now,
                            ),
                        )
                        result = (
                            f"🧯 已提交单仓清理\n{symbol} · "
                            f"{'多单' if position_side == 'LONG' else '空单'}\n"
                            "仅处理所选带单员的这一仓位; 成交后会另行通知。"
                        )
                else:
                    result = "✅ 此仓已经清空, 无需重复操作。"
                consumption_id = _digest(
                    {
                        "challenge_id": str(challenge["challenge_id"]),
                        "consumed_at": now.isoformat(),
                    }
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.telegram_position_close_consumptions(
                      consumption_id,challenge_id,user_id,signal_id,consumed_at
                    ) VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (challenge_id) DO NOTHING
                    RETURNING consumption_id
                    """,
                    (
                        consumption_id,
                        str(challenge["challenge_id"]),
                        user_id,
                        signal_id,
                        now,
                    ),
                )
                return result if cursor.fetchone() is not None else None
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_POSITION_CLOSE_CONFIRM_FAILED") from error

    def create_leader_positions_close(
        self,
        *,
        user_id: int,
        lead_portfolio_id: str,
    ) -> PositionCloseProposal:
        if user_id <= 0 or not re.fullmatch(r"[0-9]{10,24}", lead_portfolio_id):
            raise ValueError("Telegram leader position close target is invalid")
        now = datetime.now(UTC)
        nonce = secrets.token_urlsafe(12)
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        expires_at = now + timedelta(minutes=2)
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"leader-position-close:{lead_portfolio_id}",),
                )
                positions = _current_leader_virtual_positions(
                    cursor,
                    lead_portfolio_id=lead_portfolio_id,
                )
                if not positions:
                    raise ValueError("Telegram leader is already flat")
                targets = [
                    {
                        "local_quantity": str(position["local_quantity"]),
                        "position_side": str(position["position_side"]),
                        "symbol": str(position["symbol"]),
                    }
                    for position in positions
                ]
                target_digest = _digest({"leader_position_close_targets": targets})
                challenge_id = _digest(
                    {
                        "created_at": now.isoformat(),
                        "lead_portfolio_id": lead_portfolio_id,
                        "nonce_hash": nonce_hash,
                        "target_digest": target_digest,
                        "user_id": user_id,
                    }
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.telegram_leader_position_close_challenges(
                      challenge_id,user_id,lead_portfolio_id,targets,target_digest,
                      nonce_hash,expires_at,created_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        challenge_id,
                        user_id,
                        lead_portfolio_id,
                        Jsonb(targets),
                        target_digest,
                        nonce_hash,
                        expires_at,
                        now,
                    ),
                )
                nickname = _leader_identity(cursor, lead_portfolio_id)
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_LEADER_POSITION_CLOSE_CREATE_FAILED") from error
        previews = [
            f"• {target['symbol']} · "
            f"{'多单' if target['position_side'] == 'LONG' else '空单'} · "
            f"数量 {compact_decimal(target['local_quantity'])}"
            for target in targets[:8]
        ]
        if len(targets) > 8:
            previews.append(f"• 另有 {len(targets) - 8} 个仓位")
        preview_text = "\n".join(previews)
        return PositionCloseProposal(
            nonce=nonce,
            confirmation_text=(
                "⚠️ 确认清空该带单员全部仓位\n"
                f"带单员: {nickname}\n"
                f"共 {len(targets)} 个已成交仓位:\n"
                f"{preview_text}\n"
                "确认后逐仓按市价减仓/平仓; 只处理这个带单员, "
                "其他带单员仓位不受影响。"
            ),
        )

    def execute_leader_positions_close_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None:
        if user_id <= 0 or not 8 <= len(nonce) <= 32 or not nonce.isascii():
            return None
        now = datetime.now(UTC)
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"leader-position-close-confirm:{nonce_hash}",),
                )
                cursor.execute(
                    """
                    SELECT challenge.*
                      FROM copytrading.telegram_leader_position_close_challenges AS challenge
                      LEFT JOIN copytrading.telegram_leader_position_close_consumptions AS used
                        USING(challenge_id)
                     WHERE challenge.nonce_hash=%s AND challenge.user_id=%s
                       AND challenge.expires_at>%s AND used.challenge_id IS NULL
                    """,
                    (nonce_hash, user_id, now),
                )
                challenge = cursor.fetchone()
                if challenge is None:
                    return None
                leader_id = str(challenge["lead_portfolio_id"])
                targets = _leader_position_close_targets(challenge["targets"])
                if _digest({"leader_position_close_targets": targets}) != str(
                    challenge["target_digest"]
                ):
                    raise TelegramStateError("TELEGRAM_LEADER_POSITION_CLOSE_TARGETS_INVALID")
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"leader-position-close:{leader_id}",),
                )
                signal_ids: list[str] = []
                pending_count = 0
                flat_count = 0
                for target in targets:
                    symbol = str(target["symbol"])
                    position_side = str(target["position_side"])
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                        (f"position-close:{leader_id}:{symbol}:{position_side}",),
                    )
                    position = _current_virtual_position(
                        cursor,
                        lead_portfolio_id=leader_id,
                        symbol=symbol,
                        position_side=position_side,
                    )
                    if position is None or Decimal(str(position["local_quantity"])) <= 0:
                        flat_count += 1
                        continue
                    if _position_close_pending(
                        cursor,
                        lead_portfolio_id=leader_id,
                        symbol=symbol,
                        position_side=position_side,
                    ):
                        pending_count += 1
                        continue
                    source_quantity = max(
                        Decimal(str(position["source_quantity"])),
                        Decimal(str(position["local_quantity"])),
                    )
                    identity = _digest(
                        {
                            "challenge_id": str(challenge["challenge_id"]),
                            "lead_portfolio_id": leader_id,
                            "position_side": position_side,
                            "scope": "leader-position-close",
                            "symbol": symbol,
                        }
                    )
                    signal_id = _digest({"position_close": identity})
                    cursor.execute(
                        """
                        INSERT INTO copytrading.signals(
                          signal_id,delta_event_id,lead_portfolio_id,symbol,
                          position_side,signal_kind,source_delta_quantity,
                          reference_price,occurred_at,signal_origin
                        ) VALUES (%s,NULL,%s,%s,%s,'REDUCE',%s,1,%s,'CONTROL')
                        ON CONFLICT (signal_id) DO NOTHING
                        RETURNING signal_id
                        """,
                        (
                            signal_id,
                            leader_id,
                            symbol,
                            position_side,
                            source_quantity,
                            now,
                        ),
                    )
                    if cursor.fetchone() is not None:
                        signal_ids.append(signal_id)
                    else:
                        pending_count += 1
                consumption_id = _digest(
                    {
                        "challenge_id": str(challenge["challenge_id"]),
                        "consumed_at": now.isoformat(),
                    }
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.telegram_leader_position_close_consumptions(
                      consumption_id,challenge_id,user_id,signal_ids,consumed_at
                    ) VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (challenge_id) DO NOTHING
                    RETURNING consumption_id
                    """,
                    (
                        consumption_id,
                        str(challenge["challenge_id"]),
                        user_id,
                        Jsonb(signal_ids),
                        now,
                    ),
                )
                if cursor.fetchone() is None:
                    return None
                nickname = _leader_identity(cursor, leader_id)
                if signal_ids:
                    result = (
                        "🧹 已提交带单员仓位清理\n"
                        f"带单员: {nickname}\n"
                        f"新建清理任务: {len(signal_ids)} 个"
                    )
                    if pending_count:
                        result += f"\n已有清理任务: {pending_count} 个"
                    if flat_count:
                        result += f"\n确认前已清空: {flat_count} 个"
                    return f"{result}\n只处理该带单员的已成交仓位; 各仓成交后会分别发送通知。"
                if pending_count:
                    return (
                        "🕒 该带单员全部仓位已有清理任务\n"
                        f"带单员: {nickname}\n请等待逐仓成交, 系统不会重复下单。"
                    )
                return (
                    "✅ 该带单员仓位已经清空\n"
                    f"带单员: {nickname}\n确认时没有剩余仓位, 无需重复操作。"
                )
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_LEADER_POSITION_CLOSE_CONFIRM_FAILED") from error

    def leader_management_text(self) -> str:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                slots = _current_slots(cursor)
                rows: dict[str, dict[str, Any]] = {}
                if slots:
                    cursor.execute(
                        """
                        WITH snapshot AS (
                          SELECT DISTINCT ON (lead_portfolio_id)
                                 lead_portfolio_id,nickname,win_rate_pct,
                                 maximum_drawdown_pct,roi_pct
                            FROM copytrading.leader_snapshots
                           ORDER BY lead_portfolio_id,observed_at DESC,snapshot_id DESC
                        ), lifecycle AS (
                          SELECT DISTINCT ON (lead_portfolio_id)
                                 lead_portfolio_id,state
                            FROM copytrading.leader_lifecycle_events
                           ORDER BY lead_portfolio_id,occurred_at DESC,event_id DESC
                        ), positions AS (
                          SELECT lead_portfolio_id,
                                 sum(resulting_local_quantity) AS quantity
                            FROM (
                              SELECT DISTINCT ON (
                                lead_portfolio_id,symbol,position_side
                              ) lead_portfolio_id,resulting_local_quantity
                                FROM copytrading.virtual_position_events
                               ORDER BY lead_portfolio_id,symbol,position_side,
                                        occurred_at DESC,position_event_id DESC
                            ) AS latest GROUP BY lead_portfolio_id
                        ), multiplier AS (
                          SELECT DISTINCT ON (lead_portfolio_id)
                                 lead_portfolio_id,multiplier
                            FROM copytrading.leader_follow_multiplier_events
                           ORDER BY lead_portfolio_id,occurred_at DESC,
                                    multiplier_event_id DESC
                        )
                        SELECT snapshot.*,lifecycle.state,
                               coalesce(positions.quantity,0) AS quantity,
                               coalesce(multiplier.multiplier,1) AS follow_multiplier
                          FROM snapshot JOIN lifecycle USING(lead_portfolio_id)
                          LEFT JOIN positions USING(lead_portfolio_id)
                          LEFT JOIN multiplier USING(lead_portfolio_id)
                         WHERE snapshot.lead_portfolio_id=ANY(%s)
                        """,
                        (list(slots.values()),),
                    )
                    rows = {str(row["lead_portfolio_id"]): row for row in cursor.fetchall()}
                locked_leader_ids = _current_leader_locks(cursor)
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_LEADER_MANAGEMENT_READ_FAILED") from error
        slot_lines: list[str] = []
        for number, slot in enumerate(LeaderSlot, start=1):
            leader_id = slots.get(slot)
            if leader_id is None:
                slot_lines.append(f"{number}. {leader_slot_label(slot)} | 空缺")
                continue
            row = rows.get(leader_id)
            if row is None:
                slot_lines.append(
                    f"{number}. {leader_slot_label(slot)} | 名称未知 | ID {leader_id} | 资料缺失"
                )
                continue
            replacement_state = (
                "手动"
                if is_custom_slot(slot)
                else ("🔒" if leader_id in locked_leader_ids else "🔓")
            )
            slot_lines.append(
                f"{number}. {leader_slot_label(slot)} | "
                f"{_safe_text(str(row['nickname']), 18)} | ID {leader_id} | "
                f"{replacement_state} | {row['follow_multiplier']}倍 | "
                f"仓 {compact_decimal(row['quantity'])}"
            )
        body = "\n".join(["🛠 带单员槽位 (10)", *slot_lines])
        return (
            f"{body}\n{_CARD_DIVIDER}\n"
            "点击下方 1-10 号按钮进入对应槽位; 🔒/🔓 表示自动换人锁定状态。\n"
            "1号长线周日评估, 2-3号短线每日评估; "
            "4-10号自定义槽位只接受手动设置, 自动选人永不修改。"
        )

    def leader_lock_choices(self) -> tuple[LeaderLockChoice, ...]:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                slots = _current_slots(cursor)
                locked_leader_ids = _current_leader_locks(cursor)
                identities = {
                    leader_id: _leader_identity(cursor, leader_id).rsplit(" (ID ", 1)[0]
                    for leader_id in dict.fromkeys(slots.values())
                }
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_LEADER_LOCK_READ_FAILED") from error
        return tuple(
            LeaderLockChoice(
                lead_portfolio_id=leader_id,
                button_label=(
                    f"{leader_slot_label(slot).split(' ', 1)[-1]} · "
                    f"{_safe_text(identities.get(leader_id, '名称未知'), 20)}"
                ),
                locked=leader_id in locked_leader_ids,
            )
            for slot in LeaderSlot
            if not is_custom_slot(slot) and (leader_id := slots.get(slot)) is not None
        )

    def leader_lock_text(self) -> str:
        choices = self.leader_lock_choices()
        cards = [
            f"{choice.button_label}\nID: {choice.lead_portfolio_id} | "
            f"自动换人: {'🔒 已锁定' if choice.locked else '🔓 未锁定'}"
            for choice in choices
        ]
        body = _render_cards(
            "🔐 带单员锁定管理",
            cards,
            empty="当前没有可锁定的在选带单员",
        )
        return (
            f"{body}\n{_CARD_DIVIDER}\n"
            "锁定只阻止定时自动选人替换该带单员, 不会暂停跟单或影响其交易。\n"
            "锁定绑定带单员 ID, 不绑定席位; 手动换人/清空仍按你的二次确认执行。"
        )

    def create_leader_lock_change(
        self,
        *,
        user_id: int,
        lead_portfolio_id: str,
        locked: bool,
    ) -> LeaderLockProposal:
        if user_id <= 0:
            raise ValueError("Telegram leader lock user ID is invalid")
        if not re.fullmatch(r"[0-9]{10,24}", lead_portfolio_id):
            raise ValueError("Telegram leader lock portfolio ID is invalid")
        now = datetime.now(UTC)
        nonce = secrets.token_urlsafe(12)
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        desired_state = "LOCKED" if locked else "UNLOCKED"
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                slots = _current_slots(cursor)
                automatic_leader_ids = {
                    leader_id for slot, leader_id in slots.items() if not is_custom_slot(slot)
                }
                if lead_portfolio_id not in automatic_leader_ids:
                    raise ValueError("Telegram leader lock target is no longer assigned")
                currently_locked = lead_portfolio_id in _current_leader_locks(cursor)
                if currently_locked == locked:
                    raise ValueError("Telegram leader lock state is unchanged")
                challenge_id = _digest(
                    {
                        "created_at": now.isoformat(),
                        "desired_state": desired_state,
                        "lead_portfolio_id": lead_portfolio_id,
                        "nonce_hash": nonce_hash,
                        "user_id": user_id,
                    }
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.telegram_leader_lock_challenges(
                      challenge_id,user_id,lead_portfolio_id,desired_state,
                      nonce_hash,expires_at,created_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        challenge_id,
                        user_id,
                        lead_portfolio_id,
                        desired_state,
                        nonce_hash,
                        now + timedelta(minutes=2),
                        now,
                    ),
                )
                identity = _leader_identity(cursor, lead_portfolio_id)
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_LEADER_LOCK_CHALLENGE_WRITE_FAILED") from error
        action = "锁定" if locked else "解锁"
        effect = (
            "定时选人和已排队的自动换人都不能替换他; 交易跟随照常运行。"
            if locked
            else "下次定时选人起恢复正常自动替换规则; 交易跟随照常运行。"
        )
        return LeaderLockProposal(
            nonce=nonce,
            confirmation_text=f"⚠️ 确认{action}带单员\n{identity}\n\n{effect}",
        )

    def execute_leader_lock_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None:
        if user_id <= 0 or not 8 <= len(nonce) <= 32 or not nonce.isascii():
            return None
        now = datetime.now(UTC)
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        result_message: str | None = None
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("copy-leader-slots",),
                )
                cursor.execute(
                    """
                    SELECT challenge.challenge_id,challenge.lead_portfolio_id,
                           challenge.desired_state
                      FROM copytrading.telegram_leader_lock_challenges AS challenge
                      LEFT JOIN copytrading.telegram_leader_lock_consumptions AS consumption
                        USING(challenge_id)
                     WHERE challenge.nonce_hash=%s AND challenge.user_id=%s
                       AND challenge.expires_at>%s AND consumption.challenge_id IS NULL
                    """,
                    (nonce_hash, user_id, now),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                challenge_id = str(row["challenge_id"])
                leader_id = str(row["lead_portfolio_id"])
                desired_state = str(row["desired_state"])
                slots = _current_slots(cursor)
                automatic_leader_ids = {
                    assigned_leader
                    for slot, assigned_leader in slots.items()
                    if not is_custom_slot(slot)
                }
                if leader_id not in automatic_leader_ids:
                    return None
                currently_locked = leader_id in _current_leader_locks(cursor)
                if currently_locked == (desired_state == "LOCKED"):
                    return None
                cursor.execute(
                    """
                    INSERT INTO copytrading.telegram_leader_lock_consumptions(
                      consumption_id,challenge_id,user_id,consumed_at
                    ) VALUES (%s,%s,%s,%s)
                    ON CONFLICT (challenge_id) DO NOTHING RETURNING consumption_id
                    """,
                    (
                        _digest(
                            {
                                "challenge_id": challenge_id,
                                "consumed_at": now.isoformat(),
                            }
                        ),
                        challenge_id,
                        user_id,
                        now,
                    ),
                )
                if cursor.fetchone() is None:
                    return None
                cursor.execute(
                    """
                    INSERT INTO copytrading.leader_lock_events(
                      lock_event_id,lead_portfolio_id,state,actor_id,
                      reason_codes,occurred_at
                    ) VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        _digest(
                            {
                                "challenge_id": challenge_id,
                                "leader_id": leader_id,
                                "state": desired_state,
                            }
                        ),
                        leader_id,
                        desired_state,
                        f"telegram:{user_id}",
                        Jsonb(
                            [
                                "TELEGRAM_LEADER_LOCKED"
                                if desired_state == "LOCKED"
                                else "TELEGRAM_LEADER_UNLOCKED"
                            ]
                        ),
                        now,
                    ),
                )
                identity = _leader_identity(cursor, leader_id)
                if desired_state == "LOCKED":
                    result_message = (
                        f"🔒 已锁定 {identity}\n"
                        "系统处理: 定时选人不能替换该带单员; 已排队的自动换人也不会执行。\n"
                        "跟单状态: 正常, 仍会继续同步和执行该带单员的交易。"
                    )
                else:
                    result_message = (
                        f"🔓 已解锁 {identity}\n"
                        "系统处理: 恢复参与后续定时自动选人; 本次不会立即换人。\n"
                        "跟单状态: 正常, 仍会继续同步和执行该带单员的交易。"
                    )
                payload = {
                    "event": "copy_leader_lock_change",
                    "lead_portfolio_id": leader_id,
                    "lock_state": desired_state,
                    "state": "SUCCEEDED",
                    "summary": result_message,
                }
                cursor.execute(
                    """
                    INSERT INTO control.outbox(
                      message_id,deduplication_key,topic,payload,payload_hash
                    ) VALUES (%s,%s,'copy.telegram',%s,%s)
                    ON CONFLICT (deduplication_key) DO NOTHING
                    """,
                    (
                        _digest({"leader_lock_change": challenge_id}),
                        f"copy-leader-lock-change:{challenge_id}",
                        Jsonb(payload),
                        _digest(payload),
                    ),
                )
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_LEADER_LOCK_CHANGE_WRITE_FAILED") from error
        return result_message

    def leader_multiplier_choices(self) -> tuple[LeaderMultiplierChoice, ...]:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                slots = _current_slots(cursor)
                leader_ids = list(dict.fromkeys(slots.values()))
                multipliers = _current_follow_multipliers(cursor, leader_ids)
                identities = {
                    leader_id: _leader_identity(cursor, leader_id).rsplit(" (ID ", 1)[0]
                    for leader_id in leader_ids
                }
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_MULTIPLIER_READ_FAILED") from error
        return tuple(
            LeaderMultiplierChoice(
                lead_portfolio_id=leader_id,
                button_label=(
                    f"{leader_slot_label(slot)} · "
                    f"{_safe_text(identities.get(leader_id, '名称未知'), 20)}"
                ),
                current_multiplier=multipliers.get(leader_id, 1),
            )
            for slot in LeaderSlot
            if (leader_id := slots.get(slot)) is not None
        )

    def leader_multiplier_text(self) -> str:
        choices = self.leader_multiplier_choices()
        lines = [
            f"{number}. {choice.button_label} | ID {choice.lead_portfolio_id} | "
            f"{choice.current_multiplier}倍"
            for number, choice in enumerate(choices, start=1)
        ]
        body = "\n".join(["📐 带单员跟单金额倍数", *(lines or ["当前没有可配置的在选带单员"])])
        return (
            f"{body}\n{_CARD_DIVIDER}\n"
            "点击下方对应序号设置倍数。\n"
            "倍数绑定带单员 ID, 不绑定席位。换人后不会继承; 未配置默认为1倍。\n"
            "只影响后续新信号, 已有仓位和待入场订单不会改变。"
        )

    def create_follow_multiplier_change(
        self,
        *,
        user_id: int,
        lead_portfolio_id: str,
        multiplier: int,
    ) -> FollowMultiplierProposal:
        if user_id <= 0:
            raise ValueError("Telegram multiplier user ID is invalid")
        if not re.fullmatch(r"[0-9]{10,24}", lead_portfolio_id):
            raise ValueError("Telegram multiplier leader ID is invalid")
        if not 1 <= multiplier <= 10:
            raise ValueError("Telegram follow multiplier is invalid")
        now = datetime.now(UTC)
        nonce = secrets.token_urlsafe(12)
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                slots = _current_slots(cursor)
                if lead_portfolio_id not in slots.values():
                    raise ValueError("Telegram multiplier leader is no longer assigned")
                current = _current_follow_multipliers(cursor, [lead_portfolio_id]).get(
                    lead_portfolio_id, 1
                )
                if current == multiplier:
                    raise ValueError("Telegram multiplier is unchanged")
                challenge_id = _digest(
                    {
                        "created_at": now.isoformat(),
                        "lead_portfolio_id": lead_portfolio_id,
                        "multiplier": multiplier,
                        "nonce_hash": nonce_hash,
                        "user_id": user_id,
                    }
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.telegram_multiplier_challenges(
                      challenge_id,user_id,lead_portfolio_id,multiplier,
                      nonce_hash,expires_at,created_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        challenge_id,
                        user_id,
                        lead_portfolio_id,
                        multiplier,
                        nonce_hash,
                        now + timedelta(minutes=2),
                        now,
                    ),
                )
                identity = _leader_identity(cursor, lead_portfolio_id)
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_MULTIPLIER_CHALLENGE_WRITE_FAILED") from error
        return FollowMultiplierProposal(
            nonce=nonce,
            confirmation_text=(
                f"⚠️ 确认跟单金额倍数\n{identity}\n"
                f"{current}倍 → {multiplier}倍\n\n"
                "仅影响该带单员后续新信号; 资金边界和交易所规则仍然生效。"
            ),
        )

    def execute_follow_multiplier_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None:
        if user_id <= 0 or not 8 <= len(nonce) <= 32 or not nonce.isascii():
            return None
        now = datetime.now(UTC)
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        result_message: str | None = None
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"copy-leader-multiplier:{nonce_hash}",),
                )
                cursor.execute(
                    """
                    SELECT challenge.challenge_id,challenge.lead_portfolio_id,
                           challenge.multiplier
                      FROM copytrading.telegram_multiplier_challenges AS challenge
                      LEFT JOIN copytrading.telegram_multiplier_consumptions AS consumption
                        USING(challenge_id)
                     WHERE challenge.nonce_hash=%s AND challenge.user_id=%s
                       AND challenge.expires_at>%s AND consumption.challenge_id IS NULL
                    """,
                    (nonce_hash, user_id, now),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                challenge_id = str(row["challenge_id"])
                leader_id = str(row["lead_portfolio_id"])
                multiplier = int(row["multiplier"])
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"copy-leader-multiplier:{leader_id}",),
                )
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("copy-leader-slots",),
                )
                # Recheck at confirmation time: a stale screen must never configure a
                # leader that has already been replaced in its slot.
                if leader_id not in _current_slots(cursor).values():
                    return None
                current = _current_follow_multipliers(cursor, [leader_id]).get(leader_id, 1)
                if current == multiplier:
                    return None
                cursor.execute(
                    """
                    INSERT INTO copytrading.telegram_multiplier_consumptions(
                      consumption_id,challenge_id,user_id,consumed_at
                    ) VALUES (%s,%s,%s,%s)
                    ON CONFLICT (challenge_id) DO NOTHING RETURNING consumption_id
                    """,
                    (
                        _digest(
                            {
                                "challenge_id": challenge_id,
                                "consumed_at": now.isoformat(),
                            }
                        ),
                        challenge_id,
                        user_id,
                        now,
                    ),
                )
                if cursor.fetchone() is None:
                    return None
                event_id = _digest(
                    {
                        "challenge_id": challenge_id,
                        "leader_id": leader_id,
                        "multiplier": multiplier,
                    }
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.leader_follow_multiplier_events(
                      multiplier_event_id,lead_portfolio_id,multiplier,actor_id,
                      reason_codes,occurred_at
                    ) VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        event_id,
                        leader_id,
                        multiplier,
                        f"telegram:{user_id}",
                        Jsonb(["TELEGRAM_LEADER_FOLLOW_MULTIPLIER_SET"]),
                        now,
                    ),
                )
                identity = _leader_identity(cursor, leader_id)
                result_message = (
                    f"✅ 已设置 {identity}: {multiplier}倍\n"
                    "仅该带单员后续新信号生效; 换人后不会带到新带单员。"
                )
                payload = {
                    "event": "copy_leader_follow_multiplier_change",
                    "lead_portfolio_id": leader_id,
                    "multiplier": multiplier,
                    "state": "SUCCEEDED",
                    "summary": result_message,
                }
                cursor.execute(
                    """
                    INSERT INTO control.outbox(
                      message_id,deduplication_key,topic,payload,payload_hash
                    ) VALUES (%s,%s,'copy.telegram',%s,%s)
                    ON CONFLICT (deduplication_key) DO NOTHING
                    """,
                    (
                        _digest({"multiplier_change": challenge_id}),
                        f"copy-multiplier-change:{challenge_id}",
                        Jsonb(payload),
                        _digest(payload),
                    ),
                )
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_MULTIPLIER_CHANGE_WRITE_FAILED") from error
        return result_message

    def entry_margin_limit(self) -> Decimal:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                return _current_entry_margin_limit(cursor)
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_ENTRY_MARGIN_LIMIT_READ_FAILED") from error

    def create_entry_margin_limit_change(
        self,
        *,
        user_id: int,
        limit_usdt: Decimal,
    ) -> EntryMarginLimitProposal:
        if user_id <= 0:
            raise ValueError("Telegram entry margin user ID is invalid")
        limit_usdt = _valid_entry_margin_limit(limit_usdt)
        now = datetime.now(UTC)
        nonce = secrets.token_urlsafe(12)
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("copy-entry-margin-limit",),
                )
                current = _current_entry_margin_limit(cursor)
                if current == limit_usdt:
                    raise ValueError("Telegram entry margin limit is unchanged")
                challenge_id = _digest(
                    {
                        "created_at": now.isoformat(),
                        "limit_usdt": str(limit_usdt),
                        "nonce_hash": nonce_hash,
                        "user_id": user_id,
                    }
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.telegram_entry_margin_challenges(
                      challenge_id,user_id,limit_usdt,nonce_hash,expires_at,created_at
                    ) VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        challenge_id,
                        user_id,
                        limit_usdt,
                        nonce_hash,
                        now + timedelta(minutes=2),
                        now,
                    ),
                )
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_ENTRY_MARGIN_CHALLENGE_WRITE_FAILED") from error
        return EntryMarginLimitProposal(
            nonce=nonce,
            confirmation_text=(
                "⚠️ 确认共享可用保证金额度\n"
                f"{compact_money(current)} U → {compact_money(limit_usdt)} U\n\n"
                "所有带单员共享此额度, 仅影响后续新开仓。已有仓位和待入场订单不会改量或强平; "
                "若当前占用已超过新额度, 剩余额度按 0 U 处理, 等仓位释放后再允许开仓。\n"
                "30 U 固定保留、单笔最多 5 U、交易所最大杠杆及带单员倍数均保持不变。"
            ),
        )

    def execute_entry_margin_limit_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None:
        if user_id <= 0 or not 8 <= len(nonce) <= 32 or not nonce.isascii():
            return None
        now = datetime.now(UTC)
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        result_message: str | None = None
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    (f"copy-entry-margin-limit:{nonce_hash}",),
                )
                cursor.execute(
                    """
                    SELECT challenge.challenge_id,challenge.limit_usdt
                      FROM copytrading.telegram_entry_margin_challenges AS challenge
                      LEFT JOIN copytrading.telegram_entry_margin_consumptions AS consumption
                        USING(challenge_id)
                     WHERE challenge.nonce_hash=%s AND challenge.user_id=%s
                       AND challenge.expires_at>%s AND consumption.challenge_id IS NULL
                    """,
                    (nonce_hash, user_id, now),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                challenge_id = str(row["challenge_id"])
                limit_usdt = _valid_entry_margin_limit(Decimal(str(row["limit_usdt"])))
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("copy-entry-margin-limit",),
                )
                current = _current_entry_margin_limit(cursor)
                if current == limit_usdt:
                    return None
                cursor.execute(
                    """
                    INSERT INTO copytrading.telegram_entry_margin_consumptions(
                      consumption_id,challenge_id,user_id,consumed_at
                    ) VALUES (%s,%s,%s,%s)
                    ON CONFLICT (challenge_id) DO NOTHING RETURNING consumption_id
                    """,
                    (
                        _digest(
                            {
                                "challenge_id": challenge_id,
                                "consumed_at": now.isoformat(),
                            }
                        ),
                        challenge_id,
                        user_id,
                        now,
                    ),
                )
                if cursor.fetchone() is None:
                    return None
                event_id = _digest(
                    {
                        "challenge_id": challenge_id,
                        "limit_usdt": str(limit_usdt),
                    }
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.entry_margin_limit_events(
                      limit_event_id,limit_usdt,actor_id,reason_codes,occurred_at
                    ) VALUES (%s,%s,%s,%s,%s)
                    """,
                    (
                        event_id,
                        limit_usdt,
                        f"telegram:{user_id}",
                        Jsonb(["TELEGRAM_ENTRY_MARGIN_LIMIT_SET"]),
                        now,
                    ),
                )
                result_message = (
                    "✅ 共享可用保证金额度已更新\n"
                    f"{compact_money(current)} U → {compact_money(limit_usdt)} U\n"
                    "所有带单员后续新开仓立即共用新额度; 已有仓位和待入场订单保持不变。"
                )
                payload = {
                    "event": "copy_entry_margin_limit_change",
                    "previous_limit_usdt": str(current),
                    "limit_usdt": str(limit_usdt),
                    "state": "SUCCEEDED",
                    "summary": result_message,
                }
                cursor.execute(
                    """
                    INSERT INTO control.outbox(
                      message_id,deduplication_key,topic,payload,payload_hash
                    ) VALUES (%s,%s,'copy.telegram',%s,%s)
                    ON CONFLICT (deduplication_key) DO NOTHING
                    """,
                    (
                        _digest({"entry_margin_limit_change": challenge_id}),
                        f"copy-entry-margin-limit-change:{challenge_id}",
                        Jsonb(payload),
                        _digest(payload),
                    ),
                )
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_ENTRY_MARGIN_LIMIT_CHANGE_FAILED") from error
        return result_message

    def leader_candidates(
        self,
        *,
        slot: LeaderSlot,
    ) -> tuple[LeaderCandidateChoice, ...]:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                slots = _current_slots(cursor)
                cursor.execute(
                    """
                    WITH snapshot AS (
                      SELECT DISTINCT ON (lead_portfolio_id)
                             lead_portfolio_id,nickname,win_rate_pct,
                             maximum_drawdown_pct,roi_pct,observed_at
                        FROM copytrading.leader_snapshots
                       ORDER BY lead_portfolio_id,observed_at DESC,snapshot_id DESC
                    ), activity AS (
                      SELECT DISTINCT ON (lead_portfolio_id)
                             lead_portfolio_id,orders_1d,orders_3d,orders_7d,
                             active_days_7d,latest_operation_time_ms,
                             profitable_close_count,losing_close_count,
                             testnet_symbol_compatibility_pct,observed_at
                        FROM copytrading.leader_activity_snapshots
                       ORDER BY lead_portfolio_id,observed_at DESC,
                                activity_snapshot_id DESC
                    )
                    SELECT snapshot.lead_portfolio_id,snapshot.nickname,
                           snapshot.win_rate_pct,snapshot.maximum_drawdown_pct,
                           snapshot.roi_pct,activity.orders_1d,activity.orders_3d,
                           activity.orders_7d,activity.active_days_7d,
                           activity.profitable_close_count,
                           activity.losing_close_count,
                           activity.testnet_symbol_compatibility_pct,
                           activity.observed_at
                      FROM snapshot JOIN activity USING(lead_portfolio_id)
                     WHERE activity.observed_at > now()-interval '7 days'
                    """,
                    (),
                )
                rows = list(cursor.fetchall())
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_LEADER_CANDIDATE_READ_FAILED") from error
        assigned_elsewhere = {
            leader_id for other_slot, leader_id in slots.items() if other_slot is not slot
        }
        rows = [
            row
            for row in rows
            if str(row["lead_portfolio_id"]) not in assigned_elsewhere
            and (is_custom_slot(slot) or int(row["testnet_symbol_compatibility_pct"]) >= 80)
        ]
        if is_custom_slot(slot):
            rows.sort(
                key=lambda row: row["observed_at"],
                reverse=True,
            )
        elif slot is not LeaderSlot.LONG_TERM:
            rows = [
                row
                for row in rows
                if int(row["orders_1d"]) >= 3
                and int(row["orders_3d"]) >= 10
                and int(row["orders_7d"]) >= 14
                and int(row["active_days_7d"]) >= 3
            ]
            rows.sort(
                key=lambda row: (
                    int(row["orders_1d"]),
                    int(row["orders_3d"]),
                    int(row["orders_7d"]),
                    row["win_rate_pct"],
                ),
                reverse=True,
            )
        else:
            rows.sort(
                key=lambda row: (
                    row["win_rate_pct"],
                    -row["maximum_drawdown_pct"],
                    row["roi_pct"],
                ),
                reverse=True,
            )
        return tuple(
            LeaderCandidateChoice(
                lead_portfolio_id=str(row["lead_portfolio_id"]),
                button_label=(f"{_safe_text(str(row['nickname']), 16)} · {row['orders_1d']}/天"),
                summary=(
                    f"• {_safe_text(str(row['nickname']), 20)} ({row['lead_portfolio_id']})\n"
                    f"  1/3/7天 {row['orders_1d']}/{row['orders_3d']}/{row['orders_7d']} 次 | "
                    f"胜率 {compact_decimal(row['win_rate_pct'])}% | "
                    f"回撤 {compact_decimal(row['maximum_drawdown_pct'])}%"
                ),
            )
            for row in rows[:6]
        )

    def create_leader_change(
        self,
        *,
        user_id: int,
        slot: LeaderSlot,
        lead_portfolio_id: str | None,
        manual_override: bool = False,
    ) -> LeaderChangeProposal:
        if user_id <= 0:
            raise ValueError("Telegram leader manager user ID is invalid")
        action = "SET" if lead_portfolio_id is not None else "REMOVE"
        if lead_portfolio_id is not None and not re.fullmatch(r"[0-9]{10,24}", lead_portfolio_id):
            raise ValueError("Telegram leader portfolio ID is invalid")
        now = datetime.now(UTC)
        nonce = secrets.token_urlsafe(12)
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                slots = _current_slots(cursor)
                if action == "REMOVE":
                    current = slots.get(slot)
                    if current is None:
                        raise ValueError("Telegram leader slot is already empty")
                    display = (
                        f"移除 {leader_slot_label(slot)} 的带单员 "
                        f"{_leader_identity(cursor, current)}"
                    )
                else:
                    if lead_portfolio_id is None:
                        raise ValueError("Telegram leader portfolio ID is required")
                    if slots.get(slot) == lead_portfolio_id:
                        raise ValueError("COPY_TELEGRAM_LEADER_ALREADY_IN_SLOT")
                    if any(
                        other_slot is not slot and value == lead_portfolio_id
                        for other_slot, value in slots.items()
                    ):
                        raise ValueError("COPY_TELEGRAM_LEADER_ASSIGNED_ELSEWHERE")
                    cursor.execute(
                        """
                        WITH snapshot AS (
                          SELECT DISTINCT ON (lead_portfolio_id)
                                 lead_portfolio_id,nickname,win_rate_pct,
                                 maximum_drawdown_pct
                            FROM copytrading.leader_snapshots
                           WHERE lead_portfolio_id=%s
                           ORDER BY lead_portfolio_id,observed_at DESC,snapshot_id DESC
                        ), activity AS (
                          SELECT DISTINCT ON (lead_portfolio_id)
                                 lead_portfolio_id,orders_1d,orders_3d,orders_7d,
                                 active_days_7d,testnet_symbol_compatibility_pct,
                                 observed_at
                            FROM copytrading.leader_activity_snapshots
                           WHERE lead_portfolio_id=%s
                           ORDER BY lead_portfolio_id,observed_at DESC,
                                    activity_snapshot_id DESC
                        )
                        SELECT snapshot.*,activity.orders_1d,activity.orders_3d,
                               activity.orders_7d,activity.active_days_7d,
                               activity.testnet_symbol_compatibility_pct,
                               activity.observed_at
                          FROM snapshot JOIN activity USING(lead_portfolio_id)
                         WHERE activity.observed_at>now()-interval '7 days'
                        """,
                        (lead_portfolio_id, lead_portfolio_id),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise ValueError("COPY_TELEGRAM_LEADER_EVIDENCE_UNAVAILABLE")
                    if (
                        not manual_override
                        and not is_custom_slot(slot)
                        and int(row["testnet_symbol_compatibility_pct"]) < 80
                    ):
                        raise ValueError("COPY_TELEGRAM_LEADER_SYMBOL_COMPATIBILITY_LOW")
                    if _current_lifecycle(
                        cursor, lead_portfolio_id
                    ) is LeaderLifecycle.DRAINING and _leader_has_position(
                        cursor, lead_portfolio_id
                    ):
                        raise ValueError("COPY_TELEGRAM_LEADER_DRAINING_WITH_POSITION")
                    if (
                        not manual_override
                        and slot is not LeaderSlot.LONG_TERM
                        and not is_custom_slot(slot)
                        and not (
                            int(row["orders_1d"]) >= 3
                            and int(row["orders_3d"]) >= 10
                            and int(row["orders_7d"]) >= 14
                            and int(row["active_days_7d"]) >= 3
                        )
                    ):
                        raise ValueError("COPY_TELEGRAM_SHORT_LEADER_ACTIVITY_LOW")
                    display = (
                        f"将 {leader_slot_label(slot)} 设置为 "
                        f"{_safe_text(str(row['nickname']), 32)} (ID {lead_portfolio_id})\n"
                        f"近1/3/7天操作 {row['orders_1d']}/{row['orders_3d']}/"
                        f"{row['orders_7d']} 次; "
                        f"胜率 {compact_decimal(row['win_rate_pct'])}%; "
                        f"回撤 {compact_decimal(row['maximum_drawdown_pct'])}%"
                    )
                challenge_id = _digest(
                    {
                        "action": action,
                        "created_at": now.isoformat(),
                        "lead_portfolio_id": lead_portfolio_id,
                        "nonce_hash": nonce_hash,
                        "slot": slot.value,
                        "user_id": user_id,
                    }
                )
                cursor.execute(
                    """
                    INSERT INTO copytrading.telegram_leader_challenges(
                      challenge_id,user_id,action,slot,lead_portfolio_id,
                      nonce_hash,expires_at,created_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        challenge_id,
                        user_id,
                        action,
                        slot.value,
                        lead_portfolio_id,
                        nonce_hash,
                        now + timedelta(minutes=2),
                        now,
                    ),
                )
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_LEADER_CHALLENGE_WRITE_FAILED") from error
        return LeaderChangeProposal(
            nonce=nonce,
            confirmation_text=(
                f"⚠️ 确认带单员变更\n{display}\n\n"
                + (
                    "这是人工强制配置, 已跳过自动选人门槛。\n"
                    if manual_override and action == "SET"
                    else ""
                )
                + "新带单员先建立基线; 旧带单员有仓位时只排空不强平。"
            ),
        )

    def execute_leader_change_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None:
        if user_id <= 0 or not 8 <= len(nonce) <= 32 or not nonce.isascii():
            return None
        now = datetime.now(UTC)
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        result_message: str | None = None
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                    ("copy-leader-slots",),
                )
                cursor.execute(
                    """
                    SELECT challenge.challenge_id,challenge.action,challenge.slot,
                           challenge.lead_portfolio_id
                      FROM copytrading.telegram_leader_challenges AS challenge
                      LEFT JOIN copytrading.telegram_leader_consumptions AS consumption
                        USING(challenge_id)
                     WHERE challenge.nonce_hash=%s AND challenge.user_id=%s
                       AND challenge.expires_at>%s AND consumption.challenge_id IS NULL
                    """,
                    (nonce_hash, user_id, now),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                challenge_id = str(row["challenge_id"])
                action = str(row["action"])
                slot = LeaderSlot(str(row["slot"]))
                requested_leader = (
                    str(row["lead_portfolio_id"]) if row["lead_portfolio_id"] is not None else None
                )
                slots = _current_slots(cursor)
                current_leader = slots.get(slot)
                if action == "SET":
                    if (
                        requested_leader is None
                        or current_leader == requested_leader
                        or any(
                            other_slot is not slot and value == requested_leader
                            for other_slot, value in slots.items()
                        )
                    ):
                        return None
                elif current_leader is None:
                    return None
                cursor.execute(
                    """
                    INSERT INTO copytrading.telegram_leader_consumptions(
                      consumption_id,challenge_id,user_id,consumed_at
                    ) VALUES (%s,%s,%s,%s)
                    ON CONFLICT (challenge_id) DO NOTHING RETURNING consumption_id
                    """,
                    (
                        _digest(
                            {
                                "challenge_id": challenge_id,
                                "consumed_at": now.isoformat(),
                            }
                        ),
                        challenge_id,
                        user_id,
                        now,
                    ),
                )
                if cursor.fetchone() is None:
                    return None
                cursor.execute(
                    """
                    INSERT INTO copytrading.leader_slot_events(
                      slot_event_id,slot,action,lead_portfolio_id,actor_id,
                      reason_codes,occurred_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        _digest(
                            {
                                "challenge_id": challenge_id,
                                "leader_id": requested_leader,
                                "slot": slot.value,
                            }
                        ),
                        slot.value,
                        "ASSIGNED" if action == "SET" else "CLEARED",
                        requested_leader,
                        f"telegram:{user_id}",
                        Jsonb([f"TELEGRAM_LEADER_{action}"]),
                        now,
                    ),
                )
                final_slots = dict(slots)
                if action == "SET":
                    if requested_leader is None:
                        return None
                    final_slots[slot] = requested_leader
                    lifecycle = _current_lifecycle(cursor, requested_leader)
                    new_state = (
                        LeaderLifecycle.ACTIVE
                        if lifecycle
                        in {
                            LeaderLifecycle.OBSERVE_ONLY,
                            LeaderLifecycle.ACTIVE,
                            LeaderLifecycle.DRAINING,
                        }
                        else LeaderLifecycle.OBSERVE_ONLY
                    )
                    _append_manual_lifecycle(
                        cursor,
                        requested_leader,
                        new_state,
                        challenge_id,
                        now,
                    )
                    result_message = (
                        f"✅ 已更新 {leader_slot_label(slot)}: "
                        f"{_leader_identity(cursor, requested_leader)}\n"
                        "新带单员将先建立基线, 不回放历史订单。"
                    )
                else:
                    final_slots.pop(slot, None)
                    result_message = f"✅ 已清空 {leader_slot_label(slot)}。"
                if current_leader is not None and current_leader not in final_slots.values():
                    old_identity = _leader_identity(cursor, current_leader)
                    old_state = (
                        LeaderLifecycle.DRAINING
                        if _leader_has_position(cursor, current_leader)
                        else LeaderLifecycle.RETIRED
                    )
                    _append_manual_lifecycle(
                        cursor,
                        current_leader,
                        old_state,
                        challenge_id,
                        now,
                    )
                    result_message += (
                        f"\n旧带单员 {old_identity} 进入排空。"
                        if old_state is LeaderLifecycle.DRAINING
                        else f"\n旧带单员 {old_identity} 无仓位, 已退休。"
                    )
                payload = {
                    "event": "copy_leader_manual_change",
                    "slot": slot.value,
                    "state": "SUCCEEDED",
                    "summary": result_message,
                }
                cursor.execute(
                    """
                    INSERT INTO control.outbox(
                      message_id,deduplication_key,topic,payload,payload_hash
                    ) VALUES (%s,%s,'copy.telegram',%s,%s)
                    ON CONFLICT (deduplication_key) DO NOTHING
                    """,
                    (
                        _digest({"leader_change": challenge_id}),
                        f"copy-leader-change:{challenge_id}",
                        Jsonb(payload),
                        _digest(payload),
                    ),
                )
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_LEADER_CHANGE_WRITE_FAILED") from error
        return result_message

    def render(self, view: str) -> str:
        position_match = re.fullmatch(
            r"positions(?::([0-9]{10,24}))?(?::([1-9][0-9]?))?",
            view,
        )
        if position_match is not None:
            lead_portfolio_id = position_match.group(1)
            page = int(position_match.group(2) or "1")
            if lead_portfolio_id is None:
                choices = self.position_leader_choices()
                total = sum(choice.open_position_count for choice in choices)
                return bounded_telegram_text(
                    _render_cards(
                        f"📈 当前仓位 · 共 {total} 个",
                        [choice.button_label for choice in choices],
                        empty="📈 当前没有带单员或仓位",
                    )
                )
            try:
                return bounded_telegram_text(
                    self._positions(
                        lead_portfolio_id=lead_portfolio_id,
                        page=page,
                    )
                )
            except psycopg.Error as error:
                raise TelegramStateError("TELEGRAM_DASHBOARD_READ_FAILED") from error
        renderer = {
            "status": self._status,
            "leaders": self._leaders,
            "pending": self._pending_entries,
            "orders": self._orders,
            "pnl": self._pnl,
            "funds": self._funds,
            "health": self._health,
            "codex": self._codex,
            "repair": self._repair,
            "selection": self._selection,
            "control": self._control,
            "help": self._help,
        }.get(view, self._help)
        try:
            return bounded_telegram_text(renderer())
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_DASHBOARD_READ_FAILED") from error

    def pnl_leader_choices(self) -> tuple[LeaderPnlChoice, ...]:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                slots = _current_slots(cursor)
                current_ids = tuple(dict.fromkeys(slots.values()))
                nicknames: dict[str, str] = {}
                if current_ids:
                    cursor.execute(
                        """
                        SELECT DISTINCT ON (lead_portfolio_id)
                               lead_portfolio_id,nickname
                          FROM copytrading.leader_snapshots
                         WHERE lead_portfolio_id=ANY(%s)
                         ORDER BY lead_portfolio_id,observed_at DESC,snapshot_id DESC
                        """,
                        (list(current_ids),),
                    )
                    nicknames.update(
                        {
                            str(row["lead_portfolio_id"]): str(row["nickname"])
                            for row in cursor.fetchall()
                        }
                    )
                cursor.execute(
                    """
                    WITH lifecycle AS (
                      SELECT DISTINCT ON (lead_portfolio_id) lead_portfolio_id,state
                        FROM copytrading.leader_lifecycle_events
                       ORDER BY lead_portfolio_id,occurred_at DESC,event_id DESC
                    ), positions AS (
                      SELECT lead_portfolio_id,sum(resulting_local_quantity) AS quantity
                        FROM (
                          SELECT DISTINCT ON (lead_portfolio_id,symbol,position_side)
                                 lead_portfolio_id,resulting_local_quantity
                            FROM copytrading.virtual_position_events
                           ORDER BY lead_portfolio_id,symbol,position_side,
                                    occurred_at DESC,position_event_id DESC
                        ) AS latest GROUP BY lead_portfolio_id
                    ), snapshot AS (
                      SELECT DISTINCT ON (lead_portfolio_id)
                             lead_portfolio_id,nickname
                        FROM copytrading.leader_snapshots
                       ORDER BY lead_portfolio_id,observed_at DESC,snapshot_id DESC
                    )
                    SELECT lifecycle.lead_portfolio_id,snapshot.nickname
                      FROM lifecycle JOIN positions USING(lead_portfolio_id)
                      LEFT JOIN snapshot USING(lead_portfolio_id)
                     WHERE lifecycle.state='DRAINING' AND positions.quantity>0
                     ORDER BY lifecycle.lead_portfolio_id LIMIT 5
                    """,
                    (),
                )
                draining = list(cursor.fetchall())
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_PNL_LEADER_CHOICES_READ_FAILED") from error
        choices: list[LeaderPnlChoice] = []
        included: set[str] = set()
        for slot in LeaderSlot:
            leader_id = slots.get(slot)
            if leader_id is None or leader_id in included:
                continue
            included.add(leader_id)
            nickname = _safe_text(nicknames.get(leader_id, leader_id), 22)
            choices.append(
                LeaderPnlChoice(
                    lead_portfolio_id=leader_id,
                    button_label=f"{leader_slot_label(slot)} · {nickname}",
                )
            )
        for row in draining:
            leader_id = str(row["lead_portfolio_id"])
            if leader_id in included:
                continue
            included.add(leader_id)
            nickname = _safe_text(str(row["nickname"] or leader_id), 22)
            choices.append(
                LeaderPnlChoice(
                    lead_portfolio_id=leader_id,
                    button_label=f"⏳ 排空 · {nickname}",
                )
            )
        return tuple(choices)

    def render_leader_pnl(self, lead_portfolio_id: str) -> str:
        if not re.fullmatch(r"[0-9]{10,24}", lead_portfolio_id):
            raise ValueError("Telegram PnL leader ID is invalid")
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH snapshot AS (
                      SELECT nickname FROM copytrading.leader_snapshots
                       WHERE lead_portfolio_id=%s
                       ORDER BY observed_at DESC,snapshot_id DESC LIMIT 1
                    ), lifecycle AS (
                      SELECT state FROM copytrading.leader_lifecycle_events
                       WHERE lead_portfolio_id=%s
                       ORDER BY occurred_at DESC,event_id DESC LIMIT 1
                    ), slots AS (
                      SELECT DISTINCT ON (slot) slot,action,lead_portfolio_id
                        FROM copytrading.leader_slot_events
                       ORDER BY slot,occurred_at DESC,slot_event_id DESC
                    )
                    SELECT (SELECT nickname FROM snapshot) AS nickname,
                           (SELECT state FROM lifecycle) AS lifecycle,
                           (SELECT slot FROM slots
                             WHERE action='ASSIGNED' AND lead_portfolio_id=%s
                             ORDER BY CASE slot WHEN 'LONG_TERM' THEN 0
                                                WHEN 'SHORT_TERM_1' THEN 1
                                                WHEN 'SHORT_TERM_2' THEN 2
                                                WHEN 'CUSTOM_1' THEN 3
                                                WHEN 'CUSTOM_2' THEN 4
                                                WHEN 'CUSTOM_3' THEN 5
                                                WHEN 'CUSTOM_4' THEN 6
                                                WHEN 'CUSTOM_5' THEN 7
                                                WHEN 'CUSTOM_6' THEN 8
                                                WHEN 'CUSTOM_7' THEN 9
                                                ELSE 10 END
                             LIMIT 1) AS slot
                    """,
                    (lead_portfolio_id, lead_portfolio_id, lead_portfolio_id),
                )
                metadata = cursor.fetchone() or {}
                cursor.execute(
                    """
                    WITH latest AS (
                      SELECT * FROM copytrading.leader_valuation_events
                       WHERE lead_portfolio_id=%s
                       ORDER BY observed_at DESC,leader_valuation_event_id DESC LIMIT 1
                    ), boundaries AS (
                      SELECT date_trunc('day',timezone('Asia/Shanghai',now()))
                               AT TIME ZONE 'Asia/Shanghai' AS day_started_at,
                             date_trunc('month',timezone('Asia/Shanghai',now()))
                               AT TIME ZONE 'Asia/Shanghai' AS month_started_at
                    ), reset AS (
                      SELECT reset_event_id,valuation_event_id,occurred_at
                        FROM copytrading.pnl_reset_events
                       ORDER BY occurred_at DESC,reset_event_id DESC LIMIT 1
                    ), reset_anchor AS (
                      SELECT valuation.*
                        FROM copytrading.leader_valuation_events AS valuation
                        JOIN reset USING(valuation_event_id)
                       WHERE valuation.lead_portfolio_id=%s
                    )
                    SELECT latest.*,
                           coalesce(reset_anchor.realized_pnl_usdt,0)
                             AS reset_realized_pnl_usdt,
                           coalesce(reset_anchor.unrealized_pnl_usdt,0)
                             AS reset_unrealized_pnl_usdt,
                           coalesce(reset_anchor.total_pnl_usdt,0)
                             AS reset_total_pnl_usdt,
                           (SELECT occurred_at FROM reset) AS reset_occurred_at,
                           CASE WHEN (SELECT occurred_at FROM reset) IS NULL THEN coalesce(
                             (SELECT total_pnl_usdt
                                FROM copytrading.leader_valuation_events,boundaries
                               WHERE lead_portfolio_id=%s
                                 AND observed_at<boundaries.day_started_at
                               ORDER BY observed_at DESC,
                                        leader_valuation_event_id DESC LIMIT 1),
                             0)
                           WHEN (SELECT occurred_at FROM reset)>=(
                             SELECT day_started_at FROM boundaries
                           )
                             THEN coalesce(reset_anchor.total_pnl_usdt,0)
                           ELSE coalesce(
                             (SELECT total_pnl_usdt
                                FROM copytrading.leader_valuation_events,boundaries,reset
                               WHERE lead_portfolio_id=%s
                                 AND observed_at<boundaries.day_started_at
                                 AND observed_at>=reset.occurred_at
                               ORDER BY observed_at DESC,
                                        leader_valuation_event_id DESC LIMIT 1),
                             reset_anchor.total_pnl_usdt,0)
                           END AS day_anchor_pnl_usdt,
                           CASE WHEN (SELECT occurred_at FROM reset) IS NULL THEN coalesce(
                             (SELECT total_pnl_usdt
                                FROM copytrading.leader_valuation_events,boundaries
                               WHERE lead_portfolio_id=%s
                                 AND observed_at<boundaries.month_started_at
                               ORDER BY observed_at DESC,
                                        leader_valuation_event_id DESC LIMIT 1),
                             0)
                           WHEN (SELECT occurred_at FROM reset)>=(
                             SELECT month_started_at FROM boundaries
                           )
                             THEN coalesce(reset_anchor.total_pnl_usdt,0)
                           ELSE coalesce(
                             (SELECT total_pnl_usdt
                                FROM copytrading.leader_valuation_events,boundaries,reset
                               WHERE lead_portfolio_id=%s
                                 AND observed_at<boundaries.month_started_at
                                 AND observed_at>=reset.occurred_at
                               ORDER BY observed_at DESC,
                                        leader_valuation_event_id DESC LIMIT 1),
                             reset_anchor.total_pnl_usdt,0)
                           END AS month_anchor_pnl_usdt
                      FROM latest
                      LEFT JOIN reset_anchor ON true
                    """,
                    (
                        lead_portfolio_id,
                        lead_portfolio_id,
                        lead_portfolio_id,
                        lead_portfolio_id,
                        lead_portfolio_id,
                        lead_portfolio_id,
                    ),
                )
                row = cursor.fetchone()
        except psycopg.Error as error:
            raise TelegramStateError("TELEGRAM_LEADER_PNL_READ_FAILED") from error
        nickname = _safe_text(str(metadata.get("nickname") or lead_portfolio_id), 30)
        slot_raw = metadata.get("slot")
        label = (
            leader_slot_label(LeaderSlot(str(slot_raw))) if slot_raw is not None else "⏳ 排空/历史"
        )
        if row is None:
            return (
                f"💹 带单员盈亏 · {label}\n"
                f"{nickname}\nID {lead_portfolio_id}\n\n"
                "尚无本系统实际成交盈亏记录。\n"
                "收到成交后, 下一次 10 秒估值会自动生成明细。"
            )
        raw_total = Decimal(str(row["total_pnl_usdt"]))
        total = raw_total - Decimal(str(row["reset_total_pnl_usdt"]))
        today = raw_total - Decimal(str(row["day_anchor_pnl_usdt"]))
        month = raw_total - Decimal(str(row["month_anchor_pnl_usdt"]))
        realized = Decimal(str(row["realized_pnl_usdt"])) - Decimal(
            str(row["reset_realized_pnl_usdt"])
        )
        unrealized = Decimal(str(row["unrealized_pnl_usdt"])) - Decimal(
            str(row["reset_unrealized_pnl_usdt"])
        )
        observed_at = row["observed_at"]
        observed_text = (
            observed_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M:%S")
            if isinstance(observed_at, datetime)
            else str(observed_at)
        )
        mark_warning = (
            "" if row["mark_complete"] else "\n⚠️ 部分持仓缺少标记价, 未实现盈亏暂不完整。"
        )
        reset_at = row.get("reset_occurred_at")
        reset_text = (
            f"\n统计起点: {_display_shanghai_time(reset_at)} (此前盈亏已归零)"
            if reset_at is not None
            else ""
        )
        return (
            f"💹 带单员盈亏 · {label}\n"
            f"{nickname}\nID: {lead_portfolio_id}\n"
            f"{_CARD_DIVIDER}\n"
            "【周期毛盈亏】\n"
            f"今日毛盈亏: {signed_money(today)} U\n"
            f"本月毛盈亏: {signed_money(month)} U\n"
            f"累计毛盈亏: {signed_money(total)} U\n"
            f"{_CARD_DIVIDER}\n"
            "【盈亏构成】\n"
            f"已实现: {signed_money(realized)} U\n"
            f"未实现: {signed_money(unrealized)} U\n"
            f"{_CARD_DIVIDER}\n"
            f"更新时间: {observed_text}\n"
            "口径: 本系统实际成交归属; 未单独分摊手续费和资金费。"
            f"{reset_text}"
            f"{mark_warning}"
        )

    def _status(self) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT state,occurred_at FROM copytrading.runtime_control_events
                 ORDER BY occurred_at DESC,control_event_id DESC LIMIT 1
                """,
                (),
            )
            control = cursor.fetchone()
            cursor.execute(
                """
                WITH latest AS (
                  SELECT DISTINCT ON (lead_portfolio_id) lead_portfolio_id,state
                    FROM copytrading.leader_lifecycle_events
                   ORDER BY lead_portfolio_id,occurred_at DESC,event_id DESC
                )
                SELECT count(*) FILTER (WHERE state='ACTIVE') AS active,
                       count(*) FILTER (WHERE state='DRAINING') AS draining
                  FROM latest
                """,
                (),
            )
            leaders = cursor.fetchone() or {"active": 0, "draining": 0}
            cursor.execute(
                """
                SELECT count(*) FILTER (WHERE state='SUCCEEDED') AS ok,
                       count(*) FILTER (WHERE state<>'SUCCEEDED') AS failed
                  FROM copytrading.poll_events
                 WHERE occurred_at > now() - interval '10 minutes'
                """,
                (),
            )
            polls = cursor.fetchone() or {"ok": 0, "failed": 0}
        state = str(control["state"]) if control else "PAUSED_NEW_ENTRIES"
        if self._execution_environment == "PRODUCTION":
            environment_protection = (
                "环境: Binance USD-M Futures 正式盘\n"
                "保护: 独立数据库 / 独立凭据 / PRODUCTION 激活门禁"
            )
        else:
            environment_protection = "环境: Binance USD-M Futures Testnet\n正式盘: LOCKED"
        return (
            "🤖 跟单系统状态\n"
            "【运行概况】\n"
            f"运行控制: {state}\n"
            f"带单员: {leaders['active']} 活跃 / {leaders['draining']} 排空\n"
            f"近10分钟轮询: {polls['ok']} 成功 / {polls['failed']} 失败\n"
            f"{_CARD_DIVIDER}\n"
            "【环境保护】\n"
            f"{environment_protection}"
        )

    def _leaders(self) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH lifecycle AS (
                  SELECT DISTINCT ON (lead_portfolio_id) lead_portfolio_id,state
                    FROM copytrading.leader_lifecycle_events
                   ORDER BY lead_portfolio_id,occurred_at DESC,event_id DESC
                ), snapshot AS (
                  SELECT DISTINCT ON (lead_portfolio_id) lead_portfolio_id,nickname,
                         win_rate_pct,maximum_drawdown_pct,roi_pct
                    FROM copytrading.leader_snapshots
                   ORDER BY lead_portfolio_id,observed_at DESC,snapshot_id DESC
                ), slots AS (
                  SELECT DISTINCT ON (slot) slot,action,lead_portfolio_id
                    FROM copytrading.leader_slot_events
                   ORDER BY slot,occurred_at DESC,slot_event_id DESC
                )
                SELECT lifecycle.lead_portfolio_id,lifecycle.state,snapshot.nickname,
                       snapshot.win_rate_pct,snapshot.maximum_drawdown_pct,snapshot.roi_pct,
                       slots.slot
                  FROM lifecycle JOIN snapshot USING (lead_portfolio_id)
                  LEFT JOIN slots ON slots.lead_portfolio_id=lifecycle.lead_portfolio_id
                                 AND slots.action='ASSIGNED'
                 WHERE lifecycle.state IN ('OBSERVE_ONLY','ACTIVE','DRAINING')
                 ORDER BY CASE slots.slot
                            WHEN 'LONG_TERM' THEN 0
                            WHEN 'SHORT_TERM_1' THEN 1
                            WHEN 'SHORT_TERM_2' THEN 2
                            WHEN 'CUSTOM_1' THEN 3
                            WHEN 'CUSTOM_2' THEN 4
                            WHEN 'CUSTOM_3' THEN 5
                            WHEN 'CUSTOM_4' THEN 6
                            WHEN 'CUSTOM_5' THEN 7
                            WHEN 'CUSTOM_6' THEN 8
                            WHEN 'CUSTOM_7' THEN 9
                            ELSE 10
                          END,
                          lifecycle.state,lifecycle.lead_portfolio_id
                """,
                (),
            )
            rows = list(cursor.fetchall())
        lines: list[str] = []
        for row in rows:
            nickname = _safe_text(str(row["nickname"]), 22)
            if row["slot"] is None:
                slot_text = "⏳ 排空"
            else:
                slot_value = LeaderSlot(str(row["slot"]))
                slot_text = (
                    f"{tuple(LeaderSlot).index(slot_value) + 1}. {leader_slot_label(slot_value)}"
                )
            lifecycle = {
                "OBSERVE_ONLY": "建基线",
                "ACTIVE": "跟单中",
                "DRAINING": "排空中",
            }.get(str(row["state"]), str(row["state"]))
            lines.append(
                f"{slot_text} | {nickname} | {lifecycle}\n"
                f"ID {row['lead_portfolio_id']} | "
                f"胜 {compact_decimal(row['win_rate_pct'], maximum_places=2)}% | "
                f"回撤 {compact_decimal(row['maximum_drawdown_pct'], maximum_places=2)}% | "
                f"ROI {compact_decimal(row['roi_pct'], maximum_places=2)}%"
            )
        if not lines:
            return "👥 当前没有带单员"
        return (
            "\n".join(["👥 当前带单员", *lines])
            + f"\n{_CARD_DIVIDER}\n点击“管理带单员”按 1-10 号槽位查看和修改。"
        )

    def _positions(
        self,
        *,
        lead_portfolio_id: str | None = None,
        page: int = 1,
    ) -> str:
        if not 1 <= page <= 99 or (
            lead_portfolio_id is not None and not re.fullmatch(r"[0-9]{10,24}", lead_portfolio_id)
        ):
            raise ValueError("Telegram position page is invalid")
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH RECURSIVE ordered_source AS (
                  SELECT event.position_event_id,event.lead_portfolio_id,event.symbol,
                         event.position_side,event.source_quantity_delta,
                         event.resulting_source_quantity,signal.reference_price,
                         row_number() OVER (
                           PARTITION BY event.lead_portfolio_id,event.symbol,event.position_side
                           ORDER BY event.occurred_at,event.position_event_id
                         ) AS sequence_number
                    FROM copytrading.virtual_position_events AS event
                    JOIN copytrading.signals AS signal USING(signal_id)
                ), source_basis AS (
                  SELECT position_event_id,lead_portfolio_id,symbol,position_side,
                         sequence_number,resulting_source_quantity,
                         CASE WHEN source_quantity_delta>0
                              THEN reference_price ELSE 0 END AS average_entry_price
                    FROM ordered_source WHERE sequence_number=1
                  UNION ALL
                  SELECT event.position_event_id,event.lead_portfolio_id,event.symbol,
                         event.position_side,event.sequence_number,
                         event.resulting_source_quantity,
                         CASE
                           WHEN event.resulting_source_quantity=0 THEN 0
                           WHEN event.source_quantity_delta>0 THEN
                             ((basis.resulting_source_quantity*basis.average_entry_price)
                               +(event.source_quantity_delta*event.reference_price))
                             /event.resulting_source_quantity
                           ELSE basis.average_entry_price
                         END AS average_entry_price
                    FROM source_basis AS basis
                    JOIN ordered_source AS event
                      ON event.lead_portfolio_id=basis.lead_portfolio_id
                     AND event.symbol=basis.symbol
                     AND event.position_side=basis.position_side
                     AND event.sequence_number=basis.sequence_number+1
                ), latest AS (
                  SELECT DISTINCT ON (lead_portfolio_id,symbol,position_side)
                         position_event_id,lead_portfolio_id,symbol,position_side,
                         resulting_local_quantity,committed_margin_usdt,leverage
                    FROM copytrading.virtual_position_events
                   ORDER BY lead_portfolio_id,symbol,position_side,
                            occurred_at DESC,position_event_id DESC
                ), snapshot AS (
                  SELECT DISTINCT ON (lead_portfolio_id) lead_portfolio_id,nickname
                    FROM copytrading.leader_snapshots
                   ORDER BY lead_portfolio_id,observed_at DESC,snapshot_id DESC
                ), latest_mark AS (
                  SELECT DISTINCT ON (symbol,position_side)
                         symbol,position_side,mark_price,observed_at AS mark_observed_at
                    FROM copytrading.account_position_mark_events
                   ORDER BY symbol,position_side,observed_at DESC,mark_event_id DESC
                ), reset AS (
                  SELECT reset_event_id,occurred_at
                    FROM copytrading.pnl_reset_events
                   ORDER BY occurred_at DESC,reset_event_id DESC LIMIT 1
                )
                SELECT latest.*,snapshot.nickname,
                       coalesce(correction.corrected_slot,pnl.slot) AS slot,
                       source_basis.average_entry_price AS leader_average_entry_price,
                       pnl.resulting_average_entry_price AS system_average_entry_price,
                       position_cycle.realized_pnl_usdt-
                         CASE WHEN reset.occurred_at IS NOT NULL
                                   AND cycle_start.observed_at<=reset.occurred_at
                              THEN coalesce(
                                position_reset.cycle_realized_pnl_usdt,0
                              ) ELSE 0 END AS position_realized_pnl_usdt,
                       CASE WHEN reset.occurred_at IS NOT NULL
                                  AND cycle_start.observed_at<=reset.occurred_at
                            THEN coalesce(position_reset.unrealized_pnl_usdt,0)
                            ELSE 0 END AS position_unrealized_pnl_reset_anchor_usdt,
                       latest_mark.mark_price,latest_mark.mark_observed_at
                  FROM latest
                LEFT JOIN snapshot USING(lead_portfolio_id)
                LEFT JOIN source_basis USING(position_event_id)
                LEFT JOIN copytrading.leader_pnl_events AS pnl USING(position_event_id)
                LEFT JOIN copytrading.leader_pnl_slot_correction_events AS correction
                  USING(pnl_event_id)
                LEFT JOIN LATERAL (
                  SELECT started.observed_at,started.pnl_event_id
                    FROM copytrading.leader_pnl_events AS started
                   WHERE started.lead_portfolio_id=latest.lead_portfolio_id
                     AND started.symbol=latest.symbol
                     AND started.position_side=latest.position_side
                     AND started.previous_quantity=0
                     AND started.resulting_quantity>0
                     AND (started.observed_at,started.pnl_event_id)
                         <= (pnl.observed_at,pnl.pnl_event_id)
                   ORDER BY started.observed_at DESC,started.pnl_event_id DESC LIMIT 1
                ) AS cycle_start ON true
                LEFT JOIN LATERAL (
                  SELECT coalesce(sum(cycle.realized_pnl_delta_usdt),0)
                           AS realized_pnl_usdt
                    FROM copytrading.leader_pnl_events AS cycle
                   WHERE cycle.lead_portfolio_id=latest.lead_portfolio_id
                     AND cycle.symbol=latest.symbol
                     AND cycle.position_side=latest.position_side
                     AND (cycle.observed_at,cycle.pnl_event_id)
                         >= (cycle_start.observed_at,cycle_start.pnl_event_id)
                     AND (cycle.observed_at,cycle.pnl_event_id)
                         <= (pnl.observed_at,pnl.pnl_event_id)
                ) AS position_cycle ON true
                LEFT JOIN reset ON true
                LEFT JOIN copytrading.pnl_position_reset_anchors AS position_reset
                  ON position_reset.reset_event_id=reset.reset_event_id
                 AND position_reset.lead_portfolio_id=latest.lead_portfolio_id
                 AND position_reset.symbol=latest.symbol
                 AND position_reset.position_side=latest.position_side
                LEFT JOIN latest_mark
                  ON latest_mark.symbol=latest.symbol
                 AND latest_mark.position_side=latest.position_side
                WHERE (%s::text IS NULL OR latest.lead_portfolio_id=%s)
                ORDER BY latest.lead_portfolio_id,latest.symbol,latest.position_side
                """,
                (lead_portfolio_id, lead_portfolio_id),
            )
            rows = [row for row in cursor.fetchall() if row["resulting_local_quantity"] > 0]
            leader_identity = (
                _leader_identity(cursor, lead_portfolio_id)
                if lead_portfolio_id is not None
                else None
            )
        total = len(rows)
        total_pages = max(1, (total + 7) // 8)
        page_rows = rows[(page - 1) * 8 : page * 8]
        cards: list[str] = []
        for row in page_rows:
            leader_line = (
                ""
                if lead_portfolio_id is not None
                else (
                    f"带单: {_safe_text(str(row['nickname'] or '名称未知'), 32)} | "
                    f"ID {row['lead_portfolio_id']}\n"
                )
            )
            cards.append(
                f"🪙 {row['symbol']} · "
                f"{'多' if row['position_side'] == 'LONG' else '空'} · "
                f"{leader_slot_label(LeaderSlot(str(row['slot'])))}\n"
                f"{leader_line}"
                f"数量 {compact_decimal(row['resulting_local_quantity'])} | "
                f"{row['leverage']}x | "
                f"保证金 {compact_decimal(row['committed_margin_usdt'], maximum_places=8)} U\n"
                f"价格: 带单 {_display_price(row['leader_average_entry_price'])} | "
                f"我的 {_display_price(row['system_average_entry_price'])}\n"
                f"{_card_text(_position_pnl_text(row))}"
            )
        if leader_identity is not None:
            title = f"📈 {leader_identity}\n第 {page}/{total_pages} 页 · 共 {total} 个仓位"
            empty = f"📈 {leader_identity}\n当前无仓位"
        else:
            title = f"📈 全部当前仓位 · 第 {page}/{total_pages} 页 · 共 {total} 个"
            empty = "📈 当前无仓位"
        return _render_cards(title, cards, empty=empty)

    def _pending_entries(self) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH latest_decision AS (
                  SELECT DISTINCT ON (signal_id) signal_id,state,occurred_at
                    FROM copytrading.signal_decision_events
                   ORDER BY signal_id,occurred_at DESC,decision_event_id DESC
                ), latest_submission AS (
                  SELECT DISTINCT ON (signal_id)
                         signal_id,state,filled_quantity,exchange_order_id,occurred_at
                    FROM copytrading.submission_events
                   ORDER BY signal_id,occurred_at DESC,submission_event_id DESC
                ), snapshot AS (
                  SELECT DISTINCT ON (lead_portfolio_id) lead_portfolio_id,nickname
                    FROM copytrading.leader_snapshots
                   ORDER BY lead_portfolio_id,observed_at DESC,snapshot_id DESC
                )
                SELECT signal.lead_portfolio_id,signal.symbol,signal.position_side,
                       signal.signal_kind,signal.reference_price AS leader_reference_price,
                       signal.occurred_at AS source_occurred_at,snapshot.nickname,
                       claim.requested_quantity,claim.leverage,claim.limit_price,
                       CASE WHEN upgrade.signal_id IS NULL
                            THEN claim.expires_at ELSE NULL END AS expires_at,
                       claim.claimed_at,decision.state AS decision_state,
                       submission.state AS submission_state,
                       coalesce(submission.filled_quantity,0) AS filled_quantity,
                       submission.exchange_order_id,assignment.slot
                  FROM copytrading.submission_claims AS claim
                  JOIN copytrading.signals AS signal USING(signal_id)
                  JOIN latest_decision AS decision USING(signal_id)
                  LEFT JOIN copytrading.submission_policy_upgrade_events AS upgrade
                    USING(signal_id)
                  LEFT JOIN latest_submission AS submission USING(signal_id)
                  LEFT JOIN snapshot USING(lead_portfolio_id)
                  LEFT JOIN LATERAL (
                    SELECT slot FROM copytrading.leader_slot_events
                     WHERE action='ASSIGNED'
                       AND lead_portfolio_id=signal.lead_portfolio_id
                     ORDER BY occurred_at DESC,slot_event_id DESC LIMIT 1
                  ) AS assignment ON true
                 WHERE claim.order_type='LIMIT'
                   AND signal.signal_kind='INCREASE'
                   AND decision.state IN ('APPROVED','SUBMITTED','UNCERTAIN')
                 ORDER BY claim.claimed_at DESC,signal.signal_id DESC LIMIT 20
                """,
                (),
            )
            rows = list(cursor.fetchall())
        now = datetime.now(UTC)
        cards: list[str] = []
        for row in rows:
            requested = Decimal(str(row["requested_quantity"]))
            filled = min(requested, Decimal(str(row["filled_quantity"])))
            remaining = max(Decimal("0"), requested - filled)
            expected_margin = (
                requested * Decimal(str(row["limit_price"])) / Decimal(str(row["leverage"]))
            )
            slot_text = (
                leader_slot_label(LeaderSlot(str(row["slot"])))
                if row["slot"] is not None
                else "⏳ 排空/历史"
            )
            cards.append(
                f"⏳ {row['symbol']} · {row['position_side']}\n"
                f"状态: {_pending_entry_state(row)}\n"
                f"待成交: {compact_decimal(remaining)} / 委托: "
                f"{compact_decimal(requested)}\n"
                f"已成交待确认: {compact_decimal(filled)}\n"
                f"预计保证金: {compact_decimal(expected_margin, maximum_places=8)} U | "
                f"杠杆: {row['leverage']}x\n"
                "【带单员 / 归属】\n"
                f"{_safe_text(str(row['nickname'] or '名称未知'), 32)}\n"
                f"ID: {row['lead_portfolio_id']}\n"
                f"归属线路: {slot_text}\n"
                "【价格】\n"
                f"带单员价位: {_display_price(row['leader_reference_price'])}\n"
                f"我的委托限价: {_display_price(row['limit_price'])}\n"
                "【时间】\n"
                f"提交: {_display_shanghai_time(row['claimed_at'])}\n"
                f"{_pending_expiry_text(row['expires_at'], now=now)}"
            )
        return _render_cards(
            "⏳ 待入场仓位",
            cards,
            empty="⏳ 当前没有待入场仓位",
        )

    def _orders(self) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH latest_decision AS (
                  SELECT DISTINCT ON (signal_id) signal_id,state,local_quantity,occurred_at
                    FROM copytrading.signal_decision_events
                   ORDER BY signal_id,occurred_at DESC,decision_event_id DESC
                ), snapshot AS (
                  SELECT DISTINCT ON (lead_portfolio_id) lead_portfolio_id,nickname
                    FROM copytrading.leader_snapshots
                   ORDER BY lead_portfolio_id,observed_at DESC,snapshot_id DESC
                )
                SELECT signal.symbol,signal.position_side,signal.signal_kind,
                       signal.signal_origin,
                       signal.lead_portfolio_id,signal.reference_price AS leader_reference_price,
                       signal.occurred_at AS source_occurred_at,snapshot.nickname,
                       decision.state,decision.local_quantity,decision.occurred_at,
                       claim.order_type,claim.limit_price,
                       CASE WHEN upgrade.signal_id IS NULL
                            THEN claim.expires_at ELSE NULL END AS expires_at,
                       claim.requested_quantity,claim.leverage,
                       pnl.fill_price AS system_fill_price,
                       pnl.resulting_average_entry_price AS system_average_entry_price,
                       pnl.realized_pnl_delta_usdt AS system_realized_pnl_delta_usdt,
                       source.total_pnl-coalesce(prior.total_pnl,0)
                         AS leader_realized_pnl_delta
                  FROM latest_decision AS decision
                  JOIN copytrading.signals AS signal USING (signal_id)
                  LEFT JOIN snapshot USING(lead_portfolio_id)
                  LEFT JOIN copytrading.submission_claims AS claim USING(signal_id)
                  LEFT JOIN copytrading.submission_policy_upgrade_events AS upgrade
                    USING(signal_id)
                  LEFT JOIN copytrading.leader_pnl_events AS pnl USING(signal_id)
                  LEFT JOIN copytrading.source_fill_delta_events AS delta
                    ON delta.delta_event_id=signal.delta_event_id
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
                 ORDER BY decision.occurred_at DESC LIMIT 10
                """,
                (),
            )
            rows = list(cursor.fetchall())
        cards: list[str] = []
        for row in rows:
            policy = _order_policy_text(row)
            capital = _entry_capital_text(row)
            capital_suffix = f"\n{capital}" if capital else ""
            cards.append(
                f"🪙 {row['symbol']} · {row['position_side']} · {row['signal_kind']}\n"
                f"状态: {row['state']} | 数量: {compact_decimal(row['local_quantity'])}\n"
                "【带单员】\n"
                f"{_safe_text(str(row['nickname'] or '名称未知'), 32)}\n"
                f"ID: {row['lead_portfolio_id']}\n"
                "【执行方式】\n"
                f"{_stack_details(policy)}"
                f"{capital_suffix}\n"
                "【价格】\n"
                f"{_stack_details(_order_price_text(row))}\n"
                "【时间】\n"
                f"{_stack_details(_order_time_text(row))}"
                f"{_card_text(_order_pnl_suffix(row))}"
            )
        return _render_cards("🧾 最近信号 / 订单", cards, empty="🧾 暂无新信号")

    def _funds(self) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT operating_envelope_usdt,exchange_margin_balance_usdt
                  FROM copytrading.account_envelope_events
                 ORDER BY occurred_at DESC,envelope_event_id DESC LIMIT 1
                """,
                (),
            )
            envelope = cursor.fetchone()
            cursor.execute(
                """
                WITH latest AS (
                  SELECT DISTINCT ON (lead_portfolio_id,symbol,position_side)
                         resulting_local_quantity,committed_margin_usdt
                    FROM copytrading.virtual_position_events
                   ORDER BY lead_portfolio_id,symbol,position_side,
                            occurred_at DESC,position_event_id DESC
                ) SELECT coalesce(sum(committed_margin_usdt) FILTER (
                           WHERE resulting_local_quantity>0
                         ),0) AS committed FROM latest
                """,
                (),
            )
            usage = cursor.fetchone()
            cursor.execute(
                """
                WITH latest_decision AS (
                  SELECT DISTINCT ON (signal_id) signal_id,state
                    FROM copytrading.signal_decision_events
                   ORDER BY signal_id,occurred_at DESC,decision_event_id DESC
                )
                SELECT coalesce(sum(
                         claim.requested_quantity*claim.limit_price/claim.leverage
                       ) FILTER (WHERE decision.state IN ('SUBMITTED','UNCERTAIN')),0)
                         AS pending
                  FROM copytrading.submission_claims AS claim
                  JOIN latest_decision AS decision USING(signal_id)
                 WHERE claim.order_type='LIMIT'
                """,
                (),
            )
            pending = cursor.fetchone()
            cursor.execute(
                """
                SELECT logical_equity_usdt,exchange_margin_balance_usdt,
                       exchange_available_balance_usdt,total_initial_margin_usdt
                  FROM copytrading.account_valuation_events
                 ORDER BY observed_at DESC,valuation_event_id DESC LIMIT 1
                """,
                (),
            )
            valuation = cursor.fetchone()
            configured_entry_limit = _current_entry_margin_limit(cursor)
        operating_envelope = (
            Decimal(str(envelope["operating_envelope_usdt"])) if envelope else Decimal("150")
        )
        logical_equity = operating_envelope
        if valuation:
            logical_equity = (
                max(
                    Decimal("0"),
                    operating_envelope
                    + Decimal(str(valuation["exchange_margin_balance_usdt"]))
                    - Decimal(str(envelope["exchange_margin_balance_usdt"])),
                )
                if envelope
                else Decimal(str(valuation["logical_equity_usdt"]))
            )
        account_available = (
            logical_available_balance(
                exchange_available_balance_usdt=Decimal(
                    str(valuation["exchange_available_balance_usdt"])
                ),
                logical_equity_usdt=logical_equity,
                total_initial_margin_usdt=Decimal(str(valuation["total_initial_margin_usdt"])),
            )
            if valuation
            else operating_envelope
        )
        committed = Decimal(str(usage["committed"])) if usage else Decimal("0")
        pending_margin = Decimal(str(pending["pending"])) if pending else Decimal("0")
        reserve = Decimal("30")
        current_entry_limit = configured_entry_limit
        used_entry_margin = committed + pending_margin
        remaining = available_entry_margin_balance(
            account_unoccupied_usdt=account_available,
            entry_margin_limit_usdt=current_entry_limit,
            committed_margin_usdt=committed,
            pending_margin_usdt=pending_margin,
        )
        return (
            "💰 资金边界\n"
            "【额度规划】\n"
            f"当前交易净值: {compact_money(logical_equity)}U\n"
            f"共享开仓保证金上限: {compact_money(current_entry_limit)}U"
            f" | 保留: {compact_money(reserve)}U\n"
            f"{_CARD_DIVIDER}\n"
            "【当前使用】\n"
            f"已成交仓位占用: {compact_money(committed)}U\n"
            f"待入场订单预留: {compact_money(pending_margin)}U\n"
            f"合计已用开仓额度: {compact_money(used_entry_margin)}U\n"
            f"可用开仓保证金余额: {compact_money(remaining)}U\n"
            f"账户未占用资金(含保留): {compact_money(account_available)}U\n"
            "计算: min(账户未占用, 共享上限-成交占用-待入场预留)\n"
            "账户基线: "
            f"{compact_money(envelope['exchange_margin_balance_usdt']) if envelope else '待建立'}U"
        )

    def _pnl(self) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                WITH latest AS (
                  SELECT * FROM copytrading.account_valuation_events
                   ORDER BY observed_at DESC,valuation_event_id DESC LIMIT 1
                ), boundaries AS (
                  SELECT date_trunc('day',timezone('Asia/Shanghai',now()))
                           AT TIME ZONE 'Asia/Shanghai' AS day_started_at,
                         date_trunc('month',timezone('Asia/Shanghai',now()))
                           AT TIME ZONE 'Asia/Shanghai' AS month_started_at
                ), reset AS (
                  SELECT reset_event_id,valuation_event_id,occurred_at
                    FROM copytrading.pnl_reset_events
                   ORDER BY occurred_at DESC,reset_event_id DESC LIMIT 1
                ), reset_anchor AS (
                  SELECT valuation.*
                    FROM copytrading.account_valuation_events AS valuation
                    JOIN reset USING(valuation_event_id)
                ), envelope_reset AS (
                  SELECT envelope.exchange_margin_balance_usdt
                    FROM copytrading.account_envelope_events AS envelope
                    JOIN reset ON reset.occurred_at=envelope.occurred_at
                   WHERE envelope.event_type='RESET'
                   ORDER BY envelope.envelope_event_id DESC LIMIT 1
                ), account_anchor AS (
                  SELECT CASE WHEN envelope_reset.exchange_margin_balance_usdt IS NOT NULL
                              THEN reset_anchor.exchange_wallet_balance_usdt
                                   - envelope_reset.exchange_margin_balance_usdt
                              ELSE reset_anchor.realized_net_pnl_usdt END
                           AS realized_net_pnl_usdt,
                         reset_anchor.unrealized_pnl_usdt,
                         CASE WHEN envelope_reset.exchange_margin_balance_usdt IS NOT NULL
                              THEN 0 ELSE reset_anchor.total_pnl_usdt END
                           AS total_pnl_usdt
                    FROM reset_anchor
                    LEFT JOIN envelope_reset ON true
                )
                SELECT latest.*,
                       coalesce(account_anchor.realized_net_pnl_usdt,0)
                         AS reset_realized_net_pnl_usdt,
                       coalesce(account_anchor.unrealized_pnl_usdt,0)
                         AS reset_unrealized_pnl_usdt,
                       coalesce(account_anchor.total_pnl_usdt,0)
                         AS reset_total_pnl_usdt,
                       (SELECT occurred_at FROM reset) AS reset_occurred_at,
                       CASE WHEN (SELECT occurred_at FROM reset) IS NULL THEN coalesce(
                         (SELECT total_pnl_usdt
                            FROM copytrading.account_valuation_events,boundaries
                           WHERE observed_at < boundaries.day_started_at
                           ORDER BY observed_at DESC,valuation_event_id DESC LIMIT 1),
                         (SELECT total_pnl_usdt
                            FROM copytrading.account_valuation_events
                           ORDER BY observed_at,valuation_event_id LIMIT 1),
                         latest.total_pnl_usdt
                       ) WHEN (SELECT occurred_at FROM reset)>=(
                         SELECT day_started_at FROM boundaries
                       )
                         THEN coalesce(account_anchor.total_pnl_usdt,0)
                       ELSE coalesce(
                         (SELECT valuation.total_pnl_usdt
                            FROM copytrading.account_valuation_events AS valuation,
                                 boundaries,reset
                           WHERE valuation.observed_at<boundaries.day_started_at
                             AND valuation.observed_at>=reset.occurred_at
                           ORDER BY valuation.observed_at DESC,
                                    valuation.valuation_event_id DESC LIMIT 1),
                         account_anchor.total_pnl_usdt,0)
                       END AS day_anchor_pnl_usdt,
                       CASE WHEN (SELECT occurred_at FROM reset) IS NULL THEN coalesce(
                         (SELECT total_pnl_usdt
                            FROM copytrading.account_valuation_events,boundaries
                           WHERE observed_at < boundaries.month_started_at
                           ORDER BY observed_at DESC,valuation_event_id DESC LIMIT 1),
                         (SELECT total_pnl_usdt
                            FROM copytrading.account_valuation_events
                           ORDER BY observed_at,valuation_event_id LIMIT 1),
                         latest.total_pnl_usdt
                       ) WHEN (SELECT occurred_at FROM reset)>=(
                         SELECT month_started_at FROM boundaries
                       )
                         THEN coalesce(account_anchor.total_pnl_usdt,0)
                       ELSE coalesce(
                         (SELECT valuation.total_pnl_usdt
                            FROM copytrading.account_valuation_events AS valuation,
                                 boundaries,reset
                           WHERE valuation.observed_at<boundaries.month_started_at
                             AND valuation.observed_at>=reset.occurred_at
                           ORDER BY valuation.observed_at DESC,
                                    valuation.valuation_event_id DESC LIMIT 1),
                         account_anchor.total_pnl_usdt,0)
                       END AS month_anchor_pnl_usdt
                  FROM latest
                  LEFT JOIN account_anchor ON true
                """,
                (),
            )
            row = cursor.fetchone()
            cursor.execute(
                """
                WITH boundaries AS (
                  SELECT date_trunc('day',timezone('Asia/Shanghai',now()))
                           AT TIME ZONE 'Asia/Shanghai' AS day_started_at,
                         date_trunc('month',timezone('Asia/Shanghai',now()))
                           AT TIME ZONE 'Asia/Shanghai' AS month_started_at
                ), latest AS (
                  SELECT DISTINCT ON (slot) *
                    FROM copytrading.line_valuation_events
                   ORDER BY slot,observed_at DESC,line_valuation_event_id DESC
                ), current_slots AS (
                  SELECT DISTINCT ON (slot) slot,action,lead_portfolio_id
                    FROM copytrading.leader_slot_events
                   ORDER BY slot,occurred_at DESC,slot_event_id DESC
                ), snapshot AS (
                  SELECT DISTINCT ON (lead_portfolio_id) lead_portfolio_id,nickname
                    FROM copytrading.leader_snapshots
                   ORDER BY lead_portfolio_id,observed_at DESC,snapshot_id DESC
                ), reset AS (
                  SELECT reset_event_id,valuation_event_id,occurred_at
                    FROM copytrading.pnl_reset_events
                   ORDER BY occurred_at DESC,reset_event_id DESC LIMIT 1
                ), reset_anchor AS (
                  SELECT valuation.*
                    FROM copytrading.line_valuation_events AS valuation
                    JOIN reset USING(valuation_event_id)
                )
                SELECT latest.*,current_slots.lead_portfolio_id,snapshot.nickname,
                       coalesce(reset_anchor.realized_pnl_usdt,0)
                         AS reset_realized_pnl_usdt,
                       coalesce(reset_anchor.unrealized_pnl_usdt,0)
                         AS reset_unrealized_pnl_usdt,
                       coalesce(reset_anchor.total_pnl_usdt,0)
                         AS reset_total_pnl_usdt,
                       CASE WHEN (SELECT occurred_at FROM reset) IS NULL THEN coalesce(
                         (SELECT total_pnl_usdt
                            FROM copytrading.line_valuation_events,boundaries
                           WHERE slot=latest.slot
                             AND observed_at<boundaries.day_started_at
                           ORDER BY observed_at DESC,line_valuation_event_id DESC LIMIT 1),
                         0
                       ) WHEN (SELECT occurred_at FROM reset)>=(
                         SELECT day_started_at FROM boundaries
                       )
                         THEN coalesce(reset_anchor.total_pnl_usdt,0)
                       ELSE coalesce(
                         (SELECT total_pnl_usdt
                            FROM copytrading.line_valuation_events,boundaries,reset
                           WHERE slot=latest.slot
                             AND observed_at<boundaries.day_started_at
                             AND observed_at>=reset.occurred_at
                           ORDER BY observed_at DESC,line_valuation_event_id DESC LIMIT 1),
                         reset_anchor.total_pnl_usdt,0)
                       END AS day_anchor_pnl_usdt,
                       CASE WHEN (SELECT occurred_at FROM reset) IS NULL THEN coalesce(
                         (SELECT total_pnl_usdt
                            FROM copytrading.line_valuation_events,boundaries
                           WHERE slot=latest.slot
                             AND observed_at<boundaries.month_started_at
                           ORDER BY observed_at DESC,line_valuation_event_id DESC LIMIT 1),
                         0
                       ) WHEN (SELECT occurred_at FROM reset)>=(
                         SELECT month_started_at FROM boundaries
                       )
                         THEN coalesce(reset_anchor.total_pnl_usdt,0)
                       ELSE coalesce(
                         (SELECT total_pnl_usdt
                            FROM copytrading.line_valuation_events,boundaries,reset
                           WHERE slot=latest.slot
                             AND observed_at<boundaries.month_started_at
                             AND observed_at>=reset.occurred_at
                           ORDER BY observed_at DESC,line_valuation_event_id DESC LIMIT 1),
                         reset_anchor.total_pnl_usdt,0)
                       END AS month_anchor_pnl_usdt
                  FROM latest
                  LEFT JOIN current_slots
                    ON current_slots.slot=latest.slot AND current_slots.action='ASSIGNED'
                  LEFT JOIN snapshot USING(lead_portfolio_id)
                  LEFT JOIN reset_anchor ON reset_anchor.slot=latest.slot
                 ORDER BY CASE latest.slot WHEN 'LONG_TERM' THEN 0
                                      WHEN 'SHORT_TERM_1' THEN 1
                                      WHEN 'SHORT_TERM_2' THEN 2
                                      WHEN 'CUSTOM_1' THEN 3
                                      WHEN 'CUSTOM_2' THEN 4
                                      WHEN 'CUSTOM_3' THEN 5
                                      WHEN 'CUSTOM_4' THEN 6
                                      WHEN 'CUSTOM_5' THEN 7
                                      WHEN 'CUSTOM_6' THEN 8
                                      WHEN 'CUSTOM_7' THEN 9
                                      ELSE 10 END
                """,
                (),
            )
            line_rows = list(cursor.fetchall())
            cursor.execute(
                """
                WITH latest_positions AS (
                  SELECT DISTINCT ON (lead_portfolio_id,symbol,position_side)
                         resulting_local_quantity,committed_margin_usdt
                    FROM copytrading.virtual_position_events
                   ORDER BY lead_portfolio_id,symbol,position_side,
                            occurred_at DESC,position_event_id DESC
                ), latest_decision AS (
                  SELECT DISTINCT ON (signal_id) signal_id,state
                    FROM copytrading.signal_decision_events
                   ORDER BY signal_id,occurred_at DESC,decision_event_id DESC
                )
                SELECT coalesce((
                         SELECT sum(committed_margin_usdt)
                           FROM latest_positions
                          WHERE resulting_local_quantity>0
                       ),0) AS committed,
                       coalesce((
                         SELECT sum(
                                  claim.requested_quantity*claim.limit_price/claim.leverage
                                )
                           FROM copytrading.submission_claims AS claim
                           JOIN latest_decision AS decision USING(signal_id)
                          WHERE claim.order_type='LIMIT'
                            AND decision.state IN ('SUBMITTED','UNCERTAIN')
                       ),0) AS pending
                """,
                (),
            )
            margin_usage = cursor.fetchone()
            configured_entry_limit = _current_entry_margin_limit(cursor)
        if row is None:
            return "💹 系统盈亏\n等待下一次 10 秒账户估值。"
        total, today, month, realized, unrealized = _account_pnl_since_reset(row)
        net_account_adjustment = _net_account_adjustment(total, line_rows)
        envelope = Decimal(str(row["operating_envelope_usdt"]))
        displayed_equity = _rebased_logical_equity(envelope, total)
        account_unoccupied = logical_available_balance(
            exchange_available_balance_usdt=Decimal(str(row["exchange_available_balance_usdt"])),
            logical_equity_usdt=displayed_equity,
            total_initial_margin_usdt=Decimal(str(row["total_initial_margin_usdt"])),
        )
        committed_margin = Decimal(str(margin_usage["committed"])) if margin_usage else Decimal("0")
        pending_margin = Decimal(str(margin_usage["pending"])) if margin_usage else Decimal("0")
        used_entry_margin = committed_margin + pending_margin
        available_entry_margin = available_entry_margin_balance(
            account_unoccupied_usdt=account_unoccupied,
            entry_margin_limit_usdt=configured_entry_limit,
            committed_margin_usdt=committed_margin,
            pending_margin_usdt=pending_margin,
        )
        roi = (total / envelope) * Decimal("100")
        observed_at = row["observed_at"]
        observed_text = (
            observed_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M:%S")
            if isinstance(observed_at, datetime)
            else str(observed_at)
        )
        account_summary = (
            "【账户汇总】\n"
            f"当前净值: {compact_money(displayed_equity)} U\n"
            f"今日净盈亏: {signed_money(today)} U\n"
            f"本月净盈亏: {signed_money(month)} U\n"
            f"累计净盈亏: {signed_money(total)} U ({signed_percent(roi)})\n"
            f"已实现净额: {signed_money(realized)} U\n"
            f"未实现盈亏: {signed_money(unrealized)} U\n"
            f"手续费/资金费等净调整: {signed_money(net_account_adjustment)} U\n"
            f"可用开仓保证金余额: {compact_money(available_entry_margin)} U\n"
            f"系统已用开仓额度: {compact_money(used_entry_margin)} / "
            f"{compact_money(configured_entry_limit)} U\n"
            f"交易所实际占用保证金: "
            f"{compact_money(row['total_initial_margin_usdt'])} U\n"
            f"账户未占用资金(含保留): {compact_money(account_unoccupied)} U"
        )
        line_cards: list[str] = []
        line_by_slot = {str(item["slot"]): item for item in line_rows}
        for slot in LeaderSlot:
            item = line_by_slot.get(slot.value)
            if item is None or not item["lead_portfolio_id"]:
                continue
            leader_id = str(item["lead_portfolio_id"])
            nickname = _safe_text(str(item["nickname"] or leader_id), 32)
            line_raw_total = Decimal(str(item["total_pnl_usdt"]))
            line_total = line_raw_total - Decimal(str(item["reset_total_pnl_usdt"]))
            line_today = line_raw_total - Decimal(str(item["day_anchor_pnl_usdt"]))
            line_month = line_raw_total - Decimal(str(item["month_anchor_pnl_usdt"]))
            identity = f"当前: {nickname}\nID: {leader_id}"
            card = (
                f"{leader_slot_label(slot)} · {identity}\n"
                f"本线今日: {signed_money(line_today)} U\n"
                f"本线本月: {signed_money(line_month)} U\n"
                f"本线累计: {signed_money(line_total)} U"
            )
            if not item["mark_complete"]:
                card += "\n⚠️ 部分未实现盈亏缺少标记价"
            line_cards.append(card)
        line_summary = _render_cards(
            "【分线归属毛盈亏】",
            line_cards,
        )
        reset_at = row.get("reset_occurred_at")
        reset_text = (
            f"\n统计起点: {_display_shanghai_time(reset_at)} (此前盈亏已归零)"
            if reset_at is not None
            else ""
        )
        return (
            f"💹 系统盈亏 · {self._environment_label}\n{account_summary}\n"
            f"{_CARD_DIVIDER}\n{line_summary}\n"
            f"{_CARD_DIVIDER}\n"
            f"更新时间: {observed_text}\n"
            "口径: 系统净额含手续费/资金费; 本线盈亏含该槽位历史带单员, "
            "不会算作当前带单员个人成绩。\n"
            "下方按钮可继续查看当前及排空带单员明细。"
            f"{reset_text}"
        )

    def _health(self) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT state,findings,occurred_at
                  FROM copytrading.health_check_runs
                 WHERE check_kind='WATCHDOG'
                 ORDER BY occurred_at DESC,health_run_id DESC LIMIT 1
                """,
                (),
            )
            row = cursor.fetchone()
        if row is None:
            return "🩺 尚无系统巡检报告"
        raw_findings = row.get("findings")
        findings = raw_findings if isinstance(raw_findings, list) else []
        cards: list[str] = []
        for finding in findings[:10]:
            if not isinstance(finding, Mapping):
                continue
            code = _safe_text(str(finding.get("code", "UNKNOWN")), 80)
            severity = _safe_text(str(finding.get("severity", "INFO")), 16)
            detail = _safe_text(str(finding.get("detail", "无详情")), 300)
            cards.append(f"[{severity}] {_operator_reason_label(code)}\n证据: {detail}")
        if not cards:
            cards.append("✅ 本轮未发现异常")
        return _render_cards(
            (
                "🩺 最近系统巡检\n"
                f"状态: {row['state']}\n"
                f"时间: {_display_shanghai_time(row['occurred_at'])}"
            ),
            cards,
        )[:4000]

    def _codex(self) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT state,findings,occurred_at
                  FROM copytrading.health_check_runs
                 WHERE check_kind='CODEX_AUDIT'
                 ORDER BY occurred_at DESC,health_run_id DESC LIMIT 1
                """,
                (),
            )
            row = cursor.fetchone()
        if row is None:
            return "🤖 Codex 尚无系统审查报告\n可点击下方“立即审查”。"
        evidence = row.get("findings")
        document = evidence if isinstance(evidence, Mapping) else {}
        codex = document.get("codex")
        failure = document.get("codex_failure")
        if isinstance(codex, Mapping):
            status = str(codex.get("status", row["state"]))
            summary = str(codex.get("summary", "未提供结论"))[:600]
            lines = [
                "🤖 Codex 最近系统审查",
                f"状态: {status}",
                f"时间: {_display_shanghai_time(row['occurred_at'])}",
                _CARD_DIVIDER,
                "【结论】",
                summary,
            ]
            findings = codex.get("findings")
            if isinstance(findings, list) and findings:
                lines.extend([_CARD_DIVIDER, "【发现】"])
                for finding in findings[:4]:
                    if not isinstance(finding, Mapping):
                        continue
                    severity = str(finding.get("severity", "INFO"))
                    code = str(finding.get("code", "UNKNOWN"))
                    detail = str(finding.get("evidence", "无详情"))[:240]
                    lines.append(f"• [{severity}] {_operator_reason_label(code)}\n  {detail}")
            actions = document.get("applied_actions")
            applied = [str(action) for action in actions] if isinstance(actions, list) else []
            lines.extend(
                [
                    _CARD_DIVIDER,
                    "【自动操作】",
                    ", ".join(applied) if applied else "无",
                ]
            )
            return "\n".join(lines)[:4000]
        if isinstance(failure, Mapping):
            failure_code = str(failure.get("code", "COPY_CODEX_AUDIT_FAILED"))
            return (
                "🤖 Codex 系统审查执行失败\n"
                f"时间: {_display_shanghai_time(row['occurred_at'])}\n"
                f"错误: {_operator_reason_label(failure_code)}\n"
                "systemd 会自动重试; 确定性巡检和交易保护仍独立运行。"
            )
        return f"🤖 Codex 系统审查记录格式异常\n时间: {_display_shanghai_time(row['occurred_at'])}"

    def _repair(self) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
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
        if row is None:
            return "🛠 Codex 尚无自动修复记录\n系统报错后会自动唤醒修复流程。"
        evidence = row.get("findings")
        document = evidence if isinstance(evidence, Mapping) else {}
        status = _safe_text(str(document.get("status", row["state"])), 24)
        summary = _safe_text(str(document.get("summary", "未提供结论")), 700)
        root_cause = _safe_text(str(document.get("root_cause", "未提供")), 600)
        lines = [
            "🛠 Codex 最近自动修复",
            f"状态: {status}",
            f"时间: {_display_shanghai_time(row['occurred_at'])}",
            _CARD_DIVIDER,
            "【结论】",
            summary,
            _CARD_DIVIDER,
            "【根因】",
            root_cause,
        ]
        changed = document.get("changed_files")
        changed_files = (
            [_safe_text(str(path), 120) for path in changed[:8]]
            if isinstance(changed, list)
            else []
        )
        lines.extend(
            [
                _CARD_DIVIDER,
                "【修改与验证】",
                f"修改文件: {', '.join(changed_files) if changed_files else '无'}",
            ]
        )
        verification = document.get("verification")
        if isinstance(verification, list) and verification:
            lines.append("验证:")
            lines.extend(f"• {_safe_text(str(item), 260)}" for item in verification[:5])
        follow_up = document.get("follow_up_required")
        if follow_up is True:
            follow_up_text = "⚠️ 仍需继续跟进; 系统会保持保护状态并再次审查。"
        elif follow_up is False:
            follow_up_text = "无需人工跟进。"
        else:
            follow_up_text = "等待下一次审查确认。"
        lines.extend([_CARD_DIVIDER, "【后续】", follow_up_text])
        return "\n".join(lines)[:4000]

    def _selection(self) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT selection_run_id,occurred_at,codex_report_digest
                  FROM copytrading.selection_runs
                 WHERE state='COMPLETED'
                 ORDER BY occurred_at DESC LIMIT 1
                """,
                (),
            )
            row = cursor.fetchone()
        if row is None:
            return "🤖 Codex 尚无选人报告"
        return (
            "🤖 Codex 最近选人依据\n"
            f"时间: {_display_shanghai_time(row['occurred_at'])}\n"
            f"批次: {str(row['selection_run_id'])[:12]}\n"
            f"报告校验: {str(row['codex_report_digest'])[:12]}\n"
            "当前策略: 遍历全部公开带单员目录; 自动候选至少有200名当前跟单者; "
            "通过回撤、盈亏质量、样本和活跃度门槛后以跟单人数为第一排序依据; "
            "三条自动线的市场认可度均占综合分35%; 全部槽位禁止带单员重复。"
        )

    def _control(self) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT state,actor_id,occurred_at
                  FROM copytrading.runtime_control_events
                 ORDER BY occurred_at DESC,control_event_id DESC LIMIT 1
                """,
                (),
            )
            row = cursor.fetchone()
        state = str(row["state"]) if row else "PAUSED_NEW_ENTRIES"
        environment_guard = (
            "当前为正式盘, 所有操作仍需二次确认。"
            if self._execution_environment == "PRODUCTION"
            else "所有操作均需二次确认, 正式盘保持锁定。"
        )
        return (
            "⚙️ 交易控制中心\n"
            f"当前状态: {state}\n\n"
            "暂停: 停止新开仓, 减仓和平仓继续。\n"
            f"恢复: 允许后续新信号在{self._environment_label}下单。\n"
            "全部减仓: 按各带单员虚拟账本逐仓处理。\n\n"
            f"{environment_guard}"
        )

    def _help(self) -> str:
        return (
            "可用命令: /status /leaders /positions /orders /pnl /funds /codex /control\n"
            "控制操作必须通过按钮二次确认。"
        )


def _current_slots(
    cursor: psycopg.Cursor[dict[str, Any]],
) -> dict[LeaderSlot, str]:
    cursor.execute(
        """
        SELECT DISTINCT ON (slot) slot,action,lead_portfolio_id
          FROM copytrading.leader_slot_events
         ORDER BY slot,occurred_at DESC,slot_event_id DESC
        """,
        (),
    )
    return {
        LeaderSlot(str(row["slot"])): str(row["lead_portfolio_id"])
        for row in cursor.fetchall()
        if row["action"] == "ASSIGNED" and row["lead_portfolio_id"] is not None
    }


def _current_leader_locks(
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


def _current_follow_multipliers(
    cursor: psycopg.Cursor[dict[str, Any]],
    lead_portfolio_ids: list[str],
) -> dict[str, int]:
    if not lead_portfolio_ids:
        return {}
    cursor.execute(
        """
        SELECT DISTINCT ON (lead_portfolio_id) lead_portfolio_id,multiplier
          FROM copytrading.leader_follow_multiplier_events
         WHERE lead_portfolio_id=ANY(%s)
         ORDER BY lead_portfolio_id,occurred_at DESC,multiplier_event_id DESC
        """,
        (lead_portfolio_ids,),
    )
    return {str(row["lead_portfolio_id"]): int(row["multiplier"]) for row in cursor.fetchall()}


def _current_lifecycle(
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


def _leader_has_position(
    cursor: psycopg.Cursor[dict[str, Any]],
    lead_portfolio_id: str,
) -> bool:
    cursor.execute(
        """
        SELECT coalesce(sum(resulting_local_quantity),0)>0 AS has_position
          FROM (
            SELECT DISTINCT ON (symbol,position_side) resulting_local_quantity
              FROM copytrading.virtual_position_events
             WHERE lead_portfolio_id=%s
             ORDER BY symbol,position_side,occurred_at DESC,position_event_id DESC
          ) AS latest
        """,
        (lead_portfolio_id,),
    )
    row = cursor.fetchone()
    return bool(row and row["has_position"])


def _append_manual_lifecycle(
    cursor: psycopg.Cursor[dict[str, Any]],
    lead_portfolio_id: str,
    lifecycle: LeaderLifecycle,
    challenge_id: str,
    occurred_at: datetime,
) -> None:
    cursor.execute(
        """
        INSERT INTO copytrading.leader_lifecycle_events(
          event_id,lead_portfolio_id,state,selection_run_id,reason_codes,occurred_at
        ) VALUES (%s,%s,%s,NULL,%s,%s)
        """,
        (
            _digest(
                {
                    "challenge_id": challenge_id,
                    "lead_portfolio_id": lead_portfolio_id,
                    "state": lifecycle.value,
                }
            ),
            lead_portfolio_id,
            lifecycle.value,
            Jsonb(["TELEGRAM_MANUAL_SLOT_CHANGE"]),
            occurred_at,
        ),
    )


def _current_virtual_position(
    cursor: psycopg.Cursor[dict[str, Any]],
    *,
    lead_portfolio_id: str,
    symbol: str,
    position_side: str,
) -> dict[str, Any] | None:
    cursor.execute(
        """
        SELECT resulting_local_quantity AS local_quantity,
               resulting_source_quantity AS source_quantity
          FROM copytrading.virtual_position_events
         WHERE lead_portfolio_id=%s AND symbol=%s AND position_side=%s
         ORDER BY occurred_at DESC,position_event_id DESC LIMIT 1
        """,
        (lead_portfolio_id, symbol, position_side),
    )
    return cursor.fetchone()


def _current_leader_virtual_positions(
    cursor: psycopg.Cursor[dict[str, Any]],
    *,
    lead_portfolio_id: str,
) -> list[dict[str, Any]]:
    cursor.execute(
        """
        WITH latest AS (
          SELECT DISTINCT ON (symbol,position_side)
                 symbol,position_side,resulting_local_quantity
            FROM copytrading.virtual_position_events
           WHERE lead_portfolio_id=%s
           ORDER BY symbol,position_side,occurred_at DESC,position_event_id DESC
        )
        SELECT symbol,position_side,resulting_local_quantity AS local_quantity
          FROM latest
         WHERE resulting_local_quantity>0
         ORDER BY symbol,position_side
         LIMIT 101
        """,
        (lead_portfolio_id,),
    )
    rows = list(cursor.fetchall())
    if len(rows) > 100:
        raise ValueError("Telegram leader has too many positions to clear safely")
    return rows


def _leader_position_close_targets(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 100:
        raise TelegramStateError("TELEGRAM_LEADER_POSITION_CLOSE_TARGETS_INVALID")
    targets: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "local_quantity",
            "position_side",
            "symbol",
        }:
            raise TelegramStateError("TELEGRAM_LEADER_POSITION_CLOSE_TARGETS_INVALID")
        symbol = str(item["symbol"])
        position_side = str(item["position_side"])
        quantity = str(item["local_quantity"])
        try:
            parsed_quantity = Decimal(quantity)
        except InvalidOperation as error:
            raise TelegramStateError("TELEGRAM_LEADER_POSITION_CLOSE_TARGETS_INVALID") from error
        key = (symbol, position_side)
        if (
            not re.fullmatch(r"[A-Z0-9]{3,24}", symbol)
            or position_side not in {"LONG", "SHORT"}
            or not parsed_quantity.is_finite()
            or parsed_quantity <= 0
            or key in seen
        ):
            raise TelegramStateError("TELEGRAM_LEADER_POSITION_CLOSE_TARGETS_INVALID")
        seen.add(key)
        targets.append(
            {
                "local_quantity": quantity,
                "position_side": position_side,
                "symbol": symbol,
            }
        )
    if targets != sorted(targets, key=lambda item: (item["symbol"], item["position_side"])):
        raise TelegramStateError("TELEGRAM_LEADER_POSITION_CLOSE_TARGETS_INVALID")
    return targets


def _position_close_pending(
    cursor: psycopg.Cursor[dict[str, Any]],
    *,
    lead_portfolio_id: str,
    symbol: str,
    position_side: str,
) -> bool:
    cursor.execute(
        """
        WITH latest_decision AS (
          SELECT DISTINCT ON (signal_id) signal_id,state
            FROM copytrading.signal_decision_events
           ORDER BY signal_id,occurred_at DESC,decision_event_id DESC
        )
        SELECT EXISTS(
          SELECT 1 FROM copytrading.signals AS signal
          LEFT JOIN latest_decision AS decision USING(signal_id)
          WHERE signal.signal_origin='CONTROL' AND signal.signal_kind='REDUCE'
            AND signal.lead_portfolio_id=%s AND signal.symbol=%s
            AND signal.position_side=%s
            AND (decision.state IS NULL OR decision.state IN (
              'RECEIVED','APPROVED','SUBMITTED','UNCERTAIN'
            ))
        ) AS pending
        """,
        (lead_portfolio_id, symbol, position_side),
    )
    row = cursor.fetchone()
    return bool(row and row["pending"])


def _safe_text(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _leader_identity(
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
    nickname = _safe_text(str(row["nickname"]), 32) if row is not None else "名称未知"
    return f"{nickname} (ID {lead_portfolio_id})"


def _control_state(action: ControlAction) -> str:
    return {
        ControlAction.PAUSE_NEW_ENTRIES: "PAUSED_NEW_ENTRIES",
        ControlAction.RESUME_TESTNET: "RUNNING",
        ControlAction.REDUCE_ALL: "REDUCE_ALL",
    }[action]


def _control_message(action: ControlAction, *, environment_label: str = "测试盘") -> str:
    return {
        ControlAction.PAUSE_NEW_ENTRIES: "⏸ 已暂停新开仓; 减仓和平仓仍允许。",
        ControlAction.RESUME_TESTNET: f"▶️ 已恢复{environment_label}新开仓。",
        ControlAction.REDUCE_ALL: "🧯 已提交全部减仓请求, 执行器将逐仓处理。",
    }[action]


def _notification_text(
    payload: Mapping[str, Any],
    *,
    environment_label: str = "测试盘",
) -> str:
    return translate_reason_codes_in_text(
        _notification_text_raw(payload, environment_label=environment_label)
    )


def _notification_text_raw(
    payload: Mapping[str, Any],
    *,
    environment_label: str = "测试盘",
) -> str:
    if payload.get("event") == "copy_leader_availability_alert":
        slot_value = _safe_text(str(payload.get("slot", "UNKNOWN")), 16)
        try:
            slot_name = leader_slot_label(LeaderSlot(slot_value))
        except ValueError:
            slot_name = slot_value
        nickname = _safe_text(str(payload.get("nickname", "名称未知")), 32)
        leader_id = _safe_text(str(payload.get("lead_portfolio_id", "UNKNOWN")), 24)
        checked_at = _display_shanghai_time(payload.get("checked_at"))
        return (
            "⚠️ 当前槽位的带单员已不在公开带单目录\n"
            f"槽位: {slot_name}\n"
            f"带单员: {nickname} (ID {leader_id})\n"
            "原因: Binance 完整公开带单目录已找不到该带单员, 其公开带单项目可能已停止\n"
            "系统处理: 仅发送本次提醒; 未清空或替换槽位, 未取消订单, 未处理任何仓位\n"
            f"请在“带单员”页面手动更换 · 检查时间: {checked_at}"
        )
    if payload.get("event") == "copy_slot_selection":
        strategy = _safe_text(str(payload.get("strategy", "UNKNOWN")), 24)
        results = payload.get("results")
        if isinstance(results, list):
            lines = [f"👥 {strategy} 自动选人完成"]
            for item in results[:3]:
                if not isinstance(item, Mapping):
                    continue
                slot_value = _safe_text(str(item.get("slot", "UNKNOWN")), 16)
                slot_label = {
                    "LONG_TERM": "🔒 长线",
                    "SHORT_TERM_1": "⚡ 短线 1 (最高胜率)",
                    "SHORT_TERM_2": "⚡ 短线 2 (日内综合)",
                }.get(slot_value, slot_value)
                status = str(item.get("status", "UNKNOWN"))
                candidate_name = _safe_text(str(item.get("candidate_nickname", "名称未知")), 32)
                candidate_id = _safe_text(
                    str(item.get("candidate_lead_portfolio_id", "UNKNOWN")), 24
                )
                incumbent_name = _safe_text(str(item.get("incumbent_nickname", "名称未知")), 32)
                if status == "UNCHANGED":
                    outcome = "仍是本轮最优, 无需更换"
                elif status == "LOCKED_UNCHANGED":
                    outcome = "用户已锁定, 当前带单员保持不变; 备用人选只记录, 不会自动接管"
                elif status == "ASSIGNED":
                    outcome = "原槽位为空, 已直接设置"
                elif status == "REPLACED":
                    outcome = f"旧带单员 {incumbent_name} 无仓位, 已直接更换"
                elif status == "WAITING_FOR_POSITION_CLOSE":
                    deadline = _display_shanghai_time(item.get("expires_at"))
                    outcome = f"旧带单员 {incumbent_name} 有仓位, 等待平仓至 {deadline}"
                elif status == "BLOCKED_BY_LEADER_LOCK":
                    outcome = f"当前带单员 {incumbent_name} 已锁定, 本轮保留且继续正常跟单"
                else:
                    outcome = status
                card_lines = [_CARD_DIVIDER, slot_label]
                if status == "LOCKED_UNCHANGED":
                    backup_id = item.get("backup_lead_portfolio_id")
                    backup_name = item.get("backup_nickname")
                    card_lines.append(f"当前: {candidate_name} (ID {candidate_id})")
                    if backup_id is not None:
                        card_lines.append(
                            "备用: "
                            f"{_safe_text(str(backup_name or '名称未知'), 32)} "
                            f"(ID {_safe_text(str(backup_id), 24)})"
                        )
                    else:
                        card_lines.append("备用: 本轮没有通过门槛的候选")
                else:
                    card_lines.append(f"候选: {candidate_name} (ID {candidate_id})")
                card_lines.append(f"结果: {outcome}")
                lines.extend(card_lines)
            return "\n".join(lines)[:3900]
        leaders = payload.get("leaders")
        if isinstance(leaders, list):
            descriptions = []
            for item in leaders[:3]:
                if not isinstance(item, dict):
                    continue
                slot = _safe_text(str(item.get("slot", "UNKNOWN")), 16)
                label = {
                    "LONG_TERM": "🔒 长线",
                    "SHORT_TERM_1": "⚡ 短线 1",
                    "SHORT_TERM_2": "⚡ 短线 2",
                }.get(slot, slot)
                nickname = _safe_text(str(item.get("nickname", "名称未知")), 32)
                leader_id = _safe_text(str(item.get("lead_portfolio_id", "UNKNOWN")), 24)
                descriptions.append(f"• {label}: {nickname} (ID {leader_id})")
            leader_text = "\n".join(descriptions)
        else:
            legacy_ids = payload.get("leader_ids")
            leader_text = (
                ", ".join(_safe_text(str(value), 24) for value in legacy_ids[:3])
                if isinstance(legacy_ids, list)
                else ""
            )
        return f"👥 {strategy} 自动选人完成\n席位: {leader_text}"
    if payload.get("event") == "copy_slot_replacement":
        slot_value = _safe_text(str(payload.get("slot", "UNKNOWN")), 16)
        slot_label = {
            "LONG_TERM": "🔒 长线",
            "SHORT_TERM_1": "⚡ 短线 1 (最高胜率)",
            "SHORT_TERM_2": "⚡ 短线 2 (日内综合)",
        }.get(slot_value, slot_value)
        state = str(payload.get("state", "UNKNOWN"))
        incumbent = (
            f"{_safe_text(str(payload.get('incumbent_nickname', '名称未知')), 32)} "
            f"(ID {_safe_text(str(payload.get('incumbent_lead_portfolio_id', 'UNKNOWN')), 24)})"
        )
        candidate = (
            f"{_safe_text(str(payload.get('candidate_nickname', '名称未知')), 32)} "
            f"(ID {_safe_text(str(payload.get('candidate_lead_portfolio_id', 'UNKNOWN')), 24)})"
        )
        if state == "APPLIED":
            result = "旧带单员已在期限内清仓, 替换成功"
        elif state == "EXPIRED":
            result = "等待期限已到但旧带单员仍有仓位, 本轮取消更换"
        elif "COPY_SLOT_REPLACEMENT_CANCELLED_BY_LEADER_LOCK" in payload.get("reason_codes", []):
            result = "旧带单员已锁定, 本轮自动换人已取消; 跟单继续正常运行"
        else:
            result = "槽位或候选状态已变化, 本轮待替换已取消"
        return (
            f"👥 自动换人结果 · {slot_label}\n"
            f"旧带单员: {incumbent}\n候选: {candidate}\n结果: {result}"
        )
    if payload.get("event") == "copy_leader_manual_change":
        return _safe_text(str(payload.get("summary", "带单员变更完成")), 1000)
    if payload.get("event") == "copy_leader_follow_multiplier_change":
        return _safe_text(str(payload.get("summary", "带单员跟单倍数变更完成")), 1000)
    if payload.get("event") == "copy_entry_margin_limit_change":
        return _safe_text(str(payload.get("summary", "共享可用保证金额度变更完成")), 1000)
    if payload.get("event") == "copy_leader_lock_change":
        summary = str(payload.get("summary", "带单员锁定状态变更完成"))
        return "\n".join(_safe_text(line, 500) for line in summary.splitlines())[:1000]
    if payload.get("event") == "copy_pnl_reset":
        operating_envelope = compact_money(payload.get("operating_envelope_usdt", "150"))
        margin_line = ""
        if (
            payload.get("logical_available_usdt") is not None
            and payload.get("total_initial_margin_usdt") is not None
        ):
            logical_available = compact_money(payload["logical_available_usdt"])
            initial_margin = compact_money(payload["total_initial_margin_usdt"])
            margin_line = (
                f"保证金: 账户未占用资金(含保留) {logical_available} U | "
                f"交易所实际占用 {initial_margin} U; "
                "共享开仓额度仍按原配置执行\n"
            )
        return (
            "💹 交易资金与盈亏已恢复初始状态\n"
            f"系统处理: 交易资金净值已恢复为 {operating_envelope} U; 系统总盈亏、"
            "每日/每月/累计盈亏、各条线、"
            "各带单员和当前仓位的盈亏均已从现在重新计为 0\n"
            f"{margin_line}"
            "保留内容: 当前仓位、待成交订单、带单员配置和历史审计记录均未修改; "
            "已有仓位仍会占用保证金额度\n"
            f"生效时间: {_display_shanghai_time(payload.get('occurred_at'))}"
        )
    if payload.get("event") == "copy_leader_symbol_stop_triggered":
        leader_id = _safe_text(str(payload.get("lead_portfolio_id", "UNKNOWN")), 24)
        nickname = _safe_text(str(payload.get("leader_nickname", "名称未知")), 32)
        symbol = _safe_text(str(payload.get("symbol", "UNKNOWN")), 24)
        try:
            net_pnl = signed_money(payload.get("net_position_pnl_usdt", "0"))
            limit = compact_money(payload.get("loss_limit_usdt", "10"))
        except ValueError:
            net_pnl = _safe_text(str(payload.get("net_position_pnl_usdt", "未知")), 40)
            limit = _safe_text(str(payload.get("loss_limit_usdt", "10")), 40)
        side_parts: list[str] = []
        breakdown = payload.get("position_pnl_breakdown")
        if isinstance(breakdown, list):
            for item in breakdown[:2]:
                if not isinstance(item, Mapping):
                    continue
                side = {"LONG": "多", "SHORT": "空"}.get(
                    str(item.get("position_side")),
                    _safe_text(str(item.get("position_side", "未知")), 8),
                )
                try:
                    side_pnl = signed_money(item.get("unrealized_pnl_usdt", "0"))
                except ValueError:
                    side_pnl = _safe_text(
                        str(item.get("unrealized_pnl_usdt", "未知")),
                        40,
                    )
                side_parts.append(f"{side} {side_pnl} U")
        side_text = f" ({' | '.join(side_parts)})" if side_parts else ""
        return (
            "🛡️ 带单员单币种止损已触发\n"
            f"带单员: {nickname} (ID {leader_id})\n"
            f"币种: {symbol}\n"
            f"现有仓位合计浮盈亏: {net_pnl} U{side_text}\n"
            f"止损上限: -{limit} U\n"
            "系统处理: 已撤销该带单员在此币种的待入场订单, "
            "并为其多仓和空仓分别提交市价平仓; "
            "其他带单员、其他币种均不受影响\n"
            "风控范围: 仅跳过该带单员在此币种的新开仓/加仓, "
            "减仓和平仓仍允许\n"
            f"恢复时间: {_display_shanghai_time(payload.get('blocked_until'))} "
            "(24小时后自动恢复)\n"
            f"触发时间: {_display_shanghai_time(payload.get('occurred_at'))}"
        )
    if payload.get("event") == "copy_system":
        raw_state = str(payload.get("state", "UNKNOWN"))
        summary = _safe_text(str(payload.get("summary", "")), 800)
        reasons = _payload_reason_text(payload)
        strategy = str(payload.get("strategy", raw_state.split("_SELECTION_", 1)[0]))
        strategy_label = {
            "SHORT_TERM": "短线",
            "LONG_TERM": "长线",
            "LEGACY": "定时",
        }.get(strategy, "带单员")
        if raw_state.endswith("_SELECTION_FAILED"):
            reason = (reasons or translate_reason_codes_in_text(summary.rsplit(":", 1)[-1])).strip()
            return (
                f"❌ {strategy_label}选人失败\n"
                "系统处理: 保留当前带单员, 不会强行替换; 后续定时任务将自动重试\n"
                f"原因: {reason}"
            )
        if raw_state.endswith("_SELECTION_UNCHANGED"):
            reason = (
                reasons or translate_reason_codes_in_text(summary.rsplit("原因:", 1)[-1])
            ).strip()
            return (
                f"📌 {strategy_label}选人未更换\n"
                "系统处理: 保留当前带单员, 本轮不执行替换\n"
                f"原因: {reason}"
            )
        state_label = {
            "RECOVERED": "故障已恢复",
            "TESTNET RUNNING": "测试盘运行中",
            "PRODUCTION RUNNING": "正式盘运行中",
        }.get(raw_state, _safe_text(raw_state, 32))
        if raw_state == "RECOVERED":
            return (
                "✅ 跟单系统故障已恢复\n"
                "系统处理: 已恢复正常监控; 原故障证据继续保留, 同类问题再次出现会重新告警\n"
                f"恢复依据: {summary}"
            )
        lines = [f"🚀 跟单系统: {state_label}"]
        if summary:
            lines.append(f"详情: {summary}")
        if reasons:
            lines.append(f"原因: {reasons}")
        return "\n".join(lines)
    if payload.get("event") == "copy_runtime_control":
        state = _safe_text(str(payload.get("state", "UNKNOWN")), 24)
        reason_codes = payload.get("reason_codes")
        reasons = _payload_reason_text(payload)
        if (
            state == "RUNNING"
            and isinstance(reason_codes, list)
            and any(
                code
                in {
                    "COPY_OPERATOR_FLATTEN_COMPLETED_AUTO_RESUME",
                    "COPY_REDUCE_ALL_COMPLETED_AUTO_RESUME",
                }
                for code in reason_codes
            )
        ):
            return f"✅ 全部减仓完成\n系统处理: 现有仓位已清零, 已自动恢复正常跟单\n原因: {reasons}"
        if (
            state == "PAUSED_NEW_ENTRIES"
            and isinstance(reason_codes, list)
            and "COPY_SAFETY_FLATTEN_COMPLETED_REMAINS_PAUSED" in reason_codes
        ):
            return (
                "🛡️ 风险清仓完成\n"
                "系统处理: 现有仓位已清零; 风险锁继续保持, 不会自动恢复新开仓\n"
                f"原因: {reasons}"
            )
        state_label = {
            "RUNNING": "正常跟单",
            "PAUSED_NEW_ENTRIES": "暂停新开仓",
            "REDUCE_ALL": "执行全部减仓",
        }.get(state, state)
        action = {
            "RUNNING": "允许处理新开仓、加仓、减仓和平仓信号",
            "PAUSED_NEW_ENTRIES": "不再建立新仓位; 已有仓位的减仓和平仓继续执行",
            "REDUCE_ALL": "停止新开仓并逐笔清理本系统归属仓位",
        }.get(state, "已保存新的运行控制状态")
        lines = [f"⚙️ 跟单运行状态: {state_label}", f"系统处理: {action}"]
        if reasons:
            lines.append(f"原因: {reasons}")
        return "\n".join(lines)
    if payload.get("event") == "copy_codex_audit":
        state = _safe_text(str(payload.get("state", "UNKNOWN")), 16)
        summary = _safe_text(str(payload.get("summary", "")), 600)
        actions_raw = payload.get("applied_actions")
        icon = "🚨" if state == "CRITICAL" else "🤖"
        state_label = {
            "HEALTHY": "正常",
            "DEGRADED": "存在异常",
            "CRITICAL": "严重异常",
        }.get(state, state)
        action_text = _codex_action_text(actions_raw, state=state)
        text = f"{icon} Codex 小时审查: {state_label}\n检查发现: {summary}"
        return f"{text}\n系统处理: {action_text}"
    if payload.get("event") == "copy_codex_repair":
        state = _safe_text(str(payload.get("state", "UNKNOWN")), 16)
        summary = _safe_text(str(payload.get("summary", "")), 800)
        root_cause = _safe_text(str(payload.get("root_cause", "")), 600)
        changed_raw = payload.get("changed_files")
        changed = (
            ", ".join(_safe_text(str(value), 120) for value in changed_raw[:8])
            if isinstance(changed_raw, list)
            else ""
        )
        icon = "✅" if state == "REPAIRED" else ("📝" if state == "NO_CHANGE" else "🚨")
        state_label = {
            "REPAIRED": "已修复",
            "NO_CHANGE": "无需修改代码",
            "FAILED": "修复失败",
        }.get(state, state)
        text = f"{icon} Codex 自动修复: {state_label}\n结论: {summary}\n根因: {root_cause}"
        if changed:
            text = f"{text}\n修改: {changed}"
        if payload.get("resumed") is True:
            text = f"{text}\n复检通过, {environment_label}新开仓已自动恢复。"
        return text[:3900]
    if payload.get("event") == "copy_health":
        state = _safe_text(str(payload.get("state", "UNKNOWN")), 16)
        findings_raw = payload.get("findings")
        findings = (
            [
                (
                    _safe_text(str(item.get("code", "UNKNOWN")), 80),
                    _safe_text(str(item.get("detail", "")), 160),
                )
                for item in findings_raw[:6]
                if isinstance(item, dict)
            ]
            if isinstance(findings_raw, list)
            else []
        )
        icon = "🚨" if state == "FAILED" else "⚠️"
        details = "\n".join(
            f"• 原因: {_health_finding_label(code)}" + (f"\n  证据: {detail}" if detail else "")
            for code, detail in findings
        )
        protections: list[str] = []
        requested_control = payload.get("requested_control")
        if requested_control == "PAUSED_NEW_ENTRIES":
            protections.append("已请求暂停新开仓; 减仓/平仓继续")
        elif requested_control == "REDUCE_ALL":
            protections.append("已请求紧急逐仓减仓")
        if payload.get("codex_wakeup_requested") is True:
            protections.append("已唤醒 Codex 立即诊断; 若确认是代码或配置缺陷, 将自动修复并复检")
        suffix = (
            "\n系统处理:\n" + "\n".join(f"• {item}" for item in protections)
            if protections
            else "\n系统处理: 已记录异常, 等待下一轮巡检复核"
        )
        state_label = "严重异常" if state == "FAILED" else "存在异常"
        text = f"{icon} 系统巡检: {state_label}"
        if details:
            text = f"{text}\n{details}"
        return f"{text}{suffix}"
    state = _safe_text(str(payload.get("state", "UNKNOWN")), 32)
    symbol = _safe_text(str(payload.get("symbol", "UNKNOWN")), 24)
    side = _safe_text(str(payload.get("position_side", "UNKNOWN")), 8)
    kind = _safe_text(str(payload.get("signal_kind", "UNKNOWN")), 16)
    raw_quantity = payload.get("local_quantity", "0")
    try:
        quantity = compact_decimal(raw_quantity)
    except ValueError:
        quantity = _safe_text(str(raw_quantity), 40)
    leader = _safe_text(str(payload.get("lead_portfolio_id", "UNKNOWN")), 24)
    nickname = _safe_text(str(payload.get("leader_nickname", "名称未知")), 32)
    reasons_raw = payload.get("reason_codes")
    reason_codes = (
        tuple(_safe_text(str(reason), 80) for reason in reasons_raw[:4])
        if isinstance(reasons_raw, list)
        else ()
    )
    reasons = _signal_reason_text(reason_codes)
    leader_symbol_stop_signal = payload.get("leader_symbol_stop_event_id") is not None
    icon = {
        "RECEIVED": "🔄",
        "APPROVED": "⏳",
        "FILLED": "✅",
        "SUBMITTED": "⏳",
        "CANCELLED": "🕒",
        "SHADOW_ONLY": "👁",
        "IGNORED_ORPHAN": "↩️",
        "IGNORED_MINIMUM": "▫️",
        "IGNORED_DRAINING": "👤",
        "RISK_REJECTED": "⚠️",
        "FAILED": "❌",
        "UNCERTAIN": "🚨",
    }.get(state, "i")
    if state == "SUBMITTED" and leader_symbol_stop_signal:
        title = "🛡️ 单币种止损平仓已提交"
    elif state == "SUBMITTED":
        title = "📡 交易信号"
    elif state == "RECEIVED":
        title = "🔄 交易信号等待处理"
    elif state == "APPROVED":
        title = "⏳ 交易信号准备提交"
    elif state == "FILLED" and kind == "INCREASE":
        title = "✅ 跟单入场成功"
    elif state == "FILLED" and kind == "REDUCE" and leader_symbol_stop_signal:
        title = "✅ 单币种止损平仓成功"
    elif state == "FILLED" and kind == "REDUCE":
        title = "✅ 跟单减仓/平仓成功"
    elif state == "RISK_REJECTED" and any(
        reason.startswith("COPY_NEW_ENTRIES_") for reason in reason_codes
    ):
        title = "⏸️ 新开仓已暂停"
    elif state == "RISK_REJECTED" and "COPY_TRADIFI_AGREEMENT_REQUIRED" in reason_codes:
        title = "⚠️ Binance 合约协议未开通"
    elif state == "RISK_REJECTED":
        title = "🛡️ 风控跳过"
    elif state == "CANCELLED" and kind == "INCREASE":
        title = "🕒 待入场订单已取消"
    elif state == "CANCELLED":
        title = "🕒 跟单操作已取消"
    elif state == "SHADOW_ONLY":
        title = "👁 仅记录信号, 未执行下单"
    elif state == "IGNORED_ORPHAN":
        title = "📡 带单员减仓/平仓信号"
    elif state == "IGNORED_MINIMUM":
        title = "▫️ 下单量不足, 已跳过"
    elif state == "IGNORED_DRAINING":
        title = "👤 带单员退出中, 未开仓"
    elif state == "FAILED":
        title = "❌ 跟单执行失败"
    elif state == "UNCERTAIN":
        title = "🚨 订单状态待确认"
    else:
        title = f"{icon} 跟单状态更新"
    side_label = {"LONG": "多单", "SHORT": "空单"}.get(side, side)
    kind_label = {
        "INCREASE": "开仓/加仓",
        "REDUCE": "减仓/平仓",
    }.get(kind, kind)
    lines = [title, f"{symbol} · {side_label} · {kind_label}"]
    if state != "IGNORED_ORPHAN":
        lines[-1] = f"{lines[-1]} | 数量 {quantity}"
    lines.append(f"带单员: {nickname} (ID {leader})")
    if leader_symbol_stop_signal:
        try:
            stop_net_pnl = signed_money(payload.get("stop_net_position_pnl_usdt", "0"))
            stop_limit = compact_money(payload.get("stop_loss_limit_usdt", "10"))
        except ValueError:
            stop_net_pnl = "未知"
            stop_limit = "10"
        lines.append(
            f"风控: 现有仓位合计浮盈亏 {stop_net_pnl} U 触及 -{stop_limit} U; "
            f"仅平该带单员此币种, 冷却至 "
            f"{_display_shanghai_time(payload.get('stop_blocked_until'))}"
        )
    if state == "IGNORED_ORPHAN":
        try:
            source_quantity = compact_decimal(payload.get("source_quantity", "0"))
        except ValueError:
            source_quantity = _safe_text(str(payload.get("source_quantity", "未知")), 40)
        source_quantity_text = f" | 带单员数量 {source_quantity}" if source_quantity != "0" else ""
        lines[1] = f"{symbol} · {side_label} · {kind_label}{source_quantity_text}"
        lines.extend(
            [
                f"带单员成交价: {_display_price(payload.get('leader_reference_price'))}",
                *(
                    [f"带单员本次收益: {compact_decimal(payload['leader_realized_pnl_delta'])} U"]
                    if payload.get("leader_realized_pnl_delta") is not None
                    else []
                ),
                "系统处理: 只记录带单员信号, 未向 Binance 提交订单",
                f"原因: {reasons}",
                _notification_time_line(payload),
            ]
        )
        return f"\n{'\n'.join(lines)}\n"
    capital = _entry_capital_text(payload)
    if capital:
        lines.append(f"资金: {capital}")
    lines.append(_notification_price_line(payload))
    pnl_suffix = _order_pnl_suffix(payload)
    if pnl_suffix:
        lines.append(_card_text(pnl_suffix).strip())
    system_action = _signal_system_action(state, kind=kind, reason_codes=reason_codes)
    if system_action:
        lines.append(f"系统处理: {system_action}")
    if reasons and state not in {"SUBMITTED", "FILLED"}:
        lines.append(f"原因: {reasons}")
    lines.append(_notification_time_line(payload))
    body = "\n".join(lines)
    return f"\n{body}\n"


def _signal_reason_text(reason_codes: tuple[str, ...]) -> str:
    labels = {
        "COPY_NEW_ENTRIES_PAUSED_NEW_ENTRIES": (
            "系统处于暂停新开仓; 本次信号已跳过, 与交易对支持无关. 恢复后仅处理后续新信号"
        ),
        "COPY_NEW_ENTRIES_REDUCE_ALL": (
            "系统正在执行全部减仓; 本次新开仓信号已跳过. 清仓完成后会自动继续跟单"
        ),
        "COPY_ENTRY_SKIPPED_DURING_OPERATOR_FLATTEN": (
            "人工清仓窗口内的新开仓已跳过且不会事后重放; 清仓完成后仅跟随后续新信号"
        ),
        "COPY_SIZE_MARGIN_CAP_REACHED": "可用保证金容量不足, 本次没有下单",
        "COPY_SIZE_TOTAL_MARGIN_CAP_REACHED": (
            "当前配置的共享可用保证金额度不足以满足最小下单量; "
            "已成交仓位和待入场订单都会占用额度, 本次没有下单"
        ),
        "COPY_SIZE_AVAILABLE_BALANCE_RESERVE_REACHED": (
            "交易账户实际可用余额已触及 30 U 保留线, 本次没有下单"
        ),
        "COPY_SIZE_ORDER_MARGIN_CAP_REACHED": "单笔保证金上限不足以满足最小下单量",
        "COPY_SIZE_SYMBOL_MARGIN_CAP_REACHED": (
            "该交易对保证金已接近 20 U 上限, 剩余额度不足以满足最小下单量"
        ),
    }
    has_protected_limit_reason = any(
        reason.startswith("COPY_PROTECTED_LIMIT_") for reason in reason_codes
    )
    has_tradifi_prerequisite = "COPY_TRADIFI_AGREEMENT_REQUIRED" in reason_codes
    translated: list[str] = []
    for reason in reason_codes:
        if has_protected_limit_reason and reason in {
            "COPY_ORDER_CANCELED",
            "COPY_ORDER_EXPIRED",
        }:
            # Older persisted notifications may contain both the semantic reason
            # and Binance's generic terminal status.  They describe one event, so
            # only show the actionable semantic explanation to the operator.
            continue
        if has_tradifi_prerequisite and reason == "COPY_EXCHANGE_CODE_4411":
            # The semantic reason already includes Binance's original error and
            # the exact operator action.  Avoid showing the same cause twice.
            continue
        label = labels.get(reason, reason_code_text(reason))
        if label not in translated:
            translated.append(label)
    return "; ".join(translated)


def _payload_reason_text(payload: Mapping[str, Any]) -> str:
    raw = payload.get("reason_codes")
    if not isinstance(raw, list):
        return ""
    codes = tuple(_safe_text(str(reason), 120) for reason in raw[:6])
    return _signal_reason_text(codes)


def _codex_action_text(actions: Any, *, state: str) -> str:
    if not isinstance(actions, list) or not actions:
        if state == "HEALTHY":
            return "未发现需要修复的故障, 无需改代码或重启; 系统继续正常运行和定时巡检"
        if state == "DEGRADED":
            return (
                "本轮确认暂无需改代码、重启或暂停; 已保留异常证据并继续复核, "
                "异常持续或恶化时会再次触发自动修复"
            )
        return "本轮没有安全可执行的自动变更; 现有风控保持不变, 故障证据已保留供下一轮修复复核"
    labels = {
        "NO_ACTION": "无需自动操作, 继续定时巡检",
        "PAUSE_NEW_ENTRIES": "已暂停新开仓; 已有仓位的减仓和平仓继续执行",
        "RESTART_COPY_POLLER": "已重启带单员轮询与跟单服务",
        "RESTART_TELEGRAM": "已重启 Telegram 通知服务",
        "RUN_CODE_REPAIR": "已唤醒 Codex 自动诊断并修复代码",
    }
    translated = [labels.get(str(value), "已执行一项受控恢复操作") for value in actions[:4]]
    return "; ".join(dict.fromkeys(translated))


def _signal_system_action(
    state: str,
    *,
    kind: str,
    reason_codes: tuple[str, ...] = (),
) -> str:
    """Explain the consequence of a non-success state without exposing internals."""

    if state == "RECEIVED":
        return "已保存信号, 将在下一轮继续安全处理"
    if state == "APPROVED":
        return "风险和额度检查已通过, 正在准备提交订单"
    if state == "SUBMITTED":
        return "已按通知中的委托价格向 Binance 提交订单; 当前尚未宣称成交, 成交后会另发成功通知"
    if state == "FILLED":
        return "Binance 已确认成交, 本地带单员账本和仓位记录均已更新"
    if state == "CANCELLED":
        return (
            "已撤销待入场订单, 本次不会建立仓位"
            if kind == "INCREASE"
            else "本次减仓/平仓订单已撤销, 不会按未成交数量修改仓位"
        )
    if state == "SHADOW_ONLY":
        return "只保存带单员信号, 当前运行模式不会向 Binance 提交订单"
    if state == "RISK_REJECTED" and "COPY_TRADIFI_AGREEMENT_REQUIRED" in reason_codes:
        return (
            "Binance 已在撮合前拒绝请求; 系统确认原订单未生成、未成交且无仓位残留, 不会自动反复下单"
        )
    if state in {"IGNORED_MINIMUM", "IGNORED_DRAINING", "RISK_REJECTED"}:
        return "未向 Binance 提交订单"
    if state == "FAILED":
        return "本次跟单未完成, 已记录错误并触发自动排查"
    if state == "UNCERTAIN":
        return (
            "Binance 测试盘本次没有返回明确成交结果; 系统仅核对原订单, "
            "不会循环重试或把同一笔带单信号执行多次"
        )
    return ""


def _notification_contextual_view(payload: Mapping[str, Any]) -> str | None:
    # Trade-signal and fill messages are durable notifications, not dashboard pages.
    # Editing either through an inline callback makes the original event disappear.
    if payload.get("event") == "copy_pnl_reset":
        return None
    if payload.get("event") == "copy_leader_availability_alert":
        return None
    if payload.get("event") == "copy_leader_symbol_stop_triggered":
        return "positions"
    if payload.get("event") == "copy_signal_decision" and payload.get("state") in {
        "SUBMITTED",
        "FILLED",
    }:
        return None
    if payload.get("event") == "copy_signal_decision" and payload.get("state") in {
        "APPROVED",
        "UNCERTAIN",
    }:
        return "pending"
    reasons = payload.get("reason_codes")
    if (
        payload.get("event") == "copy_signal_decision"
        and payload.get("state") == "RISK_REJECTED"
        and isinstance(reasons, list)
        and any(str(reason).startswith("COPY_NEW_ENTRIES_") for reason in reasons)
    ):
        return "control"
    if payload.get("event") == "copy_signal_decision" and payload.get("state") == "RISK_REJECTED":
        return None
    return {
        "copy_signal_decision": "positions",
        "copy_codex_audit": "codex",
        "copy_codex_repair": "repair",
        "copy_slot_selection": "leaders",
        "copy_slot_replacement": "leaders",
        "copy_leader_manual_change": "leaders",
        "copy_leader_follow_multiplier_change": "leaders",
        "copy_entry_margin_limit_change": "funds",
        "copy_leader_lock_change": "leaders",
        "copy_leader_symbol_stop_triggered": "positions",
        "copy_health": "health",
        "copy_runtime_control": "control",
        "copy_system": "status",
    }.get(str(payload.get("event")), "status")


def _notification_restores_navigation_keyboard(payload: Mapping[str, Any]) -> bool:
    """Keep the reply-keyboard icon registered after any leader replacement result."""

    return payload.get("event") in {
        "copy_leader_manual_change",
        "copy_slot_replacement",
        "copy_slot_selection",
    }


def _current_entry_margin_limit(cursor: psycopg.Cursor[dict[str, Any]]) -> Decimal:
    cursor.execute(
        """
        SELECT limit_usdt FROM copytrading.entry_margin_limit_events
         ORDER BY occurred_at DESC,limit_event_id DESC LIMIT 1
        """,
        (),
    )
    row = cursor.fetchone()
    return (
        _valid_entry_margin_limit(Decimal(str(row["limit_usdt"])))
        if row is not None
        else DEFAULT_ENTRY_MARGIN_LIMIT_USDT
    )


def _valid_entry_margin_limit(value: Decimal) -> Decimal:
    quantum = Decimal("0.01")
    if (
        not value.is_finite()
        or not MINIMUM_ENTRY_MARGIN_LIMIT_USDT <= value <= DEFAULT_ENTRY_MARGIN_LIMIT_USDT
        or value != value.quantize(quantum)
    ):
        raise ValueError("Telegram entry margin limit is invalid")
    return value.quantize(quantum)


def _render_cards(title: str, cards: list[str], *, empty: str | None = None) -> str:
    if not cards:
        return empty or title
    rendered = f"{title}\n" + f"\n{_CARD_DIVIDER}\n".join(cards)
    if len(rendered) <= 4000:
        return rendered
    included: list[str] = []
    for card in cards:
        candidate_cards = [*included, card]
        omitted = len(cards) - len(candidate_cards)
        candidate = f"{title}\n" + f"\n{_CARD_DIVIDER}\n".join(candidate_cards)
        if omitted:
            candidate += f"\n{_CARD_DIVIDER}\n⚠️ 另有 {omitted} 条记录未显示。"
        if len(candidate) > 4000:
            break
        included.append(card)
    if not included:
        return bounded_telegram_text(rendered)
    omitted = len(cards) - len(included)
    result = f"{title}\n" + f"\n{_CARD_DIVIDER}\n".join(included)
    if omitted:
        result += f"\n{_CARD_DIVIDER}\n⚠️ 另有 {omitted} 条记录未显示。"
    return bounded_telegram_text(result)


def _stack_details(value: str) -> str:
    return value.replace(" | ", "\n")


def _card_text(value: str) -> str:
    return value.removeprefix("  ").replace("\n  ", "\n")


def _order_policy_text(row: Mapping[str, Any]) -> str:
    order_type = row.get("order_type")
    if order_type == "LIMIT":
        price = compact_decimal(row["limit_price"])
        expires_at = row.get("expires_at")
        if expires_at is None:
            return f"保护限价 {price} | 持续挂单至带单员退出该仓位"
        expires = (
            expires_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")
            if isinstance(expires_at, datetime)
            else "时间未知"
        )
        return f"保护限价 {price} | 有效至 {expires}"
    if order_type == "MARKET":
        return "市价立即执行"
    return "尚未提交"


def _pending_entry_state(row: Mapping[str, Any]) -> str:
    submission_state = str(row.get("submission_state") or "")
    return {
        "SUBMITTING": "正在提交",
        "ACKNOWLEDGED": "交易所已接单, 等待成交",
        "PARTIALLY_FILLED": "部分成交, 剩余数量继续挂单",
        "FILLED": "已成交, 等待本地仓位确认",
        "RECONCILED": "已成交, 等待本地仓位确认",
        "UNKNOWN": "交易所状态待确认",
        "REJECTED": "订单已终止, 等待本地归档",
    }.get(submission_state, f"本地{row.get('decision_state', 'SUBMITTED')}")


def _pending_expiry_text(value: Any, *, now: datetime) -> str:
    if value is None:
        return "有效期: 持续挂单至带单员退出该仓位"
    if not isinstance(value, datetime) or value.tzinfo is None:
        return "有效期: 未知"
    expires_at = value.astimezone(UTC)
    expires_text = expires_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M:%S")
    remaining_seconds = int((expires_at - now.astimezone(UTC)).total_seconds())
    if remaining_seconds <= 0:
        return f"有效至: {expires_text} (已到期, 等待撤单确认)"
    hours, remainder = divmod(remaining_seconds, 3600)
    minutes = remainder // 60
    remaining_text = f"{hours}小时{minutes}分" if hours else f"{minutes}分"
    return f"有效至: {expires_text} (剩余约{remaining_text})"


def _display_price(value: Any) -> str:
    if value is None:
        return "尚无"
    try:
        return compact_decimal(value, maximum_places=8)
    except ValueError:
        return "未知"


def _order_price_text(row: Mapping[str, Any]) -> str:
    leader_price = (
        "控制指令无参考价"
        if row.get("signal_origin") == "CONTROL"
        else f"带单员价位 {_display_price(row.get('leader_reference_price'))}"
    )
    if row.get("order_type") == "LIMIT":
        limit_price = _display_price(row.get("limit_price"))
    elif row.get("order_type") == "MARKET":
        limit_price = "市价单"
    else:
        limit_price = "尚未提交"
    fill_label = (
        "我的入场成交均价" if row.get("signal_kind") == "INCREASE" else "我的减/平仓成交均价"
    )
    fill_price = _display_price(row.get("system_fill_price"))
    return f"{leader_price} | 我的委托限价 {limit_price} | {fill_label} {fill_price}"


def _display_shanghai_time(value: Any) -> str:
    try:
        occurred_at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if occurred_at.tzinfo is None:
            return "未知"
        return occurred_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return "未知"


def _order_time_text(row: Mapping[str, Any]) -> str:
    source_label = "控制指令时间" if row.get("signal_origin") == "CONTROL" else "带单员记录时间"
    return (
        f"{source_label} {_display_shanghai_time(row.get('source_occurred_at'))} | "
        f"系统处理时间 {_display_shanghai_time(row.get('occurred_at'))}"
    )


def _notification_price_line(row: Mapping[str, Any]) -> str:
    control_signal = row.get("signal_origin") == "CONTROL"
    leader_price = _display_price(row.get("leader_reference_price"))
    order_type = row.get("order_type")
    if order_type == "LIMIT":
        order_price = _display_price(row.get("limit_price"))
        if row.get("state") == "SUBMITTED":
            expires = _display_shanghai_expiry(row.get("expires_at"))
            if expires != "未知":
                order_price = f"{order_price} (至 {expires})"
    elif order_type == "MARKET":
        order_price = "市价"
    else:
        order_price = "尚未提交"
    fill_price = _display_price(row.get("system_fill_price"))
    if fill_price == "尚无":
        fill_price = "待成交"
    if control_signal:
        return f"价格: 控制减仓 | 委托 {order_price} | 成交 {fill_price}"
    return f"价格: 带单员 {leader_price} | 委托 {order_price} | 成交 {fill_price}"


def _notification_time_line(row: Mapping[str, Any]) -> str:
    source_label = "控制指令" if row.get("signal_origin") == "CONTROL" else "带单员"
    return (
        f"时间: {source_label} {_display_shanghai_time(row.get('source_occurred_at'))} | "
        f"系统 {_display_shanghai_time(row.get('occurred_at'))}"
    )


def _display_shanghai_expiry(value: Any) -> str:
    try:
        expires_at = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if expires_at.tzinfo is None:
            return "未知"
        return expires_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return "未知"


def _display_pnl(value: Any) -> str:
    if value is None:
        return "未提供"
    try:
        return f"{signed_money(value)} U"
    except ValueError:
        return "未知"


def _rebased_logical_equity(operating_envelope: Decimal, pnl_since_reset: Decimal) -> Decimal:
    """Return dashboard equity from the configured envelope and latest PnL baseline."""

    return max(Decimal("0"), operating_envelope + pnl_since_reset)


def _net_account_adjustment(
    account_total_pnl: Decimal,
    line_rows: Sequence[Mapping[str, Any]],
) -> Decimal:
    """Return the net adjustment that reconciles line gross PnL to account net PnL."""

    line_gross_pnl = sum(
        (
            Decimal(str(row["total_pnl_usdt"])) - Decimal(str(row.get("reset_total_pnl_usdt") or 0))
            for row in line_rows
        ),
        Decimal("0"),
    )
    return account_total_pnl - line_gross_pnl


def _account_pnl_since_reset(
    row: Mapping[str, Any],
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    """Return account PnL, hiding the stale valuation frame immediately after reset."""

    observed_at = row.get("observed_at")
    reset_occurred_at = row.get("reset_occurred_at")
    if (
        isinstance(observed_at, datetime)
        and isinstance(reset_occurred_at, datetime)
        and observed_at < reset_occurred_at
    ):
        zero = Decimal("0")
        return zero, zero, zero, zero, zero
    raw_total = Decimal(str(row["total_pnl_usdt"]))
    return (
        raw_total - Decimal(str(row["reset_total_pnl_usdt"])),
        raw_total - Decimal(str(row["day_anchor_pnl_usdt"])),
        raw_total - Decimal(str(row["month_anchor_pnl_usdt"])),
        Decimal(str(row["realized_net_pnl_usdt"]))
        - Decimal(str(row["reset_realized_net_pnl_usdt"])),
        Decimal(str(row["unrealized_pnl_usdt"])) - Decimal(str(row["reset_unrealized_pnl_usdt"])),
    )


def _position_pnl_text(row: Mapping[str, Any]) -> str:
    mark_raw = row.get("mark_price")
    entry_raw = row.get("system_average_entry_price")
    realized_raw = row.get("position_realized_pnl_usdt")
    if mark_raw is None or entry_raw is None or realized_raw is None:
        return "  当前标记价/逐仓盈亏: 暂无完整标记数据"
    try:
        mark = Decimal(str(mark_raw))
        entry = Decimal(str(entry_raw))
        quantity = Decimal(str(row["resulting_local_quantity"]))
        margin = Decimal(str(row["committed_margin_usdt"]))
        realized = Decimal(str(realized_raw))
        unrealized_reset_anchor = Decimal(
            str(row.get("position_unrealized_pnl_reset_anchor_usdt") or 0)
        )
        direction = Decimal("1") if str(row["position_side"]) == "LONG" else Decimal("-1")
        unrealized = (mark - entry) * quantity * direction - unrealized_reset_anchor
        total = realized + unrealized
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return "  当前标记价/逐仓盈亏: 数据异常"
    if any(
        not value.is_finite()
        for value in (mark, entry, quantity, margin, realized, unrealized_reset_anchor)
    ):
        return "  当前标记价/逐仓盈亏: 数据异常"
    return_rate = unrealized / margin * Decimal("100") if margin > 0 else None
    rate_text = f" ({signed_percent(return_rate)})" if return_rate is not None else ""
    return (
        f"  标记 {_display_price(mark)} | "
        f"未实现 {signed_money(unrealized)} U{rate_text}\n"
        f"  已实现 {signed_money(realized)} U | "
        f"累计 {signed_money(total)} U"
    )


def _order_pnl_suffix(row: Mapping[str, Any]) -> str:
    if row.get("signal_kind") != "REDUCE" or row.get("state") != "FILLED":
        return ""
    system_pnl = _display_pnl(row.get("system_realized_pnl_delta_usdt"))
    if row.get("signal_origin") == "CONTROL":
        return f"\n  本次平仓收益: 我的系统 {system_pnl}"
    return (
        "\n  本次平仓收益: "
        f"带单员 {_display_pnl(row.get('leader_realized_pnl_delta'))} | "
        f"我的系统 {system_pnl}"
    )


def _entry_capital_text(row: Mapping[str, Any]) -> str:
    if row.get("signal_kind") != "INCREASE":
        return ""
    state = str(row.get("state", ""))
    if state not in {"SUBMITTED", "FILLED"}:
        return ""
    try:
        leverage = int(row["leverage"])
        if not 1 <= leverage <= 125:
            return ""
        margin_raw = row.get("order_margin_usdt")
        if margin_raw is None:
            quantity_raw = (
                row.get("local_quantity")
                if state == "FILLED"
                else row.get("requested_quantity", row.get("local_quantity"))
            )
            price_raw = (
                row.get("system_fill_price") or row.get("limit_price")
                if state == "FILLED"
                else row.get("limit_price")
            )
            quantity = Decimal(str(quantity_raw))
            price = Decimal(str(price_raw))
            margin = quantity * price / Decimal(leverage)
        else:
            margin = Decimal(str(margin_raw))
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return ""
    if not margin.is_finite() or margin <= 0:
        return ""
    label = "入场保证金" if state == "FILLED" else "预计保证金"
    return f"{label} {compact_decimal(margin, maximum_places=8)} U | 杠杆 {leverage}x"


def _health_finding_label(code: str) -> str:
    return {
        "COPY_NO_ACTIVE_LEADERS": "没有可轮询带单员",
        "COPY_POLL_STALE": "带单员轮询已经停止或严重延迟",
        "COPY_POLL_DELAYED": "带单员轮询延迟",
        "COPY_LEADER_SLOTS_INCOMPLETE": "长短线席位不完整",
        "COPY_PUBLIC_POLL_FAILURES": "所有公开带单接口轮询失败",
        "COPY_PUBLIC_POLL_PARTIAL_FAILURE": "部分带单员轮询失败",
        "COPY_PUBLIC_HISTORY_GAP": "公开操作历史存在无法覆盖的缺口",
        "COPY_UNCERTAIN_SUBMISSIONS": "存在无法确认是否成交的订单",
        "COPY_RECENT_EXECUTION_FAILURES": "最近出现下单执行失败",
        "COPY_REPEATED_MINIMUM_REJECTIONS": "短时间连续出现不合理的最小下单额拒绝",
        "COPY_PROTECTED_ENTRY_OVERDUE": "保护限价单超过到期宽限仍未终结",
        "COPY_SLOT_REPLACEMENT_RECONCILE_OVERDUE": "带单员待替换已过期但尚未完成处理",
        "COPY_DEAD_TELEGRAM_NOTIFICATIONS": "存在多次发送失败的通知",
        "COPY_TELEGRAM_OUTBOX_STALLED": "Telegram 通知队列发生积压",
        "COPY_SHORT_SELECTION_STALE": "短线带单员选择结果过期",
        "COPY_LONG_SELECTION_STALE": "长线带单员选择结果过期",
        "COPY_CODEX_AUDIT_STALE": "Codex 小时审查过期",
        "COPY_CODEX_AUDIT_REPORTED_FAILURE": "上次 Codex 审查执行失败",
        "COPY_ACCOUNT_TRADING_DISABLED": "执行账户禁止交易",
        "COPY_HEDGE_MODE_NOT_READY": "执行账户不是双向持仓模式",
        "COPY_ACCOUNT_EMERGENCY_RISK_LINE": "账户触及紧急风险线",
        "COPY_ACCOUNT_WARNING_RISK_LINE": "账户触及风险预警线",
        "COPY_POSITION_RECONCILIATION_MISMATCH": "交易所仓位与虚拟账本不一致",
        "COPY_REQUIRED_SERVICE_INACTIVE": "核心服务未运行",
        "COPY_TESTNET_USER_STREAM_INACTIVE": "测试盘用户数据监听未运行",
        "COPY_HOST_DISK_CRITICAL": "VPS 磁盘空间严重不足",
        "COPY_HOST_DISK_LOW": "VPS 磁盘空间偏低",
        "COPY_HOST_MEMORY_CRITICAL": "VPS 可用内存严重不足",
        "COPY_HOST_MEMORY_LOW": "VPS 可用内存偏低",
        "COPY_DATABASE_BACKUP_MISSING": "尚无可验证的业务数据库备份",
        "COPY_DATABASE_BACKUP_CRITICAL": "业务数据库备份严重过期",
        "COPY_DATABASE_BACKUP_STALE": "业务数据库备份已经过期",
    }.get(code, "未分类巡检异常")


def _operator_reason_label(code: str) -> str:
    """Return a Chinese label for dashboards without leaking internal codes."""
    health_label = _health_finding_label(code)
    if health_label != "未分类巡检异常":
        return health_label
    return reason_code_text(code)


def _notification_order_policy(payload: Mapping[str, Any]) -> str:
    order_type = payload.get("order_type")
    if order_type == "MARKET":
        return "市价立即执行"
    if order_type != "LIMIT":
        return ""
    try:
        price = compact_decimal(payload.get("limit_price"))
    except ValueError:
        price = "价格未知"
    expires_raw = payload.get("expires_at")
    if expires_raw is None:
        return f"保护限价 {price} | 持续挂单至带单员退出该仓位"
    try:
        expires_at = datetime.fromisoformat(str(expires_raw))
        expires = expires_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        expires = "时间未知"
    return f"保护限价 {price} | 有效至 {expires}"


def _digest(document: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("Telegram state time must be timezone-aware UTC")
