"""Exact artifact hashing for startup-evidence bindings."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ai_quant.common.config import ConfigurationError, load_strict_document
from ai_quant.rate_budget.authorization import AuthorizationDenied, canonical_digest


class ArtifactHashMode(StrEnum):
    RAW_BYTES = "RAW_BYTES"
    JCS_DOCUMENT = "JCS_DOCUMENT"
    JCS_CONTENT = "JCS_CONTENT"


@dataclass(frozen=True, slots=True)
class ArtifactBindingSource:
    path: Path
    hash_mode: ArtifactHashMode


def _is_within(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _safe_identity(path: Path, approved_roots: Sequence[Path]) -> tuple[int, int, int, int]:
    absolute = Path(os.path.abspath(path))
    roots = tuple(Path(os.path.abspath(root)) for root in approved_roots)
    try:
        metadata = absolute.lstat()
    except OSError as exc:
        raise AuthorizationDenied("ARTIFACT_FILE_UNSAFE") from exc
    if (
        path != absolute
        or absolute.resolve() != absolute
        or not stat.S_ISREG(metadata.st_mode)
        or not _is_within(absolute, roots)
    ):
        raise AuthorizationDenied("ARTIFACT_FILE_UNSAFE")
    return metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_mode


def _artifact_hash(source: ArtifactBindingSource) -> str:
    try:
        if source.hash_mode is ArtifactHashMode.RAW_BYTES:
            return hashlib.sha256(source.path.read_bytes()).hexdigest()
        document = load_strict_document(source.path)
    except (OSError, ConfigurationError) as exc:
        raise AuthorizationDenied("ARTIFACT_CONTENT_INVALID") from exc
    if source.hash_mode is ArtifactHashMode.JCS_CONTENT:
        if not isinstance(document, Mapping) or not isinstance(document.get("content"), Mapping):
            raise AuthorizationDenied("ARTIFACT_CONTENT_INVALID")
        document = document["content"]
    try:
        return canonical_digest(document).hex()
    except (TypeError, ValueError) as exc:
        raise AuthorizationDenied("ARTIFACT_CONTENT_INVALID") from exc


def verify_artifact_bindings(
    expected: Mapping[str, str],
    sources: Mapping[str, ArtifactBindingSource],
    *,
    approved_roots: Sequence[Path],
) -> Mapping[str, str]:
    """Recompute every startup artifact binding with exact, caller-declared semantics."""
    if not approved_roots or set(expected) != set(sources):
        raise AuthorizationDenied("ARTIFACT_BINDING_COVERAGE_INVALID")
    observed: dict[str, str] = {}
    for name in sorted(expected):
        expected_hash = expected[name]
        if len(expected_hash) != 64 or any(
            character not in "0123456789abcdef" for character in expected_hash
        ):
            raise AuthorizationDenied("ARTIFACT_BINDING_HASH_INVALID")
        source = sources[name]
        before = _safe_identity(source.path, approved_roots)
        observed_hash = _artifact_hash(source)
        after = _safe_identity(source.path, approved_roots)
        if before != after:
            raise AuthorizationDenied("ARTIFACT_FILE_CHANGED")
        if observed_hash != expected_hash:
            raise AuthorizationDenied("ARTIFACT_BINDING_MISMATCH")
        observed[name] = observed_hash
    return observed


def measure_artifact_bindings(
    sources: Mapping[str, ArtifactBindingSource],
    *,
    approved_roots: Sequence[Path],
) -> Mapping[str, str]:
    """Hash a closed set of local artifacts while detecting path replacement races."""
    if not approved_roots or not sources:
        raise AuthorizationDenied("ARTIFACT_BINDING_COVERAGE_INVALID")
    observed: dict[str, str] = {}
    for name in sorted(sources):
        source = sources[name]
        before = _safe_identity(source.path, approved_roots)
        observed[name] = _artifact_hash(source)
        after = _safe_identity(source.path, approved_roots)
        if before != after:
            raise AuthorizationDenied("ARTIFACT_FILE_CHANGED")
    return observed
