import type { PermissionSessionBinding } from "../../api/permission-api.ts"
import {
  PERMISSION_CANONICAL_OWNER,
  PERMISSION_CONSOLE_SCHEMA,
  type PermissionConsoleDiagnostics,
  type PermissionConsolePhase,
  type PermissionConsoleSnapshot,
  type PermissionDecisionProjection,
  type PermissionDecisionReceipt,
  type PermissionInterventionRecord,
  type PermissionModeProjection,
  type PermissionProductMode,
  type PermissionRequestProjection,
  type PermissionResponseAttempt,
  type PermissionRuleProjection,
  type PermissionTimelineEntry,
} from "./contracts.ts"
import {
  buildPermissionQueueRows,
  buildPermissionTimeline,
  selectedPermissionRequest,
} from "./view-model.ts"

export interface PermissionStorePatch {
  phase?: PermissionConsolePhase
  productMode?: PermissionProductMode
  binding?: PermissionSessionBinding
  bindings?: readonly PermissionSessionBinding[]
  requests?: readonly PermissionRequestProjection[]
  mode?: PermissionModeProjection
  rules?: readonly PermissionRuleProjection[]
  decisions?: readonly PermissionDecisionProjection[]
  attempts?: readonly PermissionResponseAttempt[]
  receipts?: readonly PermissionDecisionReceipt[]
  interventions?: readonly PermissionInterventionRecord[]
  selectedRequestId?: string | null
  responseError?: PermissionConsoleSnapshot["responseError"] | null
  diagnostics?: Partial<PermissionConsoleDiagnostics>
  canonicalEvents?: readonly PermissionTimelineEntry[]
}

export class PermissionConsoleStore {
  readonly #listeners = new Set<() => void>()
  readonly #now: () => Date
  readonly #maximumTimeline: number
  #snapshot: PermissionConsoleSnapshot
  #canonicalEvents: readonly PermissionTimelineEntry[] = Object.freeze([])
  #closed = false

  constructor(options: {
    now?: () => Date
    maximumTimeline?: number
    disabled?: boolean
  } = {}) {
    this.#now = options.now ?? (() => new Date())
    this.#maximumTimeline = Math.min(
      20_000,
      Math.max(32, Math.floor(options.maximumTimeline ?? 1_000)),
    )
    this.#snapshot = initialSnapshot(
      this.#now(),
      options.disabled === true,
    )
  }

  getSnapshot = (): PermissionConsoleSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => undefined
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  patch(value: PermissionStorePatch): PermissionConsoleSnapshot {
    if (this.#closed) return this.#snapshot
    const prior = this.#snapshot
    const requests = freezeArray(value.requests ?? prior.requests)
    const attempts = freezeArray(value.attempts ?? prior.attempts)
    const receipts = freezeArray(value.receipts ?? prior.receipts)
    const interventions = freezeArray(
      value.interventions ?? prior.interventions,
    )
    const diagnostics = freezeDiagnostics({
      ...prior.diagnostics,
      ...(value.diagnostics ?? {}),
    })
    const selectedRequestId =
      value.selectedRequestId === null
        ? undefined
        : value.selectedRequestId ?? prior.selectedRequestId
    const selected = selectedPermissionRequest(
      requests,
      selectedRequestId,
    )
    const respondingRequestIds = new Set(
      attempts
        .filter((attempt) =>
          [
            "created",
            "claimed",
            "proof_ready",
            "submitted",
          ].includes(attempt.phase),
        )
        .map((attempt) => attempt.requestId),
    )
    const mode = value.mode ?? prior.mode
    const rows = buildPermissionQueueRows(requests, {
      selectedRequestId: selected?.requestId,
      respondingRequestIds,
      now: this.#now(),
    })
    if (value.canonicalEvents) {
      this.#canonicalEvents = freezeArray(value.canonicalEvents)
    }
    const localTimeline = buildPermissionTimeline({
      requests,
      attempts,
      receipts,
      interventions,
      decisions: value.decisions ?? prior.decisions,
      mode,
      maximum: this.#maximumTimeline,
    })
    const timeline = mergeTimeline(
      localTimeline,
      this.#canonicalEvents,
      this.#maximumTimeline,
    )
    const pendingCount = rows.length
    const expiringCount = rows.filter(
      (row) => row.urgency === "critical" || row.urgency === "soon",
    ).length
    this.#snapshot = Object.freeze({
      schema: PERMISSION_CONSOLE_SCHEMA,
      phase: value.phase ?? prior.phase,
      productMode: value.productMode ?? prior.productMode,
      binding: value.binding ?? prior.binding,
      bindings: freezeArray(value.bindings ?? prior.bindings),
      selectedRequestId: selected?.requestId,
      requests,
      rows: freezeArray(rows),
      selected,
      mode,
      rules: freezeArray(value.rules ?? prior.rules),
      decisions: freezeArray(value.decisions ?? prior.decisions),
      attempts,
      receipts,
      interventions,
      timeline: freezeArray(timeline),
      pendingCount,
      expiringCount,
      respondingRequestIds: freezeArray(
        [...respondingRequestIds].sort(),
      ),
      responseError:
        value.responseError === null
          ? undefined
          : value.responseError ?? prior.responseError,
      diagnostics,
      revision: prior.revision + 1,
      updatedAt: this.#now().toISOString(),
    })
    for (const listener of this.#listeners) listener()
    return this.#snapshot
  }

  select(requestId?: string): PermissionConsoleSnapshot {
    if (!requestId) return this.patch({ selectedRequestId: null })
    const exists = this.#snapshot.requests.some(
      (request) => request.requestId === requestId,
    )
    return exists
      ? this.patch({ selectedRequestId: requestId })
      : this.#snapshot
  }

  close(): void {
    if (this.#closed) return
    this.patch({ phase: "closed" })
    this.#closed = true
    this.#listeners.clear()
  }
}

function initialSnapshot(
  now: Date,
  disabled: boolean,
): PermissionConsoleSnapshot {
  const mode = defaultMode()
  return Object.freeze({
    schema: PERMISSION_CONSOLE_SCHEMA,
    phase: disabled ? "disabled" : "idle",
    productMode: "interactive",
    bindings: Object.freeze([]),
    requests: Object.freeze([]),
    rows: Object.freeze([]),
    mode,
    rules: Object.freeze([]),
    decisions: Object.freeze([]),
    attempts: Object.freeze([]),
    receipts: Object.freeze([]),
    interventions: Object.freeze([]),
    timeline: Object.freeze([]),
    pendingCount: 0,
    expiringCount: 0,
    respondingRequestIds: Object.freeze([]),
    diagnostics: freezeDiagnostics({
      generation: 0,
      refreshCount: 0,
      reconnectCount: 0,
      sourceOwner: PERMISSION_CANONICAL_OWNER,
      sourceSchema: "zyra.permission-api.v2",
      backendAuthoritative: true,
      localPendingStore: false,
      custodyInMemoryOnly: true,
      responseProofRequired: true,
      disabledReason: disabled
        ? "Permission console was disabled by runtime configuration."
        : undefined,
    }),
    revision: 0,
    updatedAt: now.toISOString(),
  })
}

function defaultMode(): PermissionModeProjection {
  return Object.freeze({
    mode: "default",
    productMode: "interactive",
    revision: 0,
    policyRevision: 0,
    policyHash: "0".repeat(64),
    frozen: false,
    noHuman: false,
    askWillDeny: false,
    humanInterventionCount: 0,
    canonicalOwner: PERMISSION_CANONICAL_OWNER,
  })
}

function freezeDiagnostics(
  value: PermissionConsoleDiagnostics,
): PermissionConsoleDiagnostics {
  return Object.freeze({ ...value })
}

function freezeArray<T>(value: readonly T[]): readonly T[] {
  return Object.freeze([...value])
}

function mergeTimeline(
  local: readonly PermissionTimelineEntry[],
  canonical: readonly PermissionTimelineEntry[],
  maximum: number,
): PermissionTimelineEntry[] {
  const entries = new Map<string, PermissionTimelineEntry>()
  for (const entry of [...local, ...canonical]) {
    const key =
      entry.refs.event_id
        ? `event:${entry.refs.event_id}`
        : entry.entryId
    const prior = entries.get(key)
    if (!prior || (!prior.canonical && entry.canonical)) {
      entries.set(key, entry)
    }
  }
  return [...entries.values()]
    .sort(
      (left, right) =>
        left.createdAt.localeCompare(right.createdAt)
        || left.entryId.localeCompare(right.entryId),
    )
    .slice(-maximum)
}
