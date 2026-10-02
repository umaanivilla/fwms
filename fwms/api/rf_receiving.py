# RF Receiving: scan PO -> items -> QC -> Purchase Receipt into an LPN.

from __future__ import annotations

import frappe
from frappe.utils import flt, nowdate

from fwms.api.rf_common import as_list, current_worker, require_operator
from fwms.events import emit
from fwms.tasks import create_task
from fwms.utils import get_next_lpn_barcode, get_warehouse_for_type


@frappe.whitelist()
def po_info(po: str) -> dict:
	"""PO header + items with pending receive qty."""
	require_operator()
	po_doc = frappe.get_doc("Purchase Order", po)
	if po_doc.docstatus != 1:
		frappe.throw(f"Purchase Order {po} is not submitted")
	items = []
	for row in po_doc.items:
		pending = flt(row.stock_qty) - flt(row.received_qty)
		if pending <= 0:
			continue
		items.append({
			"item_code": row.item_code,
			"item_name": row.item_name,
			"pending_qty": pending,
			"uom": row.stock_uom,
			"name": row.name,
			"has_batch_no": bool(frappe.get_cached_value("Item", row.item_code, "has_batch_no")),
			"has_serial_no": bool(frappe.get_cached_value("Item", row.item_code, "has_serial_no")),
		})
	return {
		"po": po,
		"supplier": po_doc.supplier,
		"supplier_name": po_doc.supplier_name,
		"pending_count": len(items),
		"fully_received": len(items) == 0,
		"status": po_doc.status,
		"items": items,
	}


@frappe.whitelist()
def commit_receipt(po: str, lpn_barcode: str | None, lines: list[dict]) -> dict:
	"""Create + submit Purchase Receipt and LPN; queue putaway.

	lines: [{item_code, qty, qc: "Pass"|"Fail", batch_no?, expiry_date?, serials?: [..]}]
	"""
	require_operator()
	worker = current_worker()
	lines = as_list(lines, "lines")
	po_doc = frappe.get_doc("Purchase Order", po)
	if po_doc.docstatus != 1:
		frappe.throw(f"Purchase Order {po} is not submitted")

	receiving_wh = get_warehouse_for_type("Receiving Docks")
	qc_wh = get_warehouse_for_type("QC Hold")
	if not receiving_wh or not qc_wh:
		frappe.throw("FWMS Settings/zones are missing Receiving Docks or QC Hold warehouses")

	# idempotency: one receipt per (PO, LPN) pair
	if lpn_barcode and frappe.db.exists("FWMS LPN", lpn_barcode):
		existing_pr = frappe.db.get_value(
			"Purchase Receipt", {"fwms_po": po, "fwms_lpn": lpn_barcode, "docstatus": 1}, "name"
		)
		if existing_pr:
			return {"purchase_receipt": existing_pr, "lpn": lpn_barcode, "status": "already_received"}

	pr = frappe.new_doc("Purchase Receipt")
	pr.company = po_doc.company
	pr.supplier = po_doc.supplier
	pr.posting_date = nowdate()
	pr.set_posting_time = 1
	# the PR must match its PO's currency (validate_with_previous_doc)
	pr.currency = po_doc.currency
	pr.conversion_rate = po_doc.conversion_rate or 1
	pr.buying_price_list = po_doc.buying_price_list if hasattr(po_doc, "buying_price_list") else None

	for line in lines:
		qty = flt(line.get("qty"))
		if qty <= 0:
			continue
		item = line["item_code"]
		qc = line.get("qc", "Pass")
		po_row = next((r for r in po_doc.items if r.item_code == item), None)
		if not po_row:
			frappe.throw(f"{item} is not on Purchase Order {po}")

		row = pr.append("items", {})
		row.item_code = item
		row.qty = qty
		row.uom = po_row.stock_uom
		row.stock_uom = po_row.stock_uom
		row.conversion_factor = 1
		row.rate = po_row.rate
		row.purchase_order = po
		row.purchase_order_item = po_row.name
		row.warehouse = receiving_wh
		if qc == "Fail":
			row.rejected_warehouse = qc_wh
		row.use_serial_batch_fields = 1
		if line.get("batch_no"):
			row.batch_no = _ensure_batch(item, line["batch_no"])
		if line.get("serials"):
			row.serial_no = "\n".join(line["serials"])
		if qc == "Fail":
			row.rejected_qty = qty
			row.qty = 0
			row.received_qty = 0

	pr.fwms_po = po
	if lpn_barcode and frappe.db.exists("FWMS LPN", lpn_barcode):
		pr.fwms_lpn = lpn_barcode
	pr.insert(ignore_permissions=True)

	# auto-create batches for batch items without a scanned batch
	for row in pr.items:
		if row.use_serial_batch_fields and not row.batch_no and not row.serial_no:
			if frappe.get_cached_value("Item", row.item_code, "has_batch_no"):
				row.batch_no = _auto_batch(row.item_code, po)

	# LPN: reuse scanned container or mint the next tote
	if lpn_barcode and frappe.db.exists("FWMS LPN", lpn_barcode):
		lpn = frappe.get_doc("FWMS LPN", lpn_barcode)
	else:
		lpn = frappe.new_doc("FWMS LPN")
		lpn.barcode = lpn_barcode or get_next_lpn_barcode("Tote")
		lpn.lpn_type = "Tote"
		lpn.zone = frappe.db.get_value("FWMS Zone", {"zone_type": "Receiving Docks"}, "name")
	lpn.status = "Receiving"
	lpn.source_doctype = "Purchase Receipt"
	lpn.source_name = pr.name
	lpn.purchase_order = po

	for line, row in zip(lines, pr.items):
		qty = flt(line.get("qty"))
		if qty <= 0 or line.get("qc") == "Fail":
			continue
		existing = next(
			(r for r in lpn.items if r.item == line["item_code"] and (r.batch_no or None) == (row.batch_no or None)),
			None,
		)
		if existing:
			existing.qty = flt(existing.qty) + qty
		else:
			lpn.append("items", {
				"item": line["item_code"],
				"qty": qty,
				"batch_no": row.batch_no,
				"uom": frappe.get_cached_value("Item", line["item_code"], "stock_uom"),
			})
	lpn.save(ignore_permissions=True)
	pr.fwms_lpn = lpn.name
	pr.save(ignore_permissions=True)

	# Quality Inspections (ERPNext requires them BEFORE PR submit for
	# inspection-required items); failed lines are Rejected, passes Accepted
	needs_reload = False
	for line in lines:
		if flt(line.get("qty")) <= 0:
			continue
		if frappe.get_cached_value("Item", line["item_code"], "inspection_required_before_purchase"):
			_make_qi(pr, line["item_code"], flt(line["qty"]),
				status="Rejected" if line.get("qc") == "Fail" else "Accepted")
			needs_reload = True

	if needs_reload:
		pr = frappe.get_doc("Purchase Receipt", pr.name)  # QI bumps the PR timestamp
	pr.submit()

	create_task(
		"Putaway",
		item=None,
		qty=sum(flt(r.qty) for r in lpn.items),
		lpn=lpn.name,
		priority="1-High",
		references=[{
			"reference_doctype": "Purchase Receipt",
			"reference_name": pr.name,
			"note": "putaway from receiving",
		}],
	)

	emit(
		"Receiving & QC",
		f"Received {lpn.total_qty():g} units into {lpn.name} against {po} (PR {pr.name})",
		subtype="receipt_committed",
		worker=worker,
		lpn=lpn.name,
		reference_doctype="Purchase Receipt",
		reference_name=pr.name,
		payload={"po": po, "lines": lines},
	)
	return {"purchase_receipt": pr.name, "lpn": lpn.name, "status": "received"}


@frappe.whitelist()
def next_serials(item_code: str, qty: int) -> dict:
	"""Next N sequential serial codes for an item (SN-{ITEM}-00001 style),
	skipping any that already exist. Does NOT create them — they are created
	when the Purchase Receipt submits."""
	qty = min(int(qty), 500)
	prefix = f"SN-{item_code}-"
	existing = frappe.get_all(
		"Serial No",
		filters={"item_code": item_code, "name": ("like", f"{prefix}%")},
		pluck="name",
	)
	max_n = 0
	for name in existing:
		try:
			max_n = max(max_n, int(name[len(prefix):]))
		except ValueError:
			continue
	serials = [f"{prefix}{n:05d}" for n in range(max_n + 1, max_n + 1 + qty)]
	return {"item_code": item_code, "serials": serials}


def _ensure_batch(item: str, batch_no: str) -> str:
	"""RF may scan a batch that isn't in the system yet — create it."""
	if not frappe.db.exists("Batch", batch_no):
		expiry = frappe.get_cached_value("Item", item, "end_of_life")
		frappe.get_doc({
			"doctype": "Batch",
			"batch_id": batch_no,
			"item": item,
			"expiry_date": expiry,
		}).insert(ignore_permissions=True)
	return batch_no


def _auto_batch(item: str, po: str) -> str:
	"""Deterministic auto batch per (item, PO) so retries don't create duplicates."""
	batch_id = f"RCV-{po}-{item}"[:140]
	if not frappe.db.exists("Batch", batch_id):
		expiry = frappe.get_cached_value("Item", item, "end_of_life")
		frappe.get_doc({
			"doctype": "Batch",
			"batch_id": batch_id,
			"item": item,
			"expiry_date": expiry,
		}).insert(ignore_permissions=True)
	return batch_id


def _make_qi(pr, item: str, qty: float, status: str = "Rejected") -> None:
	"""Quality Inspection against a draft PR row (required before submit)."""
	row = next((r for r in pr.items if r.item_code == item), None)
	if not row:
		return
	qi = frappe.new_doc("Quality Inspection")
	qi.inspection_type = "Incoming"
	qi.reference_type = "Purchase Receipt"
	qi.reference_name = pr.name
	qi.item_code = item
	qi.sample_size = qty
	qi.inspected_by = frappe.session.user
	qi.status = status
	qi.readings = []
	qi.insert(ignore_permissions=True)
	qi.submit()
