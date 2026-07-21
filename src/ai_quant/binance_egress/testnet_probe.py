"""Bounded Binance USD-M Testnet capability probe owned by the egress package."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from ai_quant.common.private_files import read_private_file

TESTNET_REST_BASE = "https://demo-fapi.binance.com"
TESTNET_STREAM_HOST = "demo-fstream.binance.com"
TESTNET_WS_API_HOST = "testnet.binancefuture.com"
PRODUCTION_REST_BASE = "https://fapi.binance.com"
PRODUCTION_STREAM_HOST = "fstream.binance.com"
PRODUCTION_WS_API_HOST = "ws-fapi.binance.com"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class TestnetProbeError(RuntimeError):
    """A capability probe failed without exposing credential material."""


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[str, str, Mapping[str, str], bytes | None], HttpResult]
WebSocketProbe = Callable[[str, str], None]


class BinanceFuturesEnvironment(StrEnum):
    TESTNET = "TESTNET"
    PRODUCTION = "PRODUCTION"


@dataclass(frozen=True, slots=True)
class BinanceFuturesEndpointProfile:
    environment: BinanceFuturesEnvironment
    rest_base: str
    stream_host: str
    websocket_api_host: str


TESTNET_ENDPOINTS = BinanceFuturesEndpointProfile(
    environment=BinanceFuturesEnvironment.TESTNET,
    rest_base=TESTNET_REST_BASE,
    stream_host=TESTNET_STREAM_HOST,
    websocket_api_host=TESTNET_WS_API_HOST,
)
PRODUCTION_ENDPOINTS = BinanceFuturesEndpointProfile(
    environment=BinanceFuturesEnvironment.PRODUCTION,
    rest_base=PRODUCTION_REST_BASE,
    stream_host=PRODUCTION_STREAM_HOST,
    websocket_api_host=PRODUCTION_WS_API_HOST,
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
) -> HttpResult:
    if not url.startswith(f"{TESTNET_REST_BASE}/fapi/"):
        raise TestnetProbeError("TESTNET_DESTINATION_DENIED")
    request = urllib.request.Request(  # noqa: S310 -- exact HTTPS origin checked above
        url, data=body, headers=dict(headers), method=method
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise TestnetProbeError("TESTNET_RESPONSE_TOO_LARGE")
            return HttpResult(response.status, dict(response.headers.items()), payload)
    except urllib.error.HTTPError as exc:
        payload = exc.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise TestnetProbeError("TESTNET_RESPONSE_TOO_LARGE") from exc
        return HttpResult(exc.code, dict(exc.headers.items()), payload)
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise TestnetProbeError("TESTNET_TRANSPORT_FAILED") from exc


def _production_urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
) -> HttpResult:
    if not url.startswith(f"{PRODUCTION_REST_BASE}/fapi/"):
        raise TestnetProbeError("PRODUCTION_DESTINATION_DENIED")
    request = urllib.request.Request(  # noqa: S310 -- exact HTTPS origin checked above
        url, data=body, headers=dict(headers), method=method
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise TestnetProbeError("PRODUCTION_RESPONSE_TOO_LARGE")
            return HttpResult(response.status, dict(response.headers.items()), payload)
    except urllib.error.HTTPError as exc:
        payload = exc.read(MAX_RESPONSE_BYTES + 1)
        if len(payload) > MAX_RESPONSE_BYTES:
            raise TestnetProbeError("PRODUCTION_RESPONSE_TOO_LARGE") from exc
        return HttpResult(exc.code, dict(exc.headers.items()), payload)
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise TestnetProbeError("PRODUCTION_TRANSPORT_FAILED") from exc


def websocket_upgrade(host: str, path: str) -> None:
    """Perform an exact TLS WebSocket upgrade and immediately close the socket."""
    if host not in {TESTNET_STREAM_HOST, TESTNET_WS_API_HOST}:
        raise TestnetProbeError("TESTNET_WEBSOCKET_HOST_DENIED")
    if not path.startswith("/") or "\r" in path or "\n" in path:
        raise TestnetProbeError("TESTNET_WEBSOCKET_PATH_INVALID")
    nonce = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Connection: Upgrade\r\n"
        "Upgrade: websocket\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Key: {nonce}\r\n"
        "User-Agent: aiq-testnet-probe/1\r\n\r\n"
    ).encode("ascii")
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, 443), timeout=10) as raw:
            with context.wrap_socket(raw, server_hostname=host) as connection:
                connection.settimeout(10)
                connection.sendall(request)
                response = bytearray()
                while b"\r\n\r\n" not in response and len(response) <= 16_384:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
    except (OSError, TimeoutError, ssl.SSLError) as exc:
        raise TestnetProbeError("TESTNET_WEBSOCKET_UPGRADE_FAILED") from exc
    status_line = bytes(response).split(b"\r\n", 1)[0]
    if status_line != b"HTTP/1.1 101 Switching Protocols":
        raise TestnetProbeError("TESTNET_WEBSOCKET_UPGRADE_REJECTED")


def _json_object(result: HttpResult, operation: str) -> dict[str, Any]:
    try:
        document = json.loads(result.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TestnetProbeError(f"{operation}_INVALID_JSON") from exc
    if not 200 <= result.status < 300:
        code = document.get("code") if isinstance(document, dict) else None
        raise TestnetProbeError(f"{operation}_HTTP_{result.status}_CODE_{code}")
    if not isinstance(document, dict):
        raise TestnetProbeError(f"{operation}_INVALID_RESPONSE")
    return document


def _json_list(result: HttpResult, operation: str) -> list[dict[str, Any]]:
    try:
        document = json.loads(result.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TestnetProbeError(f"{operation}_INVALID_JSON") from exc
    if not 200 <= result.status < 300:
        code = document.get("code") if isinstance(document, dict) else None
        raise TestnetProbeError(f"{operation}_HTTP_{result.status}_CODE_{code}")
    if not isinstance(document, list) or not all(isinstance(item, dict) for item in document):
        raise TestnetProbeError(f"{operation}_INVALID_RESPONSE")
    return document


def _json_array(result: HttpResult, operation: str) -> list[Any]:
    try:
        document = json.loads(result.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TestnetProbeError(f"{operation}_INVALID_JSON") from exc
    if not 200 <= result.status < 300:
        code = document.get("code") if isinstance(document, dict) else None
        raise TestnetProbeError(f"{operation}_HTTP_{result.status}_CODE_{code}")
    if not isinstance(document, list):
        raise TestnetProbeError(f"{operation}_INVALID_RESPONSE")
    return document


class BinanceTestnetClient:
    """Small exact-destination client for an attended, bounded Testnet probe."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        transport: Transport = _urllib_transport,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if not api_key or not api_secret or any(char.isspace() for char in api_key + api_secret):
            raise TestnetProbeError("TESTNET_CREDENTIAL_FORMAT_INVALID")
        self._api_key = api_key
        self._api_secret = api_secret.encode("ascii")
        self._transport = transport
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._server_offset_ms = 0
        self._endpoint_profile = TESTNET_ENDPOINTS
        self._user_agent = "aiq-copy-testnet/1"

    def _call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        signed: bool = False,
        api_key_required: bool = False,
    ) -> HttpResult:
        if not path.startswith("/fapi/") or ".." in path:
            raise TestnetProbeError("TESTNET_PATH_DENIED")
        values = dict(params or {})
        if signed:
            values["timestamp"] = self._clock_ms() + self._server_offset_ms
            values["recvWindow"] = 5000
        query = urllib.parse.urlencode(values)
        if signed:
            signature = hmac.new(
                self._api_secret, query.encode("ascii"), hashlib.sha256
            ).hexdigest()
            query = f"{query}&signature={signature}"
        url = f"{self._endpoint_profile.rest_base}{path}"
        if query:
            url = f"{url}?{query}"
        headers = {"Accept": "application/json", "User-Agent": self._user_agent}
        if signed or api_key_required:
            headers["X-MBX-APIKEY"] = self._api_key
        return self._transport(method, url, headers, b"" if method in {"POST", "PUT"} else None)

    def synchronize_time(self) -> tuple[int, int]:
        before = self._clock_ms()
        document = _json_object(self._call("GET", "/fapi/v1/time"), "SERVER_TIME")
        after = self._clock_ms()
        server_time = document.get("serverTime")
        if not isinstance(server_time, int):
            raise TestnetProbeError("SERVER_TIME_INVALID_RESPONSE")
        midpoint = before + ((after - before) // 2)
        self._server_offset_ms = server_time - midpoint
        return server_time, self._server_offset_ms

    def exchange_info(self) -> dict[str, Any]:
        return _json_object(self._call("GET", "/fapi/v1/exchangeInfo"), "EXCHANGE_INFO")

    def book_ticker(self, symbol: str) -> dict[str, Any]:
        return _json_object(
            self._call("GET", "/fapi/v1/ticker/bookTicker", params={"symbol": symbol}),
            "BOOK_TICKER",
        )

    def klines(self, symbol: str, interval: str, *, limit: int) -> list[Any]:
        return _json_array(
            self._call(
                "GET",
                "/fapi/v1/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit},
            ),
            "KLINES",
        )

    def depth(self, symbol: str, *, limit: int) -> dict[str, Any]:
        return _json_object(
            self._call("GET", "/fapi/v1/depth", params={"symbol": symbol, "limit": limit}),
            "DEPTH",
        )

    def aggregate_trades(self, symbol: str, *, limit: int) -> list[dict[str, Any]]:
        return _json_list(
            self._call(
                "GET",
                "/fapi/v1/aggTrades",
                params={"symbol": symbol, "limit": limit},
            ),
            "AGGREGATE_TRADES",
        )

    def mark_price(self, symbol: str) -> dict[str, Any]:
        return _json_object(
            self._call("GET", "/fapi/v1/premiumIndex", params={"symbol": symbol}),
            "MARK_PRICE",
        )

    def position_mode(self) -> dict[str, Any]:
        return _json_object(
            self._call("GET", "/fapi/v1/positionSide/dual", signed=True),
            "POSITION_MODE",
        )

    def change_position_mode(self, *, hedge_mode: bool) -> dict[str, Any]:
        return _json_object(
            self._call(
                "POST",
                "/fapi/v1/positionSide/dual",
                params={"dualSidePosition": "true" if hedge_mode else "false"},
                signed=True,
            ),
            "CHANGE_POSITION_MODE",
        )

    def account_information(self) -> dict[str, Any]:
        return _json_object(
            self._call("GET", "/fapi/v3/account", signed=True),
            "ACCOUNT_INFORMATION",
        )

    def account_information_v2(self) -> dict[str, Any]:
        return _json_object(
            self._call("GET", "/fapi/v2/account", signed=True),
            "ACCOUNT_INFORMATION_V2",
        )

    def symbol_config(self, symbol: str) -> list[dict[str, Any]]:
        return _json_list(
            self._call("GET", "/fapi/v1/symbolConfig", params={"symbol": symbol}, signed=True),
            "SYMBOL_CONFIG",
        )

    def leverage_brackets(self, symbol: str) -> list[dict[str, Any]]:
        result = self._call(
            "GET", "/fapi/v1/leverageBracket", params={"symbol": symbol}, signed=True
        )
        try:
            document = json.loads(result.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TestnetProbeError("LEVERAGE_BRACKET_INVALID_JSON") from exc
        if not 200 <= result.status < 300:
            code = document.get("code") if isinstance(document, dict) else None
            raise TestnetProbeError(f"LEVERAGE_BRACKET_HTTP_{result.status}_CODE_{code}")
        if isinstance(document, dict):
            document = [document]
        if not isinstance(document, list) or not all(isinstance(item, dict) for item in document):
            raise TestnetProbeError("LEVERAGE_BRACKET_INVALID_RESPONSE")
        return document

    def commission_rate(self, symbol: str) -> dict[str, Any]:
        return _json_object(
            self._call("GET", "/fapi/v1/commissionRate", params={"symbol": symbol}, signed=True),
            "COMMISSION_RATE",
        )

    def change_initial_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        if not 1 <= leverage <= 125:
            raise TestnetProbeError("EXCHANGE_LEVERAGE_RANGE_INVALID")
        return _json_object(
            self._call(
                "POST",
                "/fapi/v1/leverage",
                params={"symbol": symbol, "leverage": leverage},
                signed=True,
            ),
            "CHANGE_INITIAL_LEVERAGE",
        )

    def open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return _json_list(
            self._call("GET", "/fapi/v1/openOrders", params={"symbol": symbol}, signed=True),
            "OPEN_ORDERS",
        )

    def all_open_orders(self) -> list[dict[str, Any]]:
        return _json_list(
            self._call("GET", "/fapi/v1/openOrders", signed=True),
            "ALL_OPEN_ORDERS",
        )

    def position_risk(self, symbol: str) -> list[dict[str, Any]]:
        return _json_list(
            self._call("GET", "/fapi/v3/positionRisk", params={"symbol": symbol}, signed=True),
            "POSITION_RISK",
        )

    def account_trades(self, symbol: str, *, start_time_ms: int) -> list[dict[str, Any]]:
        return _json_list(
            self._call(
                "GET",
                "/fapi/v1/userTrades",
                params={"symbol": symbol, "startTime": start_time_ms, "limit": 100},
                signed=True,
            ),
            "ACCOUNT_TRADES",
        )

    def create_listen_key(self) -> str:
        document = _json_object(
            self._call("POST", "/fapi/v1/listenKey", api_key_required=True),
            "LISTEN_KEY_CREATE",
        )
        listen_key = document.get("listenKey")
        if not isinstance(listen_key, str) or len(listen_key) < 20:
            raise TestnetProbeError("LISTEN_KEY_CREATE_INVALID_RESPONSE")
        return listen_key

    def keepalive_listen_key(self, listen_key: str) -> None:
        if len(listen_key) < 20 or any(char.isspace() for char in listen_key):
            raise TestnetProbeError("LISTEN_KEY_INVALID")
        _json_object(
            self._call(
                "PUT",
                "/fapi/v1/listenKey",
                params={"listenKey": listen_key},
                api_key_required=True,
            ),
            "LISTEN_KEY_KEEPALIVE",
        )

    def close_listen_key(self, listen_key: str) -> None:
        _json_object(
            self._call(
                "DELETE",
                "/fapi/v1/listenKey",
                params={"listenKey": listen_key},
                api_key_required=True,
            ),
            "LISTEN_KEY_CLOSE",
        )

    def test_order(self, params: Mapping[str, str]) -> None:
        _json_object(
            self._call("POST", "/fapi/v1/order/test", params=params, signed=True),
            "TEST_ORDER",
        )

    def place_order(self, params: Mapping[str, str]) -> dict[str, Any]:
        return _json_object(
            self._call("POST", "/fapi/v1/order", params=params, signed=True),
            "PLACE_ORDER",
        )

    def query_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        return _json_object(
            self._call(
                "GET",
                "/fapi/v1/order",
                params={"symbol": symbol, "origClientOrderId": client_order_id},
                signed=True,
            ),
            "QUERY_ORDER",
        )

    def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        return _json_object(
            self._call(
                "DELETE",
                "/fapi/v1/order",
                params={"symbol": symbol, "origClientOrderId": client_order_id},
                signed=True,
            ),
            "CANCEL_ORDER",
        )

    def place_algo_order(self, params: Mapping[str, str]) -> dict[str, Any]:
        return _json_object(
            self._call("POST", "/fapi/v1/algoOrder", params=params, signed=True),
            "PLACE_ALGO_ORDER",
        )

    def query_algo_order(
        self, *, client_algo_id: str | None = None, algo_id: int | None = None
    ) -> dict[str, Any]:
        if (client_algo_id is None) == (algo_id is None):
            raise TestnetProbeError("ALGO_QUERY_ID_INVALID")
        params: dict[str, str | int] = (
            {"algoId": algo_id} if algo_id is not None else {"clientAlgoId": str(client_algo_id)}
        )
        return _json_object(
            self._call(
                "GET",
                "/fapi/v1/algoOrder",
                params=params,
                signed=True,
            ),
            "QUERY_ALGO_ORDER",
        )

    def cancel_algo_order(
        self, *, client_algo_id: str | None = None, algo_id: int | None = None
    ) -> dict[str, Any]:
        if (client_algo_id is None) == (algo_id is None):
            raise TestnetProbeError("ALGO_CANCEL_ID_INVALID")
        params: dict[str, str | int] = (
            {"algoId": algo_id} if algo_id is not None else {"clientAlgoId": str(client_algo_id)}
        )
        return _json_object(
            self._call(
                "DELETE",
                "/fapi/v1/algoOrder",
                params=params,
                signed=True,
            ),
            "CANCEL_ALGO_ORDER",
        )

    def open_algo_orders(self, symbol: str) -> list[dict[str, Any]]:
        return _json_list(
            self._call("GET", "/fapi/v1/openAlgoOrders", params={"symbol": symbol}, signed=True),
            "OPEN_ALGO_ORDERS",
        )


class BinanceProductionClient(BinanceTestnetClient):
    """Exact-origin production client; construction is gated by the service layer."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        transport: Transport = _production_urllib_transport,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        super().__init__(
            api_key,
            api_secret,
            transport=transport,
            clock_ms=clock_ms,
        )
        self._endpoint_profile = PRODUCTION_ENDPOINTS
        self._user_agent = "aiq-copy-production/1"


def _decimal_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
    return (value / step).to_integral_value(rounding=rounding) * step


def _order_test_parameters(
    exchange_info: Mapping[str, Any],
    book_ticker: Mapping[str, Any],
    symbol: str,
) -> dict[str, str]:
    symbols = exchange_info.get("symbols")
    if not isinstance(symbols, list):
        raise TestnetProbeError("EXCHANGE_INFO_INVALID_RESPONSE")
    symbol_info = next(
        (item for item in symbols if isinstance(item, dict) and item.get("symbol") == symbol),
        None,
    )
    if not isinstance(symbol_info, dict) or symbol_info.get("status") != "TRADING":
        raise TestnetProbeError("TEST_SYMBOL_NOT_TRADING")
    filters = symbol_info.get("filters")
    if not isinstance(filters, list):
        raise TestnetProbeError("TEST_SYMBOL_FILTERS_INVALID")
    by_type = {item.get("filterType"): item for item in filters if isinstance(item, dict)}
    try:
        tick_size = Decimal(str(by_type["PRICE_FILTER"]["tickSize"]))
        lot = by_type["LOT_SIZE"]
        step_size = Decimal(str(lot["stepSize"]))
        minimum_quantity = Decimal(str(lot["minQty"]))
        minimum_notional = Decimal(str(by_type.get("MIN_NOTIONAL", {}).get("notional", "0")))
        bid_price = Decimal(str(book_ticker["bidPrice"]))
    except (KeyError, ArithmeticError) as exc:
        raise TestnetProbeError("TEST_SYMBOL_FILTERS_INVALID") from exc
    price = _decimal_step(bid_price * Decimal("0.90"), tick_size, ROUND_FLOOR)
    quantity = minimum_quantity
    if minimum_notional > 0:
        quantity = max(
            quantity,
            _decimal_step(minimum_notional / price, step_size, ROUND_CEILING),
        )
    return {
        "symbol": symbol,
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTX",
        "quantity": format(quantity, "f"),
        "price": format(price, "f"),
        "newClientOrderId": f"aq-t-probe-{secrets.token_hex(6)}",
    }


def _symbol_filters(exchange_info: Mapping[str, Any], symbol: str) -> dict[str, dict[str, Any]]:
    symbols = exchange_info.get("symbols")
    if not isinstance(symbols, list):
        raise TestnetProbeError("EXCHANGE_INFO_INVALID_RESPONSE")
    symbol_info = next(
        (item for item in symbols if isinstance(item, dict) and item.get("symbol") == symbol),
        None,
    )
    if not isinstance(symbol_info, dict) or symbol_info.get("status") != "TRADING":
        raise TestnetProbeError("TEST_SYMBOL_NOT_TRADING")
    filters = symbol_info.get("filters")
    if not isinstance(filters, list):
        raise TestnetProbeError("TEST_SYMBOL_FILTERS_INVALID")
    return {
        str(item["filterType"]): item
        for item in filters
        if isinstance(item, dict) and isinstance(item.get("filterType"), str)
    }


def _minimum_market_quantity(
    exchange_info: Mapping[str, Any], symbol: str, ask_price: Decimal
) -> Decimal:
    filters = _symbol_filters(exchange_info, symbol)
    try:
        lot = filters.get("MARKET_LOT_SIZE") or filters["LOT_SIZE"]
        step_size = Decimal(str(lot["stepSize"]))
        minimum_quantity = Decimal(str(lot["minQty"]))
        minimum_notional = Decimal(str(filters.get("MIN_NOTIONAL", {}).get("notional", "0")))
    except (KeyError, ArithmeticError) as exc:
        raise TestnetProbeError("TEST_SYMBOL_FILTERS_INVALID") from exc
    quantity = minimum_quantity
    if minimum_notional > 0:
        quantity = max(
            quantity,
            _decimal_step(minimum_notional / ask_price, step_size, ROUND_CEILING),
        )
    return quantity


def hedge_test_order_parameters(
    exchange_info: Mapping[str, Any],
    book_ticker: Mapping[str, Any],
    symbol: str,
) -> dict[str, str]:
    """Build a hedge-mode MARKET request for Binance's non-executing order test endpoint."""
    try:
        ask_price = Decimal(str(book_ticker["askPrice"]))
    except (KeyError, ArithmeticError) as error:
        raise TestnetProbeError("TEST_BOOK_TICKER_INVALID") from error
    if not ask_price.is_finite() or ask_price <= 0:
        raise TestnetProbeError("TEST_BOOK_TICKER_INVALID")
    quantity = _minimum_market_quantity(exchange_info, symbol, ask_price)
    return {
        "symbol": symbol,
        "side": "BUY",
        "positionSide": "LONG",
        "type": "MARKET",
        "quantity": format(quantity, "f"),
        "newClientOrderId": f"aq-h-probe-{secrets.token_hex(6)}",
    }


def _position_quantity(client: BinanceTestnetClient, symbol: str) -> Decimal:
    positions = client.position_risk(symbol)
    return sum(
        (Decimal(str(item.get("positionAmt", "0"))) for item in positions),
        start=Decimal("0"),
    )


def _flatten_position(client: BinanceTestnetClient, symbol: str) -> dict[str, Any] | None:
    quantity = _position_quantity(client, symbol)
    if quantity == 0:
        return None
    return client.place_order(
        {
            "symbol": symbol,
            "side": "SELL" if quantity > 0 else "BUY",
            "positionSide": "BOTH",
            "type": "MARKET",
            "quantity": format(abs(quantity), "f"),
            "reduceOnly": "true",
            "newOrderRespType": "RESULT",
            "newClientOrderId": f"aq-t-flat-{secrets.token_hex(6)}",
        }
    )


def _query_algo_consistent(
    client: BinanceTestnetClient,
    *,
    symbol: str,
    algo_id: int,
    client_algo_id: str,
) -> dict[str, Any]:
    last_error: TestnetProbeError | None = None
    for _ in range(5):
        try:
            return client.query_algo_order(algo_id=algo_id)
        except TestnetProbeError as exc:
            if "CODE_-2013" not in str(exc):
                raise
            last_error = exc
            time.sleep(0.1)
    open_algos = client.open_algo_orders(symbol)
    match = next(
        (
            item
            for item in open_algos
            if item.get("algoId") == algo_id or item.get("clientAlgoId") == client_algo_id
        ),
        None,
    )
    if match is not None:
        return match
    if last_error is not None:
        raise last_error
    raise TestnetProbeError("QUERY_ALGO_ORDER_NOT_FOUND")


def _terminalize_algo_after_flat(
    client: BinanceTestnetClient,
    *,
    symbol: str,
    algo_id: int,
    client_algo_id: str,
) -> str:
    try:
        current = _query_algo_consistent(
            client,
            symbol=symbol,
            algo_id=algo_id,
            client_algo_id=client_algo_id,
        )
        status = str(current.get("algoStatus"))
    except TestnetProbeError as exc:
        matching_open = any(
            item.get("algoId") == algo_id or item.get("clientAlgoId") == client_algo_id
            for item in client.open_algo_orders(symbol)
        )
        if "CODE_-2013" not in str(exc) or matching_open:
            raise
        return "REMOVED_AFTER_FLAT"
    if status in {"NEW", "TRIGGERING"}:
        try:
            client.cancel_algo_order(algo_id=algo_id)
        except TestnetProbeError as exc:
            matching_open = any(
                item.get("algoId") == algo_id or item.get("clientAlgoId") == client_algo_id
                for item in client.open_algo_orders(symbol)
            )
            if "CODE_-2011" not in str(exc) or matching_open:
                raise
            return "REMOVED_AFTER_FLAT"
        try:
            status = str(
                _query_algo_consistent(
                    client,
                    symbol=symbol,
                    algo_id=algo_id,
                    client_algo_id=client_algo_id,
                ).get("algoStatus")
            )
        except TestnetProbeError as exc:
            matching_open = any(
                item.get("algoId") == algo_id or item.get("clientAlgoId") == client_algo_id
                for item in client.open_algo_orders(symbol)
            )
            if "CODE_-2013" not in str(exc) or matching_open:
                raise
            return "REMOVED_AFTER_FLAT"
    if status not in {"CANCELED", "EXPIRED", "FINISHED", "REMOVED_AFTER_FLAT"}:
        raise TestnetProbeError("PROTECTION_ALGO_NOT_TERMINAL")
    return status


def _credential(path: Path, repository_root: Path) -> str:
    raw = read_private_file(
        path,
        forbidden_repository_root=repository_root,
        maximum_bytes=512,
        unsafe_reason="TESTNET_SECRET_FILE_UNSAFE",
    )
    try:
        return raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise TestnetProbeError("TESTNET_CREDENTIAL_FORMAT_INVALID") from exc


def run_safe_testnet_probe(
    *,
    api_key_file: Path,
    api_secret_file: Path,
    repository_root: Path,
    symbol: str = "BTCUSDT",
    transport: Transport = _urllib_transport,
    websocket_probe: WebSocketProbe = websocket_upgrade,
) -> dict[str, Any]:
    """Validate connectivity, credentials, streams and a non-matching-engine test order."""
    started_at = datetime.now(UTC)
    api_key = _credential(api_key_file, repository_root)
    api_secret = _credential(api_secret_file, repository_root)
    client = BinanceTestnetClient(api_key, api_secret, transport=transport)
    server_time, offset = client.synchronize_time()
    exchange_info = client.exchange_info()
    ticker = client.book_ticker(symbol)
    position_mode = client.position_mode()
    symbol_config = client.symbol_config(symbol)
    open_orders = client.open_orders(symbol)
    positions = client.position_risk(symbol)
    order_params = _order_test_parameters(exchange_info, ticker, symbol)
    client.test_order(order_params)

    listen_key = client.create_listen_key()
    private_stream_verified = False
    try:
        websocket_probe(TESTNET_STREAM_HOST, f"/private/ws/{listen_key}")
        private_stream_verified = True
    finally:
        client.close_listen_key(listen_key)
    websocket_probe(TESTNET_STREAM_HOST, f"/public/ws/{symbol.lower()}@bookTicker")
    websocket_probe(TESTNET_STREAM_HOST, f"/market/ws/{symbol.lower()}@markPrice@1s")
    websocket_probe(TESTNET_WS_API_HOST, "/ws-fapi/v1")

    dual_side = position_mode.get("dualSidePosition")
    if dual_side is not False:
        raise TestnetProbeError("ACCOUNT_POSITION_MODE_NOT_ONE_WAY")
    margin_types = {
        str(item.get("marginType")) for item in symbol_config if item.get("symbol") == symbol
    }
    if margin_types and margin_types != {"CROSSED"}:
        raise TestnetProbeError("ACCOUNT_MARGIN_MODE_NOT_CROSSED")
    non_flat = [
        item for item in positions if Decimal(str(item.get("positionAmt", "0"))) != Decimal("0")
    ]
    if open_orders or non_flat:
        raise TestnetProbeError("TESTNET_ACCOUNT_NOT_CLEAN")

    symbols = exchange_info.get("symbols")
    symbol_count = len(symbols) if isinstance(symbols, list) else 0
    completed_at = datetime.now(UTC)
    return {
        "schema_version": "1.0.0",
        "probe": "BINANCE_USDS_M_FUTURES_TESTNET_SAFE_CAPABILITY",
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        "result": "PASS",
        "production_endpoint_requests": 0,
        "matching_engine_orders_created": 0,
        "test_order_validated": True,
        "credential_fingerprint": hashlib.sha256(api_key.encode("ascii")).hexdigest()[:16],
        "server_time_ms": server_time,
        "clock_offset_ms": offset,
        "exchange_info_sha256": hashlib.sha256(
            json.dumps(exchange_info, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "testnet_symbol_count": symbol_count,
        "symbol": symbol,
        "account_position_mode": "ONE_WAY",
        "symbol_margin_mode": next(iter(margin_types), "UNREPORTED_NO_POSITION_OR_CONFIG"),
        "account_open_order_count": 0,
        "account_non_flat_position_count": 0,
        "listen_key_lifecycle": "CREATE_PRIVATE_WS_UPGRADE_CLOSE_PASS",
        "private_stream_verified": private_stream_verified,
        "websocket_routes": {
            "public": "PASS",
            "market": "PASS",
            "private": "PASS",
            "websocket_api": "PASS",
        },
        "endpoints": {
            "rest": TESTNET_REST_BASE,
            "streams": f"wss://{TESTNET_STREAM_HOST}",
            "websocket_api": f"wss://{TESTNET_WS_API_HOST}/ws-fapi/v1",
        },
    }


def run_testnet_risk_profile(
    *,
    api_key_file: Path,
    api_secret_file: Path,
    repository_root: Path,
    symbol: str = "BTCUSDT",
    transport: Transport = _urllib_transport,
) -> dict[str, Any]:
    """Read current fee/bracket facts and select the exchange maximum leverage."""
    started_at = datetime.now(UTC)
    api_key = _credential(api_key_file, repository_root)
    api_secret = _credential(api_secret_file, repository_root)
    client = BinanceTestnetClient(api_key, api_secret, transport=transport)
    _, offset = client.synchronize_time()
    if abs(offset) > 1_000:
        raise TestnetProbeError("TESTNET_CLOCK_OFFSET_EXCESSIVE")
    if client.position_mode().get("dualSidePosition") is not False:
        raise TestnetProbeError("ACCOUNT_POSITION_MODE_NOT_ONE_WAY")
    if client.open_orders(symbol) or client.open_algo_orders(symbol):
        raise TestnetProbeError("TESTNET_ACCOUNT_NOT_CLEAN")
    if _position_quantity(client, symbol) != 0:
        raise TestnetProbeError("TESTNET_ACCOUNT_NOT_CLEAN")

    brackets = client.leverage_brackets(symbol)
    symbol_bracket = next((item for item in brackets if item.get("symbol") == symbol), None)
    if not isinstance(symbol_bracket, dict):
        raise TestnetProbeError("LEVERAGE_BRACKET_SYMBOL_MISSING")
    bracket_rows = symbol_bracket.get("brackets")
    if not isinstance(bracket_rows, list) or not bracket_rows:
        raise TestnetProbeError("LEVERAGE_BRACKET_ROWS_MISSING")
    try:
        exchange_maximum = max(int(item["initialLeverage"]) for item in bracket_rows)
    except (KeyError, TypeError, ValueError) as exc:
        raise TestnetProbeError("LEVERAGE_BRACKET_INVALID_RESPONSE") from exc
    selected_leverage = exchange_maximum
    if selected_leverage < 1:
        raise TestnetProbeError("LEVERAGE_BRACKET_INVALID_RESPONSE")

    commission = client.commission_rate(symbol)
    try:
        maker_rate = Decimal(str(commission["makerCommissionRate"]))
        taker_rate = Decimal(str(commission["takerCommissionRate"]))
    except (KeyError, ArithmeticError) as exc:
        raise TestnetProbeError("COMMISSION_RATE_INVALID_RESPONSE") from exc
    if maker_rate < 0 or taker_rate < 0:
        raise TestnetProbeError("COMMISSION_RATE_INVALID_RESPONSE")

    changed = client.change_initial_leverage(symbol, selected_leverage)
    if changed.get("symbol") != symbol or changed.get("leverage") != selected_leverage:
        raise TestnetProbeError("CHANGE_INITIAL_LEVERAGE_RESPONSE_MISMATCH")
    max_notional = changed.get("maxNotionalValue")
    if not isinstance(max_notional, str) or Decimal(max_notional) <= 0:
        raise TestnetProbeError("CHANGE_INITIAL_LEVERAGE_RESPONSE_MISMATCH")

    profile_hash = hashlib.sha256(
        json.dumps(
            {
                "exchange_maximum": exchange_maximum,
                "maker_rate": format(maker_rate, "f"),
                "selected_leverage": selected_leverage,
                "symbol": symbol,
                "taker_rate": format(taker_rate, "f"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "schema_version": "1.0.0",
        "probe": "BINANCE_USDS_M_FUTURES_TESTNET_RISK_PROFILE",
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "result": "PASS",
        "environment": "testnet",
        "production_endpoint_requests": 0,
        "matching_engine_orders_created": 0,
        "symbol": symbol,
        "exchange_maximum_initial_leverage": exchange_maximum,
        "leverage_policy": "EXCHANGE_MAXIMUM",
        "selected_initial_leverage": selected_leverage,
        "max_notional_value": max_notional,
        "maker_commission_rate": format(maker_rate, "f"),
        "taker_commission_rate": format(taker_rate, "f"),
        "profile_hash": profile_hash,
        "clock_offset_ms": offset,
        "final_open_order_count": 0,
        "final_open_algo_order_count": 0,
        "final_position_quantity": "0",
    }


def run_testnet_order_lifecycle(
    *,
    api_key_file: Path,
    api_secret_file: Path,
    repository_root: Path,
    symbol: str = "BTCUSDT",
    transport: Transport = _urllib_transport,
) -> dict[str, Any]:
    """Place, query and cancel one far-from-market Testnet GTX order, then prove flatness."""
    started_at = datetime.now(UTC)
    api_key = _credential(api_key_file, repository_root)
    api_secret = _credential(api_secret_file, repository_root)
    client = BinanceTestnetClient(api_key, api_secret, transport=transport)
    _, offset = client.synchronize_time()
    if abs(offset) > 1_000:
        raise TestnetProbeError("TESTNET_CLOCK_OFFSET_EXCESSIVE")
    if client.position_mode().get("dualSidePosition") is not False:
        raise TestnetProbeError("ACCOUNT_POSITION_MODE_NOT_ONE_WAY")
    existing_positions = client.position_risk(symbol)
    existing_non_flat = [
        item
        for item in existing_positions
        if Decimal(str(item.get("positionAmt", "0"))) != Decimal("0")
    ]
    if client.open_orders(symbol) or existing_non_flat:
        raise TestnetProbeError("TESTNET_ACCOUNT_NOT_CLEAN")

    exchange_info = client.exchange_info()
    ticker = client.book_ticker(symbol)
    order_params = _order_test_parameters(exchange_info, ticker, symbol)
    client.test_order(order_params)
    client_order_id = order_params["newClientOrderId"]
    request_hash = hashlib.sha256(
        json.dumps(order_params, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    placed = False
    canceled_document: dict[str, Any] | None = None
    try:
        placed_document = client.place_order(order_params)
        placed = True
        if placed_document.get("clientOrderId") != client_order_id:
            raise TestnetProbeError("PLACE_ORDER_ID_MISMATCH")
        if placed_document.get("status") != "NEW":
            raise TestnetProbeError("PLACE_ORDER_NOT_RESTING_NEW")
        queried = client.query_order(symbol, client_order_id)
        if queried.get("clientOrderId") != client_order_id or queried.get("status") != "NEW":
            raise TestnetProbeError("QUERY_ORDER_NOT_RESTING_NEW")
        canceled_document = client.cancel_order(symbol, client_order_id)
        if canceled_document.get("clientOrderId") != client_order_id:
            raise TestnetProbeError("CANCEL_ORDER_ID_MISMATCH")
        final_order = client.query_order(symbol, client_order_id)
        if final_order.get("status") != "CANCELED":
            raise TestnetProbeError("ORDER_NOT_CANCELED")
    finally:
        if placed and canceled_document is None:
            try:
                client.cancel_order(symbol, client_order_id)
            except TestnetProbeError:
                pass

    open_orders = client.open_orders(symbol)
    positions = client.position_risk(symbol)
    non_flat = [
        item for item in positions if Decimal(str(item.get("positionAmt", "0"))) != Decimal("0")
    ]
    if open_orders or non_flat:
        raise TestnetProbeError("TESTNET_CLEANUP_NOT_FLAT")

    completed_at = datetime.now(UTC)
    return {
        "schema_version": "1.0.0",
        "probe": "BINANCE_USDS_M_FUTURES_TESTNET_ORDER_LIFECYCLE",
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        "result": "PASS",
        "environment": "testnet",
        "production_endpoint_requests": 0,
        "matching_engine_orders_created": 1,
        "matching_engine_fills": 0,
        "symbol": symbol,
        "request_hash": request_hash,
        "placed_status": "NEW",
        "queried_status": "NEW",
        "final_status": "CANCELED",
        "final_open_order_count": 0,
        "final_non_flat_position_count": 0,
        "clock_offset_ms": offset,
    }


def run_testnet_native_protection(
    *,
    api_key_file: Path,
    api_secret_file: Path,
    repository_root: Path,
    symbol: str = "BTCUSDT",
    transport: Transport = _urllib_transport,
) -> dict[str, Any]:
    """Open the minimum Testnet position, attach native protection, flatten and clean up."""
    started_at = datetime.now(UTC)
    api_key = _credential(api_key_file, repository_root)
    api_secret = _credential(api_secret_file, repository_root)
    client = BinanceTestnetClient(api_key, api_secret, transport=transport)
    _, offset = client.synchronize_time()
    if abs(offset) > 1_000:
        raise TestnetProbeError("TESTNET_CLOCK_OFFSET_EXCESSIVE")
    if client.position_mode().get("dualSidePosition") is not False:
        raise TestnetProbeError("ACCOUNT_POSITION_MODE_NOT_ONE_WAY")
    if client.open_orders(symbol) or client.open_algo_orders(symbol):
        raise TestnetProbeError("TESTNET_ACCOUNT_NOT_CLEAN")
    if _position_quantity(client, symbol) != 0:
        raise TestnetProbeError("TESTNET_ACCOUNT_NOT_CLEAN")

    exchange_info = client.exchange_info()
    ticker = client.book_ticker(symbol)
    try:
        ask_price = Decimal(str(ticker["askPrice"]))
        quantity = _minimum_market_quantity(exchange_info, symbol, ask_price)
        tick_size = Decimal(str(_symbol_filters(exchange_info, symbol)["PRICE_FILTER"]["tickSize"]))
    except (KeyError, ArithmeticError) as exc:
        raise TestnetProbeError("TEST_SYMBOL_FILTERS_INVALID") from exc
    entry_client_id = f"aq-t-entry-{secrets.token_hex(6)}"
    algo_client_id = f"aqa-t-stop-{secrets.token_hex(6)}"
    take_profit_client_id = f"aqa-t-tp-{secrets.token_hex(6)}"
    entry_document: dict[str, Any] | None = None
    algo_document: dict[str, Any] | None = None
    algo_id: int | None = None
    take_profit_document: dict[str, Any] | None = None
    take_profit_id: int | None = None
    algo_final_status = "NOT_CREATED"
    take_profit_final_status = "NOT_CREATED"
    cleanup_flattened = False
    protection_latency_ms: int | None = None
    take_profit_latency_ms: int | None = None
    try:
        entry_document = client.place_order(
            {
                "symbol": symbol,
                "side": "BUY",
                "positionSide": "BOTH",
                "type": "MARKET",
                "quantity": format(quantity, "f"),
                "newOrderRespType": "RESULT",
                "newClientOrderId": entry_client_id,
            }
        )
        if entry_document.get("clientOrderId") != entry_client_id:
            raise TestnetProbeError("ENTRY_ORDER_ID_MISMATCH")
        if entry_document.get("status") != "FILLED":
            raise TestnetProbeError("ENTRY_ORDER_NOT_FILLED")
        position_quantity = _position_quantity(client, symbol)
        if position_quantity <= 0:
            raise TestnetProbeError("ENTRY_POSITION_NOT_LONG")
        mark_document = client.mark_price(symbol)
        mark_price = Decimal(str(mark_document.get("markPrice", "0")))
        trigger_price = _decimal_step(mark_price * Decimal("0.95"), tick_size, ROUND_FLOOR)
        algo_document = client.place_algo_order(
            {
                "algoType": "CONDITIONAL",
                "symbol": symbol,
                "side": "SELL",
                "positionSide": "BOTH",
                "type": "STOP_MARKET",
                "triggerPrice": format(trigger_price, "f"),
                "workingType": "MARK_PRICE",
                "closePosition": "true",
                "priceProtect": "false",
                "clientAlgoId": algo_client_id,
                "newOrderRespType": "RESULT",
            }
        )
        if algo_document.get("clientAlgoId") != algo_client_id:
            raise TestnetProbeError("PROTECTION_ALGO_ID_MISMATCH")
        if algo_document.get("algoStatus") != "NEW":
            raise TestnetProbeError("PROTECTION_ALGO_NOT_NEW")
        raw_algo_id = algo_document.get("algoId")
        if not isinstance(raw_algo_id, int):
            raise TestnetProbeError("PROTECTION_ALGO_ID_MISSING")
        algo_id = raw_algo_id
        entry_update = entry_document.get("updateTime")
        protection_create = algo_document.get("createTime")
        if not isinstance(entry_update, int) or not isinstance(protection_create, int):
            raise TestnetProbeError("PROTECTION_LATENCY_TIMESTAMPS_MISSING")
        protection_latency_ms = protection_create - entry_update
        if not 0 <= protection_latency_ms <= 1_000:
            raise TestnetProbeError("PROTECTION_CONFIRMATION_OVER_1000MS")
        queried_algo = _query_algo_consistent(
            client, symbol=symbol, algo_id=algo_id, client_algo_id=algo_client_id
        )
        if queried_algo.get("algoStatus") != "NEW":
            raise TestnetProbeError("PROTECTION_QUERY_NOT_NEW")
        take_profit_trigger = _decimal_step(mark_price * Decimal("1.05"), tick_size, ROUND_CEILING)
        take_profit_document = client.place_algo_order(
            {
                "algoType": "CONDITIONAL",
                "symbol": symbol,
                "side": "SELL",
                "positionSide": "BOTH",
                "type": "TAKE_PROFIT_MARKET",
                "triggerPrice": format(take_profit_trigger, "f"),
                "workingType": "MARK_PRICE",
                "closePosition": "true",
                "priceProtect": "false",
                "clientAlgoId": take_profit_client_id,
                "newOrderRespType": "RESULT",
            }
        )
        if take_profit_document.get("clientAlgoId") != take_profit_client_id:
            raise TestnetProbeError("TAKE_PROFIT_ALGO_ID_MISMATCH")
        if take_profit_document.get("algoStatus") != "NEW":
            raise TestnetProbeError("TAKE_PROFIT_ALGO_NOT_NEW")
        raw_take_profit_id = take_profit_document.get("algoId")
        take_profit_create = take_profit_document.get("createTime")
        if not isinstance(raw_take_profit_id, int):
            raise TestnetProbeError("TAKE_PROFIT_ALGO_ID_MISSING")
        if not isinstance(take_profit_create, int) or not isinstance(entry_update, int):
            raise TestnetProbeError("PROTECTION_LATENCY_TIMESTAMPS_MISSING")
        take_profit_id = raw_take_profit_id
        take_profit_latency_ms = take_profit_create - entry_update
        if take_profit_latency_ms < 0:
            raise TestnetProbeError("PROTECTION_LATENCY_TIMESTAMPS_INVALID")
        queried_take_profit = _query_algo_consistent(
            client,
            symbol=symbol,
            algo_id=take_profit_id,
            client_algo_id=take_profit_client_id,
        )
        if queried_take_profit.get("algoStatus") != "NEW":
            raise TestnetProbeError("TAKE_PROFIT_QUERY_NOT_NEW")
        close_document = _flatten_position(client, symbol)
        cleanup_flattened = close_document is not None
        if close_document is None or close_document.get("status") != "FILLED":
            raise TestnetProbeError("TESTNET_FLATTEN_NOT_FILLED")
        if _position_quantity(client, symbol) != 0:
            raise TestnetProbeError("TESTNET_POSITION_NOT_FLAT")
        algo_final_status = _terminalize_algo_after_flat(
            client,
            symbol=symbol,
            algo_id=algo_id,
            client_algo_id=algo_client_id,
        )
        take_profit_final_status = _terminalize_algo_after_flat(
            client,
            symbol=symbol,
            algo_id=take_profit_id,
            client_algo_id=take_profit_client_id,
        )
    finally:
        if entry_document is not None and _position_quantity(client, symbol) != 0:
            _flatten_position(client, symbol)
            cleanup_flattened = True
        if algo_document is not None and algo_id is not None:
            try:
                open_algos = client.open_algo_orders(symbol)
                if any(
                    item.get("algoId") == algo_id or item.get("clientAlgoId") == algo_client_id
                    for item in open_algos
                ):
                    client.cancel_algo_order(algo_id=algo_id)
            except TestnetProbeError:
                pass
        if take_profit_document is not None and take_profit_id is not None:
            try:
                open_algos = client.open_algo_orders(symbol)
                if any(
                    item.get("algoId") == take_profit_id
                    or item.get("clientAlgoId") == take_profit_client_id
                    for item in open_algos
                ):
                    client.cancel_algo_order(algo_id=take_profit_id)
            except TestnetProbeError:
                pass

    final_open_orders = client.open_orders(symbol)
    final_open_algos = client.open_algo_orders(symbol)
    final_position = _position_quantity(client, symbol)
    if final_open_orders or final_open_algos or final_position != 0:
        raise TestnetProbeError("TESTNET_PROTECTION_CLEANUP_INCOMPLETE")
    completed_at = datetime.now(UTC)
    return {
        "schema_version": "1.0.0",
        "probe": "BINANCE_USDS_M_FUTURES_TESTNET_NATIVE_PROTECTION",
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        "result": "PASS",
        "environment": "testnet",
        "production_endpoint_requests": 0,
        "symbol": symbol,
        "entry_status": "FILLED",
        "native_protection_status": "STOP_AND_TAKE_PROFIT_NEW_CONFIRMED",
        "protection_confirmation_latency_ms": protection_latency_ms,
        "maximum_protection_latency_ms": 1_000,
        "take_profit_confirmation_latency_ms": take_profit_latency_ms,
        "flatten_status": "FILLED" if cleanup_flattened else "NOT_REQUIRED",
        "protection_final_status": algo_final_status,
        "take_profit_final_status": take_profit_final_status,
        "final_open_order_count": 0,
        "final_open_algo_order_count": 0,
        "final_position_quantity": "0",
        "clock_offset_ms": offset,
    }
