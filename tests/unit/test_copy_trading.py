from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from ai_quant.copy_trading.binance_public import (
    BINANCE_WEB_BASE,
    LEADER_LIST_PATH,
    ORDER_HISTORY_PATH,
    BinancePublicCopyClient,
    BinancePublicCopyError,
    PublicHttpResult,
)
from ai_quant.copy_trading.models import (
    NormalizedSignal,
    PublicLeaderOrder,
    SignalKind,
    SourcePositionSide,
)
from ai_quant.copy_trading.normalization import LeaderOrderTracker
from ai_quant.copy_trading.repository import _is_source_order_baseline


def _response(data: dict[str, Any], *, status: int = 200) -> PublicHttpResult:
    return PublicHttpResult(
        status=status,
        headers={},
        body=json.dumps({"success": True, "code": "000000", "data": data}).encode(),
    )


def _leader() -> dict[str, Any]:
    return {
        "leadPortfolioId": "5108371059752839168",
        "nickname": "leader",
        "roi": 123.5,
        "pnl": 2000,
        "aum": 100000,
        "mdd": 12.5,
        "winRate": 67.8,
        "currentCopyCount": 300,
        "maxCopyCount": 400,
        "startTime": 1_700_000_000_000,
        "portfolioType": "PUBLIC",
        "sharpRatio": None,
    }


def _order(
    *,
    side: str = "BUY",
    position_side: str = "LONG",
    quantity: str = "2",
    order_time: int = 1_700_000_000_000,
    update_time: int = 1_700_000_001_000,
) -> PublicLeaderOrder:
    return PublicLeaderOrder.from_api(
        "5108371059752839168",
        {
            "symbol": "ETHUSDT",
            "side": side,
            "type": "LIMIT",
            "positionSide": position_side,
            "executedQty": quantity,
            "avgPrice": "2000",
            "totalPnl": "0",
            "orderTime": order_time,
            "orderUpdateTime": update_time,
        },
    )


def test_resolved_view_of_ambiguous_initial_row_remains_baseline() -> None:
    assert _is_source_order_baseline(
        baseline=False,
        has_previous=False,
        update_time_ms=1_700_000_001_000,
        maximum_update_time_ms=1_700_000_001_000,
        matches_ambiguous_baseline=True,
    )


def test_same_millisecond_distinct_live_order_is_not_suppressed() -> None:
    assert not _is_source_order_baseline(
        baseline=False,
        has_previous=False,
        update_time_ms=1_700_000_001_000,
        maximum_update_time_ms=1_700_000_001_000,
        matches_ambiguous_baseline=False,
    )


def test_public_client_uses_only_exact_binance_paths_and_parses_pages() -> None:
    calls: list[tuple[str, str, Mapping[str, str], dict[str, Any]]] = []

    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> PublicHttpResult:
        request = json.loads(body)
        calls.append((method, url, headers, request))
        if url.endswith(LEADER_LIST_PATH):
            return _response({"list": [_leader()], "total": 1})
        if url.endswith(ORDER_HISTORY_PATH):
            return _response(
                {
                    "list": [
                        {
                            "symbol": "ETHUSDT",
                            "side": "SELL",
                            "type": "MARKET",
                            "positionSide": "LONG",
                            "executedQty": 1,
                            "avgPrice": 2100,
                            "totalPnl": 100,
                            "orderTime": 1_700_000_000_000,
                            "orderUpdateTime": 1_700_000_001_000,
                        }
                    ],
                    "total": 1,
                }
            )
        raise AssertionError(url)

    client = BinancePublicCopyClient(transport=transport)
    leaders = client.list_leaders(page_size=25)
    history = client.order_history("5108371059752839168")

    assert leaders.total == 1
    assert leaders.leaders[0].win_rate_pct == Decimal("67.8")
    assert history.total == 1
    assert history.orders[0].position_side is SourcePositionSide.LONG
    assert [call[1] for call in calls] == [
        f"{BINANCE_WEB_BASE}{LEADER_LIST_PATH}",
        f"{BINANCE_WEB_BASE}{ORDER_HISTORY_PATH}",
    ]
    assert calls[0][0] == "POST"
    assert calls[0][2]["User-Agent"].startswith("aiq-copy-trading/")
    assert calls[1][3]["portfolioId"] == "5108371059752839168"


def test_public_candidate_page_can_isolate_malformed_unrelated_rows() -> None:
    invalid = {**_leader(), "leadPortfolioId": "5000000000000000001", "nickname": None}
    client = BinancePublicCopyClient(
        transport=lambda method, url, headers, body: _response(
            {"list": [invalid, _leader()], "total": 2}
        )
    )

    with pytest.raises(BinancePublicCopyError, match="COPY_FIELD_NICKNAME_INVALID"):
        client.list_leaders()

    page = client.list_leaders(skip_invalid_rows=True)

    assert [leader.lead_portfolio_id for leader in page.leaders] == ["5108371059752839168"]
    assert page.total == 2
    assert page.invalid_row_count == 1
    assert page.invalid_reason_codes == ("COPY_FIELD_NICKNAME_INVALID",)
    assert page.invalid_leader_ids == ("5000000000000000001",)


def test_public_directory_reads_every_server_sized_page() -> None:
    calls: list[int] = []

    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> PublicHttpResult:
        del method, url, headers
        request = json.loads(body)
        page_number = int(request["pageNumber"])
        calls.append(page_number)
        if page_number <= 3:
            return _response(
                {
                    "list": [
                        {
                            **_leader(),
                            "leadPortfolioId": f"510837105975283916{page_number}",
                        }
                    ],
                    "total": 3,
                }
            )
        raise AssertionError("directory read beyond total")

    directory = BinancePublicCopyClient(transport=transport).list_all_leaders()

    assert calls == [1, 2, 3]
    assert directory.total == 3
    assert len(directory.leaders) == 3


def test_public_directory_adapts_when_live_total_shrinks_during_paging() -> None:
    calls: list[int] = []

    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> PublicHttpResult:
        del method, url, headers
        page_number = int(json.loads(body)["pageNumber"])
        calls.append(page_number)
        if page_number == 1:
            rows = [
                {**_leader(), "leadPortfolioId": "5108371059752839161"},
                {**_leader(), "leadPortfolioId": "5108371059752839162"},
            ]
            return _response({"list": rows, "total": 4})
        return _response(
            {
                "list": [{**_leader(), "leadPortfolioId": "5108371059752839163"}],
                "total": 3,
            }
        )

    directory = BinancePublicCopyClient(transport=transport).list_all_leaders()

    assert calls == [1, 2]
    assert directory.total == 3
    assert len(directory.leaders) == 3


def test_public_directory_extends_paging_when_live_total_grows() -> None:
    calls: list[int] = []

    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> PublicHttpResult:
        del method, url, headers
        page_number = int(json.loads(body)["pageNumber"])
        calls.append(page_number)
        start = (page_number - 1) * 2
        count = 1 if page_number == 3 else 2
        rows = [
            {
                **_leader(),
                "leadPortfolioId": f"5108371059752839{start + offset + 1:03d}",
            }
            for offset in range(count)
        ]
        return _response({"list": rows, "total": 4 if page_number == 1 else 5})

    directory = BinancePublicCopyClient(transport=transport).list_all_leaders()

    assert calls == [1, 2, 3]
    assert directory.total == 5
    assert len(directory.leaders) == 5


def test_public_directory_retries_whole_snapshot_before_surfacing_incomplete() -> None:
    calls: list[int] = []
    delays: list[float] = []

    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> PublicHttpResult:
        del method, url, headers
        page_number = int(json.loads(body)["pageNumber"])
        calls.append(page_number)
        if calls == [1, 2]:
            return _response({"list": [], "total": 2})
        return _response(
            {
                "list": [
                    {
                        **_leader(),
                        "leadPortfolioId": f"510837105975283916{page_number}",
                    }
                ],
                "total": 2,
            }
        )

    directory = BinancePublicCopyClient(
        transport=transport,
        retry_attempts=2,
        sleeper=delays.append,
    ).list_all_leaders()

    assert calls == [1, 2, 1, 2]
    assert delays == [0.5]
    assert directory.total == 2
    assert len(directory.leaders) == 2


def test_public_directory_can_find_id_or_name_without_trusting_unrelated_bad_rows() -> None:
    target = {
        **_leader(),
        "leadPortfolioId": "5014426348046646785",
        "nickname": "印钞机百分百胜率0回撤3号",
    }

    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> PublicHttpResult:
        del method, url, headers
        request = json.loads(body)
        if request["pageNumber"] == 1:
            return _response(
                {
                    "list": [
                        {**_leader(), "leadPortfolioId": "5000000000000000001", "nickname": None}
                    ],
                    "total": 101,
                }
            )
        return _response({"list": [target], "total": 101})

    client = BinancePublicCopyClient(transport=transport)
    found = client.find_leader("5014426348046646785")
    matches = client.search_leaders("印钞机百分百")

    assert found.nickname == "印钞机百分百胜率0回撤3号"
    assert [leader.lead_portfolio_id for leader in matches] == ["5014426348046646785"]


def test_public_directory_resolves_popular_link_id_from_fast_aum_ranking() -> None:
    target_id = "4788776444236355328"
    target = {
        **_leader(),
        "leadPortfolioId": target_id,
        "nickname": "意钦",
        "currentCopyCount": 999,
        "maxCopyCount": 1000,
    }
    requested: list[tuple[str, int]] = []

    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> PublicHttpResult:
        del method, url, headers
        request = json.loads(body)
        requested.append((str(request["dataType"]), int(request["pageNumber"])))
        if request["dataType"] == "AUM":
            return _response({"list": [target], "total": 1})
        raise AssertionError("deep ROI fallback should not be needed")

    leader = BinancePublicCopyClient(transport=transport).find_leader(target_id)

    assert leader.nickname == "意钦"
    assert leader.current_copy_count == 999
    assert requested == [("AUM", 1)]


def test_identical_source_order_from_two_leaders_has_independent_identity_and_signal() -> None:
    raw = {
        "symbol": "ETHUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "positionSide": "LONG",
        "executedQty": "2",
        "avgPrice": "2000",
        "totalPnl": "0",
        "orderTime": 1_700_000_000_000,
        "orderUpdateTime": 1_700_000_001_000,
    }
    first = PublicLeaderOrder.from_api("5108371059752839168", raw)
    second = PublicLeaderOrder.from_api("5108371059752839169", raw)
    first_signal = NormalizedSignal.from_order(first, delta_quantity=Decimal("2"))
    second_signal = NormalizedSignal.from_order(second, delta_quantity=Decimal("2"))

    assert first.identity_key != second.identity_key
    assert first.event_key != second.event_key
    assert first_signal.signal_id != second_signal.signal_id


def test_order_history_catch_up_pages_until_persisted_watermark() -> None:
    calls: list[int] = []

    def row(update_time: int) -> dict[str, object]:
        return {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "MARKET",
            "positionSide": "SHORT",
            "executedQty": "1",
            "avgPrice": "60000",
            "totalPnl": "0",
            "orderTime": update_time,
            "orderUpdateTime": update_time,
        }

    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> PublicHttpResult:
        del method, url, headers
        page_number = int(json.loads(body)["pageNumber"])
        calls.append(page_number)
        rows = [row(400), row(300)] if page_number == 1 else [row(200), row(100)]
        return _response({"list": rows, "total": 4})

    page = BinancePublicCopyClient(transport=transport).order_history_since(
        "5108371059752839168",
        after_update_time_ms=200,
        page_size=2,
        maximum_pages=2,
    )

    assert calls == [1, 2]
    assert [order.update_time_ms for order in page.orders] == [200, 300, 400]


def test_order_history_catch_up_ignores_ambiguity_older_than_persisted_watermark() -> None:
    def row(*, quantity: str, price: str, order_time: int, update_time: int) -> dict[str, object]:
        return {
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "LIMIT",
            "positionSide": "SHORT",
            "executedQty": quantity,
            "avgPrice": price,
            "totalPnl": "0",
            "orderTime": order_time,
            "orderUpdateTime": update_time,
        }

    old_ladder_orders = [
        row(quantity="1", price="60000", order_time=100, update_time=100),
        row(quantity="2", price="60100", order_time=100, update_time=150),
    ]
    recent_order = row(quantity="1", price="60200", order_time=300, update_time=300)
    client = BinancePublicCopyClient(
        transport=lambda method, url, headers, body: _response(
            {"list": [recent_order, *old_ladder_orders], "total": 3}
        )
    )

    page = client.order_history_since(
        "5108371059752839168",
        after_update_time_ms=200,
        page_size=100,
        maximum_pages=1,
    )

    assert [order.update_time_ms for order in page.orders] == [300]


def test_order_history_catch_up_fails_closed_when_page_budget_cannot_cover_watermark() -> None:
    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> PublicHttpResult:
        del method, url, headers, body
        rows = [
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "MARKET",
                "positionSide": "SHORT",
                "executedQty": "1",
                "avgPrice": "60000",
                "totalPnl": "0",
                "orderTime": update_time,
                "orderUpdateTime": update_time,
            }
            for update_time in (400, 300)
        ]
        return _response({"list": rows, "total": 2000})

    with pytest.raises(BinancePublicCopyError, match="WATERMARK_NOT_COVERED"):
        BinancePublicCopyClient(transport=transport).order_history_since(
            "5108371059752839168",
            after_update_time_ms=100,
            page_size=2,
            maximum_pages=1,
        )


def test_public_client_fails_closed_on_web_challenge() -> None:
    client = BinancePublicCopyClient(
        transport=lambda method, url, headers, body: PublicHttpResult(202, {}, b""),
    )
    with pytest.raises(BinancePublicCopyError, match="ACCESS_DENIED"):
        client.list_leaders()


def test_public_client_retries_transient_business_rejection() -> None:
    calls = 0
    delays: list[float] = []

    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> PublicHttpResult:
        nonlocal calls
        del method, url, headers, body
        calls += 1
        if calls == 1:
            return PublicHttpResult(
                200,
                {},
                b'{"success":false,"code":"100001003","data":null}',
            )
        return _response({"list": [_leader()], "total": 1})

    page = BinancePublicCopyClient(
        transport=transport,
        sleeper=delays.append,
    ).list_leaders()

    assert len(page.leaders) == 1
    assert calls == 2
    assert delays == [0.5]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_public_client_retries_transient_http_status(status: int) -> None:
    calls = 0
    delays: list[float] = []

    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> PublicHttpResult:
        nonlocal calls
        del method, url, headers, body
        calls += 1
        if calls < 3:
            return PublicHttpResult(status, {}, b"temporary")
        return _response({"list": [_leader()], "total": 1})

    page = BinancePublicCopyClient(
        transport=transport,
        sleeper=delays.append,
    ).list_leaders()

    assert len(page.leaders) == 1
    assert calls == 3
    assert delays == [0.5, 1.0]


def test_public_client_does_not_retry_access_challenge() -> None:
    calls = 0

    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> PublicHttpResult:
        nonlocal calls
        del method, url, headers, body
        calls += 1
        return PublicHttpResult(403, {}, b"challenge")

    with pytest.raises(BinancePublicCopyError, match="ACCESS_DENIED"):
        BinancePublicCopyClient(transport=transport, sleeper=lambda _delay: None).list_leaders()
    assert calls == 1


def test_public_client_rejects_invalid_success_payload_without_retry() -> None:
    calls = 0

    def transport(
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> PublicHttpResult:
        nonlocal calls
        del method, url, headers, body
        calls += 1
        return PublicHttpResult(200, {}, b"not-json")

    with pytest.raises(BinancePublicCopyError, match="INVALID_JSON"):
        BinancePublicCopyClient(transport=transport, sleeper=lambda _delay: None).list_leaders()
    assert calls == 1


def test_public_client_fails_closed_when_rows_have_ambiguous_derived_identity() -> None:
    first = {
        "symbol": "ETHUSDT",
        "side": "BUY",
        "type": "MARKET",
        "positionSide": "LONG",
        "executedQty": "1",
        "avgPrice": "2000",
        "totalPnl": "0",
        "orderTime": 1_700_000_000_000,
        "orderUpdateTime": 1_700_000_001_000,
    }
    second = {
        **first,
        "executedQty": "2",
        "avgPrice": "2001",
        "orderUpdateTime": 1_700_000_002_000,
    }
    client = BinancePublicCopyClient(
        transport=lambda method, url, headers, body: _response(
            {"list": [first, second], "total": 2}
        )
    )

    with pytest.raises(BinancePublicCopyError, match="IDENTITY_AMBIGUOUS"):
        client.order_history("5108371059752839168")


def test_public_client_baseline_tolerates_only_identity_collisions_older_than_fence() -> None:
    first = {
        "symbol": "ETHUSDT",
        "side": "BUY",
        "type": "MARKET",
        "positionSide": "LONG",
        "executedQty": "1",
        "avgPrice": "2000",
        "totalPnl": "0",
        "orderTime": 1_700_000_000_000,
        "orderUpdateTime": 1_700_000_001_000,
    }
    second = {
        **first,
        "executedQty": "2",
        "avgPrice": "2001",
        "orderUpdateTime": 1_700_000_002_000,
    }
    client = BinancePublicCopyClient(
        transport=lambda method, url, headers, body: _response(
            {"list": [first, second], "total": 2}
        )
    )

    baseline = client.order_history_baseline(
        "5108371059752839168",
        identity_guard_after_ms=1_700_000_003_000,
    )

    assert len(baseline.orders) == 2
    assert len({order.identity_key for order in baseline.orders}) == 1
    with pytest.raises(BinancePublicCopyError, match="IDENTITY_AMBIGUOUS"):
        client.order_history_baseline(
            "5108371059752839168",
            identity_guard_after_ms=1_700_000_001_000,
        )


def test_public_client_disambiguates_same_millisecond_limit_ladder_by_price() -> None:
    first = {
        "symbol": "LABUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "positionSide": "LONG",
        "executedQty": "1565",
        "avgPrice": "0.1417999",
        "totalPnl": "0",
        "orderTime": 1_700_000_000_000,
        "orderUpdateTime": 1_700_000_001_000,
    }
    second = {
        **first,
        "executedQty": "1554",
        "avgPrice": "0.1427999",
        "orderUpdateTime": 1_700_000_001_001,
    }
    client = BinancePublicCopyClient(
        transport=lambda method, url, headers, body: _response(
            {"list": [first, second], "total": 2}
        )
    )

    page = client.order_history("5108371059752839168")

    assert len(page.orders) == 2
    assert len({order.identity_key for order in page.orders}) == 2


def test_limit_ladder_identity_is_stable_when_fill_quantity_changes() -> None:
    raw = {
        "symbol": "LABUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "positionSide": "LONG",
        "executedQty": "100",
        "avgPrice": "0.1417999",
        "totalPnl": "0",
        "orderTime": 1_700_000_000_000,
        "orderUpdateTime": 1_700_000_001_000,
    }
    partial = PublicLeaderOrder.from_api("5108371059752839168", raw)
    complete = PublicLeaderOrder.from_api(
        "5108371059752839168",
        {**raw, "executedQty": "200", "orderUpdateTime": 1_700_000_002_000},
    )

    partial = partial.with_identity_discriminator("LIMIT_AVG_PRICE:0.1417999")
    complete = complete.with_identity_discriminator("LIMIT_AVG_PRICE:0.1417999")

    assert partial.identity_key == complete.identity_key
    assert partial.event_key != complete.event_key


def test_public_client_keeps_limit_collision_fail_closed_when_price_is_not_unique() -> None:
    first = {
        "symbol": "LABUSDT",
        "side": "BUY",
        "type": "LIMIT",
        "positionSide": "LONG",
        "executedQty": "100",
        "avgPrice": "0.14",
        "totalPnl": "0",
        "orderTime": 1_700_000_000_000,
        "orderUpdateTime": 1_700_000_001_000,
    }
    second = {**first, "executedQty": "200", "orderUpdateTime": 1_700_000_001_001}
    client = BinancePublicCopyClient(
        transport=lambda method, url, headers, body: _response(
            {"list": [first, second], "total": 2}
        )
    )

    with pytest.raises(BinancePublicCopyError, match="IDENTITY_AMBIGUOUS"):
        client.order_history("5108371059752839168")


def test_first_poll_is_baseline_and_never_replays_history() -> None:
    tracker = LeaderOrderTracker()
    old_open = _order()

    assert tracker.ingest((old_open,)) == ()
    assert tracker.ingest((old_open,)) == ()

    new_reduce = _order(
        side="SELL",
        quantity="1",
        order_time=1_700_000_002_000,
        update_time=1_700_000_003_000,
    )
    signals = tracker.ingest((new_reduce, old_open))
    assert len(signals) == 1
    assert signals[0].kind is SignalKind.REDUCE
    assert signals[0].source_delta_quantity == Decimal("1")


def test_cumulative_partial_fill_emits_only_increment_and_survives_restart() -> None:
    tracker = LeaderOrderTracker()
    tracker.ingest(())
    partial = _order(quantity="1")
    completed = _order(quantity="2", update_time=1_700_000_002_000)

    first = tracker.ingest((partial,))
    second = tracker.ingest((completed,))
    restored = LeaderOrderTracker(tracker.snapshot())

    assert first[0].source_delta_quantity == Decimal("1")
    assert second[0].source_delta_quantity == Decimal("1")
    assert restored.ingest((completed,)) == ()


@pytest.mark.parametrize(
    ("position_side", "side", "kind"),
    [
        ("LONG", "BUY", SignalKind.INCREASE),
        ("LONG", "SELL", SignalKind.REDUCE),
        ("SHORT", "SELL", SignalKind.INCREASE),
        ("SHORT", "BUY", SignalKind.REDUCE),
    ],
)
def test_hedge_side_mapping(
    position_side: str,
    side: str,
    kind: SignalKind,
) -> None:
    tracker = LeaderOrderTracker()
    tracker.ingest(())
    signal = tracker.ingest((_order(position_side=position_side, side=side),))[0]
    assert signal.kind is kind


def test_ambiguous_one_way_source_order_is_archived_without_signal() -> None:
    tracker = LeaderOrderTracker()
    tracker.ingest(())
    assert tracker.ingest((_order(position_side="BOTH", side="BUY"),)) == ()
