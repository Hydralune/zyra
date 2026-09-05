import {
  CONTRACT_NAMES,
  MAX_OPERATION_NAME_BYTES,
  OPERATION_NAMES,
  boundedString,
  normalizeHttpMethod,
} from "./constants.ts"
import { RequestValidationError } from "./errors.ts"
import { encodePathSegment, normalizePath, type QueryRecord } from "./request.ts"

export type EndpointKind = "query" | "mutation" | "stream"
export type ReceiptRequirement = "none" | "required"
export type AuthRequirement = "optional" | "required"

export interface EndpointDefinition {
  operation: string
  contract: string
  method: string
  pathTemplate: string
  kind: EndpointKind
  receipt: ReceiptRequirement
  auth: AuthRequirement
  expectedStatuses: readonly number[]
  pathParameters: readonly string[]
  queryParameters: readonly string[]
}

export interface ResolvedEndpoint {
  operation: string
  contract: string
  method: string
  path: string
  kind: EndpointKind
  receipt: ReceiptRequirement
  auth: AuthRequirement
  expectedStatuses: number[]
  query: QueryRecord
}

function normalizeParameterNames(values: readonly string[] | undefined, label: string): string[] {
  const result = [...new Set((values ?? []).map((value) => boundedString(value, 128, label)))]
  for (const value of result) {
    if (!/^[a-z][a-z0-9_]*$/.test(value)) throw new TypeError(`${label} contains invalid name: ${value}`)
  }
  return result.sort()
}

function templateParameters(template: string): string[] {
  const result: string[] = []
  const pattern = /\{([a-z][a-z0-9_]*)\}/g
  let match: RegExpExecArray | null
  while ((match = pattern.exec(template))) result.push(match[1]!)
  return [...new Set(result)].sort()
}

function normalizeStatuses(values: readonly number[]): number[] {
  const result = [...new Set(values.map(Number))].sort((left, right) => left - right)
  if (!result.length || result.some((value) => !Number.isInteger(value) || value < 100 || value > 599)) {
    throw new TypeError("Endpoint expected statuses must be valid HTTP statuses")
  }
  return result
}

export function normalizeEndpoint(definition: EndpointDefinition): EndpointDefinition {
  const operation = boundedString(definition.operation, MAX_OPERATION_NAME_BYTES, "endpoint operation")
  const contract = boundedString(definition.contract, 128, "endpoint contract")
  const method = normalizeHttpMethod(definition.method)
  const pathTemplate = normalizePath(definition.pathTemplate)
  const discovered = templateParameters(pathTemplate)
  const declared = normalizeParameterNames(definition.pathParameters, "path parameter")
  if (JSON.stringify(discovered) !== JSON.stringify(declared)) {
    throw new TypeError(
      `Endpoint ${operation} path parameters do not match template: ${declared.join(",")} != ${discovered.join(",")}`,
    )
  }
  if (definition.kind === "query" && method !== "GET" && method !== "HEAD") {
    throw new TypeError(`Query endpoint ${operation} must use GET or HEAD`)
  }
  if (definition.kind === "mutation" && method === "GET") {
    throw new TypeError(`Mutation endpoint ${operation} may not use GET`)
  }
  if (definition.receipt === "required" && definition.kind !== "mutation") {
    throw new TypeError(`Receipt requirement is only valid for mutations: ${operation}`)
  }
  return {
    operation,
    contract,
    method,
    pathTemplate,
    kind: definition.kind,
    receipt: definition.receipt,
    auth: definition.auth,
    expectedStatuses: normalizeStatuses(definition.expectedStatuses),
    pathParameters: declared,
    queryParameters: normalizeParameterNames(definition.queryParameters, "query parameter"),
  }
}

export function resolveEndpoint(
  definition: EndpointDefinition,
  options: {
    path?: Record<string, unknown>
    query?: QueryRecord
  } = {},
): ResolvedEndpoint {
  const normalized = normalizeEndpoint(definition)
  const supplied = options.path ?? {}
  const unknownPath = Object.keys(supplied).filter((key) => !normalized.pathParameters.includes(key))
  if (unknownPath.length) {
    throw new RequestValidationError(`Unknown path parameters for ${normalized.operation}.`, {
      unknown_path_parameters: unknownPath,
    })
  }
  let path = normalized.pathTemplate
  for (const parameter of normalized.pathParameters) {
    const value = supplied[parameter]
    if (value === undefined || value === null || value === "") {
      throw new RequestValidationError(`Missing path parameter ${parameter} for ${normalized.operation}.`)
    }
    path = path.replace(`{${parameter}}`, encodePathSegment(value, parameter))
  }
  if (/\{[^}]+\}/.test(path)) {
    throw new RequestValidationError(`Unresolved path parameters for ${normalized.operation}.`, { path })
  }
  const query = { ...(options.query ?? {}) }
  const unknownQuery = Object.keys(query).filter((key) => !normalized.queryParameters.includes(key))
  if (unknownQuery.length) {
    throw new RequestValidationError(`Unknown query parameters for ${normalized.operation}.`, {
      unknown_query_parameters: unknownQuery,
    })
  }
  return {
    operation: normalized.operation,
    contract: normalized.contract,
    method: normalized.method,
    path,
    kind: normalized.kind,
    receipt: normalized.receipt,
    auth: normalized.auth,
    expectedStatuses: [...normalized.expectedStatuses],
    query,
  }
}

export const CORE_ENDPOINTS = {
  health: normalizeEndpoint({
    operation: OPERATION_NAMES.health,
    contract: CONTRACT_NAMES.health,
    method: "GET",
    pathTemplate: "/health",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: [],
    queryParameters: [],
  }),
  readiness: normalizeEndpoint({
    operation: OPERATION_NAMES.readiness,
    contract: CONTRACT_NAMES.readiness,
    method: "GET",
    pathTemplate: "/runtime/readiness",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200, 503],
    pathParameters: [],
    queryParameters: ["wait_ms"],
  }),
  providerModels: normalizeEndpoint({
    operation: OPERATION_NAMES.providerModels,
    contract: CONTRACT_NAMES.providerBackend,
    method: "GET",
    pathTemplate: "/providers/models",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200, 409],
    pathParameters: [],
    queryParameters: ["available_only", "provider_id"],
  }),
  taskList: normalizeEndpoint({
    operation: OPERATION_NAMES.taskList,
    contract: CONTRACT_NAMES.taskList,
    method: "GET",
    pathTemplate: "/tasks",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: [],
    queryParameters: ["cursor", "limit", "status"],
  }),
  taskGet: normalizeEndpoint({
    operation: OPERATION_NAMES.taskGet,
    contract: CONTRACT_NAMES.taskDetail,
    method: "GET",
    pathTemplate: "/tasks/{task_id}",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id"],
    queryParameters: [],
  }),
  sessionList: normalizeEndpoint({
    operation: OPERATION_NAMES.sessionList,
    contract: CONTRACT_NAMES.sessionList,
    method: "GET",
    pathTemplate: "/sessions",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: [],
    queryParameters: ["cursor", "limit", "status"],
  }),
  sessionGet: normalizeEndpoint({
    operation: OPERATION_NAMES.sessionGet,
    contract: CONTRACT_NAMES.sessionDetail,
    method: "GET",
    pathTemplate: "/sessions/{session_id}",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["session_id"],
    queryParameters: [],
  }),
  sessionDelete: normalizeEndpoint({
    operation: OPERATION_NAMES.sessionDelete,
    contract: CONTRACT_NAMES.sessionDeletion,
    method: "POST",
    pathTemplate: "/sessions/{session_id}/delete",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["session_id"],
    queryParameters: [],
  }),
  taskEvents: normalizeEndpoint({
    operation: OPERATION_NAMES.taskEvents,
    contract: CONTRACT_NAMES.taskEvents,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/events",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id"],
    queryParameters: ["after", "cursor", "limit"],
  }),
  taskEventIngressCapabilities: normalizeEndpoint({
    operation: OPERATION_NAMES.taskEventIngressCapabilities,
    contract: CONTRACT_NAMES.taskEventIngressCapabilities,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/event-ingress/capabilities",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id"],
    queryParameters: [
      "event_types",
      "intent",
      "correlation_id",
      "artifact_id",
      "generation",
      "cursor",
    ],
  }),
  taskEventIngressSnapshot: normalizeEndpoint({
    operation: OPERATION_NAMES.taskEventIngressSnapshot,
    contract: CONTRACT_NAMES.taskEventIngressSnapshot,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/event-ingress/snapshot",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id"],
    queryParameters: [
      "cursor",
      "generation",
      "limit",
      "event_types",
      "intent",
      "correlation_id",
      "artifact_id",
    ],
  }),
  taskEventIngressDelta: normalizeEndpoint({
    operation: OPERATION_NAMES.taskEventIngressDelta,
    contract: CONTRACT_NAMES.taskEventIngressDelta,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/event-ingress/delta",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id"],
    queryParameters: [
      "cursor",
      "generation",
      "limit",
      "wait_ms",
      "event_types",
      "intent",
      "correlation_id",
      "artifact_id",
    ],
  }),
  taskEventIngressSse: normalizeEndpoint({
    operation: OPERATION_NAMES.taskEventIngressSse,
    contract: CONTRACT_NAMES.taskEventIngressSse,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/event-ingress/sse",
    kind: "stream",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id"],
    queryParameters: [
      "cursor",
      "generation",
      "limit",
      "wait_ms",
      "stream_ms",
      "heartbeat_ms",
      "event_types",
      "intent",
      "correlation_id",
      "artifact_id",
    ],
  }),
  taskArtifactCatalog: normalizeEndpoint({
    operation: OPERATION_NAMES.taskArtifactCatalog,
    contract: CONTRACT_NAMES.taskArtifactCatalog,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/artifacts",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id"],
    queryParameters: [
      "cursor",
      "limit",
      "node_id",
      "worker_id",
      "media_type",
      "content_family",
      "revision",
      "created_after",
      "created_before",
      "include_deleted",
    ],
  }),
  taskArtifactMetadata: normalizeEndpoint({
    operation: OPERATION_NAMES.taskArtifactMetadata,
    contract: CONTRACT_NAMES.taskArtifactMetadata,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/artifacts/{artifact_id}",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id", "artifact_id"],
    queryParameters: ["revision", "purpose"],
  }),
  taskArtifactContent: normalizeEndpoint({
    operation: OPERATION_NAMES.taskArtifactContent,
    contract: CONTRACT_NAMES.taskArtifactContent,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/artifacts/{artifact_id}/content",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id", "artifact_id"],
    queryParameters: ["revision", "offset", "length", "purpose"],
  }),
  taskArtifactReceipts: normalizeEndpoint({
    operation: OPERATION_NAMES.taskArtifactReceipts,
    contract: CONTRACT_NAMES.taskArtifactReceipts,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/artifacts/{artifact_id}/receipts",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id", "artifact_id"],
    queryParameters: [],
  }),
  taskDiffReviewManifest: normalizeEndpoint({
    operation: OPERATION_NAMES.taskDiffReviewManifest,
    contract: CONTRACT_NAMES.taskDiffReviewManifest,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/diff-reviews/{artifact_id}",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id", "artifact_id"],
    queryParameters: ["revision"],
  }),
  taskDiffReviewPage: normalizeEndpoint({
    operation: OPERATION_NAMES.taskDiffReviewPage,
    contract: CONTRACT_NAMES.taskDiffReviewPage,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/diff-reviews/{artifact_id}/files/{file_id}/hunks",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id", "artifact_id", "file_id"],
    queryParameters: ["revision", "page", "maximum_bytes", "maximum_lines"],
  }),
  taskDiffReviewFileContent: normalizeEndpoint({
    operation: OPERATION_NAMES.taskDiffReviewFileContent,
    contract: CONTRACT_NAMES.taskDiffReviewFileContent,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/diff-reviews/{artifact_id}/files/{file_id}/content",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id", "artifact_id", "file_id"],
    queryParameters: ["revision", "version"],
  }),
  taskDiffReviewComment: normalizeEndpoint({
    operation: OPERATION_NAMES.taskDiffReviewComment,
    contract: CONTRACT_NAMES.taskDiffReviewComment,
    method: "POST",
    pathTemplate: "/tasks/{task_id}/diff-reviews/{artifact_id}/comments",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [201, 202, 403],
    pathParameters: ["task_id", "artifact_id"],
    queryParameters: [],
  }),
  taskDiffReviewApply: normalizeEndpoint({
    operation: OPERATION_NAMES.taskDiffReviewApply,
    contract: CONTRACT_NAMES.taskDiffReviewTransaction,
    method: "POST",
    pathTemplate: "/tasks/{task_id}/diff-reviews/{artifact_id}/apply",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 201, 202, 403, 409],
    pathParameters: ["task_id", "artifact_id"],
    queryParameters: [],
  }),
  taskDiffReviewRollback: normalizeEndpoint({
    operation: OPERATION_NAMES.taskDiffReviewRollback,
    contract: CONTRACT_NAMES.taskDiffReviewTransaction,
    method: "POST",
    pathTemplate: "/tasks/{task_id}/diff-reviews/transactions/{transaction_id}/rollback",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 201, 202, 403, 409],
    pathParameters: ["task_id", "transaction_id"],
    queryParameters: [],
  }),
  taskTerminalList: normalizeEndpoint({
    operation: OPERATION_NAMES.taskTerminalList,
    contract: CONTRACT_NAMES.taskTerminalList,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/terminals",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id"],
    queryParameters: ["include_closed"],
  }),
  taskTerminalGet: normalizeEndpoint({
    operation: OPERATION_NAMES.taskTerminalGet,
    contract: CONTRACT_NAMES.taskTerminalSession,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/terminals/{terminal_id}",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id", "terminal_id"],
    queryParameters: [],
  }),
  taskTerminalCreate: normalizeEndpoint({
    operation: OPERATION_NAMES.taskTerminalCreate,
    contract: CONTRACT_NAMES.taskTerminalReceipt,
    method: "POST",
    pathTemplate: "/tasks/{task_id}/terminals",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [201, 202, 400, 403, 404, 409, 413, 426, 503],
    pathParameters: ["task_id"],
    queryParameters: [],
  }),
  taskTerminalTicket: normalizeEndpoint({
    operation: OPERATION_NAMES.taskTerminalTicket,
    contract: CONTRACT_NAMES.taskTerminalReceipt,
    method: "POST",
    pathTemplate: "/tasks/{task_id}/terminals/{terminal_id}/ticket",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 400, 403, 409, 410, 426],
    pathParameters: ["task_id", "terminal_id"],
    queryParameters: [],
  }),
  taskTerminalKill: normalizeEndpoint({
    operation: OPERATION_NAMES.taskTerminalKill,
    contract: CONTRACT_NAMES.taskTerminalReceipt,
    method: "POST",
    pathTemplate: "/tasks/{task_id}/terminals/{terminal_id}/kill",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 202, 400, 403, 409, 410, 503],
    pathParameters: ["task_id", "terminal_id"],
    queryParameters: [],
  }),
  taskBrowserObservability: normalizeEndpoint({
    operation: OPERATION_NAMES.taskBrowserObservability,
    contract: CONTRACT_NAMES.taskBrowserObservability,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/browser-observability",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id"],
    queryParameters: [
      "view",
      "browser_session_id",
      "worker_request_id",
      "limit",
      "after_sequence",
    ],
  }),
  taskBrowserControl: normalizeEndpoint({
    operation: OPERATION_NAMES.taskBrowserControl,
    contract: CONTRACT_NAMES.taskBrowserControl,
    method: "POST",
    pathTemplate: "/tasks/{task_id}/workers/browser",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 201, 202, 400, 403, 404, 408, 409, 503],
    pathParameters: ["task_id"],
    queryParameters: [],
  }),
  workspaceFiles: normalizeEndpoint({
    operation: OPERATION_NAMES.workspaceFiles,
    contract: CONTRACT_NAMES.workspaceFiles,
    method: "GET",
    pathTemplate: "/workspaces/{workspace_id}/files",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["workspace_id"],
    queryParameters: ["encoding", "length", "limit", "mode", "mount", "path", "read", "start"],
  }),
  workspaceFileWrite: normalizeEndpoint({
    operation: OPERATION_NAMES.workspaceFileWrite,
    contract: CONTRACT_NAMES.workspaceFiles,
    method: "POST",
    pathTemplate: "/workspaces/{workspace_id}/files",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 201],
    pathParameters: ["workspace_id"],
    queryParameters: [],
  }),
  taskCreate: normalizeEndpoint({
    operation: OPERATION_NAMES.taskCreate,
    contract: CONTRACT_NAMES.taskMutation,
    method: "POST",
    pathTemplate: "/tasks",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [201],
    pathParameters: [],
    queryParameters: [],
  }),
  taskCancel: normalizeEndpoint({
    operation: OPERATION_NAMES.taskCancel,
    contract: CONTRACT_NAMES.taskMutation,
    method: "POST",
    pathTemplate: "/tasks/{task_id}/cancel",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id"],
    queryParameters: [],
  }),
  taskResume: normalizeEndpoint({
    operation: OPERATION_NAMES.taskResume,
    contract: CONTRACT_NAMES.taskMutation,
    method: "POST",
    pathTemplate: "/tasks/{task_id}/run",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id"],
    queryParameters: [],
  }),
  taskControlCommand: normalizeEndpoint({
    operation: OPERATION_NAMES.taskControlCommand,
    contract: CONTRACT_NAMES.taskControlCommand,
    method: "POST",
    pathTemplate: "/tasks/{task_id}/commands",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 201, 202, 403, 409],
    pathParameters: ["task_id"],
    queryParameters: [],
  }),
  taskControlCommandReceipt: normalizeEndpoint({
    operation: OPERATION_NAMES.taskControlCommandReceipt,
    contract: CONTRACT_NAMES.taskControlCommand,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/commands/{request_id}",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200, 202],
    pathParameters: ["request_id", "task_id"],
    queryParameters: [],
  }),
  taskLoopxState: normalizeEndpoint({
    operation: OPERATION_NAMES.taskLoopxState,
    contract: CONTRACT_NAMES.taskLoopxState,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/loopx",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200, 503],
    pathParameters: ["task_id"],
    queryParameters: ["goal_id"],
  }),
  taskLoopxCommand: normalizeEndpoint({
    operation: OPERATION_NAMES.taskLoopxCommand,
    contract: CONTRACT_NAMES.taskLoopxCommand,
    method: "POST",
    pathTemplate: "/tasks/{task_id}/loopx/commands",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 201, 202, 409, 503],
    pathParameters: ["task_id"],
    queryParameters: [],
  }),
  taskCommandQueue: normalizeEndpoint({
    operation: OPERATION_NAMES.taskCommandQueue,
    contract: CONTRACT_NAMES.taskCommandQueue,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/command-queue",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id"],
    queryParameters: ["session_id", "include_terminal"],
  }),
  taskCommandCancel: normalizeEndpoint({
    operation: OPERATION_NAMES.taskCommandCancel,
    contract: CONTRACT_NAMES.taskCommandCancel,
    method: "POST",
    pathTemplate: "/tasks/{task_id}/commands/{request_id}/cancel",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 409],
    pathParameters: ["task_id", "request_id"],
    queryParameters: [],
  }),
  taskUserInputRequests: normalizeEndpoint({
    operation: OPERATION_NAMES.taskUserInputRequests,
    contract: CONTRACT_NAMES.taskUserInput,
    method: "GET",
    pathTemplate: "/tasks/{task_id}/user-input",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["task_id"],
    queryParameters: ["include_terminal"],
  }),
  taskUserInputAnswer: normalizeEndpoint({
    operation: OPERATION_NAMES.taskUserInputAnswer,
    contract: CONTRACT_NAMES.taskUserInput,
    method: "POST",
    pathTemplate: "/tasks/{task_id}/user-input/{request_id}/answer",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 400, 404, 409],
    pathParameters: ["task_id", "request_id"],
    queryParameters: [],
  }),
  permissionSessionOpen: normalizeEndpoint({
    operation: OPERATION_NAMES.permissionSessionOpen,
    contract: CONTRACT_NAMES.permissionControl,
    method: "POST",
    pathTemplate: "/permissions/sessions/open",
    kind: "mutation",
    receipt: "required",
    auth: "required",
    expectedStatuses: [200, 201, 400, 401, 403, 404, 409],
    pathParameters: [],
    queryParameters: [],
  }),
  permissionSessionResume: normalizeEndpoint({
    operation: OPERATION_NAMES.permissionSessionResume,
    contract: CONTRACT_NAMES.permissionControl,
    method: "POST",
    pathTemplate: "/permissions/sessions/{session_id}/resume",
    kind: "mutation",
    receipt: "required",
    auth: "required",
    expectedStatuses: [200, 400, 401, 403, 404, 409],
    pathParameters: ["session_id"],
    queryParameters: [],
  }),
  permissionSummary: normalizeEndpoint({
    operation: OPERATION_NAMES.permissionSummary,
    contract: CONTRACT_NAMES.permissionControl,
    method: "GET",
    pathTemplate: "/permissions",
    kind: "query",
    receipt: "none",
    auth: "required",
    expectedStatuses: [200, 400, 401, 403, 404, 409],
    pathParameters: [],
    queryParameters: ["session_id", "run_id", "task_id", "limit"],
  }),
  permissionRequests: normalizeEndpoint({
    operation: OPERATION_NAMES.permissionRequests,
    contract: CONTRACT_NAMES.permissionControl,
    method: "GET",
    pathTemplate: "/permissions/requests",
    kind: "query",
    receipt: "none",
    auth: "required",
    expectedStatuses: [200, 400, 401, 403, 404, 409],
    pathParameters: [],
    queryParameters: [
      "session_id",
      "run_id",
      "task_id",
      "status",
      "pending_only",
      "limit",
    ],
  }),
  permissionRequestGet: normalizeEndpoint({
    operation: OPERATION_NAMES.permissionRequestGet,
    contract: CONTRACT_NAMES.permissionControl,
    method: "GET",
    pathTemplate: "/permissions/requests/{request_id}",
    kind: "query",
    receipt: "none",
    auth: "required",
    expectedStatuses: [200, 400, 401, 403, 404, 409],
    pathParameters: ["request_id"],
    queryParameters: ["session_id", "run_id", "task_id"],
  }),
  permissionRequestResolve: normalizeEndpoint({
    operation: OPERATION_NAMES.permissionRequestResolve,
    contract: CONTRACT_NAMES.permissionControl,
    method: "POST",
    pathTemplate: "/permissions/requests/{request_id}/resolve",
    kind: "mutation",
    receipt: "required",
    auth: "required",
    expectedStatuses: [200, 400, 401, 403, 404, 408, 409, 410, 422],
    pathParameters: ["request_id"],
    queryParameters: [],
  }),
  permissionRequestsExpire: normalizeEndpoint({
    operation: OPERATION_NAMES.permissionRequestsExpire,
    contract: CONTRACT_NAMES.permissionControl,
    method: "POST",
    pathTemplate: "/permissions/requests/expire",
    kind: "mutation",
    receipt: "required",
    auth: "required",
    expectedStatuses: [200, 400, 401, 403, 404, 409],
    pathParameters: [],
    queryParameters: [],
  }),
  permissionRules: normalizeEndpoint({
    operation: OPERATION_NAMES.permissionRules,
    contract: CONTRACT_NAMES.permissionControl,
    method: "GET",
    pathTemplate: "/permissions/rules",
    kind: "query",
    receipt: "none",
    auth: "required",
    expectedStatuses: [200, 400, 401, 403, 404, 409],
    pathParameters: [],
    queryParameters: ["session_id", "run_id", "task_id"],
  }),
  permissionMode: normalizeEndpoint({
    operation: OPERATION_NAMES.permissionMode,
    contract: CONTRACT_NAMES.permissionControl,
    method: "GET",
    pathTemplate: "/permissions/mode",
    kind: "query",
    receipt: "none",
    auth: "required",
    expectedStatuses: [200, 400, 401, 403, 404, 409],
    pathParameters: [],
    queryParameters: ["session_id", "run_id", "task_id"],
  }),
  permissionModeUpdate: normalizeEndpoint({
    operation: OPERATION_NAMES.permissionModeUpdate,
    contract: CONTRACT_NAMES.permissionControl,
    method: "POST",
    pathTemplate: "/permissions/mode",
    kind: "mutation",
    receipt: "required",
    auth: "required",
    expectedStatuses: [200, 400, 401, 403, 404, 409],
    pathParameters: [],
    queryParameters: [],
  }),
  permissionDecisions: normalizeEndpoint({
    operation: OPERATION_NAMES.permissionDecisions,
    contract: CONTRACT_NAMES.permissionControl,
    method: "GET",
    pathTemplate: "/permissions/decisions",
    kind: "query",
    receipt: "none",
    auth: "required",
    expectedStatuses: [200, 400, 401, 403, 404, 409],
    pathParameters: [],
    queryParameters: ["session_id", "run_id", "task_id", "limit"],
  }),
  scenarioRegistry: normalizeEndpoint({
    operation: OPERATION_NAMES.scenarioRegistry,
    contract: CONTRACT_NAMES.scenarioRegistry,
    method: "GET",
    pathTemplate: "/scenarios/registry",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: [],
    queryParameters: [],
  }),
  scenarioRunList: normalizeEndpoint({
    operation: OPERATION_NAMES.scenarioRunList,
    contract: CONTRACT_NAMES.scenarioRun,
    method: "GET",
    pathTemplate: "/scenarios/runs",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: [],
    queryParameters: ["include_archived", "limit", "offset"],
  }),
  scenarioRunGet: normalizeEndpoint({
    operation: OPERATION_NAMES.scenarioRunGet,
    contract: CONTRACT_NAMES.scenarioRun,
    method: "GET",
    pathTemplate: "/scenarios/runs/{scenario_run_id}",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: ["scenario_run_id"],
    queryParameters: [],
  }),
  scenarioRunEvidence: normalizeEndpoint({
    operation: OPERATION_NAMES.scenarioRunEvidence,
    contract: CONTRACT_NAMES.scenarioEvidence,
    method: "GET",
    pathTemplate: "/scenarios/runs/{scenario_run_id}/evidence",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200, 409],
    pathParameters: ["scenario_run_id"],
    queryParameters: [],
  }),
  scenarioRunCreate: normalizeEndpoint({
    operation: OPERATION_NAMES.scenarioRunCreate,
    contract: CONTRACT_NAMES.scenarioMutation,
    method: "POST",
    pathTemplate: "/scenarios/runs",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [201, 409, 422, 503],
    pathParameters: [],
    queryParameters: [],
  }),
  scenarioRunStart: normalizeEndpoint({
    operation: OPERATION_NAMES.scenarioRunStart,
    contract: CONTRACT_NAMES.scenarioMutation,
    method: "POST",
    pathTemplate: "/scenarios/runs/{scenario_run_id}/start",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 202, 409, 422, 503],
    pathParameters: ["scenario_run_id"],
    queryParameters: [],
  }),
  scenarioRunCancel: normalizeEndpoint({
    operation: OPERATION_NAMES.scenarioRunCancel,
    contract: CONTRACT_NAMES.scenarioMutation,
    method: "POST",
    pathTemplate: "/scenarios/runs/{scenario_run_id}/cancel",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 409, 422, 503],
    pathParameters: ["scenario_run_id"],
    queryParameters: [],
  }),
  scenarioRunArchive: normalizeEndpoint({
    operation: OPERATION_NAMES.scenarioRunArchive,
    contract: CONTRACT_NAMES.scenarioMutation,
    method: "POST",
    pathTemplate: "/scenarios/runs/{scenario_run_id}/archive",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 409, 422],
    pathParameters: ["scenario_run_id"],
    queryParameters: [],
  }),
  scenarioRunVerify: normalizeEndpoint({
    operation: OPERATION_NAMES.scenarioRunVerify,
    contract: CONTRACT_NAMES.scenarioEvidence,
    method: "POST",
    pathTemplate: "/scenarios/runs/{scenario_run_id}/verify",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 409, 503],
    pathParameters: ["scenario_run_id"],
    queryParameters: [],
  }),
  experimentRegistry: normalizeEndpoint({
    operation: OPERATION_NAMES.experimentRegistry,
    contract: CONTRACT_NAMES.experimentRegistry,
    method: "GET",
    pathTemplate: "/experiments/registry",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: [],
    queryParameters: [],
  }),
  experimentRunList: normalizeEndpoint({
    operation: OPERATION_NAMES.experimentRunList,
    contract: CONTRACT_NAMES.experimentRun,
    method: "GET",
    pathTemplate: "/experiments/runs",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: [],
    queryParameters: ["include_archived", "limit", "offset"],
  }),
  experimentRunGet: normalizeEndpoint({
    operation: OPERATION_NAMES.experimentRunGet,
    contract: CONTRACT_NAMES.experimentRun,
    method: "GET",
    pathTemplate: "/experiments/runs/{experiment_id}",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200, 404],
    pathParameters: ["experiment_id"],
    queryParameters: [],
  }),
  experimentRunReport: normalizeEndpoint({
    operation: OPERATION_NAMES.experimentRunReport,
    contract: CONTRACT_NAMES.experimentReport,
    method: "GET",
    pathTemplate: "/experiments/runs/{experiment_id}/report",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200, 404, 409],
    pathParameters: ["experiment_id"],
    queryParameters: [],
  }),
  experimentRunSamples: normalizeEndpoint({
    operation: OPERATION_NAMES.experimentRunSamples,
    contract: CONTRACT_NAMES.experimentSamples,
    method: "GET",
    pathTemplate: "/experiments/runs/{experiment_id}/samples",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200, 404],
    pathParameters: ["experiment_id"],
    queryParameters: [
      "metric",
      "variant_id",
      "cell_id",
      "status",
      "limit",
      "offset",
    ],
  }),
  experimentRunBundle: normalizeEndpoint({
    operation: OPERATION_NAMES.experimentRunBundle,
    contract: CONTRACT_NAMES.experimentBundle,
    method: "GET",
    pathTemplate: "/experiments/runs/{experiment_id}/bundle",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200, 404, 409, 503],
    pathParameters: ["experiment_id"],
    queryParameters: [],
  }),
  experimentRunSource: normalizeEndpoint({
    operation: OPERATION_NAMES.experimentRunSource,
    contract: CONTRACT_NAMES.experimentSource,
    method: "GET",
    pathTemplate: "/experiments/runs/{experiment_id}/source",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200, 404],
    pathParameters: ["experiment_id"],
    queryParameters: [],
  }),
  experimentRunRequirements: normalizeEndpoint({
    operation: OPERATION_NAMES.experimentRunRequirements,
    contract: CONTRACT_NAMES.experimentRequirements,
    method: "GET",
    pathTemplate: "/experiments/runs/{experiment_id}/requirements",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200, 404, 409],
    pathParameters: ["experiment_id"],
    queryParameters: [],
  }),
  experimentRunCreate: normalizeEndpoint({
    operation: OPERATION_NAMES.experimentRunCreate,
    contract: CONTRACT_NAMES.experimentMutation,
    method: "POST",
    pathTemplate: "/experiments/runs",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [201, 409, 422, 503],
    pathParameters: [],
    queryParameters: [],
  }),
  experimentRunStart: normalizeEndpoint({
    operation: OPERATION_NAMES.experimentRunStart,
    contract: CONTRACT_NAMES.experimentMutation,
    method: "POST",
    pathTemplate: "/experiments/runs/{experiment_id}/start",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 202, 404, 409, 422, 503],
    pathParameters: ["experiment_id"],
    queryParameters: [],
  }),
  experimentRunCancel: normalizeEndpoint({
    operation: OPERATION_NAMES.experimentRunCancel,
    contract: CONTRACT_NAMES.experimentMutation,
    method: "POST",
    pathTemplate: "/experiments/runs/{experiment_id}/cancel",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 404, 409, 422],
    pathParameters: ["experiment_id"],
    queryParameters: [],
  }),
  experimentRunArchive: normalizeEndpoint({
    operation: OPERATION_NAMES.experimentRunArchive,
    contract: CONTRACT_NAMES.experimentMutation,
    method: "POST",
    pathTemplate: "/experiments/runs/{experiment_id}/archive",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 404, 409, 422],
    pathParameters: ["experiment_id"],
    queryParameters: [],
  }),
  experimentRunVerify: normalizeEndpoint({
    operation: OPERATION_NAMES.experimentRunVerify,
    contract: CONTRACT_NAMES.experimentMutation,
    method: "POST",
    pathTemplate: "/experiments/runs/{experiment_id}/verify",
    kind: "mutation",
    receipt: "required",
    auth: "optional",
    expectedStatuses: [200, 404, 409, 503],
    pathParameters: ["experiment_id"],
    queryParameters: [],
  }),
  policyEvidence: normalizeEndpoint({
    operation: OPERATION_NAMES.policyEvidence,
    contract: CONTRACT_NAMES.policyEvidence,
    method: "GET",
    pathTemplate: "/policy/evidence",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200, 400, 403, 409, 503],
    pathParameters: [],
    queryParameters: [
      "run_id",
      "task_id",
      "mechanism_version",
      "receipt_id",
      "report_id",
      "cursor",
      "limit",
    ],
  }),
  policyMetricSpecs: normalizeEndpoint({
    operation: OPERATION_NAMES.policyMetricSpecs,
    contract: CONTRACT_NAMES.policyMetricSpecs,
    method: "GET",
    pathTemplate: "/policy/metrics/specs",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200],
    pathParameters: [],
    queryParameters: [],
  }),
  policyMetricReport: normalizeEndpoint({
    operation: OPERATION_NAMES.policyMetricReport,
    contract: CONTRACT_NAMES.policyMetricReport,
    method: "GET",
    pathTemplate: "/policy/metrics/reports/{report_id}",
    kind: "query",
    receipt: "none",
    auth: "optional",
    expectedStatuses: [200, 404, 409],
    pathParameters: ["report_id"],
    queryParameters: [],
  }),
} as const

export type CoreEndpointName = keyof typeof CORE_ENDPOINTS

export class ProtocolCatalog {
  readonly #definitions = new Map<string, EndpointDefinition>()
  readonly #byMethodPath = new Map<string, string>()
  #frozen = false

  constructor(seed: readonly EndpointDefinition[] = Object.values(CORE_ENDPOINTS)) {
    for (const definition of seed) this.register(definition)
  }

  register(definition: EndpointDefinition, options: { replace?: boolean } = {}): void {
    if (this.#frozen) throw new TypeError("Protocol catalog is frozen")
    const normalized = normalizeEndpoint(definition)
    const existing = this.#definitions.get(normalized.operation)
    if (existing && !options.replace) throw new TypeError(`Endpoint already registered: ${normalized.operation}`)
    const routeKey = `${normalized.method} ${normalized.pathTemplate}`
    const routeOwner = this.#byMethodPath.get(routeKey)
    if (routeOwner && routeOwner !== normalized.operation && !options.replace) {
      throw new TypeError(`Endpoint route already owned by ${routeOwner}: ${routeKey}`)
    }
    if (existing) this.#byMethodPath.delete(`${existing.method} ${existing.pathTemplate}`)
    this.#definitions.set(normalized.operation, normalized)
    this.#byMethodPath.set(routeKey, normalized.operation)
  }

  unregister(operation: string): boolean {
    if (this.#frozen) throw new TypeError("Protocol catalog is frozen")
    const existing = this.#definitions.get(operation)
    if (!existing) return false
    this.#definitions.delete(operation)
    this.#byMethodPath.delete(`${existing.method} ${existing.pathTemplate}`)
    return true
  }

  get(operation: string): EndpointDefinition | undefined {
    const value = this.#definitions.get(operation)
    return value ? cloneEndpoint(value) : undefined
  }

  require(operation: string): EndpointDefinition {
    const value = this.get(operation)
    if (!value) throw new RequestValidationError(`Unknown Zyra API operation: ${operation}`)
    return value
  }

  resolve(
    operation: string,
    options: { path?: Record<string, unknown>; query?: QueryRecord } = {},
  ): ResolvedEndpoint {
    return resolveEndpoint(this.require(operation), options)
  }

  freeze(): void {
    this.#frozen = true
  }

  list(): EndpointDefinition[] {
    return [...this.#definitions.values()]
      .map(cloneEndpoint)
      .sort((left, right) => left.operation.localeCompare(right.operation))
  }

  operations(): string[] {
    return [...this.#definitions.keys()].sort()
  }

  contracts(): string[] {
    return [...new Set([...this.#definitions.values()].map((value) => value.contract))].sort()
  }

  assertComplete(): void {
    const required = Object.values(OPERATION_NAMES)
    const missing = required.filter((operation) => !this.#definitions.has(operation))
    if (missing.length) throw new TypeError(`Protocol catalog is incomplete: ${missing.join(", ")}`)
    const mutationWithoutReceipt = [...this.#definitions.values()]
      .filter((definition) => definition.kind === "mutation" && definition.receipt !== "required")
      .map((definition) => definition.operation)
    if (mutationWithoutReceipt.length) {
      throw new TypeError(`Mutations without receipt contract: ${mutationWithoutReceipt.join(", ")}`)
    }
  }

  get frozen(): boolean {
    return this.#frozen
  }
}

function cloneEndpoint(definition: EndpointDefinition): EndpointDefinition {
  return {
    ...definition,
    expectedStatuses: [...definition.expectedStatuses],
    pathParameters: [...definition.pathParameters],
    queryParameters: [...definition.queryParameters],
  }
}

export function coreProtocolCatalog(): ProtocolCatalog {
  const catalog = new ProtocolCatalog()
  catalog.assertComplete()
  catalog.freeze()
  return catalog
}

export function protocolManifest(catalog: ProtocolCatalog): Record<string, unknown> {
  return {
    version: 1,
    operations: catalog.list().map((definition) => ({
      operation: definition.operation,
      contract: definition.contract,
      method: definition.method,
      path_template: definition.pathTemplate,
      kind: definition.kind,
      receipt: definition.receipt,
      auth: definition.auth,
      expected_statuses: [...definition.expectedStatuses],
      path_parameters: [...definition.pathParameters],
      query_parameters: [...definition.queryParameters],
    })),
  }
}
