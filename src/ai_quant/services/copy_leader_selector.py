"""Daily Shanghai-time Codex-assisted public leader selection entrypoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess  # nosec B404 -- fixed systemctl path and unit
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ai_quant.common.private_files import read_private_file
from ai_quant.copy_trading.binance_public import (
    BinancePublicCopyClient,
    BinancePublicCopyError,
)
from ai_quant.copy_trading.codex_selection import (
    CandidateOrderProfile,
    CodexDailySelector,
    candidate_document,
)
from ai_quant.copy_trading.leader_slots import CandidateActivity, LeaderSlot, SelectionStrategy
from ai_quant.copy_trading.models import LeaderSnapshot
from ai_quant.copy_trading.one_way import OneWayResolutionError, resolve_one_way_orders
from ai_quant.copy_trading.repository import CopyTradingRepository
from ai_quant.copy_trading.selection import CandidateAssessment, SelectionPolicy, assess_candidate
from ai_quant.copy_trading.selection_quality import (
    LONG_TERM,
    SHORT_TERM_INTRADAY,
    SHORT_TERM_WIN_RATE,
    SelectionQualityAssessment,
    assess_selection_quality,
)
from ai_quant.copy_trading.testnet_catalog import (
    BinanceProductionCatalogClient,
    BinanceTestnetCatalogClient,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CODEX_AUDIT_UNIT = "aiq-copy-codex-audit.service"


@dataclass(frozen=True, slots=True)
class _ProfiledCandidate:
    leader: LeaderSnapshot
    document: dict[str, object]
    activity: CandidateActivity
    assessment: CandidateAssessment
    quality: Mapping[str, SelectionQualityAssessment]


@dataclass(frozen=True, slots=True)
class _CandidateDirectory:
    leaders: tuple[LeaderSnapshot, ...]
    public_total: int
    valid_total: int
    invalid_row_count: int


class _SelectionPoolError(RuntimeError):
    """A shortage with the exact candidate-stage rejection evidence attached."""

    def __init__(self, reason_code: str, rejection_codes: Counter[str]) -> None:
        super().__init__(reason_code)
        self.reason_codes = (
            reason_code,
            *(
                code
                for code, _count in rejection_codes.most_common(10)
                if code != reason_code
            ),
        )


def _trigger_codex_audit() -> bool:
    # Selection and the scheduled hourly audit both run at Shanghai 00:00. A plain
    # start can be coalesced into the already-running audit, which then never sees
    # the selection failure committed a moment later. Restart the read-only audit so
    # the event-triggered run is guaranteed to take a fresh database snapshot.
    try:
        started = subprocess.run(  # noqa: S603  # nosec B603
            ["/usr/bin/systemctl", "restart", "--no-block", _CODEX_AUDIT_UNIT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return started.returncode == 0


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select public Binance copy leaders")
    parser.add_argument("--database-url-file", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--config-file", type=Path, required=True)
    parser.add_argument("--schema-file", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    parser.add_argument("--environment", choices=("TESTNET", "PRODUCTION"), default="TESTNET")
    parser.add_argument("--leader-count", type=int, default=3)
    parser.add_argument("--candidate-pool-size", type=int, default=100)
    parser.add_argument("--review-pool-size", type=int, default=20)
    parser.add_argument(
        "--strategy",
        choices=[strategy.value for strategy in SelectionStrategy],
        required=True,
    )
    return parser.parse_args()


def _private_text(path: Path, repository_root: Path) -> str:
    raw = read_private_file(
        path,
        forbidden_repository_root=repository_root,
        maximum_bytes=4096,
        unsafe_reason="COPY_DATABASE_URL_FILE_UNSAFE",
    )
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ValueError("COPY_DATABASE_URL_INVALID") from error
    if not value or "\n" in value or "\r" in value:
        raise ValueError("COPY_DATABASE_URL_INVALID")
    return value


def _short_win_rate_key(
    item: _ProfiledCandidate,
) -> tuple[int, Decimal, Decimal, int, Decimal]:
    quality = item.quality[SHORT_TERM_WIN_RATE]
    return (
        item.leader.current_copy_count,
        quality.score,
        item.leader.win_rate_pct,
        item.activity.profitable_close_count,
        -item.leader.maximum_drawdown_pct,
    )


def _public_policy(strategy: SelectionStrategy) -> SelectionPolicy:
    return SelectionPolicy(
        minimum_win_rate_pct=(
            Decimal("45") if strategy is SelectionStrategy.LONG_TERM else Decimal("60")
        ),
        maximum_drawdown_pct=(
            Decimal("18") if strategy is SelectionStrategy.LONG_TERM else Decimal("20")
        ),
        minimum_aum_usdt=(
            Decimal("10000") if strategy is SelectionStrategy.LONG_TERM else Decimal("7500")
        ),
        minimum_track_record_days=(30 if strategy is SelectionStrategy.LONG_TERM else 18),
        minimum_current_copy_count=200,
    )


def _objectives(strategy: SelectionStrategy) -> tuple[str, ...]:
    if strategy is SelectionStrategy.LONG_TERM:
        return (LONG_TERM,)
    return (SHORT_TERM_WIN_RATE, SHORT_TERM_INTRADAY)


def _minimum_symbol_compatibility(
    strategy: SelectionStrategy,
    *,
    environment: str,
) -> float:
    """Keep Testnet's smaller catalog from vetoing an otherwise sound long leader."""

    return (
        0.6
        if environment == "TESTNET" and strategy is SelectionStrategy.LONG_TERM
        else 0.8
    )


def _candidate_directory(
    public: BinancePublicCopyClient,
    *,
    strategy: SelectionStrategy,
    candidate_pool_size: int,
    observed_at_ms: int,
) -> _CandidateDirectory:
    directory = public.list_all_leaders(
        time_range="30D",
        data_type="ROI",
        maximum_pages=400,
    )
    if directory.invalid_row_count:
        print(
            json.dumps(
                {
                    "event": "copy_selection_invalid_candidates_skipped",
                    "source": "FULL_PUBLIC_DIRECTORY",
                    "count": directory.invalid_row_count,
                    "reason_codes": list(directory.invalid_reason_codes),
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
    if not directory.leaders:
        raise BinancePublicCopyError("COPY_SELECTION_DIRECTORY_NO_VALID_CANDIDATES")
    policy = _public_policy(strategy)
    directory_assessments = {
        leader.lead_portfolio_id: assess_candidate(
            leader,
            observed_at_ms=observed_at_ms,
            policy=policy,
        )
        for leader in directory.leaders
    }
    leaders = tuple(
        sorted(
            directory.leaders,
            key=lambda leader: (
                directory_assessments[leader.lead_portfolio_id].eligible,
                leader.current_copy_count,
                directory_assessments[leader.lead_portfolio_id].deterministic_score,
            ),
            reverse=True,
        )[:candidate_pool_size]
    )
    return _CandidateDirectory(
        leaders=leaders,
        public_total=directory.total,
        valid_total=len(directory.leaders),
        invalid_row_count=directory.invalid_row_count,
    )


def _document_digest(document: Any) -> str:
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _manual_clear_cooldown_start(now: datetime) -> datetime:
    """Keep a cleared leader out of same-day reruns, but reconsider it next Shanghai day."""

    if now.tzinfo is None:
        raise ValueError("copy selection time must be timezone-aware")
    return now.astimezone(_SHANGHAI).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    ).astimezone(UTC)


def run_selection(arguments: argparse.Namespace) -> dict[str, Any]:
    strategy = SelectionStrategy(arguments.strategy)
    if arguments.leader_count != len(strategy.slots):
        raise ValueError("COPY_SELECTION_SLOT_COUNT_INVALID")
    if not 1 <= arguments.leader_count <= 8:
        raise ValueError("COPY_SELECTION_LEADER_COUNT_INVALID")
    if not 20 <= arguments.candidate_pool_size <= 500:
        raise ValueError("COPY_SELECTION_CANDIDATE_POOL_INVALID")
    if not arguments.leader_count <= arguments.review_pool_size <= 50:
        raise ValueError("COPY_SELECTION_REVIEW_POOL_INVALID")
    now = datetime.now(UTC)
    environment = str(getattr(arguments, "environment", "TESTNET"))
    if environment not in {"TESTNET", "PRODUCTION"}:
        raise ValueError("COPY_SELECTION_EXECUTION_ENVIRONMENT_INVALID")
    public = BinancePublicCopyClient()
    repository = CopyTradingRepository(
        _private_text(arguments.database_url_file, arguments.repository_root)
    )
    current_slots = repository.current_slot_assignments()
    locked_leader_ids = repository.current_locked_leader_ids()
    locked_target_leader_ids = {
        leader_id
        for slot, leader_id in current_slots.items()
        if slot in strategy.slots and leader_id in locked_leader_ids
    }
    recently_cleared = repository.recently_manually_cleared_leader_ids(
        since=_manual_clear_cooldown_start(now)
    )
    excluded_leader_ids = {
        leader_id for slot, leader_id in current_slots.items() if slot not in strategy.slots
    }
    observed_at_ms = int(now.timestamp() * 1000)
    directory = _candidate_directory(
        public,
        strategy=strategy,
        candidate_pool_size=arguments.candidate_pool_size,
        observed_at_ms=observed_at_ms,
    )
    leaders = directory.leaders
    policy = _public_policy(strategy)
    catalog = (
        BinanceProductionCatalogClient()
        if environment == "PRODUCTION"
        else BinanceTestnetCatalogClient()
    )
    execution_trading_symbols = catalog.trading_symbols()
    assessments: dict[str, CandidateAssessment] = {}
    trends = {}
    for leader in leaders:
        repository.record_leader_snapshot(leader, observed_at=now)
        trends[leader.lead_portfolio_id] = repository.leader_selection_trend(
            leader,
            observed_at=now,
        )
        assessments[leader.lead_portfolio_id] = assess_candidate(
            leader,
            observed_at_ms=observed_at_ms,
            policy=policy,
        )
        if leader.lead_portfolio_id in locked_target_leader_ids:
            previous = assessments[leader.lead_portfolio_id]
            assessments[leader.lead_portfolio_id] = CandidateAssessment(
                lead_portfolio_id=previous.lead_portfolio_id,
                eligible=False,
                deterministic_score=previous.deterministic_score,
                reason_codes=("COPY_SELECTION_LOCKED_INCUMBENT_RETAINED",),
            )
        elif leader.lead_portfolio_id in excluded_leader_ids:
            previous = assessments[leader.lead_portfolio_id]
            assessments[leader.lead_portfolio_id] = CandidateAssessment(
                lead_portfolio_id=previous.lead_portfolio_id,
                eligible=False,
                deterministic_score=previous.deterministic_score,
                reason_codes=("COPY_SELECTION_ASSIGNED_TO_OTHER_STRATEGY",),
            )
        elif leader.lead_portfolio_id in recently_cleared:
            previous = assessments[leader.lead_portfolio_id]
            assessments[leader.lead_portfolio_id] = CandidateAssessment(
                lead_portfolio_id=previous.lead_portfolio_id,
                eligible=False,
                deterministic_score=previous.deterministic_score,
                reason_codes=("COPY_SELECTION_MANUAL_CLEAR_COOLDOWN",),
            )
    # Every automatic line first passes the same public-data safety boundary. Objective-
    # specific close-quality gates below then distinguish long-term stability, credible
    # win rate, and intraday suitability without letting raw activity bypass risk controls.
    review_candidates = sorted(
        (
            leader
            for leader in leaders
            if leader.lead_portfolio_id not in excluded_leader_ids
            and assessments[leader.lead_portfolio_id].eligible
        ),
        # Current follower count is the primary ranking signal after public safety
        # gates. The composite score breaks ties and keeps quality visible.
        key=lambda leader: (
            leader.current_copy_count,
            assessments[leader.lead_portfolio_id].deterministic_score,
        ),
        reverse=True,
    )
    profiled: list[_ProfiledCandidate] = []
    profile_rejection_codes: Counter[str] = Counter()
    for leader in review_candidates:
        try:
            history = public.order_history(leader.lead_portfolio_id, page_size=100)
        except BinancePublicCopyError as error:
            previous = assessments[leader.lead_portfolio_id]
            assessments[leader.lead_portfolio_id] = CandidateAssessment(
                lead_portfolio_id=previous.lead_portfolio_id,
                eligible=False,
                deterministic_score=previous.deterministic_score,
                reason_codes=(
                    "COPY_SELECTION_HISTORY_UNAVAILABLE",
                    str(error),
                ),
            )
            profile_rejection_codes.update(assessments[leader.lead_portfolio_id].reason_codes)
            continue
        orders = history.orders
        if any(order.position_side.value == "BOTH" for order in orders):
            try:
                position_history = public.position_history(
                    leader.lead_portfolio_id,
                    page_size=100,
                )
                orders = resolve_one_way_orders(
                    orders,
                    closed_positions=position_history.positions,
                )
            except (BinancePublicCopyError, OneWayResolutionError) as error:
                previous = assessments[leader.lead_portfolio_id]
                assessments[leader.lead_portfolio_id] = CandidateAssessment(
                    lead_portfolio_id=previous.lead_portfolio_id,
                    eligible=False,
                    deterministic_score=previous.deterministic_score,
                    reason_codes=(
                        "COPY_SELECTION_ONE_WAY_EVIDENCE_UNRESOLVED",
                        str(error),
                    ),
                )
                profile_rejection_codes.update(
                    assessments[leader.lead_portfolio_id].reason_codes
                )
                continue
        profile = CandidateOrderProfile.from_orders(orders, observed_at_ms=observed_at_ms)
        if profile.ambiguous_position_side_count:
            previous = assessments[leader.lead_portfolio_id]
            assessments[leader.lead_portfolio_id] = CandidateAssessment(
                lead_portfolio_id=previous.lead_portfolio_id,
                eligible=False,
                deterministic_score=previous.deterministic_score,
                reason_codes=("COPY_SELECTION_POSITION_SIDE_AMBIGUOUS",),
            )
            profile_rejection_codes.update(assessments[leader.lead_portfolio_id].reason_codes)
            continue
        compatible_symbol_count = sum(
            symbol in execution_trading_symbols for symbol in profile.symbols
        )
        compatibility = compatible_symbol_count / len(profile.symbols) if profile.symbols else 0
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
            testnet_symbol_compatibility_pct=int(compatibility * 100),
        )
        repository.record_candidate_activity(activity)
        if compatibility < _minimum_symbol_compatibility(
            strategy,
            environment=environment,
        ):
            previous = assessments[leader.lead_portfolio_id]
            assessments[leader.lead_portfolio_id] = CandidateAssessment(
                lead_portfolio_id=previous.lead_portfolio_id,
                eligible=False,
                deterministic_score=previous.deterministic_score,
                reason_codes=("COPY_SELECTION_EXECUTION_SYMBOL_COMPATIBILITY_LOW",),
            )
            profile_rejection_codes.update(assessments[leader.lead_portfolio_id].reason_codes)
            continue
        objective_quality = {
            objective: assess_selection_quality(
                leader,
                profile,
                objective=objective,
                observed_at_ms=observed_at_ms,
                trend=trends[leader.lead_portfolio_id],
            )
            for objective in _objectives(strategy)
        }
        eligible_quality = tuple(
            quality for quality in objective_quality.values() if quality.eligible
        )
        if not eligible_quality:
            previous = assessments[leader.lead_portfolio_id]
            quality_reasons = tuple(
                dict.fromkeys(
                    reason
                    for quality in objective_quality.values()
                    for reason in quality.reason_codes
                )
            )
            assessments[leader.lead_portfolio_id] = CandidateAssessment(
                lead_portfolio_id=previous.lead_portfolio_id,
                eligible=False,
                deterministic_score=max(quality.score for quality in objective_quality.values()),
                reason_codes=quality_reasons,
            )
            profile_rejection_codes.update(quality_reasons)
            continue
        objective_score = max(quality.score for quality in eligible_quality)
        assessments[leader.lead_portfolio_id] = CandidateAssessment(
            lead_portfolio_id=leader.lead_portfolio_id,
            eligible=True,
            deterministic_score=objective_score,
            reason_codes=(),
        )
        document = candidate_document(
            leader,
            assessments[leader.lead_portfolio_id],
            profile,
            execution_trading_symbols=execution_trading_symbols,
            execution_environment=environment,
        )
        document["objective_quality"] = {
            objective: quality.document() for objective, quality in objective_quality.items()
        }
        trend = trends[leader.lead_portfolio_id]
        document["public_snapshot_trend"] = (
            None if trend is None else trend.document()
        )
        profiled.append(
            _ProfiledCandidate(
                leader=leader,
                document=document,
                activity=activity,
                assessment=assessments[leader.lead_portfolio_id],
                quality=objective_quality,
            )
        )
    selector = CodexDailySelector(
        schema_path=arguments.schema_file,
        work_root=arguments.work_root,
    )
    selected_leader_ids: tuple[str, ...]
    result_document: Mapping[str, Any]
    backup_leader_ids: dict[LeaderSlot, str] = {}
    if strategy is SelectionStrategy.SHORT_TERM:
        current_short_1 = current_slots.get(strategy.slots[0])
        current_short_2 = current_slots.get(strategy.slots[1])
        short_1_reviewed: list[_ProfiledCandidate] = []
        if current_short_1 is not None and current_short_1 in locked_leader_ids:
            short_1_id = current_short_1
            short_1_pool = [
                item
                for item in profiled
                if item.quality[SHORT_TERM_WIN_RATE].eligible
                and item.leader.lead_portfolio_id not in set(current_slots.values())
            ]
            short_1_pool.sort(key=_short_win_rate_key, reverse=True)
            short_1_reviewed = short_1_pool[: arguments.review_pool_size]
            if short_1_reviewed:
                short_1_result = selector.select(
                    tuple(item.document for item in short_1_reviewed),
                    leader_count=1,
                    strategy="SHORT_TERM_WIN_RATE",
                )
                short_1_backup = short_1_result.selected_leader_ids[0]
                backup_leader_ids[strategy.slots[0]] = short_1_backup
                short_1_document: Mapping[str, Any] = {
                    "state": "LOCKED_RETAINED_WITH_BACKUP",
                    "lead_portfolio_id": current_short_1,
                    "backup_lead_portfolio_id": short_1_backup,
                    "backup_codex_review": short_1_result.document,
                    "reason_codes": ["COPY_SELECTION_LOCKED_SLOT_BACKUP_SELECTED"],
                }
            else:
                short_1_document = {
                    "state": "LOCKED_RETAINED_BACKUP_UNAVAILABLE",
                    "lead_portfolio_id": current_short_1,
                    "reason_codes": ["COPY_SELECTION_LOCKED_SLOT_BACKUP_UNAVAILABLE"],
                }
        else:
            short_1_pool = [
                item
                for item in profiled
                if item.leader.lead_portfolio_id != current_short_2
                and item.quality[SHORT_TERM_WIN_RATE].eligible
            ]
            short_1_pool.sort(key=_short_win_rate_key, reverse=True)
            short_1_reviewed = short_1_pool[: arguments.review_pool_size]
            if not short_1_reviewed:
                raise _SelectionPoolError(
                    "COPY_SELECTION_SHORT_WIN_RATE_POOL_INSUFFICIENT",
                    profile_rejection_codes,
                )
            short_1_result = selector.select(
                tuple(item.document for item in short_1_reviewed),
                leader_count=1,
                strategy="SHORT_TERM_WIN_RATE",
            )
            short_1_id = short_1_result.selected_leader_ids[0]
            short_1_document = short_1_result.document
        short_2_reviewed: list[_ProfiledCandidate] = []
        if current_short_2 is not None and current_short_2 in locked_leader_ids:
            short_2_id = current_short_2
            reserved_leader_ids = set(current_slots.values()) | set(
                backup_leader_ids.values()
            )
            short_2_pool = [
                item
                for item in profiled
                if item.quality[SHORT_TERM_INTRADAY].eligible
                and item.leader.lead_portfolio_id not in reserved_leader_ids
            ]
            short_2_pool.sort(
                key=lambda item: (
                    item.leader.current_copy_count,
                    item.quality[SHORT_TERM_INTRADAY].score,
                    item.activity.active_days_7d,
                    item.activity.orders_3d,
                    item.activity.orders_1d,
                    -item.leader.maximum_drawdown_pct,
                ),
                reverse=True,
            )
            short_2_reviewed = short_2_pool[: arguments.review_pool_size]
            if short_2_reviewed:
                short_2_result = selector.select(
                    tuple(item.document for item in short_2_reviewed),
                    leader_count=1,
                    strategy="SHORT_TERM_INTRADAY",
                )
                short_2_backup = short_2_result.selected_leader_ids[0]
                backup_leader_ids[strategy.slots[1]] = short_2_backup
                short_2_document: Mapping[str, Any] = {
                    "state": "LOCKED_RETAINED_WITH_BACKUP",
                    "lead_portfolio_id": current_short_2,
                    "backup_lead_portfolio_id": short_2_backup,
                    "backup_codex_review": short_2_result.document,
                    "reason_codes": ["COPY_SELECTION_LOCKED_SLOT_BACKUP_SELECTED"],
                }
            else:
                short_2_document = {
                    "state": "LOCKED_RETAINED_BACKUP_UNAVAILABLE",
                    "lead_portfolio_id": current_short_2,
                    "reason_codes": ["COPY_SELECTION_LOCKED_SLOT_BACKUP_UNAVAILABLE"],
                }
        else:
            short_2_pool = [
                item
                for item in profiled
                if item.leader.lead_portfolio_id not in {short_1_id, current_short_1}
                and item.quality[SHORT_TERM_INTRADAY].eligible
            ]
            short_2_pool.sort(
                key=lambda item: (
                    item.leader.current_copy_count,
                    item.quality[SHORT_TERM_INTRADAY].score,
                    item.activity.active_days_7d,
                    item.activity.orders_3d,
                    item.activity.orders_1d,
                    -item.leader.maximum_drawdown_pct,
                ),
                reverse=True,
            )
            short_2_reviewed = short_2_pool[: arguments.review_pool_size]
            if not short_2_reviewed:
                raise _SelectionPoolError(
                    "COPY_SELECTION_SHORT_INTRADAY_POOL_INSUFFICIENT",
                    profile_rejection_codes,
                )
            short_2_result = selector.select(
                tuple(item.document for item in short_2_reviewed),
                leader_count=1,
                strategy="SHORT_TERM_INTRADAY",
            )
            short_2_id = short_2_result.selected_leader_ids[0]
            short_2_document = short_2_result.document
        short_1_slot = strategy.slots[0]
        if backup_leader_ids.get(short_1_slot) == short_2_id:
            # An active short-term assignment has priority over an advisory backup.
            # Re-select the locked slot's backup from the remaining unassigned pool.
            short_1_reviewed = [
                item
                for item in short_1_reviewed
                if item.leader.lead_portfolio_id != short_2_id
            ]
            if short_1_reviewed:
                short_1_result = selector.select(
                    tuple(item.document for item in short_1_reviewed),
                    leader_count=1,
                    strategy="SHORT_TERM_WIN_RATE",
                )
                short_1_backup = short_1_result.selected_leader_ids[0]
                backup_leader_ids[short_1_slot] = short_1_backup
                short_1_document = {
                    "state": "LOCKED_RETAINED_WITH_BACKUP",
                    "lead_portfolio_id": current_short_1,
                    "backup_lead_portfolio_id": short_1_backup,
                    "backup_codex_review": short_1_result.document,
                    "reason_codes": ["COPY_SELECTION_LOCKED_SLOT_BACKUP_SELECTED"],
                }
            else:
                backup_leader_ids.pop(short_1_slot, None)
                short_1_document = {
                    "state": "LOCKED_RETAINED_BACKUP_UNAVAILABLE",
                    "lead_portfolio_id": current_short_1,
                    "reason_codes": ["COPY_SELECTION_LOCKED_SLOT_BACKUP_UNAVAILABLE"],
                }
        selected_leader_ids = (
            short_1_id,
            short_2_id,
        )
        result_document = {
            "schema_version": "1.0.0",
            "selection_strategy": "SHORT_TERM_SPLIT",
            "short_term_1": {
                "objective": "FOLLOWER_FIRST_CREDIBLE_WIN_RATE",
                "codex_review": short_1_document,
            },
            "short_term_2": short_2_document,
        }
        candidate_digest = _document_digest(
            {
                "backup_leader_ids": {
                    slot.value: leader_id for slot, leader_id in backup_leader_ids.items()
                },
                "effective_selected_leader_ids": list(selected_leader_ids),
                "locked_target_leader_ids": sorted(locked_target_leader_ids),
                "profiled_candidates": [item.document for item in profiled],
            }
        )
        report_digest = _document_digest(result_document)
        reviewed_by_id = {
            item.leader.lead_portfolio_id: item for item in (*short_1_reviewed, *short_2_reviewed)
        }
        reviewed = list(reviewed_by_id.values())
    else:
        current_long = current_slots.get(strategy.slots[0])
        if current_long is not None and current_long in locked_leader_ids:
            profiled.sort(
                key=lambda item: (
                    item.leader.current_copy_count,
                    item.quality[LONG_TERM].score,
                ),
                reverse=True,
            )
            reviewed = profiled[: arguments.review_pool_size]
            selected_leader_ids = (current_long,)
            if reviewed:
                long_result = selector.select(
                    tuple(item.document for item in reviewed),
                    leader_count=1,
                    strategy=strategy.value,
                )
                long_backup = long_result.selected_leader_ids[0]
                backup_leader_ids[strategy.slots[0]] = long_backup
                result_document = {
                    "state": "LOCKED_RETAINED_WITH_BACKUP",
                    "lead_portfolio_id": current_long,
                    "backup_lead_portfolio_id": long_backup,
                    "backup_codex_review": long_result.document,
                    "reason_codes": ["COPY_SELECTION_LOCKED_SLOT_BACKUP_SELECTED"],
                }
            else:
                result_document = {
                    "state": "LOCKED_RETAINED_BACKUP_UNAVAILABLE",
                    "lead_portfolio_id": current_long,
                    "reason_codes": ["COPY_SELECTION_LOCKED_SLOT_BACKUP_UNAVAILABLE"],
                }
            candidate_digest = _document_digest(
                {
                    "backup_leader_ids": {
                        slot.value: leader_id
                        for slot, leader_id in backup_leader_ids.items()
                    },
                    "effective_selected_leader_ids": list(selected_leader_ids),
                    "locked_target_leader_ids": [current_long],
                    "profiled_candidates": [item.document for item in profiled],
                }
            )
            report_digest = _document_digest(result_document)
        else:
            profiled.sort(
                key=lambda item: (
                    item.leader.current_copy_count,
                    item.quality[LONG_TERM].score,
                ),
                reverse=True,
            )
            reviewed = profiled[: arguments.review_pool_size]
            if len(reviewed) < arguments.leader_count:
                raise _SelectionPoolError(
                    "COPY_SELECTION_ELIGIBLE_POOL_INSUFFICIENT",
                    profile_rejection_codes,
                )
            long_result = selector.select(
                tuple(item.document for item in reviewed),
                leader_count=arguments.leader_count,
                strategy=strategy.value,
            )
            selected_leader_ids = long_result.selected_leader_ids
            result_document = long_result.document
            candidate_digest = long_result.candidate_digest
            report_digest = long_result.report_digest
    if len(set(selected_leader_ids)) != len(selected_leader_ids):
        raise RuntimeError("COPY_SELECTION_DUPLICATE_LEADER")
    if set(selected_leader_ids) & excluded_leader_ids:
        raise RuntimeError("COPY_SELECTION_LEADER_ASSIGNED_TO_OTHER_LINE")
    if len(set(backup_leader_ids.values())) != len(backup_leader_ids):
        raise RuntimeError("COPY_SELECTION_DUPLICATE_BACKUP_LEADER")
    if set(backup_leader_ids.values()) & set(selected_leader_ids):
        raise RuntimeError("COPY_SELECTION_BACKUP_DUPLICATES_ACTIVE_LEADER")
    review_leaders = [item.leader for item in reviewed]
    local_now = now.astimezone(_SHANGHAI)
    if strategy is SelectionStrategy.SHORT_TERM:
        scheduled_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        scheduled_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=(local_now.weekday() + 1) % 7
        )
    policy_digest = hashlib.sha256(arguments.config_file.read_bytes()).hexdigest()
    selection_run_id = repository.apply_slot_selection(
        leaders,
        assessments,
        selected_leader_ids,
        strategy=strategy,
        scheduled_for=scheduled_local.astimezone(UTC),
        data_cutoff=now,
        candidate_digest=candidate_digest,
        policy_digest=policy_digest,
        codex_report_digest=report_digest,
        occurred_at=now,
        backup_leader_ids=backup_leader_ids,
    )
    evidence = {
        "schema_version": "1.0.0",
        "selection_strategy": strategy.value,
        "execution_environment": environment,
        "selection_run_id": selection_run_id,
        "occurred_at": now.isoformat().replace("+00:00", "Z"),
        "candidate_count": len(leaders),
        "directory_public_total": directory.public_total,
        "directory_valid_total": directory.valid_total,
        "directory_invalid_row_count": directory.invalid_row_count,
        "eligible_count": len(profiled),
        "reviewed_count": len(review_leaders),
        "selected_leader_ids": list(selected_leader_ids),
        "candidate_digest": candidate_digest,
        "codex_report_digest": report_digest,
        "codex_decision": result_document,
        "reviewed_candidates": [item.document for item in reviewed],
        "manual_clear_cooldown_leader_ids": sorted(recently_cleared),
        "backup_leader_ids": {
            slot.value: leader_id for slot, leader_id in backup_leader_ids.items()
        },
    }
    arguments.evidence_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = arguments.evidence_file.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(arguments.evidence_file)
    return evidence


def main() -> int:
    arguments = _arguments()
    try:
        evidence = run_selection(arguments)
    except Exception as error:
        now = datetime.now(UTC)
        raw_reason = str(error)
        reason_code = (
            raw_reason
            if re.fullmatch(r"[A-Z0-9_]{3,120}", raw_reason)
            else f"COPY_SELECTION_{type(error).__name__.upper()}"
        )
        reason_codes = (
            error.reason_codes
            if isinstance(error, _SelectionPoolError)
            else (reason_code,)
        )
        try:
            strategy = SelectionStrategy(arguments.strategy)
            local_now = now.astimezone(_SHANGHAI)
            if strategy is SelectionStrategy.SHORT_TERM:
                scheduled = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                scheduled = local_now.replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) - timedelta(days=(local_now.weekday() + 1) % 7)
            repository = CopyTradingRepository(
                _private_text(arguments.database_url_file, arguments.repository_root)
            )
            repository.record_selection_failure(
                scheduled_for=scheduled.astimezone(UTC),
                reason_code=reason_code,
                reason_codes=reason_codes,
                occurred_at=now,
                strategy=strategy,
            )
        except Exception as persistence_error:
            print(
                json.dumps(
                    {
                        "event": "copy_selection_failure_evidence_error",
                        "error_type": type(persistence_error).__name__,
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
        wakeup_started = _trigger_codex_audit()
        print(
            json.dumps(
                {
                    "event": "copy_selection_codex_wakeup",
                    "reason": reason_code,
                    "started": wakeup_started,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        raise
    print(
        json.dumps(
            {
                "selection_run_id": evidence["selection_run_id"],
                "selected_leader_ids": evidence["selected_leader_ids"],
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
