from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ai_quant.binance_egress.connection_measurements import (
    verify_stream_connection_profiles,
)
from ai_quant.rate_budget.authorization import AuthorizationDenied, canonical_digest

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 14, tzinfo=UTC)


def _documents(source_root: Path) -> tuple[dict[str, Any], dict[str, Any], str]:
    signer = Ed25519PrivateKey.generate()
    public_key = signer.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    keyring_content = {
        "keyring_id": "host-control-config-root-test",
        "purpose": "HOST_CONTROL_CONFIG",
        "provisioning": "ROOT_OWNED_0444_OUT_OF_BAND_PINNED_SHA256_NOT_RELEASE_WRITABLE",
        "keys": [
            {
                "key_id": "host-config-signing-test",
                "public_key_base64": base64.b64encode(public_key).decode(),
                "status": "CURRENT",
                "not_before": "2026-07-13T00:00:00Z",
                "not_after": "2026-10-01T00:00:00Z",
            }
        ],
        "revoked_key_ids": [],
        "unknown_key_action": "DENY_AND_OPEN_P0",
    }
    keyring_hash = canonical_digest(keyring_content).hex()
    keyring = {
        "schema_version": "1.0.0",
        "keyring_hash": keyring_hash,
        "content": keyring_content,
        "rotation_proof": None,
    }
    document = json.loads((ROOT / "config/binance-connection-contract.example.json").read_text())
    source = document["content"]["source_documents"][0]
    relative = source["artifact_path"]
    source_path = source_root / relative
    source_path.parent.mkdir(parents=True)
    source_path.write_text("official connection facts\n", encoding="utf-8")
    source["sha256"] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source["covered_environments"] = ["production"]
    document["content"]["source_documents"] = [source]
    profile = document["content"]["authority_profiles"][0]
    profile["environments"] = ["production"]
    profile["source_ids"] = [source["source_id"]]
    document["content"]["contract_status"] = "SIGNED_RUNTIME"
    document["content"]["valid_until"] = "2026-07-15T00:00:00Z"
    digest = canonical_digest(document["content"])
    document["contract_hash"] = digest.hex()
    document["signature"] = {
        "algorithm": "Ed25519",
        "key_id": "host-config-signing-test",
        "signed_at": "2026-07-14T00:00:00Z",
        "signature_base64": base64.b64encode(signer.sign(digest)).decode(),
    }
    return document, keyring, keyring_hash


def test_signed_stream_profiles_close_source_and_environment(tmp_path: Path) -> None:
    document, keyring, keyring_hash = _documents(tmp_path)
    profiles = verify_stream_connection_profiles(
        document,
        keyring,
        expected_keyring_hash=keyring_hash,
        enabled_stream_authorities=frozenset({"BINANCE_PRODUCTION_FSTREAM"}),
        enabled_environments=frozenset({"production"}),
        source_artifact_root=tmp_path,
        now=NOW,
    )
    assert profiles["BINANCE_PRODUCTION_FSTREAM"].contract_hash == document["contract_hash"]


def test_stream_profiles_reject_source_or_coverage_drift(tmp_path: Path) -> None:
    document, keyring, keyring_hash = _documents(tmp_path)
    changed = copy.deepcopy(document)
    changed["content"]["authority_profiles"][0]["environments"] = ["shadow"]
    digest = canonical_digest(changed["content"])
    changed["contract_hash"] = digest.hex()
    with pytest.raises(AuthorizationDenied):
        verify_stream_connection_profiles(
            changed,
            keyring,
            expected_keyring_hash=keyring_hash,
            enabled_stream_authorities=frozenset({"BINANCE_PRODUCTION_FSTREAM"}),
            enabled_environments=frozenset({"production"}),
            source_artifact_root=tmp_path,
            now=NOW,
        )
