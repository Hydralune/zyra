import {
  BackpressureMode,
  EventEffect,
  MessageIntent,
  RecipientKind,
  type ArtifactPointer,
  type RuntimeEventEnvelope,
  type SubscriptionSpec,
} from "./contracts.ts";
import {
  PROJECTOR_STREAM_SCHEMA,
  StreamFrameKind,
  type ProjectorStreamCursor,
  type ProjectorStreamFrame,
} from "./integration-contracts.ts";
import {
  canonicalJson,
  cloneJson,
  digestJson,
  newId,
  normalizeIdentifier,
  utcNow,
  type JsonValue,
} from "./canonical.ts";
import { EventSpineError, EventSpineErrorCode, ProjectorError, asEventSpineError } from "./errors.ts";
import type { RuntimeEventSpine } from "./runtime.ts";

export const PROJECTOR_SUBSCRIPTION_ID = "builtin-runtime-state-projector";
export const PROJECTOR_CONSUMER_ID = "runtime-state-projector";
export const PROJECTOR_TIMELINE_SCHEMA = "zyra.runtime-projector-timeline/v1";
export const PROJECTOR_TASK_VIEW_SCHEMA = "zyra.runtime-task-view/v1";
export const PROJECTOR_HISTORY_SCHEMA = "zyra.runtime-history-view/v1";

export interface ProjectorDeliveryOptions {
  subscriptionId?: string;
  consumerId?: string;
  autoDrain?: boolean;
  batchSize?: number;
  catchUpLimit?: number;
}

export interface ProjectionPumpReport {
  schema: "zyra.runtime-projector-pump/v1";
  subscriptionId: string;
  consumerId: string;
  startedAt: string;
  completedAt: string;
  highWatermark: number;
  cursor: number;
  caughtUp: number;
  leased: number;
  acknowledged: number;
  projected: number;
  duplicates: number;
  failed: number;
  repairedAckGaps: number;
  findings: readonly string[];
}

export interface ProjectionCoverageReport {
  aggregateId?: string;
  canonicalEvents: number;
  canonicalAggregates: number;
  canonicalHighWatermark: number;
  projectedEvents: number;
  projectedAggregates: number;
  projectedHighWatermark: number;
  viewAggregates: number;
  viewHighWatermark: number;
  cursorHighWatermark: number;
  eventLag: number;
  sequenceLag: number;
  complete: boolean;
}

export interface ProjectedTimelineItem {
  eventId: string;
  eventType: string;
  aggregateId: string;
  aggregateSequence: number;
  globalSequence: number;
  correlationId: string;
  causationId?: string;
  intent: string;
  effect: string;
  senderKind: string;
  senderId: string;
  summary: string;
  stateDomain: string;
  stateOperation: string;
  statePath: readonly string[];
  evidenceCount: number;
  artifactRefs: readonly ArtifactPointer[];
  uncertainty?: number;
  createdAt: string;
  committedAt: string;
  projectedAt: string;
}

export interface ProjectedTaskView {
  schema: typeof PROJECTOR_TASK_VIEW_SCHEMA;
  aggregateId: string;
  runId: string;
  sessionId?: string;
  taskId: string;
  status: string;
  phase: string;
  lastEventId: string;
  lastGlobalSequence: number;
  lastAggregateSequence: number;
  effectiveTransitions: number;
  nonEffectiveEvents: number;
  toolCallsStarted: number;
  toolCallsSettled: number;
  toolCallsFailed: number;
  permissionsRequested: number;
  permissionsPending: number;
  permissionsAllowed: number;
  permissionsDenied: number;
  mcpRequests: number;
  subagentsCreated: number;
  subagentsRunning: number;
  subagentsCompleted: number;
  subagentsFailed: number;
  browserObservations: number;
  browserFailures: number;
  controlRequests: number;
  compactCount: number;
  restoreCount: number;
  recoveryCount: number;
  artifactCount: number;
  artifactIds: readonly string[];
  workerIds: readonly string[];
  openToolCallIds: readonly string[];
  pendingPermissionIds: readonly string[];
  activeSubagentIds: readonly string[];
  lastErrorCode?: string;
  lastRecoveryStrategy?: string;
  lastControlKind?: string;
  updatedAt: string;
  digest: string;
}

export interface ProjectedHistoryView {
  schema: typeof PROJECTOR_HISTORY_SCHEMA;
  aggregateId: string;
  cursor: ProjectorStreamCursor;
  items: readonly ProjectedTimelineItem[];
  nextGlobalSequence?: number;
  hasMore: boolean;
  highWatermark: number;
  projectionLag: number;
}

interface TimelineRow {
  event_id: string;
  event_type: string;
  aggregate_id: string;
  aggregate_sequence: number;
  global_sequence: number;
  correlation_id: string;
  causation_id: string | null;
  intent: string;
  effect: string;
  sender_kind: string;
  sender_id: string;
  summary: string;
  state_domain: string;
  state_operation: string;
  state_path_json: string;
  evidence_count: number;
  artifact_refs_json: string;
  uncertainty: number | null;
  created_at: string;
  committed_at: string;
  projected_at: string;
}

interface GlobalCursorRow {
  projector_id: string;
  global_sequence: number;
  event_id: string | null;
  digest: string;
  generation: number;
  updated_at: string;
}

interface TaskViewRow {
  aggregate_id: string;
  view_json: string;
  view_digest: string;
  last_global_sequence: number;
  updated_at: string;
}

function projectorSubscription(subscriptionId = PROJECTOR_SUBSCRIPTION_ID): SubscriptionSpec {
  return {
    subscriptionId,
    recipient: {
      kind: RecipientKind.UI_PROJECTOR,
      id: PROJECTOR_CONSUMER_ID,
      requiredCapabilities: ["projector.apply"],
    },
    eventTypes: ["runtime.*"],
    intents: [],
    aggregatePrefixes: [],
    taskIds: [],
    capabilityRefs: ["projector.apply"],
    capacity: 100_000,
    maxAttempts: 10,
    ackTimeoutMs: 60_000,
    backpressureMode: BackpressureMode.REJECT,
    enabled: true,
    priority: 0,
    metadata: {
      built_in: true,
      read_side_only: true,
      canonical_owner: false,
      projector_owner: "RuntimeStateProjector",
    },
  };
}

function arrayOfStrings(value: JsonValue | undefined): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}

function stringValue(value: JsonValue | undefined): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}

function unique(values: readonly string[]): string[] {
  return [...new Set(values.filter(Boolean))].sort();
}

function remove(values: readonly string[], target: string | undefined): string[] {
  if (!target) return [...values];
  return values.filter((item) => item !== target);
}

function add(values: readonly string[], target: string | undefined): string[] {
  return target ? unique([...values, target]) : [...values];
}

function toolCallId(event: RuntimeEventEnvelope): string | undefined {
  return event.identity.toolCallId ?? stringValue(event.inline.tool_call_id);
}

function permissionId(event: RuntimeEventEnvelope): string | undefined {
  return stringValue(event.inline.permission_id);
}

function subagentId(event: RuntimeEventEnvelope): string | undefined {
  return stringValue(event.inline.subagent_id);
}

function errorCode(event: RuntimeEventEnvelope): string | undefined {
  return stringValue(event.inline.error_code) ?? stringValue(event.inline.reason_code);
}

function emptyTaskView(event: RuntimeEventEnvelope): ProjectedTaskView {
  const base = {
    schema: PROJECTOR_TASK_VIEW_SCHEMA,
    aggregateId: event.aggregateId,
    runId: event.identity.runId,
    ...(event.identity.sessionId ? { sessionId: event.identity.sessionId } : {}),
    taskId: event.identity.taskId,
    status: "active",
    phase: "admitted",
    lastEventId: event.eventId,
    lastGlobalSequence: 0,
    lastAggregateSequence: -1,
    effectiveTransitions: 0,
    nonEffectiveEvents: 0,
    toolCallsStarted: 0,
    toolCallsSettled: 0,
    toolCallsFailed: 0,
    permissionsRequested: 0,
    permissionsPending: 0,
    permissionsAllowed: 0,
    permissionsDenied: 0,
    mcpRequests: 0,
    subagentsCreated: 0,
    subagentsRunning: 0,
    subagentsCompleted: 0,
    subagentsFailed: 0,
    browserObservations: 0,
    browserFailures: 0,
    controlRequests: 0,
    compactCount: 0,
    restoreCount: 0,
    recoveryCount: 0,
    artifactCount: 0,
    artifactIds: [],
    workerIds: [],
    openToolCallIds: [],
    pendingPermissionIds: [],
    activeSubagentIds: [],
    updatedAt: event.committedAt,
    digest: "",
  } satisfies ProjectedTaskView;
  return { ...base, digest: digestTaskView(base) };
}

function digestTaskView(view: Omit<ProjectedTaskView, "digest"> | ProjectedTaskView): string {
  const { digest: _digest, ...unsigned } = view as ProjectedTaskView;
  return digestJson(unsigned as unknown as JsonValue);
}

function statusFor(event: RuntimeEventEnvelope, current: string): string {
  if (event.eventType === "runtime.task.completed") return "completed";
  if (event.eventType === "runtime.task.failed") return "failed";
  if (event.eventType === "runtime.subagent.failed" && current === "active") return "degraded";
  if (event.eventType === "runtime.recovery.completed" && current === "failed") return "recovering";
  if (event.eventType === "runtime.control.completed" && event.inline.control_kind === "cancel") return "cancelled";
  return current;
}

function phaseFor(event: RuntimeEventEnvelope, current: string): string {
  if (event.eventType.startsWith("runtime.query.")) return "query";
  if (event.eventType.startsWith("runtime.turn.")) return "turn";
  if (event.eventType.startsWith("runtime.tool.")) return "tool";
  if (event.eventType.startsWith("runtime.permission.")) return "permission";
  if (event.eventType.startsWith("runtime.mcp.")) return "mcp";
  if (event.eventType.startsWith("runtime.subagent.")) return "subagent";
  if (event.eventType.startsWith("runtime.browser.")) return "browser";
  if (event.eventType.startsWith("runtime.compact.")) return "compact";
  if (event.eventType.startsWith("runtime.recovery.")) return "recovery";
  if (event.eventType.startsWith("runtime.control.")) return "control";
  return current;
}

function foldTaskView(previous: ProjectedTaskView | undefined, event: RuntimeEventEnvelope): ProjectedTaskView {
  if (previous && event.globalSequence <= previous.lastGlobalSequence) return previous;
  const current = previous ?? emptyTaskView(event);
  const sessionId = event.identity.sessionId ?? current.sessionId;
  let openToolCallIds = [...current.openToolCallIds];
  let pendingPermissionIds = [...current.pendingPermissionIds];
  let activeSubagentIds = [...current.activeSubagentIds];
  const next: ProjectedTaskView = {
    ...current,
    runId: event.identity.runId,
    ...(sessionId ? { sessionId } : {}),
    taskId: event.identity.taskId,
    status: statusFor(event, current.status),
    phase: phaseFor(event, current.phase),
    lastEventId: event.eventId,
    lastGlobalSequence: event.globalSequence,
    lastAggregateSequence: event.aggregateSequence,
    effectiveTransitions: current.effectiveTransitions + (event.effect === EventEffect.EFFECTIVE ? 1 : 0),
    nonEffectiveEvents: current.nonEffectiveEvents + (event.effect === EventEffect.NON_EFFECTIVE ? 1 : 0),
    artifactCount: current.artifactCount + event.artifactRefs.length,
    artifactIds: unique([...current.artifactIds, ...event.artifactRefs.map((item) => item.artifactId)]),
    workerIds: add(current.workerIds, event.identity.workerId),
    updatedAt: event.committedAt,
    digest: "",
  };
  switch (event.eventType) {
    case "runtime.tool.called":
      next.toolCallsStarted += 1;
      openToolCallIds = add(openToolCallIds, toolCallId(event));
      break;
    case "runtime.tool.succeeded":
    case "runtime.tool.cancelled":
      next.toolCallsSettled += 1;
      openToolCallIds = remove(openToolCallIds, toolCallId(event));
      break;
    case "runtime.tool.failed":
      next.toolCallsSettled += 1;
      next.toolCallsFailed += 1;
      next.lastErrorCode = errorCode(event) ?? "tool_failed";
      openToolCallIds = remove(openToolCallIds, toolCallId(event));
      break;
    case "runtime.permission.requested":
      next.permissionsRequested += 1;
      break;
    case "runtime.permission.pending":
      next.permissionsPending += 1;
      pendingPermissionIds = add(pendingPermissionIds, permissionId(event));
      break;
    case "runtime.permission.allowed":
      next.permissionsAllowed += 1;
      pendingPermissionIds = remove(pendingPermissionIds, permissionId(event));
      break;
    case "runtime.permission.denied":
      next.permissionsDenied += 1;
      pendingPermissionIds = remove(pendingPermissionIds, permissionId(event));
      next.lastErrorCode = "permission_denied";
      break;
    case "runtime.mcp.auth.requested":
    case "runtime.mcp.elicitation.requested":
    case "runtime.mcp.tool.called":
      next.mcpRequests += 1;
      break;
    case "runtime.subagent.created":
      next.subagentsCreated += 1;
      activeSubagentIds = add(activeSubagentIds, subagentId(event));
      break;
    case "runtime.subagent.dispatched":
      next.subagentsRunning += 1;
      activeSubagentIds = add(activeSubagentIds, subagentId(event));
      break;
    case "runtime.subagent.completed":
    case "runtime.subagent.cancelled":
      next.subagentsCompleted += 1;
      activeSubagentIds = remove(activeSubagentIds, subagentId(event));
      break;
    case "runtime.subagent.failed":
      next.subagentsFailed += 1;
      activeSubagentIds = remove(activeSubagentIds, subagentId(event));
      next.lastErrorCode = errorCode(event) ?? "subagent_failed";
      break;
    case "runtime.browser.observation":
      next.browserObservations += 1;
      break;
    case "runtime.browser.failure":
      next.browserFailures += 1;
      next.lastErrorCode = errorCode(event) ?? "browser_failure";
      break;
    case "runtime.control.requested":
      next.controlRequests += 1;
      {
        const controlKind = stringValue(event.inline.control_kind);
        if (controlKind) next.lastControlKind = controlKind;
      }
      break;
    case "runtime.compact.completed":
      next.compactCount += 1;
      break;
    case "runtime.compact.restore":
      next.restoreCount += 1;
      break;
    case "runtime.recovery.requested":
    case "runtime.recovery.planned":
    case "runtime.recovery.completed":
      next.recoveryCount += 1;
      {
        const strategy = stringValue(event.inline.strategy) ?? current.lastRecoveryStrategy;
        if (strategy) next.lastRecoveryStrategy = strategy;
      }
      break;
    case "runtime.task.failed":
    case "runtime.backend.dispatch.failed":
    case "runtime.api.stream.disconnected":
      next.lastErrorCode = errorCode(event) ?? event.eventType;
      break;
  }
  next.openToolCallIds = openToolCallIds;
  next.pendingPermissionIds = pendingPermissionIds;
  next.activeSubagentIds = activeSubagentIds;
  next.digest = digestTaskView(next);
  return next;
}

function timelineFromRow(row: TimelineRow): ProjectedTimelineItem {
  return {
    eventId: row.event_id,
    eventType: row.event_type,
    aggregateId: row.aggregate_id,
    aggregateSequence: Number(row.aggregate_sequence),
    globalSequence: Number(row.global_sequence),
    correlationId: row.correlation_id,
    causationId: row.causation_id ?? undefined,
    intent: row.intent,
    effect: row.effect,
    senderKind: row.sender_kind,
    senderId: row.sender_id,
    summary: row.summary,
    stateDomain: row.state_domain,
    stateOperation: row.state_operation,
    statePath: JSON.parse(row.state_path_json),
    evidenceCount: Number(row.evidence_count),
    artifactRefs: JSON.parse(row.artifact_refs_json),
    uncertainty: row.uncertainty ?? undefined,
    createdAt: row.created_at,
    committedAt: row.committed_at,
    projectedAt: row.projected_at,
  };
}

export class ProjectionDeliveryRuntime {
  readonly spine: RuntimeEventSpine;
  readonly subscriptionId: string;
  readonly consumerId: string;
  readonly autoDrain: boolean;
  readonly batchSize: number;
  readonly catchUpLimit: number;

  constructor(spine: RuntimeEventSpine, options: ProjectorDeliveryOptions = {}) {
    this.spine = spine;
    this.subscriptionId = options.subscriptionId ?? PROJECTOR_SUBSCRIPTION_ID;
    this.consumerId = options.consumerId ?? PROJECTOR_CONSUMER_ID;
    this.autoDrain = options.autoDrain ?? true;
    this.batchSize = Math.max(1, Math.min(options.batchSize ?? 128, 1000));
    this.catchUpLimit = Math.max(1, Math.min(options.catchUpLimit ?? 100_000, 100_000));
    this.ensureSchema();
    this.spine.registerSubscription(projectorSubscription(this.subscriptionId));
  }

  private ensureSchema(): void {
    this.spine.store.db.exec(`
      CREATE TABLE IF NOT EXISTS runtime_projector_global_cursor (
        projector_id TEXT PRIMARY KEY,
        global_sequence INTEGER NOT NULL,
        event_id TEXT,
        digest TEXT NOT NULL,
        generation INTEGER NOT NULL,
        updated_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS runtime_projected_timeline (
        event_id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        aggregate_id TEXT NOT NULL,
        aggregate_sequence INTEGER NOT NULL,
        global_sequence INTEGER NOT NULL UNIQUE,
        correlation_id TEXT NOT NULL,
        causation_id TEXT,
        intent TEXT NOT NULL,
        effect TEXT NOT NULL,
        sender_kind TEXT NOT NULL,
        sender_id TEXT NOT NULL,
        summary TEXT NOT NULL,
        state_domain TEXT NOT NULL,
        state_operation TEXT NOT NULL,
        state_path_json TEXT NOT NULL,
        evidence_count INTEGER NOT NULL,
        artifact_refs_json TEXT NOT NULL,
        uncertainty REAL,
        created_at TEXT NOT NULL,
        committed_at TEXT NOT NULL,
        projected_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_runtime_projected_timeline_aggregate
        ON runtime_projected_timeline(aggregate_id, global_sequence);
      CREATE INDEX IF NOT EXISTS idx_runtime_projected_timeline_correlation
        ON runtime_projected_timeline(correlation_id, global_sequence);

      CREATE TABLE IF NOT EXISTS runtime_projected_task_views (
        aggregate_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        status TEXT NOT NULL,
        phase TEXT NOT NULL,
        view_json TEXT NOT NULL,
        view_digest TEXT NOT NULL,
        last_global_sequence INTEGER NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_runtime_projected_task_views_task
        ON runtime_projected_task_views(task_id, last_global_sequence);

      CREATE TABLE IF NOT EXISTS runtime_projector_failures (
        failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL,
        aggregate_id TEXT NOT NULL,
        global_sequence INTEGER NOT NULL,
        delivery_id TEXT,
        error_code TEXT NOT NULL,
        error_message TEXT NOT NULL,
        acknowledged_before_failure INTEGER NOT NULL,
        created_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_runtime_projector_failures_cursor
        ON runtime_projector_failures(global_sequence, event_id);
    `);
    if (!this.globalCursorRow()) {
      const now = utcNow();
      this.spine.store.db.prepare(`
        INSERT INTO runtime_projector_global_cursor (
          projector_id, global_sequence, event_id, digest, generation, updated_at
        ) VALUES (?, 0, NULL, ?, 1, ?)
      `).run(this.consumerId, digestJson({ cursor: 0 }), now);
    }
  }

  admit(event: RuntimeEventEnvelope): readonly string[] {
    if (this.isProjected(event)) return [];
    const inserted = this.spine.bus.ensureDelivery(this.subscriptionId, event, true);
    if (this.autoDrain) {
      const report = this.pump({ catchUp: false, maxMessages: 1 });
      if (report.failed > 0) {
        throw new ProjectorError(EventSpineErrorCode.PROJECTOR_DIVERGENCE, "projector delivery failed", {
          event_id: event.eventId,
          findings: report.findings,
        });
      }
    }
    return inserted;
  }

  catchUp(afterGlobalSequence?: number): number {
    const cursor = afterGlobalSequence ?? this.globalCursor().globalSequence;
    return this.spine.bus.catchUp(this.subscriptionId, {
      afterSequence: cursor,
      limit: this.catchUpLimit,
      projectorMaintenance: true,
    });
  }

  pump(options: { catchUp?: boolean; maxMessages?: number } = {}): ProjectionPumpReport {
    const startedAt = utcNow();
    const before = this.globalCursor();
    let caughtUp = 0;
    let leased = 0;
    let acknowledged = 0;
    let projected = 0;
    let duplicates = 0;
    let failed = 0;
    let repairedAckGaps = 0;
    const findings: string[] = [];
    if (options.catchUp ?? true) caughtUp = this.catchUp(before.globalSequence);
    const maxMessages = Math.max(1, Math.min(options.maxMessages ?? this.catchUpLimit, 100_000));
    while (leased < maxMessages) {
      const batch = this.spine.poll(this.subscriptionId, Math.min(this.batchSize, maxMessages - leased));
      if (batch.length === 0) break;
      leased += batch.length;
      for (const item of batch) {
        const event = this.spine.get(item.delivery.eventId);
        if (!event) {
          failed += 1;
          findings.push(`delivery_missing_event:${item.delivery.deliveryId}`);
          this.spine.negativeAcknowledge(item.delivery.deliveryId, item.delivery.leaseToken!, "projector_missing_event", 0);
          continue;
        }
        if (this.isProjected(event)) {
          this.spine.acknowledge(item.delivery.deliveryId, item.delivery.leaseToken!);
          acknowledged += 1;
          duplicates += 1;
          continue;
        }
        try {
          this.applyAndAcknowledge(event, item.delivery.deliveryId, item.delivery.leaseToken!);
          acknowledged += 1;
          projected += 1;
        } catch (error) {
          failed += 1;
          const normalized = asEventSpineError(error, "projector consumer failed");
          this.recordFailure(event, item.delivery.deliveryId, normalized, false);
          try {
            this.spine.negativeAcknowledge(
              item.delivery.deliveryId,
              item.delivery.leaseToken!,
              normalized.message,
              100,
            );
          } catch {
            // A concurrent sweeper can reclaim the lease.  Durable catch-up is
            // still driven by the projector cursor, never by this local error.
          }
          findings.push(`projector_apply_failed:${event.eventId}:${normalized.code}`);
        }
      }
    }
    const cursor = this.globalCursor();
    return {
      schema: "zyra.runtime-projector-pump/v1",
      subscriptionId: this.subscriptionId,
      consumerId: this.consumerId,
      startedAt,
      completedAt: utcNow(),
      highWatermark: this.spine.store.latestGlobalSequence(),
      cursor: cursor.globalSequence,
      caughtUp,
      leased,
      acknowledged,
      projected,
      duplicates,
      failed,
      repairedAckGaps,
      findings,
    };
  }

  apply(event: RuntimeEventEnvelope): boolean {
    if (this.isProjected(event)) return false;
    const cursor = this.globalCursor();
    if (event.globalSequence > cursor.globalSequence + 1) {
      throw new ProjectorError(EventSpineErrorCode.PROJECTOR_GAP, "projector global cursor has a gap", {
        expected_global_sequence: cursor.globalSequence + 1,
        actual_global_sequence: event.globalSequence,
        event_id: event.eventId,
      });
    }
    if (event.globalSequence < cursor.globalSequence) {
      // Another aggregate can legitimately have been projected through a
      // targeted repair.  Event-id idempotence remains authoritative.
      if (this.timelineEvent(event.eventId)) return false;
    }
    const previous = this.taskView(event.aggregateId);
    const next = foldTaskView(previous, event);
    this.spine.store.db.exec("BEGIN IMMEDIATE");
    try {
      this.applyWithinTransaction(event, cursor, next);
      this.spine.store.db.exec("COMMIT");
      return true;
    } catch (error) {
      try { this.spine.store.db.exec("ROLLBACK"); } catch { /* preserve apply failure */ }
      throw error;
    }
  }

  applyAndAcknowledge(event: RuntimeEventEnvelope, deliveryId: string, leaseToken: string): boolean {
    if (this.isProjected(event)) {
      this.spine.acknowledge(deliveryId, leaseToken);
      return false;
    }
    const cursor = this.globalCursor();
    if (event.globalSequence !== cursor.globalSequence + 1) {
      throw new ProjectorError(EventSpineErrorCode.PROJECTOR_GAP, "projector delivery is out of global order", {
        expected_global_sequence: cursor.globalSequence + 1,
        actual_global_sequence: event.globalSequence,
        event_id: event.eventId,
      });
    }
    const next = foldTaskView(this.taskView(event.aggregateId), event);
    this.spine.store.db.exec("BEGIN IMMEDIATE");
    try {
      this.applyWithinTransaction(event, cursor, next);
      // ACK and projector cursor commit atomically in the same SQLite owner.
      // A crash can therefore expose neither or both states, never an ACK-only
      // success that hides a missing read-model mutation.
      this.spine.acknowledge(deliveryId, leaseToken);
      this.spine.store.db.exec("COMMIT");
      return true;
    } catch (error) {
      try { this.spine.store.db.exec("ROLLBACK"); } catch { /* preserve apply failure */ }
      throw error;
    }
  }

  reconcileAckGaps(): number {
    const rows = this.spine.store.db.prepare(`
      SELECT d.event_id
      FROM runtime_event_deliveries d
      LEFT JOIN runtime_projected_timeline t ON t.event_id = d.event_id
      WHERE d.subscription_id = ? AND d.state = 'acknowledged' AND t.event_id IS NULL
      ORDER BY d.created_at, d.delivery_id
    `).all(this.subscriptionId) as unknown as Array<{ event_id: string }>;
    let repaired = 0;
    for (const row of rows) {
      repaired += this.spine.store.reopenAcknowledgedDelivery(
        row.event_id,
        this.subscriptionId,
        "projector_ack_cursor_gap_repair",
      ) ? 1 : 0;
    }
    return repaired;
  }

  rebuild(aggregateId?: string): Record<string, JsonValue> {
    const onlineChecksum = this.checksum(aggregateId);
    const events: RuntimeEventEnvelope[] = [];
    let cursor: string | undefined;
    do {
      const page = this.spine.query({
        ...(aggregateId ? { aggregateId } : {}),
        ...(cursor ? { cursor } : {}),
        limit: 1000,
      });
      events.push(...page.items);
      cursor = page.nextCursor;
    } while (cursor);
    const affected = aggregateId
      ? [aggregateId]
      : [...new Set(events.map((event) => event.aggregateId))];
    this.spine.store.db.exec("BEGIN IMMEDIATE");
    try {
      this.spine.store.projector.reset(this.spine.store.db, aggregateId);
      if (aggregateId) {
        this.spine.store.db.prepare("DELETE FROM runtime_projected_timeline WHERE aggregate_id = ?").run(aggregateId);
        this.spine.store.db.prepare("DELETE FROM runtime_projected_task_views WHERE aggregate_id = ?").run(aggregateId);
      } else {
        this.spine.store.db.exec("DELETE FROM runtime_projected_timeline; DELETE FROM runtime_projected_task_views;");
      }
      const views = new Map<string, ProjectedTaskView>();
      for (const event of events.sort((a, b) => a.globalSequence - b.globalSequence)) {
        this.spine.store.projector.apply(this.spine.store.db, event);
        this.insertTimeline(event);
        const next = foldTaskView(views.get(event.aggregateId), event);
        views.set(event.aggregateId, next);
        this.upsertTaskView(next);
      }
      if (!aggregateId) {
        const last = events.at(-1);
        this.spine.store.db.prepare(`
          UPDATE runtime_projector_global_cursor
          SET global_sequence = ?, event_id = ?, digest = ?, generation = generation + 1, updated_at = ?
          WHERE projector_id = ?
        `).run(
          last?.globalSequence ?? 0,
          last?.eventId ?? null,
          this.checksumDigest(events),
          utcNow(),
          this.consumerId,
        );
      }
      this.spine.store.db.exec("COMMIT");
    } catch (error) {
      try { this.spine.store.db.exec("ROLLBACK"); } catch { /* preserve rebuild failure */ }
      throw error;
    }
    const rebuiltChecksum = this.checksum(aggregateId);
    return {
      schema: "zyra.runtime-projector-rebuild/v2",
      aggregate_id: aggregateId ?? null,
      event_count: events.length,
      aggregate_count: affected.length,
      online_checksum: onlineChecksum,
      rebuilt_checksum: rebuiltChecksum,
      equivalent: onlineChecksum === rebuiltChecksum,
      rebuilt_at: utcNow(),
    };
  }

  taskView(aggregateId: string): ProjectedTaskView | undefined {
    const row = this.spine.store.db.prepare(`
      SELECT * FROM runtime_projected_task_views WHERE aggregate_id = ?
    `).get(aggregateId) as TaskViewRow | undefined;
    if (!row) return undefined;
    const view = JSON.parse(row.view_json) as ProjectedTaskView;
    if (view.digest !== row.view_digest || digestTaskView(view) !== view.digest) {
      throw new ProjectorError(EventSpineErrorCode.PROJECTOR_DIVERGENCE, "stored task view digest mismatch", {
        aggregate_id: aggregateId,
      });
    }
    return view;
  }

  taskViewByTaskId(taskId: string): ProjectedTaskView | undefined {
    const row = this.spine.store.db.prepare(`
      SELECT view_json FROM runtime_projected_task_views
      WHERE task_id = ? ORDER BY last_global_sequence DESC LIMIT 1
    `).get(taskId) as { view_json: string } | undefined;
    return row ? JSON.parse(row.view_json) : undefined;
  }

  history(
    aggregateId: string,
    options: { afterGlobalSequence?: number; limit?: number; eventTypes?: readonly string[] } = {},
  ): ProjectedHistoryView {
    const limit = Math.max(1, Math.min(options.limit ?? 200, 1000));
    const params: Array<string | number> = [aggregateId, options.afterGlobalSequence ?? 0];
    const clauses = ["aggregate_id = ?", "global_sequence > ?"];
    if (options.eventTypes && options.eventTypes.length > 0) {
      clauses.push(`event_type IN (${options.eventTypes.map(() => "?").join(",")})`);
      params.push(...options.eventTypes);
    }
    params.push(limit + 1);
    const rows = this.spine.store.db.prepare(`
      SELECT * FROM runtime_projected_timeline
      WHERE ${clauses.join(" AND ")}
      ORDER BY global_sequence ASC LIMIT ?
    `).all(...params) as unknown as TimelineRow[];
    const hasMore = rows.length > limit;
    const selected = rows.slice(0, limit).map(timelineFromRow);
    const global = this.globalCursor();
    const last = selected.at(-1);
    return {
      schema: PROJECTOR_HISTORY_SCHEMA,
      aggregateId,
      cursor: {
        aggregateId,
        globalSequence: last?.globalSequence ?? options.afterGlobalSequence ?? 0,
        projectionSequence: this.spine.store.projector.cursor(this.spine.store.db, aggregateId)?.sequence ?? -1,
        eventId: last?.eventId,
        projectionDigest: this.taskView(aggregateId)?.digest,
        generation: global.generation,
      },
      items: selected,
      nextGlobalSequence: hasMore ? last?.globalSequence : undefined,
      hasMore,
      highWatermark: this.spine.store.latestGlobalSequence(),
      projectionLag: Math.max(0, this.spine.store.latestGlobalSequence() - global.globalSequence),
    };
  }

  stream(
    aggregateId: string,
    cursor: Partial<ProjectorStreamCursor> = {},
    limit = 200,
  ): readonly ProjectorStreamFrame[] {
    const view = this.taskView(aggregateId);
    const global = this.globalCursor();
    const requested = Math.max(0, cursor.globalSequence ?? 0);
    const history = this.history(aggregateId, { afterGlobalSequence: requested, limit });
    const frames: ProjectorStreamFrame[] = [];
    if (requested === 0 && view) {
      frames.push({
        schema: PROJECTOR_STREAM_SCHEMA,
        frameId: newId("projection-frame"),
        kind: StreamFrameKind.SNAPSHOT,
        aggregateId,
        cursor: history.cursor,
        projection: cloneJson(view as unknown as JsonValue) as Record<string, JsonValue>,
        missingSequences: [],
        createdAt: utcNow(),
        metadata: { canonical_write_allowed: false, predictive_overlay: false },
      });
    }
    let expected = requested + 1;
    for (const item of history.items) {
      const missing: number[] = [];
      // Global sequences for other aggregates are not gaps in this aggregate's
      // view, but they are exposed as skipped positions for resume diagnostics.
      while (expected < item.globalSequence && missing.length < 256) {
        missing.push(expected);
        expected += 1;
      }
      const event = this.spine.get(item.eventId);
      if (!event) throw new ProjectorError(EventSpineErrorCode.PROJECTOR_DIVERGENCE, "timeline references missing canonical event", { event_id: item.eventId });
      frames.push({
        schema: PROJECTOR_STREAM_SCHEMA,
        frameId: newId("projection-frame"),
        kind: missing.length > 0 ? StreamFrameKind.GAP : StreamFrameKind.EVENT,
        aggregateId,
        cursor: {
          aggregateId,
          globalSequence: item.globalSequence,
          projectionSequence: item.aggregateSequence,
          eventId: item.eventId,
          projectionDigest: view?.digest,
          generation: global.generation,
        },
        event,
        missingSequences: missing,
        createdAt: utcNow(),
        metadata: {
          canonical_write_allowed: false,
          source: "RuntimeStateProjector",
          raw_inline_exposed: false,
        },
      });
      expected = item.globalSequence + 1;
    }
    if (frames.length === 0) {
      frames.push({
        schema: PROJECTOR_STREAM_SCHEMA,
        frameId: newId("projection-frame"),
        kind: StreamFrameKind.HEARTBEAT,
        aggregateId,
        cursor: {
          aggregateId,
          globalSequence: requested,
          projectionSequence: this.spine.store.projector.cursor(this.spine.store.db, aggregateId)?.sequence ?? -1,
          eventId: cursor.eventId,
          projectionDigest: view?.digest,
          generation: global.generation,
        },
        missingSequences: [],
        retryAfterMs: 1000,
        createdAt: utcNow(),
        metadata: { canonical_write_allowed: false },
      });
    }
    return frames;
  }

  globalCursor(): ProjectorStreamCursor {
    const row = this.globalCursorRow();
    if (!row) throw new ProjectorError(EventSpineErrorCode.PROJECTOR_DIVERGENCE, "projector global cursor is missing");
    return {
      aggregateId: "*",
      globalSequence: Number(row.global_sequence),
      projectionSequence: Number(row.global_sequence),
      eventId: row.event_id ?? undefined,
      projectionDigest: row.digest,
      generation: Number(row.generation),
    };
  }

  checksum(aggregateId?: string): string {
    const timeline = aggregateId
      ? this.spine.store.db.prepare(`
          SELECT event_id, global_sequence FROM runtime_projected_timeline
          WHERE aggregate_id = ? ORDER BY global_sequence
        `).all(aggregateId)
      : this.spine.store.db.prepare(`
          SELECT event_id, global_sequence FROM runtime_projected_timeline ORDER BY global_sequence
        `).all();
    const views = aggregateId
      ? this.spine.store.db.prepare(`
          SELECT aggregate_id, view_digest FROM runtime_projected_task_views
          WHERE aggregate_id = ? ORDER BY aggregate_id
        `).all(aggregateId)
      : this.spine.store.db.prepare(`
          SELECT aggregate_id, view_digest FROM runtime_projected_task_views ORDER BY aggregate_id
        `).all();
    return digestJson(JSON.parse(JSON.stringify({ timeline, views })) as JsonValue);
  }

  coverage(aggregateId?: string): ProjectionCoverageReport {
    type CountRow = { event_count: number; aggregate_count: number; high_watermark: number | null };
    type ViewCountRow = { aggregate_count: number; high_watermark: number | null };
    const canonical = (aggregateId
      ? this.spine.store.db.prepare(`
          SELECT COUNT(*) AS event_count,
                 COUNT(DISTINCT aggregate_id) AS aggregate_count,
                 MAX(global_sequence) AS high_watermark
          FROM runtime_events WHERE aggregate_id = ?
        `).get(aggregateId)
      : this.spine.store.db.prepare(`
          SELECT COUNT(*) AS event_count,
                 COUNT(DISTINCT aggregate_id) AS aggregate_count,
                 MAX(global_sequence) AS high_watermark
          FROM runtime_events
        `).get()) as CountRow;
    const projected = (aggregateId
      ? this.spine.store.db.prepare(`
          SELECT COUNT(*) AS event_count,
                 COUNT(DISTINCT aggregate_id) AS aggregate_count,
                 MAX(global_sequence) AS high_watermark
          FROM runtime_projected_timeline WHERE aggregate_id = ?
        `).get(aggregateId)
      : this.spine.store.db.prepare(`
          SELECT COUNT(*) AS event_count,
                 COUNT(DISTINCT aggregate_id) AS aggregate_count,
                 MAX(global_sequence) AS high_watermark
          FROM runtime_projected_timeline
        `).get()) as CountRow;
    const views = (aggregateId
      ? this.spine.store.db.prepare(`
          SELECT COUNT(*) AS aggregate_count,
                 MAX(last_global_sequence) AS high_watermark
          FROM runtime_projected_task_views WHERE aggregate_id = ?
        `).get(aggregateId)
      : this.spine.store.db.prepare(`
          SELECT COUNT(*) AS aggregate_count,
                 MAX(last_global_sequence) AS high_watermark
          FROM runtime_projected_task_views
        `).get()) as ViewCountRow;
    const canonicalEvents = Number(canonical.event_count);
    const canonicalAggregates = Number(canonical.aggregate_count);
    const canonicalHighWatermark = Number(canonical.high_watermark ?? 0);
    const projectedEvents = Number(projected.event_count);
    const projectedAggregates = Number(projected.aggregate_count);
    const projectedHighWatermark = Number(projected.high_watermark ?? 0);
    const viewAggregates = Number(views.aggregate_count);
    const viewHighWatermark = Number(views.high_watermark ?? 0);
    const cursorHighWatermark = aggregateId
      ? viewHighWatermark
      : this.globalCursor().globalSequence;
    const eventLag = Math.max(0, canonicalEvents - projectedEvents);
    const sequenceLag = Math.max(0, canonicalHighWatermark - Math.max(projectedHighWatermark, cursorHighWatermark));
    return {
      ...(aggregateId ? { aggregateId } : {}),
      canonicalEvents,
      canonicalAggregates,
      canonicalHighWatermark,
      projectedEvents,
      projectedAggregates,
      projectedHighWatermark,
      viewAggregates,
      viewHighWatermark,
      cursorHighWatermark,
      eventLag,
      sequenceLag,
      complete: canonicalEvents === projectedEvents
        && canonicalAggregates === projectedAggregates
        && canonicalAggregates === viewAggregates
        && canonicalHighWatermark === projectedHighWatermark
        && canonicalHighWatermark === viewHighWatermark
        && canonicalHighWatermark === cursorHighWatermark,
    };
  }

  resetPredictiveOverlay(_aggregateId: string): Record<string, JsonValue> {
    return {
      schema: "zyra.runtime-predictive-overlay/v1",
      canonical_write_allowed: false,
      projector_cursor_advanced: false,
      persistence: "none",
      action: "client_must_discard_overlay",
    };
  }

  private isProjected(event: RuntimeEventEnvelope): boolean {
    return Boolean(this.timelineEvent(event.eventId));
  }

  private applyWithinTransaction(
    event: RuntimeEventEnvelope,
    cursor: ProjectorStreamCursor,
    next: ProjectedTaskView,
  ): void {
    this.spine.store.projector.apply(this.spine.store.db, event);
    this.insertTimeline(event);
    this.upsertTaskView(next);
    const nextGlobal = Math.max(cursor.globalSequence, event.globalSequence);
    const digest = digestJson({
      previous: cursor.projectionDigest ?? digestJson({ cursor: cursor.globalSequence }),
      event_id: event.eventId,
      event_digest: event.contentDigest,
      global_sequence: nextGlobal,
      task_view_digest: next.digest,
    });
    this.spine.store.db.prepare(`
      UPDATE runtime_projector_global_cursor
      SET global_sequence = ?, event_id = ?, digest = ?, updated_at = ?
      WHERE projector_id = ?
    `).run(nextGlobal, event.eventId, digest, utcNow(), this.consumerId);
  }

  private timelineEvent(eventId: string): TimelineRow | undefined {
    return this.spine.store.db.prepare(`
      SELECT * FROM runtime_projected_timeline WHERE event_id = ?
    `).get(eventId) as TimelineRow | undefined;
  }

  private globalCursorRow(): GlobalCursorRow | undefined {
    return this.spine.store.db.prepare(`
      SELECT * FROM runtime_projector_global_cursor WHERE projector_id = ?
    `).get(this.consumerId) as GlobalCursorRow | undefined;
  }

  private insertTimeline(event: RuntimeEventEnvelope): void {
    this.spine.store.db.prepare(`
      INSERT INTO runtime_projected_timeline (
        event_id, event_type, aggregate_id, aggregate_sequence, global_sequence,
        correlation_id, causation_id, intent, effect, sender_kind, sender_id,
        summary, state_domain, state_operation, state_path_json, evidence_count,
        artifact_refs_json, uncertainty, created_at, committed_at, projected_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(event_id) DO NOTHING
    `).run(
      event.eventId,
      event.eventType,
      event.aggregateId,
      event.aggregateSequence,
      event.globalSequence,
      event.correlationId,
      event.causationId ?? null,
      event.intent,
      event.effect,
      event.sender.kind,
      event.sender.id,
      event.summary,
      event.stateDelta.domain,
      event.stateDelta.operation,
      canonicalJson(event.stateDelta.path as unknown as JsonValue),
      event.evidenceRefs.length,
      canonicalJson(event.artifactRefs as unknown as JsonValue),
      event.uncertainty ?? null,
      event.createdAt,
      event.committedAt,
      utcNow(),
    );
  }

  private upsertTaskView(view: ProjectedTaskView): void {
    this.spine.store.db.prepare(`
      INSERT INTO runtime_projected_task_views (
        aggregate_id, task_id, run_id, status, phase, view_json,
        view_digest, last_global_sequence, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(aggregate_id) DO UPDATE SET
        task_id = excluded.task_id,
        run_id = excluded.run_id,
        status = excluded.status,
        phase = excluded.phase,
        view_json = excluded.view_json,
        view_digest = excluded.view_digest,
        last_global_sequence = excluded.last_global_sequence,
        updated_at = excluded.updated_at
      WHERE excluded.last_global_sequence >= runtime_projected_task_views.last_global_sequence
    `).run(
      view.aggregateId,
      view.taskId,
      view.runId,
      view.status,
      view.phase,
      canonicalJson(view as unknown as JsonValue),
      view.digest,
      view.lastGlobalSequence,
      view.updatedAt,
    );
  }

  private recordFailure(
    event: RuntimeEventEnvelope,
    deliveryId: string | undefined,
    error: EventSpineError,
    acknowledged: boolean,
  ): void {
    this.spine.store.db.prepare(`
      INSERT INTO runtime_projector_failures (
        event_id, aggregate_id, global_sequence, delivery_id, error_code,
        error_message, acknowledged_before_failure, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      event.eventId,
      event.aggregateId,
      event.globalSequence,
      deliveryId ?? null,
      error.code,
      error.message.slice(0, 4000),
      acknowledged ? 1 : 0,
      utcNow(),
    );
  }

  private checksumDigest(events: readonly RuntimeEventEnvelope[]): string {
    return digestJson(events.map((event) => ({
      event_id: event.eventId,
      event_digest: event.contentDigest,
      global_sequence: event.globalSequence,
    })) as unknown as JsonValue);
  }
}
