import {
  ConsumerRole,
  DeliveryMode,
  RUNTIME_INTEGRATION_SCHEMA,
  SOURCE_BATCH_SCHEMA,
  SOURCE_RECORD_SCHEMA,
  SourceDomain,
  SourceRecordKind,
  canonicalIntegrationContract,
  parseSourceRecord,
  sourceRecordToJson,
  type SourceBatch,
  type SourceRecord,
} from "./integration-contracts.ts";
import {
  DurableConsumerRuntime,
  defaultConsumerSubscription,
  projectorConsumerHandler,
  type ConsumerRunReport,
  type DurableConsumerSpec,
} from "./consumer-runtime.ts";
import { RuntimeArtifactReadService, type ArtifactAuditResult } from "./artifact-read-service.ts";
import { OmpRpcFrameMapper, type OmpRpcFrameContext, type OmpRpcMappingResult } from "./omp-rpc-mapper.ts";
import { RuntimeDisableMatrix, RuntimeEventReconciler, type DisableObservation, type ReconciliationOptions } from "./reconciliation.ts";
import {
  type AppendReceipt,
  type RuntimeEventEnvelope,
  type SubscriptionSpec,
} from "./contracts.ts";
import { canonicalJson, cloneJson, digestJson, normalizeIdentifier, utcNow, type JsonValue } from "./canonical.ts";
import { EnvelopeValidationError, EventSpineError, EventSpineErrorCode } from "./errors.ts";
import type { RuntimeEventSpine } from "./runtime.ts";
import {
  DomainConsumerAuditor,
  domainConsumerHandler,
} from "./domain-consumers.ts";

export const INTEGRATION_RUNTIME_SCHEMA = "zyra.runtime-event-integration-runtime/v1";

export interface IntegrationRuntimeOptions {
  registerDefaultConsumers?: boolean;
  consumerOverrides?: Readonly<Record<string, Partial<SubscriptionSpec>>>;
}

export interface IntegrationAppendResult {
  schema: "zyra.runtime-integration-append/v1";
  sourceId: string;
  sourceKind: string;
  eventId?: string;
  eventType?: string;
  committed: boolean;
  liveOnly: boolean;
  duplicate: boolean;
  projected: boolean;
  deliveryIds: readonly string[];
  artifactIds: readonly string[];
  warnings: readonly string[];
  consumerRuns: readonly ConsumerRunReport[];
}

export interface IntegrationBatchResult {
  schema: "zyra.runtime-integration-batch/v1";
  batchId: string;
  expectedCount: number;
  admittedCount: number;
  durableCount: number;
  liveCount: number;
  duplicateCount: number;
  failedIndex?: number;
  results: readonly IntegrationAppendResult[];
  completedAt: string;
}

export interface RuntimeIngressContext {
  runId: string;
  sessionId?: string;
  taskId: string;
  nodeId?: string;
  workerId?: string;
  correlationId: string;
  producerSequence: number;
  ownerId: string;
  transactionId?: string;
  storeRef?: string;
  causationEventId?: string;
  requestId?: string;
  occurredAt?: string;
}

interface OutboxRow {
  source_id: string;
  source_kind: string;
  source_domain: string;
  source_json: string;
  state: string;
  attempt: number;
  event_id: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

function appendResult(source: SourceRecord, value: unknown): IntegrationAppendResult {
  if (Array.isArray(value)) {
    const deliveries = value.flatMap((item) => {
      if (typeof item !== "object" || item === null) return [];
      const subscriptionId = (item as { subscriptionId?: string }).subscriptionId;
      return subscriptionId ? [subscriptionId] : [];
    });
    return {
      schema: "zyra.runtime-integration-append/v1",
      sourceId: source.sourceId,
      sourceKind: source.kind,
      committed: false,
      liveOnly: true,
      duplicate: false,
      projected: false,
      deliveryIds: deliveries,
      artifactIds: [],
      warnings: [],
      consumerRuns: [],
    };
  }
  const receipt = value as AppendReceipt;
  return {
    schema: "zyra.runtime-integration-append/v1",
    sourceId: source.sourceId,
    sourceKind: source.kind,
    eventId: receipt.event.eventId,
    eventType: receipt.event.eventType,
    committed: receipt.committed,
    liveOnly: false,
    duplicate: receipt.duplicate,
    projected: receipt.projected,
    deliveryIds: receipt.deliveryIds,
    artifactIds: receipt.event.artifactRefs.map((item) => item.artifactId),
    warnings: receipt.warnings,
    consumerRuns: [],
  };
}

export class RuntimeEventIntegration {
  readonly spine: RuntimeEventSpine;
  readonly consumers: DurableConsumerRuntime;
  readonly artifacts: RuntimeArtifactReadService;
  readonly ompRpc: OmpRpcFrameMapper;
  readonly reconciler: RuntimeEventReconciler;
  readonly disableMatrix: RuntimeDisableMatrix;
  readonly domainConsumers: DomainConsumerAuditor;
  readonly options: Readonly<Required<Pick<IntegrationRuntimeOptions,
    "registerDefaultConsumers">>
    & Pick<IntegrationRuntimeOptions, "consumerOverrides">>;

  constructor(spine: RuntimeEventSpine, options: IntegrationRuntimeOptions = {}) {
    this.spine = spine;
    this.options = Object.freeze({
      registerDefaultConsumers: options.registerDefaultConsumers ?? true,
      consumerOverrides: options.consumerOverrides,
    });
    this.consumers = new DurableConsumerRuntime(spine);
    this.artifacts = new RuntimeArtifactReadService(spine);
    this.ompRpc = new OmpRpcFrameMapper(spine);
    this.reconciler = new RuntimeEventReconciler(spine);
    this.disableMatrix = new RuntimeDisableMatrix();
    this.domainConsumers = new DomainConsumerAuditor(this.consumers);
    this.ensureOutboxSchema();
    if (this.options.registerDefaultConsumers) this.registerDefaultConsumers();
  }

  private ensureOutboxSchema(): void {
    this.spine.store.db.exec(`
      CREATE TABLE IF NOT EXISTS runtime_event_source_outbox (
        source_id TEXT PRIMARY KEY,
        source_kind TEXT NOT NULL,
        source_domain TEXT NOT NULL,
        source_json TEXT NOT NULL,
        source_digest TEXT NOT NULL,
        state TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        event_id TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_runtime_event_source_outbox_state
        ON runtime_event_source_outbox(state, updated_at, source_id);

      CREATE TABLE IF NOT EXISTS runtime_event_source_receipts (
        source_id TEXT PRIMARY KEY,
        source_digest TEXT NOT NULL,
        event_id TEXT NOT NULL,
        event_digest TEXT NOT NULL,
        committed_at TEXT NOT NULL
      );
    `);
  }

  appendSource(value: SourceRecord | unknown): IntegrationAppendResult {
    const source = parseSourceRecord(value);
    this.stageSource(source);
    let result: IntegrationAppendResult;
    try {
      const appended = this.spine.appendSourceRecord(source);
      result = appendResult(source, appended);
      this.completeSource(source, result);
    } catch (error) {
      this.failSource(source, error);
      throw error;
    }
    return result;
  }

  appendBatch(value: SourceBatch | unknown): IntegrationBatchResult {
    const input = value as SourceBatch;
    if (input.schema !== SOURCE_BATCH_SCHEMA || !Array.isArray(input.records)) {
      throw new EnvelopeValidationError("integration batch must use the source batch contract");
    }
    const results: IntegrationAppendResult[] = [];
    let failedIndex: number | undefined;
    for (let index = 0; index < input.records.length; index += 1) {
      try {
        results.push(this.appendSource(input.records[index]));
      } catch (error) {
        failedIndex = index;
        if (input.atomic) {
          // Canonical EventStore commits are immutable.  Atomic here means
          // fail-stop admission for the remaining records, not rollback of
          // already committed facts.
          break;
        }
      }
    }
    return {
      schema: "zyra.runtime-integration-batch/v1",
      batchId: input.batchId,
      expectedCount: input.expectedCount,
      admittedCount: results.length,
      durableCount: results.filter((item) => !item.liveOnly).length,
      liveCount: results.filter((item) => item.liveOnly).length,
      duplicateCount: results.filter((item) => item.duplicate).length,
      failedIndex,
      results,
      completedAt: utcNow(),
    };
  }

  appendOmpRpcFrame(
    value: unknown,
    context: OmpRpcFrameContext,
  ): { mapping: OmpRpcMappingResult; results: readonly IntegrationAppendResult[] } {
    const mapping = this.ompRpc.map(value, context);
    const results = mapping.records.map((record) => this.appendSource(record));
    return { mapping, results };
  }

  appendRuntimeRecord(
    kind: keyof typeof SourceRecordKind | string,
    domain: keyof typeof SourceDomain | string,
    context: RuntimeIngressContext,
    payload: Readonly<Record<string, JsonValue>>,
    options: {
      summary?: string;
      effective?: boolean;
      deliveryMode?: typeof DeliveryMode[keyof typeof DeliveryMode];
      target?: SubscriptionSpec["recipient"];
      dependencyRecipients?: readonly string[];
      artifactRefs?: readonly JsonValue[];
      evidenceRefs?: readonly JsonValue[];
      subjectId?: string;
      metadata?: Readonly<Record<string, JsonValue>>;
    } = {},
  ): IntegrationAppendResult {
    const selectedKind = Object.values(SourceRecordKind).includes(kind as never)
      ? kind
      : SourceRecordKind[kind as keyof typeof SourceRecordKind];
    const selectedDomain = Object.values(SourceDomain).includes(domain as never)
      ? domain
      : SourceDomain[domain as keyof typeof SourceDomain];
    if (!selectedKind || !selectedDomain) {
      throw new EnvelopeValidationError("runtime ingress kind or domain is unsupported", {
        kind: String(kind),
        domain: String(domain),
      });
    }
    const id = `${selectedDomain}:${selectedKind}:${context.correlationId}:${context.producerSequence}:${digestJson(payload).slice(7, 23)}`;
    const source = parseSourceRecord({
      schema: SOURCE_RECORD_SCHEMA,
      sourceId: id,
      kind: selectedKind,
      domain: selectedDomain,
      occurredAt: context.occurredAt ?? utcNow(),
      identity: {
        runId: context.runId,
        sessionId: context.sessionId,
        taskId: context.taskId,
        nodeId: context.nodeId,
        workerId: context.workerId,
        toolCallId: typeof payload.tool_call_id === "string" ? payload.tool_call_id : undefined,
        commandId: typeof payload.control_id === "string" ? payload.control_id : undefined,
      },
      owner: {
        domain: selectedDomain,
        ownerId: context.ownerId,
        transactionId: context.transactionId,
        storeRef: context.storeRef,
        committed: options.deliveryMode !== DeliveryMode.LIVE_ONLY,
        committedAt: context.occurredAt ?? utcNow(),
      },
      causality: {
        correlationId: context.correlationId,
        causationEventId: context.causationEventId,
        requestId: context.requestId,
        producerSequence: context.producerSequence,
      },
      delivery: {
        mode: options.deliveryMode ?? DeliveryMode.TARGETED,
        target: options.target,
        dependencyRecipients: options.dependencyRecipients ?? [],
        topK: 4,
      },
      subjectId: options.subjectId ?? context.workerId ?? context.ownerId,
      summary: options.summary ?? `${selectedKind} from ${context.ownerId}`,
      effective: options.effective,
      payload,
      artifactRefs: options.artifactRefs ?? [],
      evidenceRefs: options.evidenceRefs ?? [],
      metadata: options.metadata ?? {},
    });
    return this.appendSource(source);
  }

  registerConsumer(spec: DurableConsumerSpec) {
    return this.consumers.register(spec);
  }

  registerDefaultConsumers(): void {
    const overrides = this.options.consumerOverrides ?? {};
    const register = (consumerId: string, role: typeof ConsumerRole[keyof typeof ConsumerRole], handler: DurableConsumerSpec["handler"]): void => {
      const subscription = defaultConsumerSubscription(consumerId, role, overrides[consumerId]);
      this.consumers.register({
        consumerId,
        subscription,
        role,
        handler,
        catchUpOnStart: true,
        pollBatchSize: 64,
        maxPollIterations: 1000,
        metadata: { integration_runtime: INTEGRATION_RUNTIME_SCHEMA },
      });
    };
    register("integration-api", ConsumerRole.API_PROJECTION, projectorConsumerHandler((id) => this.spine.projection(id)));
    register("integration-ui", ConsumerRole.UI_PROJECTION, projectorConsumerHandler((id) => this.spine.projection(id)));
    register("integration-worker", ConsumerRole.WORKER, domainConsumerHandler(ConsumerRole.WORKER));
    register("integration-control", ConsumerRole.CONTROL, domainConsumerHandler(ConsumerRole.CONTROL));
    register("integration-scheduler", ConsumerRole.SCHEDULER, domainConsumerHandler(ConsumerRole.SCHEDULER));
    register("integration-mcp", ConsumerRole.MCP, domainConsumerHandler(ConsumerRole.MCP));
    register("integration-memory", ConsumerRole.MEMORY, domainConsumerHandler(ConsumerRole.MEMORY));
    register("integration-artifact", ConsumerRole.ARTIFACT, domainConsumerHandler(ConsumerRole.ARTIFACT));
    register("integration-recovery", ConsumerRole.RECOVERY, domainConsumerHandler(ConsumerRole.RECOVERY));
    register("integration-auditor", ConsumerRole.AUDITOR, domainConsumerHandler(ConsumerRole.AUDITOR));
  }

  async pumpConsumers(maxMessages = 1000): Promise<readonly ConsumerRunReport[]> {
    const reports: ConsumerRunReport[] = [];
    for (const snapshot of this.consumers.snapshots()) {
      reports.push(await this.consumers.run(snapshot.consumerId, {
        maxMessages,
        catchUp: true,
        sweepExpired: true,
      }));
    }
    return reports;
  }

  reconcile(options: ReconciliationOptions = {}) {
    return this.reconciler.reconcile(options);
  }

  evaluateDisableMatrix(aggregateId: string, observations: readonly DisableObservation[]) {
    return this.disableMatrix.evaluate(aggregateId, observations);
  }

  auditArtifact(artifactId: string): ArtifactAuditResult {
    return this.artifacts.audit(artifactId);
  }

  outbox(limit = 1000): readonly Record<string, JsonValue>[] {
    const rows = this.spine.store.db.prepare(`
      SELECT * FROM runtime_event_source_outbox
      ORDER BY created_at, source_id LIMIT ?
    `).all(Math.max(1, Math.min(limit, 100_000))) as unknown as OutboxRow[];
    return rows.map((row) => ({
      source_id: row.source_id,
      source_kind: row.source_kind,
      source_domain: row.source_domain,
      state: row.state,
      attempt: row.attempt,
      event_id: row.event_id,
      last_error: row.last_error,
      created_at: row.created_at,
      updated_at: row.updated_at,
    }));
  }

  retryPending(limit = 1000): IntegrationAppendResult[] {
    const rows = this.spine.store.db.prepare(`
      SELECT * FROM runtime_event_source_outbox
      WHERE state IN ('pending', 'failed')
      ORDER BY updated_at, source_id LIMIT ?
    `).all(Math.max(1, Math.min(limit, 100_000))) as unknown as OutboxRow[];
    const results: IntegrationAppendResult[] = [];
    for (const row of rows) {
      results.push(this.appendSource(JSON.parse(row.source_json)));
    }
    return results;
  }

  contract(): Record<string, JsonValue> {
    return {
      schema: INTEGRATION_RUNTIME_SCHEMA,
      event_contract: canonicalIntegrationContract(),
      source_record_schema: SOURCE_RECORD_SCHEMA,
      source_batch_schema: SOURCE_BATCH_SCHEMA,
      canonical_owner: "RuntimeEventSqliteStore",
      message_bus_owner: "RuntimeMessageBus",
      projector_owner: "RuntimeStateProjector",
      source_outbox_owner: "RuntimeEventIntegration",
      domain_state_owners_preserved: true,
      python_state_owner: false,
      raw_rpc_forwarded: false,
      api_reads_projector_only: true,
      client_predictive_canonical_writes: false,
      consumer_count: this.consumers.snapshots().length,
      domain_consumers: this.domainConsumers.contract(),
      artifact: this.artifacts.contract(),
      disable_matrix: this.disableMatrix.contract(),
      downstream_contracts: ["M1-05D", "M1-07A", "M1-07C", "M2"],
    };
  }

  private stageSource(source: SourceRecord): void {
    const serializedSource = sourceRecordToJson(source);
    const serialized = canonicalJson(serializedSource);
    const digest = digestJson(serializedSource);
    const existing = this.spine.store.db.prepare(`
      SELECT source_digest, state FROM runtime_event_source_outbox WHERE source_id = ?
    `).get(source.sourceId) as { source_digest: string; state: string } | undefined;
    if (existing && existing.source_digest !== digest) {
      throw new EventSpineError({
        code: EventSpineErrorCode.IDEMPOTENCY_CONFLICT,
        message: "source outbox id was reused with different content",
        details: { source_id: source.sourceId },
      });
    }
    const now = utcNow();
    this.spine.store.db.prepare(`
      INSERT INTO runtime_event_source_outbox (
        source_id, source_kind, source_domain, source_json, source_digest,
        state, attempt, event_id, last_error, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, 'pending', 1, NULL, NULL, ?, ?)
      ON CONFLICT(source_id) DO UPDATE SET
        attempt = runtime_event_source_outbox.attempt + 1,
        state = CASE WHEN runtime_event_source_outbox.state = 'committed' THEN 'committed' ELSE 'pending' END,
        last_error = NULL,
        updated_at = excluded.updated_at
    `).run(source.sourceId, source.kind, source.domain, serialized, digest, now, now);
  }

  private completeSource(source: SourceRecord, result: IntegrationAppendResult): void {
    const now = utcNow();
    this.spine.store.db.prepare(`
      UPDATE runtime_event_source_outbox
      SET state = ?, event_id = ?, last_error = NULL, updated_at = ?
      WHERE source_id = ?
    `).run(result.liveOnly ? "live_delivered" : "committed", result.eventId ?? null, now, source.sourceId);
    if (!result.eventId) return;
    const event = this.spine.get(result.eventId);
    if (!event) throw new EventSpineError({ code: EventSpineErrorCode.DELIVERY_NOT_FOUND, message: "committed source receipt has no canonical event" });
    this.spine.store.db.prepare(`
      INSERT INTO runtime_event_source_receipts (
        source_id, source_digest, event_id, event_digest, committed_at
      ) VALUES (?, ?, ?, ?, ?)
      ON CONFLICT(source_id) DO UPDATE SET
        event_id = excluded.event_id,
        event_digest = excluded.event_digest,
        committed_at = excluded.committed_at
    `).run(source.sourceId, digestJson(sourceRecordToJson(source)), event.eventId, event.contentDigest, event.committedAt);
  }

  private failSource(source: SourceRecord, error: unknown): void {
    this.spine.store.db.prepare(`
      UPDATE runtime_event_source_outbox
      SET state = 'failed', last_error = ?, updated_at = ? WHERE source_id = ?
    `).run(error instanceof Error ? error.message.slice(0, 4000) : String(error).slice(0, 4000), utcNow(), source.sourceId);
  }
}
