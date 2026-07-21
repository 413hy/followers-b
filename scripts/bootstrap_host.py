#!/usr/bin/env python3
"""Auditable Debian 12 host bootstrap plan/apply/prove/verify implementation."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import ipaddress
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, NoReturn

SCHEMA_VERSION = "1.0.0"
EXPECTED_PLATFORM = {
    "architecture": "arm64",
    "distribution": "debian",
    "release": "12",
    "codename": "bookworm",
}
HARDENING_FILES = {
    "ai-quant-egress.example.json",
    "chrony/ai-quant.sources",
    "docker/daemon.json",
    "journald/99-ai-quant.conf",
    "limits/99-ai-quant.conf",
    "nftables/ai-quant-host-input.nft.template",
    "sshd/99-ai-quant.conf.template",
    "sysctl/99-ai-quant.conf",
    "systemd/aiq-host-input-firewall.service",
}


def fail(message: str, code: int = 1) -> NoReturn:
    raise SystemExit(f"bootstrap-host FAIL: {message}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def load_json(path: Path) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                fail(f"duplicate JSON key in {path}: {key}")
            result[key] = value
        return result

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=reject_duplicates)
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read strict JSON {path}: {exc}")


def run(
    command: list[str], *, input_bytes: bytes | None = None, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(  # noqa: S603
        command,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        fail(f"command failed ({' '.join(command)}): {stderr}")
    return result


def require_regular(path: Path, *, owner_uid: int | None = None) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        fail(f"required file missing: {path}")
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        fail(f"required file must be regular and not a symlink: {path}")
    if owner_uid is not None and metadata.st_uid != owner_uid:
        fail(f"unexpected owner for {path}")
    return metadata


def parse_time(value: str) -> dt.datetime:
    if not value.endswith("Z"):
        fail("approval timestamps must use UTC Z")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        fail(f"invalid approval timestamp: {exc}")
    return parsed


def inspect_operator_key(path: Path) -> tuple[str, str]:
    metadata = require_regular(path)
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        fail("operator public key is group/world writable")
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != 1:
        fail("operator public key must contain exactly one key")
    fields = lines[0].split()
    if len(fields) < 2 or fields[0] not in {"ssh-ed25519", "sk-ssh-ed25519@openssh.com"}:
        fail("operator key must be Ed25519 or hardware-backed Ed25519")
    fingerprint = run(["ssh-keygen", "-lf", str(path), "-E", "sha256"]).stdout.decode().split()[1]
    return lines[0], fingerprint


def inspect_approval_key(path: Path) -> str:
    metadata = require_regular(path)
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        fail("approval public key is group/world writable")
    result = run(["openssl", "pkey", "-pubin", "-in", str(path), "-text", "-noout"])
    if b"ED25519" not in result.stdout.upper():
        fail("approval key must be an Ed25519 PEM public key")
    return sha256(path)


def validate_lock(path: Path, repository: Path) -> dict[str, Any]:
    document = load_json(path)
    required = {
        "schema_version",
        "platform",
        "apt_sources",
        "packages",
        "artifacts",
        "required_commands",
    }
    if not isinstance(document, dict) or set(document) != required:
        fail("toolchain lock is not a closed object")
    if document["schema_version"] != SCHEMA_VERSION or document["platform"] != EXPECTED_PLATFORM:
        fail("toolchain lock platform/schema mismatch")
    sources = document["apt_sources"]
    if not isinstance(sources, list) or len(sources) != 2:
        fail("exactly Debian snapshot and Docker apt sources are required")
    source_ids = {item.get("id") for item in sources if isinstance(item, dict)}
    if source_ids != {"debian-bookworm-snapshot", "docker-debian-bookworm"}:
        fail("unexpected apt source set")
    packages = document["packages"]
    if not isinstance(packages, list) or not packages:
        fail("package lock is empty")
    names: set[str] = set()
    for package in packages:
        keys = {"name", "version", "architecture", "source", "filename", "sha256"}
        if not isinstance(package, dict) or set(package) != keys:
            fail("package entry is not closed")
        name = package["name"]
        if not isinstance(name, str) or name in names:
            fail("duplicate or invalid package name")
        names.add(name)
        if package["source"] not in source_ids:
            fail(f"unknown source for package {name}")
        if not re.fullmatch(r"[0-9a-f]{64}", package["sha256"]):
            fail(f"invalid package digest for {name}")
        result = run(["apt-cache", "show", "--no-all-versions", f"{name}={package['version']}"])
        fields: dict[str, str] = {}
        for line in result.stdout.decode().splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                if key in {"Architecture", "Filename", "SHA256"}:
                    fields[key] = value
        expected_arch = package["architecture"]
        if fields.get("Architecture") != expected_arch:
            fail(f"apt architecture mismatch for {name}")
        if (
            fields.get("Filename") != package["filename"]
            or fields.get("SHA256") != package["sha256"]
        ):
            fail(f"apt metadata mismatch for {name}")
    artifacts = document["artifacts"]
    if not isinstance(artifacts, list) or {item.get("name") for item in artifacts} != {
        "cosign",
        "quantctl",
    }:
        fail("controlled cosign and quantctl artifacts are required")
    for artifact in artifacts:
        if not re.fullmatch(r"[0-9a-f]{64}", artifact.get("sha256", "")):
            fail(f"invalid artifact digest for {artifact.get('name')}")
        if artifact["name"] == "quantctl":
            candidate = (repository / artifact["path"]).resolve()
            if (
                not candidate.is_relative_to(repository.resolve())
                or sha256(candidate) != artifact["sha256"]
            ):
                fail("quantctl artifact does not match its lock")
    return document


def validate_hardening(directory: Path) -> tuple[str, dict[str, str]]:
    manifest = directory / "manifest.sha256"
    require_regular(manifest)
    entries: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        fields = line.split("  ", 1)
        if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{64}", fields[0]):
            fail("invalid hardening manifest line")
        relative = fields[1]
        if relative in entries:
            fail("duplicate hardening manifest path")
        entries[relative] = fields[0]
    if set(entries) != HARDENING_FILES:
        fail(f"hardening manifest coverage mismatch: {sorted(set(entries) ^ HARDENING_FILES)}")
    root = directory.resolve()
    for relative, expected in entries.items():
        candidate = (directory / relative).resolve()
        if not candidate.is_relative_to(root):
            fail("hardening manifest path escapes root")
        require_regular(candidate)
        if sha256(candidate) != expected:
            fail(f"hardening hash mismatch: {relative}")
    return sha256(manifest), entries


def read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip('"')
    return values


def validate_host() -> dict[str, Any]:
    os_release = read_os_release()
    architecture = run(["dpkg", "--print-architecture"]).stdout.decode().strip()
    if (
        os_release.get("ID") != "debian"
        or os_release.get("VERSION_ID") != "12"
        or os_release.get("VERSION_CODENAME") != "bookworm"
        or architecture != "arm64"
    ):
        fail("host is not Debian 12 bookworm arm64")
    asset_tag = Path("/sys/class/dmi/id/chassis_asset_tag").read_text(encoding="utf-8").strip()
    if asset_tag != "OracleCloud.com":
        fail("host is not an Oracle Cloud instance")
    return {
        "architecture": architecture,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip(),
        "kernel": run(["uname", "-r"]).stdout.decode().strip(),
        "os_release_sha256": sha256(Path("/etc/os-release")),
        "provider": "OracleCloud.com",
    }


def render_templates(hardening: Path, ssh_port: int, source_cidr: str) -> dict[str, str]:
    rendered: dict[str, str] = {}
    replacements = {"@SSH_PORT@": str(ssh_port), "@SSH_SOURCE_CIDR@": source_cidr}
    for relative in (
        "sshd/99-ai-quant.conf.template",
        "nftables/ai-quant-host-input.nft.template",
    ):
        text = (hardening / relative).read_text(encoding="utf-8")
        for token, value in replacements.items():
            text = text.replace(token, value)
        if "@SSH_" in text:
            fail(f"unrendered token remains in {relative}")
        rendered[relative] = text
    run(
        ["nft", "--check", "-f", "-"],
        input_bytes=rendered["nftables/ai-quant-host-input.nft.template"].encode(),
    )
    with tempfile.NamedTemporaryFile("w", encoding="utf-8") as ssh_config:
        ssh_config.write("HostKey /etc/ssh/ssh_host_ed25519_key\n")
        ssh_config.write(rendered["sshd/99-ai-quant.conf.template"])
        ssh_config.flush()
        run(["sshd", "-t", "-f", ssh_config.name])
    run(
        [
            "dockerd",
            "--validate",
            "--config-file",
            str(hardening / "docker/daemon.json"),
        ]
    )
    return {key: hashlib.sha256(value.encode()).hexdigest() for key, value in rendered.items()}


def current_ssh_source() -> str:
    connection = os.environ.get("SSH_CONNECTION", "").split()
    if len(connection) != 4:
        fail("plan must run from an SSH session with SSH_CONNECTION")
    return connection[0]


def command_plan(args: argparse.Namespace) -> int:
    repository = Path(__file__).resolve().parents[1]
    lock = args.toolchain_lock.resolve()
    hardening = args.hardening_dir.resolve()
    operator_key = args.operator_public_key.resolve()
    approval_key = args.approval_public_key.resolve()
    backup_evidence = args.off_host_backup_evidence.resolve()
    for path in (lock, operator_key, approval_key, backup_evidence):
        require_regular(path)
    require_regular(hardening / "manifest.sha256")
    network = ipaddress.ip_network(args.ssh_source_cidr, strict=True)
    if network.prefixlen != network.max_prefixlen:
        fail("SSH source CIDR must bind exactly one fixed address")
    source_ip = ipaddress.ip_address(current_ssh_source())
    if source_ip not in network:
        fail("current SSH source is outside the approved CIDR")
    if not 1 <= args.ssh_port <= 65535:
        fail("invalid SSH port")
    lock_document = validate_lock(lock, repository)
    hardening_manifest_sha, _ = validate_hardening(hardening)
    rendered_hashes = render_templates(hardening, args.ssh_port, str(network))
    _, operator_fingerprint = inspect_operator_key(operator_key)
    approval_key_sha = inspect_approval_key(approval_key)
    host = validate_host()
    docker_root = run(["docker", "info", "--format", "{{.DockerRootDir}}"]).stdout.decode().strip()
    backup = load_json(backup_evidence)
    if not isinstance(backup, dict) or backup.get("schema_version") != SCHEMA_VERSION:
        fail("off-host backup evidence schema mismatch")
    required_backup = {"schema_version", "provider", "artifact_id", "created_at", "repository_head"}
    if set(backup) != required_backup or not all(
        isinstance(backup[key], str) for key in required_backup
    ):
        fail("off-host backup evidence is not a closed object")
    if backup["provider"] not in {
        "oci-boot-volume-backup",
        "encrypted-off-host-archive",
        "git-remote",
    }:
        fail("off-host backup provider is not approved")
    if not backup["artifact_id"].strip():
        fail("off-host backup artifact ID is empty")
    backup_created = parse_time(backup["created_at"])
    now = dt.datetime.now(dt.UTC)
    if not now - dt.timedelta(hours=24) <= backup_created <= now + dt.timedelta(minutes=1):
        fail("off-host backup evidence is stale or future-dated")
    head = run(["git", "rev-parse", "HEAD"]).stdout.decode().strip()
    if backup["repository_head"] != head:
        fail("off-host backup does not bind the current repository head")
    plan = {
        "schema_version": SCHEMA_VERSION,
        "created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "repository_head": head,
        "toolchain_lock": {"path": str(lock), "sha256": sha256(lock)},
        "hardening": {
            "directory": str(hardening),
            "manifest_sha256": hardening_manifest_sha,
            "rendered_sha256": rendered_hashes,
        },
        "operator": {
            "public_key_path": str(operator_key),
            "public_key_sha256": sha256(operator_key),
            "ssh_fingerprint": operator_fingerprint,
        },
        "approval": {
            "public_key_path": str(approval_key),
            "public_key_sha256": approval_key_sha,
        },
        "off_host_backup": {"path": str(backup_evidence), "sha256": sha256(backup_evidence)},
        "host": host,
        "ssh": {
            "port": args.ssh_port,
            "source_cidr": str(network),
            "observed_source_ip": str(source_ip),
            "recovery_console_confirmed": args.recovery_console_confirmed,
        },
        "docker": {
            "current_data_root": docker_root,
            "target_data_root": "/srv/ai-quant/docker",
            "copy_migration_required": docker_root != "/srv/ai-quant/docker",
        },
        "packages": [
            {"name": item["name"], "version": item["version"], "sha256": item["sha256"]}
            for item in lock_document["packages"]
        ],
        "actions": [
            "install-exact-toolchain",
            "install-controlled-cosign-and-quantctl",
            "create-aiqops-and-aiqsvc",
            "create-least-privilege-directories",
            "copy-preserving-docker-data-root-migration",
            "apply-docker-journald-chrony-sysctl-limits",
            "require-second-aiqops-ssh-session",
            "apply-source-bound-key-only-sshd",
            "apply-default-drop-host-input-nftables",
            "capture-redacted-verification-evidence",
        ],
    }
    if not args.recovery_console_confirmed:
        fail("Oracle Cloud recovery console confirmation is required")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_bytes(plan) + b"\n")
    os.chmod(args.output, 0o444)
    print(f"bootstrap plan PASS sha256={sha256(args.output)} output={args.output}")
    return 0


def validate_plan(path: Path) -> dict[str, Any]:
    plan = load_json(path)
    required = {
        "schema_version",
        "created_at",
        "repository_head",
        "toolchain_lock",
        "hardening",
        "operator",
        "approval",
        "off_host_backup",
        "host",
        "ssh",
        "docker",
        "packages",
        "actions",
    }
    if (
        not isinstance(plan, dict)
        or set(plan) != required
        or plan["schema_version"] != SCHEMA_VERSION
    ):
        fail("bootstrap plan is not a closed supported object")
    for section, key in (
        ("toolchain_lock", "path"),
        ("hardening", "directory"),
        ("operator", "public_key_path"),
        ("approval", "public_key_path"),
        ("off_host_backup", "path"),
    ):
        candidate = Path(plan[section][key])
        expected = plan[section].get("sha256") or plan[section].get("manifest_sha256")
        if section == "hardening":
            candidate = candidate / "manifest.sha256"
        if sha256(candidate) != expected:
            fail(f"plan-bound input changed: {section}")
    return plan


def verify_approval(plan_path: Path, plan: dict[str, Any], approval_path: Path) -> None:
    approval = load_json(approval_path)
    if not isinstance(approval, dict) or set(approval) != {"content", "signature"}:
        fail("approval envelope is not closed")
    content = approval["content"]
    required = {"schema_version", "action", "plan_sha256", "approved_at", "expires_at", "approver"}
    if not isinstance(content, dict) or set(content) != required:
        fail("approval content is not closed")
    if (
        content["schema_version"] != SCHEMA_VERSION
        or content["action"] != "bootstrap-apply"
        or content["plan_sha256"] != sha256(plan_path)
    ):
        fail("approval is not bound to this bootstrap plan")
    now = dt.datetime.now(dt.UTC)
    approved_at = parse_time(content["approved_at"])
    expires_at = parse_time(content["expires_at"])
    if approved_at > now + dt.timedelta(minutes=1) or not (
        now < expires_at <= approved_at + dt.timedelta(hours=1)
    ):
        fail("approval is not currently valid or exceeds one hour")
    try:
        signature = base64.b64decode(approval["signature"], validate=True)
    except (ValueError, TypeError) as exc:
        fail(f"approval signature is not canonical base64: {exc}")
    approval_key = Path(plan["approval"]["public_key_path"])
    if sha256(approval_key) != plan["approval"]["public_key_sha256"]:
        fail("approval public key changed")
    with tempfile.NamedTemporaryFile() as message, tempfile.NamedTemporaryFile() as signature_file:
        message.write(canonical_bytes(content))
        message.flush()
        signature_file.write(signature)
        signature_file.flush()
        run(
            [
                "openssl",
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(approval_key),
                "-rawin",
                "-in",
                message.name,
                "-sigfile",
                signature_file.name,
            ]
        )


def install_file(source: Path, destination: Path, mode: int) -> None:
    require_regular(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    shutil.copyfile(source, temporary)
    os.chown(temporary, 0, 0)
    os.chmod(temporary, mode)
    os.replace(temporary, destination)


def ensure_users_and_directories(plan: dict[str, Any]) -> None:
    if run(["getent", "group", "aiqsvc"], check=False).returncode != 0:
        run(["groupadd", "--system", "aiqsvc"])
    if run(["getent", "passwd", "aiqsvc"], check=False).returncode != 0:
        run(
            [
                "useradd",
                "--system",
                "--gid",
                "aiqsvc",
                "--home-dir",
                "/nonexistent",
                "--shell",
                "/usr/sbin/nologin",
                "aiqsvc",
            ]
        )
    if run(["getent", "group", "aiqops"], check=False).returncode != 0:
        run(["groupadd", "aiqops"])
    if run(["getent", "passwd", "aiqops"], check=False).returncode != 0:
        run(["useradd", "--create-home", "--gid", "aiqops", "--shell", "/bin/bash", "aiqops"])
    operator_line, _ = inspect_operator_key(Path(plan["operator"]["public_key_path"]))
    home = Path("/home/aiqops")
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    authorized = ssh_dir / "authorized_keys"
    authorized.write_text(operator_line + "\n", encoding="utf-8")
    uid = pwd.getpwnam("aiqops").pw_uid
    gid = pwd.getpwnam("aiqops").pw_gid
    for path, mode in ((home, 0o750), (ssh_dir, 0o700), (authorized, 0o600)):
        os.chown(path, uid, gid)
        os.chmod(path, mode)
    directories = (
        (Path("/srv/ai-quant"), 0, 0, 0o755),
        (Path("/srv/ai-quant/docker"), 0, 0, 0o711),
        (Path("/etc/ai-quant"), 0, 0, 0o755),
        (Path("/etc/ai-quant/trust"), 0, 0, 0o755),
        (Path("/var/log/ai-quant"), uid, gid, 0o750),
        (Path("/var/lib/ai-quant/evidence"), uid, gid, 0o750),
    )
    for path, owner, group, mode in directories:
        path.mkdir(parents=True, exist_ok=True)
        os.chown(path, owner, group)
        os.chmod(path, mode)


def install_toolchain(plan: dict[str, Any]) -> None:
    lock = load_json(Path(plan["toolchain_lock"]["path"]))
    package_specs = [f"{item['name']}={item['version']}" for item in lock["packages"]]
    run(["apt-get", "install", "--yes", "--no-install-recommends", *package_specs])
    repository = Path(__file__).resolve().parents[1]
    for artifact in lock["artifacts"]:
        destination = Path(artifact["install_path"])
        if artifact["name"] == "quantctl":
            source = repository / artifact["path"]
        else:
            with tempfile.NamedTemporaryFile(delete=False) as handle:
                temporary = Path(handle.name)
            try:
                run(
                    [
                        "curl",
                        "--fail",
                        "--location",
                        "--silent",
                        "--show-error",
                        artifact["url"],
                        "--output",
                        str(temporary),
                    ]
                )
                if sha256(temporary) != artifact["sha256"]:
                    fail(f"downloaded artifact digest mismatch: {artifact['name']}")
                install_file(temporary, destination, int(artifact["mode"], 8))
            finally:
                temporary.unlink(missing_ok=True)
            continue
        if sha256(source) != artifact["sha256"]:
            fail(f"local artifact digest mismatch: {artifact['name']}")
        install_file(source, destination, int(artifact["mode"], 8))


def command_prove_ssh(args: argparse.Namespace) -> int:
    if os.geteuid() == 0:
        fail("prove-ssh must run as aiqops in the independent SSH session")
    account = pwd.getpwuid(os.geteuid())
    if account.pw_name != "aiqops":
        fail("prove-ssh must run as aiqops")
    plan = validate_plan(args.plan)
    connection = os.environ.get("SSH_CONNECTION", "").split()
    if len(connection) != 4:
        fail("SSH_CONNECTION is absent")
    source = ipaddress.ip_address(connection[0])
    if source not in ipaddress.ip_network(plan["ssh"]["source_cidr"]):
        fail("proof session source is outside approved CIDR")
    if int(connection[3]) != plan["ssh"]["port"]:
        fail("proof session used the wrong SSH port")
    proof = {
        "schema_version": SCHEMA_VERSION,
        "plan_sha256": sha256(args.plan),
        "created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "login_user": "aiqops",
        "source_ip": str(source),
        "server_port": int(connection[3]),
        "operator_key_fingerprint": plan["operator"]["ssh_fingerprint"],
    }
    args.output.write_bytes(canonical_bytes(proof) + b"\n")
    os.chmod(args.output, 0o444)
    print(f"second SSH session proof PASS output={args.output}")
    return 0


def validate_ssh_proof(path: Path, plan_path: Path, plan: dict[str, Any]) -> None:
    account = pwd.getpwnam("aiqops")
    metadata = require_regular(path, owner_uid=account.pw_uid)
    if stat.S_IMODE(metadata.st_mode) & 0o133:
        fail("SSH proof permissions are too broad")
    proof = load_json(path)
    required = {
        "schema_version",
        "plan_sha256",
        "created_at",
        "login_user",
        "source_ip",
        "server_port",
        "operator_key_fingerprint",
    }
    if not isinstance(proof, dict) or set(proof) != required:
        fail("SSH proof is not closed")
    if (
        proof["schema_version"] != SCHEMA_VERSION
        or proof["plan_sha256"] != sha256(plan_path)
        or proof["login_user"] != "aiqops"
        or proof["server_port"] != plan["ssh"]["port"]
        or proof["operator_key_fingerprint"] != plan["operator"]["ssh_fingerprint"]
        or ipaddress.ip_address(proof["source_ip"])
        not in ipaddress.ip_network(plan["ssh"]["source_cidr"])
    ):
        fail("SSH proof does not match the plan")
    created = parse_time(proof["created_at"])
    if not dt.datetime.now(dt.UTC) - dt.timedelta(minutes=15) <= created <= dt.datetime.now(dt.UTC):
        fail("SSH proof is stale")


def migrate_docker_data(plan: dict[str, Any]) -> None:
    if not plan["docker"]["copy_migration_required"]:
        return
    source = Path(plan["docker"]["current_data_root"])
    target = Path(plan["docker"]["target_data_root"])
    marker = target / ".aiq-copy-complete"
    if marker.exists():
        return
    run(["systemctl", "stop", "docker.service", "docker.socket"])
    if any(target.iterdir()):
        fail("target Docker data root is not empty; refusing ambiguous migration")
    run(["cp", "--archive", "--reflink=auto", f"{source}/.", str(target)])
    marker.write_text(f"source={source}\nplan={plan['repository_head']}\n", encoding="utf-8")
    os.chown(marker, 0, 0)
    os.chmod(marker, 0o400)


def render_and_install_hardening(plan: dict[str, Any]) -> None:
    hardening = Path(plan["hardening"]["directory"])
    port = str(plan["ssh"]["port"])
    cidr = plan["ssh"]["source_cidr"]
    replacements = {"@SSH_PORT@": port, "@SSH_SOURCE_CIDR@": cidr}
    ssh_text = (hardening / "sshd/99-ai-quant.conf.template").read_text(encoding="utf-8")
    nft_text = (hardening / "nftables/ai-quant-host-input.nft.template").read_text(encoding="utf-8")
    for token, value in replacements.items():
        ssh_text = ssh_text.replace(token, value)
        nft_text = nft_text.replace(token, value)
    static_files = (
        ("docker/daemon.json", "/etc/docker/daemon.json", 0o644),
        ("journald/99-ai-quant.conf", "/etc/systemd/journald.conf.d/99-ai-quant.conf", 0o644),
        ("chrony/ai-quant.sources", "/etc/chrony/sources.d/ai-quant.sources", 0o644),
        ("sysctl/99-ai-quant.conf", "/etc/sysctl.d/99-ai-quant.conf", 0o644),
        ("limits/99-ai-quant.conf", "/etc/security/limits.d/99-ai-quant.conf", 0o644),
        (
            "systemd/aiq-host-input-firewall.service",
            "/etc/systemd/system/aiq-host-input-firewall.service",
            0o644,
        ),
    )
    for source, destination, mode in static_files:
        install_file(hardening / source, Path(destination), mode)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(ssh_text)
        ssh_temp = Path(handle.name)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(nft_text)
        nft_temp = Path(handle.name)
    try:
        install_file(nft_temp, Path("/etc/nftables.d/ai-quant-host-input.nft"), 0o600)
    finally:
        nft_temp.unlink(missing_ok=True)
    run(["nft", "--check", "-f", "/etc/nftables.d/ai-quant-host-input.nft"])
    run(["sysctl", "--system"])
    run(["timedatectl", "set-timezone", "UTC"])
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "restart", "systemd-journald.service"])
    run(["systemctl", "restart", "chrony.service"])
    run(["systemctl", "enable", "--now", "docker.service"])
    run(["systemctl", "enable", "aiq-host-input-firewall.service"])
    run(["systemctl", "restart", "aiq-host-input-firewall.service"])
    ssh_destination = Path("/etc/ssh/sshd_config.d/99-ai-quant.conf")
    try:
        install_file(ssh_temp, ssh_destination, 0o644)
        run(["sshd", "-t"])
        run(["systemctl", "reload", "ssh.service"])
    except SystemExit:
        ssh_destination.unlink(missing_ok=True)
        run(["systemctl", "reload", "ssh.service"], check=False)
        raise
    finally:
        ssh_temp.unlink(missing_ok=True)


def command_apply(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        fail("apply requires root")
    plan = validate_plan(args.plan)
    verify_approval(args.plan, plan, args.approval)
    current_host = validate_host()
    if current_host["boot_id"] != plan["host"]["boot_id"]:
        fail("host rebooted after plan; generate a new plan")
    validate_lock(Path(plan["toolchain_lock"]["path"]), Path(__file__).resolve().parents[1])
    validate_hardening(Path(plan["hardening"]["directory"]))
    install_toolchain(plan)
    ensure_users_and_directories(plan)
    if args.ssh_proof is None:
        print(
            "bootstrap apply PREPARED: aiqops key installed; "
            "open a second SSH session and run prove-ssh"
        )
        return 10
    validate_ssh_proof(args.ssh_proof, args.plan, plan)
    migrate_docker_data(plan)
    render_and_install_hardening(plan)
    print("bootstrap apply PASS: hardening activated; run verify immediately")
    return 0


def installed_package_versions(plan: dict[str, Any]) -> dict[str, str]:
    versions: dict[str, str] = {}
    for package in plan["packages"]:
        result = run(["dpkg-query", "-W", "-f=${Version}", package["name"]], check=False)
        versions[package["name"]] = result.stdout.decode() if result.returncode == 0 else "MISSING"
    return versions


def command_verify(args: argparse.Namespace) -> int:
    plan = validate_plan(args.plan)
    failures: list[str] = []
    versions = installed_package_versions(plan)
    for package in plan["packages"]:
        if versions[package["name"]] != package["version"]:
            failures.append(f"package:{package['name']}")
    locked_artifacts = {
        item["name"]: item["sha256"]
        for item in load_json(Path(plan["toolchain_lock"]["path"]))["artifacts"]
    }
    expected_files = {
        "/usr/local/bin/quantctl": locked_artifacts["quantctl"],
        "/usr/local/bin/cosign": locked_artifacts["cosign"],
    }
    for name, expected in expected_files.items():
        path = Path(name)
        if not path.exists() or sha256(path) != expected:
            failures.append(f"artifact:{name}")
    sshd = run(["sshd", "-T"], check=False)
    ssh_text = sshd.stdout.decode()
    for expected in (
        f"port {plan['ssh']['port']}",
        "permitrootlogin no",
        "passwordauthentication no",
        "pubkeyauthentication yes",
    ):
        if expected not in ssh_text.splitlines():
            failures.append(f"sshd:{expected}")
    nft_result = run(["nft", "list", "table", "inet", "ai_quant_host_input"], check=False)
    if nft_result.returncode != 0 or "policy drop" not in nft_result.stdout.decode():
        failures.append("nftables:ai_quant_host_input")
    chrony = run(["chronyc", "tracking"], check=False).stdout.decode()
    if "Leap status     : Normal" not in chrony:
        failures.append("chrony")
    timezone = (
        run(["timedatectl", "show", "--property=Timezone", "--value"]).stdout.decode().strip()
    )
    if timezone not in {"UTC", "Etc/UTC"}:
        failures.append("timezone")
    listeners = run(["ss", "-H", "-lntup"]).stdout.decode()
    forbidden_ports = re.findall(
        r"(?:0\.0\.0\.0|\[::\]|\*):(8080|9090|9093|5432|6379)\b", listeners
    )
    if forbidden_ports:
        failures.append("public-listeners:" + ",".join(sorted(set(forbidden_ports))))
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "captured_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "plan_sha256": sha256(args.plan),
        "host": validate_host(),
        "package_versions": versions,
        "artifact_sha256": {
            name: sha256(Path(name)) if Path(name).exists() else "MISSING"
            for name in expected_files
        },
        "sshd": {
            "port": plan["ssh"]["port"],
            "permit_root_login": "no" if "permitrootlogin no" in ssh_text else "FAIL",
            "password_authentication": "no" if "passwordauthentication no" in ssh_text else "FAIL",
        },
        "nftables_input_policy": (
            "default-drop" if "policy drop" in nft_result.stdout.decode() else "FAIL"
        ),
        "chrony_leap_status": "Normal" if "Leap status     : Normal" in chrony else "FAIL",
        "timezone": timezone,
        "forbidden_public_listener_ports": sorted(set(forbidden_ports)),
        "result": "PASS" if not failures else "FAIL",
        "failures": failures,
        "rollback": {
            "sshd": (
                "Oracle Cloud console: remove /etc/ssh/sshd_config.d/99-ai-quant.conf "
                "then reload ssh"
            ),
            "firewall": (
                "Oracle Cloud console: systemctl disable --now "
                "aiq-host-input-firewall.service; "
                "nft delete table inet ai_quant_host_input"
            ),
            "docker": (
                f"restore daemon.json and use preserved {plan['docker']['current_data_root']}"
            ),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_bytes(evidence) + b"\n")
    os.chmod(args.output, 0o444)
    if failures:
        fail("verification findings: " + ", ".join(failures))
    print(f"bootstrap verify PASS evidence={args.output} sha256={sha256(args.output)}")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="bootstrap-host")
    commands = root.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--toolchain-lock", required=True, type=Path)
    plan.add_argument("--hardening-dir", required=True, type=Path)
    plan.add_argument("--ssh-port", required=True, type=int)
    plan.add_argument("--ssh-source-cidr", required=True)
    plan.add_argument("--operator-public-key", required=True, type=Path)
    plan.add_argument("--approval-public-key", required=True, type=Path)
    plan.add_argument("--off-host-backup-evidence", required=True, type=Path)
    plan.add_argument("--recovery-console-confirmed", action="store_true")
    plan.add_argument("--output", required=True, type=Path)
    plan.set_defaults(handler=command_plan)

    apply = commands.add_parser("apply")
    apply.add_argument("--plan", required=True, type=Path)
    apply.add_argument("--approval", required=True, type=Path)
    apply.add_argument("--ssh-proof", type=Path)
    apply.set_defaults(handler=command_apply)

    prove = commands.add_parser("prove-ssh")
    prove.add_argument("--plan", required=True, type=Path)
    prove.add_argument("--output", required=True, type=Path)
    prove.set_defaults(handler=command_prove_ssh)

    verify = commands.add_parser("verify")
    verify.add_argument("--plan", required=True, type=Path)
    verify.add_argument("--output", required=True, type=Path)
    verify.set_defaults(handler=command_verify)
    return root


def main() -> int:
    args = parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
