import json
from pathlib import Path

import pytest

from ai_quant.common.config import (
    ConfigurationError,
    reject_embedded_secret_values,
    validate_config,
)


def test_secret_value_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="embedded secret"):
        reject_embedded_secret_values({"binance_api_key": "actual-secret-material"})


def test_runtime_secret_placeholder_is_allowed() -> None:
    reject_embedded_secret_values({"binance_api_key": "${BINANCE_API_KEY}"})


def test_known_config_validates() -> None:
    root = Path(__file__).resolve().parents[2]
    config = validate_config(root / "config/risk.example.yaml", root / "config/risk.schema.json")
    assert isinstance(config, dict)


def test_copy_trading_config_is_testnet_only_and_validates() -> None:
    root = Path(__file__).resolve().parents[2]
    config = validate_config(
        root / "config/copy-trading.example.yaml",
        root / "config/copy-trading.schema.json",
    )
    assert config["environment"] == "TESTNET"
    assert config["attribution"]["exchange_position_mode"] == "HEDGE"
    assert config["attribution"]["history_replay"] == "FORBIDDEN"
    assert config["risk"]["per_position_stop"] is False
    assert config["source"]["browser_fallback"]["enabled"] is False


@pytest.mark.parametrize(
    ("suffix", "document"),
    [
        (".json", '{"key":1,"key":2}'),
        (".yaml", "key: 1\nkey: 2\n"),
    ],
)
def test_duplicate_configuration_keys_are_rejected(
    tmp_path: Path,
    suffix: str,
    document: str,
) -> None:
    instance = tmp_path / f"duplicate{suffix}"
    schema = tmp_path / "schema.json"
    instance.write_text(document, encoding="utf-8")
    schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="duplicate configuration key"):
        validate_config(instance, schema)
