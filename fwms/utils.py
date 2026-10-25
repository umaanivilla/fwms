# FWMS shared helpers: settings access, barcode resolution, zone/warehouse maps.

from __future__ import annotations

import frappe
from frappe.utils import flt


def get_settings() -> dict:
	return frappe.get_cached_doc("FWMS Settings").as_dict()


def get_company() -> str:
	company = frappe.db.get_single_value("FWMS Settings", "company")
	if not company:
		frappe.throw("FWMS Settings: Company is not set")
	return company


def get_zone_by_type(zone_type: str) -> str | None:
	"""First FWMS Zone matching a zone type (e.g. 'Pick Faces')."""
	zones = frappe.get_all("FWMS Zone", filters={"zone_type": zone_type}, pluck="name", limit=1)
	return zones[0] if zones else None


def get_zones_by_type(zone_type: str) -> list[str]:
	return frappe.get_all("FWMS Zone", filters={"zone_type": zone_type}, pluck="name")


def get_zone_warehouse(zone_name: str) -> str | None:
	return frappe.db.get_value("FWMS Zone", zone_name, "warehouse")


def get_warehouse_for_type(zone_type: str) -> str | None:
	zone = get_zone_by_type(zone_type)
	return get_zone_warehouse(zone) if zone else None


def resolve_scan(barcode: str) -> dict:
	"""Resolve a scanned barcode to a known entity.

	Returns {type, name, label} where type in
	item/batch/lpn/bin/dock/so/po/dn/worker or raises nothing (returns type=unknown).
	"""
	barcode = (barcode or "").strip()
	if not barcode:
		return {"type": "unknown", "name": None}

	if frappe.db.exists("FWMS LPN", barcode):
		return {"type": "lpn", "name": barcode, "label": barcode}
	if frappe.db.exists("FWMS Bin", barcode):
		return {"type": "bin", "name": barcode, "label": barcode}
	if frappe.db.exists("FWMS Dock Door", barcode):
		return {"type": "dock", "name": barcode, "label": barcode}

	# item by code
	if frappe.db.exists("Item", barcode):
		return {"type": "item", "name": barcode, "label": barcode}
	# item by Item Barcode child
	hit = frappe.db.get_value("Item Barcode", {"barcode": barcode}, ["parent", "idx"], as_dict=False)
	if hit:
		return {"type": "item", "name": hit, "label": barcode}
	if frappe.db.exists("Batch", barcode):
		return {"type": "batch", "name": barcode, "label": barcode}
	if frappe.db.exists("Sales Order", barcode):
		return {"type": "so", "name": barcode, "label": barcode}
	if frappe.db.exists("Purchase Order", barcode):
		return {"type": "po", "name": barcode, "label": barcode}
	if frappe.db.exists("Delivery Note", barcode):
		return {"type": "dn", "name": barcode, "label": barcode}

	return {"type": "unknown", "name": barcode, "label": barcode}


def get_next_lpn_barcode(lpn_type: str) -> str:
	"""Sequential scannable barcode for a new container, e.g. LPN-000042.

	A MariaDB advisory lock serializes concurrent draws; without it two
	simultaneous receipts could compute the same barcode. The window between
	computing and inserting is narrowed, not eliminated — a true collision
	surfaces as a request error rather than silent corruption.
	"""
	prefix = {"Tote": "LPN", "Carton": "CART", "Pallet": "PLT", "Cart": "CART"}.get(lpn_type, "LPN")
	lock = f"fwms_lpn_seq_{prefix}"
	got = frappe.db.sql("SELECT GET_LOCK(%s, 5)", (lock,))[0][0]
	try:
		series = frappe.db.get_all("FWMS LPN", filters={"barcode": ("like", f"{prefix}-%")}, pluck="barcode")
		max_n = 0
		for code in series:
			try:
				max_n = max(max_n, int(code.split("-")[-1]))
			except ValueError:
				continue
		candidate = f"{prefix}-{max_n + 1:06d}"
		while got and frappe.db.exists("FWMS LPN", candidate):
			max_n += 1
			candidate = f"{prefix}-{max_n + 1:06d}"
		return candidate
	finally:
		if got:
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock,))


def fefo_sort_batches(batches: list[dict]) -> list[dict]:
	"""Sort [{batch_no, qty, ...}] FEFO (earliest expiry first) or FIFO (oldest receipt).

	Items without expiry fall back to FIFO ordering inside their group.
	"""

	def key(row):
		expiry = (
			frappe.db.get_value("Batch", row.get("batch_no"), "expiry_date") if row.get("batch_no") else None
		)
		created = (
			frappe.db.get_value("Batch", row.get("batch_no"), "creation") if row.get("batch_no") else "9999"
		)
		strategy = frappe.db.get_single_value("FWMS Settings", "pick_strategy") or "FEFO"
		if strategy == "FEFO" and expiry:
			return (0, str(expiry), str(created))
		if expiry:
			return (1, str(expiry), str(created))
		return (2, "9999-12-31", str(created) if created else "9999")

	return sorted(batches, key=key)


def is_batched_item(item: str) -> bool:
	return bool(frappe.get_cached_value("Item", item, "has_batch_no"))


def is_serial_item(item: str) -> bool:
	return bool(frappe.get_cached_value("Item", item, "has_serial_no"))
