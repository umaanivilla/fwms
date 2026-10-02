# Copyright (c) 2026
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class FWMSSettings(Document):
	pass


def get_settings() -> dict:
	"""Cached settings as a plain dict for hot paths."""
	return frappe.get_cached_doc("FWMS Settings").as_dict()
