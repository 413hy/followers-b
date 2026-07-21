from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ai_quant.binance_egress.testnet_probe import (
    BinanceTestnetClient,
    HttpResult,
    run_safe_testnet_probe,
    run_testnet_native_protection,
    run_testnet_order_lifecycle,
    run_testnet_risk_profile,
)


def _result(document: Any) -> HttpResult:
    return HttpResult(200, {}, json.dumps(document).encode())


def test_listen_key_keepalive_uses_only_testnet_rest_destination() -> None:
    calls: list[tuple[str, str]] = []

    def transport(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> HttpResult:
        del headers, body
        calls.append((method, url))
        return _result({})

    client = BinanceTestnetClient("test-key", "test-secret", transport=transport)
    client.keepalive_listen_key("a" * 32)

    assert len(calls) == 1
    assert calls[0][0] == "PUT"
    assert calls[0][1].startswith("https://demo-fapi.binance.com/fapi/v1/listenKey?")
    assert "listenKey=" + "a" * 32 in calls[0][1]


def test_hedge_mode_and_account_information_use_signed_testnet_only() -> None:
    calls: list[tuple[str, str]] = []

    def transport(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> HttpResult:
        del headers, body
        calls.append((method, url))
        if "/positionSide/dual" in url:
            return _result({"code": 200, "msg": "success"})
        return _result({"totalWalletBalance": "150"})

    client = BinanceTestnetClient("test-key", "test-secret", transport=transport)
    client.change_position_mode(hedge_mode=True)
    account = client.account_information()
    client.account_information_v2()

    assert account["totalWalletBalance"] == "150"
    assert calls[0][0] == "POST"
    assert calls[0][1].startswith(
        "https://demo-fapi.binance.com/fapi/v1/positionSide/dual?dualSidePosition=true"
    )
    assert calls[1][0] == "GET"
    assert calls[1][1].startswith("https://demo-fapi.binance.com/fapi/v3/account?")
    assert calls[2][1].startswith("https://demo-fapi.binance.com/fapi/v2/account?")


def test_all_open_orders_is_signed_and_has_no_symbol_filter() -> None:
    calls: list[tuple[str, str]] = []

    def transport(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> HttpResult:
        del headers, body
        calls.append((method, url))
        return _result([])

    client = BinanceTestnetClient("test-key", "test-secret", transport=transport)
    assert client.all_open_orders() == []
    parsed = urllib.parse.urlparse(calls[0][1])
    query = urllib.parse.parse_qs(parsed.query)
    assert calls[0][0] == "GET"
    assert parsed.path == "/fapi/v1/openOrders"
    assert "symbol" not in query
    assert "signature" in query


def test_safe_probe_never_uses_production_and_redacts_ephemeral_values(tmp_path: Path) -> None:
    key = tmp_path / "key"
    secret = tmp_path / "secret"
    key.write_text("test-key", encoding="ascii")
    secret.write_text("test-secret", encoding="ascii")
    key.chmod(0o400)
    secret.chmod(0o400)
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    seen_urls: list[str] = []
    websocket_paths: list[tuple[str, str]] = []

    def transport(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> HttpResult:
        del method, headers, body
        seen_urls.append(url)
        parsed = urllib.parse.urlparse(url)
        if parsed.path == "/fapi/v1/time":
            return _result({"serverTime": 1_800_000_000_000})
        if parsed.path == "/fapi/v1/exchangeInfo":
            return _result(
                {
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "status": "TRADING",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                                {
                                    "filterType": "LOT_SIZE",
                                    "minQty": "0.001",
                                    "stepSize": "0.001",
                                },
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                }
            )
        if parsed.path == "/fapi/v1/ticker/bookTicker":
            return _result({"bidPrice": "100000.0", "askPrice": "100000.1"})
        if parsed.path == "/fapi/v1/positionSide/dual":
            return _result({"dualSidePosition": False})
        if parsed.path == "/fapi/v1/symbolConfig":
            return _result([{"symbol": "BTCUSDT", "marginType": "CROSSED"}])
        if parsed.path in {"/fapi/v1/openOrders", "/fapi/v3/positionRisk"}:
            return _result([])
        if parsed.path == "/fapi/v1/order/test":
            return _result({})
        if parsed.path == "/fapi/v1/listenKey":
            return _result({"listenKey": "private-listen-key-value"})
        raise AssertionError(parsed.path)

    evidence = run_safe_testnet_probe(
        api_key_file=key,
        api_secret_file=secret,
        repository_root=repository_root,
        transport=transport,
        websocket_probe=lambda host, path: websocket_paths.append((host, path)),
    )

    assert evidence["result"] == "PASS"
    assert evidence["matching_engine_orders_created"] == 0
    assert all(url.startswith("https://demo-fapi.binance.com/") for url in seen_urls)
    assert "test-secret" not in json.dumps(evidence)
    assert "private-listen-key-value" not in json.dumps(evidence)
    assert any(path.startswith("/private/ws/") for _, path in websocket_paths)


def test_real_order_lifecycle_places_queries_cancels_and_finishes_flat(tmp_path: Path) -> None:
    key = tmp_path / "key"
    secret = tmp_path / "secret"
    key.write_text("test-key", encoding="ascii")
    secret.write_text("test-secret", encoding="ascii")
    key.chmod(0o400)
    secret.chmod(0o400)
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    order_status = "NONE"

    def transport(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> HttpResult:
        nonlocal order_status
        del headers, body
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/fapi/v1/time":
            return _result({"serverTime": time.time_ns() // 1_000_000})
        if parsed.path == "/fapi/v1/positionSide/dual":
            return _result({"dualSidePosition": False})
        if parsed.path == "/fapi/v1/openOrders":
            return _result([])
        if parsed.path == "/fapi/v3/positionRisk":
            return _result([])
        if parsed.path == "/fapi/v1/exchangeInfo":
            return _result(
                {
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "status": "TRADING",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                                {
                                    "filterType": "LOT_SIZE",
                                    "minQty": "0.001",
                                    "stepSize": "0.001",
                                },
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                }
            )
        if parsed.path == "/fapi/v1/ticker/bookTicker":
            return _result({"bidPrice": "100000", "askPrice": "100001"})
        if parsed.path == "/fapi/v1/order/test":
            return _result({})
        if parsed.path == "/fapi/v1/order" and method == "POST":
            order_status = "NEW"
            return _result({"clientOrderId": query["newClientOrderId"][0], "status": "NEW"})
        if parsed.path == "/fapi/v1/order" and method == "GET":
            return _result({"clientOrderId": query["origClientOrderId"][0], "status": order_status})
        if parsed.path == "/fapi/v1/order" and method == "DELETE":
            order_status = "CANCELED"
            return _result({"clientOrderId": query["origClientOrderId"][0], "status": order_status})
        raise AssertionError((method, parsed.path))

    evidence = run_testnet_order_lifecycle(
        api_key_file=key,
        api_secret_file=secret,
        repository_root=repository_root,
        transport=transport,
    )

    assert evidence["result"] == "PASS"
    assert evidence["matching_engine_orders_created"] == 1
    assert evidence["matching_engine_fills"] == 0
    assert evidence["final_status"] == "CANCELED"


def test_native_protection_fills_protects_flattens_and_cleans_algo(tmp_path: Path) -> None:
    key = tmp_path / "key"
    secret = tmp_path / "secret"
    key.write_text("test-key", encoding="ascii")
    secret.write_text("test-secret", encoding="ascii")
    key.chmod(0o400)
    secret.chmod(0o400)
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    position = "0"
    algo_statuses: dict[int, str] = {}
    next_algo_id = 123

    def transport(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> HttpResult:
        nonlocal position, next_algo_id
        del headers, body
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/fapi/v1/time":
            return _result({"serverTime": time.time_ns() // 1_000_000})
        if parsed.path == "/fapi/v1/positionSide/dual":
            return _result({"dualSidePosition": False})
        if parsed.path == "/fapi/v1/openOrders":
            return _result([])
        if parsed.path == "/fapi/v1/openAlgoOrders":
            return _result(
                [
                    {"algoId": algo_id, "clientAlgoId": f"algo-{algo_id}", "algoStatus": status}
                    for algo_id, status in algo_statuses.items()
                    if status != "CANCELED"
                ]
            )
        if parsed.path == "/fapi/v3/positionRisk":
            return _result([] if position == "0" else [{"positionAmt": position}])
        if parsed.path == "/fapi/v1/exchangeInfo":
            return _result(
                {
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "status": "TRADING",
                            "filters": [
                                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                                {
                                    "filterType": "LOT_SIZE",
                                    "minQty": "0.001",
                                    "stepSize": "0.001",
                                },
                                {
                                    "filterType": "MARKET_LOT_SIZE",
                                    "minQty": "0.001",
                                    "stepSize": "0.001",
                                },
                                {"filterType": "MIN_NOTIONAL", "notional": "5"},
                            ],
                        }
                    ]
                }
            )
        if parsed.path == "/fapi/v1/ticker/bookTicker":
            return _result({"bidPrice": "100000", "askPrice": "100001"})
        if parsed.path == "/fapi/v1/premiumIndex":
            return _result({"markPrice": "100000"})
        if parsed.path == "/fapi/v1/order" and method == "POST":
            side = query["side"][0]
            position = "0.001" if side == "BUY" else "0"
            return _result(
                {
                    "clientOrderId": query["newClientOrderId"][0],
                    "status": "FILLED",
                    "updateTime": 1_000,
                }
            )
        if parsed.path == "/fapi/v1/algoOrder" and method == "POST":
            algo_id = next_algo_id
            next_algo_id += 1
            algo_statuses[algo_id] = "NEW"
            return _result(
                {
                    "algoId": algo_id,
                    "clientAlgoId": query["clientAlgoId"][0],
                    "algoStatus": "NEW",
                    "createTime": 1_500 if algo_id == 123 else 1_750,
                }
            )
        if parsed.path == "/fapi/v1/algoOrder" and method == "GET":
            algo_id = int(query["algoId"][0])
            return _result({"algoId": algo_id, "algoStatus": algo_statuses[algo_id]})
        if parsed.path == "/fapi/v1/algoOrder" and method == "DELETE":
            algo_id = int(query["algoId"][0])
            algo_statuses[algo_id] = "CANCELED"
            return _result({"algoId": algo_id, "code": "200"})
        raise AssertionError((method, parsed.path))

    evidence = run_testnet_native_protection(
        api_key_file=key,
        api_secret_file=secret,
        repository_root=repository_root,
        transport=transport,
    )

    assert evidence["result"] == "PASS"
    assert evidence["protection_confirmation_latency_ms"] == 500
    assert evidence["take_profit_confirmation_latency_ms"] == 750
    assert evidence["protection_final_status"] == "CANCELED"
    assert evidence["take_profit_final_status"] == "CANCELED"
    assert evidence["final_position_quantity"] == "0"


def test_risk_profile_selects_exchange_maximum_and_records_current_costs(
    tmp_path: Path,
) -> None:
    key = tmp_path / "key"
    secret = tmp_path / "secret"
    key.write_text("test-key", encoding="ascii")
    secret.write_text("test-secret", encoding="ascii")
    key.chmod(0o400)
    secret.chmod(0o400)
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    changed_to: int | None = None

    def transport(
        method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> HttpResult:
        nonlocal changed_to
        del headers, body
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/fapi/v1/time":
            return _result({"serverTime": time.time_ns() // 1_000_000})
        if parsed.path == "/fapi/v1/positionSide/dual":
            return _result({"dualSidePosition": False})
        if parsed.path in {"/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"}:
            return _result([])
        if parsed.path == "/fapi/v3/positionRisk":
            return _result([])
        if parsed.path == "/fapi/v1/leverageBracket":
            return _result(
                [
                    {
                        "symbol": "BTCUSDT",
                        "brackets": [
                            {"initialLeverage": 125},
                            {"initialLeverage": 50},
                        ],
                    }
                ]
            )
        if parsed.path == "/fapi/v1/commissionRate":
            return _result(
                {
                    "symbol": "BTCUSDT",
                    "makerCommissionRate": "0.0002",
                    "takerCommissionRate": "0.0004",
                }
            )
        if parsed.path == "/fapi/v1/leverage" and method == "POST":
            changed_to = int(query["leverage"][0])
            return _result(
                {
                    "symbol": "BTCUSDT",
                    "leverage": changed_to,
                    "maxNotionalValue": "1000000",
                }
            )
        raise AssertionError((method, parsed.path))

    evidence = run_testnet_risk_profile(
        api_key_file=key,
        api_secret_file=secret,
        repository_root=repository_root,
        transport=transport,
    )

    assert changed_to == 125
    assert evidence["exchange_maximum_initial_leverage"] == 125
    assert evidence["leverage_policy"] == "EXCHANGE_MAXIMUM"
    assert evidence["selected_initial_leverage"] == 125
    assert evidence["maker_commission_rate"] == "0.0002"
    assert evidence["taker_commission_rate"] == "0.0004"
    assert evidence["matching_engine_orders_created"] == 0
