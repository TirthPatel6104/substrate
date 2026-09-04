"""Substrate — a local-first intelligence layer for AI agents.

Public API:
    from substrate import Substrate
    sub = Substrate(workspace="myproject")
    sub.dispatch("memory.remember", {"content": "we deploy with make release"})
"""

from .core import TOOL_SCHEMAS, Substrate
from .safety import Classification, Level, safety_level

__version__ = "0.1.0"
__all__ = ["Substrate", "TOOL_SCHEMAS", "safety_level", "Level", "Classification", "__version__"]
