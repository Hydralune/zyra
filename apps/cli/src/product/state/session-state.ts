import type { UiFileChange, UiPermissionRequest, UiSeverity, UiToolOutputRef, UiVerificationSummary, ZyraUiEvent } from "../../presentation/events.ts"

export interface ProductMessageState {
  messageId: string
  role: "user" | "assistant"
  text: string
  streaming: boolean
  truncated?: boolean
}

export interface ProductActivityState {
  activityId: string
  label: string
  status: "pending" | "running" | "completed"
  outcome?: string
  category?: string
  summary?: string
  severity?: UiSeverity
}

export interface ProductToolState {
  toolCallId: string
  name: string
  summary: string
  status: "running" | "completed" | "failed"
  durationMs?: number
  artifactIds?: readonly string[]
  outputRefs?: readonly UiToolOutputRef[]
}

export interface ProductAgentState {
  agentId: string
  label: string
  status: string
  summary?: string
}

export interface ProductIssueState {
  issueId: string
  severity: UiSeverity
  message: string
  code?: string
  retryable?: boolean
  recovery?: string
}

export interface ProductViewState {
  sessionId?: string
  taskId?: string
  messages: readonly ProductMessageState[]
  activities: readonly ProductActivityState[]
  tools: readonly ProductToolState[]
  agents: readonly ProductAgentState[]
  issues: readonly ProductIssueState[]
  permissions: readonly UiPermissionRequest[]
  changes: readonly UiFileChange[]
  diff?: { lines: readonly string[]; truncated: boolean }
  verification?: UiVerificationSummary
  connection: "connected" | "reconnecting" | "disconnected"
  reconnectAttempt?: number
  taskStatus: "idle" | "running" | "completed" | "failed" | "cancelled"
  taskMessage?: string
  evicted: Readonly<{ messages: number; activities: number; tools: number; agents: number; issues: number; changes: number }>
}

export interface ProductStateLimits {
  messages: number
  activities: number
  tools: number
  agents: number
  issues: number
  changes: number
  diffLines: number
  messageCharacters: number
}

const DEFAULT_LIMITS: ProductStateLimits = Object.freeze({
  messages: 10_000,
  activities: 2_000,
  tools: 2_000,
  agents: 512,
  issues: 512,
  changes: 5_000,
  diffLines: 4_000,
  messageCharacters: 1_000_000,
})

function boundedMessage(text: string, limit: number): { text: string; truncated?: true } {
  if (text.length <= limit) return { text }
  return {
    text: `${text.slice(0, limit)}\n…[内容已截断；完整内容请在 artifact 或 Web 看板查看]`,
    truncated: true,
  }
}

function boundedLimit(value: number | undefined, fallback: number): number {
  return Math.max(1, Math.floor(value ?? fallback))
}

function putBounded<K, V>(map: Map<K, V>, key: K, value: V, limit: number): number {
  if (map.has(key)) map.delete(key)
  map.set(key, value)
  let evicted = 0
  while (map.size > limit) {
    const first = map.keys().next().value as K | undefined
    if (first === undefined) break
    map.delete(first)
    evicted += 1
  }
  return evicted
}

export class ProductSessionState {
  readonly #limits: ProductStateLimits
  readonly #messages = new Map<string, ProductMessageState>()
  readonly #activities = new Map<string, ProductActivityState>()
  readonly #tools = new Map<string, ProductToolState>()
  readonly #agents = new Map<string, ProductAgentState>()
  readonly #issues = new Map<string, ProductIssueState>()
  readonly #permissions = new Map<string, UiPermissionRequest>()
  readonly #changes = new Map<string, UiFileChange>()
  #diff: ProductViewState["diff"]
  #verification: UiVerificationSummary | undefined
  #sessionId: string | undefined
  #taskId: string | undefined
  #connection: ProductViewState["connection"] = "connected"
  #reconnectAttempt: number | undefined
  #taskStatus: ProductViewState["taskStatus"] = "idle"
  #taskMessage: string | undefined
  #sourceLength = 0
  #sourceTail: string | undefined
  #evicted = { messages: 0, activities: 0, tools: 0, agents: 0, issues: 0, changes: 0 }

  constructor(limits: Partial<ProductStateLimits> = {}) {
    this.#limits = Object.freeze({
      messages: boundedLimit(limits.messages, DEFAULT_LIMITS.messages),
      activities: boundedLimit(limits.activities, DEFAULT_LIMITS.activities),
      tools: boundedLimit(limits.tools, DEFAULT_LIMITS.tools),
      agents: boundedLimit(limits.agents, DEFAULT_LIMITS.agents),
      issues: boundedLimit(limits.issues, DEFAULT_LIMITS.issues),
      changes: boundedLimit(limits.changes, DEFAULT_LIMITS.changes),
      diffLines: boundedLimit(limits.diffLines, DEFAULT_LIMITS.diffLines),
      messageCharacters: boundedLimit(limits.messageCharacters, DEFAULT_LIMITS.messageCharacters),
    })
  }

  reconcile(events: readonly ZyraUiEvent[]): void {
    const extendsPrior = this.#sourceLength === 0
      || (events.length >= this.#sourceLength && events[this.#sourceLength - 1]?.eventId === this.#sourceTail)
    if (!extendsPrior) this.#reset()
    for (const event of events.slice(this.#sourceLength)) this.apply(event)
    this.#sourceLength = events.length
    this.#sourceTail = events.at(-1)?.eventId
  }

  apply(event: ZyraUiEvent): void {
    switch (event.type) {
      case "session.started":
        this.#sessionId = event.sessionId
        this.#taskId = event.taskId
        this.#taskStatus = event.taskId ? "running" : "idle"
        break
      case "user.message":
        this.#evicted.messages += putBounded(this.#messages, event.messageId, { messageId: event.messageId, role: "user", ...boundedMessage(event.text, this.#limits.messageCharacters), streaming: false }, this.#limits.messages)
        break
      case "assistant.message.started":
        this.#evicted.messages += putBounded(this.#messages, event.messageId, { messageId: event.messageId, role: "assistant", text: "", streaming: true }, this.#limits.messages)
        break
      case "assistant.message.delta": { // Duplicate delivery is removed by the projection contract.
        const prior = this.#messages.get(event.messageId)
        const message = prior?.truncated
          ? { text: prior.text, truncated: true as const }
          : boundedMessage(`${prior?.text ?? ""}${event.text}`, this.#limits.messageCharacters)
        this.#evicted.messages += putBounded(this.#messages, event.messageId, { messageId: event.messageId, role: "assistant", ...message, streaming: true }, this.#limits.messages)
        break
      }
      case "assistant.message.completed":
        this.#evicted.messages += putBounded(this.#messages, event.messageId, { messageId: event.messageId, role: "assistant", ...boundedMessage(event.text, this.#limits.messageCharacters), streaming: false }, this.#limits.messages)
        break
      case "activity.started":
      case "activity.updated":
      case "activity.completed":
        this.#evicted.activities += putBounded(this.#activities, event.activityId, {
          activityId: event.activityId,
          label: event.label,
          status: event.type === "activity.completed" ? "completed" : event.type === "activity.started" ? "running" : "pending",
          outcome: event.type === "activity.completed" ? event.outcome : undefined,
          category: event.category,
          summary: event.summary,
          severity: event.severity,
        }, this.#limits.activities)
        break
      case "tool.started":
        this.#evicted.tools += putBounded(this.#tools, event.toolCallId, { toolCallId: event.toolCallId, name: event.name, summary: event.summary, status: "running", durationMs: event.durationMs, artifactIds: event.artifactIds, outputRefs: event.outputRefs }, this.#limits.tools)
        break
      case "tool.updated":
      case "tool.completed":
      case "tool.failed": { // Preserve the stable name across lifecycle updates.
        const prior = this.#tools.get(event.toolCallId)
        this.#evicted.tools += putBounded(this.#tools, event.toolCallId, {
          toolCallId: event.toolCallId,
          name: event.name ?? prior?.name ?? "工具",
          summary: event.type === "tool.failed" ? event.message : event.summary,
          status: event.type === "tool.failed" ? "failed" : event.type === "tool.completed" ? "completed" : "running",
          durationMs: event.durationMs ?? prior?.durationMs,
          artifactIds: event.artifactIds ?? prior?.artifactIds,
          outputRefs: event.outputRefs ?? prior?.outputRefs,
        }, this.#limits.tools)
        break
      }
      case "subagent.updated":
        {
          const prior = this.#agents.get(event.agentId)
          const label = event.label === "Execution worker" && prior ? prior.label : event.label
          this.#evicted.agents += putBounded(this.#agents, event.agentId, { agentId: event.agentId, label, status: event.status, summary: event.summary }, this.#limits.agents)
        }
        break
      case "task.issue":
        this.#evicted.issues += putBounded(this.#issues, event.issueId, {
          issueId: event.issueId,
          severity: event.severity,
          message: event.message,
          code: event.code,
          retryable: event.retryable,
          recovery: event.recovery,
        }, this.#limits.issues)
        break
      case "permission.requested":
        this.#permissions.set(event.request.requestId, event.request)
        break
      case "permission.resolved":
        this.#permissions.delete(event.requestId)
        break
      case "workspace.changed":
        for (const change of event.changes) this.#evicted.changes += putBounded(this.#changes, `${change.kind}:${change.path}`, change, this.#limits.changes)
        break
      case "workspace.diff":
        this.#diff = { lines: Object.freeze(event.lines.slice(0, this.#limits.diffLines)), truncated: event.truncated || event.lines.length > this.#limits.diffLines }
        break
      case "verification.updated":
        this.#verification = event.verification
        break
      case "task.completed":
        this.#taskId = event.taskId
        this.#taskStatus = "completed"
        break
      case "task.failed":
        this.#taskId = event.taskId
        this.#taskStatus = "failed"
        this.#taskMessage = event.recovery ? `${event.message} ${event.recovery}` : event.message
        break
      case "task.cancelled":
        this.#taskId = event.taskId
        this.#taskStatus = "cancelled"
        this.#taskMessage = event.message
        break
      case "transport.reconnecting":
        this.#connection = "reconnecting"
        this.#reconnectAttempt = event.attempt
        break
      case "transport.recovered":
        this.#connection = "connected"
        this.#reconnectAttempt = undefined
        break
    }
  }

  snapshot(): ProductViewState {
    return Object.freeze({
      sessionId: this.#sessionId,
      taskId: this.#taskId,
      messages: Object.freeze([...this.#messages.values()].map((value) => Object.freeze({ ...value }))),
      activities: Object.freeze([...this.#activities.values()].map((value) => Object.freeze({ ...value }))),
      tools: Object.freeze([...this.#tools.values()].map((value) => Object.freeze({ ...value }))),
      agents: Object.freeze([...this.#agents.values()].map((value) => Object.freeze({ ...value }))),
      issues: Object.freeze([...this.#issues.values()].map((value) => Object.freeze({ ...value }))),
      permissions: Object.freeze([...this.#permissions.values()]),
      changes: Object.freeze([...this.#changes.values()]),
      diff: this.#diff,
      verification: this.#verification,
      connection: this.#connection,
      reconnectAttempt: this.#reconnectAttempt,
      taskStatus: this.#taskStatus,
      taskMessage: this.#taskMessage,
      evicted: Object.freeze({ ...this.#evicted }),
    })
  }

  #reset(): void {
    this.#messages.clear()
    this.#activities.clear()
    this.#tools.clear()
    this.#agents.clear()
    this.#issues.clear()
    this.#permissions.clear()
    this.#changes.clear()
    this.#diff = undefined
    this.#verification = undefined
    this.#sessionId = undefined
    this.#taskId = undefined
    this.#connection = "connected"
    this.#reconnectAttempt = undefined
    this.#taskStatus = "idle"
    this.#taskMessage = undefined
    this.#sourceLength = 0
    this.#sourceTail = undefined
    this.#evicted = { messages: 0, activities: 0, tools: 0, agents: 0, issues: 0, changes: 0 }
  }
}
