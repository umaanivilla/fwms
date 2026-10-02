# Development harness: run isolated FWMS flow checks via
#   bench --site <site> execute fwms.dev.<function>
# Each helper catches exceptions and returns a diagnostic string (bench
# execute hides real errors behind its eval fallback otherwise).

from __future__ import annotations

import frappe


def _run(fn):
	frappe.flags.in_test = True
	try:
		out = fn()
		frappe.db.commit()
		return f"OK: {out}"
	except Exception as exc:
		frappe.db.rollback()
		import traceback

		traceback.print_exc()
		frappe.clear_messages()
		return f"FAIL: {type(exc).__name__}: {str(exc)[:300]}"


def check_receive():
	def fn():
		frappe.set_user("w001@fwms.local")
		from fwms.api import rf_receiving

		po = frappe.get_all("Purchase Order", filters={"docstatus": 1}, pluck="name", order_by="creation", limit=1)[0]
		info = rf_receiving.po_info(po)
		item = next(i for i in info["items"] if i["pending_qty"] >= 3)
		return rf_receiving.commit_receipt(
			po=po, lpn_barcode=None,
			lines=[{"item_code": item["item_code"], "qty": 3, "qc": "Pass"}],
		)

	return _run(fn)


def check_putaway():
	def fn():
		frappe.set_user("w002@fwms.local")
		from fwms.api import rf_putaway

		task = frappe.get_all(
			"FWMS Task", filters={"task_type": "Putaway", "status": "Open"},
			pluck="name", limit=1,
		)
		if not task:
			return "no open putaway tasks"
		lpn = frappe.db.get_value("FWMS Task", task[0], "lpn")
		sug = rf_putaway.suggest_bin(lpn)
		return rf_putaway.confirm(lpn=lpn, bin_name=sug["bin"], task=task[0])

	return _run(fn)


def check_pick():
	def fn():
		# a worker whose skills allow picking (or blank skills)
		worker = frappe.db.get_value(
			"FWMS Worker",
			filters={"active": 1, "skills": ("in", ("Pick, Pack",))},
			fieldname="user",
		) or frappe.db.get_value(
			"FWMS Worker", filters={"active": 1, "skills": ("is", "not set")}, fieldname="user"
		) or "w003@fwms.local"
		frappe.set_user(worker)
		from fwms.api import rf_picking

		claimed = rf_picking.claim(cart_barcode=None)
		if not claimed.get("task"):
			return "no pick tasks"
		detail = rf_picking.task_detail(task=claimed["task"]["name"])
		return rf_picking.pick_confirm(
			task=claimed["task"]["name"], qty=detail["qty"],
			cart=claimed["cart"], scanned_serials=[],
		)

	return _run(fn)


def check_pack():
	def fn():
		frappe.set_user("w004@fwms.local")
		from fwms.api import rf_packing

		queue = rf_packing.queue()
		if not queue:
			return "empty pack queue"
		start = rf_packing.start(cart=queue[0]["name"])
		case = rf_packing.next_case_no(delivery_note=start["delivery_note"])
		out = rf_packing.finish(cart=queue[0]["name"], delivery_note=start["delivery_note"], case_no=case)
		return {"packing_slip": out["packing_slip"], "cart": out["cart"], "zpl_len": len(out["zpl"])}

	return _run(fn)


def check_ship():
	def fn():
		frappe.set_user("w005@fwms.local")
		from fwms.api import rf_load

		dock = frappe.get_all("FWMS Dock Door", filters={"direction": "Outbound"}, pluck="name", limit=1)[0]
		carts = frappe.get_all("FWMS LPN", filters={"status": "Packed", "lpn_type": "Cart"}, pluck="name")
		for cart in carts:
			rf_load.load(cart=cart, dock_door=dock, trailer="TLR-001")
		return rf_load.ship(dock_door=dock)

	return _run(fn)


def check_count():
	def fn():
		frappe.set_user("w006@fwms.local")
		from fwms.api import rf_count

		# pick a bulk bin that actually holds LPN stock
		bin_name = frappe.db.get_value(
			"FWMS LPN", {"zone": "Bulk Storage", "status": "In Bin"}, "bin"
		)
		if not bin_name:
			return "no stocked bulk bins"
		start = rf_count.start(bin_name=bin_name)
		expected = start.get("expected") or []
		# blind: derive counted from a second stock lookup, minus one unit
		from fwms.fwms.doctype.fwms_bin.fwms_bin import get_bin_stock

		counted = [
			{"item": r["item"], "qty": r["qty"] - 1, "batch_no": r.get("batch_no")}
			for r in get_bin_stock(bin_name)
		]
		return rf_count.submit(bin_name=bin_name, counted=counted, task=start.get("task"))

	return _run(fn)


def check_replenish():
	def fn():
		from fwms.scheduler_jobs import check_replenishment

		made = check_replenishment()
		return f"{made} replenish task(s) created"

	return _run(fn)


def check_kpis():
	def fn():
		from fwms.scheduler_jobs import snapshot_kpis

		return snapshot_kpis()

	return _run(fn)


def check_floorview():
	def fn():
		frappe.set_user("supervisor@fwms.local")
		from fwms.api.floorview import dashboard

		d = dashboard()
		return {
			"slots": len(d["slots"]),
			"stocked_slots": sum(1 for s in d["slots"] if s["qty"] > 0),
			"docks": len(d["docks"]),
			"workers": len(d["workers"]),
			"tasks_with_coords": sum(1 for t in d["tasks"] if t["to_xy"][0] is not None),
			"events": len(d["events"]),
			"kpi": bool(d["kpi"]),
			"mode": d["release_mode"],
		}

	return _run(fn)


def check_consignment():
	def fn():
		dn = frappe.get_all("Delivery Note", filters={"docstatus": 1}, pluck="name", limit=1)
		if not dn:
			return "no submitted DNs yet"
		cons = frappe.get_doc({
			"doctype": "FWMS Consignment",
			"transport_mode": "Road",
			"carrier": "Nordic Haulage",
			"incoterms": "FOB",
			"place_of_loading": "Gothenburg",
			"place_of_delivery": "Hamburg",
			"delivery_notes": [{"delivery_note": dn[0]}],
		}).insert(ignore_permissions=True)
		frappe.db.commit()
		doc = frappe.get_doc("FWMS Consignment", cons.name)
		lines = doc.get_lines()
		pf = frappe.get_doc("Print Format", "Commercial Invoice (Export)")
		return {
			"consignment": cons.name,
			"lines": len(lines),
			"hs_first": lines[0]["hs_code"] if lines else None,
			"origin_first": lines[0]["origin"] if lines else None,
			"cartons": len(doc.get_cartons()),
			"print_format_loaded": bool(pf.name),
		}

	return _run(fn)


def check_pin_login():
	def fn():
		from fwms.fwms.doctype.fwms_worker.fwms_worker import verify_pin

		assert verify_pin("W001", "0001"), "valid PIN rejected"
		assert not verify_pin("W001", "9999"), "invalid PIN accepted"
		user = frappe.db.get_value("FWMS Worker", "W001", "user")
		roles = frappe.get_roles(user)
		assert "FWMS Operator" in roles, f"{user} lacks FWMS Operator role"
		return {"pin_ok": True, "user": user, "role_ok": True}

	return _run(fn)


def seed_with_traceback(site=None):
	"""Run the demo seeder with the real traceback printed (bench execute hides it)."""
	import traceback

	from fwms.setup.seed_demo import run

	try:
		out = run()
		print("SEED_OK", out)
		return f"OK: {out}"
	except Exception:
		frappe.db.rollback()
		traceback.print_exc()
		return "FAIL (traceback above)"


def check_returns():
	def fn():
		frappe.set_user("w007@fwms.local")
		from fwms.api import rf_returns

		dn = frappe.get_all("Delivery Note", filters={"docstatus": 1}, pluck="name", order_by="creation desc", limit=1)[0]
		info = rf_returns.dn_info(dn)
		item = info["items"][0]
		return rf_returns.process_return(
			delivery_note=dn,
			lines=[{"item_code": item["item_code"], "qty": 1, "grade": "B"}],
		)

	return _run(fn)


def check_replenish_confirm():
	def fn():
		frappe.set_user("w008@fwms.local")
		from fwms.api import rf_replenish

		task = frappe.get_all(
			"FWMS Task",
			filters={"task_type": "Replenish", "status": ("in", ("Open",))},
			pluck="name", limit=1,
		)
		if not task:
			return "no open replenish tasks (run check_replenish first)"
		sources = rf_replenish.source_bins(item=frappe.db.get_value("FWMS Task", task[0], "item"))
		if not sources:
			return "no bulk source stock"
		return rf_replenish.confirm(
			task=task[0], from_bin=sources[0]["bin"],
			qty=frappe.db.get_value("FWMS Task", task[0], "qty"),
			batch_no=sources[0]["batch_no"] or "",
		)

	return _run(fn)


def check_spike():
	def fn():
		from fwms.api.floorview import simulate_orders

		return simulate_orders(count=3)

	return _run(fn)
