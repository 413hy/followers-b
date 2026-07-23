from pathlib import Path


def test_business_and_host_control_migration_trees_are_independent() -> None:
    root = Path(__file__).resolve().parents[2]
    business = root / "migrations/business"
    host = root / "migrations/host_control"
    assert business.resolve() != host.resolve()
    assert (business / "alembic.ini").is_file()
    assert (host / "alembic.ini").is_file()
    assert list((business / "versions").glob("*.py"))
    assert list((host / "versions").glob("*.py"))


def test_market_data_migration_contains_required_evidence_tables() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0002_market_data.py").read_text()
    for table in (
        "market.raw_archive_objects",
        "market.remote_archive_receipts",
        "market.data_manifests",
        "market.data_quality_intervals",
    ):
        assert table in migration
    assert "create_hypertable" in migration
    assert "reject_append_only_mutation" in migration


def test_risk_execution_migration_keeps_decisions_and_intents_append_only() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0003_risk_execution.py").read_text()
    for table in (
        "trading.risk_decisions",
        "trading.risk_reservation_events",
        "trading.order_intents",
        "trading.account_snapshots",
        "trading.position_snapshots",
        "trading.protection_observations",
    ):
        assert table in migration
    assert "reject_append_only_mutation" in migration


def test_operations_migration_is_event_sourced_and_append_only() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0004_operations.py").read_text()
    for table in (
        "control.command_requests",
        "control.command_events",
        "control.flatten_challenges",
        "control.flatten_challenge_consumptions",
        "control.incident_events",
        "control.notification_deliveries",
        "control.backup_manifests",
    ):
        assert table in migration
    assert "reject_append_only_mutation" in migration


def test_copy_trading_migration_normalizes_leaders_and_keeps_events_append_only() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0005_copy_trading.py").read_text()
    for table in (
        "copytrading.leader_snapshots",
        "copytrading.selection_runs",
        "copytrading.leader_lifecycle_events",
        "copytrading.poll_events",
        "copytrading.source_order_events",
        "copytrading.source_fill_delta_events",
        "copytrading.signals",
        "copytrading.signal_decision_events",
        "copytrading.virtual_position_events",
        "copytrading.execution_links",
        "copytrading.submission_claims",
        "copytrading.submission_events",
        "copytrading.account_envelope_events",
    ):
        assert table in migration
    assert "is_baseline boolean NOT NULL" in migration
    assert "reject_append_only_mutation" in migration


def test_telegram_runtime_migration_keeps_commands_and_offsets_append_only() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0006_telegram_runtime.py").read_text()
    for table in (
        "copytrading.runtime_control_events",
        "copytrading.telegram_update_events",
        "copytrading.telegram_offset_events",
        "copytrading.telegram_control_challenges",
        "copytrading.telegram_challenge_consumptions",
    ):
        assert table in migration
    assert "reject_append_only_mutation" in migration


def test_copy_control_migration_supports_durable_reductions_and_health() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0007_copy_control_signals.py").read_text()
    assert "signal_origin" in migration
    assert "copytrading.health_check_runs" in migration
    assert "reject_append_only_mutation" in migration


def test_copy_submission_recovery_persists_original_sizing() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (
        root / "migrations/business/versions/0008_copy_submission_resilience.py"
    ).read_text()
    assert "requested_quantity" in migration
    assert "leverage" in migration
    assert "copy_submission_original_sizing_check" in migration


def test_copy_leader_slots_are_event_sourced_and_telegram_managed() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0009_copy_leader_slots.py").read_text()
    for table in (
        "copytrading.leader_slot_events",
        "copytrading.leader_activity_snapshots",
        "copytrading.telegram_leader_challenges",
        "copytrading.telegram_leader_consumptions",
    ):
        assert table in migration
    assert "LONG_TERM" in migration
    assert "SHORT_TERM_1" in migration
    assert "SHORT_TERM_2" in migration
    assert "reject_append_only_mutation" in migration


def test_copy_custom_slots_expand_every_durable_slot_constraint() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0022_copy_custom_leader_slots.py").read_text()

    assert 'revision = "0022_copy_custom_slots"' in migration
    assert 'down_revision = "0021_copy_max_leverage"' in migration
    assert "CUSTOM_1" in migration
    assert "CUSTOM_2" in migration
    for table in (
        "leader_slot_events",
        "telegram_leader_challenges",
        "leader_pnl_events",
        "line_valuation_events",
        "leader_pnl_slot_correction_events",
        "slot_replacement_events",
    ):
        assert table in migration


def test_copy_ten_slots_expand_every_durable_slot_constraint() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0031_ten_leader_slots.py").read_text()

    assert 'revision = "0031_ten_leader_slots"' in migration
    assert 'down_revision = "0030_account_summary_reset"' in migration
    assert "CUSTOM_3" in migration
    assert "CUSTOM_7" in migration
    for table in (
        "leader_slot_events",
        "telegram_leader_challenges",
        "leader_pnl_events",
        "line_valuation_events",
        "leader_pnl_slot_correction_events",
        "slot_replacement_events",
    ):
        assert table in migration


def test_copy_source_resolution_epochs_are_append_only() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (
        root / "migrations/business/versions/0023_copy_source_resolution_epochs.py"
    ).read_text()

    assert 'revision = "0023_copy_source_epochs"' in migration
    assert 'down_revision = "0022_copy_custom_slots"' in migration
    assert "copytrading.source_resolution_reset_events" in migration
    assert "reject_append_only_mutation" in migration


def test_leader_position_close_is_two_step_and_append_only() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0024_leader_position_close.py").read_text(
        encoding="utf-8"
    )

    assert 'revision = "0024_leader_position_close"' in migration
    assert 'down_revision = "0023_copy_source_epochs"' in migration
    assert "telegram_leader_position_close_challenges" in migration
    assert "telegram_leader_position_close_consumptions" in migration
    assert "target_digest char(64) NOT NULL" in migration
    assert "control.reject_append_only_mutation()" in migration


def test_leader_lock_is_durable_two_step_and_append_only() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0025_leader_lock.py").read_text(
        encoding="utf-8"
    )

    assert 'revision = "0025_leader_lock"' in migration
    assert 'down_revision = "0024_leader_position_close"' in migration
    for table in (
        "leader_lock_events",
        "telegram_leader_lock_challenges",
        "telegram_leader_lock_consumptions",
    ):
        assert table in migration
    assert "desired_state IN ('LOCKED','UNLOCKED')" in migration
    assert "control.reject_append_only_mutation()" in migration


def test_locked_slot_backup_is_durable_and_advisory() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0026_locked_slot_backup.py").read_text(
        encoding="utf-8"
    )

    assert 'revision = "0026_locked_slot_backup"' in migration
    assert 'down_revision = "0025_leader_lock"' in migration
    assert "leader_slot_backup_events" in migration
    assert "incumbent_lead_portfolio_id<>backup_lead_portfolio_id" in migration
    assert "control.reject_append_only_mutation()" in migration


def test_pnl_reset_is_an_append_only_presentation_baseline() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0027_pnl_reset_baseline.py").read_text(
        encoding="utf-8"
    )

    assert 'revision = "0027_pnl_reset_baseline"' in migration
    assert 'down_revision = "0026_locked_slot_backup"' in migration
    assert "copytrading.pnl_reset_events" in migration
    assert "copytrading.pnl_position_reset_anchors" in migration
    assert "control.reject_append_only_mutation()" in migration


def test_copy_poll_history_gap_has_an_explicit_database_state() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0010_copy_history_gap.py").read_text()
    assert 'revision = "0010_copy_history_gap"' in migration
    assert "HISTORY_GAP" in migration
    assert "poll_events_state_check" in migration


def test_copy_leader_multiplier_is_per_leader_event_sourced_and_confirmed() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0017_leader_follow_multiplier.py").read_text()
    for table in (
        "copytrading.leader_follow_multiplier_events",
        "copytrading.telegram_multiplier_challenges",
        "copytrading.telegram_multiplier_consumptions",
    ):
        assert table in migration
    assert "lead_portfolio_id" in migration
    assert "multiplier BETWEEN 1 AND 10" in migration
    assert "reject_append_only_mutation" in migration


def test_deferred_slot_replacement_migration_is_append_only() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (
        root / "migrations/business/versions/0018_deferred_slot_replacements.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "0018_deferred_slot_replacements"' in migration
    assert 'down_revision = "0017_leader_follow_multiplier"' in migration
    for required in (
        "copytrading.slot_replacement_events",
        "REQUESTED",
        "APPLIED",
        "EXPIRED",
        "SUPERSEDED",
        "copytrading_slot_replacement_events_append_only",
        "control.reject_append_only_mutation()",
    ):
        assert required in migration


def test_selective_position_close_migration_is_two_step_and_append_only() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0019_selective_position_close.py").read_text(
        encoding="utf-8"
    )
    assert 'revision = "0019_selective_position_close"' in migration
    assert 'down_revision = "0018_deferred_slot_replacements"' in migration
    assert "telegram_position_close_challenges" in migration
    assert "telegram_position_close_consumptions" in migration
    assert "nonce_hash char(64) NOT NULL UNIQUE" in migration
    assert "control.reject_append_only_mutation()" in migration


def test_copy_execution_environment_binding_is_singleton_and_append_only() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (
        root / "migrations/business/versions/0020_copy_execution_environment.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision = "0019_selective_position_close"' in migration
    assert "execution_environment_bindings" in migration
    assert "'TESTNET','PRODUCTION'" in migration
    assert "control.reject_append_only_mutation()" in migration


def test_copy_leverage_migration_expands_exchange_protocol_boundary() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (
        root / "migrations/business/versions/0021_copy_exchange_maximum_leverage.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "0021_copy_max_leverage"' in migration
    assert 'down_revision = "0020_copy_execution_environment"' in migration
    assert "leverage BETWEEN 1 AND 125" in migration
    assert "copy_submission_original_sizing_check" in migration
    assert "virtual_position_events_leverage_check" in migration


def test_business_migration_revision_ids_fit_alembic_version_column() -> None:
    root = Path(__file__).resolve().parents[2]
    for path in (root / "migrations/business/versions").glob("*.py"):
        migration = path.read_text(encoding="utf-8")
        revision_line = next(
            (line for line in migration.splitlines() if line.startswith("revision = ")),
            None,
        )
        assert revision_line is not None, path.name
        revision = revision_line.split('"', maxsplit=2)[1]
        assert len(revision) <= 32, f"{path.name}: {revision}"


def test_persistent_protected_entry_migration_allows_gtc_claims() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (
        root / "migrations/business/versions/0028_persistent_protected_entries.py"
    ).read_text(encoding="utf-8")

    assert "(order_type='LIMIT' AND limit_price>0)" in migration
    assert 'down_revision = "0027_pnl_reset_baseline"' in migration
    assert "submission_policy_upgrade_events" in migration
    assert "reject_append_only_mutation" in migration


def test_entry_margin_limit_is_bounded_two_step_and_append_only() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0029_configurable_entry_margin.py").read_text(
        encoding="utf-8"
    )

    assert 'revision = "0029_entry_margin_limit"' in migration
    assert 'down_revision = "0028_persistent_entries"' in migration
    for table in (
        "entry_margin_limit_events",
        "telegram_entry_margin_challenges",
        "telegram_entry_margin_consumptions",
    ):
        assert table in migration
    assert "limit_usdt BETWEEN 5 AND 120" in migration
    assert "control.reject_append_only_mutation()" in migration


def test_account_summary_reset_extends_the_existing_two_step_challenge() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0030_account_summary_reset.py").read_text(
        encoding="utf-8"
    )

    assert 'revision = "0030_account_summary_reset"' in migration
    assert 'down_revision = "0029_entry_margin_limit"' in migration
    assert "reset_summary" in migration
    assert "telegram_control_challenges_action_check" in migration


def test_copy_pnl_migration_has_account_and_per_leader_append_only_ledgers() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0011_copy_account_valuations.py").read_text()
    for table in (
        "copytrading.account_valuation_events",
        "copytrading.account_position_mark_events",
        "copytrading.leader_pnl_events",
        "copytrading.leader_valuation_events",
    ):
        assert table in migration
    assert "realized_pnl_delta_usdt" in migration
    assert "mark_complete" in migration
    assert "reject_append_only_mutation" in migration


def test_copy_line_pnl_survives_leader_rotation() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0012_copy_line_pnl.py").read_text()
    assert 'revision = "0012_copy_line_pnl"' in migration
    assert "copytrading.line_valuation_events" in migration
    assert "copy_leader_pnl_slot_check" in migration
    assert "LONG_TERM" in migration
    assert "SHORT_TERM_1" in migration
    assert "SHORT_TERM_2" in migration
    assert "reject_append_only_mutation" in migration


def test_copy_protected_entries_persist_price_expiry_and_cancellation() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0013_protected_entry_orders.py").read_text()
    assert 'revision = "0013_protected_entry_orders"' in migration
    assert 'down_revision = "0012_copy_line_pnl"' in migration
    assert "order_type" in migration
    assert "limit_price" in migration
    assert "expires_at" in migration
    assert "CANCELLED" in migration


def test_codex_repair_outcomes_are_durable_health_evidence() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0015_codex_repair.py").read_text()
    assert 'revision = "0015_codex_repair"' in migration
    assert 'down_revision = "0014_submission_hash_v2"' in migration
    assert "CODEX_REPAIR" in migration


def test_pnl_slot_corrections_preserve_original_events_and_are_append_only() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "migrations/business/versions/0016_pnl_slot_corrections.py").read_text()
    assert 'revision = "0016_pnl_slot_corrections"' in migration
    assert 'down_revision = "0015_codex_repair"' in migration
    assert "leader_pnl_slot_correction_events" in migration
    assert "pnl_event_id char(64) NOT NULL UNIQUE" in migration
    assert "reject_append_only_mutation" in migration
