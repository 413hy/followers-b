"""One-shot deterministic copy-trading watchdog."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess  # nosec B404 -- fixed systemctl path and allowlisted unit names
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from ai_quant.binance_egress.testnet_probe import (
    BinanceFuturesEnvironment,
    BinanceProductionClient,
    BinanceTestnetClient,
)
from ai_quant.copy_trading.health import (
    DatabaseHealthFacts,
    HealthReport,
    HealthState,
    HostHealthFacts,
    PostgresHealthStore,
    evaluate_health,
    exchange_positions_from_account,
    logical_account_snapshot,
)
from ai_quant.copy_trading.models import RuntimeControlState
from ai_quant.copy_trading.production_activation import parse_production_activation
from ai_quant.copy_trading.repository import CopyTradingRepository, RuntimeControl
from ai_quant.services.copy_trading import _private_document_text, _private_text

_CODEX_AUDIT_UNIT = "aiq-copy-codex-audit.service"
_OBSERVED_UNITS = (
    "aiq-copy-poller.service",
    "aiq-copy-telegram.service",
    "aiq-testnet-user-stream.service",
)
_BACKUP_REPORT = Path("/var/lib/ai-quant/backups/copy-trading/latest.json")
_RECONCILIATION_MISMATCH_REASONS = frozenset({"COPY_POSITION_RECONCILIATION_MISMATCH"})
_SAFE_RECONCILIATION_RESUME_FINDINGS = frozenset({"COPY_CODEX_AUDIT_REPORTED_FAILURE"})
_TRANSIENT_SERVICE_PAUSE_REASONS = frozenset(
    {
        "COPY_REQUIRED_SERVICE_INACTIVE",
        "COPY_TESTNET_USER_STREAM_INACTIVE",
    }
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run deterministic copy-trading watchdog")
    parser.add_argument(
        "--mode",
        choices=("testnet", "production"),
        default="testnet",
    )
    parser.add_argument("--database-url-file", type=Path, required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--api-secret-file", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--testnet-arm-file", type=Path)
    parser.add_argument("--production-arm-file", type=Path)
    parser.add_argument("--backup-report-file", type=Path, default=_BACKUP_REPORT)
    return parser.parse_args()


def _unit_state(unit: str) -> str:
    if unit not in _OBSERVED_UNITS:
        return "unknown"
    result = subprocess.run(  # noqa: S603  # nosec B603
        ["/usr/bin/systemctl", "is-active", unit],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    state = result.stdout.strip()
    return state if state in {"active", "inactive", "failed", "activating"} else "unknown"


def _available_memory_bytes(path: Path = Path("/proc/meminfo")) -> int:
    for line in path.read_text(encoding="ascii").splitlines():
        if line.startswith("MemAvailable:"):
            fields = line.split()
            if len(fields) == 3 and fields[2] == "kB" and fields[1].isdigit():
                return int(fields[1]) * 1024
    raise RuntimeError("COPY_HOST_MEMORY_FACTS_UNAVAILABLE")


def _host_facts(
    *,
    environment: BinanceFuturesEnvironment,
    backup_report: Path,
) -> HostHealthFacts:
    disk = shutil.disk_usage("/")
    free_percent = (
        Decimal(disk.free) * Decimal("100") / Decimal(disk.total)
        if disk.total > 0
        else Decimal("0")
    )
    observed_units = (
        _OBSERVED_UNITS if environment is BinanceFuturesEnvironment.TESTNET else _OBSERVED_UNITS[:2]
    )
    return HostHealthFacts(
        service_states={unit: _unit_state(unit) for unit in observed_units},
        root_free_bytes=disk.free,
        root_free_percent=free_percent,
        memory_available_bytes=_available_memory_bytes(),
        backup_age_hours=_backup_age_hours(datetime.now(UTC), backup_report),
        user_stream_unit=(
            "aiq-testnet-user-stream.service"
            if environment is BinanceFuturesEnvironment.TESTNET
            else None
        ),
    )


def _exchange_client(
    *,
    environment: BinanceFuturesEnvironment,
    api_key: str,
    api_secret: str,
    testnet_activation: str | None,
    production_activation_raw: str | None,
    now: datetime,
) -> BinanceTestnetClient:
    if environment is BinanceFuturesEnvironment.PRODUCTION:
        if testnet_activation is not None:
            raise ValueError("COPY_TESTNET_ARM_FORBIDDEN_IN_PRODUCTION")
        if production_activation_raw is None:
            raise ValueError("COPY_PRODUCTION_ARMING_REQUIRED")
        parse_production_activation(production_activation_raw, api_key=api_key, now=now)
        return BinanceProductionClient(api_key, api_secret)
    if production_activation_raw is not None:
        raise ValueError("COPY_PRODUCTION_ARM_FORBIDDEN_IN_TESTNET")
    if testnet_activation != "TESTNET_COPY_TRADING_ARMED":
        raise ValueError("COPY_TESTNET_ARMING_INVALID")
    return BinanceTestnetClient(api_key, api_secret)


def _backup_age_hours(now: datetime, path: Path = _BACKUP_REPORT) -> Decimal | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("verified") is not True:
            return None
        created_at = datetime.fromisoformat(str(document["created_at"]))
    except (KeyError, OSError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None
    if created_at.tzinfo is None:
        return None
    age = Decimal(str((now - created_at.astimezone(UTC)).total_seconds())) / Decimal("3600")
    return age if age >= 0 else None


def _trigger_codex_audit() -> bool:
    result = subprocess.run(  # noqa: S603  # nosec B603
        ["/usr/bin/systemctl", "start", "--no-block", _CODEX_AUDIT_UNIT],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=15,
    )
    return result.returncode == 0


def _resolved_reconciliation_pause_can_resume(
    current: RuntimeControl,
    report: HealthReport,
    facts: DatabaseHealthFacts,
    *,
    has_recoverable_signals: bool,
) -> bool:
    """Resume only a watchdog-owned mismatch pause after every safety gate is clean."""

    return (
        current.state is RuntimeControlState.PAUSED_NEW_ENTRIES
        and current.actor_id == "deterministic-watchdog"
        and frozenset(current.reason_codes) == _RECONCILIATION_MISMATCH_REASONS
        and report.state in {HealthState.HEALTHY, HealthState.DEGRADED}
        and all(finding.code in _SAFE_RECONCILIATION_RESUME_FINDINGS for finding in report.findings)
        and report.requested_control is None
        and not facts.pending_entry_allowances
        and not has_recoverable_signals
    )


def _retired_account_risk_pause_can_resume(
    current: RuntimeControl,
    report: HealthReport,
) -> bool:
    """Release pauses created by the removed automatic account-risk controller."""

    return (
        current.state is RuntimeControlState.PAUSED_NEW_ENTRIES
        and current.actor_id == "account-risk-engine"
        and frozenset(current.reason_codes)
        <= frozenset(
            {
                "COPY_ACCOUNT_WARNING_RISK_LINE",
                "COPY_ACCOUNT_EMERGENCY_RISK_LINE",
            }
        )
        and report.requested_control is None
    )


def _resolved_service_pause_can_resume(
    current: RuntimeControl,
    report: HealthReport,
) -> bool:
    """Release only watchdog-owned service pauses after live checks prove recovery."""

    pause_reasons = frozenset(current.reason_codes)
    return (
        current.state is RuntimeControlState.PAUSED_NEW_ENTRIES
        and current.actor_id == "deterministic-watchdog"
        and bool(pause_reasons & _TRANSIENT_SERVICE_PAUSE_REASONS)
        and pause_reasons <= (
            _TRANSIENT_SERVICE_PAUSE_REASONS | _SAFE_RECONCILIATION_RESUME_FINDINGS
        )
        and report.state in {HealthState.HEALTHY, HealthState.DEGRADED}
        and all(finding.code in _SAFE_RECONCILIATION_RESUME_FINDINGS for finding in report.findings)
        and report.requested_control is None
    )


def main() -> int:
    arguments = _arguments()
    environment = BinanceFuturesEnvironment(arguments.mode.upper())
    database_url = _private_text(
        arguments.database_url_file,
        arguments.repository_root,
        reason="COPY_DATABASE_URL_FILE_UNSAFE",
    )
    repository = CopyTradingRepository(database_url)
    if repository.execution_environment_binding() != environment.value:
        raise RuntimeError("COPY_WATCHDOG_EXECUTION_ENVIRONMENT_MISMATCH")
    api_key = _private_text(
        arguments.api_key_file,
        arguments.repository_root,
        reason="COPY_EXCHANGE_API_KEY_FILE_UNSAFE",
    )
    api_secret = _private_text(
        arguments.api_secret_file,
        arguments.repository_root,
        reason="COPY_EXCHANGE_API_SECRET_FILE_UNSAFE",
    )
    production_activation_raw = (
        None
        if arguments.production_arm_file is None
        else _private_document_text(
            arguments.production_arm_file,
            arguments.repository_root,
            reason="COPY_PRODUCTION_ARM_FILE_UNSAFE",
        )
    )
    testnet_activation = (
        None
        if arguments.testnet_arm_file is None
        else _private_text(
            arguments.testnet_arm_file,
            arguments.repository_root,
            reason="COPY_TESTNET_ARM_FILE_UNSAFE",
            maximum_bytes=128,
        )
    )
    client = _exchange_client(
        environment=environment,
        api_key=api_key,
        api_secret=api_secret,
        testnet_activation=testnet_activation,
        production_activation_raw=production_activation_raw,
        now=datetime.now(UTC),
    )
    client.synchronize_time()
    now = datetime.now(UTC)
    store = PostgresHealthStore(database_url)
    facts = store.read_facts()
    account_v3 = client.account_information()
    account_v2 = client.account_information_v2()
    account = logical_account_snapshot(
        account_v3,
        account_v2,
        client.position_mode(),
        baseline_usdt=facts.envelope_baseline_usdt,
        observed_at=now,
    )
    report = evaluate_health(
        facts,
        account,
        exchange_positions_from_account(account_v2),
        now=now,
        host=_host_facts(
            environment=environment,
            backup_report=arguments.backup_report_file,
        ),
    )
    current = repository.latest_runtime_control()
    auto_resumed = False
    if report.requested_control is not None:
        requested = report.requested_control
        should_append = (
            requested is RuntimeControlState.REDUCE_ALL
            and current.state is not RuntimeControlState.REDUCE_ALL
        ) or (
            requested is RuntimeControlState.PAUSED_NEW_ENTRIES
            and current.state is RuntimeControlState.RUNNING
        )
        if should_append:
            repository.append_runtime_control(
                requested,
                actor_id="deterministic-watchdog",
                reason_codes=tuple(item.code for item in report.findings),
                occurred_at=now,
            )
    elif (
        _retired_account_risk_pause_can_resume(current, report)
        or _resolved_service_pause_can_resume(current, report)
        or _resolved_reconciliation_pause_can_resume(
            current,
            report,
            facts,
            has_recoverable_signals=bool(repository.recoverable_signals(limit=1)),
        )
    ):
        if current.actor_id == "account-risk-engine":
            reason_code = "COPY_ACCOUNT_RISK_AUTOMATION_REMOVED_AUTO_RESUME"
            resume_actor = "codex-risk-policy-repair"
        elif frozenset(current.reason_codes) & _TRANSIENT_SERVICE_PAUSE_REASONS:
            reason_code = "COPY_REQUIRED_SERVICE_RECOVERED_AUTO_RESUME"
            resume_actor = "deterministic-watchdog-recovery"
        else:
            reason_code = "COPY_RECONCILIATION_VERIFIED_AUTO_RESUME"
            resume_actor = "codex-reconciliation-repair"
        repository.append_runtime_control(
            RuntimeControlState.RUNNING,
            actor_id=resume_actor,
            reason_codes=(reason_code,),
            occurred_at=now,
            notify=True,
        )
        auto_resumed = True
    wakeup_requested = report.state is not HealthState.HEALTHY
    health_run_id = store.persist(
        report,
        occurred_at=now,
        codex_wakeup_requested=wakeup_requested,
    )
    codex_wakeup_started = _trigger_codex_audit() if wakeup_requested else False
    print(
        json.dumps(
            {
                "event": "copy_watchdog",
                "health_run_id": health_run_id,
                "state": report.state.value,
                "finding_codes": [item.code for item in report.findings],
                "codex_wakeup_started": codex_wakeup_started,
                "auto_resumed": auto_resumed,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
