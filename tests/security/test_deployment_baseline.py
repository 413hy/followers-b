from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import collect_deployment_baseline as baseline


def _content(sequence: int, monotonic_ns: int) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "sequence": sequence,
        "captured_at": f"2026-07-14T11:{sequence:02d}:00.000000Z",
        "monotonic_ns": monotonic_ns,
        "boot_id": "00000000-0000-0000-0000-000000000001",
        "public_ipv4": {"aws": "203.0.113.36", "ipify": "203.0.113.36"},
        "dns_ipv4": {
            "deb.debian.org": ["151.101.2.132"],
            "download.docker.com": ["13.225.63.31"],
        },
        "default_gateway": "10.0.0.1",
        "gateway_ping": {
            "packet_loss_percent": 0.0,
            "rtt_min_ms": 0.1,
            "rtt_avg_ms": 0.2,
            "rtt_max_ms": 0.3,
            "rtt_mdev_ms": 0.1,
        },
        "chrony": {
            "leap_status": "Normal",
            "reference_id": "test",
            "stratum": "3",
            "system_offset_seconds": 0.00001,
        },
        "errors": {},
    }


@pytest.mark.security
def test_hash_chained_baseline_satisfies_fixed_ip_clock_and_gap_gates(tmp_path: Path) -> None:
    path = tmp_path / "baseline.jsonl"
    previous = "0" * 64
    with path.open("wb") as handle:
        for sequence, monotonic_ns in enumerate((0, 60_000_000_000, 120_000_000_000)):
            previous = baseline.append_record(handle, _content(sequence, monotonic_ns), previous)
    records = baseline.load_and_verify(path)
    summary = baseline.summarize(records, minimum_duration=120)
    assert summary["result"] == "PASS"
    assert summary["public_ipv4"] == "203.0.113.36"
    assert summary["maximum_gap_seconds"] == 60


@pytest.mark.security
def test_hash_chained_baseline_rejects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "baseline.jsonl"
    with path.open("wb") as handle:
        baseline.append_record(handle, _content(0, 0), "0" * 64)
    data = path.read_bytes().replace(b"203.0.113.36", b"203.0.113.37", 1)
    path.write_bytes(data)
    with pytest.raises(SystemExit, match="hash chain mismatch"):
        baseline.load_and_verify(path)


@pytest.mark.security
def test_collection_uses_recorded_sample_span_for_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    samples = iter(
        (
            _content(0, 0),
            _content(1, 59_999_000_000),
            _content(2, 60_000_000_000),
        )
    )
    monkeypatch.setattr(baseline, "capture", lambda _sequence: next(samples))
    monkeypatch.setattr(baseline.time, "sleep", lambda _seconds: None)
    output = tmp_path / "baseline.jsonl"

    result = baseline.command_collect(
        argparse.Namespace(
            output=output,
            duration_seconds=60,
            interval_seconds=10,
        )
    )

    summary = json.loads(output.with_suffix(".jsonl.summary.json").read_bytes())
    assert result == 0
    assert summary["result"] == "PASS"
    assert summary["duration_seconds"] == 60
    assert summary["sample_count"] == 3
