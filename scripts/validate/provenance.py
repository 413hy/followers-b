#!/usr/bin/env python3
"""Prove copied baseline directories remain byte-identical to immutable inputs."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path("/root/quantify/reference-materials/vps-archive/vps")
SCOPES = ("contracts", "config", "runbooks", "diagrams")
OWNER_AMENDMENTS = (
    Path("docs/adr/0006-remove-time-based-position-exit.md"),
    Path("docs/adr/0009-exchange-maximum-leverage-all-environments.md"),
)
OWNER_AMENDED_FILES = {
    Path("config/binance-mandatory-endpoint-inventory.example.json"),
    Path("config/binance-mandatory-endpoint-inventory.schema.json"),
    Path("config/price-action.example.yaml"),
    Path("config/price-action.schema.json"),
    Path("config/risk.example.yaml"),
    Path("config/risk.schema.json"),
    Path("contracts/domain-events.schema.json"),
    Path("contracts/examples/trade-plan-entry.json"),
    Path("contracts/trade-plan.schema.json"),
}
PROJECT_ADDED_FILES = {
    Path("config/copy-production-activation.example.json"),
    Path("config/copy-production-activation.schema.json"),
    Path("config/copy-trading.example.yaml"),
    Path("config/copy-trading.schema.json"),
    Path("contracts/copy-leader-selection.schema.json"),
    Path("contracts/copy-system-audit.schema.json"),
    Path("contracts/copy-system-repair.schema.json"),
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if not SOURCE.is_dir():
        print("provenance SKIP private immutable source archive is not installed")
        return 0
    failures: list[str] = []
    count = 0
    amended = 0
    project_additions = 0
    for owner_amendment in OWNER_AMENDMENTS:
        if not (ROOT / owner_amendment).is_file():
            failures.append(f"owner amendment missing: {owner_amendment}")
    for scope in SCOPES:
        source_files = {
            p.relative_to(SOURCE / scope) for p in (SOURCE / scope).rglob("*") if p.is_file()
        }
        copied_files = {
            p.relative_to(ROOT / scope) for p in (ROOT / scope).rglob("*") if p.is_file()
        }
        allowed_additions = {
            path.relative_to(scope) for path in PROJECT_ADDED_FILES if path.parts[0] == scope
        }
        if source_files | allowed_additions != copied_files:
            failures.append(f"{scope}: file set differs")
            continue
        project_additions += len(allowed_additions)
        for relative in sorted(source_files):
            count += 1
            if digest(SOURCE / scope / relative) != digest(ROOT / scope / relative):
                repository_path = Path(scope) / relative
                if repository_path in OWNER_AMENDED_FILES:
                    amended += 1
                else:
                    failures.append(f"{repository_path}: hash differs")
    if failures:
        print("\n".join(failures))
        return 1
    print(
        f"provenance PASS copied_files={count} owner_amendments={amended} "
        f"project_additions={project_additions}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
