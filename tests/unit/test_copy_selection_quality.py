from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_quant.copy_trading.codex_selection import CandidateOrderProfile, candidate_document
from ai_quant.copy_trading.models import LeaderSnapshot, PublicLeaderOrder
from ai_quant.copy_trading.selection import assess_candidate, copy_social_proof_score
from ai_quant.copy_trading.selection_quality import (
    LONG_TERM,
    SHORT_TERM_INTRADAY,
    SHORT_TERM_WIN_RATE,
    LeaderPerformanceTrend,
    assess_selection_quality,
)


def _leader(
    *,
    win_rate: str = "82.14",
    drawdown: str = "16.06",
    current_copy_count: int = 10,
) -> LeaderSnapshot:
    now = datetime.now(UTC)
    return LeaderSnapshot(
        lead_portfolio_id="5109186975387420161",
        nickname="quality-test",
        roi_pct=Decimal("500"),
        pnl_usdt=Decimal("1000"),
        aum_usdt=Decimal("50000"),
        maximum_drawdown_pct=Decimal(drawdown),
        win_rate_pct=Decimal(win_rate),
        current_copy_count=current_copy_count,
        maximum_copy_count=max(100, current_copy_count),
        start_time_ms=int((now - timedelta(days=60)).timestamp() * 1000),
        portfolio_type="PUBLIC",
        raw_payload_hash="a" * 64,
    )


def _close_orders(pnls: tuple[str, ...]) -> tuple[PublicLeaderOrder, ...]:
    now = datetime.now(UTC)
    orders: list[PublicLeaderOrder] = []
    for index, pnl in enumerate(pnls):
        update_time = now - timedelta(days=index % 6, minutes=index + 1)
        update_ms = int(update_time.timestamp() * 1000)
        orders.append(
            PublicLeaderOrder.from_api(
                "5109186975387420161",
                {
                    "symbol": "ETHUSDT",
                    "side": "SELL",
                    "type": "MARKET",
                    "positionSide": "LONG",
                    "executedQty": "1",
                    "avgPrice": "2000",
                    "totalPnl": pnl,
                    "orderTime": update_ms - 1000,
                    "orderUpdateTime": update_ms,
                },
            )
        )
    return tuple(orders)


def test_profile_exposes_outlier_concentration_instead_of_only_net_profit() -> None:
    pnls = ("406.8", "18.27", *("3" for _ in range(14)), *("-2" for _ in range(10)))
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    profile = CandidateOrderProfile.from_orders(
        _close_orders(tuple(pnls)),
        observed_at_ms=now_ms,
    )

    assert profile.profitable_close_count == 16
    assert profile.losing_close_count == 10
    assert profile.profitable_close_rate_pct is not None
    assert profile.profitable_close_rate_pct.quantize(Decimal("0.01")) == Decimal("61.54")
    assert profile.total_realized_pnl == Decimal("447.07")
    assert profile.robust_realized_pnl_ex_largest_profit == Decimal("40.27")
    assert profile.top_two_profit_contribution_pct is not None
    assert profile.top_two_profit_contribution_pct > Decimal("90")


def test_intraday_gate_rejects_busy_leader_whose_profit_depends_on_two_winners() -> None:
    pnls = ("406.8", "18.27", *("3" for _ in range(14)), *("-2" for _ in range(10)))
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    profile = CandidateOrderProfile.from_orders(
        _close_orders(tuple(pnls)),
        observed_at_ms=now_ms,
    )

    assessment = assess_selection_quality(
        _leader(current_copy_count=400),
        profile,
        objective=SHORT_TERM_INTRADAY,
        observed_at_ms=now_ms,
    )

    assert not assessment.eligible
    assert "COPY_SELECTION_PROFIT_CONCENTRATED" in assessment.reason_codes


def test_repeatable_close_profile_passes_all_three_objective_gates() -> None:
    pnls = tuple("-2" if index in {4, 9, 14, 19} else "5" for index in range(36))
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    profile = CandidateOrderProfile.from_orders(
        _close_orders(pnls),
        observed_at_ms=now_ms,
    )
    leader = _leader(win_rate="85", drawdown="5")

    assessments = tuple(
        assess_selection_quality(
            leader,
            profile,
            objective=objective,
            observed_at_ms=now_ms,
        )
        for objective in (LONG_TERM, SHORT_TERM_WIN_RATE, SHORT_TERM_INTRADAY)
    )

    assert all(assessment.eligible for assessment in assessments)
    assert all(assessment.score > Decimal("60") for assessment in assessments)


def test_established_copy_following_adds_bounded_social_proof_to_all_scores() -> None:
    pnls = tuple("-2" if index in {4, 9, 14, 19} else "5" for index in range(36))
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    profile = CandidateOrderProfile.from_orders(
        _close_orders(pnls),
        observed_at_ms=now_ms,
    )
    quiet = _leader(win_rate="85", drawdown="5", current_copy_count=10)
    established = _leader(win_rate="85", drawdown="5", current_copy_count=600)

    assert copy_social_proof_score(10) < copy_social_proof_score(400)
    assert copy_social_proof_score(400) < copy_social_proof_score(600) == Decimal("100")
    assert assess_candidate(established, observed_at_ms=now_ms).deterministic_score > (
        assess_candidate(quiet, observed_at_ms=now_ms).deterministic_score
    )
    for objective in (LONG_TERM, SHORT_TERM_WIN_RATE, SHORT_TERM_INTRADAY):
        quiet_quality = assess_selection_quality(
            quiet,
            profile,
            objective=objective,
            observed_at_ms=now_ms,
        )
        established_quality = assess_selection_quality(
            established,
            profile,
            objective=objective,
            observed_at_ms=now_ms,
        )

        assert established_quality.score > quiet_quality.score
        assert established_quality.copy_social_proof_score == Decimal("100.000000")

    document = candidate_document(
        established,
        assess_candidate(established, observed_at_ms=now_ms),
        profile,
        execution_trading_symbols=frozenset({"ETHUSDT"}),
        execution_environment="TESTNET",
    )
    assert document["current_copy_count"] == 600
    assert document["maximum_copy_count"] == 600
    assert document["copy_social_proof_score"] == "100.000000"


def test_fast_public_drawdown_and_roi_deterioration_downranks_candidate() -> None:
    pnls = tuple("-2" if index in {4, 9, 14} else "5" for index in range(32))
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    profile = CandidateOrderProfile.from_orders(
        _close_orders(pnls),
        observed_at_ms=now_ms,
    )
    trend = LeaderPerformanceTrend(
        baseline_age_hours=Decimal("24"),
        roi_change_pct=Decimal("-8"),
        pnl_change_pct=Decimal("-8"),
        aum_change_pct=Decimal("-40"),
        maximum_drawdown_change_points=Decimal("5"),
    )

    assessment = assess_selection_quality(
        _leader(win_rate="85", drawdown="10"),
        profile,
        objective=SHORT_TERM_INTRADAY,
        observed_at_ms=now_ms,
        trend=trend,
    )

    stable = assess_selection_quality(
        _leader(win_rate="85", drawdown="10"),
        profile,
        objective=SHORT_TERM_INTRADAY,
        observed_at_ms=now_ms,
        trend=LeaderPerformanceTrend(
            baseline_age_hours=Decimal("24"),
            roi_change_pct=Decimal("0"),
            pnl_change_pct=Decimal("0"),
            aum_change_pct=Decimal("0"),
            maximum_drawdown_change_points=Decimal("0"),
        ),
    )

    assert assessment.eligible
    assert assessment.score < stable.score
    assert "COPY_SELECTION_DRAWDOWN_ACCELERATING" in assessment.warning_codes
    assert "COPY_SELECTION_ROI_DROPPING_FAST" in assessment.warning_codes
    assert "COPY_SELECTION_AUM_DROPPING_FAST" in assessment.warning_codes


def test_three_day_loss_is_a_penalty_not_a_standalone_rejection() -> None:
    pnls = tuple(
        "-40" if index in {0, 6, 12} else "5"
        for index in range(36)
    )
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    profile = CandidateOrderProfile.from_orders(
        _close_orders(pnls),
        observed_at_ms=now_ms,
    )

    assessment = assess_selection_quality(
        _leader(win_rate="85", drawdown="10"),
        profile,
        objective=SHORT_TERM_INTRADAY,
        observed_at_ms=now_ms,
    )

    assert profile.realized_pnl_3d < 0
    assert assessment.eligible
    assert "COPY_SELECTION_RECENT_PNL_NONPOSITIVE" in assessment.warning_codes
