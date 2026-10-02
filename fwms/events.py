# FWMS event pipeline: every warehouse action lands in FWMS Event Log and is
# published to connected floor-view dashboards in realtime.

from __future__ import annotations

import frappe
from frappe.utils import now


def emit(
	category: str,
	message: str,
	subtype: str | None = None,
	severity: str = "Info",
	is_scan: bool | int = 0,
	worker: str | None = None,
	zone: str | None = None,
	bin: str | None = None,
	lpn: str | None = None,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	payload: dict | None = None,
) -> str:
	"""Insert an FWMS Event Log row and broadcast it over websocket."""
	doc = frappe.get_doc({
		"doctype": "FWMS Event Log",
		"category": category,
		"subtype": subtype,
		"severity": severity,
		"is_scan": 1 if is_scan else 0,
		"message": message,
		"worker": worker,
		"zone": zone,
		"bin": bin,
		"lpn": lpn,
		"reference_doctype": reference_doctype,
		"reference_name": reference_name,
		"payload": frappe.as_json(payload) if payload else None,
	})
	doc.insert(ignore_permissions=True)

	data = {
		"name": doc.name,
		"category": category,
		"subtype": subtype,
		"severity": severity,
		"is_scan": bool(is_scan),
		"message": message,
		"worker": worker,
		"zone": zone,
		"bin": bin,
		"lpn": lpn,
		"reference_doctype": reference_doctype,
		"reference_name": reference_name,
		"payload": payload,
		"creation": now(),
	}
	frappe.publish_realtime(event="fwms_event", message=data)
	return doc.name


# ---------------------------------------------------------------------------
# doc_events handlers (wired in hooks.py)
# ---------------------------------------------------------------------------

def on_sales_order_submit(doc, method=None):
	from fwms.wave import on_sales_order_submit as handle

	handle(doc, method)


def on_purchase_receipt_submit(doc, method=None):
	worker = getattr(doc, "fwms_worker", None)
	lpn = getattr(doc, "fwms_lpn", None)
	emit(
		"Receiving & QC",
		f"Purchase Receipt {doc.name} posted for {doc.supplier}",
		subtype="purchase_receipt",
		worker=worker,
		lpn=lpn,
		reference_doctype="Purchase Receipt",
		reference_name=doc.name,
		payload={"supplier": doc.supplier, "total_qty": sum(d.qty or 0 for d in doc.items)},
	)


def on_stock_entry_submit(doc, method=None):
	emit(
		"Putaway & Storage",
		f"Stock Entry {doc.name} ({doc.stock_entry_type or doc.purpose}) posted",
		subtype="stock_entry",
		reference_doctype="Stock Entry",
		reference_name=doc.name,
	)


def on_pick_list_submit(doc, method=None):
	emit(
		"Orders & Picking",
		f"Pick List {doc.name} submitted",
		subtype="pick_list_submitted",
		reference_doctype="Pick List",
		reference_name=doc.name,
	)


def on_delivery_note_submit(doc, method=None):
	from fwms.wave import on_delivery_note_shipped

	emit(
		"Sort Load & Ship",
		f"Delivery Note {doc.name} shipped to {doc.customer}",
		subtype="shipped",
		reference_doctype="Delivery Note",
		reference_name=doc.name,
		payload={"customer": doc.customer, "grand_total": doc.grand_total},
	)
	on_delivery_note_shipped(doc, method)


def on_packing_slip_submit(doc, method=None):
	emit(
		"Packing",
		f"Packing Slip {doc.name} completed",
		subtype="packing_slip",
		reference_doctype="Packing Slip",
		reference_name=doc.name,
	)


def on_stock_reconciliation_submit(doc, method=None):
	from fwms.api.rf_count import apply_count_to_lpns_for_sr

	apply_count_to_lpns_for_sr(doc)
	emit(
		"Inventory Control",
		f"Stock Reconciliation {doc.name} posted",
		subtype="stock_reconciliation",
		reference_doctype="Stock Reconciliation",
		reference_name=doc.name,
	)


def on_quality_inspection_update(doc, method=None):
	if doc.docstatus == 1:
		emit(
			"Receiving & QC",
			f"Quality Inspection {doc.name}: {doc.status}",
			subtype="quality_inspection",
			severity="Warning" if doc.status != "Accepted" else "Info",
			reference_doctype="Quality Inspection",
			reference_name=doc.name,
		)
