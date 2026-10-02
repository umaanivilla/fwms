# FWMS installation bootstrap: runs after migrate; idempotent.
# Creates roles, grants ERPNext doctype permissions to FWMS roles, seeds
# Customs Tariff Numbers and zone warehouses, and defaults FWMS Settings.

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

FWMS_ROLES = ["FWMS Operator", "FWMS Supervisor", "FWMS Inventory Control"]

# traceability custom fields on ERPNext stock documents (created after migrate)
CUSTOM_FIELDS = {
	"Purchase Receipt": [
		{"fieldname": "fwms_section", "fieldtype": "Section Break", "label": "FWMS", "collapsible": 1},
		{"fieldname": "fwms_po", "label": "FWMS Purchase Order", "fieldtype": "Link", "options": "Purchase Order", "insert_after": "fwms_section"},
		{"fieldname": "fwms_lpn", "label": "FWMS LPN", "fieldtype": "Link", "options": "FWMS LPN", "insert_after": "fwms_po"},
		{"fieldname": "fwms_worker", "label": "FWMS Worker", "fieldtype": "Link", "options": "FWMS Worker", "insert_after": "fwms_lpn"},
	],
	"Stock Entry": [
		{"fieldname": "fwms_section", "fieldtype": "Section Break", "label": "FWMS", "collapsible": 1},
		{"fieldname": "fwms_task", "label": "FWMS Task", "fieldtype": "Link", "options": "FWMS Task", "insert_after": "fwms_section"},
		{"fieldname": "fwms_worker", "label": "FWMS Worker", "fieldtype": "Link", "options": "FWMS Worker", "insert_after": "fwms_task"},
	],
	"Delivery Note": [
		{"fieldname": "fwms_section", "fieldtype": "Section Break", "label": "FWMS", "collapsible": 1},
		{"fieldname": "fwms_wave", "label": "FWMS Wave", "fieldtype": "Link", "options": "FWMS Wave", "insert_after": "fwms_section"},
		{"fieldname": "fwms_dock_door", "label": "FWMS Dock Door", "fieldtype": "Link", "options": "FWMS Dock Door", "insert_after": "fwms_wave"},
		{"fieldname": "fwms_worker", "label": "FWMS Worker", "fieldtype": "Link", "options": "FWMS Worker", "insert_after": "fwms_dock_door"},
	],
	"Stock Reconciliation": [
		{"fieldname": "fwms_section", "fieldtype": "Section Break", "label": "FWMS", "collapsible": 1},
		{"fieldname": "fwms_bin", "label": "FWMS Bin", "fieldtype": "Link", "options": "FWMS Bin", "insert_after": "fwms_section"},
		{"fieldname": "fwms_task", "label": "FWMS Task", "fieldtype": "Link", "options": "FWMS Task", "insert_after": "fwms_bin"},
	],
}

# (doctype, role, perms) granted to FWMS roles on ERPNext documents the RF flows drive
ERP_PERMISSIONS = {
	"FWMS Operator": {
		"Purchase Receipt": {"create": 1, "read": 1, "write": 1, "submit": 1, "amend": 1},
		"Stock Entry": {"create": 1, "read": 1, "write": 1, "submit": 1, "amend": 1},
		"Pick List": {"create": 1, "read": 1, "write": 1, "submit": 1},
		"Packing Slip": {"create": 1, "read": 1, "write": 1, "submit": 1},
		"Delivery Note": {"create": 1, "read": 1, "write": 1, "submit": 1, "amend": 1},
		"Quality Inspection": {"create": 1, "read": 1, "write": 1, "submit": 1},
		"Stock Reconciliation": {"create": 1, "read": 1, "write": 1},
		# masters read by erpnext.get_item_details / controllers while building RF documents
		"Item": {"read": 1},
		"Item Group": {"read": 1},
		"Item Barcode": {"read": 1},
		"Warehouse": {"read": 1},
		"Batch": {"read": 1, "write": 1, "create": 1},
		"Serial No": {"read": 1, "write": 1, "create": 1},
		"Serial and Batch Bundle": {"read": 1, "write": 1, "create": 1, "submit": 1, "amend": 1},
		"UOM": {"read": 1},
		"Company": {"read": 1},
		"Customer": {"read": 1},
		"Supplier": {"read": 1},
		"Price List": {"read": 1},
		"Item Price": {"read": 1},
		"Address": {"read": 1, "write": 1, "create": 1},
		"Contact": {"read": 1, "write": 1, "create": 1},
		"Sales Order": {"read": 1},
		"Purchase Order": {"read": 1},
		"Stock Settings": {"read": 1},
		"Account": {"read": 1},
		"Cost Center": {"read": 1},
	},
	"FWMS Inventory Control": {
		"Stock Reconciliation": {"create": 1, "read": 1, "write": 1, "submit": 1, "amend": 1},
	},
}

SEED_TARIFFS = [
	("8471.30", "Portable automatic data processing machines, weight <= 10 kg"),
	("8517.13", "Smartphones"),
	("8507.60", "Lithium-ion accumulators"),
	("1806.90", "Chocolate and other food preparations containing cocoa"),
	("2106.90", "Food preparations not elsewhere specified"),
	("9403.20", "Other metal furniture"),
	("8481.80", "Taps, cocks, valves and similar articles"),
]


def after_migrate():
	ensure_roles()
	ensure_erp_permissions()
	ensure_custom_fields()
	seed_tariffs()
	default_settings()


def ensure_custom_fields():
	entries = {}
	for doctype, fields in CUSTOM_FIELDS.items():
		new_fields = []
		for field in fields:
			existing = frappe.db.get_value(
				"Custom Field", {"dt": doctype, "fieldname": field["fieldname"]}, "name"
			)
			if not existing:
				new_fields.append({**field, "doctype": doctype})
		if new_fields:
			entries[doctype] = new_fields
	if entries:
		create_custom_fields(entries, ignore_validate=True, update=True)


def ensure_roles():
	for role in FWMS_ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({
				"doctype": "Role",
				"role_name": role,
				"desk_access": 1,
				"disabled": 0,
			}).insert(ignore_permissions=True)


def ensure_erp_permissions():
	for role, doctypes in ERP_PERMISSIONS.items():
		for doctype, perms in doctypes.items():
			# docstatus 0 = draft stage; 1 = submit stage (required for submit rights)
			stages = [0] + ([1] if perms.get("submit") else [])
			for docstatus in stages:
				_ensure_custom_perm(doctype, role, perms, docstatus)


def _ensure_custom_perm(doctype: str, role: str, perms: dict, docstatus: int) -> None:
	existing = frappe.db.get_all(
		"Custom DocPerm",
		filters={"parent": doctype, "role": role, "docstatus": docstatus},
		pluck="name",
	)
	base = {
		"parent": doctype,
		"role": role,
		"permlevel": 0,
		"docstatus": docstatus,
		"read": 1,
	}
	if existing:
		row = frappe.get_doc("Custom DocPerm", existing[0])
	else:
		row = frappe.new_doc("Custom DocPerm")
		row.update(base)
	for ptype, value in perms.items():
		row.set(ptype, value)
	if docstatus == 1:
		# submit stage rows only need write/submit/amend rights
		row.set("create", 0)
		row.set("print", 1)
	try:
		row.save(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		frappe.db.rollback()


def seed_tariffs():
	for tariff, description in SEED_TARIFFS:
		if not frappe.db.exists("Customs Tariff Number", tariff):
			frappe.get_doc({
				"doctype": "Customs Tariff Number",
				"tariff_number": tariff,
				"description": description,
			}).insert(ignore_permissions=True)


def default_settings():
	# WMS semantics: rejected QC still posts the receipt (into QC Hold)
	frappe.db.set_single_value(
		"Stock Settings", "action_if_quality_inspection_is_rejected", "Warn"
	)

	# stop ERPNext's install default (INR) leaking into new transactions
	company = frappe.db.get_value("Company", {"is_group": 0}, "name", order_by="creation")
	if company:
		currency = frappe.db.get_value("Company", company, "default_currency")
		if currency and frappe.db.get_single_value("Global Defaults", "default_currency") != currency:
			frappe.db.set_single_value("Global Defaults", "default_currency", currency)

	settings = frappe.get_doc("FWMS Settings")
	dirty = False
	if not settings.company and frappe.db.exists("Company", {"is_group": 0}):
		settings.company = company
		abbr = frappe.db.get_value("Company", company, "abbr") or "FW"
		settings.site_abbreviation = abbr
		dirty = True
	if dirty:
		settings.save(ignore_permissions=True)


# ---------------------------------------------------------------------------
# One-time site bootstrap (bench execute fwms.setup.install.setup_company)
# ---------------------------------------------------------------------------

ZONE_DEFS = [
	# zone_name, zone_type, has_warehouse, map_x, map_y, map_w, map_h, color
	("Receiving Docks", "Receiving Docks", True, 2, 2, 20, 16, "#4f8cff"),
	("QC Hold", "QC Hold", True, 2, 20, 20, 10, "#b58900"),
	("Bulk Storage", "Bulk", True, 24, 2, 36, 28, "#7f8c8d"),
	("Pick Faces", "Pick Faces", True, 62, 2, 20, 28, "#2ecc71"),
	("Packing", "Packing", False, 24, 32, 28, 12, "#e67e22"),
	("Shipping Docks", "Shipping Docks", False, 54, 32, 28, 12, "#9b59b6"),
]


def setup_company(company_name: str = "FWMS Warehouse Co", abbr: str = "FW", currency: str = "USD"):
	"""Create Company + fiscal year + FWMS zone warehouses + zones + count programs."""
	# ERPNext's company bootstrap references these warehouse types
	for wh_type in ("Transit", "Scrap"):
		if not frappe.db.exists("Warehouse Type", wh_type):
			frappe.get_doc({
				"doctype": "Warehouse Type",
				"__newname": wh_type,
				"warehouse_type": wh_type,
			}).insert(ignore_permissions=True)

	if not frappe.db.exists("Company", company_name):
		frappe.get_doc({
			"doctype": "Company",
			"company_name": company_name,
			"abbr": abbr,
			"currency": currency,
			"default_currency": currency,
			"country": "United States",
			"chart_of_accounts": "Standard with Numbers",
		}).insert(ignore_permissions=True)

	fiscal_year = f"{frappe.utils.getdate().year}"
	if not frappe.db.exists("Fiscal Year", fiscal_year):
		frappe.get_doc({
			"doctype": "Fiscal Year",
			"year": fiscal_year,
			"year_start_date": f"{fiscal_year}-01-01",
			"year_end_date": f"{fiscal_year}-12-31",
		}).insert(ignore_permissions=True)

	settings = frappe.get_doc("FWMS Settings")
	settings.company = company_name
	settings.site_abbreviation = abbr
	settings.save(ignore_permissions=True)

	_setup_zones(company_name, abbr)
	_setup_count_programs()
	frappe.db.commit()
	return company_name


def _setup_zones(company_name: str, abbr: str):
	root = f"{company_name} - {abbr}"
	if not frappe.db.exists("Warehouse", root):
		frappe.get_doc({
			"doctype": "Warehouse",
			"warehouse_name": company_name,
			"company": company_name,
			"is_group": 1,
		}).insert(ignore_permissions=True)

	for zone_name, zone_type, has_warehouse, mx, my, mw, mh, color in ZONE_DEFS:
		warehouse = None
		if has_warehouse:
			warehouse = f"{zone_name} - {abbr}"
			if not frappe.db.exists("Warehouse", warehouse):
				frappe.get_doc({
					"doctype": "Warehouse",
					"warehouse_name": zone_name,
					"company": company_name,
					"parent_warehouse": root,
					"is_group": 0,
				}).insert(ignore_permissions=True)
		if not frappe.db.exists("FWMS Zone", zone_name):
			frappe.get_doc({
				"doctype": "FWMS Zone",
				"zone_name": zone_name,
				"zone_type": zone_type,
				"warehouse": warehouse,
				"is_physical_only": 0 if has_warehouse else 1,
				"barcode_prefix": zone_name.split()[0].upper()[:4],
				"map_x": mx,
				"map_y": my,
				"map_w": mw,
				"map_h": mh,
				"color": color,
			}).insert(ignore_permissions=True)

	# scrap warehouse for returns grading
	scrap = f"Scrap - {abbr}"
	if not frappe.db.exists("Warehouse", scrap):
		frappe.get_doc({
			"doctype": "Warehouse",
			"warehouse_name": "Scrap",
			"company": company_name,
			"parent_warehouse": root,
			"is_group": 0,
		}).insert(ignore_permissions=True)


def _setup_count_programs():
	for name, abc_class, interval in (
		("Class A - Monthly", "A", 30),
		("Class B - Quarterly", "B", 90),
		("Class C - Semi-Annual", "C", 180),
	):
		if not frappe.db.exists("FWMS Count Program", name):
			frappe.get_doc({
				"doctype": "FWMS Count Program",
				"program_name": name,
				"abc_class": abc_class,
				"interval_days": interval,
				"count_method": "Blind",
				"auto_generate": 1,
				"enabled": 1,
			}).insert(ignore_permissions=True)
