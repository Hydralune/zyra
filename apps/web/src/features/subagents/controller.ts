import type { CanonicalProjectionStore } from "../../state/store.ts"
import type {
  SubagentCommandPort,
  SubagentInterventionPort,
  SubagentPanelFilter,
  SubagentPanelSnapshot,
  SubagentProjectionOptions,
  SubagentRow,
} from "./contracts.ts"
import { SubagentControlRuntime } from "./control.ts"
import {
  LateResultQuarantine,
  SubagentLifecycleMonitor,
  SubagentReconnectLedger,
} from "./lifecycle.ts"
import { buildSubagentProjection } from "./projection.ts"
import { visibleHierarchyRows } from "./hierarchy.ts"
import {
  assertBoundedText,
  compareText,
  identity,
  sortStable,
  text,
} from "./value.ts"

export interface SubagentPanelControllerOptions {
  projections: CanonicalProjectionStore
  commands: SubagentCommandPort
  interventions?: SubagentInterventionPort
  online?: () => boolean
  now?: () => Date
  actorId?: () => string
  maximumRows?: number
  maximumOperations?: number
  maximumIncidents?: number
  heartbeatIntervalMs?: number
  heartbeatTimeoutMs?: number
  initiallyOpen?: boolean
  sealed?: boolean
}

export class SubagentPanelController {
  readonly controls: SubagentControlRuntime
  readonly lifecycle: SubagentLifecycleMonitor
  readonly quarantine = new LateResultQuarantine()
  readonly reconnect = new SubagentReconnectLedger()
  readonly #projections: CanonicalProjectionStore
  readonly #online: () => boolean
  readonly #now: () => Date
  readonly #projectionOptions: Omit<SubagentProjectionOptions, "runId" | "sessionId" | "nowMs">
  readonly #listeners = new Set<() => void>()
  readonly #expanded = new Set<string>()
  #snapshot: SubagentPanelSnapshot
  #unsubscribeProjection?: () => void
  #unsubscribeControls: () => void
  #closed = false
  #disabledReason = "Subagent panel is disabled."
  #generation = 0

  constructor(options: SubagentPanelControllerOptions) {
    this.#projections = options.projections
    this.#online = options.online ?? (() =>
      typeof navigator === "undefined" ? true : navigator.onLine)
    this.#now = options.now ?? (() => new Date())
    this.#projectionOptions = Object.freeze({
      maximumRows: options.maximumRows ?? 5_000,
      heartbeatIntervalMs: options.heartbeatIntervalMs,
      heartbeatTimeoutMs: options.heartbeatTimeoutMs,
    })
    this.lifecycle = new SubagentLifecycleMonitor({
      maximum: options.maximumIncidents,
      now: this.#now,
    })
    this.#snapshot = Object.freeze({
      visibleRows: Object.freeze([]),
      expandedIds: Object.freeze([]),
      filter: "active",
      query: "",
      viewerOpen: options.initiallyOpen !== false,
      connected: this.#online(),
      enabled: true,
      sealed: options.sealed === true,
      incidents: Object.freeze([]),
      controls: Object.freeze({
        enabled: true,
        connected: this.#online(),
        sealed: options.sealed === true,
        operations: Object.freeze([]),
        interventions: Object.freeze([]),
        revision: 0,
      }),
      restoreGeneration: 0,
      revision: 0,
    })
    this.controls = new SubagentControlRuntime({
      commands: options.commands,
      state: () => this.#projections.state,
      projection: () => this.#snapshot.projection,
      interventions: options.interventions,
      online: this.#online,
      now: this.#now,
      maximumOperations: options.maximumOperations,
      actorId: options.actorId,
    })
    this.controls.setSealed(this.#snapshot.sealed)
    this.#unsubscribeControls = this.controls.subscribe(() => {
      if (this.#closed) return
      this.#replace({
        controls: this.controls.getSnapshot(),
      })
    })
    if (this.#snapshot.viewerOpen) this.#attachProjection()
  }

  getSnapshot = (): SubagentPanelSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  bind(taskId?: string, runId?: string, sessionId?: string): void {
    this.#assertAvailable()
    if (!taskId) {
      this.#expanded.clear()
      this.#replace({
        taskId: undefined,
        runId: undefined,
        sessionId: undefined,
        projection: undefined,
        selectedId: undefined,
        selected: undefined,
        visibleRows: Object.freeze([]),
        expandedIds: Object.freeze([]),
        incidents: Object.freeze([]),
      })
      return
    }
    const selectedTask = identity(taskId, "task")
    const task = this.#projections.state.tasks[selectedTask]
    if (!task) throw new Error("Selected task is absent from the canonical projection.")
    const selectedRun = runId ? identity(runId, "run") : task.runId
    if (selectedRun !== task.runId) {
      throw new Error("Selected run does not match the canonical task run.")
    }
    const selectedSession = sessionId ? identity(sessionId, "session") : undefined
    if (
      selectedSession &&
      this.#projections.state.sessions[selectedSession]?.taskId !== selectedTask
    ) {
      throw new Error("Selected session does not belong to the canonical task.")
    }
    this.#replace({
      taskId: selectedTask,
      runId: selectedRun,
      sessionId: selectedSession,
      selectedId: undefined,
      selected: undefined,
    })
    this.#refresh("bind")
  }

  select(childId?: string): SubagentRow | undefined {
    this.#assertAvailable()
    if (!childId) {
      this.#replace({
        selectedId: undefined,
        selected: undefined,
      })
      return undefined
    }
    const id = identity(childId, "subagent")
    const row = this.#snapshot.projection?.rowById[id]
    if (!row) throw new Error("Selected subagent is absent from the canonical projection.")
    for (const ancestorId of this.#snapshot.projection?.hierarchy.nodes[id]?.path ?? []) {
      this.#expanded.add(ancestorId)
    }
    this.#replace({
      selectedId: id,
      selected: row,
      expandedIds: this.#expandedList(),
      visibleRows: this.#visibleRows(this.#snapshot.projection),
    })
    return row
  }

  setFilter(filter: SubagentPanelFilter): void {
    this.#assertAvailable()
    if (!["active", "settled", "failed", "quarantined", "all"].includes(filter)) {
      throw new TypeError("Unsupported subagent panel filter.")
    }
    this.#replace({
      filter,
      visibleRows: this.#visibleRows(this.#snapshot.projection, filter, this.#snapshot.query),
    })
  }

  setQuery(query: string): void {
    this.#assertAvailable()
    const selected = text(query, "", 1_024)
    this.#replace({
      query: selected,
      visibleRows: this.#visibleRows(this.#snapshot.projection, this.#snapshot.filter, selected),
    })
  }

  toggleExpanded(childId: string): void {
    this.#assertAvailable()
    const id = identity(childId, "subagent")
    if (!this.#snapshot.projection?.rowById[id]) {
      throw new Error("Cannot expand a subagent outside the canonical projection.")
    }
    if (this.#expanded.has(id)) this.#expanded.delete(id)
    else this.#expanded.add(id)
    this.#replace({
      expandedIds: this.#expandedList(),
      visibleRows: this.#visibleRows(this.#snapshot.projection),
    })
  }

  expandAll(): void {
    this.#assertAvailable()
    for (const id of this.#snapshot.projection?.hierarchy.order ?? []) {
      this.#expanded.add(id)
    }
    this.#replace({
      expandedIds: this.#expandedList(),
      visibleRows: this.#visibleRows(this.#snapshot.projection),
    })
  }

  collapseAll(): void {
    this.#assertAvailable()
    this.#expanded.clear()
    this.#replace({
      expandedIds: Object.freeze([]),
      visibleRows: this.#visibleRows(this.#snapshot.projection),
    })
  }

  async kill(input: {
    childId?: string
    reason: string
    nonce?: string
    idempotencyKey?: string
  }) {
    this.#assertAvailable()
    const row = this.#controlRow(input.childId)
    return this.controls.kill({
      childId: row.id,
      reason: assertBoundedText("Subagent kill reason", input.reason, 1, 7_000),
      expectedRevision: row.revision,
      ownerId: row.ownerId,
      attempt: row.attempt,
      parentId: row.parentId,
      nonce: input.nonce,
      idempotencyKey: input.idempotencyKey,
      sealed: this.#snapshot.sealed,
    })
  }

  async steer(input: {
    childId?: string
    instruction: string
    reason?: string
    nonce?: string
    idempotencyKey?: string
  }) {
    this.#assertAvailable()
    const row = this.#controlRow(input.childId)
    return this.controls.steer({
      childId: row.id,
      instruction: assertBoundedText(
        "Subagent steering instruction",
        input.instruction,
        1,
        60_000,
      ),
      reason: input.reason,
      expectedRevision: row.revision,
      ownerId: row.ownerId,
      attempt: row.attempt,
      parentId: row.parentId,
      nonce: input.nonce,
      idempotencyKey: input.idempotencyKey,
      sealed: this.#snapshot.sealed,
    })
  }

  setSealed(sealed: boolean): void {
    this.#assertAvailable()
    this.controls.setSealed(sealed)
    this.#replace({
      sealed,
      controls: this.controls.getSnapshot(),
    })
  }

  acknowledgeIncident(id: string): void {
    this.#assertAvailable()
    this.lifecycle.acknowledge(id)
    this.#replace({ incidents: this.lifecycle.list() })
  }

  acknowledgeSelectedIncidents(): number {
    this.#assertAvailable()
    const selectedId = this.#snapshot.selectedId
    if (!selectedId) return 0
    const changed = this.lifecycle.acknowledgeChild(selectedId)
    this.#replace({ incidents: this.lifecycle.list() })
    return changed
  }

  openViewer(): void {
    this.#assertEnabled()
    if (this.#snapshot.viewerOpen) return
    this.#generation += 1
    this.#attachProjection()
    this.#replace({
      viewerOpen: true,
      connected: this.#online(),
      restoredAt: this.#now().toISOString(),
      restoreGeneration: this.#generation,
    })
    this.controls.setConnected(this.#online())
    this.#refresh("reopen")
  }

  closeViewer(): void {
    if (this.#closed || !this.#snapshot.viewerOpen) return
    this.#detachProjection()
    this.#expanded.clear()
    this.reconnect.capture({
      generation: this.#generation,
      connected: this.#snapshot.connected,
      viewerOpen: false,
      projection: this.#snapshot.projection,
      capturedAt: this.#now().toISOString(),
    })
    this.#replace({
      viewerOpen: false,
      selectedId: undefined,
      selected: undefined,
      expandedIds: Object.freeze([]),
      visibleRows: Object.freeze([]),
    })
  }

  disconnected(reason = "Browser transport disconnected."): void {
    if (this.#closed) return
    this.controls.setConnected(false)
    this.reconnect.capture({
      generation: this.#generation,
      connected: false,
      viewerOpen: this.#snapshot.viewerOpen,
      projection: this.#snapshot.projection,
      capturedAt: this.#now().toISOString(),
    })
    this.#replace({
      connected: false,
      controls: this.controls.getSnapshot(),
      disabledReason: this.#snapshot.enabled ? text(reason) : this.#snapshot.disabledReason,
    })
  }

  reconnected(): void {
    this.#assertEnabled()
    this.#generation += 1
    const connected = this.#online()
    this.controls.setConnected(connected)
    if (this.#snapshot.viewerOpen && !this.#unsubscribeProjection) {
      this.#attachProjection()
    }
    this.#refresh("reconnect")
    this.reconnect.capture({
      generation: this.#generation,
      connected,
      viewerOpen: this.#snapshot.viewerOpen,
      projection: this.#snapshot.projection,
      capturedAt: this.#now().toISOString(),
    })
    this.#replace({
      connected,
      disabledReason: undefined,
      restoredAt: this.#now().toISOString(),
      restoreGeneration: this.#generation,
      controls: this.controls.getSnapshot(),
    })
  }

  disable(reason = "Subagent panel is disabled."): void {
    if (this.#closed) return
    this.#disabledReason = text(reason, "Subagent panel is disabled.")
    this.#detachProjection()
    this.controls.disable(this.#disabledReason)
    this.lifecycle.disable(this.#disabledReason)
    this.quarantine.disable(this.#disabledReason)
    this.reconnect.disable()
    this.#replace({
      enabled: false,
      connected: false,
      disabledReason: this.#disabledReason,
      visibleRows: Object.freeze([]),
      controls: this.controls.getSnapshot(),
    })
  }

  enable(): void {
    if (this.#closed) return
    this.controls.enable()
    this.lifecycle.enable()
    this.quarantine.enable()
    this.reconnect.enable()
    if (this.#snapshot.viewerOpen) this.#attachProjection()
    this.#replace({
      enabled: true,
      connected: this.#online(),
      disabledReason: undefined,
      controls: this.controls.getSnapshot(),
    })
    this.#refresh("enable")
  }

  close(reason = "Subagent panel controller closed."): void {
    if (this.#closed) return
    this.#detachProjection()
    this.controls.close(reason)
    this.lifecycle.disable(reason)
    this.quarantine.disable(reason)
    this.reconnect.disable()
    this.#unsubscribeControls()
    this.#closed = true
    this.#listeners.clear()
  }

  #attachProjection(): void {
    if (this.#unsubscribeProjection || this.#closed) return
    this.#unsubscribeProjection = this.#projections.subscribe(() => {
      if (this.#closed || !this.#snapshot.viewerOpen || !this.#snapshot.enabled) return
      this.#refresh("projection")
    })
  }

  #detachProjection(): void {
    this.#unsubscribeProjection?.()
    this.#unsubscribeProjection = undefined
  }

  #refresh(source: "bind" | "projection" | "reopen" | "reconnect" | "enable"): void {
    const taskId = this.#snapshot.taskId
    if (!taskId || this.#closed || !this.#snapshot.enabled) return
    const projection = buildSubagentProjection(this.#projections.state, taskId, {
      ...this.#projectionOptions,
      runId: this.#snapshot.runId,
      sessionId: this.#snapshot.sessionId,
      nowMs: this.#now().getTime(),
    })
    const incidents = this.lifecycle.observe(projection)
    this.quarantine.replace(projection)
    const selected = this.#snapshot.selectedId
      ? projection.rowById[this.#snapshot.selectedId]
      : undefined
    if (source !== "projection") {
      this.reconnect.capture({
        generation: this.#generation,
        connected: projection.connected && this.#online(),
        viewerOpen: this.#snapshot.viewerOpen,
        projection,
        capturedAt: this.#now().toISOString(),
      })
    }
    this.#replace({
      projection,
      selectedId: selected?.id,
      selected,
      connected: projection.connected && this.#online(),
      visibleRows: this.#visibleRows(projection),
      incidents,
    })
    this.controls.setConnected(projection.connected && this.#online())
    this.controls.reconcile()
  }

  #visibleRows(
    projection = this.#snapshot.projection,
    filter = this.#snapshot.filter,
    query = this.#snapshot.query,
  ): readonly SubagentRow[] {
    if (!projection || !this.#snapshot.viewerOpen) return Object.freeze([])
    const normalizedQuery = query.toLowerCase()
    const matches = (row: SubagentRow) => {
      if (!filterMatches(row, filter)) return false
      if (!normalizedQuery) return true
      return [
        row.id,
        row.parentId,
        row.ownerId,
        row.definitionName,
        row.lifecycle,
        row.routeId,
        row.placementId,
        row.checkpointId,
        row.error?.code,
        row.error?.message,
        row.result?.summary,
        ...row.scope.childTools,
        ...row.scope.skills,
      ]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(normalizedQuery))
    }
    return visibleHierarchyRows(projection.hierarchy, this.#expanded, matches)
  }

  #expandedList(): readonly string[] {
    const order = this.#snapshot.projection?.hierarchy.order ?? []
    return Object.freeze(order.filter((id) => this.#expanded.has(id)))
  }

  #controlRow(childId?: string): SubagentRow {
    const id = childId ? identity(childId, "subagent") : this.#snapshot.selectedId
    if (!id) throw new Error("Select an exact subagent before submitting control.")
    const row = this.#snapshot.projection?.rowById[id]
    if (!row) throw new Error("Selected subagent is absent from the canonical projection.")
    return row
  }

  #replace(patch: Partial<SubagentPanelSnapshot>): void {
    this.#snapshot = Object.freeze({
      ...this.#snapshot,
      ...patch,
      revision: this.#snapshot.revision + 1,
    })
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        continue
      }
    }
  }

  #assertEnabled(): void {
    if (this.#closed) throw new Error("Subagent panel controller is closed.")
    if (!this.#snapshot.enabled) throw new Error(this.#disabledReason)
  }

  #assertAvailable(): void {
    this.#assertEnabled()
    if (!this.#snapshot.viewerOpen) {
      throw new Error("Subagent viewer is closed.")
    }
  }
}

function filterMatches(
  row: SubagentRow,
  filter: SubagentPanelFilter,
): boolean {
  if (filter === "active") return !row.terminal
  if (filter === "settled") return row.terminal
  if (filter === "failed") return row.lifecycle === "failed" || row.crashed
  if (filter === "quarantined") return row.quarantined || row.lateResults.length > 0
  return true
}

export function sortSubagentRows(
  rows: readonly SubagentRow[],
): readonly SubagentRow[] {
  return sortStable(rows, (left, right) =>
    compareText(left.parentId, right.parentId) ||
    left.depth - right.depth ||
    left.sequence - right.sequence ||
    compareText(left.id, right.id))
}
