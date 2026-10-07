"""Projections, policy vocabulary and validation for calibration management.

A calibration *version* maps raw probe readings to a reference concentration
with a multi-point piecewise linear curve. Versions are immutable once a
reading has used them; replacing a probe model means issuing a new version,
never editing an old one, so historical data can always be recomputed under
the口径 (basis) that originally produced it.

The vocabulary fixed here removes the ambiguities called out in operations:

* version status: ``draft`` -> ``active`` -> ``superseded`` (old versions are
  retained, never deleted);
* migration mode: ``legacy`` (only the old basis reports), ``dual`` (old and
  new coexist, the old basis stays the reported one), ``migrated`` (the new
  basis is the single reported one);
* insufficient samples policy: ``fallback`` (use the previous version) or
  ``nearest`` (extend the nearest calibrated segment, marked untrusted);
* out-of-span policy: ``clamp`` (hold the end-point reference) or ``nearest``
  (extend the nearest segment); either is flagged ``out_of_span``;
* failover policy when a probe is失校 (out of calibration): ``standby``
  (switch to the standby probe) or ``degraded`` (whole channel degrades).
"""

from __future__ import annotations

from dataclasses import dataclass, field

UNIT_PPM = "ppm"

DRAFT = "draft"
ACTIVE = "active"
SUPERSEDED = "superseded"
STATUSES = (DRAFT, ACTIVE, SUPERSEDED)

LEGACY = "legacy"
DUAL = "dual"
MIGRATED = "migrated"
MODES = (LEGACY, DUAL, MIGRATED)

# 样本不足时的默认口径：回退到上一版本，而不是用最近一段外推。
FALLBACK = "fallback"
NEAREST = "nearest"
INSUFFICIENT_POLICIES = (FALLBACK, NEAREST)

# 超界读数默认夹到端点参考值；外推必须显式配置。
CLAMP = "clamp"
SPAN_POLICIES = (CLAMP, NEAREST)

# 失校默认切备用探头；没有健康备用探头时一律整体降级。
STANDBY = "standby"
DEGRADED = "degraded"
FAILOVER_POLICIES = (STANDBY, DEGRADED)

PRIMARY = "primary"
ROLES = (PRIMARY, STANDBY)

DEFAULT_RANGE_MIN = 0.0
DEFAULT_RANGE_MAX = 1000.0
DEFAULT_MIN_SAMPLES = 2
DEFAULT_TOLERANCE = 2.0
DEFAULT_MIN_CROSS_SAMPLES = 3

BASELINE_VERSION = "legacy-v1"


@dataclass(frozen=True)
class CalPoint:
    """One calibration sample: raw probe value and its reference concentration."""

    raw: float
    reference: float

    def as_dict(self) -> dict[str, float]:
        return {"raw": self.raw, "reference": self.reference}


@dataclass(frozen=True)
class VersionView:
    """A calibration version and the range it is applicable over."""

    id: str
    created: str
    status: str
    range_min: float
    range_max: float
    unit: str
    points: list[CalPoint]
    sample_count: int
    min_samples: int
    predecessor: str
    sufficient: bool
    applicability: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "created": self.created,
            "status": self.status,
            "range_min": self.range_min,
            "range_max": self.range_max,
            "unit": self.unit,
            "points": [point.as_dict() for point in self.points],
            "sample_count": self.sample_count,
            "min_samples": self.min_samples,
            "predecessor": self.predecessor,
            "sufficient": self.sufficient,
            "applicability": self.applicability,
        }


@dataclass(frozen=True)
class ProbeView:
    """One physical probe and the calibration version it reports under."""

    id: str
    role: str
    healthy: bool
    range_min: float
    range_max: float
    unit: str
    version: str
    applicability: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "role": self.role,
            "healthy": self.healthy,
            "range_min": self.range_min,
            "range_max": self.range_max,
            "unit": self.unit,
            "version": self.version,
            "applicability": self.applicability,
        }


@dataclass(frozen=True)
class ReadingView:
    """A converted concentration with every basis decision that produced it."""

    at: str
    probe: str
    source: str
    raw: float
    concentration: float | None
    legacy_concentration: float | None
    unit: str
    version: str
    converted_version: str
    basis: str
    trusted: bool
    mode: str
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "at": self.at,
            "probe": self.probe,
            "source": self.source,
            "raw": self.raw,
            "concentration": self.concentration,
            "legacy_concentration": self.legacy_concentration,
            "unit": self.unit,
            "version": self.version,
            "converted_version": self.converted_version,
            "basis": self.basis,
            "trusted": self.trusted,
            "mode": self.mode,
            "flags": list(self.flags),
        }


@dataclass(frozen=True)
class CrossSample:
    """One raw value converted under both coexisting versions."""

    at: str
    raw: float
    legacy: float
    current: float
    diff: float

    def as_dict(self) -> dict[str, float | str]:
        return {
            "at": self.at,
            "raw": self.raw,
            "legacy": self.legacy,
            "current": self.current,
            "diff": self.diff,
        }


def validate_range(range_min: float, range_max: float) -> None:
    """Reject impossible or reversed probe ranges."""

    if range_min < 0:
        raise ValueError("range minimum must be non-negative")
    if range_max <= range_min:
        raise ValueError("range maximum must exceed the minimum")
    if range_max > 1_000_000:
        raise ValueError("range maximum exceeds the supported range")


def validate_min_samples(value: int) -> None:
    if value < 1:
        raise ValueError("min_samples must be positive")


def validate_tolerance(value: float) -> None:
    if value <= 0:
        raise ValueError("tolerance must be positive")


def validate_points(points: list[CalPoint]) -> None:
    """Reject calibration samples that cannot define a monotonic curve."""

    if len(points) < 1:
        raise ValueError("a calibration version needs at least one point")
    if len(points) == 1 and points[0].raw <= 0:
        raise ValueError("a single-point calibration must pass through the origin")
    previous_raw = -1.0
    previous_reference = -1.0
    for point in points:
        if point.raw < 0:
            raise ValueError("calibration raw values must be non-negative")
        if point.reference < 0:
            raise ValueError("calibration reference values must be non-negative")
        if point.raw <= previous_raw:
            raise ValueError("calibration raw values must be strictly increasing")
        if point.reference <= previous_reference and len(points) > 1:
            raise ValueError("calibration reference values must be strictly increasing")
        previous_raw = point.raw
        previous_reference = point.reference


def applicability_text(version: VersionView) -> str:
    """Render the scope of one calibration as a single operator-facing line."""

    ref_min = version.points[0].reference
    ref_max = version.points[-1].reference
    raw_min = version.points[0].raw
    raw_max = version.points[-1].raw
    trust = "trusted" if version.sufficient else "insufficient samples"
    return (
        f"status={version.status} raw[{raw_min:.4f}..{raw_max:.4f}] "
        f"probe range[{version.range_min:.4f}..{version.range_max:.4f}] {version.unit} -> "
        f"concentration[{ref_min:.4f}..{ref_max:.4f}] {version.unit} ({trust})"
    )
