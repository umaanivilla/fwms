# RF Packing: validate pick carts against their order, create Packing Slip
# + ZPL carton label, move the carton to Packed.

from __future__ import annotations

import frappe
from frappe.utils import flt

from fwms.api.rf_common import current_worker, require_operator
from fwms.events import emit


@frappe.whitelist()
def queue() -> list[dict]:
	"""Pack queue: carts done picking, awaiting packing."""
	require_operator()
	carts = frappe.get_all(
		"FWMS LPN",
		filters={"lpn_type": "Cart", "status": "Picking"},
		fields=["name", "sales_order", "zone"],
		order_by="modified asc",
		limit=50,
	)
	out = []
	for cart in carts:
		items = frappe.get_all(
			"FWMS LPN Item",
			filters={"parenttype": "FWMS LPN", "parent": cart.name},
			fields=["item", "qty", "batch_no"],
		)
		out.append({**cart, "items": items})
	return out


@frappe.whitelist()
def start(cart: str) -> dict:
	"""Resolve the cart's order + draft Delivery Note for packing validation."""
	require_operator()
	cart_doc = frappe.get_doc("FWMS LPN", cart)
	dn = _resolve_dn(cart_doc)
	if not dn:
		frappe.throw(f"No draft Delivery Note found for cart {cart} — finish picking first")

	dn_items = frappe.get_all(
		"Delivery Note Item", filters={"parent": dn}, fields=["item_code", "qty", "name"]
	)
	return {"cart": cart, "delivery_note": dn, "cart_items": cart_doc.items, "dn_items": dn_items}


def _resolve_dn(cart_doc) -> str | None:
	"""cart -> sales order -> wave order -> draft DN (fallback: DN containing the cart items)."""
	if (
		cart_doc.delivery_note
		and frappe.db.get_value("Delivery Note", cart_doc.delivery_note, "docstatus") == 0
	):
		return cart_doc.delivery_note
	if cart_doc.sales_order:
		dn = frappe.db.get_value("FWMS Wave Order", {"sales_order": cart_doc.sales_order}, "delivery_note")
		if dn and frappe.db.get_value("Delivery Note", dn, "docstatus") == 0:
			return dn
	# fallback: newest draft DN that contains all the cart's items
	cart_items = {r.item for r in cart_doc.items or []}
	if cart_items:
		for dn_name in frappe.get_all(
			"Delivery Note", {"docstatus": 0}, pluck="name", order_by="creation desc", limit=20
		):
			dn_items = set(frappe.get_all("Delivery Note Item", {"parent": dn_name}, pluck="item_code"))
			if dn_items and cart_items.issubset(dn_items):
				return dn_name
	return None


@frappe.whitelist()
def finish(cart: str, delivery_note: str, case_no: int = 1) -> dict:
	"""Create Packing Slip vs the DN, print carton label, mark cart Packed."""
	require_operator()
	worker = current_worker()
	cart_doc = frappe.get_doc("FWMS LPN", cart)

	ps = frappe.new_doc("Packing Slip")
	ps.delivery_note = delivery_note
	ps.from_case_no = case_no
	ps.to_case_no = case_no
	ps.net_weight_pkg = 0
	ps.gross_weight_pkg = 0

	# map cart contents onto Delivery Note item rows (dn_detail is mandatory)
	dn_rows = frappe.get_all(
		"Delivery Note Item",
		filters={"parent": delivery_note},
		fields=["name", "item_code", "qty"],
	)
	dn_remaining = {r.name: flt(r.qty) for r in dn_rows}
	rows_by_item: dict[str, list[str]] = {}
	for r in dn_rows:
		rows_by_item.setdefault(r.item_code, []).append(r.name)

	def consume_dn_row(item: str, qty: float) -> str | None:
		for dn_detail in rows_by_item.get(item, []):
			if dn_remaining.get(dn_detail, 0) > 0:
				return dn_detail
		return None

	for row in cart_doc.items:
		if flt(row.qty) <= 0:
			continue
		ps_row = ps.append("items", {})
		ps_row.item_code = row.item
		ps_row.qty = row.qty
		ps_row.stock_uom = row.uom or frappe.get_cached_value("Item", row.item, "stock_uom")
		ps_row.batch_no = row.batch_no
		ps_row.dn_detail = consume_dn_row(row.item, flt(row.qty))
		if ps_row.dn_detail:
			dn_remaining[ps_row.dn_detail] = dn_remaining.get(ps_row.dn_detail, 0) - flt(row.qty)
	ps.insert(ignore_permissions=True)
	ps.submit()

	cart_doc.status = "Packed"
	cart_doc.delivery_note = delivery_note
	cart_doc.zone = frappe.db.get_value("FWMS Zone", {"zone_type": "Packing"}, "name")
	cart_doc.save(ignore_permissions=True)

	from fwms.printing.zpl import render_lpn_label

	emit(
		"Packing",
		f"Carton {cart} packed against {delivery_note} (Packing Slip {ps.name})",
		subtype="packed",
		worker=worker,
		lpn=cart,
		reference_doctype="Packing Slip",
		reference_name=ps.name,
		payload={"cart": cart, "delivery_note": delivery_note},
	)
	return {"cart": cart, "packing_slip": ps.name, "zpl": render_lpn_label(cart_doc)}


@frappe.whitelist()
def next_case_no(delivery_note: str) -> int:
	require_operator()
	count = frappe.db.count("Packing Slip", {"delivery_note": delivery_note})
	return count + 1
