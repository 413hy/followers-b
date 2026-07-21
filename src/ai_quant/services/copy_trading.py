"""Long-running 30-second copy-trading poller."""

from __future__ import annotations

import argparse
import json
import signal
import subprocess  # nosec B404 -- fixed systemctl path and unit
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ai_quant.binance_egress.testnet_probe import (
    BinanceFuturesEnvironment,
    BinanceProductionClient,
    BinanceTestnetClient,
    TestnetProbeError,
)
from ai_quant.common.private_files import read_private_file
from ai_quant.common.resilience import bounded_exponential_backoff
from ai_quant.copy_trading.application import CopyRuntimeMode, CopyTradingRuntime
from ai_quant.copy_trading.binance_public import BinancePublicCopyClient
from ai_quant.copy_trading.execution import HedgeTestnetMarketExecutor
from ai_quant.copy_trading.postgres import PostgresSubmissionJournal, SubmissionJournalError
from ai_quant.copy_trading.production_activation import parse_production_activation
from ai_quant.copy_trading.repository import CopyRepositoryError, CopyTradingRepository

_STOP = False
_CODEX_AUDIT_UNIT = "aiq-copy-codex-audit.service"


class _CodexIncidentTrigger:
    """Start one immediate audit for each persisted incident in this poller process."""

    def __init__(self) -> None:
        self._requested: set[str] = set()
        self._pending: set[str] = set()

    def __call__(self, incident_key: str) -> None:
        if incident_key in self._requested:
            return
        self._pending.add(incident_key)
        self.flush()

    def flush(self) -> None:
        """Deliver incidents that arrived while another audit was still running."""

        if not self._pending:
            return
        try:
            active = subprocess.run(  # noqa: S603  # nosec B603
                ["/usr/bin/systemctl", "is-active", "--quiet", _CODEX_AUDIT_UNIT],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
            if active.returncode == 0:
                # Keep the keys queued. The poller calls flush at the beginning of every
                # cycle, so an incident that arrived during a long audit cannot be lost.
                return
            started = subprocess.run(  # noqa: S603  # nosec B603
                ["/usr/bin/systemctl", "start", "--no-block", _CODEX_AUDIT_UNIT],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as error:
            print(
                json.dumps(
                    {
                        "event": "copy_codex_wakeup_failed",
                        "incident_key": sorted(self._pending)[0][:240],
                        "reason": type(error).__name__,
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
            return
        if started.returncode != 0:
            print(
                json.dumps(
                    {
                        "event": "copy_codex_wakeup_failed",
                        "incident_key": sorted(self._pending)[0][:240],
                        "reason": f"SYSTEMCTL_EXIT_{started.returncode}",
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
            return
        delivered = set(self._pending)
        self._pending.clear()
        self._requested.update(delivered)
        if len(self._requested) > 2048:
            self._requested.clear()
            self._requested.update(delivered)
        print(
            json.dumps(
                {
                    "event": "copy_codex_wakeup_started",
                    "incident_key": sorted(delivered)[0][:240],
                    "incident_count": len(delivered),
                },
                separators=(",", ":"),
            ),
            flush=True,
        )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the public leader Testnet copy poller")
    parser.add_argument("--mode", choices=[mode.value for mode in CopyRuntimeMode], required=True)
    parser.add_argument("--database-url-file", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--api-secret-file", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--testnet-arm-file", type=Path)
    parser.add_argument("--production-arm-file", type=Path)
    parser.add_argument("--interval-seconds", type=int, default=30)
    return parser.parse_args()


def _private_text(
    path: Path,
    repository_root: Path,
    *,
    reason: str,
    maximum_bytes: int = 4096,
) -> str:
    raw = read_private_file(
        path,
        forbidden_repository_root=repository_root,
        maximum_bytes=maximum_bytes,
        unsafe_reason=reason,
    )
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ValueError(reason) from error
    if not value or "\n" in value or "\r" in value:
        raise ValueError(reason)
    return value


def _private_document_text(
    path: Path,
    repository_root: Path,
    *,
    reason: str,
    maximum_bytes: int = 16_384,
) -> str:
    raw = read_private_file(
        path,
        forbidden_repository_root=repository_root,
        maximum_bytes=maximum_bytes,
        unsafe_reason=reason,
    )
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ValueError(reason) from error
    if not value:
        raise ValueError(reason)
    return value


def _require_empty_initial_production_account(account: Mapping[str, Any]) -> None:
    if account.get("canTrade") is not True:
        raise RuntimeError("COPY_PRODUCTION_ACCOUNT_TRADING_DISABLED")
    positions = account.get("positions")
    if not isinstance(positions, list):
        raise RuntimeError("COPY_PRODUCTION_ACCOUNT_POSITIONS_INVALID")
    for position in positions:
        if not isinstance(position, Mapping):
            raise RuntimeError("COPY_PRODUCTION_ACCOUNT_POSITIONS_INVALID")
        try:
            quantity = Decimal(str(position.get("positionAmt")))
        except (InvalidOperation, ValueError) as error:
            raise RuntimeError("COPY_PRODUCTION_ACCOUNT_POSITIONS_INVALID") from error
        if not quantity.is_finite():
            raise RuntimeError("COPY_PRODUCTION_ACCOUNT_POSITIONS_INVALID")
        if quantity != 0:
            raise RuntimeError("COPY_PRODUCTION_INITIAL_ACCOUNT_NOT_FLAT")


def _stop_requested(*_: object) -> None:
    global _STOP
    _STOP = True


def _interruptible_wait(seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while not _STOP and time.monotonic() < deadline:
        time.sleep(min(0.5, deadline - time.monotonic()))


def main() -> int:
    arguments = _arguments()
    if not 5 <= arguments.interval_seconds <= 300:
        raise ValueError("COPY_POLL_INTERVAL_INVALID")
    mode = CopyRuntimeMode(arguments.mode)
    if mode is CopyRuntimeMode.TESTNET:
        if arguments.production_arm_file is not None:
            raise ValueError("COPY_PRODUCTION_ARM_FORBIDDEN_IN_TESTNET")
        if arguments.testnet_arm_file is None:
            raise ValueError("COPY_TESTNET_ARMING_REQUIRED")
        arming = _private_text(
            arguments.testnet_arm_file,
            arguments.repository_root,
            reason="COPY_TESTNET_ARM_FILE_UNSAFE",
            maximum_bytes=128,
        )
        if arming != "TESTNET_COPY_TRADING_ARMED":
            raise ValueError("COPY_TESTNET_ARMING_INVALID")
    elif mode is CopyRuntimeMode.PRODUCTION:
        if arguments.testnet_arm_file is not None:
            raise ValueError("COPY_TESTNET_ARM_FORBIDDEN_IN_PRODUCTION")
        if arguments.production_arm_file is None:
            raise ValueError("COPY_PRODUCTION_ARMING_REQUIRED")
    database_url = _private_text(
        arguments.database_url_file,
        arguments.repository_root,
        reason="COPY_DATABASE_URL_FILE_UNSAFE",
    )
    api_key = _private_text(
        arguments.api_key_file,
        arguments.repository_root,
        reason="COPY_TESTNET_API_KEY_FILE_UNSAFE",
    )
    api_secret = _private_text(
        arguments.api_secret_file,
        arguments.repository_root,
        reason="COPY_EXCHANGE_API_SECRET_FILE_UNSAFE",
    )
    exchange: BinanceTestnetClient
    if mode is CopyRuntimeMode.PRODUCTION:
        if arguments.production_arm_file is None:
            raise ValueError("COPY_PRODUCTION_ARMING_REQUIRED")
        activation_raw = _private_document_text(
            arguments.production_arm_file,
            arguments.repository_root,
            reason="COPY_PRODUCTION_ARM_FILE_UNSAFE",
        )
        parse_production_activation(
            activation_raw,
            api_key=api_key,
            now=datetime.now(UTC),
        )
        exchange = BinanceProductionClient(api_key, api_secret)
        execution_environment = BinanceFuturesEnvironment.PRODUCTION
    else:
        exchange = BinanceTestnetClient(api_key, api_secret)
        execution_environment = BinanceFuturesEnvironment.TESTNET
    exchange.synchronize_time()
    if mode in {CopyRuntimeMode.TESTNET, CopyRuntimeMode.PRODUCTION}:
        if exchange.position_mode().get("dualSidePosition") is not True:
            raise RuntimeError("COPY_ACCOUNT_HEDGE_MODE_REQUIRED")
    repository = CopyTradingRepository(database_url)
    if mode in {CopyRuntimeMode.TESTNET, CopyRuntimeMode.PRODUCTION}:
        existing_binding = repository.execution_environment_binding()
        if mode is CopyRuntimeMode.PRODUCTION and existing_binding is None:
            _require_empty_initial_production_account(exchange.account_information_v2())
            if exchange.all_open_orders():
                raise RuntimeError("COPY_PRODUCTION_INITIAL_ACCOUNT_HAS_OPEN_ORDERS")
        repository.bind_execution_environment(
            execution_environment.value,
            occurred_at=datetime.now(UTC),
        )
    journal = PostgresSubmissionJournal(database_url)
    executor = HedgeTestnetMarketExecutor(
        client=exchange,
        journal=journal,
        environment=execution_environment,
    )
    incident_trigger = _CodexIncidentTrigger()
    runtime = CopyTradingRuntime(
        mode=mode,
        public_client=BinancePublicCopyClient(),
        exchange_client=exchange,
        repository=repository,
        executor=executor,
        incident_callback=incident_trigger,
    )
    signal.signal(signal.SIGTERM, _stop_requested)
    signal.signal(signal.SIGINT, _stop_requested)
    consecutive_dependency_failures = 0
    while not _STOP:
        incident_trigger.flush()
        started = time.monotonic()
        try:
            report = runtime.run_cycle()
        except (CopyRepositoryError, SubmissionJournalError, TestnetProbeError) as error:
            runtime.mark_recovery_required()
            incident_trigger(f"poller-dependency:{type(error).__name__}:{error!s}")
            consecutive_dependency_failures += 1
            delay = bounded_exponential_backoff(consecutive_dependency_failures)
            print(
                json.dumps(
                    {
                        "event": "copy_poll_dependency_error",
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
        print(
            json.dumps(
                {
                    "event": "copy_poll_cycle",
                    "mode": mode.value,
                    "leader_count": report.leader_count,
                    "successful_polls": report.successful_polls,
                    "failed_polls": report.failed_polls,
                    "new_signal_count": report.new_signal_count,
                    "processed_signal_count": report.processed_signal_count,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        remaining = max(0.0, arguments.interval_seconds - (time.monotonic() - started))
        _interruptible_wait(remaining)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
