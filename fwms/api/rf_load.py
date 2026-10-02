# RF Load & Ship: scan packed cartons to a dock door/trailer, then submit
# the Delivery Note(s) — the trailer moment that closes the wave.

from __future__ import annotations

import frappe

from fwms.api.rf_common import current_worker, require_operator
from fwms.events import emit


@frappe.whitelist()
def load(cart: str, dock_door: str, trailer: str | None = None) -> dict:
	"""Scan carton -> dock door. Door goes Busy, cart waits in Shipping Docks."""
	require_operator()
	worker = current_worker()
	cart_doc = frappe.get_doc("FWMS LPN", cart)
	if cart_doc.status != "Packed":
		frappe.throw(f"{cart} is not Packed (status {cart_doc.status})")

	door = frappe.get_doc("FWMS Dock Door", dock_door)
	door.status = "Loading"
	if trailer:
		door.trailer = trailer
	door.save(ignore_permissions=True)

	shipping_zone = frappe.db.get_value("FWMS Zone", {"zone_type": "Shipping Docks"}, "name")
	cart_doc.zone = shipping_zone
	cart_doc.status = "Packed"
	cart_doc.save(ignore_permissions=True)

	emit(
		"Sort Load & Ship",
		f"Carton {cart} loaded at {dock_door}" + (f" (trailer {trailer})" if trailer else ""),
		subtype="loaded",
		worker=worker,
		lpn=cart,
		payload={"dock_door": dock_door, "trailer": trailer},
	)
	return {"cart": cart, "dock_door": dock_door}


@frappe.whitelist()
def loaded_cartoons(dock_door: str) -> list[dict]:
	"""Cartons staged at a dock door (packed carts in Shipping Docks zone)."""
	require_operator()
	zone = frappe.db.get_value("FWMS Zone", {"zone_type": "Shipping Docks"}, "name")
	return frappe.get_all(
		"FWMS LPN",
		filters={"zone": zone, "status": "Packed", "lpn_type": "Cart"},
		fields=["name", "sales_order", "delivery_note"],
		limit=100,
	)


@frappe.whitelist()
def ship(dock_door: str) -> dict:
	"""Submit the draft Delivery Note(s) for everything staged at this door; roll up."""
	require_operator()
	worker = current_worker()
	zone = frappe.db.get_value("FWMS Zone", {"zone_type": "Shipping Docks"}, "name")
	carts = frappe.get_all(
		"FWMS LPN", filters={"zone": zone, "status": "Packed", "lpn_type": "Cart"}, pluck="name"
	)
	dns = sorted({
		dn for dn in (
			frappe.db.get_value("FWMS LPN", c, "delivery_note") for c in carts
		) if dn
	})
	if not dns:
		frappe.throw("No staged cartons with draft Delivery Notes at this door")

	shipped = []
	for dn_name in dns:
		dn = frappe.get_doc("Delivery Note", dn_name)
		if dn.docstatus == 0:
			dn.fwms_dock_door = dock_door
			dn.fwms_worker = worker
			dn.save(ignore_permissions=True)
			dn.submit()
		shipped.append(dn_name)
		# wave rollup happens in the DN on_submit event hook
		for cart in frappe.get_all("FWMS LPN", {"delivery_note": dn_name, "status": "Packed"}, pluck="name"):
			frappe.db.set_value("FWMS LPN", cart, "status", "Shipped", update_modified=True)

	door = frappe.get_doc("FWMS Dock Door", dock_door)
	door.status = "Idle"
	door.trailer = None
	door.save(ignore_permissions=True)

	emit(
		"Sort Load & Ship",
		f"Trailer closed at {dock_door} — {len(shipped)} Delivery Note(s) shipped: {', '.join(shipped)}",
		subtype="trailer_closed",
		worker=worker,
		payload={"delivery_notes": shipped, "dock_door": dock_door},
	)
	return {"shipped": shipped, "cartons": len(carts)}
