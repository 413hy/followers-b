"""Materialize copy-trading secrets into volatile root-only runtime files."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

from ai_quant.common.private_files import read_private_file

_TOKEN = re.compile(rb"^[1-9][0-9]{4,15}:[A-Za-z0-9_-]{20,}$")
_IDENTIFIERS = re.compile(rb"^-?[1-9][0-9]{0,19}(?:\n-?[1-9][0-9]{0,19})*\n?$")
_DATABASE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize copy-trading runtime secrets")
    parser.add_argument("--environment", choices=("TESTNET", "PRODUCTION"), required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--runtime-directory", type=Path, required=True)
    parser.add_argument("--database-password-file", type=Path, required=True)
    parser.add_argument("--testnet-arm-file", type=Path)
    parser.add_argument("--production-arm-file", type=Path)
    parser.add_argument("--database-port", type=int, default=55432)
    parser.add_argument("--database-name", default="aiq_business")
    parser.add_argument("--database-user", default="aiq_business")
    parser.add_argument("--telegram-token-file", type=Path, required=True)
    parser.add_argument("--telegram-chat-ids-file", type=Path, required=True)
    parser.add_argument("--telegram-authorized-user-ids-file", type=Path, required=True)
    return parser.parse_args()


def _read(path: Path, root: Path, reason: str, maximum: int = 4096) -> bytes:
    return read_private_file(
        path,
        forbidden_repository_root=root,
        maximum_bytes=maximum,
        unsafe_reason=reason,
    ).strip()


def _write_private(directory: Path, name: str, payload: bytes) -> None:
    if not payload or b"\x00" in payload:
        raise ValueError("COPY_RUNTIME_SECRET_INVALID")
    with tempfile.NamedTemporaryFile(dir=directory, prefix=f".{name}.", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(payload + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o400)
    temporary.replace(directory / name)


def _database_url(*, password: bytes, port: int, database: str, user: str) -> bytes:
    if not 1 <= port <= 65535:
        raise ValueError("COPY_DATABASE_PORT_INVALID")
    if not _DATABASE_IDENTIFIER.fullmatch(database) or not _DATABASE_IDENTIFIER.fullmatch(user):
        raise ValueError("COPY_DATABASE_IDENTIFIER_INVALID")
    encoded_password = quote(password.decode("ascii"), safe="")
    return f"postgresql://{user}:{encoded_password}@127.0.0.1:{port}/{database}".encode("ascii")


def _validate_production_arm(payload: bytes) -> None:
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("COPY_PRODUCTION_ARM_INVALID") from error
    if not isinstance(document, dict) or document.get("environment") != "PRODUCTION":
        raise ValueError("COPY_PRODUCTION_ARM_INVALID")


def _replace_environment_arm(
    *,
    runtime: Path,
    environment: str,
    testnet_arm: bytes | None,
    production_arm: bytes | None,
) -> None:
    name, payload, stale_name = _validated_environment_arm(
        environment=environment,
        testnet_arm=testnet_arm,
        production_arm=production_arm,
    )
    _write_private(runtime, name, payload)
    (runtime / stale_name).unlink(missing_ok=True)


def _validated_environment_arm(
    *,
    environment: str,
    testnet_arm: bytes | None,
    production_arm: bytes | None,
) -> tuple[str, bytes, str]:
    if environment == "TESTNET":
        if testnet_arm != b"TESTNET_COPY_TRADING_ARMED" or production_arm is not None:
            raise ValueError("COPY_TESTNET_ARM_INVALID")
        return "copy-testnet-arm", testnet_arm, "copy-production-arm.json"
    if production_arm is None or testnet_arm is not None:
        raise ValueError("COPY_PRODUCTION_ARM_INVALID")
    _validate_production_arm(production_arm)
    return "copy-production-arm.json", production_arm, "copy-testnet-arm"


def main() -> int:
    arguments = _arguments()
    runtime = arguments.runtime_directory
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(runtime, 0o700)
    password = _read(
        arguments.database_password_file,
        arguments.repository_root,
        "COPY_DATABASE_PASSWORD_FILE_UNSAFE",
    )
    testnet_arm = (
        None
        if arguments.testnet_arm_file is None
        else _read(
            arguments.testnet_arm_file,
            arguments.repository_root,
            "COPY_TESTNET_ARM_FILE_UNSAFE",
            128,
        )
    )
    production_arm = (
        None
        if arguments.production_arm_file is None
        else _read(
            arguments.production_arm_file,
            arguments.repository_root,
            "COPY_PRODUCTION_ARM_FILE_UNSAFE",
            16_384,
        )
    )
    token = _read(
        arguments.telegram_token_file,
        arguments.repository_root,
        "COPY_TELEGRAM_TOKEN_FILE_UNSAFE",
    )
    chats = _read(
        arguments.telegram_chat_ids_file,
        arguments.repository_root,
        "COPY_TELEGRAM_CHATS_FILE_UNSAFE",
    )
    users = _read(
        arguments.telegram_authorized_user_ids_file,
        arguments.repository_root,
        "COPY_TELEGRAM_USERS_FILE_UNSAFE",
    )
    if len(password) > 512 or any(byte < 33 or byte > 126 for byte in password):
        raise ValueError("COPY_DATABASE_PASSWORD_INVALID")
    if not _TOKEN.fullmatch(token):
        raise ValueError("COPY_TELEGRAM_TOKEN_INVALID")
    if not _IDENTIFIERS.fullmatch(chats) or not _IDENTIFIERS.fullmatch(users):
        raise ValueError("COPY_TELEGRAM_IDENTIFIERS_INVALID")
    if any(int(value) <= 0 for value in users.decode("ascii").splitlines()):
        raise ValueError("COPY_TELEGRAM_USERS_INVALID")
    database_url = _database_url(
        password=password,
        port=arguments.database_port,
        database=arguments.database_name,
        user=arguments.database_user,
    )
    # Validate the complete environment profile before replacing any runtime file.
    # A malformed production arm must not leave a partially switched database URL.
    _validated_environment_arm(
        environment=arguments.environment,
        testnet_arm=testnet_arm,
        production_arm=production_arm,
    )
    _write_private(runtime, "copy-business-db-password", password)
    _write_private(runtime, "copy-business-database-url", database_url)
    _replace_environment_arm(
        runtime=runtime,
        environment=arguments.environment,
        testnet_arm=testnet_arm,
        production_arm=production_arm,
    )
    _write_private(runtime, "copy-telegram-bot-token", token)
    _write_private(runtime, "copy-telegram-chat-ids", chats)
    _write_private(runtime, "copy-telegram-authorized-user-ids", users)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
