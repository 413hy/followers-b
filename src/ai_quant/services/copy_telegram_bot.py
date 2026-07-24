"""Long-polling Telegram menu service for copy-trading observability and control."""

from __future__ import annotations

import argparse
import json
import signal
import subprocess  # nosec B404 -- fixed systemctl path and unit
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_quant.common.private_files import read_private_file
from ai_quant.common.resilience import bounded_exponential_backoff
from ai_quant.copy_trading.repository import CopyTradingRepository
from ai_quant.copy_trading.telegram_leader_admin import LiveTelegramLeaderAdmin
from ai_quant.copy_trading.telegram_state import PostgresTelegramState, TelegramStateError
from ai_quant.notifications.telegram_bot import (
    TelegramBotClient,
    TelegramBotError,
    TelegramBotFileConfig,
    TelegramMenuRouter,
    notification_inline_keyboard,
    persistent_reply_keyboard,
)

_STOP = False
_CODEX_AUDIT_UNIT = "aiq-copy-codex-audit.service"


def _trigger_codex_audit() -> bool:
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603
            ["/usr/bin/systemctl", "start", "--no-block", _CODEX_AUDIT_UNIT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the copy-trading Telegram bot")
    parser.add_argument("--database-url-file", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--chat-ids-file", type=Path, required=True)
    parser.add_argument("--authorized-user-ids-file", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--environment", choices=("TESTNET", "PRODUCTION"), default="TESTNET")
    return parser.parse_args()


def _private_database_url(path: Path, repository_root: Path) -> str:
    raw = read_private_file(
        path,
        forbidden_repository_root=repository_root,
        maximum_bytes=4096,
        unsafe_reason="TELEGRAM_DATABASE_URL_FILE_UNSAFE",
    )
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ValueError("TELEGRAM_DATABASE_URL_INVALID") from error
    if not value or "\n" in value or "\r" in value:
        raise ValueError("TELEGRAM_DATABASE_URL_INVALID")
    return value


def _stop(*_: object) -> None:
    global _STOP
    _STOP = True


def _interruptible_wait(seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while not _STOP and time.monotonic() < deadline:
        time.sleep(min(0.5, deadline - time.monotonic()))


def _identity(update: Mapping[str, Any]) -> tuple[int | None, int | None]:
    message = update.get("message")
    if not isinstance(message, dict):
        callback = update.get("callback_query")
        message = callback.get("message") if isinstance(callback, dict) else None
        sender = callback.get("from") if isinstance(callback, dict) else None
    else:
        sender = message.get("from")
    chat = message.get("chat") if isinstance(message, dict) else None
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    user_id = sender.get("id") if isinstance(sender, dict) else None
    return (
        int(chat_id) if isinstance(chat_id, int) else None,
        int(user_id) if isinstance(user_id, int) else None,
    )


def main() -> int:
    arguments = _arguments()
    config = TelegramBotFileConfig.load(
        arguments.token_file,
        arguments.chat_ids_file,
        arguments.authorized_user_ids_file,
    )
    client = TelegramBotClient(config)
    database_url = _private_database_url(arguments.database_url_file, arguments.repository_root)
    state = PostgresTelegramState(
        database_url,
        execution_environment=arguments.environment,
    )
    leader_admin = LiveTelegramLeaderAdmin(
        state=state,
        repository=CopyTradingRepository(database_url),
        execution_environment=arguments.environment,
    )
    router = TelegramMenuRouter(
        client=client,
        dashboard=state,
        challenges=state,
        controls=state,
        leader_admin=leader_admin,
        position_admin=state,
        margin_admin=state,
        audit_trigger=_trigger_codex_audit,
    )
    try:
        client.configure_menu()
    except TelegramBotError as error:
        # The reply keyboard still works through /start and /menu if Telegram's
        # command-menu configuration is temporarily unavailable.
        print(
            json.dumps(
                {
                    "event": "copy_telegram_menu_config_error",
                    "reason": str(error),
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
    for startup_chat_id in config.allowed_chat_ids:
        try:
            client.send_message(
                startup_chat_id,
                "⌨️ 导航键盘已恢复。收起后可用输入框旁的小图标再次展开。",
                reply_markup=persistent_reply_keyboard(),
                disable_notification=True,
            )
        except TelegramBotError as error:
            print(
                json.dumps(
                    {
                        "event": "copy_telegram_keyboard_restore_error",
                        "reason": str(error),
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    offset: int | None = None
    consecutive_dependency_failures = 0
    while not _STOP:
        try:
            if offset is None:
                offset = state.next_offset()
            for notification in state.claim_notifications():
                delivered = True
                try:
                    for notify_chat_id in config.allowed_chat_ids:
                        client.send_message(
                            notify_chat_id,
                            notification.text,
                            reply_markup=(
                                None
                                if notification.contextual_view is None
                                else notification_inline_keyboard(
                                    notification.contextual_view,
                                    notification.message_id,
                                )
                            ),
                        )
                except (TelegramBotError, ValueError):
                    delivered = False
                state.complete_notification(notification.message_id, delivered=delivered)
            try:
                # Keep the long poll shorter than systemd's stop timeout so SIGTERM
                # can be observed and upgrades never require a forced kill.
                updates = client.get_updates(offset=offset, timeout_seconds=10)
            except TelegramBotError as error:
                print(
                    json.dumps(
                        {
                            "event": "copy_telegram_poll_error",
                            "reason": str(error),
                        },
                        separators=(",", ":"),
                    ),
                    flush=True,
                )
                _interruptible_wait(5)
                continue
            for update in updates:
                update_id = update.get("update_id")
                if not isinstance(update_id, int):
                    continue
                chat_id, user_id = _identity(update)
                authorized = (
                    chat_id in config.allowed_chat_ids and user_id in config.authorized_user_ids
                )
                try:
                    router.handle(update)
                except (TelegramBotError, ValueError) as error:
                    # A rejected callback must never become a poison update: record it and
                    # advance the offset so Telegram cannot replay it after a restart.
                    print(
                        json.dumps(
                            {
                                "event": "copy_telegram_update_error",
                                "update_id": update_id,
                                "reason": str(error),
                            },
                            separators=(",", ":"),
                        ),
                        flush=True,
                    )
                state.record_update(
                    update,
                    chat_id=chat_id,
                    user_id=user_id,
                    authorized=authorized,
                    processed_at=datetime.now(UTC),
                )
                offset = max(offset, update_id + 1)
        except TelegramStateError as error:
            consecutive_dependency_failures += 1
            delay = bounded_exponential_backoff(consecutive_dependency_failures)
            print(
                json.dumps(
                    {
                        "event": "copy_telegram_dependency_error",
                        "reason": str(error),
                        "retry_in_seconds": delay,
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
            _interruptible_wait(delay)
            continue
        consecutive_dependency_failures = 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
