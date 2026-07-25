import {
  comparePermissionTime,
  secondsUntil,
  stablePermissionId,
} from "./canonical.ts"
import type {
  PermissionCrossViewTarget,
  PermissionDecisionProjection,
  PermissionDecisionReceipt,
  PermissionInterventionRecord,
  PermissionModeProjection,
  PermissionQueueRow,
  PermissionRequestProjection,
  PermissionResponseAttempt,
  PermissionSourceSurface,
  PermissionTimelineEntry,
  PermissionWarningSeverity,
} from "./contracts.ts"

export function buildPermissionQueueRows(
  requests: readonly PermissionRequestProjection[],
  input: {
    selectedRequestId?: string
    respondingRequestIds: ReadonlySet<string>
    now: Date
    availableSelectors?: ReadonlySet<string>
  },
): PermissionQueueRow[] {
  return requests
    .filter((request) => !request.terminal)
    .map((request) => {
      const secondsRemaining = secondsUntil(request.expiresAt, input.now)
      const urgency =
        request.expired
          ? "expired"
          : secondsRemaining <= 15
            ? "critical"
            : secondsRemaining <= 60
              ? "soon"
              : "normal"
      return Object.freeze({
        request,
        selected: request.requestId === input.selectedRequestId,
        responsePending: input.respondingRequestIds.has(request.requestId),
        secondsRemaining,
        urgency,
        crossViewTargets: Object.freeze(
          permissionCrossViewTargets(
            request,
            input.availableSelectors,
          ),
        ),
      })
    })
    .sort(compareQueueRows)
}

export function permissionCrossViewTargets(
  request: PermissionRequestProjection,
  availableSelectors: ReadonlySet<string> = new Set(),
): PermissionCrossViewTarget[] {
  const candidates: PermissionCrossViewTarget[] = [
    target(
      "permission",
      `[data-permission-request-id="${cssEscape(request.requestId)}"]`,
      "Permission request",
      request.requestId,
      true,
    ),
    target(
      "timeline",
      `[data-event-request-id="${cssEscape(request.requestId)}"], [data-permission-id="${cssEscape(request.requestId)}"]`,
      "Causal timeline",
      request.requestId,
      true,
    ),
    target(
      "command",
      `[data-command-permission-id="${cssEscape(request.requestId)}"], [data-control-request-id="${cssEscape(request.requestId)}"]`,
      "Command queue",
      request.requestId,
      request.sourceSurface === "command",
    ),
    target(
      "browser",
      `[data-browser-permission-request-id="${cssEscape(request.requestId)}"], [data-permission-request-id="${cssEscape(request.requestId)}"][data-browser-step]`,
      "Browser action",
      request.requestId,
      request.sourceSurface === "browser",
    ),
    target(
      "terminal",
      `[data-terminal-permission-request-id="${cssEscape(request.requestId)}"], [data-permission-request-id="${cssEscape(request.requestId)}"][data-terminal-entry]`,
      "Terminal action",
      request.requestId,
      request.sourceSurface === "terminal",
    ),
  ]
  return candidates.map((candidate) =>
    Object.freeze({
      ...candidate,
      available:
        candidate.available
        && (
          availableSelectors.size === 0
          || [...availableSelectors].some(
            (selector) =>
              selector === candidate.selector
              || selector.includes(request.requestId),
          )
        ),
    }),
  )
}

export function buildPermissionTimeline(input: {
  requests: readonly PermissionRequestProjection[]
  decisions: readonly PermissionDecisionProjection[]
  attempts: readonly PermissionResponseAttempt[]
  receipts: readonly PermissionDecisionReceipt[]
  interventions: readonly PermissionInterventionRecord[]
  mode: PermissionModeProjection
  maximum?: number
}): PermissionTimelineEntry[] {
  const entries: PermissionTimelineEntry[] = []
  for (const request of input.requests) {
    entries.push(
      timeline({
        entryId: stablePermissionId(
          "permission_timeline_request",
          request.requestId,
          request.createdAt,
        ),
        requestId: request.requestId,
        kind: "request",
        phase: "created",
        title: `${request.toolName} requested permission`,
        detail: request.reason,
        createdAt: request.createdAt,
        severity: request.riskLevel === "high" ? "danger" : "info",
        sourceSurface: request.sourceSurface,
        exactBinding: true,
        canonical: true,
        refs: identityRefs(request),
      }),
    )
    if (request.deliveredAt || request.status === "delivered") {
      entries.push(
        timeline({
          entryId: stablePermissionId(
            "permission_timeline_delivery",
            request.requestId,
            request.deliveryReceiptId ?? request.updatedAt,
          ),
          requestId: request.requestId,
          kind: "delivery",
          phase: request.status,
          title: "Approval request delivered",
          detail: `Attempt ${request.deliveryAttempt}; response challenge ${short(request.responseChallenge.challengeDigest)}.`,
          createdAt: request.deliveredAt ?? request.updatedAt,
          severity: request.stale ? "danger" : "info",
          sourceSurface: request.sourceSurface,
          exactBinding: !request.stale,
          canonical: true,
          refs: identityRefs(request),
        }),
      )
    }
    if (request.expired) {
      entries.push(
        timeline({
          entryId: stablePermissionId(
            "permission_timeline_timeout",
            request.requestId,
            request.expiresAt,
          ),
          requestId: request.requestId,
          kind: "timeout",
          phase: "default_deny",
          title: "Approval timed out",
          detail:
            "The exact response challenge expired. Late approval is rejected and the action remains fail closed.",
          createdAt: request.expiresAt,
          severity: "warning",
          sourceSurface: request.sourceSurface,
          exactBinding: true,
          canonical: true,
          refs: identityRefs(request),
        }),
      )
    }
  }
  for (const attempt of input.attempts) {
    entries.push(
      timeline({
        entryId: stablePermissionId(
          "permission_timeline_attempt",
          attempt.attemptId,
          attempt.updatedAt,
        ),
        requestId: attempt.requestId,
        responseId: attempt.responseId,
        kind: "response",
        phase: attempt.phase,
        title: responseAttemptTitle(attempt),
        detail:
          attempt.errorMessage
          ?? `${attempt.displayResponder} submitted ${attempt.effect} from ${attempt.source}.`,
        createdAt: attempt.updatedAt,
        severity: attemptSeverity(attempt),
        sourceSurface: sourceFromAttempt(attempt),
        exactBinding:
          attempt.phase === "accepted"
          || attempt.phase === "proof_ready"
          || attempt.phase === "submitted",
        canonical: Boolean(attempt.receipt),
        refs: Object.freeze({
          request_id: attempt.requestId,
          response_id: attempt.responseId,
          attempt_id: attempt.attemptId,
        }),
      }),
    )
  }
  for (const decision of input.decisions) {
    entries.push(
      timeline({
        entryId: stablePermissionId(
          "permission_timeline_decision",
          decision.decisionId,
        ),
        requestId: decision.requestId,
        responseId: decision.responseId,
        kind: "decision",
        phase: decision.effect,
        title: `${decision.effect.toUpperCase()} · ${decision.reasonCode}`,
        detail: decision.reason,
        createdAt: decision.evaluatedAt,
        severity: decision.effect === "deny" ? "warning" : "info",
        sourceSurface: "tool",
        exactBinding: decision.exactBinding,
        canonical: true,
        refs: Object.freeze({
          decision_id: decision.decisionId,
          ...(decision.requestId
            ? { request_id: decision.requestId }
            : {}),
          ...(decision.toolCallId
            ? { tool_call_id: decision.toolCallId }
            : {}),
        }),
      }),
    )
  }
  for (const receipt of input.receipts) {
    entries.push(
      timeline({
        entryId: stablePermissionId(
          "permission_timeline_receipt",
          receipt.responseId,
          receipt.stateDigest,
        ),
        requestId: receipt.requestId,
        responseId: receipt.responseId,
        kind: receipt.permitId ? "permit" : "response",
        phase: receipt.accepted ? "accepted" : "rejected",
        title: receipt.permitId
          ? "Exact-call permit issued"
          : "Permission decision receipt",
        detail: receipt.permitId
          ? `Permit ${receipt.permitId} is bound to the approved physical call.`
          : `Canonical owner ${receipt.canonicalOwner} returned ${receipt.effect}.`,
        createdAt: receipt.receivedAt,
        severity: receipt.accepted ? "info" : "danger",
        sourceSurface: "tool",
        exactBinding:
          receipt.responseProofVerified
          && Boolean(receipt.finalArgumentsDigest),
        canonical: true,
        refs: Object.freeze({
          request_id: receipt.requestId,
          response_id: receipt.responseId,
          ...(receipt.permitId ? { permit_id: receipt.permitId } : {}),
          ...(receipt.decisionId
            ? { decision_id: receipt.decisionId }
            : {}),
        }),
      }),
    )
  }
  for (const intervention of input.interventions) {
    entries.push(
      timeline({
        entryId: intervention.interventionId,
        requestId: intervention.requestId,
        kind: "intervention",
        phase: intervention.rejected ? "rejected" : "recorded",
        title: `${intervention.action} intervention ${intervention.rejected ? "rejected" : "recorded"}`,
        detail: intervention.reason,
        createdAt: intervention.recordedAt,
        severity: intervention.rejected ? "warning" : "danger",
        sourceSurface: "command",
        exactBinding: Boolean(intervention.requestId),
        canonical: Boolean(intervention.canonicalReceipt),
        refs: Object.freeze({
          intervention_id: intervention.interventionId,
          ...(intervention.requestId
            ? { request_id: intervention.requestId }
            : {}),
        }),
      }),
    )
  }
  if (input.mode.productMode === "sealed") {
    entries.push(
      timeline({
        entryId: stablePermissionId(
          "permission_timeline_sealed",
          input.mode.policyHash,
          input.mode.policyRevision,
          input.mode.revision,
        ),
        kind: "restore",
        phase: "sealed",
        title: "Sealed policy active",
        detail: `Policy ${short(input.mode.policyHash)} is frozen; ASK deterministically denies and replans with zero human intervention.`,
        createdAt:
          entries.at(-1)?.createdAt
          ?? new Date(0).toISOString(),
        severity: "info",
        sourceSurface: "command",
        exactBinding: true,
        canonical: true,
        refs: Object.freeze({
          policy_hash: input.mode.policyHash,
          policy_revision: String(input.mode.policyRevision),
          mode_revision: String(input.mode.revision),
        }),
      }),
    )
  }
  const maximum = Math.min(
    20_000,
    Math.max(16, Math.floor(input.maximum ?? 1_000)),
  )
  return dedupeTimeline(entries)
    .sort(
      (left, right) =>
        comparePermissionTime(left.createdAt, right.createdAt)
        || left.entryId.localeCompare(right.entryId),
    )
    .slice(-maximum)
}

export function selectedPermissionRequest(
  requests: readonly PermissionRequestProjection[],
  selectedRequestId: string | undefined,
): PermissionRequestProjection | undefined {
  if (selectedRequestId) {
    const explicit = requests.find(
      (request) => request.requestId === selectedRequestId,
    )
    if (explicit) return explicit
  }
  return requests.find((request) => !request.terminal) ?? requests[0]
}

export function permissionQueueSummary(
  rows: readonly PermissionQueueRow[],
): Readonly<Record<string, number | boolean>> {
  return Object.freeze({
    pending: rows.length,
    responding: rows.filter((row) => row.responsePending).length,
    critical: rows.filter(
      (row) => row.urgency === "critical" || row.urgency === "expired",
    ).length,
    browser: rows.filter(
      (row) => row.request.sourceSurface === "browser",
    ).length,
    terminal: rows.filter(
      (row) => row.request.sourceSurface === "terminal",
    ).length,
    mcp: rows.filter((row) => row.request.sourceSurface === "mcp").length,
    blocked: rows.length > 0,
  })
}

function identityRefs(
  request: PermissionRequestProjection,
): Readonly<Record<string, string>> {
  return Object.freeze({
    request_id: request.requestId,
    envelope_id: request.envelopeId,
    run_id: request.runId,
    task_id: request.taskId,
    session_id: request.sessionId,
    worker_request_id: request.workerRequestId,
    tool_call_id: request.toolCallId,
    arguments_digest: request.argumentsDigest,
    request_fingerprint: request.requestFingerprint,
  })
}

function target(
  kind: PermissionCrossViewTarget["kind"],
  selector: string,
  label: string,
  requestId: string,
  available: boolean,
): PermissionCrossViewTarget {
  return Object.freeze({
    kind,
    selector,
    label,
    requestId,
    available,
  })
}

function timeline(
  value: PermissionTimelineEntry,
): PermissionTimelineEntry {
  return Object.freeze({
    ...value,
    refs: Object.freeze({ ...value.refs }),
  })
}

function compareQueueRows(
  left: PermissionQueueRow,
  right: PermissionQueueRow,
): number {
  const urgencyRank = {
    expired: 0,
    critical: 1,
    soon: 2,
    normal: 3,
  } as const
  return (
    urgencyRank[left.urgency] - urgencyRank[right.urgency]
    || Number(right.responsePending) - Number(left.responsePending)
    || comparePermissionTime(
      left.request.expiresAt,
      right.request.expiresAt,
    )
    || left.request.requestId.localeCompare(right.request.requestId)
  )
}

function responseAttemptTitle(attempt: PermissionResponseAttempt): string {
  const action = attempt.effect === "allow" ? "Allow" : "Deny"
  const phases: Record<PermissionResponseAttempt["phase"], string> = {
    created: "prepared",
    claimed: "claimed",
    proof_ready: "proof ready",
    submitted: "submitted",
    accepted: "accepted",
    rejected: "rejected",
    lost_race: "lost response race",
    cancelled: "cancelled",
  }
  return `${action} response ${phases[attempt.phase]}`
}

function attemptSeverity(
  attempt: PermissionResponseAttempt,
): PermissionWarningSeverity {
  if (attempt.phase === "rejected" || attempt.phase === "lost_race") {
    return "danger"
  }
  if (attempt.phase === "cancelled") return "warning"
  return "info"
}

function sourceFromAttempt(
  attempt: PermissionResponseAttempt,
): PermissionSourceSurface {
  if (attempt.source === "browser-panel") return "browser"
  if (attempt.source === "terminal-panel") return "terminal"
  if (attempt.source === "command") return "command"
  return "tool"
}

function dedupeTimeline(
  values: readonly PermissionTimelineEntry[],
): PermissionTimelineEntry[] {
  const byId = new Map<string, PermissionTimelineEntry>()
  for (const value of values) byId.set(value.entryId, value)
  return [...byId.values()]
}

function cssEscape(value: string): string {
  if (typeof CSS !== "undefined" && typeof CSS.escape === "function") {
    return CSS.escape(value)
  }
  return value.replace(/["\\\n\r\f]/g, (character) => {
    return `\\${character.charCodeAt(0).toString(16)} `
  })
}

function short(value: string | undefined): string {
  const rendered = String(value || "")
  return rendered.length > 18
    ? `${rendered.slice(0, 10)}…${rendered.slice(-6)}`
    : rendered
}
