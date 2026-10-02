# Automation Bridge interface (Phase 8) — the MFC layer.
# AS/RS, conveyor and AGV adapters consume FWMS Tasks (execution_channel =
# "Automated") through this contract; RF consumes the same queue manually.

from abc import ABC, abstractmethod


class AutomationBridge(ABC):
	"""Dispatch tasks to material-handling equipment and consume acks."""

	@abstractmethod
	def dispatch_task(self, task: dict) -> dict:
		"""Send a task to equipment; return {accepted: bool, equipment_id}."""

	@abstractmethod
	def on_ack(self, task_name: str, ack: dict) -> None:
		"""Handle equipment acknowledgement: {status: acked|done|failed, payload}."""
