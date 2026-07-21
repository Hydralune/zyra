import {
  MEMORY_SIGNAL_PROTOCOL,
  cloneJson,
  contractError,
  digest,
  normalizeIdentity,
  nowIso,
  stableId,
  uniqueStrings,
  type JsonObject,
  type MemorySignal,
  type MemorySignalKind,
  type RuntimeIdentity,
} from "./contracts.ts";
import type { MemorySignalTransport } from "./ports.ts";

export interface MemorySignalInput {
  kind: MemorySignalKind;
  identity: RuntimeIdentity;
  aggregateId: string;
  causationId: string;
  correlationId?: string;
  priority?: MemorySignal["priority"];
  payload: JsonObject;
}

export interface MemorySignalDelivery {
  deliveryId: string;
  signalId: string;
  state: "pending" | "published" | "failed";
  attempt: number;
  publishedAt: string | null;
  error: string | null;
  digest: string;
}

export interface MemorySignalSnapshot {
  version: "zyra.memory-signal-emitter/v1";
  identity: RuntimeIdentity;
  sequence: number;
  signals: MemorySignal[];
  deliveries: MemorySignalDelivery[];
  checksum: string;
}

export class MemorySignalEmitter {
  readonly identity: RuntimeIdentity;
  private readonly signals = new Map<string, MemorySignal>();
  private readonly deliveries = new Map<string, MemorySignalDelivery>();
  private readonly transport: MemorySignalTransport | null;
  private readonly now: () => Date;
  private sequence = 0;

  constructor(options: {
    identity: RuntimeIdentity;
    transport?: MemorySignalTransport | null;
    now?: () => Date;
    snapshot?: MemorySignalSnapshot | null;
  }) {
    this.identity = normalizeIdentity(options.identity);
    this.transport = options.transport ?? null;
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  emit(inputValue: MemorySignalInput): MemorySignal {
    const input = normalizeInput(inputValue, this.identity);
    const dedupeId = stableId("memory-signal", input.kind, input.aggregateId, input.causationId, digest(input.payload));
    const existing = this.signals.get(dedupeId);
    if (existing) return cloneJson(existing);
    this.sequence += 1;
    const createdAt = nowIso(this.now);
    const payloadDigest = digest(input.payload);
    const unsigned = {
      signalId: dedupeId,
      protocol: MEMORY_SIGNAL_PROTOCOL,
      kind: input.kind,
      identity: cloneJson(input.identity),
      aggregateId: input.aggregateId,
      causationId: input.causationId,
      correlationId: input.correlationId || input.identity.sessionId,
      sequence: this.sequence,
      priority: input.priority,
      payload: cloneJson(input.payload),
      payloadDigest,
      createdAt,
      publishedAt: null,
      acknowledgedBy: [],
    } satisfies Omit<MemorySignal, "signalDigest">;
    const signal: MemorySignal = { ...unsigned, signalDigest: digest(unsigned) };
    this.signals.set(signal.signalId, signal);
    this.deliveries.set(signal.signalId, deliveryFor(signal));
    return cloneJson(signal);
  }

  async publish(signalId: string): Promise<MemorySignalDelivery> {
    const signal = this.require(signalId);
    const delivery = this.requireDelivery(signalId);
    if (delivery.state === "published") return cloneJson(delivery);
    delivery.attempt += 1;
    try {
      if (this.transport) await this.transport.publish(signalToEnvelope(signal));
      const publishedAt = nowIso(this.now);
      signal.publishedAt = publishedAt;
      signal.signalDigest = signalDigest(signal);
      delivery.state = "published";
      delivery.publishedAt = publishedAt;
      delivery.error = null;
      delivery.digest = deliveryDigest(delivery);
      return cloneJson(delivery);
    } catch (error) {
      delivery.state = "failed";
      delivery.error = error instanceof Error ? error.message.slice(0, 4_096) : String(error).slice(0, 4_096);
      delivery.digest = deliveryDigest(delivery);
      return cloneJson(delivery);
    }
  }

  async drain(limit = 100): Promise<MemorySignalDelivery[]> {
    const pending = [...this.deliveries.values()]
      .filter((item) => item.state === "pending" || item.state === "failed")
      .sort((left, right) => this.require(left.signalId).sequence - this.require(right.signalId).sequence)
      .slice(0, Math.max(0, Math.min(limit, 10_000)));
    const output: MemorySignalDelivery[] = [];
    for (const delivery of pending) output.push(await this.publish(delivery.signalId));
    return output;
  }

  acknowledge(signalId: string, consumer: string): MemorySignal {
    const signal = this.require(signalId);
    signal.acknowledgedBy = uniqueStrings([...signal.acknowledgedBy, consumer]);
    signal.signalDigest = signalDigest(signal);
    return cloneJson(signal);
  }

  list(options: { kind?: MemorySignalKind; published?: boolean; afterSequence?: number; limit?: number } = {}): MemorySignal[] {
    const afterSequence = Math.max(0, Math.floor(options.afterSequence ?? 0));
    const limit = Math.max(0, Math.min(Math.floor(options.limit ?? 1_000), 100_000));
    return [...this.signals.values()]
      .filter((signal) => !options.kind || signal.kind === options.kind)
      .filter((signal) => options.published === undefined || Boolean(signal.publishedAt) === options.published)
      .filter((signal) => signal.sequence > afterSequence)
      .sort((left, right) => left.sequence - right.sequence)
      .slice(0, limit)
      .map(cloneJson);
  }

  health(): JsonObject {
    const deliveries = [...this.deliveries.values()];
    return {
      canonical_owner: "MemorySignalEmitter",
      signal_sequence: this.sequence,
      signal_count: this.signals.size,
      pending_count: deliveries.filter((item) => item.state === "pending").length,
      published_count: deliveries.filter((item) => item.state === "published").length,
      failed_count: deliveries.filter((item) => item.state === "failed").length,
      transport_attached: Boolean(this.transport),
      consumers: ["05C-runtime-event-spine", "07C-recovery-planner", "scheduler", "audit"],
      low_entropy_kinds: [
        "skill_memory_updated",
        "skill_memory_rejected",
        "procedure_mined",
        "procedure_rejected",
        "compact_triggered",
        "compact_deferred",
        "compact_restored",
        "compact_restore_rejected",
        "context_epoch_advanced",
      ],
    };
  }

  snapshot(): MemorySignalSnapshot {
    const unsigned: Omit<MemorySignalSnapshot, "checksum"> = {
      version: "zyra.memory-signal-emitter/v1",
      identity: cloneJson(this.identity),
      sequence: this.sequence,
      signals: this.list({ limit: 100_000 }),
      deliveries: [...this.deliveries.values()].sort((left, right) => this.require(left.signalId).sequence - this.require(right.signalId).sequence).map(cloneJson),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshotValue: MemorySignalSnapshot): void {
    if (snapshotValue.version !== "zyra.memory-signal-emitter/v1") throw contractError("memory_signal_snapshot_version", "unsupported memory signal snapshot version");
    const { checksum, ...unsigned } = snapshotValue;
    if (digest(unsigned) !== checksum) throw contractError("memory_signal_snapshot_checksum", "memory signal snapshot checksum mismatch");
    const identity = normalizeIdentity(snapshotValue.identity);
    if (identity.taskId !== this.identity.taskId || identity.sessionId !== this.identity.sessionId) throw contractError("memory_signal_snapshot_binding", "memory signal snapshot belongs to another task/session");
    this.signals.clear();
    this.deliveries.clear();
    for (const signal of snapshotValue.signals) {
      if (signalDigest(signal) !== signal.signalDigest) throw contractError("memory_signal_digest", `memory signal digest mismatch: ${signal.signalId}`);
      if (this.signals.has(signal.signalId)) throw contractError("memory_signal_duplicate", `duplicate memory signal ${signal.signalId}`);
      this.signals.set(signal.signalId, cloneJson(signal));
    }
    for (const delivery of snapshotValue.deliveries) {
      if (!this.signals.has(delivery.signalId)) throw contractError("memory_signal_orphan_delivery", "memory signal delivery has no signal");
      if (deliveryDigest(delivery) !== delivery.digest) throw contractError("memory_signal_delivery_digest", `memory signal delivery digest mismatch: ${delivery.deliveryId}`);
      this.deliveries.set(delivery.signalId, cloneJson(delivery));
    }
    this.sequence = snapshotValue.sequence;
  }

  private require(signalId: string): MemorySignal {
    const signal = this.signals.get(signalId.trim());
    if (!signal) throw contractError("memory_signal_not_found", `memory signal not found: ${signalId}`);
    return signal;
  }

  private requireDelivery(signalId: string): MemorySignalDelivery {
    const delivery = this.deliveries.get(signalId.trim());
    if (!delivery) throw contractError("memory_signal_delivery_not_found", `memory signal delivery not found: ${signalId}`);
    return delivery;
  }
}

function normalizeInput(value: MemorySignalInput, identity: RuntimeIdentity): Required<MemorySignalInput> {
  const normalizedIdentity = normalizeIdentity(value.identity);
  if (normalizedIdentity.taskId !== identity.taskId || normalizedIdentity.sessionId !== identity.sessionId) throw contractError("memory_signal_binding", "memory signal belongs to another task/session");
  const kinds: MemorySignalKind[] = ["skill_memory_updated", "skill_memory_rejected", "procedure_mined", "procedure_rejected", "compact_triggered", "compact_deferred", "compact_restored", "compact_restore_rejected", "context_epoch_advanced"];
  if (!kinds.includes(value.kind)) throw contractError("memory_signal_kind", `unsupported memory signal kind ${value.kind}`);
  const priority = value.priority ?? "normal";
  if (!["low", "normal", "high", "critical"].includes(priority)) throw contractError("memory_signal_priority", `unsupported memory signal priority ${priority}`);
  const aggregateId = value.aggregateId?.trim();
  const causationId = value.causationId?.trim();
  if (!aggregateId || !causationId) throw contractError("memory_signal_causality", "memory signal requires aggregate and causation ids");
  return {
    kind: value.kind,
    identity: normalizedIdentity,
    aggregateId,
    causationId,
    correlationId: value.correlationId?.trim() || normalizedIdentity.sessionId,
    priority,
    payload: cloneJson(value.payload ?? {}),
  };
}

function deliveryFor(signal: MemorySignal): MemorySignalDelivery {
  const unsigned = {
    deliveryId: stableId("memory-signal-delivery", signal.signalId),
    signalId: signal.signalId,
    state: "pending" as const,
    attempt: 0,
    publishedAt: null,
    error: null,
  };
  return { ...unsigned, digest: digest(unsigned) };
}

function signalToEnvelope(signal: MemorySignal): JsonObject {
  return {
    protocol: "zyra.runtime-event-envelope/v1",
    event_type: `memory.${signal.kind}`,
    sequence: signal.sequence,
    run_id: signal.identity.runId,
    task_id: signal.identity.taskId,
    session_id: signal.identity.sessionId,
    worker_request_id: signal.identity.workerRequestId,
    aggregate_id: signal.aggregateId,
    causation_id: signal.causationId,
    correlation_id: signal.correlationId,
    payload: cloneJson(signal.payload),
    payload_digest: signal.payloadDigest,
    priority: signal.priority,
    canonical_owner: "MemorySignalEmitter",
  };
}

function signalDigest(signal: MemorySignal): string {
  const { signalDigest: _ignored, ...unsigned } = signal;
  return digest(unsigned);
}

function deliveryDigest(delivery: MemorySignalDelivery): string {
  const { digest: _ignored, ...unsigned } = delivery;
  return digest(unsigned);
}
