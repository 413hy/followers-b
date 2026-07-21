"""Runtime lookup for the frozen SHRUNK_MARKOUT_CELL_MEAN_V1 estimator."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class GrossEdgeKey:
    setup_type: str
    pa_regime: str
    liquidity_bucket: str
    side: str
    entry_path: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.setup_type,
                self.pa_regime,
                self.liquidity_bucket,
                self.side,
                self.entry_path,
            )
        ):
            raise ValueError("gross-edge key dimensions must be non-empty")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("gross-edge side must be BUY or SELL")


@dataclass(frozen=True, slots=True)
class GrossEdgeCell:
    setup_type: str | None
    pa_regime: str | None
    liquidity_bucket: str | None
    side: str | None
    entry_path: str | None
    observations: int
    mean_gross_edge_bps: Decimal
    evidence_hash: str

    def __post_init__(self) -> None:
        if self.observations < 1:
            raise ValueError("gross-edge cell observations must be positive")
        if len(self.evidence_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.evidence_hash
        ):
            raise ValueError("gross-edge cell evidence hash is invalid")
        dimensions = (
            self.setup_type,
            self.pa_regime,
            self.liquidity_bucket,
            self.side,
            self.entry_path,
        )
        seen_none = False
        for value in dimensions:
            if value is None:
                seen_none = True
            elif seen_none:
                raise ValueError("gross-edge parent cell must drop dimensions in frozen order")
        if self.side is not None and self.side not in {"BUY", "SELL"}:
            raise ValueError("gross-edge cell side is invalid")

    @property
    def identity(self) -> tuple[str | None, ...]:
        return (
            self.setup_type,
            self.pa_regime,
            self.liquidity_bucket,
            self.side,
            self.entry_path,
        )


@dataclass(frozen=True, slots=True)
class GrossEdgeEstimate:
    approved: bool
    gross_edge_bps: Decimal | None
    exact_observations: int
    parent_observations: int
    source_level: str | None
    evidence_hashes: tuple[str, ...]
    reason_codes: tuple[str, ...]


def estimate_gross_edge(
    key: GrossEdgeKey,
    cells: tuple[GrossEdgeCell, ...],
    *,
    minimum_cell_observations: int,
    minimum_parent_observations: int,
    shrinkage_strength_observations: int,
) -> GrossEdgeEstimate:
    """Resolve an exact cell and its first adequate frozen hierarchy parent."""
    if (
        min(
            minimum_cell_observations,
            minimum_parent_observations,
            shrinkage_strength_observations,
        )
        < 1
    ):
        raise ValueError("gross-edge observation thresholds must be positive")
    by_identity: dict[tuple[str | None, ...], GrossEdgeCell] = {}
    for cell in cells:
        if cell.identity in by_identity:
            raise ValueError("duplicate gross-edge lookup cell")
        by_identity[cell.identity] = cell

    exact_identity = (
        key.setup_type,
        key.pa_regime,
        key.liquidity_bucket,
        key.side,
        key.entry_path,
    )
    exact = by_identity.get(exact_identity)
    exact_count = exact.observations if exact is not None else 0
    parent_levels = (
        (
            "DROP_ENTRY_PATH",
            (key.setup_type, key.pa_regime, key.liquidity_bucket, key.side, None),
        ),
        (
            "DROP_SIDE",
            (key.setup_type, key.pa_regime, key.liquidity_bucket, None, None),
        ),
        (
            "DROP_LIQUIDITY_BUCKET",
            (key.setup_type, key.pa_regime, None, None, None),
        ),
        ("SETUP_ONLY", (key.setup_type, None, None, None, None)),
        ("GLOBAL", (None, None, None, None, None)),
    )
    selected_level: str | None = None
    parent: GrossEdgeCell | None = None
    for level, identity in parent_levels:
        candidate = by_identity.get(identity)
        if candidate is not None and candidate.observations >= minimum_parent_observations:
            selected_level = level
            parent = candidate
            break

    exact_adequate = exact is not None and exact.observations >= minimum_cell_observations
    if not exact_adequate and parent is None:
        return GrossEdgeEstimate(
            approved=False,
            gross_edge_bps=None,
            exact_observations=exact_count,
            parent_observations=0,
            source_level=None,
            evidence_hashes=(),
            reason_codes=("NET_EDGE_EVIDENCE_INCOMPLETE",),
        )

    if not exact_adequate:
        if parent is None:
            raise RuntimeError("gross-edge parent selection invariant breached")
        return GrossEdgeEstimate(
            approved=True,
            gross_edge_bps=parent.mean_gross_edge_bps,
            exact_observations=exact_count,
            parent_observations=parent.observations,
            source_level=selected_level,
            evidence_hashes=(parent.evidence_hash,),
            reason_codes=("GROSS_EDGE_PARENT_FALLBACK",),
        )

    if exact is None:
        raise RuntimeError("gross-edge exact-cell selection invariant breached")
    if parent is None:
        return GrossEdgeEstimate(
            approved=True,
            gross_edge_bps=exact.mean_gross_edge_bps,
            exact_observations=exact.observations,
            parent_observations=0,
            source_level="EXACT_UNSHRUNK",
            evidence_hashes=(exact.evidence_hash,),
            reason_codes=("GROSS_EDGE_PARENT_UNAVAILABLE",),
        )

    strength = Decimal(shrinkage_strength_observations)
    exact_weight = Decimal(exact.observations)
    shrunk_mean = (
        exact.mean_gross_edge_bps * exact_weight + parent.mean_gross_edge_bps * strength
    ) / (exact_weight + strength)
    return GrossEdgeEstimate(
        approved=True,
        gross_edge_bps=shrunk_mean,
        exact_observations=exact.observations,
        parent_observations=parent.observations,
        source_level=f"EXACT_SHRUNK_TO_{selected_level}",
        evidence_hashes=(exact.evidence_hash, parent.evidence_hash),
        reason_codes=(),
    )
