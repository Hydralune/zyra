import {
  DeliveryState,
  EventDurability,
  messageFromEvent,
  parseRuntimeEventDraft,
  parseSubscriptionSpec,
  type AgentMessageEnvelope,
  type DeliveryRecord,
  type LeasedDelivery,
  type RuntimeEventDraft,
  type RuntimeEventEnvelope,
  type SubscriptionSpec,
} from "./contracts.ts";
import { byteLength, newId, utcNow } from "./canonical.ts";
import { eventDefinition } from "./event-catalog.ts";
import { DeliveryError, EventSpineErrorCode, RoutingError } from "./errors.ts";
import { AgentMessageRouter, type RouterContext } from "./router.ts";
import { RuntimeEventSqliteStore } from "./sqlite-store.ts";

export interface BusPollOptions {
  limit?: number;
  now?: string;
}

export interface BusCatchUpOptions {
  aggregateId?: string;
  taskId?: string;
  afterSequence?: number;
  limit?: number;
}

export interface LiveMessageRecord {
  liveId: string;
  subscriptionId: string;
  message: AgentMessageEnvelope;
  createdAt: string;
  expiresAt: string;
}

interface LiveQueue {
  capacity: number;
  records: LiveMessageRecord[];
  dropped: number;
}

export class RuntimeMessageBus {
  readonly store: RuntimeEventSqliteStore;
  readonly router: AgentMessageRouter;
  private readonly liveQueues = new Map<string, LiveQueue>();

  constructor(store: RuntimeEventSqliteStore, router = new AgentMessageRouter()) {
    this.store = store;
    this.router = router;
  }

  register(value: SubscriptionSpec | unknown): SubscriptionSpec {
    const spec = parseSubscriptionSpec(value);
    this.store.registerSubscription(spec);
    this.liveQueues.set(spec.subscriptionId, {
      capacity: spec.capacity,
      records: [],
      dropped: 0,
    });
    return spec;
  }

  unregister(subscriptionId: string, force = false): void {
    const spec = this.store.subscription(subscriptionId);
    if (!spec) return;
    const pending = this.store.pendingCount(subscriptionId);
    if (pending > 0 && !force) {
      throw new RoutingError(EventSpineErrorCode.SUBSCRIBER_BACKPRESSURE, "cannot unregister subscriber with pending deliveries", {
        subscription_id: subscriptionId,
        pending,
      });
    }
    this.store.registerSubscription({ ...spec, enabled: false });
    this.liveQueues.delete(subscriptionId);
  }

  planRoute(event: RuntimeEventEnvelope, options: Partial<RouterContext> = {}) {
    return this.router.route(event, {
      subscriptions: options.subscriptions ?? this.store.subscriptions(true),
      pendingBySubscription: options.pendingBySubscription ?? this.store.pendingBySubscription(),
      dependencyRecipients: options.dependencyRecipients,
      topK: options.topK,
      broadcast: options.broadcast,
      fanoutReason: options.fanoutReason,
    });
  }

  poll(subscriptionId: string, options: BusPollOptions = {}): readonly LeasedDelivery[] {
    const deliveries = this.store.lease(subscriptionId, options.limit ?? 1, options.now ?? utcNow());
    return deliveries.map((delivery) => {
      const event = this.store.get(delivery.eventId);
      if (!event) {
        throw new DeliveryError(EventSpineErrorCode.DELIVERY_NOT_FOUND, "delivery references missing canonical event", {
          delivery_id: delivery.deliveryId,
          event_id: delivery.eventId,
        });
      }
      return {
        delivery,
        message: messageFromEvent(event, `msg_${delivery.deliveryId}`),
      };
    });
  }

  acknowledge(deliveryId: string, leaseToken: string, now = utcNow()): DeliveryRecord {
    return this.store.acknowledge(deliveryId, leaseToken, now);
  }

  negativeAcknowledge(
    deliveryId: string,
    leaseToken: string,
    error: string,
    delayMs = 0,
    now = utcNow(),
  ): DeliveryRecord {
    return this.store.negativeAcknowledge(deliveryId, leaseToken, error, delayMs, now);
  }

  sweep(now = utcNow()): number {
    const durable = this.store.sweepExpired(now);
    const timestamp = new Date(now).getTime();
    let expired = 0;
    for (const queue of this.liveQueues.values()) {
      const retained = queue.records.filter((record) => new Date(record.expiresAt).getTime() >= timestamp);
      expired += queue.records.length - retained.length;
      queue.records = retained;
    }
    return durable + expired;
  }

  publishLive(value: RuntimeEventDraft | unknown, ttlMs = 30_000): readonly LiveMessageRecord[] {
    const draft = parseRuntimeEventDraft(value);
    const definition = eventDefinition(draft.eventType);
    if (definition.durability !== EventDurability.LIVE_ONLY) {
      throw new RoutingError(EventSpineErrorCode.ROUTE_NOT_FOUND, "durable event must publish through RuntimeEventStore", {
        event_type: draft.eventType,
      });
    }
    const pseudoEvent: RuntimeEventEnvelope = {
      schema: "zyra.runtime-event/v1",
      eventId: draft.eventId ?? newId("live"),
      eventType: draft.eventType,
      eventVersion: draft.eventVersion ?? 1,
      aggregateId: draft.aggregateId,
      aggregateSequence: -1,
      producerSequence: draft.producerSequence ?? 0,
      idempotencyKey: draft.idempotencyKey,
      correlationId: draft.correlationId,
      causationId: draft.causationId,
      createdAt: draft.createdAt ?? utcNow(),
      committedAt: utcNow(),
      durability: EventDurability.LIVE_ONLY,
      effect: draft.effect ?? "non_effective",
      identity: draft.identity,
      sender: draft.sender,
      intent: draft.intent,
      target: draft.target,
      topKRecipients: draft.topKRecipients ?? [],
      summary: draft.summary ?? "",
      stateDelta: draft.stateDelta ?? { domain: "stream", operation: "none", path: [], effective: false },
      evidenceRefs: draft.evidenceRefs ?? [],
      artifactRefs: draft.artifactRefs ?? [],
      uncertainty: draft.uncertainty,
      provenance: draft.provenance,
      inline: draft.inline ?? {},
      sourceBytes: draft.sourceBytes ?? byteLength(draft.inline ?? {}),
      inlineBytes: byteLength(draft.inline ?? {}),
      envelopeBytes: 0,
      contentDigest: "live-only",
      metadata: draft.metadata ?? {},
    };
    const route = this.planRoute(pseudoEvent);
    const subscriptions = this.store.subscriptionsByRecipient();
    const createdAt = utcNow();
    const expiresAt = new Date(new Date(createdAt).getTime() + Math.max(100, ttlMs)).toISOString();
    const output: LiveMessageRecord[] = [];
    for (const recipient of route.recipients) {
      const specs = subscriptions.get(`${recipient.kind}:${recipient.id}`) ?? [];
      for (const spec of specs) {
        const queue = this.liveQueues.get(spec.subscriptionId) ?? { capacity: spec.capacity, records: [], dropped: 0 };
        if (queue.records.length >= queue.capacity) {
          queue.records.shift();
          queue.dropped += 1;
        }
        const record: LiveMessageRecord = {
          liveId: newId("live-delivery"),
          subscriptionId: spec.subscriptionId,
          message: messageFromEvent(pseudoEvent),
          createdAt,
          expiresAt,
        };
        queue.records.push(record);
        this.liveQueues.set(spec.subscriptionId, queue);
        output.push(record);
      }
    }
    return output;
  }

  pollLive(subscriptionId: string, limit = 100): readonly LiveMessageRecord[] {
    const queue = this.liveQueues.get(subscriptionId);
    if (!queue) return [];
    const selected = queue.records.splice(0, Math.max(1, Math.min(limit, 1000)));
    return selected;
  }

  catchUp(subscriptionId: string, options: BusCatchUpOptions = {}): number {
    const spec = this.store.subscription(subscriptionId);
    if (!spec || !spec.enabled) {
      throw new DeliveryError(EventSpineErrorCode.SUBSCRIBER_NOT_FOUND, "subscription not found", { subscription_id: subscriptionId });
    }
    const page = this.store.query({
      aggregateId: options.aggregateId,
      taskId: options.taskId,
      afterSequence: options.afterSequence,
      limit: Math.min(options.limit ?? 1000, 1000),
    });
    let matched = 0;
    for (const event of page.items) {
      const route = this.planRoute(event, { topK: 8 });
      if (!route.recipients.some((recipient) => recipient.kind === spec.recipient.kind && recipient.id === spec.recipient.id)) continue;
      if (this.store.deliveriesForEvent(event.eventId).some((delivery) => delivery.subscriptionId === subscriptionId)) continue;
      matched += 1;
    }
    return matched;
  }

  snapshot(): Record<string, unknown> {
    const subscriptions = this.store.subscriptions(false);
    return {
      schema: "zyra.runtime-message-bus.snapshot/v1",
      durable: true,
      canonical_state_owner: false,
      event_store_owner: "RuntimeEventSqliteStore",
      subscriptions: subscriptions.map((spec) => ({
        subscription_id: spec.subscriptionId,
        recipient: spec.recipient,
        enabled: spec.enabled,
        pending: this.store.pendingCount(spec.subscriptionId),
        live_pending: this.liveQueues.get(spec.subscriptionId)?.records.length ?? 0,
        live_dropped: this.liveQueues.get(spec.subscriptionId)?.dropped ?? 0,
        capacity: spec.capacity,
      })),
    };
  }
}
