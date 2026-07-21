from __future__ import annotations

import json
import socket
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_quant.binance_egress.measurement_cycle import (
    load_bootstrap_traces,
    load_measurement_cycle_plan,
    probe_unix_service,
)
from ai_quant.rate_budget.authorization import AuthorizationDenied
from ai_quant.rate_budget.postgres import host_measurement_database_dsn

NOW = datetime(2026, 7, 14, tzinfo=UTC)


def _root_file(path: Path, content: str = "{}") -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o444)


def test_cycle_plan_is_closed_and_has_no_command_or_dsn(tmp_path: Path) -> None:
    fields = {
        "local_facts_plan_file",
        "network_policy_file",
        "connection_contract_file",
        "endpoint_catalog_file",
        "keyring_file",
        "keyring_hash_file",
        "bootstrap_traces_file",
    }
    values: dict[str, str] = {}
    for field in fields:
        path = tmp_path / f"{field}.json"
        _root_file(path)
        values[field] = str(path)
    for field in (
        "database_password_file",
        "network_policy_schema_file",
        "connection_contract_schema_file",
        "connection_source_artifact_root",
        "endpoint_catalog_schema_file",
        "endpoint_source_artifact_root",
        "gateway_request_schema_file",
        "keyring_schema_file",
    ):
        values[field] = str(tmp_path / field)
    plan = {"schema_version": "1.0.0", **values}
    plan_path = tmp_path / "measurement-cycle-plan.json"
    _root_file(plan_path, json.dumps(plan))
    loaded = load_measurement_cycle_plan(
        plan_path,
        trusted_plan_directory=tmp_path,
    )
    assert loaded.database_password_file == tmp_path / "database_password_file"

    plan["command"] = ["/bin/sh"]
    plan_path.chmod(0o644)
    _root_file(plan_path, json.dumps(plan))
    with pytest.raises(AuthorizationDenied, match="MEASUREMENT_CYCLE_PLAN_INVALID"):
        load_measurement_cycle_plan(plan_path, trusted_plan_directory=tmp_path)


def test_bootstrap_source_must_be_fresh_and_root_owned(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.json"
    trace = {"trace": "placeholder"}
    document = {
        "schema_version": "1.0.0",
        "captured_at": "2026-07-14T00:00:00Z",
        "traces": [trace, {"trace": "other"}],
    }
    _root_file(path, json.dumps(document))
    assert len(load_bootstrap_traces(path, trusted_directory=tmp_path, now=NOW)) == 2
    with pytest.raises(AuthorizationDenied, match="BOOTSTRAP_SOURCE_INVALID"):
        load_bootstrap_traces(
            path,
            trusted_directory=tmp_path,
            now=NOW + timedelta(minutes=6),
        )


def test_unix_probe_binds_filesystem_and_kernel_peer(tmp_path: Path) -> None:
    path = tmp_path / "service.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o660)
    listener.listen(1)
    accepted = threading.Event()

    def serve() -> None:
        peer, _ = listener.accept()
        peer.close()
        accepted.set()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert probe_unix_service(
            path,
            expected_owner_uid=0,
            expected_owner_gid=0,
            expected_peer_uid=0,
            expected_peer_gid=0,
        )
        assert accepted.wait(1)
    finally:
        listener.close()
        thread.join(timeout=1)


def test_host_measurement_database_target_is_fixed_unix_socket() -> None:
    dsn = host_measurement_database_dsn("temporary-test-password")
    assert "host=/run/ai-quant-host-postgres" in dsn
    assert "dbname=aiq_host_rate_control" in dsn
    assert "user=aiq_rate_authority" in dsn
