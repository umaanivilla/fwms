# End-to-end: receive -> putaway -> wave -> pick -> pack -> load -> ship,
# asserting the ERP stock ledger, LPN lifecycle and KPI snapshot.

import frappe
from frappe.tests.utils import FrappeTestCase

from fwms.api import rf_load, rf_packing, rf_picking, rf_putaway, rf_receiving
from fwms.fwms.doctype.fwms_bin.fwms_bin import get_zone_stock
from fwms.utils import get_warehouse_for_type


class TestEndToEnd(FrappeTestCase):
	def setUp(self):
		self.company = frappe.db.get_single_value("FWMS Settings", "company")
		self.pick_wh = get_warehouse_for_type("Pick Faces")

	def test_full_order_cycle(self):
		# 1. receive into an LPN
		frappe.set_user("w001@fwms.local")
		po = self._po("JUICE-1L", 60)
		res = rf_receiving.commit_receipt(
			po=po, lpn_barcode="TEST-LPN-E2E",
			lines=[{"item_code": "JUICE-1L", "qty": 60, "qc": "Pass", "batch_no": "TEST-BATCH-E2E"}],
		)
		lpn = res["lpn"]

		# 2. putaway into a pick face
		frappe.set_user("w002@fwms.local")
		task = frappe.db.get_value("FWMS Task", {"task_type": "Putaway", "lpn": lpn, "status": "Open"}, "name")
		sug = rf_putaway.suggest_bin(lpn)
		rf_putaway.confirm(lpn=lpn, bin_name=sug["bin"], task=task)

		# 3. order for exactly what we stocked -> wave -> pick task
		frappe.set_user("Administrator")
		so = self._so("JUICE-1L", 15)
		wave_name = frappe.db.get_value("FWMS Wave Order", {"sales_order": so}, "parent")

		# 4. pick it into a cart (cancel unrelated seeded pick tasks so the
		# claim lands on this wave's task; the test transaction rolls back)
		frappe.set_user("w003@fwms.local")
		frappe.db.set_value("FWMS Worker", "W003", "skills", None)
		for other in frappe.get_all(
			"FWMS Task",
			filters={"task_type": "Pick", "status": "Open", "wave": ("!=", wave_name)},
			pluck="name",
		):
			frappe.db.set_value("FWMS Task", other, "status", "Cancelled", update_modified=False)
		claimed = rf_picking.claim(cart_barcode="TEST-CART-E2E")
		self.assertTrue(claimed and claimed.get("task"), "pick task must be claimable")
		self.assertEqual(claimed["task"]["wave"], wave_name)
		detail = rf_picking.task_detail(task=claimed["task"]["name"])
		self.assertEqual(detail["item"], "JUICE-1L")
		# FEFO: the offered batch must have the nearest expiry among available JUICE batches
		available = {
			r["batch_no"]
			for r in get_zone_stock(
				frappe.get_all("FWMS Zone", {"zone_type": "Pick Faces"}, pluck="name"), "JUICE-1L"
			)
			if r["batch_no"] and r["qty"] > 0
		}
		expiries = {b: frappe.db.get_value("Batch", b, "expiry_date") for b in available}
		self.assertEqual(
			expiries.get(detail["batch_no"]), min(expiries.values()),
			"must pick the nearest-expiry (FEFO) batch",
		)
		out = rf_picking.pick_confirm(
			task=claimed["task"]["name"], qty=detail["qty"], cart="TEST-CART-E2E",
		)
		self.assertEqual(out["remaining"], 0)

		# 5. wave picking complete -> PL submitted + draft DN
		pick_list = frappe.db.get_value("FWMS Wave", wave_name, "pick_list")
		self.assertEqual(frappe.db.get_value("Pick List", pick_list, "docstatus"), 1)
		dn_name = frappe.db.get_value(
			"FWMS Wave Order", {"parent": wave_name}, "delivery_note"
		) or frappe.db.get_value("Delivery Note", {"fwms_wave": wave_name, "docstatus": 0}, "name")
		self.assertTrue(dn_name, "draft DN expected after picking completes")

		# 6. pack the cart (Packing Slip + ZPL)
		frappe.set_user("w004@fwms.local")
		packed = rf_packing.finish(cart="TEST-CART-E2E", delivery_note=dn_name, case_no=1)
		self.assertTrue(packed["packing_slip"])
		self.assertIn("^XA", packed["zpl"])

		# 7. load + ship
		frappe.set_user("w005@fwms.local")
		dock = frappe.get_all("FWMS Dock Door", filters={"direction": "Outbound"}, pluck="name", limit=1)[0]
		rf_load.load(cart="TEST-CART-E2E", dock_door=dock, trailer="E2E-TLR")
		# stage any other packed carts so ship() finds our DN among staged ones
		shipping_zone = frappe.db.get_value("FWMS Zone", {"zone_type": "Shipping Docks"}, "name")
		frappe.db.set_value("FWMS LPN", "TEST-CART-E2E", "zone", shipping_zone, update_modified=False)
		shipped = rf_load.ship(dock_door=dock)
		self.assertIn(dn_name, shipped["shipped"])

		# 8. assertions: DN submitted, wave closed, LPN lifecycle, stock ledger
		self.assertEqual(frappe.db.get_value("Delivery Note", dn_name, "docstatus"), 1)
		self.assertEqual(frappe.db.get_value("FWMS Wave", wave_name, "status"), "Closed")
		self.assertEqual(frappe.db.get_value("FWMS LPN", "TEST-CART-E2E", "status"), "Shipped")

		sles = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_no": dn_name, "voucher_type": "Delivery Note"},
			fields=["warehouse", "actual_qty"],
		)
		self.assertTrue(sles)
		for sle in sles:
			self.assertEqual(sle.warehouse, self.pick_wh, "DN consumes from Pick Faces zone")

		# KPI snapshot can compute (cycle time from release to trailer)
		from fwms.scheduler_jobs import snapshot_kpis

		snap = snapshot_kpis()
		self.assertTrue(snap)
		snapshot = frappe.get_doc("FWMS KPI Snapshot", snap)
		self.assertGreaterEqual(snapshot.orders_shipped_today, 1)

	def _po(self, item: str, qty: float) -> str:
		po = frappe.new_doc("Purchase Order")
		po.company = self.company
		po.supplier = frappe.get_all("Supplier", pluck="name", limit=1)[0]
		po.transaction_date = frappe.utils.today()
		po.schedule_date = frappe.utils.today()
		po.buying_price_list = "Standard Buying"
		po.set_warehouse = get_warehouse_for_type("Receiving Docks")
		row = po.append("items", {})
		row.item_code = item
		row.qty = qty
		row.rate = frappe.db.get_value("Item", item, "standard_rate")
		po.insert(ignore_permissions=True)
		po.submit()
		return po.name

	def _so(self, item: str, qty: float) -> str:
		so = frappe.new_doc("Sales Order")
		so.company = self.company
		so.customer = frappe.get_all("Customer", pluck="name", limit=1)[0]
		so.transaction_date = frappe.utils.today()
		so.delivery_date = frappe.utils.today()
		so.selling_price_list = "Standard Selling"
		so.currency = "USD"
		so.plc_conversion_rate = 1
		so.conversion_rate = 1
		row = so.append("items", {})
		row.item_code = item
		row.qty = qty
		row.warehouse = self.pick_wh
		row.rate = frappe.db.get_value("Item", item, "standard_rate")
		so.insert(ignore_permissions=True)
		so.submit()
		return so.name
