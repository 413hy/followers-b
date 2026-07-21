import json

import pytest

from ai_quant.copy_trading.testnet_catalog import (
    BinanceTestnetCatalogClient,
    CatalogHttpResult,
)
from ai_quant.copy_trading.testnet_catalog import TestnetCatalogError as CatalogError


def test_catalog_returns_only_valid_trading_symbols() -> None:
    paths: list[str] = []

    def transport(path: str, timeout: float) -> CatalogHttpResult:
        del timeout
        paths.append(path)
        return CatalogHttpResult(
            200,
            json.dumps(
                {
                    "symbols": [
                        {"symbol": "BTCUSDT", "status": "TRADING"},
                        {"symbol": "测试测试USDT", "status": "TRADING"},
                        {"symbol": "BTCUSDT_260925", "status": "TRADING"},
                        {"symbol": "OLDUSDT", "status": "CLOSE"},
                    ]
                }
            ).encode(),
        )

    symbols = BinanceTestnetCatalogClient(transport=transport).trading_symbols()
    assert symbols == frozenset({"BTCUSDT"})
    assert paths == ["/fapi/v1/exchangeInfo"]


def test_catalog_fails_closed_on_invalid_contract() -> None:
    client = BinanceTestnetCatalogClient(
        transport=lambda path, timeout: CatalogHttpResult(200, b'{"symbols":[]}')
    )
    with pytest.raises(CatalogError, match="CATALOG_CONTRACT_INVALID"):
        client.trading_symbols()
