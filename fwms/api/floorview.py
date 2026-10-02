# Floor view dashboard API: everything the live floor page needs in one call.
# The canvas renderer maps real FWMS bins/workers/docks/tasks onto the
# reference simulator layout — nothing on the floor is faked except when
# Simulation Mode is on.

from __future__ import annotations

import hashlib

import frappe

from fwms.events import emit
from fwms.fwms.doctype.fwms_bin.fwms_bin import ACTIVE_LPN_STATUSES

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

# worker display role derived from default zone / skills (drives canvas colors)
ROLE_BY_ZONE = {
	"Receiving Docks": ("receiver", "#0891B2"),
	"QC Hold": ("qc", "#16A34A"),
	"Bulk": ("forklift", "#EAB308"),
	"Pick Faces": ("picker", "#2563EB"),
	"Packing": ("packer", "#DB2777"),
	"Shipping Docks": ("loader", "#EA580C"),
}


def _role_for(worker_doc) -> tuple[str, str]:
	skills = (worker_doc.skills or "").lower()
	if "count" in skills:
		return ("inventory", "#7C3AED")
	if "return" in skills:
		return ("returns", "#92400E")
	zone_type = frappe.db.get_value("FWMS Zone", worker_doc.default_zone, "zone_type") if worker_doc.default_zone else None
	if zone_type in ROLE_BY_ZONE:
		return ROLE_BY_ZONE[zone_type]
	if "pick" in skills:
		return ("picker", "#2563EB")
	if "pack" in skills:
		return ("packer", "#DB2777")
	if "receive" in skills:
		return ("receiver", "#0891B2")
	return ("forklift", "#EAB308")


def _bin_pos(bin_name: str) -> tuple[int, int]:
	"""Deterministic rack-grid position for a bin barcode (canvas coords)."""
	h = int(hashlib.md5(bin_name.encode()).hexdigest(), 16)
	aisle = h % 8  # 8 double racks, aisles 0..8
	side = (h >> 4) % 2
	row = (h >> 8) % 14  # avoid the 17-19 cross aisle band
	y = 4 + row if row < 13 else 33
	y = min(y, 33)
	if y >= 17 and y <= 19:
		y = 16
	x = (14 + 4 * aisle) + side
	return (x, y)


def _slot_map() -> dict[str, dict]:
	"""Real bin stock mapped onto canvas rack slots."""
	bins = frappe.get_all(
		"FWMS Bin",
		fields=["name", "zone", "abc_class", "is_pick_face", "primary_item"],
	)
	lpn_rows = frappe.get_all(
		"FWMS LPN",
		filters={"status": ("in", ACTIVE_LPN_STATUSES), "bin": ("is", "set")},
		fields=["name", "bin", "zone"],
	)
	qty_by_bin: dict[str, float] = {}
	if lpn_rows:
		items = frappe.get_all(
			"FWMS LPN Item",
			filters={"parenttype": "FWMS LPN", "parent": ("in", [l.name for l in lpn_rows])},
			fields=["parent", "item", "qty", "batch_no"],
		)
		pn_bin = {l.name: l.bin for l in lpn_rows}
		for row in items:
			bin_name = pn_bin.get(row.parent)
			if bin_name:
				qty_by_bin[bin_name] = qty_by_bin.get(bin_name, 0) + (row.qty or 0)

	used: set[tuple[int, int]] = set()
	slots = []
	for b in sorted(bins, key=lambda x: (0 if x.is_pick_face else 1, x.name)):
		x, y = _bin_pos(b.name)
		while (x, y) in used:  # de-conflict hash collisions
			y += 1
			if y > 33:
				y = 4
				x = x + 1 if (x + 1) % 4 else x - 3
		used.add((x, y))
		qty = qty_by_bin.get(b.name, 0)
		zone_type = frappe.db.get_value("FWMS Zone", b.zone, "zone_type") if b.zone else ""
		slots.append({
			"bin": b.name,
			"x": x, "y": y,
			"qty": qty,
			"abc": b.abc_class or "C",
			"pick_face": bool(b.is_pick_face),
			"zone": zone_type,
			"primary_item": b.primary_item,
		})
	return slots


def _staging_state() -> dict:
	"""Receiving staging occupancy from LPNs in Receiving/QC zones."""
	out = {"receiving": [], "qc_count": 0}
	for zone_type in ("Receiving Docks", "QC Hold"):
		zones = frappe.get_all("FWMS Zone", filters={"zone_type": zone_type}, pluck="name")
		if not zones:
			continue
		lpns = frappe.get_all(
			"FWMS LPN",
			filters={"zone": ("in", zones), "status": ("in", ACTIVE_LPN_STATUSES)},
			fields=["name"],
		)
		qtys: dict[str, float] = {}
		if lpns:
			items = frappe.get_all(
				"FWMS LPN Item",
				filters={"parenttype": "FWMS LPN", "parent": ("in", [l.name for l in lpns])},
				fields=["parent", "qty"],
			)
			for row in items:
				qtys[row.parent] = qtys.get(row.parent, 0) + (row.qty or 0)
		if zone_type == "Receiving Docks":
			out["receiving"] = [{"lpn": l.name, "qty": qtys.get(l.name, 0)} for l in lpns[:20]]
		else:
			out["qc_count"] = len(lpns)
	return out


@frappe.whitelist()
def dashboard() -> dict:
	frappe.only_for(("FWMS Supervisor", "FWMS Operator", "FWMS Inventory Control", "System Manager"))

	workers = frappe.get_all(
		"FWMS Worker",
		filters={"active": 1},
		fields=["name", "worker_code", "person_name", "default_zone", "current_task",
				"shift", "tasks_completed", "scans_today", "skills"],
	)
	roles = {}
	zones = frappe.get_all("FWMS Zone", fields=["name", "zone_type"])
	zone_type_by_name = {z.name: z.zone_type for z in zones}
	for w in workers:
		role, color = _role_for(w)
		roles[w.name] = {"role": role, "color": color}

	tasks = frappe.get_all(
		"FWMS Task",
		filters={"status": ("in", ("Open", "Assigned", "In Progress"))},
		fields=["name", "task_type", "status", "worker", "item", "qty", "lpn", "from_bin", "to_bin", "wave", "priority"],
		order_by="priority asc, creation asc",
		limit=100,
	)
	# canvas coordinates for task targets + zone labels
	bin_xy = {s["bin"]: (s["x"], s["y"]) for s in _slot_map()}
	for t in tasks:
		t["to_xy"] = list(bin_xy.get(t.to_bin, [None, None]))
		t["from_xy"] = list(bin_xy.get(t.from_bin, [None, None]))
		t["zone"] = zone_type_by_name.get(
			frappe.db.get_value("FWMS Bin", t.to_bin or t.from_bin, "zone") or "", ""
		)

	events = frappe.get_all(
		"FWMS Event Log",
		fields=["name", "category", "subtype", "severity", "is_scan", "message", "worker", "zone", "bin", "lpn",
				"reference_doctype", "reference_name", "creation"],
		order_by="creation desc",
		limit=150,
	)
	kpi = frappe.get_all("FWMS KPI Snapshot", fields=["*"], order_by="snapshot_time desc", limit=1)

	packed = frappe.db.count("FWMS LPN", {"status": "Packed", "lpn_type": "Cart"})
	staged_ship = frappe.get_all(
		"FWMS LPN", filters={"status": "Packed"}, fields=["zone", "delivery_note"], limit=200
	)
	shipping_zone = frappe.db.get_value("FWMS Zone", {"zone_type": "Shipping Docks"}, "name")
	lane_cartons = sum(1 for s in staged_ship if s.zone == shipping_zone)

	docks = frappe.get_all(
		"FWMS Dock Door",
		fields=["name", "door_code", "zone", "direction", "status", "carrier", "trailer"],
		order_by="door_code",
	)
	inbound_lpns = frappe.db.count("FWMS LPN", {"zone": frappe.db.get_value("FWMS Zone", {"zone_type": "Receiving Docks"}, "name")})

	return {
		"slots": _slot_map(),
		"staging": _staging_state(),
		"workers": [
			{**w, "role": roles[w.name]["role"], "color": roles[w.name]["color"]}
			for w in workers
		],
		"tasks": tasks,
		"task_by_worker": {t.worker: t for t in tasks if t.worker},
		"docks": docks,
		"lane_cartons": lane_cartons,
		"inbound_lpns": inbound_lpns,
		"pack_queue": packed,
		"events": events,
		"kpi": kpi[0] if kpi else None,
		"release_mode": frappe.db.get_single_value("FWMS Settings", "release_mode"),
		"simulation_mode": bool(frappe.db.get_single_value("FWMS Settings", "simulation_mode")),
		"shift": _current_shift(),
		"filters": EVENT_FILTERS,
	}


def _current_shift() -> dict | None:
	from frappe.utils import now_datetime

	now = now_datetime().time()
	for shift in frappe.get_all("FWMS Shift", fields=["shift_name", "start_time", "end_time"]):
		try:
			start, end = shift.start_time, shift.end_time
			if start <= end:
				if start <= now <= end:
					return shift
			elif now >= start or now <= end:
				return shift
		except TypeError:
			continue
	return None


@frappe.whitelist()
def set_release_mode(mode: str) -> dict:
	"""Release segmented control on the floor view."""
	frappe.only_for(("FWMS Supervisor", "System Manager"))
	if mode not in ("Continuous", "Waves"):
		frappe.throw("mode must be Continuous or Waves")
	frappe.db.set_single_value("FWMS Settings", "release_mode", mode)
	emit(
		"Orders & Picking",
		f"Order release switched to {mode}",
		subtype="release_mode",
		payload={"mode": mode},
	)
	return {"release_mode": mode}


@frappe.whitelist()
def simulate_orders(count: int = 20) -> dict:
	"""Demo spike: submit `count` Sales Orders from seeded items/customers.
	Continuous mode releases each immediately (waves + pick tasks)."""
	frappe.only_for(("FWMS Supervisor", "System Manager"))
	import random

	random.seed()
	items = [i for i in frappe.get_all("Item", filters={"is_stock_item": 1, "disabled": 0}, pluck="name") if i]
	customers = frappe.get_all("Customer", pluck="name", limit=10)
	pick_wh = frappe.db.get_value("FWMS Zone", {"zone_type": "Pick Faces"}, "warehouse")
	if not (items and customers and pick_wh):
		frappe.throw("Seed items/customers first (fwms.setup.seed_demo.run)")

	created = []
	failed = 0
	original_user = frappe.session.user
	frappe.set_user("Administrator")  # demo endpoint; supervisor-gated above
	for _ in range(min(int(count), 40)):
		so = frappe.new_doc("Sales Order")
		so.company = frappe.db.get_single_value("FWMS Settings", "company")
		so.customer = random.choice(customers)
		so.transaction_date = frappe.utils.today()
		so.delivery_date = frappe.utils.add_days(frappe.utils.today(), 2)
		so.selling_price_list = "Standard Selling"
		so.currency = "USD"
		so.plc_conversion_rate = 1
		so.conversion_rate = 1
		for item in random.sample(items, random.randint(1, 2)):
			row = so.append("items", {})
			row.item_code = item
			row.qty = random.randint(1, 5)
			row.warehouse = pick_wh
			row.rate = frappe.db.get_value("Item", item, "standard_rate")
			row.delivery_date = so.delivery_date
		try:
			so.insert(ignore_permissions=True)
			so.submit()
			created.append(so.name)
		except Exception:
			failed += 1
			try:
				if so.name and frappe.db.exists("Sales Order", so.name):
					frappe.delete_doc("Sales Order", so.name, ignore_permissions=True, force=True)
			except Exception:
				pass
	frappe.set_user(original_user)
	emit(
		"Orders & Picking",
		f"Order spike: {len(created)} new order(s) dropped in",
		subtype="order_spike",
		payload={"count": len(created)},
	)
	return {"created": created, "failed": failed}


@frappe.whitelist()
def simulate_truck() -> dict:
	"""Demo: mark an inbound door Busy + emit check-in event."""
	frappe.only_for(("FWMS Supervisor", "System Manager"))
	door = frappe.get_all(
		"FWMS Dock Door", filters={"direction": "Inbound", "status": "Idle"}, limit=1
	)
	if not door:
		frappe.throw("No idle inbound door")
	frappe.db.set_value("FWMS Dock Door", door[0].name, {
		"status": "Busy", "carrier": "FWMS Demo Lines",
		"trailer": f"TR-{frappe.generate_hash(length=4)}",
	})
	emit(
		"Receiving & QC",
		f"Trailer {frappe.db.get_value('FWMS Dock Door', door[0].name, 'trailer')} docked at {door[0].name} — ASN opened, pallets expected",
		subtype="trailer_dock",
		zone=frappe.db.get_value("FWMS Dock Door", door[0].name, "zone"),
		payload={"door": door[0].name},
	)
	return {"door": door[0].name}


@frappe.whitelist()
def simulate_tick() -> dict:
	"""Simulation mode: fabricate a plausible floor event for demos."""
	frappe.only_for(("FWMS Supervisor", "System Manager"))
	if not frappe.db.get_single_value("FWMS Settings", "simulation_mode"):
		frappe.throw("Simulation mode is off in FWMS Settings")

	import random

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
		("Returns", "RMA {n} received at returns desk"),
	]
	category, template = random.choice(samples)
	n = random.randint(100, 999)
	emit(
		category,
		template.format(n=n),
		subtype="simulated",
		is_scan=bool(random.getrandbits(1)),
		worker=random.choice(workers),
		zone=random.choice(zones),
		payload={"simulated": True},
	)
	return {"ok": True}
