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
export type PermissionModeName =
  | "default"
  | "acceptEdits"
  | "dontAsk"
  | "plan"
  | "bypassPermissions"
  | "auto"
  | "sealed"
export type UserSelectablePermissionMode = Exclude<PermissionModeName, "sealed">

const PERMISSION_MODES = new Set<PermissionModeName>([
  "default",
  "acceptEdits",
  "dontAsk",
  "plan",
  "bypassPermissions",
  "auto",
  "sealed",
])

export interface PermissionModeView {
  mode: PermissionModeName
  revision: number
  interactive: boolean
  headless: boolean
  sealedAutonomous: boolean
  bypassAvailable: boolean
  autoClassifierEnabled: boolean
  changedAt?: string
  changedBy?: string
  reason?: string
}

export interface PermissionModeUpdateResult {
  state: PermissionModeView
  reconciled: boolean
  revisionBefore: number
  revisionAfter: number
}

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

function boolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") {
    throw new CliTaskError(`${label} must be boolean.`, "permission_contract_invalid")
  }
  return value
}

function permissionModeName(value: unknown): PermissionModeName {
  const mode = text(value, "permission mode") as PermissionModeName
  if (!PERMISSION_MODES.has(mode)) {
    throw new CliTaskError(`Permission mode ${mode} is unsupported.`, "permission_mode_unsupported")
  }
  return mode
}

export function projectPermissionMode(value: Readonly<Record<string, unknown>>): PermissionModeView {
  const mode = object(value.mode, "permission mode state")
  return Object.freeze({
    mode: permissionModeName(mode.mode),
    revision: integer(mode.revision, "permission mode revision"),
    interactive: boolean(mode.interactive, "permission mode interactive"),
    headless: boolean(mode.headless, "permission mode headless"),
    sealedAutonomous: boolean(mode.sealedAutonomous, "permission mode sealedAutonomous"),
    bypassAvailable: boolean(mode.bypassAvailable, "permission mode bypassAvailable"),
    autoClassifierEnabled: boolean(mode.autoClassifierEnabled, "permission mode autoClassifierEnabled"),
    ...(typeof mode.changedAt === "string" && mode.changedAt ? { changedAt: mode.changedAt } : {}),
    ...(typeof mode.changedBy === "string" && mode.changedBy ? { changedBy: mode.changedBy } : {}),
    ...(typeof mode.reason === "string" && mode.reason ? { reason: mode.reason } : {}),
  })
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
  get available(): boolean { return Boolean(this.#claim) && !this.#custodyError }
  get custodyError(): CliTaskError | undefined { return this.#custodyError }

  async open(signal?: AbortSignal): Promise<boolean> {
    try {
      if (this.#claim) {
        await this.#api.resumePermissionSession(this.#binding, this.#claim.custodyToken, signal)
        this.#custodyError = undefined
        return true
      }
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

  async mode(signal?: AbortSignal): Promise<PermissionModeView> {
    const claim = this.#requireClaim()
    return projectPermissionMode(
      await this.#api.permissionMode(this.#binding, claim.custodyToken, signal),
    )
  }

  async setMode(input: {
    mode: UserSelectablePermissionMode
    expectedRevision: number
    reason?: string
    signal?: AbortSignal
  }): Promise<PermissionModeUpdateResult> {
    const claim = this.#requireClaim()
    if (input.mode === "bypassPermissions") {
      const current = await this.mode(input.signal)
      if (!current.bypassAvailable) {
        throw new CliTaskError(
          "Managed permission bypass is not available for this session.",
          "permission_bypass_unavailable",
        )
      }
    }
    let receipt: Readonly<Record<string, unknown>> | undefined
    let mutationError: unknown
    try {
      receipt = await this.#api.updatePermissionMode({
        binding: this.#binding,
        custodyToken: claim.custodyToken,
        mode: input.mode,
        expectedRevision: input.expectedRevision,
        reason: input.reason ?? "Zyra product CLI permission mode selection",
        signal: input.signal,
      })
    } catch (error) {
      mutationError = error
    }

    const observed = await this.mode(input.signal)
    if (mutationError) {
      if (
        observed.mode === input.mode
        && observed.revision >= input.expectedRevision
      ) {
        return Object.freeze({
          state: observed,
          reconciled: true,
          revisionBefore: input.expectedRevision,
          revisionAfter: observed.revision,
        })
      }
      throw new CliTaskError(
        `Permission mode changed concurrently; canonical state is ${observed.mode} revision ${observed.revision}.`,
        "permission_mode_conflict",
        {
          requested_mode: input.mode,
          expected_revision: input.expectedRevision,
          actual_mode: observed.mode,
          actual_revision: observed.revision,
          automatic_retry: false,
          cause: mutationError instanceof Error ? mutationError.message : String(mutationError),
        },
      )
    }

    const transition = object(object(receipt?.receipt, "permission mode receipt").transition, "permission mode transition")
    const transitionTo = permissionModeName(transition.to)
    const revisionBefore = integer(transition.revisionBefore, "permission mode revision before")
    const revisionAfter = integer(transition.revisionAfter, "permission mode revision after")
    if (
      transitionTo !== input.mode
      || revisionBefore !== input.expectedRevision
      || observed.mode !== transitionTo
      || observed.revision !== revisionAfter
    ) {
      throw new CliTaskError(
        "Permission mode receipt did not converge with canonical state.",
        "permission_mode_reconciliation_failed",
        {
          requested_mode: input.mode,
          expected_revision: input.expectedRevision,
          receipt_mode: transitionTo,
          receipt_revision: revisionAfter,
          actual_mode: observed.mode,
          actual_revision: observed.revision,
        },
      )
    }
    return Object.freeze({
      state: observed,
      reconciled: false,
      revisionBefore,
      revisionAfter,
    })
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
    const result = await this.#api.resolvePermission({
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
    const receipt = object(result.receipt, "permission resolution receipt")
    const actualEffect = text(receipt.effect, "permission receipt effect")
    const actualScope = text(receipt.decision_scope ?? receipt.decisionScope ?? "once", "permission receipt decision scope")
    const actualRequestId = text(receipt.request_id ?? receipt.requestId ?? input.requestId, "permission receipt request id")
    if (receipt.accepted !== true) {
      throw new CliTaskError("Canonical permission owner did not accept the decision.", "permission_decision_rejected", {
        request_id: input.requestId,
        effect: input.effect,
        decision_scope: decisionScope,
      })
    }
    if (actualRequestId !== input.requestId || actualEffect !== input.effect || actualScope !== decisionScope) {
      throw new CliTaskError(
        `Permission decision lost a concurrency race; canonical decision is ${actualEffect}/${actualScope}.`,
        "permission_decision_conflict",
        {
          request_id: input.requestId,
          requested_effect: input.effect,
          requested_scope: decisionScope,
          actual_request_id: actualRequestId,
          actual_effect: actualEffect,
          actual_scope: actualScope,
          automatic_retry: false,
        },
      )
    }
    return result
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
