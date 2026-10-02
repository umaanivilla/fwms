# Test bootstrap: bench run-tests calls before_tests (hooks.py).
# The dev site is expected to be seeded (fwms.setup.seed_demo.run);
# if not, seed it once so flows have masters, stock and workers.

import frappe


def before_tests():
	from fwms.setup.seed_demo import run

	if not frappe.db.get_all("FWMS Worker", limit=1):
		run()
	frappe.db.commit()
