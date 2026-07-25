import type { CommandReceipt } from "../../../../../packages/commands/src/index.ts"
import type { CommandSurfaceRuntime } from "../commands/runtime.ts"
import type { CanonicalProjectionStore } from "../../state/index.ts"
import type { CanonicalProjectionState } from "../../state/contracts.ts"
import {
  buildSessionConsoleProjection,
  type CompactPreview,
  type CompactPreviewOptions,
  type SessionConsoleProjection,
} from "./projection.ts"
import {
  assertIdentity,
  assertQuery,
  bounded,
  fingerprint,
  integer,
  record,
  text,
} from "./value.ts"
import { SessionBehaviorCoordinator } from "./coordinator.ts"

export type SessionControlPhase =
  | "idle"
  | "previewing"
  | "submitting"
  | "queued"
  | "reconciling"
  | "committed"
  | "failed"
  | "disconnected"
  | "disabled"

export interface SessionControlOperation {
  id: string
  name: "/context" | "/compact" | "/memory" | "/model" | "/resume" | "/rewind" | "/export"
  phase: SessionControlPhase
  taskId: string
  runId: string
  sessionId: string
  startedRevision: number
  expectedRevision: number
  startedAt: string
  updatedAt: string
  previewFingerprint?: string
  checkpointId?: string
  commandId?: string
  requestId?: string
  receipt?: CommandReceipt
  error?: string
  canonicalEventIds: readonly string[]
  reconciledRevision?: number
}

export interface SessionConsoleSnapshot {
  taskId?: string
  runId?: string
  activeSessionId?: string
  connected: boolean
  enabled: boolean
  disabledReason?: string
  projection?: SessionConsoleProjection
  previewOptions: CompactPreviewOptions
  active?: SessionControlOperation
  operations: readonly SessionControlOperation[]
  lastReceipt?: CommandReceipt
  revision: number
}

export interface SessionRuntimeOptions {
  projections: CanonicalProjectionStore
  commands: CommandSurfaceRuntime
  online?: () => boolean
  now?: () => Date
}

export class SessionConsoleRuntime {
  readonly behavior = new SessionBehaviorCoordinator()
  readonly #projections: CanonicalProjectionStore
  readonly #commands: CommandSurfaceRuntime
  readonly #online: () => boolean
  readonly #now: () => Date
  readonly #listeners = new Set<() => void>()
  readonly #operations = new Map<string, SessionControlOperation>()
  #snapshot: SessionConsoleSnapshot
  #unsubscribeProjection: () => void
  #unsubscribeCommands: () => void
  #closed = false

  constructor(options: SessionRuntimeOptions) {
    this.#projections = options.projections
    this.#commands = options.commands
    this.#online = options.online ?? (() =>
      typeof navigator === "undefined" ? true : navigator.onLine)
    this.#now = options.now ?? (() => new Date())
    this.#snapshot = Object.freeze({
      connected: this.#online(),
      enabled: true,
      previewOptions: Object.freeze({}),
      operations: Object.freeze([]),
      revision: 0,
    })
    this.#unsubscribeProjection = this.#projections.subscribe(() => {
      this.#projectionChanged()
    })
    this.#unsubscribeCommands = this.#commands.subscribe(() => {
      this.#commandChanged()
    })
  }

  getSnapshot = (): SessionConsoleSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  bind(taskId?: string, runId?: string, sessionId?: string): void {
    if (this.#closed) return
    if (!taskId) {
      this.#replace({
        taskId: undefined,
        runId: undefined,
        activeSessionId: undefined,
        projection: undefined,
        active: undefined,
      })
      return
    }
    const projection = buildSessionConsoleProjection(
      this.#projections.state,
      taskId,
      sessionId,
      this.#snapshot.previewOptions,
    )
    this.behavior.update(this.#projections.state, taskId, sessionId, {
      preview: this.#snapshot.previewOptions,
      source: projection.connected ? "live" : "snapshot",
    })
    this.#replace({
      taskId,
      runId: runId || this.#projections.state.tasks[taskId]?.runId,
      activeSessionId: projection.activeSessionId,
      connected: this.#online() && projection.connected,
      projection,
    })
  }

  setSession(sessionId: string): void {
    const taskId = this.#requireTask()
    const selected = assertIdentity("session", sessionId)
    const session = this.#projections.state.sessions[selected]
    if (!session || session.taskId !== taskId) {
      throw new TypeError("Session does not belong to the selected task.")
    }
    const projection = buildSessionConsoleProjection(
      this.#projections.state,
      taskId,
      selected,
      this.#snapshot.previewOptions,
    )
    this.#replace({
      activeSessionId: selected,
      projection,
    })
  }

  setPreviewOptions(options: CompactPreviewOptions): CompactPreview | undefined {
    const normalized: CompactPreviewOptions = Object.freeze({
      targetTokens: options.targetTokens === undefined
        ? undefined
        : bounded(integer(options.targetTokens), 512, 1000000),
      reserveTokens: options.reserveTokens === undefined
        ? undefined
        : bounded(integer(options.reserveTokens), 0, 1000000),
      preserveRecentEvents: options.preserveRecentEvents === undefined
        ? undefined
        : bounded(integer(options.preserveRecentEvents), 0, 1000),
      preserveKinds: options.preserveKinds
        ? Object.freeze([...new Set(options.preserveKinds.map((entry) => text(entry)).filter(Boolean))])
        : undefined,
    })
    const taskId = this.#snapshot.taskId
    const projection = taskId
      ? buildSessionConsoleProjection(
          this.#projections.state,
          taskId,
          this.#snapshot.activeSessionId,
          normalized,
        )
      : undefined
    this.#replace({
      previewOptions: normalized,
      projection,
    })
    return projection?.preview
  }

  previewCompact(options: CompactPreviewOptions = {}): CompactPreview {
    this.#assertAvailable()
    const preview = this.setPreviewOptions({
      ...this.#snapshot.previewOptions,
      ...options,
    })
    if (!preview) throw new Error("No active session context is available.")
    const operation = this.#begin("/compact", "previewing", {
      previewFingerprint: preview.fingerprint,
    })
    this.#settle(operation.id, {
      phase: "committed",
      reconciledRevision: this.#projections.state.revision,
    })
    return preview
  }

  async inspectContext(scope = "all"): Promise<CommandReceipt> {
    this.#assertAvailable()
    const sessionId = this.#requireSession()
    return this.#submit(
      "/context",
      `/context --scope ${sanitizeChoice(scope, [
        "summary",
        "categories",
        "messages",
        "tools",
        "memory",
        "all",
      ], "all")} --session ${quote(sessionId)}`,
    )
  }

  async executeCompact(input: {
    targetTokens?: number
    reason?: string
    previewFingerprint?: string
  } = {}): Promise<CommandReceipt> {
    this.#assertAvailable()
    const preview = this.#snapshot.projection?.preview
    if (!preview?.eligible) {
      throw new Error(preview?.reason ?? "Compact preview is unavailable.")
    }
    const expected = input.previewFingerprint ?? preview.fingerprint
    if (expected !== preview.fingerprint) {
      throw new Error("Compact preview is stale; refresh it before execution.")
    }
    const target = bounded(
      integer(input.targetTokens, preview.targetTokens),
      512,
      Math.max(512, this.#snapshot.projection?.context?.limit ?? 1000000),
    )
    const reason = assertQuery(input.reason ?? "operator accepted compact preview")
    return this.#submit(
      "/compact",
      `/compact execute --target ${target} --reason ${quote(reason)}`,
      {
        previewFingerprint: preview.fingerprint,
      },
    )
  }

  async queryMemory(input: {
    query?: string
    layer?: "working" | "episodic" | "semantic" | "skill" | "all"
    limit?: number
  } = {}): Promise<CommandReceipt> {
    this.#assertAvailable()
    const query = assertQuery(input.query ?? "")
    const layer = sanitizeChoice(
      input.layer ?? "all",
      ["working", "episodic", "semantic", "skill", "all"],
      "all",
    )
    const limit = bounded(integer(input.limit, 50), 1, 500)
    return this.#submit(
      "/memory",
      `/memory search ${quote(query)} --layer ${layer} --limit ${limit}`,
    )
  }

  async curateMemory(input: {
    action: "curate" | "accept" | "reject" | "recover"
    candidateId?: string
    reason?: string
  }): Promise<CommandReceipt> {
    this.#assertAvailable()
    const action = sanitizeChoice(
      input.action,
      ["curate", "accept", "reject", "recover"],
      "curate",
    )
    const subject = [input.candidateId, input.reason]
      .map((entry) => assertQuery(entry ?? ""))
      .filter(Boolean)
      .join(" ")
    return this.#submit("/memory", `/memory ${action}${subject ? ` ${quote(subject)}` : ""}`)
  }

  async selectModel(input: {
    action?: "list" | "show" | "select" | "failover"
    modelId?: string
    providerId?: string
    purpose?: string
  } = {}): Promise<CommandReceipt> {
    this.#assertAvailable()
    const action = sanitizeChoice(
      input.action ?? (input.modelId ? "select" : "list"),
      ["list", "show", "select", "failover"],
      "list",
    )
    const model = input.modelId ? assertIdentity("model", input.modelId) : ""
    const provider = input.providerId ? assertIdentity("provider", input.providerId) : ""
    const purpose = assertQuery(input.purpose ?? "general")
    const parts = ["/model", action]
    if (model) parts.push(quote(model))
    if (provider) parts.push("--provider", quote(provider))
    if (purpose) parts.push("--purpose", quote(purpose))
    return this.#submit("/model", parts.join(" "))
  }

  async resume(checkpointId: string, compactFirst = false): Promise<CommandReceipt> {
    this.#assertAvailable()
    const checkpoint = this.#exactCheckpoint(checkpointId)
    return this.#submit(
      "/resume",
      `/resume ${quote(checkpoint.id)}${compactFirst ? " --compact-first" : ""}`,
      { checkpointId: checkpoint.id },
    )
  }

  async rewind(checkpointId: string, reason = "operator selected exact checkpoint"): Promise<CommandReceipt> {
    this.#assertAvailable()
    const checkpoint = this.#exactCheckpoint(checkpointId)
    return this.#submit(
      "/rewind",
      `/rewind ${quote(checkpoint.id)} --reason ${quote(assertQuery(reason))}`,
      { checkpointId: checkpoint.id },
    )
  }

  async exportSession(input: {
    format?: "json" | "markdown"
    include?: "summary" | "context" | "memory" | "trace" | "all"
  } = {}): Promise<CommandReceipt> {
    this.#assertAvailable()
    const format = sanitizeChoice(input.format ?? "json", ["json", "markdown"], "json")
    const include = sanitizeChoice(
      input.include ?? "all",
      ["summary", "context", "memory", "trace", "all"],
      "all",
    )
    return this.#submit("/export", `/export --format ${format} --include ${include}`)
  }

  disconnected(reason = "Browser transport disconnected."): void {
    if (this.#closed) return
    const active = this.#snapshot.active
    if (active && !terminalPhase(active.phase)) {
      this.#settle(active.id, {
        phase: "disconnected",
        error: reason,
      })
    }
    this.#replace({
      connected: false,
      active: this.#snapshot.active,
    })
  }

  reconnected(): void {
    if (this.#closed || !this.#snapshot.enabled) return
    const taskId = this.#snapshot.taskId
    const projection = taskId
      ? buildSessionConsoleProjection(
          this.#projections.state,
          taskId,
          this.#snapshot.activeSessionId,
          this.#snapshot.previewOptions,
        )
      : undefined
    this.#replace({
      connected: this.#online() && (projection?.connected ?? true),
      projection,
    })
    const active = this.#snapshot.active
    if (active?.phase === "disconnected") {
      this.#settle(active.id, {
        phase: "reconciling",
        error: undefined,
      })
      this.#reconcile(active)
    }
  }

  disable(reason = "Session console is disabled."): void {
    if (this.#closed) return
    const active = this.#snapshot.active
    if (active && !terminalPhase(active.phase)) {
      this.#settle(active.id, {
        phase: "disabled",
        error: reason,
      })
    }
    this.#replace({
      enabled: false,
      disabledReason: reason,
      active: this.#snapshot.active,
    })
  }

  enable(): void {
    if (this.#closed) return
    this.#replace({
      enabled: true,
      disabledReason: undefined,
      connected: this.#online(),
    })
    this.reconnected()
  }

  close(reason = "Session console closed."): void {
    if (this.#closed) return
    this.#closed = true
    const active = this.#snapshot.active
    if (active && !terminalPhase(active.phase)) {
      this.#operations.set(active.id, Object.freeze({
        ...active,
        phase: "disabled",
        error: reason,
        updatedAt: this.#now().toISOString(),
      }))
    }
    this.#unsubscribeProjection()
    this.#unsubscribeCommands()
    this.behavior.close(reason)
    this.#listeners.clear()
  }

  #requireTask(): string {
    const taskId = this.#snapshot.taskId
    if (!taskId) throw new Error("Select a task first.")
    return taskId
  }

  #requireRun(): string {
    const runId = this.#snapshot.runId
    if (!runId) throw new Error("The selected task has no active run identity.")
    return runId
  }

  #requireSession(): string {
    const sessionId = this.#snapshot.activeSessionId
    if (!sessionId) throw new Error("The selected task has no active session identity.")
    return sessionId
  }

  #assertAvailable(): void {
    if (this.#closed) throw new Error("Session console is closed.")
    if (!this.#snapshot.enabled) {
      throw new Error(this.#snapshot.disabledReason ?? "Session console is disabled.")
    }
    if (!this.#snapshot.connected || !this.#online()) {
      throw new Error("Session controls are unavailable while disconnected.")
    }
    this.#requireTask()
    this.#requireRun()
    this.#requireSession()
  }

  #exactCheckpoint(checkpointId: string) {
    const id = assertIdentity("checkpoint", checkpointId)
    const checkpoint = this.#snapshot.projection?.checkpoints.find((entry) => entry.id === id)
    if (!checkpoint) throw new Error("Checkpoint is absent from the canonical session projection.")
    if (checkpoint.sessionId !== this.#requireSession()) {
      throw new Error("Checkpoint belongs to another session.")
    }
    if (!checkpoint.exactResumeEligible) {
      throw new Error(
        checkpoint.conflictReason ??
        checkpoint.staleReason ??
        "Checkpoint is not eligible for exact resume.",
      )
    }
    return checkpoint
  }

  #begin(
    name: SessionControlOperation["name"],
    phase: SessionControlPhase,
    patch: Partial<SessionControlOperation> = {},
  ): SessionControlOperation {
    const now = this.#now().toISOString()
    const revision = this.#projections.state.revision
    const operation: SessionControlOperation = Object.freeze({
      id: fingerprint([name, this.#requireTask(), this.#requireSession(), revision, now, this.#operations.size]),
      name,
      phase,
      taskId: this.#requireTask(),
      runId: this.#requireRun(),
      sessionId: this.#requireSession(),
      startedRevision: revision,
      expectedRevision: revision,
      startedAt: now,
      updatedAt: now,
      canonicalEventIds: Object.freeze([]),
      ...patch,
    })
    this.#operations.set(operation.id, operation)
    this.#replace({ active: operation })
    return operation
  }

  async #submit(
    name: SessionControlOperation["name"],
    command: string,
    patch: Partial<SessionControlOperation> = {},
  ): Promise<CommandReceipt> {
    const operation = this.#begin(name, "submitting", patch)
    try {
      const receipt = await this.#commands.submit(command, { mode: "enqueue" })
      const phase = receipt.phase === "queued"
        ? "queued"
        : receipt.phase === "applied"
          ? "reconciling"
          : receipt.phase === "rejected" || receipt.phase === "cancelled" || receipt.phase === "expired"
            ? "failed"
            : "submitting"
      const updated = this.#settle(operation.id, {
        phase,
        receipt,
        commandId: receipt.commandId,
        requestId: receipt.requestId,
        checkpointId: receipt.checkpointRef ?? operation.checkpointId,
        error: receipt.error?.message,
        canonicalEventIds: receipt.eventIds,
      })
      this.#replace({ lastReceipt: receipt })
      if (phase === "reconciling") this.#reconcile(updated)
      if (phase === "failed") throw new Error(receipt.error?.message ?? receipt.summary)
      return receipt
    } catch (error) {
      this.#settle(operation.id, {
        phase: "failed",
        error: error instanceof Error ? error.message : String(error),
      })
      throw error
    }
  }

  #settle(
    id: string,
    patch: Partial<SessionControlOperation>,
  ): SessionControlOperation {
    const current = this.#operations.get(id)
    if (!current) throw new Error(`Unknown session control operation: ${id}`)
    const updated: SessionControlOperation = Object.freeze({
      ...current,
      ...patch,
      updatedAt: this.#now().toISOString(),
      canonicalEventIds: Object.freeze([
        ...new Set(patch.canonicalEventIds ?? current.canonicalEventIds),
      ]),
    })
    this.#operations.set(id, updated)
    this.#replace({
      active: updated,
      operations: this.#operationList(),
    })
    return updated
  }

  #operationList(): readonly SessionControlOperation[] {
    return Object.freeze(
      [...this.#operations.values()]
        .sort((left, right) => right.startedAt.localeCompare(left.startedAt))
        .slice(0, 100),
    )
  }

  #projectionChanged(): void {
    const taskId = this.#snapshot.taskId
    if (!taskId || this.#closed) return
    const projection = buildSessionConsoleProjection(
      this.#projections.state,
      taskId,
      this.#snapshot.activeSessionId,
      this.#snapshot.previewOptions,
    )
    this.behavior.update(
      this.#projections.state,
      taskId,
      this.#snapshot.activeSessionId,
      {
        preview: this.#snapshot.previewOptions,
        source: projection.connected ? "live" : "snapshot",
      },
    )
    this.#replace({
      projection,
      activeSessionId: projection.activeSessionId,
      connected: this.#online() && projection.connected,
    })
    const active = this.#snapshot.active
    if (active?.phase === "reconciling" || active?.phase === "queued") {
      this.#reconcile(active)
    }
  }

  #commandChanged(): void {
    const receipt = this.#commands.getSnapshot().lastOverlayReceipt
    if (!receipt || !CONSOLE_NAMES.has(receipt.name)) return
    this.#replace({ lastReceipt: receipt })
    const operation = [...this.#operations.values()]
      .find((entry) =>
        entry.commandId === receipt.commandId ||
        entry.requestId === receipt.requestId)
    if (!operation) return
    if (receipt.phase === "applied") {
      const updated = this.#settle(operation.id, {
        phase: "reconciling",
        receipt,
        checkpointId: receipt.checkpointRef ?? operation.checkpointId,
        canonicalEventIds: receipt.eventIds,
      })
      this.#reconcile(updated)
      return
    }
    if (["rejected", "expired", "cancelled"].includes(receipt.phase)) {
      this.#settle(operation.id, {
        phase: "failed",
        receipt,
        error: receipt.error?.message ?? receipt.summary,
      })
      return
    }
    if (receipt.phase === "queued") {
      this.#settle(operation.id, {
        phase: "queued",
        receipt,
      })
    }
  }

  #reconcile(operation: SessionControlOperation): void {
    const state = this.#projections.state
    const eventIds = canonicalReceiptEvents(state, operation)
    const revisionAdvanced = state.revision > operation.startedRevision
    const receiptEventsObserved =
      operation.canonicalEventIds.length === 0 ||
      operation.canonicalEventIds.every((eventId) => Boolean(state.causality.byEvent[eventId]))
    const commandObserved =
      !operation.commandId ||
      Boolean(state.commands[operation.commandId]) ||
      Boolean(state.causality.byControlCommand[operation.commandId]?.length)
    if (!revisionAdvanced || !receiptEventsObserved || !commandObserved) return
    this.#settle(operation.id, {
      phase: "committed",
      canonicalEventIds: eventIds,
      reconciledRevision: state.revision,
      error: undefined,
    })
  }

  #replace(patch: Partial<SessionConsoleSnapshot>): void {
    const next: SessionConsoleSnapshot = Object.freeze({
      ...this.#snapshot,
      ...patch,
      operations: patch.operations ?? this.#operationList(),
      revision: this.#snapshot.revision + 1,
    })
    this.#snapshot = next
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // A view subscriber cannot change control settlement.
      }
    }
  }
}

const CONSOLE_NAMES = new Set([
  "/context",
  "/compact",
  "/memory",
  "/model",
  "/resume",
  "/rewind",
  "/export",
])

function canonicalReceiptEvents(
  state: CanonicalProjectionState,
  operation: SessionControlOperation,
): string[] {
  const ids = new Set(operation.canonicalEventIds)
  if (operation.commandId) {
    for (const eventId of state.causality.byControlCommand[operation.commandId] ?? []) ids.add(eventId)
  }
  if (operation.checkpointId) {
    for (const eventId of state.causality.byCheckpoint[operation.checkpointId] ?? []) ids.add(eventId)
  }
  return [...ids]
    .filter((eventId) => Boolean(state.causality.byEvent[eventId]))
    .sort((left, right) =>
      (state.causality.byEvent[left]?.sequence ?? 0) -
      (state.causality.byEvent[right]?.sequence ?? 0))
}

function sanitizeChoice<T extends string>(
  value: unknown,
  choices: readonly T[],
  fallback: T,
): T {
  const selected = text(value).toLowerCase()
  return choices.includes(selected as T) ? selected as T : fallback
}

function quote(value: string): string {
  return JSON.stringify(value)
}

function terminalPhase(phase: SessionControlPhase): boolean {
  return ["committed", "failed", "disabled"].includes(phase)
}
