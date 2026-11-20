"""orderwitness: minimal witnesses for order-dependent pytest failures."""

from .core import VERSION, OrderWitnessError, audit, collect, ddmin

__all__ = ["VERSION", "OrderWitnessError", "audit", "collect", "ddmin"]
__version__ = VERSION