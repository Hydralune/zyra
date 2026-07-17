"""Durable permission receipts and transport seams retained after E02 cutover.

Policy evaluation, risk classification, hooks, modes, shell analysis, and the
live permission runtime are TypeScript-owned.  This package exports only
storage, API projection, custody, and approval transport contracts.
"""

from .api import (
    PERMISSION_API_SCHEMA,
    PermissionApiAuthenticationError,
    PermissionApiAuthorizationError,
    PermissionApiError,
    PermissionApiFacade,
    PermissionApiNotFound,
    PermissionApiOperation,
    PermissionApiResponse,
    PermissionCustodyEnvelope,
    extract_bearer_token,
    permission_api_error_response,
)
from .canonical import (
    CANONICAL_ARGUMENTS_VERSION,
    arguments_digest,
    build_request_fingerprint,
    build_tool_identity,
    canonical_arguments_json,
    canonicalize_arguments,
)
from .continuation import (
    PermissionContinuationAlreadyClaimed,
    PermissionContinuationClaim,
    PermissionContinuationConflict,
    PermissionContinuationCorruptError,
    PermissionContinuationDisabledError,
    PermissionContinuationError,
    PermissionContinuationIdentityError,
    PermissionContinuationOutcomeUnknown,
    PermissionContinuationPayloadMissing,
    PermissionContinuationPhase,
    PermissionContinuationRecord,
    PermissionContinuationReplay,
    PermissionContinuationRuntime,
    PermissionContinuationStateError,
    PermissionContinuationStore,
)
from .control_plane import (
    PERMISSION_CONTROL_SCHEMA,
    PermissionControlAuthority,
    PermissionControlCapability,
    PermissionControlCode,
    PermissionControlDisabled,
    PermissionControlError,
    PermissionControlForbidden,
    PermissionControlIdentityError,
    PermissionControlNotFound,
    PermissionControlPlane,
    PermissionControlResult,
    PermissionQuery,
    PermissionQueryPage,
    PermissionRetryDescriptor,
    default_api_capabilities,
    default_internal_capabilities,
    project_permission_decision,
    project_permission_request,
    project_permission_rule,
)
from .custody import (
    PERMISSION_CUSTODY_SCHEMA,
    PERMISSION_CUSTODY_VERSION,
    PermissionSessionCustodyBinding,
    PermissionSessionCustodyError,
    PermissionSessionCustodyInvalid,
    PermissionSessionCustodyReceipt,
    PermissionSessionCustodyRequired,
    PermissionSessionCustodyRevoked,
    PermissionSessionCustodyScopeMismatch,
    PermissionSessionCustodyStore,
)
from .decision_log import PermissionDecisionLog
from .events import PermissionEventEnvelope, PermissionEventProjector, PermissionRuntimeEventKind
from .grants import ExecutionGrantStore
from .models import *
from .request_queue import PermissionRequestQueue
from .store import (
    PERMISSION_SNAPSHOT_SCHEMA,
    PERMISSION_SNAPSHOT_VERSION,
    PERMISSION_STATE_SCHEMA,
    PERMISSION_STATE_VERSION,
    PermissionIdentityMismatch,
    PermissionRequestExpired,
    PermissionRequestTerminal,
    PermissionRuleStore,
    PermissionStateConflict,
    PermissionStateCorrupt,
    PermissionStateDisabled,
    PermissionStateStore,
    PermissionStoreError,
)
from .transports import *
from .typescript_browser_port import (
    BrowserActionPermissionConsumption,
    BrowserActionPermissionCustodyError,
    BrowserActionPermissionDecision,
    BrowserActionPermissionDisabled,
    BrowserActionPermissionError,
    BrowserActionPermissionGate,
    BrowserActionPermissionIdentityError,
    BrowserActionPermissionInput,
    browser_permission_setup_failure_events,
)

__all__ = [name for name in globals() if not name.startswith("_")]
