import {
  TraceNodeKind,
  TraceSemanticKind,
  TraceViewKind,
  type CausalTraceProjection,
  type TraceNavigationReceipt,
  type TraceNavigationResolution,
  type TraceNavigationTarget,
  type TraceNode,
  type TraceViewKindValue,
} from "../contracts.ts"

export const TRACE_FOCUS_EVENT = "zyra:trace-focus"
export const CROSS_VIEW_FOCUS_EVENT = "zyra:cross-view-focus"

export interface TraceFocusRequest {
  taskId: string
  nodeKey?: string
  eventId?: string
  spanId?: string
  workerId?: string
  toolCallId?: string
  artifactId?: string
  mutationId?: string
  sourceView: TraceViewKindValue
  requestedAt: number
}

interface EventTargetLike {
  dispatchEvent(event: Event): boolean
  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void
  removeEventListener(type: string, listener: EventListenerOrEventListenerObject): void
}

interface ElementLike {
  scrollIntoView(options?: ScrollIntoViewOptions): void
  focus(options?: FocusOptions): void
  setAttribute(name: string, value: string): void
  removeAttribute(name: string): void
}

interface DocumentLike {
  querySelector(selectors: string): ElementLike | null
}

interface TraceShortcutElement {
  closest(selectors: string): TraceShortcutElement | null
  getAttribute(name: string): string | null
}

export interface TraceNavigationRuntimeOptions {
  document?: DocumentLike
  events?: EventTargetLike
  now?: () => number
  disabled?: boolean
  maximumHistory?: number
  highlightDurationMs?: number
}

export interface TraceNavigationAudit {
  closed: boolean
  disabled: boolean
  historyCount: number
  successfulCount: number
  unavailableCount: number
  failedCount: number
  cancelledCount: number
  listenerCount: number
  activeTargetId?: string
  lastTargetId?: string
  lastPhase?: string
}

function safeId(value: string | undefined): string | undefined {
  if (!value) return undefined
  const normalized = value.trim()
  if (!normalized || normalized.length > 512) return undefined
  if (!/^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$/.test(normalized)) return undefined
  return normalized
}

function cssAttribute(value: string): string {
  return value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/[\n\r\f]/g, " ")
}

function attributeSelector(attribute: string, value: string | undefined): string | undefined {
  const id = safeId(value)
  return id ? `[${attribute}="${cssAttribute(id)}"]` : undefined
}

function targetId(
  view: TraceViewKindValue,
  taskId: string,
  discriminator: string,
): string {
  return `trace-target:${view}:${taskId}:${discriminator}`
}

function target(
  view: TraceViewKindValue,
  taskId: string,
  discriminator: string,
  label: string,
  values: Omit<TraceNavigationTarget, "id" | "view" | "taskId" | "label">,
): TraceNavigationTarget {
  return Object.freeze({
    id: targetId(view, taskId, discriminator),
    view,
    taskId,
    label,
    ...values,
  })
}

function traceTarget(node: TraceNode): TraceNavigationTarget {
  return target(
    TraceViewKind.TRACE,
    node.taskId,
    node.key,
    `Trace · ${node.title}`,
    {
      traceNodeKey: node.key,
      eventId: node.primaryEventId,
      spanId: node.refs.spanId,
      workerId: node.refs.workerId,
      toolCallId: node.refs.toolCallId,
      artifactId: node.refs.artifactIds[0],
      mutationId: node.refs.mutationId,
      selector: attributeSelector("data-trace-node-key", node.key),
    },
  )
}

function timelineTarget(node: TraceNode): TraceNavigationTarget | undefined {
  const eventId = safeId(node.primaryEventId ?? node.eventIds[0])
  if (!eventId) return undefined
  return target(
    TraceViewKind.TIMELINE,
    node.taskId,
    eventId,
    `Timeline · ${node.title}`,
    {
      traceNodeKey: node.key,
      eventId,
      spanId: node.refs.spanId,
      workerId: node.refs.workerId,
      toolCallId: node.refs.toolCallId,
      selector: attributeSelector("data-event-id", eventId),
    },
  )
}

function topologyTargets(node: TraceNode): TraceNavigationTarget[] {
  const output: TraceNavigationTarget[] = []
  const nodeId = safeId(node.refs.nodeId)
  if (nodeId) {
    output.push(target(
      TraceViewKind.TOPOLOGY,
      node.taskId,
      `node:${nodeId}`,
      `Topology node · ${nodeId}`,
      {
        traceNodeKey: node.key,
        eventId: node.primaryEventId,
        nodeId,
        workerId: node.refs.workerId,
        routeId: node.refs.routeId,
        placementId: node.refs.placementId,
        selector: attributeSelector("data-topology-node", nodeId) ??
          attributeSelector("data-node-id", nodeId),
      },
    ))
  }
  const workerId = safeId(node.refs.workerId)
  if (workerId) {
    output.push(target(
      TraceViewKind.TOPOLOGY,
      node.taskId,
      `worker:${workerId}`,
      `Topology worker · ${workerId}`,
      {
        traceNodeKey: node.key,
        eventId: node.primaryEventId,
        nodeId,
        workerId,
        routeId: node.refs.routeId,
        placementId: node.refs.placementId,
        selector: attributeSelector("data-worker-id", workerId),
      },
    ))
  }
  const routeId = safeId(node.refs.routeId)
  if (routeId) {
    output.push(target(
      TraceViewKind.TOPOLOGY,
      node.taskId,
      `route:${routeId}`,
      `Topology route · ${routeId}`,
      {
        traceNodeKey: node.key,
        eventId: node.primaryEventId,
        routeId,
        placementId: node.refs.placementId,
        selector: attributeSelector("data-route-id", routeId),
      },
    ))
  }
  return output
}

function terminalTargets(node: TraceNode): TraceNavigationTarget[] {
  const output: TraceNavigationTarget[] = []
  const sessionId = safeId(node.refs.terminalSessionId)
  const frameId = safeId(node.refs.terminalFrameId)
  const toolCallId = safeId(node.refs.toolCallId)
  if (frameId || sessionId) {
    const discriminator = frameId ? `frame:${frameId}` : `session:${sessionId}`
    output.push(target(
      TraceViewKind.TERMINAL,
      node.taskId,
      discriminator,
      `Terminal · ${frameId ?? sessionId}`,
      {
        traceNodeKey: node.key,
        eventId: node.primaryEventId,
        spanId: node.refs.spanId,
        workerId: node.refs.workerId,
        toolCallId,
        terminalSessionId: sessionId,
        terminalFrameId: frameId,
        selector: frameId
          ? attributeSelector("data-terminal-frame-id", frameId)
          : attributeSelector("data-terminal-session-id", sessionId),
      },
    ))
  }
  if (toolCallId) {
    output.push(target(
      TraceViewKind.TERMINAL,
      node.taskId,
      `tool:${toolCallId}`,
      `Terminal tool · ${toolCallId}`,
      {
        traceNodeKey: node.key,
        eventId: node.primaryEventId,
        spanId: node.refs.spanId,
        workerId: node.refs.workerId,
        toolCallId,
        terminalSessionId: sessionId,
        terminalFrameId: frameId,
        selector: attributeSelector("data-tool-call-id", toolCallId),
      },
    ))
  }
  return output
}

function browserTargets(node: TraceNode): TraceNavigationTarget[] {
  const output: TraceNavigationTarget[] = []
  const sessionId = safeId(node.refs.browserSessionId)
  const stepId = safeId(node.refs.browserStepId)
  const actionId = safeId(node.refs.browserActionId)
  const candidates: Array<{
    id: string | undefined
    kind: "session" | "step" | "action"
    attribute: string
  }> = [
    { id: actionId, kind: "action", attribute: "data-browser-action-id" },
    { id: stepId, kind: "step", attribute: "data-browser-step-id" },
    { id: sessionId, kind: "session", attribute: "data-browser-session-id" },
  ]
  for (const candidate of candidates) {
    if (!candidate.id) continue
    output.push(target(
      TraceViewKind.BROWSER,
      node.taskId,
      `${candidate.kind}:${candidate.id}`,
      `Browser ${candidate.kind} · ${candidate.id}`,
      {
        traceNodeKey: node.key,
        eventId: node.primaryEventId,
        spanId: node.refs.spanId,
        workerId: node.refs.workerId,
        toolCallId: node.refs.toolCallId,
        browserSessionId: sessionId,
        browserStepId: stepId,
        browserActionId: actionId,
        selector: attributeSelector(candidate.attribute, candidate.id),
      },
    ))
  }
  return output
}

function artifactTargets(node: TraceNode): TraceNavigationTarget[] {
  return node.refs.artifactIds
    .map(safeId)
    .filter((artifactId): artifactId is string => Boolean(artifactId))
    .map((artifactId) => target(
      TraceViewKind.ARTIFACT,
      node.taskId,
      artifactId,
      `Artifact · ${artifactId}`,
      {
        traceNodeKey: node.key,
        eventId: node.primaryEventId,
        spanId: node.refs.spanId,
        workerId: node.refs.workerId,
        toolCallId: node.refs.toolCallId,
        artifactId,
        mutationId: node.refs.mutationId,
        selector: attributeSelector("data-artifact-id", artifactId),
      },
    ))
}

function diffTarget(node: TraceNode): TraceNavigationTarget | undefined {
  const mutationId = safeId(node.refs.mutationId)
  if (!mutationId) return undefined
  return target(
    TraceViewKind.DIFF,
    node.taskId,
    mutationId,
    `Mutation/diff · ${mutationId}`,
    {
      traceNodeKey: node.key,
      eventId: node.primaryEventId,
      spanId: node.refs.spanId,
      workerId: node.refs.workerId,
      toolCallId: node.refs.toolCallId,
      artifactId: node.refs.artifactIds[0],
      mutationId,
      selector: attributeSelector("data-mutation-id", mutationId),
    },
  )
}

export function navigationTargetsForTraceNode(
  node: TraceNode,
): readonly TraceNavigationTarget[] {
  const values: TraceNavigationTarget[] = [traceTarget(node)]
  const timeline = timelineTarget(node)
  if (timeline) values.push(timeline)
  values.push(...topologyTargets(node))
  if (
    node.semantics.includes(TraceSemanticKind.TERMINAL) ||
    node.semantics.includes(TraceSemanticKind.PTY) ||
    node.refs.terminalSessionId ||
    node.refs.terminalFrameId
  ) {
    values.push(...terminalTargets(node))
  }
  if (
    node.semantics.includes(TraceSemanticKind.BROWSER) ||
    node.refs.browserSessionId ||
    node.refs.browserStepId ||
    node.refs.browserActionId
  ) {
    values.push(...browserTargets(node))
  }
  values.push(...artifactTargets(node))
  const diff = diffTarget(node)
  if (diff) values.push(diff)
  const seen = new Set<string>()
  return Object.freeze(values.filter((value) => {
    if (seen.has(value.id)) return false
    seen.add(value.id)
    return true
  }))
}

function targetForEvent(
  projection: CausalTraceProjection,
  request: TraceFocusRequest,
): TraceNode | undefined {
  if (request.nodeKey) return projection.nodesByKey[request.nodeKey]
  const candidateKeys = [
    ...(request.eventId ? projection.indexes.nodeKeysByEvent[request.eventId] ?? [] : []),
    ...(request.spanId ? projection.indexes.nodeKeysBySpan[request.spanId] ?? [] : []),
    ...(request.workerId ? projection.indexes.nodeKeysByWorker[request.workerId] ?? [] : []),
    ...(request.toolCallId ? projection.indexes.nodeKeysByToolCall[request.toolCallId] ?? [] : []),
    ...(request.artifactId ? projection.indexes.nodeKeysByArtifact[request.artifactId] ?? [] : []),
    ...(request.mutationId ? projection.indexes.nodeKeysByMutation[request.mutationId] ?? [] : []),
  ]
  const unique = [...new Set(candidateKeys)]
  return unique
    .map((key) => projection.nodesByKey[key])
    .filter((node): node is TraceNode => Boolean(node))
    .sort((left, right) => {
      const leftEvent = left.identity.kind === TraceNodeKind.EVENT ? 0 : 1
      const rightEvent = right.identity.kind === TraceNodeKind.EVENT ? 0 : 1
      return leftEvent - rightEvent || left.sequence - right.sequence || left.key.localeCompare(right.key)
    })[0]
}

export function resolveTraceFocusRequest(
  projection: CausalTraceProjection,
  request: TraceFocusRequest,
): TraceNavigationTarget | undefined {
  if (request.taskId !== projection.taskId) return undefined
  const node = targetForEvent(projection, request)
  return node ? traceTarget(node) : undefined
}

function selectorForTarget(targetValue: TraceNavigationTarget): string | undefined {
  if (targetValue.selector) return targetValue.selector
  switch (targetValue.view) {
    case TraceViewKind.TRACE:
      return attributeSelector("data-trace-node-key", targetValue.traceNodeKey)
    case TraceViewKind.TIMELINE:
      return attributeSelector("data-event-id", targetValue.eventId)
    case TraceViewKind.TOPOLOGY:
      return attributeSelector("data-topology-node", targetValue.nodeId) ??
        attributeSelector("data-worker-id", targetValue.workerId) ??
        attributeSelector("data-route-id", targetValue.routeId)
    case TraceViewKind.TERMINAL:
      return attributeSelector("data-terminal-frame-id", targetValue.terminalFrameId) ??
        attributeSelector("data-terminal-session-id", targetValue.terminalSessionId) ??
        attributeSelector("data-tool-call-id", targetValue.toolCallId)
    case TraceViewKind.BROWSER:
      return attributeSelector("data-browser-action-id", targetValue.browserActionId) ??
        attributeSelector("data-browser-step-id", targetValue.browserStepId) ??
        attributeSelector("data-browser-session-id", targetValue.browserSessionId)
    case TraceViewKind.ARTIFACT:
      return attributeSelector("data-artifact-id", targetValue.artifactId)
    case TraceViewKind.DIFF:
      return attributeSelector("data-mutation-id", targetValue.mutationId)
  }
}

function availabilityReason(targetValue: TraceNavigationTarget): string {
  switch (targetValue.view) {
    case TraceViewKind.TRACE:
      return "The typed trace node is not mounted in the current virtual window."
    case TraceViewKind.TIMELINE:
      return "The canonical event is outside the mounted timeline window."
    case TraceViewKind.TOPOLOGY:
      return "The typed node, worker, or route is not mounted in the topology view."
    case TraceViewKind.TERMINAL:
      return "The typed PTY frame/session is not present in the terminal viewer."
    case TraceViewKind.BROWSER:
      return "The typed browser action/step/session is not present in the browser viewer."
    case TraceViewKind.ARTIFACT:
      return "The typed artifact is not mounted in the artifact catalog."
    case TraceViewKind.DIFF:
      return "The typed mutation is not mounted in the diff review."
  }
}

function fallbackTargets(
  targetValue: TraceNavigationTarget,
  projection?: CausalTraceProjection,
): readonly TraceNavigationTarget[] {
  if (!projection || !targetValue.traceNodeKey) return Object.freeze([])
  const node = projection.nodesByKey[targetValue.traceNodeKey]
  if (!node) return Object.freeze([])
  return Object.freeze(navigationTargetsForTraceNode(node).filter((candidate) => candidate.id !== targetValue.id))
}

function navigationReceipt(
  targetValue: TraceNavigationTarget,
  phase: TraceNavigationReceipt["phase"],
  attemptedAt: number,
  settledAt: number,
  selector?: string,
  reason?: string,
): TraceNavigationReceipt {
  return Object.freeze({
    id: `trace-navigation:${attemptedAt}:${targetValue.id}`,
    target: targetValue,
    phase,
    attemptedAt,
    settledAt,
    selector,
    reason,
  })
}

export class TraceNavigationRuntime {
  readonly #document?: DocumentLike
  readonly #events?: EventTargetLike
  readonly #now: () => number
  readonly #disabled: boolean
  readonly #maximumHistory: number
  readonly #highlightDurationMs: number
  readonly #listeners = new Set<() => void>()
  #history: TraceNavigationReceipt[] = []
  #active?: TraceNavigationReceipt
  #closed = false
  #successfulCount = 0
  #unavailableCount = 0
  #failedCount = 0
  #cancelledCount = 0
  #highlighted?: ElementLike
  #highlightTimer?: ReturnType<typeof setTimeout>

  constructor(options: TraceNavigationRuntimeOptions = {}) {
    this.#document = options.document ??
      (typeof document !== "undefined" ? document : undefined)
    this.#events = options.events ??
      (typeof window !== "undefined" ? window : undefined)
    this.#now = options.now ?? (() => Date.now())
    this.#disabled = options.disabled === true
    this.#maximumHistory = Math.max(1, Math.min(500, options.maximumHistory ?? 100))
    this.#highlightDurationMs = Math.max(0, Math.min(60_000, options.highlightDurationMs ?? 2_500))
  }

  get history(): readonly TraceNavigationReceipt[] {
    return Object.freeze([...this.#history])
  }

  get active(): TraceNavigationReceipt | undefined {
    return this.#active
  }

  subscribe(listener: () => void): () => void {
    if (this.#closed) return () => undefined
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  resolve(
    targetValue: TraceNavigationTarget,
    projection?: CausalTraceProjection,
  ): TraceNavigationResolution {
    this.#assertAvailable()
    if (projection && targetValue.taskId !== projection.taskId) {
      return Object.freeze({
        target: targetValue,
        available: false,
        reason: `Target belongs to ${targetValue.taskId}, not ${projection.taskId}.`,
        fallbackTargets: Object.freeze([]),
      })
    }
    const selector = selectorForTarget(targetValue)
    if (!selector) {
      return Object.freeze({
        target: targetValue,
        available: false,
        reason: "No valid typed identity can resolve this cross-view target.",
        fallbackTargets: fallbackTargets(targetValue, projection),
      })
    }
    const available = Boolean(this.#document?.querySelector(selector))
    return Object.freeze({
      target: targetValue,
      available,
      selector,
      reason: available ? undefined : availabilityReason(targetValue),
      fallbackTargets: fallbackTargets(targetValue, projection),
    })
  }

  navigate(
    targetValue: TraceNavigationTarget,
    projection?: CausalTraceProjection,
  ): TraceNavigationReceipt {
    const attemptedAt = this.#now()
    try {
      this.#assertAvailable()
      const resolution = this.resolve(targetValue, projection)
      if (!resolution.available || !resolution.selector) {
        const receipt = navigationReceipt(
          targetValue,
          "unavailable",
          attemptedAt,
          this.#now(),
          resolution.selector,
          resolution.reason,
        )
        this.#unavailableCount += 1
        this.#record(receipt)
        this.#dispatch(targetValue, receipt)
        return receipt
      }
      const element = this.#document?.querySelector(resolution.selector)
      if (!element) {
        const receipt = navigationReceipt(
          targetValue,
          "unavailable",
          attemptedAt,
          this.#now(),
          resolution.selector,
          availabilityReason(targetValue),
        )
        this.#unavailableCount += 1
        this.#record(receipt)
        this.#dispatch(targetValue, receipt)
        return receipt
      }
      element.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" })
      element.focus({ preventScroll: true })
      this.#highlight(element)
      const receipt = navigationReceipt(
        targetValue,
        "focused",
        attemptedAt,
        this.#now(),
        resolution.selector,
      )
      this.#successfulCount += 1
      this.#record(receipt)
      this.#dispatch(targetValue, receipt)
      return receipt
    } catch (error) {
      const receipt = navigationReceipt(
        targetValue,
        "failed",
        attemptedAt,
        this.#now(),
        selectorForTarget(targetValue),
        error instanceof Error ? error.message : String(error),
      )
      this.#failedCount += 1
      this.#record(receipt)
      return receipt
    }
  }

  cancel(reason = "Navigation cancelled."): TraceNavigationReceipt | undefined {
    if (!this.#active || this.#active.phase === "cancelled") return this.#active
    const receipt = navigationReceipt(
      this.#active.target,
      "cancelled",
      this.#active.attemptedAt,
      this.#now(),
      this.#active.selector,
      reason,
    )
    this.#cancelledCount += 1
    this.#record(receipt)
    return receipt
  }

  back(projection?: CausalTraceProjection): TraceNavigationReceipt | undefined {
    const focused = this.#history.filter((receipt) => receipt.phase === "focused")
    const previous = focused.at(-2)
    return previous ? this.navigate(previous.target, projection) : undefined
  }

  clearHistory(): void {
    this.#history = []
    this.#active = undefined
    this.#emit()
  }

  close(): void {
    if (this.#closed) return
    this.#closed = true
    if (this.#highlightTimer) clearTimeout(this.#highlightTimer)
    this.#highlightTimer = undefined
    this.#clearHighlight()
    this.#listeners.clear()
    this.#history = []
    this.#active = undefined
  }

  audit(): TraceNavigationAudit {
    return Object.freeze({
      closed: this.#closed,
      disabled: this.#disabled,
      historyCount: this.#history.length,
      successfulCount: this.#successfulCount,
      unavailableCount: this.#unavailableCount,
      failedCount: this.#failedCount,
      cancelledCount: this.#cancelledCount,
      listenerCount: this.#listeners.size,
      activeTargetId: this.#active?.target.id,
      lastTargetId: this.#history.at(-1)?.target.id,
      lastPhase: this.#history.at(-1)?.phase,
    })
  }

  #assertAvailable(): void {
    if (this.#closed) throw new Error("Trace navigation runtime is closed.")
    if (this.#disabled) throw new Error("Trace navigation runtime is disabled.")
  }

  #record(receipt: TraceNavigationReceipt): void {
    this.#active = receipt
    this.#history.push(receipt)
    while (this.#history.length > this.#maximumHistory) this.#history.shift()
    this.#emit()
  }

  #emit(): void {
    for (const listener of this.#listeners) listener()
  }

  #dispatch(targetValue: TraceNavigationTarget, receipt: TraceNavigationReceipt): void {
    if (!this.#events || typeof CustomEvent === "undefined") return
    this.#events.dispatchEvent(new CustomEvent(CROSS_VIEW_FOCUS_EVENT, {
      detail: Object.freeze({ target: targetValue, receipt }),
    }))
  }

  #highlight(element: ElementLike): void {
    if (this.#highlightTimer) clearTimeout(this.#highlightTimer)
    this.#clearHighlight()
    this.#highlighted = element
    element.setAttribute("data-trace-navigation-active", "true")
    if (this.#highlightDurationMs > 0) {
      this.#highlightTimer = setTimeout(() => {
        this.#clearHighlight()
        this.#highlightTimer = undefined
      }, this.#highlightDurationMs)
    }
  }

  #clearHighlight(): void {
    this.#highlighted?.removeAttribute("data-trace-navigation-active")
    this.#highlighted = undefined
  }
}

export function dispatchTraceFocusRequest(
  input: Omit<TraceFocusRequest, "requestedAt"> & { requestedAt?: number },
  events?: EventTargetLike,
): boolean {
  const targetEvents = events ?? (typeof window !== "undefined" ? window : undefined)
  if (!targetEvents || typeof CustomEvent === "undefined") return false
  const request: TraceFocusRequest = Object.freeze({
    ...input,
    requestedAt: input.requestedAt ?? Date.now(),
  })
  return targetEvents.dispatchEvent(new CustomEvent(TRACE_FOCUS_EVENT, { detail: request }))
}

export function subscribeTraceFocusRequests(
  listener: (request: TraceFocusRequest) => void,
  events?: EventTargetLike,
): () => void {
  const targetEvents = events ?? (typeof window !== "undefined" ? window : undefined)
  if (!targetEvents) return () => undefined
  const handler = (event: Event) => {
    const detail = (event as CustomEvent<unknown>).detail
    if (!detail || typeof detail !== "object") return
    const record = detail as Partial<TraceFocusRequest>
    const taskId = safeId(record.taskId)
    if (!taskId || !record.sourceView || !Object.values(TraceViewKind).includes(record.sourceView)) return
    const request: TraceFocusRequest = Object.freeze({
      taskId,
      nodeKey: safeId(record.nodeKey),
      eventId: safeId(record.eventId),
      spanId: safeId(record.spanId),
      workerId: safeId(record.workerId),
      toolCallId: safeId(record.toolCallId),
      artifactId: safeId(record.artifactId),
      mutationId: safeId(record.mutationId),
      sourceView: record.sourceView,
      requestedAt: Number.isFinite(record.requestedAt) ? Number(record.requestedAt) : Date.now(),
    })
    if (
      !request.nodeKey &&
      !request.eventId &&
      !request.spanId &&
      !request.workerId &&
      !request.toolCallId &&
      !request.artifactId &&
      !request.mutationId
    ) return
    listener(request)
  }
  targetEvents.addEventListener(TRACE_FOCUS_EVENT, handler)
  return () => targetEvents.removeEventListener(TRACE_FOCUS_EVENT, handler)
}

function shortcutView(element: TraceShortcutElement): TraceViewKindValue {
  if (element.closest("[data-browser-viewer-phase], [data-browser-action-id], [data-browser-step-id]")) {
    return TraceViewKind.BROWSER
  }
  if (element.closest("[data-terminal-state-owner], [data-terminal-session-id], [data-terminal-frame-id]")) {
    return TraceViewKind.TERMINAL
  }
  if (element.closest("[data-artifact-selector-count], [data-artifact-id]")) {
    return TraceViewKind.ARTIFACT
  }
  if (element.closest("[data-mutation-id], [data-diff-review]")) return TraceViewKind.DIFF
  if (element.closest("[data-topology-node], [data-route-id], .topology-workbench")) {
    return TraceViewKind.TOPOLOGY
  }
  if (element.closest("[data-event-id], [data-span-id], [data-worker-id]")) {
    return TraceViewKind.TIMELINE
  }
  return TraceViewKind.TRACE
}

function closestTypedAttribute(
  element: TraceShortcutElement,
  attribute: string,
): string | undefined {
  const owner = element.closest(`[${attribute}]`)
  return safeId(owner?.getAttribute(attribute) ?? undefined)
}

export function subscribeCrossViewTraceShortcuts(
  taskId: string,
  listener: (request: TraceFocusRequest) => void,
  events?: EventTargetLike,
): () => void {
  const normalizedTaskId = safeId(taskId)
  const targetEvents = events ?? (typeof window !== "undefined" ? window : undefined)
  if (!normalizedTaskId || !targetEvents) return () => undefined
  const handler = (rawEvent: Event) => {
    const event = rawEvent as globalThis.KeyboardEvent
    if (event.type !== "keydown" || event.key !== "Enter" || !event.altKey) return
    const element = event.target as TraceShortcutElement | null
    if (!element || typeof element.closest !== "function") return
    if (!element.closest(`[data-task-id="${cssAttribute(normalizedTaskId)}"]`)) return
    const request: TraceFocusRequest = Object.freeze({
      taskId: normalizedTaskId,
      nodeKey: closestTypedAttribute(element, "data-trace-node-key"),
      eventId: closestTypedAttribute(element, "data-event-id"),
      spanId: closestTypedAttribute(element, "data-span-id"),
      workerId: closestTypedAttribute(element, "data-worker-id"),
      toolCallId: closestTypedAttribute(element, "data-tool-call-id"),
      artifactId: closestTypedAttribute(element, "data-artifact-id"),
      mutationId: closestTypedAttribute(element, "data-mutation-id"),
      sourceView: shortcutView(element),
      requestedAt: Date.now(),
    })
    if (
      !request.nodeKey &&
      !request.eventId &&
      !request.spanId &&
      !request.workerId &&
      !request.toolCallId &&
      !request.artifactId &&
      !request.mutationId
    ) return
    event.preventDefault()
    listener(request)
  }
  targetEvents.addEventListener("keydown", handler)
  return () => targetEvents.removeEventListener("keydown", handler)
}

export function traceFocusRequestForNode(
  node: TraceNode,
  sourceView: TraceViewKindValue,
): TraceFocusRequest {
  return Object.freeze({
    taskId: node.taskId,
    nodeKey: node.key,
    eventId: node.primaryEventId,
    spanId: node.refs.spanId,
    workerId: node.refs.workerId,
    toolCallId: node.refs.toolCallId,
    artifactId: node.refs.artifactIds[0],
    mutationId: node.refs.mutationId,
    sourceView,
    requestedAt: Date.now(),
  })
}
