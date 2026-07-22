import { createHash, randomUUID } from "node:crypto";

import {
  asObject,
  asString,
  type CapabilitySupervisionIdentity,
  type JsonObject,
  type RuntimeRunInput,
  type ToolBatch,
  type ToolExecutionRequest,
  type ToolExecutionResponse,
} from "../contracts.ts";
import {
  ToolExecutionSupervisor,
  toolExecutionSupervisorContract,
  type SupplementaryObservationEmitter,
} from "./execution-supervisor.ts";
import {
  McpTransportSupervisor,
  mcpTransportSupervisorContract,
} from "./mcp-supervisor.ts";
import {
  ProviderStreamSupervisor,
  providerStreamSupervisorContract,
  type ProviderFailureInput,
  type ProviderResumeCursor,
} from "./provider-stream-supervisor.ts";
import type { WatchdogRefs } from "./runtime.ts";
import {
  WorkerRestartSupervisor,
  workerRestartSupervisorContract,
} from "./worker-restart-supervisor.ts";

interface BatchLease {
  toolCallId: string;
  generation: number;
  leaseToken: string;
}

interface BatchExecutionRecord {
  batchId: string;
  generation: number;
  deadlineMs: number;
  startedAtMs: number;
  toolCallIds: string[];
  leases: BatchLease[];
  timeout: NodeJS.Timeout | undefined;
  deadline: Promise<never>;
  rejectDeadline: (reason?: unknown) => void;
  settled: boolean;
}

function runtimeId(prefix: string): string {
  return prefix + "_" + randomUUID().replaceAll("-", "");
}

/**
 * Productized TypeScript supervision composition used by the stdio host.
 *
 * It keeps OMP-derived mechanisms in their source language and exposes one
 * ingress into ``RuntimeWatchdogObserver``. It owns no durable signal state;
 * emitted observations cross the JSONL protocol and are reclassified and
 * persisted by Python.
 */
export class CrossRuntimeFaultSupervisor {
  readonly toolExecution: ToolExecutionSupervisor;
  readonly providerStreams: ProviderStreamSupervisor;
  readonly mcpTransports: McpTransportSupervisor;
  readonly workerRestarts: WorkerRestartSupervisor;

  #input: RuntimeRunInput | null = null;
  #batches = new Map<string, BatchExecutionRecord>();
  #toolGeneration = new Map<string, number>();
  #streamGeneration = new Map<string, number>();
  #activeProviderStreams = new Set<string>();
  #providerFrameSequences = new Map<string, number>();
  #mcpGeneration = new Map<string, number>();
  #mcpBound = new Set<string>();
  #configuredCount = 0;
  #batchCount = 0;
  #batchDeadlineCount = 0;
  #batchSettlementCount = 0;
  #workerHeartbeatSequence = 0;

  constructor(emit: SupplementaryObservationEmitter, now: () => number = () => Date.now()) {
    this.toolExecution = new ToolExecutionSupervisor(emit, now);
    this.providerStreams = new ProviderStreamSupervisor(emit, now);
    this.mcpTransports = new McpTransportSupervisor(emit, now);
    this.workerRestarts = new WorkerRestartSupervisor(emit, now);
  }

  configure(input: RuntimeRunInput): void {
    if (this.#input !== null && this.#input.runId !== input.runId) {
      throw new Error("cross-runtime fault supervisor cannot move across runs");
    }
    this.#input = structuredClone(input);
    this.#configuredCount += 1;
    const refs = this.refs({ observationId: runtimeId("ts_worker_binding") });
    if (refs.workerId) {
      this.workerRestarts.attach({
        refs,
        generation: 0,
        pid: process.pid,
        heartbeatIntervalMs: this.#positiveMetadata(input.metadata, "worker_heartbeat_interval_ms", 5_000),
        heartbeatGraceIntervals: this.#positiveMetadata(input.metadata, "worker_heartbeat_grace_intervals", 3),
        maxRestarts: this.#positiveMetadata(input.metadata, "worker_max_restarts", 4),
        restartWindowMs: this.#positiveMetadata(input.metadata, "worker_restart_window_ms", 60_000),
        metadata: {
          process_owner: "typescript.JsonlRuntimeHost",
          canonical_lease_owner: "python.WorkerPoolStore",
        },
      });
    }
  }

  beginToolBatch(
    batch: ToolBatch,
    requests: ToolExecutionRequest[],
    deadlineMs: number,
  ): BatchExecutionRecord {
    this.#requireInput();
    if (this.#batches.has(batch.batchId)) throw new Error("tool batch already supervised");
    if (!Number.isSafeInteger(deadlineMs) || deadlineMs < 1) throw new Error("batch deadline must be positive");
    const batchGeneration = this.#batchCount + 1;
    const leases: BatchLease[] = [];
    for (const request of requests) {
      const generation = (this.#toolGeneration.get(request.toolCallId) ?? -1) + 1;
      this.#toolGeneration.set(request.toolCallId, generation);
      const armed = this.toolExecution.arm({
        refs: this.refs({
          observationId: runtimeId("ts_tool_binding"),
          toolCallId: request.toolCallId,
          toolName: request.toolName,
          sourceStateRevision: generation,
        }),
        generation,
        deadlineMs,
        batchId: batch.batchId,
        batchIndex: request.batchIndex,
        batchSize: request.batchSize,
        executionMode: request.executionMode,
        interruptible: request.executionMode === "concurrent_read_only",
        metadata: {
          turn_index: request.turnIndex,
          step_index: request.stepIndex,
          execution_owner: request.executionOwner ?? "python-tool-executor",
        },
      });
      this.toolExecution.start(request.toolCallId, generation);
      leases.push({ toolCallId: request.toolCallId, generation, leaseToken: armed.leaseToken });
    }
    const deferred = Promise.withResolvers<never>();
    const record: BatchExecutionRecord = {
      batchId: batch.batchId,
      generation: batchGeneration,
      deadlineMs,
      startedAtMs: Date.now(),
      toolCallIds: requests.map((value) => value.toolCallId),
      leases,
      timeout: undefined,
      deadline: deferred.promise,
      rejectDeadline: deferred.reject,
      settled: false,
    };
    record.timeout = setTimeout(() => {
      if (record.settled) return;
      this.#batchDeadlineCount += 1;
      const error = new Error("tool batch deadline elapsed");
      error.name = "ToolBatchDeadlineError";
      record.rejectDeadline(error);
    }, deadlineMs);
    this.#batches.set(batch.batchId, record);
    this.#batchCount += 1;
    return record;
  }

  async raceBatch<T>(batchId: string, operation: Promise<T>): Promise<T> {
    const record = this.#requireBatch(batchId);
    try {
      return await Promise.race([operation, record.deadline]);
    } catch (error) {
      if (!record.settled) {
        await Promise.all(record.leases.map((lease) => this.toolExecution.expire(
          lease.toolCallId,
          lease.generation,
          "tool_batch_deadline_elapsed",
        )));
      }
      throw error;
    }
  }

  settleToolBatch(
    batchId: string,
    requests: ToolExecutionRequest[],
    responses: ToolExecutionResponse[],
  ): void {
    const record = this.#requireBatch(batchId);
    if (record.settled) return;
    for (let index = 0; index < requests.length; index += 1) {
      const request = requests[index];
      const response = responses[index];
      const lease = record.leases.find((value) => value.toolCallId === request.toolCallId);
      if (lease === undefined) throw new Error("tool batch lease is missing");
      this.toolExecution.settle(
        request.toolCallId,
        lease.generation,
        asString(response?.metadata?.result_id) || runtimeId("tool_result"),
        response?.ok === true,
      );
    }
    record.settled = true;
    if (record.timeout !== undefined) {
      clearTimeout(record.timeout);
      record.timeout = undefined;
    }
    this.#batchSettlementCount += 1;
  }

  beginProviderStream(streamId: string, attemptNumber = 1, maxAttempts = 4): void {
    const generation = (this.#streamGeneration.get(streamId) ?? -1) + 1;
    this.#streamGeneration.set(streamId, generation);
    this.providerStreams.begin({
      refs: this.refs({
        observationId: runtimeId("ts_provider_binding"),
        sourceStateRevision: generation,
      }),
      streamId,
      generation,
      attemptNumber,
      maxAttempts,
      metadata: { runtime_owner: "typescript.ClaudeRuntimeCore" },
    });
  }

  async interruptProviderStream(
    streamId: string,
    input: ProviderFailureInput,
  ): Promise<ProviderResumeCursor | null> {
    const generation = this.#streamGeneration.get(streamId);
    if (generation === undefined) throw new Error("provider stream is not supervised");
    return await this.providerStreams.interrupt(streamId, generation, input);
  }

  async observeRuntimeEvent(event: JsonObject): Promise<void> {
    const phase = asString(event.phase);
    if (phase === "model_request_prepared") {
      const request = asObject(asObject(event.model_request_prepared).provider_request);
      const fallbackRequest = asObject(event.provider_request);
      const selected = Object.keys(request).length > 0 ? request : fallbackRequest;
      const streamId = asString(selected.request_id);
      if (!streamId || this.#activeProviderStreams.has(streamId)) return;
      const attemptNumber = this.#positiveIntegerFromRequestId(streamId, 1);
      const constraints = asObject(this.#requireInput().config.runtimeConstraints);
      const maxAttempts = this.#positiveMetadata(
        constraints,
        "api_retry_max_attempts",
        Math.max(1, attemptNumber),
      );
      this.beginProviderStream(streamId, attemptNumber, Math.max(attemptNumber, maxAttempts));
      this.#activeProviderStreams.add(streamId);
      this.#providerFrameSequences.set(streamId, 0);
      return;
    }
    if (phase === "model_stream_frame") {
      const frame = asObject(event.model_stream_frame);
      const streamId = asString(frame.request_id);
      if (!streamId || !this.#activeProviderStreams.has(streamId) || asString(frame.kind) !== "sse_chunk") return;
      const chunk = asObject(frame.chunk);
      const sequence = (this.#providerFrameSequences.get(streamId) ?? 0) + 1;
      this.#providerFrameSequences.set(streamId, sequence);
      const serialized = JSON.stringify(chunk);
      const generation = this.#streamGeneration.get(streamId);
      if (generation === undefined) throw new Error("provider stream generation is missing");
      this.providerStreams.acceptChunk(streamId, generation, {
        chunkId: streamId + ":chunk:" + String(sequence),
        sequence,
        contentDigest: createHash("sha256").update(serialized, "utf8").digest("hex"),
        textBytes: Buffer.byteLength(serialized, "utf8"),
        metadata: {
          frame_index: frame.frame_index ?? sequence,
          model: frame.model ?? "",
          transport: "http_sse",
        },
      });
      return;
    }
    if (phase !== "model_stream_report") return;
    const report = asObject(event.model_stream);
    const streamId = asString(report.request_id);
    if (!streamId || !this.#activeProviderStreams.has(streamId)) return;
    const generation = this.#streamGeneration.get(streamId);
    if (generation === undefined) throw new Error("provider stream generation is missing");
    this.#activeProviderStreams.delete(streamId);
    this.#providerFrameSequences.delete(streamId);
    if (report.ok === true) {
      const terminalDigest = createHash("sha256")
        .update(JSON.stringify(report), "utf8")
        .digest("hex");
      this.providerStreams.complete(streamId, generation, terminalDigest);
      return;
    }
    const statusCode = typeof report.status === "number" ? report.status : 0;
    const decision = asString(report.decision);
    const retryable = ["retry", "fallback", "reduce_output", "retry_fallback_model"].includes(decision)
      || [408, 429, 500, 502, 503, 504, 529].includes(statusCode);
    const recoveryPlan = asObject(report.recovery_plan);
    await this.interruptProviderStream(streamId, {
      errorCode: statusCode === 429
        ? "rate_limited"
        : statusCode === 408
          ? "timeout"
          : "provider_error",
      errorType: "ProviderStreamReportFailure",
      statusCode,
      retryable,
      terminal: !retryable,
      retryAfterMs: this.#nonnegativeInteger(
        recoveryPlan.delay_ms ?? recoveryPlan.delayMs,
      ),
      metadata: {
        request_id: streamId,
        model: report.model ?? "",
        transport: report.transport ?? "unknown",
        decision,
      },
    });
  }

  async superviseCapability<T>(
    request: ToolExecutionRequest,
    identity: CapabilitySupervisionIdentity,
    operation: (signal?: AbortSignal) => Promise<T>,
  ): Promise<T> {
    if (identity.namespace !== "mcp" || !identity.serverId) {
      return await operation(undefined);
    }
    const generation = this.#mcpGeneration.get(identity.serverId) ?? 0;
    this.#mcpGeneration.set(identity.serverId, generation);
    if (!this.#mcpBound.has(identity.serverId)) {
      this.mcpTransports.connect({
        refs: this.refs({
          observationId: runtimeId("ts_mcp_binding"),
          mcpServerId: identity.serverId,
          toolCallId: request.toolCallId,
          toolName: request.toolName,
          sourceStateRevision: generation,
        }),
        generation,
        metadata: {
          capability_owner: "typescript.McpRuntimeCoordinator",
          transport_owner: "typescript",
        },
      });
      this.#mcpBound.add(identity.serverId);
    }
    const timeoutMs = this.#positiveMetadata(
      request.metadata,
      "mcp_request_timeout_ms",
      30_000,
    );
    const requestId = request.toolCallId + ":mcp:" + generation;
    const runtime = this.#requireInput();
    const supervised = this.mcpTransports.beginRequest(
      identity.serverId,
      generation,
      {
        requestId,
        method: request.toolName,
        timeoutMs,
        idempotencyKey: [
          runtime.runId,
          runtime.taskId,
          request.toolCallId,
          identity.serverId,
        ].join(":"),
        sideEffecting: request.executionMode !== "concurrent_read_only",
        metadata: { schema_digest: identity.schemaDigest, version: identity.version },
      },
    );
    try {
      const result = await operation(supervised.signal);
      const responseDigest = createHash("sha256")
        .update(JSON.stringify(result), "utf8")
        .digest("hex");
      this.mcpTransports.settleRequest(
        identity.serverId,
        generation,
        requestId,
        responseDigest,
      );
      return result;
    } catch (error) {
      if (supervised.signal.aborted) {
        await this.mcpTransports.timeoutRequest(identity.serverId, generation, requestId);
      } else {
        const structured = asObject(error);
        const failure = asObject(structured.failure);
        const errorCode = asString(failure.code) || asString(structured.code) || "mcp_request_failed";
        this.mcpTransports.failRequest(identity.serverId, generation, requestId, errorCode);
        if (asString(failure.category) === "transport") {
          await this.mcpTransports.disconnected(identity.serverId, generation, errorCode);
        }
      }
      throw error;
    }
  }

  heartbeatWorker(sequence?: number, atMs?: number): boolean {
    const input = this.#requireInput();
    const workerId = asString(input.metadata?.worker_id);
    if (!workerId) return false;
    const selectedSequence = sequence ?? this.#workerHeartbeatSequence + 1;
    this.#workerHeartbeatSequence = Math.max(this.#workerHeartbeatSequence, selectedSequence);
    return this.workerRestarts.heartbeat(workerId, 0, selectedSequence, atMs);
  }

  async sweepWorkers(atMs?: number): Promise<string[]> {
    return await this.workerRestarts.sweep(atMs);
  }

  dispose(): void {
    for (const record of this.#batches.values()) {
      record.settled = true;
      if (record.timeout !== undefined) clearTimeout(record.timeout);
      record.timeout = undefined;
    }
    this.toolExecution.dispose();
  }

  refs(overrides: Partial<WatchdogRefs> = {}): WatchdogRefs {
    const input = this.#requireInput();
    const metadata = input.metadata ?? {};
    const runtimeConstraints = asObject(input.config.runtimeConstraints);
    return {
      runId: input.runId,
      taskId: input.taskId,
      observationId: overrides.observationId ?? runtimeId("ts_watchdog_observation"),
      sessionId: overrides.sessionId ?? input.sessionId,
      nodeId: overrides.nodeId ?? (input.nodeId ?? ""),
      attemptId: overrides.attemptId ?? input.workerRequestId,
      toolCallId: overrides.toolCallId ?? "",
      toolName: overrides.toolName ?? "",
      workerId: overrides.workerId ?? asString(metadata.worker_id),
      backendId: overrides.backendId ?? asString(metadata.backend_id),
      providerId: overrides.providerId
        || asString(metadata.provider_id)
        || asString(runtimeConstraints.provider_id)
        || input.config.modelName
        || "unknown_provider",
      workspaceId: overrides.workspaceId ?? asString(metadata.workspace_id),
      browserSessionId: overrides.browserSessionId ?? asString(metadata.browser_session_id),
      mcpServerId: overrides.mcpServerId ?? "",
      subagentTaskId: overrides.subagentTaskId ?? asString(metadata.subagent_task_id),
      sourceStateRevision: overrides.sourceStateRevision ?? 0,
    };
  }

  snapshot(): JsonObject {
    const batches: JsonObject = {};
    for (const [batchId, record] of [...this.#batches.entries()].sort(([left], [right]) => left.localeCompare(right))) {
      batches[batchId] = {
        generation: record.generation,
        deadline_ms: record.deadlineMs,
        started_at_ms: record.startedAtMs,
        tool_call_ids: [...record.toolCallIds],
        settled: record.settled,
      };
    }
    return {
      schema: "zyra.typescript-cross-runtime-fault-supervisor/v1",
      configured: this.#input !== null,
      configured_count: this.#configuredCount,
      batches,
      counts: {
        batches: this.#batchCount,
        batch_deadlines: this.#batchDeadlineCount,
        batch_settlements: this.#batchSettlementCount,
      },
      tool_execution: this.toolExecution.snapshot(),
      provider_streams: this.providerStreams.snapshot(),
      mcp_transports: this.mcpTransports.snapshot(),
      worker_restarts: this.workerRestarts.snapshot(),
      source_language: "typescript",
      source_repo: "oh-my-pi",
      canonical_signal_owner: "python.FaultStateStore",
      recovery_plan_owner: "M1-S07C",
    };
  }

  #requireInput(): RuntimeRunInput {
    if (this.#input === null) throw new Error("cross-runtime fault supervisor is not configured");
    return this.#input;
  }

  #requireBatch(batchId: string): BatchExecutionRecord {
    const record = this.#batches.get(batchId);
    if (record === undefined) throw new Error("tool batch is not supervised: " + batchId);
    return record;
  }

  #positiveMetadata(metadata: JsonObject | undefined, key: string, fallback: number): number {
    const value = metadata?.[key];
    return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : fallback;
  }

  #positiveIntegerFromRequestId(requestId: string, fallback: number): number {
    const suffix = Number(requestId.split(":").at(-1));
    return Number.isSafeInteger(suffix) && suffix > 0 ? suffix : fallback;
  }

  #nonnegativeInteger(value: unknown): number {
    const selected = Number(value);
    return Number.isSafeInteger(selected) && selected >= 0 ? selected : 0;
  }
}

export function crossRuntimeFaultSupervisorContract(): JsonObject {
  return {
    schema: "zyra.typescript-cross-runtime-fault-supervisor-contract/v1",
    source_language: "typescript",
    source_repo: "oh-my-pi",
    migration_mode: "cropped_same_language",
    tool_execution: toolExecutionSupervisorContract(),
    provider_stream: providerStreamSupervisorContract(),
    mcp_transport: mcpTransportSupervisorContract(),
    worker_restart: workerRestartSupervisorContract(),
    emits: "RuntimeEvent.phase=tool_failure_signal",
    python_ingress: "RuntimeEventObservationAdapter",
    canonical_signal_owner: "python.FaultStateStore",
    recovery_plan_owner: "M1-S07C",
  };
}
