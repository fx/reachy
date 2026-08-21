"""Shared wire types and golden fixtures for the Reachy Mini stack.

The wire types this package will hold are owned by the robot-link spec and
arrive in change 0003. Until then the package exists so the workspace has one
member that genuinely builds, imports and is tested.
"""

from reachy_contracts.version import VERSION, SemanticVersion, __version__

__all__ = ["VERSION", "SemanticVersion", "__version__"]
