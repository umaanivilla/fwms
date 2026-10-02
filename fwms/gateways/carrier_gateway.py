# Carrier Gateway interface (Phase 6).
# Concrete adapters (erpnext-shipping backend, Shippo/EasyPost-style aggregator,
# regional carriers) implement this contract; FWMS code talks only to it.

from abc import ABC, abstractmethod


class CarrierGateway(ABC):
	"""Rates / labels / tracking / pickups across carriers."""

	@abstractmethod
	def get_rates(self, shipment: dict) -> list[dict]:
		"""Return [{carrier, service, rate, currency, eta}] for a shipment payload."""

	@abstractmethod
	def create_label(self, shipment: dict, service: str) -> dict:
		"""Buy a label; return {label_url, tracking_number, carrier, zpl?}."""

	@abstractmethod
	def track(self, tracking_number: str) -> dict:
		"""Return {status, events: [{at, description, location}]}."""

	@abstractmethod
	def schedule_pickup(self, pickup: dict) -> dict:
		"""Return {confirmation, pickup_at} for a pickup request."""


class NullCarrierGateway(CarrierGateway):
	"""No-op adapter used while the gateway flag is off."""

	def get_rates(self, shipment: dict) -> list[dict]:
		return []

	def create_label(self, shipment: dict, service: str) -> dict:
		raise NotImplementedError("Carrier Gateway is not enabled in FWMS Settings")

	def track(self, tracking_number: str) -> dict:
		return {"status": "unknown", "events": []}

	def schedule_pickup(self, pickup: dict) -> dict:
		raise NotImplementedError("Carrier Gateway is not enabled in FWMS Settings")
