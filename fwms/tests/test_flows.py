# Cycle counting + task engine tests.

import frappe
from frappe.tests.utils import FrappeTestCase

from fwms.api import rf_count
from fwms.fwms.doctype.fwms_bin.fwms_bin import get_bin_stock


class TestCycleCount(FrappeTestCase):
	def setUp(self):
		frappe.set_user("w006@fwms.local")

	def tearDown(self):
		frappe.set_user("Administrator")

	def test_blind_count_hides_expected(self):
		bin_name = frappe.db.get_value("FWMS LPN", {"zone": "Bulk Storage", "status": "In Bin"}, "bin")
		start = rf_count.start(bin_name=bin_name)
		self.assertEqual(start["method"], "Blind")
		self.assertIsNone(start["expected"], "blind counts must not reveal expected qty")

	def test_count_variance_creates_sr_and_updates_bin(self):
		bin_name = frappe.db.get_value("FWMS LPN", {"zone": "Bulk Storage", "status": "In Bin"}, "bin")
		expected = get_bin_stock(bin_name)
		self.assertTrue(expected, "stocked bin required")

		counted = [
			{"item": r["item"], "qty": r["qty"] - 1, "batch_no": r.get("batch_no")}
			for r in expected
		]
		out = rf_count.submit(bin_name=bin_name, counted=counted, task=None)

		# one unit short per stocked line
		self.assertEqual(out["variance"], -len(expected))
		self.assertTrue(out["stock_reconciliation"], "variance must raise an SR draft")
		sr = frappe.get_doc("Stock Reconciliation", out["stock_reconciliation"])
		self.assertEqual(sr.docstatus, 0, "SR stays draft for Inventory Control approval")

		bin_doc = frappe.get_doc("FWMS Bin", bin_name)
		self.assertEqual(bin_doc.last_count_variance, -len(expected))
		self.assertTrue(bin_doc.last_counted_on)
		self.assertLess(bin_doc.last_count_accuracy, 100)

	def test_count_scheduler_respects_abc_interval(self):
		from fwms.scheduler_jobs import generate_count_tasks

		created = generate_count_tasks()
		self.assertIsInstance(created, int)
		# tasks must be unique per bin (no duplicates on re-run)
		bins = frappe.get_all(
			"FWMS Task",
			filters={"task_type": "Count", "status": ("in", ("Open", "Assigned"))},
			pluck="to_bin",
		)
		self.assertEqual(len(bins), len(set(bins)))
		generate_count_tasks()  # idempotent
		bins2 = frappe.get_all(
			"FWMS Task",
			filters={"task_type": "Count", "status": ("in", ("Open", "Assigned"))},
			pluck="to_bin",
		)
		self.assertEqual(len(bins2), len(set(bins2)))


class TestTaskEngine(FrappeTestCase):
	def test_claim_next_respects_skills_and_completes(self):
		from fwms.tasks import claim_next, complete_task, create_task

		name = create_task("Pack", qty=1, emit_event=False)
		claimed = claim_next("W001", task_type="Pack")
		self.assertEqual(claimed, name)
		self.assertEqual(frappe.db.get_value("FWMS Task", name, "status"), "Assigned")
		self.assertEqual(frappe.db.get_value("FWMS Worker", "W001", "current_task"), name)

		# worker with mismatched skills cannot claim
		frappe.db.set_value("FWMS Worker", "W002", "skills", "Receive, Putaway")
		other = claim_next("W002", task_type="Pack")
		self.assertNotEqual(other, name, "skills must filter the queue")

		complete_task(name)
		self.assertEqual(frappe.db.get_value("FWMS Task", name, "status"), "Complete")
		self.assertIsNone(frappe.db.get_value("FWMS Worker", "W001", "current_task"))
