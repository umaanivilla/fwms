# Copyright (c) 2026
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import flt


class FWMSConsignment(Document):
	def before_save(self):
		if not self.package_count:
			self.package_count = (
				frappe.db.count(
					"FWMS LPN",
					{
						"delivery_note": ("in", [d.delivery_note for d in self.delivery_notes]),
						"status": "Shipped",
					},
				)
				or None
			)

	def get_lines(self) -> list[dict]:
		"""Consolidated lines across the consignment's Delivery Notes with
		HS code + country of origin from the Item master."""
		lines: dict[tuple, dict] = {}
		for d in self.delivery_notes:
			for item in frappe.get_all(
				"Delivery Note Item",
				filters={"parent": d.delivery_note},
				fields=["item_code", "item_name", "qty", "rate", "amount", "uom", "batch_no"],
			):
				key = (item.item_code, item.batch_no or "")
				if key not in lines:
					lines[key] = {
						"item_code": item.item_code,
						"item_name": item.item_name,
						"qty": 0.0,
						"uom": item.uom,
						"rate": flt(item.rate),
						"amount": 0.0,
						"batch_no": item.batch_no,
					}
				lines[key]["qty"] += flt(item.qty)
				lines[key]["amount"] += flt(item.amount)
		out = []
		for line in lines.values():
			item = frappe.db.get_value(
				"Item",
				line["item_code"],
				["customs_tariff_number", "country_of_origin", "item_name"],
				as_dict=True,
			)
			line["hs_code"] = item.customs_tariff_number if item else None
			line["origin"] = item.country_of_origin if item else None
			out.append(line)
		return out

	def get_cartons(self) -> list[dict]:
		"""Carton-level detail from FWMS LPNs shipped on this consignment."""
		dns = [d.delivery_note for d in self.delivery_notes]
		if not dns:
			return []
		cartons = []
		for lpn_name in frappe.get_all("FWMS LPN", filters={"delivery_note": ("in", dns)}, pluck="name"):
			lpn = frappe.get_doc("FWMS LPN", lpn_name)
			cartons.append(
				{
					"carton": lpn.name,
					"items": [
						{"item": r.item, "qty": flt(r.qty), "batch_no": r.batch_no} for r in lpn.items or []
					],
					"zone": lpn.zone,
				}
			)
		return cartons

	def get_parties(self) -> dict:
		first_dn = self.delivery_notes[0].delivery_note if self.delivery_notes else None
		customer = None
		if first_dn:
			customer = frappe.db.get_value(
				"Delivery Note", first_dn, ["customer", "customer_name"], as_dict=True
			)
		company = frappe.db.get_single_value("FWMS Settings", "company") or None
		return {
			"customer": customer.customer if customer else None,
			"customer_name": customer.customer_name if customer else None,
			"company": company,
		}
