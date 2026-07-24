"""Exercise copy-trading guarantees against a freshly migrated PostgreSQL database."""

from __future__ import annotations

import argparse
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg

from ai_quant.copy_trading.execution import (
    CopyOrderType,
    SubmissionEvent,
    copy_client_order_id,
)
from ai_quant.copy_trading.health import PostgresHealthStore
from ai_quant.copy_trading.leader_slots import CandidateActivity, LeaderSlot, SelectionStrategy
from ai_quant.copy_trading.ledger import VirtualPosition, VirtualPositionKey
from ai_quant.copy_trading.models import (
    LeaderSnapshot,
    PositionSide,
    PublicLeaderOrder,
    SourcePositionSide,
)
from ai_quant.copy_trading.postgres import PostgresSubmissionJournal
from ai_quant.copy_trading.repository import AccountPositionMark, CopyTradingRepository
from ai_quant.copy_trading.selection import CandidateAssessment
from ai_quant.copy_trading.telegram_state import PostgresTelegramState
from ai_quant.notifications.telegram_bot import ControlAction

LEADER_ID = "5000000000000000001"
NOW = datetime.now(UTC).replace(microsecond=0)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url-file", type=Path, required=True)
    return parser.parse_args()


def _order(
    *,
    leader_id: str = LEADER_ID,
    order_time_ms: int,
    update_time_ms: int,
    quantity: str,
    side: str = "BUY",
    average_price: str = "2000",
) -> PublicLeaderOrder:
    return PublicLeaderOrder.from_api(
        leader_id,
        {
            "symbol": "ETHUSDT",
            "positionSide": "LONG",
            "side": side,
            "type": "MARKET",
            "executedQty": quantity,
            "avgPrice": average_price,
            "totalPnl": "0",
            "orderTime": order_time_ms,
            "orderUpdateTime": update_time_ms,
        },
    )


def _claim(journal: PostgresSubmissionJournal, signal_id: str) -> bool:
    event = SubmissionEvent(
        event_id=hashlib.sha256(f"submitting:{signal_id}".encode()).hexdigest(),
        signal_id=signal_id,
        state="SUBMITTING",
        filled_quantity=Decimal("0"),
        exchange_order_id=None,
        response_hash=None,
        reason_codes=(),
        occurred_at=NOW,
    )
    return journal.claim(
        signal_id=signal_id,
        client_order_id=f"aqc-{signal_id[:28]}",
        request_hash="a" * 64,
        request_hash_version=2,
        requested_quantity=Decimal("0.001"),
        leverage=20,
        order_type=CopyOrderType.MARKET,
        limit_price=None,
        expires_at=None,
        claimed_at=NOW,
        submitting_event=event,
    )


def main() -> int:
    arguments = _arguments()
    dsn = (
        arguments.database_url_file.read_text(encoding="utf-8")
        .strip()
        .replace("postgresql+psycopg://", "postgresql://", 1)
    )
    repository = CopyTradingRepository(dsn)
    telegram = PostgresTelegramState(dsn)

    repository.record_account_valuation(
        exchange_wallet_balance_usdt=Decimal("5000"),
        exchange_margin_balance_usdt=Decimal("5000"),
        exchange_available_balance_usdt=Decimal("5000"),
        envelope_baseline_usdt=Decimal("5000"),
        operating_envelope_usdt=Decimal("150"),
        total_initial_margin_usdt=Decimal("0"),
        total_maintenance_margin_usdt=Decimal("0"),
        observed_at=NOW,
    )
    capital_reset_at = NOW + timedelta(seconds=1)
    capital_reset_id = repository.reset_pnl_baseline(
        actor_id="integration-test",
        occurred_at=capital_reset_at,
    )
    assert len(capital_reset_id) == 64
    assert repository.ensure_envelope_baseline(
        exchange_margin_balance_usdt=Decimal("4900"),
        operating_envelope_usdt=Decimal("150"),
        occurred_at=capital_reset_at + timedelta(seconds=1),
    ) == Decimal("5000")
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT event_type,operating_envelope_usdt,exchange_margin_balance_usdt,
                   reason_codes
              FROM copytrading.account_envelope_events
             ORDER BY occurred_at DESC,envelope_event_id DESC LIMIT 1
            """
        )
        assert cursor.fetchone() == (
            "RESET",
            Decimal("150"),
            Decimal("5000"),
            ["COPY_ACCOUNT_ENVELOPE_RESET"],
        )

    source_leader = LeaderSnapshot.from_api(
        {
            "leadPortfolioId": LEADER_ID,
            "nickname": "source integration leader",
            "roi": "15",
            "pnl": "500",
            "aum": "50000",
            "mdd": "4",
            "winRate": "75",
            "currentCopyCount": 1,
            "maxCopyCount": 100,
            "startTime": 1_700_000_000_000,
            "portfolioType": "PUBLIC",
        }
    )
    repository.record_leader_snapshot(source_leader, observed_at=NOW)
    repository.record_candidate_activity(
        CandidateActivity(
            lead_portfolio_id=source_leader.lead_portfolio_id,
            observed_at=NOW,
            sample_order_count=20,
            orders_1d=5,
            orders_3d=12,
            orders_7d=20,
            active_days_7d=4,
            latest_operation_time_ms=int(NOW.timestamp() * 1000),
            profitable_close_count=10,
            losing_close_count=2,
            testnet_symbol_compatibility_pct=100,
        )
    )
    source_proposal = telegram.create_leader_change(
        user_id=42,
        slot=LeaderSlot.SHORT_TERM_1,
        lead_portfolio_id=source_leader.lead_portfolio_id,
    )
    assert (
        telegram.execute_leader_change_confirmed(
            user_id=42,
            nonce=source_proposal.nonce,
        )
        is not None
    )
    multiplier_proposal = telegram.create_follow_multiplier_change(
        user_id=42,
        lead_portfolio_id=source_leader.lead_portfolio_id,
        multiplier=3,
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        multiplier_changes = tuple(
            executor.map(
                lambda _: telegram.execute_follow_multiplier_confirmed(
                    user_id=42,
                    nonce=multiplier_proposal.nonce,
                ),
                range(8),
            )
        )
    assert sum(result is not None for result in multiplier_changes) == 1
    source_assignment = next(
        item
        for item in repository.active_assignments()
        if item.lead_portfolio_id == source_leader.lead_portfolio_id
    )
    assert source_assignment.follow_multiplier == 3
    assert telegram.leader_multiplier_choices()[0].current_multiplier == 3
    availability_at = datetime.now(UTC) + timedelta(seconds=1)
    assert (
        repository.record_leader_availability(
            slot=LeaderSlot.SHORT_TERM_1,
            lead_portfolio_id=source_leader.lead_portfolio_id,
            state="AVAILABLE",
            source_status="ACTIVE",
            observed_at=availability_at,
        )
        is False
    )
    missing_at = availability_at + timedelta(seconds=1)
    with ThreadPoolExecutor(max_workers=8) as executor:
        missing_alerts = tuple(
            executor.map(
                lambda _: repository.record_leader_availability(
                    slot=LeaderSlot.SHORT_TERM_1,
                    lead_portfolio_id=source_leader.lead_portfolio_id,
                    state="MISSING",
                    source_status="NOT_FOUND",
                    observed_at=missing_at,
                ),
                range(8),
            )
        )
    assert missing_alerts.count(True) == 1
    assert missing_alerts.count(False) == 7
    recovered_availability_at = missing_at + timedelta(seconds=1)
    assert (
        repository.record_leader_availability(
            slot=LeaderSlot.SHORT_TERM_1,
            lead_portfolio_id=source_leader.lead_portfolio_id,
            state="AVAILABLE",
            source_status="ACTIVE",
            observed_at=recovered_availability_at,
        )
        is True
    )
    assert repository.record_leader_availability(
        slot=LeaderSlot.SHORT_TERM_1,
        lead_portfolio_id=source_leader.lead_portfolio_id,
        state="MISSING",
        source_status="NOT_FOUND",
        observed_at=recovered_availability_at + timedelta(seconds=1),
    )
    assert (
        repository.record_leader_availability(
            slot=LeaderSlot.LONG_TERM,
            lead_portfolio_id=source_leader.lead_portfolio_id,
            state="MISSING",
            source_status="NOT_FOUND",
            observed_at=recovered_availability_at + timedelta(seconds=2),
        )
        is False
    )
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM copytrading.leader_availability_events "
            "WHERE slot='SHORT_TERM_1' AND lead_portfolio_id=%s",
            (source_leader.lead_portfolio_id,),
        )
        # Eight simultaneous identical missing observations collapse to one
        # append-only event, while recovery starts a new alert episode.
        assert cursor.fetchone() == (4,)
        cursor.execute(
            "SELECT count(*) FROM control.outbox "
            "WHERE payload->>'event'='copy_leader_availability_alert' "
            "AND payload->>'lead_portfolio_id'=%s",
            (source_leader.lead_portfolio_id,),
        )
        assert cursor.fetchone() == (2,)
        cursor.execute(
            "SELECT count(*) FROM control.outbox "
            "WHERE payload->>'event'='copy_leader_availability_recovered' "
            "AND payload->>'lead_portfolio_id'=%s",
            (source_leader.lead_portfolio_id,),
        )
        assert cursor.fetchone() == (1,)
    stale_multiplier_proposal = telegram.create_follow_multiplier_change(
        user_id=42,
        lead_portfolio_id=source_leader.lead_portfolio_id,
        multiplier=4,
    )
    trade_at = datetime.now(UTC)

    baseline = _order(order_time_ms=100, update_time_ms=100, quantity="1")
    assert repository.ingest_orders(LEADER_ID, (baseline,), baseline=True, observed_at=NOW) == ()

    fresh = _order(order_time_ms=100, update_time_ms=200, quantity="2")
    fresh_signals = repository.ingest_orders(LEADER_ID, (fresh,), baseline=False, observed_at=NOW)
    assert len(fresh_signals) == 1
    assert fresh_signals[0].source_delta_quantity == Decimal("1")

    ambiguous_leader_id = "5000000000000000004"
    ambiguous_baseline = PublicLeaderOrder.from_api(
        ambiguous_leader_id,
        {
            "symbol": "HYPEUSDT",
            "positionSide": "BOTH",
            "side": "SELL",
            "type": "LIMIT",
            "executedQty": "8.56",
            "avgPrice": "58.917",
            "totalPnl": "4.71656",
            "orderTime": 500,
            "orderUpdateTime": 500,
        },
    )
    assert (
        repository.ingest_orders(
            ambiguous_leader_id,
            (ambiguous_baseline,),
            baseline=True,
            observed_at=NOW,
        )
        == ()
    )
    repository.record_poll(
        ambiguous_leader_id,
        state="SUCCEEDED",
        row_count=2,
        maximum_update_time_ms=600,
        reason_codes=("COPY_BASELINE_ORDER_IDENTITY_AMBIGUITY_FENCED",),
        occurred_at=NOW + timedelta(milliseconds=500),
    )
    assert repository.source_watermark(ambiguous_leader_id) == 600
    resolved_baseline = ambiguous_baseline.resolve_position_side(
        position_side=SourcePositionSide.LONG,
    )
    assert (
        repository.ingest_orders(
            ambiguous_leader_id,
            (resolved_baseline,),
            baseline=False,
            observed_at=NOW + timedelta(seconds=1),
        )
        == ()
    )
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT position_side,is_baseline
              FROM copytrading.source_order_events
             WHERE lead_portfolio_id=%s
             ORDER BY position_side
            """,
            (ambiguous_leader_id,),
        )
        assert cursor.fetchall() == [("BOTH", True), ("LONG", True)]
        cursor.execute(
            "SELECT count(*) FROM copytrading.signals WHERE lead_portfolio_id=%s",
            (ambiguous_leader_id,),
        )
        assert cursor.fetchone() == (0,)

    other_leader_id = "5000000000000000003"
    other_baseline = _order(
        leader_id=other_leader_id,
        order_time_ms=100,
        update_time_ms=100,
        quantity="1",
    )
    assert (
        repository.ingest_orders(
            other_leader_id,
            (other_baseline,),
            baseline=True,
            observed_at=NOW,
        )
        == ()
    )
    other_fresh = _order(
        leader_id=other_leader_id,
        order_time_ms=100,
        update_time_ms=200,
        quantity="2",
    )
    other_signals = repository.ingest_orders(
        other_leader_id,
        (other_fresh,),
        baseline=False,
        observed_at=NOW,
    )
    assert len(other_signals) == 1
    assert fresh.identity_key != other_fresh.identity_key
    assert fresh.event_key != other_fresh.event_key
    assert fresh_signals[0].signal_id != other_signals[0].signal_id
    assert copy_client_order_id(fresh_signals[0].signal_id) != copy_client_order_id(
        other_signals[0].signal_id
    )

    late_unknown = _order(order_time_ms=150, update_time_ms=150, quantity="4")
    regression = _order(order_time_ms=100, update_time_ms=300, quantity="1.5")
    rebound = _order(order_time_ms=100, update_time_ms=400, quantity="2")
    assert (
        repository.ingest_orders(LEADER_ID, (late_unknown,), baseline=False, observed_at=NOW) == ()
    )
    assert repository.ingest_orders(LEADER_ID, (regression,), baseline=False, observed_at=NOW) == ()
    assert repository.ingest_orders(LEADER_ID, (rebound,), baseline=False, observed_at=NOW) == ()
    assert repository.source_watermark(LEADER_ID) == 400

    reset_leader_id = "5000000000000000004"
    reset_baseline = _order(
        leader_id=reset_leader_id,
        order_time_ms=100,
        update_time_ms=100,
        quantity="1",
    )
    assert (
        repository.ingest_orders(
            reset_leader_id,
            (reset_baseline,),
            baseline=True,
            observed_at=NOW,
        )
        == ()
    )
    old_resolution = repository.source_orders_for_resolution(reset_leader_id)
    reset_id = repository.reset_source_resolution(
        reset_leader_id,
        reason_codes=("COPY_TEST_SOURCE_RESOLUTION_RESET",),
        occurred_at=NOW + timedelta(seconds=1),
    )
    assert len(reset_id) == 64
    assert repository.source_watermark(reset_leader_id) is None
    assert repository.source_orders_for_resolution(reset_leader_id) == ()
    assert (
        repository.ingest_orders(
            reset_leader_id,
            (reset_baseline,),
            baseline=True,
            observed_at=NOW + timedelta(seconds=2),
        )
        == ()
    )
    new_resolution = repository.source_orders_for_resolution(reset_leader_id)
    assert len(new_resolution) == 1
    assert new_resolution[0].identity_key != old_resolution[0].identity_key
    assert repository.source_watermark(reset_leader_id) == 100

    journal = PostgresSubmissionJournal(dsn)
    signal_id = fresh_signals[0].signal_id
    with ThreadPoolExecutor(max_workers=8) as executor:
        claims = tuple(executor.map(lambda _: _claim(journal, signal_id), range(8)))
    assert claims.count(True) == 1
    assert claims.count(False) == 7
    assert journal.lookup(signal_id=signal_id) is not None
    retry_at = NOW + timedelta(minutes=2)
    journal.record(
        SubmissionEvent(
            event_id=hashlib.sha256(f"retry-submitting:{signal_id}".encode()).hexdigest(),
            signal_id=signal_id,
            state="SUBMITTING",
            filled_quantity=Decimal("0"),
            exchange_order_id=None,
            response_hash=None,
            reason_codes=(
                "COPY_PERSISTENT_ENTRY_RESUBMITTED_AFTER_CONFIRMED_ABSENCE",
            ),
            occurred_at=retry_at,
        )
    )
    retried_claim = journal.lookup(signal_id=signal_id)
    assert retried_claim is not None
    assert retried_claim.claimed_at == retry_at
    assert _claim(journal, other_signals[0].signal_id) is True

    long_incumbent = LeaderSnapshot.from_api(
        {
            "leadPortfolioId": other_leader_id,
            "nickname": "long incumbent",
            "roi": "18",
            "pnl": "800",
            "aum": "90000",
            "mdd": "6",
            "winRate": "74",
            "currentCopyCount": 1,
            "maxCopyCount": 100,
            "startTime": 1_700_000_000_000,
            "portfolioType": "PUBLIC",
        }
    )
    repository.record_leader_snapshot(long_incumbent, observed_at=NOW)
    repository.record_candidate_activity(
        CandidateActivity(
            lead_portfolio_id=other_leader_id,
            observed_at=NOW,
            sample_order_count=20,
            orders_1d=5,
            orders_3d=12,
            orders_7d=20,
            active_days_7d=4,
            latest_operation_time_ms=int(NOW.timestamp() * 1000),
            profitable_close_count=10,
            losing_close_count=2,
            testnet_symbol_compatibility_pct=100,
        )
    )
    long_proposal = telegram.create_leader_change(
        user_id=42,
        slot=LeaderSlot.LONG_TERM,
        lead_portfolio_id=other_leader_id,
    )
    assert telegram.execute_leader_change_confirmed(
        user_id=42,
        nonce=long_proposal.nonce,
    )
    # Slot attribution is event-time based; all simulated fills must occur after
    # the manual incumbent assignment created above.
    trade_at = datetime.now(UTC)
    other_key = VirtualPositionKey(other_leader_id, "ETHUSDT", PositionSide.LONG)
    other_open = VirtualPosition(
        key=other_key,
        local_quantity=Decimal("0.02"),
        observed_source_quantity=Decimal("1"),
    )
    repository.record_virtual_position(
        other_signals[0],
        previous=VirtualPosition(key=other_key),
        updated=other_open,
        reference_price=Decimal("2000"),
        leverage=20,
        occurred_at=trade_at,
    )
    usage_at_ten = repository.portfolio_usage(
        lead_portfolio_id=other_leader_id,
        symbol="ETHUSDT",
        account_equity_usdt=Decimal("150"),
        account_available_balance_usdt=Decimal("150"),
        current_symbol_leverage=10,
    )
    assert usage_at_ten.total_committed_margin_usdt == Decimal("4")
    assert usage_at_ten.leader_committed_margin_usdt == Decimal("4")
    assert usage_at_ten.symbol_committed_margin_usdt == Decimal("4")
    usage_at_forty = repository.portfolio_usage(
        lead_portfolio_id=other_leader_id,
        symbol="ETHUSDT",
        account_equity_usdt=Decimal("150"),
        account_available_balance_usdt=Decimal("150"),
        current_symbol_leverage=40,
    )
    assert usage_at_forty.symbol_committed_margin_usdt == Decimal("1")
    long_candidate = LeaderSnapshot.from_api(
        {
            "leadPortfolioId": "5000000000000000004",
            "nickname": "long candidate",
            "roi": "24",
            "pnl": "1200",
            "aum": "120000",
            "mdd": "3",
            "winRate": "82",
            "currentCopyCount": 1,
            "maxCopyCount": 100,
            "startTime": 1_700_000_000_000,
            "portfolioType": "PUBLIC",
        }
    )
    repository.record_leader_snapshot(long_candidate, observed_at=NOW)
    long_assessment = CandidateAssessment(
        lead_portfolio_id=long_candidate.lead_portfolio_id,
        eligible=True,
        deterministic_score=Decimal("90"),
        reason_codes=(),
    )
    replacement_requested_at = trade_at + timedelta(minutes=1)
    lock_proposal = telegram.create_leader_lock_change(
        user_id=42,
        lead_portfolio_id=other_leader_id,
        locked=True,
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        lock_changes = tuple(
            executor.map(
                lambda _: telegram.execute_leader_lock_confirmed(
                    user_id=42,
                    nonce=lock_proposal.nonce,
                ),
                range(8),
            )
        )
    assert sum(result is not None for result in lock_changes) == 1
    assert any(
        choice.lead_portfolio_id == other_leader_id and choice.locked
        for choice in telegram.leader_lock_choices()
    )
    locked_selection_run_id = repository.apply_slot_selection(
        (long_candidate,),
        {long_candidate.lead_portfolio_id: long_assessment},
        (long_candidate.lead_portfolio_id,),
        strategy=SelectionStrategy.LONG_TERM,
        scheduled_for=NOW,
        data_cutoff=replacement_requested_at,
        candidate_digest="a" * 64,
        policy_digest="b" * 64,
        codex_report_digest="c" * 64,
        occurred_at=replacement_requested_at,
    )
    assert repository.current_slot_assignments()[LeaderSlot.LONG_TERM] == other_leader_id
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT outcome,reason_codes FROM copytrading.selection_decisions "
            "WHERE selection_run_id=%s AND lead_portfolio_id=%s",
            (locked_selection_run_id, long_candidate.lead_portfolio_id),
        )
        assert cursor.fetchone() == (
            "REJECTED",
            ["COPY_SLOT_REPLACEMENT_BLOCKED_BY_LEADER_LOCK"],
        )
        cursor.execute(
            "SELECT count(*) FROM copytrading.slot_replacement_events "
            "WHERE selection_run_id=%s",
            (locked_selection_run_id,),
        )
        assert cursor.fetchone()[0] == 0
    backup_selection_run_id = repository.apply_slot_selection(
        (long_candidate,),
        {long_candidate.lead_portfolio_id: long_assessment},
        (other_leader_id,),
        strategy=SelectionStrategy.LONG_TERM,
        scheduled_for=NOW,
        data_cutoff=replacement_requested_at,
        candidate_digest="d" * 64,
        policy_digest="e" * 64,
        codex_report_digest="f" * 64,
        occurred_at=replacement_requested_at,
        backup_leader_ids={LeaderSlot.LONG_TERM: long_candidate.lead_portfolio_id},
    )
    assert repository.current_slot_assignments()[LeaderSlot.LONG_TERM] == other_leader_id
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT incumbent_lead_portfolio_id,backup_lead_portfolio_id "
            "FROM copytrading.leader_slot_backup_events WHERE selection_run_id=%s",
            (backup_selection_run_id,),
        )
        assert cursor.fetchone() == (other_leader_id, long_candidate.lead_portfolio_id)
    unlock_proposal = telegram.create_leader_lock_change(
        user_id=42,
        lead_portfolio_id=other_leader_id,
        locked=False,
    )
    assert telegram.execute_leader_lock_confirmed(
        user_id=42,
        nonce=unlock_proposal.nonce,
    )
    repository.apply_slot_selection(
        (long_candidate,),
        {long_candidate.lead_portfolio_id: long_assessment},
        (long_candidate.lead_portfolio_id,),
        strategy=SelectionStrategy.LONG_TERM,
        scheduled_for=NOW + timedelta(days=1),
        data_cutoff=replacement_requested_at,
        candidate_digest="1" * 64,
        policy_digest="2" * 64,
        codex_report_digest="3" * 64,
        occurred_at=replacement_requested_at,
    )
    assert repository.current_slot_assignments()[LeaderSlot.LONG_TERM] == other_leader_id
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT state,expires_at-requested_at FROM "
            "copytrading.slot_replacement_events WHERE state='REQUESTED'"
        )
        assert cursor.fetchone() == ("REQUESTED", timedelta(days=1))
    source_closed_at = replacement_requested_at + timedelta(hours=1)
    source_closed_at_ms = int(source_closed_at.timestamp() * 1000)
    other_close = _order(
        leader_id=other_leader_id,
        order_time_ms=source_closed_at_ms,
        update_time_ms=source_closed_at_ms,
        quantity="1",
        side="SELL",
    )
    recovered_at = replacement_requested_at + timedelta(days=2)
    other_close_signals = repository.ingest_orders(
        other_leader_id,
        (other_close,),
        baseline=False,
        observed_at=recovered_at,
    )
    assert len(other_close_signals) == 1
    repository.record_virtual_position(
        other_close_signals[0],
        previous=other_open,
        updated=VirtualPosition(key=other_key),
        reference_price=Decimal("2000"),
        leverage=20,
        occurred_at=recovered_at,
    )
    assert repository.reconcile_pending_slot_replacements(occurred_at=recovered_at) == 1
    assert (
        repository.current_slot_assignments()[LeaderSlot.LONG_TERM]
        == long_candidate.lead_portfolio_id
    )

    candidate_baseline = _order(
        leader_id=long_candidate.lead_portfolio_id,
        order_time_ms=600,
        update_time_ms=600,
        quantity="1",
    )
    assert (
        repository.ingest_orders(
            long_candidate.lead_portfolio_id,
            (candidate_baseline,),
            baseline=True,
            observed_at=recovered_at + timedelta(hours=1),
        )
        == ()
    )
    candidate_fresh = _order(
        leader_id=long_candidate.lead_portfolio_id,
        order_time_ms=600,
        update_time_ms=700,
        quantity="2",
    )
    candidate_signals = repository.ingest_orders(
        long_candidate.lead_portfolio_id,
        (candidate_fresh,),
        baseline=False,
        observed_at=recovered_at + timedelta(hours=1),
    )
    candidate_key = VirtualPositionKey(
        long_candidate.lead_portfolio_id,
        "ETHUSDT",
        PositionSide.LONG,
    )
    candidate_open = VirtualPosition(
        key=candidate_key,
        local_quantity=Decimal("0.01"),
        observed_source_quantity=Decimal("1"),
    )
    repository.record_virtual_position(
        candidate_signals[0],
        previous=VirtualPosition(key=candidate_key),
        updated=candidate_open,
        reference_price=Decimal("2000"),
        leverage=20,
        occurred_at=recovered_at + timedelta(hours=1),
    )
    leader_close_proposal = telegram.create_leader_positions_close(
        user_id=42,
        lead_portfolio_id=long_candidate.lead_portfolio_id,
    )
    assert "确认清空该带单员全部仓位" in leader_close_proposal.confirmation_text
    assert "ETHUSDT · 多单 · 数量 0.01" in leader_close_proposal.confirmation_text
    with ThreadPoolExecutor(max_workers=8) as executor:
        leader_close_results = tuple(
            executor.map(
                lambda _: telegram.execute_leader_positions_close_confirmed(
                    user_id=42,
                    nonce=leader_close_proposal.nonce,
                ),
                range(8),
            )
        )
    assert sum(result is not None for result in leader_close_results) == 1
    leader_close_result = next(result for result in leader_close_results if result is not None)
    assert "新建清理任务: 1 个" in leader_close_result
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT jsonb_array_length(consumption.signal_ids),count(signal.signal_id)
              FROM copytrading.telegram_leader_position_close_consumptions AS consumption
              CROSS JOIN LATERAL jsonb_array_elements_text(consumption.signal_ids)
                AS target(signal_id)
              JOIN copytrading.signals AS signal ON signal.signal_id=target.signal_id
             WHERE signal.lead_portfolio_id=%s AND signal.signal_origin='CONTROL'
             GROUP BY consumption.signal_ids
            """,
            (long_candidate.lead_portfolio_id,),
        )
        assert cursor.fetchone() == (1, 1)
    expired_candidate = LeaderSnapshot.from_api(
        {
            "leadPortfolioId": "5000000000000000005",
            "nickname": "expired replacement candidate",
            "roi": "30",
            "pnl": "1500",
            "aum": "130000",
            "mdd": "2",
            "winRate": "85",
            "currentCopyCount": 1,
            "maxCopyCount": 100,
            "startTime": 1_700_000_000_000,
            "portfolioType": "PUBLIC",
        }
    )
    repository.record_leader_snapshot(expired_candidate, observed_at=NOW)
    expired_assessment = CandidateAssessment(
        lead_portfolio_id=expired_candidate.lead_portfolio_id,
        eligible=True,
        deterministic_score=Decimal("95"),
        reason_codes=(),
    )
    expiry_request_at = recovered_at + timedelta(hours=2)
    repository.apply_slot_selection(
        (expired_candidate,),
        {expired_candidate.lead_portfolio_id: expired_assessment},
        (expired_candidate.lead_portfolio_id,),
        strategy=SelectionStrategy.LONG_TERM,
        scheduled_for=NOW + timedelta(days=7),
        data_cutoff=expiry_request_at,
        candidate_digest="4" * 64,
        policy_digest="5" * 64,
        codex_report_digest="6" * 64,
        occurred_at=expiry_request_at,
    )
    pending_lock = telegram.create_leader_lock_change(
        user_id=42,
        lead_portfolio_id=long_candidate.lead_portfolio_id,
        locked=True,
    )
    assert telegram.execute_leader_lock_confirmed(user_id=42, nonce=pending_lock.nonce)
    assert repository.reconcile_pending_slot_replacements(
        occurred_at=expiry_request_at + timedelta(seconds=1)
    ) == 1
    pending_unlock = telegram.create_leader_lock_change(
        user_id=42,
        lead_portfolio_id=long_candidate.lead_portfolio_id,
        locked=False,
    )
    assert telegram.execute_leader_lock_confirmed(user_id=42, nonce=pending_unlock.nonce)
    superseding_candidate = LeaderSnapshot.from_api(
        {
            "leadPortfolioId": "5000000000000000006",
            "nickname": "superseding replacement candidate",
            "roi": "28",
            "pnl": "1400",
            "aum": "125000",
            "mdd": "2.5",
            "winRate": "84",
            "currentCopyCount": 1,
            "maxCopyCount": 100,
            "startTime": 1_700_000_000_000,
            "portfolioType": "PUBLIC",
        }
    )
    repository.record_leader_snapshot(superseding_candidate, observed_at=NOW)
    superseding_assessment = CandidateAssessment(
        lead_portfolio_id=superseding_candidate.lead_portfolio_id,
        eligible=True,
        deterministic_score=Decimal("94"),
        reason_codes=(),
    )
    superseding_request_at = expiry_request_at + timedelta(minutes=1)
    repository.apply_slot_selection(
        (superseding_candidate,),
        {superseding_candidate.lead_portfolio_id: superseding_assessment},
        (superseding_candidate.lead_portfolio_id,),
        strategy=SelectionStrategy.LONG_TERM,
        scheduled_for=NOW + timedelta(days=14),
        data_cutoff=superseding_request_at,
        candidate_digest="7" * 64,
        policy_digest="8" * 64,
        codex_report_digest="9" * 64,
        occurred_at=superseding_request_at,
    )
    assert (
        repository.reconcile_pending_slot_replacements(
            occurred_at=superseding_request_at + timedelta(days=1, seconds=1)
        )
        == 1
    )
    assert (
        repository.current_slot_assignments()[LeaderSlot.LONG_TERM]
        == long_candidate.lead_portfolio_id
    )
    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM copytrading.slot_replacement_events WHERE state='APPLIED'"
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "SELECT count(*) FROM copytrading.slot_replacement_events WHERE state='EXPIRED'"
        )
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "SELECT count(*) FROM copytrading.slot_replacement_events WHERE state='SUPERSEDED'"
        )
        assert cursor.fetchone()[0] == 1
    candidate_close = _order(
        leader_id=long_candidate.lead_portfolio_id,
        order_time_ms=800,
        update_time_ms=800,
        quantity="1",
        side="SELL",
    )
    candidate_close_signals = repository.ingest_orders(
        long_candidate.lead_portfolio_id,
        (candidate_close,),
        baseline=False,
        observed_at=expiry_request_at + timedelta(days=1, minutes=1),
    )
    repository.record_virtual_position(
        candidate_close_signals[0],
        previous=candidate_open,
        updated=VirtualPosition(key=candidate_key),
        reference_price=Decimal("2000"),
        leverage=20,
        occurred_at=expiry_request_at + timedelta(days=1, minutes=1),
    )

    key = VirtualPositionKey(LEADER_ID, "ETHUSDT", PositionSide.LONG)
    opened = VirtualPosition(
        key=key,
        local_quantity=Decimal("0.01"),
        observed_source_quantity=Decimal("1"),
    )
    repository.record_virtual_position(
        fresh_signals[0],
        previous=VirtualPosition(key=key),
        updated=opened,
        reference_price=Decimal("2000"),
        leverage=20,
        occurred_at=trade_at,
    )
    healthy_valuation_id = repository.record_account_valuation(
        exchange_wallet_balance_usdt=Decimal("5000"),
        exchange_margin_balance_usdt=Decimal("5000.5"),
        exchange_available_balance_usdt=Decimal("4999"),
        envelope_baseline_usdt=Decimal("5000"),
        operating_envelope_usdt=Decimal("150"),
        total_initial_margin_usdt=Decimal("1"),
        total_maintenance_margin_usdt=Decimal("0.1"),
        position_marks=(
            AccountPositionMark(
                symbol="ETHUSDT",
                position_side=PositionSide.LONG,
                exchange_quantity=Decimal("0.01"),
                mark_price=Decimal("2050"),
            ),
        ),
        observed_at=trade_at + timedelta(seconds=1),
    )
    assert (
        repository.enforce_leader_symbol_stops(
            valuation_event_id=healthy_valuation_id,
            position_marks=(
                AccountPositionMark(
                    symbol="ETHUSDT",
                    position_side=PositionSide.LONG,
                    exchange_quantity=Decimal("0.01"),
                    mark_price=Decimal("2050"),
                ),
            ),
            occurred_at=trade_at + timedelta(seconds=1),
        )
        == ()
    )
    stop_at = trade_at + timedelta(seconds=1, milliseconds=100)
    stop_marks = (
        AccountPositionMark(
            symbol="ETHUSDT",
            position_side=PositionSide.LONG,
            exchange_quantity=Decimal("0.01"),
            mark_price=Decimal("500"),
        ),
    )
    stop_valuation_id = repository.record_account_valuation(
        exchange_wallet_balance_usdt=Decimal("5000"),
        exchange_margin_balance_usdt=Decimal("4985"),
        exchange_available_balance_usdt=Decimal("4984"),
        envelope_baseline_usdt=Decimal("5000"),
        operating_envelope_usdt=Decimal("150"),
        total_initial_margin_usdt=Decimal("1"),
        total_maintenance_margin_usdt=Decimal("0.1"),
        position_marks=stop_marks,
        observed_at=stop_at,
    )
    active_stops = repository.enforce_leader_symbol_stops(
        valuation_event_id=stop_valuation_id,
        position_marks=stop_marks,
        occurred_at=stop_at,
    )
    assert len(active_stops) == 1
    assert active_stops[0].lead_portfolio_id == LEADER_ID
    assert active_stops[0].symbol == "ETHUSDT"
    assert active_stops[0].net_position_pnl_usdt == Decimal("-15")
    assert active_stops[0].blocked_until == stop_at + timedelta(hours=24)
    assert active_stops[0].newly_triggered is True
    active_stop_read = repository.active_leader_symbol_stop(
        lead_portfolio_id=LEADER_ID,
        symbol="ETHUSDT",
        occurred_at=stop_at,
    )
    assert active_stop_read is not None
    assert active_stop_read.stop_event_id == active_stops[0].stop_event_id
    stop_signals = repository.recoverable_leader_symbol_stop_signals(
        active_stops[0].stop_event_id
    )
    assert len(stop_signals) == 1
    assert stop_signals[0].lead_portfolio_id == LEADER_ID
    assert stop_signals[0].symbol == "ETHUSDT"
    assert stop_signals[0].position_side is PositionSide.LONG
    assert (
        repository.active_leader_symbol_stop(
            lead_portfolio_id=other_leader_id,
            symbol="ETHUSDT",
            occurred_at=stop_at,
        )
        is None
    )
    close_order = _order(
        order_time_ms=500,
        update_time_ms=500,
        quantity="1",
        side="SELL",
        average_price="2100",
    )
    close_signals = repository.ingest_orders(
        LEADER_ID,
        (close_order,),
        baseline=False,
        observed_at=trade_at + timedelta(seconds=2),
    )
    assert len(close_signals) == 1
    repository.record_virtual_position(
        close_signals[0],
        previous=opened,
        updated=VirtualPosition(key=key),
        reference_price=Decimal("2100"),
        leverage=20,
        occurred_at=trade_at + timedelta(seconds=2),
    )
    repository.record_account_valuation(
        exchange_wallet_balance_usdt=Decimal("5001"),
        exchange_margin_balance_usdt=Decimal("5001"),
        exchange_available_balance_usdt=Decimal("5001"),
        envelope_baseline_usdt=Decimal("5000"),
        operating_envelope_usdt=Decimal("150"),
        total_initial_margin_usdt=Decimal("0"),
        total_maintenance_margin_usdt=Decimal("0"),
        observed_at=trade_at + timedelta(seconds=3),
    )
    telegram.record_update(
        {"update_id": 7, "message": {"text": "/status"}},
        chat_id=42,
        user_id=42,
        authorized=True,
        processed_at=NOW,
    )
    telegram.record_update(
        {"update_id": 7, "message": {"text": "/status"}},
        chat_id=42,
        user_id=42,
        authorized=True,
        processed_at=NOW,
    )
    assert telegram.next_offset() == 8

    control_nonce = telegram.create(user_id=42, action=ControlAction.PAUSE_NEW_ENTRIES)
    with ThreadPoolExecutor(max_workers=8) as executor:
        controls = tuple(
            executor.map(
                lambda _: telegram.execute_confirmed(user_id=42, nonce=control_nonce),
                range(8),
            )
        )
    assert sum(result is not None for result in controls) == 1

    leader = LeaderSnapshot.from_api(
        {
            "leadPortfolioId": "5000000000000000002",
            "nickname": "database integration leader",
            "roi": "20",
            "pnl": "1000",
            "aum": "100000",
            "mdd": "5",
            "winRate": "80",
            "currentCopyCount": 1,
            "maxCopyCount": 100,
            "startTime": 1_700_000_000_000,
            "portfolioType": "PUBLIC",
        }
    )
    repository.record_leader_snapshot(leader, observed_at=NOW)
    repository.record_candidate_activity(
        CandidateActivity(
            lead_portfolio_id=leader.lead_portfolio_id,
            observed_at=NOW,
            sample_order_count=20,
            orders_1d=5,
            orders_3d=12,
            orders_7d=20,
            active_days_7d=4,
            latest_operation_time_ms=int(NOW.timestamp() * 1000),
            profitable_close_count=10,
            losing_close_count=2,
            testnet_symbol_compatibility_pct=100,
        )
    )
    leader_proposal = telegram.create_leader_change(
        user_id=42,
        slot=LeaderSlot.SHORT_TERM_1,
        lead_portfolio_id=leader.lead_portfolio_id,
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        changes = tuple(
            executor.map(
                lambda _: telegram.execute_leader_change_confirmed(
                    user_id=42,
                    nonce=leader_proposal.nonce,
                ),
                range(8),
            )
        )
    assert sum(result is not None for result in changes) == 1
    assert (
        telegram.execute_follow_multiplier_confirmed(
            user_id=42,
            nonce=stale_multiplier_proposal.nonce,
        )
        is None
    )
    replacement_assignment = next(
        item
        for item in repository.active_assignments()
        if item.lead_portfolio_id == leader.lead_portfolio_id
    )
    assert replacement_assignment.follow_multiplier == 1
    multiplier_choices = telegram.leader_multiplier_choices()
    multiplier_by_id = {
        item.lead_portfolio_id: item.current_multiplier for item in multiplier_choices
    }
    assert multiplier_by_id == {
        long_candidate.lead_portfolio_id: 1,
        leader.lead_portfolio_id: 1,
    }
    assert leader.lead_portfolio_id in telegram.leader_management_text()
    assert telegram.render("status")
    pnl_view = telegram.render("pnl")
    assert "累计净盈亏: +1 U" in pnl_view, pnl_view
    assert "手续费/资金费等净调整: +0 U" in pnl_view, pnl_view
    assert "database integration leader\nID: 5000000000000000002" in pnl_view, pnl_view
    assert "本线今日: +1 U\n本线本月: +1 U\n本线累计: +1 U" in pnl_view, pnl_view
    assert "名称未知" not in pnl_view, pnl_view
    assert "自定义 7" not in pnl_view, pnl_view
    assert "累计毛盈亏: +1 U" in telegram.render_leader_pnl(LEADER_ID)
    assert tuple(item.lead_portfolio_id for item in telegram.pnl_leader_choices())[:2] == (
        long_candidate.lead_portfolio_id,
        leader.lead_portfolio_id,
    )
    notifications = telegram.claim_notifications(limit=100)
    assert len(notifications) >= 20, [item.text for item in notifications]
    availability_notifications = tuple(
        item
        for item in notifications
        if "当前槽位的带单项目已不可用" in item.text
    )
    assert len(availability_notifications) == 2
    assert all(item.contextual_view is None for item in availability_notifications)
    assert all("未清空或替换槽位" in item.text for item in availability_notifications)
    recovered_notifications = tuple(
        item for item in notifications if "带单项目状态已确认正常" in item.text
    )
    assert len(recovered_notifications) == 1
    assert recovered_notifications[0].contextual_view is None
    assert "槽位、订单和仓位从未被自动更改" in recovered_notifications[0].text
    assert any("source integration leader" in item.text for item in notifications)
    assert any("database integration leader" in item.text for item in notifications)
    assert any("3倍" in item.text for item in notifications)
    assert any("等待平仓" in item.text for item in notifications)
    assert any("替换成功" in item.text for item in notifications)
    assert any("本轮取消更换" in item.text for item in notifications)
    assert any("已锁定" in item.text for item in notifications)
    assert any("已解锁" in item.text for item in notifications)
    assert any("本轮自动换人已取消" in item.text for item in notifications)
    assert any("交易资金净值已恢复为 150 U" in item.text for item in notifications)
    assert any("现有仓位合计浮盈亏: -15 U" in item.text for item in notifications)
    assert any("其他带单员、其他币种均不受影响" in item.text for item in notifications)
    for notification in notifications:
        telegram.complete_notification(notification.message_id, delivered=True)

    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT realized_pnl_usdt,unrealized_pnl_usdt,total_pnl_usdt
              FROM copytrading.leader_valuation_events
             WHERE lead_portfolio_id=%s
             ORDER BY observed_at DESC,leader_valuation_event_id DESC LIMIT 1
            """,
            (LEADER_ID,),
        )
        assert cursor.fetchone() == (Decimal("1"), Decimal("0"), Decimal("1"))
        cursor.execute(
            """
            SELECT realized_pnl_usdt,unrealized_pnl_usdt,total_pnl_usdt
              FROM copytrading.line_valuation_events
             WHERE slot='SHORT_TERM_1'
             ORDER BY observed_at DESC,line_valuation_event_id DESC LIMIT 1
            """,
            (),
        )
        assert cursor.fetchone() == (Decimal("1"), Decimal("0"), Decimal("1"))
        try:
            cursor.execute(
                "UPDATE copytrading.source_order_events SET total_pnl=1 WHERE event_key=%s",
                (baseline.event_key,),
            )
        except psycopg.Error:
            connection.rollback()
        else:
            raise AssertionError("append-only source event mutation unexpectedly succeeded")
        try:
            cursor.execute(
                "UPDATE copytrading.leader_follow_multiplier_events "
                "SET multiplier=10 WHERE lead_portfolio_id=%s",
                (LEADER_ID,),
            )
        except psycopg.Error:
            connection.rollback()
        else:
            raise AssertionError("append-only multiplier mutation unexpectedly succeeded")
        try:
            cursor.execute(
                "UPDATE copytrading.slot_replacement_events "
                "SET state='SUPERSEDED' WHERE state='REQUESTED'"
            )
        except psycopg.Error:
            connection.rollback()
        else:
            raise AssertionError("append-only replacement mutation unexpectedly succeeded")

    custom_proposal = telegram.create_leader_change(
        user_id=42,
        slot=LeaderSlot.CUSTOM_1,
        lead_portfolio_id=other_leader_id,
    )
    assert telegram.execute_leader_change_confirmed(
        user_id=42,
        nonce=custom_proposal.nonce,
    )
    repository.apply_slot_selection(
        (long_candidate,),
        {long_candidate.lead_portfolio_id: long_assessment},
        (long_candidate.lead_portfolio_id,),
        strategy=SelectionStrategy.LONG_TERM,
        scheduled_for=NOW + timedelta(days=21),
        data_cutoff=recovered_at + timedelta(days=2),
        candidate_digest="a" * 64,
        policy_digest="b" * 64,
        codex_report_digest="c" * 64,
        occurred_at=recovered_at + timedelta(days=2),
    )
    final_slots = repository.current_slot_assignments()
    assert final_slots[LeaderSlot.LONG_TERM] == long_candidate.lead_portfolio_id
    assert final_slots[LeaderSlot.CUSTOM_1] == other_leader_id

    facts = PostgresHealthStore(dsn).read_facts()
    # Optional owner slots do not turn the required three-slot health invariant
    # into a false failure, and automatic selection cannot overwrite them.
    assert facts.assigned_slots == 2
    assert facts.history_gap_failures == 0
    assert facts.overdue_slot_replacements == 0
    print("copy repository and Telegram PostgreSQL guarantees PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
