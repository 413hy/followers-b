from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ai_quant.common.artifacts import (
    ArtifactBindingSource,
    ArtifactHashMode,
    verify_artifact_bindings,
)
from ai_quant.rate_budget.authorization import AuthorizationDenied, canonical_digest


def test_raw_document_and_content_hash_modes_are_exact(tmp_path: Path) -> None:
    raw = tmp_path / "schema.json"
    document = tmp_path / "policy.yaml"
    content = tmp_path / "signed.json"
    raw.write_bytes(b'{"type":"object"}\n')
    document.write_text("enabled: true\nlimit: 3\n", encoding="utf-8")
    content.write_text(
        json.dumps({"content": {"status": "SIGNED_RUNTIME", "limit": 3}}),
        encoding="utf-8",
    )
    expected = {
        "schema_hash": hashlib.sha256(raw.read_bytes()).hexdigest(),
        "policy_hash": canonical_digest({"enabled": True, "limit": 3}).hex(),
        "content_hash": canonical_digest({"status": "SIGNED_RUNTIME", "limit": 3}).hex(),
    }
    sources = {
        "schema_hash": ArtifactBindingSource(raw, ArtifactHashMode.RAW_BYTES),
        "policy_hash": ArtifactBindingSource(document, ArtifactHashMode.JCS_DOCUMENT),
        "content_hash": ArtifactBindingSource(content, ArtifactHashMode.JCS_CONTENT),
    }
    assert (
        verify_artifact_bindings(
            expected,
            sources,
            approved_roots=(tmp_path,),
        )
        == expected
    )


def test_artifact_mismatch_and_symlink_fail_closed(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.json"
    artifact.write_text('{"content":{"value":1}}', encoding="utf-8")
    source = ArtifactBindingSource(artifact, ArtifactHashMode.JCS_CONTENT)
    with pytest.raises(AuthorizationDenied, match="ARTIFACT_BINDING_MISMATCH"):
        verify_artifact_bindings(
            {"content_hash": "0" * 64},
            {"content_hash": source},
            approved_roots=(tmp_path,),
        )
    link = tmp_path / "artifact-link.json"
    link.symlink_to(artifact)
    with pytest.raises(AuthorizationDenied, match="ARTIFACT_FILE_UNSAFE"):
        verify_artifact_bindings(
            {"content_hash": canonical_digest({"value": 1}).hex()},
            {
                "content_hash": ArtifactBindingSource(
                    link,
                    ArtifactHashMode.JCS_CONTENT,
                )
            },
            approved_roots=(tmp_path,),
        )


def test_duplicate_keys_cannot_enter_an_artifact_hash(tmp_path: Path) -> None:
    artifact = tmp_path / "duplicate.json"
    artifact.write_text('{"content":{"value":1,"value":2}}', encoding="utf-8")
    with pytest.raises(AuthorizationDenied, match="ARTIFACT_CONTENT_INVALID"):
        verify_artifact_bindings(
            {"content_hash": "0" * 64},
            {
                "content_hash": ArtifactBindingSource(
                    artifact,
                    ArtifactHashMode.JCS_CONTENT,
                )
            },
            approved_roots=(tmp_path,),
        )
