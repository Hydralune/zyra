"""Zyra-owned BrowserWorker action registry, security and execution boundary.

M1-S04C-01 decomposes mature browser-use action mechanics into Zyra contracts
while preserving the existing canonical owners:

* M1-04A owns browser/session/target/CDP lifecycle;
* M1-04B owns DOM capture and selector revision identity;
* M1-03A owns permission decisions and one-use execution grants;
* M1-02C owns generic ToolSpec/ToolCall/result-budget contracts;
* this package owns browser action definition, risk/security preflight and the
  single typed side-effect fence.

No module in this package scans or imports a root source repository at runtime.
"""

from .catalog import default_action_definitions
from .application import BrowserActionApplication
from .cdp_probe import CdpElementProbePort, ProbeConfig
from .clipboard_guard import (
    BrowserClipboardGuard,
    ClipboardAccess,
    ClipboardGuardError,
    ClipboardReceipt,
    RecordingClipboardPort,
)
from .download_guard import (
    BrowserDownloadGuard,
    DownloadEvent,
    DownloadGuardError,
    DownloadLease,
    DownloadState,
    RecordingDownloadControlPort,
)
from .continuation_runtime import (
    BrowserActionContinuationRuntime,
    BrowserContinuationClaim,
    BrowserContinuationPayload,
    BrowserContinuationPayloadStore,
)
from .control_runtime import (
    BrowserActionControlCommand,
    BrowserActionControlKind,
    BrowserActionControlResult,
    BrowserActionControlRuntime,
    BrowserActionControlStatus,
)
from .deadline_runtime import (
    ActionDeadline,
    BrowserActionCancellationRegistry,
    BrowserActionDeadlineRuntime,
    CancellationRecord,
    CancellationState,
)
from .download_runtime import BrowserDownloadLedger, BrowserNativeDownloadRuntime, CompletedBrowserDownload
from .event_port import (
    BrowserActionEventPort,
    BrowserActionResultProjector,
    MemoryArtifactPort,
    ResultProjection,
)
from .event_writer import BrowserActionContextReceipt, BrowserActionEventWriter
from .executor import (
    BrowserSideEffectFence,
    CdpCommand,
    ExecutionBindings,
    ExecutionContext,
    RecordingCdpTransport,
)
from .file_policy import (
    BrowserFilePolicy,
    BrowserFilePolicyError,
    FileIntent,
    FilePolicyConfig,
    FileReceipt,
)
from .form_policy import (
    BrowserFormPolicy,
    FormEffect,
    FormFieldSummary,
    FormMethod,
    FormPolicyConfig,
    FormPolicyError,
    FormReceipt,
)
from .factory import (
    BrowserActionFoundation,
    BrowserActionFoundationFactory,
    BrowserActionFoundationOptions,
)
from .gateway import (
    AuthorizedBrowserAction,
    BrowserActionGateway,
    BrowserActionGatewayConfig,
    BrowserActionGatewayError,
    CompletedBrowserAction,
    PreparedBrowserAction,
    SecurityReceipts,
)
from .geometry_guard import (
    BrowserGeometryGuard,
    GeometryGuardError,
    GeometryReceipt,
    LiveElementProbe,
    Point,
    RecordingElementProbe,
    Rect,
    Viewport,
)
from .hook_preflight import (
    BrowserPreflightHookRegistry,
    HookContext,
    HookDecision,
    HookDefinition,
    HookPhase,
    default_preflight_hooks,
)
from .models import (
    ActionDefinition,
    ActionExecutionResult,
    ActionIdentity,
    ActionPreflightReceipt,
    ActionRequest,
    ActionRiskAssessment,
    SelectorBinding,
)
from .integration_models import (
    BrowserActionIntegrationError,
    BrowserActionPlan,
    DispatchBoundary,
    PendingActionCheckpoint,
    PlanAdmission,
    PlanAdmissionIssue,
    PlanAdmissionIssueKind,
    PlanExecutionResult,
    PlanPhase,
    PlanStep,
    StepOutcome,
    StepState,
)
from .network_policy import (
    BrowserNetworkPolicy,
    NetworkPolicyConfig,
    NetworkPolicyError,
    NetworkReceipt,
    StaticHostResolver,
    SystemHostResolver,
    compile_patterns,
)
from .permission_bridge import BrowserActionPermissionBridge
from .plan_adapter import BrowserActionPlanAdapter, PlanAdapterConfig
from .redirect_guard import (
    BrowserRedirectGuard,
    InterceptedRequest,
    InterceptionDecision,
    RecordingFetchInterceptionPort,
)
from .registry import BrowserActionRegistry, default_browser_action_registry
from .schema import ActionArgumentValidator, BrowserActionSchemaProjector
from .secret_policy import (
    BrowserSecretPolicy,
    InMemorySecretProvider,
    SecretDescriptor,
    SecretKind,
    SecretPolicyError,
    SecretPurpose,
    SecretRedactor,
)
from .sequence import (
    AuthorizedSequence,
    BrowserActionSequenceCoordinator,
    CompletedSequence,
    PreflightedSequence,
    SequenceAdmissionError,
    SequenceState,
)
from .selector_guard import (
    BrowserSelectorGuard,
    SelectorExpectation,
    SelectorGuardError,
    SelectorReceipt,
)
from .semantic_probe import CdpElementSemanticProbe, LiveSemanticEvidence
from .session_adapter import (
    BrowserActionArtifactPort,
    BrowserNetworkInterception,
    CdpClipboardPort,
    CdpCommandOutcome,
    CdpDownloadControlPort,
    SessionBoundCdpTransport,
    SessionTransportConfig,
)
from .sensitive_policy import (
    BrowserSensitiveActionClassifier,
    ExecutionMode,
    RiskContext,
    SensitivePolicyConfig,
)
from .source_audit import BrowserActionSourceAuditor, SourceAuditFinding, SourceAuditReport


__all__ = [
    "ActionArgumentValidator",
    "ActionDeadline",
    "ActionDefinition",
    "ActionExecutionResult",
    "ActionIdentity",
    "ActionPreflightReceipt",
    "ActionRequest",
    "ActionRiskAssessment",
    "AuthorizedBrowserAction",
    "AuthorizedSequence",
    "BrowserActionApplication",
    "BrowserActionArtifactPort",
    "BrowserActionCancellationRegistry",
    "BrowserActionControlCommand",
    "BrowserActionControlKind",
    "BrowserActionControlResult",
    "BrowserActionControlRuntime",
    "BrowserActionControlStatus",
    "BrowserActionContextReceipt",
    "BrowserActionContinuationRuntime",
    "BrowserActionDeadlineRuntime",
    "BrowserActionEventPort",
    "BrowserActionEventWriter",
    "BrowserActionFoundation",
    "BrowserActionFoundationFactory",
    "BrowserActionFoundationOptions",
    "BrowserActionGateway",
    "BrowserActionGatewayConfig",
    "BrowserActionGatewayError",
    "BrowserActionIntegrationError",
    "BrowserActionPermissionBridge",
    "BrowserActionPlan",
    "BrowserActionPlanAdapter",
    "BrowserActionRegistry",
    "BrowserActionResultProjector",
    "BrowserActionSchemaProjector",
    "BrowserActionSequenceCoordinator",
    "BrowserActionSourceAuditor",
    "BrowserClipboardGuard",
    "BrowserContinuationClaim",
    "BrowserContinuationPayload",
    "BrowserContinuationPayloadStore",
    "BrowserDownloadLedger",
    "BrowserDownloadGuard",
    "BrowserFilePolicy",
    "BrowserFilePolicyError",
    "BrowserFormPolicy",
    "BrowserGeometryGuard",
    "BrowserNetworkPolicy",
    "BrowserNativeDownloadRuntime",
    "BrowserNetworkInterception",
    "BrowserPreflightHookRegistry",
    "BrowserRedirectGuard",
    "BrowserSecretPolicy",
    "BrowserSelectorGuard",
    "BrowserSensitiveActionClassifier",
    "BrowserSideEffectFence",
    "CdpClipboardPort",
    "CdpCommand",
    "CdpCommandOutcome",
    "CdpDownloadControlPort",
    "CdpElementProbePort",
    "CdpElementSemanticProbe",
    "CancellationRecord",
    "CancellationState",
    "ClipboardAccess",
    "ClipboardGuardError",
    "ClipboardReceipt",
    "CompletedBrowserAction",
    "CompletedBrowserDownload",
    "CompletedSequence",
    "DispatchBoundary",
    "DownloadEvent",
    "DownloadGuardError",
    "DownloadLease",
    "DownloadState",
    "ExecutionBindings",
    "ExecutionContext",
    "ExecutionMode",
    "FileIntent",
    "FilePolicyConfig",
    "FileReceipt",
    "FormEffect",
    "FormFieldSummary",
    "FormMethod",
    "FormPolicyConfig",
    "FormPolicyError",
    "FormReceipt",
    "GeometryGuardError",
    "GeometryReceipt",
    "HookContext",
    "HookDecision",
    "HookDefinition",
    "HookPhase",
    "InMemorySecretProvider",
    "InterceptedRequest",
    "InterceptionDecision",
    "LiveElementProbe",
    "LiveSemanticEvidence",
    "MemoryArtifactPort",
    "NetworkPolicyConfig",
    "NetworkPolicyError",
    "NetworkReceipt",
    "Point",
    "PendingActionCheckpoint",
    "PlanAdapterConfig",
    "PlanAdmission",
    "PlanAdmissionIssue",
    "PlanAdmissionIssueKind",
    "PlanExecutionResult",
    "PlanPhase",
    "PlanStep",
    "PreparedBrowserAction",
    "PreflightedSequence",
    "ProbeConfig",
    "RecordingCdpTransport",
    "RecordingClipboardPort",
    "RecordingDownloadControlPort",
    "RecordingElementProbe",
    "RecordingFetchInterceptionPort",
    "Rect",
    "ResultProjection",
    "RiskContext",
    "SecretDescriptor",
    "SecretKind",
    "SecretPolicyError",
    "SecretPurpose",
    "SecretRedactor",
    "SessionBoundCdpTransport",
    "SessionTransportConfig",
    "SecurityReceipts",
    "SequenceAdmissionError",
    "SequenceState",
    "SelectorBinding",
    "SelectorExpectation",
    "SelectorGuardError",
    "SelectorReceipt",
    "SensitivePolicyConfig",
    "StaticHostResolver",
    "SourceAuditFinding",
    "SourceAuditReport",
    "StepOutcome",
    "StepState",
    "SystemHostResolver",
    "Viewport",
    "compile_patterns",
    "default_action_definitions",
    "default_browser_action_registry",
    "default_preflight_hooks",
]
