"""Deterministic candidate filtering before the daily Codex review."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ai_quant.copy_trading.models import LeaderSnapshot


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    minimum_win_rate_pct: Decimal = Decimal("55")
    maximum_drawdown_pct: Decimal = Decimal("35")
    minimum_aum_usdt: Decimal = Decimal("10000")
    minimum_track_record_days: int = 7

    def __post_init__(self) -> None:
        if not Decimal("0") <= self.minimum_win_rate_pct <= Decimal("100"):
            raise ValueError("copy minimum win rate is invalid")
        if not Decimal("0") <= self.maximum_drawdown_pct <= Decimal("100"):
            raise ValueError("copy maximum drawdown is invalid")
        if self.minimum_aum_usdt <= 0 or not 1 <= self.minimum_track_record_days <= 3650:
            raise ValueError("copy selection policy is invalid")


@dataclass(frozen=True, slots=True)
class CandidateAssessment:
    lead_portfolio_id: str
    eligible: bool
    deterministic_score: Decimal
    reason_codes: tuple[str, ...]


def assess_candidate(
    leader: LeaderSnapshot,
    *,
    observed_at_ms: int,
    policy: SelectionPolicy | None = None,
) -> CandidateAssessment:
    effective = policy or SelectionPolicy()
    reasons: list[str] = []
    track_record_days = max(0, (observed_at_ms - leader.start_time_ms) // 86_400_000)
    if leader.win_rate_pct < effective.minimum_win_rate_pct:
        reasons.append("COPY_SELECTION_WIN_RATE_LOW")
    if leader.maximum_drawdown_pct > effective.maximum_drawdown_pct:
        reasons.append("COPY_SELECTION_DRAWDOWN_HIGH")
    if leader.aum_usdt < effective.minimum_aum_usdt:
        reasons.append("COPY_SELECTION_AUM_LOW")
    if leader.roi_pct <= 0 or leader.pnl_usdt <= 0:
        reasons.append("COPY_SELECTION_RETURN_NONPOSITIVE")
    if track_record_days < effective.minimum_track_record_days:
        reasons.append("COPY_SELECTION_TRACK_RECORD_SHORT")

    roi_component = min(max(leader.roi_pct, Decimal("0")), Decimal("500")) / Decimal("5")
    drawdown_component = Decimal("100") - leader.maximum_drawdown_pct
    aum_component = min(
        Decimal("100"),
        (leader.aum_usdt / Decimal("100000")) * Decimal("100"),
    )
    track_component = min(
        Decimal("100"),
        (Decimal(track_record_days) / Decimal("90")) * Decimal("100"),
    )
    social_proof_component = copy_social_proof_score(leader.current_copy_count)
    score = (
        leader.win_rate_pct * Decimal("0.32")
        + drawdown_component * Decimal("0.28")
        + roi_component * Decimal("0.18")
        + aum_component * Decimal("0.07")
        + track_component * Decimal("0.05")
        + social_proof_component * Decimal("0.10")
    ).quantize(Decimal("0.000001"))
    return CandidateAssessment(
        lead_portfolio_id=leader.lead_portfolio_id,
        eligible=not reasons,
        deterministic_score=score,
        reason_codes=tuple(reasons),
    )


def copy_social_proof_score(current_copy_count: int) -> Decimal:
    """Return bounded, diminishing social proof from the current copier count."""

    if current_copy_count < 0:
        raise ValueError("copy follower count is invalid")
    # Six hundred current copiers is already strong social proof. A square-root curve
    # recognizes smaller established followings without allowing a saturated quota to
    # dominate drawdown, close quality, or recent performance.
    return min(
        Decimal("100"),
        Decimal(current_copy_count).sqrt() / Decimal("600").sqrt() * Decimal("100"),
    ).quantize(Decimal("0.000001"))
