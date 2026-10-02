# RF Putaway: directed LPN moves from receiving staging into storage bins.

from __future__ import annotations

import frappe
from frappe.utils import flt, nowdate

from fwms.api.rf_common import current_worker, require_operator
from fwms.events import emit
from fwms.tasks import complete_task
from fwms.utils import get_warehouse_for_type


@frappe.whitelist()
def open_tasks(worker: str | None = None) -> list[dict]:
	"""Putaway queue visible on the floor (Open + mine)."""
	require_operator()
	return frappe.get_all(
		"FWMS Task",
		filters={"task_type": "Putaway", "status": ("in", ("Open", "Assigned", "In Progress"))},
		fields=["name", "status", "priority", "lpn", "qty", "worker"],
		order_by="priority asc, creation asc",
		limit=50,
	)


@frappe.whitelist()
def suggest_bin(lpn: str) -> dict:
	"""Directed putaway: pick face slotted for the LPN's item (below max), else bulk."""
	require_operator()
	lpn_doc = frappe.get_doc("FWMS LPN", lpn)
	if not lpn_doc.items:
		frappe.throw(f"LPN {lpn} is empty")
	if len({r.item for r in lpn_doc.items}) == 1:
		item = lpn_doc.items[0].item
		for face in frappe.get_all(
			"FWMS Bin",
			filters={"is_pick_face": 1, "primary_item": item},
			fields=["name", "max_qty"],
		):
			current = _bin_item_qty(face.name, item)
			if flt(face.max_qty) <= 0 or current + lpn_doc.total_qty() <= flt(face.max_qty):
				return {"bin": face.name, "reason": f"pick face for {item}"}

	bulk_bins = frappe.get_all(
		"FWMS Bin",
		filters={"zone": frappe.db.get_value("FWMS Zone", {"zone_type": "Bulk"}, "name")},
		pluck="name",
		order_by="creation asc",
	)
	if bulk_bins:
		return {"bin": bulk_bins[0], "reason": "bulk storage"}
	frappe.throw("No putaway bin found — check FWMS Bin setup")


def _bin_item_qty(bin_name: str, item: str) -> float:
	from fwms.fwms.doctype.fwms_bin.fwms_bin import get_bin_stock

	return sum(flt(r.qty) for r in get_bin_stock(bin_name) if r.item == item)


@frappe.whitelist()
def confirm(lpn: str, bin_name: str, task: str | None = None) -> dict:
	"""Scan LPN -> scan bin -> move. Stock Entry only when the zone changes."""
	require_operator()
	worker = current_worker()
	lpn_doc = frappe.get_doc("FWMS LPN", lpn)
	bin_doc = frappe.get_doc("FWMS Bin", bin_name)
	from_zone = lpn_doc.zone
	to_zone = bin_doc.zone
	to_zone_wh = frappe.db.get_value("FWMS Zone", to_zone, "warehouse")

	if from_zone and from_zone != to_zone and to_zone_wh:
		from_wh = frappe.db.get_value("FWMS Zone", from_zone, "warehouse")
		if from_wh:
			_move_stock(lpn_doc, from_wh, to_zone_wh, worker)

	lpn_doc.bin = bin_name
	lpn_doc.zone = to_zone
	lpn_doc.status = "In Bin"
	lpn_doc.save(ignore_permissions=True)

	if task:
		complete_task(task, result_note=f"putaway to {bin_name}")

	emit(
		"Putaway & Storage",
		f"{lpn} put away to {bin_name} ({to_zone})",
		subtype="putaway",
		worker=worker,
		zone=to_zone,
		bin=bin_name,
		lpn=lpn,
		payload={"from_zone": from_zone, "to_zone": to_zone},
	)
	return {"lpn": lpn, "bin": bin_name, "zone": to_zone}


def _move_stock(lpn_doc, from_wh: str, to_wh: str, worker: str | None) -> None:
	"""Zone-to-zone Material Transfer preserving batch/serial identity."""
	se = frappe.new_doc("Stock Entry")
	se.company = frappe.db.get_single_value("FWMS Settings", "company")
	se.stock_entry_type = "Material Transfer"
	se.posting_date = nowdate()
	se.fwms_worker = worker
	for row in lpn_doc.items:
		if flt(row.qty) <= 0:
			continue
		target = se.append("items", {})
		target.item_code = row.item
		target.qty = row.qty
		target.s_warehouse = from_wh
		target.t_warehouse = to_wh
		target.use_serial_batch_fields = 1
		if row.batch_no:
			target.batch_no = row.batch_no
		if row.serial_and_batch_bundle:
			# pull serial numbers from the source bundle if recorded
			serials = frappe.get_all(
				"Serial and Batch Entry",
				filters={"parent": row.serial_and_batch_bundle, "parenttype": "Serial and Batch Bundle"},
				pluck="serial_no",
			)
			if serials:
				target.serial_no = "\n".join(s for s in serials if s)
	se.insert(ignore_permissions=True)
	se.submit()
