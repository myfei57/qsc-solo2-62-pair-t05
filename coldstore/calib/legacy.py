"""Bootstrap the versioned registry from the legacy single factor world.

Old stores only hold ``ammonia:calibration`` -- one factor with no range. On
first boot that factor is frozen into an immutable ``legacy-factor-v1`` version
and bound to a default primary probe, so:

* historical readings keep an explicit, permanent basis and version id;
* the new multi range probes can migrate away from it through the normal dual
  run flow instead of a silent in place rewrite.

The bootstrap is idempotent and never overwrites a registry that already has
versions, so reboots and upgraded stores converge to the same state.
"""

from __future__ import annotations

import json

from coldstore.ammonia.monitor import CALIBRATION_KEY
from coldstore.store.commands import append_command
from coldstore.store.epoch import load_float
from coldstore.store.store import Store

from .curve import Point
from .report import ACTIVE, PRIMARY
from .versioning import (
    DEFAULT_POINT,
    LEGACY_BASIS,
    LEGACY_VERSION,
    POINT_KEY,
    Registry,
    CalVersion,
    _now,
)

LEGACY_RAW_MAX = 1_000_000.0
VERSIONS_KEY = "calib:versions:{point}"


def _legacy_version(point: str, factor: float) -> CalVersion:
    timestamp = _now()
    return CalVersion(
        id=LEGACY_VERSION,
        point=point,
        basis=LEGACY_BASIS,
        state=ACTIVE,
        raw_min=0.0,
        raw_max=LEGACY_RAW_MAX,
        points=[
            Point(raw=0.0, reference=0.0),
            Point(raw=LEGACY_RAW_MAX, reference=LEGACY_RAW_MAX * factor),
        ],
        created_at=timestamp,
        activated_at=timestamp,
        note="frozen legacy single calibration factor",
    )


def bootstrap(store: Store, point: str = DEFAULT_POINT) -> Registry:
    """Make sure the registry has at least the frozen legacy basis."""

    registry = Registry(store)
    registry.ensure_point(point)
    if registry.versions(point):
        return registry

    factor, present = load_float(store, CALIBRATION_KEY)
    if not present or factor <= 0:
        factor = 1.0
    append_command(
        store,
        VERSIONS_KEY.format(point=point),
        json.dumps(_legacy_version(point, factor).as_dict(), ensure_ascii=False),
    )

    state = registry.point_state(point)
    state.update(
        {
            "authoritative_basis": LEGACY_BASIS,
            "authoritative_version": LEGACY_VERSION,
        }
    )
    store.put(POINT_KEY.format(point=point), json.dumps(state, ensure_ascii=False))

    if not registry.probes(point):
        registry.register_probe(
            probe_id="legacy-primary",
            point=point,
            raw_min=0.0,
            raw_max=LEGACY_RAW_MAX,
            role=PRIMARY,
            version_id=LEGACY_VERSION,
            basis=LEGACY_BASIS,
        )
    return registry
