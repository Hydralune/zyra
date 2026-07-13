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
from .event_port import (
    BrowserActionEventPort,
    BrowserActionResultProjector,
    MemoryArtifactPort,
    ResultProjection,
)
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
from .sensitive_policy import (
    BrowserSensitiveActionClassifier,
    ExecutionMode,
    RiskContext,
    SensitivePolicyConfig,
)
from .source_audit import BrowserActionSourceAuditor, SourceAuditFinding, SourceAuditReport


__all__ = [
    "ActionArgumentValidator",
    "ActionDefinition",
    "ActionExecutionResult",
    "ActionIdentity",
    "ActionPreflightReceipt",
    "ActionRequest",
    "ActionRiskAssessment",
    "AuthorizedBrowserAction",
    "AuthorizedSequence",
    "BrowserActionEventPort",
    "BrowserActionFoundation",
    "BrowserActionFoundationFactory",
    "BrowserActionFoundationOptions",
    "BrowserActionGateway",
    "BrowserActionGatewayConfig",
    "BrowserActionGatewayError",
    "BrowserActionPermissionBridge",
    "BrowserActionRegistry",
    "BrowserActionResultProjector",
    "BrowserActionSchemaProjector",
    "BrowserActionSequenceCoordinator",
    "BrowserActionSourceAuditor",
    "BrowserClipboardGuard",
    "BrowserDownloadGuard",
    "BrowserFilePolicy",
    "BrowserFilePolicyError",
    "BrowserFormPolicy",
    "BrowserGeometryGuard",
    "BrowserNetworkPolicy",
    "BrowserPreflightHookRegistry",
    "BrowserRedirectGuard",
    "BrowserSecretPolicy",
    "BrowserSelectorGuard",
    "BrowserSensitiveActionClassifier",
    "BrowserSideEffectFence",
    "CdpCommand",
    "CdpElementProbePort",
    "ClipboardAccess",
    "ClipboardGuardError",
    "ClipboardReceipt",
    "CompletedBrowserAction",
    "CompletedSequence",
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
    "MemoryArtifactPort",
    "NetworkPolicyConfig",
    "NetworkPolicyError",
    "NetworkReceipt",
    "Point",
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
    "SystemHostResolver",
    "Viewport",
    "compile_patterns",
    "default_action_definitions",
    "default_browser_action_registry",
    "default_preflight_hooks",
]
