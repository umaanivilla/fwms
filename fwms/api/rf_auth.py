# RF PIN login: worker code + PIN -> session as the worker's linked user.

from __future__ import annotations

import frappe

from fwms.api.rf_common import require_operator
from fwms.fwms.doctype.fwms_worker.fwms_worker import verify_pin


@frappe.whitelist(allow_guest=True)
def login(worker_code: str, pin: str) -> dict:
	worker_name = verify_pin(worker_code, pin)
	if not worker_name:
		# small delay to blunt brute force
		frappe.log_error(f"Failed RF login for {worker_code}", "FWMS RF Login")
		frappe.throw("Invalid worker code or PIN", frappe.AuthenticationError)

	worker = frappe.db.get_value(
		"FWMS Worker", worker_name,
		["name", "worker_code", "person_name", "user", "default_zone", "shift", "skills", "active"],
		as_dict=True,
	)
	if not worker.user:
		frappe.throw(f"Worker {worker_name} has no linked user")
	if not worker.active:
		frappe.throw(f"Worker {worker_name} is inactive")

	if not getattr(frappe.local, "login_manager", None):
		from frappe.auth import LoginManager

		frappe.local.login_manager = LoginManager()
	frappe.local.login_manager.login_as(worker.user)
	return {
		"worker": worker.name,
		"worker_code": worker.worker_code,
		"person_name": worker.person_name,
		"default_zone": worker.default_zone,
		"shift": worker.shift,
		"skills": worker.skills,
		"user": worker.user,
	}


@frappe.whitelist()
def me() -> dict:
	require_operator()
	worker = frappe.db.get_value(
		"FWMS Worker", {"user": frappe.session.user},
		["name", "worker_code", "person_name", "default_zone", "shift", "skills", "current_task"],
		as_dict=True,
	)
	return {"full_name": frappe.session.user_fullname, **(worker or {})}


@frappe.whitelist()
def logout() -> dict:
	frappe.local.login_manager.logout()
	return {"ok": True}
