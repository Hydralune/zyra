import {
  DeliveryMode,
  SOURCE_RECORD_SCHEMA,
  SourceDomain,
  SourceRecordKind,
  parseSourceRecord,
  type SourceRecord,
  type SourceRecordKindValue,
} from "./integration-contracts.ts";
import {
  canonicalJson,
  cloneJson,
  digestJson,
  isPlainObject,
  normalizeIdentifier,
  optionalInteger,
  optionalString,
  requireBoolean,
  requireInteger,
  requireRecord,
  requireString,
  utcNow,
  type JsonValue,
} from "./canonical.ts";
import { EnvelopeValidationError } from "./errors.ts";
import type { RuntimeEventSpine } from "./runtime.ts";

export const OMP_RPC_MAPPING_SCHEMA = "zyra.omp-rpc-event-mapping/v1";

export interface OmpRpcIdentity {
  runId: string;
  sessionId?: string;
  taskId: string;
  nodeId?: string;
  workerId: string;
  parentAgentId?: string;
}

export interface OmpRpcFrameContext {
  identity: OmpRpcIdentity;
  requestId: string;
  producerSequence: number;
  receivedAt?: string;
}

export interface OmpRpcMappingResult {
  schema: typeof OMP_RPC_MAPPING_SCHEMA;
  frameType: string;
  records: readonly SourceRecord[];
  liveRecordCount: number;
  durableRecordCount: number;
  warnings: readonly string[];
}

interface ToolCorrelationRow {
  rpc_id: string;
  request_id: string;
  tool_call_id: string;
  tool_name: string;
  run_id: string;
  task_id: string;
  subagent_id: string | null;
  called_source_id: string;
  called_event_id: string;
  state: string;
  started: number;
  terminal_source_id: string | null;
  created_at: string;
  updated_at: string;
}

interface SubagentCorrelationRow {
  subagent_id: string;
  request_id: string;
  run_id: string;
  task_id: string;
  parent_agent_id: string | null;
  created_source_id: string;
  created_event_id: string;
  terminal_source_id: string | null;
  state: string;
  last_progress_sequence: number;
  updated_at: string;
}

function frameType(input: Readonly<Record<string, unknown>>): string {
  return requireString(input.type ?? input.t ?? input.frame_type, "OMP RPC frame type", 128);
}

function payloadRecord(input: Readonly<Record<string, unknown>>): Readonly<Record<string, JsonValue>> {
  const payload = input.payload ?? input.data ?? {};
  const parsed = requireRecord(payload, "OMP RPC frame payload");
  return cloneJson(parsed);
}

function stringFrom(value: Readonly<Record<string, JsonValue>>, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const selected = value[key];
    if (typeof selected === "string" && selected.trim()) return selected.trim();
  }
  return undefined;
}

function boolFrom(value: Readonly<Record<string, JsonValue>>, key: string, fallback = false): boolean {
  return typeof value[key] === "boolean" ? value[key] as boolean : fallback;
}

function intFrom(value: Readonly<Record<string, JsonValue>>, key: string, fallback = 0): number {
  return typeof value[key] === "number" && Number.isInteger(value[key]) ? value[key] as number : fallback;
}

function objectFrom(value: Readonly<Record<string, JsonValue>>, key: string): Readonly<Record<string, JsonValue>> {
  return isPlainObject(value[key]) ? cloneJson(value[key] as Record<string, JsonValue>) : {};
}

function artifactsFrom(value: Readonly<Record<string, JsonValue>>): readonly JsonValue[] {
  const refs = value.artifact_refs ?? value.artifactRefs;
  return Array.isArray(refs) ? cloneJson(refs) : [];
}

function sourceId(prefix: string, requestId: string, sequence: number, payload: Readonly<Record<string, JsonValue>>): string {
  return `${prefix}:${requestId}:${sequence}:${digestJson(payload).slice("sha256:".length, "sha256:".length + 16)}`;
}

export class OmpRpcFrameMapper {
  readonly spine: RuntimeEventSpine;

  constructor(spine: RuntimeEventSpine) {
    this.spine = spine;
    this.ensureSchema();
  }

  private ensureSchema(): void {
    this.spine.store.db.exec(`
      CREATE TABLE IF NOT EXISTS runtime_omp_tool_correlations (
        rpc_id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL,
        tool_call_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        subagent_id TEXT,
        called_source_id TEXT NOT NULL,
        called_event_id TEXT NOT NULL,
        state TEXT NOT NULL,
        started INTEGER NOT NULL,
        terminal_source_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_runtime_omp_tool_call
        ON runtime_omp_tool_correlations(run_id, task_id, tool_call_id);

      CREATE TABLE IF NOT EXISTS runtime_omp_subagent_correlations (
        subagent_id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        parent_agent_id TEXT,
        created_source_id TEXT NOT NULL,
        created_event_id TEXT NOT NULL,
        terminal_source_id TEXT,
        state TEXT NOT NULL,
        last_progress_sequence INTEGER NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_runtime_omp_subagent_request
        ON runtime_omp_subagent_correlations(request_id, run_id, task_id);

      CREATE TABLE IF NOT EXISTS runtime_omp_frame_receipts (
        frame_digest TEXT PRIMARY KEY,
        frame_type TEXT NOT NULL,
        request_id TEXT NOT NULL,
        producer_sequence INTEGER NOT NULL,
        source_ids_json TEXT NOT NULL,
        created_at TEXT NOT NULL
      );
    `);
  }

  map(value: unknown, context: OmpRpcFrameContext): OmpRpcMappingResult {
    const input = requireRecord(value, "OMP RPC frame");
    const type = frameType(input);
    const payload = payloadRecord(input);
    const normalizedContext = this.parseContext(context);
    const digest = digestJson(JSON.parse(JSON.stringify({
      type,
      payload,
      context: normalizedContext,
    })) as JsonValue);
    const existing = this.spine.store.db.prepare(`
      SELECT source_ids_json FROM runtime_omp_frame_receipts WHERE frame_digest = ?
    `).get(digest) as { source_ids_json: string } | undefined;
    if (existing) {
      const ids = JSON.parse(existing.source_ids_json) as string[];
      const records = ids.map((id) => this.receiptRecord(id)).filter((item): item is SourceRecord => item !== undefined);
      return this.result(type, records, ["duplicate_omp_frame"]);
    }
    let records: readonly SourceRecord[];
    switch (type) {
      case "subagent_lifecycle":
      case "task:subagent:lifecycle":
        records = this.mapSubagentLifecycle(payload, normalizedContext);
        break;
      case "subagent_progress":
      case "task:subagent:progress":
        records = this.mapSubagentProgress(payload, normalizedContext);
        break;
      case "subagent_event":
      case "task:subagent:event":
        records = this.mapSubagentEvent(payload, normalizedContext);
        break;
      case "host_tool_call":
        records = this.mapHostToolCall(payload, normalizedContext);
        break;
      case "host_tool_update":
        records = this.mapHostToolUpdate(payload, normalizedContext);
        break;
      case "host_tool_result":
        records = this.mapHostToolResult(payload, normalizedContext);
        break;
      case "host_tool_cancel":
        records = this.mapHostToolCancel(payload, normalizedContext);
        break;
      case "agent_event":
        records = this.mapAgentEvent(payload, normalizedContext, undefined);
        break;
      default:
        throw new EnvelopeValidationError("unsupported OMP RPC frame type", { frame_type: type });
    }
    this.recordReceipt(digest, type, normalizedContext, records);
    return this.result(type, records, []);
  }

  private parseContext(context: OmpRpcFrameContext): OmpRpcFrameContext {
    const requestId = normalizeIdentifier(context.requestId, "OMP RPC requestId");
    const sequence = requireInteger(context.producerSequence, "OMP RPC producerSequence");
    if (sequence < 0) throw new EnvelopeValidationError("OMP RPC producerSequence must be non-negative", { sequence });
    return {
      identity: {
        runId: normalizeIdentifier(context.identity.runId, "OMP RPC runId"),
        sessionId: optionalString(context.identity.sessionId, "OMP RPC sessionId", 256),
        taskId: normalizeIdentifier(context.identity.taskId, "OMP RPC taskId"),
        nodeId: optionalString(context.identity.nodeId, "OMP RPC nodeId", 256),
        workerId: normalizeIdentifier(context.identity.workerId, "OMP RPC workerId"),
        parentAgentId: optionalString(context.identity.parentAgentId, "OMP RPC parentAgentId", 256),
      },
      requestId,
      producerSequence: sequence,
      receivedAt: context.receivedAt ?? utcNow(),
    };
  }

  private mapSubagentLifecycle(
    payload: Readonly<Record<string, JsonValue>>,
    context: OmpRpcFrameContext,
  ): readonly SourceRecord[] {
    const subagentId = stringFrom(payload, "subagent_id", "subagentId", "task_id", "taskId");
    if (!subagentId) throw new EnvelopeValidationError("subagent lifecycle frame is missing subagent id");
    const status = (stringFrom(payload, "status", "state", "phase") ?? "created").toLowerCase();
    const existing = this.subagentCorrelation(subagentId);
    let kind: SourceRecordKindValue;
    let cause: string | undefined;
    if (["created", "queued", "pending"].includes(status) && !existing) {
      kind = SourceRecordKind.SUBAGENT_CREATED;
    } else if (["dispatched", "running", "started"].includes(status)) {
      kind = SourceRecordKind.SUBAGENT_DISPATCHED;
      cause = existing?.created_event_id;
    } else if (["completed", "succeeded", "done"].includes(status)) {
      kind = existing?.state === "completed" || existing?.state === "failed" || existing?.state === "cancelled"
        ? SourceRecordKind.SUBAGENT_LATE_RESULT
        : SourceRecordKind.SUBAGENT_COMPLETED;
      cause = existing?.created_event_id;
    } else if (["failed", "error"].includes(status)) {
      kind = SourceRecordKind.SUBAGENT_FAILED;
      cause = existing?.created_event_id;
    } else if (["cancelled", "canceled", "aborted"].includes(status)) {
      kind = SourceRecordKind.SUBAGENT_CANCELLED;
      cause = existing?.created_event_id;
    } else {
      throw new EnvelopeValidationError("subagent lifecycle status is unsupported", { subagent_id: subagentId, status });
    }
    if (kind !== SourceRecordKind.SUBAGENT_CREATED && !cause) {
      throw new EnvelopeValidationError("subagent lifecycle terminal frame has no created correlation", {
        subagent_id: subagentId,
        status,
      });
    }
    const id = sourceId(`omp-subagent-${kind}`, context.requestId, context.producerSequence, payload);
    const record = this.sourceRecord({
      id,
      kind,
      domain: SourceDomain.SUBAGENT,
      context,
      causationEventId: cause,
      subjectId: subagentId,
      summary: `subagent ${subagentId} ${status}`,
      effective: true,
      payload: {
        subagent_id: subagentId,
        parent_agent_id: stringFrom(payload, "parent_agent_id", "parentAgentId") ?? context.identity.parentAgentId ?? context.identity.workerId,
        status,
        exit_code: typeof payload.exit_code === "number" ? payload.exit_code : null,
        error_code: stringFrom(payload, "error_code", "errorCode") ?? "",
        result_digest: stringFrom(payload, "result_digest", "resultDigest") ?? digestJson(payload),
      },
      artifactRefs: artifactsFrom(payload),
    });
    this.upsertSubagent(record, status);
    return [record];
  }

  private mapSubagentProgress(
    payload: Readonly<Record<string, JsonValue>>,
    context: OmpRpcFrameContext,
  ): readonly SourceRecord[] {
    const subagentId = stringFrom(payload, "subagent_id", "subagentId", "task_id", "taskId");
    if (!subagentId) throw new EnvelopeValidationError("subagent progress frame is missing subagent id");
    const correlation = this.subagentCorrelation(subagentId);
    if (!correlation) throw new EnvelopeValidationError("subagent progress arrived before lifecycle creation", { subagent_id: subagentId });
    const sequence = intFrom(payload, "sequence", intFrom(payload, "progress_sequence", context.producerSequence));
    if (sequence <= correlation.last_progress_sequence) {
      throw new EnvelopeValidationError("subagent progress sequence is not monotonic", {
        subagent_id: subagentId,
        previous: correlation.last_progress_sequence,
        actual: sequence,
      });
    }
    const id = sourceId("omp-subagent-progress", context.requestId, context.producerSequence, payload);
    const record = this.sourceRecord({
      id,
      kind: SourceRecordKind.SUBAGENT_PROGRESS,
      domain: SourceDomain.SUBAGENT,
      context,
      causationEventId: correlation.created_event_id,
      subjectId: subagentId,
      summary: `subagent ${subagentId} progress ${sequence}`,
      effective: false,
      payload: {
        subagent_id: subagentId,
        parent_agent_id: correlation.parent_agent_id ?? context.identity.workerId,
        progress_sequence: sequence,
        status: stringFrom(payload, "status", "phase") ?? "running",
        retry_state: stringFrom(payload, "retry_state", "retryState") ?? "",
        retry_failure_digest: digestJson(objectFrom(payload, "retryFailure")),
        token_count: intFrom(payload, "tokens", 0),
        context_tokens: intFrom(payload, "context_tokens", 0),
        cost_micros: intFrom(payload, "cost_micros", 0),
        progress_digest: digestJson(payload),
      },
      artifactRefs: artifactsFrom(payload),
      deliveryMode: DeliveryMode.TARGETED,
    });
    this.spine.store.db.prepare(`
      UPDATE runtime_omp_subagent_correlations
      SET last_progress_sequence = ?, updated_at = ? WHERE subagent_id = ?
    `).run(sequence, utcNow(), subagentId);
    return [record];
  }

  private mapSubagentEvent(
    payload: Readonly<Record<string, JsonValue>>,
    context: OmpRpcFrameContext,
  ): readonly SourceRecord[] {
    const subagentId = stringFrom(payload, "subagent_id", "subagentId", "task_id", "taskId");
    if (!subagentId) throw new EnvelopeValidationError("subagent event frame is missing subagent id");
    const nested = payload.event;
    if (!isPlainObject(nested)) throw new EnvelopeValidationError("subagent event frame is missing nested AgentEvent", { subagent_id: subagentId });
    const nestedContext: OmpRpcFrameContext = {
      ...context,
      identity: { ...context.identity, workerId: subagentId, parentAgentId: context.identity.workerId },
    };
    return this.mapAgentEvent(cloneJson(nested), nestedContext, subagentId);
  }

  private mapAgentEvent(
    event: Readonly<Record<string, JsonValue>>,
    context: OmpRpcFrameContext,
    subagentId: string | undefined,
  ): readonly SourceRecord[] {
    const type = stringFrom(event, "type");
    if (!type) throw new EnvelopeValidationError("OMP AgentEvent is missing type");
    const toolName = stringFrom(event, "toolName", "tool_name");
    const toolCallId = stringFrom(event, "toolCallId", "tool_call_id");
    const subagent = subagentId ? this.subagentCorrelation(subagentId) : undefined;
    if (type === "tool_execution_start") {
      if (!toolCallId || !toolName) throw new EnvelopeValidationError("tool start lacks tool identity", { type });
      const payload: Record<string, JsonValue> = {
        tool_name: toolName,
        tool_call_id: toolCallId,
        arguments: event.args ?? {},
        input_digest: digestJson((event.args ?? {}) as JsonValue),
        started: true,
        interruptible: boolFrom(event, "interruptible", false),
        subagent_id: subagentId ?? "",
      };
      const id = sourceId("omp-agent-tool-called", context.requestId, context.producerSequence, payload);
      const record = this.sourceRecord({
        id,
        kind: SourceRecordKind.TOOL_CALLED,
        domain: SourceDomain.CODEWORKER,
        context,
        subjectId: subagentId ?? context.identity.workerId,
        summary: `${subagentId ? "subagent" : "worker"} called ${toolName}`,
        effective: true,
        payload,
        toolCallId,
      });
      this.bindTool(`agent:${context.requestId}:${toolCallId}`, context, record, toolName, subagentId, true);
      return [record];
    }
    if (type === "tool_execution_update") {
      if (!toolCallId || !toolName) throw new EnvelopeValidationError("tool update lacks tool identity", { type });
      const binding = this.toolByCall(context, toolCallId);
      if (!binding) throw new EnvelopeValidationError("tool update arrived before tool start", { tool_call_id: toolCallId });
      const payload: Record<string, JsonValue> = {
        tool_name: toolName,
        tool_call_id: toolCallId,
        progress_sequence: context.producerSequence,
        partial_result: event.partialResult ?? event.partial_result ?? {},
      };
      return [this.sourceRecord({
        id: sourceId("omp-agent-tool-progress", context.requestId, context.producerSequence, payload),
        kind: SourceRecordKind.TOOL_PROGRESS,
        domain: SourceDomain.CODEWORKER,
        context,
        causationEventId: binding.called_event_id,
        subjectId: subagentId ?? context.identity.workerId,
        summary: `${toolName} progress`,
        effective: false,
        payload,
        toolCallId,
      })];
    }
    if (type === "tool_execution_end") {
      if (!toolCallId || !toolName) throw new EnvelopeValidationError("tool end lacks tool identity", { type });
      const binding = this.toolByCall(context, toolCallId);
      if (!binding) throw new EnvelopeValidationError("tool result arrived before tool start", { tool_call_id: toolCallId });
      if (toolName === "yield" && subagentId) {
        if (!subagent) throw new EnvelopeValidationError("subagent yield has no lifecycle correlation", { subagent_id: subagentId });
        const result = isPlainObject(event.result) ? cloneJson(event.result as Record<string, JsonValue>) : { value: event.result ?? null };
        const record = this.sourceRecord({
          id: sourceId("omp-subagent-yield", context.requestId, context.producerSequence, result),
          kind: SourceRecordKind.SUBAGENT_YIELD,
          domain: SourceDomain.SUBAGENT,
          context,
          causationEventId: subagent.created_event_id,
          subjectId: subagentId,
          summary: `subagent ${subagentId} yielded typed result`,
          effective: true,
          payload: {
            subagent_id: subagentId,
            parent_agent_id: subagent.parent_agent_id ?? context.identity.parentAgentId ?? "root",
            yield_kind: stringFrom(result, "type", "status") ?? "result",
            status: stringFrom(result, "status") ?? "success",
            result_digest: digestJson(result),
            use_last_turn: boolFrom(result, "useLastTurn", false),
            schema_overridden: boolFrom(result, "schemaOverridden", false),
            aborted: boolFrom(result, "aborted", false),
            output: result.data ?? result,
            session_file: result.sessionFile ?? null,
          },
          artifactRefs: artifactsFrom(result),
        });
        this.finishTool(binding, record.sourceId, "succeeded");
        return [record];
      }
      const isError = event.isError === true || event.is_error === true;
      const resultPayload: Record<string, JsonValue> = {
        tool_name: toolName,
        tool_call_id: toolCallId,
        result: event.result ?? null,
        result_digest: digestJson((event.result ?? null) as JsonValue),
        error_code: isError ? stringFrom(event, "errorCode", "error_code") ?? "tool_error" : "",
      };
      const record = this.sourceRecord({
        id: sourceId(isError ? "omp-agent-tool-failed" : "omp-agent-tool-succeeded", context.requestId, context.producerSequence, resultPayload),
        kind: isError ? SourceRecordKind.TOOL_FAILED : SourceRecordKind.TOOL_SUCCEEDED,
        domain: SourceDomain.CODEWORKER,
        context,
        causationEventId: binding.called_event_id,
        subjectId: subagentId ?? context.identity.workerId,
        summary: `${toolName} ${isError ? "failed" : "succeeded"}`,
        effective: true,
        payload: resultPayload,
        artifactRefs: artifactsFrom(event),
        toolCallId,
      });
      this.finishTool(binding, record.sourceId, isError ? "failed" : "succeeded");
      return [record];
    }
    if (type === "message_start") {
      return [this.agentTextRecord(SourceRecordKind.TEXT_STARTED, event, context, subagentId, false)];
    }
    if (type === "message_update") {
      const kind = stringFrom(event, "contentType", "content_type") === "reasoning"
        ? SourceRecordKind.REASONING_DELTA
        : SourceRecordKind.TEXT_DELTA;
      return [this.agentTextRecord(kind, event, context, subagentId, true)];
    }
    if (type === "message_end") {
      return [this.agentTextRecord(SourceRecordKind.TEXT_ENDED, event, context, subagentId, false)];
    }
    if (type === "agent_start") {
      return [this.agentTextRecord(SourceRecordKind.TURN_STARTED, event, context, subagentId, false)];
    }
    if (type === "agent_end") {
      return [this.agentTextRecord(SourceRecordKind.TURN_COMPLETED, event, context, subagentId, false)];
    }
    throw new EnvelopeValidationError("unsupported nested OMP AgentEvent", { event_type: type });
  }

  private agentTextRecord(
    kind: SourceRecordKindValue,
    event: Readonly<Record<string, JsonValue>>,
    context: OmpRpcFrameContext,
    subagentId: string | undefined,
    live: boolean,
  ): SourceRecord {
    const parent = stringFrom(event, "parentEventId", "parent_event_id");
    const payload: Record<string, JsonValue> = {
      stream_id: stringFrom(event, "messageId", "message_id") ?? `${context.requestId}:message`,
      segment_index: intFrom(event, "segmentIndex", context.producerSequence),
      delta_bytes: typeof event.delta === "string" ? Buffer.byteLength(event.delta, "utf8") : 0,
      content_digest: digestJson(event),
      content: event.delta ?? event.message ?? null,
      status: kind,
      phase: "omp-agent-event",
      subagent_id: subagentId ?? "",
    };
    return this.sourceRecord({
      id: sourceId(`omp-agent-${kind}`, context.requestId, context.producerSequence, payload),
      kind,
      domain: SourceDomain.CODEWORKER,
      context,
      causationEventId: parent,
      subjectId: subagentId ?? context.identity.workerId,
      summary: `${kind} from ${subagentId ?? context.identity.workerId}`,
      effective: !live,
      payload,
      deliveryMode: live ? DeliveryMode.LIVE_ONLY : DeliveryMode.TARGETED,
    });
  }

  private mapHostToolCall(payload: Readonly<Record<string, JsonValue>>, context: OmpRpcFrameContext): readonly SourceRecord[] {
    const rpcId = stringFrom(payload, "id", "rpc_id", "call_id");
    const toolCallId = stringFrom(payload, "tool_call_id", "toolCallId") ?? rpcId;
    const toolName = stringFrom(payload, "tool_name", "toolName", "name");
    if (!rpcId || !toolCallId || !toolName) throw new EnvelopeValidationError("host tool call lacks rpc/tool identity");
    const sourcePayload: Record<string, JsonValue> = {
      tool_name: toolName,
      tool_call_id: toolCallId,
      arguments: payload.arguments ?? payload.args ?? {},
      input_digest: digestJson((payload.arguments ?? payload.args ?? {}) as JsonValue),
      interruptible: boolFrom(payload, "interruptible", false),
      started: true,
    };
    const record = this.sourceRecord({
      id: sourceId("omp-host-tool-called", context.requestId, context.producerSequence, sourcePayload),
      kind: SourceRecordKind.TOOL_CALLED,
      domain: SourceDomain.CODEWORKER,
      context,
      subjectId: context.identity.workerId,
      summary: `host tool ${toolName} called`,
      effective: true,
      payload: sourcePayload,
      toolCallId,
    });
    this.bindTool(rpcId, context, record, toolName, undefined, true);
    return [record];
  }

  private mapHostToolUpdate(payload: Readonly<Record<string, JsonValue>>, context: OmpRpcFrameContext): readonly SourceRecord[] {
    const binding = this.requireToolBinding(payload);
    const sourcePayload: Record<string, JsonValue> = {
      tool_name: binding.tool_name,
      tool_call_id: binding.tool_call_id,
      progress_sequence: context.producerSequence,
      partial_result: payload.partial_result ?? payload.partialResult ?? payload.update ?? null,
      progress_digest: digestJson(payload),
    };
    return [this.sourceRecord({
      id: sourceId("omp-host-tool-progress", context.requestId, context.producerSequence, sourcePayload),
      kind: SourceRecordKind.TOOL_PROGRESS,
      domain: SourceDomain.CODEWORKER,
      context,
      causationEventId: binding.called_event_id,
      subjectId: context.identity.workerId,
      summary: `host tool ${binding.tool_name} progress`,
      effective: false,
      payload: sourcePayload,
      toolCallId: binding.tool_call_id,
    })];
  }

  private mapHostToolResult(payload: Readonly<Record<string, JsonValue>>, context: OmpRpcFrameContext): readonly SourceRecord[] {
    const binding = this.requireToolBinding(payload);
    if (binding.state !== "running") {
      throw new EnvelopeValidationError("host tool result arrived after terminal state", {
        rpc_id: binding.rpc_id,
        state: binding.state,
      });
    }
    const isError = boolFrom(payload, "is_error", false) || boolFrom(payload, "isError", false);
    const sourcePayload: Record<string, JsonValue> = {
      tool_name: binding.tool_name,
      tool_call_id: binding.tool_call_id,
      result: payload.result ?? payload.output ?? null,
      result_digest: digestJson((payload.result ?? payload.output ?? null) as JsonValue),
      error_code: isError ? stringFrom(payload, "error_code", "errorCode") ?? "host_tool_error" : "",
    };
    const record = this.sourceRecord({
      id: sourceId(isError ? "omp-host-tool-failed" : "omp-host-tool-succeeded", context.requestId, context.producerSequence, sourcePayload),
      kind: isError ? SourceRecordKind.TOOL_FAILED : SourceRecordKind.TOOL_SUCCEEDED,
      domain: SourceDomain.CODEWORKER,
      context,
      causationEventId: binding.called_event_id,
      subjectId: context.identity.workerId,
      summary: `host tool ${binding.tool_name} ${isError ? "failed" : "succeeded"}`,
      effective: true,
      payload: sourcePayload,
      artifactRefs: artifactsFrom(payload),
      toolCallId: binding.tool_call_id,
    });
    this.finishTool(binding, record.sourceId, isError ? "failed" : "succeeded");
    return [record];
  }

  private mapHostToolCancel(payload: Readonly<Record<string, JsonValue>>, context: OmpRpcFrameContext): readonly SourceRecord[] {
    const binding = this.requireToolBinding(payload);
    const started = boolFrom(payload, "started", Boolean(binding.started));
    const cancelPhase = stringFrom(payload, "phase", "cancel_phase") ?? (started ? "in_flight" : "queued");
    if (started || cancelPhase !== "queued") {
      // OMP preserves real results for a completed tool and does not fabricate a
      // cancelled terminal for running/non-interruptible work.  Record a
      // recovery request instead of violating the tool pair.
      const sourcePayload: Record<string, JsonValue> = {
        recovery_id: `tool-interrupt:${binding.tool_call_id}`,
        fault_id: binding.rpc_id,
        strategy: "await_real_tool_result",
        attempt: 0,
        status: "interrupt_deferred",
        tool_call_id: binding.tool_call_id,
      };
      return [this.sourceRecord({
        id: sourceId("omp-host-tool-interrupt-deferred", context.requestId, context.producerSequence, sourcePayload),
        kind: SourceRecordKind.RECOVERY_REQUESTED,
        domain: SourceDomain.RECOVERY,
        context,
        subjectId: "runtime-recovery",
        summary: `interrupt deferred for running tool ${binding.tool_name}`,
        effective: true,
        payload: sourcePayload,
      })];
    }
    const sourcePayload: Record<string, JsonValue> = {
      tool_name: binding.tool_name,
      tool_call_id: binding.tool_call_id,
      cancel_reason: stringFrom(payload, "reason", "cancel_reason") ?? "interrupt_before_start",
      started: false,
    };
    const record = this.sourceRecord({
      id: sourceId("omp-host-tool-cancelled", context.requestId, context.producerSequence, sourcePayload),
      kind: SourceRecordKind.TOOL_CANCELLED,
      domain: SourceDomain.CODEWORKER,
      context,
      causationEventId: binding.called_event_id,
      subjectId: context.identity.workerId,
      summary: `queued host tool ${binding.tool_name} cancelled`,
      effective: true,
      payload: sourcePayload,
      toolCallId: binding.tool_call_id,
    });
    this.finishTool(binding, record.sourceId, "cancelled");
    return [record];
  }

  private sourceRecord(options: {
    id: string;
    kind: SourceRecordKindValue;
    domain: SourceRecord["domain"];
    context: OmpRpcFrameContext;
    causationEventId?: string;
    subjectId: string;
    summary: string;
    effective: boolean;
    payload: Readonly<Record<string, JsonValue>>;
    artifactRefs?: readonly JsonValue[];
    deliveryMode?: typeof DeliveryMode[keyof typeof DeliveryMode];
    toolCallId?: string;
  }): SourceRecord {
    return parseSourceRecord({
      schema: SOURCE_RECORD_SCHEMA,
      sourceId: options.id,
      kind: options.kind,
      domain: options.domain,
      occurredAt: options.context.receivedAt,
      identity: {
        runId: options.context.identity.runId,
        sessionId: options.context.identity.sessionId,
        taskId: options.context.identity.taskId,
        nodeId: options.context.identity.nodeId,
        workerId: options.context.identity.workerId,
        toolCallId: options.toolCallId,
      },
      owner: {
        domain: options.domain,
        ownerId: options.domain === SourceDomain.CODEWORKER
          ? "CodeWorkerRuntime"
          : options.domain === SourceDomain.SUBAGENT
            ? "SubagentRuntime"
            : "RecoveryRuntime",
        transactionId: `omp-rpc:${options.context.requestId}:${options.context.producerSequence}`,
        storeRef: `runtime://omp/${options.context.identity.runId}/${options.context.identity.taskId}`,
        committed: options.deliveryMode !== DeliveryMode.LIVE_ONLY,
        committedAt: options.context.receivedAt,
      },
      causality: {
        correlationId: options.context.requestId,
        causationEventId: options.causationEventId,
        requestId: options.context.requestId,
        producerSequence: options.context.producerSequence,
      },
      delivery: {
        mode: options.deliveryMode ?? DeliveryMode.TARGETED,
        dependencyRecipients: [],
        topK: 4,
      },
      subjectId: options.subjectId,
      summary: options.summary,
      effective: options.effective,
      payload: options.payload,
      evidenceRefs: [],
      artifactRefs: options.artifactRefs ?? [],
      metadata: {
        omp_rpc_mapping_schema: OMP_RPC_MAPPING_SCHEMA,
        raw_rpc_frame_forwarded: false,
      },
    });
  }

  private bindTool(
    rpcId: string,
    context: OmpRpcFrameContext,
    record: SourceRecord,
    toolName: string,
    subagentId: string | undefined,
    started: boolean,
  ): void {
    const toolCallId = record.identity.toolCallId!;
    const now = utcNow();
    const existing = this.toolCorrelation(rpcId);
    if (existing) {
      if (existing.tool_call_id !== toolCallId || existing.called_source_id !== record.sourceId) {
        throw new EnvelopeValidationError("OMP rpc id was rebound to a different tool", {
          rpc_id: rpcId,
          expected_tool_call_id: existing.tool_call_id,
          actual_tool_call_id: toolCallId,
        });
      }
      return;
    }
    this.spine.store.db.prepare(`
      INSERT INTO runtime_omp_tool_correlations (
        rpc_id, request_id, tool_call_id, tool_name, run_id, task_id,
        subagent_id, called_source_id, called_event_id, state, started,
        terminal_source_id, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, NULL, ?, ?)
    `).run(
      rpcId,
      context.requestId,
      toolCallId,
      toolName,
      context.identity.runId,
      context.identity.taskId,
      subagentId ?? null,
      record.sourceId,
      `evt:${record.sourceId}`,
      started ? 1 : 0,
      now,
      now,
    );
  }

  private finishTool(binding: ToolCorrelationRow, terminalSourceId: string, state: string): void {
    this.spine.store.db.prepare(`
      UPDATE runtime_omp_tool_correlations
      SET state = ?, terminal_source_id = ?, updated_at = ?
      WHERE rpc_id = ? AND state = 'running'
    `).run(state, terminalSourceId, utcNow(), binding.rpc_id);
  }

  private upsertSubagent(record: SourceRecord, status: string): void {
    const subagentId = stringFrom(record.payload, "subagent_id")!;
    const existing = this.subagentCorrelation(subagentId);
    const createdSourceId = existing?.created_source_id ?? record.sourceId;
    const createdEventId = existing?.created_event_id ?? `evt:${record.sourceId}`;
    const terminal = ["completed", "succeeded", "done", "failed", "error", "cancelled", "canceled", "aborted"].includes(status);
    this.spine.store.db.prepare(`
      INSERT INTO runtime_omp_subagent_correlations (
        subagent_id, request_id, run_id, task_id, parent_agent_id,
        created_source_id, created_event_id, terminal_source_id, state,
        last_progress_sequence, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(subagent_id) DO UPDATE SET
        terminal_source_id = excluded.terminal_source_id,
        state = excluded.state,
        updated_at = excluded.updated_at
    `).run(
      subagentId,
      record.causality.requestId ?? record.causality.correlationId,
      record.identity.runId,
      record.identity.taskId,
      stringFrom(record.payload, "parent_agent_id") ?? null,
      createdSourceId,
      createdEventId,
      terminal ? record.sourceId : existing?.terminal_source_id ?? null,
      status,
      existing?.last_progress_sequence ?? -1,
      utcNow(),
    );
  }

  private requireToolBinding(payload: Readonly<Record<string, JsonValue>>): ToolCorrelationRow {
    const rpcId = stringFrom(payload, "id", "rpc_id", "call_id");
    if (!rpcId) throw new EnvelopeValidationError("host tool frame is missing rpc id");
    const binding = this.toolCorrelation(rpcId);
    if (!binding) throw new EnvelopeValidationError("host tool frame has no persisted call correlation", { rpc_id: rpcId });
    return binding;
  }

  private toolCorrelation(rpcId: string): ToolCorrelationRow | undefined {
    return this.spine.store.db.prepare(`
      SELECT * FROM runtime_omp_tool_correlations WHERE rpc_id = ?
    `).get(rpcId) as ToolCorrelationRow | undefined;
  }

  private toolByCall(context: OmpRpcFrameContext, toolCallId: string): ToolCorrelationRow | undefined {
    return this.spine.store.db.prepare(`
      SELECT * FROM runtime_omp_tool_correlations
      WHERE run_id = ? AND task_id = ? AND tool_call_id = ?
      ORDER BY created_at DESC LIMIT 1
    `).get(context.identity.runId, context.identity.taskId, toolCallId) as ToolCorrelationRow | undefined;
  }

  private subagentCorrelation(subagentId: string): SubagentCorrelationRow | undefined {
    return this.spine.store.db.prepare(`
      SELECT * FROM runtime_omp_subagent_correlations WHERE subagent_id = ?
    `).get(subagentId) as SubagentCorrelationRow | undefined;
  }

  private recordReceipt(
    digest: string,
    type: string,
    context: OmpRpcFrameContext,
    records: readonly SourceRecord[],
  ): void {
    this.spine.store.db.prepare(`
      INSERT INTO runtime_omp_frame_receipts (
        frame_digest, frame_type, request_id, producer_sequence,
        source_ids_json, created_at
      ) VALUES (?, ?, ?, ?, ?, ?)
    `).run(
      digest,
      type,
      context.requestId,
      context.producerSequence,
      canonicalJson(records.map((record) => record.sourceId)),
      utcNow(),
    );
  }

  private receiptRecord(sourceIdValue: string): SourceRecord | undefined {
    const event = this.spine.get(`evt:${sourceIdValue}`);
    if (!event) return undefined;
    const domain = stringFrom(event.metadata, "source_domain") as SourceRecord["domain"] | undefined;
    const kind = stringFrom(event.metadata, "source_kind") as SourceRecordKindValue | undefined;
    if (!domain || !kind) return undefined;
    return parseSourceRecord({
      schema: SOURCE_RECORD_SCHEMA,
      sourceId: sourceIdValue,
      kind,
      domain,
      occurredAt: event.createdAt,
      identity: event.identity,
      owner: {
        domain,
        ownerId: stringFrom(event.metadata, "source_owner") ?? event.sender.id,
        committed: true,
        committedAt: event.committedAt,
      },
      causality: {
        correlationId: event.correlationId,
        causationEventId: event.causationId,
        requestId: stringFrom(event.metadata, "request_id"),
        producerSequence: event.producerSequence,
      },
      delivery: { mode: DeliveryMode.STORE_ONLY, dependencyRecipients: [] },
      subjectId: event.sender.id,
      summary: event.summary,
      effective: event.effect === "effective",
      payload: event.inline,
      evidenceRefs: event.evidenceRefs,
      artifactRefs: event.artifactRefs,
      metadata: { reconstructed_from_canonical_event: true },
    });
  }

  private result(type: string, records: readonly SourceRecord[], warnings: readonly string[]): OmpRpcMappingResult {
    return {
      schema: OMP_RPC_MAPPING_SCHEMA,
      frameType: type,
      records,
      liveRecordCount: records.filter((record) => record.delivery.mode === DeliveryMode.LIVE_ONLY).length,
      durableRecordCount: records.filter((record) => record.delivery.mode !== DeliveryMode.LIVE_ONLY).length,
      warnings,
    };
  }
}
