from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ai_quant.copy_trading.leader_slots import LeaderSlot
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
    TelegramBotClient,
    TelegramBotFileConfig,
    TelegramHttpResult,
    TelegramMenuRouter,
    bounded_telegram_text,
    contextual_inline_keyboard,
    entry_margin_limit_keyboard,
    leader_lock_keyboard,
    leader_management_keyboard,
    multiplier_management_keyboard,
    multiplier_value_keyboard,
    notification_inline_keyboard,
    persistent_reply_keyboard,
    pnl_overview_keyboard,
    position_close_keyboard,
    position_leader_keyboard,
)


def test_oversized_dynamic_message_is_bounded_instead_of_crashing_bot(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del path, timeout
        calls.append(document)
        return TelegramHttpResult(
            200,
            json.dumps({"ok": True, "result": {"message_id": 10}}).encode(),
        )

    client = TelegramBotClient(_config(tmp_path), transport=transport)
    client.send_message(42, "x" * 5000)

    sent = str(calls[0]["text"])
    assert len(sent) <= 4000
    assert sent.endswith("⚠️ 内容过长, 后续记录已省略。")
    assert bounded_telegram_text("short") == "short"


class Dashboard:
    def render(self, view: str) -> str:
        return f"dashboard:{view}"

    def pnl_leader_choices(self) -> tuple[LeaderPnlChoice, ...]:
        return (
            LeaderPnlChoice("5117780547953263617", "🔒 长线 · long leader"),
            LeaderPnlChoice("5118776604240532481", "⚡ 短线 1 · short leader"),
        )

    def render_leader_pnl(self, lead_portfolio_id: str) -> str:
        return f"dashboard:pnl:{lead_portfolio_id}"

    def notification_message(self, token: str) -> tuple[str, str] | None:
        if token == "a" * 16:
            return "original notification", "health"
        return None


class Challenges:
    def __init__(self) -> None:
        self.created: list[tuple[int, ControlAction]] = []

    def create(self, *, user_id: int, action: ControlAction) -> str:
        self.created.append((user_id, action))
        return "nonce123456"

    def consume(self, *, user_id: int, nonce: str) -> ControlAction | None:
        if user_id == 42 and nonce == "nonce123456":
            return ControlAction.PAUSE_NEW_ENTRIES
        return None


class Controls:
    def __init__(self) -> None:
        self.executed: list[tuple[int, ControlAction]] = []

    def execute_confirmed(self, *, user_id: int, nonce: str) -> str | None:
        if user_id != 42 or nonce != "nonce123456":
            return None
        action = ControlAction.PAUSE_NEW_ENTRIES
        self.executed.append((user_id, action))
        return "control complete"


class LeaderAdmin:
    def __init__(self) -> None:
        self.proposed: list[tuple[int, LeaderSlot, str | None]] = []
        self.executed: list[tuple[int, str]] = []
        self.external: list[tuple[int, LeaderSlot, str]] = []
        self.searches: list[tuple[LeaderSlot, str]] = []
        self.multiplier_proposed: list[tuple[int, str, int]] = []
        self.multiplier_executed: list[tuple[int, str]] = []
        self.lock_proposed: list[tuple[int, str, bool]] = []
        self.lock_executed: list[tuple[int, str]] = []

    def leader_management_text(self) -> str:
        return "leader management"

    def leader_candidates(self, *, slot: LeaderSlot) -> tuple[LeaderCandidateChoice, ...]:
        assert slot is LeaderSlot.SHORT_TERM_1
        return (
            LeaderCandidateChoice(
                lead_portfolio_id="5109186975387420161",
                button_label="active leader",
                summary="candidate summary",
            ),
        )

    def create_leader_change(
        self,
        *,
        user_id: int,
        slot: LeaderSlot,
        lead_portfolio_id: str | None,
    ) -> LeaderChangeProposal:
        self.proposed.append((user_id, slot, lead_portfolio_id))
        return LeaderChangeProposal("leadernonce123", "confirm leader")

    def execute_leader_change_confirmed(self, *, user_id: int, nonce: str) -> str | None:
        self.executed.append((user_id, nonce))
        return "leader changed" if nonce == "leadernonce123" else None

    def create_external_leader_change(
        self,
        *,
        user_id: int,
        slot: LeaderSlot,
        lead_portfolio_id: str,
    ) -> LeaderChangeProposal:
        self.external.append((user_id, slot, lead_portfolio_id))
        return LeaderChangeProposal("externalnonce1", "confirm external leader")

    def search_external_leaders(
        self,
        *,
        slot: LeaderSlot,
        nickname_query: str,
    ) -> tuple[LeaderCandidateChoice, ...]:
        self.searches.append((slot, nickname_query))
        return (
            LeaderCandidateChoice(
                lead_portfolio_id="5014426348046646785",
                button_label="matched leader",
                summary="matched candidate summary",
            ),
        )

    def leader_multiplier_text(self) -> str:
        return "multiplier management"

    def leader_multiplier_choices(self) -> tuple[LeaderMultiplierChoice, ...]:
        return (
            LeaderMultiplierChoice(
                "5109186975387420161",
                "⚡ 短线 1 · active leader",
                2,
            ),
        )

    def create_follow_multiplier_change(
        self,
        *,
        user_id: int,
        lead_portfolio_id: str,
        multiplier: int,
    ) -> FollowMultiplierProposal:
        self.multiplier_proposed.append((user_id, lead_portfolio_id, multiplier))
        return FollowMultiplierProposal("multipliernonce", "confirm multiplier")

    def execute_follow_multiplier_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None:
        self.multiplier_executed.append((user_id, nonce))
        return "multiplier changed" if nonce == "multipliernonce" else None

    def leader_lock_text(self) -> str:
        return "leader lock management"

    def leader_lock_choices(self) -> tuple[LeaderLockChoice, ...]:
        return (
            LeaderLockChoice(
                "5109186975387420161",
                "⚡ 短线 1 · active leader",
                False,
            ),
        )

    def create_leader_lock_change(
        self,
        *,
        user_id: int,
        lead_portfolio_id: str,
        locked: bool,
    ) -> LeaderLockProposal:
        self.lock_proposed.append((user_id, lead_portfolio_id, locked))
        return LeaderLockProposal("locknonce123", "confirm leader lock")

    def execute_leader_lock_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None:
        self.lock_executed.append((user_id, nonce))
        return "leader locked" if nonce == "locknonce123" else None


class PositionAdmin:
    def __init__(self) -> None:
        self.proposed: list[tuple[int, str, str, str]] = []
        self.executed: list[tuple[int, str]] = []
        self.leader_proposed: list[tuple[int, str]] = []
        self.leader_executed: list[tuple[int, str]] = []

    def position_leader_choices(self) -> tuple[PositionLeaderChoice, ...]:
        return (
            PositionLeaderChoice(
                lead_portfolio_id="5117780547953263617",
                button_label="🔒 长线 · long leader · 1 仓",
                open_position_count=1,
            ),
        )

    def position_close_choices(
        self,
        *,
        lead_portfolio_id: str | None = None,
        page: int = 1,
    ) -> tuple[PositionCloseChoice, ...]:
        assert page >= 1
        if lead_portfolio_id is not None:
            assert lead_portfolio_id == "5117780547953263617"
        return (
            PositionCloseChoice(
                lead_portfolio_id="5117780547953263617",
                symbol="DOGEUSDT",
                position_side="LONG",
                button_label="DOGEUSDT · 多 · long leader",
            ),
        )

    def create_position_close(
        self,
        *,
        user_id: int,
        lead_portfolio_id: str,
        symbol: str,
        position_side: str,
    ) -> PositionCloseProposal:
        self.proposed.append((user_id, lead_portfolio_id, symbol, position_side))
        return PositionCloseProposal("positionnonce", "confirm one position")

    def execute_position_close_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None:
        self.executed.append((user_id, nonce))
        return "position close submitted" if nonce == "positionnonce" else None

    def create_leader_positions_close(
        self,
        *,
        user_id: int,
        lead_portfolio_id: str,
    ) -> PositionCloseProposal:
        self.leader_proposed.append((user_id, lead_portfolio_id))
        return PositionCloseProposal("leaderpositionnonce", "confirm leader positions")

    def execute_leader_positions_close_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None:
        self.leader_executed.append((user_id, nonce))
        return "leader positions close submitted" if nonce == "leaderpositionnonce" else None


class MarginAdmin:
    def __init__(self) -> None:
        self.current = Decimal("120.00")
        self.proposed: list[tuple[int, Decimal]] = []
        self.executed: list[tuple[int, str]] = []

    def entry_margin_limit(self) -> Decimal:
        return self.current

    def create_entry_margin_limit_change(
        self,
        *,
        user_id: int,
        limit_usdt: Decimal,
    ) -> EntryMarginLimitProposal:
        self.proposed.append((user_id, limit_usdt))
        return EntryMarginLimitProposal("marginnonce1", "confirm entry margin")

    def execute_entry_margin_limit_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None:
        self.executed.append((user_id, nonce))
        if nonce != "marginnonce1":
            return None
        self.current = self.proposed[-1][1]
        return "entry margin changed"


def _config(tmp_path: Path) -> TelegramBotFileConfig:
    token = tmp_path / "token"
    chats = tmp_path / "chats"
    users = tmp_path / "users"
    token.write_text("123456789:" + "a" * 32, encoding="ascii")
    chats.write_text("42\n", encoding="ascii")
    users.write_text("42\n", encoding="ascii")
    return TelegramBotFileConfig.load(token, chats, users)


def test_long_polling_and_menu_use_only_allowed_bot_methods(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object], float]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        calls.append((path, document, timeout))
        if path.endswith("/getUpdates"):
            result: Any = [{"update_id": 7}]
        else:
            result = {"message_id": 10}
        return TelegramHttpResult(200, json.dumps({"ok": True, "result": result}).encode())

    client = TelegramBotClient(_config(tmp_path), transport=transport)
    assert client.get_updates(offset=7)[0]["update_id"] == 7
    client.send_message(42, "hello", reply_markup={"inline_keyboard": []})

    assert calls[0][0].endswith("/getUpdates")
    assert calls[0][1]["allowed_updates"] == ["message", "callback_query"]
    assert calls[1][0].endswith("/sendMessage")
    assert all("setWebhook" not in call[0] for call in calls)


def test_control_buttons_require_authorized_user_and_two_step_nonce(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object], float]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        calls.append((path, document, timeout))
        result: Any = True if path.endswith("answerCallbackQuery") else {"message_id": 10}
        return TelegramHttpResult(200, json.dumps({"ok": True, "result": result}).encode())

    client = TelegramBotClient(_config(tmp_path), transport=transport)
    challenges = Challenges()
    controls = Controls()
    router = TelegramMenuRouter(
        client=client,
        dashboard=Dashboard(),
        challenges=challenges,
        controls=controls,
    )
    base = {"message": {"chat": {"id": 42}}}
    router.handle(
        {
            "callback_query": {
                "id": "one",
                "from": {"id": 99},
                "data": "ctl:pause",
                **base,
            }
        }
    )
    assert challenges.created == []
    router.handle(
        {
            "callback_query": {
                "id": "two",
                "from": {"id": 42},
                "data": "ctl:pause",
                **base,
            }
        }
    )
    assert challenges.created == [(42, ControlAction.PAUSE_NEW_ENTRIES)]
    assert controls.executed == []
    router.handle(
        {
            "callback_query": {
                "id": "three",
                "from": {"id": 42},
                "data": "confirm:nonce123456",
                **base,
            }
        }
    )
    assert controls.executed == [(42, ControlAction.PAUSE_NEW_ENTRIES)]
    assert any(
        call[0].endswith("/sendMessage") and "确认执行" in str(call[1].get("text", ""))
        for call in calls
    )


def test_read_only_menu_accepts_only_configured_chat(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object], float]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        calls.append((path, document, timeout))
        return TelegramHttpResult(
            200,
            json.dumps({"ok": True, "result": {"message_id": 10}}).encode(),
        )

    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
    )
    router.handle(
        {
            "message": {
                "chat": {"id": 999},
                "from": {"id": 42},
                "text": "/status",
            }
        }
    )
    assert calls == []


def test_reply_keyboard_is_safe_compact_and_toggleable_from_the_input_field() -> None:
    keyboard = persistent_reply_keyboard()
    rows = keyboard["keyboard"]
    assert isinstance(rows, list)
    labels = [button["text"] for row in rows for button in row]
    assert labels == ["📊 总览", "📈 仓位", "👥 带单员", "🧾 订单", "💹 盈亏", "⚙️ 控制"]
    assert not any(label in labels for label in ("暂停", "恢复", "全部减仓"))
    assert keyboard["resize_keyboard"] is True
    assert keyboard["is_persistent"] is False
    assert keyboard["one_time_keyboard"] is False
    assert keyboard["input_field_placeholder"] == "点输入框旁的键盘图标切换导航"


def test_contextual_inline_buttons_separate_navigation_from_dangerous_controls() -> None:
    status = contextual_inline_keyboard("status")["inline_keyboard"]
    status_callbacks = [button["callback_data"] for row in status for button in row]
    assert len(status_callbacks) == 4
    assert not any(value.startswith("ctl:") for value in status_callbacks)
    assert "audit:run" in status_callbacks

    leaders = contextual_inline_keyboard("leaders")["inline_keyboard"]
    leader_callbacks = [button["callback_data"] for row in leaders for button in row]
    assert "view:selection" in leader_callbacks

    funds = contextual_inline_keyboard("funds")["inline_keyboard"]
    fund_callbacks = [button["callback_data"] for row in funds for button in row]
    assert "margin:manage" in fund_callbacks
    assert "summary_reset:request" in fund_callbacks

    codex = contextual_inline_keyboard("codex")["inline_keyboard"]
    codex_callbacks = [button["callback_data"] for row in codex for button in row]
    assert codex_callbacks == [
        "audit:run",
        "view:codex",
        "view:repair",
        "view:selection",
    ]

    repair = contextual_inline_keyboard("repair")["inline_keyboard"]
    repair_callbacks = [button["callback_data"] for row in repair for button in row]
    assert repair_callbacks == ["view:repair", "view:codex", "view:status"]

    health = contextual_inline_keyboard("health")["inline_keyboard"]
    health_callbacks = [button["callback_data"] for row in health for button in row]
    assert health_callbacks == ["view:health", "view:codex", "view:status"]

    for view in (
        "status",
        "leaders",
        "positions",
        "pending",
        "orders",
        "funds",
        "pnl",
        "codex",
        "repair",
        "health",
        "selection",
    ):
        rows = contextual_inline_keyboard(view)["inline_keyboard"]
        callbacks = [button["callback_data"] for row in rows for button in row]
        assert f"view:{view}" in callbacks

    control = contextual_inline_keyboard("control")["inline_keyboard"]
    control_callbacks = [button["callback_data"] for row in control for button in row]
    assert control_callbacks == [
        "ctl:pause",
        "ctl:resume",
        "ctl:reduce_all",
        "view:status",
    ]


def test_notification_keyboard_binds_callbacks_to_durable_outbox_message() -> None:
    rows = notification_inline_keyboard("health", "a" * 64)["inline_keyboard"]
    callbacks = [button["callback_data"] for row in rows for button in row]

    assert callbacks == [
        f"n:{'a' * 16}:view:health",
        f"n:{'a' * 16}:view:codex",
        f"n:{'a' * 16}:view:status",
    ]
    assert not any(value.endswith(":return") for value in callbacks)
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)


def test_notification_navigation_can_return_to_original_after_router_restart(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del timeout
        method = path.rsplit("/", 1)[-1]
        calls.append((method, document))
        result: Any = True if method == "answerCallbackQuery" else {"message_id": 88}
        return TelegramHttpResult(200, json.dumps({"ok": True, "result": result}).encode())

    client = TelegramBotClient(_config(tmp_path), transport=transport)
    first_router = TelegramMenuRouter(
        client=client,
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
    )
    token = "a" * 16
    message = {"message_id": 88, "chat": {"id": 42}}
    first_router.handle(
        {
            "callback_query": {
                "id": "open-notification-view",
                "from": {"id": 42},
                "data": f"n:{token}:view:health",
                "message": message,
            }
        }
    )

    assert calls[-1][0] == "editMessageText"
    assert calls[-1][1]["text"] == "dashboard:health"
    derived_rows = calls[-1][1]["reply_markup"]["inline_keyboard"]  # type: ignore[index]
    derived_callbacks = [button["callback_data"] for row in derived_rows for button in row]
    assert derived_callbacks[-1] == f"n:{token}:return"
    assert all(
        value.startswith(f"n:{token}:") and len(value.encode("utf-8")) <= 64
        for value in derived_callbacks
    )

    calls.clear()
    restarted_router = TelegramMenuRouter(
        client=client,
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
    )
    restarted_router.handle(
        {
            "callback_query": {
                "id": "return-notification",
                "from": {"id": 42},
                "data": f"n:{token}:return",
                "message": message,
            }
        }
    )

    assert [method for method, _ in calls] == ["answerCallbackQuery", "editMessageText"]
    assert calls[-1][1]["text"] == "original notification"
    original_rows = calls[-1][1]["reply_markup"]["inline_keyboard"]  # type: ignore[index]
    original_callbacks = [button["callback_data"] for row in original_rows for button in row]
    assert f"n:{token}:view:health" in original_callbacks
    assert not any(value.endswith(":return") for value in original_callbacks)


def test_multiplier_keyboards_bind_callbacks_to_leader_id_not_slot() -> None:
    choices = (
        LeaderMultiplierChoice(
            "5109186975387420161",
            "⚡ 短线 1 · active leader",
            3,
        ),
    )
    management = multiplier_management_keyboard(choices)["inline_keyboard"]
    assert management[0][0]["callback_data"] == "mult:leader:5109186975387420161"
    values = multiplier_value_keyboard(choices[0])["inline_keyboard"]
    callbacks = [button["callback_data"] for row in values[:2] for button in row]
    assert callbacks == [f"mult:set:5109186975387420161:{value}" for value in range(1, 11)]
    assert values[0][2]["text"] == "✅ 3倍"
    manage_callbacks = [
        button["callback_data"]
        for row in leader_management_keyboard()["inline_keyboard"]
        for button in row
    ]
    assert "mult:manage" in manage_callbacks
    assert "lead:manual:custom1" in manage_callbacks
    assert "lead:manual:custom2" in manage_callbacks
    assert "lead:candidates:custom1" not in manage_callbacks
    assert "lead:candidates:custom2" not in manage_callbacks
    assert "lead:remove:custom1" in manage_callbacks
    assert "lead:remove:custom2" in manage_callbacks
    assert "lock:manage" in manage_callbacks


def test_entry_margin_keyboard_marks_current_and_offers_custom_input() -> None:
    rows = entry_margin_limit_keyboard(Decimal("60"))["inline_keyboard"]
    callbacks = [button["callback_data"] for row in rows for button in row]

    assert callbacks[:4] == [
        "margin:set:30",
        "margin:set:60",
        "margin:set:90",
        "margin:set:120",
    ]
    assert rows[0][1]["text"] == "✅ 60 U"
    assert "margin:custom" in callbacks
    assert callbacks[-1] == "view:funds"


def test_pnl_overview_offers_account_summary_reset() -> None:
    rows = pnl_overview_keyboard(Dashboard().pnl_leader_choices())["inline_keyboard"]
    callbacks = [button["callback_data"] for row in rows for button in row]

    assert "summary_reset:request" in callbacks


def test_account_summary_reset_requires_authorization_and_two_step_confirmation(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del timeout
        method = path.rsplit("/", 1)[-1]
        calls.append((method, document))
        result: Any = (
            True
            if method == "answerCallbackQuery"
            else {"message_id": int(document.get("message_id", 88))}
        )
        return TelegramHttpResult(200, json.dumps({"ok": True, "result": result}).encode())

    class SummaryControls:
        def __init__(self) -> None:
            self.executed: list[tuple[int, str]] = []

        def execute_confirmed(self, *, user_id: int, nonce: str) -> str | None:
            self.executed.append((user_id, nonce))
            return "账户汇总初始化完成" if nonce == "nonce123456" else None

    challenges = Challenges()
    controls = SummaryControls()
    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=challenges,
        controls=controls,
    )
    message = {"message_id": 88, "chat": {"id": 42}}

    router.handle(
        {
            "callback_query": {
                "id": "denied",
                "from": {"id": 99},
                "data": "summary_reset:request",
                "message": message,
            }
        }
    )
    assert challenges.created == []

    router.handle(
        {
            "callback_query": {
                "id": "request",
                "from": {"id": 42},
                "data": "summary_reset:request",
                "message": message,
            }
        }
    )
    assert challenges.created == [(42, ControlAction.RESET_ACCOUNT_SUMMARY)]
    confirmation = next(
        document for method, document in calls if method == "sendMessage"
    )
    assert "不会删除或平掉仓位" in str(confirmation["text"])
    confirmation_rows = confirmation["reply_markup"]["inline_keyboard"]  # type: ignore[index]
    assert confirmation_rows[0][0]["callback_data"] == "summary_confirm:nonce123456"

    router.handle(
        {
            "callback_query": {
                "id": "confirm",
                "from": {"id": 42},
                "data": "summary_confirm:nonce123456",
                "message": message,
            }
        }
    )
    assert controls.executed == [(42, "nonce123456")]
    assert calls[-1][1]["text"] == "账户汇总初始化完成\n\ndashboard:pnl"
    callbacks = [
        button["callback_data"]
        for row in calls[-1][1]["reply_markup"]["inline_keyboard"]  # type: ignore[index]
        for button in row
    ]
    assert "summary_reset:request" in callbacks


def test_leader_lock_keyboard_binds_state_change_to_leader_id() -> None:
    choices = (
        LeaderLockChoice("5109186975387420161", "⚡ 短线 1 · alpha", False),
        LeaderLockChoice("5117780547953263617", "🔒 长线 · beta", True),
    )
    rows = leader_lock_keyboard(choices)["inline_keyboard"]

    assert rows[0][0]["callback_data"] == "lock:set:5109186975387420161:1"
    assert rows[1][0]["callback_data"] == "lock:set:5117780547953263617:0"
    assert rows[0][0]["text"].endswith("锁定")
    assert rows[1][0]["text"].endswith("解锁")


def test_position_close_keyboard_binds_exact_leader_symbol_and_side() -> None:
    leader_id = "5117780547953263617"
    rows = position_close_keyboard(
        PositionAdmin().position_close_choices(lead_portfolio_id=leader_id),
        lead_portfolio_id=leader_id,
    )["inline_keyboard"]

    assert rows[1][0]["callback_data"] == ("pos:close:5117780547953263617:DOGEUSDT:L")
    assert rows[1][0]["text"].startswith("🧯 DOGEUSDT · 多")
    assert rows[2][0] == {
        "text": "🧹 清空该带单员全部仓位",
        "callback_data": "pos:close_leader:5117780547953263617",
    }


def test_position_keyboard_paginates_text_and_close_actions_together() -> None:
    leader_id = "5117780547953263617"
    choices = PositionAdmin().position_close_choices(
        lead_portfolio_id=leader_id,
        page=2,
    )
    rows = position_close_keyboard(
        choices,
        lead_portfolio_id=leader_id,
        page=2,
        has_next=True,
    )["inline_keyboard"]
    callbacks = [button["callback_data"] for row in rows for button in row]

    assert f"view:positions:{leader_id}:1" in callbacks
    assert f"view:positions:{leader_id}:2" in callbacks
    assert f"view:positions:{leader_id}:3" in callbacks


def test_position_overview_keyboard_drills_down_by_leader() -> None:
    choice = PositionAdmin().position_leader_choices()[0]
    rows = position_leader_keyboard((choice,))["inline_keyboard"]
    callbacks = [button["callback_data"] for row in rows for button in row]

    assert callbacks == [
        "view:positions",
        "view:pending",
        "view:positions:5117780547953263617",
        "view:orders",
    ]
    assert rows[1][0]["text"] == "🔒 长线 · long leader · 1 仓"


def test_selective_position_close_requires_authorization_and_confirmation(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del timeout
        method = path.rsplit("/", 1)[-1]
        calls.append((method, document))
        result: Any = True if method == "answerCallbackQuery" else {"message_id": 88}
        return TelegramHttpResult(200, json.dumps({"ok": True, "result": result}).encode())

    admin = PositionAdmin()
    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
        position_admin=admin,
    )
    message = {"message_id": 88, "chat": {"id": 42}}
    target = "pos:close:5117780547953263617:DOGEUSDT:L"
    router.handle(
        {
            "callback_query": {
                "id": "denied",
                "from": {"id": 99},
                "data": target,
                "message": message,
            }
        }
    )
    assert admin.proposed == []

    router.handle(
        {
            "callback_query": {
                "id": "close",
                "from": {"id": 42},
                "data": target,
                "message": message,
            }
        }
    )
    confirmation = next(
        document
        for method, document in calls
        if method == "sendMessage" and document.get("text") == "confirm one position"
    )
    confirmation_buttons = confirmation["reply_markup"]["inline_keyboard"][0]  # type: ignore[index]
    assert confirmation_buttons[0]["callback_data"] == (
        "pos_confirm:positionnonce:5117780547953263617"
    )
    assert confirmation_buttons[1]["callback_data"] == ("view:positions:5117780547953263617")

    router.handle(
        {
            "callback_query": {
                "id": "confirm",
                "from": {"id": 42},
                "data": "pos_confirm:positionnonce:5117780547953263617",
                "message": message,
            }
        }
    )

    assert admin.proposed == [(42, "5117780547953263617", "DOGEUSDT", "LONG")]
    assert admin.executed == [(42, "positionnonce")]
    assert calls[-1][1]["text"] == "position close submitted"
    final_callbacks = [
        button["callback_data"]
        for row in calls[-1][1]["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert "view:positions:5117780547953263617:1" in final_callbacks


def test_leader_position_close_requires_authorization_and_confirmation(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del timeout
        method = path.rsplit("/", 1)[-1]
        calls.append((method, document))
        result: Any = True if method == "answerCallbackQuery" else {"message_id": 88}
        return TelegramHttpResult(200, json.dumps({"ok": True, "result": result}).encode())

    admin = PositionAdmin()
    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
        position_admin=admin,
    )
    message = {"message_id": 88, "chat": {"id": 42}}
    target = "pos:close_leader:5117780547953263617"
    router.handle(
        {
            "callback_query": {
                "id": "denied-leader-close",
                "from": {"id": 99},
                "data": target,
                "message": message,
            }
        }
    )
    assert admin.leader_proposed == []

    router.handle(
        {
            "callback_query": {
                "id": "leader-close",
                "from": {"id": 42},
                "data": target,
                "message": message,
            }
        }
    )
    confirmation = next(
        document
        for method, document in calls
        if method == "sendMessage" and document.get("text") == "confirm leader positions"
    )
    confirmation_buttons = confirmation["reply_markup"]["inline_keyboard"][0]  # type: ignore[index]
    assert confirmation_buttons[0]["callback_data"] == (
        "pos_leader_confirm:leaderpositionnonce:5117780547953263617"
    )
    assert confirmation_buttons[1]["callback_data"] == ("view:positions:5117780547953263617")

    router.handle(
        {
            "callback_query": {
                "id": "confirm-leader-close",
                "from": {"id": 42},
                "data": ("pos_leader_confirm:leaderpositionnonce:5117780547953263617"),
                "message": message,
            }
        }
    )

    assert admin.leader_proposed == [(42, "5117780547953263617")]
    assert admin.leader_executed == [(42, "leaderpositionnonce")]
    assert calls[-1][1]["text"] == "leader positions close submitted"
    callbacks = [
        button["callback_data"]
        for row in calls[-1][1]["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert "view:positions:5117780547953263617:1" in callbacks


def test_multiplier_change_uses_authorization_and_two_step_confirmation(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del timeout
        method = path.rsplit("/", 1)[-1]
        calls.append((method, document))
        result: Any = True if method == "answerCallbackQuery" else {"message_id": 88}
        return TelegramHttpResult(200, json.dumps({"ok": True, "result": result}).encode())

    admin = LeaderAdmin()
    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
        leader_admin=admin,
    )
    message = {"message_id": 88, "chat": {"id": 42}}
    for callback_id, data in (
        ("manage", "mult:manage"),
        ("leader", "mult:leader:5109186975387420161"),
        ("set", "mult:set:5109186975387420161:5"),
        ("confirm", "mult_confirm:multipliernonce"),
    ):
        router.handle(
            {
                "callback_query": {
                    "id": callback_id,
                    "from": {"id": 42},
                    "data": data,
                    "message": message,
                }
            }
        )

    assert admin.multiplier_proposed == [(42, "5109186975387420161", 5)]
    assert admin.multiplier_executed == [(42, "multipliernonce")]
    assert any(call[1].get("text") == "confirm multiplier" for call in calls)
    assert calls[-1][1]["text"] == "multiplier changed"

    before = len(calls)
    router.handle(
        {
            "callback_query": {
                "id": "denied",
                "from": {"id": 99},
                "data": "mult:set:5109186975387420161:10",
                "message": message,
            }
        }
    )
    assert len(calls) == before + 1
    assert admin.multiplier_proposed == [(42, "5109186975387420161", 5)]


def test_entry_margin_change_requires_authorization_and_two_step_confirmation(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del timeout
        method = path.rsplit("/", 1)[-1]
        calls.append((method, document))
        result: Any = (
            True
            if method == "answerCallbackQuery"
            else {"message_id": int(document.get("message_id", 88))}
        )
        return TelegramHttpResult(200, json.dumps({"ok": True, "result": result}).encode())

    admin = MarginAdmin()
    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
        margin_admin=admin,
    )
    message = {"message_id": 88, "chat": {"id": 42}}

    router.handle(
        {
            "callback_query": {
                "id": "denied",
                "from": {"id": 99},
                "data": "margin:set:60",
                "message": message,
            }
        }
    )
    assert admin.proposed == []

    for callback_id, data in (
        ("manage", "margin:manage"),
        ("set", "margin:set:60"),
        ("confirm", "margin_confirm:marginnonce1"),
    ):
        router.handle(
            {
                "callback_query": {
                    "id": callback_id,
                    "from": {"id": 42},
                    "data": data,
                    "message": message,
                }
            }
        )

    assert admin.proposed == [(42, Decimal("60.00"))]
    assert admin.executed == [(42, "marginnonce1")]
    assert any(document.get("text") == "confirm entry margin" for _, document in calls)
    assert calls[-1][1]["text"] == "entry margin changed"


def test_entry_margin_custom_force_reply_and_command_validation(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del timeout
        method = path.rsplit("/", 1)[-1]
        calls.append((method, document))
        result: Any = True if method == "answerCallbackQuery" else {"message_id": 91}
        return TelegramHttpResult(200, json.dumps({"ok": True, "result": result}).encode())

    admin = MarginAdmin()
    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
        margin_admin=admin,
    )
    router.handle(
        {
            "callback_query": {
                "id": "custom",
                "from": {"id": 42},
                "data": "margin:custom",
                "message": {"message_id": 88, "chat": {"id": 42}},
            }
        }
    )
    prompt = str(calls[-1][1]["text"])
    assert calls[-1][1]["reply_markup"]["force_reply"] is True  # type: ignore[index]

    router.handle(
        {
            "message": {
                "chat": {"id": 42},
                "from": {"id": 42},
                "text": "75.5",
                "reply_to_message": {"text": prompt},
            }
        }
    )
    assert admin.proposed == [(42, Decimal("75.50"))]

    router.handle(
        {
            "message": {
                "chat": {"id": 42},
                "from": {"id": 42},
                "text": "/margin_limit 121",
            }
        }
    )
    assert "请输入 5-120 U" in str(calls[-1][1]["text"])


def test_authorized_codex_audit_button_starts_real_audit(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    triggers: list[bool] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del timeout
        method = path.rsplit("/", 1)[-1]
        calls.append((method, document))
        result: Any = True if method == "answerCallbackQuery" else {"message_id": 88}
        return TelegramHttpResult(200, json.dumps({"ok": True, "result": result}).encode())

    def trigger() -> bool:
        triggers.append(True)
        return True

    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
        audit_trigger=trigger,
    )
    router.handle(
        {
            "callback_query": {
                "id": "audit-now",
                "from": {"id": 42},
                "data": "audit:run",
                "message": {"message_id": 88, "chat": {"id": 42}},
            }
        }
    )

    assert triggers == [True]
    assert [method for method, _ in calls] == ["answerCallbackQuery", "editMessageText"]
    assert "系统审查已启动" in str(calls[1][1]["text"])


def test_unauthorized_codex_audit_button_does_not_start_audit(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    triggers: list[bool] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del timeout
        calls.append((path.rsplit("/", 1)[-1], document))
        return TelegramHttpResult(200, b'{"ok":true,"result":true}')

    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
        audit_trigger=lambda: triggers.append(True) is None,
    )
    router.handle(
        {
            "callback_query": {
                "id": "audit-denied",
                "from": {"id": 99},
                "data": "audit:run",
                "message": {"message_id": 88, "chat": {"id": 42}},
            }
        }
    )

    assert triggers == []
    assert [method for method, _ in calls] == ["answerCallbackQuery"]


@pytest.mark.parametrize(
    "view",
    [
        "status",
        "leaders",
        "positions",
        "pending",
        "orders",
        "funds",
        "pnl",
        "codex",
        "repair",
        "health",
        "selection",
    ],
)
def test_callback_navigation_edits_original_message_instead_of_spamming_chat(
    tmp_path: Path,
    view: str,
) -> None:
    calls: list[tuple[str, dict[str, object], float]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        calls.append((path, document, timeout))
        result: Any
        if path.endswith("answerCallbackQuery"):
            result = True
        else:
            result = {"message_id": 88}
        return TelegramHttpResult(200, json.dumps({"ok": True, "result": result}).encode())

    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
    )
    router.handle(
        {
            "callback_query": {
                "id": "refresh",
                "from": {"id": 42},
                "data": f"view:{view}",
                "message": {"message_id": 88, "chat": {"id": 42}},
            }
        }
    )
    assert [call[0].rsplit("/", 1)[-1] for call in calls] == [
        "answerCallbackQuery",
        "editMessageText",
    ]
    assert calls[1][1]["text"] == f"dashboard:{view}"


def test_position_leader_drilldown_filters_text_and_close_buttons_together(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del timeout
        method = path.rsplit("/", 1)[-1]
        calls.append((method, document))
        result: Any = True if method == "answerCallbackQuery" else {"message_id": 88}
        return TelegramHttpResult(200, json.dumps({"ok": True, "result": result}).encode())

    leader_id = "5117780547953263617"
    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
        position_admin=PositionAdmin(),
    )

    router.handle(
        {
            "callback_query": {
                "id": "leader-position",
                "from": {"id": 42},
                "data": f"view:positions:{leader_id}",
                "message": {"message_id": 88, "chat": {"id": 42}},
            }
        }
    )

    assert calls[-1][0] == "editMessageText"
    assert calls[-1][1]["text"] == f"dashboard:positions:{leader_id}"
    callbacks = [
        button["callback_data"]
        for row in calls[-1][1]["reply_markup"]["inline_keyboard"]
        for button in row
    ]
    assert f"view:positions:{leader_id}:1" in callbacks
    assert "pos:close:5117780547953263617:DOGEUSDT:L" in callbacks
    assert "view:positions" in callbacks


def test_pnl_menu_lists_long_term_first_and_opens_leader_detail(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del timeout
        method = path.rsplit("/", 1)[-1]
        calls.append((method, document))
        result: Any = True if method == "answerCallbackQuery" else {"message_id": 88}
        return TelegramHttpResult(200, json.dumps({"ok": True, "result": result}).encode())

    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
    )
    router.handle(
        {
            "message": {
                "chat": {"id": 42},
                "from": {"id": 42},
                "text": "/pnl",
            }
        }
    )
    keyboard = calls[-1][1]["reply_markup"]["inline_keyboard"]  # type: ignore[index]
    assert keyboard[1][0]["callback_data"] == "pnl:leader:5117780547953263617"
    assert keyboard[2][0]["callback_data"] == "pnl:leader:5118776604240532481"

    router.handle(
        {
            "callback_query": {
                "id": "leader-pnl",
                "from": {"id": 42},
                "data": "pnl:leader:5117780547953263617",
                "message": {"message_id": 88, "chat": {"id": 42}},
            }
        }
    )
    assert calls[-1][0] == "editMessageText"
    assert calls[-1][1]["text"] == "dashboard:pnl:5117780547953263617"
    detail_keyboard = calls[-1][1]["reply_markup"]["inline_keyboard"]  # type: ignore[index]
    assert detail_keyboard[0][1]["callback_data"] == "view:pnl"


def test_start_installs_bottom_keyboard_and_text_controls_still_require_confirmation(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, object], float]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        calls.append((path, document, timeout))
        return TelegramHttpResult(
            200,
            json.dumps({"ok": True, "result": {"message_id": 10}}).encode(),
        )

    challenges = Challenges()
    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=challenges,
        controls=Controls(),
    )
    base = {"chat": {"id": 42}, "from": {"id": 42}}
    router.handle({"message": {**base, "text": "/start"}})
    assert "keyboard" in calls[-1][1]["reply_markup"]  # type: ignore[operator]

    router.handle({"message": {**base, "text": "/pause"}})
    assert challenges.created == [(42, ControlAction.PAUSE_NEW_ENTRIES)]
    assert "inline_keyboard" in calls[-1][1]["reply_markup"]  # type: ignore[operator]


def test_unchanged_refresh_and_expired_callback_are_idempotent(tmp_path: Path) -> None:
    calls: list[str] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del document, timeout
        method = path.rsplit("/", 1)[-1]
        calls.append(method)
        if method == "answerCallbackQuery":
            return TelegramHttpResult(
                400,
                b'{"ok":false,"description":"Bad Request: query is too old"}',
            )
        if method == "editMessageText":
            return TelegramHttpResult(
                400,
                b'{"ok":false,"description":"Bad Request: message is not modified"}',
            )
        raise AssertionError(method)

    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
    )
    router.handle(
        {
            "callback_query": {
                "id": "stale-refresh",
                "from": {"id": 42},
                "data": "view:status",
                "message": {"message_id": 88, "chat": {"id": 42}},
            }
        }
    )
    assert calls == ["answerCallbackQuery", "editMessageText"]


def test_uneditable_callback_falls_back_to_one_new_context_message(tmp_path: Path) -> None:
    calls: list[str] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del document, timeout
        method = path.rsplit("/", 1)[-1]
        calls.append(method)
        if method == "answerCallbackQuery":
            result: Any = True
            status = 200
        elif method == "editMessageText":
            return TelegramHttpResult(
                400,
                b'{"ok":false,"description":"Bad Request: message to edit not found"}',
            )
        else:
            result = {"message_id": 99}
            status = 200
        return TelegramHttpResult(status, json.dumps({"ok": True, "result": result}).encode())

    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
    )
    router.handle(
        {
            "callback_query": {
                "id": "missing-message",
                "from": {"id": 42},
                "data": "view:orders",
                "message": {"message_id": 88, "chat": {"id": 42}},
            }
        }
    )
    assert calls == ["answerCallbackQuery", "editMessageText", "sendMessage"]


def test_leader_management_buttons_require_authorization_and_confirmation(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del timeout
        method = path.rsplit("/", 1)[-1]
        calls.append((method, document))
        if method == "answerCallbackQuery":
            result: Any = True
        elif method == "editMessageText":
            result = {"message_id": document["message_id"]}
        else:
            result = {"message_id": 10}
        return TelegramHttpResult(200, json.dumps({"ok": True, "result": result}).encode())

    admin = LeaderAdmin()
    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
        leader_admin=admin,
    )

    def callback(identifier: str, user_id: int, data: str, message_id: int = 88) -> None:
        router.handle(
            {
                "callback_query": {
                    "id": identifier,
                    "from": {"id": user_id},
                    "data": data,
                    "message": {"message_id": message_id, "chat": {"id": 42}},
                }
            }
        )

    callback("denied", 99, "lead:manage")
    assert admin.proposed == []
    assert calls[-1][1]["text"] == "你没有管理权限"

    callback("manage", 42, "lead:manage")
    callback("candidates", 42, "lead:candidates:short1")
    callback("set", 42, "lead:set:short1:5109186975387420161")
    assert admin.proposed == [(42, LeaderSlot.SHORT_TERM_1, "5109186975387420161")]
    assert any(
        method == "sendMessage" and document["text"] == "confirm leader"
        for method, document in calls
    )

    callback("confirm", 42, "lead_confirm:leadernonce123", message_id=10)
    assert admin.executed == [(42, "leadernonce123")]
    assert calls[-1][0] == "editMessageText"
    assert calls[-1][1]["text"] == "leader changed"


def test_leader_lock_requires_authorization_and_two_step_confirmation(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del timeout
        method = path.rsplit("/", 1)[-1]
        calls.append((method, document))
        result: Any = (
            True
            if method == "answerCallbackQuery"
            else {"message_id": int(document.get("message_id", 10))}
        )
        return TelegramHttpResult(200, json.dumps({"ok": True, "result": result}).encode())

    admin = LeaderAdmin()
    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
        leader_admin=admin,
    )

    def callback(identifier: str, user_id: int, data: str, message_id: int = 88) -> None:
        router.handle(
            {
                "callback_query": {
                    "id": identifier,
                    "from": {"id": user_id},
                    "data": data,
                    "message": {"message_id": message_id, "chat": {"id": 42}},
                }
            }
        )

    callback("denied", 99, "lock:set:5109186975387420161:1")
    assert admin.lock_proposed == []
    assert calls[-1][1]["text"] == "你没有管理权限"

    callback("manage", 42, "lock:manage")
    assert calls[-1][0] == "editMessageText"
    assert calls[-1][1]["text"] == "leader lock management"

    callback("lock", 42, "lock:set:5109186975387420161:1")
    assert admin.lock_proposed == [(42, "5109186975387420161", True)]
    assert calls[-1][0] == "sendMessage"
    assert calls[-1][1]["text"] == "confirm leader lock"

    callback("confirm", 42, "lock_confirm:locknonce123", message_id=10)
    assert admin.lock_executed == [(42, "locknonce123")]
    assert calls[-1][0] == "editMessageText"
    assert calls[-1][1]["text"] == "leader locked"


def test_manual_leader_force_reply_accepts_id_or_name_only_for_authorized_user(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del timeout
        method = path.rsplit("/", 1)[-1]
        calls.append((method, document))
        result: Any = True if method == "answerCallbackQuery" else {"message_id": 91}
        return TelegramHttpResult(200, json.dumps({"ok": True, "result": result}).encode())

    admin = LeaderAdmin()
    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
        leader_admin=admin,
    )
    router.handle(
        {
            "callback_query": {
                "id": "manual",
                "from": {"id": 42},
                "data": "lead:manual:custom2",
                "message": {"message_id": 88, "chat": {"id": 42}},
            }
        }
    )
    prompt = str(calls[-1][1]["text"])
    assert calls[-1][1]["reply_markup"]["force_reply"] is True  # type: ignore[index]
    assert "详情链接" in prompt

    base = {"chat": {"id": 42}, "from": {"id": 42}}
    router.handle(
        {
            "message": {
                **base,
                "text": "5014426348046646785",
                "reply_to_message": {"text": prompt},
            }
        }
    )
    assert admin.external == [(42, LeaderSlot.CUSTOM_2, "5014426348046646785")]
    assert calls[-1][1]["text"] == "confirm external leader"

    router.handle(
        {
            "message": {
                **base,
                "text": (
                    "https://www.binance.com/zh-TW/copy-trading/lead-details/"
                    "4788776444236355328?timeRange=30D"
                ),
                "reply_to_message": {"text": prompt},
            }
        }
    )
    assert admin.external[-1] == (42, LeaderSlot.CUSTOM_2, "4788776444236355328")
    assert calls[-1][1]["text"] == "confirm external leader"

    router.handle(
        {
            "message": {
                **base,
                "text": "https://example.com/lead-details/4788776444236355328",
                "reply_to_message": {"text": prompt},
            }
        }
    )
    assert admin.external[-1] == (42, LeaderSlot.CUSTOM_2, "4788776444236355328")
    assert "ID或Binance详情链接" in str(calls[-1][1]["text"])

    router.handle(
        {
            "message": {
                **base,
                "text": "印钞机百分百",
                "reply_to_message": {"text": prompt},
            }
        }
    )
    assert admin.searches == [(LeaderSlot.CUSTOM_2, "印钞机百分百")]
    assert calls[-1][1]["text"].startswith("🔎 🎯 自定义 2名称搜索结果")

    router.handle(
        {
            "message": {
                "chat": {"id": 42},
                "from": {"id": 99},
                "text": "/leader_set custom2 5014426348046646785",
            }
        }
    )
    assert admin.external == [
        (42, LeaderSlot.CUSTOM_2, "5014426348046646785"),
        (42, LeaderSlot.CUSTOM_2, "4788776444236355328"),
    ]
    assert calls[-1][1]["text"] == "你没有带单员管理权限。"


def test_manual_leader_rejection_logs_and_displays_the_real_failure_reason(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[dict[str, object]] = []

    def transport(
        path: str,
        document: dict[str, object],
        timeout: float,
    ) -> TelegramHttpResult:
        del path, timeout
        calls.append(document)
        return TelegramHttpResult(
            200,
            json.dumps({"ok": True, "result": {"message_id": 91}}).encode(),
        )

    class RejectingLeaderAdmin(LeaderAdmin):
        def create_external_leader_change(
            self,
            *,
            user_id: int,
            slot: LeaderSlot,
            lead_portfolio_id: str,
        ) -> LeaderChangeProposal:
            del user_id, slot, lead_portfolio_id
            raise RuntimeError("COPY_MANUAL_LEADER_ONE_WAY_EVIDENCE_UNRESOLVED")

    router = TelegramMenuRouter(
        client=TelegramBotClient(_config(tmp_path), transport=transport),
        dashboard=Dashboard(),
        challenges=Challenges(),
        controls=Controls(),
        leader_admin=RejectingLeaderAdmin(),
    )

    router.handle(
        {
            "message": {
                "chat": {"id": 42},
                "from": {"id": 42},
                "text": "/leader_set custom2 5130551903329651712",
            }
        }
    )

    assert "已读到该带单员" in str(calls[-1]["text"])
    event = json.loads(capsys.readouterr().out)
    assert event == {
        "event": "telegram_leader_admin_rejected",
        "operation": "SET",
        "reason_code": "COPY_MANUAL_LEADER_ONE_WAY_EVIDENCE_UNRESOLVED",
        "slot": "CUSTOM_2",
    }
