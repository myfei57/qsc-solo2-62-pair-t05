"""Tests for multi point calibration, versioning and migration policy."""

from __future__ import annotations

import tempfile
import unittest

from coldstore.audit import Auditor
from coldstore.calib import (
    DEFAULT_POINT,
    DEGRADED_QUALITY,
    DUAL_RUN,
    FALLBACK,
    NEW,
    NOMINAL,
    PRIMARY,
    SINGLE,
    STANDBY,
    SWITCHED,
    Curve,
    Point,
    ReadingService,
    Registry,
    bootstrap,
    qualify,
)
from coldstore.store import Store


def three_points() -> list[Point]:
    return [Point(0.0, 0.0), Point(25.0, 25.0), Point(50.0, 50.0)]


class CurveCase(unittest.TestCase):
    def test_interpolates_linearly_between_points(self) -> None:
        curve = Curve([Point(0, 0), Point(10, 20), Point(20, 30)])
        self.assertAlmostEqual(curve.convert(5.0).value, 10.0)
        self.assertTrue(curve.convert(5.0).within)
        self.assertAlmostEqual(curve.convert(15.0).value, 25.0)

    def test_extrapolation_is_flagged_not_trusted(self) -> None:
        curve = Curve([Point(0, 0), Point(10, 10)])
        conversion = curve.convert(12.0)
        self.assertTrue(conversion.extrapolated)
        self.assertFalse(conversion.within)
        self.assertAlmostEqual(conversion.value, 12.0)

    def test_rejects_non_monotonic_and_negative_reference(self) -> None:
        with self.assertRaises(ValueError):
            Curve([Point(0, 10), Point(10, 5)])
        with self.assertRaises(ValueError):
            Curve([Point(0, -1), Point(10, 10)])

    def test_single_factor_is_a_two_point_line(self) -> None:
        curve = Curve.from_factor(2.0, raw_max=100.0)
        self.assertAlmostEqual(curve.convert(7.0).value, 14.0)

    def test_qualification_needs_enough_spanning_samples(self) -> None:
        self.assertFalse(qualify([Point(0, 0), Point(10, 10)], 50.0).qualified)
        thin = qualify(three_points(), 100.0)
        self.assertFalse(thin.qualified)
        self.assertTrue(any("cover" in reason for reason in thin.reasons))
        self.assertTrue(qualify(three_points(), 50.0).qualified)


class RegistryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.registry = Registry(self.store)
        self.auditor = Auditor(self.store)
        self.service = ReadingService(self.registry, self.auditor)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def draft(self, basis: str = "range-50", raw_max: float = 50.0) -> str:
        version, reasons = self.registry.draft_version(
            DEFAULT_POINT, basis, 0.0, raw_max, three_points()
        )
        self.assertEqual(reasons, [])
        return version.id

    def test_bootstrap_freezes_legacy_factor_as_immutable_version(self) -> None:
        self.store.put("ammonia:calibration", "2.0")
        bootstrap(self.store)
        version = self.registry.active_version(DEFAULT_POINT)
        self.assertIsNotNone(version)
        self.assertEqual(version.id, "legacy-factor-v1")
        self.assertEqual(version.basis, "legacy")
        result = self.service.ingest(7.0).payload
        self.assertAlmostEqual(result["concentration"], 14.0)
        self.assertEqual(result["version_id"], "legacy-factor-v1")
        self.assertEqual(result["quality"], NOMINAL)
        # rebooting must not duplicate the frozen version
        bootstrap(self.store)
        self.assertEqual(len(self.registry.versions(DEFAULT_POINT)), 1)

    def test_unqualified_draft_never_activates_and_reads_keep_old_version(self) -> None:
        bootstrap(self.store)
        version, reasons = self.registry.draft_version(
            DEFAULT_POINT, "thin", 0.0, 50.0, [Point(0, 0), Point(10, 10)]
        )
        self.assertTrue(reasons)
        with self.assertRaises(ValueError):
            self.registry.activate_version(DEFAULT_POINT, version.id)
        result = self.service.ingest(3.0).payload
        self.assertEqual(result["version_id"], "legacy-factor-v1")

    def test_dual_run_keeps_old_basis_authoritative_and_shadows_new(self) -> None:
        bootstrap(self.store)
        new_id = self.draft()
        self.registry.register_probe(
            "new-standby", DEFAULT_POINT, 0.0, 50.0, STANDBY, new_id, "range-50"
        )
        self.registry.begin_migration(DEFAULT_POINT, new_id, tolerance_ppm=1.0, min_pairs=3)
        for raw in (10.0, 11.0, 12.0):
            payload = self.service.ingest(raw, shadow_raw=raw).payload
            self.assertTrue(payload["authoritative"])
            self.assertEqual(payload["basis"], "legacy")
            self.assertEqual(payload["migration"], DUAL_RUN)
            self.assertEqual(payload["shadow"]["basis"], "range-50")
        self.assertTrue(self.registry.gate_status(DEFAULT_POINT)["passed"])

    def test_gate_blocks_cutover_when_bases_disagree(self) -> None:
        self.store.put("ammonia:calibration", "2.0")
        bootstrap(self.store)
        new_id = self.draft()
        self.registry.register_probe(
            "new-standby", DEFAULT_POINT, 0.0, 50.0, STANDBY, new_id, "range-50"
        )
        self.registry.begin_migration(DEFAULT_POINT, new_id, tolerance_ppm=1.0, min_pairs=2)
        for raw in (10.0, 11.0):
            self.service.ingest(raw, shadow_raw=raw)
        self.assertFalse(self.registry.gate_status(DEFAULT_POINT)["passed"])
        with self.assertRaises(ValueError):
            self.registry.cutover(DEFAULT_POINT)

    def test_cutover_promotes_new_probe_and_retires_old_version(self) -> None:
        bootstrap(self.store)
        new_id = self.draft()
        self.registry.register_probe(
            "new-standby", DEFAULT_POINT, 0.0, 50.0, STANDBY, new_id, "range-50"
        )
        self.registry.begin_migration(DEFAULT_POINT, new_id, tolerance_ppm=1.0, min_pairs=3)
        for raw in (10.0, 11.0, 12.0):
            self.service.ingest(raw, shadow_raw=raw)
        self.registry.cutover(DEFAULT_POINT)
        state = self.registry.point_state(DEFAULT_POINT)
        self.assertEqual(state["migration"], NEW)
        self.assertEqual(state["authoritative_basis"], "range-50")
        new_probe = self.registry.probe("new-standby")
        self.assertEqual(new_probe.role, PRIMARY)
        payload = self.service.ingest(10.0).payload
        self.assertEqual(payload["basis"], "range-50")
        self.assertTrue(payload["authoritative"])
        self.assertEqual(
            [v.state for v in self.registry.versions(DEFAULT_POINT)], ["retired", "active"]
        )

    def test_rollback_returns_to_single_old_basis(self) -> None:
        bootstrap(self.store)
        new_id = self.draft()
        self.registry.register_probe(
            "new-standby", DEFAULT_POINT, 0.0, 50.0, STANDBY, new_id, "range-50"
        )
        self.registry.begin_migration(DEFAULT_POINT, new_id)
        state = self.registry.rollback_migration(DEFAULT_POINT)
        self.assertEqual(state["migration"], SINGLE)
        self.assertEqual(state["authoritative_basis"], "legacy")
        payload = self.service.ingest(10.0).payload
        self.assertNotIn("shadow", payload)

    def test_out_of_calibration_fails_over_to_same_basis_standby(self) -> None:
        bootstrap(self.store)
        new_id = self.draft()
        self.registry.register_probe(
            "new-standby", DEFAULT_POINT, 0.0, 50.0, STANDBY, new_id, "range-50"
        )
        self.registry.begin_migration(DEFAULT_POINT, new_id, tolerance_ppm=1.0, min_pairs=3)
        for raw in (10.0, 11.0, 12.0):
            self.service.ingest(raw, shadow_raw=raw)
        self.registry.cutover(DEFAULT_POINT)
        self.registry.mark_probe("new-standby", "out_of_calibration")
        # old standby is a different (retired) basis, so no eligible backup
        payload = self.service.ingest(5.0).payload
        self.assertEqual(payload["quality"], DEGRADED_QUALITY)
        self.assertFalse(payload["authoritative"])
        self.assertIsNone(payload["concentration"])

    def test_failover_uses_healthy_standby_of_same_basis(self) -> None:
        bootstrap(self.store)
        spare, _ = self.registry.draft_version(
            DEFAULT_POINT, "legacy-spare", 0.0, 1_000_000.0,
            [Point(0, 0), Point(500_000, 500_000), Point(1_000_000, 1_000_000)],
        )
        self.registry.activate_version(DEFAULT_POINT, spare.id)
        self.registry.register_probe(
            "spare", DEFAULT_POINT, 0.0, 1_000_000.0, STANDBY, spare.id, "legacy-spare"
        )
        self.registry.mark_probe("legacy-primary", "invalid")
        payload = self.service.ingest(9.0).payload
        self.assertEqual(payload["quality"], SWITCHED)
        self.assertEqual(payload["probe"], "spare")
        self.assertTrue(payload["authoritative"])
        self.assertEqual(self.registry.primary_probe(DEFAULT_POINT).id, "spare")

    def test_reading_outside_probe_range_triggers_failover_policy(self) -> None:
        bootstrap(self.store)
        new_id = self.draft()
        covered = self.registry.register_probe(
            "covered-standby", DEFAULT_POINT, 0.0, 50.0, STANDBY, new_id, "range-50"
        )
        # activate the new basis straight away and give it a narrow primary probe
        self.registry.activate_version(DEFAULT_POINT, new_id)
        narrow = self.registry.register_probe(
            "narrow-primary", DEFAULT_POINT, 0.0, 20.0, PRIMARY, new_id, "range-50"
        )
        self.registry.switch_probe_role(narrow.id, PRIMARY)
        self.assertEqual(covered.role, STANDBY)
        payload = self.service.ingest(40.0, probe_id="narrow-primary").payload
        self.assertEqual(payload["quality"], SWITCHED)
        self.assertEqual(payload["probe"], "covered-standby")

    def test_bound_inactive_version_falls_back_to_previous_active(self) -> None:
        bootstrap(self.store)
        new_id = self.draft()
        self.registry.register_probe(
            "draft-probe", DEFAULT_POINT, 0.0, 50.0, PRIMARY, new_id, "range-50"
        )
        payload = self.service.ingest(10.0, probe_id="draft-probe").payload
        self.assertEqual(payload["quality"], FALLBACK)
        self.assertEqual(payload["version_id"], "legacy-factor-v1")

    def test_audit_carries_concentration_basis_version_migration_quality(self) -> None:
        bootstrap(self.store)
        self.service.ingest(4.0)
        kinds = self.auditor.count_by_kind()
        self.assertIn("ammonia-reading", kinds)
        detail = [e for e in self.auditor.entries() if e.kind == "ammonia-reading"][-1].detail
        self.assertIn("ppm=4.0000", detail)
        self.assertIn("basis=legacy", detail)
        self.assertIn("version=legacy-factor-v1", detail)
        self.assertIn("migration=single", detail)
        self.assertIn("quality=nominal", detail)

    def test_applicable_ranges_list_every_version(self) -> None:
        bootstrap(self.store)
        self.draft()
        rows = self.registry.applicable_ranges(DEFAULT_POINT)
        self.assertEqual({row["basis"] for row in rows}, {"legacy", "range-50"})
        new_row = next(row for row in rows if row["basis"] == "range-50")
        self.assertEqual(new_row["instrument_range"], {"raw_min": 0.0, "raw_max": 50.0})
        self.assertEqual(new_row["calibrated_span"]["concentration_max"], 50.0)


if __name__ == "__main__":
    unittest.main()
