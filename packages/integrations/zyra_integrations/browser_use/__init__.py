"""Zyra-owned browser transport primitives derived from browser-use mechanics.

The package deliberately has no dependency on ``browser_use``.  It owns the
wire, process and event-delivery mechanics needed by BrowserWorker while the
worker package owns browser session policy and durable state.
"""

from .chrome_process import (
    ChromeLaunchPlan,
    ChromeLaunchPolicy,
    ChromeProcess,
    ChromeProcessController,
    ChromeProcessSnapshot,
    ChromeProcessState,
    ChromeSandboxBypassRequired,
)
from .cdp_transport import WebSocketCdpTransport
from .discovery import (
    BrowserEndpoint,
    BrowserEndpointDiscovery,
    BrowserTargetDescriptor,
    DiscoveryError,
    DiscoverySnapshot,
    RedactedHeaders,
)
from .event_bus import (
    BrowserEvent,
    BrowserEventBus,
    BrowserEventBusSnapshot,
    BrowserEventDelivery,
    BrowserSubscription,
)
from .protocol import (
    CdpCommand,
    CdpError,
    CdpEvent,
    CdpMessageCodec,
    CdpProtocolError,
    CdpResponse,
    CdpSessionRoute,
    CdpWireMessage,
    RequestIdAllocator,
)
from .websocket import (
    WebSocketClient,
    WebSocketClosed,
    WebSocketConfig,
    WebSocketError,
    WebSocketHandshakeError,
    WebSocketProtocolError,
    WebSocketSnapshot,
    WebSocketTimeout,
)

__all__ = [
    "BrowserEndpoint",
    "BrowserEndpointDiscovery",
    "BrowserEvent",
    "BrowserEventBus",
    "BrowserEventBusSnapshot",
    "BrowserEventDelivery",
    "BrowserSubscription",
    "BrowserTargetDescriptor",
    "CdpCommand",
    "CdpError",
    "CdpEvent",
    "CdpMessageCodec",
    "CdpProtocolError",
    "CdpResponse",
    "CdpSessionRoute",
    "CdpWireMessage",
    "ChromeLaunchPlan",
    "ChromeLaunchPolicy",
    "ChromeProcess",
    "ChromeProcessController",
    "ChromeProcessSnapshot",
    "ChromeProcessState",
    "ChromeSandboxBypassRequired",
    "DiscoveryError",
    "DiscoverySnapshot",
    "RedactedHeaders",
    "RequestIdAllocator",
    "WebSocketClient",
    "WebSocketCdpTransport",
    "WebSocketClosed",
    "WebSocketConfig",
    "WebSocketError",
    "WebSocketHandshakeError",
    "WebSocketProtocolError",
    "WebSocketSnapshot",
    "WebSocketTimeout",
]
