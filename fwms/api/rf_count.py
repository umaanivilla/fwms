# RF Cycle Counting: blind/expected bin counts, variance -> Stock
# Reconciliation draft for Inventory Control approval.

from __future__ import annotations

import frappe
from frappe.utils import flt

from fwms.api.rf_common import as_list, current_worker, require_operator
from fwms.events import emit
from fwms.fwms.doctype.fwms_bin.fwms_bin import get_bin_stock
from fwms.utils import get_warehouse_for_type


@frappe.whitelist()
def start(bin_name: str) -> dict:
	"""Open a count for a bin: expected stock hidden unless the program says otherwise."""
	require_operator()
	bin_doc = frappe.get_doc("FWMS Bin", bin_name)
	method = "Blind"
	program = None
	# find the count program that covers this bin (via active task if any)
	task_name = frappe.db.get_value(
		"FWMS Task",
		filters={"task_type": "Count", "to_bin": bin_name, "status": ("in", ("Open", "Assigned", "In Progress"))},
		fieldname="name",
	)
	task = {"name": task_name} if task_name else None
	if task:
		task_doc = frappe.get_doc("FWMS Task", task_name)
		for ref in task_doc.references or []:
			if ref.reference_doctype == "FWMS Count Program":
				program = ref.reference_name
				method = ref.note or "Blind"
				break
	if not program:
		method = frappe.db.get_single_value("FWMS Settings", "default_count_method") or "Blind"

	expected = get_bin_stock(bin_name)
	return {
		"bin": bin_name,
		"zone": bin_doc.zone,
		"method": method,
		"program": program,
		"task": task.name if task else None,
		"expected": expected if method == "Expected" else None,
	}


@frappe.whitelist()
def submit(bin_name: str, counted: list[dict], task: str | None = None) -> dict:
	"""Record counted qty; adjust FWMS truth + raise SR draft for approval.

	counted: [{item, qty, batch_no?}]
	"""
	require_operator()
	worker = current_worker()
	counted = as_list(counted, "counted")
	bin_doc = frappe.get_doc("FWMS Bin", bin_name)
	expected = get_bin_stock(bin_name)
	counted = _enrich_batches(counted, expected, frappe.db.get_value("FWMS Zone", bin_doc.zone, "warehouse"))

	variances = _diff(expected, counted)

	# FWMS-side truth: LPN contents in this bin are rewritten to the counted qty
	_apply_count_to_lpns(bin_name, counted)

	sr_name = None
	if variances:
		sr_name = _make_stock_reconciliation(bin_doc, counted, worker, task)

	total_expected = sum(flt(r["qty"]) for r in expected)
	total_counted = sum(flt(r["qty"]) for r in counted)
	variance = total_counted - total_expected
	accuracy = 100.0 * (
		1 - (abs(variance) / total_expected if total_expected else 0)
	)

	frappe.db.set_value("FWMS Bin", bin_name, {
		"last_counted_on": frappe.utils.today(),
		"last_count_variance": variance,
		"last_count_accuracy": round(max(accuracy, 0), 2),
	}, update_modified=True)

	if task:
		from fwms.tasks import complete_task

		complete_task(task, result_note=f"count {bin_name}: variance {variance:g}")

	emit(
		"Inventory Control",
		f"Count {bin_name}: expected {total_expected:g}, counted {total_counted:g}, variance {variance:g}"
		+ (f" — SR {sr_name} awaits approval" if sr_name else " — clean"),
		subtype="cycle_count",
		severity="Warning" if variance else "Info",
		worker=worker,
		bin=bin_name,
		zone=bin_doc.zone,
		reference_doctype="Stock Reconciliation",
		reference_name=sr_name,
		payload={"variances": variances, "accuracy": round(max(accuracy, 0), 2)},
	)
	return {
		"bin": bin_name,
		"variance": variance,
		"accuracy": round(max(accuracy, 0), 2),
		"stock_reconciliation": sr_name,
		"variances": variances,
	}


def _diff(expected: list[dict], counted: list[dict]) -> list[dict]:
	def key(row):
		return (row.get("item"), row.get("batch_no") or None)

	exp_map = {key(r): flt(r.get("qty")) for r in expected}
	cnt_map = {key(r): flt(r.get("qty")) for r in counted}
	variances = []
	for k in set(exp_map) | set(cnt_map):
		diff = cnt_map.get(k, 0) - exp_map.get(k, 0)
		if abs(diff) > 1e-9:
			variances.append({"item": k[0], "batch_no": k[1], "variance": diff})
	return variances


def _apply_count_to_lpns(bin_name: str, counted: list[dict]) -> None:
	"""Rewrite LPN contents in the bin to match the count (single-LPN bins)."""
	lpns = frappe.get_all(
		"FWMS LPN", filters={"bin": bin_name, "status": ("in", ("In Bin", "Receiving", "Picking"))}, pluck="name"
	)
	if len(lpns) > 1:
		# multi-LPN bins: keep FWMS truth as-is; SR carries the ledger correction
		return
	if not lpns:
		if counted:
			# stock found where FWMS has none — record onto a new tote in that bin
			frappe.get_doc({
				"doctype": "FWMS LPN",
				"barcode": f"CNT-{bin_name}",
				"lpn_type": "Tote",
				"status": "In Bin",
				"bin": bin_name,
				"zone": frappe.db.get_value("FWMS Bin", bin_name, "zone"),
				"items": [
					{"item": r["item"], "qty": flt(r["qty"]), "batch_no": r.get("batch_no")}
					for r in counted if flt(r["qty"]) > 0
				],
			}).insert(ignore_permissions=True)
		return

	lpn = frappe.get_doc("FWMS LPN", lpns[0])
	lpn.items = []
	for row in counted:
		if flt(row["qty"]) > 0:
			lpn.append("items", {
				"item": row["item"],
				"qty": flt(row["qty"]),
				"batch_no": row.get("batch_no"),
			})
	lpn.save(ignore_permissions=True)


def _enrich_batches(counted: list[dict], expected: list[dict], zone_wh: str | None) -> list[dict]:
	"""Blind counts may omit batch — fill it from expected stock when unambiguous."""
	expected_batches: dict[str, set[str]] = {}
	for row in expected:
		expected_batches.setdefault(row.get("item"), set()).add(row.get("batch_no"))
	for row in counted:
		if row.get("batch_no"):
			continue
		if not frappe.get_cached_value("Item", row["item"], "has_batch_no"):
			continue
		batches = expected_batches.get(row["item"]) or set()
		if len(batches) == 1:
			row["batch_no"] = next(iter(batches))
		elif zone_wh:
			# surplus found where FWMS expected none: attribute to the only
			# live batch of this item in the zone, if there is exactly one
			from erpnext.stock.doctype.batch.batch import get_batch_qty

			live = [b for b in (get_batch_qty(item_code=row["item"], warehouse=zone_wh) or []) if b.qty > 0]
			if len(live) == 1:
				row["batch_no"] = live[0].batch_no
		if not row.get("batch_no"):
			frappe.throw(
				f"Item {row['item']} is batch-tracked — scan the batch (multiple batches are possible)"
			)
	return counted


def _make_stock_reconciliation(bin_doc, counted: list[dict], worker: str, task: str | None) -> str:
	zone_wh = frappe.db.get_value("FWMS Zone", bin_doc.zone, "warehouse")
	if not zone_wh:
		frappe.throw(f"Zone {bin_doc.zone} has no ERP warehouse — cannot raise Stock Reconciliation")
	sr = frappe.new_doc("Stock Reconciliation")
	sr.company = frappe.db.get_single_value("FWMS Settings", "company")
	sr.purpose = "Stock Reconciliation"
	sr.set_posting_time = 1
	sr.posting_date = frappe.utils.today()
	sr.fwms_bin = bin_doc.name
	sr.fwms_task = task
	for row in counted:
		qty = flt(row["qty"])
		if qty <= 0:
			continue
		target = sr.append("items", {})
		target.item_code = row["item"]
		target.warehouse = zone_wh
		target.qty = qty
		target.count_serial_and_batch_bundle_manually = 0
		if row.get("batch_no"):
			target.use_serial_batch_fields = 1
			target.batch_no = row["batch_no"]
	sr.insert(ignore_permissions=True)
	return sr.name
