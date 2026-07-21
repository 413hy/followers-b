"""Objective-specific robustness gates and scores for public copy leaders."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ai_quant.copy_trading.codex_selection import CandidateOrderProfile
from ai_quant.copy_trading.models import LeaderSnapshot
from ai_quant.copy_trading.selection import copy_social_proof_score

LONG_TERM = "LONG_TERM"
SHORT_TERM_WIN_RATE = "SHORT_TERM_WIN_RATE"
SHORT_TERM_INTRADAY = "SHORT_TERM_INTRADAY"

_SUPPORTED_OBJECTIVES = frozenset({LONG_TERM, SHORT_TERM_WIN_RATE, SHORT_TERM_INTRADAY})


@dataclass(frozen=True, slots=True)
class SelectionQualityAssessment:
    objective: str
    eligible: bool
    score: Decimal
    reason_codes: tuple[str, ...]
    warning_codes: tuple[str, ...]
    track_record_days: int
    confidence_cap: str
    copy_social_proof_score: Decimal

    def document(self) -> dict[str, object]:
        return {
            "eligible": self.eligible,
            "objective_score": str(self.score),
            "reason_codes": list(self.reason_codes),
            "warning_codes": list(self.warning_codes),
            "track_record_days": self.track_record_days,
            "confidence_cap": self.confidence_cap,
            "copy_social_proof_score": str(self.copy_social_proof_score),
        }


@dataclass(frozen=True, slots=True)
class LeaderPerformanceTrend:
    """Change from a durable prior public snapshot, never inferred from one point."""

    baseline_age_hours: Decimal
    roi_change_pct: Decimal
    pnl_change_pct: Decimal
    aum_change_pct: Decimal
    maximum_drawdown_change_points: Decimal

    def document(self) -> dict[str, str]:
        return {
            "baseline_age_hours": str(self.baseline_age_hours),
            "roi_change_pct": str(self.roi_change_pct),
            "pnl_change_pct": str(self.pnl_change_pct),
            "aum_change_pct": str(self.aum_change_pct),
            "maximum_drawdown_change_points": str(
                self.maximum_drawdown_change_points
            ),
        }


@dataclass(frozen=True, slots=True)
class _QualityPolicy:
    minimum_track_record_days: int
    maximum_drawdown_pct: Decimal
    minimum_nonzero_close_count: int
    minimum_profitable_close_rate_pct: Decimal
    minimum_profit_factor: Decimal
    maximum_top_two_profit_contribution_pct: Decimal
    maximum_consecutive_losing_closes: int
    maximum_profit_factor_before_warning: Decimal = Decimal("50")
    maximum_drawdown_acceleration_points: Decimal = Decimal("3")
    maximum_recent_roi_drop_pct: Decimal = Decimal("5")
    maximum_recent_aum_drop_pct_before_warning: Decimal = Decimal("35")
    minimum_orders_1d: int = 0
    minimum_orders_3d: int = 0
    minimum_orders_7d: int = 0
    minimum_active_days_7d: int = 0
    maximum_operation_age_hours: int | None = None


_POLICIES = {
    LONG_TERM: _QualityPolicy(
        minimum_track_record_days=30,
        maximum_drawdown_pct=Decimal("18"),
        minimum_nonzero_close_count=30,
        minimum_profitable_close_rate_pct=Decimal("55"),
        minimum_profit_factor=Decimal("1.25"),
        maximum_top_two_profit_contribution_pct=Decimal("45"),
        maximum_consecutive_losing_closes=4,
    ),
    SHORT_TERM_WIN_RATE: _QualityPolicy(
        minimum_track_record_days=18,
        maximum_drawdown_pct=Decimal("18"),
        minimum_nonzero_close_count=20,
        minimum_profitable_close_rate_pct=Decimal("75"),
        minimum_profit_factor=Decimal("1.20"),
        maximum_top_two_profit_contribution_pct=Decimal("55"),
        maximum_consecutive_losing_closes=3,
        minimum_orders_7d=10,
        minimum_active_days_7d=3,
        maximum_operation_age_hours=48,
    ),
    SHORT_TERM_INTRADAY: _QualityPolicy(
        minimum_track_record_days=18,
        maximum_drawdown_pct=Decimal("20"),
        minimum_nonzero_close_count=20,
        minimum_profitable_close_rate_pct=Decimal("65"),
        minimum_profit_factor=Decimal("1.20"),
        maximum_top_two_profit_contribution_pct=Decimal("55"),
        maximum_consecutive_losing_closes=3,
        minimum_orders_1d=1,
        minimum_orders_3d=5,
        minimum_orders_7d=14,
        minimum_active_days_7d=3,
        maximum_operation_age_hours=36,
    ),
}


def assess_selection_quality(
    leader: LeaderSnapshot,
    profile: CandidateOrderProfile,
    *,
    objective: str,
    observed_at_ms: int,
    trend: LeaderPerformanceTrend | None = None,
) -> SelectionQualityAssessment:
    if objective not in _SUPPORTED_OBJECTIVES:
        raise ValueError("copy selection quality objective is invalid")
    if observed_at_ms <= 0:
        raise ValueError("copy selection quality observation time is invalid")
    policy = _POLICIES[objective]
    track_record_days = max(0, (observed_at_ms - leader.start_time_ms) // 86_400_000)
    nonzero_closes = profile.profitable_close_count + profile.losing_close_count
    reasons: list[str] = []
    warnings: list[str] = []
    if track_record_days < policy.minimum_track_record_days:
        reasons.append("COPY_SELECTION_TRACK_RECORD_SHORT")
    if leader.maximum_drawdown_pct > policy.maximum_drawdown_pct:
        reasons.append("COPY_SELECTION_DRAWDOWN_HIGH")
    if nonzero_closes < policy.minimum_nonzero_close_count:
        reasons.append("COPY_SELECTION_CLOSE_SAMPLE_SMALL")
    if (
        profile.profitable_close_rate_pct is None
        or profile.profitable_close_rate_pct < policy.minimum_profitable_close_rate_pct
    ):
        reasons.append("COPY_SELECTION_CLOSE_QUALITY_LOW")
    if profile.profit_factor is not None and profile.profit_factor < policy.minimum_profit_factor:
        reasons.append("COPY_SELECTION_PROFIT_FACTOR_LOW")
    if (
        profile.top_two_profit_contribution_pct is None
        or profile.top_two_profit_contribution_pct > policy.maximum_top_two_profit_contribution_pct
    ):
        reasons.append("COPY_SELECTION_PROFIT_CONCENTRATED")
    if profile.robust_realized_pnl_ex_largest_profit <= 0:
        reasons.append("COPY_SELECTION_ROBUST_PNL_NONPOSITIVE")
    if profile.maximum_consecutive_losing_closes > policy.maximum_consecutive_losing_closes:
        reasons.append("COPY_SELECTION_LOSS_STREAK_HIGH")
    if objective in {SHORT_TERM_WIN_RATE, SHORT_TERM_INTRADAY} and profile.realized_pnl_3d <= 0:
        warnings.append("COPY_SELECTION_RECENT_PNL_NONPOSITIVE")
    if (
        profile.orders_1d < policy.minimum_orders_1d
        or profile.orders_3d < policy.minimum_orders_3d
        or profile.orders_7d < policy.minimum_orders_7d
        or profile.active_days_7d < policy.minimum_active_days_7d
        or _operation_is_stale(
            profile,
            observed_at_ms=observed_at_ms,
            maximum_age_hours=policy.maximum_operation_age_hours,
        )
    ):
        reasons.append("COPY_SELECTION_ACTIVITY_LOW")
    if trend is not None:
        if (
            trend.maximum_drawdown_change_points
            >= policy.maximum_drawdown_acceleration_points
        ):
            warnings.append("COPY_SELECTION_DRAWDOWN_ACCELERATING")
        if trend.roi_change_pct <= -policy.maximum_recent_roi_drop_pct:
            warnings.append("COPY_SELECTION_ROI_DROPPING_FAST")
        if trend.aum_change_pct <= -policy.maximum_recent_aum_drop_pct_before_warning:
            warnings.append("COPY_SELECTION_AUM_DROPPING_FAST")
    if (
        profile.profit_factor is None
        and profile.gross_profit > 0
        and profile.gross_loss == 0
    ) or (
        profile.profit_factor is not None
        and profile.profit_factor > policy.maximum_profit_factor_before_warning
    ):
        warnings.append("COPY_SELECTION_PROFIT_FACTOR_ANOMALOUS")
    if track_record_days < 45:
        warnings.append("COPY_SELECTION_TRACK_RECORD_MATURING")
    confidence_cap = _confidence_cap(
        track_record_days=track_record_days,
        warning_codes=warnings,
        trend=trend,
    )
    return SelectionQualityAssessment(
        objective=objective,
        eligible=not reasons,
        score=_selection_score(
            leader,
            profile,
            objective=objective,
            track_record_days=track_record_days,
            trend=trend,
        ),
        reason_codes=tuple(dict.fromkeys(reasons)),
        warning_codes=tuple(dict.fromkeys(warnings)),
        track_record_days=track_record_days,
        confidence_cap=confidence_cap,
        copy_social_proof_score=copy_social_proof_score(leader.current_copy_count),
    )


def _operation_is_stale(
    profile: CandidateOrderProfile,
    *,
    observed_at_ms: int,
    maximum_age_hours: int | None,
) -> bool:
    if maximum_age_hours is None:
        return False
    if profile.latest_operation_time_ms is None:
        return True
    return profile.latest_operation_time_ms < observed_at_ms - maximum_age_hours * 3_600_000


def _selection_score(
    leader: LeaderSnapshot,
    profile: CandidateOrderProfile,
    *,
    objective: str,
    track_record_days: int,
    trend: LeaderPerformanceTrend | None,
) -> Decimal:
    close_rate = profile.profitable_close_rate_pct or Decimal("0")
    drawdown = Decimal("100") - _bounded(leader.maximum_drawdown_pct)
    concentration = Decimal("100") - _bounded(
        profile.top_two_profit_contribution_pct or Decimal("100")
    )
    profit_factor = _profit_factor_score(profile)
    sample_size = _bounded(
        Decimal(profile.profitable_close_count + profile.losing_close_count)
        / Decimal("40")
        * Decimal("100")
    )
    track_record = _bounded(Decimal(track_record_days) / Decimal("90") * Decimal("100"))
    social_proof = copy_social_proof_score(leader.current_copy_count)
    if objective == LONG_TERM:
        score = (
            drawdown * Decimal("0.22")
            + close_rate * Decimal("0.15")
            + profit_factor * Decimal("0.13")
            + concentration * Decimal("0.18")
            + track_record * Decimal("0.13")
            + sample_size * Decimal("0.09")
            + social_proof * Decimal("0.10")
        )
    elif objective == SHORT_TERM_WIN_RATE:
        score = (
            _bounded(leader.win_rate_pct) * Decimal("0.28")
            + close_rate * Decimal("0.23")
            + drawdown * Decimal("0.14")
            + concentration * Decimal("0.13")
            + profit_factor * Decimal("0.09")
            + sample_size * Decimal("0.05")
            + social_proof * Decimal("0.08")
        )
    else:
        recent_health = _recent_health_score(profile)
        activity = _activity_score(profile)
        score = (
            close_rate * Decimal("0.14")
            + _bounded(leader.win_rate_pct) * Decimal("0.07")
            + drawdown * Decimal("0.20")
            + concentration * Decimal("0.18")
            + profit_factor * Decimal("0.10")
            + recent_health * Decimal("0.08")
            + track_record * Decimal("0.07")
            + activity * Decimal("0.09")
            + social_proof * Decimal("0.07")
        )
    score -= _trend_penalty(trend)
    if objective in {SHORT_TERM_WIN_RATE, SHORT_TERM_INTRADAY}:
        score -= _recent_performance_penalty(profile)
    return score.quantize(Decimal("0.000001"))


def _profit_factor_score(profile: CandidateOrderProfile) -> Decimal:
    value = profile.profit_factor
    if value is None:
        no_observed_losses = profile.gross_profit > 0 and profile.gross_loss == 0
        return Decimal("70") if no_observed_losses else Decimal("0")
    if value <= Decimal("1"):
        return _bounded(value * Decimal("30"))
    if value <= Decimal("5"):
        return Decimal("30") + (value - Decimal("1")) / Decimal("4") * Decimal("70")
    if value <= Decimal("20"):
        return Decimal("100") - (value - Decimal("5")) / Decimal("15") * Decimal("15")
    if value <= Decimal("50"):
        return Decimal("85") - (value - Decimal("20")) / Decimal("30") * Decimal("15")
    return Decimal("60")


def _recent_health_score(profile: CandidateOrderProfile) -> Decimal:
    if profile.realized_pnl_3d <= 0:
        return Decimal("0")
    if profile.realized_pnl_1d < 0:
        return Decimal("35")
    if profile.realized_pnl_1d == 0:
        return Decimal("65")
    return Decimal("100")


def _recent_performance_penalty(profile: CandidateOrderProfile) -> Decimal:
    """Down-rank a soft patch without treating it as proof of lasting poor quality."""

    penalty = Decimal("0")
    if profile.realized_pnl_3d <= 0:
        penalty += Decimal("6")
    if profile.realized_pnl_1d < 0:
        penalty += Decimal("2")
    return penalty


def _trend_penalty(trend: LeaderPerformanceTrend | None) -> Decimal:
    if trend is None:
        return Decimal("3")
    penalty = Decimal("0")
    if trend.roi_change_pct < 0:
        penalty += min(Decimal("12"), -trend.roi_change_pct * Decimal("0.6"))
    if trend.maximum_drawdown_change_points > 0:
        penalty += min(
            Decimal("12"),
            trend.maximum_drawdown_change_points * Decimal("2"),
        )
    if trend.aum_change_pct < Decimal("-20"):
        penalty += min(
            Decimal("5"),
            (-trend.aum_change_pct - Decimal("20")) * Decimal("0.1"),
        )
    return penalty


def _confidence_cap(
    *,
    track_record_days: int,
    warning_codes: list[str],
    trend: LeaderPerformanceTrend | None,
) -> str:
    if track_record_days < 21 or trend is None:
        return "LOW"
    if track_record_days < 45 or warning_codes:
        return "MEDIUM"
    return "HIGH"


def _activity_score(profile: CandidateOrderProfile) -> Decimal:
    return (
        _bounded(Decimal(profile.orders_1d) / Decimal("8") * Decimal("100")) * Decimal("0.35")
        + _bounded(Decimal(profile.orders_3d) / Decimal("24") * Decimal("100")) * Decimal("0.25")
        + _bounded(Decimal(profile.orders_7d) / Decimal("60") * Decimal("100")) * Decimal("0.20")
        + _bounded(Decimal(profile.active_days_7d) / Decimal("7") * Decimal("100"))
        * Decimal("0.20")
    )


def _bounded(value: Decimal) -> Decimal:
    return min(Decimal("100"), max(Decimal("0"), value))
