import type { SessionConsoleProjection } from "./projection.ts"
import type { SessionControlOperation } from "./runtime.ts"
import {
  bounded,
  compareNumber,
  compareText,
  fingerprint,
  sortStable,
  text,
  unique,
} from "./value.ts"

export interface ReconnectFrame {
  id: string
  taskId: string
  sessionId?: string
  generation: number
  revision: number
  committedSequence: number
  connected: boolean
  ready: boolean
  checkpointIds: readonly string[]
  commandIds: readonly string[]
  capturedAt: string
  source: "live" | "snapshot" | "replay" | "unknown"
}

export interface ReconnectConflict {
  id: string
  code:
    | "task-changed"
    | "session-changed"
    | "revision-regressed"
    | "sequence-regressed"
    | "checkpoint-lost"
    | "command-lost"
    | "operation-stale"
    | "projection-incomplete"
  severity: "warning" | "error"
  message: string
  before?: string | number
  after?: string | number
  operationId?: string
}

export interface ReconnectSettlement {
  id: string
  before?: ReconnectFrame
  after: ReconnectFrame
  restored: boolean
  safe: boolean
  conflicts: readonly ReconnectConflict[]
  resumedOperationIds: readonly string[]
  failedOperationIds: readonly string[]
  retainedCheckpointIds: readonly string[]
  lostCheckpointIds: readonly string[]
  addedCheckpointIds: readonly string[]
  retainedCommandIds: readonly string[]
  lostCommandIds: readonly string[]
  addedCommandIds: readonly string[]
  generationDelta: number
  revisionDelta: number
  sequenceDelta: number
}

export class SessionReconnectSupervisor {
  readonly #frames = new Map<string, ReconnectFrame[]>()
  readonly #settlements: ReconnectSettlement[] = []
  #enabled = true
  #disabledReason = "Session reconnect supervisor is disabled."

  capture(
    projection: SessionConsoleProjection,
    input: {
      generation?: number
      capturedAt?: string
      source?: ReconnectFrame["source"]
    } = {},
  ): ReconnectFrame {
    this.#assertEnabled()
    const frame: ReconnectFrame = Object.freeze({
      id: fingerprint([
        projection.taskId,
        projection.activeSessionId,
        input.generation,
        projection.revision,
        projection.committedSequence,
        input.capturedAt,
      ]),
      taskId: projection.taskId,
      sessionId: projection.activeSessionId,
      generation: bounded(Math.floor(input.generation ?? 0), 0, Number.MAX_SAFE_INTEGER),
      revision: projection.revision,
      committedSequence: projection.committedSequence,
      connected: projection.connected,
      ready: projection.ready,
      checkpointIds: Object.freeze(projection.checkpoints.map((checkpoint) => checkpoint.id).sort()),
      commandIds: Object.freeze(projection.latestCommands.map((command) => command.id).sort()),
      capturedAt: input.capturedAt ?? new Date().toISOString(),
      source: input.source ?? (projection.connected ? "live" : "snapshot"),
    })
    const key = frame.taskId
    const frames = this.#frames.get(key) ?? []
    const previous = frames.at(-1)
    if (previous && frame.id === previous.id) return previous
    frames.push(frame)
    this.#frames.set(key, frames.slice(-500))
    return frame
  }

  settle(
    before: ReconnectFrame | undefined,
    after: ReconnectFrame,
    operations: readonly SessionControlOperation[] = [],
  ): ReconnectSettlement {
    this.#assertEnabled()
    const conflicts: ReconnectConflict[] = []
    if (!after.ready) {
      conflicts.push(conflict("projection-incomplete", "error", "Restored projection is incomplete."))
    }
    if (before && before.taskId !== after.taskId) {
      conflicts.push(conflict("task-changed", "error", "Task identity changed across reconnect.", before.taskId, after.taskId))
    }
    if (before?.sessionId && after.sessionId && before.sessionId !== after.sessionId) {
      conflicts.push(conflict("session-changed", "error", "Session identity changed across reconnect.", before.sessionId, after.sessionId))
    }
    if (before && after.revision < before.revision) {
      conflicts.push(conflict("revision-regressed", "error", "Canonical revision regressed.", before.revision, after.revision))
    }
    if (before && after.committedSequence < before.committedSequence) {
      conflicts.push(conflict("sequence-regressed", "error", "Committed sequence regressed.", before.committedSequence, after.committedSequence))
    }
    const beforeCheckpoints = new Set(before?.checkpointIds ?? [])
    const afterCheckpoints = new Set(after.checkpointIds)
    const lostCheckpointIds = [...beforeCheckpoints].filter((id) => !afterCheckpoints.has(id))
    const addedCheckpointIds = [...afterCheckpoints].filter((id) => !beforeCheckpoints.has(id))
    const retainedCheckpointIds = [...afterCheckpoints].filter((id) => beforeCheckpoints.has(id))
    for (const id of lostCheckpointIds) {
      conflicts.push(conflict("checkpoint-lost", "warning", `Checkpoint ${id} is absent after reconnect.`, id, undefined))
    }
    const beforeCommands = new Set(before?.commandIds ?? [])
    const afterCommands = new Set(after.commandIds)
    const lostCommandIds = [...beforeCommands].filter((id) => !afterCommands.has(id))
    const addedCommandIds = [...afterCommands].filter((id) => !beforeCommands.has(id))
    const retainedCommandIds = [...afterCommands].filter((id) => beforeCommands.has(id))
    for (const id of lostCommandIds) {
      conflicts.push(conflict("command-lost", "warning", `Command ${id} is absent after reconnect.`, id, undefined))
    }
    const resumedOperationIds: string[] = []
    const failedOperationIds: string[] = []
    for (const operation of operations) {
      if (["committed", "failed", "disabled"].includes(operation.phase)) continue
      const commandObserved = !operation.commandId || afterCommands.has(operation.commandId)
      const revisionAdvanced = after.revision > operation.startedRevision
      if (commandObserved && revisionAdvanced) {
        resumedOperationIds.push(operation.id)
      } else {
        failedOperationIds.push(operation.id)
        conflicts.push({
          ...conflict(
            "operation-stale",
            "warning",
            `Operation ${operation.id} cannot be reconciled after reconnect.`,
            operation.startedRevision,
            after.revision,
          ),
          operationId: operation.id,
        })
      }
    }
    const settlement: ReconnectSettlement = Object.freeze({
      id: fingerprint([before?.id, after.id, resumedOperationIds, failedOperationIds]),
      before,
      after,
      restored: after.ready && after.connected,
      safe: !conflicts.some((entry) => entry.severity === "error"),
      conflicts: Object.freeze(sortStable(conflicts, (left, right) =>
        severityRank(right.severity) - severityRank(left.severity) ||
        compareText(left.code, right.code) ||
        compareText(left.id, right.id))),
      resumedOperationIds: Object.freeze(resumedOperationIds),
      failedOperationIds: Object.freeze(failedOperationIds),
      retainedCheckpointIds: Object.freeze(retainedCheckpointIds),
      lostCheckpointIds: Object.freeze(lostCheckpointIds),
      addedCheckpointIds: Object.freeze(addedCheckpointIds),
      retainedCommandIds: Object.freeze(retainedCommandIds),
      lostCommandIds: Object.freeze(lostCommandIds),
      addedCommandIds: Object.freeze(addedCommandIds),
      generationDelta: after.generation - (before?.generation ?? 0),
      revisionDelta: after.revision - (before?.revision ?? 0),
      sequenceDelta: after.committedSequence - (before?.committedSequence ?? 0),
    })
    this.#settlements.push(settlement)
    if (this.#settlements.length > 1000) {
      this.#settlements.splice(0, this.#settlements.length - 1000)
    }
    return settlement
  }

  latestFrame(taskId: string): ReconnectFrame | undefined {
    this.#assertEnabled()
    return this.#frames.get(taskId)?.at(-1)
  }

  frames(taskId: string): readonly ReconnectFrame[] {
    this.#assertEnabled()
    return Object.freeze([...(this.#frames.get(taskId) ?? [])])
  }

  settlements(taskId?: string): readonly ReconnectSettlement[] {
    this.#assertEnabled()
    return Object.freeze(this.#settlements.filter((settlement) =>
      !taskId || settlement.after.taskId === taskId))
  }

  unresolved(taskId?: string): readonly ReconnectConflict[] {
    this.#assertEnabled()
    return Object.freeze(unique(
      this.settlements(taskId)
        .flatMap((settlement) => settlement.conflicts)
        .map((entry) => entry.id),
    ).map((id) =>
      this.settlements(taskId)
        .flatMap((settlement) => settlement.conflicts)
        .find((entry) => entry.id === id)!)
      .filter(Boolean))
  }

  disable(reason = "Session reconnect supervisor is disabled."): void {
    this.#enabled = false
    this.#disabledReason = text(reason, "Session reconnect supervisor is disabled.")
  }

  enable(): void {
    this.#enabled = true
  }

  clear(taskId?: string): void {
    this.#assertEnabled()
    if (taskId) this.#frames.delete(taskId)
    else this.#frames.clear()
    if (!taskId) this.#settlements.splice(0)
    else {
      for (let index = this.#settlements.length - 1; index >= 0; index -= 1) {
        if (this.#settlements[index]?.after.taskId === taskId) this.#settlements.splice(index, 1)
      }
    }
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

function conflict(
  code: ReconnectConflict["code"],
  severity: ReconnectConflict["severity"],
  message: string,
  before?: string | number,
  after?: string | number,
): ReconnectConflict {
  return Object.freeze({
    id: fingerprint([code, message, before, after]),
    code,
    severity,
    message,
    before,
    after,
  })
}

function severityRank(value: ReconnectConflict["severity"]): number {
  return ["warning", "error"].indexOf(value)
}
