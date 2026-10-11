# RF Quality: work the QC-Hold queue. Receiving QC-fails and grade-B returns
# create "Quality" tasks carrying an LPN parked in QC Hold; operators inspect
# per line — Pass releases stock back into storage (directed putaway), Fail
# holds it in QC Hold with a Rejected Quality Inspection, and the RF offers
# to jump straight into the Scrap flow.

from __future__ import annotations

import frappe
from frappe.utils import flt

from fwms.api.rf_common import as_list, current_worker, require_operator
from fwms.events import emit
from fwms.tasks import complete_task
from fwms.utils import get_warehouse_for_type


@frappe.whitelist()
def open_tasks() -> list[dict]:
	"""Quality queue visible on the floor (Open + mine)."""
	require_operator()
	return frappe.get_all(
		"FWMS Task",
		filters={"task_type": "Quality", "status": ("in", ("Open", "Assigned", "In Progress"))},
		fields=["name", "status", "priority", "lpn", "qty", "worker"],
		order_by="priority asc, creation asc",
		limit=50,
	)


@frappe.whitelist()
def task_detail(task: str) -> dict:
	"""Task card: LPN contents + where the held stock came from."""
	require_operator()
	t = frappe.db.get_value("FWMS Task", task, ["name", "status", "priority", "lpn", "qty"], as_dict=True)
	if not t:
		frappe.throw(f"Task {task} not found")
	lpn_doc = frappe.get_doc("FWMS LPN", t.lpn)
	return {
		"task": t.name,
		"status": t.status,
		"lpn": t.lpn,
		"zone": lpn_doc.zone,
		"bin": lpn_doc.bin,
		"source_doctype": lpn_doc.source_doctype,
		"source_name": lpn_doc.source_name,
		"items": [
			{"item_code": r.item, "qty": flt(r.qty), "batch_no": r.batch_no or None, "uom": r.uom}
			for r in lpn_doc.items if flt(r.qty) > 0
		],
	}


@frappe.whitelist()
def inspect(lpn: str, results: list[dict], task: str | None = None) -> dict:
	"""Inspect every unit in a QC-Hold LPN.

	results: [{item_code, qty, verdict: "Pass"|"Fail", batch_no?}] — must cover
	the full LPN contents. Pass qty is released to storage (a directed putaway
	when the whole LPN passes, otherwise a fresh Bulk tote for the good part);
	Fail qty stays in the LPN on QC Hold with a Rejected Quality Inspection
	against the source document, ready for the Scrap flow if the operator
	chooses.
	"""
	require_operator()
	worker = current_worker()
	results = as_list(results, "results")

	lpn_doc = frappe.get_doc("FWMS LPN", lpn)
	if lpn_doc.status == "Closed":
		frappe.throw(f"LPN {lpn} is closed")
	zone_type = frappe.db.get_value("FWMS Zone", lpn_doc.zone, "zone_type") if lpn_doc.zone else None
	if zone_type != "QC Hold":
		frappe.throw(f"LPN {lpn} is not in a QC Hold zone (zone: {lpn_doc.zone or 'none'})")

	available = {
		(r.item, r.batch_no or None): flt(r.qty)
		for r in lpn_doc.items if flt(r.qty) > 0
	}
	# list, not dict: the same item+batch can carry split verdicts (6 pass / 4 fail)
	parsed = []
	for line in results:
		qty = flt(line.get("qty"))
		key = (line["item_code"], line.get("batch_no") or None)
		if key not in available:
			frappe.throw(f"{line['item_code']} is not in LPN {lpn}")
		if line.get("verdict") not in ("Pass", "Fail"):
			frappe.throw(f"Verdict for {line['item_code']} must be Pass or Fail")
		if qty <= 0 or qty > available[key]:
			frappe.throw(f"{line['item_code']}: inspect between 1 and {available[key]:g} units")
		available[key] = available[key] - qty
		parsed.append({"item_code": line["item_code"], "batch_no": key[1], "qty": qty, "verdict": line["verdict"]})
	if any(q > 0 for q in available.values()):
		missing = ", ".join(f"{qty:g} × {item}" for (item, _), qty in available.items() if qty > 0)
		frappe.throw(f"Every unit must get a verdict — still uninspected: {missing}")

	pass_lines = [l for l in parsed if l["verdict"] == "Pass"]
	fail_lines = [l for l in parsed if l["verdict"] == "Fail"]

	qis, released = [], None
	if pass_lines:
		released = (
			_release_whole_lpn(lpn_doc, worker)
			if not fail_lines
			else _release_partial(lpn_doc, pass_lines, worker)
		)

	for line in parsed:
		qi = _make_qi(lpn_doc, line["item_code"], line["qty"], line["verdict"])
		if qi:
			qis.append(qi)

	note = (
		f"{sum(l['qty'] for l in pass_lines):g} released"
		f" ({released['stock_entry']}), {sum(l['qty'] for l in fail_lines):g} held on QC Hold"
	)
	if task:
		complete_task(task, result_note=note)

	emit(
		"Receiving & QC",
		f"Quality disposition {lpn}: {note}",
		subtype="quality_disposition",
		severity="Warning" if fail_lines else "Info",
		worker=worker,
		zone=lpn_doc.zone,
		lpn=lpn,
		reference_doctype="Stock Entry" if (released and released.get("stock_entry")) else "FWMS LPN",
		reference_name=(released["stock_entry"] if released and released.get("stock_entry") else lpn),
		payload={"results": results},
	)
	return {
		"released": released,  # {stock_entry, lpn, bin, zone} | None
		"held": fail_lines,   # ready for the Scrap flow if the operator chooses
		"quality_inspections": qis,
		"task": task,
		"message": note,
	}


def _release_whole_lpn(lpn_doc, worker: str) -> dict:
	"""All units passed: a directed putaway of the whole LPN into storage."""
	from fwms.api.rf_putaway import confirm, suggest_bin

	suggested = suggest_bin(lpn_doc.name)
	out = confirm(lpn=lpn_doc.name, bin_name=suggested["bin"])
	return {"stock_entry": None, "lpn": out["lpn"], "bin": out["bin"], "zone": out["zone"]}


def _release_partial(lpn_doc, pass_lines: list[dict], worker: str) -> dict:
	"""Mixed verdicts: good units move to a fresh Bulk tote, fails stay put.

	Released stock goes to Bulk (one tote, one target) — replenishment or a
	later directed putaway can slot it into pick faces.
	"""
	from fwms.utils import get_next_lpn_barcode

	qc_wh = get_warehouse_for_type("QC Hold")
	bulk_zone = frappe.db.get_value("FWMS Zone", {"zone_type": "Bulk"}, "name")
	bulk_wh = frappe.db.get_value("FWMS Zone", bulk_zone, "warehouse")
	if not qc_wh or not bulk_wh:
		frappe.throw("QC Hold / Bulk zone warehouses are not set up")

	# aggregate split verdicts per (item, batch) before moving stock
	release: dict[tuple, float] = {}
	for line in pass_lines:
		key = (line["item_code"], line.get("batch_no") or None)
		release[key] = release.get(key, 0) + flt(line["qty"])

	# SE for the good units (serials: first N from the source bundle)
	se = frappe.new_doc("Stock Entry")
	se.company = frappe.db.get_single_value("FWMS Settings", "company")
	se.stock_entry_type = "Material Transfer"
	se.fwms_worker = worker
	for (item, batch), qty in release.items():
		match = next(
			(r for r in lpn_doc.items
				if r.item == item and (r.batch_no or None) == batch),
			None,
		)
		row = se.append("items", {})
		row.item_code = item
		row.qty = qty
		row.s_warehouse = qc_wh
		row.t_warehouse = bulk_wh
		row.use_serial_batch_fields = 1
		if batch:
			row.batch_no = batch
		if match.serial_and_batch_bundle:
			serials = frappe.get_all(
				"Serial and Batch Entry",
				filters={"parent": match.serial_and_batch_bundle, "parenttype": "Serial and Batch Bundle"},
				pluck="serial_no",
			)
			if serials:
				row.serial_no = "\n".join(s for s in serials[: int(qty)] if s)
	se.insert(ignore_permissions=True)
	se.submit()

	# fresh tote in the first bulk bin carrying the good units
	bulk_bin = frappe.get_all(
		"FWMS Bin", filters={"zone": bulk_zone}, pluck="name", order_by="creation asc", limit=1
	)
	tote = frappe.new_doc("FWMS LPN")
	tote.barcode = get_next_lpn_barcode("Tote")
	tote.lpn_type = "Tote"
	tote.status = "In Bin"
	tote.zone = bulk_zone
	tote.bin = bulk_bin[0] if bulk_bin else None
	tote.source_doctype = lpn_doc.source_doctype
	tote.source_name = lpn_doc.source_name
	for (item, batch), qty in release.items():
		tote.append("items", {
			"item": item,
			"qty": qty,
			"batch_no": batch,
		})
	tote.insert(ignore_permissions=True)

	# pull the released units out of the held LPN
	for (item, batch), qty in release.items():
		match = next(
			(r for r in lpn_doc.items
				if r.item == item and (r.batch_no or None) == batch),
			None,
		)
		if match:
			match.qty = flt(match.qty) - qty
	for r in list(lpn_doc.items):
		if flt(r.qty) <= 0:
			lpn_doc.remove(r)
	lpn_doc.save(ignore_permissions=True)

	return {"stock_entry": se.name, "lpn": tote.name, "bin": tote.bin, "zone": bulk_zone}


def _make_qi(lpn_doc, item_code: str, qty: float, verdict: str) -> str | None:
	"""QI against the document that put the stock on QC Hold (source of truth)."""
	if lpn_doc.source_doctype not in ("Purchase Receipt", "Delivery Note") or not lpn_doc.source_name:
		return None  # no referenceable source doc — verdict lives in the event/task
	qi = frappe.new_doc("Quality Inspection")
	qi.inspection_type = "Incoming" if lpn_doc.source_doctype == "Purchase Receipt" else "Outgoing"
	qi.reference_type = lpn_doc.source_doctype
	qi.reference_name = lpn_doc.source_name
	qi.item_code = item_code
	qi.sample_size = qty
	qi.inspected_by = frappe.session.user
	qi.status = "Accepted" if verdict == "Pass" else "Rejected"
	qi.readings = []
	qi.insert(ignore_permissions=True)
	qi.submit()
	return qi.name
