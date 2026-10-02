# Wave engine tests: Continuous release on SO submit, FEFO batch ordering,
# Pick List generation from pick-face LPN stock.

import frappe
from frappe.tests.utils import FrappeTestCase

from fwms.utils import get_warehouse_for_type


class TestWaveFEFO(FrappeTestCase):
	def setUp(self):
		self.company = frappe.db.get_single_value("FWMS Settings", "company")
		self.pick_wh = get_warehouse_for_type("Pick Faces")

	def _so(self, item: str, qty: float) -> str:
		so = frappe.new_doc("Sales Order")
		so.company = self.company
		so.customer = frappe.get_all("Customer", pluck="name", limit=1)[0]
		so.transaction_date = frappe.utils.today()
		so.delivery_date = frappe.utils.add_days(frappe.utils.today(), 3)
		so.selling_price_list = "Standard Selling"
		so.currency = "USD"
		so.plc_conversion_rate = 1
		so.conversion_rate = 1
		row = so.append("items", {})
		row.item_code = item
		row.qty = qty
		row.warehouse = self.pick_wh
		row.rate = frappe.db.get_value("Item", item, "standard_rate")
		row.delivery_date = so.delivery_date
		so.insert(ignore_permissions=True)
		so.submit()
		return so.name

	def test_continuous_release_builds_pick_list_fefo(self):
		"""SO submit -> wave released -> Pick List rows ordered FEFO (nearest expiry first)."""
		so = self._so("COCOA-100", 40)
		wave_name = frappe.db.get_value("FWMS Wave Order", {"sales_order": so}, "parent")
		self.assertTrue(wave_name, "Continuous mode must release a wave on SO submit")
		wave = frappe.get_doc("FWMS Wave", wave_name)
		self.assertEqual(wave.status, "Released")
		self.assertTrue(wave.pick_list)

		rows = frappe.get_all(
			"Pick List Item",
			filters={"parent": wave.pick_list, "item_code": "COCOA-100"},
			fields=["batch_no", "qty", "warehouse"],
			order_by="idx",
		)
		self.assertTrue(rows, "pick rows expected from pick-face stock")

		# policy: pick faces first, FEFO within each zone; the nearest-expiry
		# pick-face batch must lead
		pick_wh = self.pick_wh
		pick_face_rows = [r for r in rows if r.warehouse == pick_wh]
		self.assertTrue(pick_face_rows)
		nearest = min(
			(frappe.db.get_value("Batch", r.batch_no, "expiry_date") for r in pick_face_rows if r.batch_no),
			default=None,
		)
		first_expiry = frappe.db.get_value("Batch", pick_face_rows[0].batch_no, "expiry_date")
		self.assertEqual(first_expiry, nearest, "nearest-expiry pick-face batch must be offered first")

		# FEFO holds within the pick-face run
		expiries = [
			frappe.db.get_value("Batch", r.batch_no, "expiry_date")
			for r in pick_face_rows if r.batch_no
		]
		self.assertEqual(expiries, sorted(expiries), "pick-face batches must be FEFO")

		# pick tasks exist and point into pick-face bins
		tasks = frappe.get_all(
			"FWMS Task",
			filters={"wave": wave_name, "task_type": "Pick", "status": "Open"},
			fields=["from_bin", "item"],
		)
		self.assertTrue(tasks)
		for task in tasks:
			self.assertEqual(
				frappe.db.get_value("FWMS Bin", task.from_bin, "zone"),
				frappe.db.get_value("FWMS Zone", {"zone_type": "Pick Faces"}, "name"),
			)

	def test_wave_closes_after_ship(self):
		so = self._so("STEEL-SHELF", 2)
		wave_name = frappe.db.get_value("FWMS Wave Order", {"sales_order": so}, "parent")
		# simulate full pipeline: complete tasks, then submit the linked DN
		for task in frappe.get_all(
			"FWMS Task", filters={"wave": wave_name, "task_type": "Pick"}, pluck="name"
		):
			frappe.db.set_value("FWMS Task", task, "status", "Complete")
		pick_list = frappe.get_doc("Pick List", frappe.db.get_value("FWMS Wave", wave_name, "pick_list"))
		if pick_list.docstatus == 0:
			for row in pick_list.locations:
				row.picked_qty = row.qty
			pick_list.save(ignore_permissions=True)
			pick_list.submit()

		from erpnext.stock.doctype.pick_list.pick_list import create_delivery_note

		dn = create_delivery_note(pick_list.name)
		if isinstance(dn, list):
			dn = dn[0]
		dn.fwms_wave = wave_name
		dn.save(ignore_permissions=True)
		dn.submit()

		self.assertEqual(frappe.db.get_value("FWMS Wave", wave_name, "status"), "Closed")
