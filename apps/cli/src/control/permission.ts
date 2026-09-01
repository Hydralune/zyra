import { createHash } from "node:crypto"
import type { TaskProjection } from "@zyra/typed-api-client"
import {
  CliApi,
  type PermissionBinding,
  type PermissionSessionClaim,
} from "../api.ts"
import { CliTaskError } from "../contracts.ts"

const PERMISSION_RESPONSE_VERSION = "zyra.permission-response/v2"
const LEGACY_PERMISSION_RESPONSE_VERSION = "zyra.permission-response/v1"
const PERMISSION_OWNER = "typescript.PermissionCoordinator"
export type PermissionDecisionScope = "once" | "session" | "workspace"

export interface PermissionRequestView {
  requestId: string
  sessionId: string
  status: string
  prompt: string
  reason: string
  toolName: string
  operation: string
  expiresAt: string
  selectable: boolean
  supportedDecisionScopes: readonly PermissionDecisionScope[]
  raw: Readonly<Record<string, unknown>>
}

function object(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new CliTaskError(`${label} must be an object.`, "permission_contract_invalid")
  }
  return value as Record<string, unknown>
}

function text(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim() || /[\u0000\r\n]/.test(value)) {
    throw new CliTaskError(`${label} must be a bounded non-empty string.`, "permission_contract_invalid")
  }
  return value.trim()
}

function integer(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) {
    throw new CliTaskError(`${label} must be a non-negative safe integer.`, "permission_contract_invalid")
  }
  return Number(value)
}

function sortedJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(sortedJson).join(",")}]`
  if (value && typeof value === "object") {
    const selected = value as Record<string, unknown>
    return `{${Object.keys(selected).sort().map((key) => `${JSON.stringify(key)}:${sortedJson(selected[key])}`).join(",")}}`
  }
  return JSON.stringify(value)
}

function sha256(value: unknown): string {
  return createHash("sha256").update(sortedJson(value), "utf8").digest("hex")
}

function permissionItems(value: Readonly<Record<string, unknown>>): Record<string, unknown>[] {
  const requests = object(value.requests ?? {}, "permission requests")
  const items = Array.isArray(requests.items) ? requests.items : []
  return items.map((item) => object(item, "permission request"))
}

function decisionScopes(value: unknown): readonly PermissionDecisionScope[] {
  if (!Array.isArray(value)) return Object.freeze(["once"])
  const scopes = value.filter((item): item is PermissionDecisionScope => (
    item === "once" || item === "session" || item === "workspace"
  ))
  return Object.freeze(scopes.includes("once") ? [...new Set(scopes)] : ["once"])
}

export function projectPermissionRequests(
  value: Readonly<Record<string, unknown>>,
  now = new Date(),
): readonly PermissionRequestView[] {
  return Object.freeze(permissionItems(value).map((item) => {
    const requestId = text(item.request_id, "permission request id")
    const sessionId = text(item.session_id, "permission session id")
    const expiresAt = text(item.expires_at, "permission request expiry")
    const status = typeof item.status === "string" ? item.status.trim().toLowerCase() : "unknown"
    const terminal = ["resolved", "denied", "allowed", "cancelled", "expired", "aborted"].includes(status)
      || item.response_accepted === true
    return Object.freeze({
      requestId,
      sessionId,
      status,
      prompt: typeof item.prompt === "string" ? item.prompt.trim() : "",
      reason: typeof item.reason === "string" ? item.reason.trim() : "",
      toolName: typeof item.tool_name === "string" ? item.tool_name.trim() : "",
      operation: typeof item.operation === "string" ? item.operation.trim() : "",
      expiresAt,
      selectable: !terminal && status === "delivered" && Date.parse(expiresAt) > now.getTime(),
      supportedDecisionScopes: decisionScopes(item.supported_decision_scopes),
      raw: Object.freeze({ ...item }),
    })
  }))
}

export function createPermissionProof(
  request: Readonly<Record<string, unknown>>,
  input: {
    responseId: string
    effect: "allow" | "deny"
    decisionScope?: PermissionDecisionScope
    now?: Date
  },
): Readonly<Record<string, unknown>> {
  const challenge = object(request.response_challenge, "permission response challenge")
  const version = text(challenge.version, "permission response version")
  const owner = text(challenge.canonical_owner, "permission canonical owner")
  if (![PERMISSION_RESPONSE_VERSION, LEGACY_PERMISSION_RESPONSE_VERSION].includes(version) || owner !== PERMISSION_OWNER) {
    throw new CliTaskError(
      "Permission response challenge names an unsupported version or owner.",
      "permission_response_challenge_invalid",
    )
  }
  const decisionScope = input.decisionScope ?? "once"
  if (version === LEGACY_PERMISSION_RESPONSE_VERSION && decisionScope !== "once") {
    throw new CliTaskError(
      "Legacy permission challenges support only the once decision scope.",
      "permission_response_scope_unsupported",
    )
  }
  if (input.effect === "deny" && decisionScope !== "once") {
    throw new CliTaskError("Deny decisions support only the once scope.", "permission_response_scope_invalid")
  }
  const expiresAt = text(request.expires_at, "permission request expiry")
  if (Date.parse(expiresAt) <= (input.now ?? new Date()).getTime()) {
    throw new CliTaskError("Permission request expired before it could be resolved.", "permission_request_expired")
  }
  const material = {
    version,
    nonce: text(challenge.nonce, "permission response nonce"),
    canonical_owner: owner,
    envelope_id: text(request.envelope_id, "permission envelope id"),
    request_id: text(request.request_id, "permission request id"),
    response_id: text(input.responseId, "permission response id"),
    effect: input.effect,
    ...(version === PERMISSION_RESPONSE_VERSION ? { decision_scope: decisionScope } : {}),
    run_id: text(request.run_id, "permission run id"),
    task_id: text(request.task_id, "permission task id"),
    session_id: text(request.session_id, "permission session id"),
    session_revision: integer(request.session_revision, "permission session revision"),
    worker_request_id: text(request.worker_request_id, "permission worker request id"),
    tool_call_id: text(request.tool_call_id, "permission tool call id"),
    request_fingerprint: text(request.request_fingerprint, "permission request fingerprint"),
    arguments_digest: text(request.arguments_digest, "permission arguments digest"),
    policy_revision: integer(request.policy_revision, "permission policy revision"),
    mode_revision: integer(request.mode_revision, "permission mode revision"),
    expires_at: expiresAt,
  }
  return Object.freeze({ ...material, proof: sha256(material) })
}

export class CliPermissionSession {
  readonly #api: CliApi
  readonly #binding: PermissionBinding
  readonly #presentedToken?: string
  #claim?: PermissionSessionClaim
  #custodyError?: CliTaskError

  constructor(input: {
    api: CliApi
    task: TaskProjection
    sessionId?: string
    custodyToken?: string
  }) {
    this.#api = input.api
    this.#binding = Object.freeze({
      taskId: input.task.taskId,
      runId: input.task.runId,
      sessionId: input.sessionId
        ?? (typeof input.task.metadata.query_session_id === "string" ? input.task.metadata.query_session_id : undefined)
        ?? input.task.sessionId
        ?? `task:${input.task.taskId}`,
    })
    this.#presentedToken = input.custodyToken?.trim() || undefined
  }

  get binding(): PermissionBinding { return this.#binding }
  get available(): boolean { return Boolean(this.#claim) }
  get custodyError(): CliTaskError | undefined { return this.#custodyError }

  async open(signal?: AbortSignal): Promise<boolean> {
    if (this.#claim) {
      await this.#api.resumePermissionSession(this.#binding, this.#claim.custodyToken, signal)
      return true
    }
    try {
      this.#claim = await this.#api.openPermissionSession(this.#binding, {
        custodyToken: this.#presentedToken,
        signal,
      })
      this.#custodyError = undefined
      return true
    } catch (error) {
      this.#custodyError = error instanceof CliTaskError
        ? error
        : new CliTaskError(
          error instanceof Error
            ? error.message
            : "Permission session custody is unavailable; requests remain fail-closed.",
          typeof (error as { code?: unknown } | undefined)?.code === "string"
            ? String((error as { code: string }).code)
            : "permission_custody_unavailable",
          {
            status: (error as { status?: unknown } | undefined)?.status,
            category: (error as { category?: unknown } | undefined)?.category,
          },
        )
      return false
    }
  }

  async pending(signal?: AbortSignal): Promise<readonly PermissionRequestView[]> {
    const claim = this.#requireClaim()
    const raw = await this.#api.permissionRequests(this.#binding, claim.custodyToken, {
      pendingOnly: true,
      limit: 200,
      signal,
    })
    return projectPermissionRequests(raw).filter((item) => item.selectable)
  }

  async resolve(input: {
    requestId: string
    effect: "allow" | "deny"
    decisionScope?: PermissionDecisionScope
    feedback?: string
    signal?: AbortSignal
  }): Promise<Readonly<Record<string, unknown>>> {
    const claim = this.#requireClaim()
    const raw = await this.#api.permissionRequests(this.#binding, claim.custodyToken, {
      pendingOnly: false,
      limit: 1_000,
      signal: input.signal,
    })
    const request = permissionItems(raw).find((item) => item.request_id === input.requestId)
    if (!request) {
      throw new CliTaskError("Permission request is not visible in the canonical session.", "permission_request_not_found", {
        request_id: input.requestId,
        session_id: this.#binding.sessionId,
      })
    }
    const responseId = `permission_response_${crypto.randomUUID().replaceAll("-", "")}`
    const decisionScope = input.decisionScope ?? "once"
    if (!decisionScopes(request.supported_decision_scopes).includes(decisionScope)) {
      throw new CliTaskError(
        `Permission request does not support the ${decisionScope} decision scope.`,
        "permission_response_scope_unsupported",
      )
    }
    const proof = createPermissionProof(request, {
      responseId,
      effect: input.effect,
      decisionScope,
    })
    return this.#api.resolvePermission({
      binding: this.#binding,
      custodyToken: claim.custodyToken,
      requestId: input.requestId,
      responseId,
      effect: input.effect,
      decisionScope,
      consoleResponse: proof,
      feedback: input.feedback,
      signal: input.signal,
    })
  }

  #requireClaim(): PermissionSessionClaim {
    if (this.#claim) return this.#claim
    throw this.#custodyError ?? new CliTaskError(
      "Permission session custody is unavailable; requests remain fail-closed.",
      "permission_custody_missing",
      { session_id: this.#binding.sessionId },
    )
  }
}
