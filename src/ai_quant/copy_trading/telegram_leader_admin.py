"""Live public-data validation for Telegram leader additions and searches."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from ai_quant.copy_trading.binance_public import (
    BinancePublicCopyClient,
    BinancePublicCopyError,
)
from ai_quant.copy_trading.codex_selection import CandidateOrderProfile
from ai_quant.copy_trading.leader_slots import CandidateActivity, LeaderSlot, is_custom_slot
from ai_quant.copy_trading.models import LeaderSnapshot
from ai_quant.copy_trading.one_way import OneWayResolutionError, resolve_one_way_orders
from ai_quant.copy_trading.repository import CopyTradingRepository
from ai_quant.copy_trading.telegram_format import compact_decimal
from ai_quant.copy_trading.telegram_state import PostgresTelegramState
from ai_quant.copy_trading.testnet_catalog import (
    BinanceProductionCatalogClient,
    BinanceTestnetCatalogClient,
)
from ai_quant.notifications.telegram_bot import (
    FollowMultiplierProposal,
    LeaderCandidateChoice,
    LeaderChangeProposal,
    LeaderLockChoice,
    LeaderLockProposal,
    LeaderMultiplierChoice,
)


class ManualLeaderEvidenceError(RuntimeError):
    """A manually supplied leader did not have unambiguous public evidence."""


class LiveTelegramLeaderAdmin:
    """Delegate slot writes to PostgreSQL after refreshing arbitrary public leaders."""

    def __init__(
        self,
        *,
        state: PostgresTelegramState,
        repository: CopyTradingRepository,
        public: BinancePublicCopyClient | None = None,
        catalog: BinanceTestnetCatalogClient | BinanceProductionCatalogClient | None = None,
        execution_environment: str = "TESTNET",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._state = state
        self._repository = repository
        self._public = public or BinancePublicCopyClient()
        if execution_environment not in {"TESTNET", "PRODUCTION"}:
            raise ValueError("copy Telegram execution environment is invalid")
        self._environment = execution_environment
        self._environment_label = "正式盘" if execution_environment == "PRODUCTION" else "测试盘"
        self._catalog = catalog or (
            BinanceProductionCatalogClient()
            if execution_environment == "PRODUCTION"
            else BinanceTestnetCatalogClient()
        )
        self._clock = clock
        self._execution_symbols: frozenset[str] | None = None

    def leader_management_text(self) -> str:
        return self._state.leader_management_text()

    def leader_candidates(
        self,
        *,
        slot: LeaderSlot,
    ) -> tuple[LeaderCandidateChoice, ...]:
        return self._state.leader_candidates(slot=slot)

    def leader_multiplier_text(self) -> str:
        return self._state.leader_multiplier_text()

    def leader_multiplier_choices(self) -> tuple[LeaderMultiplierChoice, ...]:
        return self._state.leader_multiplier_choices()

    def leader_lock_text(self) -> str:
        return self._state.leader_lock_text()

    def leader_lock_choices(self) -> tuple[LeaderLockChoice, ...]:
        return self._state.leader_lock_choices()

    def create_leader_lock_change(
        self,
        *,
        user_id: int,
        lead_portfolio_id: str,
        locked: bool,
    ) -> LeaderLockProposal:
        return self._state.create_leader_lock_change(
            user_id=user_id,
            lead_portfolio_id=lead_portfolio_id,
            locked=locked,
        )

    def execute_leader_lock_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None:
        return self._state.execute_leader_lock_confirmed(user_id=user_id, nonce=nonce)

    def create_follow_multiplier_change(
        self,
        *,
        user_id: int,
        lead_portfolio_id: str,
        multiplier: int,
    ) -> FollowMultiplierProposal:
        return self._state.create_follow_multiplier_change(
            user_id=user_id,
            lead_portfolio_id=lead_portfolio_id,
            multiplier=multiplier,
        )

    def execute_follow_multiplier_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None:
        return self._state.execute_follow_multiplier_confirmed(
            user_id=user_id,
            nonce=nonce,
        )

    def create_leader_change(
        self,
        *,
        user_id: int,
        slot: LeaderSlot,
        lead_portfolio_id: str | None,
    ) -> LeaderChangeProposal:
        return self._state.create_leader_change(
            user_id=user_id,
            slot=slot,
            lead_portfolio_id=lead_portfolio_id,
        )

    def execute_leader_change_confirmed(
        self,
        *,
        user_id: int,
        nonce: str,
    ) -> str | None:
        return self._state.execute_leader_change_confirmed(user_id=user_id, nonce=nonce)

    def create_external_leader_change(
        self,
        *,
        user_id: int,
        slot: LeaderSlot,
        lead_portfolio_id: str,
    ) -> LeaderChangeProposal:
        leader = self._public.find_leader(lead_portfolio_id)
        # An owner-supplied ID is an explicit assignment choice, not an automatic
        # candidate recommendation. Historical one-way rows may be directionally
        # incomplete, but they are only used to establish a baseline and must not
        # prevent the leader from being assigned.
        self._refresh_evidence(leader, allow_ambiguous_direction=True)
        return self._state.create_leader_change(
            user_id=user_id,
            slot=slot,
            lead_portfolio_id=leader.lead_portfolio_id,
            manual_override=True,
        )

    def search_external_leaders(
        self,
        *,
        slot: LeaderSlot,
        nickname_query: str,
    ) -> tuple[LeaderCandidateChoice, ...]:
        choices: list[LeaderCandidateChoice] = []
        for leader in self._public.search_leaders(nickname_query):
            try:
                activity = self._refresh_evidence(
                    leader,
                    allow_ambiguous_direction=is_custom_slot(slot),
                )
            except (BinancePublicCopyError, ManualLeaderEvidenceError):
                continue
            if not is_custom_slot(slot) and activity.testnet_symbol_compatibility_pct < 80:
                continue
            if (
                slot is not LeaderSlot.LONG_TERM
                and not is_custom_slot(slot)
                and not activity.eligible_for_short_term(
                    observed_at_ms=int(activity.observed_at.timestamp() * 1000)
                )
            ):
                continue
            choices.append(_choice(leader, activity, environment_label=self._environment_label))
        return tuple(choices)

    def _refresh_evidence(
        self,
        leader: LeaderSnapshot,
        *,
        allow_ambiguous_direction: bool = False,
    ) -> CandidateActivity:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("copy manual leader clock must be timezone-aware")
        now = now.astimezone(UTC)
        # Binance's public history has no stable source order ID. Some leaders place
        # same-millisecond ladder orders, so a compact recent window avoids unrelated
        # historical identity collisions while preserving the fail-closed guard for
        # genuinely ambiguous new operations.
        history = self._public.order_history(
            leader.lead_portfolio_id,
            page_size=100,
        )
        orders = history.orders
        if any(order.position_side.value == "BOTH" for order in orders):
            try:
                positions = self._public.position_history(
                    leader.lead_portfolio_id,
                    page_size=100,
                )
                orders = resolve_one_way_orders(
                    orders,
                    closed_positions=positions.positions,
                )
            except (BinancePublicCopyError, OneWayResolutionError) as error:
                if not allow_ambiguous_direction:
                    raise ManualLeaderEvidenceError(
                        "COPY_MANUAL_LEADER_ONE_WAY_EVIDENCE_UNRESOLVED"
                    ) from error
        profile = CandidateOrderProfile.from_orders(
            orders,
            observed_at_ms=int(now.timestamp() * 1000),
        )
        if profile.ambiguous_position_side_count and not allow_ambiguous_direction:
            raise ManualLeaderEvidenceError("COPY_MANUAL_LEADER_POSITION_SIDE_AMBIGUOUS")
        if self._execution_symbols is None:
            self._execution_symbols = self._catalog.trading_symbols()
        compatible_count = sum(symbol in self._execution_symbols for symbol in profile.symbols)
        compatibility = int(compatible_count / len(profile.symbols) * 100) if profile.symbols else 0
        activity = CandidateActivity(
            lead_portfolio_id=leader.lead_portfolio_id,
            observed_at=now,
            sample_order_count=profile.order_count,
            orders_1d=profile.orders_1d,
            orders_3d=profile.orders_3d,
            orders_7d=profile.orders_7d,
            active_days_7d=profile.active_days_7d,
            latest_operation_time_ms=profile.latest_operation_time_ms,
            profitable_close_count=profile.profitable_close_count,
            losing_close_count=profile.losing_close_count,
            testnet_symbol_compatibility_pct=compatibility,
        )
        self._repository.record_leader_snapshot(leader, observed_at=now)
        self._repository.record_candidate_activity(activity)
        return activity


def _choice(
    leader: LeaderSnapshot,
    activity: CandidateActivity,
    *,
    environment_label: str,
) -> LeaderCandidateChoice:
    nickname = "".join(
        character for character in leader.nickname if character.isprintable() and character != "\n"
    )[:20]
    return LeaderCandidateChoice(
        lead_portfolio_id=leader.lead_portfolio_id,
        button_label=f"{nickname[:16]} · {activity.orders_1d}/天",
        summary=(
            f"• {nickname} ({leader.lead_portfolio_id})\n"
            f"  1/3/7天 {activity.orders_1d}/{activity.orders_3d}/{activity.orders_7d} 次 | "
            f"胜率 {compact_decimal(leader.win_rate_pct)}% | "
            f"回撤 {compact_decimal(leader.maximum_drawdown_pct)}% | "
            f"{environment_label}兼容 {activity.testnet_symbol_compatibility_pct}%"
        ),
    )
