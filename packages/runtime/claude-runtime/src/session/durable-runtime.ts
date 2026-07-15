import { createHash, randomBytes, randomUUID } from "node:crypto";

import { asBoolean, asObject, asString, type JsonObject, type JsonValue } from "../contracts.ts";

export const DURABLE_SESSION_SNAPSHOT_VERSION = "zyra.durable-session/v1";
export const DURABLE_CHECKPOINT_VERSION = "zyra.durable-checkpoint/v1";

export type DurableSessionStatus = "created" | "active" | "paused" | "completed" | "failed" | "cancelled";
export type DurableMessageRole = "system" | "user" | "assistant" | "tool";
export type DurableEffectState = "prepared" | "executing" | "committed" | "failed" | "cancelled";
export type DurableOutboxState = "pending" | "leased" | "delivered" | "dead_letter";

export interface DurableSessionIdentity {
  sessionId: string;
  runId: string;
  taskId: string;
  workerRequestId: string;
  tenantId: string | null;
  parentSessionId: string | null;
  branchId: string;
}

export interface DurableMessage {
  messageId: string;
  role: DurableMessageRole;
  content: JsonValue;
  contentDigest: string;
  turnId: string | null;
  toolCallId: string | null;
  correlationId: string;
  causationId: string | null;
  metadata: JsonObject;
  createdAt: string;
  revision: number;
}

export interface DurableEffect {
  effectId: string;
  effectKind: string;
  idempotencyKey: string;
  requestDigest: string;
  responseDigest: string | null;
  state: DurableEffectState;
  attempt: number;
  leaseOwner: string | null;
  leaseExpiresAt: string | null;
  errorCode: string | null;
  errorMessage: string | null;
  preparedAt: string;
  committedAt: string | null;
  revision: number;
}

export interface DurableOutboxItem {
  outboxId: string;
  topic: string;
  partitionKey: string;
  payload: JsonValue;
  payloadDigest: string;
  idempotencyKey: string;
  state: DurableOutboxState;
  attempt: number;
  leaseOwner: string | null;
  leaseExpiresAt: string | null;
  nextAttemptAt: string;
  deliveredAt: string | null;
  lastError: string | null;
  createdAt: string;
  revision: number;
}

export interface DurableResumeToken {
  tokenId: string;
  sessionId: string;
  checkpointId: string;
  restartEpoch: number;
  issuedAt: string;
  expiresAt: string;
  correlationId: string;
  nonce: string;
  signature: string;
  consumedAt: string | null;
}

export interface DurableCheckpoint {
  version: typeof DURABLE_CHECKPOINT_VERSION;
  checkpointId: string;
  sessionId: string;
  runId: string;
  branchId: string;
  parentCheckpointId: string | null;
  revision: number;
  sequence: number;
  restartEpoch: number;
  status: DurableSessionStatus;
  state: JsonObject;
  stateDigest: string;
  pendingEffectIds: string[];
  committedEffectIds: string[];
  pendingOutboxIds: string[];
  messageHeadId: string | null;
  createdAt: string;
  checksum: string;
}

export interface DurableSessionSnapshot {
  version: typeof DURABLE_SESSION_SNAPSHOT_VERSION;
  identity: DurableSessionIdentity;
  status: DurableSessionStatus;
  revision: number;
  sequence: number;
  restartEpoch: number;
  messages: DurableMessage[];
  effects: DurableEffect[];
  outbox: DurableOutboxItem[];
  checkpoints: DurableCheckpoint[];
  resumeTokens: DurableResumeToken[];
  state: JsonObject;
  lastCheckpointId: string | null;
  committedIdempotency: Record<string, string>;
  checksum: string;
}

export class DurableSessionRuntime {
  private readonly identity: DurableSessionIdentity;
  private readonly signingSecret: string;
  private readonly messages = new Map<string, DurableMessage>();
  private readonly effects = new Map<string, DurableEffect>();
  private readonly outbox = new Map<string, DurableOutboxItem>();
  private readonly checkpoints = new Map<string, DurableCheckpoint>();
  private readonly resumeTokens = new Map<string, DurableResumeToken>();
  private readonly committedIdempotency = new Map<string, string>();
  private state: JsonObject = {};
  private status: DurableSessionStatus = "created";
  private revision = 0;
  private sequence = 0;
  private restartEpoch = 0;
  private lastCheckpointId: string | null = null;

  constructor(identity: DurableSessionIdentity, signingSecret?: string) {
    this.identity = normalizeIdentity(identity);
    this.signingSecret = signingSecret?.trim() || randomBytes(32).toString("hex");
  }

  lifecycle_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "activate") this.activate(asString(value.correlation_id, this.identity.sessionId));
    else if (action === "pause") this.pause(asString(value.correlation_id, randomUUID()));
    else if (action === "complete") this.finish("completed", asString(value.correlation_id, randomUUID()));
    else if (action === "fail") this.finish("failed", asString(value.correlation_id, randomUUID()));
    else if (action === "cancel") this.finish("cancelled", asString(value.correlation_id, randomUUID()));
    return this.project();
  }

  snapshot_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "checkpoint");
    if (action === "checkpoint") return checkpointToJson(this.checkpoint(asObject(value.state)));
    if (action === "resume_token") return resumeTokenToJson(this.issueResumeToken(asString(value.correlation_id, randomUUID()), positive(value.ttl_ms, 60 * 60 * 1_000)));
    return this.snapshot() as unknown as JsonObject;
  }

  effect_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "prepare") return effectToJson(this.prepareEffect({
      effectKind: asString(value.effect_kind),
      idempotencyKey: asString(value.idempotency_key, randomUUID()),
      request: value.request ?? null,
    }));
    if (action === "start") return effectToJson(this.startEffect(asString(value.effect_id), asString(value.owner), positive(value.ttl_ms, 30_000)));
    if (action === "commit") return effectToJson(this.commitEffect(asString(value.effect_id), value.response ?? null));
    if (action === "fail") return effectToJson(this.failEffect(asString(value.effect_id), asString(value.error_code, "effect_failed"), asString(value.error_message)));
    return effectToJson(this.requireEffect(asString(value.effect_id)));
  }

  outbox_module(value: JsonObject): JsonObject {
    const action = asString(value.action, "inspect");
    if (action === "enqueue") return outboxToJson(this.enqueue({
      topic: asString(value.topic),
      partitionKey: asString(value.partition_key, this.identity.sessionId),
      payload: value.payload ?? null,
      idempotencyKey: asString(value.idempotency_key, randomUUID()),
    }));
    if (action === "lease") return { items: this.leaseOutbox(asString(value.owner), positive(value.maximum, 10), positive(value.ttl_ms, 30_000)).map(outboxToJson) };
    if (action === "ack") return outboxToJson(this.ackOutbox(asString(value.outbox_id), asString(value.owner)));
    if (action === "nack") return outboxToJson(this.nackOutbox(asString(value.outbox_id), asString(value.owner), asString(value.error), positive(value.delay_ms, 1_000)));
    return outboxToJson(this.requireOutbox(asString(value.outbox_id)));
  }

  activate(correlationId: string): void {
    if (this.status === "active") return;
    if (this.status !== "created" && this.status !== "paused") throw new Error(`cannot activate session from ${this.status}`);
    this.status = "active";
    this.appendMessage({
      role: "system",
      content: { event: "session_activated", restart_epoch: this.restartEpoch },
      turnId: null,
      toolCallId: null,
      correlationId,
      causationId: null,
      metadata: { effective: true },
    });
    this.bump();
  }

  pause(correlationId: string): void {
    if (this.status === "paused") return;
    if (this.status !== "active") throw new Error(`cannot pause session from ${this.status}`);
    this.status = "paused";
    this.appendMessage({
      role: "system",
      content: { event: "session_paused" },
      turnId: null,
      toolCallId: null,
      correlationId,
      causationId: null,
      metadata: { effective: true },
    });
    this.bump();
  }

  finish(status: Extract<DurableSessionStatus, "completed" | "failed" | "cancelled">, correlationId: string): void {
    if (this.isTerminal()) return;
    if ([...this.effects.values()].some((effect) => effect.state === "executing")) {
      throw new Error("cannot finish session while external effects are executing");
    }
    this.status = status;
    this.appendMessage({
      role: "system",
      content: { event: `session_${status}` },
      turnId: null,
      toolCallId: null,
      correlationId,
      causationId: null,
      metadata: { effective: true },
    });
    this.bump();
  }

  appendMessage(input: {
    messageId?: string;
    role: DurableMessageRole;
    content: JsonValue;
    turnId: string | null;
    toolCallId: string | null;
    correlationId: string;
    causationId: string | null;
    metadata?: JsonObject;
    createdAt?: string;
  }): DurableMessage {
    const messageId = input.messageId?.trim() || randomUUID();
    const existing = this.messages.get(messageId);
    const contentDigest = digest(input.content);
    if (existing) {
      if (existing.contentDigest !== contentDigest) throw new Error(`message id conflict: ${messageId}`);
      return structuredClone(existing);
    }
    this.sequence += 1;
    const message: DurableMessage = {
      messageId,
      role: messageRole(input.role),
      content: structuredClone(input.content),
      contentDigest,
      turnId: input.turnId?.trim() || null,
      toolCallId: input.toolCallId?.trim() || null,
      correlationId: required(input.correlationId, "message correlation id"),
      causationId: input.causationId?.trim() || null,
      metadata: asObject(input.metadata),
      createdAt: normalizeTimestamp(input.createdAt),
      revision: 1,
    };
    this.messages.set(messageId, message);
    this.bump();
    return structuredClone(message);
  }

  updateState(path: readonly string[], value: JsonValue, expectedDigest?: string): JsonObject {
    if (path.length === 0) {
      const replacement = asObject(value);
      if (expectedDigest && digest(this.state) !== expectedDigest) throw new Error("session state compare-and-swap conflict");
      this.state = structuredClone(replacement);
      this.bump();
      return structuredClone(this.state);
    }
    for (const segment of path) if (!segment.trim() || segment === "__proto__" || segment === "constructor") throw new Error(`invalid session state path: ${segment}`);
    const root = structuredClone(this.state);
    let cursor: JsonObject = root;
    for (let index = 0; index < path.length - 1; index += 1) {
      const key = path[index];
      const child = asObject(cursor[key]);
      cursor[key] = child;
      cursor = child;
    }
    const key = path.at(-1)!;
    if (expectedDigest && digest(cursor[key] ?? null) !== expectedDigest) throw new Error("session state compare-and-swap conflict");
    cursor[key] = structuredClone(value);
    this.state = root;
    this.bump();
    return structuredClone(this.state);
  }

  prepareEffect(input: { effectKind: string; idempotencyKey: string; request: JsonValue }): DurableEffect {
    const existingId = this.committedIdempotency.get(input.idempotencyKey);
    if (existingId) return structuredClone(this.requireEffect(existingId));
    for (const effect of this.effects.values()) {
      if (effect.idempotencyKey !== input.idempotencyKey) continue;
      if (effect.requestDigest !== digest(input.request)) throw new Error("effect idempotency request conflict");
      return structuredClone(effect);
    }
    const effect: DurableEffect = {
      effectId: randomUUID(),
      effectKind: required(input.effectKind, "effect kind"),
      idempotencyKey: required(input.idempotencyKey, "effect idempotency key"),
      requestDigest: digest(input.request),
      responseDigest: null,
      state: "prepared",
      attempt: 0,
      leaseOwner: null,
      leaseExpiresAt: null,
      errorCode: null,
      errorMessage: null,
      preparedAt: new Date().toISOString(),
      committedAt: null,
      revision: 1,
    };
    this.effects.set(effect.effectId, effect);
    this.bump();
    return structuredClone(effect);
  }

  startEffect(effectId: string, owner: string, ttlMs: number): DurableEffect {
    const effect = this.requireEffect(effectId);
    if (effect.state === "committed") return structuredClone(effect);
    if (effect.state === "executing" && effect.leaseExpiresAt && Date.parse(effect.leaseExpiresAt) > Date.now() && effect.leaseOwner !== owner) {
      throw new Error(`effect lease held by ${effect.leaseOwner}`);
    }
    if (effect.state !== "prepared" && effect.state !== "failed" && effect.state !== "executing") throw new Error(`effect cannot start from ${effect.state}`);
    effect.state = "executing";
    effect.attempt += 1;
    effect.leaseOwner = required(owner, "effect lease owner");
    effect.leaseExpiresAt = new Date(Date.now() + positive(ttlMs, 30_000)).toISOString();
    effect.errorCode = null;
    effect.errorMessage = null;
    effect.revision += 1;
    this.bump();
    return structuredClone(effect);
  }

  commitEffect(effectId: string, response: JsonValue): DurableEffect {
    const effect = this.requireEffect(effectId);
    const responseDigest = digest(response);
    if (effect.state === "committed") {
      if (effect.responseDigest !== responseDigest) throw new Error("committed effect response conflict");
      return structuredClone(effect);
    }
    if (effect.state !== "executing") throw new Error(`effect cannot commit from ${effect.state}`);
    effect.state = "committed";
    effect.responseDigest = responseDigest;
    effect.committedAt = new Date().toISOString();
    effect.leaseOwner = null;
    effect.leaseExpiresAt = null;
    effect.revision += 1;
    this.committedIdempotency.set(effect.idempotencyKey, effect.effectId);
    this.bump();
    return structuredClone(effect);
  }

  failEffect(effectId: string, code: string, message: string): DurableEffect {
    const effect = this.requireEffect(effectId);
    if (effect.state === "committed" || effect.state === "cancelled") throw new Error(`effect cannot fail from ${effect.state}`);
    effect.state = "failed";
    effect.errorCode = required(code, "effect error code");
    effect.errorMessage = message.trim().slice(0, 8_192);
    effect.leaseOwner = null;
    effect.leaseExpiresAt = null;
    effect.revision += 1;
    this.bump();
    return structuredClone(effect);
  }

  enqueue(input: { topic: string; partitionKey: string; payload: JsonValue; idempotencyKey: string }): DurableOutboxItem {
    for (const item of this.outbox.values()) {
      if (item.idempotencyKey !== input.idempotencyKey) continue;
      if (item.payloadDigest !== digest(input.payload)) throw new Error("outbox idempotency payload conflict");
      return structuredClone(item);
    }
    const now = new Date().toISOString();
    const item: DurableOutboxItem = {
      outboxId: randomUUID(),
      topic: required(input.topic, "outbox topic"),
      partitionKey: required(input.partitionKey, "outbox partition key"),
      payload: structuredClone(input.payload),
      payloadDigest: digest(input.payload),
      idempotencyKey: required(input.idempotencyKey, "outbox idempotency key"),
      state: "pending",
      attempt: 0,
      leaseOwner: null,
      leaseExpiresAt: null,
      nextAttemptAt: now,
      deliveredAt: null,
      lastError: null,
      createdAt: now,
      revision: 1,
    };
    this.outbox.set(item.outboxId, item);
    this.bump();
    return structuredClone(item);
  }

  leaseOutbox(owner: string, maximum = 10, ttlMs = 30_000): DurableOutboxItem[] {
    const now = Date.now();
    const selected = [...this.outbox.values()]
      .filter((item) => {
        if (item.state === "delivered" || item.state === "dead_letter") return false;
        if (item.state === "leased" && item.leaseExpiresAt && Date.parse(item.leaseExpiresAt) > now) return false;
        return Date.parse(item.nextAttemptAt) <= now;
      })
      .sort((left, right) => Date.parse(left.createdAt) - Date.parse(right.createdAt))
      .slice(0, Math.max(0, Math.floor(maximum)));
    for (const item of selected) {
      item.state = "leased";
      item.attempt += 1;
      item.leaseOwner = required(owner, "outbox lease owner");
      item.leaseExpiresAt = new Date(now + positive(ttlMs, 30_000)).toISOString();
      item.revision += 1;
    }
    if (selected.length > 0) this.bump();
    return structuredClone(selected);
  }

  ackOutbox(outboxId: string, owner: string): DurableOutboxItem {
    const item = this.requireOutbox(outboxId);
    if (item.state === "delivered") return structuredClone(item);
    this.assertOutboxLease(item, owner);
    item.state = "delivered";
    item.deliveredAt = new Date().toISOString();
    item.leaseOwner = null;
    item.leaseExpiresAt = null;
    item.lastError = null;
    item.revision += 1;
    this.bump();
    return structuredClone(item);
  }

  nackOutbox(outboxId: string, owner: string, error: string, delayMs: number): DurableOutboxItem {
    const item = this.requireOutbox(outboxId);
    this.assertOutboxLease(item, owner);
    item.state = item.attempt >= 10 ? "dead_letter" : "pending";
    item.nextAttemptAt = new Date(Date.now() + positive(delayMs, 1_000)).toISOString();
    item.leaseOwner = null;
    item.leaseExpiresAt = null;
    item.lastError = error.trim().slice(0, 8_192);
    item.revision += 1;
    this.bump();
    return structuredClone(item);
  }

  checkpoint(nextState: JsonObject = this.state): DurableCheckpoint {
    const pendingEffects = [...this.effects.values()].filter((item) => item.state !== "committed" && item.state !== "cancelled");
    const committedEffects = [...this.effects.values()].filter((item) => item.state === "committed");
    const pendingOutbox = [...this.outbox.values()].filter((item) => item.state !== "delivered" && item.state !== "dead_letter");
    const unsigned: Omit<DurableCheckpoint, "checksum"> = {
      version: DURABLE_CHECKPOINT_VERSION,
      checkpointId: randomUUID(),
      sessionId: this.identity.sessionId,
      runId: this.identity.runId,
      branchId: this.identity.branchId,
      parentCheckpointId: this.lastCheckpointId,
      revision: this.revision,
      sequence: this.sequence,
      restartEpoch: this.restartEpoch,
      status: this.status,
      state: structuredClone(nextState),
      stateDigest: digest(nextState),
      pendingEffectIds: pendingEffects.map((item) => item.effectId).sort(),
      committedEffectIds: committedEffects.map((item) => item.effectId).sort(),
      pendingOutboxIds: pendingOutbox.map((item) => item.outboxId).sort(),
      messageHeadId: [...this.messages.values()].at(-1)?.messageId ?? null,
      createdAt: new Date().toISOString(),
    };
    const checkpoint: DurableCheckpoint = { ...unsigned, checksum: digest(unsigned) };
    this.checkpoints.set(checkpoint.checkpointId, checkpoint);
    this.lastCheckpointId = checkpoint.checkpointId;
    this.state = structuredClone(nextState);
    this.bump();
    return structuredClone(checkpoint);
  }

  issueResumeToken(correlationId: string, ttlMs = 60 * 60 * 1_000): DurableResumeToken {
    if (!this.lastCheckpointId) this.checkpoint();
    const issuedAt = new Date().toISOString();
    const unsigned = {
      tokenId: randomUUID(),
      sessionId: this.identity.sessionId,
      checkpointId: this.lastCheckpointId!,
      restartEpoch: this.restartEpoch,
      issuedAt,
      expiresAt: new Date(Date.parse(issuedAt) + positive(ttlMs, 60 * 60 * 1_000)).toISOString(),
      correlationId: required(correlationId, "resume correlation id"),
      nonce: randomBytes(16).toString("hex"),
      consumedAt: null,
    };
    const token: DurableResumeToken = { ...unsigned, signature: sign(unsigned, this.signingSecret) };
    this.resumeTokens.set(token.tokenId, token);
    this.bump();
    return structuredClone(token);
  }

  consumeResumeToken(token: DurableResumeToken): DurableCheckpoint {
    const stored = this.resumeTokens.get(token.tokenId);
    if (!stored) throw new Error("resume token is unknown");
    const { signature, ...unsigned } = token;
    if (sign(unsigned, this.signingSecret) !== signature || stored.signature !== signature) throw new Error("resume token signature mismatch");
    if (stored.consumedAt) throw new Error("resume token already consumed");
    if (Date.parse(stored.expiresAt) <= Date.now()) throw new Error("resume token expired");
    if (stored.sessionId !== this.identity.sessionId) throw new Error("resume token session mismatch");
    const checkpoint = this.checkpoints.get(stored.checkpointId);
    if (!checkpoint) throw new Error("resume checkpoint is missing");
    stored.consumedAt = new Date().toISOString();
    this.restartEpoch = Math.max(this.restartEpoch, checkpoint.restartEpoch) + 1;
    this.recoverPendingWork();
    this.bump();
    return structuredClone(checkpoint);
  }

  project(): JsonObject {
    return {
      identity: identityToJson(this.identity),
      status: this.status,
      revision: this.revision,
      sequence: this.sequence,
      restart_epoch: this.restartEpoch,
      message_count: this.messages.size,
      effect_count: this.effects.size,
      pending_effect_count: [...this.effects.values()].filter((item) => item.state !== "committed" && item.state !== "cancelled").length,
      pending_outbox_count: [...this.outbox.values()].filter((item) => item.state === "pending" || item.state === "leased").length,
      checkpoint_count: this.checkpoints.size,
      last_checkpoint_id: this.lastCheckpointId,
      state_digest: digest(this.state),
    };
  }

  snapshot(): DurableSessionSnapshot {
    const unsigned: Omit<DurableSessionSnapshot, "checksum"> = {
      version: DURABLE_SESSION_SNAPSHOT_VERSION,
      identity: structuredClone(this.identity),
      status: this.status,
      revision: this.revision,
      sequence: this.sequence,
      restartEpoch: this.restartEpoch,
      messages: structuredClone([...this.messages.values()]),
      effects: structuredClone([...this.effects.values()]),
      outbox: structuredClone([...this.outbox.values()]),
      checkpoints: structuredClone([...this.checkpoints.values()]),
      resumeTokens: structuredClone([...this.resumeTokens.values()]),
      state: structuredClone(this.state),
      lastCheckpointId: this.lastCheckpointId,
      committedIdempotency: Object.fromEntries(this.committedIdempotency),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshot: DurableSessionSnapshot): void {
    if (snapshot.version !== DURABLE_SESSION_SNAPSHOT_VERSION) throw new Error("unsupported durable session snapshot version");
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) throw new Error("durable session snapshot checksum mismatch");
    if (snapshot.identity.sessionId !== this.identity.sessionId || snapshot.identity.branchId !== this.identity.branchId) {
      throw new Error("durable session snapshot identity mismatch");
    }
    validateSnapshot(snapshot);
    this.messages.clear();
    for (const message of snapshot.messages) this.messages.set(message.messageId, structuredClone(message));
    this.effects.clear();
    for (const effect of snapshot.effects) this.effects.set(effect.effectId, structuredClone(effect));
    this.outbox.clear();
    for (const item of snapshot.outbox) this.outbox.set(item.outboxId, structuredClone(item));
    this.checkpoints.clear();
    for (const checkpoint of snapshot.checkpoints) this.checkpoints.set(checkpoint.checkpointId, structuredClone(checkpoint));
    this.resumeTokens.clear();
    for (const token of snapshot.resumeTokens) this.resumeTokens.set(token.tokenId, structuredClone(token));
    this.state = structuredClone(snapshot.state);
    this.status = snapshot.status;
    this.revision = snapshot.revision;
    this.sequence = snapshot.sequence;
    this.restartEpoch = snapshot.restartEpoch + 1;
    this.lastCheckpointId = snapshot.lastCheckpointId;
    this.committedIdempotency.clear();
    for (const [key, value] of Object.entries(snapshot.committedIdempotency)) this.committedIdempotency.set(key, value);
    this.recoverPendingWork();
    this.bump();
  }

  private recoverPendingWork(): void {
    const now = new Date().toISOString();
    for (const effect of this.effects.values()) {
      if (effect.state !== "executing") continue;
      if (effect.leaseExpiresAt && Date.parse(effect.leaseExpiresAt) > Date.now()) continue;
      effect.state = "prepared";
      effect.leaseOwner = null;
      effect.leaseExpiresAt = null;
      effect.errorCode = "lease_lost_on_restore";
      effect.errorMessage = "effect returned to prepared state after restore";
      effect.revision += 1;
    }
    for (const item of this.outbox.values()) {
      if (item.state !== "leased") continue;
      item.state = "pending";
      item.leaseOwner = null;
      item.leaseExpiresAt = null;
      item.nextAttemptAt = now;
      item.revision += 1;
    }
  }

  private assertOutboxLease(item: DurableOutboxItem, owner: string): void {
    if (item.state !== "leased") throw new Error(`outbox item is not leased: ${item.state}`);
    if (item.leaseOwner !== owner) throw new Error("outbox lease owner mismatch");
    if (!item.leaseExpiresAt || Date.parse(item.leaseExpiresAt) <= Date.now()) throw new Error("outbox lease expired");
  }

  private requireEffect(effectId: string): DurableEffect {
    const effect = this.effects.get(effectId);
    if (!effect) throw new Error(`durable effect not found: ${effectId}`);
    return effect;
  }

  private requireOutbox(outboxId: string): DurableOutboxItem {
    const item = this.outbox.get(outboxId);
    if (!item) throw new Error(`outbox item not found: ${outboxId}`);
    return item;
  }

  private isTerminal(): boolean {
    return this.status === "completed" || this.status === "failed" || this.status === "cancelled";
  }

  private bump(): void {
    this.revision += 1;
  }
}

function validateSnapshot(snapshot: DurableSessionSnapshot): void {
  const messageIds = new Set<string>();
  for (const message of snapshot.messages) {
    if (messageIds.has(message.messageId)) throw new Error(`duplicate durable message: ${message.messageId}`);
    if (digest(message.content) !== message.contentDigest) throw new Error(`durable message digest mismatch: ${message.messageId}`);
    messageIds.add(message.messageId);
  }
  const effectIds = new Set(snapshot.effects.map((item) => item.effectId));
  const outboxIds = new Set(snapshot.outbox.map((item) => item.outboxId));
  let parent: string | null = null;
  for (const checkpoint of snapshot.checkpoints) {
    const { checksum, ...unsigned } = checkpoint;
    if (digest(unsigned) !== checksum) throw new Error(`durable checkpoint checksum mismatch: ${checkpoint.checkpointId}`);
    if (checkpoint.parentCheckpointId !== parent) throw new Error("durable checkpoint lineage is discontinuous");
    for (const id of [...checkpoint.pendingEffectIds, ...checkpoint.committedEffectIds]) if (!effectIds.has(id)) throw new Error(`checkpoint references missing effect: ${id}`);
    for (const id of checkpoint.pendingOutboxIds) if (!outboxIds.has(id)) throw new Error(`checkpoint references missing outbox item: ${id}`);
    parent = checkpoint.checkpointId;
  }
  if (snapshot.lastCheckpointId !== parent) throw new Error("durable session last checkpoint mismatch");
}

function normalizeIdentity(value: DurableSessionIdentity): DurableSessionIdentity {
  return {
    sessionId: required(value.sessionId, "session id"),
    runId: required(value.runId, "run id"),
    taskId: required(value.taskId, "task id"),
    workerRequestId: required(value.workerRequestId, "worker request id"),
    tenantId: value.tenantId?.trim() || null,
    parentSessionId: value.parentSessionId?.trim() || null,
    branchId: required(value.branchId, "branch id"),
  };
}

function effectToJson(value: DurableEffect): JsonObject {
  return {
    effect_id: value.effectId,
    effect_kind: value.effectKind,
    idempotency_key: value.idempotencyKey,
    request_digest: value.requestDigest,
    response_digest: value.responseDigest,
    state: value.state,
    attempt: value.attempt,
    lease_owner: value.leaseOwner,
    lease_expires_at: value.leaseExpiresAt,
    error_code: value.errorCode,
    error_message: value.errorMessage,
    prepared_at: value.preparedAt,
    committed_at: value.committedAt,
    revision: value.revision,
  };
}

function outboxToJson(value: DurableOutboxItem): JsonObject {
  return {
    outbox_id: value.outboxId,
    topic: value.topic,
    partition_key: value.partitionKey,
    payload: value.payload,
    payload_digest: value.payloadDigest,
    idempotency_key: value.idempotencyKey,
    state: value.state,
    attempt: value.attempt,
    lease_owner: value.leaseOwner,
    lease_expires_at: value.leaseExpiresAt,
    next_attempt_at: value.nextAttemptAt,
    delivered_at: value.deliveredAt,
    last_error: value.lastError,
    created_at: value.createdAt,
    revision: value.revision,
  };
}

function checkpointToJson(value: DurableCheckpoint): JsonObject {
  return {
    version: value.version,
    checkpoint_id: value.checkpointId,
    session_id: value.sessionId,
    run_id: value.runId,
    branch_id: value.branchId,
    parent_checkpoint_id: value.parentCheckpointId,
    revision: value.revision,
    sequence: value.sequence,
    restart_epoch: value.restartEpoch,
    status: value.status,
    state: value.state,
    state_digest: value.stateDigest,
    pending_effect_ids: value.pendingEffectIds,
    committed_effect_ids: value.committedEffectIds,
    pending_outbox_ids: value.pendingOutboxIds,
    message_head_id: value.messageHeadId,
    created_at: value.createdAt,
    checksum: value.checksum,
  };
}

function resumeTokenToJson(value: DurableResumeToken): JsonObject {
  return {
    token_id: value.tokenId,
    session_id: value.sessionId,
    checkpoint_id: value.checkpointId,
    restart_epoch: value.restartEpoch,
    issued_at: value.issuedAt,
    expires_at: value.expiresAt,
    correlation_id: value.correlationId,
    nonce: value.nonce,
    signature: value.signature,
    consumed_at: value.consumedAt,
  };
}

function identityToJson(value: DurableSessionIdentity): JsonObject {
  return {
    session_id: value.sessionId,
    run_id: value.runId,
    task_id: value.taskId,
    worker_request_id: value.workerRequestId,
    tenant_id: value.tenantId,
    parent_session_id: value.parentSessionId,
    branch_id: value.branchId,
  };
}

function messageRole(value: string): DurableMessageRole {
  if (value === "system" || value === "assistant" || value === "tool") return value;
  return "user";
}

function required(value: string, name: string): string {
  const normalized = value.trim();
  if (!normalized) throw new Error(`${name} is required`);
  return normalized;
}

function positive(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : fallback;
}

function normalizeTimestamp(value?: string): string {
  if (!value) return new Date().toISOString();
  const timestamp = Date.parse(value);
  if (!Number.isFinite(timestamp)) throw new Error(`invalid durable timestamp: ${value}`);
  return new Date(timestamp).toISOString();
}

function sign(value: unknown, secret: string): string {
  return `sha256:${createHash("sha256").update(`${secret}:${canonicalJson(value)}`).digest("hex")}`;
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function digest(value: unknown): string {
  return `sha256:${createHash("sha256").update(canonicalJson(value)).digest("hex")}`;
}
