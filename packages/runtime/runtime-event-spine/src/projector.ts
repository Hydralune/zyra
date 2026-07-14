import type { DatabaseSync } from "node:sqlite";
import {
  EventEffect,
  type ArtifactPointer,
  type ProjectedArtifact,
  type ProjectedControlState,
  type ProjectedSession,
  type ProjectedToolCall,
  type ProjectionCursor,
  type RuntimeEventEnvelope,
} from "./contracts.ts";
import { canonicalJson, digestJson, requireRecord, type JsonValue } from "./canonical.ts";
import { EventSpineErrorCode, ProjectorError } from "./errors.ts";

export const RUNTIME_STATE_PROJECTOR_ID = "zyra.runtime-state-projector/v1";

export interface ProjectionApplyReceipt {
  projectorId: string;
  aggregateId: string;
  eventId: string;
  sequence: number;
  duplicate: boolean;
  domains: readonly string[];
  cursor: ProjectionCursor;
}

export interface ProjectionRebuildReceipt {
  projectorId: string;
  aggregateId?: string;
  eventCount: number;
  aggregateCount: number;
  firstSequence?: number;
  lastSequence?: number;
  digest: string;
  rebuiltAt: string;
}

interface CursorRow {
  projector_id: string;
  aggregate_id: string;
  sequence: number;
  event_id: string;
  digest: string;
  updated_at: string;
}

interface SessionRow {
  aggregate_id: string;
  run_id: string;
  session_id: string | null;
  task_id: string;
  status: string;
  last_event_id: string;
  last_sequence: number;
  last_intent: string;
  effective_transitions: number;
  non_effective_events: number;
  tool_calls_started: number;
  tool_calls_settled: number;
  permission_pending: number;
  artifact_count: number;
  worker_routes: number;
  failure_count: number;
  compact_count: number;
  metadata_json: string;
  updated_at: string;
}

interface ToolRow {
  aggregate_id: string;
  tool_call_id: string;
  tool_name: string;
  status: ProjectedToolCall["status"];
  called_event_id: string | null;
  settled_event_id: string | null;
  input_digest: string | null;
  result_digest: string | null;
  artifact_refs_json: string;
  progress_sequence: number;
  provider_executed: number;
  malformed_result: number;
  error_code: string | null;
  updated_at: string;
}

interface ControlRow {
  aggregate_id: string;
  control_id: string;
  control_kind: string;
  status: string;
  requested_event_id: string;
  resolved_event_id: string | null;
  decision: string | null;
  actor_id: string | null;
  updated_at: string;
}

interface ArtifactRow {
  aggregate_id: string;
  artifact_id: string;
  artifact_json: string;
  source_event_id: string;
  intent: string;
  updated_at: string;
}

interface StreamRow {
  aggregate_id: string;
  stream_id: string;
  stream_kind: string;
  status: string;
  started_event_id: string;
  ended_event_id: string | null;
  final_artifact_id: string | null;
  final_digest: string | null;
  updated_at: string;
}

function asCursor(row: CursorRow): ProjectionCursor {
  return {
    projectorId: row.projector_id,
    aggregateId: row.aggregate_id,
    sequence: Number(row.sequence),
    eventId: row.event_id,
    digest: row.digest,
    updatedAt: row.updated_at,
  };
}

function jsonObject(value: string): Record<string, JsonValue> {
  const parsed = JSON.parse(value);
  return requireRecord(parsed, "stored_json");
}

function parseArtifacts(value: string): ArtifactPointer[] {
  const parsed = JSON.parse(value);
  return Array.isArray(parsed) ? (parsed as ArtifactPointer[]) : [];
}

function scalar(inline: Readonly<Record<string, JsonValue>>, key: string): string | undefined {
  const value = inline[key];
  return typeof value === "string" && value ? value : undefined;
}

function booleanScalar(inline: Readonly<Record<string, JsonValue>>, key: string): boolean {
  return inline[key] === true;
}

function streamIdentity(event: RuntimeEventEnvelope): string {
  return (
    scalar(event.inline, "stream_id") ??
    scalar(event.inline, "text_id") ??
    scalar(event.inline, "reasoning_id") ??
    event.identity.toolCallId ??
    `${event.eventType}:${event.eventId}`
  );
}

function controlIdentity(event: RuntimeEventEnvelope): string | undefined {
  return scalar(event.inline, "control_id") ?? scalar(event.inline, "permission_id") ?? event.identity.commandId;
}

function sessionStatus(event: RuntimeEventEnvelope, current: string): string {
  if (event.eventType.endsWith(".completed")) return "completed";
  if (event.eventType.endsWith(".failed")) return "failed";
  if (event.eventType.endsWith(".cancelled")) return "cancelled";
  if (event.eventType === "runtime.task.created") return "pending";
  if (event.eventType.includes("started") || event.eventType.includes("called") || event.eventType.includes("dispatched")) return "running";
  return current || "active";
}

export class RuntimeStateProjector {
  readonly projectorId: string;

  constructor(projectorId = RUNTIME_STATE_PROJECTOR_ID) {
    this.projectorId = projectorId;
  }

  initialize(db: DatabaseSync): void {
    db.exec(`
      CREATE TABLE IF NOT EXISTS runtime_projector_cursors (
        projector_id TEXT NOT NULL,
        aggregate_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        event_id TEXT NOT NULL,
        digest TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (projector_id, aggregate_id)
      );

      CREATE TABLE IF NOT EXISTS runtime_projected_sessions (
        aggregate_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        session_id TEXT,
        task_id TEXT NOT NULL,
        status TEXT NOT NULL,
        last_event_id TEXT NOT NULL,
        last_sequence INTEGER NOT NULL,
        last_intent TEXT NOT NULL,
        effective_transitions INTEGER NOT NULL DEFAULT 0,
        non_effective_events INTEGER NOT NULL DEFAULT 0,
        tool_calls_started INTEGER NOT NULL DEFAULT 0,
        tool_calls_settled INTEGER NOT NULL DEFAULT 0,
        permission_pending INTEGER NOT NULL DEFAULT 0,
        artifact_count INTEGER NOT NULL DEFAULT 0,
        worker_routes INTEGER NOT NULL DEFAULT 0,
        failure_count INTEGER NOT NULL DEFAULT 0,
        compact_count INTEGER NOT NULL DEFAULT 0,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL
      );

      CREATE INDEX IF NOT EXISTS idx_runtime_projected_sessions_task
        ON runtime_projected_sessions(task_id, updated_at);

      CREATE TABLE IF NOT EXISTS runtime_projected_tools (
        aggregate_id TEXT NOT NULL,
        tool_call_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        status TEXT NOT NULL,
        called_event_id TEXT,
        settled_event_id TEXT,
        input_digest TEXT,
        result_digest TEXT,
        artifact_refs_json TEXT NOT NULL DEFAULT '[]',
        progress_sequence INTEGER NOT NULL DEFAULT 0,
        provider_executed INTEGER NOT NULL DEFAULT 0,
        malformed_result INTEGER NOT NULL DEFAULT 0,
        error_code TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (aggregate_id, tool_call_id)
      );

      CREATE INDEX IF NOT EXISTS idx_runtime_projected_tools_status
        ON runtime_projected_tools(aggregate_id, status, updated_at);

      CREATE TABLE IF NOT EXISTS runtime_projected_controls (
        aggregate_id TEXT NOT NULL,
        control_id TEXT NOT NULL,
        control_kind TEXT NOT NULL,
        status TEXT NOT NULL,
        requested_event_id TEXT NOT NULL,
        resolved_event_id TEXT,
        decision TEXT,
        actor_id TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (aggregate_id, control_id)
      );

      CREATE TABLE IF NOT EXISTS runtime_projected_artifacts (
        aggregate_id TEXT NOT NULL,
        artifact_id TEXT NOT NULL,
        artifact_json TEXT NOT NULL,
        source_event_id TEXT NOT NULL,
        intent TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (aggregate_id, artifact_id)
      );

      CREATE INDEX IF NOT EXISTS idx_runtime_projected_artifacts_event
        ON runtime_projected_artifacts(source_event_id);

      CREATE TABLE IF NOT EXISTS runtime_projected_streams (
        aggregate_id TEXT NOT NULL,
        stream_id TEXT NOT NULL,
        stream_kind TEXT NOT NULL,
        status TEXT NOT NULL,
        started_event_id TEXT NOT NULL,
        ended_event_id TEXT,
        final_artifact_id TEXT,
        final_digest TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (aggregate_id, stream_id)
      );

      CREATE TABLE IF NOT EXISTS runtime_projected_messages (
        aggregate_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        intent TEXT NOT NULL,
        sender_kind TEXT NOT NULL,
        sender_id TEXT NOT NULL,
        summary TEXT NOT NULL,
        state_delta_json TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL,
        artifact_refs_json TEXT NOT NULL,
        effective INTEGER NOT NULL,
        correlation_id TEXT NOT NULL,
        causation_id TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (aggregate_id, event_id),
        UNIQUE (aggregate_id, sequence)
      );

      CREATE INDEX IF NOT EXISTS idx_runtime_projected_messages_intent
        ON runtime_projected_messages(aggregate_id, intent, sequence);
    `);
  }

  apply(db: DatabaseSync, event: RuntimeEventEnvelope): ProjectionApplyReceipt {
    this.initialize(db);
    const existing = this.cursor(db, event.aggregateId);
    if (existing && event.aggregateSequence <= existing.sequence) {
      if (existing.sequence === event.aggregateSequence && existing.eventId === event.eventId) {
        return {
          projectorId: this.projectorId,
          aggregateId: event.aggregateId,
          eventId: event.eventId,
          sequence: event.aggregateSequence,
          duplicate: true,
          domains: [],
          cursor: existing,
        };
      }
      throw new ProjectorError(EventSpineErrorCode.PROJECTOR_DIVERGENCE, "projector received divergent historical event", {
        aggregate_id: event.aggregateId,
        event_id: event.eventId,
        sequence: event.aggregateSequence,
        cursor_event_id: existing.eventId,
        cursor_sequence: existing.sequence,
      });
    }
    const expected = existing ? existing.sequence + 1 : 0;
    if (event.aggregateSequence !== expected) {
      throw new ProjectorError(EventSpineErrorCode.PROJECTOR_GAP, "projector sequence gap", {
        aggregate_id: event.aggregateId,
        expected_sequence: expected,
        actual_sequence: event.aggregateSequence,
        event_id: event.eventId,
      });
    }
    const domains = new Set<string>();
    this.projectMessage(db, event);
    domains.add("message");
    this.projectSession(db, event);
    domains.add("session");
    if (event.eventType.startsWith("runtime.tool.")) {
      this.projectTool(db, event);
      domains.add("tool");
    }
    if (event.eventType.startsWith("runtime.permission.") || event.eventType.startsWith("runtime.control.")) {
      this.projectControl(db, event);
      domains.add("control");
    }
    if (event.artifactRefs.length > 0 || event.eventType.startsWith("runtime.artifact.")) {
      this.projectArtifacts(db, event);
      domains.add("artifact");
    }
    if (
      event.eventType.startsWith("runtime.text.") ||
      event.eventType.startsWith("runtime.reasoning.") ||
      event.eventType.startsWith("runtime.tool.input.")
    ) {
      this.projectStream(db, event);
      domains.add("stream");
    }
    const cursor: ProjectionCursor = {
      projectorId: this.projectorId,
      aggregateId: event.aggregateId,
      sequence: event.aggregateSequence,
      eventId: event.eventId,
      digest: event.contentDigest,
      updatedAt: event.committedAt,
    };
    db.prepare(`
      INSERT INTO runtime_projector_cursors (
        projector_id, aggregate_id, sequence, event_id, digest, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?)
      ON CONFLICT(projector_id, aggregate_id) DO UPDATE SET
        sequence = excluded.sequence,
        event_id = excluded.event_id,
        digest = excluded.digest,
        updated_at = excluded.updated_at
    `).run(this.projectorId, event.aggregateId, event.aggregateSequence, event.eventId, event.contentDigest, event.committedAt);
    return {
      projectorId: this.projectorId,
      aggregateId: event.aggregateId,
      eventId: event.eventId,
      sequence: event.aggregateSequence,
      duplicate: false,
      domains: [...domains].sort(),
      cursor,
    };
  }

  cursor(db: DatabaseSync, aggregateId: string): ProjectionCursor | undefined {
    this.initialize(db);
    const row = db.prepare(`
      SELECT projector_id, aggregate_id, sequence, event_id, digest, updated_at
      FROM runtime_projector_cursors
      WHERE projector_id = ? AND aggregate_id = ?
    `).get(this.projectorId, aggregateId) as CursorRow | undefined;
    return row ? asCursor(row) : undefined;
  }

  session(db: DatabaseSync, aggregateId: string): ProjectedSession | undefined {
    this.initialize(db);
    const row = db.prepare("SELECT * FROM runtime_projected_sessions WHERE aggregate_id = ?").get(aggregateId) as SessionRow | undefined;
    if (!row) return undefined;
    return {
      aggregateId: row.aggregate_id,
      runId: row.run_id,
      sessionId: row.session_id ?? undefined,
      taskId: row.task_id,
      status: row.status,
      lastEventId: row.last_event_id,
      lastSequence: Number(row.last_sequence),
      lastIntent: row.last_intent,
      effectiveTransitions: Number(row.effective_transitions),
      nonEffectiveEvents: Number(row.non_effective_events),
      toolCallsStarted: Number(row.tool_calls_started),
      toolCallsSettled: Number(row.tool_calls_settled),
      permissionPending: Number(row.permission_pending),
      artifactCount: Number(row.artifact_count),
      workerRoutes: Number(row.worker_routes),
      failureCount: Number(row.failure_count),
      compactCount: Number(row.compact_count),
      metadata: jsonObject(row.metadata_json),
      updatedAt: row.updated_at,
    };
  }

  toolCalls(db: DatabaseSync, aggregateId: string): readonly ProjectedToolCall[] {
    this.initialize(db);
    const rows = db.prepare("SELECT * FROM runtime_projected_tools WHERE aggregate_id = ? ORDER BY updated_at, tool_call_id").all(aggregateId) as unknown as ToolRow[];
    return rows.map((row) => ({
      aggregateId: row.aggregate_id,
      toolCallId: row.tool_call_id,
      toolName: row.tool_name,
      status: row.status,
      calledEventId: row.called_event_id ?? undefined,
      settledEventId: row.settled_event_id ?? undefined,
      inputDigest: row.input_digest ?? undefined,
      resultDigest: row.result_digest ?? undefined,
      artifactRefs: parseArtifacts(row.artifact_refs_json),
      progressSequence: Number(row.progress_sequence),
      providerExecuted: Boolean(row.provider_executed),
      malformedResult: Boolean(row.malformed_result),
      errorCode: row.error_code ?? undefined,
      updatedAt: row.updated_at,
    }));
  }

  controls(db: DatabaseSync, aggregateId: string): readonly ProjectedControlState[] {
    this.initialize(db);
    const rows = db.prepare("SELECT * FROM runtime_projected_controls WHERE aggregate_id = ? ORDER BY updated_at, control_id").all(aggregateId) as unknown as ControlRow[];
    return rows.map((row) => ({
      aggregateId: row.aggregate_id,
      controlId: row.control_id,
      controlKind: row.control_kind,
      status: row.status,
      requestedEventId: row.requested_event_id,
      resolvedEventId: row.resolved_event_id ?? undefined,
      decision: row.decision ?? undefined,
      actorId: row.actor_id ?? undefined,
      updatedAt: row.updated_at,
    }));
  }

  artifacts(db: DatabaseSync, aggregateId: string): readonly ProjectedArtifact[] {
    this.initialize(db);
    const rows = db.prepare("SELECT * FROM runtime_projected_artifacts WHERE aggregate_id = ? ORDER BY updated_at, artifact_id").all(aggregateId) as unknown as ArtifactRow[];
    return rows.map((row) => ({
      aggregateId: row.aggregate_id,
      artifact: JSON.parse(row.artifact_json) as ArtifactPointer,
      sourceEventId: row.source_event_id,
      intent: row.intent as ProjectedArtifact["intent"],
      updatedAt: row.updated_at,
    }));
  }

  reset(db: DatabaseSync, aggregateId?: string): void {
    this.initialize(db);
    const tables = [
      "runtime_projector_cursors",
      "runtime_projected_sessions",
      "runtime_projected_tools",
      "runtime_projected_controls",
      "runtime_projected_artifacts",
      "runtime_projected_streams",
      "runtime_projected_messages",
    ];
    for (const table of tables) {
      if (aggregateId) {
        const column = table === "runtime_projector_cursors" ? "aggregate_id" : "aggregate_id";
        db.prepare(`DELETE FROM ${table} WHERE ${column} = ?`).run(aggregateId);
      } else {
        db.exec(`DELETE FROM ${table}`);
      }
    }
  }

  private projectMessage(db: DatabaseSync, event: RuntimeEventEnvelope): void {
    db.prepare(`
      INSERT INTO runtime_projected_messages (
        aggregate_id, event_id, sequence, event_type, intent, sender_kind, sender_id,
        summary, state_delta_json, evidence_refs_json, artifact_refs_json, effective,
        correlation_id, causation_id, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      event.aggregateId,
      event.eventId,
      event.aggregateSequence,
      event.eventType,
      event.intent,
      event.sender.kind,
      event.sender.id,
      event.summary,
      canonicalJson(event.stateDelta),
      canonicalJson(event.evidenceRefs),
      canonicalJson(event.artifactRefs),
      event.effect === EventEffect.EFFECTIVE ? 1 : 0,
      event.correlationId,
      event.causationId ?? null,
      event.createdAt,
    );
  }

  private projectSession(db: DatabaseSync, event: RuntimeEventEnvelope): void {
    const current = this.session(db, event.aggregateId);
    const effectiveTransitions = (current?.effectiveTransitions ?? 0) + (event.effect === EventEffect.EFFECTIVE ? 1 : 0);
    const nonEffectiveEvents = (current?.nonEffectiveEvents ?? 0) + (event.effect === EventEffect.NON_EFFECTIVE ? 1 : 0);
    const toolCallsStarted = (current?.toolCallsStarted ?? 0) + (event.eventType === "runtime.tool.called" ? 1 : 0);
    const toolCallsSettled = (current?.toolCallsSettled ?? 0) + (["runtime.tool.succeeded", "runtime.tool.failed", "runtime.tool.cancelled"].includes(event.eventType) ? 1 : 0);
    const permissionPending = Math.max(
      0,
      (current?.permissionPending ?? 0) +
        (event.eventType === "runtime.permission.pending" || event.eventType === "runtime.permission.requested" ? 1 : 0) -
        (["runtime.permission.allowed", "runtime.permission.denied"].includes(event.eventType) ? 1 : 0),
    );
    const artifactCount = (current?.artifactCount ?? 0) + event.artifactRefs.length;
    const workerRoutes = (current?.workerRoutes ?? 0) + (event.intent === "dispatch" ? 1 : 0);
    const failureCount = (current?.failureCount ?? 0) + (event.eventType.endsWith(".failed") || event.eventType.includes("failure") ? 1 : 0);
    const compactCount = (current?.compactCount ?? 0) + (event.eventType === "runtime.compact.completed" ? 1 : 0);
    const metadata = {
      last_sender_kind: event.sender.kind,
      last_sender_id: event.sender.id,
      last_correlation_id: event.correlationId,
      last_causation_id: event.causationId ?? "",
      last_state_domain: event.stateDelta.domain,
      last_state_path: [...event.stateDelta.path],
      last_artifact_ids: event.artifactRefs.map((item) => item.artifactId),
    };
    db.prepare(`
      INSERT INTO runtime_projected_sessions (
        aggregate_id, run_id, session_id, task_id, status, last_event_id,
        last_sequence, last_intent, effective_transitions, non_effective_events,
        tool_calls_started, tool_calls_settled, permission_pending, artifact_count,
        worker_routes, failure_count, compact_count, metadata_json, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(aggregate_id) DO UPDATE SET
        run_id = excluded.run_id,
        session_id = excluded.session_id,
        task_id = excluded.task_id,
        status = excluded.status,
        last_event_id = excluded.last_event_id,
        last_sequence = excluded.last_sequence,
        last_intent = excluded.last_intent,
        effective_transitions = excluded.effective_transitions,
        non_effective_events = excluded.non_effective_events,
        tool_calls_started = excluded.tool_calls_started,
        tool_calls_settled = excluded.tool_calls_settled,
        permission_pending = excluded.permission_pending,
        artifact_count = excluded.artifact_count,
        worker_routes = excluded.worker_routes,
        failure_count = excluded.failure_count,
        compact_count = excluded.compact_count,
        metadata_json = excluded.metadata_json,
        updated_at = excluded.updated_at
    `).run(
      event.aggregateId,
      event.identity.runId,
      event.identity.sessionId ?? null,
      event.identity.taskId,
      sessionStatus(event, current?.status ?? ""),
      event.eventId,
      event.aggregateSequence,
      event.intent,
      effectiveTransitions,
      nonEffectiveEvents,
      toolCallsStarted,
      toolCallsSettled,
      permissionPending,
      artifactCount,
      workerRoutes,
      failureCount,
      compactCount,
      canonicalJson(metadata),
      event.committedAt,
    );
  }

  private projectTool(db: DatabaseSync, event: RuntimeEventEnvelope): void {
    const callId = event.identity.toolCallId ?? scalar(event.inline, "tool_call_id") ?? scalar(event.inline, "call_id");
    if (!callId) {
      throw new ProjectorError(EventSpineErrorCode.TOOL_PAIR_VIOLATION, "tool event has no tool call identity", {
        event_id: event.eventId,
        event_type: event.eventType,
      });
    }
    const row = db.prepare("SELECT * FROM runtime_projected_tools WHERE aggregate_id = ? AND tool_call_id = ?").get(event.aggregateId, callId) as ToolRow | undefined;
    const toolName = scalar(event.inline, "tool_name") ?? row?.tool_name ?? "unknown";
    if (event.eventType === "runtime.tool.input.started") {
      if (row && !["input"].includes(row.status)) this.toolDivergence(event, row, "input start after call settlement");
      this.upsertTool(db, event, {
        callId,
        toolName,
        status: "input",
        calledEventId: row?.called_event_id ?? null,
        settledEventId: row?.settled_event_id ?? null,
        inputDigest: row?.input_digest ?? null,
        resultDigest: row?.result_digest ?? null,
        artifactRefs: row ? parseArtifacts(row.artifact_refs_json) : [],
        progressSequence: row?.progress_sequence ?? 0,
        providerExecuted: Boolean(row?.provider_executed),
        malformedResult: Boolean(row?.malformed_result),
        errorCode: row?.error_code ?? null,
      });
      return;
    }
    if (event.eventType === "runtime.tool.input.ended") {
      if (!row || row.status !== "input") this.toolViolation(event, "tool input ended without matching input start");
      this.upsertTool(db, event, {
        callId,
        toolName,
        status: "input",
        calledEventId: row.called_event_id,
        settledEventId: row.settled_event_id,
        inputDigest: scalar(event.inline, "input_digest") ?? row.input_digest,
        resultDigest: row.result_digest,
        artifactRefs: parseArtifacts(row.artifact_refs_json),
        progressSequence: row.progress_sequence,
        providerExecuted: Boolean(row.provider_executed),
        malformedResult: Boolean(row.malformed_result),
        errorCode: row.error_code,
      });
      return;
    }
    if (event.eventType === "runtime.tool.called") {
      if (row && ["called", "running", "succeeded", "failed", "cancelled"].includes(row.status)) {
        this.toolDivergence(event, row, "duplicate tool call with different event identity");
      }
      this.upsertTool(db, event, {
        callId,
        toolName,
        status: "called",
        calledEventId: event.eventId,
        settledEventId: null,
        inputDigest: scalar(event.inline, "input_digest") ?? row?.input_digest ?? null,
        resultDigest: null,
        artifactRefs: event.artifactRefs,
        progressSequence: 0,
        providerExecuted: booleanScalar(event.inline, "provider_executed"),
        malformedResult: false,
        errorCode: null,
      });
      return;
    }
    if (event.eventType === "runtime.tool.progress") {
      if (!row || !["called", "running"].includes(row.status)) this.toolViolation(event, "tool progress has no active call");
      this.upsertTool(db, event, {
        callId,
        toolName,
        status: "running",
        calledEventId: row.called_event_id,
        settledEventId: null,
        inputDigest: row.input_digest,
        resultDigest: row.result_digest,
        artifactRefs: [...parseArtifacts(row.artifact_refs_json), ...event.artifactRefs],
        progressSequence: row.progress_sequence + 1,
        providerExecuted: Boolean(row.provider_executed),
        malformedResult: Boolean(row.malformed_result),
        errorCode: null,
      });
      return;
    }
    if (["runtime.tool.succeeded", "runtime.tool.failed", "runtime.tool.cancelled"].includes(event.eventType)) {
      if (!row || !["called", "running"].includes(row.status)) this.toolViolation(event, "tool result has no unsettled call");
      const status = event.eventType === "runtime.tool.succeeded" ? "succeeded" : event.eventType === "runtime.tool.failed" ? "failed" : "cancelled";
      this.upsertTool(db, event, {
        callId,
        toolName,
        status,
        calledEventId: row.called_event_id,
        settledEventId: event.eventId,
        inputDigest: row.input_digest,
        resultDigest: scalar(event.inline, "result_digest") ?? digestJson({ artifacts: event.artifactRefs, delta: event.stateDelta }),
        artifactRefs: [...parseArtifacts(row.artifact_refs_json), ...event.artifactRefs],
        progressSequence: row.progress_sequence,
        providerExecuted: Boolean(row.provider_executed) || booleanScalar(event.inline, "provider_executed"),
        malformedResult: booleanScalar(event.inline, "malformed_result"),
        errorCode: scalar(event.inline, "error_code") ?? null,
      });
    }
  }

  private upsertTool(
    db: DatabaseSync,
    event: RuntimeEventEnvelope,
    value: {
      callId: string;
      toolName: string;
      status: ProjectedToolCall["status"];
      calledEventId: string | null;
      settledEventId: string | null;
      inputDigest: string | null;
      resultDigest: string | null;
      artifactRefs: readonly ArtifactPointer[];
      progressSequence: number;
      providerExecuted: boolean;
      malformedResult: boolean;
      errorCode: string | null;
    },
  ): void {
    const uniqueArtifacts = [...new Map(value.artifactRefs.map((item) => [item.artifactId, item])).values()];
    db.prepare(`
      INSERT INTO runtime_projected_tools (
        aggregate_id, tool_call_id, tool_name, status, called_event_id, settled_event_id,
        input_digest, result_digest, artifact_refs_json, progress_sequence,
        provider_executed, malformed_result, error_code, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(aggregate_id, tool_call_id) DO UPDATE SET
        tool_name = excluded.tool_name,
        status = excluded.status,
        called_event_id = excluded.called_event_id,
        settled_event_id = excluded.settled_event_id,
        input_digest = excluded.input_digest,
        result_digest = excluded.result_digest,
        artifact_refs_json = excluded.artifact_refs_json,
        progress_sequence = excluded.progress_sequence,
        provider_executed = excluded.provider_executed,
        malformed_result = excluded.malformed_result,
        error_code = excluded.error_code,
        updated_at = excluded.updated_at
    `).run(
      event.aggregateId,
      value.callId,
      value.toolName,
      value.status,
      value.calledEventId,
      value.settledEventId,
      value.inputDigest,
      value.resultDigest,
      canonicalJson(uniqueArtifacts),
      value.progressSequence,
      value.providerExecuted ? 1 : 0,
      value.malformedResult ? 1 : 0,
      value.errorCode,
      event.committedAt,
    );
  }

  private projectControl(db: DatabaseSync, event: RuntimeEventEnvelope): void {
    const controlId = controlIdentity(event);
    if (!controlId) return;
    const existing = db.prepare("SELECT * FROM runtime_projected_controls WHERE aggregate_id = ? AND control_id = ?").get(event.aggregateId, controlId) as ControlRow | undefined;
    const requested = event.eventType.endsWith(".requested") || event.eventType === "runtime.permission.pending";
    if (!requested && !existing) {
      throw new ProjectorError(EventSpineErrorCode.PROJECTOR_DIVERGENCE, "control resolution has no pending request", {
        aggregate_id: event.aggregateId,
        control_id: controlId,
        event_type: event.eventType,
      });
    }
    const status = requested
      ? event.eventType.endsWith(".pending") ? "pending" : "requested"
      : event.eventType.endsWith(".allowed") || event.eventType.endsWith(".accepted") ? "accepted"
      : event.eventType.endsWith(".denied") || event.eventType.endsWith(".rejected") ? "rejected"
      : event.eventType.endsWith(".completed") ? "completed"
      : "resolved";
    db.prepare(`
      INSERT INTO runtime_projected_controls (
        aggregate_id, control_id, control_kind, status, requested_event_id,
        resolved_event_id, decision, actor_id, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(aggregate_id, control_id) DO UPDATE SET
        control_kind = excluded.control_kind,
        status = excluded.status,
        resolved_event_id = excluded.resolved_event_id,
        decision = excluded.decision,
        actor_id = excluded.actor_id,
        updated_at = excluded.updated_at
    `).run(
      event.aggregateId,
      controlId,
      scalar(event.inline, "control_kind") ?? (event.eventType.startsWith("runtime.permission.") ? "permission" : "runtime_control"),
      status,
      requested ? event.eventId : existing!.requested_event_id,
      requested ? null : event.eventId,
      scalar(event.inline, "decision") ?? null,
      scalar(event.inline, "actor_id") ?? null,
      event.committedAt,
    );
  }

  private projectArtifacts(db: DatabaseSync, event: RuntimeEventEnvelope): void {
    for (const artifact of event.artifactRefs) {
      const existing = db.prepare("SELECT source_event_id, artifact_json FROM runtime_projected_artifacts WHERE aggregate_id = ? AND artifact_id = ?").get(event.aggregateId, artifact.artifactId) as { source_event_id: string; artifact_json: string } | undefined;
      if (existing && digestJson(JSON.parse(existing.artifact_json)) !== digestJson(artifact)) {
        throw new ProjectorError(EventSpineErrorCode.PROJECTOR_DIVERGENCE, "artifact ref changed after projection", {
          aggregate_id: event.aggregateId,
          artifact_id: artifact.artifactId,
          first_event_id: existing.source_event_id,
          event_id: event.eventId,
        });
      }
      db.prepare(`
        INSERT INTO runtime_projected_artifacts (
          aggregate_id, artifact_id, artifact_json, source_event_id, intent, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(aggregate_id, artifact_id) DO UPDATE SET
          updated_at = excluded.updated_at
      `).run(event.aggregateId, artifact.artifactId, canonicalJson(artifact), event.eventId, event.intent, event.committedAt);
    }
  }

  private projectStream(db: DatabaseSync, event: RuntimeEventEnvelope): void {
    if (event.eventType.endsWith(".delta")) {
      throw new ProjectorError(EventSpineErrorCode.STREAM_LIFECYCLE_VIOLATION, "live stream delta reached durable projector", {
        event_id: event.eventId,
        event_type: event.eventType,
      });
    }
    const streamId = streamIdentity(event);
    const existing = db.prepare("SELECT * FROM runtime_projected_streams WHERE aggregate_id = ? AND stream_id = ?").get(event.aggregateId, streamId) as StreamRow | undefined;
    const started = event.eventType.endsWith(".started");
    const ended = event.eventType.endsWith(".ended");
    if (started && existing) {
      throw new ProjectorError(EventSpineErrorCode.STREAM_LIFECYCLE_VIOLATION, "stream started twice", {
        aggregate_id: event.aggregateId,
        stream_id: streamId,
      });
    }
    if (ended && !existing) {
      throw new ProjectorError(EventSpineErrorCode.STREAM_LIFECYCLE_VIOLATION, "stream ended without start", {
        aggregate_id: event.aggregateId,
        stream_id: streamId,
      });
    }
    if (!started && !ended) return;
    const finalArtifact = event.artifactRefs[0];
    db.prepare(`
      INSERT INTO runtime_projected_streams (
        aggregate_id, stream_id, stream_kind, status, started_event_id,
        ended_event_id, final_artifact_id, final_digest, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(aggregate_id, stream_id) DO UPDATE SET
        status = excluded.status,
        ended_event_id = excluded.ended_event_id,
        final_artifact_id = excluded.final_artifact_id,
        final_digest = excluded.final_digest,
        updated_at = excluded.updated_at
    `).run(
      event.aggregateId,
      streamId,
      event.eventType.split(".")[1] ?? "stream",
      started ? "started" : "ended",
      started ? event.eventId : existing!.started_event_id,
      ended ? event.eventId : null,
      finalArtifact?.artifactId ?? null,
      finalArtifact?.digest ?? scalar(event.inline, "final_digest") ?? null,
      event.committedAt,
    );
  }

  private toolViolation(event: RuntimeEventEnvelope, message: string): never {
    throw new ProjectorError(EventSpineErrorCode.TOOL_PAIR_VIOLATION, message, {
      aggregate_id: event.aggregateId,
      event_id: event.eventId,
      event_type: event.eventType,
      tool_call_id: event.identity.toolCallId ?? "",
    });
  }

  private toolDivergence(event: RuntimeEventEnvelope, row: ToolRow, message: string): never {
    throw new ProjectorError(EventSpineErrorCode.TOOL_PAIR_VIOLATION, message, {
      aggregate_id: event.aggregateId,
      event_id: event.eventId,
      event_type: event.eventType,
      tool_call_id: row.tool_call_id,
      current_status: row.status,
      called_event_id: row.called_event_id,
      settled_event_id: row.settled_event_id,
    });
  }
}
