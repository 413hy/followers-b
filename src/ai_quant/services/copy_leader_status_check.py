"""Daily status check for every currently assigned public copy-trading leader."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from ai_quant.common.private_files import read_private_file
from ai_quant.copy_trading.binance_public import (
    BinancePublicCopyClient,
    BinancePublicCopyError,
    LeaderPage,
)
from ai_quant.copy_trading.leader_slots import LeaderSlot
from ai_quant.copy_trading.repository import CopyTradingRepository


class _PublicDirectory(Protocol):
    def list_all_leaders(
        self,
        *,
        time_range: str = "30D",
        data_type: str = "ROI",
        maximum_pages: int = 400,
    ) -> LeaderPage: ...


class _AvailabilityRepository(Protocol):
    def current_slot_assignments(self) -> Mapping[LeaderSlot, str]: ...

    def record_leader_availability(
        self,
        *,
        slot: LeaderSlot,
        lead_portfolio_id: str,
        state: str,
        public_directory_total: int,
        valid_directory_total: int,
        invalid_row_count: int,
        observed_at: datetime,
    ) -> bool: ...


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check current public copy-leader status")
    parser.add_argument("--database-url-file", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    return parser.parse_args()


def _private_text(path: Path, repository_root: Path) -> str:
    raw = read_private_file(
        path,
        forbidden_repository_root=repository_root,
        maximum_bytes=4096,
        unsafe_reason="COPY_DATABASE_URL_FILE_UNSAFE",
    )
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise ValueError("COPY_DATABASE_URL_INVALID") from error
    if not value or "\n" in value or "\r" in value:
        raise ValueError("COPY_DATABASE_URL_INVALID")
    return value


def run_status_check(
    *,
    repository: _AvailabilityRepository,
    public: _PublicDirectory,
    observed_at: datetime,
) -> dict[str, Any]:
    """Check one complete public-directory snapshot without changing slot or trade state."""

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("copy leader status check time must be timezone-aware")
    checked_at = observed_at.astimezone(UTC)
    assignments = dict(repository.current_slot_assignments())
    if not assignments:
        return {
            "event": "copy_leader_status_check",
            "state": "SUCCEEDED",
            "assigned_count": 0,
            "available_count": 0,
            "missing_count": 0,
            "alerts_created": 0,
        }

    directory = public.list_all_leaders(
        time_range="30D",
        data_type="ROI",
        maximum_pages=400,
    )
    valid_total = len(directory.leaders)
    if directory.total <= 0 or valid_total <= 0:
        raise BinancePublicCopyError("COPY_LEADER_STATUS_DIRECTORY_EMPTY")
    # Binance has no snapshot token. If pages shifted enough to introduce duplicates,
    # the scan cannot prove that an absent ID really disappeared and must fail closed.
    if valid_total + directory.invalid_row_count < directory.total:
        raise BinancePublicCopyError("COPY_LEADER_STATUS_DIRECTORY_INCOMPLETE")

    available_ids = {leader.lead_portfolio_id for leader in directory.leaders}
    invalid_ids = set(directory.invalid_leader_ids)
    assigned_ids = set(assignments.values())
    if assigned_ids & invalid_ids:
        raise BinancePublicCopyError("COPY_LEADER_STATUS_ASSIGNED_ROW_INVALID")

    missing_count = 0
    alerts_created = 0
    slot_order = {slot: index for index, slot in enumerate(LeaderSlot)}
    for slot, leader_id in sorted(assignments.items(), key=lambda item: slot_order[item[0]]):
        state = "AVAILABLE" if leader_id in available_ids else "MISSING"
        if state == "MISSING":
            missing_count += 1
        if repository.record_leader_availability(
            slot=slot,
            lead_portfolio_id=leader_id,
            state=state,
            public_directory_total=directory.total,
            valid_directory_total=valid_total,
            invalid_row_count=directory.invalid_row_count,
            observed_at=checked_at,
        ):
            alerts_created += 1

    return {
        "event": "copy_leader_status_check",
        "state": "SUCCEEDED",
        "assigned_count": len(assignments),
        "available_count": len(assignments) - missing_count,
        "missing_count": missing_count,
        "alerts_created": alerts_created,
        "public_directory_total": directory.total,
        "valid_directory_total": valid_total,
        "invalid_row_count": directory.invalid_row_count,
    }


def main() -> int:
    arguments = _arguments()
    repository = CopyTradingRepository(
        _private_text(arguments.database_url_file, arguments.repository_root)
    )
    result = run_status_check(
        repository=repository,
        public=BinancePublicCopyClient(),
        observed_at=datetime.now(UTC),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
