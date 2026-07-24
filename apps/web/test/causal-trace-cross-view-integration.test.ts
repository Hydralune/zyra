import { describe, expect, test } from "bun:test"
import {
  CanonicalProjectionStore,
  CausalTraceProjectionEngine,
  TraceCompleteness,
  TraceNavigationRuntime,
  TraceNodeKind,
  TraceProjectionError,
  TraceReconciliationRuntime,
  TraceReportReferenceStore,
  TraceSearchIndex,
  TraceSemanticKind,
  TraceViewKind,
  TraceVirtualizer,
  buildCausalTraceProjection,
  buildTraceCriticalPath,
  defaultTraceFilter,
  defaultTraceFoldState,
  filterCausalTrace,
  foldCausalTrace,
  navigationTargetsForTraceNode,
  selectCausalTrace,
  type CausalTraceProjection,
  type TraceNavigationTarget,
} from "../src/state/index.ts"
import { TraceWorkbenchController } from "../src/features/trace/view/controller.ts"
import {
  normalizeEventFrame,
  type IngressBatch,
  type IngressEvent,
  type JsonObject,
} from "../src/events/ingress/index.ts"

const TASK = "task_causal_trace"
const RUN = "run_causal_trace"
const SESSION = "session_causal_trace"
const NOW = "2026-07-25T02:00:00.000Z"
const DIGEST = `sha256:${"d".repeat(64)}`

interface TraceEventOptions {
  eventId?: string
  eventType: string
  domain: string
  inline?: JsonObject
  metadata?: JsonObject
  taskId?: string
  runId?: string
  sessionId?: string
  nodeId?: string
  workerId?: string
  spanId?: string
  parentSpanId?: string
  toolCallId?: string
  artifactId?: string
  checkpointId?: string
  commandId?: string
  causationId?: string
  correlationId?: string
  terminal?: boolean
  effective?: boolean
  committedOffset?: number
  createdOffset?: number
  artifactRefs?: Array<{
    artifactId: string
    title?: string
    mediaType?: string
    sizeBytes?: number
  }>
}

function traceEvent(sequence: number, options: TraceEventOptions): IngressEvent {
  const taskId = options.taskId ?? TASK
  const runId = options.runId ?? RUN
  const eventId = options.eventId ?? `trace_event_${sequence}`
  const correlationId = options.correlationId ?? "trace_request_root"
  const createdAt = new Date(
    Date.parse(NOW) + (options.createdOffset ?? sequence * 25),
  ).toISOString()
  const committedAt = new Date(
    Date.parse(NOW) + (options.committedOffset ?? sequence * 50),
  ).toISOString()
  const raw = {
    schema: "zyra.runtime-event/v1",
    eventId,
    eventType: options.eventType,
    eventVersion: 1,
    aggregateId: `task:${taskId}`,
    aggregateSequence: sequence,
    globalSequence: sequence,
    producerSequence: sequence,
    idempotencyKey: `trace-test-${eventId}`,
    correlationId,
    causationId: options.causationId,
    createdAt,
    committedAt,
    durability: "durable",
    effect: options.effective === false ? "non_effective" : "effective",
    identity: {
      taskId,
      runId,
      sessionId: options.sessionId ?? SESSION,
      nodeId: options.nodeId,
      workerId: options.workerId,
      spanId: options.spanId,
      parentSpanId: options.parentSpanId,
      toolCallId: options.toolCallId,
      artifactId: options.artifactId,
      checkpointId: options.checkpointId,
      controlCommandId: options.commandId,
    },
    sender: {
      kind: "runtime",
      id: options.workerId ?? "trace-runtime",
      capabilityRefs: [],
    },
    intent: "status",
    topKRecipients: [],
    summary: `Canonical ${options.eventType}`,
    stateDelta: {
      domain: options.domain,
      operation: "transition",
      path: ["status"],
      beforeDigest: `sha256:${"1".repeat(64)}`,
      afterDigest: `sha256:${"2".repeat(64)}`,
      effective: options.effective !== false,
    },
    evidenceRefs: [],
    artifactRefs: (options.artifactRefs ?? []).map((artifact) => ({
      artifactId: artifact.artifactId,
      digest: DIGEST,
      mediaType: artifact.mediaType ?? "application/json",
      sizeBytes: artifact.sizeBytes ?? 256,
      title: artifact.title ?? artifact.artifactId,
    })),
    provenance: {
      sourceRepository: "zyra",
      sourceModule: "causal-trace-cross-view-test",
      migrationRole: "zyra_owned",
      producerVersion: "test",
      trust: "internal",
    },
    inline: {
      ...(options.inline ?? {}),
      ...(options.terminal ? { terminal: true } : {}),
    },
    sourceBytes: 256,
    inlineBytes: 128,
    envelopeBytes: 2_048,
    contentDigest: DIGEST,
    metadata: options.metadata ?? {},
    settlement: "atomic",
    terminal: options.terminal ?? false,
  }
  return normalizeEventFrame(
    JSON.parse(JSON.stringify({
      schema: "zyra.event-ingress-frame/v1",
      kind: "event",
      source: "delta",
      generation: 1,
      taskId,
      sequence,
      previousSequence: Math.max(0, sequence - 1),
      eventId,
      eventType: options.eventType,
      correlationId,
      causationId: options.causationId,
      observedAtMs: 100_000 + sequence,
      cursor: `cursor.${sequence}`,
      event: raw,
    })),
    taskId,
    1,
  ).event
}

function traceBatch(
  events: readonly IngressEvent[],
  options: {
    taskId?: string
    sequence?: number
    highWatermark?: number
    snapshot?: boolean
    caughtUp?: boolean
    generation?: number
  } = {},
): IngressBatch {
  const taskId = options.taskId ?? TASK
  const sequence = options.sequence ?? Math.max(0, ...events.map((event) => event.globalSequence))
  const highWatermark = options.highWatermark ?? sequence
  return {
    taskId,
    generation: options.generation ?? 1,
    events: Object.freeze([...events]),
    receipts: Object.freeze([]),
    cursor: `cursor.${sequence}`,
    fromSequence: events.length
      ? Math.min(...events.map((event) => event.globalSequence))
      : sequence,
    sequence,
    highWatermark,
    receivedAt: 200_000 + sequence,
    transport: "long_poll",
    snapshot: options.snapshot ?? true,
    caughtUp: options.caughtUp ?? highWatermark === sequence,
  }
}

function projectionStore(): CanonicalProjectionStore {
  return new CanonicalProjectionStore({
    restore: false,
    autoPersist: false,
    now: () => Date.parse(NOW) + 600_000,
    limits: {
      maxEvents: 100_000,
      maxEventsPerTask: 100_000,
      maxEntitiesPerDomain: 100_000,
      maxMutations: 100_000,
      maxIndexEntries: 200_000,
      maxIndexValues: 1_000_000,
    },
  })
}

function completeScenario(): IngressEvent[] {
  const values: TraceEventOptions[] = [
    {
      eventId: "trace_task_started",
      eventType: "task.started",
      domain: "task",
      nodeId: "node_trace_root",
      spanId: "span_trace_root",
      inline: { task_id: TASK, title: "Causal trace task", status: "running" },
    },
    {
      eventId: "trace_worker_started",
      eventType: "worker.started",
      domain: "worker",
      nodeId: "node_trace_worker",
      workerId: "worker_trace_primary",
      spanId: "span_worker",
      parentSpanId: "span_trace_root",
      causationId: "trace_task_started",
      inline: {
        worker_id: "worker_trace_primary",
        node_id: "node_trace_worker",
        role: "code-browser-worker",
        lease_id: "lease_trace_primary",
        status: "running",
      },
    },
    {
      eventId: "trace_placement_selected",
      eventType: "scheduler.placement.provider.selected",
      domain: "scheduler",
      nodeId: "node_trace_worker",
      workerId: "worker_trace_primary",
      spanId: "span_dispatch",
      parentSpanId: "span_worker",
      causationId: "trace_worker_started",
      inline: {
        scheduler_id: "scheduler_trace",
        route_id: "route_cloud_primary",
        placement_id: "placement_cloud_primary",
        provider_id: "provider_cloud_a",
        model_id: "model_reasoner_a",
        candidate_ids: ["provider_cloud_a", "provider_edge_b"],
        status: "running",
      },
    },
    {
      eventId: "trace_provider_retry",
      eventType: "provider.request.retry.backoff",
      domain: "scheduler",
      nodeId: "node_trace_worker",
      workerId: "worker_trace_primary",
      spanId: "span_dispatch_retry",
      parentSpanId: "span_dispatch",
      causationId: "trace_placement_selected",
      inline: {
        scheduler_id: "scheduler_trace",
        route_id: "route_cloud_fallback",
        placement_id: "placement_cloud_fallback",
        provider_id: "provider_cloud_a",
        attempt: 2,
        status: "running",
      },
    },
    {
      eventId: "trace_permission_requested",
      eventType: "permission.tool.requested",
      domain: "permission",
      workerId: "worker_trace_primary",
      spanId: "span_tool",
      parentSpanId: "span_worker",
      toolCallId: "tool_call_trace",
      causationId: "trace_provider_retry",
      inline: {
        permission_id: "permission_trace",
        request_id: "permission_request_trace",
        permission_kind: "tool",
        tool_name: "mcp.browser.click",
        decision: "ask",
        status: "waiting_policy",
      },
    },
    {
      eventId: "trace_permission_allowed",
      eventType: "permission.tool.allowed",
      domain: "permission",
      workerId: "worker_trace_primary",
      spanId: "span_tool",
      toolCallId: "tool_call_trace",
      causationId: "trace_permission_requested",
      inline: {
        permission_id: "permission_trace",
        request_id: "permission_request_trace",
        decision: "allow",
        status: "completed",
      },
      terminal: true,
    },
    {
      eventId: "trace_tool_started",
      eventType: "tool.mcp.started",
      domain: "tool",
      workerId: "worker_trace_primary",
      spanId: "span_tool",
      toolCallId: "tool_call_trace",
      causationId: "trace_permission_allowed",
      inline: {
        tool_call_id: "tool_call_trace",
        tool_name: "mcp.browser.click",
        permission_id: "permission_trace",
        status: "running",
      },
    },
    {
      eventId: "trace_mcp_reconnect",
      eventType: "mcp.connection.reconnected",
      domain: "tool",
      workerId: "worker_trace_primary",
      spanId: "span_mcp",
      parentSpanId: "span_tool",
      toolCallId: "tool_call_trace",
      causationId: "trace_tool_started",
      inline: {
        tool_call_id: "tool_call_trace",
        tool_name: "mcp.browser.click",
        mcp_call_id: "mcp_call_trace",
        mcp_server_id: "mcp_server_browser",
        reconnect_epoch: 2,
        status: "running",
      },
    },
    {
      eventId: "trace_skill_started",
      eventType: "skill.execution.started",
      domain: "tool",
      workerId: "worker_trace_primary",
      spanId: "span_skill",
      parentSpanId: "span_tool",
      toolCallId: "tool_call_skill",
      causationId: "trace_mcp_reconnect",
      inline: {
        tool_call_id: "tool_call_skill",
        tool_name: "SkillTool",
        skill_id: "skill_browser_checkout",
        status: "running",
      },
    },
    {
      eventId: "trace_subagent_spawned",
      eventType: "subagent.spawned",
      domain: "worker",
      workerId: "worker_trace_subagent",
      spanId: "span_subagent",
      parentSpanId: "span_skill",
      causationId: "trace_skill_started",
      inline: {
        worker_id: "worker_trace_subagent",
        subagent_id: "subagent_trace_1",
        parent_tool_call_id: "tool_call_skill",
        role: "reviewer",
        status: "running",
      },
    },
    {
      eventId: "trace_subagent_yield_partial",
      eventType: "subagent.yield.partial",
      domain: "worker",
      workerId: "worker_trace_subagent",
      spanId: "span_subagent",
      causationId: "trace_subagent_spawned",
      inline: {
        worker_id: "worker_trace_subagent",
        subagent_id: "subagent_trace_1",
        parent_tool_call_id: "tool_call_skill",
        yield_id: "yield_trace_1",
        settlement: "partial",
        status: "running",
      },
    },
    {
      eventId: "trace_subagent_yield_final",
      eventType: "subagent.yield.final",
      domain: "worker",
      workerId: "worker_trace_subagent",
      spanId: "span_subagent",
      causationId: "trace_subagent_yield_partial",
      inline: {
        worker_id: "worker_trace_subagent",
        subagent_id: "subagent_trace_1",
        parent_tool_call_id: "tool_call_skill",
        yield_id: "yield_trace_2",
        settlement: "final",
        status: "completed",
      },
      terminal: true,
    },
    {
      eventId: "trace_terminal_output",
      eventType: "terminal.pty.output",
      domain: "tool",
      workerId: "worker_trace_primary",
      spanId: "span_terminal",
      parentSpanId: "span_tool",
      toolCallId: "tool_call_terminal",
      causationId: "trace_subagent_yield_final",
      inline: {
        tool_call_id: "tool_call_terminal",
        tool_name: "terminal",
        terminal_session_id: "terminal_session_trace",
        terminal_frame_id: "terminal_frame_42",
        status: "running",
      },
    },
    {
      eventId: "trace_browser_action",
      eventType: "browser.action.started",
      domain: "tool",
      workerId: "worker_trace_primary",
      spanId: "span_browser",
      parentSpanId: "span_tool",
      toolCallId: "tool_call_trace",
      causationId: "trace_terminal_output",
      inline: {
        tool_call_id: "tool_call_trace",
        tool_name: "browser.click",
        browser_session_id: "browser_session_trace",
        browser_step_id: "browser_step_trace",
        browser_action_id: "browser_action_trace",
        permission_id: "permission_trace",
        status: "running",
      },
    },
    {
      eventId: "trace_artifact_created",
      eventType: "artifact.browser.screenshot.created",
      domain: "artifact",
      workerId: "worker_trace_primary",
      spanId: "span_browser",
      toolCallId: "tool_call_trace",
      artifactId: "artifact_trace_screenshot",
      causationId: "trace_browser_action",
      artifactRefs: [{
        artifactId: "artifact_trace_screenshot",
        title: "Trace screenshot",
        mediaType: "image/png",
        sizeBytes: 1_024,
      }],
      inline: {
        artifact_id: "artifact_trace_screenshot",
        browser_session_id: "browser_session_trace",
        browser_step_id: "browser_step_trace",
        browser_action_id: "browser_action_trace",
        status: "completed",
      },
      terminal: true,
    },
    {
      eventId: "trace_checkpoint_committed",
      eventType: "checkpoint.compact.committed",
      domain: "session",
      workerId: "worker_trace_primary",
      spanId: "span_checkpoint",
      parentSpanId: "span_worker",
      checkpointId: "checkpoint_trace_1",
      causationId: "trace_artifact_created",
      inline: {
        session_id: SESSION,
        checkpoint_id: "checkpoint_trace_1",
        compact_count: 1,
        status: "completed",
      },
      terminal: true,
    },
    {
      eventId: "trace_worker_fault",
      eventType: "worker.fault.timeout.failed",
      domain: "worker",
      workerId: "worker_trace_primary",
      spanId: "span_fault",
      parentSpanId: "span_worker",
      causationId: "trace_checkpoint_committed",
      inline: {
        worker_id: "worker_trace_primary",
        failure_id: "failure_trace_timeout",
        error_code: "worker_timeout",
        status: "failed",
      },
      terminal: true,
    },
    {
      eventId: "trace_recovery_planned",
      eventType: "recovery.planned",
      domain: "recovery",
      workerId: "worker_trace_replacement",
      spanId: "span_recovery",
      parentSpanId: "span_fault",
      checkpointId: "checkpoint_trace_1",
      causationId: "trace_worker_fault",
      inline: {
        recovery_id: "recovery_trace_1",
        failure_id: "failure_trace_timeout",
        previous_worker_id: "worker_trace_primary",
        replacement_worker_id: "worker_trace_replacement",
        resumed_checkpoint_id: "checkpoint_trace_1",
        attempt: 1,
        strategy: "checkpoint_replace",
        status: "recovering",
      },
    },
    {
      eventId: "trace_recovery_completed",
      eventType: "recovery.restore.completed",
      domain: "recovery",
      workerId: "worker_trace_replacement",
      spanId: "span_recovery",
      checkpointId: "checkpoint_trace_1",
      causationId: "trace_recovery_planned",
      inline: {
        recovery_id: "recovery_trace_1",
        failure_id: "failure_trace_timeout",
        previous_worker_id: "worker_trace_primary",
        replacement_worker_id: "worker_trace_replacement",
        resumed_checkpoint_id: "checkpoint_trace_1",
        attempt: 1,
        strategy: "checkpoint_replace",
        status: "completed",
      },
      terminal: true,
    },
    {
      eventId: "trace_task_completed",
      eventType: "task.completed",
      domain: "task",
      nodeId: "node_trace_root",
      workerId: "worker_trace_replacement",
      spanId: "span_trace_root",
      causationId: "trace_recovery_completed",
      inline: { task_id: TASK, title: "Causal trace task", status: "completed" },
      terminal: true,
    },
  ]
  return values.map((value, index) => traceEvent(index + 1, value))
}

function builtProjection(events = completeScenario()): {
  store: CanonicalProjectionStore
  projection: CausalTraceProjection
} {
  const store = projectionStore()
  store.apply(traceBatch(events))
  return { store, projection: buildCausalTraceProjection(store.state, TASK) }
}

class FakeElement {
  focused = false
  scrolled = false
  readonly attributes = new Map<string, string>()

  scrollIntoView(): void {
    this.scrolled = true
  }

  focus(): void {
    this.focused = true
  }

  setAttribute(name: string, value: string): void {
    this.attributes.set(name, value)
  }

  removeAttribute(name: string): void {
    this.attributes.delete(name)
  }
}

class FakeDocument {
  readonly selectors: string[] = []
  readonly elements = new Map<string, FakeElement>()

  mount(selector: string): FakeElement {
    const element = new FakeElement()
    this.elements.set(selector, element)
    return element
  }

  querySelector(selector: string): FakeElement | null {
    this.selectors.push(selector)
    return this.elements.get(selector) ?? null
  }
}

describe("M2-S03B-03 causal trace cross-view integration", () => {
  test("builds one canonical typed index across runtime domains and semantic mechanisms", () => {
    const { store, projection } = builtProjection()
    expect(projection.schema).toBe("zyra.causal-trace-projection/v1")
    expect(projection.taskId).toBe(TASK)
    expect(projection.diagnostics.eventCount).toBe(20)
    expect(projection.nodes.length).toBeGreaterThan(projection.diagnostics.eventCount)
    expect(projection.edges.length).toBeGreaterThan(projection.diagnostics.eventCount)
    expect(projection.diagnostics.disabled).toBe(false)
    expect(projection.diagnostics.projectionRevision).toBe(store.state.revision)
    expect(projection.nodes.filter((node) => node.identity.kind === TraceNodeKind.EVENT)).toHaveLength(20)
    expect(projection.nodes.some((node) => node.identity.kind === TraceNodeKind.SPAN)).toBe(true)
    expect(projection.nodes.some((node) => node.identity.kind === TraceNodeKind.WORKER)).toBe(true)
    expect(projection.nodes.some((node) => node.identity.kind === TraceNodeKind.TOOL)).toBe(true)
    expect(projection.nodes.some((node) => node.identity.kind === TraceNodeKind.ARTIFACT)).toBe(true)
    expect(projection.nodes.some((node) => node.identity.kind === TraceNodeKind.MUTATION)).toBe(true)
    expect(projection.nodes.some((node) => node.identity.kind === TraceNodeKind.CHECKPOINT)).toBe(true)
    const semanticKinds = new Set(projection.nodes.flatMap((node) => node.semantics))
    for (const semantic of [
      TraceSemanticKind.PERMISSION,
      TraceSemanticKind.COMPACT,
      TraceSemanticKind.CHECKPOINT,
      TraceSemanticKind.PLACEMENT,
      TraceSemanticKind.PROVIDER,
      TraceSemanticKind.PROVIDER_RETRY,
      TraceSemanticKind.FAULT,
      TraceSemanticKind.RECOVERY,
      TraceSemanticKind.MCP,
      TraceSemanticKind.MCP_RECONNECT,
      TraceSemanticKind.SKILL,
      TraceSemanticKind.SUBAGENT,
      TraceSemanticKind.SUBAGENT_YIELD,
      TraceSemanticKind.TERMINAL,
      TraceSemanticKind.PTY,
      TraceSemanticKind.BROWSER,
      TraceSemanticKind.ARTIFACT,
      TraceSemanticKind.MUTATION,
    ]) {
      expect(semanticKinds.has(semantic)).toBe(true)
    }
    const artifactKeys = projection.indexes.nodeKeysByArtifact.artifact_trace_screenshot
    expect(artifactKeys?.some((key) => key === "artifact:artifact_trace_screenshot")).toBe(true)
    expect(projection.indexes.eventIdByMutation).not.toEqual({})
    const terminal = projection.nodes.find((node) => node.primaryEventId === "trace_terminal_output")
    expect(terminal?.refs.terminalSessionId).toBe("terminal_session_trace")
    expect(terminal?.refs.terminalFrameId).toBe("terminal_frame_42")
    const browser = projection.nodes.find((node) => node.primaryEventId === "trace_browser_action")
    expect(browser?.refs.browserSessionId).toBe("browser_session_trace")
    expect(browser?.refs.browserStepId).toBe("browser_step_trace")
    expect(browser?.refs.browserActionId).toBe("browser_action_trace")
  })

  test("critical path remains deterministic and attributes latency, retries, permission and recovery", () => {
    const { projection } = builtProjection()
    const first = buildTraceCriticalPath(projection.nodes, projection.edges, 6)
    const second = buildTraceCriticalPath(projection.nodes, projection.edges, 6)
    expect(second).toEqual(first)
    expect(first.nodeKeys.length).toBeGreaterThan(10)
    expect(first.eventIds).toContain("trace_permission_requested")
    expect(first.eventIds).toContain("trace_provider_retry")
    expect(first.eventIds).toContain("trace_worker_fault")
    expect(first.eventIds).toContain("trace_recovery_completed")
    expect(first.totalWeight).toBeGreaterThan(0)
    expect(first.durationMs).toBeGreaterThan(0)
    expect(first.bottlenecks.some((item) => item.kind === "retry")).toBe(true)
    expect(first.bottlenecks.some((item) => item.kind === "permission")).toBe(true)
    expect(projection.nodes.filter((node) => node.critical).length).toBe(first.nodeKeys.length)
  })

  test("typed navigation resolves terminal, browser, artifact, timeline, topology and diff without display-string guessing", () => {
    const { projection } = builtProjection()
    const browserNode = projection.nodes.find((node) => node.primaryEventId === "trace_browser_action")!
    const targets = navigationTargetsForTraceNode(browserNode)
    expect(targets.some((target) => target.view === TraceViewKind.TIMELINE && target.eventId === "trace_browser_action")).toBe(true)
    expect(targets.some((target) => target.view === TraceViewKind.BROWSER && target.browserActionId === "browser_action_trace")).toBe(true)
    const artifactNode = projection.nodes.find((node) => node.primaryEventId === "trace_artifact_created")!
    expect(navigationTargetsForTraceNode(artifactNode).some((target) =>
      target.view === TraceViewKind.ARTIFACT && target.artifactId === "artifact_trace_screenshot",
    )).toBe(true)
    const terminalNode = projection.nodes.find((node) => node.primaryEventId === "trace_terminal_output")!
    const terminalTarget = navigationTargetsForTraceNode(terminalNode).find((target) =>
      target.view === TraceViewKind.TERMINAL && target.terminalFrameId === "terminal_frame_42",
    )!
    const document = new FakeDocument()
    const selector = '[data-terminal-frame-id="terminal_frame_42"]'
    const element = document.mount(selector)
    const navigation = new TraceNavigationRuntime({
      document,
      now: (() => {
        let now = 100
        return () => ++now
      })(),
      highlightDurationMs: 0,
    })
    const receipt = navigation.navigate(terminalTarget, projection)
    expect(receipt.phase).toBe("focused")
    expect(receipt.selector).toBe(selector)
    expect(element.focused).toBe(true)
    expect(element.scrolled).toBe(true)
    expect(document.selectors.every((value) => !value.includes(terminalNode.summary))).toBe(true)
    const unavailable = navigation.navigate({
      ...terminalTarget,
      id: `${terminalTarget.id}:missing`,
      terminalFrameId: "terminal_frame_missing",
      selector: '[data-terminal-frame-id="terminal_frame_missing"]',
    }, projection)
    expect(unavailable.phase).toBe("unavailable")
    expect(unavailable.reason).toContain("typed PTY")
    navigation.close()
  })

  test("search, filters and causal folds preserve exact evidence boundaries", () => {
    const { projection } = builtProjection()
    const index = new TraceSearchIndex(projection.nodes, { now: () => 10 })
    const provider = index.search("semantic:provider worker:worker_trace_primary")
    expect(provider.matches.length).toBeGreaterThan(0)
    expect(provider.matches.every((match) =>
      projection.nodesByKey[match.nodeKey]?.refs.workerId === "worker_trace_primary",
    )).toBe(true)
    const artifact = index.search('artifact:"artifact_trace_screenshot"')
    expect(artifact.matches.some((match) =>
      projection.nodesByKey[match.nodeKey]?.refs.artifactIds.includes("artifact_trace_screenshot"),
    )).toBe(true)
    const excluded = index.search("recovery -failure")
    expect(excluded.matches.every((match) =>
      !projection.nodesByKey[match.nodeKey]?.semantics.includes(TraceSemanticKind.FAULT),
    )).toBe(true)
    const filtered = filterCausalTrace(projection, {
      ...defaultTraceFilter(),
      semantics: [TraceSemanticKind.FAULT, TraceSemanticKind.RECOVERY],
      includeIssueNodes: false,
    }, index)
    expect(filtered.nodes.length).toBeGreaterThan(0)
    expect(filtered.hiddenNodeCount).toBeGreaterThan(0)
    expect(filtered.gaps.length).toBeGreaterThan(0)
    expect(filtered.gaps.some((gap) => gap.causalBridge)).toBe(true)
    const folded = foldCausalTrace(projection, filterCausalTrace(projection), {
      ...defaultTraceFoldState(),
      mode: "span",
      maximumVisibleChildren: 2,
    })
    expect(folded.groups.length).toBeGreaterThan(0)
    expect(folded.hiddenNodeKeys.length).toBeGreaterThan(0)
    expect(folded.nodes.some((node) => node.critical)).toBe(true)
    expect(folded.boundaryEdgeIds.every((edgeId) => projection.edgesById[edgeId])).toBe(true)
    index.close()
  })

  test("late events, reconnects, missing spans, missing causes and quarantine remain explicit", () => {
    const initial = completeScenario()
    const store = projectionStore()
    store.apply(traceBatch(initial))
    const first = buildCausalTraceProjection(store.state, TASK)
    const reconciliation = new TraceReconciliationRuntime()
    expect(reconciliation.reconcile(first).changes).toHaveLength(0)
    const lateEvents = [
      traceEvent(21, {
        eventId: "trace_late_orphan",
        eventType: "browser.result.partial",
        domain: "tool",
        workerId: "worker_trace_primary",
        toolCallId: "tool_call_trace",
        causationId: "event_missing_from_retention",
        correlationId: "correlation_late_browser",
        inline: {
          tool_call_id: "tool_call_trace",
          browser_session_id: "browser_session_trace",
          browser_step_id: "browser_step_late",
          status: "running",
        },
        committedOffset: 900_000,
      }),
      traceEvent(22, {
        eventId: "trace_late_mcp_reconnect",
        eventType: "mcp.connection.reconnected",
        domain: "tool",
        workerId: "worker_trace_primary",
        spanId: "span_late_mcp",
        toolCallId: "tool_call_trace",
        causationId: "trace_mcp_reconnect",
        correlationId: "correlation_late_mcp",
        inline: {
          tool_call_id: "tool_call_trace",
          mcp_call_id: "mcp_call_trace_late",
          mcp_server_id: "mcp_server_browser",
          status: "running",
        },
        committedOffset: 900_050,
      }),
    ]
    store.apply(traceBatch(lateEvents, {
      sequence: 22,
      highWatermark: 30,
      snapshot: false,
      caughtUp: false,
    }))
    const second = buildCausalTraceProjection(store.state, TASK)
    const result = reconciliation.reconcile(second)
    expect(result.addedEventIds).toContain("trace_late_mcp_reconnect")
    expect(result.reconnectEventIds).toContain("trace_late_mcp_reconnect")
    const orphan = second.nodes.find((node) => node.primaryEventId === "trace_late_orphan")
    expect(orphan?.completeness).toBe(TraceCompleteness.ORPHAN)
    expect(orphan?.diagnostics.some((value) => value === "missing-span")).toBe(true)
    expect(second.quarantine.some((record) => record.code === "missing-edge-source")).toBe(true)
    expect(second.diagnostics.lag).toBe(8)
    expect(second.diagnostics.ready).toBe(false)
    reconciliation.close()
  })

  test("partial-to-final and late artifact reconciliation uses event identity rather than labels", () => {
    const { projection } = builtProjection()
    const runtime = new TraceReconciliationRuntime()
    runtime.reconcile(projection)
    const eventId = "trace_browser_action"
    const previousAdmission = projection.admissionByEvent[eventId]!
    const nextAdmission = Object.freeze({
      ...previousAdmission,
      event: Object.freeze({ ...previousAdmission.event, terminal: true }),
      refs: Object.freeze({
        ...previousAdmission.refs,
        artifactIds: Object.freeze([...previousAdmission.refs.artifactIds, "artifact_late_result"]),
      }),
      completeness: TraceCompleteness.COMPLETE,
    })
    const nextProjection: CausalTraceProjection = Object.freeze({
      ...projection,
      projectionRevision: projection.projectionRevision + 1,
      admissionByEvent: Object.freeze({
        ...projection.admissionByEvent,
        [eventId]: nextAdmission,
      }),
    })
    const result = runtime.reconcile(nextProjection)
    expect(result.finalizedEventIds).toEqual([eventId])
    expect(result.resolvedArtifactIds).toEqual(["artifact_late_result"])
    expect(result.changes.some((change) => change.kind === "event-finalized")).toBe(true)
    expect(result.changes.some((change) =>
      change.kind === "artifact-resolved" && change.artifactId === "artifact_late_result",
    )).toBe(true)
    runtime.close()
  })

  test("large traces virtualize without mounting the full canonical event set", () => {
    const events: IngressEvent[] = []
    for (let sequence = 1; sequence <= 2_500; sequence += 1) {
      events.push(traceEvent(sequence, {
        eventId: `trace_large_${sequence}`,
        eventType: sequence % 97 === 0 ? "provider.request.retry" : "tool.progress",
        domain: "tool",
        workerId: `worker_large_${sequence % 8}`,
        spanId: `span_large_${Math.floor((sequence - 1) / 20)}`,
        parentSpanId: sequence > 20 ? `span_large_${Math.max(0, Math.floor((sequence - 1) / 20) - 1)}` : undefined,
        toolCallId: `tool_large_${Math.floor((sequence - 1) / 5)}`,
        causationId: sequence > 1 ? `trace_large_${sequence - 1}` : undefined,
        correlationId: `large_correlation_${Math.floor((sequence - 1) / 10)}`,
        inline: {
          tool_call_id: `tool_large_${Math.floor((sequence - 1) / 5)}`,
          tool_name: "large_trace_tool",
          provider_id: `provider_${sequence % 3}`,
          status: sequence === 5_000 ? "completed" : "running",
        },
        terminal: sequence === 2_500,
      }))
    }
    const { projection } = builtProjection(events)
    expect(projection.diagnostics.eventCount).toBe(2_500)
    expect(projection.nodes.filter((node) => node.identity.kind === TraceNodeKind.EVENT)).toHaveLength(2_500)
    const virtualizer = new TraceVirtualizer(projection.nodes, { estimatedRowHeight: 84 })
    const first = virtualizer.window({
      scrollTop: 0,
      viewportHeight: 720,
      overscanPx: 720,
      pinnedKeys: [projection.nodes.at(-1)!.key],
    })
    expect(first.items.length).toBeLessThan(100)
    expect(first.items.some((item) => item.key === projection.nodes.at(-1)!.key && item.pinned)).toBe(true)
    const middle = virtualizer.window({
      scrollTop: first.totalHeight / 2,
      viewportHeight: 720,
      overscanPx: 360,
    })
    expect(middle.firstIndex).toBeGreaterThan(1_000)
    expect(middle.items.length).toBeLessThan(100)
    virtualizer.measure(middle.items[0]!.key, 180)
    expect(virtualizer.audit().measuredCount).toBe(1)
    virtualizer.close()
  }, 30_000)

  test("pinning emits report references and stale rebasing without becoming canonical state", () => {
    const { store, projection } = builtProjection()
    const pins = new TraceReportReferenceStore({ now: () => Date.parse(NOW) })
    const node = projection.nodes.find((candidate) => candidate.primaryEventId === "trace_artifact_created")!
    const reference = pins.pin(projection, node.key, "Screenshot proves browser completion.")
    expect(reference.eventIds).toContain("trace_artifact_created")
    expect(reference.artifactIds).toContain("artifact_trace_screenshot")
    const exported = pins.export(projection)
    expect(exported.markdown).toContain("artifact\\_trace\\_screenshot")
    expect(exported.markdown).toContain("trace\\_artifact\\_created")
    expect(exported.references).toHaveLength(1)
    expect(store.state.causality.byEvent[reference.id]).toBeUndefined()
    const missingProjection = Object.freeze({
      ...projection,
      projectionRevision: projection.projectionRevision + 1,
      nodesByKey: Object.freeze(Object.fromEntries(
        Object.entries(projection.nodesByKey).filter(([key]) => key !== node.key),
      )),
    }) as CausalTraceProjection
    pins.rebase(missingProjection)
    expect(pins.list()[0]?.stale).toBe(true)
    pins.close()
  })

  test("viewer close releases only local indexes and never mutates the task or worker session", () => {
    const { store, projection } = builtProjection()
    const revision = store.state.revision
    const eventCount = Object.keys(store.state.causality.byEvent).length
    const controller = new TraceWorkbenchController(TASK, projection, {
      viewportHeight: 500,
    })
    const artifactNode = projection.nodes.find((node) => node.primaryEventId === "trace_artifact_created")!
    controller.select(artifactNode.key)
    controller.pin(artifactNode.key)
    expect(controller.audit().closeStopsTask).toBe(false)
    controller.close("User closed trace viewer.")
    expect(controller.audit().closed).toBe(true)
    expect(store.state.revision).toBe(revision)
    expect(Object.keys(store.state.causality.byEvent)).toHaveLength(eventCount)
    expect(store.state.tasks[TASK]?.terminal).toBe(true)
    expect(() => controller.select(artifactNode.key)).toThrow("closed")
  })

  test("disabling projection, projection engine, navigation and controller fails claimed behavior explicitly", () => {
    const { store, projection } = builtProjection()
    expect(() => buildCausalTraceProjection(store.state, TASK, { disabled: true })).toThrow(
      TraceProjectionError,
    )
    expect(() => buildCausalTraceProjection(store.state, TASK, { disabled: true })).toThrow(
      "typed joins are disabled",
    )
    const engine = new CausalTraceProjectionEngine({ disabled: true })
    expect(() => engine.project(store.state, TASK)).toThrow("disabled")
    const navigation = new TraceNavigationRuntime({ disabled: true, document: new FakeDocument() })
    const node = projection.nodes.find((candidate) => candidate.primaryEventId === "trace_terminal_output")!
    const target = navigationTargetsForTraceNode(node).find((candidate) => candidate.view === TraceViewKind.TERMINAL)!
    expect(navigation.navigate(target, projection).phase).toBe("failed")
    expect(navigation.audit().failedCount).toBe(1)
    expect(() => new TraceWorkbenchController(TASK, projection, { disabled: true })).toThrow(
      "cross-view joins are unavailable",
    )
  })

  test("selector and projection engine consume the canonical state without a second replay store", () => {
    const { store } = builtProjection()
    const selector = selectCausalTrace(TASK)
    const selected = store.select(selector)
    expect(selected.projectionRevision).toBe(store.state.revision)
    expect(selected.diagnostics.eventCount).toBe(Object.keys(store.state.causality.byEvent).length)
    const engine = new CausalTraceProjectionEngine({ now: () => 10 })
    const first = engine.project(store.state, TASK)
    const cached = engine.project(store.state, TASK)
    expect(cached).toBe(first)
    expect(engine.audit().cacheHitCount).toBe(1)
    expect(engine.audit().lastEventCount).toBe(selected.diagnostics.eventCount)
    engine.close()
    expect(() => engine.project(store.state, TASK)).toThrow("closed")
  })

  test("same labels and summaries never create a causal join without matching typed IDs", () => {
    const events = [
      traceEvent(1, {
        eventId: "trace_same_label_a",
        eventType: "tool.progress",
        domain: "tool",
        workerId: "worker_a",
        sessionId: "session_a",
        spanId: "span_a",
        toolCallId: "tool_a",
        correlationId: "correlation_a",
        inline: { tool_call_id: "tool_a", tool_name: "identical", status: "running" },
      }),
      traceEvent(2, {
        eventId: "trace_same_label_b",
        eventType: "tool.progress",
        domain: "tool",
        workerId: "worker_b",
        sessionId: "session_b",
        spanId: "span_b",
        toolCallId: "tool_b",
        correlationId: "correlation_b",
        inline: { tool_call_id: "tool_b", tool_name: "identical", status: "running" },
      }),
    ]
    const { projection } = builtProjection(events)
    const a = "event:trace_same_label_a"
    const b = "event:trace_same_label_b"
    expect(projection.edges.some((edge) =>
      (edge.sourceKey === a && edge.targetKey === b) ||
      (edge.sourceKey === b && edge.targetKey === a),
    )).toBe(false)
    expect(projection.indexes.nodeKeysByToolCall.tool_a).not.toEqual(
      projection.indexes.nodeKeysByToolCall.tool_b,
    )
  })
})
