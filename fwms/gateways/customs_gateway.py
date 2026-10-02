# Customs Gateway interface (Phase 7).
# Per-country e-filing adapters (US AES/EEI, UK CDS, EU ATLAS, India ICEGATE)
# implement this contract. The printable export pack needs no gateway.

from abc import ABC, abstractmethod


class CustomsGateway(ABC):
	"""Territory-specific export declaration filing."""

	@abstractmethod
	def submit_declaration(self, declaration: dict) -> dict:
		"""File an export declaration; return {declaration_id, status, accepted_at}."""

	@abstractmethod
	def get_status(self, declaration_id: str) -> dict:
		"""Return {status, messages: [{code, severity, text}]}."""


class NullCustomsGateway(CustomsGateway):
	def submit_declaration(self, declaration: dict) -> dict:
		raise NotImplementedError("Customs Gateway is not enabled in FWMS Settings")

	def get_status(self, declaration_id: str) -> dict:
		return {"status": "not-filed", "messages": []}
