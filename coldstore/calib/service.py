"""Ingest path: convert a raw probe signal into an audited concentration.

This module owns the two policy choices that had to be picked once:

1. Insufficient samples: a draft never backs a live reading. When the bound
   calibration cannot be used the service falls back to the previous active
   version (quality ``fallback``); when no previous version exists it degrades
   the point rather than guessing.
2. Out of calibration: prefer failover to a healthy standby probe bound to the
   same basis (quality ``switched``). With no eligible standby the whole point
   degrades (quality ``degraded``): its readings are marked non authoritative
   and ``trip_on_degrade`` drives the safety interlock. There is deliberately no
   half mode where a drifting probe keeps issuing trusted concentrations.

During dual run every reading is converted on both bases; the old basis stays
authoritative and the new basis is recorded as ``shadow`` for the deviation
gate, so the concentration口径 stays single and consistent while migration is
under way.
"""

from __future__ import annotations

from dataclasses import dataclass

from coldstore.audit import Auditor

from .report import (
    DEGRADED,
    DEGRADED_QUALITY,
    FALLBACK,
    NOMINAL,
    PRIMARY,
    SWITCHED,
    validate_concentration,
)
from .versioning import DEFAULT_POINT, Probe, Registry, _now


@dataclass(frozen=True)
class IngestResult:
    """Outcome of ingesting one raw signal."""

    payload: dict[str, object]
    action: str
    reason: str

    @property
    def degraded(self) -> bool:
        return self.payload.get("quality") == DEGRADED_QUALITY


class ReadingService:
    """Converts raw signals, runs failover policy and registers audit rows."""

    def __init__(self, registry: Registry, auditor: Auditor) -> None:
        self._registry = registry
        self._auditor = auditor

    def ingest(
        self,
        raw: float,
        point: str = DEFAULT_POINT,
        probe_id: str = "",
        level: str = "ok",
        at: str | None = None,
        shadow_raw: float | None = None,
    ) -> IngestResult:
        if raw < 0:
            raise ValueError("raw signal must be non-negative")
        if shadow_raw is not None and shadow_raw < 0:
            raise ValueError("shadow raw signal must be non-negative")
        timestamp = at or _now()
        probe = self._select_probe(point, probe_id)
        if probe is None:
            return self._degrade(
                point, probe_id, raw, "no probe registered for point", timestamp, level
            )
        if probe.status in ("invalid", "out_of_calibration"):
            return self._handle_lost_probe(point, probe, raw, timestamp, level)
        if not (probe.raw_min <= raw <= probe.raw_max):
            return self._handle_lost_probe(
                point,
                probe,
                raw,
                timestamp,
                level,
                reason=f"raw {raw} outside probe range {probe.raw_min}..{probe.raw_max}",
            )
        return self._convert(point, probe, raw, NOMINAL, timestamp, level, shadow_raw)

    def _select_probe(self, point: str, probe_id: str) -> Probe | None:
        if probe_id:
            probe = self._registry.probe(probe_id)
            if probe is not None and probe.point == point:
                return probe
        return self._registry.primary_probe(point)

    def _handle_lost_probe(
        self, point: str, probe: Probe, raw: float, timestamp: str, level: str, reason: str = ""
    ) -> IngestResult:
        if not reason:
            reason = f"probe {probe.id} status={probe.status}"
        state = self._registry.point_state(point)
        basis = str(state.get("authoritative_basis") or probe.basis)
        standby = None
        for candidate in self._registry.standby_candidates(point, basis):
            if candidate.raw_min <= raw <= candidate.raw_max:
                standby = candidate
                break
        if standby is None:
            self._registry.switch_probe_role(probe.id, DEGRADED)
            self._auditor.record(
                "probe-degrade",
                f"point={point} probe={probe.id} reason={reason}",
            )
            return self._degrade(point, probe.id, raw, reason, timestamp, level)
        self._registry.switch_probe_role(standby.id, PRIMARY)
        self._auditor.record(
            "probe-failover",
            f"point={point} from={probe.id} to={standby.id} reason={reason}",
        )
        result = self._convert(point, standby, raw, SWITCHED, timestamp, level)
        return IngestResult(payload=result.payload, action="failover", reason=reason)

    def _degrade(
        self, point: str, probe_id: str, raw: float, reason: str, timestamp: str, level: str
    ) -> IngestResult:
        state = self._registry.point_state(point)
        payload = {
            "point": point,
            "probe": probe_id,
            "raw": raw,
            "concentration": None,
            "basis": state.get("authoritative_basis", ""),
            "version_id": state.get("authoritative_version", ""),
            "migration": state.get("migration", ""),
            "quality": DEGRADED_QUALITY,
            "authoritative": False,
            "within_span": False,
            "level": level,
            "at": timestamp,
        }
        self._registry.append_reading(point, payload)
        self._auditor.record(
            "reading-degraded",
            f"point={point} raw={raw:.4f} reason={reason}",
        )
        return IngestResult(payload=payload, action="degrade", reason=reason)

    def _convert(
        self,
        point: str,
        probe: Probe,
        raw: float,
        quality: str,
        timestamp: str,
        level: str,
        shadow_raw: float | None = None,
    ) -> IngestResult:
        state = self._registry.point_state(point)
        version = self._registry.version(point, probe.version_id) if probe.version_id else None
        fallback_reason = ""
        if version is None or version.state != "active":
            previous = self._registry.previous_active(point)
            if previous is None:
                return self._degrade(
                    point,
                    probe.id,
                    raw,
                    "no active calibration version for probe",
                    timestamp,
                    level,
                )
            version = previous
            if quality == NOMINAL:
                quality = FALLBACK
            fallback_reason = "bound version not active; using previous active version"
        conversion = version.curve.convert(raw)
        concentration = conversion.value
        validate_concentration(concentration)

        shadow: dict[str, object] | None = None
        if state.get("migration") == "dual_run":
            shadow_signal = raw if shadow_raw is None else shadow_raw
            shadow = self._shadow(point, state, shadow_signal)

        payload = {
            "point": point,
            "probe": probe.id,
            "raw": raw,
            "concentration": concentration,
            "basis": version.basis,
            "version_id": version.id,
            "migration": state.get("migration", "single"),
            "quality": quality,
            "authoritative": version.id == state.get("authoritative_version")
            and quality != DEGRADED_QUALITY,
            "within_span": conversion.within,
            "level": level,
            "at": timestamp,
        }
        if shadow is not None:
            payload["shadow"] = shadow
        if fallback_reason:
            payload["fallback_reason"] = fallback_reason
        self._registry.append_reading(point, payload)
        self._auditor.record(
            "ammonia-reading",
            " ".join(
                [
                    f"point={point}",
                    f"ppm={concentration:.4f}",
                    f"basis={version.basis}",
                    f"version={version.id}",
                    f"migration={state.get('migration', 'single')}",
                    f"quality={quality}",
                ]
            ),
        )
        return IngestResult(payload=payload, action="convert", reason=fallback_reason)

    def _shadow(self, point: str, state: dict[str, object], raw: float) -> dict[str, object]:
        new_version = self._registry.version(point, str(state["new_version"]))
        if new_version is None:
            return {}
        conversion = new_version.curve.convert(raw)
        return {
            "concentration": conversion.value,
            "basis": new_version.basis,
            "version_id": new_version.id,
            "within_span": conversion.within,
        }

    def should_trip(self, point: str = DEFAULT_POINT) -> tuple[bool, str]:
        """Whether degradation on this point must latch the safety interlock."""

        state = self._registry.point_state(point)
        if not state.get("trip_on_degrade", True):
            return False, ""
        for reading in reversed(self._registry.readings(point)):
            if reading.get("quality") == DEGRADED_QUALITY:
                return True, f"ammonia point {point} degraded: concentration untrusted"
            return False, ""
        return False, ""
