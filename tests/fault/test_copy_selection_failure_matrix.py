from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ai_quant.copy_trading.binance_public import (
    BinancePublicCopyError,
    LeaderPage,
    OrderHistoryPage,
)
from ai_quant.copy_trading.codex_selection import (
    CodexDailySelector,
    CodexSelectionError,
    CodexSelectionResult,
)
from ai_quant.copy_trading.leader_slots import LeaderSlot
from ai_quant.copy_trading.models import LeaderSnapshot, PublicLeaderOrder
from ai_quant.services import copy_leader_selector as service


def _leader(index: int) -> LeaderSnapshot:
    return LeaderSnapshot(
        lead_portfolio_id=f"5108371059752839{index:03d}",
        nickname=f"leader-{index}",
        roi_pct=Decimal("80"),
        pnl_usdt=Decimal("2000"),
        aum_usdt=Decimal("100000"),
        maximum_drawdown_pct=Decimal("12"),
        win_rate_pct=Decimal("68"),
        current_copy_count=10,
        maximum_copy_count=100,
        start_time_ms=1_700_000_000_000,
        portfolio_type="PUBLIC",
        raw_payload_hash=f"{index:064x}",
    )


def _quality_orders(leader_id: str) -> tuple[PublicLeaderOrder, ...]:
    now = datetime.now(UTC)
    orders: list[PublicLeaderOrder] = []
    for index in range(36):
        offset = timedelta(days=index % 6, hours=index // 6 + 1)
        update_ms = int((now - offset).timestamp() * 1000)
        for side, pnl, delta_ms in (
            ("BUY", "0", -1000),
            ("SELL", "-2" if index in {5, 13} else "5", 0),
        ):
            orders.append(
                PublicLeaderOrder.from_api(
                    leader_id,
                    {
                        "symbol": "ETHUSDT",
                        "side": side,
                        "type": "MARKET",
                        "positionSide": "LONG",
                        "executedQty": "1",
                        "avgPrice": "2000",
                        "totalPnl": pnl,
                        "orderTime": update_ms + delta_ms - 1000,
                        "orderUpdateTime": update_ms + delta_ms,
                    },
                )
            )
    return tuple(orders)


def test_candidate_directory_combines_return_win_rate_and_drawdown_rankings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    leaders_by_ranking = {
        "ROI": (_leader(1), _leader(2)),
        "WIN_RATE": (_leader(2), _leader(3)),
        "MDD": (_leader(3), _leader(4)),
    }
    requested: list[str] = []

    class Public:
        def list_leaders(self, **kwargs: Any) -> LeaderPage:
            data_type = str(kwargs["data_type"])
            requested.append(data_type)
            assert kwargs["skip_invalid_rows"] is True
            leaders = leaders_by_ranking[data_type]
            return LeaderPage(
                leaders=leaders,
                total=len(leaders) + (1 if data_type == "WIN_RATE" else 0),
                invalid_row_count=1 if data_type == "WIN_RATE" else 0,
                invalid_reason_codes=(
                    ("COPY_FIELD_NICKNAME_INVALID",) if data_type == "WIN_RATE" else ()
                ),
            )

    leaders = service._candidate_directory(
        Public(),  # type: ignore[arg-type]
        strategy=service.SelectionStrategy.LONG_TERM,
        candidate_pool_size=20,
        observed_at_ms=int(datetime.now(UTC).timestamp() * 1000),
    )

    assert requested == ["ROI", "WIN_RATE", "MDD"]
    assert {leader.lead_portfolio_id for leader in leaders} == {
        leader.lead_portfolio_id for ranking in leaders_by_ranking.values() for leader in ranking
    }
    warning = json.loads(capsys.readouterr().out)
    assert warning == {
        "event": "copy_selection_invalid_candidates_skipped",
        "ranking": "WIN_RATE",
        "count": 1,
        "reason_codes": ["COPY_FIELD_NICKNAME_INVALID"],
    }


def test_candidate_directory_fails_closed_when_every_ranking_row_is_invalid() -> None:
    class Public:
        def list_leaders(self, **kwargs: Any) -> LeaderPage:
            assert kwargs["skip_invalid_rows"] is True
            return LeaderPage(
                leaders=(),
                total=1,
                invalid_row_count=1,
                invalid_reason_codes=("COPY_FIELD_NICKNAME_INVALID",),
            )

    with pytest.raises(
        BinancePublicCopyError,
        match="COPY_SELECTION_DIRECTORY_NO_VALID_CANDIDATES",
    ):
        service._candidate_directory(
            Public(),  # type: ignore[arg-type]
            strategy=service.SelectionStrategy.SHORT_TERM,
            candidate_pool_size=20,
            observed_at_ms=int(datetime.now(UTC).timestamp() * 1000),
        )


def test_selection_failure_restarts_audit_to_avoid_midnight_snapshot_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    def run(command: list[str], **kwargs: Any) -> Result:
        del kwargs
        calls.append(command)
        return Result()

    monkeypatch.setattr(service.subprocess, "run", run)

    assert service._trigger_codex_audit() is True
    assert calls == [
        [
            "/usr/bin/systemctl",
            "restart",
            "--no-block",
            "aiq-copy-codex-audit.service",
        ]
    ]


def test_manual_clear_cooldown_ends_on_next_shanghai_day() -> None:
    assert service._manual_clear_cooldown_start(
        datetime(2026, 7, 20, 17, 2, tzinfo=UTC)
    ) == datetime(2026, 7, 20, 16, 0, tzinfo=UTC)


def test_short_selection_splits_highest_win_rate_and_intraday_composite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaders = (
        replace(
            _leader(1),
            nickname="highest",
            win_rate_pct=Decimal("92"),
        ),
        replace(_leader(2), nickname="balanced", win_rate_pct=Decimal("75")),
        replace(_leader(3), nickname="active", win_rate_pct=Decimal("72")),
        replace(_leader(4), nickname="owner-custom", win_rate_pct=Decimal("99")),
    )

    class Public:
        def list_leaders(self, **kwargs: Any) -> LeaderPage:
            return LeaderPage(leaders=leaders, total=len(leaders))

        def order_history(self, leader_id: str, **kwargs: Any) -> OrderHistoryPage:
            orders = _quality_orders(leader_id)
            return OrderHistoryPage(orders=orders, total=len(orders))

    class Catalog:
        def trading_symbols(self) -> frozenset[str]:
            return frozenset({"ETHUSDT"})

    class Repository:
        selected: tuple[str, ...] = ()

        def __init__(self, dsn: str) -> None:
            assert dsn.startswith("postgresql://")

        def current_slot_assignments(self) -> dict[Any, str]:
            return {
                LeaderSlot.SHORT_TERM_1: leaders[2].lead_portfolio_id,
                LeaderSlot.SHORT_TERM_2: leaders[1].lead_portfolio_id,
                LeaderSlot.CUSTOM_1: leaders[3].lead_portfolio_id,
            }

        def current_locked_leader_ids(self) -> frozenset[str]:
            return frozenset()

        def recently_manually_cleared_leader_ids(self, **kwargs: Any) -> frozenset[str]:
            return frozenset()

        def leader_selection_trend(self, *args: Any, **kwargs: Any) -> None:
            return None

        def record_leader_snapshot(self, *args: Any, **kwargs: Any) -> None:
            pass

        def record_candidate_activity(self, *args: Any, **kwargs: Any) -> None:
            pass

        def apply_slot_selection(
            self,
            candidates: tuple[LeaderSnapshot, ...],
            assessments: dict[str, Any],
            selected_leader_ids: tuple[str, ...],
            **kwargs: Any,
        ) -> str:
            del candidates, assessments
            assert kwargs["strategy"].value == "SHORT_TERM"
            Repository.selected = selected_leader_ids
            return "a" * 64

    selector_calls: list[tuple[str, tuple[str, ...]]] = []

    class Selector:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def select(
            self,
            candidates: tuple[dict[str, object], ...],
            *,
            leader_count: int,
            strategy: str,
        ) -> CodexSelectionResult:
            assert leader_count == 1
            candidate_ids = tuple(str(item["lead_portfolio_id"]) for item in candidates)
            selector_calls.append((strategy, candidate_ids))
            assert leaders[3].lead_portfolio_id not in candidate_ids
            if strategy == "SHORT_TERM_WIN_RATE":
                assert leaders[1].lead_portfolio_id not in candidate_ids
                selected = (leaders[0].lead_portfolio_id,)
            else:
                assert strategy == "SHORT_TERM_INTRADAY"
                assert leaders[0].lead_portfolio_id not in candidate_ids
                assert leaders[2].lead_portfolio_id not in candidate_ids
                selected = (leaders[1].lead_portfolio_id,)
            return CodexSelectionResult(
                selected_leader_ids=selected,
                document={"selected_leader_ids": list(selected)},
                candidate_digest="b" * 64,
                report_digest="c" * 64,
            )

    monkeypatch.setattr(service, "BinancePublicCopyClient", Public)
    monkeypatch.setattr(service, "BinanceTestnetCatalogClient", Catalog)
    monkeypatch.setattr(service, "CopyTradingRepository", Repository)
    monkeypatch.setattr(service, "CodexDailySelector", Selector)
    database_file = tmp_path / "database-url"
    database_file.write_text("postgresql://local/test", encoding="utf-8")
    os.chmod(database_file, 0o400)
    config_file = tmp_path / "config.yaml"
    config_file.write_text("test: true\n", encoding="utf-8")
    schema_file = tmp_path / "schema.json"
    schema_file.write_text("{}", encoding="utf-8")

    evidence = service.run_selection(
        argparse.Namespace(
            database_url_file=database_file,
            repository_root=Path("/root/quantify/ai-quant-system"),
            config_file=config_file,
            schema_file=schema_file,
            work_root=tmp_path / "work",
            evidence_file=tmp_path / "evidence.json",
            leader_count=2,
            candidate_pool_size=20,
            review_pool_size=3,
            strategy="SHORT_TERM",
        )
    )

    assert Repository.selected == (
        leaders[0].lead_portfolio_id,
        leaders[1].lead_portfolio_id,
    )
    assert evidence["codex_decision"]["short_term_1"]["objective"] == ("HIGHEST_CREDIBLE_WIN_RATE")
    assert [call[0] for call in selector_calls] == [
        "SHORT_TERM_WIN_RATE",
        "SHORT_TERM_INTRADAY",
    ]


def test_locked_short_slot_keeps_incumbent_and_selects_advisory_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaders = tuple(_leader(index) for index in range(1, 5))
    locked_incumbent = leaders[0].lead_portfolio_id
    current_short_2 = leaders[1].lead_portfolio_id
    expected_backup = leaders[2].lead_portfolio_id

    class Public:
        def list_leaders(self, **kwargs: Any) -> LeaderPage:
            return LeaderPage(leaders=leaders, total=len(leaders))

        def order_history(self, leader_id: str, **kwargs: Any) -> OrderHistoryPage:
            orders = _quality_orders(leader_id)
            return OrderHistoryPage(orders=orders, total=len(orders))

    class Catalog:
        def trading_symbols(self) -> frozenset[str]:
            return frozenset({"ETHUSDT"})

    class Repository:
        selected: tuple[str, ...] = ()
        backups: dict[LeaderSlot, str] | None = None

        def __init__(self, dsn: str) -> None:
            assert dsn.startswith("postgresql://")

        def current_slot_assignments(self) -> dict[LeaderSlot, str]:
            return {
                LeaderSlot.SHORT_TERM_1: locked_incumbent,
                LeaderSlot.SHORT_TERM_2: current_short_2,
            }

        def current_locked_leader_ids(self) -> frozenset[str]:
            return frozenset({locked_incumbent})

        def recently_manually_cleared_leader_ids(self, **kwargs: Any) -> frozenset[str]:
            return frozenset()

        def leader_selection_trend(self, *args: Any, **kwargs: Any) -> None:
            return None

        def record_leader_snapshot(self, *args: Any, **kwargs: Any) -> None:
            pass

        def record_candidate_activity(self, *args: Any, **kwargs: Any) -> None:
            pass

        def apply_slot_selection(
            self,
            candidates: tuple[LeaderSnapshot, ...],
            assessments: dict[str, Any],
            selected_leader_ids: tuple[str, ...],
            **kwargs: Any,
        ) -> str:
            del candidates, assessments
            Repository.selected = selected_leader_ids
            Repository.backups = dict(kwargs["backup_leader_ids"])
            return "9" * 64

    selector_calls: list[tuple[str, tuple[str, ...]]] = []

    class Selector:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def select(
            self,
            candidates: tuple[dict[str, object], ...],
            *,
            leader_count: int,
            strategy: str,
        ) -> CodexSelectionResult:
            assert leader_count == 1
            candidate_ids = tuple(str(item["lead_portfolio_id"]) for item in candidates)
            assert locked_incumbent not in candidate_ids
            selector_calls.append((strategy, candidate_ids))
            selected = (
                expected_backup if strategy == "SHORT_TERM_WIN_RATE" else current_short_2
            )
            return CodexSelectionResult(
                selected_leader_ids=(selected,),
                document={"selected_leader_ids": [selected]},
                candidate_digest="7" * 64,
                report_digest="8" * 64,
            )

    monkeypatch.setattr(service, "BinancePublicCopyClient", Public)
    monkeypatch.setattr(service, "BinanceTestnetCatalogClient", Catalog)
    monkeypatch.setattr(service, "CopyTradingRepository", Repository)
    monkeypatch.setattr(service, "CodexDailySelector", Selector)
    database_file = tmp_path / "database-url"
    database_file.write_text("postgresql://local/test", encoding="utf-8")
    os.chmod(database_file, 0o400)
    config_file = tmp_path / "config.yaml"
    config_file.write_text("test: true\n", encoding="utf-8")
    schema_file = tmp_path / "schema.json"
    schema_file.write_text("{}", encoding="utf-8")

    evidence = service.run_selection(
        argparse.Namespace(
            database_url_file=database_file,
            repository_root=Path("/root/quantify/ai-quant-system"),
            config_file=config_file,
            schema_file=schema_file,
            work_root=tmp_path / "work",
            evidence_file=tmp_path / "evidence.json",
            leader_count=2,
            candidate_pool_size=20,
            review_pool_size=3,
            strategy="SHORT_TERM",
        )
    )

    assert Repository.selected == (locked_incumbent, current_short_2)
    assert Repository.backups == {LeaderSlot.SHORT_TERM_1: expected_backup}
    assert evidence["backup_leader_ids"] == {"SHORT_TERM_1": expected_backup}
    assert evidence["codex_decision"]["short_term_1"]["codex_review"]["state"] == (
        "LOCKED_RETAINED_WITH_BACKUP"
    )
    assert [call[0] for call in selector_calls] == [
        "SHORT_TERM_WIN_RATE",
        "SHORT_TERM_INTRADAY",
    ]


def test_short_pool_shortage_is_a_failure_and_wakes_codex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, Any]] = []
    wakeups: list[bool] = []

    class Repository:
        def __init__(self, dsn: str) -> None:
            assert dsn == "postgresql://local/test"

        def record_selection_failure(self, **kwargs: Any) -> None:
            recorded.append(kwargs)

    arguments = argparse.Namespace(
        database_url_file=tmp_path / "database-url",
        repository_root=tmp_path,
        strategy="SHORT_TERM",
    )
    monkeypatch.setattr(service, "_arguments", lambda: arguments)
    monkeypatch.setattr(service, "_private_text", lambda *args: "postgresql://local/test")
    monkeypatch.setattr(service, "CopyTradingRepository", Repository)
    monkeypatch.setattr(
        service,
        "run_selection",
        lambda args: (_ for _ in ()).throw(
            RuntimeError("COPY_SELECTION_SHORT_WIN_RATE_POOL_INSUFFICIENT")
        ),
    )
    monkeypatch.setattr(
        service,
        "_trigger_codex_audit",
        lambda: wakeups.append(True) or True,
    )

    with pytest.raises(
        RuntimeError,
        match="COPY_SELECTION_SHORT_WIN_RATE_POOL_INSUFFICIENT",
    ):
        service.main()
    assert len(recorded) == 1
    assert recorded[0]["strategy"].value == "SHORT_TERM"
    assert recorded[0]["reason_code"] == ("COPY_SELECTION_SHORT_WIN_RATE_POOL_INSUFFICIENT")
    assert wakeups == [True]


def test_unexpected_selection_failure_immediately_requests_codex_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict[str, Any]] = []
    wakeups: list[bool] = []

    class Repository:
        def __init__(self, dsn: str) -> None:
            assert dsn == "postgresql://local/test"

        def record_selection_failure(self, **kwargs: Any) -> None:
            recorded.append(kwargs)

    arguments = argparse.Namespace(
        database_url_file=tmp_path / "database-url",
        repository_root=tmp_path,
        strategy="SHORT_TERM",
    )
    monkeypatch.setattr(service, "_arguments", lambda: arguments)
    monkeypatch.setattr(service, "_private_text", lambda *args: "postgresql://local/test")
    monkeypatch.setattr(service, "CopyTradingRepository", Repository)
    monkeypatch.setattr(
        service,
        "run_selection",
        lambda args: (_ for _ in ()).throw(RuntimeError("COPY_SELECTION_UNEXPECTED")),
    )
    monkeypatch.setattr(
        service,
        "_trigger_codex_audit",
        lambda: wakeups.append(True) or True,
    )

    with pytest.raises(RuntimeError, match="COPY_SELECTION_UNEXPECTED"):
        service.main()
    assert len(recorded) == 1
    assert recorded[0]["reason_code"] == "COPY_SELECTION_UNEXPECTED"
    assert wakeups == [True]


def test_daily_selection_skips_one_inaccessible_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaders = tuple(_leader(index) for index in range(4))
    inaccessible = leaders[0].lead_portfolio_id

    class Public:
        def list_leaders(self, **kwargs: Any) -> LeaderPage:
            return LeaderPage(leaders=leaders, total=len(leaders))

        def order_history(self, leader_id: str, **kwargs: Any) -> OrderHistoryPage:
            if leader_id == inaccessible:
                raise BinancePublicCopyError("COPY_ORDER_HISTORY_ACCESS_DENIED")
            orders = _quality_orders(leader_id)
            return OrderHistoryPage(orders=orders, total=len(orders))

    class Catalog:
        def trading_symbols(self) -> frozenset[str]:
            return frozenset({"ETHUSDT"})

    class Repository:
        last: Repository | None = None

        def __init__(self, dsn: str) -> None:
            assert dsn.startswith("postgresql://")
            self.assessments: dict[str, Any] = {}
            Repository.last = self

        def record_leader_snapshot(self, *args: Any, **kwargs: Any) -> None:
            pass

        def current_slot_assignments(self) -> dict[Any, str]:
            return {}

        def current_locked_leader_ids(self) -> frozenset[str]:
            return frozenset()

        def recently_manually_cleared_leader_ids(self, **kwargs: Any) -> frozenset[str]:
            return frozenset()

        def leader_selection_trend(self, *args: Any, **kwargs: Any) -> None:
            return None

        def record_candidate_activity(self, *args: Any, **kwargs: Any) -> None:
            pass

        def apply_slot_selection(
            self,
            candidates: tuple[LeaderSnapshot, ...],
            assessments: dict[str, Any],
            selected_leader_ids: tuple[str, ...],
            **kwargs: Any,
        ) -> str:
            assert candidates == leaders
            self.assessments = assessments
            assert inaccessible not in selected_leader_ids
            scheduled = kwargs["scheduled_for"].astimezone(ZoneInfo("Asia/Shanghai"))
            assert scheduled.weekday() == 6
            assert (scheduled.hour, scheduled.minute, scheduled.second) == (0, 0, 0)
            return "f" * 64

    class Selector:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def select(
            self,
            candidates: tuple[dict[str, object], ...],
            *,
            leader_count: int,
            strategy: str,
        ) -> CodexSelectionResult:
            assert strategy == "LONG_TERM"
            selected = tuple(
                str(candidate["lead_portfolio_id"]) for candidate in candidates[:leader_count]
            )
            return CodexSelectionResult(
                selected_leader_ids=selected,
                document={"selected_leader_ids": list(selected)},
                candidate_digest="d" * 64,
                report_digest="e" * 64,
            )

    monkeypatch.setattr(service, "BinancePublicCopyClient", Public)
    monkeypatch.setattr(service, "BinanceTestnetCatalogClient", Catalog)
    monkeypatch.setattr(service, "CopyTradingRepository", Repository)
    monkeypatch.setattr(service, "CodexDailySelector", Selector)
    database_file = tmp_path / "database-url"
    database_file.write_text("postgresql://local/test", encoding="utf-8")
    os.chmod(database_file, 0o400)
    config_file = tmp_path / "config.yaml"
    config_file.write_text("test: true\n", encoding="utf-8")
    schema_file = tmp_path / "schema.json"
    schema_file.write_text("{}", encoding="utf-8")
    evidence_file = tmp_path / "evidence.json"

    evidence = service.run_selection(
        argparse.Namespace(
            database_url_file=database_file,
            repository_root=Path("/root/quantify/ai-quant-system"),
            config_file=config_file,
            schema_file=schema_file,
            work_root=tmp_path / "work",
            evidence_file=evidence_file,
            leader_count=1,
            candidate_pool_size=20,
            review_pool_size=3,
            strategy="LONG_TERM",
        )
    )

    assert evidence["selected_leader_ids"] == [leaders[1].lead_portfolio_id]
    assert Repository.last is not None
    assert Repository.last.assessments[inaccessible].reason_codes == (
        "COPY_SELECTION_HISTORY_UNAVAILABLE",
        "COPY_ORDER_HISTORY_ACCESS_DENIED",
    )


def test_codex_selection_rejects_schema_valid_outsider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outsider = "9999999999999999999"

    def fake_run(command: list[str], **kwargs: Any) -> None:
        assert command[command.index("--model") + 1] == "gpt-5.6-sol"
        assert command[command.index("--config") + 1] == 'model_reasoning_effort="high"'
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "selected_leader_ids": [outsider],
                    "decisions": [
                        {
                            "lead_portfolio_id": outsider,
                            "confidence": "HIGH",
                            "reasons": ["looks good"],
                            "concerns": [],
                        }
                    ],
                    "portfolio_summary": "test",
                    "risk_notes": ["testnet only"],
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    selector = CodexDailySelector(
        schema_path=Path("contracts/copy-leader-selection.schema.json"),
        work_root=tmp_path,
    )

    with pytest.raises(CodexSelectionError, match="NOT_ADMISSIBLE"):
        selector.select(
            ({"lead_portfolio_id": "5108371059752839168"},),
            leader_count=1,
        )


def test_short_selection_does_not_force_the_busiest_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quieter = "5108371059752839168"

    def fake_run(command: list[str], **kwargs: Any) -> None:
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "selected_leader_ids": [quieter],
                    "decisions": [
                        {
                            "lead_portfolio_id": quieter,
                            "confidence": "HIGH",
                            "reasons": ["lower drawdown"],
                            "concerns": [],
                        }
                    ],
                    "portfolio_summary": "test",
                    "risk_notes": ["testnet only"],
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    selector = CodexDailySelector(
        schema_path=Path("contracts/copy-leader-selection.schema.json"),
        work_root=tmp_path,
    )

    result = selector.select(
        (
            {
                "lead_portfolio_id": quieter,
                "recent_public_orders": {"orders_1d": 3},
            },
            {
                "lead_portfolio_id": "5109186975387420161",
                "recent_public_orders": {"orders_1d": 12},
            },
        ),
        leader_count=1,
        strategy="SHORT_TERM",
    )

    assert result.selected_leader_ids == (quieter,)


def test_short_win_rate_codex_strategy_uses_win_rate_objective_without_activity_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = "5108371059752839168"
    prompts: list[str] = []

    def fake_run(command: list[str], **kwargs: Any) -> None:
        prompts.append(command[-1])
        output_index = command.index("--output-last-message") + 1
        Path(command[output_index]).write_text(
            json.dumps(
                {
                    "schema_version": "1.0.0",
                    "selected_leader_ids": [selected],
                    "decisions": [
                        {
                            "lead_portfolio_id": selected,
                            "confidence": "HIGH",
                            "reasons": ["highest credible public win rate"],
                            "concerns": ["small recent sample"],
                        }
                    ],
                    "portfolio_summary": "win-rate-first selection",
                    "risk_notes": ["testnet only"],
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    selector = CodexDailySelector(
        schema_path=Path("contracts/copy-leader-selection.schema.json"),
        work_root=tmp_path,
    )

    result = selector.select(
        (
            {
                "lead_portfolio_id": selected,
                "win_rate_pct": "92",
                "recent_public_orders": {
                    "orders_1d": 0,
                    "orders_3d": 1,
                    "orders_7d": 1,
                },
            },
        ),
        leader_count=1,
        strategy="SHORT_TERM_WIN_RATE",
    )

    assert result.selected_leader_ids == (selected,)
    assert "highest credible win-rate" in prompts[0]
    assert "current_copy_count as bounded supporting social proof" in prompts[0]
