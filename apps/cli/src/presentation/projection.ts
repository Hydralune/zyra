import type { TaskProjection } from "@zyra/typed-api-client"
import type { IngressFrame } from "../api.ts"
import { CliTaskError } from "../contracts.ts"
import type { UiPermissionSnapshot, UiTransportSnapshot, UiUserInputRequest, ZyraUiEvent } from "./events.ts"
import { projectProductEvents } from "./projector.ts"

export interface ProductProjectionSnapshot {
  taskId: string
  runId: string
  generation: number
  revision: string
  lastSequence: number
  cursor?: string
  frameCount: number
  retainedFrameCount: number
  connection: "connecting" | "connected" | "reconnecting" | "disconnected" | "complete"
  events: readonly ZyraUiEvent[]
}

const MAX_RETAINED_FRAMES = 20_000
const MAX_RETAINED_LIVE_FRAMES = 4_096

function terminal(task: TaskProjection): boolean {
  return task.terminal || ["completed", "failed", "blocked", "cancelled", "killed"].includes(task.status)
}

function assistantIdentity(frame: IngressFrame): string | undefined {
  const value = frame.presentation
  if (!value || value.schema !== "zyra.product-presentation/v1" || value.kind !== "assistant") return undefined
  return typeof value.identity === "string" && value.identity.trim()
    ? value.identity.trim().slice(0, 256)
    : undefined
}

function validateTask(prior: TaskProjection, next: TaskProjection): void {
  if (prior.taskId !== next.taskId || prior.runId !== next.runId) {
    throw new CliTaskError("Product projection rejected a cross-task canonical refresh.", "contract_projection_binding_invalid", {
      expected_task_id: prior.taskId,
      actual_task_id: next.taskId,
      expected_run_id: prior.runId,
      actual_run_id: next.runId,
    })
  }
}

function orderedSnapshotFrames(frames: readonly IngressFrame[], taskId: string, generation: number): IngressFrame[] {
  const ordered = [...frames].sort((left, right) => left.sequence - right.sequence || left.eventId.localeCompare(right.eventId))
  const result: IngressFrame[] = []
  const identities = new Set<string>()
  let prior: IngressFrame | undefined
  for (const frame of ordered) {
    if (frame.taskId !== taskId || frame.generation !== generation) {
      throw new CliTaskError("Product snapshot contains a cross-binding frame.", "contract_projection_binding_invalid")
    }
    const identity = `${frame.sequence}\u0000${frame.eventId}`
    if (identities.has(identity)) continue
    if (prior && (frame.sequence <= prior.sequence || frame.previousSequence !== prior.sequence)) {
      throw new CliTaskError("Product snapshot contains an event gap or conflicting duplicate.", "contract_event_gap", {
        previous_sequence: prior.sequence,
        frame_previous_sequence: frame.previousSequence,
        sequence: frame.sequence,
      })
    }
    identities.add(identity)
    result.push(frame)
    prior = frame
  }
  return result
}

export class ProductProjection {
  #task: TaskProjection
  #generation: number
  #frames: IngressFrame[] = []
  #liveFrames: IngressFrame[] = []
  #liveEventIds = new Set<string>()
  #bySequence = new Map<number, string>()
  #frameCount = 0
  #lastSequence = 0
  #permissions: readonly UiPermissionSnapshot[] = Object.freeze([])
  #userInputs: readonly UiUserInputRequest[] = Object.freeze([])
  #transport: UiTransportSnapshot | undefined
  #connection: ProductProjectionSnapshot["connection"] = "connecting"
  #cursor: string | undefined

  constructor(input: {
    task: TaskProjection
    generation: number
    frames?: readonly IngressFrame[]
    cursor?: string
    permissions?: readonly UiPermissionSnapshot[]
    userInputs?: readonly UiUserInputRequest[]
  }) {
    this.#task = input.task
    this.#generation = input.generation
    this.replaceSnapshot({
      task: input.task,
      generation: input.generation,
      frames: input.frames ?? [],
      cursor: input.cursor,
      permissions: input.permissions,
      userInputs: input.userInputs,
    })
  }

  apply(frame: IngressFrame): boolean {
    if (frame.taskId !== this.#task.taskId || frame.generation !== this.#generation) {
      throw new CliTaskError("Product event does not match the active task generation.", "contract_projection_binding_invalid", {
        task_id: frame.taskId,
        generation: frame.generation,
      })
    }
    const priorId = this.#bySequence.get(frame.sequence)
    if (priorId === frame.eventId) {
      if (frame.cursor) this.#cursor = frame.cursor
      return false
    }
    const last = this.#frames.at(-1)
    if (priorId || (last && (frame.sequence <= last.sequence || frame.previousSequence !== last.sequence))) {
      throw new CliTaskError("Product event stream is reordered, conflicting, or has a gap.", "contract_event_order_invalid", {
        revision: this.revision,
        sequence: frame.sequence,
        previous_sequence: frame.previousSequence,
      })
    }
    this.#frames.push(frame)
    this.#bySequence.set(frame.sequence, frame.eventId)
    this.#frameCount += 1
    this.#lastSequence = frame.sequence
    while (this.#frames.length > MAX_RETAINED_FRAMES) {
      const evicted = this.#frames.shift()
      if (evicted) this.#bySequence.delete(evicted.sequence)
    }
    if (frame.cursor) this.#cursor = frame.cursor
    if (frame.eventType === "runtime.text.ended") {
      const identity = assistantIdentity(frame)
      if (identity) {
        this.#liveFrames = this.#liveFrames.filter((item) => assistantIdentity(item) !== identity)
        this.#liveEventIds = new Set(this.#liveFrames.map((item) => item.eventId))
      }
    }
    return true
  }

  applyLive(frame: IngressFrame): boolean {
    if (frame.taskId !== this.#task.taskId || frame.generation !== this.#generation) {
      throw new CliTaskError("Live product event does not match the active task generation.", "contract_projection_binding_invalid")
    }
    if (frame.liveSequence === undefined || this.#liveEventIds.has(frame.eventId)) return false
    this.#liveFrames.push(frame)
    this.#liveEventIds.add(frame.eventId)
    this.#frameCount += 1
    while (this.#liveFrames.length > MAX_RETAINED_LIVE_FRAMES) {
      const evicted = this.#liveFrames.shift()
      if (evicted) this.#liveEventIds.delete(evicted.eventId)
    }
    return true
  }

  replaceSnapshot(input: {
    task: TaskProjection
    generation: number
    frames: readonly IngressFrame[]
    cursor?: string
    permissions?: readonly UiPermissionSnapshot[]
    userInputs?: readonly UiUserInputRequest[]
  }): void {
    validateTask(this.#task, input.task)
    const frames = orderedSnapshotFrames(input.frames, input.task.taskId, input.generation)
    this.#task = input.task
    this.#generation = input.generation
    this.#frames = frames
    this.#liveFrames = []
    this.#liveEventIds.clear()
    this.#bySequence = new Map(frames.map((frame) => [frame.sequence, frame.eventId]))
    this.#frameCount = frames.length
    this.#lastSequence = frames.at(-1)?.sequence ?? 0
    if (this.#frames.length > MAX_RETAINED_FRAMES) {
      this.#frames = this.#frames.slice(-MAX_RETAINED_FRAMES)
      this.#bySequence = new Map(this.#frames.map((frame) => [frame.sequence, frame.eventId]))
    }
    this.#cursor = input.cursor ?? frames.at(-1)?.cursor
    if (input.permissions) this.permissions(input.permissions)
    if (input.userInputs) this.userInputs(input.userInputs)
    this.#connection = terminal(input.task) ? "complete" : "connecting"
    this.#transport = undefined
  }

  refreshTask(task: TaskProjection): void {
    validateTask(this.#task, task)
    this.#task = task
    if (terminal(task)) this.#connection = "complete"
  }

  permissions(permissions: readonly UiPermissionSnapshot[]): void {
    const seen = new Set<string>()
    this.#permissions = Object.freeze([...permissions]
      .sort((left, right) => left.requestId.localeCompare(right.requestId))
      .filter((permission) => {
        if (seen.has(permission.requestId)) return false
        seen.add(permission.requestId)
        return true
      })
      .map((permission) => Object.freeze({ ...permission })))
  }

  userInputs(requests: readonly UiUserInputRequest[]): void {
    const seen = new Set<string>()
    this.#userInputs = Object.freeze([...requests]
      .sort((left, right) => left.requestId.localeCompare(right.requestId))
      .filter((request) => {
        if (seen.has(request.requestId)) return false
        seen.add(request.requestId)
        return true
      })
      .map((request) => Object.freeze({ ...request })))
  }

  connected(): void {
    this.#connection = terminal(this.#task) ? "complete" : "connected"
    this.#transport = { state: "connected" }
  }

  reconnecting(attempt: number): void {
    this.#connection = "reconnecting"
    this.#transport = { state: "reconnecting", attempt: Math.max(1, Math.floor(attempt)) }
  }

  disconnected(): void {
    this.#connection = "disconnected"
    this.#transport = { state: "disconnected" }
  }

  complete(): void {
    this.#connection = "complete"
    this.#transport = undefined
  }

  cursor(cursor: string): void {
    if (cursor) this.#cursor = cursor
  }

  get task(): TaskProjection { return this.#task }
  get terminal(): boolean { return terminal(this.#task) }
  get generation(): number { return this.#generation }
  get lastSequence(): number { return this.#lastSequence }
  get revision(): string { return `${this.#generation}:${this.lastSequence}` }

  snapshot(): ProductProjectionSnapshot {
    return Object.freeze({
      taskId: this.#task.taskId,
      runId: this.#task.runId,
      generation: this.#generation,
      revision: this.revision,
      lastSequence: this.lastSequence,
      cursor: this.#cursor,
      frameCount: this.#frameCount,
      retainedFrameCount: this.#frames.length + this.#liveFrames.length,
      connection: this.#connection,
      events: projectProductEvents({
        task: this.#task,
        frames: [...this.#frames, ...this.#liveFrames],
        permissions: this.#permissions,
        userInputs: this.#userInputs,
        transport: this.#transport,
      }),
    })
  }
}
