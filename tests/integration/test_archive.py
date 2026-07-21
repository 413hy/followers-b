from __future__ import annotations

import base64
import json
import runpy
from datetime import UTC, date, datetime, timedelta

import pyarrow.parquet as pq  # type: ignore[import-untyped]
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ai_quant.archive.manifest import manifest_hash, write_daily_manifest
from ai_quant.archive.parquet import RawArchiveWriter
from ai_quant.archive.receipt import (
    RemoteDecryptionReceipt,
    RemoteReceipt,
    verify_remote_decryption_receipt,
    verify_remote_receipt,
)
from ai_quant.archive.retention import RetentionCandidate, plan_verified_deletions
from tests.market_fixtures import BASE_TIME, update


def test_depth_archive_is_nested_zstd_atomic_and_manifested(tmp_path: object) -> None:
    from pathlib import Path

    root = Path(str(tmp_path))
    archived = RawArchiveWriter(root).write_depth(
        [update(101, 101, 100, bids=(("100", "3.25"),))], object_id="fixed01"
    )

    assert archived.absolute_path.is_file()
    assert not list(root.rglob("*.tmp"))
    parquet = pq.ParquetFile(archived.absolute_path)
    assert parquet.metadata.num_rows == 1
    assert parquet.metadata.row_group(0).column(0).compression == "ZSTD"
    row = parquet.read().to_pylist()[0]
    assert row["bids"] == [{"price": "100", "quantity": "3.25"}]

    manifest = write_daily_manifest(
        root,
        date(2026, 7, 14),
        [archived],
        manifest_id="fixed01",
        created_at=datetime(2026, 7, 14, 11, tzinfo=UTC),
    )
    document = json.loads(manifest.read_text())
    assert document["objects"][0]["sha256"] == archived.sha256
    assert len(manifest_hash(manifest)) == 64


def test_only_exact_signed_remote_receipt_verifies(tmp_path: object) -> None:
    from pathlib import Path

    archived = RawArchiveWriter(Path(str(tmp_path))).write_depth(
        [update(101, 101, 100)], object_id="receipt01"
    )
    signer = Ed25519PrivateKey.generate()
    unsigned = RemoteReceipt(
        object_path=archived.relative_path,
        object_sha256=archived.sha256,
        object_size_bytes=archived.size_bytes,
        remote_uri="oci://quant-archive/object",
        remote_etag="etag-1",
        uploaded_at=BASE_TIME,
        signer_key_id="archive-key-1",
        signature_base64="",
    )
    signed = unsigned.model_copy(
        update={
            "signature_base64": base64.b64encode(signer.sign(unsigned.signing_bytes())).decode()
        }
    )

    assert verify_remote_receipt(signed, archived, signer.public_key())
    tampered = signed.model_copy(update={"object_sha256": "f" * 64})
    assert not verify_remote_receipt(tampered, archived, signer.public_key())


def test_only_exact_remote_decryption_receipt_verifies(tmp_path: object) -> None:
    from pathlib import Path

    archived = RawArchiveWriter(Path(str(tmp_path))).write_depth(
        [update(101, 101, 100)], object_id="decrypt01"
    )
    signer = Ed25519PrivateKey.generate()
    unsigned = RemoteDecryptionReceipt(
        receipt_id="a" * 32,
        object_path=archived.relative_path,
        remote_ciphertext_path="objects/a" + "a" * 31 + ".age",
        plaintext_sha256=archived.sha256,
        plaintext_size_bytes=archived.size_bytes,
        ciphertext_sha256="b" * 64,
        ciphertext_size_bytes=archived.size_bytes + 200,
        remote_uri="sftp://archive.example/objects/decrypt01.age",
        remote_etag="b" * 64,
        uploaded_at=BASE_TIME,
        decrypted_at=BASE_TIME + timedelta(seconds=1),
        encryption_format="age-v1-x25519",
        inspection_type="PARQUET",
        inspection_status="PASS",
        parquet_row_count=archived.row_count,
        parquet_schema_version=archived.schema_version,
        signer_key_id="archive-receipt-20260714",
        signature_base64="",
    )
    receiver = runpy.run_path(
        str(Path(__file__).parents[2] / "deploy" / "archive-receiver" / "receiver.py")
    )
    assert receiver["_json_utc"](BASE_TIME).endswith("Z")
    signed_document = receiver["_sign_receipt"](
        unsigned.model_dump(mode="json", exclude={"signature_base64"}), signer
    )
    signed = RemoteDecryptionReceipt.model_validate(signed_document)

    arguments = {
        "expected_ciphertext_sha256": "b" * 64,
        "expected_ciphertext_size_bytes": archived.size_bytes + 200,
        "expected_signer_key_id": "archive-receipt-20260714",
    }
    assert verify_remote_decryption_receipt(signed, archived, signer.public_key(), **arguments)
    assert not verify_remote_decryption_receipt(
        signed,
        archived,
        signer.public_key(),
        seen_receipt_ids={signed.receipt_id},
        **arguments,
    )
    tampered = signed.model_copy(update={"parquet_row_count": archived.row_count + 1})
    assert not verify_remote_decryption_receipt(
        tampered, archived, signer.public_key(), **arguments
    )


def test_retention_never_selects_unverified_objects() -> None:
    old_verified = RetentionCandidate(
        path=__import__("pathlib").Path("verified.parquet"),
        size_bytes=10,
        modified_at=BASE_TIME,
    )
    old_unverified = RetentionCandidate(
        path=__import__("pathlib").Path("unverified.parquet"),
        size_bytes=10,
        modified_at=BASE_TIME,
    )
    selected = plan_verified_deletions(
        [old_unverified, old_verified],
        remotely_verified={old_verified.path},
        now=BASE_TIME + timedelta(hours=73),
    )
    assert selected == (old_verified,)
