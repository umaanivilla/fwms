# FWMS demo seeder — one call builds a demo-ready warehouse floor:
# bins, docks, shifts, 26 workers, items (batch/serial/plain, barcodes, HS
# codes, origin), parties, opening stock with staggered batch expiries
# (FEFO demo), open POs and released SOs.
#
#   bench --site fwms.localhost execute fwms.setup.seed_demo.run

from __future__ import annotations

import random
from datetime import date, timedelta

import frappe
from frappe.utils import add_days, flt, nowdate, today

from fwms.setup.install import setup_company

COMPANY = "FWMS Warehouse Co"
ABBR = "FW"

FIRST_NAMES = [
	"Alice",
	"Bob",
	"Chen",
	"Diana",
	"Emil",
	"Farah",
	"Goran",
	"Hana",
	"Ivan",
	"Julia",
	"Kwame",
	"Lena",
	"Marco",
	"Nadia",
	"Omar",
	"Priya",
	"Quentin",
	"Rosa",
	"Samir",
	"Tara",
	"Umesh",
	"Vera",
	"Wesley",
	"Ximena",
	"Yusuf",
	"Zoe",
]
LAST_NAMES = [
	"Andersson",
	"Baptista",
	"Chowdhury",
	"Dvorak",
	"Eriksen",
	"Farrell",
	"Gruber",
	"Haddad",
	"Ibrahim",
	"Jensen",
	"Kowalski",
	"Lindqvist",
	"Moreau",
	"Nguyen",
	"Ozturk",
	"Patel",
	"Quintero",
	"Rossi",
	"Santos",
	"Tanaka",
	"Ueda",
	"Vogel",
	"Walsh",
	"Xu",
	"Yilmaz",
	"Zhang",
]

# item_code, name, group, has_batch, has_serial, tariff, origin, rate, weight-ish
ITEMS = [
	("COCOA-100", "Cocoa Bar 100g", "Products", 1, 0, "1806.90", "Ghana", 2.50),
	("PROTBAR-20", "Protein Bar 20x", "Products", 1, 0, "2106.90", "United States", 18.00),
	("JUICE-1L", "Orange Juice 1L", "Products", 1, 0, "2106.90", "Spain", 1.80),
	("LAPTOP-14", "Laptop 14 inch", "Electronics", 0, 1, "8471.30", "China", 899.00),
	("PHONE-X", "Smartphone X", "Electronics", 0, 1, "8517.13", "Vietnam", 549.00),
	("BATT-20", "Power Bank 20Ah", "Electronics", 0, 1, "8507.60", "China", 39.00),
	("STEEL-SHELF", "Steel Shelving Unit", "Components", 0, 0, "9403.20", "Germany", 125.00),
	("VALVE-08", "Brass Valve DN08", "Components", 0, 0, "8481.80", "India", 14.50),
]

SUPPLIERS = [
	("Cocoa West GmbH", "Germany"),
	("Shenzen Electrade", "China"),
	("Euro Metals BV", "Netherlands"),
]
CUSTOMERS = [
	("Nordic Retail AB", "Sweden"),
	("Export Handels GmbH", "Germany"),
	("City Stores Ltd", "United States"),
]


def run() -> str:
	random.seed(42)
	if frappe.db.get_all("FWMS Worker", limit=1):
		return "Demo data already seeded — skipping (drop FWMS records to reseed)"

	setup_company(COMPANY, ABBR)
	receiving_wh = f"Receiving Docks - {ABBR}"
	bulk_wh = f"Bulk Storage - {ABBR}"
	pick_wh = f"Pick Faces - {ABBR}"

	company_doc = frappe.get_doc("Company", COMPANY)
	# account defaults can be missing on programmatic companies
	stock_in_hand = _account_or(company_doc, "stock_in_hand", "1010 - Stock Assets - FW")

	_roots()
	_stock_entry_types()
	_shifts()
	_docks()
	_items(pick_wh)
	_bins()
	_workers()
	_parties()
	_stock(bulk_wh, pick_wh, stock_in_hand)
	_purchase_orders(receiving_wh)
	_sales_orders(pick_wh)

	frappe.db.commit()
	return "Seeded: zones/bins/docks/shifts, 26 workers, 8 items, stock, POs + released SOs"


def _account_or(company, field, default):
	value = company.get(field)
	if value and frappe.db.exists("Account", value):
		return value
	account = frappe.db.get_value(
		"Account", {"company": COMPANY, "account_name": default.split(" - ")[0], "is_group": 0}, "name"
	)
	return account or value


def _roots():
	"""Tree roots the setup wizard normally creates on a fresh site."""
	# v16 gates per-item has_serial_no/has_batch_no behind this Stock Settings flag
	frappe.db.set_single_value("Stock Settings", "enable_serial_and_batch_no_for_item", 1)
	roots = [
		("Item Group", {"doctype": "Item Group", "item_group_name": "All Item Groups", "is_group": 1}),
		(
			"Customer Group",
			{"doctype": "Customer Group", "customer_group_name": "All Customer Groups", "is_group": 1},
		),
		("Territory", {"doctype": "Territory", "territory_name": "All Territories", "is_group": 1}),
		(
			"Supplier Group",
			{"doctype": "Supplier Group", "supplier_group_name": "All Supplier Groups", "is_group": 1},
		),
	]
	for name, doc in roots:
		if not frappe.db.exists(doc["doctype"], name):
			frappe.get_doc(doc).insert(ignore_permissions=True)
	for uom in ("Nos", "Unit"):
		if not frappe.db.exists("UOM", uom):
			frappe.get_doc({"doctype": "UOM", "uom_name": uom, "enabled": 1}).insert(ignore_permissions=True)
	for pl_name, selling in (("Standard Selling", 1), ("Standard Buying", 0)):
		if not frappe.db.exists("Price List", pl_name):
			frappe.get_doc(
				{
					"doctype": "Price List",
					"price_list_name": pl_name,
					"__newname": pl_name,
					"selling": selling,
					"buying": not selling,
					"enabled": 1,
					"currency": "USD",
					"countries": [],
				}
			).insert(ignore_permissions=True)


def _stock_entry_types():
	for name, purpose in (
		("Material Receipt", "Material Receipt"),
		("Material Issue", "Material Issue"),
		("Material Transfer", "Material Transfer"),
	):
		if not frappe.db.exists("Stock Entry Type", name):
			frappe.get_doc(
				{
					"doctype": "Stock Entry Type",
					"__newname": name,
					"purpose": purpose,
					"company": COMPANY,
				}
			).insert(ignore_permissions=True)


def _shifts():
	for name, start, end in (
		("Morning", "06:00:00", "14:00:00"),
		("Evening", "14:00:00", "22:00:00"),
		("Night", "22:00:00", "06:00:00"),
	):
		if not frappe.db.exists("FWMS Shift", name):
			frappe.get_doc(
				{
					"doctype": "FWMS Shift",
					"shift_name": name,
					"start_time": start,
					"end_time": end,
					"break_start": "10:00:00",
					"break_end": "10:15:00",
				}
			).insert(ignore_permissions=True)


def _docks():
	receiving = frappe.db.get_value("FWMS Zone", {"zone_type": "Receiving Docks"}, "name")
	shipping = frappe.db.get_value("FWMS Zone", {"zone_type": "Shipping Docks"}, "name")
	for i in (1, 2, 3):
		for code, zone, direction in (
			(f"IN-0{i}", receiving, "Inbound"),
			(f"OUT-0{i}", shipping, "Outbound"),
		):
			if not frappe.db.exists("FWMS Dock Door", code):
				frappe.get_doc(
					{
						"doctype": "FWMS Dock Door",
						"door_code": code,
						"zone": zone,
						"direction": direction,
						"status": "Idle",
					}
				).insert(ignore_permissions=True)


def _bins():
	bulk_zone = frappe.db.get_value("FWMS Zone", {"zone_type": "Bulk"}, "name")
	pick_zone = frappe.db.get_value("FWMS Zone", {"zone_type": "Pick Faces"}, "name")
	receiving_zone = frappe.db.get_value("FWMS Zone", {"zone_type": "Receiving Docks"}, "name")
	qc_zone = frappe.db.get_value("FWMS Zone", {"zone_type": "QC Hold"}, "name")

	def bin_doc(barcode, zone, abc, pick=False, item=None, minq=None, maxq=None, counted_days_ago=None):
		if frappe.db.exists("FWMS Bin", barcode):
			return
		frappe.get_doc(
			{
				"doctype": "FWMS Bin",
				"barcode": barcode,
				"zone": zone,
				"abc_class": abc,
				"is_pick_face": 1 if pick else 0,
				"primary_item": item,
				"min_qty": minq,
				"max_qty": maxq,
				"aisle": barcode.split("-")[1] if "-" in barcode else None,
				"last_counted_on": add_days(today(), -(counted_days_ago or random.randint(5, 100))),
			}
		).insert(ignore_permissions=True)

	# bulk: 5 aisles x 2 racks x 4 bins = 40
	for a in range(1, 6):
		for r in range(1, 3):
			for b in range(1, 5):
				bin_doc(f"BULK-A{a:02d}-R{r}-B{b}", bulk_zone, random.choice("ABC"))
	# pick faces: 24 (3 per batch/plain item seeded below)
	for i in range(1, 25):
		item = None
		if i <= 18:
			item = ITEMS[(i - 1) % 6][0]  # first 6 items get 3 faces each
		bin_doc(
			f"PICK-{i:02d}",
			pick_zone,
			"A" if i <= 12 else "B",
			pick=True,
			item=item,
			minq=20 if item else None,
			maxq=100 if item else None,
		)
	# staging + qc
	for i in range(1, 5):
		bin_doc(f"RCV-STG-{i}", receiving_zone, "A")
		bin_doc(f"QC-STG-{i}", qc_zone, "B")


def _workers():
	shifts = frappe.get_all("FWMS Shift", pluck="name")
	# roles
	frappe.get_doc(
		{
			"doctype": "User",
			"email": "supervisor@fwms.local",
			"first_name": "Warehouse",
			"last_name": "Supervisor",
			"send_welcome_email": 0,
			"roles": [{"role": "FWMS Supervisor"}, {"role": "FWMS Inventory Control"}],
		}
	).insert(ignore_permissions=True) if not frappe.db.exists("User", "supervisor@fwms.local") else None

	for i in range(1, 27):
		code = f"W{i:03d}"
		email = f"{code.lower()}@fwms.local"
		if not frappe.db.exists("User", email):
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": FIRST_NAMES[i - 1],
					"last_name": LAST_NAMES[i - 1],
					"send_welcome_email": 0,
					"roles": [{"role": "FWMS Operator"}],
				}
			)
			user.insert(ignore_permissions=True)
		if frappe.db.exists("FWMS Worker", code):
			continue
		worker = frappe.get_doc(
			{
				"doctype": "FWMS Worker",
				"worker_code": code,
				"person_name": f"{FIRST_NAMES[i - 1]} {LAST_NAMES[i - 1]}",
				"user": email,
				"shift": shifts[(i - 1) % len(shifts)],
				"default_zone": frappe.db.get_value(
					"FWMS Zone",
					{
						"zone_type": ["Receiving Docks", "Bulk", "Pick Faces", "Packing", "Shipping Docks"][
							i % 5
						]
					},
					"name",
				),
				"skills": random.choice(
					[
						None,
						"Pick, Pack",
						"Receive, Putaway",
						"Count, Replenish",
						"Receive, Quality",
						"Quality, Scrap",
					]
				),
				"active": 1,
			}
		)
		worker.insert(ignore_permissions=True)
		worker.set_pin(f"{i:04d}")


def _items(pick_wh):
	for idx, (code, name, group, batched, serial, tariff, origin, rate) in enumerate(ITEMS):
		if frappe.db.exists("Item", code):
			continue
		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_group": _item_group(group),
				"stock_uom": "Nos",
				"description": name,
				"maintain_stock": 1,
				"is_stock_item": 1,
				"has_batch_no": 1 if batched else 0,
				"create_new_batch": 1 if batched else 0,
				"has_serial_no": 1 if serial else 0,
				"has_expiry_date": 0,
				"end_of_life": add_days(today(), 720),
				"standard_rate": rate,
				"customs_tariff_number": tariff
				if frappe.db.exists("Customs Tariff Number", tariff)
				else None,
				"country_of_origin": origin if frappe.db.exists("Country", origin) else None,
				"is_sales_item": 1,
				"is_purchase_item": 1,
				"item_defaults": [{"company": COMPANY, "default_warehouse": pick_wh}],
				"barcodes": [{"barcode": _ean13(200000000000 + idx + 1), "barcode_type": "EAN"}],
			}
		).insert(ignore_permissions=True)


def _ean13(base12: int) -> str:
	"""Turn a 12-digit number into a valid EAN-13 (with checksum)."""
	digits = [int(c) for c in str(base12)]
	weights = [1, 3] * 6
	checksum = (10 - sum(d * w for d, w in zip(digits, weights, strict=False)) % 10) % 10
	return "".join(map(str, digits)) + str(checksum)


def _item_group(name):
	if not frappe.db.exists("Item Group", name):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": name,
				"parent_item_group": "All Item Groups",
				"is_group": 0,
			}
		).insert(ignore_permissions=True)
	return name


def _parties():
	for name, country in SUPPLIERS:
		if not frappe.db.exists("Supplier", name):
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": name,
					"supplier_group": "All Supplier Groups"
					if frappe.db.exists("Supplier Group", "All Supplier Groups")
					else None,
					"country": country,
				}
			).insert(ignore_permissions=True)
	for name, country in CUSTOMERS:
		if not frappe.db.exists("Customer", name):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": name,
					"customer_group": "All Customer Groups"
					if frappe.db.exists("Customer Group", "All Customer Groups")
					else None,
					"territory": "All Territories"
					if frappe.db.exists("Territory", "All Territories")
					else None,
					"country": country,
				}
			).insert(ignore_permissions=True)


def _stock(bulk_wh, pick_wh, stock_account):
	"""Opening stock: FWMS totes in bulk bins + matching Material Receipt entries.

	Batches get staggered expiries so FEFO picking is demonstrable.
	"""
	bulk_zone = frappe.db.get_value("FWMS Zone", {"zone_type": "Bulk"}, "name")
	pick_zone = frappe.db.get_value("FWMS Zone", {"zone_type": "Pick Faces"}, "name")
	bulk_bins = frappe.get_all("FWMS Bin", filters={"zone": bulk_zone}, pluck="name")
	pick_bins = frappe.get_all("FWMS Bin", filters={"zone": pick_zone, "is_pick_face": 1}, pluck="name")

	def ensure_batch(item, batch_id, expiry: date):
		if not frappe.db.exists("Batch", batch_id):
			frappe.get_doc(
				{
					"doctype": "Batch",
					"batch_id": batch_id,
					"item": item,
					"expiry_date": expiry,
				}
			).insert(ignore_permissions=True)
		return batch_id

	def tote(barcode, zone, bin_name, contents):
		if frappe.db.exists("FWMS LPN", barcode):
			return barcode
		frappe.get_doc(
			{
				"doctype": "FWMS LPN",
				"barcode": barcode,
				"lpn_type": "Pallet" if barcode.startswith("PLT") else "Tote",
				"status": "In Bin",
				"zone": zone,
				"bin": bin_name,
				"items": contents,
			}
		).insert(ignore_permissions=True)
		return barcode

	today_d = frappe.utils.getdate(today())
	# ledger: Material Receipt into Bulk (batch + serial aware), then transfer to pick faces
	se = frappe.new_doc("Stock Entry")
	se.company = COMPANY
	se.stock_entry_type = "Material Receipt"
	se.posting_date = nowdate()
	lpn_no = 0

	def add_receipt(item, qty, batch_id=None, serials=None):
		row = se.append("items", {})
		row.item_code = item
		row.qty = qty
		row.t_warehouse = bulk_wh
		row.basic_rate = frappe.db.get_value("Item", item, "standard_rate")
		row.use_serial_batch_fields = 1
		if batch_id:
			row.batch_no = batch_id
		if serials:
			row.serial_no = "\n".join(serials)

	for code, _name, _group, batched, serial, *_ in ITEMS:
		if batched:
			# three batches per item with different expiries (FEFO demo)
			for tag, days in (("A", 90), ("B", 30), ("C", 200)):
				batch_id = f"OPEN-{code}-{tag}"
				ensure_batch(code, batch_id, today_d + timedelta(days=days))
				qty = 60 if tag == "B" else 40
				add_receipt(code, qty, batch_id)
				lpn_no += 1
				tote(
					f"LPN-{lpn_no:06d}",
					bulk_zone,
					bulk_bins[lpn_no % len(bulk_bins)],
					[{"item": code, "qty": qty, "batch_no": batch_id, "uom": "Nos"}],
				)
		elif serial:
			serials = [f"SN-{code}-{n:05d}" for n in range(1, 26)]
			add_receipt(code, 25, serials=serials)
			lpn_no += 1
			tote(
				f"LPN-{lpn_no:06d}",
				bulk_zone,
				bulk_bins[lpn_no % len(bulk_bins)],
				[{"item": code, "qty": 25, "uom": "Nos"}],
			)
		else:
			add_receipt(code, 80)
			lpn_no += 1
			tote(
				f"LPN-{lpn_no:06d}",
				bulk_zone,
				bulk_bins[lpn_no % len(bulk_bins)],
				[{"item": code, "qty": 80, "uom": "Nos"}],
			)

	se.insert(ignore_permissions=True)
	se.submit()

	# pick faces: put some stock forward from bulk (ledger + FWMS totes)
	transfer = frappe.new_doc("Stock Entry")
	transfer.company = COMPANY
	transfer.stock_entry_type = "Material Transfer"
	transfer.posting_date = nowdate()
	faces_used = 0
	for code, _name, _group, batched, _serial, *_ in ITEMS:
		face = next(
			(b for b in pick_bins if frappe.db.get_value("FWMS Bin", b, "primary_item") == code), None
		)
		if not face:
			continue
		if batched:
			batch_id = f"OPEN-{code}-B"  # nearest expiry goes to the face
			row = transfer.append("items", {})
			row.item_code = code
			row.qty = 30
			row.s_warehouse = bulk_wh
			row.t_warehouse = pick_wh
			row.use_serial_batch_fields = 1
			row.batch_no = batch_id
			tote(
				f"RPL-{face}",
				pick_zone,
				face,
				[{"item": code, "qty": 30, "batch_no": batch_id, "uom": "Nos"}],
			)
		else:
			row = transfer.append("items", {})
			row.item_code = code
			row.qty = 20
			row.s_warehouse = bulk_wh
			row.t_warehouse = pick_wh
			tote(f"RPL-{face}", pick_zone, face, [{"item": code, "qty": 20, "uom": "Nos"}])
		faces_used += 1
	transfer.insert(ignore_permissions=True)
	transfer.submit()


def _purchase_orders(receiving_wh):
	maker = [
		("Cocoa West GmbH", [("COCOA-100", 200), ("PROTBAR-20", 50)]),
		("Shenzen Electrade", [("LAPTOP-14", 30), ("PHONE-X", 40), ("BATT-20", 100)]),
		("Euro Metals BV", [("STEEL-SHELF", 60), ("VALVE-08", 200)]),
		("Cocoa West GmbH", [("JUICE-1L", 300)]),
	]
	for supplier, items in maker:
		po = frappe.new_doc("Purchase Order")
		po.company = COMPANY
		po.supplier = supplier
		po.schedule_date = add_days(today(), 7)
		po.transaction_date = today()
		po.set_warehouse = receiving_wh
		po.buying_price_list = "Standard Buying"
		for code, qty in items:
			row = po.append("items", {})
			row.item_code = code
			row.qty = qty
			row.schedule_date = po.schedule_date
			row.rate = frappe.db.get_value("Item", code, "standard_rate")
		po.insert(ignore_permissions=True)
		po.submit()


def _sales_orders(pick_wh):
	maker = [
		("Nordic Retail AB", [("COCOA-100", 25), ("PROTBAR-20", 10)]),
		("Export Handels GmbH", [("LAPTOP-14", 5), ("BATT-20", 10)]),
		("City Stores Ltd", [("STEEL-SHELF", 6), ("VALVE-08", 40)]),
		("Nordic Retail AB", [("PHONE-X", 8), ("JUICE-1L", 40)]),
	]
	for customer, items in maker:
		so = frappe.new_doc("Sales Order")
		so.company = COMPANY
		so.customer = customer
		so.transaction_date = today()
		so.selling_price_list = "Standard Selling"
		so.currency = "USD"
		so.plc_conversion_rate = 1
		so.conversion_rate = 1
		so.delivery_date = add_days(today(), 5)
		for code, qty in items:
			row = so.append("items", {})
			row.item_code = code
			row.qty = qty
			row.delivery_date = so.delivery_date
			row.warehouse = pick_wh
			row.rate = flt(frappe.db.get_value("Item", code, "standard_rate")) * 1.25
		so.insert(ignore_permissions=True)
		so.submit()  # Continuous release -> wave + pick tasks
