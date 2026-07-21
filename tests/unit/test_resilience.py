from __future__ import annotations

import pytest

from ai_quant.common.resilience import bounded_exponential_backoff


def test_dependency_backoff_grows_and_caps() -> None:
    assert [bounded_exponential_backoff(value) for value in range(1, 8)] == [
        5,
        10,
        20,
        40,
        60,
        60,
        60,
    ]


@pytest.mark.parametrize(
    ("failures", "base", "maximum"),
    [(0, 5, 60), (1, 0, 60), (1, 10, 5)],
)
def test_dependency_backoff_rejects_invalid_bounds(failures: int, base: int, maximum: int) -> None:
    with pytest.raises(ValueError):
        bounded_exponential_backoff(
            failures,
            base_seconds=base,
            maximum_seconds=maximum,
        )
