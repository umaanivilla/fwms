# ZPL label bridge: renders LPN/carton/pallet labels as raw ZPL II.
# Delivered by the RF browser as a .zpl download or pushed raw to a
# configured network printer (Settings host:port, default 9100).

from __future__ import annotations

import socket

import frappe

# 4x6 inch label, 203 dpi Zebra baseline
LPN_TEMPLATE = """^XA
^CF0,36
^FO60,40^FD{header}^FS
^FO60,100^FD{lpn}^FS
^BY3,3,120^FO60,150^BC^FD{lpn}^FS
^FO60,300^FD{line2}^FS
^FO60,350^FD{line3}^FS
^FO60,400^FD{company}^FS
^PQ{copies}
^XZ
"""


def render_lpn_label(lpn) -> str:
	"""Render the standard container label. `lpn` is an FWMS LPN doc."""
	contents = ", ".join(f"{r.qty:g} x {r.item}" for r in (lpn.items or [])[:3]) or "empty"
	line2 = f"Type: {lpn.lpn_type}  Zone: {lpn.zone or '-'}"
	line3 = contents[:70]
	return LPN_TEMPLATE.format(
		header="FWMS CONTAINER",
		lpn=lpn.name,
		line2=line2,
		line3=line3,
		company=frappe.db.get_single_value("FWMS Settings", "company") or "FWMS",
		copies=1,
	)


def render_pallet_label(children: list[str]) -> str:
	body = "^FO60,100^FDPALLET^FS\n"
	for i, child in enumerate(children[:8]):
		body += f"^FO60,{160 + i * 36}^FD{child}^FS\n"
	return f"^XA\n^CF0,36\n^FO60,40^FD{len(children)} X CARTONS^FS\n{body}^XZ"


def send_to_printer(zpl: str) -> dict:
	"""Raw TCP print to the configured ZPL printer (port 9100)."""
	host = frappe.db.get_single_value("FWMS Settings", "zpl_printer_host")
	port = frappe.db.get_single_value("FWMS Settings", "zpl_printer_port") or 9100
	if not host:
		frappe.throw("No ZPL printer configured in FWMS Settings")
	with socket.create_connection((host, int(port)), timeout=5) as sock:
		sock.sendall(zpl.encode())
	return {"sent_to": f"{host}:{port}", "bytes": len(zpl)}


@frappe.whitelist()
def label(lpn: str, print_now: bool = False) -> str:
	frappe.only_for(("FWMS Operator", "FWMS Supervisor", "System Manager"))
	doc = frappe.get_doc("FWMS LPN", lpn)
	zpl = render_lpn_label(doc)
	if print_now:
		send_to_printer(zpl)
	frappe.response.headers["Content-Type"] = "application/octet-stream"
	frappe.response.headers["Content-Disposition"] = f'attachment; filename="{lpn}.zpl"'
	frappe.response.text = zpl
	return zpl
