"""Unauthenticated Binance USD-M Testnet symbol catalog."""

from __future__ import annotations

import http.client
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

_HOST = "demo-fapi.binance.com"
_PRODUCTION_HOST = "fapi.binance.com"
_PATH = "/fapi/v1/exchangeInfo"
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_SYMBOL = re.compile(r"^[A-Z0-9]{3,24}$")


class TestnetCatalogError(RuntimeError):
    """The public Testnet symbol catalog was unavailable or malformed."""


@dataclass(frozen=True, slots=True)
class CatalogHttpResult:
    status: int
    body: bytes


CatalogTransport = Callable[[str, float], CatalogHttpResult]


def _https_get(path: str, timeout_seconds: float) -> CatalogHttpResult:
    if path != _PATH:
        raise TestnetCatalogError("COPY_TESTNET_CATALOG_DESTINATION_INVALID")
    connection = http.client.HTTPSConnection(_HOST, timeout=timeout_seconds)
    try:
        connection.request(
            "GET",
            path,
            headers={"User-Agent": "aiq-copy-trading/1.0 (+testnet-symbol-catalog)"},
        )
        response = connection.getresponse()
        body = response.read(_MAX_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException) as error:
        raise TestnetCatalogError("COPY_TESTNET_CATALOG_TRANSPORT_FAILED") from error
    finally:
        connection.close()
    if len(body) > _MAX_RESPONSE_BYTES:
        raise TestnetCatalogError("COPY_TESTNET_CATALOG_RESPONSE_TOO_LARGE")
    return CatalogHttpResult(response.status, body)


def _production_https_get(path: str, timeout_seconds: float) -> CatalogHttpResult:
    if path != _PATH:
        raise TestnetCatalogError("COPY_PRODUCTION_CATALOG_DESTINATION_INVALID")
    connection = http.client.HTTPSConnection(_PRODUCTION_HOST, timeout=timeout_seconds)
    try:
        connection.request(
            "GET",
            path,
            headers={"User-Agent": "aiq-copy-trading/1.0 (+production-symbol-catalog)"},
        )
        response = connection.getresponse()
        body = response.read(_MAX_RESPONSE_BYTES + 1)
    except (OSError, http.client.HTTPException) as error:
        raise TestnetCatalogError("COPY_PRODUCTION_CATALOG_TRANSPORT_FAILED") from error
    finally:
        connection.close()
    if len(body) > _MAX_RESPONSE_BYTES:
        raise TestnetCatalogError("COPY_PRODUCTION_CATALOG_RESPONSE_TOO_LARGE")
    return CatalogHttpResult(response.status, body)


class BinanceTestnetCatalogClient:
    def __init__(
        self,
        *,
        transport: CatalogTransport = _https_get,
        timeout_seconds: float = 15,
    ) -> None:
        if not 1 <= timeout_seconds <= 30:
            raise ValueError("copy Testnet catalog timeout is invalid")
        self._transport = transport
        self._timeout = timeout_seconds

    def trading_symbols(self) -> frozenset[str]:
        response = self._transport(_PATH, self._timeout)
        if response.status != 200:
            raise TestnetCatalogError(f"COPY_TESTNET_CATALOG_HTTP_{response.status}")
        try:
            document = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TestnetCatalogError("COPY_TESTNET_CATALOG_JSON_INVALID") from error
        symbols_raw = document.get("symbols") if isinstance(document, dict) else None
        if not isinstance(symbols_raw, list) or not 1 <= len(symbols_raw) <= 2000:
            raise TestnetCatalogError("COPY_TESTNET_CATALOG_CONTRACT_INVALID")
        symbols: set[str] = set()
        for item in symbols_raw:
            if not isinstance(item, Mapping):
                raise TestnetCatalogError("COPY_TESTNET_CATALOG_CONTRACT_INVALID")
            symbol = item.get("symbol")
            status = item.get("status")
            if status == "TRADING":
                # Testnet contains synthetic Unicode instruments and dated contracts
                # that cannot occur in the public leader feed. Ignore those rows while
                # retaining a strict allowlist for symbols that may reach execution.
                if isinstance(symbol, str) and _SYMBOL.fullmatch(symbol):
                    symbols.add(symbol)
        if not symbols:
            raise TestnetCatalogError("COPY_TESTNET_CATALOG_EMPTY")
        return frozenset(symbols)


class BinanceProductionCatalogClient(BinanceTestnetCatalogClient):
    """Unauthenticated, exact-origin production USD-M trading-symbol catalog."""

    def __init__(
        self,
        *,
        transport: CatalogTransport = _production_https_get,
        timeout_seconds: float = 15,
    ) -> None:
        super().__init__(transport=transport, timeout_seconds=timeout_seconds)
