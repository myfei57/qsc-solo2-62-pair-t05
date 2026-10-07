"""Behavioural tests for versioned calibration, ranges and migration."""

from __future__ import annotations

import tempfile
import unittest

from coldstore.audit import Auditor
from coldstore.calibration import (
    ACTIVE,
    CLAMP,
    DEGRADED,
    DRAFT,
    DUAL,
    FALLBACK,
    LEGACY,
    MIGRATED,
    NEAREST,
    PRIMARY,
    STANDBY,
    SUPERSEDED,
    CalibrationRegistry,
    CalPoint,
    applicability_text,
    interpolate,
    legacy_factor,
    validate_points,
    validate_range,
)
from coldstore.store import Store


class CurveCase(unittest.TestCase):
    def test_interpolation_and_span_policies(self) -> None:
        points = [CalPoint(0.0, 0.0), CalPoint(10.0, 8.0), CalPoint(100.0, 90.0)]
        self.assertEqual(interpolate(points, 50.0, CLAMP)[0], 44.44444444444444)
        self.assertFalse(interpolate(points, 50.0, CLAMP)[1])
        self.assertEqual(interpolate(points, 0.0, CLAMP), (0.0, False))

        value, out_of_span = interpolate(points, 300.0, CLAMP)
        self.assertEqual(value, 90.0)
        self.assertTrue(out_of_span)
        value, out_of_span = interpolate(points, -5.0, CLAMP)
        self.assertEqual(value, 0.0)
        self.assertTrue(out_of_span)

        value, out_of_span = interpolate(points, 200.0, NEAREST)
        # nearest segment slope (90-8)/(100-10), extended from 100
        self.assertAlmostEqual(value, 90.0 + (200.0 - 100.0) * (82.0 / 90.0))
        self.assertTrue(out_of_span)

    def test_single_point_is_a_factor_not_a_span(self) -> None:
        points = [CalPoint(2.0, 4.0)]
        self.assertEqual(interpolate(points, 10.0, CLAMP), (20.0, False))
        self.assertEqual(legacy_factor(points), 2.0)

    def test_point_validation(self) -> None:
        validate_points([CalPoint(0, 0), CalPoint(1, 1)])
        validate_range(0.0, 500.0)
        with self.assertRaises(ValueError):
            validate_points([])
        with self.assertRaises(ValueError):
            validate_points([CalPoint(1, 1), CalPoint(1, 2)])
        with self.assertRaises(ValueError):
            validate_points([CalPoint(2, 2), CalPoint(1, 1)])
        with self.assertRaises(ValueError):
            validate_range(10.0, 5.0)
        with self.assertRaises(ValueError):
            validate_points([CalPoint(0, -1.0)])


class VersionCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.registry = CalibrationRegistry(
            Store.open(f"{self._tmp.name}/state.json"),
            Auditor(Store.open(f"{self._tmp.name}/state.json")),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_baseline_version_is_bootstrapped(self) -> None:
        latest = self.registry.latest()
        self.assertEqual(latest.id, "legacy-v1")
        self.assertEqual(latest.status, ACTIVE)
        self.assertTrue(latest.sufficient)
        self.assertIn("trusted", applicability_text(latest))

    def test_create_activate_and_supersede(self) -> None:
        version = self.registry.create_version(
            "v2", [CalPoint(0, 0), CalPoint(10, 8)], range_max=500, min_samples=2
        )
        self.assertEqual(version.status, DRAFT)
        self.assertEqual(version.predecessor, "legacy-v1")
        self.assertTrue(version.sufficient)
        self.assertEqual(self.registry.version("v2").status, DRAFT)

        self.registry.activate("v2")
        self.assertEqual(self.registry.version("v2").status, ACTIVE)
        self.assertEqual({view.id for view in self.registry.active_versions()}, {"legacy-v1", "v2"})

        self.registry.supersede("legacy-v1")
        self.assertEqual(self.registry.version("legacy-v1").status, SUPERSEDED)
        with self.assertRaises(ValueError):
            self.registry.activate("legacy-v1")

    def test_insufficient_draft_is_flagged(self) -> None:
        version = self.registry.create_version(
            "v2", [CalPoint(2, 4)], range_max=500, min_samples=2
        )
        self.assertFalse(version.sufficient)
        self.assertIn("insufficient samples", version.applicability)

    def test_duplicate_and_unknown_version_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.create_version("legacy-v1", [CalPoint(1, 1)])
        with self.assertRaises(ValueError):
            self.registry.create_version("v2", [CalPoint(1, 1)], predecessor="missing")


class ProbeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        store = Store.open(f"{self._tmp.name}/state.json")
        self.auditor = Auditor(store)
        self.registry = CalibrationRegistry(store, self.auditor)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_register_and_lifecycle(self) -> None:
        self.registry.register_probe("p1", 0, 1000, role=PRIMARY)
        self.registry.register_probe("p2", 0, 500, role=STANDBY)
        self.assertEqual(self.registry.primary(), "p1")
        self.assertEqual(self.registry.healthy_standby(), "p2")
        probe = self.registry.probe("p2")
        self.assertEqual(probe.version, "legacy-v1")
        self.assertIn("installed range[0.0000..500.0000]", probe.applicability)

        self.registry.create_version("v2", [CalPoint(0, 0), CalPoint(10, 8)], range_max=500)
        self.registry.assign_version("p2", "v2")
        self.assertEqual(self.registry.probe("p2").version, "v2")

        self.registry.mark_lost("p1")
        self.registry.switch_over("p2")
        self.assertEqual(self.registry.primary(), "p2")
        with self.assertRaises(ValueError):
            self.registry.switch_over("p1")

        self.registry.repair("p1")
        self.assertEqual(self.registry.probe("p1").role, STANDBY)
        self.assertTrue(self.registry.probe("p1").healthy)

    def test_invalid_registration(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.register_probe("p", 0, -1)
        with self.assertRaises(ValueError):
            self.registry.register_probe("p", 0, 1000, role="odd")


class ConversionCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        store = Store.open(f"{self._tmp.name}/state.json")
        self.auditor = Auditor(store)
        self.registry = CalibrationRegistry(store, self.auditor)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reading_records_basis_and_version(self) -> None:
        self.registry.register_probe("p1", 0, 1000, role=PRIMARY)
        reading = self.registry.read(12.0, "p1")
        self.assertEqual(reading.concentration, 12.0)
        self.assertEqual(reading.basis, "legacy-v1")
        self.assertEqual(reading.version, "legacy-v1")
        self.assertTrue(reading.trusted)
        self.assertEqual(reading.mode, LEGACY)
        self.assertEqual(reading.flags, [])
        self.assertEqual(len(self.registry.readings()), 1)

    def test_insufficient_samples_falls_back_to_predecessor(self) -> None:
        self.registry.register_probe("p1", 0, 1000, role=PRIMARY)
        self.registry.create_version("v2", [CalPoint(2, 4)], range_max=500, min_samples=2)
        self.registry.assign_version("p1", "v2")

        self.assertEqual(self.registry.policies()["insufficient"], FALLBACK)
        reading = self.registry.read(10.0, "p1")
        self.assertEqual(reading.concentration, 10.0)
        self.assertEqual(reading.basis, "legacy-v1")
        self.assertIn("insufficient_fallback", reading.flags)

    def test_insufficient_samples_nearest_policy_is_untrusted(self) -> None:
        self.registry.register_probe("p1", 0, 1000, role=PRIMARY)
        self.registry.create_version("v2", [CalPoint(2, 4)], range_max=500, min_samples=2)
        self.registry.assign_version("p1", "v2")
        self.registry.set_policy("insufficient", NEAREST)

        reading = self.registry.read(10.0, "p1")
        self.assertEqual(reading.concentration, 20.0)
        self.assertEqual(reading.basis, "v2")
        self.assertFalse(reading.trusted)
        self.assertIn("insufficient", reading.flags)

    def test_out_of_span_is_clamped_and_flagged(self) -> None:
        self.registry.register_probe("p1", 0, 500, role=PRIMARY)
        self.registry.create_version(
            "v2", [CalPoint(0, 0), CalPoint(10, 8), CalPoint(100, 90)], range_max=500
        )
        self.registry.assign_version("p1", "v2")
        self.registry.activate("v2")
        reading = self.registry.read(300.0, "p1")
        self.assertEqual(reading.concentration, 90.0)
        self.assertIn("out_of_span", reading.flags)

    def test_reading_outside_probe_range_has_no_concentration(self) -> None:
        self.registry.register_probe("p1", 0, 100, role=PRIMARY)
        reading = self.registry.read(150.0, "p1")
        self.assertIsNone(reading.concentration)
        self.assertIn("out_of_range", reading.flags)
        self.assertFalse(reading.trusted)


class FailoverCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        store = Store.open(f"{self._tmp.name}/state.json")
        self.auditor = Auditor(store)
        self.registry = CalibrationRegistry(store, self.auditor)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_lost_primary_switches_to_standby(self) -> None:
        self.registry.register_probe("prim", 0, 1000, role=PRIMARY)
        self.registry.register_probe("bak", 0, 500, role=STANDBY)
        self.registry.mark_lost("prim")

        reading = self.registry.read(5.0, "prim")
        self.assertEqual(reading.source, "bak")
        self.assertEqual(reading.probe, "bak")
        self.assertIn("failover", reading.flags)
        self.assertEqual(reading.concentration, 5.0)

    def test_no_standby_degrades_whole_channel(self) -> None:
        self.registry.register_probe("prim", 0, 1000, role=PRIMARY)
        self.registry.mark_lost("prim")

        reading = self.registry.read(5.0, "prim")
        self.assertIsNone(reading.concentration)
        self.assertIn("degraded", reading.flags)

    def test_degraded_policy_skips_standby(self) -> None:
        self.registry.register_probe("prim", 0, 1000, role=PRIMARY)
        self.registry.register_probe("bak", 0, 500, role=STANDBY)
        self.registry.set_policy("failover", DEGRADED)
        self.registry.mark_lost("prim")

        reading = self.registry.read(5.0, "prim")
        self.assertIsNone(reading.concentration)
        self.assertIn("degraded", reading.flags)
        self.assertNotIn("failover", reading.flags)

    def test_policy_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.set_policy("failover", "maybe")
        with self.assertRaises(ValueError):
            self.registry.set_policy("span", "guess")
        with self.assertRaises(ValueError):
            self.registry.set_tolerance(0)


class MigrationCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        store = Store.open(f"{self._tmp.name}/state.json")
        self.auditor = Auditor(store)
        self.registry = CalibrationRegistry(store, self.auditor)
        self.registry.register_probe("old", 0, 1000, role=PRIMARY, version="legacy-v1")
        self.registry.register_probe("new", 0, 500, role=STANDBY)
        # new probe: factor 0.9 with two points so it is sufficient
        self.registry.create_version(
            "v2", [CalPoint(0, 0), CalPoint(100, 90)], range_max=500, min_samples=2
        )
        self.registry.assign_version("new", "v2")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _dual(self) -> None:
        self.registry.begin_dual("v2")

    def test_dual_run_reports_legacy_but_collects_both(self) -> None:
        self._dual()
        migration = self.registry.migration()
        self.assertEqual(migration["mode"], DUAL)
        self.assertEqual(migration["legacy_version"], "legacy-v1")
        self.assertEqual(migration["current_version"], "v2")

        reading = self.registry.read(100.0, "new")
        self.assertEqual(reading.concentration, 90.0)
        self.assertEqual(reading.legacy_concentration, 100.0)
        self.assertEqual(reading.mode, DUAL)
        cross = self.registry.cross_check()
        self.assertEqual(cross["samples"], 1)
        self.assertEqual(cross["max_diff"], 10.0)

    def test_promotion_gated_on_sample_count_and_tolerance(self) -> None:
        self._dual()
        self.registry.read(100.0, "new")
        with self.assertRaises(ValueError):
            self.registry.promote()  # only one of the required three samples

        for raw in (50.0, 20.0):
            self.registry.read(raw, "new")
        with self.assertRaises(ValueError):
            self.registry.promote()  # enough samples, diff exceeds tolerance

        self.registry.set_tolerance(10.0)
        migration = self.registry.promote()
        self.assertEqual(migration["mode"], MIGRATED)
        self.assertEqual(self.registry.version("legacy-v1").status, SUPERSEDED)

        reading = self.registry.read(100.0, "new")
        self.assertIsNone(reading.legacy_concentration)
        self.assertEqual(reading.basis, "v2")

    def test_old_probe_is_rebased_after_migration(self) -> None:
        self._dual()
        for raw in (100.0, 50.0, 20.0):
            self.registry.read(raw, "new")
        self.registry.set_tolerance(10.0)
        self.registry.promote()

        reading = self.registry.read(100.0, "old")
        self.assertEqual(reading.version, "legacy-v1")
        self.assertEqual(reading.converted_version, "v2")
        self.assertEqual(reading.basis, "v2")
        self.assertEqual(reading.concentration, 90.0)
        self.assertIn("rebased", reading.flags)

    def test_historical_readings_recompute_under_other_versions(self) -> None:
        self.registry.read(100.0, "old")
        rows = self.registry.recompute("v2")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stored_basis"], "legacy-v1")
        self.assertEqual(rows[0]["stored_concentration"], 100.0)
        self.assertEqual(rows[0]["recomputed"], 90.0)
        self.assertEqual(rows[0]["basis"], "v2")
        # the original reading is untouched
        self.assertEqual(self.registry.readings()[0].basis, "legacy-v1")

    def test_abort_dual_returns_to_legacy(self) -> None:
        self._dual()
        migration = self.registry.abort_dual()
        self.assertEqual(migration["mode"], LEGACY)
        self.assertEqual(migration["current_version"], "legacy-v1")
        self.assertEqual(self.registry.version("v2").status, DRAFT)

    def test_dual_only_starts_from_legacy(self) -> None:
        self._dual()
        with self.assertRaises(ValueError):
            self.registry.begin_dual("v2")


class AuditCase(unittest.TestCase):
    def test_basis_decisions_are_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            auditor = Auditor(store)
            registry = CalibrationRegistry(store, auditor)
            registry.register_probe("p1", 0, 1000, role=PRIMARY)
            registry.create_version("v2", [CalPoint(0, 0), CalPoint(10, 8)], range_max=500)
            registry.assign_version("p1", "v2")
            registry.begin_dual("v2")
            registry.read(5.0, "p1")
            registry.abort_dual()

            kinds = auditor.count_by_kind()
            self.assertIn("calibration.version_created", kinds)
            self.assertIn("calibration.migration_begin", kinds)
            self.assertIn("calibration.reading", kinds)
            self.assertIn("calibration.migration_aborted", kinds)
            reading_entries = auditor.filter("calibration.reading")
            self.assertIn("basis=", reading_entries[-1].detail)
            self.assertIn("mode=dual", reading_entries[-1].detail)


if __name__ == "__main__":
    unittest.main()
