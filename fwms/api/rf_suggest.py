# RF suggestion dropdowns: short, context-aware pick lists for every scan
# field. Typed text filters by substring; empty text shows the most relevant
# records first. Read-only.

from __future__ import annotations

import frappe

from fwms.api.rf_common import require_operator

LIMIT = 10


@frappe.whitelist()
def suggest(context: str, txt: str = "") -> list[dict]:
	"""Suggestions for an RF scan field.

	context: po | bin | lpn | item | dock | dn | cart | packed_cart
	returns: [{value, label, desc}]
	"""
	require_operator()
	txt = (txt or "").strip()

	if context == "po":
		rows = frappe.get_all(
			"Purchase Order",
			filters={
				"docstatus": 1,
				"status": ("in", ("To Receive and Bill", "To Receive")),
				"per_received": ("<", 100),
				"name": ("like", f"%{txt}%"),
			},
			fields=["name", "supplier_name", "status"],
			order_by="creation desc",
			limit=LIMIT,
		)
		return [
			{"value": r.name, "label": r.name, "desc": f"{r.supplier_name or ''} · {r.status}"} for r in rows
		]

	if context == "bin":
		rows = frappe.get_all(
			"FWMS Bin",
			filters={"name": ("like", f"%{txt}%")},
			fields=["name", "zone", "abc_class", "is_pick_face"],
			order_by="name",
			limit=LIMIT * 3,
		)
		out = []
		for r in rows:
			zone = frappe.db.get_value("FWMS Zone", r.zone, "zone_type")
			out.append(
				{
					"value": r.name,
					"label": r.name,
					"desc": f"{zone or ''}{' · PICK FACE' if r.is_pick_face else ''} · class {r.abc_class or '—'}",
				}
			)
		return out[:LIMIT]

	if context == "lpn":
		rows = frappe.get_all(
			"FWMS LPN",
			filters={"status": ("in", ("In Bin", "Receiving", "Picking")), "name": ("like", f"%{txt}%")},
			fields=["name", "lpn_type", "status", "zone"],
			order_by="modified desc",
			limit=LIMIT,
		)
		return [
			{"value": r.name, "label": r.name, "desc": f"{r.lpn_type} · {r.status} · {r.zone or ''}"}
			for r in rows
		]

	if context == "cart":
		rows = frappe.get_all(
			"FWMS LPN",
			filters={
				"lpn_type": ("in", ("Cart", "Tote")),
				"status": ("in", ("Picking", "Empty")),
				"name": ("like", f"%{txt}%"),
			},
			fields=["name", "status"],
			order_by="modified desc",
			limit=LIMIT,
		)
		return [{"value": r.name, "label": r.name, "desc": r.status} for r in rows]

	if context == "packed_cart":
		rows = frappe.get_all(
			"FWMS LPN",
			filters={"lpn_type": "Cart", "status": "Packed", "name": ("like", f"%{txt}%")},
			fields=["name", "delivery_note", "sales_order"],
			order_by="modified desc",
			limit=LIMIT,
		)
		return [
			{
				"value": r.name,
				"label": r.name,
				"desc": f"DN {r.delivery_note or '—'} · SO {r.sales_order or '—'}",
			}
			for r in rows
		]

	if context == "item":
		rows = frappe.get_all(
			"Item",
			filters={"is_stock_item": 1, "disabled": 0, "name": ("like", f"%{txt}%")},
			fields=["name", "item_name", "stock_uom"],
			order_by="name",
			limit=LIMIT,
		)
		return [{"value": r.name, "label": r.name, "desc": r.item_name or ""} for r in rows]

	if context == "dock":
		rows = frappe.get_all(
			"FWMS Dock Door",
			filters={"name": ("like", f"%{txt}%")},
			fields=["name", "direction", "status", "trailer"],
			order_by="door_code",
			limit=LIMIT,
		)
		return [
			{
				"value": r.name,
				"label": r.name,
				"desc": f"{r.direction} · {r.status}{f' · {r.trailer}' if r.trailer else ''}",
			}
			for r in rows
		]

	if context == "dn":
		rows = frappe.get_all(
			"Delivery Note",
			filters={"docstatus": 1, "name": ("like", f"%{txt}%")},
			fields=["name", "customer_name", "posting_date"],
			order_by="creation desc",
			limit=LIMIT,
		)
		return [
			{"value": r.name, "label": r.name, "desc": f"{r.customer_name or ''} · {r.posting_date}"}
			for r in rows
		]

	frappe.throw(f"Unknown suggestion context: {context}")
