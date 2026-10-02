# Copyright (c) 2026
# For license information, please see license.txt

import hashlib
import secrets

import frappe
from frappe.model.document import Document


class FWMSWorker(Document):
	def set_pin(self, pin: str) -> None:
		"""Store a salted SHA-256 hash of the numeric PIN."""
		salt = secrets.token_hex(8)
		digest = hashlib.sha256((salt + pin).encode()).hexdigest()
		self.db_set("pin_hash", f"{salt}${digest}", update_modified=False)


def verify_pin(worker_code: str, pin: str) -> str | None:
	"""Return worker name if PIN matches, else None. Constant-time compare."""
	pin_hash = frappe.db.get_value("FWMS Worker", worker_code, "pin_hash")
	if not pin_hash or not pin:
		return None
	salt, digest = pin_hash.split("$", 1)
	calc = hashlib.sha256((salt + pin).encode()).hexdigest()
	if secrets.compare_digest(calc, digest):
		return worker_code
	return None
