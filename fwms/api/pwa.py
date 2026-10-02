# PWA plumbing for the RF app: service worker + manifest under the /rf scope.

from __future__ import annotations

import frappe

SERVICE_WORKER_JS = """// FWMS RF offline shell
const CACHE = 'fwms-rf-v1';
const SHELL = [
  '/rf',
  '/assets/fwms/rf/css/rf.css',
  '/assets/fwms/rf/js/app.js',
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


@frappe.whitelist(allow_guest=True)
def service_worker():
	if not frappe.response.get("headers"):
		frappe.response.headers = {}
	frappe.response.headers["Content-Type"] = "application/javascript"
	frappe.response.headers["Service-Worker-Allowed"] = "/rf"
	frappe.response.text = SERVICE_WORKER_JS


@frappe.whitelist(allow_guest=True)
def manifest():
	if not frappe.response.get("headers"):
		frappe.response.headers = {}
	frappe.response.headers["Content-Type"] = "application/manifest+json"
	frappe.response.text = MANIFEST_JSON
