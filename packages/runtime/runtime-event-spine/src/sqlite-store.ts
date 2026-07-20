import { DatabaseSync } from "node:sqlite";
import {
  BackpressureMode,
  DeliveryState,
  EventDurability,
  EventEffect,
  buildCommittedEnvelope,
  deliveryToJson,
  eventFromJson,
  eventToJson,
  parseEventQuery,
  parseRuntimeEventDraft,
  subscriptionToJson,
  type AppendOptions,
  type AppendReceipt,
  type DeliveryRecord,
  type EventPage,
  type EventQuery,
  type RouteDecision,
  type RuntimeEventDraft,
  type RuntimeEventEnvelope,
  type SubscriptionSpec,
} from "./contracts.ts";
import {
  canonicalJson,
  decodeCursor,
  digestJson,
  encodeCursor,
  newId,
  newToken,
  parseTimestamp,
  requireInteger,
  requireString,
  utcNow,
  type JsonValue,
} from "./canonical.ts";
import { validateEventKind } from "./event-catalog.ts";
import {
  AggregateConflictError,
  DeliveryError,
  EventSpineErrorCode,
  EventSpineError,
  PayloadBudgetError,
  RoutingError,
  asEventSpineError,
} from "./errors.ts";
import { RuntimeStateProjector } from "./projector.ts";

interface AggregateRow {
  aggregate_id: string;
  owner_id: string | null;
  latest_sequence: number;
  latest_event_id: string | null;
  created_at: string;
  updated_at: string;
}

interface EventRow {
  event_id: string;
  aggregate_id: string;
  sequence: number;
  global_sequence: number;
  event_type: string;
  event_version: number;
  run_id: string;
  session_id: string | null;
  task_id: string;
  worker_id: string | null;
  intent: string;
  effect: string;
  idempotency_key: string;
  correlation_id: string;
  causation_id: string | null;
  created_at: string;
  committed_at: string;
  draft_digest: string;
  content_digest: string;
  envelope_json: string;
  source_bytes: number;
  inline_bytes: number;
  envelope_bytes: number;
  artifact_ref_count: number;
}

interface SubscriptionRow {
  subscription_id: string;
  recipient_kind: string;
  recipient_id: string;
  spec_json: string;
  enabled: number;
  capacity: number;
  max_attempts: number;
  ack_timeout_ms: number;
  backpressure_mode: string;
  priority: number;
  created_at: string;
  updated_at: string;
}

interface DeliveryRow {
  delivery_id: string;
  event_id: string;
  subscription_id: string;
  recipient_kind: string;
  recipient_id: string;
  recipient_json: string;
  state: string;
  attempt: number;
  available_at: string;
  leased_at: string | null;
  lease_expires_at: string | null;
  lease_token: string | null;
  acknowledged_at: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
  redelivered: number;
  route_policy_id: string;
  route_reason: string;
}

export interface StoreAppendInput {
  draft: RuntimeEventDraft;
  inlineBytes: number;
  estimatedEnvelopeBytes: number;
  artifactSpillCount: number;
  offloadedBytes?: number;
  warnings: readonly string[];
  options?: AppendOptions;
  route?: RouteDecision;
  subscriptionsByRecipient?: ReadonlyMap<string, readonly SubscriptionSpec[]>;
  legacyProjection?: {
    eventId: string;
    runId: string;
    taskId: string;
    nodeId?: string;
    eventType: string;
    createdAt: string;
    payload: Readonly<Record<string, JsonValue>>;
  };
}

export interface StoreHealth {
  open: boolean;
  path: string;
  eventCount: number;
  highWatermark: number;
  aggregateCount: number;
  subscriptionCount: number;
  pendingDeliveryCount: number;
  deadLetterCount: number;
  projectorId: string;
}

function parseEventRow(row: EventRow): RuntimeEventEnvelope {
  const stored = JSON.parse(row.envelope_json) as Record<string, unknown>;
  if (stored.globalSequence === undefined) {
    delete stored.contentDigest;
    stored.globalSequence = Number(row.global_sequence);
    stored.contentDigest = digestJson(stored as Record<string, JsonValue>);
  }
  return eventFromJson(stored);
}

function parseSubscriptionRow(row: SubscriptionRow): SubscriptionSpec {
  return JSON.parse(row.spec_json) as SubscriptionSpec;
}

function deliveryFromRow(row: DeliveryRow): DeliveryRecord {
  return {
    deliveryId: row.delivery_id,
    eventId: row.event_id,
    subscriptionId: row.subscription_id,
    recipient: JSON.parse(row.recipient_json),
    state: row.state as DeliveryRecord["state"],
    attempt: Number(row.attempt),
    availableAt: row.available_at,
    leasedAt: row.leased_at ?? undefined,
    leaseExpiresAt: row.lease_expires_at ?? undefined,
    leaseToken: row.lease_token ?? undefined,
    acknowledgedAt: row.acknowledged_at ?? undefined,
    lastError: row.last_error ?? undefined,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    redelivered: Boolean(row.redelivered),
    routePolicyId: row.route_policy_id,
    routeReason: row.route_reason,
  };
}

function recipientKey(kind: string, id: string): string {
  return `${kind}:${id}`;
}

function matchesPattern(value: string, pattern: string): boolean {
  if (pattern === "*") return true;
  const escaped = pattern.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replaceAll("*", ".*");
  return new RegExp(`^${escaped}$`).test(value);
}

function subscriptionMatchesEvent(spec: SubscriptionSpec, event: RuntimeEventEnvelope): boolean {
  if (!spec.enabled) return false;
  if (spec.eventTypes.length > 0 && !spec.eventTypes.some((pattern) => matchesPattern(event.eventType, pattern))) return false;
  if (spec.intents.length > 0 && !spec.intents.includes(event.intent)) return false;
  if (spec.aggregatePrefixes.length > 0 && !spec.aggregatePrefixes.some((prefix) => event.aggregateId.startsWith(prefix))) return false;
  if (spec.taskIds.length > 0 && !spec.taskIds.includes(event.identity.taskId)) return false;
  return true;
}

export class RuntimeEventSqliteStore {
  readonly path: string;
  readonly db: DatabaseSync;
  readonly projector: RuntimeStateProjector;
  private closed = false;

  constructor(path: string, projector = new RuntimeStateProjector()) {
    this.path = path;
    this.projector = projector;
    this.db = new DatabaseSync(path);
    this.initialize();
  }

  initialize(): void {
    this.assertOpen();
    this.db.exec("PRAGMA journal_mode = WAL");
    this.db.exec("PRAGMA synchronous = FULL");
    this.db.exec("PRAGMA foreign_keys = ON");
    this.db.exec("PRAGMA busy_timeout = 5000");
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS runtime_event_aggregates (
        aggregate_id TEXT PRIMARY KEY,
        owner_id TEXT,
        latest_sequence INTEGER NOT NULL,
        latest_event_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS runtime_events (
        event_id TEXT PRIMARY KEY,
        aggregate_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        global_sequence INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        event_version INTEGER NOT NULL,
        run_id TEXT NOT NULL,
        session_id TEXT,
        task_id TEXT NOT NULL,
        worker_id TEXT,
        intent TEXT NOT NULL,
        effect TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        correlation_id TEXT NOT NULL,
        causation_id TEXT,
        created_at TEXT NOT NULL,
        committed_at TEXT NOT NULL,
        draft_digest TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        envelope_json TEXT NOT NULL,
        source_bytes INTEGER NOT NULL,
        inline_bytes INTEGER NOT NULL,
        envelope_bytes INTEGER NOT NULL,
        artifact_ref_count INTEGER NOT NULL,
        FOREIGN KEY (aggregate_id) REFERENCES runtime_event_aggregates(aggregate_id),
        UNIQUE (aggregate_id, sequence),
        UNIQUE (aggregate_id, idempotency_key),
        UNIQUE (global_sequence)
      );

      CREATE TABLE IF NOT EXISTS runtime_event_global_sequence (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        latest_sequence INTEGER NOT NULL
      );

      CREATE INDEX IF NOT EXISTS idx_runtime_events_task_sequence
        ON runtime_events(task_id, aggregate_id, sequence);
      CREATE INDEX IF NOT EXISTS idx_runtime_events_run_created
        ON runtime_events(run_id, created_at, event_id);
      CREATE INDEX IF NOT EXISTS idx_runtime_events_type_created
        ON runtime_events(event_type, created_at, event_id);
      CREATE INDEX IF NOT EXISTS idx_runtime_events_correlation
        ON runtime_events(correlation_id, aggregate_id, sequence);
      CREATE INDEX IF NOT EXISTS idx_runtime_events_causation
        ON runtime_events(causation_id);

      CREATE TABLE IF NOT EXISTS runtime_event_routes (
        event_id TEXT PRIMARY KEY,
        policy_id TEXT NOT NULL,
        broadcast INTEGER NOT NULL,
        fanout_reason TEXT,
        recipient_count INTEGER NOT NULL,
        available_recipient_count INTEGER NOT NULL,
        route_digest TEXT NOT NULL,
        route_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (event_id) REFERENCES runtime_events(event_id)
      );

      CREATE TABLE IF NOT EXISTS runtime_event_subscriptions (
        subscription_id TEXT PRIMARY KEY,
        recipient_kind TEXT NOT NULL,
        recipient_id TEXT NOT NULL,
        spec_json TEXT NOT NULL,
        enabled INTEGER NOT NULL,
        capacity INTEGER NOT NULL,
        max_attempts INTEGER NOT NULL,
        ack_timeout_ms INTEGER NOT NULL,
        backpressure_mode TEXT NOT NULL,
        priority INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );

      CREATE INDEX IF NOT EXISTS idx_runtime_subscriptions_recipient
        ON runtime_event_subscriptions(recipient_kind, recipient_id, enabled, priority);

      CREATE TABLE IF NOT EXISTS runtime_event_deliveries (
        delivery_id TEXT PRIMARY KEY,
        event_id TEXT NOT NULL,
        subscription_id TEXT NOT NULL,
        recipient_kind TEXT NOT NULL,
        recipient_id TEXT NOT NULL,
        recipient_json TEXT NOT NULL,
        state TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        available_at TEXT NOT NULL,
        leased_at TEXT,
        lease_expires_at TEXT,
        lease_token TEXT,
        acknowledged_at TEXT,
        last_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        redelivered INTEGER NOT NULL DEFAULT 0,
        route_policy_id TEXT NOT NULL,
        route_reason TEXT NOT NULL,
        FOREIGN KEY (event_id) REFERENCES runtime_events(event_id),
        FOREIGN KEY (subscription_id) REFERENCES runtime_event_subscriptions(subscription_id),
        UNIQUE (event_id, subscription_id)
      );

      CREATE INDEX IF NOT EXISTS idx_runtime_deliveries_poll
        ON runtime_event_deliveries(subscription_id, state, available_at, created_at);

      CREATE TABLE IF NOT EXISTS runtime_event_dead_letters (
        dead_letter_id TEXT PRIMARY KEY,
        delivery_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        subscription_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        delivery_json TEXT NOT NULL
      );

      CREATE TABLE IF NOT EXISTS runtime_event_metrics (
        metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT,
        metric_kind TEXT NOT NULL,
        value REAL NOT NULL,
        dimensions_json TEXT NOT NULL,
        created_at TEXT NOT NULL
      );

      CREATE INDEX IF NOT EXISTS idx_runtime_event_metrics_kind
        ON runtime_event_metrics(metric_kind, created_at);

      CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        node_id TEXT,
        event_type TEXT NOT NULL,
        created_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
      );
    `);
    this.ensureGlobalSequenceSchema();
    this.projector.initialize(this.db);
  }

  append(input: StoreAppendInput): AppendReceipt {
    this.assertOpen();
    const draft = parseRuntimeEventDraft(input.draft);
    const options = input.options ?? {};
    if (draft.durability === EventDurability.LIVE_ONLY) {
      throw new EventSpineError({
        code: EventSpineErrorCode.INVALID_ENVELOPE,
        message: "live-only event cannot be appended to RuntimeEventStore",
        details: { event_type: draft.eventType },
      });
    }
    const draftDigest = this.draftDigest(draft);
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const duplicate = this.findIdempotent(draft.aggregateId, draft.idempotencyKey);
      if (duplicate) {
        if (duplicate.draft_digest !== draftDigest) {
          throw new AggregateConflictError(EventSpineErrorCode.IDEMPOTENCY_CONFLICT, "idempotency key reused with different event draft", {
            aggregate_id: draft.aggregateId,
            idempotency_key: draft.idempotencyKey,
            existing_event_id: duplicate.event_id,
          });
        }
        const event = parseEventRow(duplicate);
        this.db.exec("COMMIT");
        this.recordMetric(event.eventId, "event.duplicate", 1, { event_type: event.eventType });
        const cursor = this.projector.cursor(this.db, event.aggregateId);
        return {
          event,
          committed: false,
          duplicate: true,
          projected: Boolean(cursor && cursor.sequence >= event.aggregateSequence),
          routed: false,
          deliveryIds: this.deliveryIdsForEvent(event.eventId),
          artifactSpillCount: input.artifactSpillCount,
          warnings: input.warnings,
        };
      }
      const eventIdConflict = this.eventRow(draft.eventId ?? "");
      if (eventIdConflict) {
        throw new AggregateConflictError(EventSpineErrorCode.EVENT_ID_CONFLICT, "event id already exists", {
          event_id: draft.eventId,
          aggregate_id: eventIdConflict.aggregate_id,
          sequence: eventIdConflict.sequence,
        });
      }
      const aggregate = this.aggregateRow(draft.aggregateId);
      this.assertOwner(aggregate, draft.aggregateId, options.ownerId, options.strictOwner ?? false);
      const expectedSequence = aggregate ? Number(aggregate.latest_sequence) + 1 : 0;
      if (draft.expectedSequence !== undefined && draft.expectedSequence !== expectedSequence) {
        throw new AggregateConflictError(EventSpineErrorCode.AGGREGATE_SEQUENCE_CONFLICT, "aggregate sequence precondition failed", {
          aggregate_id: draft.aggregateId,
          expected_sequence: draft.expectedSequence,
          actual_next_sequence: expectedSequence,
        });
      }
      this.assertCausation(draft);
      const committedAt = utcNow();
      const globalSequence = this.allocateGlobalSequence();
      let envelope = buildCommittedEnvelope(draft, expectedSequence, globalSequence, committedAt, input.inlineBytes, input.estimatedEnvelopeBytes);
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const actualBytes = Buffer.byteLength(canonicalJson(envelope), "utf8");
        if (actualBytes === envelope.envelopeBytes) break;
        envelope = buildCommittedEnvelope(draft, expectedSequence, globalSequence, committedAt, input.inlineBytes, actualBytes);
      }
      this.assertEnvelopeBudgets(envelope);
      validateEventKind(
        envelope.eventType,
        envelope.inline,
        envelope.durability,
        envelope.effect,
        envelope.causationId,
        envelope.sender.kind,
        envelope.intent,
        envelope.eventVersion,
      );
      this.upsertAggregate(draft.aggregateId, options.ownerId, aggregate, expectedSequence, envelope.eventId, committedAt);
      this.insertEvent(envelope, draftDigest);
      if (input.route) this.insertRouteDecision(input.route);
      this.db.exec("COMMIT");
      const maintenanceWarnings = [...input.warnings];
      // Projectors are durable message-bus consumers.  This store owns only
      // canonical facts and the legacy compatibility row; it must never advance
      // a query model ahead of delivery/ACK.
      if (options.publishLegacyProjection !== false && input.legacyProjection) {
        this.db.exec("BEGIN IMMEDIATE");
        try {
          if (input.legacyProjection) this.insertLegacyProjection(input.legacyProjection);
          this.db.exec("COMMIT");
        } catch (error) {
          try { this.db.exec("ROLLBACK"); } catch { /* preserve maintenance error */ }
          maintenanceWarnings.push(`projector_pending:${asEventSpineError(error).code}`);
        }
      }
      let deliveryIds: readonly string[] = [];
      if (input.route && input.subscriptionsByRecipient) {
        try {
          deliveryIds = this.enqueueDeliveries(envelope, input.route, input.subscriptionsByRecipient);
        } catch (error) {
          maintenanceWarnings.push(`delivery_pending:${asEventSpineError(error).code}`);
        }
      }
      try {
        this.recordAppendMetrics(envelope, input.route, input.artifactSpillCount, input.offloadedBytes ?? 0, false);
      } catch (error) {
        maintenanceWarnings.push(`metrics_pending:${asEventSpineError(error).code}`);
      }
      return {
        event: envelope,
        committed: true,
        duplicate: false,
        projected: false,
        routed: deliveryIds.length > 0,
        route: input.route,
        deliveryIds,
        artifactSpillCount: input.artifactSpillCount,
        warnings: maintenanceWarnings,
      };
    } catch (error) {
      try {
        this.db.exec("ROLLBACK");
      } catch {
        // Preserve original transactional failure.
      }
      throw asEventSpineError(error, "runtime event append failed");
    }
  }

  replay(events: readonly RuntimeEventEnvelope[], options: AppendOptions = {}): readonly AppendReceipt[] {
    if (events.length === 0) return [];
    const aggregateId = events[0]!.aggregateId;
    if (events.some((event) => event.aggregateId !== aggregateId)) {
      throw new AggregateConflictError(EventSpineErrorCode.REPLAY_DIVERGENCE, "replay batch contains multiple aggregates", { aggregate_id: aggregateId });
    }
    const ordered = [...events].sort((left, right) => left.aggregateSequence - right.aggregateSequence);
    const receipts: AppendReceipt[] = [];
    for (let index = 0; index < ordered.length; index += 1) {
      const event = ordered[index]!;
      if (index > 0 && event.aggregateSequence !== ordered[index - 1]!.aggregateSequence + 1) {
        throw new AggregateConflictError(EventSpineErrorCode.REPLAY_DIVERGENCE, "replay input sequence has a gap", {
          aggregate_id: aggregateId,
          previous: ordered[index - 1]!.aggregateSequence,
          actual: event.aggregateSequence,
        });
      }
      const existing = this.eventRow(event.eventId);
      if (existing) {
        const stored = parseEventRow(existing);
        if (stored.contentDigest !== event.contentDigest || stored.aggregateSequence !== event.aggregateSequence) {
          throw new AggregateConflictError(EventSpineErrorCode.REPLAY_DIVERGENCE, "stored replay event diverges", {
            aggregate_id: aggregateId,
            event_id: event.eventId,
            sequence: event.aggregateSequence,
          });
        }
        receipts.push({
          event: stored,
          committed: false,
          duplicate: true,
          projected: true,
          routed: false,
          deliveryIds: this.deliveryIdsForEvent(event.eventId),
          artifactSpillCount: 0,
          warnings: [],
        });
        continue;
      }
      if (event.aggregateSequence !== (this.aggregateRow(aggregateId)?.latest_sequence ?? -1) + 1) {
        throw new AggregateConflictError(EventSpineErrorCode.REPLAY_DIVERGENCE, "replay sequence does not extend aggregate", {
          aggregate_id: aggregateId,
          sequence: event.aggregateSequence,
        });
      }
      const draft: RuntimeEventDraft = {
        ...event,
        expectedSequence: event.aggregateSequence,
      };
      receipts.push(this.append({
        draft,
        inlineBytes: event.inlineBytes,
        estimatedEnvelopeBytes: event.envelopeBytes,
        artifactSpillCount: 0,
        warnings: [],
        options: { ...options, replay: true, route: false, publishLegacyProjection: false },
      }));
      if (receipts.at(-1)!.event.contentDigest !== event.contentDigest) {
        throw new AggregateConflictError(EventSpineErrorCode.REPLAY_DIVERGENCE, "replayed event digest changed", {
          event_id: event.eventId,
          expected: event.contentDigest,
          actual: receipts.at(-1)!.event.contentDigest,
        });
      }
    }
    return receipts;
  }

  query(input: EventQuery | unknown = {}): EventPage {
    this.assertOpen();
    const query = parseEventQuery(input);
    const clauses: string[] = [];
    const params: Array<string | number> = [];
    if (query.aggregateId) { clauses.push("aggregate_id = ?"); params.push(query.aggregateId); }
    if (query.runId) { clauses.push("run_id = ?"); params.push(query.runId); }
    if (query.sessionId) { clauses.push("session_id = ?"); params.push(query.sessionId); }
    if (query.taskId) { clauses.push("task_id = ?"); params.push(query.taskId); }
    if (query.workerId) { clauses.push("worker_id = ?"); params.push(query.workerId); }
    if (query.eventTypes && query.eventTypes.length > 0) {
      clauses.push(`event_type IN (${query.eventTypes.map(() => "?").join(",")})`);
      params.push(...query.eventTypes);
    }
    if (query.intents && query.intents.length > 0) {
      clauses.push(`intent IN (${query.intents.map(() => "?").join(",")})`);
      params.push(...query.intents);
    }
    if (query.effects && query.effects.length > 0) {
      clauses.push(`effect IN (${query.effects.map(() => "?").join(",")})`);
      params.push(...query.effects);
    }
    if (query.afterSequence !== undefined) { clauses.push("sequence > ?"); params.push(query.afterSequence); }
    if (query.beforeSequence !== undefined) { clauses.push("sequence < ?"); params.push(query.beforeSequence); }
    if (query.afterGlobalSequence !== undefined) { clauses.push("global_sequence > ?"); params.push(query.afterGlobalSequence); }
    if (query.beforeGlobalSequence !== undefined) { clauses.push("global_sequence < ?"); params.push(query.beforeGlobalSequence); }
    if (query.createdAtGte) { clauses.push("created_at >= ?"); params.push(query.createdAtGte); }
    if (query.createdAtLt) { clauses.push("created_at < ?"); params.push(query.createdAtLt); }
    if (query.correlationId) { clauses.push("correlation_id = ?"); params.push(query.correlationId); }
    if (query.causationId) { clauses.push("causation_id = ?"); params.push(query.causationId); }
    if (query.artifactId) {
      clauses.push("envelope_json LIKE ? ESCAPE '\\'");
      params.push(`%\"artifactId\":\"${query.artifactId.replaceAll("\\", "\\\\").replaceAll("%", "\\%").replaceAll("_", "\\_")}\"%`);
    }
    if (query.cursor) {
      const cursor = decodeCursor(query.cursor);
      const sequence = requireInteger(cursor.global_sequence, "cursor.global_sequence", 1);
      clauses.push(query.descending ? "global_sequence < ?" : "global_sequence > ?");
      params.push(sequence);
    }
    const where = clauses.length > 0 ? `WHERE ${clauses.join(" AND ")}` : "";
    const order = query.descending ? "DESC" : "ASC";
    const limit = query.limit ?? 100;
    const rows = this.db.prepare(`
      SELECT * FROM runtime_events
      ${where}
      ORDER BY global_sequence ${order}
      LIMIT ?
    `).all(...params, limit + 1) as unknown as EventRow[];
    const page = rows.slice(0, limit);
    const items = page.map(parseEventRow);
    const last = page.at(-1);
    return {
      items,
      hasMore: rows.length > limit,
      nextCursor: rows.length > limit && last ? encodeCursor({ global_sequence: Number(last.global_sequence) }) : undefined,
      nextSequence: Number(last?.global_sequence ?? query.afterGlobalSequence ?? 0),
      scanned: rows.length,
      highWatermark: this.latestGlobalSequence(),
    };
  }

  get(eventId: string): RuntimeEventEnvelope | undefined {
    const row = this.eventRow(eventId);
    return row ? parseEventRow(row) : undefined;
  }

  latestSequence(aggregateId: string): number {
    return Number(this.aggregateRow(aggregateId)?.latest_sequence ?? -1);
  }

  latestGlobalSequence(): number {
    const row = this.db.prepare("SELECT latest_sequence FROM runtime_event_global_sequence WHERE singleton = 1").get() as { latest_sequence: number } | undefined;
    return Number(row?.latest_sequence ?? 0);
  }

  claim(aggregateId: string, ownerId: string, strict = true): void {
    this.assertOpen();
    const owner = requireString(ownerId, "ownerId", 256);
    const now = utcNow();
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const row = this.aggregateRow(aggregateId);
      if (!row) {
        this.db.prepare(`
          INSERT INTO runtime_event_aggregates (
            aggregate_id, owner_id, latest_sequence, latest_event_id, created_at, updated_at
          ) VALUES (?, ?, -1, NULL, ?, ?)
        `).run(aggregateId, owner, now, now);
      } else if (row.owner_id && row.owner_id !== owner && strict) {
        throw new AggregateConflictError(EventSpineErrorCode.AGGREGATE_OWNER_MISMATCH, "aggregate already claimed", {
          aggregate_id: aggregateId,
          expected_owner: row.owner_id,
          actual_owner: owner,
        });
      } else if (!row.owner_id) {
        this.db.prepare("UPDATE runtime_event_aggregates SET owner_id = ?, updated_at = ? WHERE aggregate_id = ?").run(owner, now, aggregateId);
      }
      this.db.exec("COMMIT");
    } catch (error) {
      try { this.db.exec("ROLLBACK"); } catch { /* preserve original */ }
      throw error;
    }
  }

  registerSubscription(spec: SubscriptionSpec): SubscriptionSpec {
    this.assertOpen();
    const now = utcNow();
    const existing = this.subscription(spec.subscriptionId);
    if (existing && digestJson(existing) !== digestJson(spec)) {
      const pending = this.pendingCount(spec.subscriptionId);
      if (pending > 0) {
        throw new RoutingError(EventSpineErrorCode.SUBSCRIBER_BACKPRESSURE, "cannot replace subscription with pending deliveries", {
          subscription_id: spec.subscriptionId,
          pending,
        });
      }
    }
    this.db.prepare(`
      INSERT INTO runtime_event_subscriptions (
        subscription_id, recipient_kind, recipient_id, spec_json, enabled,
        capacity, max_attempts, ack_timeout_ms, backpressure_mode, priority,
        created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(subscription_id) DO UPDATE SET
        recipient_kind = excluded.recipient_kind,
        recipient_id = excluded.recipient_id,
        spec_json = excluded.spec_json,
        enabled = excluded.enabled,
        capacity = excluded.capacity,
        max_attempts = excluded.max_attempts,
        ack_timeout_ms = excluded.ack_timeout_ms,
        backpressure_mode = excluded.backpressure_mode,
        priority = excluded.priority,
        updated_at = excluded.updated_at
    `).run(
      spec.subscriptionId,
      spec.recipient.kind,
      spec.recipient.id,
      canonicalJson(subscriptionToJson(spec)),
      spec.enabled ? 1 : 0,
      spec.capacity,
      spec.maxAttempts,
      spec.ackTimeoutMs,
      spec.backpressureMode,
      spec.priority,
      existing ? now : now,
      now,
    );
    return spec;
  }

  subscription(subscriptionId: string): SubscriptionSpec | undefined {
    const row = this.db.prepare("SELECT * FROM runtime_event_subscriptions WHERE subscription_id = ?").get(subscriptionId) as SubscriptionRow | undefined;
    return row ? parseSubscriptionRow(row) : undefined;
  }

  subscriptions(enabledOnly = true): readonly SubscriptionSpec[] {
    const rows = this.db.prepare(`
      SELECT * FROM runtime_event_subscriptions
      ${enabledOnly ? "WHERE enabled = 1" : ""}
      ORDER BY priority ASC, subscription_id ASC
    `).all() as unknown as SubscriptionRow[];
    return rows.map(parseSubscriptionRow);
  }

  pendingCount(subscriptionId: string): number {
    const row = this.db.prepare(`
      SELECT COUNT(*) AS count
      FROM runtime_event_deliveries
      WHERE subscription_id = ? AND state IN (?, ?, ?)
    `).get(subscriptionId, DeliveryState.PENDING, DeliveryState.LEASED, DeliveryState.RETRY_WAIT) as { count: number };
    return Number(row.count);
  }

  pendingBySubscription(): ReadonlyMap<string, number> {
    const rows = this.db.prepare(`
      SELECT subscription_id, COUNT(*) AS count
      FROM runtime_event_deliveries
      WHERE state IN (?, ?, ?)
      GROUP BY subscription_id
    `).all(DeliveryState.PENDING, DeliveryState.LEASED, DeliveryState.RETRY_WAIT) as unknown as Array<{ subscription_id: string; count: number }>;
    return new Map(rows.map((row) => [row.subscription_id, Number(row.count)]));
  }

  subscriptionsByRecipient(): ReadonlyMap<string, readonly SubscriptionSpec[]> {
    const grouped = new Map<string, SubscriptionSpec[]>();
    for (const spec of this.subscriptions(true)) {
      const key = recipientKey(spec.recipient.kind, spec.recipient.id);
      grouped.set(key, [...(grouped.get(key) ?? []), spec]);
    }
    return grouped;
  }

  lease(subscriptionId: string, limit = 1, now = utcNow()): readonly DeliveryRecord[] {
    const spec = this.subscription(subscriptionId);
    if (!spec || !spec.enabled) {
      throw new DeliveryError(EventSpineErrorCode.SUBSCRIBER_NOT_FOUND, "subscription not found or disabled", { subscription_id: subscriptionId });
    }
    this.sweepExpired(now, subscriptionId);
    const selected = this.db.prepare(`
      SELECT * FROM runtime_event_deliveries
      WHERE subscription_id = ?
        AND state IN (?, ?)
        AND available_at <= ?
      ORDER BY created_at ASC, delivery_id ASC
      LIMIT ?
    `).all(subscriptionId, DeliveryState.PENDING, DeliveryState.RETRY_WAIT, now, Math.max(1, Math.min(limit, 100))) as unknown as DeliveryRow[];
    const output: DeliveryRecord[] = [];
    this.db.exec("BEGIN IMMEDIATE");
    try {
      for (const row of selected) {
        const token = newToken();
        const leasedAt = now;
        const leaseExpiresAt = new Date(new Date(now).getTime() + spec.ackTimeoutMs).toISOString();
        const result = this.db.prepare(`
          UPDATE runtime_event_deliveries
          SET state = ?, attempt = attempt + 1, leased_at = ?, lease_expires_at = ?,
              lease_token = ?, updated_at = ?, redelivered = CASE WHEN attempt > 0 THEN 1 ELSE redelivered END
          WHERE delivery_id = ? AND state IN (?, ?)
        `).run(
          DeliveryState.LEASED,
          leasedAt,
          leaseExpiresAt,
          token,
          now,
          row.delivery_id,
          DeliveryState.PENDING,
          DeliveryState.RETRY_WAIT,
        );
        if (Number(result.changes) !== 1) continue;
        const leased = this.delivery(row.delivery_id);
        if (leased) output.push(leased);
      }
      this.db.exec("COMMIT");
      return output;
    } catch (error) {
      try { this.db.exec("ROLLBACK"); } catch { /* preserve original */ }
      throw error;
    }
  }

  acknowledge(deliveryId: string, leaseToken: string, now = utcNow()): DeliveryRecord {
    const row = this.deliveryRow(deliveryId);
    if (!row) throw new DeliveryError(EventSpineErrorCode.DELIVERY_NOT_FOUND, "delivery not found", { delivery_id: deliveryId });
    if (row.state === DeliveryState.ACKNOWLEDGED) return deliveryFromRow(row);
    if (row.state !== DeliveryState.LEASED) {
      throw new DeliveryError(EventSpineErrorCode.DELIVERY_TERMINAL, "delivery is not leased", { delivery_id: deliveryId, state: row.state });
    }
    if (row.lease_token !== leaseToken) {
      throw new DeliveryError(EventSpineErrorCode.DELIVERY_LEASE_MISMATCH, "delivery lease token mismatch", { delivery_id: deliveryId });
    }
    if (row.lease_expires_at && row.lease_expires_at < now) {
      throw new DeliveryError(EventSpineErrorCode.DELIVERY_LEASE_MISMATCH, "delivery lease expired", { delivery_id: deliveryId, lease_expires_at: row.lease_expires_at });
    }
    this.db.prepare(`
      UPDATE runtime_event_deliveries
      SET state = ?, acknowledged_at = ?, updated_at = ?, lease_token = NULL,
          leased_at = NULL, lease_expires_at = NULL
      WHERE delivery_id = ?
    `).run(DeliveryState.ACKNOWLEDGED, now, now, deliveryId);
    this.recordMetric(row.event_id, "delivery.acknowledged", 1, { subscription_id: row.subscription_id });
    return this.delivery(deliveryId)!;
  }

  negativeAcknowledge(deliveryId: string, leaseToken: string, error: string, delayMs = 0, now = utcNow()): DeliveryRecord {
    const row = this.deliveryRow(deliveryId);
    if (!row) throw new DeliveryError(EventSpineErrorCode.DELIVERY_NOT_FOUND, "delivery not found", { delivery_id: deliveryId });
    if (row.state !== DeliveryState.LEASED || row.lease_token !== leaseToken) {
      throw new DeliveryError(EventSpineErrorCode.DELIVERY_LEASE_MISMATCH, "delivery cannot be nacked without active lease", { delivery_id: deliveryId, state: row.state });
    }
    const spec = this.subscription(row.subscription_id);
    if (!spec) throw new DeliveryError(EventSpineErrorCode.SUBSCRIBER_NOT_FOUND, "delivery subscription missing", { subscription_id: row.subscription_id });
    if (row.attempt >= spec.maxAttempts) return this.deadLetter(row, error || "max_attempts_exhausted", now);
    const availableAt = new Date(new Date(now).getTime() + Math.max(0, delayMs)).toISOString();
    this.db.prepare(`
      UPDATE runtime_event_deliveries
      SET state = ?, available_at = ?, leased_at = NULL, lease_expires_at = NULL,
          lease_token = NULL, last_error = ?, updated_at = ?, redelivered = 1
      WHERE delivery_id = ?
    `).run(DeliveryState.RETRY_WAIT, availableAt, error.slice(0, 2000), now, deliveryId);
    this.recordMetric(row.event_id, "delivery.nack", 1, { subscription_id: row.subscription_id });
    return this.delivery(deliveryId)!;
  }

  sweepExpired(now = utcNow(), subscriptionId?: string): number {
    const clauses = ["state = ?", "lease_expires_at IS NOT NULL", "lease_expires_at < ?"];
    const params: Array<string> = [DeliveryState.LEASED, now];
    if (subscriptionId) { clauses.push("subscription_id = ?"); params.push(subscriptionId); }
    const rows = this.db.prepare(`SELECT * FROM runtime_event_deliveries WHERE ${clauses.join(" AND ")}`).all(...params) as unknown as DeliveryRow[];
    let swept = 0;
    for (const row of rows) {
      const spec = this.subscription(row.subscription_id);
      if (!spec || row.attempt >= spec.maxAttempts) this.deadLetter(row, "ack_timeout_exhausted", now);
      else {
        this.db.prepare(`
          UPDATE runtime_event_deliveries
          SET state = ?, available_at = ?, leased_at = NULL, lease_expires_at = NULL,
              lease_token = NULL, last_error = ?, updated_at = ?, redelivered = 1
          WHERE delivery_id = ? AND state = ?
        `).run(DeliveryState.RETRY_WAIT, now, "ack_timeout", now, row.delivery_id, DeliveryState.LEASED);
      }
      swept += 1;
    }
    return swept;
  }

  delivery(deliveryId: string): DeliveryRecord | undefined {
    const row = this.deliveryRow(deliveryId);
    return row ? deliveryFromRow(row) : undefined;
  }

  deliveriesForEvent(eventId: string): readonly DeliveryRecord[] {
    const rows = this.db.prepare("SELECT * FROM runtime_event_deliveries WHERE event_id = ? ORDER BY subscription_id").all(eventId) as unknown as DeliveryRow[];
    return rows.map(deliveryFromRow);
  }

  persistedRoute(eventId: string): RouteDecision | undefined {
    const row = this.db.prepare(`
      SELECT route_json FROM runtime_event_routes WHERE event_id = ?
    `).get(eventId) as { route_json: string } | undefined;
    return row ? JSON.parse(row.route_json) as RouteDecision : undefined;
  }

  /**
   * Enqueue one delivery without changing the canonical route fact.  Normal
   * catch-up must first verify the recipient against `persistedRoute`; the
   * dedicated projector consumer is allowed to use `internalReason` because it
   * is a read-side maintenance subscriber, never a business recipient.
   */
  enqueueForSubscription(
    event: RuntimeEventEnvelope,
    spec: SubscriptionSpec,
    policyId: string,
    internalReason: string,
  ): readonly string[] {
    if (this.deliveriesForEvent(event.eventId).some((item) => item.subscriptionId === spec.subscriptionId)) {
      return [];
    }
    const route: RouteDecision = {
      eventId: event.eventId,
      policyId,
      explicitTarget: true,
      broadcast: false,
      recipients: [spec.recipient],
      candidates: [{ recipient: spec.recipient, score: 1, reasons: [internalReason], rejectedReasons: [] }],
      routeDensity: 1,
      availableRecipientCount: 1,
      createdAt: utcNow(),
    };
    return this.enqueueDeliveries(
      event,
      route,
      new Map([[recipientKey(spec.recipient.kind, spec.recipient.id), [spec]]]),
    );
  }

  reopenAcknowledgedDelivery(eventId: string, subscriptionId: string, reason: string): boolean {
    const now = utcNow();
    const result = this.db.prepare(`
      UPDATE runtime_event_deliveries
      SET state = ?, available_at = ?, leased_at = NULL, lease_expires_at = NULL,
          lease_token = NULL, acknowledged_at = NULL, last_error = ?,
          updated_at = ?, redelivered = 1
      WHERE event_id = ? AND subscription_id = ? AND state = ?
    `).run(
      DeliveryState.PENDING,
      now,
      reason.slice(0, 2000),
      now,
      eventId,
      subscriptionId,
      DeliveryState.ACKNOWLEDGED,
    );
    return Number(result.changes) > 0;
  }

  /** Persist delivery work for an already committed canonical event. */
  enqueueDeliveries(
    event: RuntimeEventEnvelope,
    route: RouteDecision,
    subscriptionsByRecipient: ReadonlyMap<string, readonly SubscriptionSpec[]>,
  ): readonly string[] {
    this.assertOpen();
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const deliveryIds = this.insertDeliveries(event, route, subscriptionsByRecipient);
      this.db.exec("COMMIT");
      return deliveryIds;
    } catch (error) {
      try { this.db.exec("ROLLBACK"); } catch { /* preserve delivery failure */ }
      throw asEventSpineError(error, "runtime event delivery enqueue failed");
    }
  }

  deliveryIdsForEvent(eventId: string): readonly string[] {
    return this.deliveriesForEvent(eventId).map((item) => item.deliveryId);
  }

  recordMetric(eventId: string | undefined, kind: string, value: number, dimensions: Record<string, JsonValue> = {}): void {
    this.db.prepare(`
      INSERT INTO runtime_event_metrics (event_id, metric_kind, value, dimensions_json, created_at)
      VALUES (?, ?, ?, ?, ?)
    `).run(eventId ?? null, kind, value, canonicalJson(dimensions), utcNow());
  }

  metricRows(kind?: string): readonly { eventId?: string; kind: string; value: number; dimensions: Record<string, JsonValue>; createdAt: string }[] {
    const rows = this.db.prepare(`
      SELECT event_id, metric_kind, value, dimensions_json, created_at
      FROM runtime_event_metrics
      ${kind ? "WHERE metric_kind = ?" : ""}
      ORDER BY metric_id ASC
    `).all(...(kind ? [kind] : [])) as unknown as Array<{ event_id: string | null; metric_kind: string; value: number; dimensions_json: string; created_at: string }>;
    return rows.map((row) => ({
      eventId: row.event_id ?? undefined,
      kind: row.metric_kind,
      value: Number(row.value),
      dimensions: JSON.parse(row.dimensions_json),
      createdAt: row.created_at,
    }));
  }

  health(): StoreHealth {
    const scalar = (sql: string): number => Number((this.db.prepare(sql).get() as { count: number }).count);
    return {
      open: !this.closed,
      path: this.path,
      eventCount: scalar("SELECT COUNT(*) AS count FROM runtime_events"),
      highWatermark: this.latestGlobalSequence(),
      aggregateCount: scalar("SELECT COUNT(*) AS count FROM runtime_event_aggregates"),
      subscriptionCount: scalar("SELECT COUNT(*) AS count FROM runtime_event_subscriptions WHERE enabled = 1"),
      pendingDeliveryCount: scalar(`SELECT COUNT(*) AS count FROM runtime_event_deliveries WHERE state IN ('pending','leased','retry_wait')`),
      deadLetterCount: scalar("SELECT COUNT(*) AS count FROM runtime_event_dead_letters"),
      projectorId: this.projector.projectorId,
    };
  }

  close(): void {
    if (this.closed) return;
    this.db.close();
    this.closed = true;
  }

  private ensureGlobalSequenceSchema(): void {
    const columns = this.db.prepare("PRAGMA table_info(runtime_events)").all() as unknown as Array<{ name: string }>;
    if (!columns.some((column) => column.name === "global_sequence")) {
      this.db.exec("ALTER TABLE runtime_events ADD COLUMN global_sequence INTEGER");
    }
    this.db.exec(`
      UPDATE runtime_events SET global_sequence = rowid WHERE global_sequence IS NULL;
      CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_events_global_sequence
        ON runtime_events(global_sequence);
      INSERT INTO runtime_event_global_sequence (singleton, latest_sequence)
      VALUES (1, COALESCE((SELECT MAX(global_sequence) FROM runtime_events), 0))
      ON CONFLICT(singleton) DO UPDATE SET latest_sequence = MAX(
        runtime_event_global_sequence.latest_sequence,
        excluded.latest_sequence
      );
    `);
  }

  private assertEnvelopeBudgets(event: RuntimeEventEnvelope): void {
    const actualEnvelopeBytes = Buffer.byteLength(canonicalJson(event), "utf8");
    const actualInlineBytes = Buffer.byteLength(canonicalJson(event.inline), "utf8");
    const summaryBytes = Buffer.byteLength(event.summary, "utf8");
    if (actualInlineBytes > 4 * 1024 || event.inlineBytes !== actualInlineBytes) {
      throw new PayloadBudgetError(EventSpineErrorCode.INLINE_PAYLOAD_TOO_LARGE, "inline payload exceeds 4 KiB canonical limit", {
        inline_bytes: actualInlineBytes,
        declared_inline_bytes: event.inlineBytes,
      });
    }
    if (summaryBytes > 1024) {
      throw new PayloadBudgetError(EventSpineErrorCode.SUMMARY_TOO_LARGE, "event summary exceeds 1 KiB canonical limit", {
        summary_bytes: summaryBytes,
      });
    }
    if (event.artifactRefs.length + event.evidenceRefs.length > 64) {
      throw new PayloadBudgetError(EventSpineErrorCode.TOO_MANY_REFS, "event reference count exceeds canonical limit", {
        reference_count: event.artifactRefs.length + event.evidenceRefs.length,
      });
    }
    if (actualEnvelopeBytes > 8 * 1024) {
      throw new PayloadBudgetError(EventSpineErrorCode.EVENT_TOO_LARGE, "serialized event exceeds 8 KiB canonical limit", {
        envelope_bytes: actualEnvelopeBytes,
      });
    }
  }

  private allocateGlobalSequence(): number {
    this.db.prepare(`
      INSERT INTO runtime_event_global_sequence (singleton, latest_sequence)
      VALUES (1, 1)
      ON CONFLICT(singleton) DO UPDATE SET latest_sequence = latest_sequence + 1
    `).run();
    return this.latestGlobalSequence();
  }

  private assertOpen(): void {
    if (this.closed) throw new EventSpineError({ code: EventSpineErrorCode.RUNTIME_CLOSED, message: "runtime event store is closed" });
  }

  private aggregateRow(aggregateId: string): AggregateRow | undefined {
    return this.db.prepare("SELECT * FROM runtime_event_aggregates WHERE aggregate_id = ?").get(aggregateId) as AggregateRow | undefined;
  }

  private eventRow(eventId: string): EventRow | undefined {
    if (!eventId) return undefined;
    return this.db.prepare("SELECT * FROM runtime_events WHERE event_id = ?").get(eventId) as EventRow | undefined;
  }

  private findIdempotent(aggregateId: string, key: string): EventRow | undefined {
    return this.db.prepare("SELECT * FROM runtime_events WHERE aggregate_id = ? AND idempotency_key = ?").get(aggregateId, key) as EventRow | undefined;
  }

  private assertOwner(row: AggregateRow | undefined, aggregateId: string, ownerId: string | undefined, strict: boolean): void {
    if (!row?.owner_id) return;
    if (row.owner_id === ownerId) return;
    if (!strict && ownerId === undefined) return;
    throw new AggregateConflictError(EventSpineErrorCode.AGGREGATE_OWNER_MISMATCH, "aggregate owner mismatch", {
      aggregate_id: aggregateId,
      expected_owner: row.owner_id,
      actual_owner: ownerId ?? "",
    });
  }

  private assertCausation(draft: RuntimeEventDraft): void {
    if (!draft.causationId) return;
    const cause = this.eventRow(draft.causationId);
    if (!cause) {
      throw new EventSpineError({
        code: EventSpineErrorCode.CAUSATION_MISSING,
        message: "causation event does not exist",
        details: { event_type: draft.eventType, causation_id: draft.causationId },
      });
    }
    if (cause.correlation_id !== draft.correlationId) {
      throw new EventSpineError({
        code: EventSpineErrorCode.CORRELATION_MISMATCH,
        message: "causation event belongs to another correlation",
        details: {
          causation_id: draft.causationId,
          cause_correlation_id: cause.correlation_id,
          event_correlation_id: draft.correlationId,
        },
      });
    }
  }

  private draftDigest(draft: RuntimeEventDraft): string {
    const { expectedSequence: _expectedSequence, ...identity } = draft;
    return digestJson(JSON.parse(JSON.stringify(identity)) as JsonValue);
  }

  private upsertAggregate(
    aggregateId: string,
    ownerId: string | undefined,
    existing: AggregateRow | undefined,
    sequence: number,
    eventId: string,
    now: string,
  ): void {
    this.db.prepare(`
      INSERT INTO runtime_event_aggregates (
        aggregate_id, owner_id, latest_sequence, latest_event_id, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?)
      ON CONFLICT(aggregate_id) DO UPDATE SET
        owner_id = COALESCE(runtime_event_aggregates.owner_id, excluded.owner_id),
        latest_sequence = excluded.latest_sequence,
        latest_event_id = excluded.latest_event_id,
        updated_at = excluded.updated_at
    `).run(aggregateId, ownerId ?? existing?.owner_id ?? null, sequence, eventId, existing?.created_at ?? now, now);
  }

  private insertEvent(event: RuntimeEventEnvelope, draftDigest: string): void {
    this.db.prepare(`
      INSERT INTO runtime_events (
        event_id, aggregate_id, sequence, global_sequence, event_type, event_version, run_id,
        session_id, task_id, worker_id, intent, effect, idempotency_key,
        correlation_id, causation_id, created_at, committed_at, draft_digest,
        content_digest, envelope_json, source_bytes, inline_bytes, envelope_bytes,
        artifact_ref_count
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      event.eventId,
      event.aggregateId,
      event.aggregateSequence,
      event.globalSequence,
      event.eventType,
      event.eventVersion,
      event.identity.runId,
      event.identity.sessionId ?? null,
      event.identity.taskId,
      event.identity.workerId ?? null,
      event.intent,
      event.effect,
      event.idempotencyKey,
      event.correlationId,
      event.causationId ?? null,
      event.createdAt,
      event.committedAt,
      draftDigest,
      event.contentDigest,
      canonicalJson(eventToJson(event)),
      event.sourceBytes,
      event.inlineBytes,
      event.envelopeBytes,
      event.artifactRefs.length,
    );
  }

  private insertLegacyProjection(value: NonNullable<StoreAppendInput["legacyProjection"]>): void {
    this.db.prepare(`
      INSERT INTO events (
        event_id, run_id, task_id, node_id, event_type, created_at, payload_json
      ) VALUES (?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(event_id) DO UPDATE SET
        run_id = excluded.run_id,
        task_id = excluded.task_id,
        node_id = excluded.node_id,
        event_type = excluded.event_type,
        created_at = excluded.created_at,
        payload_json = excluded.payload_json
    `).run(
      value.eventId,
      value.runId,
      value.taskId,
      value.nodeId ?? null,
      value.eventType,
      value.createdAt,
      canonicalJson(value.payload),
    );
  }

  private insertRouteDecision(route: RouteDecision): void {
    // Route decisions contain optional fields; persist the JSON wire form so
    // undefined process-local properties cannot leak into the canonical fact.
    const routeJson = canonicalJson(JSON.parse(JSON.stringify(route)));
    this.db.prepare(`
      INSERT INTO runtime_event_routes (
        event_id, policy_id, broadcast, fanout_reason, recipient_count,
        available_recipient_count, route_digest, route_json, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(event_id) DO NOTHING
    `).run(
      route.eventId,
      route.policyId,
      route.broadcast ? 1 : 0,
      route.fanoutReason ?? null,
      route.recipients.length,
      route.availableRecipientCount,
      digestJson(JSON.parse(routeJson)),
      routeJson,
      route.createdAt,
    );
  }

  private insertDeliveries(
    event: RuntimeEventEnvelope,
    route: RouteDecision,
    subscriptionsByRecipient: ReadonlyMap<string, readonly SubscriptionSpec[]>,
  ): readonly string[] {
    const deliveryIds: string[] = [];
    for (const recipient of route.recipients) {
      const key = recipientKey(recipient.kind, recipient.id);
      const subscriptions = (subscriptionsByRecipient.get(key) ?? []).filter((spec) => subscriptionMatchesEvent(spec, event));
      if (subscriptions.length === 0) {
        throw new RoutingError(EventSpineErrorCode.ROUTE_NOT_FOUND, "route recipient has no durable subscription", {
          event_id: event.eventId,
          recipient: key,
        });
      }
      for (const spec of subscriptions) {
        this.enforceCapacity(spec, event);
        const deliveryId = newId("delivery");
        const now = event.committedAt;
        this.db.prepare(`
          INSERT OR IGNORE INTO runtime_event_deliveries (
            delivery_id, event_id, subscription_id, recipient_kind, recipient_id,
            recipient_json, state, attempt, available_at, leased_at,
            lease_expires_at, lease_token, acknowledged_at, last_error,
            created_at, updated_at, redelivered, route_policy_id, route_reason
          ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, 0, ?, ?)
        `).run(
          deliveryId,
          event.eventId,
          spec.subscriptionId,
          recipient.kind,
          recipient.id,
          canonicalJson(recipient),
          DeliveryState.PENDING,
          now,
          now,
          now,
          route.policyId,
          route.broadcast ? route.fanoutReason ?? "system_critical" : route.explicitTarget ? "explicit_target" : "targeted_top_k",
        );
        const stored = this.db.prepare("SELECT delivery_id FROM runtime_event_deliveries WHERE event_id = ? AND subscription_id = ?").get(event.eventId, spec.subscriptionId) as { delivery_id: string };
        deliveryIds.push(stored.delivery_id);
      }
    }
    return [...new Set(deliveryIds)];
  }

  private enforceCapacity(spec: SubscriptionSpec, event: RuntimeEventEnvelope): void {
    const pending = this.pendingCount(spec.subscriptionId);
    if (pending < spec.capacity) return;
    if (spec.backpressureMode === BackpressureMode.REJECT) {
      throw new RoutingError(EventSpineErrorCode.SUBSCRIBER_BACKPRESSURE, "subscriber queue is full", {
        subscription_id: spec.subscriptionId,
        capacity: spec.capacity,
        pending,
        event_id: event.eventId,
      });
    }
    if (spec.backpressureMode === BackpressureMode.COALESCE_NON_EFFECTIVE && event.effect === EventEffect.NON_EFFECTIVE) {
      const oldest = this.db.prepare(`
        SELECT * FROM runtime_event_deliveries
        WHERE subscription_id = ? AND state IN (?, ?)
        ORDER BY created_at ASC LIMIT 1
      `).get(spec.subscriptionId, DeliveryState.PENDING, DeliveryState.RETRY_WAIT) as DeliveryRow | undefined;
      if (oldest) {
        this.deadLetter(oldest, "coalesced_non_effective_backpressure", event.committedAt);
        return;
      }
    }
    const oldest = this.db.prepare(`
      SELECT * FROM runtime_event_deliveries
      WHERE subscription_id = ? AND state IN (?, ?)
      ORDER BY created_at ASC LIMIT 1
    `).get(spec.subscriptionId, DeliveryState.PENDING, DeliveryState.RETRY_WAIT) as DeliveryRow | undefined;
    if (!oldest) {
      throw new RoutingError(EventSpineErrorCode.SUBSCRIBER_BACKPRESSURE, "subscriber capacity exhausted by leased deliveries", {
        subscription_id: spec.subscriptionId,
        capacity: spec.capacity,
      });
    }
    this.deadLetter(oldest, "dead_letter_oldest_backpressure", event.committedAt);
  }

  private deliveryRow(deliveryId: string): DeliveryRow | undefined {
    return this.db.prepare("SELECT * FROM runtime_event_deliveries WHERE delivery_id = ?").get(deliveryId) as DeliveryRow | undefined;
  }

  private deadLetter(row: DeliveryRow, reason: string, now: string): DeliveryRecord {
    const delivery = deliveryFromRow(row);
    this.db.prepare(`
      INSERT OR IGNORE INTO runtime_event_dead_letters (
        dead_letter_id, delivery_id, event_id, subscription_id, reason,
        attempt, created_at, delivery_json
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    `).run(newId("dead"), row.delivery_id, row.event_id, row.subscription_id, reason.slice(0, 2000), row.attempt, now, canonicalJson(deliveryToJson(delivery)));
    this.db.prepare(`
      UPDATE runtime_event_deliveries
      SET state = ?, last_error = ?, lease_token = NULL, leased_at = NULL,
          lease_expires_at = NULL, updated_at = ?
      WHERE delivery_id = ?
    `).run(DeliveryState.DEAD_LETTER, reason.slice(0, 2000), now, row.delivery_id);
    this.recordMetric(row.event_id, "delivery.dead_letter", 1, { subscription_id: row.subscription_id, reason });
    return this.delivery(row.delivery_id)!;
  }

  private recordAppendMetrics(event: RuntimeEventEnvelope, route: RouteDecision | undefined, spillCount: number, offloadedBytes: number, duplicate: boolean): void {
    const values: Array<[string, number, Record<string, JsonValue>]> = [
      ["event.append", 1, { event_type: event.eventType, effect: event.effect }],
      ["event.envelope_bytes", event.envelopeBytes, { event_type: event.eventType }],
      ["event.inline_bytes", event.inlineBytes, { event_type: event.eventType }],
      ["event.source_bytes", event.sourceBytes, { event_type: event.eventType }],
      ["event.artifact_refs", event.artifactRefs.length, { event_type: event.eventType }],
      ["event.artifact_spills", spillCount, { event_type: event.eventType }],
      ["event.offloaded_bytes", offloadedBytes, { event_type: event.eventType }],
      ["event.duplicate", duplicate ? 1 : 0, { event_type: event.eventType }],
    ];
    if (route) {
      values.push(["route.recipient_count", route.recipients.length, { policy_id: route.policyId }]);
      values.push(["route.available_recipient_count", route.availableRecipientCount, { policy_id: route.policyId }]);
      values.push(["route.density", route.routeDensity, { policy_id: route.policyId }]);
      values.push(["route.broadcast", route.broadcast ? 1 : 0, { policy_id: route.policyId }]);
    }
    for (const [kind, value, dimensions] of values) this.recordMetric(event.eventId, kind, value, dimensions);
  }
}
