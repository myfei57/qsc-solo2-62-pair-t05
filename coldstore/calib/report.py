"""Projections and validation rules for calibration management."""

from __future__ import annotations

from dataclasses import dataclass, field

from .curve import Point

DRAFT = "draft"
ACTIVE = "active"
RETIRED = "retired"
VERSION_STATES = (DRAFT, ACTIVE, RETIRED)

SINGLE = "single"
MIGRATING = "migrating"
DUAL_RUN = "dual_run"
NEW = "new"
POINT_STATES = (SINGLE, MIGRATING, DUAL_RUN, NEW)

PRIMARY = "primary"
STANDBY = "standby"
DEGRADED = "degraded"

NOMINAL = "nominal"
FALLBACK = "fallback"
SWITCHED = "switched"
DEGRADED_QUALITY = "degraded"
QUALITY_STATES = (NOMINAL, FALLBACK, SWITCHED, DEGRADED_QUALITY)

MAX_RANGE = 1_000_000.0


def validate_concentration(value: float) -> None:
    """Reject concentration values outside the supported ammonia range."""

    if value < 0:
        raise ValueError("concentration must be non-negative")
    if value > MAX_RANGE:
        raise ValueError("concentration exceeds the supported range")


def validate_range(raw_min: float, raw_max: float) -> None:
    """Reject an inverted or empty instrument range."""

    if raw_min < 0:
        raise ValueError("range minimum must be non-negative")
    if raw_max <= raw_min:
        raise ValueError("range maximum must exceed the range minimum")
    if raw_max > MAX_RANGE:
        raise ValueError("range maximum exceeds the supported range")


@dataclass(frozen=True)
class VersionView:
    """Serializable projection of one calibration version."""

    id: str
    point: str
    basis: str
    state: str
    raw_min: float
    raw_max: float
    points: list[dict[str, float]]
    created_at: str
    activated_at: str
    retired_at: str
    note: str
    applicable: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "point": self.point,
            "basis": self.basis,
            "state": self.state,
            "range": {"raw_min": self.raw_min, "raw_max": self.raw_max},
            "points": self.points,
            "created_at": self.created_at,
            "activated_at": self.activated_at,
            "retired_at": self.retired_at,
            "note": self.note,
            "applicable": self.applicable,
        }


@dataclass(frozen=True)
class ProbeView:
    """Serializable projection of one registered probe."""

    id: str
    point: str
    role: str
    basis: str
    version_id: str
    raw_min: float
    raw_max: float
    status: str
    active: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "point": self.point,
            "role": self.role,
            "basis": self.basis,
            "version_id": self.version_id,
            "range": {"raw_min": self.raw_min, "raw_max": self.raw_max},
            "status": self.status,
            "active": self.active,
        }


@dataclass(frozen=True)
class ReadingView:
    """One converted ammonia reading with full calibration provenance."""

    point: str
    probe: str
    raw: float
    concentration: float
    basis: str
    version_id: str
    migration: str
    quality: str
    authoritative: bool
    within_span: bool
    level: str
    at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "point": self.point,
            "probe": self.probe,
            "raw": self.raw,
            "concentration": self.concentration,
            "basis": self.basis,
            "version_id": self.version_id,
            "migration": self.migration,
            "quality": self.quality,
            "authoritative": self.authoritative,
            "within_span": self.within_span,
            "level": self.level,
            "at": self.at,
        }


def point_tuples(points: list[Point]) -> list[tuple[float, float]]:
    return [(point.raw, point.reference) for point in points]
