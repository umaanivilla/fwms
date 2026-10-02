# Shared RF API plumbing: role checks, worker session, scan resolution.

from __future__ import annotations

import json

import frappe

from fwms.events import emit
from fwms.utils import resolve_scan

FWMS_ROLES = ("FWMS Operator", "FWMS Supervisor", "FWMS Inventory Control", "System Manager")


def as_list(value, field: str = "param") -> list:
	"""Form posts deliver JSON list params as strings — normalize."""
	if value is None or value == "":
		return []
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except json.JSONDecodeError:
			frappe.throw(f"Invalid JSON for {field}")
	if isinstance(value, list):
		return value
	if isinstance(value, dict):
		return [value]
	frappe.throw(f"Invalid payload for {field}")


def require_operator() -> None:
	"""Every RF endpoint calls this first — no role, no RF."""
	roles = set(frappe.get_roles())
	if not roles.intersection(FWMS_ROLES):
		raise frappe.PermissionError("FWMS Operator role required")


def current_worker() -> str:
	"""FWMS Worker linked to the logged-in RF user."""
	worker = frappe.db.get_value("FWMS Worker", {"user": frappe.session.user, "active": 1}, "name")
	if not worker:
		frappe.throw(f"No active FWMS Worker linked to user {frappe.session.user}")
	return worker


@frappe.whitelist()
def scan(barcode: str, screen: str = "") -> dict:
	"""Resolve a barcode, log it as a handheld scan, return the resolution."""
	require_operator()
	result = resolve_scan(barcode)
	emit(
		_category_for_scan(result),
		f"Scan {barcode} ({result['type']})",
		subtype=f"scan:{screen}",
		is_scan=1,
		worker=current_worker(),
		bin=result["name"] if result["type"] == "bin" else None,
		lpn=result["name"] if result["type"] == "lpn" else None,
		payload={"screen": screen, "resolved": result["type"]},
	)
	# bump worker scan counter
	worker = current_worker()
	frappe.db.set_value(
		"FWMS Worker", worker, "scans_today", (frappe.db.get_value("FWMS Worker", worker, "scans_today") or 0) + 1,
		update_modified=False,
	)
	return result


def _category_for_scan(result: dict) -> str:
	return {
		"lpn": "Putaway & Storage",
		"bin": "Putaway & Storage",
		"item": "Receiving & QC",
		"batch": "Receiving & QC",
		"so": "Orders & Picking",
		"dn": "Sort Load & Ship",
		"po": "Receiving & QC",
		"dock": "Sort Load & Ship",
	}.get(result["type"], "Alerts & Supervisor")
