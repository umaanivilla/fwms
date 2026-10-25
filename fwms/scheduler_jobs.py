# FWMS scheduled jobs: cycle-count generation, replenishment watch, KPI snapshots.

from __future__ import annotations

from datetime import datetime, timedelta

import frappe
from frappe.utils import add_days, flt, now_datetime, today

from fwms.events import emit
from fwms.tasks import create_task


def generate_count_tasks() -> int:
	"""Daily: create Count tasks for bins due under each Count Program (ABC-driven)."""
	created = 0
	programs = frappe.get_all(
		"FWMS Count Program",
		filters={"enabled": 1, "auto_generate": 1},
		fields=["name", "abc_class", "interval_days", "zone", "count_method"],
	)
	for program in programs:
		filters = {"abc_class": program.abc_class}
		if program.zone:
			filters["zone"] = program.zone
		bins = frappe.get_all("FWMS Bin", filters=filters, fields=["name", "last_counted_on"])
		due_cutoff = frappe.utils.getdate(add_days(today(), -program.interval_days))
		for bin_row in bins:
			if bin_row.last_counted_on and bin_row.last_counted_on > due_cutoff:
				continue
			# skip if an open/assigned count already exists for this bin
			exists = frappe.db.exists(
				"FWMS Task",
				{
					"task_type": "Count",
					"to_bin": bin_row.name,
					"status": ("in", ("Open", "Assigned", "In Progress")),
				},
			)
			if exists:
				continue
			create_task(
				"Count",
				to_bin=bin_row.name,
				priority="3-Low",
				emit_event=False,
				references=[
					{
						"reference_doctype": "FWMS Count Program",
						"reference_name": program.name,
						"note": program.count_method or "Blind",
					}
				],
			)
			created += 1
		frappe.db.set_value(
			"FWMS Count Program",
			program.name,
			{"last_generated_on": today(), "tasks_generated": (program_tasks(program.name))},
		)
	if created:
		emit(
			"Inventory Control",
			f"Count scheduler queued {created} cycle count task(s)",
			subtype="count_scheduled",
		)
	return created


def program_tasks(program_name: str) -> int:
	return frappe.db.sql(
		"""
		select count(distinct t.name)
		from `tabFWMS Task` t
		join `tabFWMS Task Reference` r on r.parent = t.name
		where t.task_type = 'Count'
		  and r.reference_doctype = 'FWMS Count Program'
		  and r.reference_name = %s
		""",
		program_name,
	)[0][0]


def check_replenishment() -> int:
	"""Hourly: pick-face bins below min stock get a replenish task from bulk."""
	from fwms.fwms.doctype.fwms_bin.fwms_bin import get_bin_stock

	created = 0
	bins = frappe.get_all(
		"FWMS Bin",
		filters={"is_pick_face": 1, "primary_item": ("is", "set")},
		fields=["name", "primary_item", "min_qty", "max_qty"],
	)
	for bin_row in bins:
		stock = sum(flt(r.qty) for r in get_bin_stock(bin_row.name) if r.item == bin_row.primary_item)
		if stock >= flt(bin_row.min_qty):
			continue
		exists = frappe.db.exists(
			"FWMS Task",
			{
				"task_type": "Replenish",
				"to_bin": bin_row.name,
				"status": ("in", ("Open", "Assigned", "In Progress")),
			},
		)
		if exists:
			continue
		short = flt(bin_row.max_qty) - stock
		qty = short if short > 0 else flt(bin_row.min_qty)
		qty = min(qty, _bulk_available(bin_row.primary_item))
		if qty <= 0:
			continue  # nothing in bulk to send forward yet
		create_task(
			"Replenish",
			item=bin_row.primary_item,
			qty=qty,
			to_bin=bin_row.name,
			priority="1-High",
		)
		created += 1
	return created


def _bulk_available(item: str) -> float:
	"""Qty of item sitting in Bulk-zone LPNs."""
	from fwms.fwms.doctype.fwms_bin.fwms_bin import get_zone_stock

	bulk_zones = frappe.get_all("FWMS Zone", filters={"zone_type": "Bulk"}, pluck="name")
	return sum(flt(r["qty"]) for r in get_zone_stock(bulk_zones, item=item))


def snapshot_kpis() -> str:
	"""Every 5 minutes: roll floor KPIs into FWMS KPI Snapshot + broadcast."""
	now = now_datetime()

	orders_shipped_today = frappe.db.count("Delivery Note", {"docstatus": 1, "posting_date": today()})
	open_orders = frappe.db.count(
		"Sales Order", {"docstatus": 1, "status": ("!=", "Closed"), "per_delivered": ("<", 100)}
	)
	rf_scans = frappe.db.count(
		"FWMS Event Log",
		{"is_scan": 1, "creation": (">=", now - timedelta(hours=1))},
	)
	pack_queue = frappe.db.count("FWMS LPN", {"status": "Picking", "lpn_type": "Cart"})
	dock_doors = frappe.db.count("FWMS Dock Door", {"status": ("!=", "Idle")})

	hour_ago = now - timedelta(hours=1)
	lines_last_hour = frappe.db.count(
		"FWMS Task", {"task_type": "Pick", "status": "Complete", "completed_at": (">=", hour_ago)}
	)

	shift_start = now - timedelta(hours=8)
	counts_shift = frappe.db.count(
		"FWMS Task", {"task_type": "Count", "status": "Complete", "completed_at": (">=", shift_start)}
	)
	accuracies = frappe.get_all(
		"FWMS Bin", filters={"last_count_accuracy": (">", 0)}, pluck="last_count_accuracy"
	)
	count_accuracy = round(sum(accuracies) / len(accuracies), 1) if accuracies else None

	cycle_minutes = _avg_cycle_time_minutes()

	snapshot = frappe.get_doc(
		{
			"doctype": "FWMS KPI Snapshot",
			"snapshot_time": now,
			"orders_shipped_today": orders_shipped_today,
			"open_orders": open_orders,
			"avg_cycle_time_minutes": cycle_minutes,
			"lines_picked_per_hour": lines_last_hour,
			"rf_scans_last_hour": rf_scans,
			"pack_queue": pack_queue,
			"dock_doors_in_use": dock_doors,
			"count_accuracy_pct": count_accuracy,
			"counts_completed_shift": counts_shift,
		}
	).insert(ignore_permissions=True)

	frappe.publish_realtime(event="fwms_kpi", message=snapshot.as_dict())
	return snapshot.name


def _avg_cycle_time_minutes() -> float | None:
	"""Release-to-trailer for the last 30 shipped orders (wave.released_at -> dn.creation)."""
	rows = frappe.db.sql(
		"""
		select wave.released_at, dn.creation
		from `tabFWMS Wave Order` wo
		join `tabFWMS Wave` wave on wave.name = wo.parent
		join `tabDelivery Note` dn on dn.name = wo.delivery_note
		where wo.status = 'Shipped' and wave.released_at is not null
		order by dn.creation desc limit 30
		""",
		as_dict=True,
	)
	if not rows:
		return None
	minutes = []
	for row in rows:
		released = row.released_at
		shipped = row.creation
		if isinstance(released, str):
			released = datetime.fromisoformat(released)
		if isinstance(shipped, str):
			shipped = datetime.fromisoformat(shipped)
		delta = (shipped - released).total_seconds() / 60
		if delta >= 0:
			minutes.append(delta)
	return round(sum(minutes) / len(minutes), 1) if minutes else None
