"""Small, deterministic resilience helpers shared by long-running services."""

from __future__ import annotations


def bounded_exponential_backoff(
    consecutive_failures: int,
    *,
    base_seconds: int = 5,
    maximum_seconds: int = 60,
) -> int:
    """Return a bounded delay without allowing an unbounded exponent."""
    if consecutive_failures < 1:
        raise ValueError("consecutive failures must be positive")
    if base_seconds < 1 or maximum_seconds < base_seconds:
        raise ValueError("backoff bounds are invalid")
    exponent = min(consecutive_failures - 1, 30)
    delay = base_seconds << exponent
    return min(maximum_seconds, delay)
