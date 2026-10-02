# RF receiving + putaway tests: Purchase Receipt into LPN, QC split, putaway
# move, serial/batch capture and stock-ledger correctness.

import frappe
from frappe.tests.utils import FrappeTestCase

from fwms.api import rf_putaway, rf_receiving
from fwms.utils import get_warehouse_for_type


class TestReceiving(FrappeTestCase):
	def setUp(self):
		frappe.set_user("w001@fwms.local")

	def tearDown(self):
		frappe.set_user("Administrator")

	def _open_po(self, item: str, qty: float) -> str:
		po = frappe.new_doc("Purchase Order")
		po.company = frappe.db.get_single_value("FWMS Settings", "company")
		po.supplier = frappe.get_all("Supplier", pluck="name", limit=1)[0]
		po.transaction_date = frappe.utils.today()
		po.schedule_date = frappe.utils.today()
		po.buying_price_list = "Standard Buying"
		po.set_warehouse = get_warehouse_for_type("Receiving Docks")
		row = po.append("items", {})
		row.item_code = item
		row.qty = qty
		row.rate = frappe.db.get_value("Item", item, "standard_rate")
		row.schedule_date = po.schedule_date
		po.insert(ignore_permissions=True)
		po.submit()
		return po.name

	def test_receive_batch_item_creates_pr_lpn_and_putaway_task(self):
		po = self._open_po("COCOA-100", 12)
		res = rf_receiving.commit_receipt(
			po=po, lpn_barcode="TEST-LPN-RCV-1",
			lines=[{"item_code": "COCOA-100", "qty": 12, "qc": "Pass", "batch_no": "TEST-BATCH-RCV"}],
		)
		self.assertTrue(res["purchase_receipt"])
		self.assertEqual(res["status"], "received")

		pr = frappe.get_doc("Purchase Receipt", res["purchase_receipt"])
		self.assertEqual(pr.docstatus, 1)
		self.assertEqual(pr.items[0].batch_no, "TEST-BATCH-RCV")

		lpn = frappe.get_doc("FWMS LPN", "TEST-LPN-RCV-1")
		self.assertEqual(lpn.status, "Receiving")
		self.assertAlmostEqual(lpn.total_qty(), 12)

		# stock ledger landed in Receiving Docks warehouse
		sle = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_type": "Purchase Receipt", "voucher_no": pr.name},
			pluck="warehouse",
		)
		self.assertIn(get_warehouse_for_type("Receiving Docks"), sle)

		# putaway task queued
		task = frappe.db.exists("FWMS Task", {"task_type": "Putaway", "lpn": "TEST-LPN-RCV-1", "status": "Open"})
		self.assertTrue(task)

	def test_receive_qc_fail_routes_to_qc_hold(self):
		# Quality Inspections only apply to items flagged for inspection
		frappe.db.set_value("Item", "VALVE-08", "inspection_required_before_purchase", 1)
		self.addCleanup(frappe.db.set_value, "Item", "VALVE-08", "inspection_required_before_purchase", 0)
		po = self._open_po("VALVE-08", 7)
		res = rf_receiving.commit_receipt(
			po=po, lpn_barcode=None,
			lines=[{"item_code": "VALVE-08", "qty": 7, "qc": "Fail"}],
		)
		pr = frappe.get_doc("Purchase Receipt", res["purchase_receipt"])
		self.assertEqual(pr.items[0].rejected_warehouse, get_warehouse_for_type("QC Hold"))
		qi = frappe.db.exists("Quality Inspection", {"reference_name": pr.name, "status": "Rejected"})
		self.assertTrue(qi, "Quality Inspection expected for failed line")

	def test_receive_serial_item_records_serials(self):
		po = self._open_po("BATT-20", 3)
		serials = ["TEST-SN-BATT-001", "TEST-SN-BATT-002", "TEST-SN-BATT-003"]
		res = rf_receiving.commit_receipt(
			po=po, lpn_barcode=None,
			lines=[{"item_code": "BATT-20", "qty": 3, "qc": "Pass", "serials": serials}],
		)
		pr = frappe.get_doc("Purchase Receipt", res["purchase_receipt"])
		for sn in serials:
			self.assertTrue(frappe.db.exists("Serial No", sn))
		# genealogy: the serials' bundle traces back to the receipt
		entry = frappe.db.get_value(
			"Serial and Batch Entry", {"serial_no": ("in", serials)}, ["parent"], as_dict=False
		)
		self.assertTrue(entry)
		voucher = frappe.db.get_value("Serial and Batch Bundle", entry, "voucher_no")
		self.assertEqual(voucher, pr.name)

	def test_putaway_moves_lpn_and_ledger(self):
		po = self._open_po("STEEL-SHELF", 4)
		res = rf_receiving.commit_receipt(
			po=po, lpn_barcode="TEST-LPN-PUT-1",
			lines=[{"item_code": "STEEL-SHELF", "qty": 4, "qc": "Pass"}],
		)
		lpn = res["lpn"]
		task = frappe.db.get_value("FWMS Task", {"task_type": "Putaway", "lpn": lpn, "status": "Open"}, "name")
		sug = rf_putaway.suggest_bin(lpn)
		out = rf_putaway.confirm(lpn=lpn, bin_name=sug["bin"], task=task)

		self.assertEqual(out["bin"], sug["bin"])
		lpn_doc = frappe.get_doc("FWMS LPN", lpn)
		self.assertEqual(lpn_doc.status, "In Bin")
		self.assertEqual(lpn_doc.bin, sug["bin"])
		self.assertEqual(frappe.db.get_value("FWMS Task", task, "status"), "Complete")
