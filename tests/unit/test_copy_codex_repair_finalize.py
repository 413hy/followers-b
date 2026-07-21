from types import SimpleNamespace

from ai_quant.services import copy_codex_repair_finalize


def _document() -> dict[str, object]:
    return {"status": "REPAIRED", "follow_up_required": False}


def _facts(**overrides: object) -> SimpleNamespace:
    values = {
        "uncertain_signals": 0,
        "overdue_pending_entries": 0,
        "latest_poll_failures": 0,
        "history_gap_failures": 0,
        "pending_entry_allowances": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_safe_resume_requires_no_pending_or_recoverable_order(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        copy_codex_repair_finalize,
        "_latest_watchdog",
        lambda _database_url: ("HEALTHY", []),
    )
    monkeypatch.setattr(
        copy_codex_repair_finalize,
        "PostgresHealthStore",
        lambda _database_url: SimpleNamespace(
            read_facts=lambda: _facts(pending_entry_allowances={("ETHUSDT", "LONG"): 1})
        ),
    )
    monkeypatch.setattr(
        copy_codex_repair_finalize,
        "_has_recoverable_signals",
        lambda _database_url: False,
    )

    assert not copy_codex_repair_finalize._safe_to_resume("database", _document())


def test_safe_resume_accepts_verified_clean_repair(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        copy_codex_repair_finalize,
        "_latest_watchdog",
        lambda _database_url: ("HEALTHY", []),
    )
    monkeypatch.setattr(
        copy_codex_repair_finalize,
        "PostgresHealthStore",
        lambda _database_url: SimpleNamespace(read_facts=lambda: _facts()),
    )
    monkeypatch.setattr(
        copy_codex_repair_finalize,
        "_has_recoverable_signals",
        lambda _database_url: False,
    )

    assert copy_codex_repair_finalize._safe_to_resume("database", _document())
