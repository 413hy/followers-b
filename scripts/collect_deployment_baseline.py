#!/usr/bin/env python3
"""Collect and verify a hash-chained 24-hour Debian deployment baseline."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import ipaddress
import itertools
import json
import os
import re
import socket
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, NoReturn

SCHEMA_VERSION = "1.0.0"
PUBLIC_IP_ENDPOINTS = {
    "aws": "https://checkip.amazonaws.com",
    "ipify": "https://api.ipify.org",
}
DNS_NAMES = ("deb.debian.org", "download.docker.com")


def fail(message: str) -> NoReturn:
    raise SystemExit(f"deployment baseline FAIL: {message}")


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def run(command: list[str]) -> str:
    result = subprocess.run(  # noqa: S603
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"exit {result.returncode}")
    return result.stdout


def public_ip(url: str) -> str:
    value = run(
        [
            "/usr/bin/curl",
            "--fail",
            "--silent",
            "--show-error",
            "--proto",
            "=https",
            "--tlsv1.2",
            "--max-redirs",
            "0",
            "--max-time",
            "5",
            url,
        ]
    ).strip()
    address = ipaddress.ip_address(value)
    if address.version != 4 or not address.is_global:
        raise RuntimeError("public endpoint returned a non-global IPv4 address")
    return str(address)


def dns_addresses(name: str) -> list[str]:
    addresses = {
        item[4][0] for item in socket.getaddrinfo(name, 443, socket.AF_INET, socket.SOCK_STREAM)
    }
    if not addresses:
        raise RuntimeError("DNS returned no IPv4 addresses")
    return sorted(addresses)


def default_gateway() -> str:
    output = run(["/usr/sbin/ip", "-4", "route", "show", "default"])
    matches = re.findall(r"^default via (\S+) dev (\S+)(?:\s|$)", output, re.MULTILINE)
    if len(matches) != 1:
        raise RuntimeError("exactly one IPv4 default route is required")
    return matches[0][0]


def gateway_ping(address: str) -> dict[str, float | int]:
    output = run(["/usr/bin/ping", "-n", "-c", "3", "-W", "2", address])
    loss = re.search(r"([0-9.]+)% packet loss", output)
    rtt = re.search(r"= ([0-9.]+)/([0-9.]+)/([0-9.]+)/([0-9.]+) ms", output)
    if loss is None or rtt is None:
        raise RuntimeError("cannot parse gateway ping")
    return {
        "packet_loss_percent": float(loss.group(1)),
        "rtt_min_ms": float(rtt.group(1)),
        "rtt_avg_ms": float(rtt.group(2)),
        "rtt_max_ms": float(rtt.group(3)),
        "rtt_mdev_ms": float(rtt.group(4)),
    }


def chrony_tracking() -> dict[str, str | float]:
    output = run(["/usr/bin/chronyc", "tracking"])
    values: dict[str, str] = {}
    for line in output.splitlines():
        if " : " in line:
            key, value = line.split(" : ", 1)
            values[key.strip()] = value.strip()
    system_time = values.get("System time", "")
    offset = re.fullmatch(r"([0-9.]+) seconds (fast|slow) of NTP time", system_time)
    if values.get("Leap status") != "Normal" or offset is None:
        raise RuntimeError("chrony is not synchronized")
    signed_offset = float(offset.group(1)) * (1 if offset.group(2) == "fast" else -1)
    return {
        "leap_status": "Normal",
        "reference_id": values.get("Reference ID", ""),
        "stratum": values.get("Stratum", ""),
        "system_offset_seconds": signed_offset,
    }


def capture(sequence: int) -> dict[str, Any]:
    errors: dict[str, str] = {}
    ip_results: dict[str, str] = {}
    dns_results: dict[str, list[str]] = {}
    gateway = ""
    ping: dict[str, float | int] = {}
    chrony: dict[str, str | float] = {}
    for name, url in PUBLIC_IP_ENDPOINTS.items():
        try:
            ip_results[name] = public_ip(url)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            errors[f"public_ip:{name}"] = str(exc)
    for name in DNS_NAMES:
        try:
            dns_results[name] = dns_addresses(name)
        except OSError as exc:
            errors[f"dns:{name}"] = str(exc)
    try:
        gateway = default_gateway()
        ping = gateway_ping(gateway)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        errors["gateway"] = str(exc)
    try:
        chrony = chrony_tracking()
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        errors["chrony"] = str(exc)
    return {
        "schema_version": SCHEMA_VERSION,
        "sequence": sequence,
        "captured_at": dt.datetime.now(dt.UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "monotonic_ns": time.monotonic_ns(),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip(),
        "public_ipv4": ip_results,
        "dns_ipv4": dns_results,
        "default_gateway": gateway,
        "gateway_ping": ping,
        "chrony": chrony,
        "errors": errors,
    }


def append_record(handle: Any, content: dict[str, Any], previous: str) -> str:
    material = {"content": content, "previous_sha256": previous}
    record_hash = hashlib.sha256(canonical(material)).hexdigest()
    record = {**material, "record_sha256": record_hash}
    handle.write(canonical(record) + b"\n")
    handle.flush()
    os.fsync(handle.fileno())
    return record_hash


def load_and_verify(path: Path) -> list[dict[str, Any]]:
    previous = "0" * 64
    records: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        for number, line in enumerate(handle, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                fail(f"invalid JSON at line {number}: {exc}")
            if not isinstance(record, dict) or set(record) != {
                "content",
                "previous_sha256",
                "record_sha256",
            }:
                fail(f"record {number} is not closed")
            material = {
                "content": record["content"],
                "previous_sha256": record["previous_sha256"],
            }
            expected = hashlib.sha256(canonical(material)).hexdigest()
            if record["previous_sha256"] != previous or record["record_sha256"] != expected:
                fail(f"hash chain mismatch at record {number}")
            content = record["content"]
            if not isinstance(content, dict) or content.get("sequence") != number - 1:
                fail(f"sequence mismatch at record {number}")
            previous = expected
            records.append(record)
    if not records:
        fail("baseline contains no samples")
    return records


def summarize(records: list[dict[str, Any]], minimum_duration: int) -> dict[str, Any]:
    contents = [record["content"] for record in records]
    first = contents[0]
    last = contents[-1]
    duration = (last["monotonic_ns"] - first["monotonic_ns"]) / 1_000_000_000
    gaps = [
        (right["monotonic_ns"] - left["monotonic_ns"]) / 1_000_000_000
        for left, right in itertools.pairwise(contents)
    ]
    public_values = {value for content in contents for value in content["public_ipv4"].values()}
    offsets = [
        abs(float(content["chrony"].get("system_offset_seconds", 999.0))) for content in contents
    ]
    packet_losses = [
        float(content["gateway_ping"].get("packet_loss_percent", 100.0)) for content in contents
    ]
    failures: list[str] = []
    if duration < minimum_duration:
        failures.append("duration")
    if gaps and max(gaps) > 90:
        failures.append("sample-gap")
    if len({content["boot_id"] for content in contents}) != 1:
        failures.append("boot-id")
    if len(public_values) != 1 or any(len(content["public_ipv4"]) != 2 for content in contents):
        failures.append("public-ip")
    if any(content["errors"] for content in contents):
        failures.append("sample-errors")
    if max(offsets) > 0.05:
        failures.append("clock-offset")
    if max(packet_losses) > 0:
        failures.append("gateway-loss")
    return {
        "schema_version": SCHEMA_VERSION,
        "result": "PASS" if not failures else "FAIL",
        "sample_count": len(records),
        "duration_seconds": duration,
        "maximum_gap_seconds": max(gaps, default=0.0),
        "boot_id": first["boot_id"],
        "public_ipv4": next(iter(public_values)) if len(public_values) == 1 else None,
        "maximum_clock_offset_seconds": max(offsets),
        "maximum_gateway_packet_loss_percent": max(packet_losses),
        "first_captured_at": first["captured_at"],
        "last_captured_at": last["captured_at"],
        "final_record_sha256": records[-1]["record_sha256"],
        "failures": failures,
    }


def validate_output_path(path: Path) -> None:
    if os.geteuid() != 0:
        fail("collection requires root")
    parent = path.parent
    metadata = parent.stat()
    if parent.is_symlink() or metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
        fail("output directory must be root-owned and not group/world writable")
    if path.exists() or path.is_symlink():
        fail("output path already exists")


def command_collect(args: argparse.Namespace) -> int:
    if args.duration_seconds < 60 or not 10 <= args.interval_seconds <= 60:
        fail("duration/interval outside safe bounds")
    validate_output_path(args.output)
    descriptor = os.open(
        args.output,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    previous = "0" * 64
    sequence = 0
    first_sample_ns: int | None = None
    with os.fdopen(descriptor, "wb") as handle:
        while True:
            content = capture(sequence)
            previous = append_record(handle, content, previous)
            sequence += 1
            sample_ns = int(content["monotonic_ns"])
            if first_sample_ns is None:
                first_sample_ns = sample_ns
            recorded_duration = (sample_ns - first_sample_ns) / 1_000_000_000
            if recorded_duration >= args.duration_seconds:
                break
            time.sleep(min(args.interval_seconds, args.duration_seconds - recorded_duration))
    records = load_and_verify(args.output)
    summary = summarize(records, args.duration_seconds)
    summary_path = args.output.with_suffix(args.output.suffix + ".summary.json")
    summary_path.write_bytes(canonical(summary) + b"\n")
    os.chown(args.output, 0, 0)
    os.chown(summary_path, 0, 0)
    os.chmod(args.output, 0o400)
    os.chmod(summary_path, 0o400)
    if summary["result"] != "PASS":
        fail("completed baseline did not satisfy acceptance thresholds")
    print(f"deployment baseline PASS samples={len(records)} summary={summary_path}")
    return 0


def command_verify(args: argparse.Namespace) -> int:
    records = load_and_verify(args.input)
    summary = summarize(records, args.minimum_duration_seconds)
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0 if summary["result"] == "PASS" else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--output", required=True, type=Path)
    collect.add_argument("--duration-seconds", required=True, type=int)
    collect.add_argument("--interval-seconds", type=int, default=60)
    collect.set_defaults(handler=command_collect)
    verify = commands.add_parser("verify")
    verify.add_argument("--input", required=True, type=Path)
    verify.add_argument("--minimum-duration-seconds", type=int, default=86400)
    verify.set_defaults(handler=command_verify)
    return root


def main() -> int:
    args = parser().parse_args()
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
