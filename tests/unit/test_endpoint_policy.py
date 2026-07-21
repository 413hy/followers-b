from __future__ import annotations

import base64
import copy
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ai_quant.rate_budget.authorization import AuthorizationDenied, canonical_digest
from ai_quant.rate_budget.policy import verify_endpoint_catalog

NOW = datetime(2026, 7, 14, 0, 0, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


def _public(signer: Ed25519PrivateKey) -> str:
    value = signer.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(value).decode()


def _signed_documents(
    source_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], str, Ed25519PrivateKey]:
    signer = Ed25519PrivateKey.generate()
    keyring_content = {
        "keyring_id": "host-control-config-root-test",
        "purpose": "HOST_CONTROL_CONFIG",
        "provisioning": "ROOT_OWNED_0444_OUT_OF_BAND_PINNED_SHA256_NOT_RELEASE_WRITABLE",
        "keys": [
            {
                "key_id": "host-config-signing-test",
                "public_key_base64": _public(signer),
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
    source = source_root / "source.txt"
    source.write_text("official endpoint facts\n", encoding="utf-8")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    schema_hash = hashlib.sha256(
        (ROOT / "contracts/binance-gateway-request.schema.json").read_bytes()
    ).hexdigest()

    def endpoint(endpoint_id: str, path: str) -> dict[str, Any]:
        not_applicable = {
            "mode": "NOT_APPLICABLE",
            "fixed_cost": 0,
            "parameter_name": None,
            "tiers": [],
        }
        return {
            "endpoint_id": endpoint_id,
            "endpoint_authorities": ["BINANCE_PRODUCTION_FAPI"],
            "transport": "REST",
            "method": "GET",
            "path": path,
            "market_stream_role": None,
            "control_frame_type": None,
            "allowed_operation_classes": ["HOST_RATE_CONTROL"],
            "causal_role_class_map": {"HOST_RATE_CONTROL": "HOST_RATE_CONTROL"},
            "request_weight_rule": {
                "mode": "FIXED",
                "fixed_cost": 1,
                "parameter_name": None,
                "tiers": [],
            },
            "order_count_rule": not_applicable,
            "websocket_control_rule": not_applicable,
            "connection_attempt_rule": not_applicable,
            "parameter_policy": "NO_PARAMETERS",
            "allowed_parameter_names": [],
            "required_parameter_names": [],
            "forbidden_parameter_names": ["apiSecret", "secretKey"],
            "request_schema_ref": "contracts/binance-gateway-request.schema.json",
            "request_schema_sha256": schema_hash,
            "source_document_sha256": source_hash,
        }

    content = {
        "catalog_id": "endpoint-catalog-test",
        "catalog_status": "SIGNED_RUNTIME",
        "created_at": "2026-07-13T00:00:00Z",
        "checked_at": "2026-07-13T23:59:00Z",
        "valid_until": "2026-07-15T00:00:00Z",
        "source_documents": [
            {
                "uri": "https://developers.binance.com/test",
                "retrieved_at": "2026-07-13T00:00:00Z",
                "artifact_path": "source.txt",
                "canonicalization": (
                    "UTF8_LF_NORMALIZED_VISIBLE_DOCUMENT_TEXT_WITH_URI_AND_RETRIEVED_AT_SIDECAR"
                ),
                "sha256": source_hash,
            }
        ],
        "endpoint_authorities": ["BINANCE_PRODUCTION_FAPI"],
        "bootstrap": {
            "allowed_endpoint_ids": ["REST_SERVER_TIME", "REST_UM_EXCHANGE_INFO"],
            "snapshot_limits": [],
            "snapshot_source": "SIGNED_CONSERVATIVE_BOOTSTRAP_FLOOR",
            "snapshot_checked_at": "2026-07-13T23:59:00Z",
            "snapshot_valid_until": "2026-07-15T00:00:00Z",
            "missing_or_expired_action": "BLOCK_ALL_BINANCE_EGRESS",
        },
        "endpoint_contracts": [
            endpoint("REST_SERVER_TIME", "/fapi/v1/time"),
            endpoint("REST_UM_EXCHANGE_INFO", "/fapi/v1/exchangeInfo"),
        ],
        "unknown_endpoint_policy": "DENY",
    }
    digest = canonical_digest(content)
    catalog = {
        "schema_version": "1.0.0",
        "catalog_hash": digest.hex(),
        "content": content,
        "signature": {
            "algorithm": "Ed25519",
            "key_id": "host-config-signing-test",
            "signed_at": "2026-07-14T00:00:00Z",
            "signature_base64": base64.b64encode(signer.sign(digest)).decode(),
        },
    }
    return catalog, keyring, keyring_hash, signer


def _verify(catalog: dict[str, Any], keyring: dict[str, Any], keyring_hash: str, root: Path) -> Any:
    return verify_endpoint_catalog(
        catalog,
        keyring,
        expected_keyring_hash=keyring_hash,
        request_schema_path=ROOT / "contracts/binance-gateway-request.schema.json",
        source_artifact_root=root,
        now=NOW,
    )


def test_signed_endpoint_catalog_closes_sources_and_identities(tmp_path: Path) -> None:
    catalog, keyring, keyring_hash, _ = _signed_documents(tmp_path)
    verified = _verify(catalog, keyring, keyring_hash, tmp_path)
    assert verified.catalog_hash == catalog["catalog_hash"]
    assert set(verified.endpoints) == {
        ("BINANCE_PRODUCTION_FAPI", "REST_SERVER_TIME"),
        ("BINANCE_PRODUCTION_FAPI", "REST_UM_EXCHANGE_INFO"),
    }


def test_catalog_tampering_is_rejected(tmp_path: Path) -> None:
    catalog, keyring, keyring_hash, _ = _signed_documents(tmp_path)
    catalog["signature"]["signature_base64"] = base64.b64encode(b"x" * 64).decode()
    with pytest.raises(AuthorizationDenied, match="SIGNED_POLICY_SIGNATURE_INVALID"):
        _verify(catalog, keyring, keyring_hash, tmp_path)


def test_catalog_source_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    catalog, keyring, keyring_hash, _ = _signed_documents(tmp_path)
    (tmp_path / "source.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(AuthorizationDenied, match="ENDPOINT_SOURCE_HASH_MISMATCH"):
        _verify(catalog, keyring, keyring_hash, tmp_path)


def test_catalog_source_symlink_is_rejected_even_with_matching_bytes(
    tmp_path: Path,
) -> None:
    catalog, keyring, keyring_hash, _ = _signed_documents(tmp_path)
    source = tmp_path / "source.txt"
    replacement = tmp_path / "replacement.txt"
    source.replace(replacement)
    source.symlink_to(replacement)

    with pytest.raises(AuthorizationDenied, match="ENDPOINT_SOURCE_INVALID"):
        _verify(catalog, keyring, keyring_hash, tmp_path)


def test_catalog_duplicate_wire_identity_is_rejected(tmp_path: Path) -> None:
    catalog, keyring, keyring_hash, signer = _signed_documents(tmp_path)
    changed = copy.deepcopy(catalog)
    second = changed["content"]["endpoint_contracts"][1]
    second["path"] = "/fapi/v1/time"
    digest = canonical_digest(changed["content"])
    changed["catalog_hash"] = digest.hex()
    changed["signature"]["signature_base64"] = base64.b64encode(signer.sign(digest)).decode()
    with pytest.raises(AuthorizationDenied, match="ENDPOINT_IDENTITY_DUPLICATE"):
        _verify(changed, keyring, keyring_hash, tmp_path)


def test_discontinuous_signed_cost_tiers_are_rejected(tmp_path: Path) -> None:
    catalog, keyring, keyring_hash, signer = _signed_documents(tmp_path)
    changed = copy.deepcopy(catalog)
    changed["content"]["endpoint_contracts"][0]["request_weight_rule"] = {
        "mode": "SIGNED_PARAMETER_TIERS",
        "fixed_cost": None,
        "parameter_name": "limit",
        "tiers": [
            {"min_inclusive": 0, "max_inclusive": 2, "cost": 1},
            {"min_inclusive": 4, "max_inclusive": 5, "cost": 2},
        ],
    }
    digest = canonical_digest(changed["content"])
    changed["catalog_hash"] = digest.hex()
    changed["signature"]["signature_base64"] = base64.b64encode(signer.sign(digest)).decode()
    with pytest.raises(AuthorizationDenied, match="ENDPOINT_COST_TIERS_INVALID"):
        _verify(changed, keyring, keyring_hash, tmp_path)
