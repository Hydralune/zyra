import {
  comparePermissionTime,
  identifier,
  stablePermissionId,
} from "./canonical.ts"
import type {
  PermissionTimelineEntry,
  PermissionWarningSeverity,
} from "./contracts.ts"
import type { PermissionCanonicalEvent } from "./event-reconciler.ts"
import type { PermissionReceiptAdmission } from "./receipt-admission.ts"

export type PermissionPermitPhase =
  | "issued"
  | "claimed"
  | "consumed"
  | "revoked"
  | "expired"
  | "conflict"

export interface PermissionPermitProjection {
  permitId: string
  requestId: string
  responseId: string
  argumentsDigest: string
  phase: PermissionPermitPhase
  issuedAt: string
  updatedAt: string
  claimedAt?: string
  consumedAt?: string
  terminalEventId?: string
  eventIds: readonly string[]
  exactBinding: boolean
  replayRejected: boolean
  canonical: boolean
}

export interface PermissionPermitLifecycleSnapshot {
  schema: "zyra.permission-permit-lifecycle/v1"
  permits: readonly PermissionPermitProjection[]
  quarantines: readonly {
    quarantineId: string
    permitId?: string
    eventId?: string
    code: string
    message: string
    createdAt: string
  }[]
  issuedCount: number
  consumedCount: number
  revokedCount: number
  conflictCount: number
  orphanEventCount: number
  ownsPermitState: false
  revision: number
}

export class PermissionPermitLifecycle {
  readonly #permits = new Map<string, PermissionPermitProjection>()
  readonly #eventFingerprints = new Map<string, string>()
  readonly #conflictingEventIds = new Set<string>()
  readonly #orphanObservations = new Map<
    string,
    {
      event: PermissionCanonicalEvent
      observation: NonNullable<ReturnType<typeof permitObservation>>
    }
  >()
  readonly #quarantines = new Map<
    string,
    PermissionPermitLifecycleSnapshot["quarantines"][number]
  >()
  readonly #now: () => Date
  readonly #maximum: number
  #conflictCount = 0
  #orphanEventCount = 0
  #revision = 0
  #disabled = false

  constructor(options: {
    now?: () => Date
    maximum?: number
  } = {}) {
    this.#now = options.now ?? (() => new Date())
    this.#maximum = Math.min(
      10_000,
      Math.max(16, Math.floor(options.maximum ?? 512)),
    )
  }

  issue(
    admission: PermissionReceiptAdmission,
  ): PermissionPermitProjection | undefined {
    this.#assertEnabled()
    if (!admission.permitId) return undefined
    const permitId = identifier(
      admission.permitId,
      "permission permit id",
    )
    const prior = this.#permits.get(permitId)
    if (prior) {
      if (
        prior.requestId !== admission.requestId
        || prior.responseId !== admission.responseId
        || prior.argumentsDigest !== admission.argumentsDigest
      ) {
        this.#conflictCount += 1
        this.#quarantine(
          permitId,
          undefined,
          "permission_permit_issue_conflict",
          "Canonical receipt reused a permit id with a different exact binding.",
        )
        throw permitError(
          "permission_permit_issue_conflict",
          `Permit ${permitId} was already issued for another binding.`,
        )
      }
      return freezePermit(prior)
    }
    const projection = freezePermit({
      permitId,
      requestId: admission.requestId,
      responseId: admission.responseId,
      argumentsDigest: admission.argumentsDigest,
      phase: "issued",
      issuedAt: admission.admittedAt,
      updatedAt: admission.admittedAt,
      eventIds: Object.freeze([]),
      exactBinding: true,
      replayRejected: false,
      canonical: true,
    })
    this.#permits.set(permitId, projection)
    this.#revision += 1
    for (const [eventId, pending] of this.#orphanObservations) {
      if (pending.observation.permitId !== permitId) continue
      this.#orphanObservations.delete(eventId)
      this.#applyObservation(pending.event, pending.observation)
    }
    this.#prune()
    return freezePermit(this.#permits.get(permitId) ?? projection)
  }

  ingest(
    events: readonly PermissionCanonicalEvent[],
  ): PermissionPermitLifecycleSnapshot {
    this.#assertEnabled()
    let changed = false
    for (const event of events) {
      const observation = permitObservation(event)
      if (!observation) continue
      const fingerprint = stablePermissionId(
        "permission_permit_event_fingerprint",
        event.eventType,
        event.taskId,
        event.runId,
        event.payload,
        event.entityRefs,
        event.summary,
      )
      const priorFingerprint = this.#eventFingerprints.get(event.eventId)
      if (priorFingerprint === fingerprint) continue
      if (priorFingerprint || this.#conflictingEventIds.has(event.eventId)) {
        if (!this.#conflictingEventIds.has(event.eventId)) {
          this.#conflictingEventIds.add(event.eventId)
          this.#conflictCount += 1
          this.#quarantine(
            observation.permitId,
            event.eventId,
            "permission_permit_event_identity_conflict",
            "A canonical event id was reused with different permit material.",
            event.createdAt,
          )
          const permit = this.#permits.get(observation.permitId)
          if (permit) {
            this.#permits.set(
              permit.permitId,
              freezePermit({
                ...permit,
                phase: "conflict",
                updatedAt: event.createdAt,
                terminalEventId: event.eventId,
                eventIds: appendUnique(permit.eventIds, event.eventId),
                exactBinding: false,
                replayRejected: true,
              }),
            )
          }
          changed = true
        }
        continue
      }
      this.#eventFingerprints.set(event.eventId, fingerprint)
      const prior = this.#permits.get(observation.permitId)
      if (!prior) {
        this.#orphanEventCount += 1
        this.#orphanObservations.set(event.eventId, {
          event,
          observation,
        })
        this.#quarantine(
          observation.permitId,
          event.eventId,
          "permission_permit_event_orphan",
          "Canonical permit event arrived before its verified issue receipt.",
          event.createdAt,
        )
        changed = true
        continue
      }
      this.#applyObservation(event, observation)
      changed = true
    }
    if (changed) {
      this.#revision += 1
      this.#prune()
    }
    return this.snapshot()
  }

  timelineEntries(): PermissionTimelineEntry[] {
    return this.permits().map((permit) =>
      Object.freeze({
        entryId: stablePermissionId(
          "permission_permit_timeline",
          permit.permitId,
          permit.phase,
          permit.updatedAt,
        ),
        requestId: permit.requestId,
        responseId: permit.responseId,
        kind: "permit",
        phase: permit.phase,
        title: permitTitle(permit.phase),
        detail: permitDetail(permit),
        createdAt: permit.updatedAt,
        severity: permitSeverity(permit.phase),
        sourceSurface: "tool",
        exactBinding: permit.exactBinding,
        canonical: permit.canonical,
        refs: Object.freeze({
          request_id: permit.requestId,
          response_id: permit.responseId,
          permit_id: permit.permitId,
          arguments_digest: permit.argumentsDigest,
          ...(permit.terminalEventId
            ? { event_id: permit.terminalEventId }
            : {}),
        }),
      }),
    )
  }

  permits(): PermissionPermitProjection[] {
    return [...this.#permits.values()]
      .sort(
        (left, right) =>
          comparePermissionTime(left.issuedAt, right.issuedAt)
          || left.permitId.localeCompare(right.permitId),
      )
      .map(freezePermit)
  }

  snapshot(): PermissionPermitLifecycleSnapshot {
    const permits = this.permits()
    return Object.freeze({
      schema: "zyra.permission-permit-lifecycle/v1",
      permits: Object.freeze(permits),
      quarantines: Object.freeze(
        [...this.#quarantines.values()]
          .sort(
            (left, right) =>
              comparePermissionTime(left.createdAt, right.createdAt)
              || left.quarantineId.localeCompare(right.quarantineId),
          )
          .map((value) => Object.freeze({ ...value })),
      ),
      issuedCount: permits.length,
      consumedCount: permits.filter(
        (permit) => permit.phase === "consumed",
      ).length,
      revokedCount: permits.filter(
        (permit) =>
          permit.phase === "revoked" || permit.phase === "expired",
      ).length,
      conflictCount: this.#conflictCount,
      orphanEventCount: this.#orphanEventCount,
      ownsPermitState: false,
      revision: this.#revision,
    })
  }

  disable(): void {
    this.#disabled = true
    this.#revision += 1
  }

  clear(): void {
    this.#permits.clear()
    this.#eventFingerprints.clear()
    this.#conflictingEventIds.clear()
    this.#orphanObservations.clear()
    this.#quarantines.clear()
    this.#conflictCount = 0
    this.#orphanEventCount = 0
    this.#disabled = false
    this.#revision += 1
  }

  #applyObservation(
    event: PermissionCanonicalEvent,
    observation: NonNullable<ReturnType<typeof permitObservation>>,
  ): void {
    const prior = this.#permits.get(observation.permitId)
    if (!prior) return
    if (
      observation.argumentsDigest
      && observation.argumentsDigest !== prior.argumentsDigest
    ) {
      this.#conflictCount += 1
      this.#permits.set(
        prior.permitId,
        freezePermit({
          ...prior,
          phase: "conflict",
          updatedAt: event.createdAt,
          terminalEventId: event.eventId,
          eventIds: appendUnique(prior.eventIds, event.eventId),
          exactBinding: false,
          replayRejected: true,
        }),
      )
      this.#quarantine(
        prior.permitId,
        event.eventId,
        "permission_permit_arguments_mismatch",
        "Permit event arguments digest does not match the issued exact call.",
        event.createdAt,
      )
      return
    }
    const nextPhase = transition(prior.phase, observation.phase)
    if (nextPhase === "conflict") {
      this.#conflictCount += 1
      this.#quarantine(
        prior.permitId,
        event.eventId,
        "permission_permit_transition_invalid",
        `Permit cannot transition from ${prior.phase} to ${observation.phase}.`,
        event.createdAt,
      )
    }
    const terminal = ["consumed", "revoked", "expired", "conflict"]
      .includes(nextPhase)
    this.#permits.set(
      prior.permitId,
      freezePermit({
        ...prior,
        phase: nextPhase,
        updatedAt: event.createdAt,
        claimedAt:
          observation.phase === "claimed"
            ? event.createdAt
            : prior.claimedAt,
        consumedAt:
          observation.phase === "consumed"
            ? event.createdAt
            : prior.consumedAt,
        terminalEventId: terminal ? event.eventId : prior.terminalEventId,
        eventIds: appendUnique(prior.eventIds, event.eventId),
        exactBinding: nextPhase !== "conflict",
        replayRejected:
          prior.replayRejected
          || observation.replayRejected
          || nextPhase === "conflict",
      }),
    )
  }

  #quarantine(
    permitId: string | undefined,
    eventId: string | undefined,
    code: string,
    message: string,
    createdAt = this.#now().toISOString(),
  ): void {
    const quarantineId = stablePermissionId(
      "permission_permit_quarantine",
      permitId,
      eventId,
      code,
    )
    this.#quarantines.set(
      quarantineId,
      Object.freeze({
        quarantineId,
        permitId,
        eventId,
        code,
        message,
        createdAt,
      }),
    )
  }

  #prune(): void {
    while (this.#permits.size > this.#maximum) {
      const terminal = this.permits().find((permit) =>
        ["consumed", "revoked", "expired", "conflict"].includes(
          permit.phase,
        ),
      )
      if (!terminal) break
      this.#permits.delete(terminal.permitId)
    }
    while (this.#quarantines.size > this.#maximum) {
      const oldest = [...this.#quarantines.values()]
        .sort(
          (left, right) =>
            comparePermissionTime(left.createdAt, right.createdAt)
            || left.quarantineId.localeCompare(right.quarantineId),
        )[0]
      if (!oldest) break
      this.#quarantines.delete(oldest.quarantineId)
    }
  }

  #assertEnabled(): void {
    if (this.#disabled) {
      throw permitError(
        "permission_permit_lifecycle_disabled",
        "Permission permit lifecycle projection is disabled.",
      )
    }
  }
}

function permitObservation(
  event: PermissionCanonicalEvent,
): {
  permitId: string
  phase: PermissionPermitPhase
  argumentsDigest?: string
  replayRejected: boolean
} | undefined {
  const records = flatRecords(event.payload ?? {}, 4)
  const permitRef = (event.entityRefs ?? [])
    .map((value) => String(value))
    .find((value) => /^permit[:/]/i.test(value))
  const permitIdValue =
    firstText(records, ["permit_id", "permission_permit_id"])
    || permitRef?.replace(/^permit[:/]/i, "")
  if (!permitIdValue) return undefined
  let permitId: string
  try {
    permitId = identifier(permitIdValue, "permission permit id")
  } catch {
    return undefined
  }
  const material = [
    event.eventType,
    firstText(records, ["phase", "status", "result", "reason_code"]),
    event.summary ?? "",
  ].join(" ").toLowerCase()
  const phase =
    /replay|already.?consum|claim.?false/.test(material)
      ? "consumed"
      : /revok|fence|cancel/.test(material)
        ? "revoked"
        : /expir/.test(material)
          ? "expired"
          : /consum|execut|complete/.test(material)
            ? "consumed"
            : /claim|admit/.test(material)
              ? "claimed"
              : undefined
  if (!phase) return undefined
  const digest = firstText(records, [
    "final_arguments_digest",
    "arguments_digest",
  ])
  return {
    permitId,
    phase,
    argumentsDigest:
      /^[0-9a-f]{64}$/i.test(digest) ? digest.toLowerCase() : undefined,
    replayRejected: /replay|already.?consum|claim.?false/.test(material),
  }
}

function transition(
  prior: PermissionPermitPhase,
  requested: PermissionPermitPhase,
): PermissionPermitPhase {
  if (prior === requested) return prior
  const allowed: Record<PermissionPermitPhase, readonly PermissionPermitPhase[]> = {
    issued: ["claimed", "consumed", "revoked", "expired"],
    claimed: ["consumed", "revoked", "expired"],
    consumed: [],
    revoked: [],
    expired: [],
    conflict: [],
  }
  return allowed[prior].includes(requested) ? requested : "conflict"
}

function flatRecords(
  value: unknown,
  maximumDepth: number,
): Record<string, unknown>[] {
  const output: Record<string, unknown>[] = []
  const queue: Array<{ value: unknown; depth: number }> = [
    { value, depth: 0 },
  ]
  while (queue.length && output.length < 256) {
    const selected = queue.shift()!
    if (
      !selected.value
      || typeof selected.value !== "object"
      || Array.isArray(selected.value)
    ) {
      continue
    }
    const record = selected.value as Record<string, unknown>
    output.push(record)
    if (selected.depth >= maximumDepth) continue
    for (const [key, child] of Object.entries(record)) {
      if (
        child
        && typeof child === "object"
        && !Array.isArray(child)
        && !/token|secret|password|authorization|cookie/i.test(key)
      ) {
        queue.push({ value: child, depth: selected.depth + 1 })
      }
    }
  }
  return output
}

function firstText(
  records: readonly Record<string, unknown>[],
  keys: readonly string[],
): string {
  for (const record of records) {
    for (const key of keys) {
      const value = record[key]
      if (typeof value === "string" && value.trim()) {
        return value.trim().slice(0, 512)
      }
    }
  }
  return ""
}

function appendUnique(
  values: readonly string[],
  value: string,
): readonly string[] {
  return Object.freeze(
    values.includes(value) ? [...values] : [...values, value],
  )
}

function permitTitle(phase: PermissionPermitPhase): string {
  return {
    issued: "Exact-call permit issued",
    claimed: "Exact-call permit claimed",
    consumed: "Exact-call permit consumed",
    revoked: "Exact-call permit revoked",
    expired: "Exact-call permit expired",
    conflict: "Permit binding conflict",
  }[phase]
}

function permitDetail(permit: PermissionPermitProjection): string {
  if (permit.phase === "consumed" && permit.replayRejected) {
    return "The exact permit was consumed; a later replay was rejected."
  }
  if (permit.phase === "conflict") {
    return "Canonical event identity did not match the issued permit binding."
  }
  return `Permit ${permit.permitId} is ${permit.phase} for arguments ${permit.argumentsDigest.slice(0, 12)}….`
}

function permitSeverity(
  phase: PermissionPermitPhase,
): PermissionWarningSeverity {
  if (phase === "conflict") return "danger"
  if (phase === "revoked" || phase === "expired") return "warning"
  return "info"
}

function freezePermit(
  value: PermissionPermitProjection,
): PermissionPermitProjection {
  return Object.freeze({
    ...value,
    eventIds: Object.freeze([...value.eventIds]),
  })
}

function permitError(code: string, message: string): Error {
  return Object.assign(new Error(message), {
    name: "PermissionPermitLifecycleError",
    code,
  })
}
