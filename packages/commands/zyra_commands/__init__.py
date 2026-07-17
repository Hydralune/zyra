"""Durable control-plane descriptors retained after E02 command cutover.

Slash-command parsing and MCP local dispatch are TypeScript-owned.  The Python
package continues to expose only the protected durable control-plane runtime.
"""

from .runtime import *

__all__ = [name for name in globals() if not name.startswith("_")]
