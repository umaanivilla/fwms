# Copyright (c) 2026
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.query_builder.functions import Sum
from frappe.utils import flt


class FWMSBin(Document):
	pass


ACTIVE_LPN_STATUSES = ("In Bin", "Receiving", "Picking")

LPN = frappe.qb.DocType("FWMS LPN")
LPN_ITEM = frappe.qb.DocType("FWMS LPN Item")


def _agg_lpns(lpn_names: list[str], item: str | None = None, with_bundle: bool = False) -> list[dict]:
	"""Sum LPN contents grouped by (item, batch)."""
	q = (
		frappe.qb.from_(LPN_ITEM)
		.select(LPN_ITEM.item, LPN_ITEM.batch_no, Sum(LPN_ITEM.qty).as_("qty"))
		.where((LPN_ITEM.parenttype == "FWMS LPN") & (LPN_ITEM.parent.isin(lpn_names)))
		.groupby(LPN_ITEM.item, LPN_ITEM.batch_no)
		.orderby(LPN_ITEM.item)
	)
	if item:
		q = q.where(LPN_ITEM.item == item)
	if with_bundle:
		q = q.select(LPN_ITEM.serial_and_batch_bundle, LPN_ITEM.uom)
	return q.run(as_dict=True)


def _lpns_in_zones(zone_names: list[str]) -> list[str]:
	if not zone_names:
		return []
	return frappe.get_all(
		"FWMS LPN",
		filters={"zone": ("in", zone_names), "status": ("in", ACTIVE_LPN_STATUSES)},
		pluck="name",
	)


def get_bin_stock(bin_name: str) -> list[dict]:
	"""Aggregate LPN contents sitting in one bin -> [{item, batch_no, qty, uom, serial_and_batch_bundle}]."""
	pnames = frappe.get_all(
		"FWMS LPN",
		filters={"bin": bin_name, "status": ("in", ACTIVE_LPN_STATUSES)},
		pluck="name",
	)
	if not pnames:
		return []
	return _agg_lpns(pnames, with_bundle=True)


def get_zone_stock(zone_names: list[str], item: str | None = None) -> list[dict]:
	"""Aggregate LPN contents across zones -> [{item, batch_no, qty}]."""
	pnames = _lpns_in_zones(zone_names)
	if not pnames:
		return []
	return _agg_lpns(pnames, item=item)


def get_pick_face_stock(item: str) -> float:
	"""Total qty of item in pick-face bins (via LPN aggregation)."""
	zones = frappe.get_all("FWMS Zone", filters={"zone_type": "Pick Faces"}, pluck="name")
	pnames = _lpns_in_zones(zones)
	if not pnames:
		return 0.0
	rows = _agg_lpns(pnames, item=item)
	return flt(rows[0].qty) if rows else 0.0
