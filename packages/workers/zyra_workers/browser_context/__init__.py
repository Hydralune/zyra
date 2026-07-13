"""Bounded browser messages, artifacts, context, and memory-candidate projection."""

from .action_result import BrowserActionResultProjector
from .action_envelope import (
    BrowserActionEnvelopeBatch,
    BrowserActionEnvelopeNormalizer,
    BrowserActionOutputArtifact,
    BrowserActionOutputExternalizer,
    NormalizedBrowserActionReceipt,
)
from .ablation import (
    BrowserAblationLane,
    BrowserAblationLaneMetrics,
    BrowserCompressionAblationReport,
    BrowserCompressionAblationRuntime,
)
from .application import BrowserMessageStateApplication
from .api_projection import BrowserContextApiProjection, BrowserContextApiProjectionRuntime
from .causal_runtime import BrowserTurnCausalAudit, BrowserTurnCausalRuntime
from .integration_audit import BrowserMessageIntegrationAudit, BrowserMessageIntegrationAuditRuntime
from .compressor import BrowserStateCompressor
from .context_port import BrowserNextContextPort
from .externalizer import BrowserStateArtifactExternalizer
from .fidelity import BrowserDisclosureAudit, BrowserDisclosureFidelityAuditor
from .history import BrowserHistoryNormalizer, BrowserHistoryPolicy, BrowserHistoryProjection
from .memory_bridge import BrowserMemorySignalBridge
from .message_manager import BrowserMessageManagerRuntime
from .models import (
    BrowserActionResultProjection,
    BrowserArtifactExternalization,
    BrowserMemoryCandidate,
    BrowserMessagePart,
    BrowserMessageTurn,
    BrowserNextContextReceipt,
)
from .turn_store import BrowserTurnProjectionRecord, BrowserTurnProjectionStore
from .task_integration import (
    BrowserContextDeliveryBatch,
    BrowserContextDeliveryState,
    BrowserContextQueueItem,
    BrowserContextProviderSelectionReceipt,
    BrowserContextScope,
    BrowserContextTaskCheckpoint,
    BrowserContextTaskIntegrationRuntime,
    BrowserMemoryCandidateConsumerPort,
    BrowserMemoryCandidateReceipt,
)

__all__ = [
    "BrowserActionResultProjection",
    "BrowserActionResultProjector",
    "BrowserActionEnvelopeBatch",
    "BrowserActionEnvelopeNormalizer",
    "BrowserActionOutputArtifact",
    "BrowserActionOutputExternalizer",
    "NormalizedBrowserActionReceipt",
    "BrowserAblationLane",
    "BrowserAblationLaneMetrics",
    "BrowserCompressionAblationReport",
    "BrowserCompressionAblationRuntime",
    "BrowserArtifactExternalization",
    "BrowserContextApiProjection",
    "BrowserContextApiProjectionRuntime",
    "BrowserTurnCausalAudit",
    "BrowserTurnCausalRuntime",
    "BrowserMessageIntegrationAudit",
    "BrowserMessageIntegrationAuditRuntime",
    "BrowserDisclosureAudit",
    "BrowserDisclosureFidelityAuditor",
    "BrowserHistoryNormalizer",
    "BrowserHistoryPolicy",
    "BrowserHistoryProjection",
    "BrowserMemoryCandidate",
    "BrowserMemorySignalBridge",
    "BrowserMessageManagerRuntime",
    "BrowserMessagePart",
    "BrowserMessageStateApplication",
    "BrowserMessageTurn",
    "BrowserNextContextPort",
    "BrowserNextContextReceipt",
    "BrowserStateArtifactExternalizer",
    "BrowserStateCompressor",
    "BrowserTurnProjectionRecord",
    "BrowserTurnProjectionStore",
    "BrowserContextDeliveryBatch",
    "BrowserContextDeliveryState",
    "BrowserContextQueueItem",
    "BrowserContextProviderSelectionReceipt",
    "BrowserContextScope",
    "BrowserContextTaskCheckpoint",
    "BrowserContextTaskIntegrationRuntime",
    "BrowserMemoryCandidateConsumerPort",
    "BrowserMemoryCandidateReceipt",
]
