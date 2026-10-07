"""Versioned calibration, probe ranges and migration state.

The registry is the single source of truth for which calibration version a
reading was converted under, which probe supplied it, and whether the channel
is running on its legacy basis, running both bases in parallel, or has
migrated. Every basis-changing action is appended to the audit trail so the
口径 behind any concentration can be reconstructed.

Fixed口径 decisions (see module report for the vocabulary):

* a draft with fewer than ``min_samples`` calibrated points never converts a
  reading; the default ``fallback`` policy routes the reading to the previous
  active version, while ``nearest`` extends the nearest segment but stamps the
  reading ``insufficient`` and untrusted;
* readings outside the calibrated span are flagged ``out_of_span`` and, by
  default, clamped to the end-point reference;
* a probe outside its own range, or one marked失校, triggers the failover
  policy: switch to a healthy standby probe, else degrade the whole channel;
* readings carry the calibration version id, basis and migration mode in the
  reading log and in the audit trail.
"""

from __future__ import annotations

from typing import Any

from dataclasses import replace

from coldstore.audit import Auditor

from .curve import interpolate, legacy_factor
from .persistence import load_dict, load_list, now, save_dict, save_list
from .report import (
    ACTIVE,
    BASELINE_VERSION,
    CLAMP,
    DEFAULT_MIN_CROSS_SAMPLES,
    DEFAULT_MIN_SAMPLES,
    DEFAULT_RANGE_MAX,
    DEFAULT_RANGE_MIN,
    DEFAULT_TOLERANCE,
    DEGRADED,
    DRAFT,
    DUAL,
    FALLBACK,
    FAILOVER_POLICIES,
    INSUFFICIENT_POLICIES,
    LEGACY,
    MIGRATED,
    NEAREST,
    PRIMARY,
    ROLES,
    SPAN_POLICIES,
    STANDBY,
    SUPERSEDED,
    UNIT_PPM,
    CalPoint,
    ProbeView,
    ReadingView,
    VersionView,
    applicability_text,
    validate_min_samples,
    validate_points,
    validate_range,
    validate_tolerance,
)
from coldstore.store.store import Store

VERSIONS_KEY = "calibration:versions"
PROBES_KEY = "calibration:probes"
POLICIES_KEY = "calibration:policies"
MIGRATION_KEY = "calibration:migration"
READINGS_KEY = "calibration:readings"
CROSS_KEY = "calibration:cross"

DEFAULT_POLICIES = {
    "insufficient": FALLBACK,
    "span": CLAMP,
    "failover": STANDBY,
    "tolerance": DEFAULT_TOLERANCE,
    "min_cross_samples": DEFAULT_MIN_CROSS_SAMPLES,
}


def _point_from_dict(item: dict[str, Any]) -> CalPoint:
    return CalPoint(raw=float(item["raw"]), reference=float(item["reference"]))


class CalibrationRegistry:
    """Persisted calibration versions, probes, policies and migration."""

    def __init__(self, store: Store, auditor: Auditor | None = None) -> None:
        self._store = store
        self._auditor = auditor
        self._ensure_baseline()

    # -- audit ------------------------------------------------------------

    def _audit(self, kind: str, detail: dict[str, object]) -> None:
        if self._auditor is not None:
            self._auditor.record(kind, _detail_text(detail))

    # -- bootstrap --------------------------------------------------------

    def _ensure_baseline(self) -> None:
        """Create the legacy one-factor version and default policies once."""

        if not self._versions_raw():
            self._save_versions(
                [
                    {
                        "id": BASELINE_VERSION,
                        "created": now(),
                        "status": ACTIVE,
                        "range_min": DEFAULT_RANGE_MIN,
                        "range_max": DEFAULT_RANGE_MAX,
                        "unit": UNIT_PPM,
                        "points": [{"raw": 1.0, "reference": 1.0}],
                        "min_samples": 1,
                        "predecessor": "",
                        "factor": 1.0,
                    }
                ]
            )
        policies = load_dict(self._store, POLICIES_KEY)
        merged = {**DEFAULT_POLICIES, **policies}
        save_dict(self._store, POLICIES_KEY, merged)
        migration = load_dict(self._store, MIGRATION_KEY)
        if not migration:
            migration = {
                "mode": LEGACY,
                "legacy_version": BASELINE_VERSION,
                "current_version": BASELINE_VERSION,
                "started_at": "",
                "migrated_at": "",
            }
            save_dict(self._store, MIGRATION_KEY, migration)

    # -- versions ---------------------------------------------------------

    def _versions_raw(self) -> list[dict[str, Any]]:
        return load_list(self._store, VERSIONS_KEY)

    def _save_versions(self, versions: list[dict[str, Any]]) -> None:
        save_list(self._store, VERSIONS_KEY, versions)

    def _version_raw(self, version_id: str) -> dict[str, Any] | None:
        for version in self._versions_raw():
            if str(version.get("id")) == version_id:
                return version
        return None

    def _require_version(self, version_id: str) -> dict[str, Any]:
        version = self._version_raw(version_id)
        if version is None:
            raise ValueError(f"calibration version {version_id} not found")
        return version

    @staticmethod
    def _view(version: dict[str, Any]) -> VersionView:
        points = [_point_from_dict(item) for item in version["points"]]
        sample_count = len(points)
        min_samples = int(version.get("min_samples", DEFAULT_MIN_SAMPLES))
        view = VersionView(
            id=str(version["id"]),
            created=str(version.get("created", "")),
            status=str(version.get("status", DRAFT)),
            range_min=float(version["range_min"]),
            range_max=float(version["range_max"]),
            unit=str(version.get("unit", UNIT_PPM)),
            points=points,
            sample_count=sample_count,
            min_samples=min_samples,
            predecessor=str(version.get("predecessor", "")),
            sufficient=sample_count >= min_samples,
            applicability="",
        )
        return replace(view, applicability=applicability_text(view))

    def versions(self) -> list[VersionView]:
        """List every calibration version, oldest first."""

        return [self._view(version) for version in self._versions_raw()]

    def version(self, version_id: str) -> VersionView:
        return self._view(self._require_version(version_id))

    def active_versions(self) -> list[VersionView]:
        return [self._view(version) for version in self._versions_raw() if version.get("status") == ACTIVE]

    def latest(self) -> VersionView:
        return self._view(self._versions_raw()[-1])

    def create_version(
        self,
        version_id: str,
        points: list[CalPoint],
        range_min: float = DEFAULT_RANGE_MIN,
        range_max: float = DEFAULT_RANGE_MAX,
        unit: str = UNIT_PPM,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        predecessor: str = "",
    ) -> VersionView:
        """Register a new multi-point calibration as a draft."""

        if not version_id:
            raise ValueError("version id must not be empty")
        if self._version_raw(version_id) is not None:
            raise ValueError(f"calibration version {version_id} already exists")
        validate_range(range_min, range_max)
        validate_points(points)
        validate_min_samples(min_samples)
        parent = predecessor
        if not parent:
            parent = self.latest().id
        if parent and self._version_raw(parent) is None:
            raise ValueError(f"predecessor {parent} not found")

        versions = self._versions_raw()
        entry = {
            "id": version_id,
            "created": now(),
            "status": DRAFT,
            "range_min": range_min,
            "range_max": range_max,
            "unit": unit,
            "points": [point.as_dict() for point in points],
            "min_samples": min_samples,
            "predecessor": parent,
        }
        versions.append(entry)
        self._save_versions(versions)
        self._audit(
            "calibration.version_created",
            {
                "version": version_id,
                "predecessor": parent,
                "samples": len(points),
                "min_samples": min_samples,
                "range": f"{range_min}-{range_max}",
            },
        )
        return self._view(entry)

    def activate(self, version_id: str) -> VersionView:
        """Mark a draft active without retiring older versions (coexistence)."""

        versions = self._versions_raw()
        entry: dict[str, Any] | None = None
        for candidate in versions:
            if str(candidate.get("id")) == version_id:
                entry = candidate
                break
        if entry is None:
            raise ValueError(f"calibration version {version_id} not found")
        if entry["status"] == SUPERSEDED:
            raise ValueError(f"calibration version {version_id} is superseded")
        if entry["status"] == DRAFT:
            entry["status"] = ACTIVE
            self._save_versions(versions)
            self._audit("calibration.version_activated", {"version": version_id})
        return self._view(entry)

    def supersede(self, version_id: str) -> None:
        """Retire one version; its data stays for historical recomputation."""

        versions = self._versions_raw()
        changed = False
        for candidate in versions:
            if str(candidate.get("id")) == version_id:
                if candidate["status"] == DRAFT:
                    raise ValueError(f"cannot supersede draft {version_id}")
                candidate["status"] = SUPERSEDED
                changed = True
        if not changed:
            raise ValueError(f"calibration version {version_id} not found")
        self._save_versions(versions)
        self._audit("calibration.version_superseded", {"version": version_id})

    def applicability(self) -> list[dict[str, object]]:
        """One row per calibration describing the range it applies to."""

        rows: list[dict[str, object]] = []
        for view in self.versions():
            payload = view.as_dict()
            payload["applicability"] = applicability_text(view)
            rows.append(payload)
        return rows

    # -- probes -----------------------------------------------------------

    def _probes_raw(self) -> dict[str, dict[str, Any]]:
        probes = load_dict(self._store, PROBES_KEY)
        return {str(key): dict(value) for key, value in probes.items()}

    def _save_probes(self, probes: dict[str, dict[str, Any]]) -> None:
        save_dict(self._store, PROBES_KEY, probes)

    def register_probe(
        self,
        probe_id: str,
        range_min: float,
        range_max: float,
        role: str = STANDBY,
        version: str = "",
        unit: str = UNIT_PPM,
    ) -> ProbeView:
        """Add a physical probe with its measurement range and basis."""

        if not probe_id:
            raise ValueError("probe id must not be empty")
        if role not in ROLES:
            raise ValueError("role must be primary or standby")
        validate_range(range_min, range_max)
        probes = self._probes_raw()
        if probe_id in probes:
            raise ValueError(f"probe {probe_id} already registered")
        basis = version or self.migration()["current_version"]
        self._require_version(basis)
        probes[probe_id] = {
            "id": probe_id,
            "role": role,
            "healthy": True,
            "range_min": range_min,
            "range_max": range_max,
            "unit": unit,
            "version": basis,
        }
        self._save_probes(probes)
        self._audit(
            "calibration.probe_registered",
            {"probe": probe_id, "role": role, "range": f"{range_min}-{range_max}", "version": basis},
        )
        return self.probe(probe_id)

    def probe(self, probe_id: str) -> ProbeView:
        probe = self._probes_raw().get(probe_id)
        if probe is None:
            raise ValueError(f"probe {probe_id} not found")
        return self._probe_view(probe)

    def _probe_view(self, probe: dict[str, Any]) -> ProbeView:
        version_id = str(probe["version"])
        version = self._version_raw(version_id)
        if version:
            scope = (
                f"{applicability_text(self._view(version))} "
                f"installed range[{float(probe['range_min']):.4f}..{float(probe['range_max']):.4f}]"
            )
        else:
            scope = "version missing"
        return ProbeView(
            id=str(probe["id"]),
            role=str(probe.get("role", STANDBY)),
            healthy=bool(probe.get("healthy", True)),
            range_min=float(probe["range_min"]),
            range_max=float(probe["range_max"]),
            unit=str(probe.get("unit", UNIT_PPM)),
            version=version_id,
            applicability=scope,
        )

    def probes(self) -> list[ProbeView]:
        return [self._probe_view(probe) for probe in self._probes_raw().values()]

    def primary(self) -> str:
        for probe in self._probes_raw().values():
            if probe.get("role") == PRIMARY:
                return str(probe["id"])
        return ""

    def healthy_standby(self) -> str:
        for probe in self._probes_raw().values():
            if probe.get("role") == STANDBY and probe.get("healthy", True):
                return str(probe["id"])
        return ""

    def assign_version(self, probe_id: str, version_id: str) -> ProbeView:
        """Move a probe onto a calibration version (draft allowed for setup)."""

        self._require_version(version_id)
        probes = self._probes_raw()
        if probe_id not in probes:
            raise ValueError(f"probe {probe_id} not found")
        probes[probe_id]["version"] = version_id
        self._save_probes(probes)
        self._audit("calibration.probe_version", {"probe": probe_id, "version": version_id})
        return self.probe(probe_id)

    def mark_lost(self, probe_id: str) -> ProbeView:
        """Flag a probe失校: out of calibration or faulty."""

        probes = self._probes_raw()
        if probe_id not in probes:
            raise ValueError(f"probe {probe_id} not found")
        probes[probe_id]["healthy"] = False
        self._save_probes(probes)
        self._audit("calibration.probe_lost", {"probe": probe_id})
        return self.probe(probe_id)

    def repair(self, probe_id: str) -> ProbeView:
        """Clear the失校 flag; a repaired primary parks on standby."""

        probes = self._probes_raw()
        if probe_id not in probes:
            raise ValueError(f"probe {probe_id} not found")
        probes[probe_id]["healthy"] = True
        if probes[probe_id]["role"] == PRIMARY:
            probes[probe_id]["role"] = STANDBY
        self._save_probes(probes)
        self._audit("calibration.probe_repaired", {"probe": probe_id})
        return self.probe(probe_id)

    def switch_over(self, probe_id: str) -> str:
        """Make one healthy probe primary, demoting every other probe."""

        probes = self._probes_raw()
        if probe_id not in probes:
            raise ValueError(f"probe {probe_id} not found")
        if not probes[probe_id].get("healthy", True):
            raise ValueError(f"probe {probe_id} is 失校")
        for candidate in probes.values():
            candidate["role"] = STANDBY
        probes[probe_id]["role"] = PRIMARY
        self._save_probes(probes)
        self._audit("calibration.probe_switch", {"probe": probe_id})
        return probe_id

    # -- policies ---------------------------------------------------------

    def policies(self) -> dict[str, Any]:
        return dict(load_dict(self._store, POLICIES_KEY))

    def set_policy(self, name: str, value: str) -> dict[str, Any]:
        """Configure the insufficient-samples, span or failover口径."""

        allowed = {
            "insufficient": INSUFFICIENT_POLICIES,
            "span": SPAN_POLICIES,
            "failover": FAILOVER_POLICIES,
        }
        if name not in allowed:
            raise ValueError(f"unknown policy {name}")
        if value not in allowed[name]:
            raise ValueError(f"policy {name} must be one of {', '.join(allowed[name])}")
        policies = load_dict(self._store, POLICIES_KEY)
        policies[name] = value
        save_dict(self._store, POLICIES_KEY, policies)
        self._audit("calibration.policy", {"policy": name, "value": value})
        return policies

    def set_tolerance(self, tolerance: float) -> None:
        validate_tolerance(tolerance)
        policies = load_dict(self._store, POLICIES_KEY)
        policies["tolerance"] = tolerance
        save_dict(self._store, POLICIES_KEY, policies)
        self._audit("calibration.policy", {"policy": "tolerance", "value": tolerance})

    def set_min_cross_samples(self, count: int) -> None:
        validate_min_samples(count)
        policies = load_dict(self._store, POLICIES_KEY)
        policies["min_cross_samples"] = count
        save_dict(self._store, POLICIES_KEY, policies)
        self._audit("calibration.policy", {"policy": "min_cross_samples", "value": count})

    # -- migration --------------------------------------------------------

    def migration(self) -> dict[str, Any]:
        return dict(load_dict(self._store, MIGRATION_KEY))

    def begin_dual(self, new_version: str) -> dict[str, Any]:
        """Activate a new version while the legacy basis keeps reporting.

        Both versions stay active and every reading is converted under both;
        the reported concentration remains the legacy one until promotion, so
        the concentration口径 stays consistent through the migration.
        """

        migration = self.migration()
        if migration["mode"] != LEGACY:
            raise ValueError(f"dual run can only start from legacy mode (current={migration['mode']})")
        version = self.version(new_version)
        if version.status == SUPERSEDED:
            raise ValueError(f"version {new_version} is superseded")
        self.activate(new_version)
        migration.update(
            {
                "mode": DUAL,
                "current_version": new_version,
                "started_at": now(),
            }
        )
        save_dict(self._store, MIGRATION_KEY, migration)
        self._audit(
            "calibration.migration_begin",
            {"legacy": migration["legacy_version"], "new": new_version},
        )
        return migration

    def promote(self) -> dict[str, Any]:
        """Cut over to the new basis. Gate: enough agreeing cross-samples."""

        migration = self.migration()
        if migration["mode"] != DUAL:
            raise ValueError("nothing to promote outside dual mode")
        agreement = self.cross_check()
        if not agreement["ok"]:
            raise ValueError(
                f"cross check gate failed: {agreement['samples']} samples, "
                f"max diff {agreement['max_diff']:.4f} vs tolerance "
                f"{agreement['tolerance']:.4f}"
            )
        legacy_id = str(migration["legacy_version"])
        new_id = str(migration["current_version"])
        self.supersede(legacy_id)
        migration.update({"mode": MIGRATED, "migrated_at": now()})
        save_dict(self._store, MIGRATION_KEY, migration)
        self._audit(
            "calibration.migration_promoted",
            {"legacy": legacy_id, "new": new_id, "max_diff": agreement["max_diff"]},
        )
        return migration

    def abort_dual(self) -> dict[str, Any]:
        """Drop back to the legacy basis while retaining the new draft."""

        migration = self.migration()
        if migration["mode"] != DUAL:
            raise ValueError("not in dual mode")
        new_id = str(migration["current_version"])
        legacy_id = str(migration["legacy_version"])
        versions = self._versions_raw()
        for candidate in versions:
            if str(candidate.get("id")) == new_id:
                candidate["status"] = DRAFT
        self._save_versions(versions)
        migration.update({"mode": LEGACY, "current_version": legacy_id, "started_at": ""})
        save_dict(self._store, MIGRATION_KEY, migration)
        self._audit("calibration.migration_aborted", {"legacy": legacy_id, "new": new_id})
        return migration

    def cross_samples(self) -> list[dict[str, Any]]:
        return load_list(self._store, CROSS_KEY)

    def _record_cross(self, raw: float, legacy: float, current: float) -> None:
        samples = self.cross_samples()
        sample = {
            "at": now(),
            "raw": raw,
            "legacy": legacy,
            "current": current,
            "diff": abs(current - legacy),
        }
        samples.append(sample)
        save_list(self._store, CROSS_KEY, samples)

    def cross_check(self) -> dict[str, object]:
        """Evaluate dual-run agreement against tolerance and sample count."""

        samples = self.cross_samples()
        policies = self.policies()
        tolerance = float(policies["tolerance"])
        required = int(policies["min_cross_samples"])
        max_diff = max((float(item["diff"]) for item in samples), default=0.0)
        agreeing = sum(1 for item in samples if float(item["diff"]) <= tolerance)
        enough = len(samples) >= required
        ok = enough and max_diff <= tolerance
        return {
            "samples": len(samples),
            "required": required,
            "agreeing": agreeing,
            "max_diff": max_diff,
            "tolerance": tolerance,
            "enough": enough,
            "ok": ok,
        }

    # -- reading conversion ----------------------------------------------

    def _resolve_probe(self, probe_id: str, policies: dict[str, Any]) -> tuple[dict[str, Any], str, list[str]]:
        """Pick the probe that will supply the reading."""

        probes = self._probes_raw()
        flags: list[str] = []
        if probe_id:
            probe = probes.get(probe_id)
            if probe is None:
                raise ValueError(f"probe {probe_id} not found")
            if not probe.get("healthy", True):
                chosen, switched = self._failover(probes, probe, policies)
                if switched:
                    flags.append("failover")
                probe = chosen
            return probe, str(probe["id"]), flags

        primary_id = self.primary()
        primary = probes.get(primary_id) if primary_id else None
        if primary is not None and primary.get("healthy", True):
            return primary, str(primary["id"]), flags
        chosen, switched = self._failover(probes, primary, policies)
        if switched:
            flags.append("failover")
        return chosen, str(chosen["id"]) if chosen else "", flags

    @staticmethod
    def _failover(
        probes: dict[str, dict[str, Any]],
        failed: dict[str, Any] | None,
        policies: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Apply the configured失校 policy; degrade when no standby exists."""

        if policies.get("failover", STANDBY) == DEGRADED:
            raise _Degraded("channel degraded: failover policy is degraded")
        for candidate in probes.values():
            if candidate.get("role") == STANDBY and candidate.get("healthy", True):
                return candidate, True
        raise _Degraded("channel degraded: no healthy standby probe")

    def _convert(self, version: dict[str, Any], raw: float, policies: dict[str, Any]) -> tuple[float, str, bool, list[str]]:
        """Convert under one version, applying the sample/span口径."""

        flags: list[str] = []
        points = [_point_from_dict(item) for item in version["points"]]
        sufficient = len(points) >= int(version.get("min_samples", DEFAULT_MIN_SAMPLES))
        trusted = True
        basis = str(version["id"])

        if not sufficient:
            if policies.get("insufficient", FALLBACK) == NEAREST:
                value, out_of_span = interpolate(points, raw, NEAREST)
                flags.append("insufficient")
                if out_of_span:
                    flags.append("out_of_span")
                return value, basis, False, flags
            predecessor = str(version.get("predecessor", ""))
            if not predecessor:
                raise _Degraded(f"version {version['id']} has insufficient samples and no predecessor")
            flags.append("insufficient_fallback")
            parent = self._require_version(predecessor)
            value, parent_basis, trusted, parent_flags = self._convert(parent, raw, policies)
            return value, parent_basis, trusted, flags + parent_flags

        value, out_of_span = interpolate(points, raw, str(policies.get("span", CLAMP)))
        if out_of_span:
            flags.append("out_of_span")
        return value, basis, trusted, flags

    def read(self, raw: float, probe_id: str = "") -> ReadingView:
        """Convert one raw probe value under the migration and failover口径."""

        if raw < 0:
            raise ValueError("raw reading must be non-negative")
        policies = self.policies()
        migration = self.migration()
        mode = str(migration["mode"])
        legacy_id = str(migration["legacy_version"])
        current_id = str(migration["current_version"])
        flags: list[str] = []
        source = ""

        try:
            probe, source, flags = self._resolve_probe(probe_id, policies)
        except _Degraded as degraded:
            return self._store_reading(
                ReadingView(
                    at=now(),
                    probe=probe_id,
                    source="",
                    raw=raw,
                    concentration=None,
                    legacy_concentration=None,
                    unit=UNIT_PPM,
                    version="",
                    converted_version="",
                    basis="",
                    trusted=False,
                    mode=mode,
                    flags=["degraded", str(degraded)],
                )
            )

        bound_version = str(probe["version"])
        self._require_version(bound_version)
        if raw < float(probe["range_min"]) or raw > float(probe["range_max"]):
            flags.append("out_of_range")
            return self._store_reading(
                ReadingView(
                    at=now(),
                    probe=str(probe["id"]),
                    source=source,
                    raw=raw,
                    concentration=None,
                    legacy_concentration=None,
                    unit=str(probe.get("unit", UNIT_PPM)),
                    version=bound_version,
                    converted_version="",
                    basis="",
                    trusted=False,
                    mode=mode,
                    flags=flags,
                )
            )

        # A probe bound to a retired version is rebased onto the channel's
        # current basis; the reading records both the bound and the converting
        # version so historical口径 stays traceable.
        version_id = bound_version
        bound = self._version_raw(version_id)
        if bound is not None and bound.get("status") == SUPERSEDED:
            version_id = str(migration["current_version"])
            flags.append("rebased")
        version = self._require_version(version_id)
        try:
            concentration, basis, trusted, convert_flags = self._convert(version, raw, policies)
        except _Degraded as degraded:
            return self._store_reading(
                ReadingView(
                    at=now(),
                    probe=str(probe["id"]),
                    source=source,
                    raw=raw,
                    concentration=None,
                    legacy_concentration=None,
                    unit=str(probe.get("unit", UNIT_PPM)),
                    version=bound_version,
                    converted_version=version_id,
                    basis="",
                    trusted=False,
                    mode=mode,
                    flags=flags + ["degraded", str(degraded)],
                )
            )
        flags.extend(convert_flags)

        legacy_concentration: float | None = None
        if mode == DUAL and basis == current_id:
            legacy_version = self._version_raw(legacy_id)
            if legacy_version is not None:
                legacy_value, _, _, _ = self._convert(legacy_version, raw, policies)
                legacy_concentration = legacy_value
                self._record_cross(raw, legacy_value, concentration)
        elif mode == DUAL:
            # Probe still on the legacy basis: convert the raw under the new
            # version too, purely for the dual-run agreement evidence.
            current_version = self._version_raw(current_id)
            if current_version is not None and current_id != basis:
                current_value, _, _, _ = self._convert(current_version, raw, policies)
                legacy_concentration = concentration
                self._record_cross(raw, concentration, current_value)

        return self._store_reading(
            ReadingView(
                at=now(),
                probe=str(probe["id"]),
                source=source,
                raw=raw,
                concentration=concentration,
                legacy_concentration=legacy_concentration,
                unit=str(probe.get("unit", UNIT_PPM)),
                version=bound_version,
                converted_version=basis,
                basis=basis,
                trusted=trusted,
                mode=mode,
                flags=flags,
            )
        )

    def _store_reading(self, view: ReadingView) -> ReadingView:
        readings = load_list(self._store, READINGS_KEY)
        readings.append(view.as_dict())
        save_list(self._store, READINGS_KEY, readings)
        self._audit(
            "calibration.reading",
            {
                "probe": view.probe,
                "raw": view.raw,
                "concentration": view.concentration,
                "basis": view.basis,
                "bound_version": view.version,
                "converted_version": view.converted_version,
                "mode": view.mode,
                "trusted": view.trusted,
                "flags": ",".join(view.flags),
            },
        )
        return view

    def readings(self, limit: int | None = None) -> list[ReadingView]:
        items = load_list(self._store, READINGS_KEY)
        if limit is not None:
            items = items[-limit:]
        return [self._reading_view(item) for item in items]

    def recompute(self, version_id: str, limit: int | None = None) -> list[dict[str, object]]:
        """Re-evaluate stored raw readings under any retained version.

        This answers "旧数据按新口径是多少" without mutating any original
        reading; each row carries the stored value and the recomputed value.
        """

        version = self._require_version(version_id)
        policies = self.policies()
        rows: list[dict[str, object]] = []
        for item in load_list(self._store, READINGS_KEY)[-limit if limit else None :]:
            raw = float(item.get("raw", 0.0))
            try:
                value, basis, trusted, flags = self._convert(version, raw, policies)
                rows.append(
                    {
                        "at": item.get("at", ""),
                        "probe": item.get("probe", ""),
                        "raw": raw,
                        "stored_concentration": item.get("concentration"),
                        "stored_basis": item.get("basis", ""),
                        "recomputed": value,
                        "version": version_id,
                        "basis": basis,
                        "trusted": trusted,
                        "flags": flags,
                    }
                )
            except _Degraded:
                rows.append(
                    {
                        "at": item.get("at", ""),
                        "probe": item.get("probe", ""),
                        "raw": raw,
                        "stored_concentration": item.get("concentration"),
                        "stored_basis": item.get("basis", ""),
                        "recomputed": None,
                        "version": version_id,
                        "basis": "",
                        "trusted": False,
                        "flags": ["degraded"],
                    }
                )
        return rows

    @staticmethod
    def _reading_view(item: dict[str, Any]) -> ReadingView:
        return ReadingView(
            at=str(item.get("at", "")),
            probe=str(item.get("probe", "")),
            source=str(item.get("source", "")),
            raw=float(item.get("raw", 0.0)),
            concentration=item.get("concentration"),
            legacy_concentration=item.get("legacy_concentration"),
            unit=str(item.get("unit", UNIT_PPM)),
            version=str(item.get("version", "")),
            converted_version=str(item.get("converted_version", item.get("basis", ""))),
            basis=str(item.get("basis", "")),
            trusted=bool(item.get("trusted", False)),
            mode=str(item.get("mode", LEGACY)),
            flags=[str(flag) for flag in item.get("flags", [])],
        )

    # -- projection -------------------------------------------------------

    def state(self) -> dict[str, object]:
        migration = self.migration()
        legacy_version = self._require_version(migration["legacy_version"])
        legacy_points = [_point_from_dict(point) for point in legacy_version["points"]]
        return {
            "versions": [view.as_dict() for view in self.versions()],
            "probes": [probe.as_dict() for probe in self.probes()],
            "policies": self.policies(),
            "migration": migration,
            "cross_check": self.cross_check(),
            "primary": self.primary(),
            "healthy_standby": self.healthy_standby(),
            "readings": len(load_list(self._store, READINGS_KEY)),
            "legacy_factor": legacy_factor(legacy_points),
        }

    def describe(self) -> str:
        migration = self.migration()
        return (
            f"calibration versions={len(self.versions())} probes={len(self.probes())} "
            f"mode={migration['mode']} basis={migration['current_version']}"
        )


class _Degraded(Exception):
    """Internal signal that the channel must report no concentration."""


def _detail_text(detail: dict[str, object]) -> str:
    """Render structured audit detail in the auditor's string slot."""

    return " ".join(f"{key}={value}" for key, value in detail.items())
