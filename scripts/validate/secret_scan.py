#!/usr/bin/env python3
"""Conservative repository secret scan without reading external auth stores."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIP = {
    ".git",
    ".hypothesis",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "evidence",
}
PATTERNS = {
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "telegram_token": re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),
    "literal_secret_assignment": re.compile(
        r"(?i)(?:api[_-]?key|secret|token|password|private[_-]?key)"
        r'''\s*[:=]\s*["'][A-Za-z0-9+/=_-]{16,}["']'''
    ),
}


def main() -> int:
    findings: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP for part in path.relative_to(ROOT).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for name, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{path.relative_to(ROOT)}: {name}")
    if findings:
        print("\n".join(findings))
        return 1
    print("secret scan PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
