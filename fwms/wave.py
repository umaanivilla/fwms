# FWMS wave engine: converts released Sales Orders into directed picking work.
#
# Continuous mode: SO submit -> single-order wave -> immediate release.
# Waves mode: supervisor/scheduler batches open orders by cutoff/route/dock.
#
# Release builds an ERPNext Pick List (draft) whose rows are ordered FEFO/FIFO
# from real pick-face LPN stock, plus one Pick task per bin visit, and raises
# replenishment tasks when pick faces run short.

from __future__ import annotations

import frappe
from frappe.utils import flt, now

from fwms.events import emit
from fwms.tasks import create_task
from fwms.utils import (
	fefo_sort_batches,
	get_warehouse_for_type,
	get_zones_by_type,
)


def on_sales_order_submit(doc, method=None):
	mode = frappe.db.get_single_value("FWMS Settings", "release_mode") or "Continuous"
	if mode != "Continuous":
		emit(
			"Orders & Picking",
			f"Order {doc.name} ({doc.customer}) held for wave release",
			subtype="order_held",
			reference_doctype="Sales Order",
			reference_name=doc.name,
			payload={"customer": doc.customer, "mode": "Waves"},
		)
		return

	wave = frappe.get_doc({
		"doctype": "FWMS Wave",
		"mode": "Continuous",
		"orders": [{"sales_order": doc.name}],
	}).insert(ignore_permissions=True)
	release_wave(wave.name)


def release_wave_action(doctype: str | None = None, name: str | None = None, doc=None) -> None:
	"""Server action on FWMS Wave list."""
	target = name or (doc.name if doc else None)
	if target:
		release_wave(target)


def release_wave(wave_name: str) -> str:
	"""Build Pick List + Pick tasks for a Draft wave. Returns the Pick List name."""
	wave = frappe.get_doc("FWMS Wave", wave_name)
	if wave.status != "Draft":
		frappe.throw(f"Wave {wave_name} is already {wave.status}")

	settings_company = frappe.db.get_single_value("FWMS Settings", "company")

	pick_list = frappe.new_doc("Pick List")
	pick_list.company = settings_company
	pick_list.purpose = "Delivery"
	pick_list.pick_manually = 1  # FWMS supplies directed FEFO locations itself
	pick_list.consider_rejected_warehouses = 0

	pick_face_wh = get_warehouse_for_type("Pick Faces")
	bulk_wh = get_warehouse_for_type("Bulk")

	tasks_created = 0
	for order in wave.orders:
		so = frappe.get_doc("Sales Order", order.sales_order)
		for so_item in so.items:
			item = so_item.item_code
			needs = flt(so_item.stock_qty) - flt(so_item.delivered_qty)
			if needs <= 0:
				continue

			# allocate from pick faces first (bin-directed), then bulk
			allocations = _allocate(item, needs)
			remaining = needs
			for alloc in allocations:
				row = pick_list.append("locations")
				row.item_code = item
				row.qty = alloc["qty"]
				row.stock_qty = alloc["qty"]
				row.uom = so_item.stock_uom
				row.conversion_factor = 1
				row.warehouse = pick_face_wh if alloc["zone_type"] == "Pick Faces" else bulk_wh
				row.batch_no = alloc["batch_no"]
				row.use_serial_batch_fields = 1
				row.sales_order = so.name
				row.sales_order_item = so_item.name
				row.stock_uom = so_item.stock_uom
				remaining -= alloc["qty"]

				if alloc["zone_type"] == "Pick Faces":
					create_task(
						"Pick",
						item=item,
						qty=alloc["qty"],
						batch_no=alloc["batch_no"],
						from_bin=alloc["bin"],
						lpn=alloc["lpn"],
						wave=wave.name,
						emit_event=False,
						references=[{
							"reference_doctype": "Pick List",
							"reference_name": None,  # patched after PL insert
							"note": f"so:{so.name}",
						}],
					)
					tasks_created += 1

			if remaining > 0.0:
				_emit_shortfall_alert(item, remaining, so.name)

	pick_list.insert(ignore_permissions=True)

	# patch pick tasks with the real PL row names (row order == creation order)
	_tasks = frappe.get_all(
		"FWMS Task",
		filters={"wave": wave.name, "task_type": "Pick", "status": "Open"},
		order_by="creation asc",
		pluck="name",
	)
	for task_name, row in zip(_tasks, [r for r in pick_list.locations if r.warehouse == pick_face_wh]):
		for ref in frappe.get_doc("FWMS Task", task_name).references or []:
			if ref.reference_doctype == "Pick List":
				frappe.db.set_value("FWMS Task Reference", ref.name, "reference_name", row.name)

	wave.db_set("pick_list", pick_list.name)
	wave.db_set("status", "Released")
	wave.db_set("released_at", now())
	for order in wave.orders:
		frappe.db.set_value("FWMS Wave Order", order.name, "status", "Released")

	emit(
		"Orders & Picking",
		f"Wave {wave.name} released — {len(wave.orders)} order(s), {tasks_created} pick task(s)",
		subtype="wave_released",
		reference_doctype="FWMS Wave",
		reference_name=wave.name,
		payload={"pick_list": pick_list.name, "orders": [o.sales_order for o in wave.orders]},
	)

	# fully bulk-sourced waves have no RF pick tasks — complete right away
	if tasks_created == 0:
		_finish_wave(wave.name, pick_list.name)

	return pick_list.name


def _finish_wave(wave_name: str, pick_list_name: str) -> None:
	"""Submit the wave's Pick List (picked reality) and draft the Delivery Notes."""
	pick_list = frappe.get_doc("Pick List", pick_list_name)
	if pick_list.docstatus == 0:
		from frappe.utils import flt

		for row in pick_list.locations:
			if flt(row.picked_qty) < flt(row.qty):
				row.qty = flt(row.picked_qty) if flt(row.picked_qty) > 0 else 0
		pick_list.save(ignore_permissions=True)
		pick_list.submit()
	frappe.db.set_value("FWMS Wave", wave_name, "status", "Picking Complete")
	emit(
		"Orders & Picking",
		f"Wave {wave_name} picking complete — Pick List {pick_list_name} submitted",
		subtype="wave_pick_complete",
		reference_doctype="Pick List",
		reference_name=pick_list_name,
	)
	from fwms.api.rf_picking import _ensure_draft_dns

	_ensure_draft_dns(wave_name, pick_list_name)


def _allocate(item: str, qty: float) -> list[dict]:
	"""FEFO/FIFO allocate qty across pick-face LPN stock (bin-directed), then bulk stock."""
	allocations: list[dict] = []

	for zone_type in ("Pick Faces", "Bulk"):
		if qty <= 0:
			break
		zones = get_zones_by_type(zone_type)
		if not zones:
			continue
		lpns = frappe.get_all(
			"FWMS LPN",
			filters={"zone": ("in", zones), "status": ("in", ("In Bin", "Receiving", "Picking"))},
			fields=["name", "bin"],
		)
		if not lpns:
			continue
		rows = frappe.get_all(
			"FWMS LPN Item",
			filters={"parenttype": "FWMS LPN", "parent": ("in", [l.name for l in lpns]), "item": item},
			fields=["parent", "batch_no", "qty"],
		)
		if not rows:
			continue
		bin_by_lpn = {l.name: l.bin for l in lpns}
		rows = fefo_sort_batches(rows)
		for row in rows:
			if qty <= 0:
				break
			take = min(flt(row.qty), qty)
			if take <= 0:
				continue
			allocations.append({
				"qty": take,
				"batch_no": row.batch_no,
				"bin": bin_by_lpn.get(row.parent),
				"lpn": row.parent,
				"zone_type": zone_type,
			})
			qty -= take

	return allocations


def _emit_shortfall_alert(item: str, qty: float, so: str) -> None:
	emit(
		"Alerts & Supervisor",
		f"Short {qty} of {item} for {so} — not fully covered by Pick Faces/Bulk",
		subtype="shortage",
		severity="Alert",
		reference_doctype="Sales Order",
		reference_name=so,
		payload={"item": item, "short_qty": qty},
	)


def on_delivery_note_shipped(doc, method=None):
	"""Mark wave orders shipped; close the wave once every order has shipped."""
	wave_names = {doc.fwms_wave} if getattr(doc, "fwms_wave", None) else set()
	wave_names.update(
		r.parent for r in frappe.get_all(
			"FWMS Wave Order", filters={"delivery_note": doc.name}, fields=["parent"]
		)
	)
	# link the DN onto its wave order row when not yet recorded
	so = frappe.db.get_value(
		"Delivery Note Item", {"parent": doc.name}, "against_sales_order"
	)
	for wave_name in filter(None, wave_names):
		orders = frappe.get_all(
			"FWMS Wave Order",
			filters={"parent": wave_name},
			fields=["name", "sales_order", "status", "delivery_note"],
		)
		row_name = None
		if so:
			row_name = next((o.name for o in orders if o.sales_order == so), None)
		if not row_name and len(orders) == 1:
			row_name = orders[0].name  # single-order wave: unambiguous
		if row_name:
			frappe.db.set_value(
				"FWMS Wave Order", row_name,
				{"status": "Shipped", "delivery_note": doc.name}, update_modified=False,
			)
			for o in orders:
				if o.name == row_name:
					o.status = "Shipped"
					o.delivery_note = doc.name

	for wave_name in filter(None, wave_names):
		orders = frappe.get_all(
			"FWMS Wave Order",
			filters={"parent": wave_name},
			fields=["status", "sales_order", "delivery_note"],
		)
		if not orders:
			continue
		all_shipped = all(
			o.status == "Shipped" or (
				o.delivery_note and frappe.db.get_value("Delivery Note", o.delivery_note, "docstatus") == 1
			)
			for o in orders
		)
		if all_shipped:
			frappe.db.set_value("FWMS Wave", wave_name, {"status": "Closed", "closed_at": now()})
			emit(
				"Sort Load & Ship",
				f"Wave {wave_name} closed — all orders shipped",
				subtype="wave_closed",
				reference_doctype="FWMS Wave",
				reference_name=wave_name,
			)


def release_due_waves() -> int:
	"""Scheduler: release Waves-mode waves whose cutoff has passed."""
	released = 0
	for wave_name in frappe.get_all(
		"FWMS Wave",
		filters={"status": "Draft", "mode": "Waves", "cutoff_time": ("<=", now())},
		pluck="name",
	):
		release_wave(wave_name)
		released += 1
	return released
