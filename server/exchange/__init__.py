"""Exchange client factory — placeholder.

Real deployments wire a concrete exchange (ccxt, a venue-specific SDK, or a
hand-rolled REST/WS client) behind the `Exchange` protocol below. The bot
talks to that protocol; everything venue-specific stays inside this module.
"""
from .client import Exchange, get_client

__all__ = ["Exchange", "get_client"]
