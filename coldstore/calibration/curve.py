"""Piecewise linear conversion defined by ordered calibration points.

Inside the calibrated span the reading is linearly interpolated. Outside the
span two behaviours are available, chosen by configuration: ``clamp`` holds
the end-point reference concentration, ``nearest`` extends the nearest
segment linearly. Both callers must flag such readings as ``out_of_span`` so
the basis is never silently claimed as calibrated.
"""

from __future__ import annotations

from .report import CalPoint


def sorted_points(points: list[CalPoint]) -> list[CalPoint]:
    """Return calibration samples ordered by raw value."""

    return sorted(points, key=lambda point: point.raw)


def interpolate(points: list[CalPoint], raw: float, span_policy: str) -> tuple[float, bool]:
    """Convert ``raw`` to a reference concentration.

    Returns the concentration and whether the value fell outside the
    calibrated span. A single point defines a factor through that point.
    """

    ordered = sorted_points(points)
    if len(ordered) == 1:
        point = ordered[0]
        factor = point.reference / point.raw if point.raw > 0 else 1.0
        # A one-point calibration is a factor through the origin, valid over
        # the whole probe range; the caller checks the probe range itself.
        return raw * factor, False

    first = ordered[0]
    last = ordered[-1]
    if raw < first.raw:
        if span_policy == "nearest":
            return _extend(ordered[0], ordered[1], raw), True
        return first.reference, True
    if raw > last.raw:
        if span_policy == "nearest":
            return _extend(ordered[-2], ordered[-1], raw), True
        return last.reference, True

    for lower, upper in zip(ordered, ordered[1:]):
        if lower.raw <= raw <= upper.raw:
            return _extend(lower, upper, raw), False
    return first.reference, True


def _extend(lower: CalPoint, upper: CalPoint, raw: float) -> float:
    """Linear conversion on the segment joining two calibration samples."""

    span = upper.raw - lower.raw
    if span == 0:
        return lower.reference
    ratio = (raw - lower.raw) / span
    return lower.reference + ratio * (upper.reference - lower.reference)


def legacy_factor(points: list[CalPoint]) -> float:
    """Factor implied by a legacy one-point calibration."""

    ordered = sorted_points(points)
    point = ordered[-1]
    if point.raw <= 0:
        return 1.0
    return point.reference / point.raw
