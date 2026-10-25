# RF Quality + Scrap tests: QC-Hold queue feeding from receiving/returns,
# disposition (release vs hold, split verdicts, QIs against the source doc)
# and the controlled scrap write-off with LPN realignment on approval.

import frappe
from frappe.tests.utils import FrappeTestCase

from fwms.api import rf_quality, rf_receiving, rf_returns, rf_scrap
from fwms.utils import get_warehouse_for_type


class TestQualityScrap(FrappeTestCase):
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

	def _receive_qc_fail(self, item: str = "VALVE-08", qty: float = 6) -> tuple[str, str]:
		"""Receive a fully QC-failed line -> (qc LPN, open Quality task)."""
		po = self._open_po(item, qty)
		res = rf_receiving.commit_receipt(
			po=po,
			lpn_barcode=None,
			lines=[{"item_code": item, "qty": qty, "qc": "Fail"}],
		)
		qc_lpn = f"QC-{res['purchase_receipt']}"
		task = frappe.db.get_value(
			"FWMS Task", {"task_type": "Quality", "lpn": qc_lpn, "status": "Open"}, "name"
		)
		self.assertTrue(task, "Quality task expected for the QC-hold LPN")
		return qc_lpn, task

	def test_qc_fail_receiving_queues_quality_task_and_qc_lpn(self):
		qc_lpn, task = self._receive_qc_fail(qty=5)

		lpn = frappe.get_doc("FWMS LPN", qc_lpn)
		qc_zone = frappe.db.get_value("FWMS Zone", {"zone_type": "QC Hold"}, "name")
		self.assertEqual(lpn.zone, qc_zone)
		self.assertAlmostEqual(lpn.total_qty(), 5)
		self.assertEqual(frappe.db.get_value("FWMS Task", task, "status"), "Open")

	def test_inspect_all_pass_releases_whole_lpn(self):
		qc_lpn, task = self._receive_qc_fail(qty=5)
		detail = rf_quality.task_detail(task)
		self.assertEqual(detail["source_doctype"], "Purchase Receipt")

		out = rf_quality.inspect(
			lpn=qc_lpn,
			task=task,
			results=[{"item_code": "VALVE-08", "qty": 5, "verdict": "Pass"}],
		)

		# whole LPN moved out of QC Hold into a storage bin
		lpn = frappe.get_doc("FWMS LPN", qc_lpn)
		self.assertEqual(lpn.status, "In Bin")
		self.assertTrue(lpn.bin)
		zone_type = frappe.db.get_value("FWMS Zone", lpn.zone, "zone_type")
		self.assertIn(zone_type, ("Pick Faces", "Bulk"))
		# transfer actually posted from QC Hold
		sle_wh = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_type": "Stock Entry", "item_code": "VALVE-08"},
			pluck="warehouse",
			order_by="creation desc",
			limit=5,
		)
		self.assertIn(get_warehouse_for_type("QC Hold"), sle_wh)

		self.assertEqual(frappe.db.get_value("FWMS Task", task, "status"), "Complete")
		qi = frappe.db.exists(
			"Quality Inspection", {"reference_name": detail["source_name"], "status": "Accepted"}
		)
		self.assertTrue(qi, "Accepted QI against the source PR expected")
		self.assertEqual(out["held"], [])

	def test_inspect_split_verdict_releases_tote_and_holds_rest(self):
		qc_lpn, task = self._receive_qc_fail(qty=6)

		out = rf_quality.inspect(
			lpn=qc_lpn,
			task=task,
			results=[
				{"item_code": "VALVE-08", "qty": 4, "verdict": "Pass"},
				{"item_code": "VALVE-08", "qty": 2, "verdict": "Fail"},
			],
		)

		# good units: submitted transfer to Bulk + fresh tote there
		se = frappe.get_doc("Stock Entry", out["released"]["stock_entry"])
		self.assertEqual(se.docstatus, 1)
		self.assertEqual(se.items[0].qty, 4)
		self.assertEqual(se.items[0].s_warehouse, get_warehouse_for_type("QC Hold"))
		bulk_zone = frappe.db.get_value("FWMS Zone", {"zone_type": "Bulk"}, "name")
		self.assertEqual(out["released"]["zone"], bulk_zone)
		tote = frappe.get_doc("FWMS LPN", out["released"]["lpn"])
		self.assertAlmostEqual(tote.total_qty(), 4)
		self.assertEqual(tote.zone, bulk_zone)

		# failed units stayed on QC Hold in the original LPN
		held = frappe.get_doc("FWMS LPN", qc_lpn)
		self.assertAlmostEqual(held.total_qty(), 2)
		self.assertEqual(frappe.db.get_value("FWMS Zone", held.zone, "zone_type"), "QC Hold")
		self.assertEqual(
			out["held"],
			[{"item_code": "VALVE-08", "qty": 2.0, "batch_no": None, "verdict": "Fail"}],
		)

		# both QIs filed against the source PR
		pr = held.source_name
		self.assertTrue(frappe.db.exists("Quality Inspection", {"reference_name": pr, "status": "Accepted"}))
		self.assertTrue(frappe.db.exists("Quality Inspection", {"reference_name": pr, "status": "Rejected"}))
		self.assertEqual(frappe.db.get_value("FWMS Task", task, "status"), "Complete")

	def test_scrap_draft_then_approval_closes_lpn(self):
		qc_lpn, _task = self._receive_qc_fail(qty=3)
		batch = frappe.db.get_value("FWMS LPN Item", {"parent": qc_lpn}, "batch_no")

		out = rf_scrap.scrap(
			lpn=qc_lpn,
			reason="Obsolete",
			lines=[{"item_code": "VALVE-08", "qty": 2, "batch_no": batch}],
		)
		se = frappe.get_doc("Stock Entry", out["stock_entry"])
		self.assertEqual(se.docstatus, 0, "scrap files a DRAFT for Inventory Control")
		scrap_wh = get_warehouse_for_type("Scrap")
		self.assertEqual(se.items[0].t_warehouse, scrap_wh)
		self.assertEqual(se.fwms_lpn, qc_lpn)
		self.assertEqual(se.remarks, "RF scrap (Obsolete)")

		# LPN untouched until approval
		self.assertAlmostEqual(frappe.get_doc("FWMS LPN", qc_lpn).total_qty(), 3)

		# approve as Inventory Control
		frappe.set_user("Administrator")
		ic_user = "w009@fwms.local"
		if "FWMS Inventory Control" not in frappe.get_roles(ic_user):
			frappe.get_doc("User", ic_user).add_roles("FWMS Inventory Control")
		frappe.set_user(ic_user)
		se.submit()

		lpn = frappe.get_doc("FWMS LPN", qc_lpn)
		self.assertAlmostEqual(lpn.total_qty(), 1, "only the approved qty leaves the LPN")
		self.assertEqual(lpn.status, "In Bin")

		# scrap the last unit -> LPN closes out
		frappe.set_user("w001@fwms.local")
		out2 = rf_scrap.scrap(
			lpn=qc_lpn,
			reason="Quality Fail",
			lines=[{"item_code": "VALVE-08", "qty": 1, "batch_no": batch}],
		)
		frappe.set_user(ic_user)
		frappe.get_doc("Stock Entry", out2["stock_entry"]).submit()

		lpn = frappe.get_doc("FWMS LPN", qc_lpn)
		self.assertEqual(lpn.status, "Closed")
		self.assertFalse(lpn.items)
		self.assertTrue(frappe.db.exists("FWMS Event Log", {"subtype": "scrap_posted", "lpn": qc_lpn}))

	def test_scrap_reason_and_qty_validation(self):
		qc_lpn, _task = self._receive_qc_fail(qty=2)
		with self.assertRaises(frappe.ValidationError):
			rf_scrap.scrap(lpn=qc_lpn, reason="Free text", lines=[{"item_code": "VALVE-08", "qty": 1}])
		with self.assertRaises(frappe.ValidationError):
			rf_scrap.scrap(lpn=qc_lpn, reason="Damaged", lines=[{"item_code": "VALVE-08", "qty": 99}])

	def test_returns_scrap_grade_maps_to_scrap_warehouse(self):
		# regression for the grade gap: Scrap used to map to no zone at all
		self.assertEqual(rf_returns.GRADE_WAREHOUSE_ZONE["Scrap"], "Scrap")
		self.assertEqual(
			get_warehouse_for_type("Scrap"), frappe.db.get_value("FWMS Zone", "Scrap", "warehouse")
		)

		dn = frappe.get_all("Delivery Note", filters={"docstatus": 1}, pluck="name", limit=1)
		if not dn:
			self.skipTest("no submitted Delivery Note on the site (ship one via §8 first)")
		frappe.set_user("w007@fwms.local")
		info = rf_returns.dn_info(dn[0])
		item = info["items"][0]
		out = rf_returns.process_return(
			delivery_note=dn[0],
			lines=[{"item_code": item["item_code"], "qty": 1, "grade": "Scrap"}],
		)
		ret_name = next(r["return"] for r in out["returns"] if r["grade"] == "Scrap")
		row = frappe.get_doc("Delivery Note", ret_name).items[0]  # DN return (is_return)
		self.assertEqual(row.warehouse, get_warehouse_for_type("Scrap"))
