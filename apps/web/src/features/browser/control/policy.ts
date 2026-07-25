import {
  BROWSER_CONTROL_SCHEMA,
  BrowserControlAction,
  BrowserContractError,
  BrowserStepPhase,
  normalizedIdentity,
  normalizedUrl,
  stableDigest,
  type BrowserAction,
  type BrowserControlActionValue,
  type BrowserControlIdentity,
  type BrowserControlRequest,
  type BrowserHealth,
  type BrowserSessionProjection,
  type BrowserStep,
} from "../contracts.ts"
import { browserHealthCanControl } from "../projection/health.ts"
import { retryActionPayload } from "../projection/steps.ts"

export interface BrowserControlDraft {
  action: BrowserControlActionValue
  identity: BrowserControlIdentity
  url?: string
  retryAction?: Readonly<Record<string, unknown>>
  reason?: string
  commandId?: string
  requestId?: string
  idempotencyKey?: string
  timeoutMs?: number
}

export interface BrowserControlDecision {
  allowed: boolean
  request?: BrowserControlRequest
  code?: string
  reason?: string
  expectedDenial: boolean
  requiresPermission: boolean
  mutating: boolean
  retryable: boolean
}

const CONTROL_ACTIONS = new Set<BrowserControlActionValue>(
  Object.values(BrowserControlAction),
)

function timeoutValue(value: number | undefined): number {
  if (value === undefined) return 30_000
  if (!Number.isFinite(value) || value < 1_000 || value > 300_000) {
    throw new BrowserContractError(
      "browser_control_timeout_invalid",
      "Browser control timeout must be between 1 and 300 seconds.",
      "$.timeout_ms",
      value,
    )
  }
  return Math.floor(value)
}

function reasonValue(
  action: BrowserControlActionValue,
  value: string | undefined,
): string {
  const fallback =
    action === BrowserControlAction.NAVIGATE
      ? "Navigate from the Zyra browser viewer."
      : action === BrowserControlAction.STOP
        ? "Stop from the Zyra browser viewer."
        : action === BrowserControlAction.RETRY
          ? "Bounded retry from the Zyra browser viewer."
          : "Inspect from the Zyra browser viewer."
  const reason = String(value ?? fallback).trim()
  if (!reason) {
    throw new BrowserContractError(
      "browser_control_reason_empty",
      "Browser control reason must not be empty.",
      "$.reason",
      value,
    )
  }
  if (new TextEncoder().encode(reason).byteLength > 8 * 1024) {
    throw new BrowserContractError(
      "browser_control_reason_oversized",
      "Browser control reason exceeds 8 KiB.",
      "$.reason",
      value,
    )
  }
  return reason
}

function validateIdentity(identity: BrowserControlIdentity): BrowserControlIdentity {
  const expectedGeneration =
    identity.expectedGeneration === undefined
      ? undefined
      : boundedRevision(identity.expectedGeneration, "expected_generation")
  const expectedTaskRevision =
    identity.expectedTaskRevision === undefined
      ? undefined
      : boundedRevision(identity.expectedTaskRevision, "expected_task_revision")
  return Object.freeze({
    taskId: normalizedIdentity(identity.taskId, "task_id"),
    runId: normalizedIdentity(identity.runId, "run_id"),
    browserSessionId: normalizedIdentity(
      identity.browserSessionId,
      "browser_session_id",
    ),
    workerRequestId:
      normalizedIdentity(
        identity.workerRequestId,
        "worker_request_id",
        true,
      ) || undefined,
    actionId:
      normalizedIdentity(identity.actionId, "action_id", true) || undefined,
    expectedGeneration,
    expectedTaskRevision,
    actorId: normalizedIdentity(identity.actorId || "zyra-web-browser", "actor_id"),
    sealed: identity.sealed === true,
  })
}

function boundedRevision(value: number, label: string): number {
  const result = Number(value)
  if (!Number.isSafeInteger(result) || result < 0) {
    throw new BrowserContractError(
      "browser_control_revision_invalid",
      `${label} must be a non-negative safe integer.`,
      `$.${label}`,
      value,
    )
  }
  return result
}

function retryPayload(value: unknown): Readonly<Record<string, unknown>> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new BrowserContractError(
      "browser_control_retry_action_required",
      "Retry requires one prior browser action.",
      "$.retry_action",
      value,
    )
  }
  const raw = value as Readonly<Record<string, unknown>>
  const action = String(raw.action ?? "").trim()
  const args = raw.arguments
  if (!action || !args || typeof args !== "object" || Array.isArray(args)) {
    throw new BrowserContractError(
      "browser_control_retry_action_invalid",
      "Retry action requires an action name and arguments object.",
      "$.retry_action",
      value,
    )
  }
  if (Object.keys(args).length > 128) {
    throw new BrowserContractError(
      "browser_control_retry_action_oversized",
      "Retry action contains too many arguments.",
      "$.retry_action.arguments",
      value,
    )
  }
  return Object.freeze({
    action: action.slice(0, 512),
    arguments: Object.freeze({ ...(args as Record<string, unknown>) }),
    retry_of_action_id: String(raw.retry_of_action_id ?? "").slice(0, 512),
    expected_argument_digest: String(
      raw.expected_argument_digest ?? "",
    ).slice(0, 512),
  })
}

function createIds(
  draft: BrowserControlDraft,
  identity: BrowserControlIdentity,
  actionPayload: unknown,
  now: number,
): { commandId: string; requestId: string; idempotencyKey: string } {
  const fingerprint = stableDigest({
    taskId: identity.taskId,
    runId: identity.runId,
    browserSessionId: identity.browserSessionId,
    workerRequestId: identity.workerRequestId,
    actionId: identity.actionId,
    action: draft.action,
    payload: actionPayload,
    expectedGeneration: identity.expectedGeneration,
    expectedTaskRevision: identity.expectedTaskRevision,
    actorId: identity.actorId,
    sealed: identity.sealed,
  })
  const suffix = fingerprint.replace(/[^A-Za-z0-9]/g, "").slice(-24)
  const epoch = Math.floor(now).toString(36)
  return {
    commandId:
      draft.commandId
        ? normalizedIdentity(draft.commandId, "command_id")
        : `browser-control-${draft.action}-${epoch}-${suffix}`,
    requestId:
      draft.requestId
        ? normalizedIdentity(draft.requestId, "request_id")
        : `browser-control-request-${epoch}-${suffix}`,
    idempotencyKey:
      draft.idempotencyKey
        ? normalizedIdentity(draft.idempotencyKey, "idempotency_key")
        : `browser-viewer:${identity.taskId}:${draft.action}:${suffix}`,
  }
}

export function createBrowserControlRequest(
  draft: BrowserControlDraft,
  now = Date.now(),
): BrowserControlRequest {
  if (!CONTROL_ACTIONS.has(draft.action)) {
    throw new BrowserContractError(
      "browser_control_action_invalid",
      `Unknown browser viewer control ${String(draft.action)}.`,
      "$.action",
      draft.action,
    )
  }
  const identity = validateIdentity(draft.identity)
  const url =
    draft.action === BrowserControlAction.NAVIGATE
      ? normalizedUrl(draft.url, "$.url", { forbidCredentials: true })
      : undefined
  const retry =
    draft.action === BrowserControlAction.RETRY
      ? retryPayload(draft.retryAction)
      : undefined
  if (draft.action === BrowserControlAction.STOP && !identity.browserSessionId) {
    throw new BrowserContractError(
      "browser_control_stop_session_required",
      "Stop requires a browser session identity.",
      "$.identity.browser_session_id",
      identity.browserSessionId,
    )
  }
  const ids = createIds(draft, identity, url ?? retry, now)
  return Object.freeze({
    schema: BROWSER_CONTROL_SCHEMA,
    ...ids,
    action: draft.action,
    identity,
    url,
    retryAction: retry,
    reason: reasonValue(draft.action, draft.reason),
    createdAt: now,
    timeoutMs: timeoutValue(draft.timeoutMs),
  })
}

function selectedStep(
  session: BrowserSessionProjection,
  stepId?: string,
): BrowserStep | undefined {
  return (
    (stepId
      ? session.steps.find((step) => step.stepId === stepId)
      : undefined)
    ?? session.steps.at(-1)
  )
}

function selectedAction(
  session: BrowserSessionProjection,
  step: BrowserStep | undefined,
  actionId?: string,
): BrowserAction | undefined {
  return (
    (actionId
      ? session.actions.find((action) => action.actionId === actionId)
      : undefined)
    ?? (step
      ? session.actions
        .filter((action) => step.actionIds.includes(action.actionId))
        .at(-1)
      : undefined)
  )
}

export function browserControlDecision(input: {
  action: BrowserControlActionValue
  session: BrowserSessionProjection
  taskId: string
  runId: string
  actorId?: string
  sealed?: boolean
  url?: string
  stepId?: string
  actionId?: string
  expectedGeneration?: number
  expectedTaskRevision?: number
  reason?: string
  timeoutMs?: number
  now?: number
}): BrowserControlDecision {
  if (!CONTROL_ACTIONS.has(input.action)) {
    return Object.freeze({
      allowed: false,
      code: "browser_control_action_invalid",
      reason: "Unsupported browser viewer control.",
      expectedDenial: false,
      requiresPermission: false,
      mutating: false,
      retryable: false,
    })
  }
  const mutating = input.action !== BrowserControlAction.INSPECT
  const requiresPermission =
    input.action === BrowserControlAction.NAVIGATE
    || input.action === BrowserControlAction.RETRY
    || input.action === BrowserControlAction.STOP
  const expectedDenial = input.sealed === true
  const healthDecision = browserHealthCanControl(
    input.session.health,
    input.action,
  )
  if (!healthDecision.allowed && !expectedDenial) {
    return Object.freeze({
      allowed: false,
      code: "browser_control_health_rejected",
      reason: healthDecision.reason,
      expectedDenial,
      requiresPermission,
      mutating,
      retryable: input.action === BrowserControlAction.RETRY,
    })
  }
  const step = selectedStep(input.session, input.stepId)
  const action = selectedAction(input.session, step, input.actionId)
  if (input.action === BrowserControlAction.RETRY) {
    if (!step || !action) {
      return Object.freeze({
        allowed: false,
        code: "browser_control_retry_target_missing",
        reason: "Select a failed browser action before retrying.",
        expectedDenial,
        requiresPermission,
        mutating,
        retryable: false,
      })
    }
    if (
      step.status !== BrowserStepPhase.FAILED
      && !step.retryable
      && !action.retryable
    ) {
      return Object.freeze({
        allowed: false,
        code: "browser_control_retry_not_bounded",
        reason: "Selected browser action is not marked retryable.",
        expectedDenial,
        requiresPermission,
        mutating,
        retryable: false,
      })
    }
  }
  try {
    const request = createBrowserControlRequest({
      action: input.action,
      identity: {
        taskId: input.taskId,
        runId: input.runId,
        browserSessionId: input.session.scope.browserSessionId,
        workerRequestId: input.session.scope.workerRequestId,
        actionId: action?.actionId,
        expectedGeneration: input.expectedGeneration,
        expectedTaskRevision: input.expectedTaskRevision,
        actorId: input.actorId ?? "zyra-web-browser",
        sealed: input.sealed === true,
      },
      url: input.url,
      retryAction:
        input.action === BrowserControlAction.RETRY && action
          ? retryActionPayload(action)
          : undefined,
      reason: input.reason,
      timeoutMs: input.timeoutMs,
    }, input.now)
    return Object.freeze({
      allowed: true,
      request,
      expectedDenial,
      requiresPermission,
      mutating,
      retryable: input.action === BrowserControlAction.RETRY,
    })
  } catch (error) {
    return Object.freeze({
      allowed: false,
      code:
        error instanceof BrowserContractError
          ? error.code
          : "browser_control_request_invalid",
      reason: error instanceof Error ? error.message : String(error),
      expectedDenial,
      requiresPermission,
      mutating,
      retryable: false,
    })
  }
}

export function browserControlActionLabel(
  action: BrowserControlActionValue,
): string {
  switch (action) {
    case BrowserControlAction.NAVIGATE:
      return "Navigate"
    case BrowserControlAction.STOP:
      return "Stop session"
    case BrowserControlAction.RETRY:
      return "Retry action"
    case BrowserControlAction.INSPECT:
      return "Inspect runtime"
  }
}

export function browserControlMutates(
  action: BrowserControlActionValue,
): boolean {
  return action !== BrowserControlAction.INSPECT
}
