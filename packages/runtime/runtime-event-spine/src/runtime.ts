import {
  EventDurability,
  buildCommittedEnvelope,
  parseEventQuery,
  parseRuntimeEventDraft,
  type AppendOptions,
  type AppendReceipt,
  type EventPage,
  type EventQuery,
  type LegacyEventRecord,
  type LeasedDelivery,
  type RuntimeEventDraft,
  type RuntimeEventEnvelope,
  type SubscriptionSpec,
} from "./contracts.ts";
import { byteLength, utcNow, type JsonValue } from "./canonical.ts";
import { eventDefinition, catalogSummary } from "./event-catalog.ts";
import { EventSpineErrorCode, RoutingError } from "./errors.ts";
import { LowEntropyMetrics } from "./metrics.ts";
import { RuntimeMessageBus } from "./message-bus.ts";
import { normalizeLegacyEvent, normalizeOmpFrame, type NormalizedLegacyEvent, type OmpFrameInput } from "./normalizers.ts";
import { LocalContentAddressedArtifactStore, LowEntropyPayloadPolicy, type ExternalizedPayload } from "./payload-policy.ts";
import { builtInSubscriptions, AgentMessageRouter } from "./router.ts";
import { RuntimeEventSqliteStore } from "./sqlite-store.ts";
import { verifyToolPairs } from "./tool-pair.ts";

export interface RuntimeEventSpineOptions {
  sqlitePath: string;
  artifactRoot: string;
  registerBuiltIns?: boolean;
  routeByDefault?: boolean;
  publishLegacyProjection?: boolean;
}

export interface RuntimeAppendOptions extends AppendOptions {
  route?: boolean;
  dependencyRecipients?: readonly string[];
  topK?: number;
  broadcast?: boolean;
  fanoutReason?: string;
}

export interface RuntimeHealth {
  schema: string;
  ok: boolean;
  canonicalOwner: string;
  messageBusOwner: string;
  projectorOwner: string;
  sourceLanguage: string;
  store: ReturnType<RuntimeEventSqliteStore["health"]>;
  bus: Record<string, unknown>;
  catalog: Record<string, unknown>;
  lowEntropy: ReturnType<LowEntropyMetrics["report"]>;
}

export class RuntimeEventSpine {
  readonly options: Readonly<RuntimeEventSpineOptions>;
  readonly store: RuntimeEventSqliteStore;
  readonly artifacts: LocalContentAddressedArtifactStore;
  readonly payloadPolicy: LowEntropyPayloadPolicy;
  readonly router: AgentMessageRouter;
  readonly bus: RuntimeMessageBus;
  readonly metrics: LowEntropyMetrics;
  private closed = false;

  constructor(options: RuntimeEventSpineOptions) {
    this.options = Object.freeze({
      ...options,
      registerBuiltIns: options.registerBuiltIns ?? true,
      routeByDefault: options.routeByDefault ?? true,
      publishLegacyProjection: options.publishLegacyProjection ?? true,
    });
    this.store = new RuntimeEventSqliteStore(options.sqlitePath);
    this.artifacts = new LocalContentAddressedArtifactStore(options.artifactRoot);
    this.payloadPolicy = new LowEntropyPayloadPolicy(this.artifacts);
    this.router = new AgentMessageRouter();
    this.bus = new RuntimeMessageBus(this.store, this.router);
    this.metrics = new LowEntropyMetrics(this.store);
    if (this.options.registerBuiltIns) {
      for (const subscription of builtInSubscriptions()) this.bus.register(subscription);
    }
  }

  appendLegacy(value: LegacyEventRecord | unknown, options: RuntimeAppendOptions = {}): AppendReceipt {
    this.assertOpen();
    const normalized = normalizeLegacyEvent(value);
    const prepared = this.payloadPolicy.externalize(normalized.draft, normalized.sourcePayload);
    return this.commitPrepared(prepared, options, normalized);
  }

  appendLegacyBatch(values: readonly unknown[], options: RuntimeAppendOptions = {}): readonly AppendReceipt[] {
    const receipts: AppendReceipt[] = [];
    for (const value of values) receipts.push(this.appendLegacy(value, options));
    return receipts;
  }

  appendCanonical(value: RuntimeEventDraft | unknown, options: RuntimeAppendOptions = {}): AppendReceipt {
    this.assertOpen();
    const draft = parseRuntimeEventDraft(value);
    const prepared = this.payloadPolicy.externalize(draft, draft.inline ?? {});
    return this.commitPrepared(prepared, options);
  }

  appendOmpFrame(value: OmpFrameInput, options: RuntimeAppendOptions = {}): AppendReceipt | readonly ReturnType<RuntimeMessageBus["publishLive"]>[] {
    const draft = normalizeOmpFrame(value);
    if (draft.durability === EventDurability.LIVE_ONLY) return [this.bus.publishLive(draft)];
    return this.appendCanonical(draft, options);
  }

  publishLive(value: RuntimeEventDraft | unknown, ttlMs = 30_000) {
    this.assertOpen();
    return this.bus.publishLive(value, ttlMs);
  }

  query(value: EventQuery | unknown = {}): EventPage {
    this.assertOpen();
    return this.store.query(parseEventQuery(value));
  }

  count(value: EventQuery | unknown = {}): number {
    let cursor: string | undefined;
    let count = 0;
    do {
      const query = { ...parseEventQuery(value), cursor, limit: 1000 };
      const page = this.store.query(query);
      count += page.items.length;
      cursor = page.nextCursor;
    } while (cursor);
    return count;
  }

  get(eventId: string): RuntimeEventEnvelope | undefined {
    return this.store.get(eventId);
  }

  registerSubscription(value: SubscriptionSpec | unknown): SubscriptionSpec {
    return this.bus.register(value);
  }

  poll(subscriptionId: string, limit = 1): readonly LeasedDelivery[] {
    return this.bus.poll(subscriptionId, { limit });
  }

  acknowledge(deliveryId: string, leaseToken: string) {
    return this.bus.acknowledge(deliveryId, leaseToken);
  }

  negativeAcknowledge(deliveryId: string, leaseToken: string, error: string, delayMs = 0) {
    return this.bus.negativeAcknowledge(deliveryId, leaseToken, error, delayMs);
  }

  projection(aggregateId: string): Record<string, unknown> {
    this.assertOpen();
    return {
      schema: "zyra.runtime-event-projection/v1",
      aggregate_id: aggregateId,
      cursor: this.store.projector.cursor(this.store.db, aggregateId),
      session: this.store.projector.session(this.store.db, aggregateId),
      tools: this.store.projector.toolCalls(this.store.db, aggregateId),
      controls: this.store.projector.controls(this.store.db, aggregateId),
      artifacts: this.store.projector.artifacts(this.store.db, aggregateId),
      tool_pair_audit: verifyToolPairs(this.store.db, aggregateId),
    };
  }

  rebuildProjection(aggregateId?: string): Record<string, unknown> {
    this.assertOpen();
    const query = aggregateId ? { aggregateId, limit: 1000 } : { limit: 1000 };
    const events: RuntimeEventEnvelope[] = [];
    let cursor: string | undefined;
    do {
      const page = this.store.query({ ...query, cursor });
      events.push(...page.items);
      cursor = page.nextCursor;
    } while (cursor);
    this.store.db.exec("BEGIN IMMEDIATE");
    try {
      this.store.projector.reset(this.store.db, aggregateId);
      for (const event of events) this.store.projector.apply(this.store.db, event);
      this.store.db.exec("COMMIT");
    } catch (error) {
      try { this.store.db.exec("ROLLBACK"); } catch { /* preserve original */ }
      throw error;
    }
    return {
      schema: "zyra.runtime-projection-rebuild/v1",
      aggregate_id: aggregateId ?? null,
      event_count: events.length,
      aggregate_count: new Set(events.map((event) => event.aggregateId)).size,
      first_sequence: events[0]?.aggregateSequence,
      last_sequence: events.at(-1)?.aggregateSequence,
      rebuilt_at: utcNow(),
    };
  }

  health(): RuntimeHealth {
    this.assertOpen();
    return {
      schema: "zyra.runtime-event-spine.health/v1",
      ok: true,
      canonicalOwner: "RuntimeEventSqliteStore",
      messageBusOwner: "RuntimeMessageBus (delivery only)",
      projectorOwner: this.store.projector.projectorId,
      sourceLanguage: "TypeScript",
      store: this.store.health(),
      bus: this.bus.snapshot(),
      catalog: catalogSummary(),
      lowEntropy: this.metrics.report(),
    };
  }

  close(): void {
    if (this.closed) return;
    this.store.close();
    this.closed = true;
  }

  private commitPrepared(
    prepared: ExternalizedPayload,
    options: RuntimeAppendOptions,
    legacy?: NormalizedLegacyEvent,
  ): AppendReceipt {
    const routeEnabled = options.route ?? this.options.routeByDefault ?? true;
    const nextSequence = this.store.latestSequence(prepared.draft.aggregateId) + 1;
    const preview = buildCommittedEnvelope(
      prepared.draft,
      nextSequence,
      utcNow(),
      prepared.inlineBytes,
      prepared.estimatedEnvelopeBytes,
    );
    eventDefinition(preview.eventType);
    const route = routeEnabled
      ? this.bus.planRoute(preview, {
          dependencyRecipients: options.dependencyRecipients,
          topK: options.topK,
          broadcast: options.broadcast,
          fanoutReason: options.fanoutReason,
        })
      : undefined;
    if (routeEnabled && !route) {
      throw new RoutingError(EventSpineErrorCode.ROUTE_NOT_FOUND, "event routing was enabled but produced no decision", {
        event_type: preview.eventType,
      });
    }
    return this.store.append({
      draft: prepared.draft,
      inlineBytes: prepared.inlineBytes,
      estimatedEnvelopeBytes: prepared.estimatedEnvelopeBytes,
      artifactSpillCount: prepared.artifactSpillCount,
      warnings: prepared.findings,
      options: {
        ...options,
        route: routeEnabled,
        project: options.project ?? true,
        publishLegacyProjection: options.publishLegacyProjection ?? this.options.publishLegacyProjection,
      },
      route,
      subscriptionsByRecipient: route ? this.store.subscriptionsByRecipient() : undefined,
      legacyProjection: legacy
        ? {
            eventId: String(legacy.record.event_id),
            runId: String(legacy.record.run_id),
            taskId: String(legacy.record.task_id),
            nodeId: legacy.record.node_id ?? undefined,
            eventType: String(legacy.record.event_type),
            createdAt: String(legacy.record.created_at),
            payload: legacy.sourcePayload,
          }
        : undefined,
    });
  }

  private assertOpen(): void {
    if (!this.closed) return;
    throw new Error("RuntimeEventSpine is closed");
  }
}
