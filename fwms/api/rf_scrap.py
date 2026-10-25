# RF Scrap: scan an LPN, write off lines with a reason. Files a DRAFT
# Material Transfer to the Scrap warehouse; Inventory Control submits it in
# the desk (LPN truth realigns on submit via fwms.events.apply_scrap_to_lpn).

from __future__ import annotations

import frappe
from frappe.utils import flt

from fwms.api.rf_common import as_list, current_worker, require_operator
from fwms.events import emit
from fwms.utils import get_warehouse_for_type

SCRAP_REASONS = ("Damaged", "Expired", "Contamination", "Obsolete", "Quality Fail", "Other")


@frappe.whitelist()
def lpn_info(lpn: str) -> dict:
	"""LPN contents + location for the scrap screen."""
	require_operator()
	lpn_doc = frappe.get_doc("FWMS LPN", lpn)
	if lpn_doc.status in ("Closed", "Shipped"):
		frappe.throw(f"LPN {lpn} is {lpn_doc.status} — nothing to scrap")
	return {
		"lpn": lpn,
		"status": lpn_doc.status,
		"zone": lpn_doc.zone,
		"bin": lpn_doc.bin,
		"source_doctype": lpn_doc.source_doctype,
		"source_name": lpn_doc.source_name,
		"items": [
			{"item_code": r.item, "qty": flt(r.qty), "batch_no": r.batch_no or None, "uom": r.uom}
			for r in lpn_doc.items
			if flt(r.qty) > 0
		],
	}


@frappe.whitelist()
def scrap(lpn: str, lines: list[dict], reason: str) -> dict:
	"""Draft a Material Transfer (zone warehouse -> Scrap) for the given lines.

	lines: [{item_code, qty, batch_no?}] — validated against LPN contents.
	"""
	require_operator()
	worker = current_worker()
	if reason not in SCRAP_REASONS:
		frappe.throw(f"Reason must be one of: {', '.join(SCRAP_REASONS)}")
	lines = as_list(lines, "lines")

	lpn_doc = frappe.get_doc("FWMS LPN", lpn)
	if lpn_doc.status == "Closed":
		frappe.throw(f"LPN {lpn} is already closed")

	from_wh = frappe.db.get_value("FWMS Zone", lpn_doc.zone, "warehouse") if lpn_doc.zone else None
	if not from_wh:
		frappe.throw(f"LPN {lpn} has no stocked zone warehouse to scrap from")
	scrap_wh = get_warehouse_for_type("Scrap")
	if not scrap_wh:
		frappe.throw("Scrap zone/warehouse is not set up — run `bench --site <site> migrate`")

	se = frappe.new_doc("Stock Entry")
	se.company = frappe.db.get_single_value("FWMS Settings", "company")
	se.stock_entry_type = "Material Transfer"
	se.fwms_worker = worker
	se.fwms_lpn = lpn
	se.remarks = f"RF scrap ({reason})"
	for line in lines:
		qty = flt(line.get("qty"))
		if qty <= 0:
			continue
		match = next(
			(
				r
				for r in lpn_doc.items
				if r.item == line["item_code"] and (r.batch_no or None) == (line.get("batch_no") or None)
			),
			None,
		)
		if not match:
			frappe.throw(f"{line['item_code']} is not in LPN {lpn}")
		if qty > flt(match.qty):
			frappe.throw(f"Only {flt(match.qty):g} × {line['item_code']} in {lpn}, tried to scrap {qty:g}")  # noqa: RUF001
		row = se.append("items", {})
		row.item_code = line["item_code"]
		row.qty = qty
		row.s_warehouse = from_wh
		row.t_warehouse = scrap_wh
		row.use_serial_batch_fields = 1
		if match.batch_no:
			row.batch_no = match.batch_no
		if match.serial_and_batch_bundle:
			serials = frappe.get_all(
				"Serial and Batch Entry",
				filters={"parent": match.serial_and_batch_bundle, "parenttype": "Serial and Batch Bundle"},
				pluck="serial_no",
			)
			if serials:
				row.serial_no = "\n".join(s for s in serials[: int(qty)] if s)
	if not se.items:
		frappe.throw("Nothing to scrap — all line quantities were zero")
	se.insert(ignore_permissions=True)  # draft: Inventory Control submits

	total = sum(flt(d.qty) for d in se.items)
	emit(
		"Inventory Control",
		f"Scrap draft {se.name}: {total:g} units from {lpn} ({reason}) — awaiting approval",
		subtype="scrap",
		severity="Warning",
		worker=worker,
		zone=lpn_doc.zone,
		bin=lpn_doc.bin,
		lpn=lpn,
		reference_doctype="Stock Entry",
		reference_name=se.name,
		payload={"reason": reason, "lines": lines},
	)
	return {
		"stock_entry": se.name,
		"qty": total,
		"reason": reason,
		"status": "draft",
		"message": f"Scrap draft {se.name} created — Inventory Control submits it to post stock",
	}
