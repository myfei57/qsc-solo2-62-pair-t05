"""HTTP handlers for every console endpoint."""

from __future__ import annotations

from typing import TYPE_CHECKING

from coldstore.ammonia import validate_ppm, validate_threshold
from coldstore.ammonia.monitor import LATCH_KEY
from coldstore.ammonia.monitor import level_for as ammonia_level_for
from coldstore.calib import DEFAULT_POINT
from coldstore.calib.curve import parse_point
from coldstore.comp import validate_stage
from coldstore.evap import validate_baseline
from coldstore.fan import PRIMARY, STANDBY
from coldstore.load import validate_baseline as validate_load_baseline
from coldstore.ns import Stage, control_line
from coldstore.quota import Quota, check_quota, validate_amount
from coldstore.reporting import audit_export, telemetry_export, to_csv
from coldstore.room import validate_zone
from coldstore.scheduler import validate_threshold as validate_schedule_threshold
from coldstore.store import export_state
from coldstore.temp import validate_temp
from coldstore.valve import OPEN, validate_valve

from . import checks, history, ops
from . import describe as describe_module
from . import simulate as simulate_module
from . import snapshot as snapshot_module
from . import telemetry as telemetry_module
from .cycle import run_cycle
from .http import Request, Response, csv_response, json_response, text_response
from .report import text_report
from .routes import route_table
from .system import system_payload
from .version import version_payload

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server


def _fan_set(server: "Server", request: Request):
    """Resolve the cold air group or the evaporative group by name."""

    group = request.str_field("group", "cold")
    if group == "evap":
        return server.runtime.evap_fans
    if group != "cold":
        raise ValueError("group must be cold or evap")
    return server.runtime.cold_fans


def health(server: "Server", request: Request) -> Response:
    store = server.runtime.store
    return json_response(
        {
            "status": "ok",
            "keys": store.keys(),
            "size": store.count(),
            "path": store.path,
            "data": export_state(store),
        }
    )


def health_checks(server: "Server", request: Request) -> Response:
    return json_response({"checks": checks.run_checks(server.runtime)})


def snapshot(server: "Server", request: Request) -> Response:
    return json_response(snapshot_module.collect(server.runtime))


def describe(server: "Server", request: Request) -> Response:
    return json_response(describe_module.collect(server.runtime))


def report(server: "Server", request: Request) -> Response:
    return text_response(text_report(server.runtime))


def pipeline(server: "Server", request: Request) -> Response:
    line = control_line()
    last = line.last()
    return json_response(
        {
            "name": line.name,
            "steps": [step.as_dict() for step in line.steps()],
            "ordered": line.before(Stage.TEMP, Stage.VALVE),
            "last_stage": "" if last is None else last.value,
        }
    )


def catalog(server: "Server", request: Request) -> Response:
    return json_response(
        {
            "routes": [route.as_dict() for route in route_table()],
            "paths": server.router.paths(),
        }
    )


def ops_counters(server: "Server", request: Request) -> Response:
    return json_response(ops.collect(server.runtime).as_dict())


def ops_reset_store(server: "Server", request: Request) -> Response:
    return json_response(ops.reset_store(server.runtime))


def telemetry(server: "Server", request: Request) -> Response:
    return json_response(telemetry_module.collect(server.runtime).as_dict())


def version(server: "Server", request: Request) -> Response:
    return json_response(version_payload())


def system(server: "Server", request: Request) -> Response:
    return json_response(system_payload())


def history_append(server: "Server", request: Request) -> Response:
    return history.append(server.runtime.store, request)


def history_list(server: "Server", request: Request) -> Response:
    return history.listing(server.runtime.store)


def history_clear(server: "Server", request: Request) -> Response:
    return history.clear(server.runtime.store)


def metrics(server: "Server", request: Request) -> Response:
    return json_response(server.metrics.snapshot().as_dict())


def metrics_reset(server: "Server", request: Request) -> Response:
    server.metrics.reset()
    return json_response({"reset": True})


def room_add(server: "Server", request: Request) -> Response:
    zone = request.int_field("zone")
    validate_zone(zone)
    room = server.runtime.rooms.add_room(
        request.str_field("id"), zone, request.float_field("temp")
    )
    return json_response({"id": room.id, "zone": room.zone, "temp": room.temp})


def room_remove(server: "Server", request: Request) -> Response:
    room_id = request.str_field("id")
    server.runtime.rooms.remove_room(room_id)
    return json_response({"removed": room_id})


def room_assign(server: "Server", request: Request) -> Response:
    room_id = request.str_field("id")
    compressor = request.str_field("compressor")
    server.runtime.rooms.assign_compressor(room_id, compressor)
    return json_response({"id": room_id, "compressor": compressor})


def room_sensor(server: "Server", request: Request) -> Response:
    room_id = request.str_field("id")
    sensor = request.str_field("sensor")
    server.runtime.rooms.assign_sensor(room_id, sensor)
    return json_response({"id": room_id, "sensor": sensor})


def room_renumber(server: "Server", request: Request) -> Response:
    zone = request.int_field("zone")
    validate_zone(zone)
    room_id = request.str_field("id")
    server.runtime.rooms.renumber(room_id, zone)
    return json_response({"renumbered": room_id, "zone": zone})


def room_temp_set(server: "Server", request: Request) -> Response:
    room_id = request.str_field("id")
    temp = request.float_field("temp")
    validate_temp(temp)
    server.runtime.rooms.set_temp(room_id, temp)
    return json_response({"id": room_id, "temp": temp})


def room_temp_get(server: "Server", request: Request) -> Response:
    room_id = request.str_field("id")
    value, present = server.runtime.rooms.temp(room_id)
    return json_response({"id": room_id, "temp": value, "ok": present})


def room_lookup(server: "Server", request: Request) -> Response:
    room_id = request.str_field("id")
    rooms = server.runtime.rooms
    zone, present = rooms.room_zone(room_id)
    compressor, sensor, assigned = rooms.assignment(room_id)
    room = rooms.room(room_id)
    return json_response(
        {
            "id": room_id,
            "zone": zone,
            "ok": present,
            "compressor": compressor,
            "sensor": sensor,
            "assigned": assigned,
            "room": room.snapshot() if room else None,
        }
    )


def room_compressor(server: "Server", request: Request) -> Response:
    compressor = request.str_field("compressor")
    served = server.runtime.rooms.rooms_for_compressor(compressor)
    return json_response({"compressor": compressor, "rooms": served})


def room_reset(server: "Server", request: Request) -> Response:
    server.runtime.rooms.reset_defrosting()
    return json_response({"reset": True})


def rooms_list(server: "Server", request: Request) -> Response:
    rooms = server.runtime.rooms
    payload = rooms.state().as_dict()
    payload["ids"] = rooms.room_ids()
    payload["coldest"] = rooms.coldest()
    payload["unassigned"] = rooms.unassigned()
    return json_response(payload)


def temp_state(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.temp.state().as_dict())


def temp_read(server: "Server", request: Request) -> Response:
    value = request.float_field("value")
    validate_temp(value)
    return json_response(server.runtime.temp.record(value).as_dict())


def temp_verdict(server: "Server", request: Request) -> Response:
    collector = server.runtime.temp
    verdict = collector.verdict()
    payload = verdict.as_dict()
    payload["current"] = collector.current()
    payload["rate"] = collector.rate()
    payload["within"] = collector.within_window()
    return json_response(payload)


def temp_window(server: "Server", request: Request) -> Response:
    value = request.float_field("window")
    return json_response({"window": server.runtime.temp.set_window(value)})


def temp_readings(server: "Server", request: Request) -> Response:
    readings = server.runtime.temp.readings()
    return json_response(
        {"readings": [reading.as_dict() for reading in readings], "samples": len(readings)}
    )


def temp_reset(server: "Server", request: Request) -> Response:
    server.runtime.temp.reset()
    return json_response({"reset": True})


def comp_add(server: "Server", request: Request) -> Response:
    stages = request.int_field("stages", 1)
    if stages <= 0:
        raise ValueError("stages must be positive")
    compressor = server.runtime.compressors.add_compressor(request.str_field("id"), stages)
    return json_response({"id": compressor.id, "stages": compressor.stages})


def comp_remove(server: "Server", request: Request) -> Response:
    comp_id = request.str_field("id")
    server.runtime.compressors.remove(comp_id)
    return json_response({"removed": comp_id})


def comp_state(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.compressors.state().as_dict())


def comp_lookup(server: "Server", request: Request) -> Response:
    comp_id = request.str_field("id")
    compressor = server.runtime.compressors.compressor(comp_id)
    stage, present = server.runtime.compressors.stage(comp_id)
    return json_response(
        {"compressor": compressor.snapshot() if compressor else None, "stage": stage, "ok": present}
    )


def comp_stage(server: "Server", request: Request) -> Response:
    comp_id = request.str_field("id")
    stage = request.int_field("stage")
    validate_stage(stage)
    return json_response({"id": comp_id, "stage": server.runtime.compressors.set_stage(comp_id, stage)})


def comp_start(server: "Server", request: Request) -> Response:
    comp_id = request.str_field("id")
    server.runtime.compressors.start(comp_id)
    return json_response({"started": comp_id, "stage": server.runtime.compressors.stage(comp_id)[0]})


def comp_stop(server: "Server", request: Request) -> Response:
    comp_id = request.str_field("id")
    server.runtime.compressors.stop(comp_id)
    return json_response({"stopped": comp_id})


def comp_order(server: "Server", request: Request) -> Response:
    bank = server.runtime.compressors
    return json_response(
        {
            "order": bank.order(),
            "ids": bank.compressor_ids(),
            "running": bank.running_ids(),
            "load_ratio": bank.load_ratio(),
        }
    )


def comp_reserve(server: "Server", request: Request) -> Response:
    comp_id = request.str_field("id")
    room_id = request.str_field("room")
    bank = server.runtime.compressors
    bank.reserve(comp_id, room_id)
    return json_response({"id": comp_id, "room": bank.reserved_by(comp_id)})


def comp_release(server: "Server", request: Request) -> Response:
    comp_id = request.str_field("id")
    room_id = request.str_field("room")
    bank = server.runtime.compressors
    bank.release(comp_id, room_id)
    return json_response({"id": comp_id, "room": bank.reserved_by(comp_id)})


def comp_reservations(server: "Server", request: Request) -> Response:
    return json_response({"reservations": server.runtime.compressors.reservations()})


def valve_state(server: "Server", request: Request) -> Response:
    supply = server.runtime.supply
    return json_response({"opened": supply.opened_ids(), "state": supply.state().as_dict()})


def valve_open(server: "Server", request: Request) -> Response:
    comp_id = request.str_field("id")
    return json_response({"id": comp_id, "position": server.runtime.supply.open_valve(comp_id)})


def valve_close(server: "Server", request: Request) -> Response:
    comp_id = request.str_field("id")
    return json_response({"id": comp_id, "position": server.runtime.supply.close_valve(comp_id)})


def valve_position(server: "Server", request: Request) -> Response:
    comp_id = request.str_field("id")
    position = request.str_field("position")
    validate_valve(position)
    supply = server.runtime.supply
    if position == OPEN:
        supply.open_valve(comp_id)
    else:
        supply.close_valve(comp_id)
    return json_response(
        {"id": comp_id, "position": supply.position(comp_id), "open": supply.is_open(comp_id)}
    )


def valve_start(server: "Server", request: Request) -> Response:
    comp_id = request.str_field("id")
    supply = server.runtime.supply
    supply.start_with_supply(server.runtime.compressors, comp_id)
    return json_response({"started": comp_id, "open": supply.is_open(comp_id)})


def valve_stop(server: "Server", request: Request) -> Response:
    comp_id = request.str_field("id")
    supply = server.runtime.supply
    supply.stop_with_supply(server.runtime.compressors, comp_id)
    return json_response({"stopped": comp_id, "position": supply.position(comp_id)})


def fan_add(server: "Server", request: Request) -> Response:
    role = request.str_field("role", STANDBY)
    if role not in (PRIMARY, STANDBY):
        raise ValueError("role must be primary or standby")
    unit = _fan_set(server, request).add_fan(request.str_field("id"), role)
    return json_response({"id": unit.id, "role": unit.role})


def fan_state(server: "Server", request: Request) -> Response:
    fans = _fan_set(server, request)
    payload = fans.state().as_dict()
    payload["healthy_standby"] = fans.healthy_standby()
    return json_response(payload)


def fan_lookup(server: "Server", request: Request) -> Response:
    unit = _fan_set(server, request).fan(request.str_field("id"))
    return json_response({"fan": unit.snapshot() if unit else None, "ok": unit is not None})


def fan_start(server: "Server", request: Request) -> Response:
    fan_id = request.str_field("id")
    _fan_set(server, request).start(fan_id)
    return json_response({"started": fan_id})


def fan_stop(server: "Server", request: Request) -> Response:
    fan_id = request.str_field("id")
    _fan_set(server, request).stop(fan_id)
    return json_response({"stopped": fan_id})


def fan_switch(server: "Server", request: Request) -> Response:
    return json_response({"primary": _fan_set(server, request).switch_over(request.str_field("id"))})


def fan_fail(server: "Server", request: Request) -> Response:
    fan_id = request.str_field("id")
    _fan_set(server, request).fail(fan_id)
    return json_response({"failed": fan_id})


def fan_repair(server: "Server", request: Request) -> Response:
    fan_id = request.str_field("id")
    _fan_set(server, request).repair(fan_id)
    return json_response({"repaired": fan_id})


def fan_faults(server: "Server", request: Request) -> Response:
    fans = _fan_set(server, request)
    return json_response(
        {
            "faulted": fans.faulted_ids(),
            "running": fans.running_ids(),
            "ids": fans.fan_ids(),
            "count": fans.count(),
        }
    )


def evap_state(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.evap.state().as_dict())


def evap_baseline(server: "Server", request: Request) -> Response:
    value = request.float_field("baseline")
    validate_baseline(value)
    return json_response({"baseline": server.runtime.evap.set_baseline(value)})


def evap_temp(server: "Server", request: Request) -> Response:
    value = request.float_field("temperature")
    validate_baseline(value)
    condenser = server.runtime.evap
    condenser.set_temperature(value)
    return json_response({"temperature": value, "above_baseline": condenser.above_baseline()})


def evap_switch(server: "Server", request: Request) -> Response:
    return json_response({"duty": server.runtime.evap.switch_to_standby(request.str_field("id"))})


def defrost_state(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.defrost.state().as_dict())


def defrost_start(server: "Server", request: Request) -> Response:
    room_id = request.str_field("id")
    controller = server.runtime.defrost
    controller.start(room_id)
    return json_response({"started": room_id, "spilled": controller.spilled()})


def defrost_finish(server: "Server", request: Request) -> Response:
    room_id = request.str_field("id")
    server.runtime.defrost.finish(room_id)
    return json_response({"finished": room_id})


def defrost_enqueue(server: "Server", request: Request) -> Response:
    room_id = request.str_field("id")
    controller = server.runtime.defrost
    controller.enqueue(room_id)
    return json_response({"enqueued": room_id, "pending": controller.pending_count()})


def defrost_replay(server: "Server", request: Request) -> Response:
    return json_response({"replayed": server.runtime.defrost.replay()})


def defrost_recover(server: "Server", request: Request) -> Response:
    controller = server.runtime.defrost
    controller.recover()
    return json_response({"recovered": True, "last_warmup": controller.last_warmup()})


def defrost_select(server: "Server", request: Request) -> Response:
    zone = request.int_field("zone")
    return json_response({"room": server.runtime.defrost.select(zone), "zone": zone})


def ammonia_state(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.ammonia.state().as_dict())


def ammonia_read(server: "Server", request: Request) -> Response:
    ppm = request.float_field("ppm")
    validate_ppm(ppm)
    return json_response(server.runtime.ammonia.detect(ppm).as_dict())


def ammonia_verdict(server: "Server", request: Request) -> Response:
    monitor = server.runtime.ammonia
    last = monitor.last()
    return json_response(
        {
            "last": last,
            "calibrated": monitor.calibrated(last),
            "threshold": monitor.threshold(),
            "calibration": monitor.calibration(),
            "latched": monitor.latched(),
        }
    )


def ammonia_threshold(server: "Server", request: Request) -> Response:
    value = request.float_field("threshold")
    validate_threshold(value)
    return json_response({"threshold": server.runtime.ammonia.set_threshold(value)})


def ammonia_calibrate(server: "Server", request: Request) -> Response:
    return json_response({"calibration": server.runtime.ammonia.calibrate(request.float_field("factor"))})


def ammonia_reset(server: "Server", request: Request) -> Response:
    server.runtime.ammonia.reset()
    return json_response({"latched": server.runtime.ammonia.latched()})


def interlock_state(server: "Server", request: Request) -> Response:
    interlock = server.runtime.interlock
    payload = interlock.state().as_dict()
    last = interlock.last_trip()
    payload["last"] = last.as_dict() if last else None
    return json_response(payload)


def interlock_trip(server: "Server", request: Request) -> Response:
    interlock = server.runtime.interlock
    entry = interlock.trip(request.str_field("reason", "manual"))
    return json_response({"trip": entry.as_dict(), "tripped": interlock.is_tripped()})


def interlock_reset(server: "Server", request: Request) -> Response:
    interlock = server.runtime.interlock
    interlock.reset()
    return json_response({"tripped": interlock.is_tripped(), "reason": interlock.reason()})


def interlock_trips(server: "Server", request: Request) -> Response:
    interlock = server.runtime.interlock
    return json_response(
        {"trips": [trip.as_dict() for trip in interlock.trips()], "count": interlock.count()}
    )


def interlock_clear(server: "Server", request: Request) -> Response:
    interlock = server.runtime.interlock
    interlock.clear_log()
    return json_response({"count": interlock.count()})


def load_state(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.load.state(server.runtime.rooms).as_dict())


def load_baseline(server: "Server", request: Request) -> Response:
    value = request.float_field("baseline")
    validate_load_baseline(value)
    return json_response({"baseline": server.runtime.load.set_baseline(value)})


def load_coefficient(server: "Server", request: Request) -> Response:
    value = request.float_field("coefficient")
    return json_response({"coefficient": server.runtime.load.set_coefficient(value)})


def load_estimate(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.load.estimate(request.float_field("temp")).as_dict())


def quota_add(server: "Server", request: Request) -> Response:
    amount = request.float_field("amount")
    validate_amount(amount)
    return json_response({"value": server.runtime.accumulator.add(amount)})


def quota_get(server: "Server", request: Request) -> Response:
    return json_response({"value": server.runtime.accumulator.value()})


def quota_remaining(server: "Server", request: Request) -> Response:
    return json_response({"remaining": server.runtime.accumulator.remaining(request.float_field("limit"))})


def quota_check(server: "Server", request: Request) -> Response:
    quota = Quota(resource=request.str_field("resource"), limit=request.float_field("limit"))
    remaining, ok = check_quota(quota.resource, request.float_field("used"), quota.limit)
    return json_response({"remaining": remaining, "ok": ok})


def quota_reset(server: "Server", request: Request) -> Response:
    server.runtime.accumulator.reset()
    return json_response({"value": server.runtime.accumulator.value()})


def audit_list(server: "Server", request: Request) -> Response:
    return json_response({"entries": [entry.as_dict() for entry in server.runtime.auditor.entries()]})


def audit_summary(server: "Server", request: Request) -> Response:
    auditor = server.runtime.auditor
    last = auditor.last()
    return json_response(
        {
            "count": auditor.count(),
            "by_kind": auditor.count_by_kind(),
            "last": last.as_dict() if last else None,
        }
    )


def audit_filter(server: "Server", request: Request) -> Response:
    entries = server.runtime.auditor.filter(request.str_field("kind"))
    return json_response({"entries": [entry.as_dict() for entry in entries]})


def audit_totals(server: "Server", request: Request) -> Response:
    return json_response({"totals": server.runtime.auditor.totals()})


def _points_field(request: Request) -> list[Point]:
    raw_points = request.payload.get("points", [])
    if not isinstance(raw_points, list):
        raise ValueError("points must be a list of {raw, reference} pairs")
    points: list[Point] = []
    for item in raw_points:
        point = parse_point(item)
        if point is None:
            raise ValueError("every point needs numeric raw and reference values")
        points.append(point)
    return points


def calibration_state(server: "Server", request: Request) -> Response:
    point = request.str_field("point", DEFAULT_POINT)
    return json_response(server.runtime.registry.state_view(point))


def calibration_points(server: "Server", request: Request) -> Response:
    return json_response({"points": server.runtime.registry.point_ids()})


def calibration_point_add(server: "Server", request: Request) -> Response:
    point = request.str_field("point")
    if not point:
        raise ValueError("point is required")
    server.runtime.registry.ensure_point(point)
    return json_response({"point": point})


def calibration_versions(server: "Server", request: Request) -> Response:
    point = request.str_field("point", "")
    versions = server.runtime.registry.applicable_ranges(point or None)
    return json_response({"versions": versions, "count": len(versions)})


def calibration_draft(server: "Server", request: Request) -> Response:
    point = request.str_field("point", DEFAULT_POINT)
    basis = request.str_field("basis")
    raw_min = request.float_field("raw_min", 0.0)
    raw_max = request.float_field("raw_max")
    note = request.str_field("note", "")
    points = _points_field(request)
    draft, reasons = server.runtime.registry.draft_version(
        point, basis, raw_min, raw_max, points, note
    )
    payload = draft.view().as_dict()
    payload["qualified"] = not reasons
    payload["qualification"] = reasons
    server.runtime.auditor.record(
        "calibration-draft",
        f"point={point} version={draft.id} basis={basis} qualified={not reasons}",
    )
    return json_response(payload)


def calibration_activate(server: "Server", request: Request) -> Response:
    point = request.str_field("point", DEFAULT_POINT)
    version_id = request.str_field("version")
    version = server.runtime.registry.activate_version(point, version_id)
    server.runtime.auditor.record(
        "calibration-activate", f"point={point} version={version.id} basis={version.basis}"
    )
    return json_response(version.view().as_dict())


def calibration_gate(server: "Server", request: Request) -> Response:
    point = request.str_field("point", DEFAULT_POINT)
    return json_response(server.runtime.registry.gate_status(point))


def calibration_migrate(server: "Server", request: Request) -> Response:
    point = request.str_field("point", DEFAULT_POINT)
    version_id = request.str_field("version")
    state = server.runtime.registry.begin_migration(
        point,
        version_id,
        tolerance_ppm=request.float_field("tolerance_ppm", 0.0),
        tolerance_ratio=request.float_field("tolerance_ratio", 0.10),
        min_pairs=request.int_field("min_pairs", 5),
    )
    server.runtime.auditor.record(
        "migration-begin", f"point={point} to={version_id} state=dual_run"
    )
    return json_response(state)


def calibration_cutover(server: "Server", request: Request) -> Response:
    point = request.str_field("point", DEFAULT_POINT)
    force = request.str_field("force", "") in ("1", "true", "yes")
    state = server.runtime.registry.cutover(point, force=force)
    server.runtime.auditor.record(
        "migration-cutover",
        f"point={point} basis={state['authoritative_basis']} forced={force}",
    )
    return json_response(state)


def calibration_rollback(server: "Server", request: Request) -> Response:
    point = request.str_field("point", DEFAULT_POINT)
    state = server.runtime.registry.rollback_migration(point)
    server.runtime.auditor.record(
        "migration-rollback", f"point={point} basis={state['authoritative_basis']}"
    )
    return json_response(state)


def calibration_reading(server: "Server", request: Request) -> Response:
    point = request.str_field("point", DEFAULT_POINT)
    probe_id = request.str_field("probe", "")
    raw = request.float_field("raw")
    rt = server.runtime
    threshold = rt.ammonia.threshold()
    placeholder = 0.0
    level = ammonia_level_for(placeholder, threshold)
    result = rt.calibration.ingest(
        raw,
        point=point,
        probe_id=probe_id,
        level=level,
        shadow_raw=request.float_field("shadow_raw") if "shadow_raw" in request.payload else None,
    )
    payload = dict(result.payload)
    concentration = payload.get("concentration")
    if isinstance(concentration, (int, float)):
        payload["level"] = ammonia_level_for(float(concentration), threshold)
        payload["threshold"] = threshold
        if payload["level"] == "trip":
            rt.store.put(LATCH_KEY, "true")
            rt.interlock.trip(f"ammonia {float(concentration):.2f} ppm {payload['basis']}")
    should_trip, reason = rt.calibration.should_trip(point)
    payload["safety_trip"] = should_trip
    if should_trip and not rt.interlock.is_tripped():
        rt.interlock.trip(reason)
    payload["tripped"] = rt.interlock.is_tripped()
    payload["action"] = result.action
    return json_response(payload)


def calibration_readings(server: "Server", request: Request) -> Response:
    point = request.str_field("point", DEFAULT_POINT)
    readings = server.runtime.registry.readings(point)
    return json_response({"point": point, "readings": readings, "count": len(readings)})


def calibration_probe_add(server: "Server", request: Request) -> Response:
    point = request.str_field("point", DEFAULT_POINT)
    role = request.str_field("role", "standby")
    if role not in ("primary", "standby"):
        raise ValueError("role must be primary or standby")
    probe = server.runtime.registry.register_probe(
        probe_id=request.str_field("id"),
        point=point,
        raw_min=request.float_field("raw_min", 0.0),
        raw_max=request.float_field("raw_max"),
        role=role,
        version_id=request.str_field("version", ""),
        basis=request.str_field("basis", ""),
    )
    server.runtime.auditor.record(
        "probe-register",
        f"point={point} probe={probe.id} role={role} range={probe.raw_min}..{probe.raw_max}",
    )
    return json_response(probe.view().as_dict())


def calibration_probes(server: "Server", request: Request) -> Response:
    point = request.str_field("point", "")
    probes = server.runtime.registry.probes(point or None)
    return json_response({"probes": [probe.view().as_dict() for probe in probes]})


def calibration_probe_bind(server: "Server", request: Request) -> Response:
    probe = server.runtime.registry.bind_probe(
        request.str_field("id"), request.str_field("version")
    )
    server.runtime.auditor.record(
        "probe-bind", f"probe={probe.id} version={probe.version_id} basis={probe.basis}"
    )
    return json_response(probe.view().as_dict())


def calibration_probe_mark(server: "Server", request: Request) -> Response:
    probe = server.runtime.registry.mark_probe(
        request.str_field("id"), request.str_field("status", "out_of_calibration")
    )
    server.runtime.auditor.record(
        "probe-mark", f"probe={probe.id} status={probe.status}"
    )
    return json_response(probe.view().as_dict())


def schedule_state(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.scheduler.state(server.runtime.rooms).as_dict())


def schedule_threshold(server: "Server", request: Request) -> Response:
    value = request.float_field("threshold")
    validate_schedule_threshold(value)
    return json_response({"threshold": server.runtime.scheduler.set_threshold(value)})


def schedule_plan(server: "Server", request: Request) -> Response:
    scheduler = server.runtime.scheduler
    rooms = server.runtime.rooms
    return json_response(
        {"entries": [entry.as_dict() for entry in scheduler.plan(rooms)], "due": scheduler.due(rooms)}
    )


def export_audit(server: "Server", request: Request) -> Response:
    columns, rows = audit_export(server.runtime.auditor)
    return csv_response(to_csv(columns, rows))


def export_telemetry(server: "Server", request: Request) -> Response:
    columns, rows = telemetry_export(telemetry_module.collect(server.runtime))
    return csv_response(to_csv(columns, rows))


def cycle(server: "Server", request: Request) -> Response:
    return run_cycle(server.runtime, request)


def simulate(server: "Server", request: Request) -> Response:
    return simulate_module.run_simulation(server.runtime, request)
