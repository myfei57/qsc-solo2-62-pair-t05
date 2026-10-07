"""Versioned calibration registry with migration state per monitoring point.

Three entities live here:

* ``CalVersion`` -- an immutable multi point calibration curve plus the
  instrument range it applies to. Versions move draft -> active -> retired and
  are never rewritten, so every stored reading keeps a version id that can be
  replayed forever.
* ``PointState`` -- per monitoring point migration state: which basis is
  authoritative and, during dual run, how the old and new bases compare.
* ``Probe`` -- a physical probe bound to a point, carrying its own range and a
  calibration version. Primary and standby roles drive failover.

The agreed policy is encoded in the methods, not left to callers:

* A draft only activates when its samples qualify; otherwise readings continue
  on the previous active version (``fallback`` quality). A draft is never used
  for live conversion.
* Migration is dual run first: both bases convert every reading, the old basis
  stays authoritative until the new basis passes the deviation gate, then one
  cutover retires the old version. Rollback returns to single basis on the old
  version.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

from coldstore.store.commands import append_command, load_commands
from coldstore.store.store import Store

from .curve import Curve, Point, parse_point, qualify
from .report import (
    ACTIVE,
    DEGRADED,
    DRAFT,
    DUAL_RUN,
    NEW,
    PRIMARY,
    RETIRED,
    SINGLE,
    STANDBY,
    VersionView,
    validate_range,
)

POINT_KEY = "calib:point:{point}"
VERSIONS_KEY = "calib:versions:{point}"
PROBES_KEY = "calib:probes"
READINGS_KEY = "calib:readings:{point}"
LEGACY_VERSION = "legacy-factor-v1"
LEGACY_BASIS = "legacy"
DEFAULT_POINT = "default"
MAX_READINGS = 512


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class CalVersion:
    """An immutable calibration version with a lifecycle state."""

    id: str
    point: str
    basis: str
    state: str
    raw_min: float
    raw_max: float
    points: list[Point] = field(default_factory=list)
    created_at: str = ""
    activated_at: str = ""
    retired_at: str = ""
    note: str = ""

    @property
    def curve(self) -> Curve:
        return Curve(self.points)

    def covers(self, raw: float) -> bool:
        return self.raw_min <= raw <= self.raw_max

    def applicability(self) -> dict[str, object]:
        """The range this version is valid for, as served by the listing API."""

        raws = [point.raw for point in self.points]
        references = [point.reference for point in self.points]
        try:
            curve = self.curve
            calibrated_span = {
                "raw_min": curve.raw_min,
                "raw_max": curve.raw_max,
                "concentration_min": curve.reference_min,
                "concentration_max": curve.reference_max,
            }
        except ValueError:
            # An unqualified draft (too few samples) is still listed for audit.
            calibrated_span = {
                "raw_min": min(raws) if raws else self.raw_min,
                "raw_max": max(raws) if raws else self.raw_min,
                "concentration_min": min(references) if references else 0.0,
                "concentration_max": max(references) if references else 0.0,
            }
        return {
            "point": self.point,
            "basis": self.basis,
            "state": self.state,
            "instrument_range": {"raw_min": self.raw_min, "raw_max": self.raw_max},
            "calibrated_span": calibrated_span,
            "sample_count": len(self.points),
            "samples": [point.as_dict() for point in self.points],
            "active_from": self.activated_at,
            "active_until": self.retired_at,
        }

    def view(self) -> VersionView:
        return VersionView(
            id=self.id,
            point=self.point,
            basis=self.basis,
            state=self.state,
            raw_min=self.raw_min,
            raw_max=self.raw_max,
            points=[point.as_dict() for point in self.points],
            created_at=self.created_at,
            activated_at=self.activated_at,
            retired_at=self.retired_at,
            note=self.note,
            applicable=self.applicability(),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "point": self.point,
            "basis": self.basis,
            "state": self.state,
            "raw_min": self.raw_min,
            "raw_max": self.raw_max,
            "points": [point.as_dict() for point in self.points],
            "created_at": self.created_at,
            "activated_at": self.activated_at,
            "retired_at": self.retired_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "CalVersion":
        return cls(
            id=str(payload.get("id", "")),
            point=str(payload.get("point", "")),
            basis=str(payload.get("basis", "")),
            state=str(payload.get("state", DRAFT)),
            raw_min=float(payload.get("raw_min", 0.0)),
            raw_max=float(payload.get("raw_max", 1_000_000.0)),
            points=[
                point
                for item in payload.get("points", [])
                if (point := parse_point(item)) is not None
            ]
            if isinstance(payload.get("points"), list)
            else [],
            created_at=str(payload.get("created_at", "")),
            activated_at=str(payload.get("activated_at", "")),
            retired_at=str(payload.get("retired_at", "")),
            note=str(payload.get("note", "")),
        )


@dataclass
class Probe:
    """A physical ammonia probe bound to one monitoring point."""

    id: str
    point: str
    role: str = STANDBY
    basis: str = ""
    version_id: str = ""
    raw_min: float = 0.0
    raw_max: float = 1_000_000.0
    status: str = "ok"

    @property
    def active(self) -> bool:
        return self.status == "ok"

    def view(self):
        from .report import ProbeView

        return ProbeView(
            id=self.id,
            point=self.point,
            role=self.role,
            basis=self.basis,
            version_id=self.version_id,
            raw_min=self.raw_min,
            raw_max=self.raw_max,
            status=self.status,
            active=self.active,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "point": self.point,
            "role": self.role,
            "basis": self.basis,
            "version_id": self.version_id,
            "raw_min": self.raw_min,
            "raw_max": self.raw_max,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Probe":
        return cls(
            id=str(payload.get("id", "")),
            point=str(payload.get("point", "")),
            role=str(payload.get("role", STANDBY)),
            basis=str(payload.get("basis", "")),
            version_id=str(payload.get("version_id", "")),
            raw_min=float(payload.get("raw_min", 0.0)),
            raw_max=float(payload.get("raw_max", 1_000_000.0)),
            status=str(payload.get("status", "ok")),
        )


class Registry:
    """Persists versions, probes, point migration state and recent readings."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # -- points -----------------------------------------------------------

    def ensure_point(self, point: str = DEFAULT_POINT) -> None:
        self._load_point(point)

    def point_state(self, point: str) -> dict[str, object]:
        return self._load_point(point)

    def point_ids(self) -> list[str]:
        return sorted(
            {
                key.split(":", 2)[2]
                for key in self._store.keys()
                if key.startswith("calib:point:")
            }
        )

    def _load_point(self, point: str) -> dict[str, object]:
        raw, present = self._store.get(POINT_KEY.format(point=point))
        if present:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {}
            if isinstance(parsed, dict):
                return parsed
        state = {
            "id": point,
            "migration": SINGLE,
            "authoritative_basis": "",
            "authoritative_version": "",
            "old_basis": "",
            "new_basis": "",
            "old_version": "",
            "new_version": "",
            "began_at": "",
            "cutover_at": "",
            "tolerance_ppm": 0.0,
            "tolerance_ratio": 0.10,
            "min_pairs": 5,
            "trip_on_degrade": True,
        }
        self._save_point(point, state)
        return state

    def _save_point(self, point: str, state: dict[str, object]) -> None:
        self._store.put(POINT_KEY.format(point=point), json.dumps(state, ensure_ascii=False))

    # -- versions ---------------------------------------------------------

    def versions(self, point: str) -> list[CalVersion]:
        versions: list[CalVersion] = []
        for item in load_commands(self._store, VERSIONS_KEY.format(point=point)):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if isinstance(payload, dict):
                versions.append(CalVersion.from_dict(payload))
        return versions

    def version(self, point: str, version_id: str) -> CalVersion | None:
        for candidate in self.versions(point):
            if candidate.id == version_id:
                return candidate
        return None

    def active_version(self, point: str) -> CalVersion | None:
        active = [version for version in self.versions(point) if version.state == ACTIVE]
        return active[-1] if active else None

    def previous_active(self, point: str) -> CalVersion | None:
        """The most recently retired version, used when a draft cannot qualify."""

        retired = [version for version in self.versions(point) if version.state == RETIRED]
        return retired[-1] if retired else self.active_version(point)

    def _save_versions(self, point: str, versions: list[CalVersion]) -> None:
        self._store.delete(VERSIONS_KEY.format(point=point))
        for version in versions:
            append_command(
                self._store,
                VERSIONS_KEY.format(point=point),
                json.dumps(version.as_dict(), ensure_ascii=False),
            )

    def draft_version(
        self,
        point: str,
        basis: str,
        raw_min: float,
        raw_max: float,
        points: list[Point],
        note: str = "",
    ) -> tuple[CalVersion, list[str]]:
        """Create a draft and try to qualify it.

        Returns the draft and the qualification reasons. A draft that fails
        qualification is stored for the record but is never activated; callers
        must keep serving the previous active version.
        """

        validate_range(raw_min, raw_max)
        if not basis:
            raise ValueError("basis is required")
        if basis == self._load_point(point).get("old_basis"):
            raise ValueError(f"basis {basis} is already retired on point {point}")
        qualification = qualify(points, raw_max - raw_min)
        version = CalVersion(
            id=f"cal-{uuid.uuid4().hex[:12]}",
            point=point,
            basis=basis,
            state=DRAFT,
            raw_min=raw_min,
            raw_max=raw_max,
            points=list(points),
            created_at=_now(),
            note=note,
        )
        versions = self.versions(point)
        versions.append(version)
        self._save_versions(point, versions)
        return version, qualification.reasons

    def activate_version(self, point: str, version_id: str) -> CalVersion:
        """Promote a qualified draft to active and retire the current version."""

        versions = self.versions(point)
        candidate = next((version for version in versions if version.id == version_id), None)
        if candidate is None:
            raise ValueError(f"version {version_id} not found for point {point}")
        if candidate.state != DRAFT:
            raise ValueError("only a draft version can be activated")
        qualification = qualify(candidate.points, candidate.raw_max - candidate.raw_min)
        if not qualification.qualified:
            raise ValueError("draft cannot activate: " + "; ".join(qualification.reasons))
        for version in versions:
            if version.state == ACTIVE:
                version.state = RETIRED
                version.retired_at = _now()
        candidate.state = ACTIVE
        candidate.activated_at = _now()
        self._save_versions(point, versions)
        point_state = self._load_point(point)
        if point_state["migration"] == SINGLE:
            point_state["authoritative_basis"] = candidate.basis
            point_state["authoritative_version"] = candidate.id
            self._save_point(point, point_state)
        return candidate

    def retire_version(self, point: str, version_id: str) -> CalVersion:
        versions = self.versions(point)
        candidate = next((version for version in versions if version.id == version_id), None)
        if candidate is None:
            raise ValueError(f"version {version_id} not found for point {point}")
        candidate.state = RETIRED
        candidate.retired_at = _now()
        self._save_versions(point, versions)
        return candidate

    def applicable_ranges(self, point: str | None = None) -> list[dict[str, object]]:
        """Applicable range of every calibration version, for the listing API."""

        points = [point] if point else self.point_ids()
        rows: list[dict[str, object]] = []
        for point_id in points:
            for version in self.versions(point_id):
                rows.append({"id": version.id, **version.applicability()})
        return rows

    # -- probes -----------------------------------------------------------

    def probes(self, point: str | None = None) -> list[Probe]:
        probes: list[Probe] = []
        for item in load_commands(self._store, PROBES_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if isinstance(payload, dict):
                probe = Probe.from_dict(payload)
                if point is None or probe.point == point:
                    probes.append(probe)
        return probes

    def probe(self, probe_id: str) -> Probe | None:
        for candidate in self.probes():
            if candidate.id == probe_id:
                return candidate
        return None

    def _save_probes(self, probes: list[Probe]) -> None:
        self._store.delete(PROBES_KEY)
        for probe in probes:
            append_command(
                self._store, PROBES_KEY, json.dumps(probe.as_dict(), ensure_ascii=False)
            )

    def register_probe(
        self,
        probe_id: str,
        point: str,
        raw_min: float,
        raw_max: float,
        role: str = STANDBY,
        version_id: str = "",
        basis: str = "",
    ) -> Probe:
        validate_range(raw_min, raw_max)
        if any(probe.id == probe_id for probe in self.probes()):
            raise ValueError(f"probe {probe_id} already registered")
        if version_id:
            version = self.version(point, version_id)
            if version is None:
                raise ValueError(f"version {version_id} not found for point {point}")
            basis = basis or version.basis
        probe = Probe(
            id=probe_id,
            point=point,
            role=role,
            basis=basis,
            version_id=version_id,
            raw_min=raw_min,
            raw_max=raw_max,
        )
        probes = self.probes()
        probes.append(probe)
        self._save_probes(probes)
        self.ensure_point(point)
        return probe

    def bind_probe(self, probe_id: str, version_id: str) -> Probe:
        probes = self.probes()
        probe = next((candidate for candidate in probes if candidate.id == probe_id), None)
        if probe is None:
            raise ValueError(f"probe {probe_id} not found")
        version = self.version(probe.point, version_id)
        if version is None:
            raise ValueError(f"version {version_id} not found for point {probe.point}")
        if not (version.raw_min <= probe.raw_min and version.raw_max >= probe.raw_max):
            raise ValueError("version range does not cover the probe instrument range")
        probe.version_id = version_id
        probe.basis = version.basis
        self._save_probes(probes)
        return probe

    def mark_probe(self, probe_id: str, status: str) -> Probe:
        if status not in ("ok", "invalid", "out_of_calibration"):
            raise ValueError("status must be ok, invalid or out_of_calibration")
        probes = self.probes()
        probe = next((candidate for candidate in probes if candidate.id == probe_id), None)
        if probe is None:
            raise ValueError(f"probe {probe_id} not found")
        probe.status = status
        self._save_probes(probes)
        return probe

    def switch_probe_role(self, probe_id: str, role: str) -> Probe:
        if role not in (PRIMARY, STANDBY, DEGRADED):
            raise ValueError("role must be primary, standby or degraded")
        probes = self.probes()
        probe = next((candidate for candidate in probes if candidate.id == probe_id), None)
        if probe is None:
            raise ValueError(f"probe {probe_id} not found")
        for candidate in probes:
            if candidate.point == probe.point and candidate.role == PRIMARY and candidate.id != probe_id:
                candidate.role = STANDBY
        probe.role = role
        self._save_probes(probes)
        return probe

    def primary_probe(self, point: str) -> Probe | None:
        for probe in self.probes(point):
            if probe.role == PRIMARY:
                return probe
        return None

    def standby_candidates(self, point: str, basis: str) -> list[Probe]:
        """Healthy standbys that speak the authoritative basis and span the point."""

        candidates: list[Probe] = []
        for probe in self.probes(point):
            if probe.role != STANDBY or probe.status != "ok" or probe.basis != basis:
                continue
            version = self.version(point, probe.version_id) if probe.version_id else None
            if version is not None and version.state == ACTIVE:
                candidates.append(probe)
        return candidates

    def _promote_basis_probe(self, point: str, basis: str) -> Probe | None:
        """Put the healthy probe of the newly authoritative basis on primary duty."""

        probes = self.probes(point)
        target = next(
            (
                probe
                for probe in probes
                if probe.point == point
                and probe.basis == basis
                and probe.status == "ok"
                and probe.version_id
            ),
            None,
        )
        if target is None:
            return None
        for probe in probes:
            if probe.point == point and probe.id != target.id:
                probe.role = STANDBY
        target.role = PRIMARY
        self._save_probes(probes)
        return target

    # -- readings ---------------------------------------------------------

    def readings(self, point: str) -> list[dict[str, object]]:
        readings: list[dict[str, object]] = []
        for item in load_commands(self._store, READINGS_KEY.format(point=point)):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if isinstance(payload, dict):
                readings.append(payload)
        return readings

    def append_reading(self, point: str, payload: dict[str, object]) -> None:
        key = READINGS_KEY.format(point=point)
        readings = self.readings(point)
        readings.append(payload)
        readings = readings[-MAX_READINGS:]
        self._store.delete(key)
        for reading in readings:
            append_command(self._store, key, json.dumps(reading, ensure_ascii=False))

    # -- migration --------------------------------------------------------

    def begin_migration(
        self,
        point: str,
        new_version_id: str,
        tolerance_ppm: float = 0.0,
        tolerance_ratio: float = 0.10,
        min_pairs: int = 5,
    ) -> dict[str, object]:
        """Start dual run: old basis stays authoritative, new basis shadows it."""

        old = self.active_version(point)
        new = self.version(point, new_version_id)
        if old is None:
            raise ValueError(f"point {point} has no active version")
        if new is None:
            raise ValueError(f"version {new_version_id} not found for point {point}")
        if new.state != DRAFT:
            raise ValueError("migration target must be a draft version")
        if new.basis == old.basis:
            raise ValueError("new version must declare a different basis")
        if tolerance_ppm < 0 or tolerance_ratio < 0:
            raise ValueError("tolerances must be non-negative")
        if min_pairs < 1:
            raise ValueError("min_pairs must be positive")
        state = self._load_point(point)
        state.update(
            {
                "migration": DUAL_RUN,
                "authoritative_basis": old.basis,
                "authoritative_version": old.id,
                "old_basis": old.basis,
                "new_basis": new.basis,
                "old_version": old.id,
                "new_version": new.id,
                "began_at": _now(),
                "cutover_at": "",
                "tolerance_ppm": tolerance_ppm,
                "tolerance_ratio": tolerance_ratio,
                "min_pairs": min_pairs,
            }
        )
        self._save_point(point, state)
        return state

    def cutover(self, point: str, force: bool = False) -> dict[str, object]:
        """Promote the new basis once the shadow readings pass the deviation gate."""

        state = self._load_point(point)
        if state["migration"] != DUAL_RUN:
            raise ValueError("point is not in dual run")
        pairs = self.comparison_pairs(point)
        passed, reasons = self._gate(state, pairs)
        if not passed and not force:
            raise ValueError("new basis failed the deviation gate: " + "; ".join(reasons))
        self.activate_version(point, str(state["new_version"]))
        self.retire_version(point, str(state["old_version"]))
        self._promote_basis_probe(point, str(state["new_basis"]))
        state = self._load_point(point)
        state.update(
            {
                "migration": NEW,
                "authoritative_basis": state["new_basis"],
                "authoritative_version": state["new_version"],
                "cutover_at": _now(),
            }
        )
        self._save_point(point, state)
        return state

    def rollback_migration(self, point: str) -> dict[str, object]:
        """Abandon the new basis and return to single basis on the old version."""

        state = self._load_point(point)
        if state["migration"] != DUAL_RUN:
            raise ValueError("point is not in dual run")
        new_version = self.version(point, str(state["new_version"]))
        if new_version is not None and new_version.state == DRAFT:
            self.retire_version(point, new_version.id)
        state.update(
            {
                "migration": SINGLE,
                "authoritative_basis": state["old_basis"],
                "authoritative_version": state["old_version"],
                "new_basis": "",
                "new_version": "",
                "cutover_at": "",
            }
        )
        self._save_point(point, state)
        return state

    def comparison_pairs(self, point: str) -> list[dict[str, float]]:
        """Old/new concentration pairs captured while dual running."""

        pairs: list[dict[str, float]] = []
        for reading in self.readings(point):
            shadow = reading.get("shadow")
            if isinstance(shadow, dict):
                pairs.append(
                    {
                        "authoritative": float(reading.get("concentration", 0.0)),
                        "shadow": float(shadow.get("concentration", 0.0)),
                    }
                )
        return pairs

    def _gate(
        self, state: dict[str, object], pairs: list[dict[str, float]]
    ) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        min_pairs = int(state.get("min_pairs", 5))
        if len(pairs) < min_pairs:
            reasons.append(f"need {min_pairs} comparison pairs, have {len(pairs)}")
        tolerance_ppm = float(state.get("tolerance_ppm", 0.0))
        tolerance_ratio = float(state.get("tolerance_ratio", 0.10))
        breaches = 0
        max_ratio = 0.0
        for pair in pairs:
            old_value = pair["authoritative"]
            new_value = pair["shadow"]
            allowed = tolerance_ppm + tolerance_ratio * max(old_value, 1e-9)
            delta = abs(new_value - old_value)
            if old_value > 0:
                max_ratio = max(max_ratio, delta / old_value)
            if delta > allowed:
                breaches += 1
        if breaches:
            reasons.append(f"{breaches}/{len(pairs)} pairs exceed the agreed tolerance")
        return not reasons, reasons

    def gate_status(self, point: str) -> dict[str, object]:
        state = self._load_point(point)
        pairs = self.comparison_pairs(point)
        passed, reasons = self._gate(state, pairs)
        return {
            "point": point,
            "migration": state["migration"],
            "pairs": len(pairs),
            "min_pairs": state.get("min_pairs", 5),
            "tolerance_ppm": state.get("tolerance_ppm", 0.0),
            "tolerance_ratio": state.get("tolerance_ratio", 0.10),
            "passed": passed,
            "reasons": reasons,
        }

    def state_view(self, point: str = DEFAULT_POINT) -> dict[str, object]:
        state = self._load_point(point)
        active = self.active_version(point)
        return {
            **state,
            "active_version": active.view().as_dict() if active else None,
            "probe": self.primary_probe(point).view().as_dict()
            if self.primary_probe(point)
            else None,
            "gate": self.gate_status(point),
        }
