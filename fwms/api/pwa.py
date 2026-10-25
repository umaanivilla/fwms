# PWA plumbing for the RF app: service worker + manifest under the /rf scope.

from __future__ import annotations

import frappe
from werkzeug.wrappers import Response

SERVICE_WORKER_JS = """// FWMS RF offline shell
const CACHE = 'fwms-rf-v6';
const SHELL = [
  '/rf',
  '/assets/fwms/rf/css/rf.css?v=20261004c',
  '/assets/fwms/rf/js/app.js?v=20261004c',
];
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith('/api') || url.pathname.startsWith('/assets')) return; // network for data
  e.respondWith(
    caches.match(e.request).then((hit) => hit || fetch(e.request))
  );
});
"""

MANIFEST_JSON = """{
  "name": "FWMS RF",
  "short_name": "FWMS RF",
  "start_url": "/rf",
  "display": "standalone",
  "background_color": "#0f1419",
  "theme_color": "#0f1419",
  "description": "FWMS handheld RF scanning terminal",
  "icons": []
}
"""


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def service_worker():
	"""Raw JS response — a whitelisted method may return a werkzeug Response
	(frappe.handler passes it through)."""
	return Response(
		SERVICE_WORKER_JS,
		mimetype="application/javascript",
		headers={
			"Service-Worker-Allowed": "/rf",
			"Cache-Control": "no-cache",
		},
	)


@frappe.whitelist(allow_guest=True)
def csrf():
	"""CSRF token for the vanilla-JS RF app (frappe.sessions.get_csrf_token
	is not guest-whitelisted in v16)."""
	from frappe.sessions import get_csrf_token

	return get_csrf_token()


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
def manifest():
	return Response(
		MANIFEST_JSON, mimetype="application/manifest+json", headers={"Cache-Control": "no-cache"}
	)
