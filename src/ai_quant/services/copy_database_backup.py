"""Verified, retained local PostgreSQL backups for the copy-trading ledger."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess  # nosec B404 -- fixed docker binary and validated identifiers
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

_TESTNET_PROFILE = ("aiq-copy-trading-postgres-1", "aiq_business", "aiq_business")
_PRODUCTION_PROFILE = (
    "aiq-copy-production-postgres-1",
    "aiq_copy_production",
    "aiq_copy_production",
)
_ALLOWED_PROFILES = {_TESTNET_PROFILE, _PRODUCTION_PROFILE}
_BACKUP_PATTERN = re.compile(
    r"^(?:aiq_business|aiq_copy_production)-[0-9]{8}T[0-9]{6}Z\.(?:dump|json)$"
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Back up the copy-trading PostgreSQL database")
    parser.add_argument("--container", default=_TESTNET_PROFILE[0])
    parser.add_argument("--database", default=_TESTNET_PROFILE[1])
    parser.add_argument("--user", default=_TESTNET_PROFILE[2])
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=Path("/var/lib/ai-quant/backups/copy-trading"),
    )
    parser.add_argument("--retention-days", type=int, default=14)
    return parser.parse_args()


def _run_to_file(command: list[str], output: BinaryIO, *, timeout: int) -> None:
    result = subprocess.run(  # noqa: S603  # nosec B603
        command,
        stdout=output,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError("COPY_DATABASE_BACKUP_DUMP_FAILED")


def _verify_archive(command: list[str], archive: BinaryIO, *, timeout: int) -> None:
    result = subprocess.run(  # noqa: S603  # nosec B603
        command,
        stdin=archive,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError("COPY_DATABASE_BACKUP_VERIFY_FAILED")


def _run_command(
    command: list[str],
    *,
    timeout: int,
    expected_stdout: str | None = None,
) -> None:
    result = subprocess.run(  # noqa: S603  # nosec B603
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0 or (
        expected_stdout is not None and result.stdout.strip() != expected_stdout
    ):
        raise RuntimeError("COPY_DATABASE_BACKUP_RESTORE_DRILL_FAILED")


def _verify_isolated_restore(
    *,
    container: str,
    user: str,
    archive_path: Path,
    restore_database: str,
) -> None:
    created = False
    try:
        _run_command(
            [
                "/usr/bin/docker",
                "exec",
                container,
                "createdb",
                "--username",
                user,
                "--template=template0",
                restore_database,
            ],
            timeout=60,
        )
        created = True
        with archive_path.open("rb") as source:
            _verify_archive(
                [
                    "/usr/bin/docker",
                    "exec",
                    "--interactive",
                    container,
                    "pg_restore",
                    "--username",
                    user,
                    "--dbname",
                    restore_database,
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                ],
                source,
                timeout=300,
            )
        _run_command(
            [
                "/usr/bin/docker",
                "exec",
                container,
                "psql",
                "--username",
                user,
                "--dbname",
                restore_database,
                "--tuples-only",
                "--no-align",
                "--command",
                (
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE (table_schema,table_name) IN "
                    "(('copytrading','signals'),('copytrading','submission_claims'),"
                    "('copytrading','health_check_runs'),('control','outbox'));"
                ),
            ],
            timeout=60,
            expected_stdout="4",
        )
    finally:
        if created:
            _run_command(
                [
                    "/usr/bin/docker",
                    "exec",
                    container,
                    "dropdb",
                    "--username",
                    user,
                    "--force",
                    restore_database,
                ],
                timeout=60,
            )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, document: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _prune(root: Path, *, now: datetime, retention_days: int) -> None:
    cutoff = now - timedelta(days=retention_days)
    for path in root.iterdir():
        if not path.is_file() or not _BACKUP_PATTERN.fullmatch(path.name):
            continue
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if modified < cutoff:
            path.unlink(missing_ok=True)


def run_backup(
    *,
    container: str,
    database: str,
    user: str,
    backup_root: Path,
    retention_days: int,
    now: datetime | None = None,
) -> dict[str, object]:
    if (container, database, user) not in _ALLOWED_PROFILES or not 2 <= retention_days <= 90:
        raise ValueError("COPY_DATABASE_BACKUP_CONFIGURATION_INVALID")
    occurred_at = now or datetime.now(UTC)
    if occurred_at.tzinfo is None or occurred_at.utcoffset() != UTC.utcoffset(occurred_at):
        raise ValueError("COPY_DATABASE_BACKUP_TIME_INVALID")
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = occurred_at.strftime("%Y%m%dT%H%M%SZ")
    archive = backup_root / f"{database}-{stamp}.dump"
    temporary_archive = archive.with_suffix(".dump.tmp")
    with temporary_archive.open("xb") as output:
        os.chmod(temporary_archive, 0o600)
        _run_to_file(
            [
                "/usr/bin/docker",
                "exec",
                container,
                "pg_dump",
                "--username",
                user,
                "--dbname",
                database,
                "--format=custom",
                "--no-owner",
                "--no-acl",
            ],
            output,
            timeout=300,
        )
        output.flush()
        os.fsync(output.fileno())
    if temporary_archive.stat().st_size < 1024:
        raise RuntimeError("COPY_DATABASE_BACKUP_TOO_SMALL")
    with temporary_archive.open("rb") as source:
        _verify_archive(
            ["/usr/bin/docker", "exec", "--interactive", container, "pg_restore", "--list"],
            source,
            timeout=120,
        )
    _verify_isolated_restore(
        container=container,
        user=user,
        archive_path=temporary_archive,
        restore_database=f"aiq_restore_verify_{occurred_at.strftime('%Y%m%d%H%M%S')}",
    )
    temporary_archive.replace(archive)
    report: dict[str, object] = {
        "schema_version": "1.0.0",
        "created_at": occurred_at.isoformat(),
        "database": database,
        "archive": archive.name,
        "size_bytes": archive.stat().st_size,
        "sha256": _sha256(archive),
        "verified": True,
        "verification": "PG_RESTORE_LIST_AND_ISOLATED_RESTORE",
    }
    _write_json(backup_root / f"{database}-{stamp}.json", report)
    _write_json(backup_root / "latest.json", report)
    _prune(backup_root, now=occurred_at, retention_days=retention_days)
    return report


def main() -> int:
    arguments = _arguments()
    arguments.backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = arguments.backup_root / ".backup.lock"
    with lock_path.open("a", encoding="ascii") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        report = run_backup(
            container=arguments.container,
            database=arguments.database,
            user=arguments.user,
            backup_root=arguments.backup_root,
            retention_days=arguments.retention_days,
        )
    print(
        json.dumps(
            {
                "event": "copy_database_backup",
                "archive": report["archive"],
                "size_bytes": report["size_bytes"],
                "verified": report["verified"],
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
