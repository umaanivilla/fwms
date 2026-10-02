# Copyright (c) 2026
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt


class FWMSLPN(Document):
	def before_save(self):
		# keep zone in sync with bin for fast floor queries
		if self.bin and not self.zone:
			self.zone = frappe.db.get_value("FWMS Bin", self.bin, "zone")

	def total_qty(self, item: str | None = None) -> float:
		total = 0.0
		for row in self.items or []:
			if row.qty and (item is None or row.item == item):
				total += flt(row.qty)
		return total


def find_lpn_by_barcode(barcode: str) -> str | None:
	"""Resolve a scanned barcode to an LPN (name == barcode)."""
	if frappe.db.exists("FWMS LPN", barcode):
		return barcode
	return None


def add_contents(lpn_name: str, item: str, qty: float, batch_no: str | None = None,
	serial_and_batch_bundle: str | None = None, uom: str | None = None) -> None:
	"""Accumulate qty onto an existing (item, batch) row or append a new row."""
	lpn = frappe.get_doc("FWMS LPN", lpn_name)
	uom = uom or frappe.db.get_value("Item", item, "stock_uom")
	for row in lpn.items or []:
		if row.item == item and (row.batch_no or None) == (batch_no or None):
			row.qty = flt(row.qty) + flt(qty)
			if serial_and_batch_bundle:
				row.serial_and_batch_bundle = serial_and_batch_bundle
			break
	else:
		lpn.append("items", {
			"item": item,
			"qty": flt(qty),
			"batch_no": batch_no,
			"serial_and_batch_bundle": serial_and_batch_bundle,
			"uom": uom,
		})
	lpn.save(ignore_permissions=True)


def remove_contents(lpn_name: str, item: str, qty: float, batch_no: str | None = None) -> None:
	"""Decrement qty from an (item, batch) row; raise if insufficient."""
	lpn = frappe.get_doc("FWMS LPN", lpn_name)
	qty = flt(qty)
	for row in lpn.items or []:
		if row.item == item and (row.batch_no or None) == (batch_no or None):
			if flt(row.qty) < qty:
				frappe.throw(f"LPN {lpn_name} has only {row.qty} of {item}"
					f" (batch {batch_no or '-'}) — cannot remove {qty}")
			row.qty = flt(row.qty) - qty
			break
	else:
		frappe.throw(f"LPN {lpn_name} has no {item} (batch {batch_no or '-'})")
	lpn.save(ignore_permissions=True)


def move_lpn(lpn_name: str, bin_name: str | None, zone: str | None = None,
	status: str | None = None) -> "FWMSLPN":
	"""Move an LPN to a bin/zone and optionally change status."""
	lpn = frappe.get_doc("FWMS LPN", lpn_name)
	if bin_name:
		lpn.bin = bin_name
		lpn.zone = frappe.db.get_value("FWMS Bin", bin_name, "zone") or zone
	elif zone:
		lpn.zone = zone
	if status:
		lpn.status = status
	lpn.save(ignore_permissions=True)
	return lpn
