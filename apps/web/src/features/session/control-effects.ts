import type { CanonicalProjectionState } from "../../state/contracts.ts"
import { buildMemoryConsoleProjection } from "../memory/projection.ts"
import { buildProviderConsoleProjection } from "../providers/projection.ts"
import { buildSessionConsoleProjection } from "./projection.ts"
import {
  compareNumber,
  compareText,
  fingerprint,
  sortStable,
  text,
  unique,
} from "./value.ts"

export type ConsoleControlName =
  | "/context"
  | "/compact"
  | "/memory"
  | "/model"
  | "/resume"
  | "/rewind"
  | "/export"

export type ControlEffectKind =
  | "receipt"
  | "context"
  | "memory"
  | "route"
  | "checkpoint"
  | "artifact"

export interface ControlEffectBaseline {
  id: string
  taskId: string
  sessionId: string
  revision: number
  contextTokens: number
  contextFingerprint?: string
  compactEpoch: number
  compactCount: number
  checkpointId?: string
  restoredCheckpointId?: string
  memoryFingerprint: string
  memoryIds: readonly string[]
  routeId?: string
  placementId?: string
  providerId?: string
  modelId?: string
  artifactIds: readonly string[]
  eventCount: number
}

export interface ControlEffectVerification {
  id: string
  operationId: string
  name: ConsoleControlName
  commandText: string
  expected: ControlEffectKind
  satisfied: boolean
  pending: boolean
  baselineRevision: number
  observedRevision: number
  changedFields: readonly string[]
  evidenceEventIds: readonly string[]
  evidenceArtifactIds: readonly string[]
  reasons: readonly string[]
  checkedAt: string
}

interface TrackedControl {
  operationId: string
  name: ConsoleControlName
  commandText: string
  baseline: ControlEffectBaseline
  createdAt: string
}

export class ControlEffectLedger {
  readonly #tracked = new Map<string, TrackedControl>()
  readonly #verifications = new Map<string, ControlEffectVerification>()
  #enabled = true
  #disabledReason = "Control effect ledger is disabled."

  begin(input: {
    operationId: string
    name: ConsoleControlName
    commandText?: string
    state: CanonicalProjectionState
    taskId: string
    sessionId: string
    createdAt?: string
  }): ControlEffectBaseline {
    this.#assertEnabled()
    const baseline = captureControlEffectBaseline(
      input.state,
      input.taskId,
      input.sessionId,
    )
    const tracked: TrackedControl = Object.freeze({
      operationId: input.operationId,
      name: input.name,
      commandText: text(input.commandText),
      baseline,
      createdAt: input.createdAt ?? new Date().toISOString(),
    })
    const existing = this.#tracked.get(input.operationId)
    if (existing && existing.baseline.id !== baseline.id) {
      throw new Error("Control operation identity was reused with another baseline.")
    }
    this.#tracked.set(input.operationId, existing ?? tracked)
    return existing?.baseline ?? baseline
  }

  verify(input: {
    operationId: string
    state: CanonicalProjectionState
    commandId?: string
    checkpointId?: string
    receiptEventIds?: readonly string[]
    checkedAt?: string
  }): ControlEffectVerification {
    this.#assertEnabled()
    const tracked = this.#tracked.get(input.operationId)
    if (!tracked) throw new Error("Control effect baseline does not exist.")
    const verification = evaluateControlEffect({
      operationId: input.operationId,
      name: tracked.name,
      commandText: tracked.commandText,
      baseline: tracked.baseline,
      state: input.state,
      commandId: input.commandId,
      checkpointId: input.checkpointId,
      receiptEventIds: input.receiptEventIds,
      checkedAt: input.checkedAt,
    })
    this.#verifications.set(input.operationId, verification)
    return verification
  }

  get(operationId: string): ControlEffectVerification | undefined {
    this.#assertEnabled()
    return this.#verifications.get(operationId)
  }

  list(): readonly ControlEffectVerification[] {
    this.#assertEnabled()
    return Object.freeze(sortStable(
      [...this.#verifications.values()],
      (left, right) =>
        compareText(right.checkedAt, left.checkedAt) ||
        compareText(left.operationId, right.operationId),
    ))
  }

  remove(operationId: string): boolean {
    this.#assertEnabled()
    this.#verifications.delete(operationId)
    return this.#tracked.delete(operationId)
  }

  clear(): void {
    this.#assertEnabled()
    this.#tracked.clear()
    this.#verifications.clear()
  }

  disable(reason = "Control effect ledger is disabled."): void {
    this.#enabled = false
    this.#disabledReason = text(reason, "Control effect ledger is disabled.")
  }

  enable(): void {
    this.#enabled = true
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

export function captureControlEffectBaseline(
  state: CanonicalProjectionState,
  taskId: string,
  sessionId: string,
): ControlEffectBaseline {
  const session = buildSessionConsoleProjection(state, taskId, sessionId)
  const memory = buildMemoryConsoleProjection(state, taskId)
  const provider = buildProviderConsoleProjection(state, taskId)
  const active = state.sessions[session.activeSessionId ?? sessionId]
  const selectedScheduler = Object.values(state.schedulers).find((scheduler) =>
    scheduler.taskId === taskId &&
    scheduler.routeId === provider.selectedRoute?.routeId)
  const artifactIds = Object.values(state.artifacts)
    .filter((artifact) => artifact.taskId === taskId && !artifact.deleted)
    .map((artifact) => artifact.id)
    .sort()
  const memoryIds = memory.rows.map((row) => row.id).sort()
  const compactEpoch = session.context?.compactEpoch ?? 0
  const compactCount = active?.compactCount ?? 0
  const baseline = {
    taskId,
    sessionId: session.activeSessionId ?? sessionId,
    revision: state.revision,
    contextTokens: session.context?.used ?? 0,
    contextFingerprint: session.context
      ? fingerprint([session.context])
      : undefined,
    compactEpoch,
    compactCount,
    checkpointId: active?.checkpointId,
    restoredCheckpointId: active?.restoredCheckpointId,
    memoryFingerprint: memory.fingerprint,
    memoryIds,
    routeId: provider.selectedRoute?.routeId,
    placementId: selectedScheduler?.placementId,
    providerId: provider.selectedRoute?.providerId,
    modelId: provider.selectedRoute?.modelId,
    artifactIds,
    eventCount: state.causality.eventOrder.length,
  }
  return Object.freeze({
    id: fingerprint([baseline]),
    ...baseline,
    memoryIds: Object.freeze(memoryIds),
    artifactIds: Object.freeze(artifactIds),
  })
}

export function evaluateControlEffect(input: {
  operationId: string
  name: ConsoleControlName
  commandText?: string
  baseline: ControlEffectBaseline
  state: CanonicalProjectionState
  commandId?: string
  checkpointId?: string
  receiptEventIds?: readonly string[]
  checkedAt?: string
}): ControlEffectVerification {
  const commandText = text(input.commandText)
  const expected = expectedEffect(input.name, commandText)
  const current = captureControlEffectBaseline(
    input.state,
    input.baseline.taskId,
    input.baseline.sessionId,
  )
  const changedFields = changedControlFields(input.baseline, current)
  const eventIds = evidenceEvents(input.state, {
    commandId: input.commandId,
    checkpointId: input.checkpointId,
    receiptEventIds: input.receiptEventIds,
    baselineEventCount: input.baseline.eventCount,
    name: input.name,
  })
  const evidenceArtifactIds = unique([
    ...current.artifactIds.filter((id) => !input.baseline.artifactIds.includes(id)),
    ...eventIds.flatMap((eventId) =>
      input.state.causality.byEvent[eventId]?.artifactIds ?? []),
  ])
  const revisionAdvanced = current.revision > input.baseline.revision
  const commandObserved = !input.commandId ||
    Boolean(input.state.commands[input.commandId]) ||
    Boolean(input.state.causality.byControlCommand[input.commandId]?.length)
  const effectObserved = effectSatisfied(
    expected,
    changedFields,
    eventIds,
    evidenceArtifactIds,
  )
  const satisfied =
    revisionAdvanced &&
    commandObserved &&
    effectObserved
  const reasons: string[] = []
  if (!revisionAdvanced) reasons.push("canonical projection revision has not advanced")
  if (!commandObserved) reasons.push("control command is absent from canonical projection")
  if (!effectObserved) reasons.push(effectPendingReason(expected))
  if (satisfied) reasons.push(`${expected} effect observed in canonical projection`)
  const checkedAt = input.checkedAt ?? new Date().toISOString()
  return Object.freeze({
    id: fingerprint([
      input.operationId,
      expected,
      input.baseline.id,
      current.id,
      eventIds,
      evidenceArtifactIds,
    ]),
    operationId: input.operationId,
    name: input.name,
    commandText,
    expected,
    satisfied,
    pending: !satisfied,
    baselineRevision: input.baseline.revision,
    observedRevision: current.revision,
    changedFields: Object.freeze(changedFields),
    evidenceEventIds: Object.freeze(eventIds),
    evidenceArtifactIds: Object.freeze(evidenceArtifactIds),
    reasons: Object.freeze(reasons),
    checkedAt,
  })
}

function expectedEffect(name: ConsoleControlName, commandText: string): ControlEffectKind {
  const normalized = commandText.trim().toLowerCase()
  if (name === "/compact" && /\bexecute\b/.test(normalized)) return "context"
  if (name === "/memory" && /\b(curate|accept|reject|recover)\b/.test(normalized)) return "memory"
  if (name === "/model" && /\b(select|failover)\b/.test(normalized)) return "route"
  if (name === "/resume" || name === "/rewind") return "checkpoint"
  if (name === "/export") return "artifact"
  return "receipt"
}

function changedControlFields(
  baseline: ControlEffectBaseline,
  current: ControlEffectBaseline,
): string[] {
  const changed: string[] = []
  if (baseline.contextTokens !== current.contextTokens) changed.push("context.tokens")
  if (baseline.contextFingerprint !== current.contextFingerprint) changed.push("context.fingerprint")
  if (baseline.compactEpoch !== current.compactEpoch) changed.push("context.compact_epoch")
  if (baseline.compactCount !== current.compactCount) changed.push("session.compact_count")
  if (baseline.checkpointId !== current.checkpointId) changed.push("session.checkpoint")
  if (baseline.restoredCheckpointId !== current.restoredCheckpointId) changed.push("session.restored_checkpoint")
  if (baseline.memoryFingerprint !== current.memoryFingerprint) changed.push("memory.fingerprint")
  if (baseline.memoryIds.join("\u001f") !== current.memoryIds.join("\u001f")) changed.push("memory.identities")
  if (baseline.routeId !== current.routeId) changed.push("route.id")
  if (baseline.placementId !== current.placementId) changed.push("route.placement")
  if (baseline.providerId !== current.providerId) changed.push("route.provider")
  if (baseline.modelId !== current.modelId) changed.push("route.model")
  if (baseline.artifactIds.join("\u001f") !== current.artifactIds.join("\u001f")) changed.push("artifact.identities")
  return changed
}

function evidenceEvents(
  state: CanonicalProjectionState,
  input: {
    commandId?: string
    checkpointId?: string
    receiptEventIds?: readonly string[]
    baselineEventCount: number
    name: ConsoleControlName
  },
): string[] {
  const ids = new Set(input.receiptEventIds ?? [])
  if (input.commandId) {
    for (const eventId of state.causality.byControlCommand[input.commandId] ?? []) ids.add(eventId)
  }
  if (input.checkpointId) {
    for (const eventId of state.causality.byCheckpoint[input.checkpointId] ?? []) ids.add(eventId)
  }
  for (const eventId of state.causality.eventOrder.slice(input.baselineEventCount)) {
    const event = state.causality.byEvent[eventId]
    if (!event || !eventMatchesControl(event.eventType, input.name)) continue
    ids.add(eventId)
  }
  return [...ids]
    .filter((eventId) => Boolean(state.causality.byEvent[eventId]))
    .sort((left, right) =>
      compareNumber(
        state.causality.byEvent[left]?.sequence,
        state.causality.byEvent[right]?.sequence,
      ) || compareText(left, right))
}

function eventMatchesControl(eventType: string, name: ConsoleControlName): boolean {
  const normalized = eventType.toLowerCase()
  if (name === "/compact") return /compact|context/.test(normalized)
  if (name === "/memory") return /memory|curat/.test(normalized)
  if (name === "/model") return /provider|model|route|dispatch|failover/.test(normalized)
  if (name === "/resume" || name === "/rewind") return /checkpoint|resume|rewind|restore/.test(normalized)
  if (name === "/export") return /artifact|export/.test(normalized)
  return /command|context/.test(normalized)
}

function effectSatisfied(
  expected: ControlEffectKind,
  changedFields: readonly string[],
  eventIds: readonly string[],
  artifactIds: readonly string[],
): boolean {
  if (expected === "receipt") return eventIds.length > 0
  if (expected === "artifact") return artifactIds.length > 0 || eventIds.length > 0
  if (expected === "context") {
    return changedFields.some((field) => field.startsWith("context.") || field === "session.compact_count")
  }
  if (expected === "memory") return changedFields.some((field) => field.startsWith("memory."))
  if (expected === "route") return changedFields.some((field) => field.startsWith("route."))
  return changedFields.some((field) =>
    field === "session.checkpoint" || field === "session.restored_checkpoint")
}

function effectPendingReason(expected: ControlEffectKind): string {
  if (expected === "context") return "later context projection has not changed"
  if (expected === "memory") return "later memory selection projection has not changed"
  if (expected === "route") return "later provider or placement route has not changed"
  if (expected === "checkpoint") return "later checkpoint restore projection has not changed"
  if (expected === "artifact") return "export artifact or event has not appeared"
  return "canonical receipt event has not appeared"
}
