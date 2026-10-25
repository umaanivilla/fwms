# RF Replenishment: bulk -> pick face moves driven by min/max breaches.

from __future__ import annotations

import frappe
from frappe.utils import flt, nowdate

from fwms.api.rf_common import current_worker, require_operator
from fwms.events import emit
from fwms.tasks import complete_task
from fwms.utils import get_warehouse_for_type


@frappe.whitelist()
def open_tasks() -> list[dict]:
	require_operator()
	return frappe.get_all(
		"FWMS Task",
		filters={"task_type": "Replenish", "status": ("in", ("Open", "Assigned", "In Progress"))},
		fields=["name", "status", "item", "qty", "to_bin", "priority"],
		order_by="priority asc, creation asc",
		limit=50,
	)


@frappe.whitelist()
def source_bins(item: str) -> list[dict]:
	"""Bulk-zone bins holding the item (via LPN aggregation)."""
	require_operator()
	bulk_zone = frappe.db.get_value("FWMS Zone", {"zone_type": "Bulk"}, "name")
	lpns = frappe.get_all(
		"FWMS LPN",
		filters={"zone": bulk_zone, "status": "In Bin"},
		fields=["name", "bin"],
	)
	if not lpns:
		return []
	lp = frappe.qb.DocType("FWMS LPN Item")
	rows = (
		frappe.qb.from_(lp)
		.select(lp.parent, lp.batch_no, frappe.query_builder.functions.Sum(lp.qty).as_("qty"))
		.where((lp.parenttype == "FWMS LPN") & (lp.parent.isin([l.name for l in lpns])) & (lp.item == item))
		.groupby(lp.parent, lp.batch_no)
		.run(as_dict=True)
	)
	bin_by_lpn = {l.name: l.bin for l in lpns}
	return [
		{"bin": bin_by_lpn.get(r.parent), "lpn": r.parent, "batch_no": r.batch_no, "qty": flt(r.qty)}
		for r in rows
		if flt(r.qty) > 0
	]


@frappe.whitelist()
def confirm(task: str, from_bin: str, qty: float, batch_no: str | None = None) -> dict:
	"""Move stock bulk -> pick face: Stock Entry + LPN relocation/transfer."""
	require_operator()
	worker = current_worker()
	qty = flt(qty)
	if qty <= 0:
		frappe.throw("Replenishment qty must be positive")

	task_doc = frappe.get_doc("FWMS Task", task)
	to_bin = task_doc.to_bin
	item = task_doc.item

	from_zone = frappe.db.get_value("FWMS Bin", from_bin, "zone")
	to_zone = frappe.db.get_value("FWMS Bin", to_bin, "zone")
	from_wh = frappe.db.get_value("FWMS Zone", from_zone, "warehouse")
	to_wh = frappe.db.get_value("FWMS Zone", to_zone, "warehouse")
	if not (from_wh and to_wh):
		frappe.throw("Replenishment needs ERP warehouses on both zones")

	# source LPN holding the stock
	from fwms.fwms.doctype.fwms_bin.fwms_bin import get_bin_stock

	stock = [
		r for r in get_bin_stock(from_bin) if r.item == item and (not batch_no or r.batch_no == batch_no)
	]
	if not stock:
		frappe.throw(f"No {item} stock found in {from_bin}")

	se = frappe.new_doc("Stock Entry")
	se.company = frappe.db.get_single_value("FWMS Settings", "company")
	se.stock_entry_type = "Material Transfer"
	se.posting_date = nowdate()
	se.fwms_task = task
	se.fwms_worker = worker
	row = se.append("items", {})
	row.item_code = item
	row.qty = qty
	row.s_warehouse = from_wh
	row.t_warehouse = to_wh
	row.use_serial_batch_fields = 1
	if batch_no or stock[0].batch_no:
		row.batch_no = batch_no or stock[0].batch_no
	if frappe.get_cached_value("Item", item, "has_serial_no"):
		serials = frappe.get_all(
			"Serial No",
			filters={"item_code": item, "warehouse": from_wh, "status": "Active"},
			pluck="name",
			limit=int(qty),
		)
		if len(serials) < int(qty):
			frappe.throw(f"Only {len(serials)} active serials of {item} in {from_wh} — cannot move {qty}")
		row.serial_no = "\n".join(serials)
	se.insert(ignore_permissions=True)
	se.submit()

	# FWMS truth: move the picked qty into a tote in the pick face (or whole LPN)
	from fwms.fwms.doctype.fwms_lpn.fwms_lpn import add_contents, find_lpn_by_barcode, remove_contents

	src_lpn = None
	lpn_rows = frappe.get_all("FWMS LPN", filters={"bin": from_bin, "status": "In Bin"}, pluck="name")
	for candidate in lpn_rows:
		rows = frappe.get_all(
			"FWMS LPN Item",
			filters={"parenttype": "FWMS LPN", "parent": candidate, "item": item},
			fields=["batch_no"],
		)
		if any(not batch_no or r.batch_no == batch_no for r in rows):
			src_lpn = candidate
			break

	dst = f"RPL-{to_bin}"
	if not find_lpn_by_barcode(dst):
		frappe.get_doc(
			{
				"doctype": "FWMS LPN",
				"barcode": dst,
				"lpn_type": "Tote",
				"status": "In Bin",
				"bin": to_bin,
				"zone": to_zone,
				"primary_item": item,
			}
		).insert(ignore_permissions=True)
	else:
		frappe.db.set_value(
			"FWMS LPN",
			dst,
			{
				"bin": to_bin,
				"zone": to_zone,
				"status": "In Bin",
			},
			update_modified=True,
		)

	if src_lpn:
		remove_contents(src_lpn, item, qty, batch_no=batch_no)
	add_contents(dst, item, qty, batch_no=batch_no)

	complete_task(task, result_note=f"replenished {qty:g} {item} {from_bin} -> {to_bin}")
	emit(
		"Putaway & Storage",
		f"Replenished {qty:g} x {item}: {from_bin} -> {to_bin}",
		subtype="replenishment",
		worker=worker,
		bin=to_bin,
		reference_doctype="Stock Entry",
		reference_name=se.name,
		payload={"from_bin": from_bin, "to_bin": to_bin, "qty": qty, "item": item},
	)
	return {"stock_entry": se.name, "to_bin": to_bin, "qty": qty}
