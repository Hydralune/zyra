import type { JsonObject } from "../contracts.ts";
import {
  createId,
  digest,
  E03RuntimeError,
  isTerminal,
  sealTask,
  type DeliveryKind,
  type E03Clock,
  type E03Delivery,
  type E03TaskState,
  SystemE03Clock,
} from "../e03/contracts.ts";

export class TeamDelivery {
  constructor(
    private readonly clock: E03Clock = new SystemE03Clock(),
    private readonly maximumPending = 128,
  ) {
    if (!Number.isSafeInteger(maximumPending) || maximumPending < 1)
      throw new E03RuntimeError(
        "invalid_backpressure_limit",
        "delivery pending limit must be positive",
      );
  }

  publishPartial(
    task: E03TaskState,
    input: {
      summary: string;
      payload?: JsonObject;
      artifactIds?: readonly string[];
      idempotencyKey: string;
    },
  ): { task: E03TaskState; delivery: E03Delivery } {
    if (task.status !== "running" && task.status !== "waiting")
      throw new E03RuntimeError(
        "partial_delivery_phase",
        `task in ${task.status} cannot publish partial output`,
      );
    return this.publish(task, "partial", input);
  }

  publishFinal(
    task: E03TaskState,
    input: {
      summary: string;
      payload?: JsonObject;
      artifactIds?: readonly string[];
      idempotencyKey: string;
    },
  ): { task: E03TaskState; delivery: E03Delivery } {
    if (
      task.status !== "completed" &&
      task.status !== "failed" &&
      task.status !== "cancelled" &&
      task.status !== "killed"
    )
      throw new E03RuntimeError(
        "final_delivery_phase",
        `task in ${task.status} cannot publish final output`,
      );
    const priorFinals = task.deliveries.filter(
      (delivery) => delivery.kind === "final" || delivery.kind === "error",
    );
    if (priorFinals.length >= task.identity.attempt) {
      const prior = task.deliveries.find(
        (delivery) => delivery.idempotencyKey === input.idempotencyKey,
      );
      if (prior)
        return {
          task: structuredClone(task),
          delivery: structuredClone(prior),
        };
      throw new E03RuntimeError(
        "duplicate_final_delivery",
        "task already published a final delivery",
      );
    }
    return this.publish(task, "final", input);
  }

  applyBackpressure(task: E03TaskState): {
    accepted: boolean;
    pending: number;
    limit: number;
    reason: string;
  } {
    const pending = task.deliveries.filter(
      (delivery) => delivery.acknowledgedAt === null,
    ).length;
    return {
      accepted: pending < this.maximumPending,
      pending,
      limit: this.maximumPending,
      reason: pending < this.maximumPending ? "" : "delivery_backpressure",
    };
  }

  rejectLate(
    task: E03TaskState,
    kind: DeliveryKind,
    expectedRevision: number,
  ): void {
    if (task.revision !== expectedRevision)
      throw new E03RuntimeError(
        "stale_revision",
        `delivery expected revision ${expectedRevision}, current ${task.revision}`,
      );
    if (
      ["completed", "failed", "cancelled", "killed"].includes(task.status) &&
      kind !== "final" &&
      kind !== "error" &&
      kind !== "artifact"
    )
      throw new E03RuntimeError(
        "late_delivery",
        `cannot publish ${kind} after ${task.status}`,
      );
  }

  acknowledge(task: E03TaskState, deliveryId: string): E03TaskState {
    const selected = task.deliveries.find(
      (delivery) => delivery.deliveryId === deliveryId,
    );
    if (!selected)
      throw new E03RuntimeError(
        "unknown_delivery",
        `unknown delivery ${deliveryId}`,
      );
    if (selected.acknowledgedAt) return structuredClone(task);
    const now = this.clock.now();
    const deliveries = task.deliveries.map((delivery) =>
      delivery.deliveryId === deliveryId
        ? sealDelivery({ ...delivery, acknowledgedAt: now })
        : delivery,
    );
    return sealTask({
      ...task,
      deliveries,
      sequence: task.sequence + 1,
      updatedAt: now,
      checksum: "",
    });
  }

  private publish(
    task: E03TaskState,
    kind: DeliveryKind,
    input: {
      summary: string;
      payload?: JsonObject;
      artifactIds?: readonly string[];
      idempotencyKey: string;
    },
  ): { task: E03TaskState; delivery: E03Delivery } {
    const prior = task.deliveries.find(
      (delivery) => delivery.idempotencyKey === input.idempotencyKey,
    );
    if (prior) {
      if (prior.kind !== kind || prior.summary !== input.summary.trim())
        throw new E03RuntimeError(
          "delivery_idempotency_conflict",
          "delivery idempotency key was reused with different content",
        );
      return { task: structuredClone(task), delivery: structuredClone(prior) };
    }
    const pressure = this.applyBackpressure(task);
    if (!pressure.accepted)
      throw new E03RuntimeError(
        "delivery_backpressure",
        "too many unacknowledged deliveries",
        pressure as unknown as JsonObject,
      );
    const summary = input.summary.trim();
    if (!summary || summary.length > 256_000)
      throw new E03RuntimeError(
        "invalid_delivery_summary",
        "delivery summary must contain 1..256000 characters",
      );
    const payload = {
      deliveryId: createId("team-delivery"),
      taskId: task.identity.taskId,
      sequence: task.deliveries.length
        ? Math.max(...task.deliveries.map((delivery) => delivery.sequence)) + 1
        : 1,
      kind,
      summary,
      payload: structuredClone(input.payload ?? {}),
      artifactIds: [...new Set(input.artifactIds ?? [])],
      idempotencyKey: input.idempotencyKey,
      createdAt: this.clock.now(),
      acknowledgedAt: null,
    };
    const delivery = { ...payload, digest: digest(payload) };
    return {
      task: sealTask({
        ...task,
        deliveries: [...task.deliveries, delivery],
        sequence: task.sequence + 1,
        updatedAt: this.clock.now(),
        checksum: "",
      }),
      delivery,
    };
  }
}

function sealDelivery(delivery: E03Delivery): E03Delivery {
  const { digest: _digest, ...payload } = delivery;
  return { ...payload, digest: digest(payload) };
}

export type OutboxPhase =
  | "prepared"
  | "published"
  | "acknowledged"
  | "dead-lettered";

export interface DeliveryOutboxRecord {
  outboxId: string;
  taskId: string;
  parentTaskId: string;
  taskLeaseId: string;
  taskRevision: number;
  delivery: E03Delivery;
  phase: OutboxPhase;
  attempt: number;
  maximumAttempts: number;
  availableAt: string;
  publishedAt: string | null;
  acknowledgedAt: string | null;
  lastError: string;
  idempotencyKey: string;
  digest: string;
}

export interface OutboxBatch {
  batchId: string;
  selectedAt: string;
  records: DeliveryOutboxRecord[];
  byteSize: number;
  hasMore: boolean;
}

export interface DeliveryPublishReceipt {
  outboxId: string;
  deliveryId: string;
  accepted: boolean;
  duplicate: boolean;
  remoteSequence: number;
  error: string;
  completedAt: string;
  digest: string;
}

export class DeliveryOutboxRuntime {
  private readonly records = new Map<string, DeliveryOutboxRecord>();
  private readonly idempotency = new Map<string, string>();

  constructor(
    private readonly clock: E03Clock = new SystemE03Clock(),
    private readonly maximumPending = 1024,
  ) {
    if (
      !Number.isSafeInteger(maximumPending) ||
      maximumPending < 1 ||
      maximumPending > 1_000_000
    )
      throw new E03RuntimeError(
        "invalid_outbox_limit",
        "outbox pending limit is out of range",
      );
  }

  prepare(
    task: E03TaskState,
    delivery: E03Delivery,
    input: {
      idempotencyKey: string;
      maximumAttempts?: number;
      availableAt?: string;
    },
  ): DeliveryOutboxRecord {
    this.assertDeliveryCustody(task, delivery);
    const priorId = this.idempotency.get(input.idempotencyKey);
    if (priorId) {
      const prior = this.require(priorId);
      if (
        prior.delivery.digest !== delivery.digest ||
        prior.taskRevision !== task.revision
      )
        throw new E03RuntimeError(
          "outbox_idempotency_conflict",
          "outbox idempotency key was reused with different delivery custody",
        );
      return structuredClone(prior);
    }
    const pending = [...this.records.values()].filter(
      (record) => record.phase === "prepared" || record.phase === "published",
    ).length;
    if (pending >= this.maximumPending)
      throw new E03RuntimeError(
        "outbox_backpressure",
        `outbox has ${pending} pending records`,
        { pending, limit: this.maximumPending },
      );
    const maximumAttempts = input.maximumAttempts ?? 5;
    if (
      !Number.isSafeInteger(maximumAttempts) ||
      maximumAttempts < 1 ||
      maximumAttempts > 100
    )
      throw new E03RuntimeError(
        "invalid_outbox_attempts",
        "outbox maximum attempts must be 1..100",
      );
    const availableAt = input.availableAt ?? this.clock.now();
    if (!Number.isFinite(Date.parse(availableAt)))
      throw new E03RuntimeError(
        "invalid_outbox_availability",
        "outbox availability must be an ISO timestamp",
      );
    const record = sealOutbox({
      outboxId: createId("delivery-outbox"),
      taskId: task.identity.taskId,
      parentTaskId: task.identity.parentTaskId,
      taskLeaseId: task.identity.leaseId,
      taskRevision: task.revision,
      delivery: structuredClone(delivery),
      phase: "prepared",
      attempt: 0,
      maximumAttempts,
      availableAt,
      publishedAt: null,
      acknowledgedAt: null,
      lastError: "",
      idempotencyKey: input.idempotencyKey,
    });
    this.records.set(record.outboxId, record);
    this.idempotency.set(record.idempotencyKey, record.outboxId);
    return structuredClone(record);
  }

  claim(input: {
    maximumRecords: number;
    maximumBytes: number;
    now?: string;
  }): OutboxBatch {
    if (
      !Number.isSafeInteger(input.maximumRecords) ||
      input.maximumRecords < 1 ||
      input.maximumRecords > 10_000
    )
      throw new E03RuntimeError(
        "invalid_outbox_batch",
        "outbox batch record limit is invalid",
      );
    if (
      !Number.isSafeInteger(input.maximumBytes) ||
      input.maximumBytes < 1 ||
      input.maximumBytes > 256_000_000
    )
      throw new E03RuntimeError(
        "invalid_outbox_batch",
        "outbox batch byte limit is invalid",
      );
    const selectedAt = input.now ?? this.clock.now();
    const available = [...this.records.values()]
      .filter(
        (record) =>
          record.phase === "prepared" && record.availableAt <= selectedAt,
      )
      .sort(compareOutbox);
    const records: DeliveryOutboxRecord[] = [];
    let byteSize = 0;
    for (const record of available) {
      const size = Buffer.byteLength(JSON.stringify(record.delivery), "utf8");
      if (
        records.length &&
        (records.length >= input.maximumRecords ||
          byteSize + size > input.maximumBytes)
      )
        break;
      if (!records.length && size > input.maximumBytes)
        throw new E03RuntimeError(
          "outbox_record_too_large",
          `delivery ${record.delivery.deliveryId} exceeds batch bytes`,
        );
      records.push(structuredClone(record));
      byteSize += size;
    }
    return {
      batchId: `outbox-batch-${digest({ selectedAt, ids: records.map((record) => record.outboxId) }).slice(0, 32)}`,
      selectedAt,
      records,
      byteSize,
      hasMore: available.length > records.length,
    };
  }

  markPublished(
    outboxId: string,
    receipt: DeliveryPublishReceipt,
  ): DeliveryOutboxRecord {
    const record = this.require(outboxId);
    this.assertReceipt(record, receipt);
    if (record.phase === "acknowledged") return structuredClone(record);
    if (record.phase === "dead-lettered")
      throw new E03RuntimeError(
        "outbox_dead_lettered",
        "dead-lettered delivery cannot publish",
      );
    if (!receipt.accepted)
      return this.retry(outboxId, receipt.error || "delivery_publish_rejected");
    const next = sealOutbox({
      ...record,
      phase: "published",
      attempt: record.attempt + 1,
      publishedAt: receipt.completedAt,
      lastError: "",
    });
    this.records.set(outboxId, next);
    return structuredClone(next);
  }

  acknowledge(
    outboxId: string,
    receipt: DeliveryPublishReceipt,
  ): DeliveryOutboxRecord {
    const record = this.require(outboxId);
    this.assertReceipt(record, receipt);
    if (record.phase === "acknowledged") return structuredClone(record);
    if (!receipt.accepted)
      throw new E03RuntimeError(
        "delivery_ack_rejected",
        receipt.error || "delivery acknowledgment rejected",
      );
    if (record.phase !== "published" && !receipt.duplicate)
      throw new E03RuntimeError(
        "delivery_ack_before_publish",
        "delivery cannot ACK before publish",
      );
    const next = sealOutbox({
      ...record,
      phase: "acknowledged",
      acknowledgedAt: receipt.completedAt,
      publishedAt: record.publishedAt ?? receipt.completedAt,
      lastError: "",
    });
    this.records.set(outboxId, next);
    return structuredClone(next);
  }

  retry(
    outboxId: string,
    error: string,
    baseDelayMs = 250,
  ): DeliveryOutboxRecord {
    const record = this.require(outboxId);
    if (record.phase === "acknowledged" || record.phase === "dead-lettered")
      return structuredClone(record);
    if (!error.trim())
      throw new E03RuntimeError(
        "outbox_retry_reason_missing",
        "outbox retry requires an error",
      );
    const attempt = record.attempt + 1;
    if (attempt >= record.maximumAttempts)
      return this.deadLetter(outboxId, error);
    const delay = Math.min(
      60_000,
      baseDelayMs * 2 ** Math.min(attempt - 1, 12),
    );
    const jitter =
      parseInt(digest({ outboxId, attempt }).slice(0, 4), 16) %
      Math.max(1, Math.floor(delay / 4));
    const availableAt = new Date(
      Date.parse(this.clock.now()) + delay + jitter,
    ).toISOString();
    const next = sealOutbox({
      ...record,
      phase: "prepared",
      attempt,
      availableAt,
      lastError: error.trim(),
    });
    this.records.set(outboxId, next);
    return structuredClone(next);
  }

  deadLetter(outboxId: string, error: string): DeliveryOutboxRecord {
    const record = this.require(outboxId);
    if (record.phase === "acknowledged")
      throw new E03RuntimeError(
        "outbox_already_acknowledged",
        "acknowledged record cannot dead-letter",
      );
    const next = sealOutbox({
      ...record,
      phase: "dead-lettered",
      attempt: Math.max(record.attempt, record.maximumAttempts),
      lastError: error.trim() || "delivery_attempts_exhausted",
    });
    this.records.set(outboxId, next);
    return structuredClone(next);
  }

  restore(records: readonly DeliveryOutboxRecord[]): void {
    const next = new Map<string, DeliveryOutboxRecord>();
    const keys = new Map<string, string>();
    for (const record of records) {
      assertOutbox(record);
      if (next.has(record.outboxId))
        throw new E03RuntimeError(
          "duplicate_outbox_record",
          `duplicate outbox id ${record.outboxId}`,
        );
      const prior = keys.get(record.idempotencyKey);
      if (prior && prior !== record.outboxId)
        throw new E03RuntimeError(
          "outbox_idempotency_conflict",
          `outbox key ${record.idempotencyKey} maps to multiple records`,
        );
      next.set(record.outboxId, structuredClone(record));
      keys.set(record.idempotencyKey, record.outboxId);
    }
    this.records.clear();
    this.idempotency.clear();
    for (const [key, record] of next) this.records.set(key, record);
    for (const [key, value] of keys) this.idempotency.set(key, value);
  }

  snapshot(): DeliveryOutboxRecord[] {
    return [...this.records.values()]
      .sort(compareOutbox)
      .map((record) => structuredClone(record));
  }

  pending(taskId?: string): DeliveryOutboxRecord[] {
    return this.snapshot().filter(
      (record) =>
        (!taskId || record.taskId === taskId) &&
        (record.phase === "prepared" || record.phase === "published"),
    );
  }

  projection(): JsonObject {
    const records = this.snapshot();
    return {
      total: records.length,
      prepared: records.filter((record) => record.phase === "prepared").length,
      published: records.filter((record) => record.phase === "published")
        .length,
      acknowledged: records.filter((record) => record.phase === "acknowledged")
        .length,
      dead_lettered: records.filter(
        (record) => record.phase === "dead-lettered",
      ).length,
      pending_delivery_ids: records
        .filter(
          (record) =>
            record.phase === "prepared" || record.phase === "published",
        )
        .map((record) => record.delivery.deliveryId),
    };
  }

  private require(outboxId: string): DeliveryOutboxRecord {
    const record = this.records.get(outboxId);
    if (!record)
      throw new E03RuntimeError(
        "unknown_outbox_record",
        `unknown outbox record ${outboxId}`,
      );
    assertOutbox(record);
    return record;
  }

  private assertDeliveryCustody(
    task: E03TaskState,
    delivery: E03Delivery,
  ): void {
    const { digest: checksum, ...payload } = delivery;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "delivery_digest_mismatch",
        "delivery digest is invalid",
      );
    if (delivery.taskId !== task.identity.taskId)
      throw new E03RuntimeError(
        "delivery_task_mismatch",
        "delivery belongs to another task",
      );
    if (!task.deliveries.some((item) => item.digest === delivery.digest))
      throw new E03RuntimeError(
        "delivery_not_committed",
        "delivery is not present in canonical task state",
      );
  }

  private assertReceipt(
    record: DeliveryOutboxRecord,
    receipt: DeliveryPublishReceipt,
  ): void {
    const { digest: checksum, ...payload } = receipt;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "publish_receipt_digest_mismatch",
        "publish receipt digest is invalid",
      );
    if (
      receipt.outboxId !== record.outboxId ||
      receipt.deliveryId !== record.delivery.deliveryId
    )
      throw new E03RuntimeError(
        "publish_receipt_custody_mismatch",
        "publish receipt belongs to another outbox record",
      );
  }
}

function sealOutbox(
  value: Omit<DeliveryOutboxRecord, "digest"> | DeliveryOutboxRecord,
): DeliveryOutboxRecord {
  const { digest: _digest, ...payload } = value as DeliveryOutboxRecord;
  return { ...payload, digest: digest(payload) };
}

function assertOutbox(record: DeliveryOutboxRecord): void {
  const { digest: checksum, ...payload } = record;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "outbox_digest_mismatch",
      `outbox ${record.outboxId} digest is invalid`,
    );
  if (
    !record.outboxId ||
    !record.taskId ||
    !record.taskLeaseId ||
    !record.idempotencyKey
  )
    throw new E03RuntimeError(
      "invalid_outbox_record",
      "outbox identity is incomplete",
    );
  if (
    record.attempt < 0 ||
    record.maximumAttempts < 1 ||
    record.attempt > record.maximumAttempts
  )
    throw new E03RuntimeError(
      "invalid_outbox_attempt",
      "outbox attempt is invalid",
    );
}

function compareOutbox(
  left: DeliveryOutboxRecord,
  right: DeliveryOutboxRecord,
): number {
  return (
    left.availableAt.localeCompare(right.availableAt) ||
    left.taskId.localeCompare(right.taskId) ||
    left.delivery.sequence - right.delivery.sequence ||
    left.outboxId.localeCompare(right.outboxId)
  );
}

export function publishReceipt(
  input: Omit<DeliveryPublishReceipt, "digest">,
): DeliveryPublishReceipt {
  return { ...input, digest: digest(input) };
}

export interface DeliveryEvidence {
  evidenceId: string;
  taskId: string;
  deliveryId: string;
  deliverySequence: number;
  channel: "parent-session" | "artifact" | "event" | "control";
  destination: string;
  payloadDigest: string;
  accepted: boolean;
  duplicate: boolean;
  visible: boolean;
  attemptedAt: string;
  completedAt: string;
  error: string;
  digest: string;
}

export interface DeliveryEvidenceSummary {
  taskId: string;
  deliveryId: string;
  attempts: number;
  accepted: number;
  duplicates: number;
  visible: boolean;
  channels: string[];
  destinations: string[];
  errors: string[];
  complete: boolean;
  digest: string;
}

export class DeliveryEvidenceRuntime {
  private readonly evidence = new Map<string, DeliveryEvidence[]>();

  record(
    delivery: E03Delivery,
    input: Omit<
      DeliveryEvidence,
      | "evidenceId"
      | "taskId"
      | "deliveryId"
      | "deliverySequence"
      | "payloadDigest"
      | "digest"
    >,
  ): DeliveryEvidence {
    assertDelivery(delivery);
    if (!input.destination.trim())
      throw new E03RuntimeError(
        "delivery_destination_missing",
        "delivery evidence destination is empty",
      );
    if (
      !Number.isFinite(Date.parse(input.attemptedAt)) ||
      !Number.isFinite(Date.parse(input.completedAt)) ||
      input.completedAt < input.attemptedAt
    )
      throw new E03RuntimeError(
        "delivery_evidence_time",
        "delivery evidence timestamps are invalid or reversed",
      );
    if (!input.accepted && !input.error.trim())
      throw new E03RuntimeError(
        "delivery_evidence_error_missing",
        "rejected delivery evidence has no error",
      );
    if (input.visible && !input.accepted)
      throw new E03RuntimeError(
        "delivery_visibility_without_acceptance",
        "rejected delivery cannot be visible",
      );
    const payloadDigest = digest(delivery.payload);
    const evidenceId = `delivery-evidence-${digest({ deliveryId: delivery.deliveryId, channel: input.channel, destination: input.destination, attemptedAt: input.attemptedAt }).slice(0, 32)}`;
    const existing = this.evidence.get(delivery.deliveryId) ?? [];
    const duplicate = existing.find((value) => value.evidenceId === evidenceId);
    if (duplicate) {
      const proposed = sealEvidence({
        evidenceId,
        taskId: delivery.taskId,
        deliveryId: delivery.deliveryId,
        deliverySequence: delivery.sequence,
        payloadDigest,
        ...input,
      });
      if (proposed.digest !== duplicate.digest)
        throw new E03RuntimeError(
          "delivery_evidence_conflict",
          `evidence ${evidenceId} changed content`,
        );
      return structuredClone(duplicate);
    }
    const value = sealEvidence({
      evidenceId,
      taskId: delivery.taskId,
      deliveryId: delivery.deliveryId,
      deliverySequence: delivery.sequence,
      payloadDigest,
      ...input,
    });
    existing.push(value);
    existing.sort(
      (left, right) =>
        left.attemptedAt.localeCompare(right.attemptedAt) ||
        left.evidenceId.localeCompare(right.evidenceId),
    );
    this.evidence.set(delivery.deliveryId, existing);
    return structuredClone(value);
  }

  summarize(delivery: E03Delivery): DeliveryEvidenceSummary {
    assertDelivery(delivery);
    const values = (this.evidence.get(delivery.deliveryId) ?? []).map((value) =>
      structuredClone(value),
    );
    for (const value of values) this.assertEvidence(value, delivery);
    const payload = {
      taskId: delivery.taskId,
      deliveryId: delivery.deliveryId,
      attempts: values.length,
      accepted: values.filter((value) => value.accepted).length,
      duplicates: values.filter((value) => value.duplicate).length,
      visible: values.some((value) => value.visible),
      channels: [...new Set(values.map((value) => value.channel))].sort(),
      destinations: [
        ...new Set(values.map((value) => value.destination)),
      ].sort(),
      errors: [
        ...new Set(values.map((value) => value.error).filter(Boolean)),
      ].sort(),
      complete: values.some(
        (value) =>
          value.accepted &&
          (value.visible ||
            delivery.kind === "artifact" ||
            delivery.kind === "progress"),
      ),
    };
    return { ...payload, digest: digest(payload) };
  }

  requireComplete(delivery: E03Delivery): DeliveryEvidenceSummary {
    const summary = this.summarize(delivery);
    if (!summary.complete)
      throw new E03RuntimeError(
        "delivery_evidence_incomplete",
        `delivery ${delivery.deliveryId} has no accepted visible evidence`,
        {
          attempts: summary.attempts,
          channels: summary.channels,
          errors: summary.errors,
        },
      );
    return summary;
  }

  restore(values: readonly DeliveryEvidence[]): void {
    const next = new Map<string, DeliveryEvidence[]>();
    for (const value of values) {
      const { digest: checksum, ...payload } = value;
      if (digest(payload) !== checksum)
        throw new E03RuntimeError(
          "delivery_evidence_digest",
          `evidence ${value.evidenceId} digest is invalid`,
        );
      const list = next.get(value.deliveryId) ?? [];
      const prior = list.find((item) => item.evidenceId === value.evidenceId);
      if (prior && prior.digest !== value.digest)
        throw new E03RuntimeError(
          "delivery_evidence_conflict",
          `evidence ${value.evidenceId} is duplicated differently`,
        );
      if (!prior) list.push(structuredClone(value));
      next.set(value.deliveryId, list);
    }
    this.evidence.clear();
    for (const [deliveryId, list] of next)
      this.evidence.set(
        deliveryId,
        list.sort((left, right) =>
          left.attemptedAt.localeCompare(right.attemptedAt),
        ),
      );
  }

  snapshot(taskId?: string): DeliveryEvidence[] {
    return [...this.evidence.values()]
      .flat()
      .filter((value) => !taskId || value.taskId === taskId)
      .sort(
        (left, right) =>
          left.taskId.localeCompare(right.taskId) ||
          left.deliverySequence - right.deliverySequence ||
          left.attemptedAt.localeCompare(right.attemptedAt),
      )
      .map((value) => structuredClone(value));
  }

  private assertEvidence(value: DeliveryEvidence, delivery: E03Delivery): void {
    const { digest: checksum, ...payload } = value;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "delivery_evidence_digest",
        `evidence ${value.evidenceId} digest is invalid`,
      );
    if (
      value.deliveryId !== delivery.deliveryId ||
      value.taskId !== delivery.taskId ||
      value.deliverySequence !== delivery.sequence ||
      value.payloadDigest !== digest(delivery.payload)
    )
      throw new E03RuntimeError(
        "delivery_evidence_custody",
        `evidence ${value.evidenceId} differs from delivery custody`,
      );
  }
}

export interface YieldFragment {
  fragmentId: string;
  taskId: string;
  leaseId: string;
  sequence: number;
  kind: "text" | "json" | "artifact" | "error";
  text: string;
  value: JsonObject;
  artifactIds: string[];
  final: boolean;
  idempotencyKey: string;
  createdAt: string;
  digest: string;
}

export interface YieldAssembly {
  assemblyId: string;
  taskId: string;
  leaseId: string;
  fragments: YieldFragment[];
  text: string;
  values: JsonObject[];
  artifactIds: string[];
  errors: string[];
  complete: boolean;
  nextSequence: number;
  digest: string;
}

export class TypedYieldAssembler {
  private readonly fragments = new Map<string, YieldFragment[]>();

  append(
    task: E03TaskState,
    input: Omit<
      YieldFragment,
      "fragmentId" | "taskId" | "leaseId" | "sequence" | "createdAt" | "digest"
    > & { sequence?: number },
  ): YieldFragment {
    if (isTerminal(task.status) && !input.final)
      throw new E03RuntimeError(
        "late_yield_fragment",
        `cannot append non-final yield after ${task.status}`,
      );
    if (!input.idempotencyKey.trim())
      throw new E03RuntimeError(
        "yield_idempotency_missing",
        "yield fragment requires an idempotency key",
      );
    const list = this.fragments.get(task.identity.taskId) ?? [];
    const prior = list.find(
      (fragment) => fragment.idempotencyKey === input.idempotencyKey,
    );
    const sequence = input.sequence ?? (list.at(-1)?.sequence ?? 0) + 1;
    const payload = {
      fragmentId: `yield-fragment-${digest({ taskId: task.identity.taskId, idempotencyKey: input.idempotencyKey }).slice(0, 32)}`,
      taskId: task.identity.taskId,
      leaseId: task.identity.leaseId,
      sequence,
      kind: input.kind,
      text: input.text.trim(),
      value: structuredClone(input.value),
      artifactIds: [...new Set(input.artifactIds)].sort(),
      final: input.final,
      idempotencyKey: input.idempotencyKey,
      createdAt: new Date().toISOString(),
    };
    const fragment = { ...payload, digest: digest(payload) };
    if (prior) {
      if (prior.digest !== fragment.digest)
        throw new E03RuntimeError(
          "yield_idempotency_conflict",
          "yield idempotency key changed fragment content",
        );
      return structuredClone(prior);
    }
    if (sequence !== (list.at(-1)?.sequence ?? 0) + 1)
      throw new E03RuntimeError(
        "yield_sequence_gap",
        `expected yield sequence ${(list.at(-1)?.sequence ?? 0) + 1}, got ${sequence}`,
      );
    if (list.some((value) => value.final))
      throw new E03RuntimeError(
        "yield_after_final",
        "yield fragment was appended after final fragment",
      );
    if (input.kind === "text" && !payload.text)
      throw new E03RuntimeError(
        "yield_text_missing",
        "text yield fragment is empty",
      );
    if (input.kind === "artifact" && !payload.artifactIds.length)
      throw new E03RuntimeError(
        "yield_artifact_missing",
        "artifact yield fragment has no artifact ids",
      );
    if (input.kind === "error" && !payload.text)
      throw new E03RuntimeError(
        "yield_error_missing",
        "error yield fragment has no message",
      );
    list.push(fragment);
    this.fragments.set(task.identity.taskId, list);
    return structuredClone(fragment);
  }

  assemble(task: E03TaskState): YieldAssembly {
    const fragments = (this.fragments.get(task.identity.taskId) ?? []).map(
      (fragment) => structuredClone(fragment),
    );
    let expected = 1;
    for (const fragment of fragments) {
      assertFragment(fragment);
      if (
        fragment.taskId !== task.identity.taskId ||
        fragment.leaseId !== task.identity.leaseId
      )
        throw new E03RuntimeError(
          "yield_custody_mismatch",
          `yield ${fragment.fragmentId} uses another task or lease`,
        );
      if (fragment.sequence !== expected)
        throw new E03RuntimeError(
          "yield_sequence_gap",
          `expected yield sequence ${expected}, got ${fragment.sequence}`,
        );
      expected += 1;
    }
    const payload = {
      assemblyId: `yield-assembly-${digest({ taskId: task.identity.taskId, leaseId: task.identity.leaseId, fragments: fragments.map((fragment) => fragment.digest) }).slice(0, 32)}`,
      taskId: task.identity.taskId,
      leaseId: task.identity.leaseId,
      fragments,
      text: fragments
        .filter((fragment) => fragment.kind === "text")
        .map((fragment) => fragment.text)
        .join("\n"),
      values: fragments
        .filter((fragment) => fragment.kind === "json")
        .map((fragment) => structuredClone(fragment.value)),
      artifactIds: [
        ...new Set(fragments.flatMap((fragment) => fragment.artifactIds)),
      ].sort(),
      errors: fragments
        .filter((fragment) => fragment.kind === "error")
        .map((fragment) => fragment.text),
      complete: fragments.some((fragment) => fragment.final),
      nextSequence: expected,
    };
    return { ...payload, digest: digest(payload) };
  }

  toDelivery(
    task: E03TaskState,
    idempotencyKey: string,
  ): {
    kind: DeliveryKind;
    summary: string;
    payload: JsonObject;
    artifactIds: string[];
    idempotencyKey: string;
  } {
    const assembly = this.assemble(task);
    if (!assembly.fragments.length)
      throw new E03RuntimeError(
        "yield_empty",
        "task has no typed yield fragments",
      );
    const kind: DeliveryKind = assembly.errors.length
      ? "error"
      : assembly.complete
        ? "final"
        : "partial";
    const summary =
      assembly.errors.join("; ") ||
      assembly.text ||
      `${assembly.values.length} structured values, ${assembly.artifactIds.length} artifacts`;
    return {
      kind,
      summary,
      payload: {
        assembly_id: assembly.assemblyId,
        text: assembly.text,
        values: assembly.values,
        errors: assembly.errors,
        complete: assembly.complete,
        fragment_count: assembly.fragments.length,
      },
      artifactIds: assembly.artifactIds,
      idempotencyKey,
    };
  }

  restore(fragments: readonly YieldFragment[]): void {
    const next = new Map<string, YieldFragment[]>();
    for (const fragment of fragments) {
      assertFragment(fragment);
      const list = next.get(fragment.taskId) ?? [];
      if (fragment.sequence !== list.length + 1)
        throw new E03RuntimeError(
          "yield_sequence_gap",
          `restored task ${fragment.taskId} has a yield sequence gap`,
        );
      if (list.some((value) => value.final))
        throw new E03RuntimeError(
          "yield_after_final",
          `restored task ${fragment.taskId} has fragments after final`,
        );
      if (
        list.some((value) => value.idempotencyKey === fragment.idempotencyKey)
      )
        throw new E03RuntimeError(
          "yield_idempotency_conflict",
          `restored task ${fragment.taskId} repeats a yield key`,
        );
      list.push(structuredClone(fragment));
      next.set(fragment.taskId, list);
    }
    this.fragments.clear();
    for (const [taskId, values] of next) this.fragments.set(taskId, values);
  }

  snapshot(taskId?: string): YieldFragment[] {
    return [...this.fragments.values()]
      .flat()
      .filter((fragment) => !taskId || fragment.taskId === taskId)
      .sort(
        (left, right) =>
          left.taskId.localeCompare(right.taskId) ||
          left.sequence - right.sequence,
      )
      .map((fragment) => structuredClone(fragment));
  }
}

export interface DeliveryRetryDecision {
  retry: boolean;
  attempt: number;
  availableAt: string;
  delayMs: number;
  deadLetter: boolean;
  reason: string;
  digest: string;
}

export class DeliveryRetryPolicy {
  decide(input: {
    delivery: E03Delivery;
    attempt: number;
    maximumAttempts: number;
    error: string;
    retryAfterMs?: number;
    now?: string;
  }): DeliveryRetryDecision {
    assertDelivery(input.delivery);
    if (
      !Number.isSafeInteger(input.attempt) ||
      input.attempt < 0 ||
      !Number.isSafeInteger(input.maximumAttempts) ||
      input.maximumAttempts < 1
    )
      throw new E03RuntimeError(
        "invalid_delivery_attempt",
        "delivery retry attempt or maximum is invalid",
      );
    const now = input.now ?? new Date().toISOString();
    if (!Number.isFinite(Date.parse(now)))
      throw new E03RuntimeError(
        "invalid_delivery_retry_time",
        "delivery retry now timestamp is invalid",
      );
    if (!input.error.trim())
      throw new E03RuntimeError(
        "delivery_retry_error_missing",
        "delivery retry requires an error",
      );
    const attempt = input.attempt + 1;
    const permanent = permanentDeliveryError(input.error);
    const deadLetter = permanent || attempt >= input.maximumAttempts;
    const requestedDelay = input.retryAfterMs;
    if (
      requestedDelay !== undefined &&
      (!Number.isSafeInteger(requestedDelay) ||
        requestedDelay < 0 ||
        requestedDelay > 86_400_000)
    )
      throw new E03RuntimeError(
        "invalid_delivery_retry_delay",
        "delivery retry-after is out of range",
      );
    const exponential = Math.min(300_000, 250 * 2 ** Math.min(attempt - 1, 12));
    const jitter =
      parseInt(
        digest({ deliveryId: input.delivery.deliveryId, attempt }).slice(0, 8),
        16,
      ) % Math.max(1, Math.floor(exponential / 3));
    const delayMs = deadLetter
      ? 0
      : Math.max(requestedDelay ?? 0, exponential + jitter);
    const payload = {
      retry: !deadLetter,
      attempt,
      availableAt: new Date(Date.parse(now) + delayMs).toISOString(),
      delayMs,
      deadLetter,
      reason: permanent
        ? "permanent_delivery_error"
        : deadLetter
          ? "delivery_attempts_exhausted"
          : "delivery_retry_scheduled",
    };
    return { ...payload, digest: digest(payload) };
  }

  classify(error: string): {
    retryable: boolean;
    category: string;
    normalized: string;
  } {
    const normalized = error.trim().toLowerCase();
    if (!normalized)
      return { retryable: false, category: "missing", normalized };
    if (/permission|denied|unauthorized|forbidden/.test(normalized))
      return { retryable: false, category: "permission", normalized };
    if (
      /invalid|malformed|unsupported|not found|unknown destination/.test(
        normalized,
      )
    )
      return { retryable: false, category: "validation", normalized };
    if (/duplicate|already delivered/.test(normalized))
      return { retryable: false, category: "duplicate", normalized };
    if (
      /timeout|temporar|unavailable|disconnect|reset|busy|rate|throttl/.test(
        normalized,
      )
    )
      return { retryable: true, category: "transient", normalized };
    return { retryable: true, category: "unknown", normalized };
  }
}

export interface DeliveryReconciliationFinding {
  code: string;
  taskId: string;
  deliveryId: string;
  sequence: number;
  detail: string;
  recoverable: boolean;
}

export interface DeliveryReconciliationReport {
  taskId: string;
  valid: boolean;
  finalCount: number;
  pendingCount: number;
  acknowledgedCount: number;
  evidenceCount: number;
  findings: DeliveryReconciliationFinding[];
  digest: string;
}

export class DeliveryReconciliationRuntime {
  reconcile(
    task: E03TaskState,
    evidence: readonly DeliveryEvidence[],
    outbox: readonly DeliveryOutboxRecord[],
  ): DeliveryReconciliationReport {
    const findings: DeliveryReconciliationFinding[] = [];
    let expected = 1;
    let finalSeen = false;
    let priorFinalAt = "";
    for (const delivery of task.deliveries) {
      assertDelivery(delivery);
      if (delivery.sequence !== expected)
        findings.push(
          deliveryFinding(
            "delivery_sequence_gap",
            task,
            delivery,
            `expected ${expected}`,
          ),
        );
      expected = delivery.sequence + 1;
      if (finalSeen) {
        const resumed = task.transitions.some(
          (transition) =>
            transition.eventType === "persist_agent_task_resume" &&
            transition.committedAt !== null &&
            transition.committedAt > priorFinalAt &&
            transition.committedAt <= delivery.createdAt,
        );
        if (resumed) finalSeen = false;
        else
          findings.push(
            deliveryFinding(
              "delivery_after_final",
              task,
              delivery,
              "delivery follows final/error without a resume boundary",
            ),
          );
      }
      if (delivery.kind === "final" || delivery.kind === "error") {
        finalSeen = true;
        priorFinalAt = delivery.createdAt;
      }
      const matchingEvidence = evidence.filter(
        (value) => value.deliveryId === delivery.deliveryId,
      );
      const matchingOutbox = outbox.filter(
        (value) => value.delivery.deliveryId === delivery.deliveryId,
      );
      for (const value of matchingEvidence) {
        const { digest: checksum, ...payload } = value;
        if (
          digest(payload) !== checksum ||
          value.taskId !== task.identity.taskId
        )
          findings.push(
            deliveryFinding(
              "invalid_delivery_evidence",
              task,
              delivery,
              `evidence ${value.evidenceId} is corrupt or cross-task`,
              false,
            ),
          );
      }
      for (const value of matchingOutbox) {
        const { digest: checksum, ...payload } = value;
        if (
          digest(payload) !== checksum ||
          value.taskId !== task.identity.taskId ||
          value.taskLeaseId !== task.identity.leaseId
        )
          findings.push(
            deliveryFinding(
              "invalid_delivery_outbox",
              task,
              delivery,
              `outbox ${value.outboxId} is corrupt or stale`,
              false,
            ),
          );
      }
      if (
        !delivery.acknowledgedAt &&
        !matchingOutbox.some(
          (value) => value.phase === "prepared" || value.phase === "published",
        ) &&
        !matchingEvidence.some((value) => value.accepted)
      )
        findings.push(
          deliveryFinding(
            "delivery_unrecoverable_gap",
            task,
            delivery,
            "unacknowledged delivery has no outbox or accepted evidence",
            false,
          ),
        );
      if (
        delivery.acknowledgedAt &&
        !matchingEvidence.some((value) => value.accepted)
      )
        findings.push(
          deliveryFinding(
            "delivery_ack_without_evidence",
            task,
            delivery,
            "delivery ACK has no accepted physical evidence",
          ),
        );
      if (
        matchingOutbox.some((value) => value.phase === "acknowledged") &&
        !delivery.acknowledgedAt
      )
        findings.push(
          deliveryFinding(
            "outbox_ack_not_projected",
            task,
            delivery,
            "outbox ACK is not reflected in canonical task delivery",
          ),
        );
    }
    if (
      isTerminal(task.status) &&
      !task.deliveries.some(
        (delivery) => delivery.kind === "final" || delivery.kind === "error",
      )
    )
      findings.push({
        code: "terminal_delivery_missing",
        taskId: task.identity.taskId,
        deliveryId: "",
        sequence: 0,
        detail: "terminal task has no final/error delivery",
        recoverable: true,
      });
    const payload = {
      taskId: task.identity.taskId,
      valid: findings.every((finding) => finding.recoverable),
      finalCount: task.deliveries.filter(
        (delivery) => delivery.kind === "final" || delivery.kind === "error",
      ).length,
      pendingCount: task.deliveries.filter(
        (delivery) => !delivery.acknowledgedAt,
      ).length,
      acknowledgedCount: task.deliveries.filter(
        (delivery) => delivery.acknowledgedAt,
      ).length,
      evidenceCount: evidence.filter(
        (value) => value.taskId === task.identity.taskId,
      ).length,
      findings,
    };
    return { ...payload, digest: digest(payload) };
  }

  assert(
    task: E03TaskState,
    evidence: readonly DeliveryEvidence[],
    outbox: readonly DeliveryOutboxRecord[],
  ): DeliveryReconciliationReport {
    const report = this.reconcile(task, evidence, outbox);
    if (!report.valid)
      throw new E03RuntimeError(
        "delivery_reconciliation_failed",
        report.findings
          .filter((finding) => !finding.recoverable)
          .map((finding) => finding.code)
          .join(", "),
        {
          taskId: report.taskId,
          findings: report.findings as unknown as JsonObject[],
        },
      );
    return report;
  }
}

function sealEvidence(
  value: Omit<DeliveryEvidence, "digest"> | DeliveryEvidence,
): DeliveryEvidence {
  const { digest: _digest, ...payload } = value as DeliveryEvidence;
  return { ...payload, digest: digest(payload) };
}

function assertDelivery(delivery: E03Delivery): void {
  const { digest: checksum, ...payload } = delivery;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "delivery_digest_mismatch",
      `delivery ${delivery.deliveryId} digest is invalid`,
    );
  if (
    !delivery.deliveryId ||
    !delivery.taskId ||
    !delivery.idempotencyKey ||
    delivery.sequence < 1
  )
    throw new E03RuntimeError(
      "invalid_delivery",
      "delivery identity is incomplete",
    );
}

function assertFragment(fragment: YieldFragment): void {
  const { digest: checksum, ...payload } = fragment;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "yield_fragment_digest",
      `yield fragment ${fragment.fragmentId} digest is invalid`,
    );
  if (
    !fragment.fragmentId ||
    !fragment.taskId ||
    !fragment.leaseId ||
    !fragment.idempotencyKey ||
    fragment.sequence < 1
  )
    throw new E03RuntimeError(
      "invalid_yield_fragment",
      "yield fragment identity is incomplete",
    );
}

function permanentDeliveryError(error: string): boolean {
  return /permission|denied|unauthorized|forbidden|invalid|malformed|unsupported|unknown destination/i.test(
    error,
  );
}

function deliveryFinding(
  code: string,
  task: E03TaskState,
  delivery: E03Delivery,
  detail: string,
  recoverable = true,
): DeliveryReconciliationFinding {
  return {
    code,
    taskId: task.identity.taskId,
    deliveryId: delivery.deliveryId,
    sequence: delivery.sequence,
    detail,
    recoverable,
  };
}

export type DeliveryStreamState =
  | "opening"
  | "open"
  | "paused"
  | "draining"
  | "closed"
  | "failed";

export interface DeliveryStream {
  streamId: string;
  taskId: string;
  sessionId: string;
  leaseId: string;
  destinationTaskId: string;
  state: DeliveryStreamState;
  contentType: string;
  maximumChunkBytes: number;
  maximumPendingChunks: number;
  maximumPendingBytes: number;
  nextSequence: number;
  highestAcknowledgedSequence: number;
  pendingChunks: number;
  pendingBytes: number;
  sentChunks: number;
  sentBytes: number;
  duplicateAcknowledgments: number;
  openedAt: string;
  lastWriteAt: string;
  lastAckAt: string;
  closedAt: string | null;
  closeReason: string;
  revision: number;
  digest: string;
}

export interface DeliveryStreamChunk {
  chunkId: string;
  streamId: string;
  taskId: string;
  leaseId: string;
  destinationTaskId: string;
  sequence: number;
  contentType: string;
  payload: string;
  payloadBytes: number;
  payloadDigest: string;
  final: boolean;
  state: "pending" | "sent" | "acknowledged" | "failed" | "dropped";
  attempts: number;
  maximumAttempts: number;
  idempotencyKey: string;
  createdAt: string;
  sentAt: string | null;
  acknowledgedAt: string | null;
  failedAt: string | null;
  errorCode: string;
  previousDigest: string;
  digest: string;
}

export interface DeliveryStreamProjection {
  streamId: string;
  taskId: string;
  destinationTaskId: string;
  state: DeliveryStreamState;
  totalChunks: number;
  pendingChunks: number;
  sentChunks: number;
  acknowledgedChunks: number;
  failedChunks: number;
  droppedChunks: number;
  pendingBytes: number;
  totalBytes: number;
  complete: boolean;
  finalChunkId: string | null;
  projectedAt: string;
  digest: string;
}

function assertDeliveryStream(stream: DeliveryStream): void {
  const { digest: checksum, ...payload } = stream;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "delivery_stream_checksum",
      `delivery stream ${stream.streamId} checksum mismatch`,
    );
  if (
    !stream.taskId ||
    !stream.leaseId ||
    !stream.destinationTaskId ||
    stream.maximumChunkBytes < 1 ||
    stream.maximumPendingChunks < 1 ||
    stream.maximumPendingBytes < stream.maximumChunkBytes ||
    stream.nextSequence < 1 ||
    stream.highestAcknowledgedSequence < 0 ||
    stream.pendingChunks < 0 ||
    stream.pendingBytes < 0 ||
    stream.revision < 1
  )
    throw new E03RuntimeError(
      "delivery_stream_invalid",
      `delivery stream ${stream.streamId} is invalid`,
    );
  if (["closed", "failed"].includes(stream.state) && !stream.closedAt)
    throw new E03RuntimeError(
      "delivery_stream_close_time",
      `closed delivery stream ${stream.streamId} has no close time`,
    );
}

function assertDeliveryChunk(chunk: DeliveryStreamChunk): void {
  const { digest: checksum, ...payload } = chunk;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "delivery_chunk_checksum",
      `delivery chunk ${chunk.chunkId} checksum mismatch`,
    );
  if (
    !chunk.streamId ||
    !chunk.taskId ||
    !chunk.leaseId ||
    chunk.sequence < 1 ||
    chunk.payloadBytes < 0 ||
    chunk.attempts < 0 ||
    chunk.maximumAttempts < 1
  )
    throw new E03RuntimeError(
      "delivery_chunk_invalid",
      `delivery chunk ${chunk.chunkId} is invalid`,
    );
  if (Buffer.byteLength(chunk.payload, "utf8") !== chunk.payloadBytes)
    throw new E03RuntimeError(
      "delivery_chunk_length",
      `delivery chunk ${chunk.chunkId} length mismatch`,
    );
  if (digest(chunk.payload) !== chunk.payloadDigest)
    throw new E03RuntimeError(
      "delivery_chunk_payload_digest",
      `delivery chunk ${chunk.chunkId} payload digest mismatch`,
    );
  if (chunk.state === "sent" && !chunk.sentAt)
    throw new E03RuntimeError(
      "delivery_chunk_sent_time",
      `sent delivery chunk ${chunk.chunkId} has no sent time`,
    );
  if (chunk.state === "acknowledged" && !chunk.acknowledgedAt)
    throw new E03RuntimeError(
      "delivery_chunk_ack_time",
      `acknowledged delivery chunk ${chunk.chunkId} has no ack time`,
    );
}

function resealDeliveryStream(
  stream: DeliveryStream,
  patch: Partial<Omit<DeliveryStream, "streamId" | "taskId" | "digest">>,
): DeliveryStream {
  const { digest: _, ...prior } = stream;
  const payload = {
    ...prior,
    ...patch,
    streamId: stream.streamId,
    taskId: stream.taskId,
  };
  const next = { ...payload, digest: digest(payload) };
  assertDeliveryStream(next);
  return next;
}

function resealDeliveryChunk(
  chunk: DeliveryStreamChunk,
  patch: Partial<Omit<DeliveryStreamChunk, "chunkId" | "streamId" | "digest">>,
): DeliveryStreamChunk {
  const { digest: _, ...prior } = chunk;
  const payload = {
    ...prior,
    ...patch,
    chunkId: chunk.chunkId,
    streamId: chunk.streamId,
  };
  const next = { ...payload, digest: digest(payload) };
  assertDeliveryChunk(next);
  return next;
}

export class DeliveryStreamRuntime {
  private streams = new Map<string, DeliveryStream>();
  private chunks = new Map<string, DeliveryStreamChunk[]>();
  private chunkById = new Map<string, DeliveryStreamChunk>();
  private idempotency = new Map<string, string>();

  open(input: {
    task: E03TaskState;
    destinationTaskId: string;
    contentType: string;
    maximumChunkBytes?: number;
    maximumPendingChunks?: number;
    maximumPendingBytes?: number;
    now?: string;
  }): DeliveryStream {
    if (!input.task.scope.allowTeamMessaging)
      throw new E03RuntimeError(
        "delivery_stream_messaging_denied",
        "task scope denies delivery streaming",
      );
    const destinationTaskId = input.destinationTaskId.trim();
    const contentType = input.contentType.trim();
    if (!destinationTaskId || !contentType)
      throw new E03RuntimeError(
        "delivery_stream_identity_missing",
        "delivery stream destination and content type are required",
      );
    const maximumChunkBytes = input.maximumChunkBytes ?? 256 * 1024;
    const maximumPendingChunks = input.maximumPendingChunks ?? 128;
    const maximumPendingBytes =
      input.maximumPendingBytes ?? maximumChunkBytes * maximumPendingChunks;
    if (
      !Number.isSafeInteger(maximumChunkBytes) ||
      !Number.isSafeInteger(maximumPendingChunks) ||
      !Number.isSafeInteger(maximumPendingBytes) ||
      maximumChunkBytes < 1 ||
      maximumPendingChunks < 1 ||
      maximumPendingBytes < maximumChunkBytes
    )
      throw new E03RuntimeError(
        "delivery_stream_limits_invalid",
        "delivery stream limits are invalid",
      );
    const openedAt = input.now ?? new Date().toISOString();
    const payload = {
      streamId: `delivery-stream-${digest({
        taskId: input.task.identity.taskId,
        leaseId: input.task.identity.leaseId,
        destinationTaskId,
        contentType,
      }).slice(0, 32)}`,
      taskId: input.task.identity.taskId,
      sessionId: input.task.identity.sessionId,
      leaseId: input.task.identity.leaseId,
      destinationTaskId,
      state: "open" as const,
      contentType,
      maximumChunkBytes,
      maximumPendingChunks,
      maximumPendingBytes,
      nextSequence: 1,
      highestAcknowledgedSequence: 0,
      pendingChunks: 0,
      pendingBytes: 0,
      sentChunks: 0,
      sentBytes: 0,
      duplicateAcknowledgments: 0,
      openedAt,
      lastWriteAt: openedAt,
      lastAckAt: openedAt,
      closedAt: null,
      closeReason: "",
      revision: 1,
    };
    const stream = { ...payload, digest: digest(payload) };
    assertDeliveryStream(stream);
    const existing = this.streams.get(stream.streamId);
    if (existing) return structuredClone(existing);
    this.streams.set(stream.streamId, stream);
    this.chunks.set(stream.streamId, []);
    return structuredClone(stream);
  }

  append(input: {
    streamId: string;
    task: E03TaskState;
    payload: string;
    final?: boolean;
    maximumAttempts?: number;
    idempotencyKey: string;
    now?: string;
  }): DeliveryStreamChunk {
    let stream = this.requireStream(input.streamId);
    if (stream.state !== "open")
      throw new E03RuntimeError(
        "delivery_stream_not_open",
        `delivery stream ${stream.streamId} is ${stream.state}`,
      );
    if (
      stream.taskId !== input.task.identity.taskId ||
      stream.leaseId !== input.task.identity.leaseId
    )
      throw new E03RuntimeError(
        "delivery_stream_stale_lease",
        "delivery stream belongs to another task lease",
      );
    const idempotencyKey = input.idempotencyKey.trim();
    const bound = this.idempotency.get(idempotencyKey);
    if (bound) return structuredClone(this.chunkById.get(bound)!);
    const payloadBytes = Buffer.byteLength(input.payload, "utf8");
    if (payloadBytes > stream.maximumChunkBytes)
      throw new E03RuntimeError(
        "delivery_chunk_too_large",
        `delivery chunk ${payloadBytes} exceeds ${stream.maximumChunkBytes}`,
      );
    if (
      stream.pendingChunks >= stream.maximumPendingChunks ||
      stream.pendingBytes + payloadBytes > stream.maximumPendingBytes
    )
      throw new E03RuntimeError(
        "delivery_stream_backpressure",
        `delivery stream ${stream.streamId} is backpressured`,
      );
    const current = this.chunks.get(stream.streamId) ?? [];
    if (current.at(-1)?.final)
      throw new E03RuntimeError(
        "delivery_chunk_after_final",
        "cannot append delivery chunk after final chunk",
      );
    const maximumAttempts = input.maximumAttempts ?? 3;
    if (!Number.isSafeInteger(maximumAttempts) || maximumAttempts < 1)
      throw new E03RuntimeError(
        "delivery_chunk_attempt_limit",
        "delivery chunk maximum attempts is invalid",
      );
    const createdAt = input.now ?? new Date().toISOString();
    const body = {
      chunkId: `delivery-chunk-${digest({
        streamId: stream.streamId,
        sequence: stream.nextSequence,
        payloadDigest: digest(input.payload),
        idempotencyKey,
      }).slice(0, 32)}`,
      streamId: stream.streamId,
      taskId: stream.taskId,
      leaseId: stream.leaseId,
      destinationTaskId: stream.destinationTaskId,
      sequence: stream.nextSequence,
      contentType: stream.contentType,
      payload: input.payload,
      payloadBytes,
      payloadDigest: digest(input.payload),
      final: input.final ?? false,
      state: "pending" as const,
      attempts: 0,
      maximumAttempts,
      idempotencyKey,
      createdAt,
      sentAt: null,
      acknowledgedAt: null,
      failedAt: null,
      errorCode: "",
      previousDigest: current.at(-1)?.digest ?? "",
    };
    const chunk = { ...body, digest: digest(body) };
    assertDeliveryChunk(chunk);
    current.push(chunk);
    this.chunks.set(stream.streamId, current);
    this.chunkById.set(chunk.chunkId, chunk);
    this.idempotency.set(idempotencyKey, chunk.chunkId);
    stream = resealDeliveryStream(stream, {
      nextSequence: stream.nextSequence + 1,
      pendingChunks: stream.pendingChunks + 1,
      pendingBytes: stream.pendingBytes + payloadBytes,
      lastWriteAt: createdAt,
      state: chunk.final ? "draining" : stream.state,
      revision: stream.revision + 1,
    });
    this.streams.set(stream.streamId, stream);
    return structuredClone(chunk);
  }

  acquire(streamId: string, maximum: number): DeliveryStreamChunk[] {
    const stream = this.requireStream(streamId);
    if (!["open", "draining"].includes(stream.state))
      throw new E03RuntimeError(
        "delivery_stream_not_writable",
        `delivery stream ${streamId} is ${stream.state}`,
      );
    if (!Number.isSafeInteger(maximum) || maximum < 1)
      throw new E03RuntimeError(
        "delivery_chunk_acquire_limit",
        "delivery chunk acquire maximum is invalid",
      );
    const selected = (this.chunks.get(streamId) ?? [])
      .filter((chunk) => chunk.state === "pending")
      .sort((left, right) => left.sequence - right.sequence)
      .slice(0, maximum)
      .map((chunk) => {
        if (chunk.attempts >= chunk.maximumAttempts)
          throw new E03RuntimeError(
            "delivery_chunk_attempts_exhausted",
            `delivery chunk ${chunk.chunkId} exhausted attempts`,
          );
        const next = resealDeliveryChunk(chunk, {
          state: "sent",
          attempts: chunk.attempts + 1,
          sentAt: new Date().toISOString(),
          failedAt: null,
          errorCode: "",
        });
        this.replaceChunk(next);
        return next;
      });
    return selected.map((chunk) => structuredClone(chunk));
  }

  acknowledge(
    streamId: string,
    sequence: number,
    now = new Date().toISOString(),
  ): DeliveryStreamChunk[] {
    let stream = this.requireStream(streamId);
    if (!Number.isSafeInteger(sequence) || sequence < 0)
      throw new E03RuntimeError(
        "delivery_stream_ack_sequence",
        "delivery stream acknowledgment sequence is invalid",
      );
    if (sequence >= stream.nextSequence)
      throw new E03RuntimeError(
        "delivery_stream_ack_ahead",
        "delivery stream acknowledgment is ahead of written chunks",
      );
    if (sequence <= stream.highestAcknowledgedSequence) {
      stream = resealDeliveryStream(stream, {
        duplicateAcknowledgments: stream.duplicateAcknowledgments + 1,
        lastAckAt: now,
        revision: stream.revision + 1,
      });
      this.streams.set(streamId, stream);
      return [];
    }
    const acknowledged: DeliveryStreamChunk[] = [];
    let bytes = 0;
    for (const chunk of this.chunks.get(streamId) ?? []) {
      if (chunk.sequence > sequence || chunk.state === "acknowledged") continue;
      if (chunk.state !== "sent")
        throw new E03RuntimeError(
          "delivery_chunk_ack_before_send",
          `delivery chunk ${chunk.chunkId} acknowledged before send`,
        );
      const next = resealDeliveryChunk(chunk, {
        state: "acknowledged",
        acknowledgedAt: now,
      });
      this.replaceChunk(next);
      acknowledged.push(next);
      bytes += next.payloadBytes;
    }
    stream = resealDeliveryStream(stream, {
      highestAcknowledgedSequence: sequence,
      pendingChunks: Math.max(0, stream.pendingChunks - acknowledged.length),
      pendingBytes: Math.max(0, stream.pendingBytes - bytes),
      sentChunks: stream.sentChunks + acknowledged.length,
      sentBytes: stream.sentBytes + bytes,
      lastAckAt: now,
      state:
        stream.state === "draining" && sequence === stream.nextSequence - 1
          ? "closed"
          : stream.state,
      closedAt:
        stream.state === "draining" && sequence === stream.nextSequence - 1
          ? now
          : stream.closedAt,
      closeReason:
        stream.state === "draining" && sequence === stream.nextSequence - 1
          ? "final_chunk_acknowledged"
          : stream.closeReason,
      revision: stream.revision + 1,
    });
    this.streams.set(streamId, stream);
    return acknowledged.map((chunk) => structuredClone(chunk));
  }

  fail(input: {
    chunkId: string;
    errorCode: string;
    retryable: boolean;
    now?: string;
  }): DeliveryStreamChunk {
    const current = this.requireChunk(input.chunkId);
    if (current.state !== "sent")
      throw new E03RuntimeError(
        "delivery_chunk_not_sent",
        `delivery chunk ${current.chunkId} cannot fail from ${current.state}`,
      );
    const retry = input.retryable && current.attempts < current.maximumAttempts;
    const next = resealDeliveryChunk(current, {
      state: retry ? "pending" : "failed",
      failedAt: retry ? null : (input.now ?? new Date().toISOString()),
      errorCode: input.errorCode.trim() || "delivery_chunk_failed",
    });
    this.replaceChunk(next);
    if (!retry) {
      const stream = this.requireStream(next.streamId);
      const failed = resealDeliveryStream(stream, {
        state: "failed",
        closedAt: input.now ?? new Date().toISOString(),
        closeReason: next.errorCode,
        revision: stream.revision + 1,
      });
      this.streams.set(failed.streamId, failed);
    }
    return structuredClone(next);
  }

  project(streamId: string): DeliveryStreamProjection {
    const stream = this.requireStream(streamId);
    const chunks = this.chunks.get(streamId) ?? [];
    const final = chunks.find((chunk) => chunk.final) ?? null;
    const payload = {
      streamId,
      taskId: stream.taskId,
      destinationTaskId: stream.destinationTaskId,
      state: stream.state,
      totalChunks: chunks.length,
      pendingChunks: chunks.filter((chunk) => chunk.state === "pending").length,
      sentChunks: chunks.filter((chunk) => chunk.state === "sent").length,
      acknowledgedChunks: chunks.filter(
        (chunk) => chunk.state === "acknowledged",
      ).length,
      failedChunks: chunks.filter((chunk) => chunk.state === "failed").length,
      droppedChunks: chunks.filter((chunk) => chunk.state === "dropped").length,
      pendingBytes: stream.pendingBytes,
      totalBytes: chunks.reduce((sum, chunk) => sum + chunk.payloadBytes, 0),
      complete: Boolean(final?.acknowledgedAt),
      finalChunkId: final?.chunkId ?? null,
      projectedAt: new Date().toISOString(),
    };
    return { ...payload, digest: digest(payload) };
  }

  restore(input: {
    streams: readonly DeliveryStream[];
    chunks: readonly DeliveryStreamChunk[];
  }): void {
    const streams = new Map<string, DeliveryStream>();
    const chunks = new Map<string, DeliveryStreamChunk[]>();
    const chunkById = new Map<string, DeliveryStreamChunk>();
    const idempotency = new Map<string, string>();
    for (const raw of input.streams) {
      const stream = structuredClone(raw);
      assertDeliveryStream(stream);
      if (streams.has(stream.streamId))
        throw new E03RuntimeError(
          "duplicate_delivery_stream",
          `delivery stream ${stream.streamId} repeats`,
        );
      streams.set(stream.streamId, stream);
      chunks.set(stream.streamId, []);
    }
    for (const raw of input.chunks) {
      const chunk = structuredClone(raw);
      assertDeliveryChunk(chunk);
      if (!streams.has(chunk.streamId))
        throw new E03RuntimeError(
          "delivery_chunk_stream_missing",
          `delivery chunk ${chunk.chunkId} stream is missing`,
        );
      if (chunkById.has(chunk.chunkId) || idempotency.has(chunk.idempotencyKey))
        throw new E03RuntimeError(
          "duplicate_delivery_chunk",
          `delivery chunk ${chunk.chunkId} repeats`,
        );
      const current = chunks.get(chunk.streamId)!;
      if (
        chunk.sequence !== current.length + 1 ||
        chunk.previousDigest !== (current.at(-1)?.digest ?? "")
      )
        throw new E03RuntimeError(
          "delivery_chunk_chain",
          `delivery stream ${chunk.streamId} chunk chain is invalid`,
        );
      current.push(chunk);
      chunkById.set(chunk.chunkId, chunk);
      idempotency.set(chunk.idempotencyKey, chunk.chunkId);
    }
    this.streams = streams;
    this.chunks = chunks;
    this.chunkById = chunkById;
    this.idempotency = idempotency;
  }

  snapshot(): {
    streams: DeliveryStream[];
    chunks: DeliveryStreamChunk[];
  } {
    return {
      streams: [...this.streams.values()]
        .sort((left, right) => left.streamId.localeCompare(right.streamId))
        .map((stream) => structuredClone(stream)),
      chunks: [...this.chunks.values()]
        .flat()
        .sort(
          (left, right) =>
            left.streamId.localeCompare(right.streamId) ||
            left.sequence - right.sequence,
        )
        .map((chunk) => structuredClone(chunk)),
    };
  }

  private replaceChunk(chunk: DeliveryStreamChunk): void {
    const current = this.chunks.get(chunk.streamId);
    if (!current)
      throw new E03RuntimeError(
        "delivery_chunk_stream_missing",
        `delivery chunk ${chunk.chunkId} stream is missing`,
      );
    const index = current.findIndex((value) => value.chunkId === chunk.chunkId);
    if (index < 0)
      throw new E03RuntimeError(
        "delivery_chunk_missing",
        `delivery chunk ${chunk.chunkId} is missing`,
      );
    current[index] = chunk;
    this.chunks.set(chunk.streamId, current);
    this.chunkById.set(chunk.chunkId, chunk);
  }

  private requireStream(streamId: string): DeliveryStream {
    const stream = this.streams.get(streamId);
    if (!stream)
      throw new E03RuntimeError(
        "delivery_stream_missing",
        `delivery stream ${streamId} is missing`,
      );
    assertDeliveryStream(stream);
    return stream;
  }

  private requireChunk(chunkId: string): DeliveryStreamChunk {
    const chunk = this.chunkById.get(chunkId);
    if (!chunk)
      throw new E03RuntimeError(
        "delivery_chunk_missing",
        `delivery chunk ${chunkId} is missing`,
      );
    assertDeliveryChunk(chunk);
    return chunk;
  }
}

export type DeliveryQuorumMode = "all" | "majority" | "minimum" | "weighted";

export interface DeliveryQuorumMember {
  memberId: string;
  taskId: string;
  required: boolean;
  weight: number;
  role: string;
  acknowledgedDeliveryIds: string[];
  rejectedDeliveryIds: string[];
  lastAcknowledgedAt: string | null;
  lastRejectedAt: string | null;
  digest: string;
}

export interface DeliveryQuorum {
  quorumId: string;
  sourceTaskId: string;
  sourceLeaseId: string;
  deliveryId: string;
  mode: DeliveryQuorumMode;
  minimumCount: number;
  minimumWeight: number;
  members: DeliveryQuorumMember[];
  deadlineAt: string;
  state: "open" | "reached" | "failed" | "expired" | "cancelled";
  reachedAt: string | null;
  failedAt: string | null;
  failureReason: string;
  revision: number;
  createdAt: string;
  digest: string;
}

export interface DeliveryQuorumProjection {
  quorumId: string;
  deliveryId: string;
  state: DeliveryQuorum["state"];
  acknowledgedMembers: string[];
  rejectedMembers: string[];
  pendingMembers: string[];
  requiredPendingMembers: string[];
  acknowledgedCount: number;
  acknowledgedWeight: number;
  requiredCount: number;
  requiredWeight: number;
  reached: boolean;
  impossible: boolean;
  expired: boolean;
  evaluatedAt: string;
  digest: string;
}

function assertQuorumMember(member: DeliveryQuorumMember): void {
  const { digest: checksum, ...payload } = member;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "delivery_quorum_member_checksum",
      `delivery quorum member ${member.memberId} checksum mismatch`,
    );
  if (
    !member.memberId ||
    !member.taskId ||
    !member.role ||
    !Number.isFinite(member.weight) ||
    member.weight <= 0
  )
    throw new E03RuntimeError(
      "delivery_quorum_member_invalid",
      `delivery quorum member ${member.memberId} is invalid`,
    );
}

function assertDeliveryQuorum(quorum: DeliveryQuorum): void {
  const { digest: checksum, ...payload } = quorum;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "delivery_quorum_checksum",
      `delivery quorum ${quorum.quorumId} checksum mismatch`,
    );
  if (
    !quorum.sourceTaskId ||
    !quorum.sourceLeaseId ||
    !quorum.deliveryId ||
    !quorum.members.length ||
    quorum.minimumCount < 1 ||
    quorum.minimumWeight <= 0 ||
    quorum.revision < 1 ||
    Date.parse(quorum.deadlineAt) <= Date.parse(quorum.createdAt)
  )
    throw new E03RuntimeError(
      "delivery_quorum_invalid",
      `delivery quorum ${quorum.quorumId} is invalid`,
    );
  const ids = new Set<string>();
  for (const member of quorum.members) {
    assertQuorumMember(member);
    if (ids.has(member.memberId))
      throw new E03RuntimeError(
        "duplicate_delivery_quorum_member",
        `delivery quorum member ${member.memberId} repeats`,
      );
    ids.add(member.memberId);
  }
  if (
    ["reached", "failed", "expired"].includes(quorum.state) &&
    !quorum.reachedAt &&
    !quorum.failedAt
  )
    throw new E03RuntimeError(
      "delivery_quorum_terminal_time",
      `terminal delivery quorum ${quorum.quorumId} has no terminal time`,
    );
}

function resealQuorum(
  quorum: DeliveryQuorum,
  patch: Partial<Omit<DeliveryQuorum, "quorumId" | "deliveryId" | "digest">>,
): DeliveryQuorum {
  const { digest: _, ...prior } = quorum;
  const payload = {
    ...prior,
    ...patch,
    quorumId: quorum.quorumId,
    deliveryId: quorum.deliveryId,
  };
  const next = { ...payload, digest: digest(payload) };
  assertDeliveryQuorum(next);
  return next;
}

export class DeliveryQuorumRuntime {
  private quorums = new Map<string, DeliveryQuorum>();
  private byDelivery = new Map<string, string>();

  create(input: {
    task: E03TaskState;
    delivery: E03Delivery;
    mode: DeliveryQuorumMode;
    members: readonly {
      memberId: string;
      taskId: string;
      required?: boolean;
      weight?: number;
      role?: string;
    }[];
    minimumCount?: number;
    minimumWeight?: number;
    timeoutMs: number;
    now?: string;
  }): DeliveryQuorum {
    if (input.delivery.taskId !== input.task.identity.taskId)
      throw new E03RuntimeError(
        "delivery_quorum_custody",
        "delivery quorum input belongs to another task lease",
      );
    if (!input.members.length)
      throw new E03RuntimeError(
        "delivery_quorum_members_empty",
        "delivery quorum requires members",
      );
    if (!Number.isSafeInteger(input.timeoutMs) || input.timeoutMs < 1)
      throw new E03RuntimeError(
        "delivery_quorum_timeout_invalid",
        "delivery quorum timeout is invalid",
      );
    const members = input.members.map((raw) => {
      const payload = {
        memberId: raw.memberId.trim(),
        taskId: raw.taskId.trim(),
        required: raw.required ?? false,
        weight: raw.weight ?? 1,
        role: raw.role?.trim() || "recipient",
        acknowledgedDeliveryIds: [],
        rejectedDeliveryIds: [],
        lastAcknowledgedAt: null,
        lastRejectedAt: null,
      };
      const member = { ...payload, digest: digest(payload) };
      assertQuorumMember(member);
      return member;
    });
    if (
      new Set(members.map((member) => member.memberId)).size !== members.length
    )
      throw new E03RuntimeError(
        "duplicate_delivery_quorum_member",
        "delivery quorum members repeat",
      );
    const minimumCount =
      input.mode === "all"
        ? members.length
        : input.mode === "majority"
          ? Math.floor(members.length / 2) + 1
          : (input.minimumCount ?? 1);
    const totalWeight = members.reduce((sum, member) => sum + member.weight, 0);
    const minimumWeight =
      input.mode === "weighted"
        ? (input.minimumWeight ?? totalWeight / 2)
        : input.mode === "all"
          ? totalWeight
          : (input.minimumWeight ?? 1);
    if (
      !Number.isSafeInteger(minimumCount) ||
      minimumCount < 1 ||
      minimumCount > members.length ||
      !Number.isFinite(minimumWeight) ||
      minimumWeight <= 0 ||
      minimumWeight > totalWeight
    )
      throw new E03RuntimeError(
        "delivery_quorum_threshold_invalid",
        "delivery quorum threshold is invalid",
      );
    const createdAt = input.now ?? new Date().toISOString();
    const payload = {
      quorumId: `delivery-quorum-${digest({
        deliveryId: input.delivery.deliveryId,
        members: members.map((member) => member.digest),
        mode: input.mode,
      }).slice(0, 32)}`,
      sourceTaskId: input.task.identity.taskId,
      sourceLeaseId: input.task.identity.leaseId,
      deliveryId: input.delivery.deliveryId,
      mode: input.mode,
      minimumCount,
      minimumWeight,
      members,
      deadlineAt: new Date(
        Date.parse(createdAt) + input.timeoutMs,
      ).toISOString(),
      state: "open" as const,
      reachedAt: null,
      failedAt: null,
      failureReason: "",
      revision: 1,
      createdAt,
    };
    const quorum = { ...payload, digest: digest(payload) };
    assertDeliveryQuorum(quorum);
    const existingId = this.byDelivery.get(quorum.deliveryId);
    if (existingId) return structuredClone(this.quorums.get(existingId)!);
    this.quorums.set(quorum.quorumId, quorum);
    this.byDelivery.set(quorum.deliveryId, quorum.quorumId);
    return structuredClone(quorum);
  }

  acknowledge(input: {
    quorumId: string;
    memberId: string;
    deliveryId: string;
    accepted: boolean;
    reason?: string;
    now?: string;
  }): DeliveryQuorum {
    const current = this.require(input.quorumId);
    if (current.state !== "open") return structuredClone(current);
    if (current.deliveryId !== input.deliveryId)
      throw new E03RuntimeError(
        "delivery_quorum_delivery_mismatch",
        "delivery quorum acknowledgment belongs to another delivery",
      );
    const member = current.members.find(
      (candidate) => candidate.memberId === input.memberId,
    );
    if (!member)
      throw new E03RuntimeError(
        "delivery_quorum_member_missing",
        `delivery quorum member ${input.memberId} is missing`,
      );
    if (
      member.acknowledgedDeliveryIds.includes(input.deliveryId) ||
      member.rejectedDeliveryIds.includes(input.deliveryId)
    )
      return structuredClone(current);
    const now = input.now ?? new Date().toISOString();
    const members = current.members.map((candidate) => {
      if (candidate.memberId !== input.memberId) return candidate;
      const { digest: _, ...prior } = candidate;
      const payload = {
        ...prior,
        acknowledgedDeliveryIds: input.accepted
          ? [...candidate.acknowledgedDeliveryIds, input.deliveryId]
          : candidate.acknowledgedDeliveryIds,
        rejectedDeliveryIds: input.accepted
          ? candidate.rejectedDeliveryIds
          : [...candidate.rejectedDeliveryIds, input.deliveryId],
        lastAcknowledgedAt: input.accepted ? now : candidate.lastAcknowledgedAt,
        lastRejectedAt: input.accepted ? candidate.lastRejectedAt : now,
      };
      const next = { ...payload, digest: digest(payload) };
      assertQuorumMember(next);
      return next;
    });
    let next = resealQuorum(current, {
      members,
      revision: current.revision + 1,
    });
    const projection = this.project(next, now);
    if (projection.reached)
      next = resealQuorum(next, {
        state: "reached",
        reachedAt: now,
        revision: next.revision + 1,
      });
    else if (projection.impossible)
      next = resealQuorum(next, {
        state: "failed",
        failedAt: now,
        failureReason: input.reason?.trim() || "quorum_impossible",
        revision: next.revision + 1,
      });
    this.quorums.set(next.quorumId, next);
    return structuredClone(next);
  }

  evaluate(quorumId: string, now = new Date().toISOString()): DeliveryQuorum {
    let current = this.require(quorumId);
    if (current.state !== "open") return structuredClone(current);
    const projection = this.project(current, now);
    if (projection.expired)
      current = resealQuorum(current, {
        state: "expired",
        failedAt: now,
        failureReason: "quorum_deadline_expired",
        revision: current.revision + 1,
      });
    else if (projection.reached)
      current = resealQuorum(current, {
        state: "reached",
        reachedAt: now,
        revision: current.revision + 1,
      });
    else if (projection.impossible)
      current = resealQuorum(current, {
        state: "failed",
        failedAt: now,
        failureReason: "quorum_impossible",
        revision: current.revision + 1,
      });
    this.quorums.set(current.quorumId, current);
    return structuredClone(current);
  }

  project(
    quorumOrId: DeliveryQuorum | string,
    now = new Date().toISOString(),
  ): DeliveryQuorumProjection {
    const quorum =
      typeof quorumOrId === "string" ? this.require(quorumOrId) : quorumOrId;
    assertDeliveryQuorum(quorum);
    const acknowledgedMembers = quorum.members
      .filter((member) =>
        member.acknowledgedDeliveryIds.includes(quorum.deliveryId),
      )
      .map((member) => member.memberId)
      .sort();
    const rejectedMembers = quorum.members
      .filter((member) =>
        member.rejectedDeliveryIds.includes(quorum.deliveryId),
      )
      .map((member) => member.memberId)
      .sort();
    const pendingMembers = quorum.members
      .filter(
        (member) =>
          !acknowledgedMembers.includes(member.memberId) &&
          !rejectedMembers.includes(member.memberId),
      )
      .map((member) => member.memberId)
      .sort();
    const requiredPendingMembers = quorum.members
      .filter(
        (member) => member.required && pendingMembers.includes(member.memberId),
      )
      .map((member) => member.memberId)
      .sort();
    const acknowledgedWeight = quorum.members
      .filter((member) => acknowledgedMembers.includes(member.memberId))
      .reduce((sum, member) => sum + member.weight, 0);
    const pendingWeight = quorum.members
      .filter((member) => pendingMembers.includes(member.memberId))
      .reduce((sum, member) => sum + member.weight, 0);
    const requiredRejected = quorum.members.some(
      (member) => member.required && rejectedMembers.includes(member.memberId),
    );
    const reached =
      !requiredRejected &&
      acknowledgedMembers.length >= quorum.minimumCount &&
      acknowledgedWeight >= quorum.minimumWeight;
    const impossible =
      requiredRejected ||
      acknowledgedMembers.length + pendingMembers.length <
        quorum.minimumCount ||
      acknowledgedWeight + pendingWeight < quorum.minimumWeight;
    const payload = {
      quorumId: quorum.quorumId,
      deliveryId: quorum.deliveryId,
      state: quorum.state,
      acknowledgedMembers,
      rejectedMembers,
      pendingMembers,
      requiredPendingMembers,
      acknowledgedCount: acknowledgedMembers.length,
      acknowledgedWeight,
      requiredCount: quorum.minimumCount,
      requiredWeight: quorum.minimumWeight,
      reached,
      impossible,
      expired: Date.parse(now) >= Date.parse(quorum.deadlineAt),
      evaluatedAt: now,
    };
    return { ...payload, digest: digest(payload) };
  }

  restore(quorums: readonly DeliveryQuorum[]): void {
    const next = new Map<string, DeliveryQuorum>();
    const byDelivery = new Map<string, string>();
    for (const raw of quorums) {
      const quorum = structuredClone(raw);
      assertDeliveryQuorum(quorum);
      if (next.has(quorum.quorumId) || byDelivery.has(quorum.deliveryId))
        throw new E03RuntimeError(
          "duplicate_delivery_quorum",
          `delivery quorum ${quorum.quorumId} repeats`,
        );
      next.set(quorum.quorumId, quorum);
      byDelivery.set(quorum.deliveryId, quorum.quorumId);
    }
    this.quorums = next;
    this.byDelivery = byDelivery;
  }

  snapshot(): DeliveryQuorum[] {
    return [...this.quorums.values()]
      .sort((left, right) => left.quorumId.localeCompare(right.quorumId))
      .map((quorum) => structuredClone(quorum));
  }

  private require(quorumId: string): DeliveryQuorum {
    const quorum = this.quorums.get(quorumId);
    if (!quorum)
      throw new E03RuntimeError(
        "delivery_quorum_missing",
        `delivery quorum ${quorumId} is missing`,
      );
    assertDeliveryQuorum(quorum);
    return quorum;
  }
}

export interface DeliveryArtifactEntry {
  artifactId: string;
  relativePath: string;
  mediaType: string;
  byteLength: number;
  contentDigest: string;
  chunkIds: string[];
  required: boolean;
  executable: boolean;
  digest: string;
}

export interface DeliveryArtifactChunk {
  chunkId: string;
  bundleId: string;
  artifactId: string;
  ordinal: number;
  byteOffset: number;
  byteLength: number;
  content: string;
  contentDigest: string;
  previousChunkDigest: string;
  state: "prepared" | "transferred" | "verified" | "rejected";
  transferredAt: string | null;
  verifiedAt: string | null;
  rejectionReason: string | null;
  revision: number;
  digest: string;
}

export interface DeliveryArtifactBundle {
  bundleId: string;
  deliveryId: string;
  taskId: string;
  state: "building" | "sealed" | "transferring" | "verified" | "rejected";
  entries: DeliveryArtifactEntry[];
  totalBytes: number;
  rootDigest: string;
  createdAt: string;
  sealedAt: string | null;
  verifiedAt: string | null;
  revision: number;
  digest: string;
}

function assertDeliveryArtifactEntry(entry: DeliveryArtifactEntry): void {
  const { digest: checksum, ...payload } = entry;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "delivery_artifact_entry_digest",
      `delivery artifact ${entry.artifactId} digest is invalid`,
    );
  if (
    !entry.artifactId ||
    !entry.relativePath ||
    !entry.mediaType ||
    !entry.contentDigest
  )
    throw new E03RuntimeError(
      "delivery_artifact_entry_identity",
      "delivery artifact entry identity is incomplete",
    );
  if (
    entry.relativePath.startsWith("/") ||
    entry.relativePath.includes("../") ||
    entry.relativePath.includes("..\\")
  )
    throw new E03RuntimeError(
      "delivery_artifact_path",
      `delivery artifact path ${entry.relativePath} is unsafe`,
    );
  if (!Number.isSafeInteger(entry.byteLength) || entry.byteLength < 0)
    throw new E03RuntimeError(
      "delivery_artifact_length",
      "delivery artifact byte length is invalid",
    );
}

function assertDeliveryArtifactChunk(
  chunk: DeliveryArtifactChunk,
  previousChunkDigest: string,
): void {
  const { digest: checksum, ...payload } = chunk;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "delivery_artifact_chunk_digest",
      `delivery artifact chunk ${chunk.chunkId} digest is invalid`,
    );
  if (
    !chunk.chunkId ||
    !chunk.bundleId ||
    !chunk.artifactId ||
    !chunk.contentDigest
  )
    throw new E03RuntimeError(
      "delivery_artifact_chunk_identity",
      "delivery artifact chunk identity is incomplete",
    );
  if (
    !Number.isSafeInteger(chunk.ordinal) ||
    chunk.ordinal < 0 ||
    !Number.isSafeInteger(chunk.byteOffset) ||
    chunk.byteOffset < 0 ||
    !Number.isSafeInteger(chunk.byteLength) ||
    chunk.byteLength < 0
  )
    throw new E03RuntimeError(
      "delivery_artifact_chunk_position",
      "delivery artifact chunk position is invalid",
    );
  if (Buffer.byteLength(chunk.content, "base64") !== chunk.byteLength)
    throw new E03RuntimeError(
      "delivery_artifact_chunk_length",
      `delivery artifact chunk ${chunk.chunkId} byte length is invalid`,
    );
  if (digest(chunk.content) !== chunk.contentDigest)
    throw new E03RuntimeError(
      "delivery_artifact_chunk_content",
      `delivery artifact chunk ${chunk.chunkId} content is corrupt`,
    );
  if (chunk.previousChunkDigest !== previousChunkDigest)
    throw new E03RuntimeError(
      "delivery_artifact_chunk_chain",
      `delivery artifact chunk ${chunk.chunkId} does not extend prior chunk`,
    );
  if (!Number.isSafeInteger(chunk.revision) || chunk.revision < 1)
    throw new E03RuntimeError(
      "delivery_artifact_chunk_revision",
      "delivery artifact chunk revision is invalid",
    );
}

function assertDeliveryArtifactBundle(bundle: DeliveryArtifactBundle): void {
  const { digest: checksum, ...payload } = bundle;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "delivery_artifact_bundle_digest",
      `delivery artifact bundle ${bundle.bundleId} digest is invalid`,
    );
  if (!bundle.bundleId || !bundle.deliveryId || !bundle.taskId)
    throw new E03RuntimeError(
      "delivery_artifact_bundle_identity",
      "delivery artifact bundle identity is incomplete",
    );
  if (!Number.isSafeInteger(bundle.totalBytes) || bundle.totalBytes < 0)
    throw new E03RuntimeError(
      "delivery_artifact_bundle_length",
      "delivery artifact bundle byte length is invalid",
    );
  for (const entry of bundle.entries) assertDeliveryArtifactEntry(entry);
  if (bundle.state === "sealed" && !bundle.sealedAt)
    throw new E03RuntimeError(
      "delivery_artifact_bundle_state",
      "sealed delivery artifact bundle requires sealedAt",
    );
  if (bundle.state === "verified" && !bundle.verifiedAt)
    throw new E03RuntimeError(
      "delivery_artifact_bundle_state",
      "verified delivery artifact bundle requires verifiedAt",
    );
}

export class DeliveryArtifactRuntime {
  private bundles = new Map<string, DeliveryArtifactBundle>();
  private chunks = new Map<string, DeliveryArtifactChunk>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  create(delivery: E03Delivery): DeliveryArtifactBundle {
    assertDelivery(delivery);
    const prior = [...this.bundles.values()].find(
      (bundle) => bundle.deliveryId === delivery.deliveryId,
    );
    if (prior) return structuredClone(prior);
    const payload = {
      bundleId: createId("delivery-artifact-bundle"),
      deliveryId: delivery.deliveryId,
      taskId: delivery.taskId,
      state: "building" as const,
      entries: [],
      totalBytes: 0,
      rootDigest: "root",
      createdAt: this.clock.now(),
      sealedAt: null,
      verifiedAt: null,
      revision: 1,
    };
    const bundle = { ...payload, digest: digest(payload) };
    assertDeliveryArtifactBundle(bundle);
    this.bundles.set(bundle.bundleId, bundle);
    return structuredClone(bundle);
  }

  add(input: {
    bundleId: string;
    expectedRevision: number;
    artifactId: string;
    relativePath: string;
    mediaType: string;
    content: Uint8Array;
    chunkBytes?: number;
    required?: boolean;
    executable?: boolean;
  }): DeliveryArtifactBundle {
    const bundle = this.requireBundle(input.bundleId);
    this.assertBundleRevision(bundle, input.expectedRevision);
    if (bundle.state !== "building")
      throw new E03RuntimeError(
        "delivery_artifact_add_state",
        `delivery artifact bundle ${bundle.bundleId} is ${bundle.state}`,
      );
    if (bundle.entries.some((entry) => entry.artifactId === input.artifactId))
      throw new E03RuntimeError(
        "delivery_artifact_duplicate",
        `delivery artifact ${input.artifactId} already exists`,
      );
    const chunkBytes = input.chunkBytes ?? 64 * 1024;
    if (!Number.isSafeInteger(chunkBytes) || chunkBytes < 1)
      throw new E03RuntimeError(
        "delivery_artifact_chunk_size",
        "delivery artifact chunk size is invalid",
      );
    const chunkIds: string[] = [];
    let previousChunkDigest = "root";
    for (
      let offset = 0, ordinal = 0;
      offset < input.content.byteLength;
      offset += chunkBytes, ordinal += 1
    ) {
      const bytes = input.content.slice(
        offset,
        Math.min(input.content.byteLength, offset + chunkBytes),
      );
      const content = Buffer.from(bytes).toString("base64");
      const payload = {
        chunkId: createId("delivery-artifact-chunk"),
        bundleId: bundle.bundleId,
        artifactId: input.artifactId,
        ordinal,
        byteOffset: offset,
        byteLength: bytes.byteLength,
        content,
        contentDigest: digest(content),
        previousChunkDigest,
        state: "prepared" as const,
        transferredAt: null,
        verifiedAt: null,
        rejectionReason: null,
        revision: 1,
      };
      const chunk = { ...payload, digest: digest(payload) };
      assertDeliveryArtifactChunk(chunk, previousChunkDigest);
      this.chunks.set(chunk.chunkId, chunk);
      chunkIds.push(chunk.chunkId);
      previousChunkDigest = chunk.digest;
    }
    const entryPayload = {
      artifactId: input.artifactId.trim(),
      relativePath: input.relativePath.replaceAll("\\", "/"),
      mediaType: input.mediaType.trim(),
      byteLength: input.content.byteLength,
      contentDigest: digest(Buffer.from(input.content).toString("base64")),
      chunkIds,
      required: input.required ?? true,
      executable: input.executable ?? false,
    };
    const entry = { ...entryPayload, digest: digest(entryPayload) };
    assertDeliveryArtifactEntry(entry);
    return this.transitionBundle(bundle, {
      entries: [...bundle.entries, entry],
      totalBytes: bundle.totalBytes + entry.byteLength,
      rootDigest: previousChunkDigest,
    });
  }

  seal(bundleId: string, expectedRevision: number): DeliveryArtifactBundle {
    const bundle = this.requireBundle(bundleId);
    this.assertBundleRevision(bundle, expectedRevision);
    if (bundle.state !== "building")
      throw new E03RuntimeError(
        "delivery_artifact_seal_state",
        `delivery artifact bundle ${bundleId} is ${bundle.state}`,
      );
    return this.transitionBundle(bundle, {
      state: "sealed",
      sealedAt: this.clock.now(),
    });
  }

  transfer(chunkId: string, expectedRevision: number): DeliveryArtifactChunk {
    const chunk = this.requireChunk(chunkId);
    this.assertChunkRevision(chunk, expectedRevision);
    if (chunk.state !== "prepared")
      throw new E03RuntimeError(
        "delivery_artifact_transfer_state",
        `delivery artifact chunk ${chunkId} is ${chunk.state}`,
      );
    const bundle = this.requireBundle(chunk.bundleId);
    if (bundle.state !== "sealed" && bundle.state !== "transferring")
      throw new E03RuntimeError(
        "delivery_artifact_bundle_transfer_state",
        `delivery artifact bundle ${bundle.bundleId} is ${bundle.state}`,
      );
    if (bundle.state === "sealed")
      this.transitionBundle(bundle, { state: "transferring" });
    return this.transitionChunk(chunk, {
      state: "transferred",
      transferredAt: this.clock.now(),
    });
  }

  verifyChunk(
    chunkId: string,
    expectedRevision: number,
    content: string,
  ): DeliveryArtifactChunk {
    const chunk = this.requireChunk(chunkId);
    this.assertChunkRevision(chunk, expectedRevision);
    if (chunk.state !== "transferred")
      throw new E03RuntimeError(
        "delivery_artifact_verify_state",
        `delivery artifact chunk ${chunkId} is ${chunk.state}`,
      );
    if (digest(content) !== chunk.contentDigest)
      return this.transitionChunk(chunk, {
        state: "rejected",
        rejectionReason: "content digest mismatch",
      });
    return this.transitionChunk(chunk, {
      state: "verified",
      verifiedAt: this.clock.now(),
    });
  }

  verifyBundle(
    bundleId: string,
    expectedRevision: number,
  ): DeliveryArtifactBundle {
    const bundle = this.requireBundle(bundleId);
    this.assertBundleRevision(bundle, expectedRevision);
    if (bundle.state !== "transferring")
      throw new E03RuntimeError(
        "delivery_artifact_bundle_verify_state",
        `delivery artifact bundle ${bundleId} is ${bundle.state}`,
      );
    const values = bundle.entries.flatMap((entry) =>
      entry.chunkIds.map((chunkId) => this.requireChunk(chunkId)),
    );
    if (values.some((chunk) => chunk.state === "rejected"))
      return this.transitionBundle(bundle, { state: "rejected" });
    if (values.some((chunk) => chunk.state !== "verified"))
      throw new E03RuntimeError(
        "delivery_artifact_bundle_incomplete",
        `delivery artifact bundle ${bundleId} has unverified chunks`,
      );
    return this.transitionBundle(bundle, {
      state: "verified",
      verifiedAt: this.clock.now(),
    });
  }

  materialize(bundleId: string, artifactId: string): Uint8Array {
    const bundle = this.requireBundle(bundleId);
    if (bundle.state !== "verified")
      throw new E03RuntimeError(
        "delivery_artifact_materialize_state",
        `delivery artifact bundle ${bundleId} is ${bundle.state}`,
      );
    const entry = bundle.entries.find(
      (candidate) => candidate.artifactId === artifactId,
    );
    if (!entry)
      throw new E03RuntimeError(
        "delivery_artifact_missing",
        `delivery artifact ${artifactId} does not exist`,
      );
    const bytes = Buffer.concat(
      entry.chunkIds.map((chunkId) =>
        Buffer.from(this.requireChunk(chunkId).content, "base64"),
      ),
    );
    if (
      bytes.byteLength !== entry.byteLength ||
      digest(bytes.toString("base64")) !== entry.contentDigest
    )
      throw new E03RuntimeError(
        "delivery_artifact_materialize_digest",
        `delivery artifact ${artifactId} is corrupt`,
      );
    return new Uint8Array(bytes);
  }

  snapshot(): {
    bundles: DeliveryArtifactBundle[];
    chunks: DeliveryArtifactChunk[];
  } {
    return {
      bundles: [...this.bundles.values()].map((value) =>
        structuredClone(value),
      ),
      chunks: [...this.chunks.values()].map((value) => structuredClone(value)),
    };
  }

  restore(snapshot: {
    bundles: readonly DeliveryArtifactBundle[];
    chunks: readonly DeliveryArtifactChunk[];
  }): void {
    const bundles = new Map<string, DeliveryArtifactBundle>();
    const chunks = new Map<string, DeliveryArtifactChunk>();
    for (const bundle of snapshot.bundles) {
      assertDeliveryArtifactBundle(bundle);
      if (bundles.has(bundle.bundleId))
        throw new E03RuntimeError(
          "delivery_artifact_restore_duplicate_bundle",
          `duplicate delivery artifact bundle ${bundle.bundleId}`,
        );
      bundles.set(bundle.bundleId, structuredClone(bundle));
    }
    for (const chunk of snapshot.chunks) {
      if (chunks.has(chunk.chunkId))
        throw new E03RuntimeError(
          "delivery_artifact_restore_duplicate_chunk",
          `duplicate delivery artifact chunk ${chunk.chunkId}`,
        );
      const bundle = bundles.get(chunk.bundleId);
      if (!bundle)
        throw new E03RuntimeError(
          "delivery_artifact_restore_orphan_chunk",
          `delivery artifact chunk ${chunk.chunkId} has no bundle`,
        );
      if (
        !bundle.entries.some((entry) => entry.chunkIds.includes(chunk.chunkId))
      )
        throw new E03RuntimeError(
          "delivery_artifact_restore_unreferenced_chunk",
          `delivery artifact chunk ${chunk.chunkId} is not referenced`,
        );
      chunks.set(chunk.chunkId, structuredClone(chunk));
    }
    for (const bundle of bundles.values()) {
      const seen = new Set<string>();
      for (const entry of bundle.entries) {
        for (const chunkId of entry.chunkIds) {
          if (seen.has(chunkId))
            throw new E03RuntimeError(
              "delivery_artifact_restore_duplicate_reference",
              `delivery artifact chunk ${chunkId} is referenced twice`,
            );
          seen.add(chunkId);
          const chunk = chunks.get(chunkId);
          if (!chunk || chunk.artifactId !== entry.artifactId)
            throw new E03RuntimeError(
              "delivery_artifact_restore_missing_chunk",
              `delivery artifact entry ${entry.artifactId} has an invalid chunk`,
            );
        }
      }
    }
    this.bundles = bundles;
    this.chunks = chunks;
  }

  private requireBundle(bundleId: string): DeliveryArtifactBundle {
    const bundle = this.bundles.get(bundleId);
    if (!bundle)
      throw new E03RuntimeError(
        "delivery_artifact_bundle_missing",
        `delivery artifact bundle ${bundleId} does not exist`,
      );
    assertDeliveryArtifactBundle(bundle);
    return bundle;
  }

  private requireChunk(chunkId: string): DeliveryArtifactChunk {
    const chunk = this.chunks.get(chunkId);
    if (!chunk)
      throw new E03RuntimeError(
        "delivery_artifact_chunk_missing",
        `delivery artifact chunk ${chunkId} does not exist`,
      );
    return chunk;
  }

  private assertBundleRevision(
    bundle: DeliveryArtifactBundle,
    expected: number,
  ): void {
    if (bundle.revision !== expected)
      throw new E03RuntimeError(
        "delivery_artifact_bundle_stale_revision",
        `delivery artifact bundle ${bundle.bundleId} revision is stale`,
      );
  }

  private assertChunkRevision(
    chunk: DeliveryArtifactChunk,
    expected: number,
  ): void {
    if (chunk.revision !== expected)
      throw new E03RuntimeError(
        "delivery_artifact_chunk_stale_revision",
        `delivery artifact chunk ${chunk.chunkId} revision is stale`,
      );
  }

  private transitionBundle(
    bundle: DeliveryArtifactBundle,
    patch: Partial<
      Omit<DeliveryArtifactBundle, "bundleId" | "revision" | "digest">
    >,
  ): DeliveryArtifactBundle {
    const { digest: _, ...prior } = bundle;
    const payload = {
      ...prior,
      ...patch,
      bundleId: bundle.bundleId,
      revision: bundle.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertDeliveryArtifactBundle(next);
    this.bundles.set(next.bundleId, next);
    return structuredClone(next);
  }

  private transitionChunk(
    chunk: DeliveryArtifactChunk,
    patch: Partial<
      Omit<DeliveryArtifactChunk, "chunkId" | "revision" | "digest">
    >,
  ): DeliveryArtifactChunk {
    const { digest: _, ...prior } = chunk;
    const payload = {
      ...prior,
      ...patch,
      chunkId: chunk.chunkId,
      revision: chunk.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    this.chunks.set(next.chunkId, next);
    return structuredClone(next);
  }
}

export interface DeliveryCustodyTransfer {
  transferId: string;
  deliveryId: string;
  taskId: string;
  fromTaskId: string;
  toTaskId: string;
  state:
    | "proposed"
    | "accepted"
    | "committed"
    | "rejected"
    | "expired"
    | "cancelled";
  deliveryDigest: string;
  reason: string;
  acceptanceDigest: string | null;
  committedReceiptDigest: string | null;
  proposedAt: string;
  acceptedAt: string | null;
  committedAt: string | null;
  expiresAt: string;
  rejectionReason: string | null;
  revision: number;
  digest: string;
}
export interface DeliveryCustodyReceipt {
  receiptId: string;
  transferId: string;
  deliveryId: string;
  fromTaskId: string;
  toTaskId: string;
  sequence: number;
  outcome: "accepted" | "committed" | "rejected" | "cancelled";
  payloadDigest: string;
  occurredAt: string;
  previousDigest: string;
  digest: string;
}
function assertDeliveryCustodyTransfer(value: DeliveryCustodyTransfer): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "delivery_custody_transfer_digest",
      `delivery custody transfer ${value.transferId} is corrupt`,
    );
  if (
    !value.transferId ||
    !value.deliveryId ||
    !value.taskId ||
    !value.fromTaskId ||
    !value.toTaskId ||
    value.fromTaskId === value.toTaskId ||
    !value.deliveryDigest ||
    !value.reason ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.expiresAt))
  )
    throw new E03RuntimeError(
      "delivery_custody_transfer",
      `delivery custody transfer ${value.transferId} is invalid`,
    );
  if (
    value.state === "accepted" &&
    (!value.acceptedAt || !value.acceptanceDigest)
  )
    throw new E03RuntimeError(
      "delivery_custody_acceptance",
      `accepted delivery custody transfer ${value.transferId} lacks receipt`,
    );
  if (
    value.state === "committed" &&
    (!value.committedAt || !value.committedReceiptDigest)
  )
    throw new E03RuntimeError(
      "delivery_custody_commit",
      `committed delivery custody transfer ${value.transferId} lacks receipt`,
    );
}
function assertDeliveryCustodyReceipt(value: DeliveryCustodyReceipt): void {
  const { digest: checksum, ...payload } = value;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "delivery_custody_receipt_digest",
      `delivery custody receipt ${value.receiptId} is corrupt`,
    );
  if (
    !value.receiptId ||
    !value.transferId ||
    !value.deliveryId ||
    !value.fromTaskId ||
    !value.toTaskId ||
    !value.payloadDigest ||
    !Number.isSafeInteger(value.sequence) ||
    value.sequence < 1
  )
    throw new E03RuntimeError(
      "delivery_custody_receipt",
      `delivery custody receipt ${value.receiptId} is invalid`,
    );
}
export interface DeliveryAcknowledgementWindow {
  windowId: string;
  taskId: string;
  deliveryId: string;
  senderId: string;
  recipientId: string;
  sequence: number;
  idempotencyKey: string;
  state:
    | "open"
    | "received"
    | "accepted"
    | "rejected"
    | "expired"
    | "cancelled";
  deadlineAt: string;
  openedAt: string;
  updatedAt: string;
  terminalAt: string;
  payloadDigest: string;
  acknowledgementDigest: string;
  errorCode: string;
  revision: number;
  digest: string;
}

export interface DeliveryAcknowledgementReceipt {
  receiptId: string;
  windowId: string;
  deliveryId: string;
  recipientId: string;
  sequence: number;
  outcome: "received" | "accepted" | "rejected";
  payloadDigest: string;
  recipientRevision: number;
  receivedAt: string;
  previousDigest: string;
  digest: string;
}

export interface DeliveryRetransmission {
  retransmissionId: string;
  windowId: string;
  originalDeliveryId: string;
  attempt: number;
  idempotencyKey: string;
  state: "scheduled" | "sent" | "acknowledged" | "failed" | "cancelled";
  scheduledAt: string;
  sentAt: string;
  settledAt: string;
  transportReceiptDigest: string;
  errorCode: string;
  revision: number;
  digest: string;
}

export interface DeliveryAcknowledgementSnapshot {
  windows: DeliveryAcknowledgementWindow[];
  receipts: DeliveryAcknowledgementReceipt[];
  retransmissions: DeliveryRetransmission[];
  windowByIdempotencyKey: [string, string][];
  activeWindowByDelivery: [string, string][];
  retransmissionByIdempotencyKey: [string, string][];
}

function assertDeliveryAcknowledgementWindow(
  value: DeliveryAcknowledgementWindow,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.windowId ||
    !value.taskId ||
    !value.deliveryId ||
    !value.senderId ||
    !value.recipientId ||
    value.sequence < 0 ||
    !value.idempotencyKey ||
    !value.payloadDigest ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "delivery_acknowledgement_window_corrupt",
      `delivery acknowledgement window ${value.windowId || "<empty>"} is corrupt`,
    );
}

function assertDeliveryAcknowledgementReceipt(
  value: DeliveryAcknowledgementReceipt,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.receiptId ||
    !value.windowId ||
    !value.deliveryId ||
    !value.recipientId ||
    value.sequence < 0 ||
    !value.payloadDigest ||
    value.recipientRevision < 0 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "delivery_acknowledgement_receipt_corrupt",
      `delivery acknowledgement receipt ${value.receiptId || "<empty>"} is corrupt`,
    );
}

function assertDeliveryRetransmission(value: DeliveryRetransmission): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.retransmissionId ||
    !value.windowId ||
    !value.originalDeliveryId ||
    value.attempt < 1 ||
    !value.idempotencyKey ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "delivery_retransmission_corrupt",
      `delivery retransmission ${value.retransmissionId || "<empty>"} is corrupt`,
    );
}

export class DeliveryAcknowledgementRuntime {
  private windows = new Map<string, DeliveryAcknowledgementWindow>();
  private receipts = new Map<string, DeliveryAcknowledgementReceipt[]>();
  private retransmissions = new Map<string, DeliveryRetransmission[]>();
  private windowByIdempotencyKey = new Map<string, string>();
  private activeWindowByDelivery = new Map<string, string>();
  private retransmissionByIdempotencyKey = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  open(input: {
    windowId?: string;
    taskId: string;
    deliveryId: string;
    senderId: string;
    recipientId: string;
    sequence: number;
    idempotencyKey: string;
    deadlineAt: string;
    payloadDigest: string;
  }): DeliveryAcknowledgementWindow {
    const duplicateId = this.windowByIdempotencyKey.get(input.idempotencyKey);
    if (duplicateId) return structuredClone(this.requireWindow(duplicateId));
    if (this.activeWindowByDelivery.has(input.deliveryId))
      throw new E03RuntimeError(
        "delivery_acknowledgement_window_active",
        `delivery ${input.deliveryId} already awaits acknowledgement`,
      );
    if (input.senderId === input.recipientId)
      throw new E03RuntimeError(
        "delivery_acknowledgement_same_party",
        "delivery acknowledgement sender and recipient must differ",
      );
    const now = this.clock.now();
    if (Date.parse(input.deadlineAt) <= Date.parse(now))
      throw new E03RuntimeError(
        "delivery_acknowledgement_deadline",
        "delivery acknowledgement deadline must be in the future",
      );
    const windowId = input.windowId ?? createId("delivery-ack-window");
    const payload = {
      windowId,
      taskId: input.taskId,
      deliveryId: input.deliveryId,
      senderId: input.senderId,
      recipientId: input.recipientId,
      sequence: input.sequence,
      idempotencyKey: input.idempotencyKey,
      state: "open" as const,
      deadlineAt: input.deadlineAt,
      openedAt: now,
      updatedAt: now,
      terminalAt: "",
      payloadDigest: input.payloadDigest,
      acknowledgementDigest: "",
      errorCode: "",
      revision: 1,
    };
    const window = { ...payload, digest: digest(payload) };
    assertDeliveryAcknowledgementWindow(window);
    this.windows.set(windowId, window);
    this.receipts.set(windowId, []);
    this.retransmissions.set(windowId, []);
    this.windowByIdempotencyKey.set(input.idempotencyKey, windowId);
    this.activeWindowByDelivery.set(input.deliveryId, windowId);
    return structuredClone(window);
  }

  receive(input: {
    receiptId?: string;
    windowId: string;
    expectedRevision: number;
    recipientId: string;
    sequence: number;
    payloadDigest: string;
    recipientRevision: number;
  }): DeliveryAcknowledgementReceipt {
    const window = this.requireWindow(input.windowId);
    this.assertWindowRevision(window, input.expectedRevision);
    if (!["open", "received"].includes(window.state))
      throw new E03RuntimeError(
        "delivery_acknowledgement_receive_state",
        `delivery acknowledgement window ${window.windowId} is ${window.state}`,
      );
    if (
      input.recipientId !== window.recipientId ||
      input.sequence !== window.sequence ||
      input.payloadDigest !== window.payloadDigest
    )
      throw new E03RuntimeError(
        "delivery_acknowledgement_binding",
        `delivery acknowledgement window ${window.windowId} binding is invalid`,
      );
    const entries = this.receiptEntries(window.windowId);
    const duplicate = entries.find(
      (value) =>
        value.outcome === "received" &&
        value.recipientRevision === input.recipientRevision,
    );
    if (duplicate) return structuredClone(duplicate);
    const payload = {
      receiptId: input.receiptId ?? createId("delivery-ack-receipt"),
      windowId: window.windowId,
      deliveryId: window.deliveryId,
      recipientId: input.recipientId,
      sequence: input.sequence,
      outcome: "received" as const,
      payloadDigest: input.payloadDigest,
      recipientRevision: input.recipientRevision,
      receivedAt: this.clock.now(),
      previousDigest: entries.at(-1)?.digest ?? "",
    };
    const receipt = { ...payload, digest: digest(payload) };
    assertDeliveryAcknowledgementReceipt(receipt);
    entries.push(receipt);
    this.receipts.set(window.windowId, entries);
    this.transitionWindow(window, {
      state: "received",
      acknowledgementDigest: receipt.digest,
    });
    return structuredClone(receipt);
  }

  decide(input: {
    receiptId?: string;
    windowId: string;
    expectedRevision: number;
    accepted: boolean;
    recipientRevision: number;
    errorCode?: string;
  }): DeliveryAcknowledgementReceipt {
    const window = this.requireWindow(input.windowId);
    this.assertWindowRevision(window, input.expectedRevision);
    if (window.state !== "received")
      throw new E03RuntimeError(
        "delivery_acknowledgement_decide_state",
        `delivery acknowledgement window ${window.windowId} is ${window.state}`,
      );
    const entries = this.receiptEntries(window.windowId);
    const received = entries.at(-1);
    if (!received || received.outcome !== "received")
      throw new E03RuntimeError(
        "delivery_acknowledgement_receive_receipt_missing",
        `delivery acknowledgement window ${window.windowId} lacks receive receipt`,
      );
    const payload = {
      receiptId: input.receiptId ?? createId("delivery-ack-decision"),
      windowId: window.windowId,
      deliveryId: window.deliveryId,
      recipientId: window.recipientId,
      sequence: window.sequence,
      outcome: input.accepted ? ("accepted" as const) : ("rejected" as const),
      payloadDigest: window.payloadDigest,
      recipientRevision: input.recipientRevision,
      receivedAt: this.clock.now(),
      previousDigest: received.digest,
    };
    const receipt = { ...payload, digest: digest(payload) };
    assertDeliveryAcknowledgementReceipt(receipt);
    entries.push(receipt);
    this.receipts.set(window.windowId, entries);
    this.transitionWindow(window, {
      state: input.accepted ? "accepted" : "rejected",
      acknowledgementDigest: receipt.digest,
      terminalAt: receipt.receivedAt,
      errorCode: input.errorCode ?? "",
    });
    this.activeWindowByDelivery.delete(window.deliveryId);
    for (const retransmission of this.retransmissionEntries(window.windowId))
      if (["scheduled", "sent"].includes(retransmission.state))
        this.transitionRetransmission(retransmission, {
          state: input.accepted ? "acknowledged" : "cancelled",
          settledAt: receipt.receivedAt,
        });
    return structuredClone(receipt);
  }

  scheduleRetransmission(input: {
    retransmissionId?: string;
    windowId: string;
    expectedRevision: number;
    idempotencyKey: string;
  }): DeliveryRetransmission {
    const duplicateId = this.retransmissionByIdempotencyKey.get(
      input.idempotencyKey,
    );
    if (duplicateId)
      return structuredClone(this.requireRetransmission(duplicateId));
    const window = this.requireWindow(input.windowId);
    this.assertWindowRevision(window, input.expectedRevision);
    if (!["open", "received"].includes(window.state))
      throw new E03RuntimeError(
        "delivery_retransmission_window_state",
        `delivery acknowledgement window ${window.windowId} is ${window.state}`,
      );
    const entries = this.retransmissionEntries(window.windowId);
    if (entries.some((value) => ["scheduled", "sent"].includes(value.state)))
      throw new E03RuntimeError(
        "delivery_retransmission_active",
        `delivery ${window.deliveryId} already retransmits`,
      );
    const retransmissionId =
      input.retransmissionId ?? createId("delivery-retransmission");
    const payload = {
      retransmissionId,
      windowId: window.windowId,
      originalDeliveryId: window.deliveryId,
      attempt: entries.length + 1,
      idempotencyKey: input.idempotencyKey,
      state: "scheduled" as const,
      scheduledAt: this.clock.now(),
      sentAt: "",
      settledAt: "",
      transportReceiptDigest: "",
      errorCode: "",
      revision: 1,
    };
    const retransmission = { ...payload, digest: digest(payload) };
    assertDeliveryRetransmission(retransmission);
    entries.push(retransmission);
    this.retransmissions.set(window.windowId, entries);
    this.retransmissionByIdempotencyKey.set(
      input.idempotencyKey,
      retransmissionId,
    );
    return structuredClone(retransmission);
  }

  markSent(
    retransmissionId: string,
    expectedRevision: number,
    transportReceiptDigest: string,
  ): DeliveryRetransmission {
    const value = this.requireRetransmission(retransmissionId);
    this.assertRetransmissionRevision(value, expectedRevision);
    if (value.state !== "scheduled" || !transportReceiptDigest)
      throw new E03RuntimeError(
        "delivery_retransmission_send_state",
        `delivery retransmission ${retransmissionId} cannot be sent`,
      );
    return this.transitionRetransmission(value, {
      state: "sent",
      sentAt: this.clock.now(),
      transportReceiptDigest,
    });
  }

  failRetransmission(
    retransmissionId: string,
    expectedRevision: number,
    errorCode: string,
  ): DeliveryRetransmission {
    const value = this.requireRetransmission(retransmissionId);
    this.assertRetransmissionRevision(value, expectedRevision);
    if (!["scheduled", "sent"].includes(value.state))
      throw new E03RuntimeError(
        "delivery_retransmission_fail_state",
        `delivery retransmission ${retransmissionId} is ${value.state}`,
      );
    return this.transitionRetransmission(value, {
      state: "failed",
      settledAt: this.clock.now(),
      errorCode,
    });
  }

  expire(at = this.clock.now()): DeliveryAcknowledgementWindow[] {
    const expired: DeliveryAcknowledgementWindow[] = [];
    for (const window of [...this.windows.values()]) {
      if (
        !["open", "received"].includes(window.state) ||
        Date.parse(window.deadlineAt) > Date.parse(at)
      )
        continue;
      const next = this.transitionWindow(window, {
        state: "expired",
        terminalAt: at,
        errorCode: "acknowledgement_deadline",
      });
      this.activeWindowByDelivery.delete(window.deliveryId);
      expired.push(next);
    }
    return expired;
  }

  snapshot(): DeliveryAcknowledgementSnapshot {
    return {
      windows: [...this.windows.values()].map((value) =>
        structuredClone(value),
      ),
      receipts: [...this.receipts.values()]
        .flat()
        .map((value) => structuredClone(value)),
      retransmissions: [...this.retransmissions.values()]
        .flat()
        .map((value) => structuredClone(value)),
      windowByIdempotencyKey: [...this.windowByIdempotencyKey.entries()],
      activeWindowByDelivery: [...this.activeWindowByDelivery.entries()],
      retransmissionByIdempotencyKey: [
        ...this.retransmissionByIdempotencyKey.entries(),
      ],
    };
  }

  restore(snapshot: DeliveryAcknowledgementSnapshot): void {
    const windows = new Map<string, DeliveryAcknowledgementWindow>();
    const receipts = new Map<string, DeliveryAcknowledgementReceipt[]>();
    const retransmissions = new Map<string, DeliveryRetransmission[]>();
    for (const value of snapshot.windows) {
      assertDeliveryAcknowledgementWindow(value);
      if (windows.has(value.windowId))
        throw new E03RuntimeError(
          "delivery_ack_restore_duplicate",
          `window ${value.windowId} duplicate`,
        );
      windows.set(value.windowId, structuredClone(value));
      receipts.set(value.windowId, []);
      retransmissions.set(value.windowId, []);
    }
    for (const value of snapshot.receipts) {
      assertDeliveryAcknowledgementReceipt(value);
      const entries = receipts.get(value.windowId);
      const window = windows.get(value.windowId);
      if (
        !entries ||
        !window ||
        window.deliveryId !== value.deliveryId ||
        value.previousDigest !== (entries.at(-1)?.digest ?? "")
      )
        throw new E03RuntimeError(
          "delivery_ack_receipt_restore_chain",
          `receipt ${value.receiptId} breaks chain`,
        );
      entries.push(structuredClone(value));
    }
    for (const value of snapshot.retransmissions) {
      assertDeliveryRetransmission(value);
      const entries = retransmissions.get(value.windowId);
      const window = windows.get(value.windowId);
      if (
        !entries ||
        !window ||
        window.deliveryId !== value.originalDeliveryId ||
        value.attempt !== entries.length + 1
      )
        throw new E03RuntimeError(
          "delivery_retransmission_restore_order",
          `retransmission ${value.retransmissionId} invalid`,
        );
      entries.push(structuredClone(value));
    }
    const windowByIdempotencyKey = new Map(snapshot.windowByIdempotencyKey);
    const activeWindowByDelivery = new Map(snapshot.activeWindowByDelivery);
    const retransmissionByIdempotencyKey = new Map(
      snapshot.retransmissionByIdempotencyKey,
    );
    if (
      windowByIdempotencyKey.size !== snapshot.windowByIdempotencyKey.length ||
      activeWindowByDelivery.size !== snapshot.activeWindowByDelivery.length ||
      retransmissionByIdempotencyKey.size !==
        snapshot.retransmissionByIdempotencyKey.length
    )
      throw new E03RuntimeError(
        "delivery_ack_restore_index_duplicate",
        "delivery acknowledgement indexes duplicate",
      );
    for (const [key, windowId] of windowByIdempotencyKey) {
      const value = windows.get(windowId);
      if (!value || value.idempotencyKey !== key)
        throw new E03RuntimeError(
          "delivery_ack_restore_idempotency",
          `window index ${key} invalid`,
        );
    }
    for (const [deliveryId, windowId] of activeWindowByDelivery) {
      const value = windows.get(windowId);
      if (
        !value ||
        value.deliveryId !== deliveryId ||
        !["open", "received"].includes(value.state)
      )
        throw new E03RuntimeError(
          "delivery_ack_restore_active",
          `delivery index ${deliveryId} invalid`,
        );
    }
    for (const [key, retransmissionId] of retransmissionByIdempotencyKey) {
      const value = [...retransmissions.values()]
        .flat()
        .find((entry) => entry.retransmissionId === retransmissionId);
      if (!value || value.idempotencyKey !== key)
        throw new E03RuntimeError(
          "delivery_retransmission_restore_idempotency",
          `retransmission index ${key} invalid`,
        );
    }
    this.windows = windows;
    this.receipts = receipts;
    this.retransmissions = retransmissions;
    this.windowByIdempotencyKey = windowByIdempotencyKey;
    this.activeWindowByDelivery = activeWindowByDelivery;
    this.retransmissionByIdempotencyKey = retransmissionByIdempotencyKey;
  }

  private receiptEntries(windowId: string): DeliveryAcknowledgementReceipt[] {
    return this.receipts.get(windowId) ?? [];
  }

  private retransmissionEntries(windowId: string): DeliveryRetransmission[] {
    return this.retransmissions.get(windowId) ?? [];
  }

  private requireWindow(id: string): DeliveryAcknowledgementWindow {
    const value = this.windows.get(id);
    if (!value)
      throw new E03RuntimeError(
        "delivery_ack_window_missing",
        `window ${id} missing`,
      );
    assertDeliveryAcknowledgementWindow(value);
    return value;
  }

  private requireRetransmission(id: string): DeliveryRetransmission {
    const value = [...this.retransmissions.values()]
      .flat()
      .find((entry) => entry.retransmissionId === id);
    if (!value)
      throw new E03RuntimeError(
        "delivery_retransmission_missing",
        `retransmission ${id} missing`,
      );
    assertDeliveryRetransmission(value);
    return value;
  }

  private assertWindowRevision(
    value: DeliveryAcknowledgementWindow,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "delivery_ack_window_stale_revision",
        `window ${value.windowId} stale`,
      );
  }

  private assertRetransmissionRevision(
    value: DeliveryRetransmission,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "delivery_retransmission_stale_revision",
        `retransmission ${value.retransmissionId} stale`,
      );
  }

  private transitionWindow(
    value: DeliveryAcknowledgementWindow,
    patch: Partial<
      Omit<DeliveryAcknowledgementWindow, "windowId" | "revision" | "digest">
    >,
  ): DeliveryAcknowledgementWindow {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      windowId: value.windowId,
      updatedAt: this.clock.now(),
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertDeliveryAcknowledgementWindow(next);
    this.windows.set(next.windowId, next);
    return structuredClone(next);
  }

  private transitionRetransmission(
    value: DeliveryRetransmission,
    patch: Partial<
      Omit<DeliveryRetransmission, "retransmissionId" | "revision" | "digest">
    >,
  ): DeliveryRetransmission {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      retransmissionId: value.retransmissionId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertDeliveryRetransmission(next);
    const entries = this.retransmissionEntries(value.windowId);
    const index = entries.findIndex(
      (entry) => entry.retransmissionId === value.retransmissionId,
    );
    entries[index] = next;
    this.retransmissions.set(value.windowId, entries);
    return structuredClone(next);
  }
}

export class DeliveryCustodyTransferRuntime {
  private transfers = new Map<string, DeliveryCustodyTransfer>();
  private receipts = new Map<string, DeliveryCustodyReceipt[]>();
  private activeByDelivery = new Map<string, string>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  propose(input: {
    delivery: E03Delivery;
    fromTaskId: string;
    toTaskId: string;
    reason: string;
    ttlMs: number;
  }): DeliveryCustodyTransfer {
    assertDelivery(input.delivery);
    if (
      input.delivery.taskId !== input.fromTaskId ||
      !input.toTaskId.trim() ||
      input.fromTaskId === input.toTaskId ||
      !input.reason.trim() ||
      !Number.isSafeInteger(input.ttlMs) ||
      input.ttlMs < 1
    )
      throw new E03RuntimeError(
        "delivery_custody_proposal",
        "delivery custody proposal is invalid",
      );
    const activeId = this.activeByDelivery.get(input.delivery.deliveryId);
    if (activeId) return structuredClone(this.requireTransfer(activeId));
    const payload = {
      transferId: createId("delivery-custody-transfer"),
      deliveryId: input.delivery.deliveryId,
      taskId: input.delivery.taskId,
      fromTaskId: input.fromTaskId,
      toTaskId: input.toTaskId.trim(),
      state: "proposed" as const,
      deliveryDigest: input.delivery.digest,
      reason: input.reason.trim(),
      acceptanceDigest: null,
      committedReceiptDigest: null,
      proposedAt: this.clock.now(),
      acceptedAt: null,
      committedAt: null,
      expiresAt: new Date(
        Date.parse(this.clock.now()) + input.ttlMs,
      ).toISOString(),
      rejectionReason: null,
      revision: 1,
    };
    const transfer = { ...payload, digest: digest(payload) };
    assertDeliveryCustodyTransfer(transfer);
    this.transfers.set(transfer.transferId, transfer);
    this.activeByDelivery.set(transfer.deliveryId, transfer.transferId);
    return structuredClone(transfer);
  }
  accept(input: {
    transferId: string;
    expectedRevision: number;
    acceptingTaskId: string;
    acceptancePayloadDigest: string;
  }): { transfer: DeliveryCustodyTransfer; receipt: DeliveryCustodyReceipt } {
    const transfer = this.requireTransfer(input.transferId);
    this.assertTransferRevision(transfer, input.expectedRevision);
    if (transfer.state !== "proposed")
      throw new E03RuntimeError(
        "delivery_custody_accept_state",
        `delivery custody transfer ${transfer.transferId} is ${transfer.state}`,
      );
    if (
      transfer.toTaskId !== input.acceptingTaskId ||
      !input.acceptancePayloadDigest
    )
      throw new E03RuntimeError(
        "delivery_custody_acceptor",
        `task ${input.acceptingTaskId} cannot accept transfer`,
      );
    if (Date.parse(transfer.expiresAt) <= Date.parse(this.clock.now()))
      return {
        transfer: this.expire(transfer.transferId, transfer.revision),
        receipt: this.appendReceipt(transfer, "rejected", digest("expired")),
      };
    const receipt = this.appendReceipt(
      transfer,
      "accepted",
      input.acceptancePayloadDigest,
    );
    const next = this.transitionTransfer(transfer, {
      state: "accepted",
      acceptanceDigest: receipt.digest,
      acceptedAt: this.clock.now(),
    });
    return { transfer: next, receipt };
  }
  commit(input: {
    transferId: string;
    expectedRevision: number;
    delivery: E03Delivery;
    ownershipPayloadDigest: string;
  }): { transfer: DeliveryCustodyTransfer; receipt: DeliveryCustodyReceipt } {
    const transfer = this.requireTransfer(input.transferId);
    this.assertTransferRevision(transfer, input.expectedRevision);
    if (transfer.state !== "accepted")
      throw new E03RuntimeError(
        "delivery_custody_commit_state",
        `delivery custody transfer ${transfer.transferId} is ${transfer.state}`,
      );
    assertDelivery(input.delivery);
    if (
      input.delivery.deliveryId !== transfer.deliveryId ||
      input.delivery.digest !== transfer.deliveryDigest ||
      !input.ownershipPayloadDigest
    )
      throw new E03RuntimeError(
        "delivery_custody_commit_delivery",
        "delivery custody commit payload is invalid",
      );
    const receipt = this.appendReceipt(
      transfer,
      "committed",
      input.ownershipPayloadDigest,
    );
    const next = this.transitionTransfer(transfer, {
      state: "committed",
      committedReceiptDigest: receipt.digest,
      committedAt: this.clock.now(),
    });
    this.activeByDelivery.delete(next.deliveryId);
    return { transfer: next, receipt };
  }
  reject(
    transferId: string,
    expectedRevision: number,
    rejectingTaskId: string,
    reason: string,
  ): { transfer: DeliveryCustodyTransfer; receipt: DeliveryCustodyReceipt } {
    const transfer = this.requireTransfer(transferId);
    this.assertTransferRevision(transfer, expectedRevision);
    if (transfer.state !== "proposed")
      throw new E03RuntimeError(
        "delivery_custody_reject_state",
        `delivery custody transfer ${transferId} is ${transfer.state}`,
      );
    if (transfer.toTaskId !== rejectingTaskId || !reason.trim())
      throw new E03RuntimeError(
        "delivery_custody_rejector",
        `task ${rejectingTaskId} cannot reject transfer`,
      );
    const receipt = this.appendReceipt(
      transfer,
      "rejected",
      digest(reason.trim()),
    );
    const next = this.transitionTransfer(transfer, {
      state: "rejected",
      rejectionReason: reason.trim(),
    });
    this.activeByDelivery.delete(next.deliveryId);
    return { transfer: next, receipt };
  }
  cancel(
    transferId: string,
    expectedRevision: number,
    cancellingTaskId: string,
    reason: string,
  ): { transfer: DeliveryCustodyTransfer; receipt: DeliveryCustodyReceipt } {
    const transfer = this.requireTransfer(transferId);
    this.assertTransferRevision(transfer, expectedRevision);
    if (
      transfer.fromTaskId !== cancellingTaskId ||
      (transfer.state !== "proposed" && transfer.state !== "accepted") ||
      !reason.trim()
    )
      throw new E03RuntimeError(
        "delivery_custody_cancel",
        `delivery custody transfer ${transferId} cannot be cancelled`,
      );
    const receipt = this.appendReceipt(
      transfer,
      "cancelled",
      digest(reason.trim()),
    );
    const next = this.transitionTransfer(transfer, {
      state: "cancelled",
      rejectionReason: reason.trim(),
    });
    this.activeByDelivery.delete(next.deliveryId);
    return { transfer: next, receipt };
  }
  expire(
    transferId: string,
    expectedRevision: number,
  ): DeliveryCustodyTransfer {
    const transfer = this.requireTransfer(transferId);
    this.assertTransferRevision(transfer, expectedRevision);
    if (transfer.state !== "proposed" && transfer.state !== "accepted")
      return structuredClone(transfer);
    const next = this.transitionTransfer(transfer, {
      state: "expired",
      rejectionReason: "delivery custody transfer expired",
    });
    this.activeByDelivery.delete(next.deliveryId);
    return next;
  }
  history(transferId: string): DeliveryCustodyReceipt[] {
    const entries = this.receipts.get(transferId) ?? [];
    let previousDigest = "root";
    let sequence = 1;
    for (const receipt of entries) {
      assertDeliveryCustodyReceipt(receipt);
      if (
        receipt.sequence !== sequence ||
        receipt.previousDigest !== previousDigest
      )
        throw new E03RuntimeError(
          "delivery_custody_receipt_chain",
          `delivery custody receipt ${receipt.receiptId} breaks chain`,
        );
      previousDigest = receipt.digest;
      sequence += 1;
    }
    return entries.map((value) => structuredClone(value));
  }
  snapshot(): {
    transfers: DeliveryCustodyTransfer[];
    receipts: DeliveryCustodyReceipt[];
    activeByDelivery: Array<[string, string]>;
  } {
    for (const id of this.transfers.keys()) this.history(id);
    return {
      transfers: [...this.transfers.values()].map((value) =>
        structuredClone(value),
      ),
      receipts: [...this.receipts.values()]
        .flat()
        .map((value) => structuredClone(value)),
      activeByDelivery: [...this.activeByDelivery.entries()].map(
        ([deliveryId, transferId]) => [deliveryId, transferId],
      ),
    };
  }
  restore(snapshot: {
    transfers: readonly DeliveryCustodyTransfer[];
    receipts: readonly DeliveryCustodyReceipt[];
    activeByDelivery: ReadonlyArray<readonly [string, string]>;
  }): void {
    const transfers = new Map<string, DeliveryCustodyTransfer>();
    const receipts = new Map<string, DeliveryCustodyReceipt[]>();
    const activeByDelivery = new Map<string, string>();
    for (const value of snapshot.transfers) {
      assertDeliveryCustodyTransfer(value);
      if (transfers.has(value.transferId))
        throw new E03RuntimeError(
          "delivery_custody_transfer_restore_duplicate",
          `duplicate delivery custody transfer ${value.transferId}`,
        );
      transfers.set(value.transferId, structuredClone(value));
    }
    for (const value of snapshot.receipts) {
      assertDeliveryCustodyReceipt(value);
      const transfer = transfers.get(value.transferId);
      if (
        !transfer ||
        transfer.deliveryId !== value.deliveryId ||
        transfer.fromTaskId !== value.fromTaskId ||
        transfer.toTaskId !== value.toTaskId
      )
        throw new E03RuntimeError(
          "delivery_custody_receipt_restore",
          `delivery custody receipt ${value.receiptId} is invalid`,
        );
      const entries = receipts.get(value.transferId) ?? [];
      if (entries.some((entry) => entry.receiptId === value.receiptId))
        throw new E03RuntimeError(
          "delivery_custody_receipt_restore_duplicate",
          `duplicate delivery custody receipt ${value.receiptId}`,
        );
      entries.push(structuredClone(value));
      receipts.set(value.transferId, entries);
    }
    for (const entries of receipts.values())
      entries.sort((left, right) => left.sequence - right.sequence);
    for (const [deliveryId, transferId] of snapshot.activeByDelivery) {
      const transfer = transfers.get(transferId);
      if (
        !transfer ||
        transfer.deliveryId !== deliveryId ||
        (transfer.state !== "proposed" && transfer.state !== "accepted") ||
        activeByDelivery.has(deliveryId)
      )
        throw new E03RuntimeError(
          "delivery_custody_active_restore",
          `delivery custody active index ${deliveryId} is invalid`,
        );
      activeByDelivery.set(deliveryId, transferId);
    }
    this.transfers = transfers;
    this.receipts = receipts;
    this.activeByDelivery = activeByDelivery;
    for (const id of transfers.keys()) this.history(id);
  }
  private appendReceipt(
    transfer: DeliveryCustodyTransfer,
    outcome: DeliveryCustodyReceipt["outcome"],
    payloadDigest: string,
  ): DeliveryCustodyReceipt {
    const entries = this.receipts.get(transfer.transferId) ?? [];
    const payload = {
      receiptId: createId("delivery-custody-receipt"),
      transferId: transfer.transferId,
      deliveryId: transfer.deliveryId,
      fromTaskId: transfer.fromTaskId,
      toTaskId: transfer.toTaskId,
      sequence: entries.length + 1,
      outcome,
      payloadDigest,
      occurredAt: this.clock.now(),
      previousDigest: entries[entries.length - 1]?.digest ?? "root",
    };
    const receipt = { ...payload, digest: digest(payload) };
    assertDeliveryCustodyReceipt(receipt);
    entries.push(receipt);
    this.receipts.set(transfer.transferId, entries);
    return structuredClone(receipt);
  }
  private requireTransfer(id: string): DeliveryCustodyTransfer {
    const value = this.transfers.get(id);
    if (!value)
      throw new E03RuntimeError(
        "delivery_custody_transfer_missing",
        `delivery custody transfer ${id} does not exist`,
      );
    assertDeliveryCustodyTransfer(value);
    return value;
  }
  private assertTransferRevision(
    value: DeliveryCustodyTransfer,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "delivery_custody_transfer_stale_revision",
        `delivery custody transfer ${value.transferId} revision is stale`,
      );
  }
  private transitionTransfer(
    value: DeliveryCustodyTransfer,
    patch: Partial<
      Omit<DeliveryCustodyTransfer, "transferId" | "revision" | "digest">
    >,
  ): DeliveryCustodyTransfer {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      transferId: value.transferId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertDeliveryCustodyTransfer(next);
    this.transfers.set(next.transferId, next);
    return structuredClone(next);
  }
}

export type DeliveryTransferRecoveryState =
  | "detected"
  | "replaying"
  | "verified"
  | "resolved"
  | "quarantined";

export interface DeliveryTransferRecovery {
  recoveryId: string;
  transferId: string;
  deliveryId: string;
  fromTaskId: string;
  toTaskId: string;
  state: DeliveryTransferRecoveryState;
  observedTransferState: DeliveryCustodyTransfer["state"];
  expectedDeliveryDigest: string;
  sourceReceiptHead: string;
  replayStepIds: string[];
  acceptedStepIds: string[];
  failedStepIds: string[];
  resolutionDigest: string;
  quarantineReason: string;
  generation: number;
  createdAt: string;
  updatedAt: string;
  revision: number;
  digest: string;
}

export interface DeliveryTransferReplayStep {
  stepId: string;
  recoveryId: string;
  kind:
    | "validate-source"
    | "reissue-acceptance"
    | "reissue-commit"
    | "verify-target";
  state: "pending" | "claimed" | "accepted" | "failed";
  executorId: string;
  idempotencyKey: string;
  attempt: number;
  expectedInputDigest: string;
  effectDigest: string;
  errorCode: string;
  claimedAt: string;
  settledAt: string;
  previousStepDigest: string;
  revision: number;
  digest: string;
}

export interface DeliveryTransferRecoveryReceipt {
  receiptId: string;
  recoveryId: string;
  transferId: string;
  deliveryId: string;
  generation: number;
  outcome: "resolved" | "quarantined";
  transferDigest: string;
  stepChainDigest: string;
  resolutionDigest: string;
  completedAt: string;
  revision: number;
  digest: string;
}

export interface DeliveryTransferRecoverySnapshot {
  recoveries: DeliveryTransferRecovery[];
  steps: DeliveryTransferReplayStep[];
  receipts: DeliveryTransferRecoveryReceipt[];
  activeRecoveryIdByTransfer: Array<[string, string]>;
  stepIdByIdempotencyKey: Array<[string, string]>;
  digest: string;
}

function assertDeliveryTransferRecovery(value: DeliveryTransferRecovery): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.recoveryId ||
    !value.transferId ||
    !value.deliveryId ||
    !value.fromTaskId ||
    !value.toTaskId ||
    value.fromTaskId === value.toTaskId ||
    !value.expectedDeliveryDigest ||
    !value.sourceReceiptHead ||
    new Set(value.replayStepIds).size !== value.replayStepIds.length ||
    value.acceptedStepIds.some((id) => value.failedStepIds.includes(id)) ||
    value.generation < 1 ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "delivery_transfer_recovery_corrupt",
      `delivery transfer recovery ${value.recoveryId || "<empty>"} is corrupt`,
    );
  if (value.state === "resolved" && !value.resolutionDigest)
    throw new E03RuntimeError(
      "delivery_transfer_recovery_resolution_missing",
      "resolved delivery transfer recovery lacks digest",
    );
  if (value.state === "quarantined" && !value.quarantineReason)
    throw new E03RuntimeError(
      "delivery_transfer_recovery_quarantine_reason_missing",
      "quarantined delivery transfer recovery lacks reason",
    );
}

function assertDeliveryTransferReplayStep(
  value: DeliveryTransferReplayStep,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.stepId ||
    !value.recoveryId ||
    !value.idempotencyKey ||
    value.attempt < 1 ||
    !value.expectedInputDigest ||
    !value.previousStepDigest ||
    value.revision < 1 ||
    (value.state === "accepted" && !value.effectDigest) ||
    (value.state === "failed" && !value.errorCode) ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "delivery_transfer_replay_step_corrupt",
      `delivery transfer replay step ${value.stepId || "<empty>"} is corrupt`,
    );
}

function assertDeliveryTransferRecoveryReceipt(
  value: DeliveryTransferRecoveryReceipt,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.receiptId ||
    !value.recoveryId ||
    !value.transferId ||
    !value.deliveryId ||
    value.generation < 1 ||
    !value.transferDigest ||
    !value.stepChainDigest ||
    !value.resolutionDigest ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "delivery_transfer_recovery_receipt_corrupt",
      `delivery transfer recovery receipt ${value.receiptId || "<empty>"} is corrupt`,
    );
}

export class DeliveryTransferRecoveryRuntime {
  private recoveries = new Map<string, DeliveryTransferRecovery>();
  private steps = new Map<string, DeliveryTransferReplayStep>();
  private receipts = new Map<string, DeliveryTransferRecoveryReceipt>();
  private activeRecoveryIdByTransfer = new Map<string, string>();
  private stepIdByIdempotencyKey = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  detect(input: {
    transfer: DeliveryCustodyTransfer;
    receipts: DeliveryCustodyReceipt[];
    recoveryId?: string;
  }): DeliveryTransferRecovery {
    assertDeliveryCustodyTransfer(input.transfer);
    input.receipts.forEach(assertDeliveryCustodyReceipt);
    if (input.transfer.state === "committed")
      throw new E03RuntimeError(
        "delivery_transfer_recovery_not_required",
        "committed delivery custody transfer does not require recovery",
      );
    const activeId = this.activeRecoveryIdByTransfer.get(
      input.transfer.transferId,
    );
    const active = activeId ? this.recoveries.get(activeId) : undefined;
    if (
      active &&
      active.state !== "resolved" &&
      active.state !== "quarantined"
    ) {
      if (active.expectedDeliveryDigest !== input.transfer.deliveryDigest)
        throw new E03RuntimeError(
          "delivery_transfer_recovery_active_conflict",
          "delivery transfer recovery already tracks another payload",
        );
      return structuredClone(active);
    }
    const ordered = input.receipts
      .slice()
      .sort((left, right) => left.sequence - right.sequence);
    ordered.forEach((receipt, index) => {
      if (
        receipt.transferId !== input.transfer.transferId ||
        receipt.deliveryId !== input.transfer.deliveryId ||
        receipt.sequence !== index + 1 ||
        (index > 0 && receipt.previousDigest !== ordered[index - 1]!.digest)
      )
        throw new E03RuntimeError(
          "delivery_transfer_recovery_receipt_chain_corrupt",
          "delivery custody receipt chain cannot seed recovery",
        );
    });
    const recoveryId =
      input.recoveryId ?? createId("delivery-transfer-recovery");
    const now = this.clock.now();
    const payload = {
      recoveryId,
      transferId: input.transfer.transferId,
      deliveryId: input.transfer.deliveryId,
      fromTaskId: input.transfer.fromTaskId,
      toTaskId: input.transfer.toTaskId,
      state: "detected" as const,
      observedTransferState: input.transfer.state,
      expectedDeliveryDigest: input.transfer.deliveryDigest,
      sourceReceiptHead:
        ordered.at(-1)?.digest ?? digest("delivery-transfer-receipt-root"),
      replayStepIds: [] as string[],
      acceptedStepIds: [] as string[],
      failedStepIds: [] as string[],
      resolutionDigest: "",
      quarantineReason: "",
      generation: (active?.generation ?? 0) + 1,
      createdAt: now,
      updatedAt: now,
      revision: 1,
    };
    const recovery = { ...payload, digest: digest(payload) };
    assertDeliveryTransferRecovery(recovery);
    this.recoveries.set(recovery.recoveryId, recovery);
    this.activeRecoveryIdByTransfer.set(
      recovery.transferId,
      recovery.recoveryId,
    );
    return structuredClone(recovery);
  }

  plan(input: {
    recoveryId: string;
    expectedRevision: number;
    kinds: DeliveryTransferReplayStep["kind"][];
  }): {
    recovery: DeliveryTransferRecovery;
    steps: DeliveryTransferReplayStep[];
  } {
    const recovery = this.requireRecovery(input.recoveryId);
    this.assertRecoveryRevision(recovery, input.expectedRevision);
    if (recovery.state !== "detected")
      throw new E03RuntimeError(
        "delivery_transfer_recovery_plan_invalid_state",
        `cannot plan delivery recovery from ${recovery.state}`,
      );
    const kinds = [...new Set(input.kinds)];
    if (
      !kinds.length ||
      kinds[0] !== "validate-source" ||
      kinds.at(-1) !== "verify-target"
    )
      throw new E03RuntimeError(
        "delivery_transfer_recovery_plan_invalid",
        "delivery recovery plan must validate source and verify target",
      );
    const planned: DeliveryTransferReplayStep[] = [];
    let previousStepDigest = recovery.sourceReceiptHead;
    kinds.forEach((kind, index) => {
      const payload = {
        stepId: createId("delivery-transfer-replay-step"),
        recoveryId: recovery.recoveryId,
        kind,
        state: "pending" as const,
        executorId: "",
        idempotencyKey: `${recovery.recoveryId}:${recovery.generation}:${index + 1}:${kind}`,
        attempt: 1,
        expectedInputDigest:
          index === 0 ? recovery.expectedDeliveryDigest : previousStepDigest,
        effectDigest: "",
        errorCode: "",
        claimedAt: "",
        settledAt: "",
        previousStepDigest,
        revision: 1,
      };
      const step = { ...payload, digest: digest(payload) };
      assertDeliveryTransferReplayStep(step);
      this.steps.set(step.stepId, step);
      this.stepIdByIdempotencyKey.set(step.idempotencyKey, step.stepId);
      planned.push(step);
      previousStepDigest = step.digest;
    });
    const next = this.transitionRecovery(recovery, {
      state: "replaying",
      replayStepIds: planned.map((value) => value.stepId),
      updatedAt: this.clock.now(),
    });
    return {
      recovery: next,
      steps: planned.map((value) => structuredClone(value)),
    };
  }

  claim(input: {
    stepId: string;
    expectedRevision: number;
    executorId: string;
    idempotencyKey: string;
  }): DeliveryTransferReplayStep {
    const step = this.requireStep(input.stepId);
    this.assertStepRevision(step, input.expectedRevision);
    if (step.idempotencyKey !== input.idempotencyKey || !input.executorId)
      throw new E03RuntimeError(
        "delivery_transfer_replay_claim_invalid",
        "delivery transfer replay claim identity is invalid",
      );
    if (step.state === "claimed" && step.executorId === input.executorId)
      return structuredClone(step);
    if (step.state !== "pending" && step.state !== "failed")
      throw new E03RuntimeError(
        "delivery_transfer_replay_claim_invalid_state",
        `cannot claim delivery replay step from ${step.state}`,
      );
    return this.transitionStep(step, {
      state: "claimed",
      executorId: input.executorId,
      attempt: step.state === "failed" ? step.attempt + 1 : step.attempt,
      claimedAt: this.clock.now(),
      settledAt: "",
      errorCode: "",
    });
  }

  settle(input: {
    stepId: string;
    expectedRevision: number;
    executorId: string;
    accepted: boolean;
    effectDigest?: string;
    errorCode?: string;
  }): { recovery: DeliveryTransferRecovery; step: DeliveryTransferReplayStep } {
    const step = this.requireStep(input.stepId);
    this.assertStepRevision(step, input.expectedRevision);
    if (step.state !== "claimed" || step.executorId !== input.executorId)
      throw new E03RuntimeError(
        "delivery_transfer_replay_settle_not_claimed",
        "delivery transfer replay step is not claimed by executor",
      );
    const nextStep = this.transitionStep(step, {
      state: input.accepted ? "accepted" : "failed",
      effectDigest: input.effectDigest ?? "",
      errorCode: input.errorCode ?? "",
      settledAt: this.clock.now(),
    });
    const recovery = this.requireRecovery(step.recoveryId);
    const accepted = new Set(recovery.acceptedStepIds);
    const failed = new Set(recovery.failedStepIds);
    if (input.accepted) {
      accepted.add(step.stepId);
      failed.delete(step.stepId);
    } else {
      failed.add(step.stepId);
      accepted.delete(step.stepId);
    }
    const nextRecovery = this.transitionRecovery(recovery, {
      acceptedStepIds: [...accepted],
      failedStepIds: [...failed],
      updatedAt: this.clock.now(),
    });
    return { recovery: nextRecovery, step: nextStep };
  }

  verify(input: {
    recoveryId: string;
    expectedRevision: number;
  }): DeliveryTransferRecovery {
    const recovery = this.requireRecovery(input.recoveryId);
    this.assertRecoveryRevision(recovery, input.expectedRevision);
    if (recovery.state !== "replaying")
      throw new E03RuntimeError(
        "delivery_transfer_recovery_verify_invalid_state",
        "delivery transfer recovery is not replaying",
      );
    const steps = recovery.replayStepIds.map((id) => this.requireStep(id));
    if (steps.some((step) => step.state !== "accepted"))
      throw new E03RuntimeError(
        "delivery_transfer_recovery_steps_incomplete",
        "delivery transfer recovery replay steps are incomplete",
      );
    return this.transitionRecovery(recovery, {
      state: "verified",
      resolutionDigest: digest(
        steps.map((step) => ({
          kind: step.kind,
          effectDigest: step.effectDigest,
        })),
      ),
      updatedAt: this.clock.now(),
    });
  }

  resolve(input: {
    recoveryId: string;
    expectedRevision: number;
    transfer: DeliveryCustodyTransfer;
  }): {
    recovery: DeliveryTransferRecovery;
    receipt: DeliveryTransferRecoveryReceipt;
  } {
    const recovery = this.requireRecovery(input.recoveryId);
    this.assertRecoveryRevision(recovery, input.expectedRevision);
    assertDeliveryCustodyTransfer(input.transfer);
    if (recovery.state !== "verified")
      throw new E03RuntimeError(
        "delivery_transfer_recovery_not_verified",
        "delivery transfer recovery must be verified before resolve",
      );
    if (
      input.transfer.transferId !== recovery.transferId ||
      input.transfer.deliveryId !== recovery.deliveryId ||
      input.transfer.deliveryDigest !== recovery.expectedDeliveryDigest ||
      input.transfer.state !== "committed"
    )
      throw new E03RuntimeError(
        "delivery_transfer_recovery_resolution_mismatch",
        "delivery transfer recovery final transfer does not match",
      );
    const steps = recovery.replayStepIds.map((id) => this.requireStep(id));
    const receiptPayload = {
      receiptId: createId("delivery-transfer-recovery-receipt"),
      recoveryId: recovery.recoveryId,
      transferId: recovery.transferId,
      deliveryId: recovery.deliveryId,
      generation: recovery.generation,
      outcome: "resolved" as const,
      transferDigest: input.transfer.digest,
      stepChainDigest: digest(steps.map((value) => value.digest)),
      resolutionDigest: recovery.resolutionDigest,
      completedAt: this.clock.now(),
      revision: 1,
    };
    const receipt = { ...receiptPayload, digest: digest(receiptPayload) };
    assertDeliveryTransferRecoveryReceipt(receipt);
    this.receipts.set(receipt.receiptId, receipt);
    const next = this.transitionRecovery(recovery, {
      state: "resolved",
      updatedAt: this.clock.now(),
    });
    this.activeRecoveryIdByTransfer.delete(recovery.transferId);
    return { recovery: next, receipt: structuredClone(receipt) };
  }

  quarantine(input: {
    recoveryId: string;
    expectedRevision: number;
    reason: string;
    transferDigest: string;
  }): {
    recovery: DeliveryTransferRecovery;
    receipt: DeliveryTransferRecoveryReceipt;
  } {
    const recovery = this.requireRecovery(input.recoveryId);
    this.assertRecoveryRevision(recovery, input.expectedRevision);
    if (recovery.state === "resolved")
      throw new E03RuntimeError(
        "delivery_transfer_recovery_quarantine_resolved",
        "resolved delivery transfer recovery cannot quarantine",
      );
    if (!input.reason || !input.transferDigest)
      throw new E03RuntimeError(
        "delivery_transfer_recovery_quarantine_invalid",
        "delivery transfer quarantine requires reason and transfer digest",
      );
    const steps = recovery.replayStepIds.map((id) => this.requireStep(id));
    const resolutionDigest = digest({
      reason: input.reason,
      transferDigest: input.transferDigest,
    });
    const receiptPayload = {
      receiptId: createId("delivery-transfer-recovery-receipt"),
      recoveryId: recovery.recoveryId,
      transferId: recovery.transferId,
      deliveryId: recovery.deliveryId,
      generation: recovery.generation,
      outcome: "quarantined" as const,
      transferDigest: input.transferDigest,
      stepChainDigest: digest(steps.map((value) => value.digest)),
      resolutionDigest,
      completedAt: this.clock.now(),
      revision: 1,
    };
    const receipt = { ...receiptPayload, digest: digest(receiptPayload) };
    assertDeliveryTransferRecoveryReceipt(receipt);
    this.receipts.set(receipt.receiptId, receipt);
    const next = this.transitionRecovery(recovery, {
      state: "quarantined",
      resolutionDigest,
      quarantineReason: input.reason,
      updatedAt: this.clock.now(),
    });
    this.activeRecoveryIdByTransfer.delete(recovery.transferId);
    return { recovery: next, receipt: structuredClone(receipt) };
  }

  snapshot(): DeliveryTransferRecoverySnapshot {
    const payload = {
      recoveries: [...this.recoveries.values()].map((value) =>
        structuredClone(value),
      ),
      steps: [...this.steps.values()].map((value) => structuredClone(value)),
      receipts: [...this.receipts.values()].map((value) =>
        structuredClone(value),
      ),
      activeRecoveryIdByTransfer: [
        ...this.activeRecoveryIdByTransfer.entries(),
      ],
      stepIdByIdempotencyKey: [...this.stepIdByIdempotencyKey.entries()],
    };
    return { ...payload, digest: digest(payload) };
  }

  restore(snapshot: DeliveryTransferRecoverySnapshot): void {
    const { digest: expected, ...payload } = snapshot;
    if (digest(payload) !== expected)
      throw new E03RuntimeError(
        "delivery_transfer_recovery_snapshot_corrupt",
        "delivery transfer recovery snapshot digest mismatch",
      );
    const recoveries = new Map<string, DeliveryTransferRecovery>();
    const steps = new Map<string, DeliveryTransferReplayStep>();
    const receipts = new Map<string, DeliveryTransferRecoveryReceipt>();
    for (const value of payload.recoveries) {
      assertDeliveryTransferRecovery(value);
      if (recoveries.has(value.recoveryId))
        throw new E03RuntimeError(
          "delivery_transfer_recovery_snapshot_duplicate",
          `duplicate delivery transfer recovery ${value.recoveryId}`,
        );
      recoveries.set(value.recoveryId, structuredClone(value));
    }
    for (const value of payload.steps) {
      assertDeliveryTransferReplayStep(value);
      if (!recoveries.has(value.recoveryId))
        throw new E03RuntimeError(
          "delivery_transfer_recovery_snapshot_step_orphaned",
          `delivery transfer recovery step ${value.stepId} is orphaned`,
        );
      steps.set(value.stepId, structuredClone(value));
    }
    for (const value of payload.receipts) {
      assertDeliveryTransferRecoveryReceipt(value);
      if (!recoveries.has(value.recoveryId))
        throw new E03RuntimeError(
          "delivery_transfer_recovery_snapshot_receipt_orphaned",
          `delivery transfer recovery receipt ${value.receiptId} is orphaned`,
        );
      receipts.set(value.receiptId, structuredClone(value));
    }
    const activeIndex = new Map(payload.activeRecoveryIdByTransfer);
    for (const [transferId, recoveryId] of activeIndex) {
      const value = recoveries.get(recoveryId);
      if (
        !value ||
        value.transferId !== transferId ||
        ["resolved", "quarantined"].includes(value.state)
      )
        throw new E03RuntimeError(
          "delivery_transfer_recovery_snapshot_active_index_corrupt",
          `delivery transfer recovery active index ${recoveryId} is invalid`,
        );
    }
    const stepIndex = new Map(payload.stepIdByIdempotencyKey);
    for (const stepId of stepIndex.values())
      if (!steps.has(stepId))
        throw new E03RuntimeError(
          "delivery_transfer_recovery_snapshot_step_index_corrupt",
          `delivery transfer recovery step index references ${stepId}`,
        );
    this.recoveries = recoveries;
    this.steps = steps;
    this.receipts = receipts;
    this.activeRecoveryIdByTransfer = activeIndex;
    this.stepIdByIdempotencyKey = stepIndex;
  }

  private requireRecovery(id: string): DeliveryTransferRecovery {
    const value = this.recoveries.get(id);
    if (!value)
      throw new E03RuntimeError(
        "delivery_transfer_recovery_missing",
        `delivery transfer recovery ${id} does not exist`,
      );
    assertDeliveryTransferRecovery(value);
    return value;
  }

  private requireStep(id: string): DeliveryTransferReplayStep {
    const value = this.steps.get(id);
    if (!value)
      throw new E03RuntimeError(
        "delivery_transfer_replay_step_missing",
        `delivery transfer replay step ${id} does not exist`,
      );
    assertDeliveryTransferReplayStep(value);
    return value;
  }

  private assertRecoveryRevision(
    value: DeliveryTransferRecovery,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "delivery_transfer_recovery_stale_revision",
        `delivery transfer recovery ${value.recoveryId} revision is stale`,
      );
  }

  private assertStepRevision(
    value: DeliveryTransferReplayStep,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "delivery_transfer_replay_step_stale_revision",
        `delivery transfer replay step ${value.stepId} revision is stale`,
      );
  }

  private transitionRecovery(
    value: DeliveryTransferRecovery,
    patch: Partial<
      Omit<DeliveryTransferRecovery, "recoveryId" | "revision" | "digest">
    >,
  ): DeliveryTransferRecovery {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      recoveryId: value.recoveryId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertDeliveryTransferRecovery(next);
    this.recoveries.set(next.recoveryId, next);
    return structuredClone(next);
  }

  private transitionStep(
    value: DeliveryTransferReplayStep,
    patch: Partial<
      Omit<DeliveryTransferReplayStep, "stepId" | "revision" | "digest">
    >,
  ): DeliveryTransferReplayStep {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      stepId: value.stepId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertDeliveryTransferReplayStep(next);
    this.steps.set(next.stepId, next);
    return structuredClone(next);
  }
}
