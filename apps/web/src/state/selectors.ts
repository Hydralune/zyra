import {
  EntityLifecycle,
  ProjectionDomain,
  type ArtifactProjection,
  type CanonicalProjectionState,
  type CausalEventProjection,
  type CommandProjection,
  type DomainProjection,
  type MemoryProjection,
  type MutationProjection,
  type NodeProjection,
  type OverlayProjection,
  type PermissionProjection,
  type ProjectionChangeSet,
  type ProjectionCursor,
  type ProjectionDomainValue,
  type ProjectionEntity,
  type ProjectionSelector,
  type RecoveryProjection,
  type SchedulerProjection,
  type SessionProjection,
  type TaskProjection,
  type ToolProjection,
  type WorkerProjection,
} from "./contracts.ts"
import { compareCausalEvents } from "./causality.ts"

export type SelectorListener<T> = (
  value: T,
  previous: T,
  changes: ProjectionChangeSet,
) => void

interface SelectorSubscription<T> {
  id: number
  selector: ProjectionSelector<T>
  listener: SelectorListener<T>
  value: T
  closed: boolean
}

export class ProjectionSelectorRegistry {
  readonly #subscriptions = new Map<number, SelectorSubscription<unknown>>()
  readonly #selectors = new Map<string, ProjectionSelector<unknown>>()
  #nextId = 1
  #disabled = false

  register<T>(selector: ProjectionSelector<T>): ProjectionSelector<T> {
    if (this.#disabled) {
      throw new Error("Projection selector registry is disabled.")
    }
    const existing = this.#selectors.get(selector.key)
    if (existing && existing !== selector) {
      if (!sameDependencies(existing.dependencies, selector.dependencies)) {
        throw new Error(
          `Projection selector ${selector.key} was registered with conflicting dependencies.`,
        )
      }
    }
    this.#selectors.set(selector.key, selector as ProjectionSelector<unknown>)
    return selector
  }

  get<T>(key: string): ProjectionSelector<T> | undefined {
    return this.#selectors.get(key) as ProjectionSelector<T> | undefined
  }

  select<T>(
    state: CanonicalProjectionState,
    selector: ProjectionSelector<T>,
  ): T {
    if (this.#disabled) {
      throw new Error("Projection selector registry is disabled.")
    }
    this.register(selector)
    return selector.select(state)
  }

  subscribe<T>(
    state: CanonicalProjectionState,
    selector: ProjectionSelector<T>,
    listener: SelectorListener<T>,
  ): { id: number; close(): void } {
    if (this.#disabled) {
      throw new Error("Projection selector registry is disabled.")
    }
    this.register(selector)
    const id = this.#nextId++
    const subscription: SelectorSubscription<T> = {
      id,
      selector,
      listener,
      value: selector.select(state),
      closed: false,
    }
    this.#subscriptions.set(
      id,
      subscription as SelectorSubscription<unknown>,
    )
    return {
      id,
      close: () => {
        if (subscription.closed) return
        subscription.closed = true
        this.#subscriptions.delete(id)
      },
    }
  }

  notify(
    state: CanonicalProjectionState,
    changes: ProjectionChangeSet,
  ): number {
    if (this.#disabled) return 0
    let notified = 0
    for (const subscription of this.#subscriptions.values()) {
      if (subscription.closed) continue
      if (!selectorAffected(subscription.selector, changes)) continue
      const next = subscription.selector.select(state)
      const equals = subscription.selector.equals ?? Object.is
      if (equals(next, subscription.value)) continue
      const previous = subscription.value
      subscription.value = next
      subscription.listener(next, previous, changes)
      notified += 1
    }
    return notified
  }

  snapshot<T>(
    state: CanonicalProjectionState,
    selector: ProjectionSelector<T>,
  ): {
    getSnapshot(): T
    subscribe(listener: () => void): () => void
  } {
    let current = this.select(state, selector)
    const listeners = new Set<() => void>()
    let close: (() => void) | undefined
    return {
      getSnapshot: () => current,
      subscribe: (listener) => {
        listeners.add(listener)
        if (!close) {
          const subscription = this.subscribe(state, selector, (value) => {
            current = value
            for (const item of listeners) item()
          })
          close = subscription.close
        }
        return () => {
          listeners.delete(listener)
          if (listeners.size > 0) return
          close?.()
          close = undefined
        }
      },
    }
  }

  disable(): void {
    this.#disabled = true
    this.clear()
  }

  enable(): void {
    this.#disabled = false
  }

  clear(): void {
    for (const subscription of this.#subscriptions.values()) {
      subscription.closed = true
    }
    this.#subscriptions.clear()
  }

  audit(): {
    disabled: boolean
    selectors: readonly string[]
    subscriptions: number
  } {
    return {
      disabled: this.#disabled,
      selectors: Object.freeze([...this.#selectors.keys()].sort()),
      subscriptions: this.#subscriptions.size,
    }
  }
}

function sameDependencies(
  left: readonly string[],
  right: readonly string[],
): boolean {
  if (left.length !== right.length) return false
  return left.every((value, index) => value === right[index])
}

export function selectorAffected(
  selector: ProjectionSelector<unknown>,
  changes: ProjectionChangeSet,
): boolean {
  if (selector.dependencies.length === 0) return true
  for (const dependency of selector.dependencies) {
    if (dependency === "*") return true
    if (dependency === "diagnostics" && changes.diagnostic) return true
    const separator = dependency.indexOf(":")
    const category =
      separator < 0 ? dependency : dependency.slice(0, separator)
    const id = separator < 0 ? undefined : dependency.slice(separator + 1)
    if (category === "revision") return true
    if (category === "domain") {
      if (id && changes.domains.has(id as ProjectionDomainValue)) return true
      continue
    }
    if (category === "task") {
      if (id && changes.taskIds.has(id)) return true
      continue
    }
    if (category === "entity") {
      if (id && changes.entityKeys.has(id)) return true
      continue
    }
    if (category === "event") {
      if (id && changes.eventIds.has(id)) return true
      continue
    }
    if (category === "causal") {
      if (id && changes.causalKeys.has(id)) return true
      continue
    }
    if (category === "cursor") {
      if (id && changes.cursorTaskIds.has(id)) return true
      continue
    }
  }
  return false
}

export function projectionSelector<T>(
  key: string,
  dependencies: readonly string[],
  select: (state: CanonicalProjectionState) => T,
  equals?: (left: T, right: T) => boolean,
): ProjectionSelector<T> {
  return Object.freeze({
    key,
    dependencies: Object.freeze([...dependencies]),
    select,
    equals,
  })
}

export function selectRevision(): ProjectionSelector<number> {
  return projectionSelector("projection.revision", ["revision"], (state) => state.revision)
}

export function selectTask(taskId: string): ProjectionSelector<TaskProjection | undefined> {
  return projectionSelector(
    `task.${taskId}`,
    [`task:${taskId}`, `entity:task:${taskId}`],
    (state) => state.tasks[taskId],
  )
}

export function selectTasks(options: {
  lifecycle?: readonly string[]
  terminal?: boolean
  limit?: number
} = {}): ProjectionSelector<readonly TaskProjection[]> {
  const lifecycle = new Set(options.lifecycle ?? [])
  return projectionSelector(
    `tasks.${JSON.stringify(options)}`,
    ["domain:task"],
    (state) => {
      let values = Object.values(state.tasks)
      if (lifecycle.size > 0) {
        values = values.filter((item) => lifecycle.has(item.lifecycle))
      }
      if (options.terminal !== undefined) {
        values = values.filter((item) => item.terminal === options.terminal)
      }
      values.sort(compareEntitiesRecent)
      return Object.freeze(values.slice(0, options.limit ?? values.length))
    },
    sameEntityList,
  )
}

export function selectNodesForTask(
  taskId: string,
): ProjectionSelector<readonly NodeProjection[]> {
  return projectionSelector(
    `task.${taskId}.nodes`,
    [`task:${taskId}`, "domain:node"],
    (state) =>
      Object.freeze(
        Object.values(state.nodes)
          .filter((item) => item.taskId === taskId)
          .sort(compareEntities),
      ),
    sameEntityList,
  )
}

export function selectNode(nodeId: string): ProjectionSelector<NodeProjection | undefined> {
  return projectionSelector(
    `node.${nodeId}`,
    [`entity:node:${nodeId}`],
    (state) => state.nodes[nodeId],
  )
}

export function selectWorkersForTask(
  taskId: string,
): ProjectionSelector<readonly WorkerProjection[]> {
  return projectionSelector(
    `task.${taskId}.workers`,
    [`task:${taskId}`, "domain:worker"],
    (state) =>
      Object.freeze(
        Object.values(state.workers)
          .filter((item) => item.taskId === taskId)
          .sort(compareWorkers),
      ),
    sameEntityList,
  )
}

export function selectWorker(
  workerId: string,
): ProjectionSelector<WorkerProjection | undefined> {
  return projectionSelector(
    `worker.${workerId}`,
    [`entity:worker:${workerId}`],
    (state) => state.workers[workerId],
  )
}

export function selectActiveWorkers(
  taskId?: string,
): ProjectionSelector<readonly WorkerProjection[]> {
  return projectionSelector(
    `workers.active.${taskId ?? "*"}`,
    taskId ? [`task:${taskId}`, "domain:worker"] : ["domain:worker"],
    (state) =>
      Object.freeze(
        Object.values(state.workers)
          .filter((item) => !item.terminal)
          .filter((item) => !taskId || item.taskId === taskId)
          .sort(compareWorkers),
      ),
    sameEntityList,
  )
}

export function selectToolsForWorker(
  workerId: string,
): ProjectionSelector<readonly ToolProjection[]> {
  return projectionSelector(
    `worker.${workerId}.tools`,
    [`entity:worker:${workerId}`, "domain:tool"],
    (state) =>
      Object.freeze(
        Object.values(state.tools)
          .filter((item) => item.workerId === workerId)
          .sort(compareEntities),
      ),
    sameEntityList,
  )
}

export function selectTool(
  toolCallId: string,
): ProjectionSelector<ToolProjection | undefined> {
  return projectionSelector(
    `tool.${toolCallId}`,
    [`entity:tool:${toolCallId}`, `causal:tool:${toolCallId}`],
    (state) => state.tools[toolCallId],
  )
}

export function selectArtifactsForTask(
  taskId: string,
  options: { includeDeleted?: boolean; mediaType?: string } = {},
): ProjectionSelector<readonly ArtifactProjection[]> {
  return projectionSelector(
    `task.${taskId}.artifacts.${JSON.stringify(options)}`,
    [`task:${taskId}`, "domain:artifact"],
    (state) =>
      Object.freeze(
        Object.values(state.artifacts)
          .filter((item) => item.taskId === taskId)
          .filter((item) => options.includeDeleted || !item.deleted)
          .filter(
            (item) =>
              !options.mediaType || item.mediaType.startsWith(options.mediaType),
          )
          .sort(compareEntitiesRecent),
      ),
    sameEntityList,
  )
}

export function selectArtifact(
  artifactId: string,
): ProjectionSelector<ArtifactProjection | undefined> {
  return projectionSelector(
    `artifact.${artifactId}`,
    [`entity:artifact:${artifactId}`, `causal:artifact:${artifactId}`],
    (state) => state.artifacts[artifactId],
  )
}

export function selectMemoriesForTask(
  taskId: string,
): ProjectionSelector<readonly MemoryProjection[]> {
  return projectionSelector(
    `task.${taskId}.memories`,
    [`task:${taskId}`, "domain:memory"],
    (state) =>
      Object.freeze(
        Object.values(state.memories)
          .filter((item) => item.taskId === taskId)
          .sort(
            (left, right) =>
              (right.score ?? 0) - (left.score ?? 0) ||
              compareEntitiesRecent(left, right),
          ),
      ),
    sameEntityList,
  )
}

export function selectSchedulerForTask(
  taskId: string,
): ProjectionSelector<readonly SchedulerProjection[]> {
  return projectionSelector(
    `task.${taskId}.scheduler`,
    [`task:${taskId}`, "domain:scheduler"],
    (state) =>
      Object.freeze(
        Object.values(state.schedulers)
          .filter((item) => item.taskId === taskId)
          .sort(compareEntitiesRecent),
      ),
    sameEntityList,
  )
}

export function selectRecoveriesForTask(
  taskId: string,
): ProjectionSelector<readonly RecoveryProjection[]> {
  return projectionSelector(
    `task.${taskId}.recoveries`,
    [`task:${taskId}`, "domain:recovery"],
    (state) =>
      Object.freeze(
        Object.values(state.recoveries)
          .filter((item) => item.taskId === taskId)
          .sort(compareEntities),
      ),
    sameEntityList,
  )
}

export function selectRecovery(
  recoveryId: string,
): ProjectionSelector<RecoveryProjection | undefined> {
  return projectionSelector(
    `recovery.${recoveryId}`,
    [`entity:recovery:${recoveryId}`, `causal:recovery:${recoveryId}`],
    (state) => state.recoveries[recoveryId],
  )
}

export function selectCommandsForTask(
  taskId: string,
  options: { activeOnly?: boolean } = {},
): ProjectionSelector<readonly CommandProjection[]> {
  return projectionSelector(
    `task.${taskId}.commands.${options.activeOnly ? "active" : "all"}`,
    [`task:${taskId}`, "domain:command"],
    (state) => {
      const priority = { now: 0, next: 1, later: 2 } as const
      return Object.freeze(
        Object.values(state.commands)
          .filter((item) => item.taskId === taskId)
          .filter((item) => !options.activeOnly || !item.terminal)
          .sort(
            (left, right) =>
              priority[left.priority] - priority[right.priority] ||
              (left.queuePosition ?? Number.MAX_SAFE_INTEGER) -
                (right.queuePosition ?? Number.MAX_SAFE_INTEGER) ||
              compareEntities(left, right),
          ),
      )
    },
    sameEntityList,
  )
}

export function selectCommand(
  commandId: string,
): ProjectionSelector<CommandProjection | undefined> {
  return projectionSelector(
    `command.${commandId}`,
    [`entity:command:${commandId}`, `causal:command:${commandId}`],
    (state) => state.commands[commandId],
  )
}

export function selectPendingPermissions(
  taskId?: string,
): ProjectionSelector<readonly PermissionProjection[]> {
  return projectionSelector(
    `permissions.pending.${taskId ?? "*"}`,
    taskId ? [`task:${taskId}`, "domain:permission"] : ["domain:permission"],
    (state) =>
      Object.freeze(
        Object.values(state.permissions)
          .filter((item) => !item.resolvedAt && !item.terminal)
          .filter((item) => !taskId || item.taskId === taskId)
          .sort(comparePermissionUrgency),
      ),
    sameEntityList,
  )
}

export function selectPermission(
  permissionId: string,
): ProjectionSelector<PermissionProjection | undefined> {
  return projectionSelector(
    `permission.${permissionId}`,
    [`entity:permission:${permissionId}`],
    (state) => state.permissions[permissionId],
  )
}

export function selectActiveOverlays(
  taskId?: string,
  modalOnly = false,
): ProjectionSelector<readonly OverlayProjection[]> {
  return projectionSelector(
    `overlays.active.${taskId ?? "*"}.${modalOnly ? "modal" : "all"}`,
    taskId ? [`task:${taskId}`, "domain:overlay"] : ["domain:overlay"],
    (state) =>
      Object.freeze(
        Object.values(state.overlays)
          .filter((item) => !item.closedAt && !item.terminal)
          .filter((item) => !taskId || item.taskId === taskId)
          .filter((item) => !modalOnly || item.modal)
          .sort(compareEntities),
      ),
    sameEntityList,
  )
}

export function selectSessionsForTask(
  taskId: string,
): ProjectionSelector<readonly SessionProjection[]> {
  return projectionSelector(
    `task.${taskId}.sessions`,
    [`task:${taskId}`, "domain:session"],
    (state) =>
      Object.freeze(
        Object.values(state.sessions)
          .filter((item) => item.taskId === taskId)
          .sort(compareSessionHierarchy),
      ),
    sameEntityList,
  )
}

export function selectSession(
  sessionId: string,
): ProjectionSelector<SessionProjection | undefined> {
  return projectionSelector(
    `session.${sessionId}`,
    [`entity:session:${sessionId}`],
    (state) => state.sessions[sessionId],
  )
}

export function selectCursor(taskId: string): ProjectionSelector<ProjectionCursor | undefined> {
  return projectionSelector(
    `cursor.${taskId}`,
    [`cursor:${taskId}`],
    (state) => state.cursors[taskId],
  )
}

export function selectEvent(
  eventId: string,
): ProjectionSelector<CausalEventProjection | undefined> {
  return projectionSelector(
    `event.${eventId}`,
    [`event:${eventId}`],
    (state) => state.causality.byEvent[eventId],
  )
}

export function selectEventsForTask(
  taskId: string,
  options: {
    afterSequence?: number
    beforeSequence?: number
    effectiveOnly?: boolean
    terminalOnly?: boolean
    limit?: number
  } = {},
): ProjectionSelector<readonly CausalEventProjection[]> {
  return projectionSelector(
    `task.${taskId}.events.${JSON.stringify(options)}`,
    [`causal:task:${taskId}`],
    (state) => {
      let values = eventsFromIndex(state, state.causality.byTask[taskId] ?? [])
      if (options.afterSequence !== undefined) {
        values = values.filter((item) => item.sequence > options.afterSequence!)
      }
      if (options.beforeSequence !== undefined) {
        values = values.filter((item) => item.sequence < options.beforeSequence!)
      }
      if (options.effectiveOnly) values = values.filter((item) => item.effective)
      if (options.terminalOnly) values = values.filter((item) => item.terminal)
      if (options.limit !== undefined && values.length > options.limit) {
        values = values.slice(values.length - options.limit)
      }
      return Object.freeze(values)
    },
    sameCausalList,
  )
}

export function selectEventsForSpan(
  spanId: string,
): ProjectionSelector<readonly CausalEventProjection[]> {
  return projectionSelector(
    `span.${spanId}.events`,
    [`causal:span:${spanId}`, `causal:parent-span:${spanId}`],
    (state) =>
      Object.freeze(
        mergeEvents(
          state,
          state.causality.bySpan[spanId] ?? [],
          state.causality.byParentSpan[spanId] ?? [],
        ),
      ),
    sameCausalList,
  )
}

export function selectEventsForTool(
  toolCallId: string,
): ProjectionSelector<readonly CausalEventProjection[]> {
  return causalIndexSelector(
    `tool.${toolCallId}.events`,
    `tool:${toolCallId}`,
    (state) => state.causality.byToolCall[toolCallId] ?? [],
  )
}

export function selectEventsForArtifact(
  artifactId: string,
): ProjectionSelector<readonly CausalEventProjection[]> {
  return causalIndexSelector(
    `artifact.${artifactId}.events`,
    `artifact:${artifactId}`,
    (state) => state.causality.byArtifact[artifactId] ?? [],
  )
}

export function selectEventsForFailure(
  failureId: string,
): ProjectionSelector<readonly CausalEventProjection[]> {
  return causalIndexSelector(
    `failure.${failureId}.events`,
    `failure:${failureId}`,
    (state) => state.causality.byFailure[failureId] ?? [],
  )
}

export function selectEventsForRecovery(
  recoveryId: string,
): ProjectionSelector<readonly CausalEventProjection[]> {
  return causalIndexSelector(
    `recovery.${recoveryId}.events`,
    `recovery:${recoveryId}`,
    (state) => state.causality.byRecovery[recoveryId] ?? [],
  )
}

export function selectEventsForCorrelation(
  correlationId: string,
): ProjectionSelector<readonly CausalEventProjection[]> {
  return causalIndexSelector(
    `correlation.${correlationId}.events`,
    `correlation:${correlationId}`,
    (state) => state.causality.byCorrelation[correlationId] ?? [],
  )
}

export function selectEventsCausedBy(
  eventId: string,
): ProjectionSelector<readonly CausalEventProjection[]> {
  return causalIndexSelector(
    `causation.${eventId}.events`,
    `causation:${eventId}`,
    (state) => state.causality.byCausation[eventId] ?? [],
  )
}

export function selectCheckpointEvents(
  checkpointId: string,
): ProjectionSelector<readonly CausalEventProjection[]> {
  return causalIndexSelector(
    `checkpoint.${checkpointId}.events`,
    `checkpoint:${checkpointId}`,
    (state) => state.causality.byCheckpoint[checkpointId] ?? [],
  )
}

function causalIndexSelector(
  key: string,
  dependency: string,
  ids: (state: CanonicalProjectionState) => readonly string[],
): ProjectionSelector<readonly CausalEventProjection[]> {
  return projectionSelector(
    key,
    [`causal:${dependency}`],
    (state) => Object.freeze(eventsFromIndex(state, ids(state))),
    sameCausalList,
  )
}

export function selectMutationsForTask(
  taskId: string,
): ProjectionSelector<readonly MutationProjection[]> {
  return projectionSelector(
    `task.${taskId}.mutations`,
    [`task:${taskId}`],
    (state) =>
      Object.freeze(
        Object.values(state.mutations)
          .filter((item) => item.taskId === taskId)
          .sort(
            (left, right) =>
              left.sequence - right.sequence || left.id.localeCompare(right.id),
          ),
      ),
    sameMutationList,
  )
}

export function selectEntity(
  domain: ProjectionDomainValue,
  id: string,
): ProjectionSelector<DomainProjection | undefined> {
  return projectionSelector(
    `${domain}.${id}`,
    [`entity:${domain}:${id}`],
    (state) => tableFromState(state, domain)[id] as DomainProjection | undefined,
  )
}

export function selectTaskSummary(taskId: string): ProjectionSelector<{
  task?: TaskProjection
  nodes: number
  workers: number
  activeWorkers: number
  tools: number
  artifacts: number
  pendingPermissions: number
  queuedCommands: number
  recoveries: number
  sessions: number
  lastSequence: number
}> {
  return projectionSelector(
    `task.${taskId}.summary`,
    [
      `task:${taskId}`,
      "domain:node",
      "domain:worker",
      "domain:tool",
      "domain:artifact",
      "domain:permission",
      "domain:command",
      "domain:recovery",
      "domain:session",
      `cursor:${taskId}`,
    ],
    (state) => {
      const matches = <T extends ProjectionEntity>(values: readonly T[]) =>
        values.filter((item) => item.taskId === taskId)
      const workers = matches(Object.values(state.workers))
      return Object.freeze({
        task: state.tasks[taskId],
        nodes: matches(Object.values(state.nodes)).length,
        workers: workers.length,
        activeWorkers: workers.filter((item) => !item.terminal).length,
        tools: matches(Object.values(state.tools)).length,
        artifacts: matches(Object.values(state.artifacts)).filter(
          (item) => !item.deleted,
        ).length,
        pendingPermissions: matches(Object.values(state.permissions)).filter(
          (item) => !item.resolvedAt && !item.terminal,
        ).length,
        queuedCommands: matches(Object.values(state.commands)).filter(
          (item) => !item.terminal,
        ).length,
        recoveries: matches(Object.values(state.recoveries)).length,
        sessions: matches(Object.values(state.sessions)).length,
        lastSequence: state.cursors[taskId]?.committedSequence ?? 0,
      })
    },
    shallowObjectEqual,
  )
}

export function selectCausalNeighborhood(
  eventId: string,
): ProjectionSelector<{
  event?: CausalEventProjection
  cause?: CausalEventProjection
  effects: readonly CausalEventProjection[]
  sameSpan: readonly CausalEventProjection[]
  entities: readonly DomainProjection[]
}> {
  return projectionSelector(
    `event.${eventId}.neighborhood`,
    [`event:${eventId}`, `causal:causation:${eventId}`],
    (state) => {
      const event = state.causality.byEvent[eventId]
      if (!event) {
        return Object.freeze({
          event: undefined,
          cause: undefined,
          effects: Object.freeze([]),
          sameSpan: Object.freeze([]),
          entities: Object.freeze([]),
        })
      }
      const cause = event.causationId
        ? state.causality.byEvent[event.causationId]
        : undefined
      const effects = eventsFromIndex(
        state,
        state.causality.byCausation[eventId] ?? [],
      )
      const sameSpan = event.spanId
        ? eventsFromIndex(state, state.causality.bySpan[event.spanId] ?? [])
        : []
      const entities = event.entityRefs
        .map((ref) => {
          const separator = ref.indexOf(":")
          if (separator <= 0) return undefined
          const domain = ref.slice(0, separator) as ProjectionDomainValue
          const id = ref.slice(separator + 1)
          return tableFromState(state, domain)[id] as DomainProjection | undefined
        })
        .filter((item): item is DomainProjection => Boolean(item))
      return Object.freeze({
        event,
        cause,
        effects: Object.freeze(effects),
        sameSpan: Object.freeze(sameSpan),
        entities: Object.freeze(entities),
      })
    },
  )
}

export function selectDiagnostics() {
  return projectionSelector(
    "projection.diagnostics",
    ["diagnostics"],
    (state) => state.diagnostics,
  )
}

function eventsFromIndex(
  state: CanonicalProjectionState,
  eventIds: readonly string[],
): CausalEventProjection[] {
  return eventIds
    .map((eventId) => state.causality.byEvent[eventId])
    .filter((item): item is CausalEventProjection => Boolean(item))
    .sort(compareCausalEvents)
}

function mergeEvents(
  state: CanonicalProjectionState,
  ...lists: readonly (readonly string[])[]
): CausalEventProjection[] {
  const ids = new Set<string>()
  for (const list of lists) list.forEach((id) => ids.add(id))
  return eventsFromIndex(state, [...ids])
}

function tableFromState(
  state: CanonicalProjectionState,
  domain: ProjectionDomainValue,
): Readonly<Record<string, ProjectionEntity>> {
  switch (domain) {
    case ProjectionDomain.TASK:
      return state.tasks
    case ProjectionDomain.NODE:
      return state.nodes
    case ProjectionDomain.WORKER:
      return state.workers
    case ProjectionDomain.TOOL:
      return state.tools
    case ProjectionDomain.ARTIFACT:
      return state.artifacts
    case ProjectionDomain.MEMORY:
      return state.memories
    case ProjectionDomain.SCHEDULER:
      return state.schedulers
    case ProjectionDomain.RECOVERY:
      return state.recoveries
    case ProjectionDomain.COMMAND:
      return state.commands
    case ProjectionDomain.PERMISSION:
      return state.permissions
    case ProjectionDomain.OVERLAY:
      return state.overlays
    case ProjectionDomain.SESSION:
      return state.sessions
    case ProjectionDomain.EVENT:
      return {}
  }
}

function compareEntities(
  left: ProjectionEntity,
  right: ProjectionEntity,
): number {
  return (
    left.sequence - right.sequence ||
    left.updatedAt.localeCompare(right.updatedAt) ||
    left.id.localeCompare(right.id)
  )
}

function compareEntitiesRecent(
  left: ProjectionEntity,
  right: ProjectionEntity,
): number {
  return (
    right.sequence - left.sequence ||
    right.updatedAt.localeCompare(left.updatedAt) ||
    left.id.localeCompare(right.id)
  )
}

function compareWorkers(
  left: WorkerProjection,
  right: WorkerProjection,
): number {
  const rank = {
    [EntityLifecycle.RUNNING]: 0,
    [EntityLifecycle.WAITING_TOOL]: 1,
    [EntityLifecycle.WAITING_POLICY]: 2,
    [EntityLifecycle.RECOVERING]: 3,
    [EntityLifecycle.STARTING]: 4,
    [EntityLifecycle.ADMITTED]: 5,
    [EntityLifecycle.QUEUED]: 6,
    [EntityLifecycle.PAUSED]: 7,
    [EntityLifecycle.UNKNOWN]: 8,
    [EntityLifecycle.COMPLETED]: 9,
    [EntityLifecycle.FAILED]: 10,
    [EntityLifecycle.CANCELLED]: 11,
    [EntityLifecycle.REJECTED]: 12,
    [EntityLifecycle.EXPIRED]: 13,
    [EntityLifecycle.DELETED]: 14,
  }
  return (
    rank[left.lifecycle] - rank[right.lifecycle] ||
    compareEntities(left, right)
  )
}

function comparePermissionUrgency(
  left: PermissionProjection,
  right: PermissionProjection,
): number {
  const leftExpiry = left.expiresAt ? Date.parse(left.expiresAt) : Infinity
  const rightExpiry = right.expiresAt ? Date.parse(right.expiresAt) : Infinity
  return leftExpiry - rightExpiry || compareEntities(left, right)
}

function compareSessionHierarchy(
  left: SessionProjection,
  right: SessionProjection,
): number {
  if (left.parentSessionId === right.id) return 1
  if (right.parentSessionId === left.id) return -1
  return compareEntities(left, right)
}

function sameEntityList(
  left: readonly ProjectionEntity[],
  right: readonly ProjectionEntity[],
): boolean {
  if (left.length !== right.length) return false
  return left.every(
    (item, index) =>
      item === right[index] ||
      (item.id === right[index]?.id && item.revision === right[index]?.revision),
  )
}

function sameCausalList(
  left: readonly CausalEventProjection[],
  right: readonly CausalEventProjection[],
): boolean {
  if (left.length !== right.length) return false
  return left.every((item, index) => item.eventId === right[index]?.eventId)
}

function sameMutationList(
  left: readonly MutationProjection[],
  right: readonly MutationProjection[],
): boolean {
  if (left.length !== right.length) return false
  return left.every((item, index) => item.id === right[index]?.id)
}

function shallowObjectEqual<T extends Record<string, unknown>>(
  left: T,
  right: T,
): boolean {
  const leftKeys = Object.keys(left)
  const rightKeys = Object.keys(right)
  if (leftKeys.length !== rightKeys.length) return false
  return leftKeys.every((key) => Object.is(left[key], right[key]))
}
