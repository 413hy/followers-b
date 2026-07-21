"""Pinned Codex model policy for automated copy-trading review tasks."""

from __future__ import annotations

CODEX_INTERVENTION_MODEL = "gpt-5.6-sol"
CODEX_INTERVENTION_REASONING_EFFORT = "high"


def codex_model_arguments() -> tuple[str, ...]:
    """Return explicit CLI overrides that survive ``--ignore-user-config``."""
    return (
        "--model",
        CODEX_INTERVENTION_MODEL,
        "--config",
        f'model_reasoning_effort="{CODEX_INTERVENTION_REASONING_EFFORT}"',
    )
