"""Human readable component summaries."""

from __future__ import annotations

from coldstore.ns import control_line
from coldstore.store import describe_store

from .runtime import Runtime


def collect(rt: Runtime) -> dict[str, str]:
    """Return one description line per component."""

    return {
        "pipeline": control_line().describe(),
        "store": describe_store(rt.store),
        "rooms": rt.rooms.describe(),
        "compressors": rt.compressors.describe(),
        "supply": rt.supply.describe(),
        "temp": rt.temp.describe(),
        "ammonia": rt.ammonia.describe(),
        "interlock": rt.interlock.describe(),
        "cold_fans": rt.cold_fans.describe(),
        "evap": rt.evap.describe(),
        "defrost": rt.defrost.describe(),
        "load": rt.load.describe(rt.rooms),
        "quota": rt.accumulator.describe(),
        "audit": rt.auditor.describe(),
        "schedule": rt.scheduler.describe(rt.rooms),
        "calibration": _describe_calibration(rt),
    }


def _describe_calibration(rt: Runtime) -> str:
    points = rt.registry.point_ids()
    if not points:
        return "calibration points=0"
    parts = [f"points={len(points)}"]
    for point in points:
        state = rt.registry.state_view(point)
        probe = state.get("probe") or {}
        parts.append(
            f"{point}:{state['migration']}/"
            f"{state.get('authoritative_basis') or '-'} probe={probe.get('id') or '-'}"
        )
    return "calibration " + " ".join(parts)
