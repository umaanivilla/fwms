# Floor view dashboard API: everything the live floor page needs in one call.

from __future__ import annotations

import random

import frappe

from fwms.events import emit

EVENT_FILTERS = [
	{"key": "all", "label": "All events"},
	{"key": "scans", "label": "Handheld scans only"},
	{"key": "Receiving & QC", "label": "Receiving & QC"},
	{"key": "Putaway & Storage", "label": "Putaway & storage"},
	{"key": "Orders & Picking", "label": "Orders & picking"},
	{"key": "Packing", "label": "Packing"},
	{"key": "Sort Load & Ship", "label": "Sort, load & ship"},
	{"key": "Inventory Control", "label": "Inventory control"},
	{"key": "Returns", "label": "Returns"},
	{"key": "Alerts & Supervisor", "label": "Alerts & supervisor"},
]


@frappe.whitelist()
def dashboard() -> dict:
	frappe.only_for(("FWMS Supervisor", "FWMS Operator", "FWMS Inventory Control", "System Manager"))

	zones = frappe.get_all(
		"FWMS Zone",
		fields=["zone_name", "zone_type", "map_x", "map_y", "map_w", "map_h", "color", "warehouse"],
		order_by="map_y asc",
	)
	docks = frappe.get_all(
		"FWMS Dock Door",
		fields=["name", "door_code", "zone", "direction", "status", "carrier", "trailer"],
		order_by="door_code",
	)
	workers = frappe.get_all(
		"FWMS Worker",
		filters={"active": 1},
		fields=["name", "worker_code", "person_name", "default_zone", "current_task", "shift", "tasks_completed", "scans_today"],
	)
	events = frappe.get_all(
		"FWMS Event Log",
		fields=["name", "category", "subtype", "severity", "is_scan", "message", "worker", "zone", "bin", "lpn",
				"reference_doctype", "reference_name", "creation"],
		order_by="creation desc",
		limit=60,
	)
	kpi = frappe.get_all(
		"FWMS KPI Snapshot",
		fields=["*"],
		order_by="snapshot_time desc",
		limit=1,
	)
	open_tasks = frappe.get_all(
		"FWMS Task",
		filters={"status": ("in", ("Open", "Assigned", "In Progress"))},
		fields=["name", "task_type", "status", "worker", "to_bin", "from_bin"],
	)
	bin_zone = {}
	for t in open_tasks:
		for b in (t.to_bin, t.from_bin):
			if b and b not in bin_zone:
				bin_zone[b] = frappe.db.get_value("FWMS Bin", b, "zone")
		t.zone = bin_zone.get(t.to_bin) or bin_zone.get(t.from_bin)
	settings = frappe.db.get_single_value("FWMS Settings", "release_mode")
	simulation = frappe.db.get_single_value("FWMS Settings", "simulation_mode")
	shift = _current_shift()

	return {
		"zones": zones,
		"docks": docks,
		"workers": workers,
		"events": events,
		"kpi": kpi[0] if kpi else None,
		"open_tasks": open_tasks,
		"release_mode": settings,
		"simulation_mode": bool(simulation),
		"shift": shift,
		"filters": EVENT_FILTERS,
	}


def _current_shift() -> dict | None:
	from frappe.utils import now_datetime

	now = now_datetime().time()
	shifts = frappe.get_all("FWMS Shift", fields=["shift_name", "start_time", "end_time"])
	for shift in shifts:
		try:
			start = shift.start_time
			end = shift.end_time
			if start <= end:
				if start <= now <= end:
					return shift
			else:  # overnight shift
				if now >= start or now <= end:
					return shift
		except TypeError:
			continue
	return None


@frappe.whitelist()
def simulate_tick() -> dict:
	"""Simulation mode: fabricate a plausible floor event for demos."""
	frappe.only_for(("FWMS Supervisor", "System Manager"))
	if not frappe.db.get_single_value("FWMS Settings", "simulation_mode"):
		frappe.throw("Simulation mode is off in FWMS Settings")

	workers = frappe.get_all("FWMS Worker", filters={"active": 1}, pluck="name")
	zones = frappe.get_all("FWMS Zone", pluck="zone_name")
	if not workers or not zones:
		frappe.throw("Seed workers/zones first")

	samples = [
		("Receiving & QC", "Pallet {n} checked in at dock"),
		("Putaway & Storage", "Tote {n} put away in Bulk"),
		("Orders & Picking", "Line {n} picked from Pick Faces"),
		("Packing", "Carton {n} sealed and labelled"),
		("Sort Load & Ship", "Carton {n} loaded to trailer"),
		("Inventory Control", "Bin {n} cycle count started"),
		("Returns", "RMA {n} received at returns dock"),
	]
	category, template = random.choice(samples)
	n = random.randint(100, 999)
	worker = random.choice(workers)
	zone = random.choice(zones)
	emit(
		category,
		template.format(n=n),
		subtype="simulated",
		is_scan=bool(random.getrandbits(1)),
		worker=worker,
		zone=zone,
		payload={"simulated": True},
	)
	return {"ok": True}
