# Integration gateway stubs (Phases 6-8). Interfaces only — no runtime effect
# until a concrete adapter is registered and the matching Settings flag is on.

from fwms.gateways.automation_bridge import AutomationBridge
from fwms.gateways.carrier_gateway import CarrierGateway
from fwms.gateways.customs_gateway import CustomsGateway

__all__ = ["CarrierGateway", "CustomsGateway", "AutomationBridge"]
