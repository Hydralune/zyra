import type { JsonObject, JsonValue } from "../contracts.ts";
import {
  canonicalize,
  cloneJson,
  constantTimeDigestEquals,
  deterministicId,
  digest,
  hashChain,
  normalizeIdentifier,
} from "./canonical.ts";
import type { E02RuntimeIdentity } from "./contracts.ts";

export type E02ProjectionDomain =
  | "runtime"
  | "permission"
  | "mcp"
  | "skill"
  | "plugin"
  | "command"
  | "agent"
  | "route"
  | "checkpoint"
  | "execution"
  | "recovery"
  | "event";

export interface E02ProjectionRequest {
  domains?: E02ProjectionDomain[];
  includeDetails?: boolean;
  includeHistory?: boolean;
  includePayloads?: boolean;
  filters?: JsonObject;
  cursor?: string | null;
  limit?: number;
  maximumDepth?: number;
  maximumArrayItems?: number;
  maximumStringLength?: number;
}

export interface E02ProjectionRecord extends JsonObject {
  projectionId: string;
  sequence: number;
  runtimeId: string;
  runtimeEpoch: number;
  domains: E02ProjectionDomain[];
  sourceSnapshotHash: string;
  sourceRevision: number;
  value: JsonObject;
  valueDigest: string;
  redactionCount: number;
  truncationCount: number;
  itemCount: number;
  capturedAt: string;
  priorProjectionHash: string;
  projectionHash: string;
  metadata: JsonObject;
}

export interface E02ProjectionCursor extends JsonObject {
  cursorId: string;
  projectionId: string;
  projectionHash: string;
  runtimeId: string;
  runtimeEpoch: number;
  offset: number;
  limit: number;
  domains: E02ProjectionDomain[];
  issuedAt: string;
  expiresAt: string;
  exhaustedAt: string | null;
  cursorDigest: string;
}

export interface E02ProjectionPage extends JsonObject {
  projectionId: string;
  projectionHash: string;
  cursor: string | null;
  nextCursor: string | null;
  offset: number;
  limit: number;
  totalItems: number;
  items: JsonValue[];
  exhausted: boolean;
  pageDigest: string;
}

export interface E02ProjectionDiff extends JsonObject {
  diffId: string;
  fromProjectionId: string;
  toProjectionId: string;
  fromHash: string;
  toHash: string;
  addedPaths: string[];
  removedPaths: string[];
  changedPaths: string[];
  unchangedPathCount: number;
  entries: JsonObject[];
  computedAt: string;
  diffDigest: string;
}

export interface E02ProjectionSubscription extends JsonObject {
  subscriptionId: string;
  consumerId: string;
  domains: E02ProjectionDomain[];
  lastProjectionId: string | null;
  lastProjectionHash: string | null;
  lastAcknowledgedSequence: number;
  createdAt: string;
  updatedAt: string;
  closedAt: string | null;
  metadata: JsonObject;
  subscriptionDigest: string;
}

export interface E02ProjectionSnapshot {
  version: "zyra.e02-projection/v1";
  runtime: E02RuntimeIdentity;
  sequence: number;
  previousProjectionHash: string;
  records: E02ProjectionRecord[];
  cursors: E02ProjectionCursor[];
  subscriptions: E02ProjectionSubscription[];
  snapshotHash: string;
}

const ALL_DOMAINS: readonly E02ProjectionDomain[] = Object.freeze([
  "runtime",
  "permission",
  "mcp",
  "skill",
  "plugin",
  "command",
  "agent",
  "route",
  "checkpoint",
  "execution",
  "recovery",
  "event",
]);

const SECRET_KEYS = Object.freeze([
  "access_token",
  "refresh_token",
  "authorization",
  "cookie",
  "password",
  "secret",
  "token",
  "api_key",
  "apikey",
  "private_key",
  "credential",
]);

const PAYLOAD_KEYS = Object.freeze([
  "arguments",
  "content",
  "body",
  "payload",
  "result",
  "output",
  "context",
  "resource_contents",
]);

export class E02ProjectionRuntime {
  readonly runtime: E02RuntimeIdentity;
  private readonly now: () => Date;
  private sequence = 0;
  private previousProjectionHash = digest({ genesis: "zyra.e02-projection/v1" });
  private readonly records = new Map<string, E02ProjectionRecord>();
  private readonly cursors = new Map<string, E02ProjectionCursor>();
  private readonly subscriptions = new Map<string, E02ProjectionSubscription>();

  constructor(options: {
    runtime: E02RuntimeIdentity;
    now?: () => Date;
    snapshot?: E02ProjectionSnapshot | null;
  }) {
    this.runtime = cloneJson(options.runtime);
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  capture(
    sourceValue: JsonObject,
    requestValue: E02ProjectionRequest = {},
    metadataValue: JsonObject = {},
  ): E02ProjectionRecord {
    const request = this.normalizeRequest(requestValue);
    const source = canonicalize(sourceValue) as JsonObject;
    const sourceSnapshotHash = stringValue(source.snapshotHash)
      || stringValue(source.snapshot_hash)
      || digest(source);
    const sourceRevision = safeInteger(source.snapshotSequence)
      ?? safeInteger(source.snapshot_sequence)
      ?? this.sequence;
    const selected = this.selectDomains(source, request.domains);
    const statistics = { redactions: 0, truncations: 0 };
    const value = this.projectValue(selected, request, statistics, "$", 0) as JsonObject;
    const filtered = this.applyFilters(value, request.filters);
    const itemCount = flattenProjection(filtered).length;
    const capturedAt = this.timestamp();
    const sequence = this.sequence + 1;
    const valueDigest = digest(filtered);
    const base = {
      sequence,
      runtimeId: this.runtime.runtimeId,
      runtimeEpoch: this.runtime.epoch,
      domains: request.domains,
      sourceSnapshotHash,
      sourceRevision,
      value: filtered,
      valueDigest,
      redactionCount: statistics.redactions,
      truncationCount: statistics.truncations,
      itemCount,
      capturedAt,
      priorProjectionHash: this.previousProjectionHash,
      metadata: canonicalize(metadataValue) as JsonObject,
    };
    const projectionId = deterministicId("e02-projection", {
      runtime_id: this.runtime.runtimeId,
      runtime_epoch: this.runtime.epoch,
      source_snapshot_hash: sourceSnapshotHash,
      domains: request.domains,
      value_digest: valueDigest,
      request: canonicalize(request as unknown as JsonObject),
    }, 48);
    const prior = this.records.get(projectionId);
    if (prior) {
      if (!constantTimeDigestEquals(prior.valueDigest, valueDigest)) {
        throw projectionError("e02_projection_identity_collision", `projection ${projectionId} collided`);
      }
      return cloneJson(prior);
    }
    const projectionHash = hashChain(this.previousProjectionHash, { projectionId, ...base });
    const record: E02ProjectionRecord = { projectionId, ...base, projectionHash };
    this.sequence = sequence;
    this.previousProjectionHash = projectionHash;
    this.records.set(projectionId, record);
    return cloneJson(record);
  }

  record(projectionId: string): E02ProjectionRecord | null {
    const value = this.records.get(projectionId);
    return value ? cloneJson(value) : null;
  }

  latest(domainsValue?: E02ProjectionDomain[]): E02ProjectionRecord | null {
    const domains = domainsValue ? this.normalizeDomains(domainsValue) : null;
    const values = [...this.records.values()]
      .filter((record) => !domains || domains.every((domain) => record.domains.includes(domain)))
      .sort((left, right) => right.sequence - left.sequence);
    return values[0] ? cloneJson(values[0]) : null;
  }

  list(input: {
    domains?: E02ProjectionDomain[];
    sourceSnapshotHash?: string;
    minimumSequence?: number;
    maximumSequence?: number;
    limit?: number;
  } = {}): E02ProjectionRecord[] {
    const domains = input.domains ? this.normalizeDomains(input.domains) : null;
    const limit = Math.max(1, Math.min(10_000, Math.trunc(input.limit ?? 100)));
    return [...this.records.values()]
      .filter((record) => !domains || domains.every((domain) => record.domains.includes(domain)))
      .filter((record) => !input.sourceSnapshotHash || record.sourceSnapshotHash === input.sourceSnapshotHash)
      .filter((record) => input.minimumSequence === undefined || record.sequence >= input.minimumSequence)
      .filter((record) => input.maximumSequence === undefined || record.sequence <= input.maximumSequence)
      .sort((left, right) => right.sequence - left.sequence)
      .slice(0, limit)
      .map(cloneJson);
  }

  openCursor(projectionId: string, limitValue = 100, ttlMsValue = 300_000): E02ProjectionCursor {
    const record = this.requireRecord(projectionId);
    const limit = Math.max(1, Math.min(1_000, Math.trunc(limitValue)));
    const ttlMs = Math.max(1_000, Math.min(3_600_000, Math.trunc(ttlMsValue)));
    const issuedAt = this.timestamp();
    const expiresAt = new Date(Date.parse(issuedAt) + ttlMs).toISOString();
    const base = {
      projectionId: record.projectionId,
      projectionHash: record.projectionHash,
      runtimeId: this.runtime.runtimeId,
      runtimeEpoch: this.runtime.epoch,
      offset: 0,
      limit,
      domains: [...record.domains],
      issuedAt,
      expiresAt,
      exhaustedAt: null,
    };
    const cursorId = deterministicId("e02-projection-cursor", base, 48);
    const cursor: E02ProjectionCursor = {
      cursorId,
      ...base,
      cursorDigest: digest({ cursorId, ...base }),
    };
    const prior = this.cursors.get(cursorId);
    if (prior && !constantTimeDigestEquals(prior.cursorDigest, cursor.cursorDigest)) {
      throw projectionError("e02_projection_cursor_collision", `projection cursor ${cursorId} collided`);
    }
    this.cursors.set(cursorId, cursor);
    return cloneJson(cursor);
  }

  page(cursorId: string): E02ProjectionPage {
    const cursor = this.requireCursor(cursorId);
    if (cursor.runtimeEpoch !== this.runtime.epoch || cursor.runtimeId !== this.runtime.runtimeId) {
      throw projectionError("e02_projection_cursor_binding", `projection cursor ${cursorId} binding mismatch`);
    }
    if (cursor.exhaustedAt) {
      throw projectionError("e02_projection_cursor_exhausted", `projection cursor ${cursorId} is exhausted`);
    }
    if (Date.parse(cursor.expiresAt) <= this.now().getTime()) {
      throw projectionError("e02_projection_cursor_expired", `projection cursor ${cursorId} expired`);
    }
    const record = this.requireRecord(cursor.projectionId);
    if (!constantTimeDigestEquals(record.projectionHash, cursor.projectionHash)) {
      throw projectionError("e02_projection_cursor_stale", `projection ${record.projectionId} changed after cursor issue`);
    }
    const flattened = flattenProjection(record.value);
    const offset = cursor.offset;
    const items = flattened.slice(offset, offset + cursor.limit);
    cursor.offset += items.length;
    const exhausted = cursor.offset >= flattened.length;
    if (exhausted) cursor.exhaustedAt = this.timestamp();
    cursor.cursorDigest = projectionCursorDigest(cursor);
    const base = {
      projectionId: record.projectionId,
      projectionHash: record.projectionHash,
      cursor: cursorId,
      nextCursor: exhausted ? null : cursorId,
      offset,
      limit: cursor.limit,
      totalItems: flattened.length,
      items,
      exhausted,
    };
    return { ...base, pageDigest: digest(base) };
  }

  pageRecord(projectionId: string, limitValue = 100): E02ProjectionPage {
    const cursor = this.openCursor(projectionId, limitValue);
    return this.page(cursor.cursorId);
  }

  diff(fromProjectionId: string, toProjectionId: string, maximumEntriesValue = 1_000): E02ProjectionDiff {
    const from = this.requireRecord(fromProjectionId);
    const to = this.requireRecord(toProjectionId);
    if (from.runtimeId !== to.runtimeId) {
      throw projectionError("e02_projection_diff_binding", "cannot diff projections from different runtimes");
    }
    const maximumEntries = Math.max(1, Math.min(10_000, Math.trunc(maximumEntriesValue)));
    const fromPaths = new Map(flattenProjection(from.value).map((entry) => [stringValue((entry as JsonObject).path), (entry as JsonObject).value]));
    const toPaths = new Map(flattenProjection(to.value).map((entry) => [stringValue((entry as JsonObject).path), (entry as JsonObject).value]));
    const addedPaths: string[] = [];
    const removedPaths: string[] = [];
    const changedPaths: string[] = [];
    const entries: JsonObject[] = [];
    let unchangedPathCount = 0;
    const allPaths = [...new Set([...fromPaths.keys(), ...toPaths.keys()])].sort();
    for (const path of allPaths) {
      const hasFrom = fromPaths.has(path);
      const hasTo = toPaths.has(path);
      if (!hasFrom && hasTo) {
        addedPaths.push(path);
        if (entries.length < maximumEntries) entries.push({ path, kind: "added", after: canonicalize(toPaths.get(path)) });
      } else if (hasFrom && !hasTo) {
        removedPaths.push(path);
        if (entries.length < maximumEntries) entries.push({ path, kind: "removed", before: canonicalize(fromPaths.get(path)) });
      } else if (!constantTimeDigestEquals(digest(fromPaths.get(path)), digest(toPaths.get(path)))) {
        changedPaths.push(path);
        if (entries.length < maximumEntries) {
          entries.push({ path, kind: "changed", before: canonicalize(fromPaths.get(path)), after: canonicalize(toPaths.get(path)) });
        }
      } else {
        unchangedPathCount += 1;
      }
    }
    const computedAt = this.timestamp();
    const base = {
      fromProjectionId: from.projectionId,
      toProjectionId: to.projectionId,
      fromHash: from.projectionHash,
      toHash: to.projectionHash,
      addedPaths,
      removedPaths,
      changedPaths,
      unchangedPathCount,
      entries,
      computedAt,
    };
    const diffId = deterministicId("e02-projection-diff", base, 48);
    return { diffId, ...base, diffDigest: digest({ diffId, ...base }) };
  }

  subscribe(consumerIdValue: string, domainsValue: E02ProjectionDomain[], metadataValue: JsonObject = {}): E02ProjectionSubscription {
    const consumerId = normalizeIdentifier(consumerIdValue);
    if (!consumerId) throw projectionError("e02_projection_consumer_missing", "projection subscription requires consumer id");
    const domains = this.normalizeDomains(domainsValue);
    const createdAt = this.timestamp();
    const base = {
      consumerId,
      domains,
      lastProjectionId: null,
      lastProjectionHash: null,
      lastAcknowledgedSequence: 0,
      createdAt,
      updatedAt: createdAt,
      closedAt: null,
      metadata: canonicalize(metadataValue) as JsonObject,
    };
    const subscriptionId = deterministicId("e02-projection-subscription", {
      runtime_id: this.runtime.runtimeId,
      consumer_id: consumerId,
      domains,
    }, 48);
    const prior = this.subscriptions.get(subscriptionId);
    if (prior) {
      if (prior.closedAt) throw projectionError("e02_projection_subscription_closed", `subscription ${subscriptionId} is closed`);
      return cloneJson(prior);
    }
    const subscription: E02ProjectionSubscription = {
      subscriptionId,
      ...base,
      subscriptionDigest: digest({ subscriptionId, ...base }),
    };
    this.subscriptions.set(subscriptionId, subscription);
    return cloneJson(subscription);
  }

  pendingForSubscription(subscriptionId: string, limitValue = 100): E02ProjectionRecord[] {
    const subscription = this.requireSubscription(subscriptionId);
    if (subscription.closedAt) {
      throw projectionError("e02_projection_subscription_closed", `subscription ${subscriptionId} is closed`);
    }
    const limit = Math.max(1, Math.min(1_000, Math.trunc(limitValue)));
    return [...this.records.values()]
      .filter((record) => record.sequence > subscription.lastAcknowledgedSequence)
      .filter((record) => subscription.domains.some((domain) => record.domains.includes(domain)))
      .sort((left, right) => left.sequence - right.sequence)
      .slice(0, limit)
      .map(cloneJson);
  }

  acknowledge(subscriptionId: string, projectionId: string): E02ProjectionSubscription {
    const subscription = this.requireSubscription(subscriptionId);
    if (subscription.closedAt) {
      throw projectionError("e02_projection_subscription_closed", `subscription ${subscriptionId} is closed`);
    }
    const projection = this.requireRecord(projectionId);
    if (!subscription.domains.some((domain) => projection.domains.includes(domain))) {
      throw projectionError("e02_projection_ack_domain_mismatch", `projection ${projectionId} is outside subscription domains`);
    }
    if (projection.sequence < subscription.lastAcknowledgedSequence) {
      throw projectionError("e02_projection_ack_regression", `projection acknowledgement would regress subscription ${subscriptionId}`);
    }
    subscription.lastProjectionId = projection.projectionId;
    subscription.lastProjectionHash = projection.projectionHash;
    subscription.lastAcknowledgedSequence = projection.sequence;
    subscription.updatedAt = this.timestamp();
    subscription.subscriptionDigest = projectionSubscriptionDigest(subscription);
    return cloneJson(subscription);
  }

  closeSubscription(subscriptionId: string, reason: string): E02ProjectionSubscription {
    const subscription = this.requireSubscription(subscriptionId);
    if (!reason.trim()) throw projectionError("e02_projection_close_reason_missing", "closing a subscription requires reason");
    if (!subscription.closedAt) {
      subscription.closedAt = this.timestamp();
      subscription.updatedAt = subscription.closedAt;
      subscription.metadata = { ...subscription.metadata, close_reason: reason };
      subscription.subscriptionDigest = projectionSubscriptionDigest(subscription);
    }
    return cloneJson(subscription);
  }

  verifyRecord(projectionId: string): JsonObject {
    const record = this.requireRecord(projectionId);
    const errors: JsonObject[] = [];
    if (!constantTimeDigestEquals(digest(record.value), record.valueDigest)) {
      errors.push({ code: "value_digest_mismatch" });
    }
    if (record.runtimeId !== this.runtime.runtimeId || record.runtimeEpoch !== this.runtime.epoch) {
      errors.push({ code: "runtime_binding_mismatch" });
    }
    if (record.itemCount !== flattenProjection(record.value).length) {
      errors.push({ code: "item_count_mismatch" });
    }
    if (record.domains.some((domain) => !ALL_DOMAINS.includes(domain))) {
      errors.push({ code: "unknown_domain" });
    }
    return {
      ok: errors.length === 0,
      projection_id: projectionId,
      sequence: record.sequence,
      errors,
    };
  }

  verifyChain(): JsonObject {
    const records = [...this.records.values()].sort((left, right) => left.sequence - right.sequence);
    const errors: JsonObject[] = [];
    let previous = digest({ genesis: "zyra.e02-projection/v1" });
    let sequence = 0;
    for (const record of records) {
      if (record.sequence !== sequence + 1) {
        errors.push({ code: "sequence_gap", projection_id: record.projectionId, expected: sequence + 1, actual: record.sequence });
      }
      if (!constantTimeDigestEquals(record.priorProjectionHash, previous)) {
        errors.push({ code: "prior_hash_mismatch", projection_id: record.projectionId });
      }
      const verification = this.verifyRecord(record.projectionId);
      if (verification.ok !== true) errors.push({ code: "record_invalid", projection_id: record.projectionId });
      previous = record.projectionHash;
      sequence = record.sequence;
    }
    if (sequence !== this.sequence) errors.push({ code: "head_sequence_mismatch", expected: sequence, actual: this.sequence });
    if (!constantTimeDigestEquals(previous, this.previousProjectionHash)) errors.push({ code: "head_hash_mismatch" });
    return {
      ok: errors.length === 0,
      sequence: this.sequence,
      record_count: records.length,
      head_hash: this.previousProjectionHash,
      errors,
    };
  }

  compact(retainCountValue = 256): JsonObject {
    const retainCount = Math.max(16, Math.min(10_000, Math.trunc(retainCountValue)));
    const records = [...this.records.values()].sort((left, right) => right.sequence - left.sequence);
    const protectedIds = new Set([...this.subscriptions.values()]
      .filter((value) => !value.closedAt && value.lastProjectionId)
      .map((value) => value.lastProjectionId!));
    const removable = records.slice(retainCount).filter((value) => !protectedIds.has(value.projectionId));
    let cursorCount = 0;
    for (const record of removable) {
      this.records.delete(record.projectionId);
      for (const [cursorId, cursor] of this.cursors) {
        if (cursor.projectionId === record.projectionId) {
          this.cursors.delete(cursorId);
          cursorCount += 1;
        }
      }
    }
    return {
      removed_projection_count: removable.length,
      removed_cursor_count: cursorCount,
      retained_projection_count: this.records.size,
      protected_projection_count: protectedIds.size,
      chain_head_preserved: this.previousProjectionHash,
    };
  }

  health(): JsonObject {
    const activeSubscriptions = [...this.subscriptions.values()].filter((value) => !value.closedAt);
    const expiredCursors = [...this.cursors.values()].filter((value) => Date.parse(value.expiresAt) <= this.now().getTime());
    return {
      canonical_owner: "typescript",
      runtime_epoch: this.runtime.epoch,
      sequence: this.sequence,
      projection_count: this.records.size,
      cursor_count: this.cursors.size,
      expired_cursor_count: expiredCursors.length,
      subscription_count: this.subscriptions.size,
      active_subscription_count: activeSubscriptions.length,
      previous_projection_hash: this.previousProjectionHash,
      chain_ok: this.verifyChain().ok,
      redaction_keys: [...SECRET_KEYS],
      python_projection_fallback: false,
    };
  }

  snapshot(): E02ProjectionSnapshot {
    const withoutHash = {
      version: "zyra.e02-projection/v1" as const,
      runtime: cloneJson(this.runtime),
      sequence: this.sequence,
      previousProjectionHash: this.previousProjectionHash,
      records: [...this.records.values()].sort((left, right) => left.sequence - right.sequence).map(cloneJson),
      cursors: [...this.cursors.values()].sort((left, right) => left.issuedAt.localeCompare(right.issuedAt)).map(cloneJson),
      subscriptions: [...this.subscriptions.values()].sort((left, right) => left.createdAt.localeCompare(right.createdAt)).map(cloneJson),
    };
    return { ...withoutHash, snapshotHash: digest(withoutHash) };
  }

  private normalizeRequest(value: E02ProjectionRequest): Required<E02ProjectionRequest> {
    return {
      domains: this.normalizeDomains(value.domains ?? [...ALL_DOMAINS]),
      includeDetails: value.includeDetails === true,
      includeHistory: value.includeHistory === true,
      includePayloads: value.includePayloads === true,
      filters: canonicalize(value.filters ?? {}) as JsonObject,
      cursor: value.cursor ?? null,
      limit: Math.max(1, Math.min(10_000, Math.trunc(value.limit ?? 1_000))),
      maximumDepth: Math.max(2, Math.min(32, Math.trunc(value.maximumDepth ?? 10))),
      maximumArrayItems: Math.max(1, Math.min(10_000, Math.trunc(value.maximumArrayItems ?? 500))),
      maximumStringLength: Math.max(32, Math.min(1_000_000, Math.trunc(value.maximumStringLength ?? 8_192))),
    };
  }

  private normalizeDomains(values: E02ProjectionDomain[]): E02ProjectionDomain[] {
    const output: E02ProjectionDomain[] = [];
    for (const value of values) {
      if (!ALL_DOMAINS.includes(value)) {
        throw projectionError("e02_projection_domain_unknown", `unknown projection domain ${value}`);
      }
      if (!output.includes(value)) output.push(value);
    }
    output.sort();
    if (output.length === 0) throw projectionError("e02_projection_domains_empty", "projection requires at least one domain");
    return output;
  }

  private selectDomains(source: JsonObject, domains: E02ProjectionDomain[]): JsonObject {
    const output: JsonObject = {};
    for (const domain of domains) {
      const keys = projectionSourceKeys(domain);
      let selected: JsonValue | undefined;
      for (const key of keys) {
        if (key in source) {
          selected = source[key];
          break;
        }
      }
      if (selected !== undefined) output[domain] = canonicalize(selected);
      else output[domain] = { available: false, reason: "domain_not_present_in_source" };
    }
    return output;
  }

  private projectValue(
    value: JsonValue,
    request: Required<E02ProjectionRequest>,
    statistics: { redactions: number; truncations: number },
    path: string,
    depth: number,
  ): JsonValue {
    if (depth > request.maximumDepth) {
      statistics.truncations += 1;
      return { truncated: true, reason: "maximum_depth", path, digest: digest(value) };
    }
    if (value === null || typeof value === "number" || typeof value === "boolean") return value;
    if (typeof value === "string") {
      if (value.length <= request.maximumStringLength) return value;
      statistics.truncations += 1;
      return `${value.slice(0, request.maximumStringLength)}…[sha256:${digest(value)}]`;
    }
    if (Array.isArray(value)) {
      const selected = value.slice(0, request.maximumArrayItems);
      if (selected.length < value.length) statistics.truncations += value.length - selected.length;
      const projected = selected.map((item, index) => this.projectValue(item, request, statistics, `${path}[${index}]`, depth + 1));
      if (selected.length < value.length) {
        projected.push({
          truncated: true,
          reason: "maximum_array_items",
          omitted_count: value.length - selected.length,
          original_digest: digest(value),
        });
      }
      return projected;
    }
    const output: JsonObject = {};
    for (const key of Object.keys(value).sort()) {
      const item = value[key];
      const normalizedKey = normalizeIdentifier(key).replaceAll("-", "_");
      if (SECRET_KEYS.some((secret) => normalizedKey === secret || normalizedKey.endsWith(`_${secret}`))) {
        statistics.redactions += 1;
        output[key] = { redacted: true, digest: digest(item), reason: "secret_key" };
        continue;
      }
      if (!request.includePayloads && PAYLOAD_KEYS.some((payloadKey) => normalizedKey === payloadKey || normalizedKey.endsWith(`_${payloadKey}`))) {
        statistics.redactions += 1;
        output[key] = { redacted: true, digest: digest(item), reason: "payload_excluded" };
        continue;
      }
      if (!request.includeHistory && isHistoryKey(normalizedKey)) {
        statistics.redactions += 1;
        output[key] = { omitted: true, digest: digest(item), reason: "history_excluded" };
        continue;
      }
      if (!request.includeDetails && (normalizedKey === "details" || normalizedKey.endsWith("_details"))) {
        statistics.redactions += 1;
        output[key] = { omitted: true, digest: digest(item), reason: "details_excluded" };
        continue;
      }
      output[key] = this.projectValue(item, request, statistics, `${path}.${key}`, depth + 1);
    }
    return output;
  }

  private applyFilters(value: JsonObject, filters: JsonObject): JsonObject {
    if (Object.keys(filters).length === 0) return value;
    const includePaths = stringArray(filters.include_paths);
    const excludePaths = stringArray(filters.exclude_paths);
    const requiredValues = objectValue(filters.equals);
    const flattened = flattenProjection(value);
    const selected = flattened.filter((entryValue) => {
      const entry = entryValue as JsonObject;
      const path = stringValue(entry.path);
      if (includePaths.length > 0 && !includePaths.some((prefix) => path.startsWith(prefix))) return false;
      if (excludePaths.some((prefix) => path.startsWith(prefix))) return false;
      const expected = requiredValues[path];
      if (expected !== undefined && !constantTimeDigestEquals(digest(entry.value), digest(expected))) return false;
      return true;
    });
    return {
      filtered: true,
      include_paths: includePaths,
      exclude_paths: excludePaths,
      source_item_count: flattened.length,
      selected_item_count: selected.length,
      items: selected,
    };
  }

  private restore(snapshotValue: E02ProjectionSnapshot): void {
    const snapshot = cloneJson(snapshotValue);
    if (snapshot.version !== "zyra.e02-projection/v1") {
      throw projectionError("e02_projection_snapshot_version", `unsupported projection snapshot ${snapshot.version}`);
    }
    const { snapshotHash, ...withoutHash } = snapshot;
    if (!constantTimeDigestEquals(digest(withoutHash), snapshotHash)) {
      throw projectionError("e02_projection_snapshot_digest", "projection snapshot digest mismatch");
    }
    this.assertRuntimeBinding(snapshot.runtime);
    if (this.runtime.epoch <= snapshot.runtime.epoch) {
      throw projectionError("e02_projection_snapshot_epoch", "projection restore target epoch must advance");
    }
    for (const recordValue of snapshot.records) {
      const record = cloneJson(recordValue);
      if (this.records.has(record.projectionId) || !constantTimeDigestEquals(digest(record.value), record.valueDigest)) {
        throw projectionError("e02_projection_snapshot_record", `invalid projection ${record.projectionId}`);
      }
      record.runtimeEpoch = this.runtime.epoch;
      this.records.set(record.projectionId, record);
    }
    for (const cursorValue of snapshot.cursors) {
      const cursor = cloneJson(cursorValue);
      if (this.cursors.has(cursor.cursorId) || !constantTimeDigestEquals(projectionCursorDigest(cursor), cursor.cursorDigest)) {
        throw projectionError("e02_projection_snapshot_cursor", `invalid projection cursor ${cursor.cursorId}`);
      }
      cursor.exhaustedAt = cursor.exhaustedAt ?? this.timestamp();
      cursor.cursorDigest = projectionCursorDigest(cursor);
      this.cursors.set(cursor.cursorId, cursor);
    }
    for (const subscriptionValue of snapshot.subscriptions) {
      const subscription = cloneJson(subscriptionValue);
      if (this.subscriptions.has(subscription.subscriptionId)
        || !constantTimeDigestEquals(projectionSubscriptionDigest(subscription), subscription.subscriptionDigest)) {
        throw projectionError("e02_projection_snapshot_subscription", `invalid subscription ${subscription.subscriptionId}`);
      }
      this.subscriptions.set(subscription.subscriptionId, subscription);
    }
    this.sequence = snapshot.sequence;
    this.previousProjectionHash = snapshot.previousProjectionHash;
    const chain = this.verifyChain();
    if (chain.ok !== true) throw projectionError("e02_projection_snapshot_chain", "projection chain is invalid", chain);
  }

  private assertRuntimeBinding(runtime: E02RuntimeIdentity): void {
    if (
      runtime.runtimeId !== this.runtime.runtimeId
      || runtime.runId !== this.runtime.runId
      || runtime.taskId !== this.runtime.taskId
      || runtime.sessionId !== this.runtime.sessionId
      || runtime.workerRequestId !== this.runtime.workerRequestId
    ) {
      throw projectionError("e02_projection_snapshot_binding", "projection snapshot belongs to another runtime binding");
    }
  }

  private requireRecord(projectionId: string): E02ProjectionRecord {
    const value = this.records.get(projectionId);
    if (!value) throw projectionError("e02_projection_not_found", `projection ${projectionId} was not found`);
    return value;
  }

  private requireCursor(cursorId: string): E02ProjectionCursor {
    const value = this.cursors.get(cursorId);
    if (!value) throw projectionError("e02_projection_cursor_not_found", `cursor ${cursorId} was not found`);
    return value;
  }

  private requireSubscription(subscriptionId: string): E02ProjectionSubscription {
    const value = this.subscriptions.get(subscriptionId);
    if (!value) throw projectionError("e02_projection_subscription_not_found", `subscription ${subscriptionId} was not found`);
    return value;
  }

  private timestamp(): string {
    return this.now().toISOString();
  }
}

function projectionSourceKeys(domain: E02ProjectionDomain): string[] {
  if (domain === "runtime") return ["runtime", "health"];
  if (domain === "permission") return ["permission"];
  if (domain === "mcp") return ["mcp"];
  if (domain === "skill") return ["skills", "skill"];
  if (domain === "plugin") return ["plugins", "plugin"];
  if (domain === "command") return ["commands", "command", "controlPlane"];
  if (domain === "agent") return ["agents", "agent"];
  if (domain === "route") return ["routes", "route"];
  if (domain === "checkpoint") return ["checkpointBundles", "checkpoint"];
  if (domain === "execution") return ["executionLedger", "execution"];
  if (domain === "recovery") return ["recoveries", "recovery"];
  return ["events", "event"];
}

function isHistoryKey(key: string): boolean {
  return key === "history"
    || key.endsWith("_history")
    || key === "records"
    || key.endsWith("_records")
    || key === "events"
    || key === "captures"
    || key === "revisions";
}

function flattenProjection(value: JsonValue, path = "$", output: JsonValue[] = []): JsonValue[] {
  if (Array.isArray(value)) {
    for (const [index, item] of value.entries()) flattenProjection(item, `${path}[${index}]`, output);
    if (value.length === 0) output.push({ path, value: [] });
    return output;
  }
  if (value !== null && typeof value === "object") {
    const keys = Object.keys(value);
    for (const key of keys) flattenProjection(value[key]!, `${path}.${key}`, output);
    if (keys.length === 0) output.push({ path, value: {} });
    return output;
  }
  output.push({ path, value: canonicalize(value) });
  return output;
}

function projectionCursorDigest(cursor: E02ProjectionCursor): string {
  const { cursorDigest: _ignored, ...base } = cursor;
  return digest(base);
}

function projectionSubscriptionDigest(value: E02ProjectionSubscription): string {
  const { subscriptionDigest: _ignored, ...base } = value;
  return digest(base);
}

function stringArray(value: JsonValue | undefined): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.filter((item): item is string => typeof item === "string" && Boolean(item.trim())).map((item) => item.trim()))].sort();
}

function objectValue(value: JsonValue | undefined): JsonObject {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? cloneJson(value) : {};
}

function stringValue(value: JsonValue | undefined): string {
  return typeof value === "string" ? value : "";
}

function safeInteger(value: JsonValue | undefined): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function projectionError(code: string, message: string, details: JsonObject = {}): Error {
  return Object.assign(new Error(message), {
    name: "E02ProjectionError",
    code,
    details: cloneJson(details),
  });
}
