import type {
  ArtifactProjection,
  CausalEventProjection,
} from "../../state/contracts.ts"

export const BROWSER_VIEWER_SCHEMA = "zyra.browser-viewer.v1" as const
export const BROWSER_OBSERVABILITY_SCHEMA =
  "zyra.browser-observability.api.v1" as const
export const BROWSER_CONTROL_SCHEMA = "zyra.browser-viewer.control.v1" as const

export const BrowserViewerPhase = {
  IDLE: "idle",
  LOADING: "loading",
  READY: "ready",
  PARTIAL: "partial",
  DISCONNECTED: "disconnected",
  FAILED: "failed",
  CLOSED: "closed",
} as const

export type BrowserViewerPhaseValue =
  (typeof BrowserViewerPhase)[keyof typeof BrowserViewerPhase]

export const BrowserStepPhase = {
  QUEUED: "queued",
  RUNNING: "running",
  WAITING_PERMISSION: "waiting_permission",
  COMPLETED: "completed",
  FAILED: "failed",
  CANCELLED: "cancelled",
  PARTIAL: "partial",
} as const

export type BrowserStepPhaseValue =
  (typeof BrowserStepPhase)[keyof typeof BrowserStepPhase]

export const BrowserControlAction = {
  NAVIGATE: "navigate",
  STOP: "stop",
  RETRY: "retry",
  INSPECT: "inspect",
} as const

export type BrowserControlActionValue =
  (typeof BrowserControlAction)[keyof typeof BrowserControlAction]

export const BrowserControlPhase = {
  DRAFT: "draft",
  VALIDATING: "validating",
  SUBMITTING: "submitting",
  ACCEPTED: "accepted",
  APPLIED: "applied",
  OBSERVED: "observed",
  DENIED: "denied",
  FAILED: "failed",
  TIMED_OUT: "timed_out",
  STALE: "stale",
  CANCELLED: "cancelled",
} as const

export type BrowserControlPhaseValue =
  (typeof BrowserControlPhase)[keyof typeof BrowserControlPhase]

export interface BrowserScope {
  runId: string
  taskId: string
  nodeId?: string
  browserSessionId: string
  canonicalSessionId?: string
  workerRequestId: string
}

export interface BrowserTarget {
  targetId: string
  browserSessionId: string
  url: string
  title: string
  kind: "page" | "popup" | "frame" | "worker" | "unknown"
  parentTargetId?: string
  openerTargetId?: string
  frameId?: string
  parentFrameId?: string
  depth: number
  active: boolean
  attached: boolean
  crashed: boolean
  createdSequence: number
  updatedSequence: number
  sourceRecordIds: readonly string[]
  findings: readonly string[]
}

export interface BrowserFrame {
  frameId: string
  targetId?: string
  parentFrameId?: string
  browserSessionId: string
  url: string
  name?: string
  securityOrigin?: string
  mimeType?: string
  depth: number
  main: boolean
  attached: boolean
  sequence: number
  sourceRecordIds: readonly string[]
}

export interface BrowserScreenshot {
  artifactId: string
  browserSessionId: string
  stepId?: string
  targetId?: string
  frameId?: string
  mediaType: string
  sha256: string
  expectedSha256?: string
  sizeBytes: number
  width?: number
  height?: number
  sequence: number
  sourceEventIds: readonly string[]
  sourceRecordIds: readonly string[]
  quarantined: boolean
  integrity: "verified" | "mismatch" | "unverified"
  current: boolean
}

export interface BrowserDomNodeSummary {
  nodeId: string
  backendNodeId?: number
  parentNodeId?: string
  role?: string
  name?: string
  tag?: string
  text?: string
  value?: string
  description?: string
  selector?: string
  clickable: boolean
  editable: boolean
  disabled: boolean
  hidden: boolean
  focused: boolean
  attributes: Readonly<Record<string, string>>
  sourceRecordId: string
}

export interface BrowserDomSummary {
  browserSessionId: string
  stepId?: string
  targetId?: string
  frameId?: string
  url?: string
  title?: string
  textDigest?: string
  accessibilityDigest?: string
  nodeCount: number
  interactiveCount: number
  visibleCount: number
  truncated: boolean
  promptInjectionFindings: readonly string[]
  nodes: readonly BrowserDomNodeSummary[]
  sourceRecordIds: readonly string[]
  sequence: number
}

export interface BrowserAction {
  actionId: string
  stepId: string
  browserSessionId: string
  workerRequestId: string
  name: string
  arguments: Readonly<Record<string, unknown>>
  argumentDigest?: string
  targetId?: string
  frameId?: string
  toolCallId?: string
  spanId?: string
  correlationId?: string
  causationId?: string
  permissionRequestId?: string
  permissionDecision?: string
  sequence: number
  startedAt?: string
  status: BrowserStepPhaseValue
  retryable: boolean
  sourceEventIds: readonly string[]
  sourceRecordIds: readonly string[]
}

export interface BrowserActionResult {
  resultId: string
  actionId?: string
  stepId: string
  browserSessionId: string
  toolCallId?: string
  spanId?: string
  correlationId?: string
  causationId?: string
  ok: boolean
  status: BrowserStepPhaseValue
  summary: string
  extractedContent?: string
  errorCode?: string
  errorMessage?: string
  retryable: boolean
  artifactIds: readonly string[]
  mutationIds: readonly string[]
  sequence: number
  completedAt?: string
  sourceEventIds: readonly string[]
  sourceRecordIds: readonly string[]
}

export interface BrowserDownload {
  artifactId: string
  browserSessionId: string
  stepId?: string
  actionId?: string
  filename: string
  mediaType: string
  sha256: string
  sizeBytes: number
  quarantined: boolean
  url?: string
  suggestedFilename?: string
  state: "requested" | "in_progress" | "completed" | "failed" | "quarantined"
  permissionDecision?: string
  sourceEventIds: readonly string[]
  sourceRecordIds: readonly string[]
  sequence: number
}

export interface BrowserToolReceipt {
  receiptId: string
  browserSessionId: string
  workerRequestId?: string
  stepId?: string
  actionId?: string
  toolCallId?: string
  spanId?: string
  correlationId?: string
  causationId?: string
  mutationIds: readonly string[]
  artifactIds: readonly string[]
  permissionRequestId?: string
  status: BrowserStepPhaseValue
  errorCode?: string
  summary: string
  sequence: number
  sourceEventIds: readonly string[]
  sourceRecordIds: readonly string[]
}

export interface BrowserStep {
  stepId: string
  browserSessionId: string
  workerRequestId: string
  index: number
  status: BrowserStepPhaseValue
  url?: string
  title?: string
  targetId?: string
  frameId?: string
  actionIds: readonly string[]
  resultIds: readonly string[]
  screenshotArtifactIds: readonly string[]
  downloadArtifactIds: readonly string[]
  toolReceiptIds: readonly string[]
  mutationIds: readonly string[]
  eventIds: readonly string[]
  recordIds: readonly string[]
  startSequence: number
  endSequence: number
  startedAt?: string
  completedAt?: string
  retryable: boolean
  partial: boolean
  errorCode?: string
  errorMessage?: string
}

export interface BrowserHealth {
  browserSessionId: string
  phase:
    | "unknown"
    | "healthy"
    | "degraded"
    | "unhealthy"
    | "crashed"
    | "reconnecting"
    | "stopped"
  crashed: boolean
  reconnecting: boolean
  reconnectCount: number
  processEpoch?: number
  lastHeartbeatAt?: string
  lastFailureId?: string
  recoveryIds: readonly string[]
  signalIds: readonly string[]
  reasons: readonly string[]
  sourceRecordIds: readonly string[]
  sequence: number
}

export interface BrowserCausalityFinding {
  code: string
  severity: "info" | "warning" | "error"
  message: string
  browserSessionId?: string
  stepId?: string
  actionId?: string
  resultId?: string
  artifactId?: string
  eventIds: readonly string[]
  recordIds: readonly string[]
}

export interface BrowserSessionProjection {
  scope: BrowserScope
  url?: string
  title?: string
  activeTargetId?: string
  status: BrowserHealth["phase"]
  targets: readonly BrowserTarget[]
  frames: readonly BrowserFrame[]
  steps: readonly BrowserStep[]
  actions: readonly BrowserAction[]
  results: readonly BrowserActionResult[]
  screenshots: readonly BrowserScreenshot[]
  domSummaries: readonly BrowserDomSummary[]
  downloads: readonly BrowserDownload[]
  toolReceipts: readonly BrowserToolReceipt[]
  health: BrowserHealth
  findings: readonly BrowserCausalityFinding[]
  firstSequence: number
  lastSequence: number
  headDigest?: string
}

export interface BrowserViewerProjection {
  schema: typeof BROWSER_VIEWER_SCHEMA
  taskId: string
  runId: string
  phase: BrowserViewerPhaseValue
  sessions: readonly BrowserSessionProjection[]
  activeSessionId?: string
  selectedSessionId?: string
  selectedStepId?: string
  selectedActionId?: string
  totalSteps: number
  totalActions: number
  totalDownloads: number
  totalScreenshots: number
  projectionRevision: number
  refreshedAt: number
  partial: boolean
  findings: readonly BrowserCausalityFinding[]
}

export interface BrowserObservabilityEnvelope {
  schema: typeof BROWSER_OBSERVABILITY_SCHEMA
  view: string
  taskId: string
  scopeCount: number
  scopes: readonly Readonly<Record<string, unknown>>[]
  raw: Readonly<Record<string, unknown>>
}

export interface BrowserControlIdentity {
  taskId: string
  runId: string
  browserSessionId: string
  workerRequestId?: string
  actionId?: string
  expectedGeneration?: number
  expectedTaskRevision?: number
  actorId: string
  sealed: boolean
}

export interface BrowserControlRequest {
  schema: typeof BROWSER_CONTROL_SCHEMA
  commandId: string
  requestId: string
  idempotencyKey: string
  action: BrowserControlActionValue
  identity: BrowserControlIdentity
  url?: string
  retryAction?: Readonly<Record<string, unknown>>
  reason: string
  createdAt: number
  timeoutMs: number
}

export interface BrowserControlReceipt {
  schema: typeof BROWSER_CONTROL_SCHEMA
  commandId: string
  requestId: string
  idempotencyKey: string
  action: BrowserControlActionValue
  phase: BrowserControlPhaseValue
  taskId: string
  runId: string
  browserSessionId: string
  workerRequestId?: string
  actionId?: string
  statusCode?: number
  eventIds: readonly string[]
  mutationIds: readonly string[]
  artifactIds: readonly string[]
  permissionRequestId?: string
  lifecycleReceiptId?: string
  errorCode?: string
  errorMessage?: string
  retryable: boolean
  replayed: boolean
  sealed: boolean
  interventionCounted: boolean
  humanInterventionCount: number
  manualMutationApplied: boolean
  approvalWaitEntered: boolean
  automaticRecoveryAction?: string
  submittedAt: number
  completedAt: number
  raw: Readonly<Record<string, unknown>>
}

export interface BrowserViewerSelection {
  sessionId?: string
  stepId?: string
  actionId?: string
  targetId?: string
  artifactId?: string
  followLive: boolean
}

export interface BrowserViewerFilters {
  query: string
  statuses: readonly BrowserStepPhaseValue[]
  actionNames: readonly string[]
  failuresOnly: boolean
  downloadsOnly: boolean
  popupsOnly: boolean
  fromSequence?: number
  toSequence?: number
}

export interface BrowserViewerWindow {
  start: number
  end: number
  overscanStart: number
  overscanEnd: number
  beforeHeight: number
  afterHeight: number
  totalHeight: number
  itemHeight: number
  stepIds: readonly string[]
}

export interface BrowserViewerState {
  projection: BrowserViewerProjection
  selection: BrowserViewerSelection
  filters: BrowserViewerFilters
  window: BrowserViewerWindow
  controls: readonly BrowserControlReceipt[]
  activeControlIds: readonly string[]
  online: boolean
  error?: {
    code: string
    message: string
    retryable: boolean
    occurredAt: number
  }
  revision: number
}

export interface BrowserCanonicalInputs {
  events: readonly CausalEventProjection[]
  artifacts: readonly ArtifactProjection[]
  projectionRevision: number
}

export interface BrowserProjectionInputs extends BrowserCanonicalInputs {
  taskId: string
  runId: string
  envelopes: readonly BrowserObservabilityEnvelope[]
  refreshedAt?: number
}

export class BrowserContractError extends TypeError {
  readonly code: string
  readonly path: string
  readonly value: unknown

  constructor(code: string, message: string, path: string, value?: unknown) {
    super(message)
    this.name = "BrowserContractError"
    this.code = code
    this.path = path
    this.value = value
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value)
}

export function recordValue(
  value: unknown,
  path = "$",
): Readonly<Record<string, unknown>> {
  if (!isRecord(value)) {
    throw new BrowserContractError(
      "browser_contract_object_required",
      `${path} must be an object`,
      path,
      value,
    )
  }
  return value
}

export function optionalRecord(
  value: unknown,
): Readonly<Record<string, unknown>> | undefined {
  return isRecord(value) ? value : undefined
}

export function arrayValue(
  value: unknown,
  path: string,
): readonly unknown[] {
  if (!Array.isArray(value)) {
    throw new BrowserContractError(
      "browser_contract_array_required",
      `${path} must be an array`,
      path,
      value,
    )
  }
  return value
}

export function stringValue(
  value: unknown,
  path: string,
  options: {
    optional?: boolean
    maximum?: number
    allowEmpty?: boolean
  } = {},
): string {
  if (value === undefined || value === null) {
    if (options.optional) return ""
    throw new BrowserContractError(
      "browser_contract_string_required",
      `${path} must be a string`,
      path,
      value,
    )
  }
  if (typeof value !== "string" && typeof value !== "number") {
    throw new BrowserContractError(
      "browser_contract_string_required",
      `${path} must be a string`,
      path,
      value,
    )
  }
  const result = String(value).trim()
  if (!result && !options.optional && !options.allowEmpty) {
    throw new BrowserContractError(
      "browser_contract_string_empty",
      `${path} must not be empty`,
      path,
      value,
    )
  }
  const maximum = options.maximum ?? 256 * 1024
  if (new TextEncoder().encode(result).byteLength > maximum) {
    throw new BrowserContractError(
      "browser_contract_string_oversized",
      `${path} exceeds ${maximum} bytes`,
      path,
      value,
    )
  }
  return result
}

export function optionalString(
  value: unknown,
  path = "$",
  maximum = 256 * 1024,
): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return stringValue(value, path, { optional: true, maximum }) || undefined
}

export function integerValue(
  value: unknown,
  path: string,
  options: { minimum?: number; maximum?: number; fallback?: number } = {},
): number {
  if (value === undefined || value === null || value === "") {
    if (options.fallback !== undefined) return options.fallback
    throw new BrowserContractError(
      "browser_contract_integer_required",
      `${path} must be an integer`,
      path,
      value,
    )
  }
  const result = Number(value)
  const minimum = options.minimum ?? Number.MIN_SAFE_INTEGER
  const maximum = options.maximum ?? Number.MAX_SAFE_INTEGER
  if (!Number.isSafeInteger(result) || result < minimum || result > maximum) {
    throw new BrowserContractError(
      "browser_contract_integer_invalid",
      `${path} must be an integer between ${minimum} and ${maximum}`,
      path,
      value,
    )
  }
  return result
}

export function numberValue(
  value: unknown,
  path: string,
  options: { minimum?: number; maximum?: number; fallback?: number } = {},
): number {
  if (value === undefined || value === null || value === "") {
    if (options.fallback !== undefined) return options.fallback
    throw new BrowserContractError(
      "browser_contract_number_required",
      `${path} must be a finite number`,
      path,
      value,
    )
  }
  const result = Number(value)
  const minimum = options.minimum ?? -Number.MAX_VALUE
  const maximum = options.maximum ?? Number.MAX_VALUE
  if (!Number.isFinite(result) || result < minimum || result > maximum) {
    throw new BrowserContractError(
      "browser_contract_number_invalid",
      `${path} must be a finite number between ${minimum} and ${maximum}`,
      path,
      value,
    )
  }
  return result
}

export function booleanValue(
  value: unknown,
  fallback = false,
): boolean {
  if (typeof value === "boolean") return value
  if (typeof value === "number") return value !== 0
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase()
    if (["1", "true", "yes", "allow", "allowed", "ok"].includes(normalized)) {
      return true
    }
    if (["0", "false", "no", "deny", "denied", "failed"].includes(normalized)) {
      return false
    }
  }
  return fallback
}

export function stringArray(
  value: unknown,
  maximum = 10_000,
): readonly string[] {
  const values =
    Array.isArray(value)
      ? value
      : typeof value === "string"
        ? value.split(",")
        : []
  const output = new Set<string>()
  for (const candidate of values.slice(0, maximum)) {
    const normalized = String(candidate ?? "").trim()
    if (normalized) output.add(normalized)
  }
  return Object.freeze([...output])
}

export function recordArray(
  value: unknown,
  maximum = 10_000,
): readonly Readonly<Record<string, unknown>>[] {
  if (!Array.isArray(value)) return Object.freeze([])
  return Object.freeze(
    value
      .slice(0, maximum)
      .filter(isRecord)
      .map((entry) => Object.freeze({ ...entry })),
  )
}

export function firstDefined(
  source: Readonly<Record<string, unknown>>,
  ...keys: readonly string[]
): unknown {
  for (const key of keys) {
    if (source[key] !== undefined && source[key] !== null) return source[key]
  }
  return undefined
}

export function nestedRecord(
  source: Readonly<Record<string, unknown>>,
  ...path: readonly string[]
): Readonly<Record<string, unknown>> | undefined {
  let current: unknown = source
  for (const segment of path) {
    if (!isRecord(current)) return undefined
    current = current[segment]
  }
  return optionalRecord(current)
}

export function nestedValue(
  source: Readonly<Record<string, unknown>>,
  ...path: readonly string[]
): unknown {
  let current: unknown = source
  for (const segment of path) {
    if (!isRecord(current)) return undefined
    current = current[segment]
  }
  return current
}

export function normalizedIdentity(
  value: unknown,
  label: string,
  optional = false,
): string {
  const normalized = String(value ?? "").trim()
  if (!normalized && optional) return ""
  if (
    normalized.length < 1
    || normalized.length > 512
    || !/^[A-Za-z0-9][A-Za-z0-9._:@/+~-]*$/.test(normalized)
  ) {
    throw new BrowserContractError(
      "browser_contract_identity_invalid",
      `${label} is not a valid identity`,
      label,
      value,
    )
  }
  return normalized
}

export function normalizedUrl(
  value: unknown,
  path = "$.url",
  options: { optional?: boolean; allowInternal?: boolean } = {},
): string {
  const raw = stringValue(value, path, {
    optional: options.optional,
    maximum: 16 * 1024,
  })
  if (!raw && options.optional) return ""
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    throw new BrowserContractError(
      "browser_contract_url_invalid",
      `${path} is not a valid absolute URL`,
      path,
      value,
    )
  }
  const allowed = new Set(["http:", "https:"])
  if (options.allowInternal) {
    allowed.add("about:")
    allowed.add("data:")
    allowed.add("blob:")
    allowed.add("file:")
  }
  if (!allowed.has(url.protocol)) {
    throw new BrowserContractError(
      "browser_contract_url_scheme",
      `${path} uses an unsupported URL scheme`,
      path,
      value,
    )
  }
  url.username = ""
  url.password = ""
  return url.toString()
}

export function safeObservedUrl(value: unknown): string {
  if (value === undefined || value === null || value === "") return ""
  try {
    return normalizedUrl(value, "$.observed_url", {
      optional: true,
      allowInternal: true,
    })
  } catch {
    const raw = String(value).trim()
    return raw.length <= 16 * 1024 ? raw : `${raw.slice(0, 16 * 1024)}…`
  }
}

export function stableDigest(value: unknown): string {
  const normalized = stableJson(value)
  let h1 = 0xdeadbeef ^ normalized.length
  let h2 = 0x41c6ce57 ^ normalized.length
  for (let index = 0; index < normalized.length; index += 1) {
    const code = normalized.charCodeAt(index)
    h1 = Math.imul(h1 ^ code, 2654435761)
    h2 = Math.imul(h2 ^ code, 1597334677)
  }
  h1 =
    Math.imul(h1 ^ (h1 >>> 16), 2246822507)
    ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909)
  h2 =
    Math.imul(h2 ^ (h2 >>> 16), 2246822507)
    ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909)
  const left = (h2 >>> 0).toString(16).padStart(8, "0")
  const right = (h1 >>> 0).toString(16).padStart(8, "0")
  return `fnv128:${left}${right}`
}

export function stableJson(value: unknown): string {
  const seen = new WeakSet<object>()
  const normalize = (current: unknown, depth: number): unknown => {
    if (depth > 32) return "[depth-limit]"
    if (
      current === null
      || typeof current === "string"
      || typeof current === "boolean"
    ) return current
    if (typeof current === "number") {
      return Number.isFinite(current) ? current : String(current)
    }
    if (typeof current === "bigint") return current.toString()
    if (typeof current === "undefined") return null
    if (Array.isArray(current)) {
      return current.slice(0, 10_000).map((entry) => normalize(entry, depth + 1))
    }
    if (isRecord(current)) {
      if (seen.has(current)) return "[circular]"
      seen.add(current)
      const output: Record<string, unknown> = {}
      for (const key of Object.keys(current).sort().slice(0, 10_000)) {
        output[key] = normalize(current[key], depth + 1)
      }
      return output
    }
    return String(current)
  }
  return JSON.stringify(normalize(value, 0))
}

export function parseBrowserScope(
  value: unknown,
  path = "$.scope",
): BrowserScope {
  const source = recordValue(value, path)
  return Object.freeze({
    runId: normalizedIdentity(
      firstDefined(source, "run_id", "runId"),
      `${path}.run_id`,
    ),
    taskId: normalizedIdentity(
      firstDefined(source, "task_id", "taskId"),
      `${path}.task_id`,
    ),
    nodeId:
      normalizedIdentity(
        firstDefined(source, "node_id", "nodeId"),
        `${path}.node_id`,
        true,
      ) || undefined,
    browserSessionId: normalizedIdentity(
      firstDefined(source, "browser_session_id", "browserSessionId"),
      `${path}.browser_session_id`,
    ),
    canonicalSessionId:
      normalizedIdentity(
        firstDefined(source, "canonical_session_id", "canonicalSessionId"),
        `${path}.canonical_session_id`,
        true,
      ) || undefined,
    workerRequestId: normalizedIdentity(
      firstDefined(source, "worker_request_id", "workerRequestId"),
      `${path}.worker_request_id`,
    ),
  })
}

export function parseBrowserObservabilityEnvelope(
  value: unknown,
  expectedTaskId?: string,
): BrowserObservabilityEnvelope {
  const outer = recordValue(value)
  const source =
    optionalRecord(outer.browser_observability)
    ?? optionalRecord(outer.browserObservability)
    ?? outer
  const schema = optionalString(source.schema, "$.schema", 256)
  if (schema && schema !== BROWSER_OBSERVABILITY_SCHEMA) {
    throw new BrowserContractError(
      "browser_observability_schema_unsupported",
      `Unsupported browser observability schema ${schema}`,
      "$.schema",
      schema,
    )
  }
  const taskId = normalizedIdentity(
    firstDefined(source, "task_id", "taskId",)
      ?? firstDefined(outer, "task_id", "taskId"),
    "$.task_id",
  )
  if (expectedTaskId && taskId !== expectedTaskId) {
    throw new BrowserContractError(
      "browser_observability_task_mismatch",
      `Browser observability belongs to ${taskId}, expected ${expectedTaskId}`,
      "$.task_id",
      taskId,
    )
  }
  const view = stringValue(source.view ?? "summary", "$.view", {
    maximum: 64,
  }).toLowerCase()
  const scopes = recordArray(source.scopes, 5_000)
  const scopeCount = integerValue(
    firstDefined(source, "scope_count", "scopeCount") ?? scopes.length,
    "$.scope_count",
    { minimum: 0, maximum: 5_000 },
  )
  if (scopeCount !== scopes.length && view !== "summary") {
    throw new BrowserContractError(
      "browser_observability_scope_count_mismatch",
      "Browser observability scope count does not match admitted scopes",
      "$.scope_count",
      scopeCount,
    )
  }
  return Object.freeze({
    schema: BROWSER_OBSERVABILITY_SCHEMA,
    view,
    taskId,
    scopeCount,
    scopes,
    raw: Object.freeze({ ...source }),
  })
}

export function emptyBrowserViewerProjection(
  taskId: string,
  runId: string,
  phase: BrowserViewerPhaseValue = BrowserViewerPhase.IDLE,
): BrowserViewerProjection {
  return Object.freeze({
    schema: BROWSER_VIEWER_SCHEMA,
    taskId: normalizedIdentity(taskId, "task_id"),
    runId: normalizedIdentity(runId, "run_id"),
    phase,
    sessions: Object.freeze([]),
    totalSteps: 0,
    totalActions: 0,
    totalDownloads: 0,
    totalScreenshots: 0,
    projectionRevision: 0,
    refreshedAt: Date.now(),
    partial: false,
    findings: Object.freeze([]),
  })
}

export function defaultBrowserViewerFilters(): BrowserViewerFilters {
  return Object.freeze({
    query: "",
    statuses: Object.freeze([]),
    actionNames: Object.freeze([]),
    failuresOnly: false,
    downloadsOnly: false,
    popupsOnly: false,
  })
}

export function defaultBrowserViewerWindow(): BrowserViewerWindow {
  return Object.freeze({
    start: 0,
    end: 0,
    overscanStart: 0,
    overscanEnd: 0,
    beforeHeight: 0,
    afterHeight: 0,
    totalHeight: 0,
    itemHeight: 72,
    stepIds: Object.freeze([]),
  })
}
