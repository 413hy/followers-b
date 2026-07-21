#!/usr/bin/env python3
"""Process age-encrypted Parquet uploads and issue signed decryption receipts."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELATIVE_ARCHIVE_PATH = re.compile(r"^[A-Za-z0-9._=/-]+$")
_METADATA_FIELDS = {
    "schema_version",
    "receipt_id",
    "object_path",
    "plaintext_sha256",
    "plaintext_size_bytes",
    "ciphertext_sha256",
    "ciphertext_size_bytes",
    "uploaded_at",
    "encryption_format",
    "inspection_type",
    "expected_parquet_row_count",
    "expected_parquet_schema_version",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("metadata must be a JSON object")
    return document


def _normalized_relative_path(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("object_path must be a string")
    parts = value.split("/")
    if (
        not value
        or value.startswith("/")
        or not _RELATIVE_ARCHIVE_PATH.fullmatch(value)
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError("object_path must be a normalized relative path")
    return value


def _utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("uploaded_at must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):  # noqa: UP017
        raise ValueError("uploaded_at must be UTC")
    return parsed


def _json_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _validate_metadata(document: dict[str, Any], receipt_id: str) -> None:
    if set(document) != _METADATA_FIELDS:
        raise ValueError("metadata field set is not exact")
    if document["schema_version"] != "1.0.0":
        raise ValueError("unsupported metadata schema")
    if document["receipt_id"] != receipt_id or not _IDENTIFIER.fullmatch(receipt_id):
        raise ValueError("receipt identity mismatch")
    _normalized_relative_path(document["object_path"])
    for field in ("plaintext_sha256", "ciphertext_sha256"):
        if not isinstance(document[field], str) or not _SHA256.fullmatch(document[field]):
            raise ValueError(f"invalid {field}")
    for field in ("plaintext_size_bytes", "ciphertext_size_bytes", "expected_parquet_row_count"):
        if (
            not isinstance(document[field], int)
            or isinstance(document[field], bool)
            or document[field] <= 0
        ):
            raise ValueError(f"invalid {field}")
    if document["encryption_format"] != "age-v1-x25519":
        raise ValueError("unsupported encryption format")
    if document["inspection_type"] != "PARQUET":
        raise ValueError("unsupported inspection type")
    if (
        not isinstance(document["expected_parquet_schema_version"], str)
        or not document["expected_parquet_schema_version"]
    ):
        raise ValueError("invalid expected Parquet schema version")
    _utc_timestamp(document["uploaded_at"])


def _atomic_json(path: Path, document: dict[str, Any], mode: int = 0o640) -> None:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _sign_receipt(document: dict[str, Any], key: Ed25519PrivateKey) -> dict[str, Any]:
    signing_bytes = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return {**document, "signature_base64": base64.b64encode(key.sign(signing_bytes)).decode()}


def _inspect_parquet(path: Path) -> tuple[int, str]:
    parquet = pq.ParquetFile(path)
    row_count = parquet.metadata.num_rows
    versions = (
        pq.read_table(path, columns=["schema_version"])["schema_version"].unique().to_pylist()
    )
    if len(versions) != 1 or not isinstance(versions[0], str):
        raise ValueError("Parquet schema_version must have one non-null value")
    return row_count, versions[0]


def process_one(config: dict[str, Any], metadata_path: Path) -> Path:
    root = Path(config["archive_root"]).resolve()
    incoming = root / "incoming"
    objects = root / "objects"
    receipts = root / "receipts"
    receipt_id = metadata_path.name.removesuffix(".metadata.json")
    if not _IDENTIFIER.fullmatch(receipt_id):
        raise ValueError("invalid metadata filename")
    ciphertext_path = incoming / f"{receipt_id}.age"
    if metadata_path.parent.resolve() != incoming or not ciphertext_path.is_file():
        raise ValueError("ciphertext pair is missing")
    if metadata_path.is_symlink() or ciphertext_path.is_symlink():
        raise ValueError("symlink uploads are forbidden")

    metadata = _load_json(metadata_path)
    _validate_metadata(metadata, receipt_id)
    if ciphertext_path.stat().st_size != metadata["ciphertext_size_bytes"]:
        raise ValueError("ciphertext size mismatch")
    if _sha256(ciphertext_path) != metadata["ciphertext_sha256"]:
        raise ValueError("ciphertext digest mismatch")

    with tempfile.TemporaryDirectory(prefix="aiq-archive-", dir=config["temporary_root"]) as temp:
        plaintext_path = Path(temp) / "object.parquet"
        completed = subprocess.run(  # noqa: S603
            [
                config["age_binary"],
                "--decrypt",
                "--identity",
                config["age_identity_file"],
                "--output",
                str(plaintext_path),
                str(ciphertext_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError("age decryption failed")
        if plaintext_path.stat().st_size != metadata["plaintext_size_bytes"]:
            raise ValueError("plaintext size mismatch")
        if _sha256(plaintext_path) != metadata["plaintext_sha256"]:
            raise ValueError("plaintext digest mismatch")
        row_count, schema_version = _inspect_parquet(plaintext_path)
        if row_count != metadata["expected_parquet_row_count"]:
            raise ValueError("Parquet row count mismatch")
        if schema_version != metadata["expected_parquet_schema_version"]:
            raise ValueError("Parquet schema version mismatch")

    object_ciphertext = objects / f"{receipt_id}.age"
    object_metadata = objects / f"{receipt_id}.metadata.json"
    receipt_path = receipts / f"{receipt_id}.json"
    if any(path.exists() for path in (object_ciphertext, object_metadata, receipt_path)):
        raise FileExistsError("append-only remote identity already exists")
    os.replace(ciphertext_path, object_ciphertext)
    os.replace(metadata_path, object_metadata)

    decrypted_at = datetime.now(timezone.utc)  # noqa: UP017
    uploaded_at = _utc_timestamp(metadata["uploaded_at"])
    receipt = {
        "schema_version": "1.1.0",
        "receipt_id": receipt_id,
        "object_path": metadata["object_path"],
        "remote_ciphertext_path": f"objects/{receipt_id}.age",
        "plaintext_sha256": metadata["plaintext_sha256"],
        "plaintext_size_bytes": metadata["plaintext_size_bytes"],
        "ciphertext_sha256": metadata["ciphertext_sha256"],
        "ciphertext_size_bytes": metadata["ciphertext_size_bytes"],
        "remote_uri": config["remote_uri_prefix"].rstrip("/") + f"/objects/{receipt_id}.age",
        "remote_etag": metadata["ciphertext_sha256"],
        "uploaded_at": _json_utc(uploaded_at),
        "decrypted_at": _json_utc(decrypted_at),
        "encryption_format": "age-v1-x25519",
        "inspection_type": "PARQUET",
        "inspection_status": "PASS",
        "parquet_row_count": row_count,
        "parquet_schema_version": schema_version,
        "signer_key_id": config["signer_key_id"],
    }
    signed = _sign_receipt(receipt, config["signing_key"])
    _atomic_json(receipt_path, signed)
    return receipt_path


def _quarantine(config: dict[str, Any], metadata_path: Path, reason: str) -> None:
    root = Path(config["archive_root"]).resolve()
    receipt_id = metadata_path.name.removesuffix(".metadata.json")
    destination = root / "quarantine" / f"{receipt_id}-{uuid.uuid4().hex}"
    destination.mkdir(mode=0o750)
    sources = (
        metadata_path,
        root / "incoming" / f"{receipt_id}.age",
        root / "objects" / f"{receipt_id}.metadata.json",
        root / "objects" / f"{receipt_id}.age",
    )
    allowed_parents = {root / "incoming", root / "objects"}
    for source in sources:
        if source.exists() and source.parent.resolve() in allowed_parents:
            shutil.move(str(source), destination / source.name)
    _atomic_json(destination / "failure.json", {"status": "REJECTED", "reason": reason})


def load_config(path: Path) -> dict[str, Any]:
    config = _load_json(path)
    required = {
        "archive_root",
        "temporary_root",
        "age_binary",
        "age_identity_file",
        "receipt_signing_key_file",
        "signer_key_id",
        "remote_uri_prefix",
    }
    if set(config) != required:
        raise ValueError("receiver config field set is not exact")
    key_bytes = Path(config["receipt_signing_key_file"]).read_bytes()
    key = serialization.load_pem_private_key(key_bytes, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("receipt signing key must be Ed25519")
    config["signing_key"] = key
    return config


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    arguments = parser.parse_args()
    config = load_config(arguments.config)
    incoming = Path(config["archive_root"]).resolve() / "incoming"
    failures = 0
    for metadata_path in sorted(incoming.glob("*.metadata.json")):
        try:
            process_one(config, metadata_path)
        except Exception as error:
            failures += 1
            _quarantine(config, metadata_path, f"{type(error).__name__}: {error}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
