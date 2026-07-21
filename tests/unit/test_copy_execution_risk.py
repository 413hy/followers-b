from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from ai_quant.binance_egress.testnet_probe import TestnetProbeError as ProbeError
from ai_quant.copy_trading.execution import (
    CopyExecutionState,
    CopyMarketOrder,
    CopyOrderType,
    HedgeTestnetMarketExecutor,
    SubmissionClaim,
    SubmissionEvent,
    _request_hash,
    copy_client_order_id,
    copy_gtc_upgrade_client_order_id,
    protected_entry_price,
)
from ai_quant.copy_trading.models import NormalizedSignal, PositionSide, SignalKind
from ai_quant.copy_trading.risk import (
    AccountRiskLevel,
    CopyAccountRiskPolicy,
    CopyAccountSnapshot,
    evaluate_account_risk,
)

NOW = datetime(2026, 7, 16, 4, 0, tzinfo=UTC)


def _signal(
    *,
    kind: SignalKind = SignalKind.INCREASE,
    side: PositionSide = PositionSide.LONG,
    reference_price: str = "2000",
) -> NormalizedSignal:
    return NormalizedSignal(
        signal_id="a" * 64,
        source_event_key="b" * 64,
        source_identity_key="c" * 64,
        lead_portfolio_id="5108371059752839168",
        symbol="ETHUSDT",
        position_side=side,
        kind=kind,
        source_delta_quantity=Decimal("10"),
        source_cumulative_quantity=Decimal("10"),
        reference_price=Decimal(reference_price),
        occurred_at_ms=1_700_000_000_000,
    )


def _snapshot(
    *,
    margin: str = "150",
    maintenance: str = "1",
    observed_at: datetime = NOW,
) -> CopyAccountSnapshot:
    return CopyAccountSnapshot(
        observed_at=observed_at,
        hedge_mode=True,
        can_trade=True,
        wallet_balance_usdt=Decimal("150"),
        margin_balance_usdt=Decimal(margin),
        available_balance_usdt=Decimal("140"),
        total_initial_margin_usdt=Decimal("10"),
        total_maintenance_margin_usdt=Decimal(maintenance),
    )


def _risk(kind: SignalKind = SignalKind.INCREASE):  # type: ignore[no-untyped-def]
    return evaluate_account_risk(_snapshot(), signal_kind=kind, now=NOW)


class FakeJournal:
    def __init__(self, *, claim_result: bool = True) -> None:
        self.claim_result = claim_result
        self.claims: list[dict[str, object]] = []
        self.events: list[SubmissionEvent] = []
        self.existing: SubmissionClaim | None = None

    def lookup(self, *, signal_id: str) -> SubmissionClaim | None:
        if self.existing is not None and self.existing.signal_id == signal_id:
            return self.existing
        return None

    def claim(self, **values: object) -> bool:
        self.claims.append(values)
        self.existing = SubmissionClaim(
            signal_id=str(values["signal_id"]),
            client_order_id=str(values["client_order_id"]),
            request_hash=str(values["request_hash"]),
            request_hash_version=int(str(values["request_hash_version"])),
            requested_quantity=Decimal(str(values["requested_quantity"])),
            leverage=int(str(values["leverage"])),
            order_type=CopyOrderType(str(values["order_type"])),
            limit_price=(
                None if values["limit_price"] is None else Decimal(str(values["limit_price"]))
            ),
            expires_at=(
                values["expires_at"] if isinstance(values["expires_at"], datetime) else None
            ),
            claimed_at=(
                values["claimed_at"] if isinstance(values["claimed_at"], datetime) else None
            ),
        )
        if self.claim_result:
            event = values["submitting_event"]
            assert isinstance(event, SubmissionEvent)
            self.events.append(event)
        return self.claim_result

    def record(self, event: SubmissionEvent) -> None:
        self.events.append(event)


class FakeClient:
    def __init__(self) -> None:
        self.placed: list[dict[str, str]] = []
        self.queried: list[tuple[str, str]] = []
        self.leverages: list[tuple[str, int]] = []
        self.place_error = False
        self.query_error = False
        self.cancel_error = False
        self.cancelled: list[tuple[str, str]] = []
        self.open_order_rows: list[dict[str, Any]] = []

    def position_mode(self) -> dict[str, Any]:
        return {"dualSidePosition": True}

    def change_initial_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        self.leverages.append((symbol, leverage))
        return {"symbol": symbol, "leverage": leverage}

    def position_risk(self, symbol: str) -> list[dict[str, Any]]:
        return [{"symbol": symbol, "positionSide": "LONG", "positionAmt": "0.100"}]

    def place_order(self, params: dict[str, str]) -> dict[str, Any]:
        self.placed.append(params)
        if self.place_error:
            raise ProbeError("PLACE_ORDER_TRANSPORT_FAILED")
        return {
            "clientOrderId": params["newClientOrderId"],
            "orderId": 123,
            "status": "FILLED",
            "executedQty": params["quantity"],
            "avgPrice": "2001",
        }

    def query_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        self.queried.append((symbol, client_order_id))
        if self.query_error:
            raise ProbeError("QUERY_ORDER_NOT_FOUND")
        return {
            "clientOrderId": client_order_id,
            "orderId": 123,
            "status": "FILLED",
            "executedQty": "0.010",
            "avgPrice": "2001",
        }

    def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        self.cancelled.append((symbol, client_order_id))
        if self.cancel_error:
            raise ProbeError("CANCEL_ORDER_FAILED")
        return {
            "symbol": symbol,
            "clientOrderId": client_order_id,
            "orderId": 123,
            "status": "CANCELED",
            "executedQty": "0",
            "avgPrice": "0",
        }

    def open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return list(self.open_order_rows)


def _protected_order(
    *,
    signal: NormalizedSignal | None = None,
    expires_at: datetime = NOW + timedelta(hours=1),
) -> CopyMarketOrder:
    return CopyMarketOrder(
        signal or _signal(),
        local_quantity=Decimal("0.010"),
        leverage=10,
        order_type=CopyOrderType.LIMIT,
        limit_price=Decimal("2000"),
        expires_at=expires_at,
    )


def _persistent_protected_order() -> CopyMarketOrder:
    return CopyMarketOrder(
        _signal(),
        local_quantity=Decimal("0.010"),
        leverage=10,
        order_type=CopyOrderType.LIMIT,
        limit_price=Decimal("2000"),
        expires_at=None,
    )


def test_account_risk_warning_is_advisory_by_default() -> None:
    warning = _snapshot(margin="115")
    entry = evaluate_account_risk(warning, signal_kind=SignalKind.INCREASE, now=NOW)
    reduction = evaluate_account_risk(warning, signal_kind=SignalKind.REDUCE, now=NOW)

    assert entry.level is AccountRiskLevel.WARNING
    assert entry.allow_execution
    assert not entry.pause_new_entries
    assert reduction.allow_execution


def test_account_risk_emergency_is_advisory_by_default() -> None:
    snapshot = _snapshot(margin="100")
    decision = evaluate_account_risk(
        snapshot,
        signal_kind=SignalKind.REDUCE,
        now=NOW,
        policy=CopyAccountRiskPolicy(),
    )
    assert decision.level is AccountRiskLevel.EMERGENCY
    assert decision.allow_execution
    assert not decision.reduce_all_required


def test_account_risk_automatic_intervention_requires_explicit_opt_in() -> None:
    snapshot = _snapshot(margin="100")
    decision = evaluate_account_risk(
        snapshot,
        signal_kind=SignalKind.INCREASE,
        now=NOW,
        policy=CopyAccountRiskPolicy(automatic_intervention_enabled=True),
    )

    assert decision.level is AccountRiskLevel.EMERGENCY
    assert not decision.allow_execution
    assert decision.pause_new_entries
    assert decision.reduce_all_required


def test_stale_account_snapshot_fails_closed() -> None:
    stale = _snapshot(observed_at=NOW - timedelta(minutes=1))
    decision = evaluate_account_risk(stale, signal_kind=SignalKind.INCREASE, now=NOW)
    assert decision.level is AccountRiskLevel.INVALID
    assert not decision.allow_execution
    assert "COPY_ACCOUNT_SNAPSHOT_STALE" in decision.reason_codes


def test_hedge_market_order_is_persisted_before_submit_and_has_no_reduce_only() -> None:
    client = FakeClient()
    journal = FakeJournal()
    executor = HedgeTestnetMarketExecutor(client=client, journal=journal, clock=lambda: NOW)
    receipt = executor.execute(
        CopyMarketOrder(_signal(), local_quantity=Decimal("0.010"), leverage=10),
        risk_decision=_risk(),
    )

    assert receipt.state is CopyExecutionState.FILLED
    assert len(journal.claims) == 1
    assert journal.events[0].state == "SUBMITTING"
    assert client.leverages == [("ETHUSDT", 10)]
    assert client.placed[0]["positionSide"] == "LONG"
    assert client.placed[0]["side"] == "BUY"
    assert "reduceOnly" not in client.placed[0]


def test_protected_entry_price_never_rounds_to_a_worse_source_price() -> None:
    assert protected_entry_price(
        _signal(side=PositionSide.LONG, reference_price="2000.019"),
        Decimal("0.01"),
    ) == Decimal("2000.01")
    assert protected_entry_price(
        _signal(side=PositionSide.SHORT, reference_price="2000.011"),
        Decimal("0.01"),
    ) == Decimal("2000.02")


def test_protected_entry_keeps_source_boundary_when_live_quote_is_better() -> None:
    assert protected_entry_price(
        _signal(side=PositionSide.LONG, reference_price="0.1659499"),
        Decimal("0.00001"),
        market_price=Decimal("0.154321"),
    ) == Decimal("0.16594")
    assert protected_entry_price(
        _signal(side=PositionSide.SHORT, reference_price="90.451"),
        Decimal("0.01"),
        market_price=Decimal("94.729"),
    ) == Decimal("90.46")


def test_protected_entry_price_keeps_source_limit_when_market_is_worse() -> None:
    assert protected_entry_price(
        _signal(side=PositionSide.LONG, reference_price="100"),
        Decimal("0.01"),
        market_price=Decimal("101"),
    ) == Decimal("100")
    assert protected_entry_price(
        _signal(side=PositionSide.SHORT, reference_price="100"),
        Decimal("0.01"),
        market_price=Decimal("99"),
    ) == Decimal("100")


def test_entry_uses_gtd_protected_limit_with_durable_expiry() -> None:
    client = FakeClient()
    journal = FakeJournal()

    def acknowledge(params: dict[str, str]) -> dict[str, Any]:
        client.placed.append(params)
        return {
            "clientOrderId": params["newClientOrderId"],
            "orderId": 124,
            "status": "NEW",
            "executedQty": "0",
            "avgPrice": "0",
        }

    client.place_order = acknowledge  # type: ignore[method-assign]
    receipt = HedgeTestnetMarketExecutor(
        client=client,
        journal=journal,
        clock=lambda: NOW,
    ).execute(_protected_order(), risk_decision=_risk())

    assert receipt.state is CopyExecutionState.ACKNOWLEDGED
    assert receipt.reason_codes == ("COPY_PROTECTED_LIMIT_PENDING",)
    assert client.placed[0]["type"] == "LIMIT"
    assert client.placed[0]["newOrderRespType"] == "RESULT"
    assert client.placed[0]["price"] == "2000"
    assert client.placed[0]["timeInForce"] == "GTD"
    assert client.placed[0]["goodTillDate"] == str(
        int((NOW + timedelta(hours=1)).timestamp()) * 1000
    )
    assert journal.existing is not None
    assert journal.existing.expires_at == NOW + timedelta(hours=1)


def test_persistent_protected_limit_uses_gtc_without_an_expiry() -> None:
    client = FakeClient()
    journal = FakeJournal()

    def acknowledge(params: dict[str, str]) -> dict[str, Any]:
        client.placed.append(params)
        return {
            "clientOrderId": params["newClientOrderId"],
            "orderId": 126,
            "status": "NEW",
            "executedQty": "0",
            "avgPrice": "0",
        }

    client.place_order = acknowledge  # type: ignore[method-assign]
    receipt = HedgeTestnetMarketExecutor(
        client=client,
        journal=journal,
        clock=lambda: NOW,
    ).execute(_persistent_protected_order(), risk_decision=_risk())

    assert receipt.state is CopyExecutionState.ACKNOWLEDGED
    assert client.placed[0]["timeInForce"] == "GTC"
    assert "goodTillDate" not in client.placed[0]
    assert journal.existing is not None
    assert journal.existing.expires_at is None


def test_expired_protected_entry_is_cancelled_without_market_conversion() -> None:
    client = FakeClient()
    journal = FakeJournal()
    order = _protected_order()
    executor = HedgeTestnetMarketExecutor(client=client, journal=journal, clock=lambda: NOW)

    def acknowledge(params: dict[str, str]) -> dict[str, Any]:
        client.placed.append(params)
        return {
            "clientOrderId": params["newClientOrderId"],
            "orderId": 125,
            "status": "NEW",
            "executedQty": "0",
            "avgPrice": "0",
        }

    client.place_order = acknowledge  # type: ignore[method-assign]
    executor.execute(order, risk_decision=_risk())

    def still_open(symbol: str, client_order_id: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "clientOrderId": client_order_id,
            "orderId": 125,
            "status": "NEW",
            "executedQty": "0",
            "avgPrice": "0",
        }

    client.query_order = still_open  # type: ignore[method-assign]
    expired = HedgeTestnetMarketExecutor(
        client=client,
        journal=journal,
        clock=lambda: NOW + timedelta(hours=1),
    ).execute(order, risk_decision=_risk())

    assert expired.state is CopyExecutionState.REJECTED
    assert expired.reason_codes == ("COPY_PROTECTED_LIMIT_EXPIRED",)
    assert len(client.placed) == 1
    assert len(client.cancelled) == 1


def test_exchange_native_gtd_expiry_is_classified_as_cancellation() -> None:
    client = FakeClient()
    journal = FakeJournal()
    order = _protected_order()

    def acknowledge(params: dict[str, str]) -> dict[str, Any]:
        client.placed.append(params)
        return {
            "clientOrderId": params["newClientOrderId"],
            "orderId": 127,
            "status": "NEW",
            "executedQty": "0",
            "avgPrice": "0",
        }

    client.place_order = acknowledge  # type: ignore[method-assign]
    HedgeTestnetMarketExecutor(
        client=client,
        journal=journal,
        clock=lambda: NOW,
    ).execute(order, risk_decision=_risk())

    def exchange_expired(symbol: str, client_order_id: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "clientOrderId": client_order_id,
            "orderId": 127,
            "status": "EXPIRED",
            "executedQty": "0",
            "avgPrice": "0",
        }

    client.query_order = exchange_expired  # type: ignore[method-assign]
    receipt = HedgeTestnetMarketExecutor(
        client=client,
        journal=journal,
        clock=lambda: NOW + timedelta(hours=1),
    ).execute(order, risk_decision=_risk())

    assert receipt.state is CopyExecutionState.REJECTED
    assert receipt.reason_codes == ("COPY_PROTECTED_LIMIT_EXPIRED",)
    assert client.cancelled == []


def test_cancelled_entry_missing_at_exchange_remains_uncertain() -> None:
    client = FakeClient()
    journal = FakeJournal()
    order = _protected_order()

    def acknowledge(params: dict[str, str]) -> dict[str, Any]:
        client.placed.append(params)
        return {
            "clientOrderId": params["newClientOrderId"],
            "orderId": 128,
            "status": "NEW",
            "executedQty": "0",
            "avgPrice": "0",
        }

    client.place_order = acknowledge  # type: ignore[method-assign]
    HedgeTestnetMarketExecutor(
        client=client,
        journal=journal,
        clock=lambda: NOW,
    ).execute(order, risk_decision=_risk())
    client.cancel_error = True

    def missing_order(symbol: str, client_order_id: str) -> dict[str, Any]:
        client.queried.append((symbol, client_order_id))
        raise ProbeError("QUERY_ORDER_HTTP_400_CODE_-2013")

    client.query_order = missing_order  # type: ignore[method-assign]
    receipt = HedgeTestnetMarketExecutor(
        client=client,
        journal=journal,
        clock=lambda: NOW + timedelta(minutes=2),
    ).cancel_pending_increase(order.signal)

    assert receipt.state is CopyExecutionState.UNKNOWN
    assert receipt.reason_codes == ("COPY_PROTECTED_LIMIT_CANCEL_STATUS_UNKNOWN",)
    assert journal.events[-1].state == "UNKNOWN"


def test_source_reduction_cancels_pending_entry_before_it_can_fill() -> None:
    client = FakeClient()
    journal = FakeJournal()
    order = _protected_order()

    def acknowledge(params: dict[str, str]) -> dict[str, Any]:
        client.placed.append(params)
        return {
            "clientOrderId": params["newClientOrderId"],
            "orderId": 126,
            "status": "NEW",
            "executedQty": "0",
            "avgPrice": "0",
        }

    client.place_order = acknowledge  # type: ignore[method-assign]
    executor = HedgeTestnetMarketExecutor(client=client, journal=journal, clock=lambda: NOW)
    executor.execute(order, risk_decision=_risk())
    cancelled = executor.cancel_pending_increase(order.signal)

    assert cancelled.state is CopyExecutionState.REJECTED
    assert cancelled.reason_codes == ("COPY_PROTECTED_LIMIT_CANCELLED_BY_SOURCE_REDUCTION",)
    assert len(client.cancelled) == 1


def test_duplicate_persistent_claim_reconciles_without_resubmission() -> None:
    client = FakeClient()
    journal = FakeJournal(claim_result=False)
    executor = HedgeTestnetMarketExecutor(client=client, journal=journal, clock=lambda: NOW)
    receipt = executor.execute(
        CopyMarketOrder(_signal(), local_quantity=Decimal("0.010"), leverage=10),
        risk_decision=_risk(),
    )

    assert receipt.state is CopyExecutionState.RECONCILED
    assert client.placed == []
    assert len(client.queried) == 1


def test_restart_reconciles_with_original_persisted_quantity() -> None:
    client = FakeClient()
    journal = FakeJournal()
    executor = HedgeTestnetMarketExecutor(client=client, journal=journal, clock=lambda: NOW)
    executor.execute(
        CopyMarketOrder(_signal(), local_quantity=Decimal("0.010"), leverage=10),
        risk_decision=_risk(),
    )

    receipt = executor.execute(
        CopyMarketOrder(_signal(), local_quantity=Decimal("0.005"), leverage=5),
        risk_decision=_risk(),
    )

    assert receipt.state is CopyExecutionState.RECONCILED
    assert receipt.requested_quantity == Decimal("0.010")
    assert receipt.leverage == 10
    assert len(client.placed) == 1
    assert len(client.queried) == 1


def test_v2_claim_hash_survives_postgres_numeric_scale_round_trip() -> None:
    client = FakeClient()
    journal = FakeJournal()
    executor = HedgeTestnetMarketExecutor(client=client, journal=journal, clock=lambda: NOW)
    order = _protected_order()
    executor.execute(order, risk_decision=_risk())
    assert journal.existing is not None
    journal.existing = replace(
        journal.existing,
        requested_quantity=Decimal("0.010000000000000000"),
        limit_price=Decimal("2000.000000000000000000"),
    )

    receipt = executor.execute(order, risk_decision=_risk())

    assert receipt.state is CopyExecutionState.RECONCILED
    assert len(client.placed) == 1
    assert len(client.queried) == 1


def test_legacy_claim_uses_validated_append_only_fields_after_scale_change() -> None:
    client = FakeClient()
    journal = FakeJournal()
    executor = HedgeTestnetMarketExecutor(client=client, journal=journal, clock=lambda: NOW)
    order = _protected_order()
    executor.execute(order, risk_decision=_risk())
    assert journal.existing is not None
    journal.existing = replace(
        journal.existing,
        request_hash="f" * 64,
        request_hash_version=1,
        requested_quantity=Decimal("0.010000000000000000"),
        limit_price=Decimal("2000.000000000000000000"),
    )

    receipt = executor.execute(order, risk_decision=_risk())

    assert receipt.state is CopyExecutionState.RECONCILED
    assert len(client.placed) == 1
    assert len(client.queried) == 1


def test_v2_claim_rejects_semantically_changed_durable_parameters() -> None:
    client = FakeClient()
    journal = FakeJournal()
    executor = HedgeTestnetMarketExecutor(client=client, journal=journal, clock=lambda: NOW)
    order = _protected_order()
    executor.execute(order, risk_decision=_risk())
    assert journal.existing is not None
    journal.existing = replace(
        journal.existing,
        requested_quantity=Decimal("0.011"),
    )

    receipt = executor.execute(order, risk_decision=_risk())

    assert receipt.state is CopyExecutionState.UNKNOWN
    assert receipt.reason_codes == ("COPY_SUBMISSION_CLAIM_PARAMETERS_INVALID",)
    assert len(client.placed) == 1
    assert client.queried == []


def test_fill_average_price_falls_back_to_cumulative_quote() -> None:
    client = FakeClient()
    journal = FakeJournal()

    def place_without_average(params: dict[str, str]) -> dict[str, Any]:
        client.placed.append(params)
        return {
            "clientOrderId": params["newClientOrderId"],
            "orderId": 124,
            "status": "FILLED",
            "executedQty": params["quantity"],
            "cumQuote": "20.01",
        }

    client.place_order = place_without_average  # type: ignore[method-assign]
    executor = HedgeTestnetMarketExecutor(client=client, journal=journal, clock=lambda: NOW)
    receipt = executor.execute(
        CopyMarketOrder(_signal(), local_quantity=Decimal("0.010"), leverage=10),
        risk_decision=_risk(),
    )
    assert receipt.state is CopyExecutionState.FILLED
    assert receipt.average_price == Decimal("2001")


def test_terminal_fill_without_price_is_queried_immediately() -> None:
    client = FakeClient()
    journal = FakeJournal()

    def place_without_fill_details(params: dict[str, str]) -> dict[str, Any]:
        client.placed.append(params)
        return {
            "clientOrderId": params["newClientOrderId"],
            "orderId": 124,
            "status": "FILLED",
            "executedQty": params["quantity"],
        }

    client.place_order = place_without_fill_details  # type: ignore[method-assign]
    receipt = HedgeTestnetMarketExecutor(
        client=client,
        journal=journal,
        clock=lambda: NOW,
        sleeper=lambda _: None,
    ).execute(_protected_order(), risk_decision=_risk())

    assert receipt.state is CopyExecutionState.RECONCILED
    assert receipt.average_price == Decimal("2001")
    assert client.queried == [("ETHUSDT", "aqc-t-" + "a" * 28)]
    assert [event.state for event in journal.events] == [
        "SUBMITTING",
        "FILLED",
        "RECONCILED",
    ]


def test_terminal_fill_with_still_missing_price_remains_silently_recoverable() -> None:
    client = FakeClient()
    journal = FakeJournal()

    def place_without_fill_details(params: dict[str, str]) -> dict[str, Any]:
        client.placed.append(params)
        return {
            "clientOrderId": params["newClientOrderId"],
            "orderId": 124,
            "status": "FILLED",
            "executedQty": params["quantity"],
        }

    def query_without_fill_details(symbol: str, client_order_id: str) -> dict[str, Any]:
        client.queried.append((symbol, client_order_id))
        return {
            "clientOrderId": client_order_id,
            "orderId": 124,
            "status": "FILLED",
            "executedQty": "0.010",
            "avgPrice": "0",
            "cumQuote": "0",
        }

    client.place_order = place_without_fill_details  # type: ignore[method-assign]
    client.query_order = query_without_fill_details  # type: ignore[method-assign]
    receipt = HedgeTestnetMarketExecutor(
        client=client,
        journal=journal,
        clock=lambda: NOW,
        sleeper=lambda _: None,
    ).execute(_protected_order(), risk_decision=_risk())

    assert receipt.state is CopyExecutionState.FILLED
    assert receipt.average_price == Decimal("0")
    assert receipt.reason_codes == ("COPY_FILL_PRICE_PENDING",)
    assert len(client.queried) == 3
    assert journal.events[-1].reason_codes == ("COPY_FILL_PRICE_PENDING",)


def test_uncertain_submit_is_never_automatically_retried() -> None:
    client = FakeClient()
    client.place_error = True
    client.query_error = True
    journal = FakeJournal()
    executor = HedgeTestnetMarketExecutor(client=client, journal=journal, clock=lambda: NOW)
    receipt = executor.execute(
        CopyMarketOrder(_signal(), local_quantity=Decimal("0.010"), leverage=10),
        risk_decision=_risk(),
    )

    assert receipt.state is CopyExecutionState.UNKNOWN
    assert len(client.placed) == 1
    assert len(client.queried) == 1
    assert journal.events[-1].state == "UNKNOWN"


def test_explicit_exchange_rejection_is_terminal_not_uncertain() -> None:
    client = FakeClient()
    journal = FakeJournal()

    def reject_notional(params: dict[str, str]) -> dict[str, Any]:
        client.placed.append(params)
        raise ProbeError("PLACE_ORDER_HTTP_400_CODE_-4164")

    client.place_order = reject_notional  # type: ignore[method-assign]
    receipt = HedgeTestnetMarketExecutor(
        client=client,
        journal=journal,
        clock=lambda: NOW,
    ).execute(_protected_order(), risk_decision=_risk())

    assert receipt.state is CopyExecutionState.REJECTED
    assert receipt.reason_codes == (
        "COPY_ORDER_NOTIONAL_BELOW_EXCHANGE_MINIMUM",
        "COPY_EXCHANGE_CODE_4164",
    )
    assert client.queried == []
    assert journal.events[-1].state == "REJECTED"


def test_tradifi_agreement_rejection_is_an_explicit_account_prerequisite() -> None:
    client = FakeClient()
    journal = FakeJournal()

    def reject_unsigned_agreement(params: dict[str, str]) -> dict[str, Any]:
        client.placed.append(params)
        raise ProbeError("PLACE_ORDER_HTTP_400_CODE_-4411")

    client.place_order = reject_unsigned_agreement  # type: ignore[method-assign]
    receipt = HedgeTestnetMarketExecutor(
        client=client,
        journal=journal,
        clock=lambda: NOW,
    ).execute(_protected_order(), risk_decision=_risk())

    assert receipt.state is CopyExecutionState.REJECTED
    assert receipt.reason_codes == (
        "COPY_TRADIFI_AGREEMENT_REQUIRED",
        "COPY_EXCHANGE_CODE_4411",
    )
    assert client.queried == []
    assert journal.events[-1].state == "REJECTED"


def test_missing_claimed_order_after_grace_becomes_terminal_rejection() -> None:
    client = FakeClient()
    journal = FakeJournal()
    order = _protected_order()
    initial = HedgeTestnetMarketExecutor(
        client=client,
        journal=journal,
        clock=lambda: NOW,
    )
    initial.execute(order, risk_decision=_risk())

    def missing_order(symbol: str, client_order_id: str) -> dict[str, Any]:
        client.queried.append((symbol, client_order_id))
        raise ProbeError("QUERY_ORDER_HTTP_400_CODE_-2013")

    client.query_order = missing_order  # type: ignore[method-assign]
    receipt = HedgeTestnetMarketExecutor(
        client=client,
        journal=journal,
        clock=lambda: NOW + timedelta(minutes=2),
    ).execute(order, risk_decision=_risk())

    assert receipt.state is CopyExecutionState.REJECTED
    assert receipt.reason_codes == ("COPY_SUBMISSION_NOT_FOUND_AFTER_GRACE",)
    assert len(client.placed) == 1
    assert journal.events[-1].state == "REJECTED"


def test_durable_claim_restores_result_response_mode() -> None:
    order = _protected_order()
    client_order_id = copy_client_order_id(order.signal.signal_id)
    claim = SubmissionClaim(
        signal_id=order.signal.signal_id,
        client_order_id=client_order_id,
        request_hash=_request_hash(order, client_order_id),
        requested_quantity=order.local_quantity,
        leverage=order.leverage,
        order_type=order.order_type,
        limit_price=order.limit_price,
        expires_at=order.expires_at,
        request_hash_version=2,
        claimed_at=NOW,
    )

    assert claim.restored_order(order.signal) == order


def test_upgraded_legacy_claim_restores_persistent_replacement_order() -> None:
    order = _persistent_protected_order()
    claim = SubmissionClaim(
        signal_id=order.signal.signal_id,
        client_order_id=copy_gtc_upgrade_client_order_id(order.signal.signal_id),
        request_hash="0" * 64,
        requested_quantity=order.local_quantity,
        leverage=order.leverage,
        order_type=order.order_type,
        limit_price=order.limit_price,
        expires_at=None,
        request_hash_version=1,
        claimed_at=NOW,
        policy_upgraded=True,
    )

    assert claim.restored_order(order.signal) == order


def test_live_partial_fill_remains_recoverable_until_terminal() -> None:
    client = FakeClient()
    journal = FakeJournal()

    def partial(params: dict[str, str]) -> dict[str, Any]:
        client.placed.append(params)
        return {
            "clientOrderId": params["newClientOrderId"],
            "orderId": 125,
            "status": "PARTIALLY_FILLED",
            "executedQty": "0.005",
            "avgPrice": "2001",
        }

    client.place_order = partial  # type: ignore[method-assign]
    receipt = HedgeTestnetMarketExecutor(
        client=client,
        journal=journal,
        clock=lambda: NOW,
    ).execute(
        CopyMarketOrder(_signal(), local_quantity=Decimal("0.010"), leverage=10),
        risk_decision=_risk(),
    )

    assert receipt.state is CopyExecutionState.PARTIALLY_FILLED
    assert receipt.reason_codes == ("COPY_ORDER_PARTIAL_PENDING",)


def test_terminal_partial_fill_is_explicitly_distinguished() -> None:
    client = FakeClient()
    journal = FakeJournal()

    def canceled_partial(params: dict[str, str]) -> dict[str, Any]:
        client.placed.append(params)
        return {
            "clientOrderId": params["newClientOrderId"],
            "orderId": 126,
            "status": "CANCELED",
            "executedQty": "0.005",
            "avgPrice": "2001",
        }

    client.place_order = canceled_partial  # type: ignore[method-assign]
    receipt = HedgeTestnetMarketExecutor(
        client=client,
        journal=journal,
        clock=lambda: NOW,
    ).execute(
        CopyMarketOrder(_signal(), local_quantity=Decimal("0.010"), leverage=10),
        risk_decision=_risk(),
    )

    assert receipt.state is CopyExecutionState.PARTIALLY_FILLED
    assert "COPY_ORDER_PARTIAL_TERMINAL" in receipt.reason_codes


def test_reduction_cannot_exceed_exchange_hedge_side() -> None:
    client = FakeClient()
    journal = FakeJournal()
    executor = HedgeTestnetMarketExecutor(client=client, journal=journal, clock=lambda: NOW)
    receipt = executor.execute(
        CopyMarketOrder(
            _signal(kind=SignalKind.REDUCE),
            local_quantity=Decimal("0.101"),
            leverage=10,
        ),
        risk_decision=_risk(SignalKind.REDUCE),
    )
    assert receipt.state is CopyExecutionState.REJECTED
    assert client.placed == []
    assert journal.claims == []
