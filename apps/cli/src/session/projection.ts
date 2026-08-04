import { CliTaskError } from "../contracts.ts"
import type { IngressFrame } from "../api.ts"

export type TranscriptKind = "model" | "tool" | "agent" | "artifact" | "warning" | "error" | "recovery" | "task" | "event"

export interface TranscriptRecord {
  sequence: number
  eventId: string
  eventType: string
  kind: TranscriptKind
  summary: string
  refs: readonly string[]
}

export interface PendingPermissionView {
  requestId: string
  reason?: string
  risk?: string
  expiresAt?: string
  eventId: string
}

export interface SessionViewSnapshot {
  taskId: string
  generation: number
  revision: string
  lastSequence: number
  action: string
  steps: number
  pendingPermissions: number
  pendingPermissionRequests: readonly PendingPermissionView[]
  connection: "connecting" | "live" | "disconnected" | "complete"
  records: readonly TranscriptRecord[]
  evicted: number
  follow: boolean
  unread: number
  searchEnabled: boolean
  searchQuery?: string
  searchMatches: readonly number[]
}

function objectValue(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Readonly<Record<string, unknown>> : {}
}

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined
}

function refsFor(event: Readonly<Record<string, unknown>>, inline: Readonly<Record<string, unknown>>): string[] {
  const refs = new Set<string>()
  for (const key of ["result_digest", "content_digest", "progress_digest", "input_digest", "error_digest"]) {
    const value = text(inline[key])
    if (value) refs.add(`${key.replace("_digest", "")}:${value.slice(0, 16)}`)
  }
  const artifacts = Array.isArray(event.artifactRefs) ? event.artifactRefs : []
  for (const artifact of artifacts) {
    const id = text(objectValue(artifact).artifactId)
    if (id) refs.add(`artifact:${id}`)
  }
  const requestId = text(inline.request_id) ?? text(inline.requestId)
  if (requestId) refs.add(`permission:${requestId}`)
  return [...refs]
}

function permissionFields(frame: IngressFrame): PendingPermissionView {
  const event = frame.event
  const inline = objectValue(event.inline)
  const payload = objectValue(event.payload)
  const nested = objectValue(inline.permission_request ?? payload.permission_request ?? payload.request)
  const requestId = text(inline.request_id)
    ?? text(inline.requestId)
    ?? text(payload.request_id)
    ?? text(payload.requestId)
    ?? text(nested.request_id)
    ?? text(nested.requestId)
    ?? frame.eventId
  return Object.freeze({
    requestId,
    reason: text(inline.reason) ?? text(payload.reason) ?? text(nested.reason),
    risk: text(inline.risk) ?? text(inline.risk_level) ?? text(payload.risk) ?? text(nested.risk),
    expiresAt: text(inline.expires_at) ?? text(payload.expires_at) ?? text(nested.expires_at),
    eventId: frame.eventId,
  })
}

function classify(eventType: string): TranscriptKind {
  if (eventType.startsWith("runtime.text.") || eventType.startsWith("runtime.reasoning.") || eventType === "runtime.agent.message") return "model"
  if (eventType.startsWith("runtime.tool.") || eventType.startsWith("runtime.mcp.tool.")) return "tool"
  if (eventType.startsWith("runtime.subagent.") || eventType.startsWith("runtime.agent.")) return "agent"
  if (eventType.startsWith("runtime.artifact.")) return "artifact"
  if (eventType.startsWith("runtime.recovery.")) return "recovery"
  if (eventType.includes("failed") || eventType.includes("error") || eventType.includes("denied")) return "error"
  if (eventType.includes("warning") || eventType.includes("degraded") || eventType.includes("disconnected")) return "warning"
  if (eventType.startsWith("runtime.task.")) return "task"
  return "event"
}

function summarize(frame: IngressFrame): TranscriptRecord {
  const event = frame.event
  const inline = objectValue(event.inline)
  const identity = objectValue(event.identity)
  const kind = classify(frame.eventType)
  const name = text(inline.tool_name) ?? text(inline.skill_id) ?? text(identity.workerId)
  const status = text(inline.status) ?? text(inline.decision)
  const parts = [frame.eventType]
  if (name) parts.push(name)
  if (status) parts.push(status)
  if (frame.eventType === "runtime.tool.progress") {
    const percent = typeof inline.percent === "number" ? Math.max(0, Math.min(100, inline.percent)) : undefined
    if (percent !== undefined) parts.push(`${percent}%`)
  }
  if (frame.eventType === "runtime.text.delta") {
    const bytes = typeof inline.delta_bytes === "number" ? inline.delta_bytes : undefined
    if (bytes !== undefined) parts.push(`${bytes} bytes`)
  }
  if (frame.eventType.startsWith("runtime.permission.")) {
    const permission = permissionFields(frame)
    if (permission.requestId) parts.push(permission.requestId)
    if (permission.risk) parts.push(`risk ${permission.risk}`)
    if (permission.reason) parts.push(permission.reason)
    if (permission.expiresAt) parts.push(`expires ${permission.expiresAt}`)
  }
  return Object.freeze({
    sequence: frame.sequence,
    eventId: frame.eventId,
    eventType: frame.eventType,
    kind,
    summary: parts.join(" · "),
    refs: Object.freeze(refsFor(event, inline)),
  })
}

export class SessionProjection {
  readonly #taskId: string
  readonly #generation: number
  readonly #budget: number
  readonly #records: TranscriptRecord[] = []
  readonly #seen = new Map<number, string>()
  #lastSequence = 0
  #steps = 0
  readonly #pendingPermissions = new Map<string, PendingPermissionView>()
  #action = "snapshot"
  #connection: SessionViewSnapshot["connection"] = "connecting"
  #evicted = 0
  #follow = true
  #unread = 0
  #searchEnabled = true
  #searchQuery: string | undefined
  #terminal = false

  constructor(input: { taskId: string; generation: number; budget?: number }) {
    this.#taskId = input.taskId
    this.#generation = input.generation
    this.#budget = Math.max(32, input.budget ?? 512)
  }

  apply(frame: IngressFrame): TranscriptRecord | undefined {
    if (frame.taskId !== this.#taskId || frame.generation !== this.#generation) {
      throw new CliTaskError("Event does not match the active session binding.", "contract_projection_binding_invalid")
    }
    const priorId = this.#seen.get(frame.sequence)
    if (priorId === frame.eventId) return undefined
    if (priorId || (this.#lastSequence && (frame.sequence <= this.#lastSequence || frame.previousSequence !== this.#lastSequence))) {
      throw new CliTaskError("Event stream is duplicated, reordered, or has a gap.", "contract_event_order_invalid", {
        revision: `${this.#generation}:${this.#lastSequence}`,
        sequence: frame.sequence,
        previous_sequence: frame.previousSequence,
      })
    }
    this.#seen.set(frame.sequence, frame.eventId)
    this.#lastSequence = frame.sequence
    this.#steps += 1
    this.#action = frame.eventType
    if (["runtime.task.completed", "runtime.task.failed", "runtime.task.cancelled"].includes(frame.eventType)) this.#terminal = true
    if (frame.eventType === "runtime.permission.pending") {
      const permission = permissionFields(frame)
      this.#pendingPermissions.set(permission.requestId, permission)
    }
    if (["runtime.permission.allowed", "runtime.permission.denied", "runtime.permission.expired", "runtime.permission.cancelled"].includes(frame.eventType)) {
      const permission = permissionFields(frame)
      this.#pendingPermissions.delete(permission.requestId)
    }
    const record = summarize(frame)
    const transientProgress = frame.eventType === "runtime.tool.progress"
    if (!transientProgress) this.#records.push(record)
    while (this.#records.length > this.#budget) {
      this.#records.shift()
      this.#evicted += 1
    }
    if (!this.#follow) this.#unread += 1
    return transientProgress ? undefined : record
  }

  get terminal(): boolean { return this.#terminal }

  connected(): void { this.#connection = "live" }
  disconnected(): void { this.#connection = "disconnected" }
  complete(): void { this.#connection = "complete" }
  recovering(action = "event_stream_recovering"): void { this.#connection = "connecting"; this.#action = action }

  follow(enabled: boolean): void {
    this.#follow = enabled
    if (enabled) this.#unread = 0
  }

  search(query: string): readonly TranscriptRecord[] {
    this.#searchQuery = query.trim() || undefined
    if (!this.#searchEnabled || !this.#searchQuery) return []
    const lowered = this.#searchQuery.toLowerCase()
    return this.#records.filter((record) => `${record.summary} ${record.refs.join(" ")}`.toLowerCase().includes(lowered))
  }

  disableSearch(): void { this.#searchEnabled = false; this.#searchQuery = undefined }
  resized(): void { this.#searchQuery = undefined }

  snapshot(): SessionViewSnapshot {
    const query = this.#searchQuery?.toLowerCase()
    return Object.freeze({
      taskId: this.#taskId,
      generation: this.#generation,
      revision: `${this.#generation}:${this.#lastSequence}`,
      lastSequence: this.#lastSequence,
      action: this.#action,
      steps: this.#steps,
      pendingPermissions: this.#pendingPermissions.size,
      pendingPermissionRequests: Object.freeze([...this.#pendingPermissions.values()]),
      connection: this.#connection,
      records: Object.freeze([...this.#records]),
      evicted: this.#evicted,
      follow: this.#follow,
      unread: this.#unread,
      searchEnabled: this.#searchEnabled,
      searchQuery: this.#searchQuery,
      searchMatches: Object.freeze(query ? this.#records.filter((record) => `${record.summary} ${record.refs.join(" ")}`.toLowerCase().includes(query)).map((record) => record.sequence) : []),
    })
  }
}
