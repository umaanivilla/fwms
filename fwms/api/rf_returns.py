# RF Returns: scan a Delivery Note, grade items (A/B/Refurb/Scrap) and push
# them back into the right zone via a Sales Return against the source DN.

from __future__ import annotations

import frappe
from frappe.utils import flt

from fwms.api.rf_common import current_worker, require_operator
from fwms.events import emit
from fwms.utils import get_warehouse_for_type as _zone_warehouse

GRADE_WAREHOUSE_ZONE = {
	"A": "Pick Faces",   # resellable
	"B": "QC Hold",      # needs inspection
	"Refurb": "Bulk",    # refurbish later
	"Scrap": None,       # scrap warehouse (no FWMS zone)
}


@frappe.whitelist()
def dn_info(delivery_note: str) -> dict:
	"""Returnable items on a shipped DN."""
	require_operator()
	dn = frappe.get_doc("Delivery Note", delivery_note)
	if dn.docstatus != 1:
		frappe.throw(f"Delivery Note {delivery_note} is not submitted")
	items = []
	for row in dn.items:
		returnable = flt(row.qty) - flt(row.returned_qty)
		if returnable <= 0:
			continue
		items.append({
			"item_code": row.item_code,
			"item_name": row.item_name,
			"returnable_qty": returnable,
			"name": row.name,
		})
	return {"delivery_note": delivery_note, "customer": dn.customer, "items": items}


@frappe.whitelist()
def process_return(delivery_note: str, lines: list[dict]) -> dict:
	"""Create a draft Sales Return per grade bucket; log disposition events.

	lines: [{item_code, qty, grade: A|B|Refurb|Scrap, batch_no?, serials?}]
	"""
	require_operator()
	worker = current_worker()
	dn = frappe.get_doc("Delivery Note", delivery_note)
	if dn.docstatus != 1:
		frappe.throw(f"Delivery Note {delivery_note} is not submitted")

	from erpnext.controllers.sales_and_purchase_return import make_return_doc

	by_grade: dict[str, list[dict]] = {}
	for line in lines:
		grade = line.get("grade", "A")
		if flt(line.get("qty")) <= 0:
			continue
		by_grade.setdefault(grade, []).append(line)

	returns = []
	for grade, grade_lines in by_grade.items():
		zone_type = GRADE_WAREHOUSE_ZONE[grade]
		ret = make_return_doc("Delivery Note", delivery_note)
		ret.customer = dn.customer
		ret.fwms_worker = worker
		keep = []
		for row in ret.items:
			match = next((l for l in grade_lines if l["item_code"] == row.item_code), None)
			if not match:
				continue
			row.qty = -flt(match["qty"])
			zone_wh = _zone_warehouse(zone_type)
			if zone_wh:
				row.warehouse = zone_wh
			if match.get("batch_no"):
				row.use_serial_batch_fields = 1
				row.batch_no = match["batch_no"]
			if match.get("serials"):
				row.use_serial_batch_fields = 1
				row.serial_no = "\n".join(match["serials"])
			keep.append(row.name)
		ret.items = [r for r in ret.items if r.name in keep and flt(r.qty) < 0]
		if not ret.items:
			continue
		ret.insert(ignore_permissions=True)
		returns.append({"grade": grade, "return": ret.name})

		# FWMS-side: LPN in the target zone carrying the returned stock
		if zone_type:
			zone = frappe.db.get_value("FWMS Zone", {"zone_type": zone_type}, "name")
			lpn_barcode = f"RET-{ret.name}"
			if not frappe.db.exists("FWMS LPN", lpn_barcode):
				frappe.get_doc({
					"doctype": "FWMS LPN",
					"barcode": lpn_barcode,
					"lpn_type": "Tote",
					"status": "In Bin",
					"zone": zone,
					"source_doctype": "Delivery Note",
					"source_name": delivery_note,
					"items": [
						{"item": l["item_code"], "qty": flt(l["qty"]), "batch_no": l.get("batch_no")}
						for l in grade_lines
					],
				}).insert(ignore_permissions=True)

		emit(
			"Returns",
			f"Return {ret.name} graded {grade} against {delivery_note}"
			f" ({sum(flt(l['qty']) for l in grade_lines):g} units)",
			subtype=f"return_grade_{grade.lower()}",
			worker=worker,
			reference_doctype="Delivery Note",
			reference_name=ret.name,
			payload={"grade": grade, "source_dn": delivery_note, "lines": grade_lines},
		)

	return {"returns": returns, "message": "Return drafted — submit in desk to post stock"}
