"""Point based calibration curve math.

A calibration version maps a probe's raw signal (the value the probe reports on
its own scale) to the reference concentration (ppm). Old probes carried one
multiplicative factor; new probes span a different range and need a multi point
curve. A linear fit through sorted reference points covers both cases: a single
factor is just a two point line through the origin.

Values outside the calibrated reference span are never extrapolated for
authoritative readings. Interpolation is safe inside the span; outside it the
curve only projects a value and flags it, which drives the out of calibration
decision rather than silently extending a guessed line.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_POINTS = 3
MIN_COVERAGE = 0.80


@dataclass(frozen=True)
class Point:
    """One calibration sample: raw signal at a known reference concentration."""

    raw: float
    reference: float

    def as_dict(self) -> dict[str, float]:
        return {"raw": self.raw, "reference": self.reference}


def parse_point(item: object) -> Point | None:
    """Parse one ``{"raw", "reference"}`` mapping, skipping malformed rows."""

    if not isinstance(item, dict):
        return None
    try:
        raw = float(item["raw"])
        reference = float(item["reference"])
    except (KeyError, TypeError, ValueError):
        return None
    return Point(raw=raw, reference=reference)


def sorted_points(points: list[Point]) -> list[Point]:
    return sorted(points, key=lambda point: point.raw)


def is_monotonic(points: list[Point]) -> bool:
    """References must rise (or stay level) as the raw signal rises."""

    ordered = sorted_points(points)
    return all(later.reference >= earlier.reference for earlier, later in zip(ordered, ordered[1:]))


@dataclass(frozen=True)
class Conversion:
    """A projected concentration plus flags about how it was obtained."""

    value: float
    within: bool
    extrapolated: bool

    def as_dict(self) -> dict[str, object]:
        return {"value": self.value, "within": self.within, "extrapolated": self.extrapolated}


class Curve:
    """Piecewise linear interpolation over sorted calibration points."""

    def __init__(self, points: list[Point]) -> None:
        ordered = sorted_points(points)
        if len(ordered) < 2:
            raise ValueError("a calibration curve needs at least two points")
        if any(point.reference < 0 for point in ordered):
            raise ValueError("reference concentration must be non-negative")
        if not is_monotonic(ordered):
            raise ValueError("calibration points must be monotonic in raw signal")
        self._points = ordered

    @property
    def points(self) -> list[Point]:
        return list(self._points)

    @property
    def raw_min(self) -> float:
        return self._points[0].raw

    @property
    def raw_max(self) -> float:
        return self._points[-1].raw

    @property
    def reference_min(self) -> float:
        return self._points[0].reference

    @property
    def reference_max(self) -> float:
        return self._points[-1].reference

    def coverage(self, instrument_span: float) -> float:
        """Fraction of the instrument raw span bracketed by calibration points."""

        if instrument_span <= 0:
            return 1.0
        return max(0.0, min(1.0, (self.raw_max - self.raw_min) / instrument_span))

    def convert(self, raw: float) -> Conversion:
        """Project a raw signal, flagging readings beyond the calibrated span."""

        if raw < self.raw_min:
            extrapolated = True
            first, second = self._points[0], self._points[1]
        elif raw > self.raw_max:
            extrapolated = True
            first, second = self._points[-2], self._points[-1]
        else:
            extrapolated = False
            first, second = self._bracket(raw)
        value = self._interpolate(first, second, raw)
        return Conversion(value=value, within=not extrapolated, extrapolated=extrapolated)

    def _bracket(self, raw: float) -> tuple[Point, Point]:
        for earlier, later in zip(self._points, self._points[1:]):
            if earlier.raw <= raw <= later.raw:
                return earlier, later
        return self._points[-2], self._points[-1]

    @staticmethod
    def _interpolate(first: Point, second: Point, raw: float) -> float:
        span = second.raw - first.raw
        if span == 0:
            return first.reference
        ratio = (raw - first.raw) / span
        return first.reference + ratio * (second.reference - first.reference)

    def as_dicts(self) -> list[dict[str, float]]:
        return [point.as_dict() for point in self._points]

    @classmethod
    def from_dicts(cls, items: list[object]) -> "Curve":
        points = [point for item in items if (point := parse_point(item)) is not None]
        return cls(points)

    @classmethod
    def from_factor(
        cls, factor: float, raw_min: float = 0.0, raw_max: float = 1_000_000.0
    ) -> "Curve":
        """Represent a legacy single factor as a two point line."""

        if factor <= 0:
            raise ValueError("calibration factor must be positive")
        return cls(
            [
                Point(raw=raw_min, reference=raw_min * factor),
                Point(raw=raw_max, reference=raw_max * factor),
            ]
        )


@dataclass(frozen=True)
class Qualification:
    """Result of checking whether candidate points may activate a version."""

    qualified: bool
    reasons: list[str]

    def as_dict(self) -> dict[str, object]:
        return {"qualified": self.qualified, "reasons": list(self.reasons)}


def qualify(
    points: list[Point],
    instrument_span: float,
    min_points: int = MIN_POINTS,
    min_coverage: float = MIN_COVERAGE,
) -> Qualification:
    """Enforce the agreed sample rule before a draft becomes authoritative.

    The rule, made explicit on purpose: a candidate must bracket at least
    ``min_points`` monotonic samples spanning most of the instrument range. A
    candidate that fails this check never activates; readings keep using the
    previous active version instead of a curve fitted from too few samples.
    """

    reasons: list[str] = []
    if len(points) < min_points:
        reasons.append(f"needs at least {min_points} calibration samples")
    if any(point.raw < 0 for point in points):
        reasons.append("raw signal must be non-negative")
    if any(point.reference < 0 for point in points):
        reasons.append("reference concentration must be non-negative")
    raws = [point.raw for point in sorted_points(points)]
    if len(set(raws)) != len(raws):
        reasons.append("raw signal samples must be distinct")
    if points and not is_monotonic(points):
        reasons.append("calibration points must be monotonic in raw signal")
    try:
        curve = Curve(points)
    except ValueError as exc:
        curve = None
        reasons.append(str(exc))
    if curve is not None:
        coverage = curve.coverage(instrument_span)
        if coverage < min_coverage:
            reasons.append(
                f"samples cover {coverage:.0%} of the range, need at least {min_coverage:.0%}"
            )
    return Qualification(qualified=not reasons, reasons=reasons)
