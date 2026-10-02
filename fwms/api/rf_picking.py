# RF Picking: claim pick tasks, push a cart (LPN), pick bin-directed with
# FEFO batches and serial validation, then submit the wave's Pick List.

from __future__ import annotations

import frappe
from frappe.utils import flt

from fwms.api.rf_common import current_worker, require_operator
from fwms.events import emit
from fwms.tasks import claim_next, complete_task, start_task
from fwms.utils import get_next_lpn_barcode


@frappe.whitelist()
def claim(worker: str | None = None, cart_barcode: str | None = None) -> dict:
	"""Claim the next Pick task and bind a cart LPN to it."""
	require_operator()
	worker = worker or current_worker()
	task_name = claim_next(worker, task_type="Pick")
	if not task_name:
		return {"task": None, "message": "No pick tasks in queue"}

	task = frappe.db.get_value(
		"FWMS Task", task_name,
		["name", "item", "qty", "batch_no", "from_bin", "wave", "priority"],
		as_dict=True,
	)
	cart = _ensure_cart(cart_barcode, worker)
	start_task(task_name, worker)
	return {"task": task, "cart": cart}


def _ensure_cart(cart_barcode: str | None, worker: str) -> str:
	if cart_barcode and frappe.db.exists("FWMS LPN", cart_barcode):
		frappe.db.set_value("FWMS LPN", cart_barcode, "status", "Picking", update_modified=True)
		return cart_barcode
	barcode = cart_barcode or get_next_lpn_barcode("Cart")
	if not frappe.db.exists("FWMS LPN", barcode):
		frappe.get_doc({
			"doctype": "FWMS LPN",
			"barcode": barcode,
			"lpn_type": "Cart",
			"status": "Picking",
			"zone": frappe.db.get_value("FWMS Zone", {"zone_type": "Pick Faces"}, "name"),
		}).insert(ignore_permissions=True)
	return barcode


@frappe.whitelist()
def task_detail(task: str) -> dict:
	require_operator()
	task_doc = frappe.get_doc("FWMS Task", task)
	pl_row = None
	wave_order = None
	for ref in task_doc.references or []:
		if ref.reference_doctype == "Pick List Item" and ref.reference_name:
			pl_row = frappe.db.get_value(
				"Pick List Item", ref.reference_name,
				["name", "parent", "picked_qty", "qty", "sales_order"],
				as_dict=True,
			)
		if ref.reference_doctype == "Pick List" and ref.reference_name:
			if not pl_row:
				pl_row = frappe.db.get_value(
					"Pick List Item", ref.reference_name,
					["name", "parent", "picked_qty", "qty", "sales_order"],
					as_dict=True,
				)
	if pl_row and pl_row.sales_order:
		wave_order = pl_row.sales_order

	serials = []
	if task_doc.item:
		serials = _offer_serials(task_doc)
	return {
		"task": task_doc.name,
		"item": task_doc.item,
		"qty": task_doc.qty,
		"batch_no": task_doc.batch_no,
		"from_bin": task_doc.from_bin,
		"wave": task_doc.wave,
		"pick_list_row": pl_row.name if pl_row else None,
		"pick_list": pl_row.parent if pl_row else None,
		"sales_order": wave_order,
		"serials_expected": serials,
	}


def _offer_serials(task_doc) -> list[str]:
	"""Serials expected from the source LPN's bundle (for validation hints)."""
	if not (task_doc.item and frappe.get_cached_value("Item", task_doc.item, "has_serial_no")):
		return []
	if not task_doc.lpn:
		return []
	row = next((r for r in frappe.get_doc("FWMS LPN", task_doc.lpn).items if r.item == task_doc.item), None)
	if not row or not row.serial_and_batch_bundle:
		return []
	return frappe.get_all(
		"Serial and Batch Entry",
		filters={"parent": row.serial_and_batch_bundle, "parenttype": "Serial and Batch Bundle"},
		pluck="serial_no",
	)


@frappe.whitelist()
def pick_confirm(task: str, qty: float, cart: str, scanned_serials: list[str] | None = None) -> dict:
	"""Confirm a pick: move qty from bin stock to the cart, update the Pick List row."""
	require_operator()
	worker = current_worker()
	qty = flt(qty)
	if qty <= 0:
		frappe.throw("Pick qty must be positive")

	task_doc = frappe.get_doc("FWMS Task", task)
	if task_doc.status not in ("Assigned", "In Progress"):
		frappe.throw(f"Task {task} is not active")
	if qty > flt(task_doc.qty):
		frappe.throw(f"Cannot pick {qty} — task {task} needs {task_doc.qty}")

	from fwms.fwms.doctype.fwms_lpn.fwms_lpn import add_contents, remove_contents

	# physical stock: from the source LPN in the bin (if known) to the cart
	if task_doc.lpn:
		remove_contents(task_doc.lpn, task_doc.item, qty, batch_no=task_doc.batch_no)
	add_contents(cart, task_doc.item, qty, batch_no=task_doc.batch_no)

	# Pick List row picked qty
	pl_row_name = None
	for ref in task_doc.references or []:
		if ref.reference_doctype in ("Pick List Item", "Pick List") and ref.reference_name:
			pl_row_name = ref.reference_name
			if ref.reference_doctype == "Pick List":
				pl_row_name = frappe.db.get_value(
					"Pick List Item", {"parent": ref.reference_name, "item_code": task_doc.item},
					"name",
				)
			break
	if pl_row_name:
		row = frappe.db.get_value("Pick List Item", pl_row_name, ["picked_qty", "parent", "sales_order"], as_dict=True)
		frappe.db.set_value("Pick List Item", pl_row_name, "picked_qty", flt(row.picked_qty) + qty)
		# record scanned serials on the row (old fields path)
		if scanned_serials:
			existing = frappe.db.get_value("Pick List Item", pl_row_name, "serial_no") or ""
			merged = "\n".join(filter(None, [existing, *scanned_serials]))
			frappe.db.set_value("Pick List Item", pl_row_name, {
				"serial_no": merged,
				"use_serial_batch_fields": 1,
			})
		# bind the cart to its order so packing resolves the right DN
		if row.sales_order and not frappe.db.get_value("FWMS LPN", cart, "sales_order"):
			frappe.db.set_value("FWMS LPN", cart, "sales_order", row.sales_order, update_modified=False)

	remaining = flt(task_doc.qty) - qty
	if remaining <= 0:
		complete_task(task, result_note=f"picked {qty:g}")
	else:
		frappe.db.set_value("FWMS Task", task, {"qty": remaining, "result_note": f"partial {qty:g} picked"})

	emit(
		"Orders & Picking",
		f"Picked {qty:g} x {task_doc.item} from {task_doc.from_bin or 'bin'} into {cart}",
		subtype="pick",
		worker=worker,
		bin=task_doc.from_bin,
		lpn=cart,
		reference_doctype="FWMS Task",
		reference_name=task,
		payload={"item": task_doc.item, "qty": qty, "batch": task_doc.batch_no},
	)

	if remaining <= 0:
		_maybe_complete_wave(task_doc.wave)
	return {"task": task, "picked": qty, "remaining": remaining, "cart": cart}


def _maybe_complete_wave(wave: str | None) -> None:
	"""When every Pick task of the wave is done: submit the Pick List + create draft DN(s)."""
	if not wave:
		return
	remaining = frappe.db.count(
		"FWMS Task", {"wave": wave, "task_type": "Pick", "status": ("in", ("Open", "Assigned", "In Progress"))}
	)
	if remaining:
		return

	pick_list_name = frappe.db.get_value("FWMS Wave", wave, "pick_list")
	if not pick_list_name:
		return
	pick_list = frappe.get_doc("Pick List", pick_list_name)
	if pick_list.docstatus == 0:
		# shrink short rows so submit validation passes on picked reality
		for row in pick_list.locations:
			if flt(row.picked_qty) < flt(row.qty):
				if flt(row.picked_qty) <= 0:
					row.qty = 0
				else:
					row.qty = row.picked_qty
		pick_list.save(ignore_permissions=True)
		pick_list.submit()

	frappe.db.set_value("FWMS Wave", wave, "status", "Picking Complete")
	emit(
		"Orders & Picking",
		f"Wave {wave} picking complete — Pick List {pick_list_name} submitted",
		subtype="wave_pick_complete",
		reference_doctype="Pick List",
		reference_name=pick_list_name,
	)

	# draft the Delivery Note(s) so packing can proceed against them
	_ensure_draft_dns(wave, pick_list_name)


def _ensure_draft_dns(wave: str, pick_list_name: str) -> list[str]:
	from erpnext.stock.doctype.pick_list.pick_list import create_delivery_note

	existing = frappe.get_all("Delivery Note", {"fwms_wave": wave, "docstatus": 0}, pluck="name")
	if existing:
		_link_wave_orders(wave, existing)
		return existing
	created = create_delivery_note(pick_list_name)
	docs = created if isinstance(created, list) else ([created] if created else [])
	names = []
	for doc in docs:
		if isinstance(doc, str):
			doc = frappe.get_doc("Delivery Note", doc)
		doc.fwms_wave = wave
		doc.save(ignore_permissions=True)
		names.append(doc.name)
	_link_wave_orders(wave, names)
	return names


def _link_wave_orders(wave: str, dn_names: list[str]) -> None:
	"""Record each draft DN against its FWMS Wave Order row (via the DN's SO)."""
	for dn_name in dn_names:
		so = frappe.db.get_value("Delivery Note Item", {"parent": dn_name}, "against_sales_order")
		if not so:
			continue
		row_name = frappe.db.get_value("FWMS Wave Order", {"parent": wave, "sales_order": so}, "name")
		if row_name:
			frappe.db.set_value(
				"FWMS Wave Order", row_name, "delivery_note", dn_name, update_modified=False
			)
