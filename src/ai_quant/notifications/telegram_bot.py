"""Bidirectional Telegram Bot API client and authorization-aware menu router."""

from __future__ import annotations

import http.client
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from ai_quant.copy_trading.leader_slots import (
    LeaderSlot,
    leader_slot_callback,
    leader_slot_from_callback,
    leader_slot_label,
)
from ai_quant.copy_trading.reason_text import reason_code_text

_TOKEN = re.compile(r"^[1-9][0-9]{4,15}:[A-Za-z0-9_-]{20,}$")
_IDENTIFIER = re.compile(r"^-?[1-9][0-9]{0,19}$")
_TELEGRAM_HOST = "api.telegram.org"
_MAX_RESPONSE_BYTES = 1024 * 1024
_MESSAGE_SAFE_LIMIT = 4000
_MESSAGE_TRUNCATION_SUFFIX = "\n────────────\n⚠️ 内容过长, 后续记录已省略。"
_INTERNAL_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_NOTIFICATION_TOKEN = re.compile(r"^[0-9a-f]{16}$")
_NOTIFICATION_CALLBACK = re.compile(r"^n:([0-9a-f]{16}):(.+)$")
_TELEGRAM_CALLBACK_DATA_LIMIT = 64
_BINANCE_LEADER_PATH = re.compile(
    r"^/(?:[A-Za-z]{2}(?:-[A-Za-z]{2})?/)?copy-trading/lead-details/"
    r"([0-9]{10,24})/?$"
)


def bounded_telegram_text(text: str) -> str:
    """Keep dynamic messages inside Telegram's hard limit without crashing the bot."""
    if not text:
        raise ValueError("Telegram message cannot be empty")
    if len(text) <= _MESSAGE_SAFE_LIMIT:
        return text
    prefix = text[: _MESSAGE_SAFE_LIMIT - len(_MESSAGE_TRUNCATION_SUFFIX)].rstrip()
    return f"{prefix}{_MESSAGE_TRUNCATION_SUFFIX}"


class TelegramBotError(RuntimeError):
    """Telegram API or update contract failed without exposing the bot token."""


class TelegramCallbackExpired(TelegramBotError):
    """A callback query was already too old to acknowledge."""


class TelegramMessageNotModified(TelegramBotError):
    """An edit was rejected because text and inline markup are unchanged."""


@dataclass(frozen=True, slots=True)
class TelegramBotFileConfig:
    token: str
    allowed_chat_ids: frozenset[int]
    authorized_user_ids: frozenset[int]

    @classmethod
    def load(
        cls,
        token_file: Path,
        chat_ids_file: Path,
        authorized_user_ids_file: Path,
    ) -> TelegramBotFileConfig:
        token = token_file.read_text(encoding="ascii").strip()
        if not _TOKEN.fullmatch(token):
            raise ValueError("Telegram bot token file is empty or invalid")
        chats = _identifier_file(chat_ids_file, "chat")
        users = _identifier_file(authorized_user_ids_file, "authorized user")
        if any(user <= 0 for user in users):
            raise ValueError("Telegram authorized user IDs must be positive")
        return cls(token, frozenset(chats), frozenset(users))


@dataclass(frozen=True, slots=True)
class TelegramHttpResult:
    status: int
    body: bytes


TelegramTransport = Callable[[str, Mapping[str, object], float], TelegramHttpResult]


def _https_post(
    path: str,
    document: Mapping[str, object],
    timeout_seconds: float,
) -> TelegramHttpResult:
    if not path.startswith("/bot") or "/" not in path[4:]:
        raise TelegramBotError("TELEGRAM_DESTINATION_INVALID")
    body = json.dumps(dict(document), separators=(",", ":")).encode()
    connection = http.client.HTTPSConnection(_TELEGRAM_HOST, timeout=timeout_seconds)
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = response.read(_MAX_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException) as error:
        raise TelegramBotError("TELEGRAM_TRANSPORT_FAILED") from error
    finally:
        connection.close()
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise TelegramBotError("TELEGRAM_RESPONSE_TOO_LARGE")
    return TelegramHttpResult(response.status, payload)


class TelegramBotClient:
    def __init__(
        self,
        config: TelegramBotFileConfig,
        *,
        transport: TelegramTransport = _https_post,
        timeout_seconds: float = 15,
    ) -> None:
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("Telegram bot timeout is invalid")
        self.config = config
        self._transport = transport
        self._timeout = timeout_seconds

    def get_updates(self, *, offset: int, timeout_seconds: int = 30) -> tuple[dict[str, Any], ...]:
        if offset < 0 or not 0 <= timeout_seconds <= 50:
            raise ValueError("Telegram getUpdates parameters are invalid")
        result = self._call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout_seconds,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=float(timeout_seconds + 10),
        )
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise TelegramBotError("TELEGRAM_UPDATES_INVALID")
        return tuple(result)

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: Mapping[str, object] | None = None,
    ) -> int:
        if chat_id not in self.config.allowed_chat_ids:
            raise TelegramBotError("TELEGRAM_CHAT_NOT_ALLOWED")
        text = bounded_telegram_text(text)
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        result = self._call("sendMessage", payload)
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise TelegramBotError("TELEGRAM_SEND_RESULT_INVALID")
        return int(result["message_id"])

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        if not callback_query_id or len(callback_query_id) > 128 or len(text) > 200:
            raise ValueError("Telegram callback answer is invalid")
        self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: Mapping[str, object] | None = None,
    ) -> None:
        if chat_id not in self.config.allowed_chat_ids or message_id <= 0:
            raise TelegramBotError("TELEGRAM_MESSAGE_NOT_ALLOWED")
        text = bounded_telegram_text(text)
        payload: dict[str, object] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = dict(reply_markup)
        result = self._call("editMessageText", payload)
        if not isinstance(result, dict) or result.get("message_id") != message_id:
            raise TelegramBotError("TELEGRAM_EDIT_RESULT_INVALID")

    def _call(
        self,
        method: str,
        document: Mapping[str, object],
        *,
        timeout: float | None = None,
    ) -> Any:
        if method not in {
            "getUpdates",
            "sendMessage",
            "editMessageText",
            "answerCallbackQuery",
        }:
            raise TelegramBotError("TELEGRAM_METHOD_DENIED")
        response = self._transport(
            f"/bot{self.config.token}/{method}",
            document,
            timeout or self._timeout,
        )
        try:
            body = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TelegramBotError("TELEGRAM_RESPONSE_INVALID") from error
        if isinstance(body, dict) and body.get("ok") is not True:
            description = body.get("description")
            normalized = description.lower() if isinstance(description, str) else ""
            if "message is not modified" in normalized:
                raise TelegramMessageNotModified("TELEGRAM_MESSAGE_NOT_MODIFIED")
            if "query is too old" in normalized or "query id is invalid" in normalized:
                raise TelegramCallbackExpired("TELEGRAM_CALLBACK_EXPIRED")
        if response.status != 200 or not isinstance(body, dict) or body.get("ok") is not True:
            raise TelegramBotError("TELEGRAM_API_REJECTED")
        return body.get("result")


class ControlAction(StrEnum):
    PAUSE_NEW_ENTRIES = "pause"
    RESUME_TESTNET = "resume"
    REDUCE_ALL = "reduce_all"
    RESET_ACCOUNT_SUMMARY = "reset_summary"


class TelegramDashboardProvider(Protocol):
    def render(self, view: str) -> str: ...

    def pnl_leader_choices(self) -> tuple[LeaderPnlChoice, ...]: ...

    def render_leader_pnl(self, lead_portfolio_id: str) -> str: ...

    def notification_message(self, token: str) -> tuple[str, str] | None: ...


class TelegramChallengeStore(Protocol):
    def create(self, *, user_id: int, action: ControlAction) -> str: ...


class TelegramControlHandler(Protocol):
    def execute_confirmed(self, *, user_id: int, nonce: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class LeaderCandidateChoice:
    lead_portfolio_id: str
    button_label: str
    summary: str


@dataclass(frozen=True, slots=True)
class LeaderPnlChoice:
    lead_portfolio_id: str
    button_label: str


@dataclass(frozen=True, slots=True)
class LeaderChangeProposal:
    nonce: str
    confirmation_text: str


@dataclass(frozen=True, slots=True)
class LeaderMultiplierChoice:
    lead_portfolio_id: str
    button_label: str
    current_multiplier: int


@dataclass(frozen=True, slots=True)
class FollowMultiplierProposal:
    nonce: str
    confirmation_text: str


@dataclass(frozen=True, slots=True)
class EntryMarginLimitProposal:
    nonce: str
    confirmation_text: str


@dataclass(frozen=True, slots=True)
class LeaderLockChoice:
    lead_portfolio_id: str
    button_label: str
    locked: bool


@dataclass(frozen=True, slots=True)
class LeaderLockProposal:
    nonce: str
    confirmation_text: str


@dataclass(frozen=True, slots=True)
class PositionCloseChoice:
    lead_portfolio_id: str
    symbol: str
    position_side: str
    button_label: str


@dataclass(frozen=True, slots=True)
class PositionLeaderChoice:
    lead_portfolio_id: str
    button_label: str
    open_position_count: int


@dataclass(frozen=True, slots=True)
class PositionCloseProposal:
    nonce: str
    confirmation_text: str


class TelegramPositionAdmin(Protocol):
    def position_leader_choices(self) -> tuple[PositionLeaderChoice, ...]: ...

    def position_close_choices(
        self,
        *,
        lead_portfolio_id: str | None = None,
        page: int = 1,
    ) -> tuple[PositionCloseChoice, ...]: ...

    def create_position_close(
        self,
        *,
        user_id: int,
        lead_portfolio_id: str,
        symbol: str,
        position_side: str,
    ) -> PositionCloseProposal: ...

    def execute_position_close_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None: ...

    def create_leader_positions_close(
        self,
        *,
        user_id: int,
        lead_portfolio_id: str,
    ) -> PositionCloseProposal: ...

    def execute_leader_positions_close_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None: ...


class TelegramMarginAdmin(Protocol):
    def entry_margin_limit(self) -> Decimal: ...

    def create_entry_margin_limit_change(
        self,
        *,
        user_id: int,
        limit_usdt: Decimal,
    ) -> EntryMarginLimitProposal: ...

    def execute_entry_margin_limit_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None: ...


class TelegramLeaderAdmin(Protocol):
    def leader_management_text(self) -> str: ...

    def leader_candidates(
        self,
        *,
        slot: LeaderSlot,
    ) -> tuple[LeaderCandidateChoice, ...]: ...

    def create_leader_change(
        self,
        *,
        user_id: int,
        slot: LeaderSlot,
        lead_portfolio_id: str | None,
    ) -> LeaderChangeProposal: ...

    def execute_leader_change_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None: ...

    def create_external_leader_change(
        self,
        *,
        user_id: int,
        slot: LeaderSlot,
        lead_portfolio_id: str,
    ) -> LeaderChangeProposal: ...

    def search_external_leaders(
        self,
        *,
        slot: LeaderSlot,
        nickname_query: str,
    ) -> tuple[LeaderCandidateChoice, ...]: ...

    def leader_multiplier_text(self) -> str: ...

    def leader_multiplier_choices(self) -> tuple[LeaderMultiplierChoice, ...]: ...

    def create_follow_multiplier_change(
        self,
        *,
        user_id: int,
        lead_portfolio_id: str,
        multiplier: int,
    ) -> FollowMultiplierProposal: ...

    def execute_follow_multiplier_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None: ...

    def leader_lock_text(self) -> str: ...

    def leader_lock_choices(self) -> tuple[LeaderLockChoice, ...]: ...

    def create_leader_lock_change(
        self,
        *,
        user_id: int,
        lead_portfolio_id: str,
        locked: bool,
    ) -> LeaderLockProposal: ...

    def execute_leader_lock_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None: ...


class TelegramMenuRouter:
    def __init__(
        self,
        *,
        client: TelegramBotClient,
        dashboard: TelegramDashboardProvider,
        challenges: TelegramChallengeStore,
        controls: TelegramControlHandler,
        leader_admin: TelegramLeaderAdmin | None = None,
        position_admin: TelegramPositionAdmin | None = None,
        margin_admin: TelegramMarginAdmin | None = None,
        audit_trigger: Callable[[], bool] | None = None,
    ) -> None:
        self._client = client
        self._dashboard = dashboard
        self._challenges = challenges
        self._controls = controls
        self._leader_admin = leader_admin
        self._position_admin = position_admin
        self._margin_admin = margin_admin
        self._audit_trigger = audit_trigger
        self._notification_token: str | None = None

    def handle(self, update: Mapping[str, Any]) -> None:
        parsed = _parse_update(update)
        if parsed is None:
            return
        update_kind, callback_id, chat_id, user_id, message_id, value = parsed
        self._notification_token = None
        notification_callback = _NOTIFICATION_CALLBACK.fullmatch(value)
        if update_kind == "callback" and notification_callback is not None:
            self._notification_token = notification_callback.group(1)
            value = notification_callback.group(2)
        if chat_id not in self._client.config.allowed_chat_ids:
            if callback_id:
                self._client.answer_callback(callback_id, "未授权会话")
            return
        if update_kind == "message":
            try:
                margin_limit = _message_entry_margin_limit(value)
            except ValueError:
                self._client.send_message(
                    chat_id,
                    "可用保证金额度格式: /margin_limit 60。请输入 5-120 U, 最多两位小数。",
                    reply_markup=contextual_inline_keyboard("funds"),
                )
                return
            if margin_limit is not None:
                if user_id not in self._client.config.authorized_user_ids:
                    self._client.send_message(chat_id, "你没有资金配置权限。")
                    return
                if self._margin_admin is None:
                    self._client.send_message(chat_id, "资金配置功能当前不可用。")
                    return
                try:
                    margin_proposal = self._margin_admin.create_entry_margin_limit_change(
                        user_id=user_id,
                        limit_usdt=margin_limit,
                    )
                except (ValueError, RuntimeError):
                    self._client.send_message(
                        chat_id,
                        "额度未变更或配置已失效, 请刷新资金页面后重试。",
                        reply_markup=contextual_inline_keyboard("funds"),
                    )
                    return
                self._send_entry_margin_confirmation(chat_id, margin_proposal)
                return
            try:
                leader_command = _message_leader_command(value)
            except ValueError:
                self._client.send_message(
                    chat_id,
                    "带单员命令格式: /leader_set custom1 <ID或Binance详情链接>, "
                    "或 /leader_find custom1 名称。",
                )
                return
            if leader_command is not None:
                if user_id not in self._client.config.authorized_user_ids:
                    self._client.send_message(chat_id, "你没有带单员管理权限。")
                    return
                if self._leader_admin is None:
                    self._client.send_message(chat_id, "带单员管理功能当前不可用。")
                    return
                operation, slot, query = leader_command
                try:
                    if operation == "SET":
                        proposal = self._leader_admin.create_external_leader_change(
                            user_id=user_id,
                            slot=slot,
                            lead_portfolio_id=query,
                        )
                        self._send_leader_confirmation(chat_id, proposal)
                        return
                    choices = self._leader_admin.search_external_leaders(
                        slot=slot,
                        nickname_query=query,
                    )
                except (ValueError, RuntimeError) as error:
                    reason_code = _leader_admin_reason_code(error)
                    print(
                        json.dumps(
                            {
                                "event": "telegram_leader_admin_rejected",
                                "operation": operation,
                                "reason_code": reason_code,
                                "slot": slot.value,
                            },
                            separators=(",", ":"),
                        ),
                        flush=True,
                    )
                    self._client.send_message(
                        chat_id,
                        _leader_admin_error_text(reason_code),
                        reply_markup=leader_management_keyboard(),
                    )
                    return
                text = "\n".join(
                    [f"🔎 {leader_slot_label(slot)}名称搜索结果"]
                    + [choice.summary for choice in choices]
                )
                self._client.send_message(
                    chat_id,
                    text if choices else "没有找到符合条件且可在 Testnet 执行的带单员。",
                    reply_markup=leader_candidate_keyboard(slot, choices),
                )
                return
            view = _message_view(value)
            action = _message_action(value)
            if action is not None:
                if user_id not in self._client.config.authorized_user_ids:
                    self._client.send_message(chat_id, "你没有控制权限。")
                    return
                self._request_confirmation(chat_id, user_id, action)
                return
            if value.split(maxsplit=1)[0].lower().startswith(("/start", "/menu")):
                self._client.send_message(
                    chat_id,
                    self._dashboard.render("status"),
                    reply_markup=persistent_reply_keyboard(),
                )
                return
            self._client.send_message(
                chat_id,
                self._dashboard.render(view),
                reply_markup=self._view_markup(view),
            )
            return
        if callback_id is None:
            return
        if value == "return" and self._notification_token is not None:
            notification = self._dashboard.notification_message(self._notification_token)
            if notification is None:
                self._answer_callback(callback_id, "原通知已过期或不存在")
                return
            self._answer_callback(callback_id, "已返回原通知")
            if message_id is not None:
                text, contextual_view = notification
                self._edit_text(
                    chat_id,
                    message_id,
                    text,
                    notification_inline_keyboard(contextual_view, self._notification_token),
                    preserve_notification_context=False,
                )
            return
        if value.startswith("view:"):
            view = value.removeprefix("view:")
            self._answer_callback(callback_id)
            if message_id is None:
                return
            self._edit_or_send(chat_id, message_id, view)
            return
        if value.startswith("pnl:leader:"):
            leader_id = value.removeprefix("pnl:leader:")
            if not re.fullmatch(r"[0-9]{10,24}", leader_id):
                self._answer_callback(callback_id, "带单员参数无效")
                return
            self._answer_callback(callback_id)
            if message_id is not None:
                self._edit_text(
                    chat_id,
                    message_id,
                    self._dashboard.render_leader_pnl(leader_id),
                    leader_pnl_keyboard(leader_id),
                )
            return
        if value == "audit:run":
            if user_id not in self._client.config.authorized_user_ids:
                self._answer_callback(callback_id, "你没有审查权限")
                return
            if self._audit_trigger is None or not self._audit_trigger():
                self._answer_callback(callback_id, "Codex 审查启动失败")
                return
            self._answer_callback(callback_id, "已启动 Codex 系统审查")
            if message_id is not None:
                self._edit_text(
                    chat_id,
                    message_id,
                    (
                        "🤖 Codex 系统审查已启动\n"
                        "正在读取最新服务、数据库、仓位、订单及通知异常。\n"
                        "完成后会自动通知; 也可以稍后点击“查看结果”。"
                    ),
                    contextual_inline_keyboard("codex"),
                )
            return
        if value == "summary_reset:request":
            if user_id not in self._client.config.authorized_user_ids:
                self._answer_callback(callback_id, "你没有账户初始化权限")
                return
            self._answer_callback(callback_id, "请二次确认")
            self._request_confirmation(
                chat_id,
                user_id,
                ControlAction.RESET_ACCOUNT_SUMMARY,
            )
            return
        if (
            value.startswith(
                (
                    "lead:",
                    "lead_confirm:",
                    "lock:",
                    "lock_confirm:",
                    "mult:",
                    "mult_confirm:",
                    "pos:",
                    "pos_confirm:",
                    "pos_leader_confirm:",
                    "margin:",
                    "margin_confirm:",
                )
            )
            and user_id not in self._client.config.authorized_user_ids
        ):
            self._answer_callback(callback_id, "你没有管理权限")
            return
        if value == "margin:manage":
            if self._margin_admin is None:
                self._answer_callback(callback_id, "资金配置功能不可用")
                return
            current = self._margin_admin.entry_margin_limit()
            self._answer_callback(callback_id)
            if message_id is not None:
                self._edit_text(
                    chat_id,
                    message_id,
                    self._dashboard.render("funds"),
                    entry_margin_limit_keyboard(current),
                )
            return
        if value == "margin:custom":
            if self._margin_admin is None:
                self._answer_callback(callback_id, "资金配置功能不可用")
                return
            self._answer_callback(callback_id, "请回复额度")
            self._client.send_message(
                chat_id,
                _ENTRY_MARGIN_INPUT_PROMPT,
                reply_markup={
                    "force_reply": True,
                    "selective": True,
                    "input_field_placeholder": "输入 5-120 U, 最多两位小数",
                },
            )
            return
        if value.startswith("margin:set:"):
            if self._margin_admin is None:
                self._answer_callback(callback_id, "资金配置功能不可用")
                return
            try:
                limit_usdt = _normalize_entry_margin_limit(
                    value.removeprefix("margin:set:")
                )
                margin_proposal = self._margin_admin.create_entry_margin_limit_change(
                    user_id=user_id,
                    limit_usdt=limit_usdt,
                )
            except (ValueError, RuntimeError):
                self._answer_callback(callback_id, "额度未变更或参数已失效")
                return
            self._answer_callback(callback_id, "请二次确认")
            self._send_entry_margin_confirmation(chat_id, margin_proposal)
            return
        if value.startswith("margin_confirm:"):
            if self._margin_admin is None:
                self._answer_callback(callback_id, "资金配置功能不可用")
                return
            result = self._margin_admin.execute_entry_margin_limit_confirmed(
                user_id=user_id,
                nonce=value.removeprefix("margin_confirm:"),
            )
            if result is None:
                self._answer_callback(callback_id, "确认已失效")
                return
            self._answer_callback(callback_id, "已执行")
            if message_id is not None:
                self._edit_text(
                    chat_id,
                    message_id,
                    result,
                    entry_margin_limit_keyboard(self._margin_admin.entry_margin_limit()),
                )
            return
        if value.startswith("pos:close:"):
            if self._position_admin is None:
                self._answer_callback(callback_id, "仓位清理功能不可用")
                return
            parts = value.split(":")
            if (
                len(parts) != 5
                or not re.fullmatch(r"[0-9]{10,24}", parts[2])
                or not re.fullmatch(r"[A-Z0-9]{3,24}", parts[3])
                or parts[4] not in {"L", "S"}
            ):
                self._answer_callback(callback_id, "仓位参数无效")
                return
            try:
                position_proposal = self._position_admin.create_position_close(
                    user_id=user_id,
                    lead_portfolio_id=parts[2],
                    symbol=parts[3],
                    position_side="LONG" if parts[4] == "L" else "SHORT",
                )
            except (ValueError, RuntimeError):
                self._answer_callback(callback_id, "仓位已变化, 请刷新")
                return
            self._answer_callback(callback_id, "请二次确认")
            self._send_position_close_confirmation(
                chat_id,
                position_proposal,
                lead_portfolio_id=parts[2],
            )
            return
        if value.startswith("pos:close_leader:"):
            if self._position_admin is None:
                self._answer_callback(callback_id, "仓位清理功能不可用")
                return
            leader_id = value.removeprefix("pos:close_leader:")
            if not re.fullmatch(r"[0-9]{10,24}", leader_id):
                self._answer_callback(callback_id, "带单员参数无效")
                return
            try:
                position_proposal = self._position_admin.create_leader_positions_close(
                    user_id=user_id,
                    lead_portfolio_id=leader_id,
                )
            except (ValueError, RuntimeError):
                self._answer_callback(callback_id, "仓位已变化, 请刷新")
                return
            self._answer_callback(callback_id, "请二次确认")
            self._send_leader_positions_close_confirmation(
                chat_id,
                position_proposal,
                lead_portfolio_id=leader_id,
            )
            return
        if value.startswith("pos_confirm:"):
            if self._position_admin is None:
                self._answer_callback(callback_id, "仓位清理功能不可用")
                return
            confirmation_parts = value.split(":")
            if len(confirmation_parts) not in {2, 3}:
                self._answer_callback(callback_id, "确认参数无效")
                return
            return_leader_id = confirmation_parts[2] if len(confirmation_parts) == 3 else None
            if return_leader_id is not None and not re.fullmatch(
                r"[0-9]{10,24}",
                return_leader_id,
            ):
                self._answer_callback(callback_id, "确认参数无效")
                return
            result = self._position_admin.execute_position_close_confirmed(
                user_id=user_id,
                nonce=confirmation_parts[1],
            )
            if result is None:
                self._answer_callback(callback_id, "确认已失效")
                return
            self._answer_callback(callback_id, "已提交")
            if message_id is not None:
                return_view = (
                    "positions" if return_leader_id is None else f"positions:{return_leader_id}"
                )
                self._edit_text(
                    chat_id,
                    message_id,
                    result,
                    self._view_markup(return_view),
                )
            return
        if value.startswith("pos_leader_confirm:"):
            if self._position_admin is None:
                self._answer_callback(callback_id, "仓位清理功能不可用")
                return
            confirmation_parts = value.split(":")
            if len(confirmation_parts) != 3 or not re.fullmatch(
                r"[0-9]{10,24}", confirmation_parts[2]
            ):
                self._answer_callback(callback_id, "确认参数无效")
                return
            result = self._position_admin.execute_leader_positions_close_confirmed(
                user_id=user_id,
                nonce=confirmation_parts[1],
            )
            if result is None:
                self._answer_callback(callback_id, "确认已失效")
                return
            self._answer_callback(callback_id, "已提交")
            if message_id is not None:
                leader_id = confirmation_parts[2]
                self._edit_text(
                    chat_id,
                    message_id,
                    result,
                    self._view_markup(f"positions:{leader_id}"),
                )
            return
        if value == "lead:manage":
            self._answer_callback(callback_id)
            if message_id is not None and self._leader_admin is not None:
                self._edit_text(
                    chat_id,
                    message_id,
                    self._leader_admin.leader_management_text(),
                    leader_management_keyboard(),
                )
            return
        if value.startswith("lead:manual:"):
            try:
                slot = leader_slot_from_callback(value.rsplit(":", 1)[-1])
            except ValueError:
                self._answer_callback(callback_id, "未知席位")
                return
            self._answer_callback(callback_id, "请回复链接、ID 或名称")
            self._client.send_message(
                chat_id,
                _leader_input_prompt(slot),
                reply_markup={
                    "force_reply": True,
                    "selective": True,
                    "input_field_placeholder": "粘贴 Binance 链接、ID 或名称",
                },
            )
            return
        if value.startswith("lead:candidates:"):
            if self._leader_admin is None:
                self._answer_callback(callback_id, "管理功能不可用")
                return
            try:
                slot = leader_slot_from_callback(value.rsplit(":", 1)[-1])
            except ValueError:
                self._answer_callback(callback_id, "未知席位")
                return
            candidate_choices = self._leader_admin.leader_candidates(slot=slot)
            self._answer_callback(callback_id)
            if message_id is not None:
                text = "\n".join(
                    [
                        f"{leader_slot_label(slot)}候选",
                        "可点选推荐候选, 也可输入任意公开带单员 ID/名称。",
                    ]
                    + [choice.summary for choice in candidate_choices]
                )
                self._edit_text(
                    chat_id,
                    message_id,
                    text or "暂无候选",
                    leader_candidate_keyboard(slot, candidate_choices),
                )
            return
        if value.startswith("lead:set:"):
            if self._leader_admin is None:
                self._answer_callback(callback_id, "管理功能不可用")
                return
            parts = value.split(":")
            if len(parts) != 4:
                self._answer_callback(callback_id, "候选参数无效")
                return
            try:
                slot = leader_slot_from_callback(parts[2])
                proposal = self._leader_admin.create_leader_change(
                    user_id=user_id,
                    slot=slot,
                    lead_portfolio_id=parts[3],
                )
            except (ValueError, RuntimeError):
                self._answer_callback(callback_id, "候选已失效, 请刷新")
                return
            self._answer_callback(callback_id, "请二次确认")
            self._send_leader_confirmation(chat_id, proposal)
            return
        if value.startswith("lead:remove:"):
            if self._leader_admin is None:
                self._answer_callback(callback_id, "管理功能不可用")
                return
            try:
                slot = leader_slot_from_callback(value.rsplit(":", 1)[-1])
                proposal = self._leader_admin.create_leader_change(
                    user_id=user_id,
                    slot=slot,
                    lead_portfolio_id=None,
                )
            except (ValueError, RuntimeError):
                self._answer_callback(callback_id, "该席位当前不可删除")
                return
            self._answer_callback(callback_id, "请二次确认")
            self._send_leader_confirmation(chat_id, proposal)
            return
        if value.startswith("lead_confirm:"):
            if self._leader_admin is None:
                self._answer_callback(callback_id, "管理功能不可用")
                return
            result = self._leader_admin.execute_leader_change_confirmed(
                user_id=user_id,
                nonce=value.removeprefix("lead_confirm:"),
            )
            if result is None:
                self._answer_callback(callback_id, "确认已失效")
                return
            self._answer_callback(callback_id, "已执行")
            if message_id is not None:
                self._edit_text(
                    chat_id,
                    message_id,
                    result,
                    leader_management_keyboard(),
                )
            return
        if value == "lock:manage":
            if self._leader_admin is None:
                self._answer_callback(callback_id, "管理功能不可用")
                return
            lock_choices = self._leader_admin.leader_lock_choices()
            self._answer_callback(callback_id)
            if message_id is not None:
                self._edit_text(
                    chat_id,
                    message_id,
                    self._leader_admin.leader_lock_text(),
                    leader_lock_keyboard(lock_choices),
                )
            return
        if value.startswith("lock:set:"):
            if self._leader_admin is None:
                self._answer_callback(callback_id, "管理功能不可用")
                return
            parts = value.split(":")
            if (
                len(parts) != 4
                or not re.fullmatch(r"[0-9]{10,24}", parts[2])
                or parts[3] not in {"0", "1"}
            ):
                self._answer_callback(callback_id, "锁定参数无效")
                return
            try:
                lock_proposal = self._leader_admin.create_leader_lock_change(
                    user_id=user_id,
                    lead_portfolio_id=parts[2],
                    locked=parts[3] == "1",
                )
            except (ValueError, RuntimeError):
                self._answer_callback(callback_id, "状态已变化, 请刷新")
                return
            self._answer_callback(callback_id, "请二次确认")
            self._send_leader_lock_confirmation(chat_id, lock_proposal)
            return
        if value.startswith("lock_confirm:"):
            if self._leader_admin is None:
                self._answer_callback(callback_id, "管理功能不可用")
                return
            result = self._leader_admin.execute_leader_lock_confirmed(
                user_id=user_id,
                nonce=value.removeprefix("lock_confirm:"),
            )
            if result is None:
                self._answer_callback(callback_id, "确认已失效")
                return
            self._answer_callback(callback_id, "已执行")
            if message_id is not None:
                self._edit_text(
                    chat_id,
                    message_id,
                    result,
                    leader_lock_keyboard(self._leader_admin.leader_lock_choices()),
                )
            return
        if value == "mult:manage":
            if self._leader_admin is None:
                self._answer_callback(callback_id, "管理功能不可用")
                return
            multiplier_choices = self._leader_admin.leader_multiplier_choices()
            self._answer_callback(callback_id)
            if message_id is not None:
                self._edit_text(
                    chat_id,
                    message_id,
                    self._leader_admin.leader_multiplier_text(),
                    multiplier_management_keyboard(multiplier_choices),
                )
            return
        if value.startswith("mult:leader:"):
            if self._leader_admin is None:
                self._answer_callback(callback_id, "管理功能不可用")
                return
            leader_id = value.removeprefix("mult:leader:")
            multiplier_choices_by_id = {
                choice.lead_portfolio_id: choice
                for choice in self._leader_admin.leader_multiplier_choices()
            }
            choice = multiplier_choices_by_id.get(leader_id)
            if choice is None:
                self._answer_callback(callback_id, "带单员已变更, 请刷新")
                return
            self._answer_callback(callback_id)
            if message_id is not None:
                self._edit_text(
                    chat_id,
                    message_id,
                    (
                        f"📐 配置跟单金额倍数\n{choice.button_label}\n"
                        f"当前: {choice.current_multiplier}倍\n\n"
                        "只影响后续新交易信号; 已有仓位和待入场订单保持原数量。"
                    ),
                    multiplier_value_keyboard(choice),
                )
            return
        if value.startswith("mult:set:"):
            if self._leader_admin is None:
                self._answer_callback(callback_id, "管理功能不可用")
                return
            parts = value.split(":")
            if len(parts) != 4 or not re.fullmatch(r"[0-9]{10,24}", parts[2]):
                self._answer_callback(callback_id, "倍数参数无效")
                return
            try:
                multiplier = int(parts[3])
                multiplier_proposal = self._leader_admin.create_follow_multiplier_change(
                    user_id=user_id,
                    lead_portfolio_id=parts[2],
                    multiplier=multiplier,
                )
            except (ValueError, RuntimeError):
                self._answer_callback(callback_id, "配置已失效, 请刷新")
                return
            self._answer_callback(callback_id, "请二次确认")
            self._send_multiplier_confirmation(chat_id, multiplier_proposal)
            return
        if value.startswith("mult_confirm:"):
            if self._leader_admin is None:
                self._answer_callback(callback_id, "管理功能不可用")
                return
            result = self._leader_admin.execute_follow_multiplier_confirmed(
                user_id=user_id,
                nonce=value.removeprefix("mult_confirm:"),
            )
            if result is None:
                self._answer_callback(callback_id, "确认已失效")
                return
            self._answer_callback(callback_id, "已执行")
            if message_id is not None:
                self._edit_text(
                    chat_id,
                    message_id,
                    result,
                    multiplier_management_keyboard(self._leader_admin.leader_multiplier_choices()),
                )
            return
        if user_id not in self._client.config.authorized_user_ids:
            self._client.answer_callback(callback_id, "你没有控制权限")
            return
        if value.startswith("ctl:"):
            try:
                action = ControlAction(value.removeprefix("ctl:"))
            except ValueError:
                self._client.answer_callback(callback_id, "未知操作")
                return
            self._answer_callback(callback_id, "请二次确认")
            self._request_confirmation(chat_id, user_id, action)
            return
        if value.startswith("cancel:"):
            self._answer_callback(callback_id, "已取消")
            if message_id is not None:
                try:
                    self._client.edit_message(
                        chat_id,
                        message_id,
                        "已取消本次控制操作。",
                        reply_markup=contextual_inline_keyboard("status"),
                    )
                except TelegramMessageNotModified:
                    pass
            return
        if value.startswith("summary_confirm:"):
            nonce = value.removeprefix("summary_confirm:")
            try:
                result = self._controls.execute_confirmed(user_id=user_id, nonce=nonce)
            except RuntimeError:
                self._answer_callback(callback_id, "初始化失败, 请刷新后重试")
                return
            if result is None:
                self._client.answer_callback(callback_id, "确认已失效")
                return
            self._answer_callback(callback_id, "已初始化")
            if message_id is not None:
                self._client.edit_message(
                    chat_id,
                    message_id,
                    f"{result}\n\n{self._dashboard.render('pnl')}",
                    reply_markup=pnl_overview_keyboard(
                        self._dashboard.pnl_leader_choices()
                    ),
                )
            return
        if value.startswith("confirm:"):
            nonce = value.removeprefix("confirm:")
            result = self._controls.execute_confirmed(user_id=user_id, nonce=nonce)
            if result is None:
                self._client.answer_callback(callback_id, "确认已失效")
                return
            self._answer_callback(callback_id, "已执行")
            if message_id is not None:
                self._client.edit_message(
                    chat_id,
                    message_id,
                    result,
                    reply_markup=contextual_inline_keyboard("status"),
                )

    def _answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self._client.answer_callback(callback_id, text)
        except TelegramCallbackExpired:
            pass

    def _edit_or_send(self, chat_id: int, message_id: int, view: str) -> None:
        text = self._dashboard.render(view)
        markup = self._view_markup(view)
        self._edit_text(chat_id, message_id, text, markup)

    def _view_markup(self, view: str) -> dict[str, object]:
        base_view, lead_portfolio_id, page = _view_page(view)
        if base_view == "pnl":
            return pnl_overview_keyboard(self._dashboard.pnl_leader_choices())
        if base_view == "positions" and self._position_admin is not None:
            if lead_portfolio_id is None:
                return position_leader_keyboard(self._position_admin.position_leader_choices())
            choices = self._position_admin.position_close_choices(
                lead_portfolio_id=lead_portfolio_id,
                page=page,
            )
            return position_close_keyboard(
                choices[:8],
                lead_portfolio_id=lead_portfolio_id,
                page=page,
                has_next=len(choices) > 8,
            )
        return contextual_inline_keyboard(base_view)

    def _edit_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        markup: Mapping[str, object],
        *,
        preserve_notification_context: bool = True,
    ) -> None:
        if preserve_notification_context and self._notification_token is not None:
            markup = _notification_context_markup(
                markup,
                self._notification_token,
                include_return=True,
            )
        try:
            self._client.edit_message(
                chat_id,
                message_id,
                text,
                reply_markup=markup,
            )
        except TelegramMessageNotModified:
            return
        except TelegramBotError:
            self._client.send_message(chat_id, text, reply_markup=markup)

    def _send_leader_confirmation(
        self,
        chat_id: int,
        proposal: LeaderChangeProposal,
    ) -> None:
        self._client.send_message(
            chat_id,
            proposal.confirmation_text,
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "✅ 确认变更",
                            "callback_data": f"lead_confirm:{proposal.nonce}",
                        },
                        {"text": "取消", "callback_data": "view:leaders"},
                    ]
                ]
            },
        )

    def _send_multiplier_confirmation(
        self,
        chat_id: int,
        proposal: FollowMultiplierProposal,
    ) -> None:
        self._client.send_message(
            chat_id,
            proposal.confirmation_text,
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "✅ 确认倍数",
                            "callback_data": f"mult_confirm:{proposal.nonce}",
                        },
                        {"text": "取消", "callback_data": "mult:manage"},
                    ]
                ]
            },
        )

    def _send_entry_margin_confirmation(
        self,
        chat_id: int,
        proposal: EntryMarginLimitProposal,
    ) -> None:
        self._client.send_message(
            chat_id,
            proposal.confirmation_text,
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "✅ 确认额度",
                            "callback_data": f"margin_confirm:{proposal.nonce}",
                        },
                        {"text": "取消", "callback_data": "margin:manage"},
                    ]
                ]
            },
        )

    def _send_leader_lock_confirmation(
        self,
        chat_id: int,
        proposal: LeaderLockProposal,
    ) -> None:
        self._client.send_message(
            chat_id,
            proposal.confirmation_text,
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "✅ 确认",
                            "callback_data": f"lock_confirm:{proposal.nonce}",
                        },
                        {"text": "取消", "callback_data": "lock:manage"},
                    ]
                ]
            },
        )

    def _send_position_close_confirmation(
        self,
        chat_id: int,
        proposal: PositionCloseProposal,
        *,
        lead_portfolio_id: str,
    ) -> None:
        if not re.fullmatch(r"[0-9]{10,24}", lead_portfolio_id):
            raise ValueError("Telegram position leader ID is invalid")
        self._client.send_message(
            chat_id,
            proposal.confirmation_text,
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "✅ 确认清除此仓",
                            "callback_data": (f"pos_confirm:{proposal.nonce}:{lead_portfolio_id}"),
                        },
                        {
                            "text": "取消",
                            "callback_data": f"view:positions:{lead_portfolio_id}",
                        },
                    ]
                ]
            },
        )

    def _send_leader_positions_close_confirmation(
        self,
        chat_id: int,
        proposal: PositionCloseProposal,
        *,
        lead_portfolio_id: str,
    ) -> None:
        if not re.fullmatch(r"[0-9]{10,24}", lead_portfolio_id):
            raise ValueError("Telegram position leader ID is invalid")
        self._client.send_message(
            chat_id,
            proposal.confirmation_text,
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "✅ 确认清空该带单员",
                            "callback_data": (
                                f"pos_leader_confirm:{proposal.nonce}:{lead_portfolio_id}"
                            ),
                        },
                        {
                            "text": "取消",
                            "callback_data": f"view:positions:{lead_portfolio_id}",
                        },
                    ]
                ]
            },
        )

    def _request_confirmation(
        self,
        chat_id: int,
        user_id: int,
        action: ControlAction,
    ) -> None:
        nonce = self._challenges.create(user_id=user_id, action=action)
        reset_summary = action is ControlAction.RESET_ACCOUNT_SUMMARY
        self._client.send_message(
            chat_id,
            (
                "⚠️ 确认初始化账户汇总\n"
                "当前净值将重新以 150 U 为起点; 今日、本月、累计、各条线、"
                "各带单员和各仓位盈亏将从现在归零。\n"
                "可用开仓保证金余额会按共享上限扣除已有仓位和待入场订单的占用后重算; "
                "不会删除或平掉仓位、撤销订单、修改带单员和额度配置。\n"
                "确认按钮 2 分钟内有效。"
                if reset_summary
                else f"⚠️ 确认执行: {_action_label(action)}\n确认按钮 2 分钟内有效。"
            ),
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "✅ 确认初始化" if reset_summary else "✅ 确认执行",
                            "callback_data": (
                                f"summary_confirm:{nonce}"
                                if reset_summary
                                else f"confirm:{nonce}"
                            ),
                        },
                        {
                            "text": "取消",
                            "callback_data": "view:funds" if reset_summary else f"cancel:{nonce}",
                        },
                    ]
                ]
            },
        )


def persistent_reply_keyboard() -> dict[str, object]:
    """Navigation keyboard that clients may collapse behind the input-field icon."""

    return {
        "keyboard": [
            [{"text": "📊 总览"}, {"text": "📈 仓位"}],
            [{"text": "👥 带单员"}, {"text": "🧾 订单"}],
            [{"text": "💹 盈亏"}, {"text": "⚙️ 控制"}],
        ],
        "resize_keyboard": True,
        "is_persistent": False,
        "one_time_keyboard": False,
        "input_field_placeholder": "点输入框旁的键盘图标切换导航",
    }


def contextual_inline_keyboard(view: str) -> dict[str, object]:
    buttons: dict[str, list[list[dict[str, str]]]] = {
        "status": [
            [
                {"text": "🔄 刷新总览", "callback_data": "view:status"},
                {"text": "🤖 立即审查", "callback_data": "audit:run"},
            ],
            [
                {"text": "🩺 巡检报告", "callback_data": "view:health"},
                {"text": "⚙️ 控制中心", "callback_data": "view:control"},
            ],
        ],
        "leaders": [
            [
                {"text": "🔄 刷新带单员", "callback_data": "view:leaders"},
                {"text": "🤖 选人依据", "callback_data": "view:selection"},
            ],
            [{"text": "🛠 管理带单员", "callback_data": "lead:manage"}],
        ],
        "positions": [
            [
                {"text": "🔄 刷新仓位", "callback_data": "view:positions"},
                {"text": "⏳ 待入场", "callback_data": "view:pending"},
            ],
            [
                {"text": "🧾 最近订单", "callback_data": "view:orders"},
                {"text": "💹 系统盈亏", "callback_data": "view:pnl"},
            ],
        ],
        "pending": [
            [
                {"text": "🔄 刷新待入场", "callback_data": "view:pending"},
                {"text": "📈 当前仓位", "callback_data": "view:positions"},
            ],
            [{"text": "🧾 最近订单", "callback_data": "view:orders"}],
        ],
        "orders": [
            [
                {"text": "🔄 刷新订单", "callback_data": "view:orders"},
                {"text": "📈 当前仓位", "callback_data": "view:positions"},
            ],
            [{"text": "⏳ 待入场", "callback_data": "view:pending"}],
        ],
        "funds": [
            [
                {"text": "🔄 刷新资金", "callback_data": "view:funds"},
                {"text": "💹 系统盈亏", "callback_data": "view:pnl"},
            ],
            [
                {"text": "⚙️ 配置可用保证金", "callback_data": "margin:manage"},
                {"text": "♻️ 初始化账户汇总", "callback_data": "summary_reset:request"},
            ],
        ],
        "pnl": [
            [
                {"text": "🔄 刷新盈亏", "callback_data": "view:pnl"},
                {"text": "📈 当前仓位", "callback_data": "view:positions"},
            ],
            [{"text": "💰 资金边界", "callback_data": "view:funds"}],
        ],
        "codex": [
            [
                {"text": "🚀 立即审查", "callback_data": "audit:run"},
                {"text": "🔄 刷新审查结果", "callback_data": "view:codex"},
            ],
            [
                {"text": "🛠 修复记录", "callback_data": "view:repair"},
                {"text": "👥 选人依据", "callback_data": "view:selection"},
            ],
        ],
        "repair": [
            [
                {"text": "🔄 刷新修复结果", "callback_data": "view:repair"},
                {"text": "🤖 查看系统审查", "callback_data": "view:codex"},
            ],
            [{"text": "📊 系统状态", "callback_data": "view:status"}],
        ],
        "health": [
            [
                {"text": "🔄 刷新巡检报告", "callback_data": "view:health"},
                {"text": "🤖 Codex 审查", "callback_data": "view:codex"},
            ],
            [{"text": "📊 系统总览", "callback_data": "view:status"}],
        ],
        "selection": [
            [
                {"text": "🔄 刷新选人依据", "callback_data": "view:selection"},
                {"text": "👥 当前带单员", "callback_data": "view:leaders"},
            ],
            [{"text": "🤖 系统审查", "callback_data": "view:codex"}],
        ],
        "control": [
            [
                {"text": "⏸ 暂停新开仓", "callback_data": "ctl:pause"},
                {"text": "▶️ 恢复新开仓", "callback_data": "ctl:resume"},
            ],
            [{"text": "🧯 全部减仓", "callback_data": "ctl:reduce_all"}],
            [{"text": "↩️ 返回状态", "callback_data": "view:status"}],
        ],
        "help": [[{"text": "📊 查看状态", "callback_data": "view:status"}]],
    }
    return {
        "inline_keyboard": [
            *buttons.get(view, buttons["help"]),
        ]
    }


def notification_inline_keyboard(view: str, message_id: str) -> dict[str, object]:
    """Bind a notification keyboard to its durable outbox message."""

    token = message_id[:16]
    if not _NOTIFICATION_TOKEN.fullmatch(token):
        raise ValueError("Telegram notification message ID is invalid")
    return _notification_context_markup(
        contextual_inline_keyboard(view),
        token,
        include_return=False,
    )


def _notification_context_markup(
    markup: Mapping[str, object],
    token: str,
    *,
    include_return: bool,
) -> dict[str, object]:
    if not _NOTIFICATION_TOKEN.fullmatch(token):
        raise ValueError("Telegram notification token is invalid")
    raw_rows = markup.get("inline_keyboard")
    rows: list[list[dict[str, str]]] = []
    if isinstance(raw_rows, list):
        for raw_row in raw_rows:
            if not isinstance(raw_row, list):
                continue
            row: list[dict[str, str]] = []
            for raw_button in raw_row:
                if not isinstance(raw_button, Mapping):
                    continue
                button = {
                    key: value
                    for key, value in raw_button.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
                callback_data = button.get("callback_data")
                if callback_data is not None and not _NOTIFICATION_CALLBACK.fullmatch(
                    callback_data
                ):
                    wrapped = f"n:{token}:{callback_data}"
                    if len(wrapped.encode("utf-8")) <= _TELEGRAM_CALLBACK_DATA_LIMIT:
                        button["callback_data"] = wrapped
                if button:
                    row.append(button)
            if row:
                rows.append(row)
    if include_return:
        rows.append(
            [
                {
                    "text": "↩️ 返回查看原通知",
                    "callback_data": f"n:{token}:return",
                }
            ]
        )
    return {"inline_keyboard": rows}


def pnl_overview_keyboard(
    choices: tuple[LeaderPnlChoice, ...],
) -> dict[str, object]:
    leader_rows = [
        [
            {
                "text": choice.button_label[:40],
                "callback_data": f"pnl:leader:{choice.lead_portfolio_id}",
            }
        ]
        for choice in choices[:8]
    ]
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 刷新总盈亏", "callback_data": "view:pnl"},
                {"text": "📈 当前仓位", "callback_data": "view:positions"},
            ],
            *leader_rows,
            [{"text": "♻️ 初始化账户汇总", "callback_data": "summary_reset:request"}],
            [{"text": "💰 资金边界", "callback_data": "view:funds"}],
        ]
    }


def position_leader_keyboard(
    choices: tuple[PositionLeaderChoice, ...],
) -> dict[str, object]:
    leader_rows = [
        [
            {
                "text": choice.button_label[:40],
                "callback_data": f"view:positions:{choice.lead_portfolio_id}",
            }
        ]
        for choice in choices[:12]
    ]
    return {
        "inline_keyboard": [
            [
                {"text": "🔄 刷新全部仓位", "callback_data": "view:positions"},
                {"text": "⏳ 待入场", "callback_data": "view:pending"},
            ],
            *leader_rows,
            [{"text": "🧾 最近订单", "callback_data": "view:orders"}],
        ]
    }


def position_close_keyboard(
    choices: tuple[PositionCloseChoice, ...],
    *,
    lead_portfolio_id: str,
    page: int = 1,
    has_next: bool = False,
) -> dict[str, object]:
    if not re.fullmatch(r"[0-9]{10,24}", lead_portfolio_id) or not 1 <= page <= 99:
        raise ValueError("Telegram position page is invalid")
    position_rows = [
        [
            {
                "text": f"🧯 {choice.button_label}"[:40],
                "callback_data": (
                    f"pos:close:{choice.lead_portfolio_id}:{choice.symbol}:"
                    f"{'L' if choice.position_side == 'LONG' else 'S'}"
                ),
            }
        ]
        for choice in choices[:8]
    ]
    navigation: list[dict[str, str]] = []
    if page > 1:
        navigation.append(
            {
                "text": "⬅️ 上一页",
                "callback_data": f"view:positions:{lead_portfolio_id}:{page - 1}",
            }
        )
    if has_next:
        navigation.append(
            {
                "text": "下一页 ➡️",
                "callback_data": f"view:positions:{lead_portfolio_id}:{page + 1}",
            }
        )
    rows: list[list[dict[str, str]]] = [
        [
            {
                "text": "🔄 刷新此带单员",
                "callback_data": f"view:positions:{lead_portfolio_id}:{page}",
            },
            {"text": "↩️ 全部带单员", "callback_data": "view:positions"},
        ],
        *position_rows,
    ]
    if choices:
        rows.append(
            [
                {
                    "text": "🧹 清空该带单员全部仓位",
                    "callback_data": f"pos:close_leader:{lead_portfolio_id}",
                }
            ]
        )
    if navigation:
        rows.append(navigation)
    rows.append(
        [
            {"text": "⏳ 待入场", "callback_data": "view:pending"},
            {"text": "🧾 最近订单", "callback_data": "view:orders"},
        ]
    )
    return {"inline_keyboard": rows}


def _view_page(view: str) -> tuple[str, str | None, int]:
    match = re.fullmatch(
        r"positions(?::([0-9]{10,24}))?(?::([1-9][0-9]?))?",
        view,
    )
    if match is None:
        return view, None, 1
    return "positions", match.group(1), int(match.group(2) or "1")


def leader_pnl_keyboard(lead_portfolio_id: str) -> dict[str, object]:
    if not re.fullmatch(r"[0-9]{10,24}", lead_portfolio_id):
        raise ValueError("Telegram PnL leader ID is invalid")
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🔄 刷新此带单员",
                    "callback_data": f"pnl:leader:{lead_portfolio_id}",
                },
                {"text": "↩️ 总盈亏", "callback_data": "view:pnl"},
            ],
            [{"text": "📈 当前仓位", "callback_data": "view:positions"}],
        ]
    }


def leader_management_keyboard() -> dict[str, object]:
    return {
        "inline_keyboard": [
            [{"text": "🔒 长线", "callback_data": "lead:candidates:long"}],
            [
                {"text": "⚡ 短线 1", "callback_data": "lead:candidates:short1"},
                {"text": "⚡ 短线 2", "callback_data": "lead:candidates:short2"},
            ],
            [
                {"text": "🎯 自定义 1", "callback_data": "lead:manual:custom1"},
                {"text": "🎯 自定义 2", "callback_data": "lead:manual:custom2"},
            ],
            [
                {"text": "✖ 删长线", "callback_data": "lead:remove:long"},
                {"text": "✖ 删短1", "callback_data": "lead:remove:short1"},
                {"text": "✖ 删短2", "callback_data": "lead:remove:short2"},
            ],
            [
                {"text": "✖ 删自定义1", "callback_data": "lead:remove:custom1"},
                {"text": "✖ 删自定义2", "callback_data": "lead:remove:custom2"},
            ],
            [{"text": "📐 配置跟单倍数", "callback_data": "mult:manage"}],
            [{"text": "🔐 锁定带单员", "callback_data": "lock:manage"}],
            [{"text": "↩️ 返回带单员", "callback_data": "view:leaders"}],
        ]
    }


def leader_candidate_keyboard(
    slot: LeaderSlot,
    choices: tuple[LeaderCandidateChoice, ...],
) -> dict[str, object]:
    slot_value = leader_slot_callback(slot)
    rows = [
        [
            {
                "text": choice.button_label[:32],
                "callback_data": (f"lead:set:{slot_value}:{choice.lead_portfolio_id}"),
            }
        ]
        for choice in choices[:6]
    ]
    rows.append(
        [
            {
                "text": "✍️ 输入 ID 或名称",
                "callback_data": f"lead:manual:{slot_value}",
            }
        ]
    )
    rows.append([{"text": "↩️ 返回管理", "callback_data": "lead:manage"}])
    return {"inline_keyboard": rows}


def multiplier_management_keyboard(
    choices: tuple[LeaderMultiplierChoice, ...],
) -> dict[str, object]:
    rows = [
        [
            {
                "text": f"{choice.button_label[:27]} · {choice.current_multiplier}倍",
                "callback_data": f"mult:leader:{choice.lead_portfolio_id}",
            }
        ]
        for choice in choices[:10]
    ]
    rows.extend(
        [
            [{"text": "🔄 刷新倍数", "callback_data": "mult:manage"}],
            [{"text": "↩️ 返回管理", "callback_data": "lead:manage"}],
        ]
    )
    return {"inline_keyboard": rows}


def leader_lock_keyboard(
    choices: tuple[LeaderLockChoice, ...],
) -> dict[str, object]:
    rows = [
        [
            {
                "text": (
                    f"{'🔒' if choice.locked else '🔓'} {choice.button_label[:25]} · "
                    f"{'解锁' if choice.locked else '锁定'}"
                ),
                "callback_data": (
                    f"lock:set:{choice.lead_portfolio_id}:{'0' if choice.locked else '1'}"
                ),
            }
        ]
        for choice in choices[:10]
    ]
    rows.extend(
        [
            [{"text": "🔄 刷新锁定状态", "callback_data": "lock:manage"}],
            [{"text": "↩️ 返回管理", "callback_data": "lead:manage"}],
        ]
    )
    return {"inline_keyboard": rows}


def multiplier_value_keyboard(choice: LeaderMultiplierChoice) -> dict[str, object]:
    if not re.fullmatch(r"[0-9]{10,24}", choice.lead_portfolio_id):
        raise ValueError("Telegram multiplier leader ID is invalid")
    buttons = [
        {
            "text": f"{'✅ ' if value == choice.current_multiplier else ''}{value}倍",
            "callback_data": f"mult:set:{choice.lead_portfolio_id}:{value}",
        }
        for value in range(1, 11)
    ]
    return {
        "inline_keyboard": [
            buttons[0:5],
            buttons[5:10],
            [{"text": "↩️ 返回倍数", "callback_data": "mult:manage"}],
        ]
    }


def entry_margin_limit_keyboard(current: Decimal) -> dict[str, object]:
    current = _normalize_entry_margin_limit(str(current))
    buttons = [
        {
            "text": f"{'✅ ' if current == value else ''}{value} U",
            "callback_data": f"margin:set:{value}",
        }
        for value in (Decimal("30"), Decimal("60"), Decimal("90"), Decimal("120"))
    ]
    return {
        "inline_keyboard": [
            buttons[:2],
            buttons[2:],
            [{"text": "✍️ 自定义额度", "callback_data": "margin:custom"}],
            [{"text": "🔄 刷新资金", "callback_data": "margin:manage"}],
            [{"text": "↩️ 返回资金页", "callback_data": "view:funds"}],
        ]
    }


def _parse_update(
    update: Mapping[str, Any],
) -> tuple[str, str | None, int, int, int | None, str] | None:
    message = update.get("message")
    if isinstance(message, dict):
        chat = message.get("chat")
        sender = message.get("from")
        text = message.get("text")
        if (
            isinstance(chat, dict)
            and isinstance(sender, dict)
            and isinstance(chat.get("id"), int)
            and isinstance(sender.get("id"), int)
            and isinstance(text, str)
        ):
            reply = message.get("reply_to_message")
            reply_text = reply.get("text") if isinstance(reply, dict) else None
            slot = _leader_input_reply_slot(reply_text) if isinstance(reply_text, str) else None
            if slot is not None:
                value = f"/leader_input {leader_slot_callback(slot)} {text[:256]}"
            elif isinstance(reply_text, str) and _entry_margin_input_reply(reply_text):
                value = f"/margin_input {text[:32]}"
            else:
                value = text[:128]
            return "message", None, int(chat["id"]), int(sender["id"]), None, value
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        sender = callback.get("from")
        message = callback.get("message")
        chat = message.get("chat") if isinstance(message, dict) else None
        callback_message_id = message.get("message_id") if isinstance(message, dict) else None
        if (
            isinstance(sender, dict)
            and isinstance(chat, dict)
            and isinstance(sender.get("id"), int)
            and isinstance(chat.get("id"), int)
            and isinstance(callback.get("id"), str)
            and isinstance(callback.get("data"), str)
        ):
            return (
                "callback",
                str(callback["id"]),
                int(chat["id"]),
                int(sender["id"]),
                int(callback_message_id) if isinstance(callback_message_id, int) else None,
                str(callback["data"])[:64],
            )
    return None


def _message_view(text: str) -> str:
    command = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()
    return {
        "/start": "status",
        "/menu": "status",
        "/status": "status",
        "/leaders": "leaders",
        "/positions": "positions",
        "/pending": "pending",
        "/orders": "orders",
        "/pnl": "pnl",
        "/funds": "funds",
        "/codex": "codex",
        "/control": "control",
        "📊": "status",
        "📈": "positions",
        "👥": "leaders",
        "🧾": "orders",
        "💹": "pnl",
        "💰": "funds",
        "⚙️": "control",
    }.get(command, "help")


def _message_action(text: str) -> ControlAction | None:
    command = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()
    return {
        "/pause": ControlAction.PAUSE_NEW_ENTRIES,
        "/resume": ControlAction.RESUME_TESTNET,
        "/reduce_all": ControlAction.REDUCE_ALL,
    }.get(command)


def _message_entry_margin_limit(text: str) -> Decimal | None:
    parts = text.strip().split()
    if not parts:
        return None
    command = parts[0].split("@", maxsplit=1)[0].lower()
    if command not in {"/margin_limit", "/margin_input"}:
        return None
    if len(parts) != 2:
        raise ValueError("Telegram entry margin command is invalid")
    return _normalize_entry_margin_limit(parts[1])


def _normalize_entry_margin_limit(raw: str) -> Decimal:
    if len(raw) > 16 or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", raw):
        raise ValueError("Telegram entry margin limit is invalid")
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError("Telegram entry margin limit is invalid") from error
    if not value.is_finite() or not Decimal("5") <= value <= Decimal("120"):
        raise ValueError("Telegram entry margin limit is outside bounds")
    return value.quantize(Decimal("0.01"))


def _message_leader_command(text: str) -> tuple[str, LeaderSlot, str] | None:
    parts = text.strip().split(maxsplit=2)
    if not parts:
        return None
    command = parts[0].split("@", maxsplit=1)[0].lower()
    if command not in {"/leader_input", "/leader_set", "/leader_find"}:
        return None
    if len(parts) != 3:
        raise ValueError("Telegram leader command is invalid")
    slot = leader_slot_from_callback(parts[1].lower())
    query = parts[2].strip()
    if not query or len(query) > 256 or "\n" in query or "\r" in query:
        raise ValueError("Telegram leader query is invalid")
    leader_id = _binance_leader_reference_id(query)
    if command == "/leader_set" or (
        command == "/leader_input" and leader_id is not None
    ):
        if leader_id is None:
            raise ValueError("Telegram leader portfolio ID is invalid")
        return "SET", slot, leader_id
    if query.lower().startswith(("http://", "https://")):
        raise ValueError("Telegram Binance leader URL is invalid")
    return "FIND", slot, query


def _binance_leader_reference_id(value: str) -> str | None:
    if re.fullmatch(r"[0-9]{10,24}", value):
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or parsed.netloc.lower() != "www.binance.com"
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    matched = _BINANCE_LEADER_PATH.fullmatch(parsed.path)
    return matched.group(1) if matched is not None else None


def _leader_admin_reason_code(error: ValueError | RuntimeError) -> str:
    reason = str(error)
    if _INTERNAL_REASON_CODE.fullmatch(reason):
        return reason
    return f"UNEXPECTED_{type(error).__name__.upper()}"


def _leader_admin_error_text(reason_code: str) -> str:
    if reason_code == "COPY_LEADER_LOOKUP_NOT_FOUND":
        return "没有在 Binance 公开带单员目录中找到这个 ID。如果详情页刚刚恢复公开, 请稍后重试。"
    if reason_code in {
        "COPY_MANUAL_LEADER_ONE_WAY_EVIDENCE_UNRESOLVED",
        "COPY_MANUAL_LEADER_POSITION_SIDE_AMBIGUOUS",
    }:
        return "已读到该带单员, 但公开操作的多空方向证据不完整, 系统为避免下反单已拒绝添加。"
    if reason_code.endswith(("_ACCESS_DENIED", "_RETRY_EXHAUSTED")) or reason_code == (
        "COPY_PUBLIC_TRANSPORT_FAILED"
    ):
        return "Binance 公开数据暂时不可用, 系统已记录具体原因, 请稍后重试。"
    return f"带单员校验未通过: {reason_code_text(reason_code)}。请核对 ID/名称或稍后重试。"


def _leader_input_prompt(slot: LeaderSlot) -> str:
    return (
        f"✍️ 输入{leader_slot_label(slot)}带单员\n"
        "请直接回复 Binance 带单员详情链接、带单员 ID, "
        "或回复完整/部分名称进行搜索。\n"
        "系统会实时读取公开资料和最近操作, 之后仍需二次确认。"
    )


_ENTRY_MARGIN_INPUT_PROMPT = (
    "✍️ 输入共享可用保证金额度\n"
    "请直接回复 5-120 之间的 U 数值, 最多两位小数。\n"
    "确认后只限制后续新开仓, 不会调整或强平已有仓位。"
)


def _entry_margin_input_reply(reply_text: str) -> bool:
    return (
        bool(reply_text)
        and reply_text.splitlines()[0] == _ENTRY_MARGIN_INPUT_PROMPT.splitlines()[0]
    )


def _leader_input_reply_slot(reply_text: str) -> LeaderSlot | None:
    first_line = reply_text.splitlines()[0] if reply_text else ""
    return {
        f"✍️ 输入{leader_slot_label(LeaderSlot.LONG_TERM)}带单员": LeaderSlot.LONG_TERM,
        f"✍️ 输入{leader_slot_label(LeaderSlot.SHORT_TERM_1)}带单员": LeaderSlot.SHORT_TERM_1,
        f"✍️ 输入{leader_slot_label(LeaderSlot.SHORT_TERM_2)}带单员": LeaderSlot.SHORT_TERM_2,
        f"✍️ 输入{leader_slot_label(LeaderSlot.CUSTOM_1)}带单员": LeaderSlot.CUSTOM_1,
        f"✍️ 输入{leader_slot_label(LeaderSlot.CUSTOM_2)}带单员": LeaderSlot.CUSTOM_2,
    }.get(first_line)


def _action_label(action: ControlAction) -> str:
    return {
        ControlAction.PAUSE_NEW_ENTRIES: "暂停所有新开仓 (减仓仍允许)",
        ControlAction.RESUME_TESTNET: "恢复新开仓",
        ControlAction.REDUCE_ALL: "按虚拟账本全部减仓",
        ControlAction.RESET_ACCOUNT_SUMMARY: "初始化账户汇总",
    }[action]


def _identifier_file(path: Path, label: str) -> tuple[int, ...]:
    lines = tuple(line.strip() for line in path.read_text(encoding="ascii").splitlines())
    values = tuple(dict.fromkeys(line for line in lines if line))
    if not values or any(not _IDENTIFIER.fullmatch(value) for value in values):
        raise ValueError(f"Telegram {label} ID file is invalid")
    return tuple(int(value) for value in values)
