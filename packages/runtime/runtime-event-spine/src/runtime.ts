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
import { byteLength, newId, utcNow, type JsonValue } from "./canonical.ts";
import { eventDefinition, catalogSummary } from "./event-catalog.ts";
import { EventSpineErrorCode, RoutingError } from "./errors.ts";
import { LowEntropyMetrics } from "./metrics.ts";
import { RuntimeMessageBus } from "./message-bus.ts";
import { normalizeLegacyEvent, normalizeOmpFrame, type NormalizedLegacyEvent, type OmpFrameInput } from "./normalizers.ts";
import { LocalContentAddressedArtifactStore, LowEntropyPayloadPolicy, type ExternalizedPayload } from "./payload-policy.ts";
import { builtInSubscriptions, AgentMessageRouter } from "./router.ts";
import { RuntimeEventSqliteStore } from "./sqlite-store.ts";
import { verifyToolPairs } from "./tool-pair.ts";
import { ProjectionDeliveryRuntime } from "./projection-delivery.ts";
import { OmpRuntimeFrameMapper, RuntimeSourceMapper, type OmpAgentFrame } from "./source-mapper.ts";
import type { SourceBatch, SourceRecord } from "./integration-contracts.ts";

export interface RuntimeEventSpineOptions {
  sqlitePath: string;
  artifactRoot: string;
  registerBuiltIns?: boolean;
  routeByDefault?: boolean;
  publishLegacyProjection?: boolean;
  enableMessageBus?: boolean;
  enableProjector?: boolean;
  autoDrainProjector?: boolean;
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
  artifactRoot: string;
  store: ReturnType<RuntimeEventSqliteStore["health"]>;
  bus: Record<string, unknown>;
  catalog: Record<string, unknown>;
  lowEntropy: ReturnType<LowEntropyMetrics["report"]>;
  integration: Record<string, unknown>;
}

export class RuntimeEventSpine {
  readonly options: Readonly<RuntimeEventSpineOptions>;
  readonly store: RuntimeEventSqliteStore;
  readonly artifacts: LocalContentAddressedArtifactStore;
  readonly payloadPolicy: LowEntropyPayloadPolicy;
  readonly router: AgentMessageRouter;
  readonly bus: RuntimeMessageBus;
  readonly metrics: LowEntropyMetrics;
  readonly sourceMapper: RuntimeSourceMapper;
  readonly ompMapper: OmpRuntimeFrameMapper;
  readonly projectionDelivery?: ProjectionDeliveryRuntime;
  private closed = false;

  constructor(options: RuntimeEventSpineOptions) {
    this.options = Object.freeze({
      ...options,
      registerBuiltIns: options.registerBuiltIns ?? true,
      routeByDefault: options.routeByDefault ?? true,
      publishLegacyProjection: options.publishLegacyProjection ?? true,
      enableMessageBus: options.enableMessageBus ?? true,
      enableProjector: options.enableProjector ?? true,
      autoDrainProjector: options.autoDrainProjector ?? true,
    });
    this.store = new RuntimeEventSqliteStore(options.sqlitePath);
    this.artifacts = new LocalContentAddressedArtifactStore(options.artifactRoot);
    this.payloadPolicy = new LowEntropyPayloadPolicy(this.artifacts);
    this.router = new AgentMessageRouter();
    this.bus = new RuntimeMessageBus(this.store, this.router);
    this.metrics = new LowEntropyMetrics(this.store);
    this.sourceMapper = new RuntimeSourceMapper();
    this.ompMapper = new OmpRuntimeFrameMapper(this.sourceMapper);
    if (this.options.registerBuiltIns) {
      for (const subscription of builtInSubscriptions()) this.bus.register(subscription);
    }
    // The read model is itself a durable bus consumer.  Enabling it while the
    // bus is disabled would silently create a direct store->projector path and
    // make the fault-injection matrix lie about the component boundary.
    if (this.options.enableProjector && this.options.enableMessageBus) {
      this.projectionDelivery = new ProjectionDeliveryRuntime(this, {
        autoDrain: this.options.autoDrainProjector,
      });
      this.projectionDelivery.reconcileAckGaps();
      this.projectionDelivery.pump({ catchUp: true });
    }
  }

  appendLegacy(value: LegacyEventRecord | unknown, options: RuntimeAppendOptions = {}): AppendReceipt {
    this.assertOpen();
    const normalized = normalizeLegacyEvent(value);
    let draft = JSON.parse(JSON.stringify(normalized.draft)) as RuntimeEventDraft;
    if (eventDefinition(draft.eventType).requiresCausation && !draft.causationId) {
      // Legacy emitters can interleave unrelated command/tool correlations on
      // the same task aggregate.  Never invent a cross-correlation parent.
      const cause = this.store.query({
        aggregateId: draft.aggregateId,
        correlationId: draft.correlationId,
        descending: true,
        limit: 1,
      }).items[0];
      if (cause) {
        draft = {
          ...draft,
          causationId: cause.eventId,
          metadata: { ...(draft.metadata ?? {}), legacy_causation_inferred: true },
        };
      }
    }
    const prepared = this.payloadPolicy.externalize(draft, normalized.sourcePayload);
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

  appendSourceRecord(value: SourceRecord | unknown): AppendReceipt | ReturnType<RuntimeMessageBus["publishLive"]> {
    const mapped = this.sourceMapper.map(value);
    if (mapped.draft.durability === EventDurability.LIVE_ONLY) {
      const prepared = this.payloadPolicy.externalize(mapped.draft, mapped.source.payload);
      return this.bus.publishLive(JSON.parse(JSON.stringify(prepared.draft)), 30_000);
    }
    return this.commitPrepared(this.payloadPolicy.externalize(mapped.draft, mapped.source.payload), mapped.options);
  }

  appendSourceBatch(value: SourceBatch | unknown): readonly (AppendReceipt | ReturnType<RuntimeMessageBus["publishLive"]>)[] {
    return this.sourceMapper.mapBatch(value).map((mapped) => {
      const prepared = this.payloadPolicy.externalize(mapped.draft, mapped.source.payload);
      if (mapped.draft.durability === EventDurability.LIVE_ONLY) return this.bus.publishLive(JSON.parse(JSON.stringify(prepared.draft)), 30_000);
      return this.commitPrepared(prepared, mapped.options);
    });
  }

  appendOmpAgentFrame(value: OmpAgentFrame | unknown): AppendReceipt | ReturnType<RuntimeMessageBus["publishLive"]> {
    const mapped = this.ompMapper.map(value);
    const prepared = this.payloadPolicy.externalize(mapped.draft, mapped.source.payload);
    if (mapped.draft.durability === EventDurability.LIVE_ONLY) return this.bus.publishLive(JSON.parse(JSON.stringify(prepared.draft)), 30_000);
    return this.commitPrepared(prepared, mapped.options);
  }

  appendOmpFrame(value: OmpFrameInput, options: RuntimeAppendOptions = {}): AppendReceipt | readonly ReturnType<RuntimeMessageBus["publishLive"]>[] {
    const draft = normalizeOmpFrame(value);
    const prepared = this.payloadPolicy.externalize(draft, JSON.parse(JSON.stringify(value)) as Record<string, JsonValue>);
    if (draft.durability === EventDurability.LIVE_ONLY) return [this.bus.publishLive(JSON.parse(JSON.stringify(prepared.draft)), 30_000)];
    return this.commitPrepared(prepared, options);
  }

  publishLive(value: RuntimeEventDraft | unknown, ttlMs = 30_000) {
    this.assertOpen();
    const draft = parseRuntimeEventDraft(value);
    const prepared = this.payloadPolicy.externalize(draft, draft.inline ?? {});
    return this.bus.publishLive(JSON.parse(JSON.stringify(prepared.draft)), ttlMs);
  }

  query(value: EventQuery | unknown = {}): EventPage {
    this.assertOpen();
    return this.store.query(value);
  }

  count(value: EventQuery | unknown = {}): number {
    let cursor: string | undefined;
    let count = 0;
    do {
      const query = JSON.parse(JSON.stringify({ ...parseEventQuery(value), cursor, limit: 1000 })) as EventQuery;
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
      task_view: this.projectionDelivery?.taskView(aggregateId) ?? null,
      history: this.projectionDelivery?.history(aggregateId, { limit: 200 }) ?? null,
      tool_pair_audit: verifyToolPairs(this.store.db, aggregateId),
    };
  }

  rebuildProjection(aggregateId?: string): Record<string, unknown> {
    if (this.projectionDelivery) return this.projectionDelivery.rebuild(aggregateId);
    this.assertOpen();
    const query = aggregateId ? { aggregateId, limit: 1000 } : { limit: 1000 };
    const events: RuntimeEventEnvelope[] = [];
    let cursor: string | undefined;
    do {
      const page = this.store.query(JSON.parse(JSON.stringify({ ...query, cursor })));
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
      artifactRoot: this.artifacts.root,
      store: this.store.health(),
      bus: this.bus.snapshot(),
      catalog: catalogSummary(),
      lowEntropy: this.metrics.report(),
      integration: {
        message_bus_enabled: this.options.enableMessageBus,
        projector_enabled: this.options.enableProjector,
        projector_cursor: this.projectionDelivery?.globalCursor() ?? null,
        source_mapper: this.sourceMapper.describe(),
        omp_mapper: this.ompMapper.describe(),
      },
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
    const routeRequested = options.route ?? this.options.routeByDefault ?? true;
    const routeEnabled = routeRequested && this.options.enableMessageBus !== false;
    const canonicalDraft = JSON.parse(JSON.stringify(prepared.draft)) as RuntimeEventDraft;
    const preparedDraft = canonicalDraft.eventId
      ? canonicalDraft
      : { ...canonicalDraft, eventId: newId("evt") };
    const nextSequence = this.store.latestSequence(preparedDraft.aggregateId) + 1;
    const preview = buildCommittedEnvelope(
      preparedDraft,
      nextSequence,
      this.store.latestGlobalSequence() + 1,
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
    const receipt = this.store.append({
      draft: preparedDraft,
      inlineBytes: prepared.inlineBytes,
      estimatedEnvelopeBytes: prepared.estimatedEnvelopeBytes,
      artifactSpillCount: prepared.artifactSpillCount,
      offloadedBytes: prepared.offloadedBytes,
      warnings: prepared.findings,
      options: {
        ...options,
        route: routeEnabled,
        project: false,
        publishLegacyProjection: options.publishLegacyProjection ?? this.options.publishLegacyProjection,
      },
      route,
      subscriptionsByRecipient: route ? this.store.subscriptionsByRecipient() : undefined,
      legacyProjection: legacy
        ? {
            eventId: String(legacy.record.event_id),
            runId: String(legacy.record.run_id),
            taskId: String(legacy.record.task_id),
            ...(legacy.record.node_id
              ? { nodeId: String(legacy.record.node_id) }
              : {}),
            eventType: String(legacy.record.event_type),
            createdAt: String(legacy.record.created_at),
            payload: legacy.sourcePayload,
          }
        : undefined,
    });
    const warnings = [...receipt.warnings];
    const deliveryIds = new Set(receipt.deliveryIds);
    if (receipt.duplicate && routeEnabled) {
      for (const subscription of this.store.subscriptions(true)) {
        if (subscription.subscriptionId === this.projectionDelivery?.subscriptionId) continue;
        for (const deliveryId of this.bus.ensureDelivery(subscription.subscriptionId, receipt.event, false)) {
          deliveryIds.add(deliveryId);
        }
      }
      if (deliveryIds.size > receipt.deliveryIds.length) warnings.push("duplicate_delivery_gap_repaired");
    }
    let projected = receipt.projected;
    if (options.project !== false && this.projectionDelivery) {
      try {
        this.projectionDelivery.admit(receipt.event);
        projected = Boolean(this.projectionDelivery.taskView(receipt.event.aggregateId));
      } catch (error) {
        warnings.push(`projector_pending:${error instanceof Error ? error.message : String(error)}`);
      }
    }
    return {
      ...receipt,
      projected,
      routed: deliveryIds.size > 0,
      deliveryIds: Object.freeze([...deliveryIds]),
      warnings,
    };
  }

  private assertOpen(): void {
    if (!this.closed) return;
    throw new Error("RuntimeEventSpine is closed");
  }
}
