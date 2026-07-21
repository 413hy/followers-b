from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import bootstrap_host

ROOT = Path(__file__).resolve().parents[2]


def _generate_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    operator = tmp_path / "operator"
    subprocess.run(  # noqa: S603
        ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(operator)],
        check=True,
    )
    approval_private = tmp_path / "approval.pem"
    approval_public = tmp_path / "approval.pub.pem"
    subprocess.run(  # noqa: S603
        [
            "/usr/bin/openssl",
            "genpkey",
            "-algorithm",
            "ED25519",
            "-out",
            str(approval_private),
        ],
        check=True,
    )
    subprocess.run(  # noqa: S603
        [
            "/usr/bin/openssl",
            "pkey",
            "-in",
            str(approval_private),
            "-pubout",
            "-out",
            str(approval_public),
        ],
        check=True,
    )
    head = subprocess.run(
        ["/usr/bin/git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    backup = tmp_path / "backup.json"
    backup.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "provider": "oci-boot-volume-backup",
                "artifact_id": "test-only",
                "created_at": dt.datetime.now(dt.UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
                "repository_head": head,
            }
        ),
        encoding="utf-8",
    )
    return operator.with_suffix(".pub"), approval_private, approval_public, backup


@pytest.mark.security
def test_bootstrap_plan_is_read_only_and_binds_exact_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator_public, approval_private, approval_public, backup = _generate_inputs(tmp_path)
    output = tmp_path / "plan.json"
    monkeypatch.setenv("SSH_CONNECTION", "198.51.100.164 11160 10.0.0.70 22")
    args = bootstrap_host.parser().parse_args(
        [
            "plan",
            "--toolchain-lock",
            str(ROOT / "deploy/host-toolchain.lock.yaml"),
            "--hardening-dir",
            str(ROOT / "deploy/host-hardening"),
            "--ssh-port",
            "22",
            "--ssh-source-cidr",
            "198.51.100.164/32",
            "--operator-public-key",
            str(operator_public),
            "--approval-public-key",
            str(approval_public),
            "--off-host-backup-evidence",
            str(backup),
            "--recovery-console-confirmed",
            "--output",
            str(output),
        ]
    )
    assert args.handler(args) == 0
    plan = json.loads(output.read_text(encoding="utf-8"))
    assert plan["ssh"] == {
        "observed_source_ip": "198.51.100.164",
        "port": 22,
        "recovery_console_confirmed": True,
        "source_cidr": "198.51.100.164/32",
    }
    assert plan["docker"]["target_data_root"] == "/srv/ai-quant/docker"
    assert plan["operator"]["public_key_sha256"] == bootstrap_host.sha256(operator_public)
    assert oct(os.stat(output).st_mode & 0o777) == "0o444"
    approval = tmp_path / "approval.json"
    subprocess.run(  # noqa: S603
        [
            str(ROOT / "scripts/create_bootstrap_approval.py"),
            "--plan",
            str(output),
            "--private-key",
            str(approval_private),
            "--approver",
            "test-owner",
            "--output",
            str(approval),
        ],
        check=True,
    )
    bootstrap_host.verify_approval(output, plan, approval)


@pytest.mark.security
def test_bootstrap_rejects_broad_ssh_source_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator_public, _approval_private, approval_public, backup = _generate_inputs(tmp_path)
    monkeypatch.setenv("SSH_CONNECTION", "198.51.100.164 11160 10.0.0.70 22")
    args = bootstrap_host.parser().parse_args(
        [
            "plan",
            "--toolchain-lock",
            str(ROOT / "deploy/host-toolchain.lock.yaml"),
            "--hardening-dir",
            str(ROOT / "deploy/host-hardening"),
            "--ssh-port",
            "22",
            "--ssh-source-cidr",
            "171.221.123.0/24",
            "--operator-public-key",
            str(operator_public),
            "--approval-public-key",
            str(approval_public),
            "--off-host-backup-evidence",
            str(backup),
            "--recovery-console-confirmed",
            "--output",
            str(tmp_path / "plan.json"),
        ]
    )
    with pytest.raises(SystemExit, match="bind exactly one fixed address"):
        args.handler(args)


@pytest.mark.security
def test_quantctl_unsupported_commands_fail_closed() -> None:
    result = subprocess.run(  # noqa: S603
        [str(ROOT / "deploy/quantctl/quantctl.py"), "status"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "invalid choice" in result.stderr
