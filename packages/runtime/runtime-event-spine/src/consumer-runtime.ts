import {
  BackpressureMode,
  DeliveryState,
  RecipientKind,
  type AgentMessageEnvelope,
  type LeasedDelivery,
  type RuntimeEventEnvelope,
  type SubscriptionSpec,
} from "./contracts.ts";
import {
  CONSUMER_CHECKPOINT_SCHEMA,
  ConsumerOutcome,
  ConsumerRole,
  checkpointToJson,
  emptyConsumerCheckpoint,
  parseConsumerCheckpoint,
  type ConsumerCheckpoint,
  type ConsumerOutcomeValue,
  type ConsumerResult,
  type ConsumerRoleValue,
} from "./integration-contracts.ts";
import { canonicalJson, cloneJson, digestJson, normalizeIdentifier, utcNow, type JsonValue } from "./canonical.ts";
import { DeliveryError, EventSpineErrorCode, EventSpineError, RoutingError } from "./errors.ts";
import type { RuntimeEventSpine } from "./runtime.ts";

export interface ConsumerContext {
  consumerId: string;
  subscriptionId: string;
  role: ConsumerRoleValue;
  attempt: number;
  delivery: LeasedDelivery["delivery"];
  checkpoint: ConsumerCheckpoint;
  now: string;
}

export interface ConsumerDisposition {
  outcome: "ack" | "retry" | "dead_letter" | "skip";
  retryDelayMs?: number;
  errorCode?: string;
  errorMessage?: string;
  stateMutation?: Readonly<Record<string, JsonValue>>;
}

export type ConsumerHandler = (
  event: RuntimeEventEnvelope,
  message: AgentMessageEnvelope,
  context: ConsumerContext,
) => ConsumerDisposition | Promise<ConsumerDisposition>;

export interface DurableConsumerSpec {
  consumerId: string;
  subscription: SubscriptionSpec;
  role: ConsumerRoleValue;
  handler: ConsumerHandler;
  enabled?: boolean;
  pollBatchSize?: number;
  maxPollIterations?: number;
  retryBaseMs?: number;
  retryMaxMs?: number;
  catchUpOnStart?: boolean;
  catchUpLimit?: number;
  stopOnError?: boolean;
  metadata?: Readonly<Record<string, JsonValue>>;
}

export interface ConsumerRunOptions {
  maxMessages?: number;
  maxIterations?: number;
  catchUp?: boolean;
  sweepExpired?: boolean;
  now?: string;
}

export interface ConsumerRunReport {
  schema: "zyra.runtime-consumer-run/v1";
  consumerId: string;
  subscriptionId: string;
  role: ConsumerRoleValue;
  generation: number;
  startedAt: string;
  completedAt: string;
  caughtUp: number;
  swept: number;
  leased: number;
  acknowledged: number;
  retried: number;
  deadLettered: number;
  duplicates: number;
  skipped: number;
  stalled: boolean;
  highWatermark: number;
  checkpoint: ConsumerCheckpoint;
  results: readonly ConsumerResult[];
}

export interface ConsumerSnapshot {
  schema: "zyra.runtime-consumer-snapshot/v1";
  consumerId: string;
  subscriptionId: string;
  role: ConsumerRoleValue;
  enabled: boolean;
  running: boolean;
  generation: number;
  checkpoint: ConsumerCheckpoint;
  pendingCount: number;
  processedCount: number;
  failedCount: number;
  metadata: Readonly<Record<string, JsonValue>>;
}

interface ProcessedRow {
  consumer_id: string;
  event_id: string;
  event_digest: string;
  delivery_id: string;
  global_sequence: number;
  outcome: string;
  attempt: number;
  processed_at: string;
  mutation_json: string | null;
}

interface CheckpointRow {
  consumer_id: string;
  subscription_id: string;
  role: string;
  checkpoint_json: string;
  generation: number;
  updated_at: string;
}

function recipientForRole(role: ConsumerRoleValue): SubscriptionSpec["recipient"] {
  switch (role) {
    case ConsumerRole.API_PROJECTION:
      return { kind: RecipientKind.API, id: "api-projection", requiredCapabilities: ["api.project"] };
    case ConsumerRole.UI_PROJECTION:
      return { kind: RecipientKind.UI_PROJECTOR, id: "ui-projector", requiredCapabilities: ["ui.project"] };
    case ConsumerRole.WORKER:
      return { kind: RecipientKind.WORKER, id: "codeworker", requiredCapabilities: ["worker.execute"] };
    case ConsumerRole.SCHEDULER:
      return { kind: RecipientKind.SCHEDULER, id: "scheduler", requiredCapabilities: ["scheduler.route"] };
    case ConsumerRole.CONTROL:
      return { kind: RecipientKind.CONTROL, id: "permission-control", requiredCapabilities: ["control.decide"] };
    case ConsumerRole.MCP:
      return { kind: RecipientKind.MCP, id: "mcp-runtime", requiredCapabilities: ["mcp.invoke"] };
    case ConsumerRole.MEMORY:
      return { kind: RecipientKind.MEMORY, id: "memory-fabric", requiredCapabilities: ["memory.write"] };
    case ConsumerRole.ARTIFACT:
      return { kind: RecipientKind.ARTIFACT_STORE, id: "artifact-store", requiredCapabilities: ["artifact.commit"] };
    case ConsumerRole.RECOVERY:
      return { kind: RecipientKind.RECOVERY, id: "recovery-planner", requiredCapabilities: ["recovery.plan"] };
    case ConsumerRole.AUDITOR:
      return { kind: RecipientKind.AUDITOR, id: "runtime-auditor", requiredCapabilities: ["audit.observe"] };
  }
}

export function defaultConsumerSubscription(
  consumerId: string,
  role: ConsumerRoleValue,
  options: Partial<SubscriptionSpec> = {},
): SubscriptionSpec {
  const recipient = options.recipient ?? recipientForRole(role);
  return {
    subscriptionId: options.subscriptionId ?? `sub:${normalizeIdentifier(consumerId, "consumerId")}`,
    recipient,
    eventTypes: options.eventTypes ?? ["runtime.*"],
    intents: options.intents ?? [],
    aggregatePrefixes: options.aggregatePrefixes ?? [],
    taskIds: options.taskIds ?? [],
    capabilityRefs: options.capabilityRefs ?? recipient.requiredCapabilities,
    capacity: options.capacity ?? 2048,
    maxAttempts: options.maxAttempts ?? 5,
    ackTimeoutMs: options.ackTimeoutMs ?? 30_000,
    backpressureMode: options.backpressureMode ?? BackpressureMode.DEAD_LETTER_OLDEST,
    enabled: options.enabled ?? true,
    priority: options.priority ?? 100,
    metadata: {
      consumer_id: consumerId,
      consumer_role: role,
      durable_checkpoint: true,
      ...(options.metadata ?? {}),
    },
  };
}

function retryDelay(spec: DurableConsumerSpec, attempt: number): number {
  const base = Math.max(1, spec.retryBaseMs ?? 100);
  const ceiling = Math.max(base, spec.retryMaxMs ?? 30_000);
  return Math.min(ceiling, base * (2 ** Math.max(0, attempt - 1)));
}

function elapsedMs(startedAt: string, completedAt: string): number {
  return Math.max(0, new Date(completedAt).getTime() - new Date(startedAt).getTime());
}

export class DurableConsumerRuntime {
  readonly spine: RuntimeEventSpine;
  private readonly specs = new Map<string, DurableConsumerSpec>();
  private readonly running = new Set<string>();

  constructor(spine: RuntimeEventSpine) {
    this.spine = spine;
    this.ensureSchema();
  }

  private ensureSchema(): void {
    this.spine.store.db.exec(`
      CREATE TABLE IF NOT EXISTS runtime_event_consumer_checkpoints (
        consumer_id TEXT PRIMARY KEY,
        subscription_id TEXT NOT NULL,
        role TEXT NOT NULL,
        checkpoint_json TEXT NOT NULL,
        generation INTEGER NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_event_consumer_subscription
        ON runtime_event_consumer_checkpoints(subscription_id);

      CREATE TABLE IF NOT EXISTS runtime_event_consumer_processed (
        consumer_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        event_digest TEXT NOT NULL,
        delivery_id TEXT NOT NULL,
        global_sequence INTEGER NOT NULL,
        outcome TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        processed_at TEXT NOT NULL,
        mutation_json TEXT,
        PRIMARY KEY (consumer_id, event_id),
        UNIQUE (consumer_id, delivery_id)
      );
      CREATE INDEX IF NOT EXISTS idx_runtime_event_consumer_processed_cursor
        ON runtime_event_consumer_processed(consumer_id, global_sequence);

      CREATE TABLE IF NOT EXISTS runtime_event_consumer_failures (
        failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
        consumer_id TEXT NOT NULL,
        delivery_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        error_code TEXT NOT NULL,
        error_message TEXT NOT NULL,
        retry_delay_ms INTEGER NOT NULL,
        created_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_runtime_event_consumer_failures_event
        ON runtime_event_consumer_failures(consumer_id, event_id, attempt);

      CREATE TABLE IF NOT EXISTS runtime_event_consumer_mutations (
        consumer_id TEXT NOT NULL,
        domain TEXT NOT NULL,
        state_key TEXT NOT NULL,
        event_id TEXT NOT NULL,
        global_sequence INTEGER NOT NULL,
        state_json TEXT NOT NULL,
        state_digest TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (consumer_id, domain, state_key)
      );
    `);
  }

  register(value: DurableConsumerSpec): ConsumerSnapshot {
    const consumerId = normalizeIdentifier(value.consumerId, "consumerId");
    if (value.subscription.subscriptionId.trim() === "") {
      throw new DeliveryError(EventSpineErrorCode.SUBSCRIBER_NOT_FOUND, "consumer subscription id is empty");
    }
    const spec: DurableConsumerSpec = {
      ...value,
      consumerId,
      enabled: value.enabled ?? true,
      pollBatchSize: Math.max(1, Math.min(value.pollBatchSize ?? 32, 1000)),
      maxPollIterations: Math.max(1, Math.min(value.maxPollIterations ?? 100, 10_000)),
      retryBaseMs: Math.max(1, value.retryBaseMs ?? 100),
      retryMaxMs: Math.max(1, value.retryMaxMs ?? 30_000),
      catchUpOnStart: value.catchUpOnStart ?? true,
      catchUpLimit: Math.max(1, Math.min(value.catchUpLimit ?? 1000, 1000)),
      stopOnError: value.stopOnError ?? false,
      metadata: cloneJson(value.metadata ?? {}),
    };
    const existing = this.specs.get(consumerId);
    if (existing && existing.subscription.subscriptionId !== spec.subscription.subscriptionId) {
      throw new DeliveryError(EventSpineErrorCode.SUBSCRIBER_NOT_FOUND, "consumer id is already bound to another subscription", {
        consumer_id: consumerId,
        existing_subscription_id: existing.subscription.subscriptionId,
        requested_subscription_id: spec.subscription.subscriptionId,
      });
    }
    this.spine.registerSubscription(spec.subscription);
    this.specs.set(consumerId, spec);
    const current = this.loadCheckpoint(consumerId);
    const checkpoint = current ?? emptyConsumerCheckpoint(consumerId, spec.subscription.subscriptionId, spec.role);
    this.saveCheckpoint(checkpoint);
    return this.snapshot(consumerId);
  }

  unregister(consumerId: string, force = false): void {
    const spec = this.requireSpec(consumerId);
    if (this.running.has(spec.consumerId)) {
      throw new RoutingError(EventSpineErrorCode.SUBSCRIBER_BACKPRESSURE, "cannot unregister a running consumer", {
        consumer_id: spec.consumerId,
      });
    }
    this.spine.bus.unregister(spec.subscription.subscriptionId, force);
    this.specs.delete(spec.consumerId);
  }

  checkpoint(consumerId: string): ConsumerCheckpoint {
    const spec = this.requireSpec(consumerId);
    return this.loadCheckpoint(spec.consumerId)
      ?? emptyConsumerCheckpoint(spec.consumerId, spec.subscription.subscriptionId, spec.role);
  }

  restart(consumerId: string): ConsumerCheckpoint {
    const spec = this.requireSpec(consumerId);
    if (this.running.has(spec.consumerId)) {
      throw new RoutingError(EventSpineErrorCode.SUBSCRIBER_BACKPRESSURE, "cannot restart a running consumer", {
        consumer_id: spec.consumerId,
      });
    }
    const previous = this.checkpoint(spec.consumerId);
    const restarted: ConsumerCheckpoint = {
      ...previous,
      generation: previous.generation + 1,
      startedAt: utcNow(),
      updatedAt: utcNow(),
    };
    this.saveCheckpoint(restarted);
    this.spine.bus.sweep();
    if (spec.catchUpOnStart) {
      this.spine.bus.catchUp(spec.subscription.subscriptionId, {
        afterSequence: restarted.lastGlobalSequence,
        limit: spec.catchUpLimit,
      });
    }
    return restarted;
  }

  async run(consumerId: string, options: ConsumerRunOptions = {}): Promise<ConsumerRunReport> {
    const spec = this.requireSpec(consumerId);
    if (spec.enabled === false) {
      throw new DeliveryError(EventSpineErrorCode.SUBSCRIBER_NOT_FOUND, "consumer is disabled", { consumer_id: spec.consumerId });
    }
    if (this.running.has(spec.consumerId)) {
      throw new RoutingError(EventSpineErrorCode.SUBSCRIBER_BACKPRESSURE, "consumer is already running", { consumer_id: spec.consumerId });
    }
    this.running.add(spec.consumerId);
    const startedAt = options.now ?? utcNow();
    const maxMessages = Math.max(1, Math.min(options.maxMessages ?? 1000, 100_000));
    const maxIterations = Math.max(1, Math.min(options.maxIterations ?? spec.maxPollIterations ?? 100, 10_000));
    let checkpoint = this.checkpoint(spec.consumerId);
    let caughtUp = 0;
    let swept = 0;
    let leased = 0;
    let acknowledged = 0;
    let retried = 0;
    let deadLettered = 0;
    let duplicates = 0;
    let skipped = 0;
    let stalled = false;
    const results: ConsumerResult[] = [];
    try {
      if (options.sweepExpired ?? true) swept = this.spine.bus.sweep(startedAt);
      if (options.catchUp ?? spec.catchUpOnStart ?? true) {
        caughtUp = this.spine.bus.catchUp(spec.subscription.subscriptionId, {
          afterSequence: checkpoint.lastGlobalSequence,
          limit: spec.catchUpLimit,
        });
      }
      for (let iteration = 0; iteration < maxIterations && leased < maxMessages; iteration += 1) {
        const batchLimit = Math.min(spec.pollBatchSize ?? 32, maxMessages - leased);
        const batch = this.spine.poll(spec.subscription.subscriptionId, batchLimit);
        if (batch.length === 0) {
          stalled = this.spine.store.pendingCount(spec.subscription.subscriptionId) > 0;
          break;
        }
        leased += batch.length;
        for (const item of batch) {
          const result = await this.processDelivery(spec, item, checkpoint);
          results.push(result);
          checkpoint = result.checkpoint;
          if (result.outcome === ConsumerOutcome.ACKNOWLEDGED) acknowledged += 1;
          else if (result.outcome === ConsumerOutcome.RETRY) retried += 1;
          else if (result.outcome === ConsumerOutcome.DEAD_LETTER) deadLettered += 1;
          else if (result.outcome === ConsumerOutcome.DUPLICATE) duplicates += 1;
          else if (result.outcome === ConsumerOutcome.SKIPPED) skipped += 1;
          if (result.outcome === ConsumerOutcome.RETRY && spec.stopOnError) {
            iteration = maxIterations;
            break;
          }
        }
      }
    } finally {
      this.running.delete(spec.consumerId);
    }
    const completedAt = utcNow();
    return {
      schema: "zyra.runtime-consumer-run/v1",
      consumerId: spec.consumerId,
      subscriptionId: spec.subscription.subscriptionId,
      role: spec.role,
      generation: checkpoint.generation,
      startedAt,
      completedAt,
      caughtUp,
      swept,
      leased,
      acknowledged,
      retried,
      deadLettered,
      duplicates,
      skipped,
      stalled,
      highWatermark: this.spine.store.latestGlobalSequence(),
      checkpoint,
      results,
    };
  }

  snapshot(consumerId: string): ConsumerSnapshot {
    const spec = this.requireSpec(consumerId);
    const checkpoint = this.checkpoint(spec.consumerId);
    const processed = this.spine.store.db.prepare(`
      SELECT COUNT(*) AS count FROM runtime_event_consumer_processed WHERE consumer_id = ?
    `).get(spec.consumerId) as { count: number };
    const failed = this.spine.store.db.prepare(`
      SELECT COUNT(*) AS count FROM runtime_event_consumer_failures WHERE consumer_id = ?
    `).get(spec.consumerId) as { count: number };
    return {
      schema: "zyra.runtime-consumer-snapshot/v1",
      consumerId: spec.consumerId,
      subscriptionId: spec.subscription.subscriptionId,
      role: spec.role,
      enabled: spec.enabled ?? true,
      running: this.running.has(spec.consumerId),
      generation: checkpoint.generation,
      checkpoint,
      pendingCount: this.spine.store.pendingCount(spec.subscription.subscriptionId),
      processedCount: Number(processed.count),
      failedCount: Number(failed.count),
      metadata: cloneJson(spec.metadata ?? {}),
    };
  }

  snapshots(): readonly ConsumerSnapshot[] {
    return [...this.specs.keys()].sort().map((consumerId) => this.snapshot(consumerId));
  }

  processedEvents(consumerId: string, limit = 1000): readonly Record<string, JsonValue>[] {
    const spec = this.requireSpec(consumerId);
    const rows = this.spine.store.db.prepare(`
      SELECT * FROM runtime_event_consumer_processed
      WHERE consumer_id = ?
      ORDER BY global_sequence ASC
      LIMIT ?
    `).all(spec.consumerId, Math.max(1, Math.min(limit, 100_000))) as unknown as ProcessedRow[];
    return rows.map((row) => ({
      consumer_id: row.consumer_id,
      event_id: row.event_id,
      event_digest: row.event_digest,
      delivery_id: row.delivery_id,
      global_sequence: row.global_sequence,
      outcome: row.outcome,
      attempt: row.attempt,
      processed_at: row.processed_at,
      mutation: row.mutation_json ? JSON.parse(row.mutation_json) : null,
    }));
  }

  readMutation(consumerId: string, domain: string, stateKey: string): Readonly<Record<string, JsonValue>> | undefined {
    this.requireSpec(consumerId);
    const row = this.spine.store.db.prepare(`
      SELECT state_json FROM runtime_event_consumer_mutations
      WHERE consumer_id = ? AND domain = ? AND state_key = ?
    `).get(consumerId, domain, stateKey) as { state_json: string } | undefined;
    return row ? JSON.parse(row.state_json) : undefined;
  }

  private async processDelivery(
    spec: DurableConsumerSpec,
    item: LeasedDelivery,
    checkpoint: ConsumerCheckpoint,
  ): Promise<ConsumerResult> {
    const startedAt = utcNow();
    const event = this.spine.get(item.delivery.eventId);
    if (!event) {
      throw new DeliveryError(EventSpineErrorCode.DELIVERY_NOT_FOUND, "consumer leased a delivery with no canonical event", {
        consumer_id: spec.consumerId,
        delivery_id: item.delivery.deliveryId,
        event_id: item.delivery.eventId,
      });
    }
    const existing = this.processedRow(spec.consumerId, event.eventId);
    if (existing) {
      if (existing.event_digest !== event.contentDigest) {
        throw new EventSpineError({
          code: EventSpineErrorCode.IDEMPOTENCY_CONFLICT,
          message: "consumer observed an event id with a different digest",
          details: {
            consumer_id: spec.consumerId,
            event_id: event.eventId,
            expected_digest: existing.event_digest,
            actual_digest: event.contentDigest,
          },
        });
      }
      this.spine.acknowledge(item.delivery.deliveryId, item.delivery.leaseToken!);
      const duplicateCheckpoint = this.advanceCheckpoint(checkpoint, event, item.delivery.deliveryId, ConsumerOutcome.DUPLICATE);
      this.saveCheckpoint(duplicateCheckpoint);
      return this.result(spec, item, event, ConsumerOutcome.DUPLICATE, startedAt, duplicateCheckpoint);
    }
    let disposition: ConsumerDisposition;
    try {
      disposition = await spec.handler(event, item.message, {
        consumerId: spec.consumerId,
        subscriptionId: spec.subscription.subscriptionId,
        role: spec.role,
        attempt: item.delivery.attempt,
        delivery: item.delivery,
        checkpoint,
        now: startedAt,
      });
    } catch (error) {
      disposition = {
        outcome: "retry",
        errorCode: error instanceof EventSpineError ? error.code : "consumer_handler_failed",
        errorMessage: error instanceof Error ? error.message : String(error),
      };
    }
    if (disposition.outcome === "retry") {
      const delay = disposition.retryDelayMs ?? retryDelay(spec, item.delivery.attempt);
      this.recordFailure(spec.consumerId, item, disposition, delay);
      const updated = this.spine.negativeAcknowledge(
        item.delivery.deliveryId,
        item.delivery.leaseToken!,
        disposition.errorMessage ?? disposition.errorCode ?? "consumer_retry",
        delay,
      );
      const outcome = updated.state === DeliveryState.DEAD_LETTER
        ? ConsumerOutcome.DEAD_LETTER
        : ConsumerOutcome.RETRY;
      const retryCheckpoint = this.countCheckpoint(checkpoint, outcome);
      this.saveCheckpoint(retryCheckpoint);
      return this.result(spec, item, event, outcome, startedAt, retryCheckpoint, disposition);
    }
    if (disposition.outcome === "dead_letter") {
      this.recordFailure(spec.consumerId, item, disposition, 0);
      let updated = this.spine.negativeAcknowledge(
        item.delivery.deliveryId,
        item.delivery.leaseToken!,
        disposition.errorMessage ?? disposition.errorCode ?? "consumer_dead_letter",
        0,
      );
      while (updated.state !== DeliveryState.DEAD_LETTER && updated.leaseToken) {
        updated = this.spine.negativeAcknowledge(
          updated.deliveryId,
          updated.leaseToken,
          disposition.errorMessage ?? "consumer_dead_letter",
          0,
        );
      }
      const deadCheckpoint = this.countCheckpoint(checkpoint, ConsumerOutcome.DEAD_LETTER);
      this.saveCheckpoint(deadCheckpoint);
      return this.result(spec, item, event, ConsumerOutcome.DEAD_LETTER, startedAt, deadCheckpoint, disposition);
    }
    const outcome: ConsumerOutcomeValue = disposition.outcome === "skip"
      ? ConsumerOutcome.SKIPPED
      : ConsumerOutcome.ACKNOWLEDGED;
    const next = this.advanceCheckpoint(checkpoint, event, item.delivery.deliveryId, outcome);
    this.spine.store.db.exec("BEGIN IMMEDIATE");
    try {
      this.recordProcessed(spec.consumerId, item, event, outcome, disposition.stateMutation);
      if (disposition.stateMutation) this.applyMutation(spec.consumerId, event, disposition.stateMutation);
      this.saveCheckpoint(next);
      this.spine.store.db.exec("COMMIT");
    } catch (error) {
      try { this.spine.store.db.exec("ROLLBACK"); } catch { /* preserve source error */ }
      throw error;
    }
    this.spine.acknowledge(item.delivery.deliveryId, item.delivery.leaseToken!);
    return this.result(spec, item, event, outcome, startedAt, next, disposition);
  }

  private advanceCheckpoint(
    previous: ConsumerCheckpoint,
    event: RuntimeEventEnvelope,
    deliveryId: string,
    outcome: ConsumerOutcomeValue,
  ): ConsumerCheckpoint {
    if (event.globalSequence < previous.lastGlobalSequence) {
      return this.countCheckpoint(previous, ConsumerOutcome.DUPLICATE);
    }
    const counted = this.countCheckpoint(previous, outcome);
    return {
      ...counted,
      lastGlobalSequence: Math.max(previous.lastGlobalSequence, event.globalSequence),
      lastEventId: event.eventId,
      lastDeliveryId: deliveryId,
      lastEventDigest: event.contentDigest,
      updatedAt: utcNow(),
    };
  }

  private countCheckpoint(previous: ConsumerCheckpoint, outcome: ConsumerOutcomeValue): ConsumerCheckpoint {
    return {
      ...previous,
      handledCount: previous.handledCount + (outcome === ConsumerOutcome.ACKNOWLEDGED || outcome === ConsumerOutcome.SKIPPED ? 1 : 0),
      duplicateCount: previous.duplicateCount + (outcome === ConsumerOutcome.DUPLICATE ? 1 : 0),
      retryCount: previous.retryCount + (outcome === ConsumerOutcome.RETRY ? 1 : 0),
      deadLetterCount: previous.deadLetterCount + (outcome === ConsumerOutcome.DEAD_LETTER ? 1 : 0),
      updatedAt: utcNow(),
    };
  }

  private result(
    spec: DurableConsumerSpec,
    item: LeasedDelivery,
    event: RuntimeEventEnvelope,
    outcome: ConsumerOutcomeValue,
    startedAt: string,
    checkpoint: ConsumerCheckpoint,
    disposition: ConsumerDisposition = { outcome: "ack" },
  ): ConsumerResult {
    const completedAt = utcNow();
    return {
      consumerId: spec.consumerId,
      subscriptionId: spec.subscription.subscriptionId,
      deliveryId: item.delivery.deliveryId,
      eventId: event.eventId,
      globalSequence: event.globalSequence,
      outcome,
      attempt: item.delivery.attempt,
      startedAt,
      completedAt,
      durationMs: elapsedMs(startedAt, completedAt),
      errorCode: disposition.errorCode,
      errorMessage: disposition.errorMessage,
      checkpoint,
    };
  }

  private processedRow(consumerId: string, eventId: string): ProcessedRow | undefined {
    return this.spine.store.db.prepare(`
      SELECT * FROM runtime_event_consumer_processed
      WHERE consumer_id = ? AND event_id = ?
    `).get(consumerId, eventId) as ProcessedRow | undefined;
  }

  private recordProcessed(
    consumerId: string,
    item: LeasedDelivery,
    event: RuntimeEventEnvelope,
    outcome: ConsumerOutcomeValue,
    mutation?: Readonly<Record<string, JsonValue>>,
  ): void {
    this.spine.store.db.prepare(`
      INSERT INTO runtime_event_consumer_processed (
        consumer_id, event_id, event_digest, delivery_id, global_sequence,
        outcome, attempt, processed_at, mutation_json
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(consumer_id, event_id) DO NOTHING
    `).run(
      consumerId,
      event.eventId,
      event.contentDigest,
      item.delivery.deliveryId,
      event.globalSequence,
      outcome,
      item.delivery.attempt,
      utcNow(),
      mutation ? canonicalJson(mutation) : null,
    );
  }

  private recordFailure(
    consumerId: string,
    item: LeasedDelivery,
    disposition: ConsumerDisposition,
    delay: number,
  ): void {
    this.spine.store.db.prepare(`
      INSERT INTO runtime_event_consumer_failures (
        consumer_id, delivery_id, event_id, attempt, error_code,
        error_message, retry_delay_ms, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      consumerId,
      item.delivery.deliveryId,
      item.delivery.eventId,
      item.delivery.attempt,
      disposition.errorCode ?? "consumer_failed",
      (disposition.errorMessage ?? "consumer failed").slice(0, 4000),
      Math.max(0, delay),
      utcNow(),
    );
  }

  private applyMutation(
    consumerId: string,
    event: RuntimeEventEnvelope,
    mutation: Readonly<Record<string, JsonValue>>,
  ): void {
    const domain = typeof mutation.domain === "string" ? mutation.domain : event.stateDelta.domain;
    const stateKey = typeof mutation.state_key === "string"
      ? mutation.state_key
      : event.stateDelta.path.join("/") || event.aggregateId;
    const current = this.readMutation(consumerId, domain, stateKey) ?? {};
    const merged = { ...current, ...mutation, last_event_id: event.eventId, global_sequence: event.globalSequence };
    const serialized = canonicalJson(merged);
    this.spine.store.db.prepare(`
      INSERT INTO runtime_event_consumer_mutations (
        consumer_id, domain, state_key, event_id, global_sequence,
        state_json, state_digest, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(consumer_id, domain, state_key) DO UPDATE SET
        event_id = excluded.event_id,
        global_sequence = excluded.global_sequence,
        state_json = excluded.state_json,
        state_digest = excluded.state_digest,
        updated_at = excluded.updated_at
      WHERE excluded.global_sequence >= runtime_event_consumer_mutations.global_sequence
    `).run(
      consumerId,
      domain,
      stateKey,
      event.eventId,
      event.globalSequence,
      serialized,
      digestJson(merged),
      utcNow(),
    );
  }

  private loadCheckpoint(consumerId: string): ConsumerCheckpoint | undefined {
    const row = this.spine.store.db.prepare(`
      SELECT * FROM runtime_event_consumer_checkpoints WHERE consumer_id = ?
    `).get(consumerId) as CheckpointRow | undefined;
    if (!row) return undefined;
    return parseConsumerCheckpoint(JSON.parse(row.checkpoint_json));
  }

  private saveCheckpoint(checkpoint: ConsumerCheckpoint): void {
    const parsed = parseConsumerCheckpoint(checkpoint);
    this.spine.store.db.prepare(`
      INSERT INTO runtime_event_consumer_checkpoints (
        consumer_id, subscription_id, role, checkpoint_json, generation, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?)
      ON CONFLICT(consumer_id) DO UPDATE SET
        subscription_id = excluded.subscription_id,
        role = excluded.role,
        checkpoint_json = excluded.checkpoint_json,
        generation = excluded.generation,
        updated_at = excluded.updated_at
    `).run(
      parsed.consumerId,
      parsed.subscriptionId,
      parsed.role,
      canonicalJson(checkpointToJson(parsed)),
      parsed.generation,
      parsed.updatedAt,
    );
  }

  private requireSpec(consumerId: string): DurableConsumerSpec {
    const normalized = normalizeIdentifier(consumerId, "consumerId");
    const spec = this.specs.get(normalized);
    if (!spec) {
      throw new DeliveryError(EventSpineErrorCode.SUBSCRIBER_NOT_FOUND, "consumer is not registered", {
        consumer_id: normalized,
      });
    }
    return spec;
  }
}

export function projectorConsumerHandler(
  projector: (aggregateId: string) => Record<string, unknown>,
): ConsumerHandler {
  return (event) => {
    const projection = projector(event.aggregateId);
    return {
      outcome: "ack",
      stateMutation: {
        domain: "projection",
        state_key: event.aggregateId,
        projection_digest: digestJson(JSON.parse(JSON.stringify(projection)) as JsonValue),
        projection_cursor: event.globalSequence,
        source_event_id: event.eventId,
      },
    };
  };
}

export function controlConsumerHandler(): ConsumerHandler {
  return (event) => {
    if (!event.eventType.startsWith("runtime.permission.") && !event.eventType.startsWith("runtime.control.")) {
      return { outcome: "skip" };
    }
    return {
      outcome: "ack",
      stateMutation: {
        domain: event.stateDelta.domain,
        state_key: event.identity.commandId ?? event.stateDelta.path.join("/"),
        status: event.stateDelta.value ?? event.inline.decision ?? event.eventType,
        source_event_id: event.eventId,
      },
    };
  };
}

export function workerConsumerHandler(): ConsumerHandler {
  return (event): ConsumerDisposition => {
    if (event.eventType === "runtime.permission.pending") {
      return { outcome: "skip", stateMutation: { domain: "session", state_key: event.aggregateId, blocked: true } };
    }
    if (event.eventType === "runtime.permission.denied") {
      return { outcome: "ack", stateMutation: { domain: "session", state_key: event.aggregateId, permission: "denied" } };
    }
    if (event.eventType === "runtime.permission.allowed") {
      return { outcome: "ack", stateMutation: { domain: "session", state_key: event.aggregateId, permission: "allowed" } };
    }
    return { outcome: "ack", stateMutation: { domain: "worker", state_key: event.aggregateId, last_intent: event.intent } };
  };
}
