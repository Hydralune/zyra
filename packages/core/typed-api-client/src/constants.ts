export const ZYRA_API_VERSION = "1.0"
export const ZYRA_API_VERSION_HEADER = "X-Zyra-Api-Version"
export const ZYRA_API_MIN_VERSION_HEADER = "X-Zyra-Api-Min-Version"
export const ZYRA_REQUEST_ID_HEADER = "X-Request-Id"
export const ZYRA_CORRELATION_ID_HEADER = "X-Correlation-Id"
export const ZYRA_CAUSATION_ID_HEADER = "X-Causation-Id"
export const ZYRA_IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
export const ZYRA_RECEIPT_ID_HEADER = "X-Zyra-Receipt-Id"
export const ZYRA_RECEIPT_REPLAY_HEADER = "X-Zyra-Receipt-Replayed"
export const ZYRA_CURSOR_HEADER = "X-Next-Cursor"
export const ZYRA_RETRY_AFTER_HEADER = "Retry-After"
export const ZYRA_AUTHORIZATION_HEADER = "Authorization"
export const ZYRA_CONTENT_TYPE_HEADER = "Content-Type"
export const ZYRA_ACCEPT_HEADER = "Accept"
export const ZYRA_CLIENT_NAME_HEADER = "X-Zyra-Client"
export const ZYRA_CLIENT_VERSION_HEADER = "X-Zyra-Client-Version"
export const ZYRA_OPERATION_HEADER = "X-Zyra-Operation"
export const ZYRA_ATTEMPT_HEADER = "X-Zyra-Attempt"
export const ZYRA_DEADLINE_HEADER = "X-Zyra-Deadline-Ms"
export const ZYRA_CONTRACT_HEADER = "X-Zyra-Contract"

export const JSON_MEDIA_TYPE = "application/json"
export const JSON_UTF8_MEDIA_TYPE = "application/json; charset=utf-8"
export const PROBLEM_JSON_MEDIA_TYPE = "application/problem+json"
export const DEFAULT_CLIENT_NAME = "zyra-web"
export const DEFAULT_CLIENT_VERSION = "0.1.0"
export const DEFAULT_TIMEOUT_MS = 30_000
export const MIN_TIMEOUT_MS = 25
export const MAX_TIMEOUT_MS = 10 * 60_000
export const DEFAULT_CONNECT_TIMEOUT_MS = 5_000
export const DEFAULT_RETRY_ATTEMPTS = 3
export const DEFAULT_RETRY_BASE_DELAY_MS = 125
export const DEFAULT_RETRY_MAX_DELAY_MS = 3_000
export const DEFAULT_RETRY_FACTOR = 2
export const DEFAULT_RETRY_JITTER = 0.2
export const DEFAULT_CURSOR_TTL_MS = 15 * 60_000
export const MAX_CURSOR_BYTES = 4_096
export const MAX_ERROR_BODY_BYTES = 64 * 1_024
export const MAX_RESPONSE_BODY_BYTES = 32 * 1_024 * 1_024
export const MAX_REQUEST_BODY_BYTES = 8 * 1_024 * 1_024
export const MAX_HEADER_VALUE_BYTES = 8 * 1_024
export const MAX_IDENTIFIER_BYTES = 256
export const MAX_IDEMPOTENCY_KEY_BYTES = 256
export const MAX_OPERATION_NAME_BYTES = 128
export const MAX_PATH_BYTES = 4_096
export const MAX_QUERY_ITEMS = 256
export const MAX_RECEIPT_HISTORY = 2_048
export const MAX_ATTEMPT_HISTORY = 256

export const TERMINAL_TASK_STATUSES = new Set([
  "completed",
  "failed",
  "cancelled",
  "rejected",
  "timed_out",
])

export const ACTIVE_TASK_STATUSES = new Set([
  "pending",
  "queued",
  "running",
  "blocked",
  "waiting",
  "paused",
  "recovering",
])

export const SAFE_HTTP_METHODS = new Set(["GET", "HEAD", "OPTIONS"])
export const IDEMPOTENT_HTTP_METHODS = new Set(["GET", "HEAD", "OPTIONS", "PUT", "DELETE"])
export const MUTATING_HTTP_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"])
export const RETRYABLE_HTTP_STATUSES = new Set([408, 425, 429, 500, 502, 503, 504])
export const NON_RETRYABLE_HTTP_STATUSES = new Set([400, 401, 403, 404, 405, 409, 410, 412, 415, 422, 426])
export const AUTH_HTTP_STATUSES = new Set([401, 403])
export const VERSION_HTTP_STATUSES = new Set([406, 409, 426])

export const RETRYABLE_NETWORK_MARKERS = [
  "load failed",
  "failed to fetch",
  "network request failed",
  "network connection was lost",
  "networkerror",
  "econnreset",
  "econnrefused",
  "ehostunreach",
  "enetunreach",
  "etimedout",
  "socket hang up",
  "connection closed",
  "connection reset",
  "connection refused",
  "fetch failed",
]

export const ABORT_ERROR_NAMES = new Set(["AbortError", "TimeoutError"])
export const DISCONNECT_ERROR_NAMES = new Set([
  "NetworkError",
  "TypeError",
  "SocketError",
  "ConnectionError",
])

export const IDENTITY_KINDS = [
  "session",
  "run",
  "task",
  "span",
  "checkpoint",
  "tool",
  "artifact",
  "control_command",
  "request",
  "receipt",
  "event",
] as const

export const IDENTITY_PREFIXES = {
  session: "session",
  run: "run",
  task: "task",
  span: "span",
  checkpoint: "checkpoint",
  tool: "tool",
  artifact: "artifact",
  control_command: "cmd",
  request: "request",
  receipt: "receipt",
  event: "event",
} as const

export const ERROR_CODES = {
  transportDisabled: "transport_disabled",
  normalizerDisabled: "normalizer_disabled",
  invalidConfiguration: "invalid_configuration",
  invalidIdentifier: "invalid_identifier",
  invalidRequest: "invalid_request",
  invalidResponse: "invalid_response",
  malformedJson: "malformed_json",
  responseTooLarge: "response_too_large",
  requestTooLarge: "request_too_large",
  timeout: "request_timeout",
  cancelled: "request_cancelled",
  disconnected: "transport_disconnected",
  authenticationRequired: "authentication_required",
  authenticationRejected: "authentication_rejected",
  versionMismatch: "api_version_mismatch",
  idempotencyRequired: "idempotency_key_required",
  receiptMissing: "receipt_missing",
  receiptMismatch: "receipt_mismatch",
  receiptConflict: "receipt_conflict",
  retryExhausted: "retry_exhausted",
  retryUnsafe: "retry_unsafe",
  cursorInvalid: "cursor_invalid",
  cursorExpired: "cursor_expired",
  cursorScopeMismatch: "cursor_scope_mismatch",
  httpError: "http_error",
  serverError: "server_error",
  notFound: "not_found",
  conflict: "conflict",
  validationFailed: "validation_failed",
  internalInvariant: "internal_invariant_failed",
} as const

export const OPERATION_NAMES = {
  health: "health",
  readiness: "runtime.readiness",
  taskList: "task.list",
  taskGet: "task.get",
  taskEvents: "task.events",
  taskEventIngressCapabilities: "task.event-ingress.capabilities",
  taskEventIngressSnapshot: "task.event-ingress.snapshot",
  taskEventIngressDelta: "task.event-ingress.delta",
  taskEventIngressSse: "task.event-ingress.sse",
  taskArtifactCatalog: "task.artifacts.catalog",
  taskArtifactMetadata: "task.artifacts.metadata",
  taskArtifactContent: "task.artifacts.content",
  taskArtifactReceipts: "task.artifacts.receipts",
  taskDiffReviewManifest: "task.diff-review.manifest",
  taskDiffReviewPage: "task.diff-review.page",
  taskDiffReviewFileContent: "task.diff-review.file-content",
  taskDiffReviewComment: "task.diff-review.comment",
  taskDiffReviewApply: "task.diff-review.apply",
  taskDiffReviewRollback: "task.diff-review.rollback",
  taskTerminalList: "task.terminals.list",
  taskTerminalGet: "task.terminals.get",
  taskTerminalCreate: "task.terminals.create",
  taskTerminalTicket: "task.terminals.ticket",
  taskTerminalKill: "task.terminals.kill",
  taskBrowserObservability: "task.browser.observability",
  taskBrowserControl: "task.browser.control",
  taskCreate: "task.create",
  taskCancel: "task.cancel",
  taskResume: "task.resume",
  taskControlCommand: "task.control-command",
  taskCommandQueue: "task.command-queue",
  taskCommandCancel: "task.command-cancel",
  permissionSessionOpen: "permission.session.open",
  permissionSessionResume: "permission.session.resume",
  permissionSummary: "permission.summary",
  permissionRequests: "permission.requests",
  permissionRequestGet: "permission.request.get",
  permissionRequestResolve: "permission.request.resolve",
  permissionRequestsExpire: "permission.requests.expire",
  permissionRules: "permission.rules",
  permissionMode: "permission.mode",
  permissionDecisions: "permission.decisions",
  scenarioRegistry: "scenario.registry",
  scenarioRunList: "scenario.run.list",
  scenarioRunGet: "scenario.run.get",
  scenarioRunEvidence: "scenario.run.evidence",
  scenarioRunCreate: "scenario.run.create",
  scenarioRunStart: "scenario.run.start",
  scenarioRunCancel: "scenario.run.cancel",
  scenarioRunArchive: "scenario.run.archive",
  scenarioRunVerify: "scenario.run.verify",
} as const

export const CONTRACT_NAMES = {
  health: "zyra.health.v1",
  readiness: "zyra.runtime-readiness.v1",
  taskList: "zyra.task-list.v1",
  taskDetail: "zyra.task-detail.v1",
  taskEvents: "zyra.task-events.v1",
  taskEventIngressCapabilities: "zyra.event-ingress-capabilities.v1",
  taskEventIngressSnapshot: "zyra.event-ingress-snapshot.v1",
  taskEventIngressDelta: "zyra.event-ingress-delta.v1",
  taskEventIngressSse: "zyra.event-ingress-sse.v1",
  taskArtifactCatalog: "zyra.artifact-catalog.v2",
  taskArtifactMetadata: "zyra.artifact-read.v2",
  taskArtifactContent: "zyra.artifact-read.v2",
  taskArtifactReceipts: "zyra.artifact-read-audit.v1",
  taskDiffReviewManifest: "zyra.diff-review-manifest.v1",
  taskDiffReviewPage: "zyra.diff-review-page.v1",
  taskDiffReviewFileContent: "zyra.diff-review-file-content.v1",
  taskDiffReviewComment: "zyra.diff-review-receipt.v1",
  taskDiffReviewTransaction: "zyra.patch-review-transaction-receipt.v1",
  taskTerminalList: "zyra.terminal-list.v1",
  taskTerminalSession: "zyra.terminal-session.v1",
  taskTerminalReceipt: "zyra.terminal-receipt.v1",
  taskBrowserObservability: "zyra.browser-observability.api.v1",
  taskBrowserControl: "zyra.browser-viewer.control.v1",
  taskMutation: "zyra.task-mutation.v1",
  taskControlCommand: "zyra.task-control-command.v1",
  taskCommandQueue: "zyra.command-queue.v1",
  taskCommandCancel: "zyra.command-cancel.v1",
  permissionControl: "zyra.permission-control.v2",
  scenarioRegistry: "zyra.scenario-registry.v1",
  scenarioRun: "zyra.scenario-run.v1",
  scenarioEvidence: "zyra.scenario-evidence-manifest.v1",
  scenarioMutation: "zyra.scenario-mutation.v1",
  error: "zyra.error.v1",
} as const

export function isJsonMediaType(value: string | null | undefined): boolean {
  if (!value) return false
  const normalized = value.split(";", 1)[0]?.trim().toLowerCase()
  return normalized === JSON_MEDIA_TYPE || normalized === PROBLEM_JSON_MEDIA_TYPE || normalized?.endsWith("+json") === true
}

export function normalizeHttpMethod(value: string): string {
  const method = String(value || "").trim().toUpperCase()
  if (!/^[A-Z]+$/.test(method)) throw new TypeError(`Invalid HTTP method: ${value}`)
  return method
}

export function isSafeHttpMethod(value: string): boolean {
  return SAFE_HTTP_METHODS.has(normalizeHttpMethod(value))
}

export function isMutatingHttpMethod(value: string): boolean {
  return MUTATING_HTTP_METHODS.has(normalizeHttpMethod(value))
}

export function isRetryableHttpStatus(value: number): boolean {
  return Number.isInteger(value) && RETRYABLE_HTTP_STATUSES.has(value)
}

export function isTerminalTaskStatus(value: unknown): boolean {
  return typeof value === "string" && TERMINAL_TASK_STATUSES.has(value.toLowerCase())
}

export function clampTimeout(value: number | undefined, fallback = DEFAULT_TIMEOUT_MS): number {
  const candidate = value === undefined ? fallback : value
  if (!Number.isFinite(candidate)) throw new TypeError("Timeout must be finite")
  return Math.min(MAX_TIMEOUT_MS, Math.max(MIN_TIMEOUT_MS, Math.floor(candidate)))
}

export function clampInteger(value: number, min: number, max: number, label: string): number {
  if (!Number.isFinite(value)) throw new TypeError(`${label} must be finite`)
  return Math.min(max, Math.max(min, Math.floor(value)))
}

export function boundedString(value: unknown, maxBytes: number, label: string): string {
  if (typeof value !== "string") throw new TypeError(`${label} must be a string`)
  const normalized = value.trim()
  if (!normalized) throw new TypeError(`${label} must not be empty`)
  if (new TextEncoder().encode(normalized).byteLength > maxBytes) {
    throw new TypeError(`${label} exceeds ${maxBytes} bytes`)
  }
  if (/[\u0000-\u001f\u007f]/.test(normalized)) {
    throw new TypeError(`${label} contains control characters`)
  }
  return normalized
}

export function optionalBoundedString(value: unknown, maxBytes: number, label: string): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return boundedString(value, maxBytes, label)
}

export function invariant(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error(`${ERROR_CODES.internalInvariant}: ${message}`)
}
