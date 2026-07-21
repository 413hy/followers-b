import os
from datetime import UTC, datetime, timedelta

from ai_quant.services.copy_database_backup import _prune, _sha256, _write_json, run_backup
from ai_quant.services.copy_watchdog import _backup_age_hours


def test_backup_manifest_age_requires_verified_timestamp(tmp_path) -> None:
    now = datetime(2026, 7, 17, 6, 0, tzinfo=UTC)
    report = tmp_path / "latest.json"
    _write_json(
        report,
        {
            "created_at": (now - timedelta(hours=2)).isoformat(),
            "verified": True,
        },
    )

    assert _backup_age_hours(now, report) == 2

    _write_json(report, {"created_at": now.isoformat(), "verified": False})
    assert _backup_age_hours(now, report) is None


def test_backup_retention_only_removes_strict_old_backup_names(tmp_path) -> None:
    now = datetime(2026, 7, 17, 6, 0, tzinfo=UTC)
    old_dump = tmp_path / "aiq_business-20260601T000000Z.dump"
    unrelated = tmp_path / "operator-notes.txt"
    old_dump.write_bytes(b"archive")
    unrelated.write_text("keep", encoding="utf-8")
    old_timestamp = (now - timedelta(days=30)).timestamp()
    os.utime(old_dump, (old_timestamp, old_timestamp))
    os.utime(unrelated, (old_timestamp, old_timestamp))

    _prune(tmp_path, now=now, retention_days=14)

    assert not old_dump.exists()
    assert unrelated.exists()
    assert _sha256(unrelated) == "6ca7ea2feefc88ecb5ed6356ed963f47dc9137f82526fdd25d618ea626d0803f"


def test_backup_rejects_mixed_testnet_and_production_profile(tmp_path) -> None:
    try:
        run_backup(
            container="aiq-copy-trading-postgres-1",
            database="aiq_copy_production",
            user="aiq_copy_production",
            backup_root=tmp_path,
            retention_days=14,
        )
    except ValueError as error:
        assert str(error) == "COPY_DATABASE_BACKUP_CONFIGURATION_INVALID"
    else:
        raise AssertionError("mixed execution environments must fail closed")
