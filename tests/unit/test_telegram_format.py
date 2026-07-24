from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_quant.copy_trading.telegram_format import (
    compact_decimal,
    compact_money,
    signed_money,
    signed_percent,
)
from ai_quant.copy_trading.telegram_state import (
    PostgresTelegramState,
    _account_pnl_since_reset,
    _card_text,
    _control_message,
    _net_account_adjustment,
    _notification_contextual_view,
    _notification_text,
    _operator_reason_label,
    _order_price_text,
    _pending_entry_state,
    _pending_expiry_text,
    _position_pnl_text,
    _rebased_logical_equity,
    _render_cards,
)
from ai_quant.notifications.telegram_bot import ControlAction


def test_telegram_environment_labels_are_explicit_and_validated() -> None:
    production = PostgresTelegramState(
        "postgresql://unused",
        execution_environment="PRODUCTION",
    )

    assert production._environment_label == "正式盘"
    assert (
        _control_message(ControlAction.RESUME_TESTNET, environment_label="正式盘")
        == "▶️ 已恢复正式盘新开仓。"
    )
    assert "正式盘新开仓已自动恢复" in _notification_text(
        {
            "event": "copy_codex_repair",
            "state": "REPAIRED",
            "summary": "复检完成",
            "root_cause": "已修复",
            "resumed": True,
        },
        environment_label="正式盘",
    )
    with pytest.raises(ValueError, match="execution environment"):
        PostgresTelegramState("postgresql://unused", execution_environment="UNKNOWN")


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("copy_signal_decision", "positions"),
        ("copy_codex_audit", "codex"),
        ("copy_codex_repair", "repair"),
        ("copy_slot_selection", "leaders"),
        ("copy_leader_follow_multiplier_change", "leaders"),
        ("copy_entry_margin_limit_change", "funds"),
        ("copy_leader_lock_change", "leaders"),
        ("copy_leader_symbol_stop_triggered", "positions"),
        ("copy_health", "health"),
        ("unknown", "status"),
    ],
)
def test_notification_refresh_stays_in_its_own_context(event: str, expected: str) -> None:
    assert _notification_contextual_view({"event": event, "state": "FAILED"}) == expected


def test_submitted_trade_signal_notification_has_no_inline_keyboard_context() -> None:
    assert (
        _notification_contextual_view({"event": "copy_signal_decision", "state": "SUBMITTED"})
        is None
    )


def test_filled_trade_notification_has_no_inline_keyboard_context() -> None:
    assert (
        _notification_contextual_view({"event": "copy_signal_decision", "state": "FILLED"}) is None
    )


def test_pnl_reset_notification_is_explicit_and_keyboard_free() -> None:
    payload = {
        "event": "copy_pnl_reset",
        "occurred_at": "2026-07-21T01:30:00+00:00",
        "operating_envelope_usdt": "150.000000000000000000",
        "logical_available_usdt": "61.440000000000000000",
        "total_initial_margin_usdt": "88.560000000000000000",
        "reason_codes": [
            "COPY_PNL_PRESENTATION_RESET",
            "COPY_ACCOUNT_ENVELOPE_RESET",
        ],
    }

    text = _notification_text(payload)

    assert "交易资金与盈亏已恢复初始状态" in text
    assert "交易资金净值已恢复为 150 U" in text
    assert "均已从现在重新计为 0" in text
    assert "当前仓位、待成交订单" in text
    assert "已有仓位仍会占用保证金额度" in text
    assert "账户未占用资金(含保留) 61.44 U" in text
    assert "交易所实际占用 88.56 U" in text
    assert "07-21 09:30:00" in text
    assert _notification_contextual_view(payload) is None


def test_leader_symbol_stop_notification_explains_netting_isolation_and_cooldown() -> None:
    payload = {
        "event": "copy_leader_symbol_stop_triggered",
        "lead_portfolio_id": "5014426348046646785",
        "leader_nickname": "测试带单员",
        "symbol": "BTCUSDT",
        "net_position_pnl_usdt": "-10.25",
        "loss_limit_usdt": "10",
        "position_pnl_breakdown": [
            {"position_side": "LONG", "unrealized_pnl_usdt": "-15"},
            {"position_side": "SHORT", "unrealized_pnl_usdt": "4.75"},
        ],
        "blocked_until": "2026-07-25T03:00:00+00:00",
        "occurred_at": "2026-07-24T03:00:00+00:00",
        "reason_codes": [
            "COPY_LEADER_SYMBOL_NET_LOSS_LIMIT_REACHED",
            "COPY_LEADER_SYMBOL_ENTRY_COOLDOWN_24H",
        ],
    }

    text = _notification_text(payload)

    assert "现有仓位合计浮盈亏: -10.25 U (多 -15 U | 空 +4.75 U)" in text
    assert "其他带单员、其他币种均不受影响" in text
    assert "减仓和平仓仍允许" in text
    assert "07-25 11:00:00 (24小时后自动恢复)" in text


def test_stop_generated_close_notification_has_distinct_title_and_scope() -> None:
    text = _notification_text(
        {
            "event": "copy_signal_decision",
            "state": "FILLED",
            "signal_origin": "CONTROL",
            "leader_symbol_stop_event_id": "7" * 64,
            "stop_net_position_pnl_usdt": "-10.25",
            "stop_loss_limit_usdt": "10",
            "stop_blocked_until": "2026-07-25T03:00:00+00:00",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "signal_kind": "REDUCE",
            "local_quantity": "0.01",
            "lead_portfolio_id": "5014426348046646785",
            "leader_nickname": "测试带单员",
            "leader_reference_price": "65000",
            "order_type": "MARKET",
            "system_fill_price": "64990",
            "source_occurred_at": "2026-07-24T03:00:00+00:00",
            "occurred_at": "2026-07-24T03:00:01+00:00",
            "reason_codes": [],
        }
    )

    assert "✅ 单币种止损平仓成功" in text
    assert "仅平该带单员此币种" in text
    assert "冷却至 07-25 11:00:00" in text


def test_entry_margin_change_notification_is_explicit_and_returns_to_funds() -> None:
    payload = {
        "event": "copy_entry_margin_limit_change",
        "previous_limit_usdt": "120",
        "limit_usdt": "60",
        "state": "SUCCEEDED",
        "summary": (
            "✅ 共享可用保证金额度已更新\n120 U → 60 U\n"
            "所有带单员后续新开仓立即共用新额度; 已有仓位和待入场订单保持不变。"
        ),
    }

    text = _notification_text(payload)

    assert "120 U → 60 U" in text
    assert "已有仓位和待入场订单保持不变" in text
    assert _notification_contextual_view(payload) == "funds"


def test_uncertain_entry_notification_keeps_pending_context() -> None:
    assert (
        _notification_contextual_view({"event": "copy_signal_decision", "state": "UNCERTAIN"})
        == "pending"
    )


def test_paused_entry_is_explained_as_control_state_not_symbol_or_codex_failure() -> None:
    payload = {
        "event": "copy_signal_decision",
        "state": "RISK_REJECTED",
        "symbol": "DOGEUSDT",
        "position_side": "LONG",
        "signal_kind": "INCREASE",
        "local_quantity": "0",
        "lead_portfolio_id": "5117780547953263617",
        "leader_nickname": "勇行观势",
        "leader_reference_price": "0.072219",
        "source_occurred_at": "2026-07-18T16:48:43+00:00",
        "occurred_at": "2026-07-18T16:51:05+00:00",
        "reason_codes": ["COPY_NEW_ENTRIES_PAUSED_NEW_ENTRIES"],
    }

    text = _notification_text(payload)

    assert text.startswith("\n⏸️ 新开仓已暂停\nDOGEUSDT")
    assert "本次信号已跳过, 与交易对支持无关" in text
    assert "恢复后仅处理后续新信号" in text
    assert "RISK_REJECTED" not in text
    assert _notification_contextual_view(payload) == "control"


def test_margin_capacity_rejection_is_a_clear_keyboard_free_risk_skip() -> None:
    payload = {
        "event": "copy_signal_decision",
        "state": "RISK_REJECTED",
        "symbol": "ETHUSDT",
        "position_side": "SHORT",
        "signal_kind": "INCREASE",
        "local_quantity": "0",
        "lead_portfolio_id": "5109186975387420161",
        "leader_nickname": "短线带单员",
        "leader_reference_price": "1865.81",
        "source_occurred_at": "2026-07-19T03:54:53+00:00",
        "occurred_at": "2026-07-19T03:54:56+00:00",
        "reason_codes": ["COPY_SIZE_SYMBOL_MARGIN_CAP_REACHED"],
    }

    text = _notification_text(payload)

    assert text.startswith("\n🛡️ 风控跳过\nETHUSDT")
    assert "该交易对保证金已接近 20 U 上限" in text
    assert "RISK_REJECTED" not in text
    assert _notification_contextual_view(payload) is None


def test_tradifi_agreement_rejection_explains_exchange_state_and_operator_action() -> None:
    payload = {
        "event": "copy_signal_decision",
        "state": "RISK_REJECTED",
        "symbol": "NVDAUSDT",
        "position_side": "SHORT",
        "signal_kind": "INCREASE",
        "local_quantity": "0.24",
        "lead_portfolio_id": "5117780547953263617",
        "leader_nickname": "领航量化观势",
        "leader_reference_price": "205.14",
        "source_occurred_at": "2026-07-21T14:03:00+00:00",
        "occurred_at": "2026-07-21T14:05:10+00:00",
        "reason_codes": [
            "COPY_TRADIFI_AGREEMENT_REQUIRED",
            "COPY_EXCHANGE_CODE_4411",
        ],
    }

    text = _notification_text(payload)

    assert text.startswith("\n⚠️ Binance 合约协议未开通\nNVDAUSDT")
    assert "Binance 已在撮合前拒绝请求" in text
    assert "没有生成订单、没有成交、也没有仓位残留" in text
    assert "签署协议后, 才能重新提交" in text
    assert text.count("-4411") == 0
    assert "COPY_" not in text


def test_source_reduction_cancellation_is_one_chinese_business_reason() -> None:
    payload = {
        "event": "copy_signal_decision",
        "state": "CANCELLED",
        "symbol": "INJUSDT",
        "position_side": "LONG",
        "signal_kind": "INCREASE",
        "local_quantity": "1.2",
        "lead_portfolio_id": "5130551903329651712",
        "leader_nickname": "测试带单员",
        "leader_reference_price": "12.34",
        "order_type": "LIMIT",
        "limit_price": "12.34",
        "source_occurred_at": "2026-07-19T14:23:46+00:00",
        "occurred_at": "2026-07-19T14:23:50+00:00",
        # Legacy rows may contain both codes for the same cancellation.
        "reason_codes": [
            "COPY_PROTECTED_LIMIT_CANCELLED_BY_SOURCE_REDUCTION",
            "COPY_ORDER_CANCELED",
        ],
    }

    text = _notification_text(payload)

    assert text.startswith("\n🕒 待入场订单已取消\nINJUSDT")
    assert "带单员在本系统入场前已减仓或平仓" in text
    assert "交易所确认订单已撤销" not in text
    assert "COPY_" not in text


def test_orphan_close_is_information_not_a_zero_quantity_failed_order() -> None:
    payload = {
        "event": "copy_signal_decision",
        "state": "IGNORED_ORPHAN",
        "symbol": "AKEUSDT",
        "position_side": "SHORT",
        "signal_kind": "REDUCE",
        "local_quantity": "0",
        "lead_portfolio_id": "5130551903329651712",
        "leader_nickname": "稳重求大胜",
        "source_quantity": "40946297.000000000000000000",
        "leader_reference_price": "0.0017105",
        "leader_realized_pnl_delta": "6756.204234000000000000",
        "source_occurred_at": "2026-07-19T12:15:23+00:00",
        "occurred_at": "2026-07-19T12:17:51+00:00",
        "reason_codes": ["COPY_REDUCTION_ORPHAN"],
    }

    text = _notification_text(payload)

    assert text.startswith("\n📡 带单员减仓/平仓信号\nAKEUSDT")
    assert "空单 · 减仓/平仓 | 带单员数量 40946297" in text
    assert "带单员成交价: 0.0017105" in text
    assert "带单员本次收益: 6756.204234 U" in text
    assert "系统处理: 只记录带单员信号, 未向 Binance 提交订单" in text
    assert "原因: 系统虚拟账本中没有该带单员在这个币种、这个方向的已成交仓位" in text
    assert "本系统此前没有成功跟入这笔仓位" in text
    assert "避免把平仓误执行成反向开仓" in text
    assert "数量 0" not in text
    assert "委托 尚未提交" not in text
    assert "成交 待成交" not in text
    assert "COPY_" not in text


@pytest.mark.parametrize(
    ("state", "reason", "expected_action", "expected_reason"),
    [
        (
            "CANCELLED",
            "COPY_PROTECTED_LIMIT_EXPIRED",
            "系统处理: 已撤销待入场订单, 本次不会建立仓位",
            "原因: 入场限价单在有效期内未成交, 系统已自动撤销",
        ),
        (
            "IGNORED_MINIMUM",
            "COPY_SIZE_BELOW_MINIMUM_QUANTITY",
            "系统处理: 未向 Binance 提交订单",
            "原因: 按当前额度计算出的数量低于交易所最低要求",
        ),
        (
            "RISK_REJECTED",
            "COPY_SYMBOL_NOT_AVAILABLE_ON_TESTNET",
            "系统处理: 未向 Binance 提交订单",
            "原因: Binance 测试盘不支持这个交易对",
        ),
        (
            "FAILED",
            "COPY_SUBMISSION_NOT_FOUND_AFTER_GRACE",
            "系统处理: 本次跟单未完成, 已记录错误并触发自动排查",
            "原因: 宽限期后仍未在交易所找到这笔订单",
        ),
        (
            "UNCERTAIN",
            "COPY_SUBMISSION_STATUS_UNKNOWN",
            "不会循环重试或把同一笔带单信号执行多次",
            "原因: Binance 测试盘没有在本次下单请求中返回明确结果",
        ),
    ],
)
def test_non_success_notifications_explain_action_and_reason(
    state: str,
    reason: str,
    expected_action: str,
    expected_reason: str,
) -> None:
    text = _notification_text(
        {
            "event": "copy_signal_decision",
            "state": state,
            "symbol": "ETHUSDT",
            "position_side": "LONG",
            "signal_kind": "INCREASE",
            "local_quantity": "0.01",
            "lead_portfolio_id": "5130551903329651712",
            "leader_nickname": "测试带单员",
            "leader_reference_price": "2000",
            "source_occurred_at": "2026-07-19T12:15:23+00:00",
            "occurred_at": "2026-07-19T12:17:51+00:00",
            "reason_codes": [reason],
        }
    )

    assert expected_action in text
    assert expected_reason in text
    assert "COPY_" not in text


def test_testnet_timeout_and_gateway_errors_have_specific_chinese_explanations() -> None:
    text = _notification_text(
        {
            "event": "copy_signal_decision",
            "state": "UNCERTAIN",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "signal_kind": "INCREASE",
            "local_quantity": "0.01",
            "lead_portfolio_id": "5014426348046646785",
            "leader_nickname": "测试带单员",
            "leader_reference_price": "64395",
            "source_occurred_at": "2026-07-20T05:05:27+00:00",
            "occurred_at": "2026-07-20T05:07:37+00:00",
            "reason_codes": [
                "COPY_SUBMISSION_STATUS_UNKNOWN",
                "PLACE_ORDER_HTTP_408_CODE_-1007",
                "PLACE_ORDER_INVALID_JSON",
            ],
        }
    )

    assert "Binance 测试盘下单后端等待超时" in text
    assert "Binance 测试盘下单网关返回了非标准响应" in text
    assert "未分类" not in text


def test_internal_reason_codes_embedded_in_system_summary_are_translated() -> None:
    text = _notification_text(
        {
            "event": "copy_system",
            "state": "WARNING",
            "summary": ("last decision: COPY_ORDER_REJECTED; COPY_PUBLIC_TRANSPORT_FAILED"),
        }
    )

    assert "交易所拒绝了订单" in text
    assert "连接 Binance 公开数据接口失败" in text
    assert "COPY_" not in text


def test_dashboard_reason_labels_never_expose_internal_codes() -> None:
    assert _operator_reason_label("COPY_POSITION_RECONCILIATION_MISMATCH") == (
        "交易所仓位与虚拟账本不一致"
    )
    assert _operator_reason_label("COPY_ORDER_REJECTED") == "交易所拒绝了订单"
    assert _operator_reason_label("COPY_FUTURE_UNKNOWN_REASON") == (
        "系统遇到尚未配置中文明细的内部异常; 本次不会据此盲目交易, 完整证据已保留并交由 Codex 排查"
    )


def test_reduce_all_completion_notification_confirms_automatic_resume() -> None:
    payload = {
        "event": "copy_runtime_control",
        "state": "RUNNING",
        "reason_codes": ["COPY_OPERATOR_FLATTEN_COMPLETED_AUTO_RESUME"],
    }

    text = _notification_text(payload)
    assert text.startswith("✅ 全部减仓完成\n")
    assert "系统处理: 现有仓位已清零, 已自动恢复正常跟单" in text
    assert "原因: 用户发起的全部清仓已经完成" in text
    assert _notification_contextual_view(payload) == "control"


def test_runtime_reconciliation_resume_explains_why_trading_resumed() -> None:
    text = _notification_text(
        {
            "event": "copy_runtime_control",
            "state": "RUNNING",
            "actor_id": "codex-reconciliation-repair",
            "reason_codes": ["COPY_RECONCILIATION_VERIFIED_AUTO_RESUME"],
        }
    )

    assert "跟单运行状态: 正常跟单" in text
    assert "系统处理: 允许处理新开仓、加仓、减仓和平仓信号" in text
    assert "原因: 此前状态不明确的订单已经与 Binance 完成核对" in text
    assert "COPY_" not in text


def test_selection_failure_explains_retention_retry_and_exact_reason() -> None:
    text = _notification_text(
        {
            "event": "copy_system",
            "state": "SHORT_TERM_SELECTION_FAILED",
            "strategy": "SHORT_TERM",
            "summary": "SHORT_TERM selection failed",
            "reason_codes": ["COPY_SELECTION_SHORT_WIN_RATE_POOL_INSUFFICIENT"],
        }
    )

    assert text.startswith("❌ 短线选人失败\n")
    assert "系统处理: 保留当前带单员" in text
    assert "后续定时任务将自动重试" in text
    assert "原因: 短线一没有足够的合格候选" in text
    assert "COPY_" not in text


def test_selection_directory_contract_failure_is_explained_in_chinese() -> None:
    text = _notification_text(
        {
            "event": "copy_system",
            "state": "SHORT_TERM_SELECTION_FAILED",
            "strategy": "SHORT_TERM",
            "reason_codes": ["COPY_SELECTION_DIRECTORY_NO_VALID_CANDIDATES"],
        }
    )

    assert "Binance 本轮返回的候选资料均不完整" in text
    assert "保留当前带单员" in text
    assert "COPY_" not in text


def test_recovery_notification_explains_action_and_evidence() -> None:
    text = _notification_text(
        {
            "event": "copy_system",
            "state": "RECOVERED",
            "summary": "Telegram 服务复核结果为 success",
        }
    )

    assert text.startswith("✅ 跟单系统故障已恢复\n")
    assert "系统处理: 已恢复正常监控" in text
    assert "恢复依据: Telegram 服务复核结果为 success" in text


def test_codex_audit_translates_applied_action_and_explains_finding() -> None:
    text = _notification_text(
        {
            "event": "copy_codex_audit",
            "state": "CRITICAL",
            "summary": "存在一笔尚未完成对账的部分成交订单",
            "applied_actions": ["PAUSE_NEW_ENTRIES"],
        }
    )

    assert text.startswith("🚨 Codex 小时审查: 严重异常\n")
    assert "检查发现: 存在一笔尚未完成对账的部分成交订单" in text
    assert "系统处理: 已暂停新开仓; 已有仓位的减仓和平仓继续执行" in text
    assert "PAUSE_NEW_ENTRIES" not in text


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("HEALTHY", "未发现需要修复的故障"),
        ("DEGRADED", "本轮确认暂无需改代码、重启或暂停"),
        ("CRITICAL", "本轮没有安全可执行的自动变更"),
    ],
)
def test_codex_audit_without_action_explains_why_nothing_changed(
    state: str,
    expected: str,
) -> None:
    text = _notification_text(
        {
            "event": "copy_codex_audit",
            "state": state,
            "summary": "已完成当前证据审查",
            "applied_actions": [],
        }
    )

    assert expected in text
    assert "未自动变更交易状态" not in text


def test_pending_entry_status_and_expiry_are_human_readable() -> None:
    now = datetime(2026, 7, 18, 1, 0, tzinfo=UTC)

    assert _pending_entry_state({"submission_state": "ACKNOWLEDGED"}) == ("交易所已接单, 等待成交")
    assert "剩余约1小时30分" in _pending_expiry_text(
        now + timedelta(minutes=90),
        now=now,
    )
    assert "已到期" in _pending_expiry_text(now - timedelta(seconds=1), now=now)


def test_dashboard_cards_are_visually_separated_without_indented_carryover() -> None:
    rendered = _render_cards("标题", ["第一条\n明细", "第二条\n明细"])

    assert rendered.startswith("标题\n第一条")
    assert rendered.count("────────") == 1
    assert "第一条\n明细\n────────\n第二条" in rendered
    assert _card_text("  第一行\n  第二行") == "第一行\n第二行"


def test_compact_decimal_removes_only_insignificant_zeroes() -> None:
    assert compact_decimal(Decimal("0.001600000000000000")) == "0.0016"
    assert compact_decimal(Decimal("0.011000000000000000")) == "0.011"
    assert compact_decimal(Decimal("150.000000000000000000")) == "150"
    assert compact_decimal(Decimal("0E-18")) == "0"


def test_money_and_signed_values_remain_bounded_and_readable() -> None:
    assert compact_money(Decimal("10.219104000000000000")) == "10.2191"
    assert signed_money(Decimal("1.000000000000000000")) == "+1"
    assert signed_money(Decimal("-0.130500000000000000")) == "-0.1305"
    assert signed_money(Decimal("-0.00001")) == "+0"
    assert signed_percent(Decimal("2.500000000000000000")) == "+2.5%"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "not-a-number"])
def test_compact_decimal_rejects_non_finite_or_invalid_values(value: str) -> None:
    with pytest.raises(ValueError, match="Telegram numeric value"):
        compact_decimal(value)


def test_health_notification_explains_evidence_and_automatic_protection() -> None:
    text = _notification_text(
        {
            "event": "copy_health",
            "state": "FAILED",
            "findings": [
                {
                    "code": "COPY_POSITION_RECONCILIATION_MISMATCH",
                    "severity": "CRITICAL",
                    "detail": "ETHUSDT:LONG:virtual=0.02:exchange=0.01",
                }
            ],
            "requested_control": "PAUSED_NEW_ENTRIES",
            "codex_wakeup_requested": True,
        }
    )

    assert "交易所仓位与虚拟账本不一致" in text
    assert "virtual=0.02:exchange=0.01" in text
    assert "暂停新开仓" in text
    assert "已唤醒 Codex 立即诊断" in text
    assert "自动修复并复检" in text
    assert "原因: 交易所仓位与虚拟账本不一致" in text
    assert "系统处理:" in text


def test_codex_repair_notification_reports_fix_and_safe_resume() -> None:
    text = _notification_text(
        {
            "event": "copy_codex_repair",
            "state": "REPAIRED",
            "summary": "修复提交状态分类并通过完整回归测试",
            "root_cause": "明确拒单被错误归类为状态未知",
            "changed_files": ["src/ai_quant/copy_trading/execution.py"],
            "resumed": True,
        }
    )

    assert "Codex 自动修复: 已修复" in text
    assert "execution.py" in text
    assert "已自动恢复" in text


def test_multiplier_notification_reports_leader_specific_result() -> None:
    summary = "✅ 已设置 active leader (ID 5109186975387420161): 3倍"
    assert (
        _notification_text(
            {
                "event": "copy_leader_follow_multiplier_change",
                "summary": summary,
            }
        )
        == summary
    )


def test_leader_lock_notifications_explain_protection_without_pausing_trades() -> None:
    summary = (
        "🔒 已锁定 alpha (ID 5109186975387420161)\n"
        "系统处理: 定时选人不能替换该带单员。\n"
        "跟单状态: 正常。"
    )
    assert (
        _notification_text(
            {
                "event": "copy_leader_lock_change",
                "summary": summary,
            }
        )
        == summary
    )


def test_slot_selection_notification_explains_locked_incumbent() -> None:
    text = _notification_text(
        {
            "event": "copy_slot_selection",
            "strategy": "SHORT_TERM",
            "results": [
                {
                    "slot": "SHORT_TERM_1",
                    "status": "BLOCKED_BY_LEADER_LOCK",
                    "incumbent_lead_portfolio_id": "5109186975387420161",
                    "incumbent_nickname": "locked alpha",
                    "candidate_lead_portfolio_id": "5117780547953263617",
                    "candidate_nickname": "candidate beta",
                    "expires_at": None,
                }
            ],
        }
    )

    assert "当前带单员 locked alpha 已锁定" in text
    assert "本轮保留且继续正常跟单" in text


def test_slot_selection_notification_separates_locked_current_and_backup() -> None:
    text = _notification_text(
        {
            "event": "copy_slot_selection",
            "strategy": "SHORT_TERM",
            "results": [
                {
                    "slot": "SHORT_TERM_1",
                    "status": "LOCKED_UNCHANGED",
                    "incumbent_lead_portfolio_id": "5109186975387420161",
                    "incumbent_nickname": "locked alpha",
                    "candidate_lead_portfolio_id": "5109186975387420161",
                    "candidate_nickname": "locked alpha",
                    "backup_lead_portfolio_id": "5117780547953263617",
                    "backup_nickname": "backup beta",
                    "expires_at": None,
                }
            ],
        }
    )

    assert "当前: locked alpha (ID 5109186975387420161)" in text
    assert "备用: backup beta (ID 5117780547953263617)" in text
    assert "用户已锁定, 当前带单员保持不变" in text
    assert "备用人选只记录, 不会自动接管" in text


def test_position_pnl_is_calculated_per_leader_owned_position() -> None:
    text = _position_pnl_text(
        {
            "position_side": "LONG",
            "resulting_local_quantity": Decimal("0.02"),
            "committed_margin_usdt": Decimal("4"),
            "system_average_entry_price": Decimal("2000"),
            "mark_price": Decimal("2100"),
            "position_realized_pnl_usdt": Decimal("1.5"),
        }
    )

    assert "标记 2100" in text
    assert "未实现 +2 U (+50%)" in text
    assert "已实现 +1.5 U" in text
    assert "累计 +3.5 U" in text


def test_position_pnl_subtracts_the_latest_reset_anchor() -> None:
    text = _position_pnl_text(
        {
            "position_side": "LONG",
            "resulting_local_quantity": Decimal("0.02"),
            "committed_margin_usdt": Decimal("4"),
            "system_average_entry_price": Decimal("2000"),
            "mark_price": Decimal("2100"),
            "position_realized_pnl_usdt": Decimal("0.5"),
            "position_unrealized_pnl_reset_anchor_usdt": Decimal("1.25"),
        }
    )

    assert "未实现 +0.75 U" in text
    assert "已实现 +0.5 U" in text
    assert "累计 +1.25 U" in text


def test_account_adjustment_reconciles_line_gross_pnl_to_account_net_pnl() -> None:
    adjustment = _net_account_adjustment(
        Decimal("-0.46323225"),
        [
            {"total_pnl_usdt": Decimal("1.42522571")},
            {"total_pnl_usdt": Decimal("0.6272")},
            {"total_pnl_usdt": Decimal("0")},
        ],
    )

    assert adjustment == Decimal("-2.51565796")


def test_dashboard_equity_restarts_from_the_operating_envelope() -> None:
    assert _rebased_logical_equity(Decimal("150"), Decimal("0")) == Decimal("150")
    assert _rebased_logical_equity(Decimal("150"), Decimal("1.25")) == Decimal("151.25")
    assert _rebased_logical_equity(Decimal("150"), Decimal("-200")) == Decimal("0")


def test_account_pnl_is_zero_during_the_post_reset_valuation_window() -> None:
    reset_at = datetime(2026, 7, 21, 15, 27, 6, tzinfo=UTC)
    values = _account_pnl_since_reset(
        {
            "observed_at": reset_at - timedelta(seconds=7),
            "reset_occurred_at": reset_at,
            "total_pnl_usdt": Decimal("0.4754"),
            "reset_total_pnl_usdt": Decimal("0"),
            "day_anchor_pnl_usdt": Decimal("0"),
            "month_anchor_pnl_usdt": Decimal("0"),
            "realized_net_pnl_usdt": Decimal("0.4754"),
            "reset_realized_net_pnl_usdt": Decimal("0"),
            "unrealized_pnl_usdt": Decimal("0"),
            "reset_unrealized_pnl_usdt": Decimal("0"),
        }
    )

    assert values == (Decimal("0"),) * 5


def test_account_adjustment_uses_reset_line_totals() -> None:
    adjustment = _net_account_adjustment(
        Decimal("1.25"),
        [
            {
                "total_pnl_usdt": Decimal("11.5"),
                "reset_total_pnl_usdt": Decimal("10"),
            },
            {
                "total_pnl_usdt": Decimal("-3"),
                "reset_total_pnl_usdt": Decimal("-2.75"),
            },
        ],
    )

    assert adjustment == Decimal("0")


def test_short_position_pnl_uses_inverse_price_direction() -> None:
    text = _position_pnl_text(
        {
            "position_side": "SHORT",
            "resulting_local_quantity": Decimal("0.02"),
            "committed_margin_usdt": Decimal("4"),
            "system_average_entry_price": Decimal("2000"),
            "mark_price": Decimal("1900"),
            "position_realized_pnl_usdt": Decimal("0"),
        }
    )

    assert "未实现 +2 U (+50%)" in text


def test_filled_entry_notification_distinguishes_all_three_prices() -> None:
    text = _notification_text(
        {
            "event": "copy_signal_decision",
            "state": "FILLED",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "signal_kind": "INCREASE",
            "local_quantity": "0.000800000000000000",
            "lead_portfolio_id": "5014426348046646785",
            "leader_nickname": "测试带单员",
            "leader_reference_price": "63037.800000000000000000",
            "order_type": "LIMIT",
            "limit_price": "63037.800000000000000000",
            "requested_quantity": "0.000800000000000000",
            "leverage": 10,
            "system_fill_price": "63037.700000000000000000",
            "source_occurred_at": "2026-07-17T06:42:27+00:00",
            "occurred_at": "2026-07-17T06:42:30+00:00",
            "reason_codes": [],
        }
    )

    assert text.startswith("\n✅ 跟单入场成功\n")
    assert "资金: 入场保证金 5.043016 U | 杠杆 10x" in text
    assert "价格: 带单员 63037.8 | 委托 63037.8 | 成交 63037.7" in text
    assert "Binance 已确认成交, 本地带单员账本和仓位记录均已更新" in text
    assert text.endswith("时间: 带单员 07-17 14:42:27 | 系统 07-17 14:42:30\n")
    assert "────────────" not in text
    assert "\n\n" not in text


def test_submitted_entry_is_presented_as_a_distinct_trade_signal() -> None:
    text = _notification_text(
        {
            "event": "copy_signal_decision",
            "state": "SUBMITTED",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "signal_kind": "INCREASE",
            "local_quantity": "0.0008",
            "lead_portfolio_id": "5014426348046646785",
            "leader_nickname": "测试带单员",
            "leader_reference_price": "63037.8",
            "order_type": "LIMIT",
            "limit_price": "63037.8",
            "expires_at": "2026-07-17T07:42:30+00:00",
            "requested_quantity": "0.0008",
            "leverage": 10,
            "system_fill_price": None,
            "source_occurred_at": "2026-07-17T06:42:27+00:00",
            "occurred_at": "2026-07-17T06:42:30+00:00",
            "reason_codes": [],
        }
    )

    assert text.startswith("\n📡 交易信号\n")
    assert "资金: 预计保证金 5.043024 U | 杠杆 10x" in text
    assert "委托 63037.8 (至 07-17 15:42)" in text
    assert "成交 待成交" in text
    assert "当前尚未宣称成交" in text
    assert "成交后会另发成功通知" in text
    assert text.endswith("时间: 带单员 07-17 14:42:27 | 系统 07-17 14:42:30\n")
    assert "────────────" not in text
    assert "\n\n" not in text


def test_persistent_entry_notification_has_no_clock_expiry() -> None:
    text = _notification_text(
        {
            "event": "copy_signal_decision",
            "state": "SUBMITTED",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "signal_kind": "INCREASE",
            "local_quantity": "0.0008",
            "lead_portfolio_id": "5014426348046646785",
            "leader_nickname": "测试带单员",
            "leader_reference_price": "63037.8",
            "order_type": "LIMIT",
            "limit_price": "63037.8",
            "expires_at": None,
            "requested_quantity": "0.0008",
            "leverage": 10,
            "system_fill_price": None,
            "source_occurred_at": "2026-07-17T06:42:27+00:00",
            "occurred_at": "2026-07-17T06:42:30+00:00",
            "reason_codes": [],
        }
    )

    assert "委托 63037.8" in text
    assert "(至 " not in text


def test_close_notification_includes_leader_and_system_realized_pnl() -> None:
    text = _notification_text(
        {
            "event": "copy_signal_decision",
            "state": "FILLED",
            "symbol": "ETHUSDT",
            "position_side": "LONG",
            "signal_kind": "REDUCE",
            "local_quantity": "0.011",
            "lead_portfolio_id": "5014426348046646785",
            "leader_nickname": "测试带单员",
            "leader_reference_price": "1850",
            "order_type": "MARKET",
            "system_fill_price": "1849.5",
            "source_occurred_at": "2026-07-17T07:01:02+00:00",
            "occurred_at": "2026-07-17T07:01:05+00:00",
            "leader_realized_pnl_delta": "12.340000000000000000",
            "system_realized_pnl_delta_usdt": "0.192500000000000000",
            "reason_codes": [],
        }
    )

    assert "时间: 带单员 07-17 15:01:02 | 系统 07-17 15:01:05" in text
    assert "本次平仓收益: 带单员 +12.34 U | 我的系统 +0.1925 U" in text
    assert "\n\n" not in text


def test_unsubmitted_signal_does_not_claim_it_was_a_market_order() -> None:
    text = _order_price_text(
        {
            "signal_kind": "INCREASE",
            "leader_reference_price": Decimal("1831.99"),
            "order_type": None,
            "system_fill_price": None,
        }
    )

    assert "我的委托限价 尚未提交" in text
    assert "我的入场成交均价 尚无" in text


def test_control_reduction_notification_does_not_show_sentinel_leader_price() -> None:
    text = _notification_text(
        {
            "event": "copy_signal_decision",
            "state": "FILLED",
            "signal_origin": "CONTROL",
            "symbol": "BCHUSDT",
            "position_side": "SHORT",
            "signal_kind": "REDUCE",
            "local_quantity": "0.023",
            "lead_portfolio_id": "5109186975387420161",
            "leader_nickname": "测试带单员",
            "leader_reference_price": "1",
            "order_type": "MARKET",
            "system_fill_price": "219.64",
            "system_realized_pnl_delta_usdt": "-0.0064",
            "source_occurred_at": "2026-07-18T15:59:36+00:00",
            "occurred_at": "2026-07-18T15:59:38+00:00",
            "reason_codes": ["COPY_ORDER_RECONCILED"],
        }
    )

    assert "价格: 控制减仓 | 委托 市价 | 成交 219.64" in text
    assert "带单员 1" not in text
    assert "本次平仓收益: 我的系统 -0.0064 U" in text
    assert text.endswith("时间: 控制指令 07-18 23:59:36 | 系统 07-18 23:59:38\n")
