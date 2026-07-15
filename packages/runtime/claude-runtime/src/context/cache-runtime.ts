import {
  type Clock,
  type IdFactory,
  type JsonRecord,
  type JsonValue,
  RandomIdFactory,
  RuntimeInvariantError,
  SystemClock,
  assertNonEmpty,
  assertNonNegativeInteger,
  compareNumbers,
  compareStrings,
  deepClone,
  digestJson,
  uniqueSorted,
} from "../core/runtime-primitives.js";

export interface ContextCacheKey {
  sessionId: string;
  branchId: string;
  modelId: string;
  policyDigest: string;
  systemPromptDigest: string;
  toolSetDigest: string;
  messageBoundaryId: string;
  messageSequence: number;
  compactGeneration: number;
  disclosureClass: string;
}

export interface ContextCacheValue {
  system: JsonValue;
  messages: JsonValue[];
  tools: JsonValue[];
  estimatedInputTokens: number;
  selectedMessageIds: string[];
  omittedMessageIds: string[];
  compactSummaryId: string | null;
  provenanceDigest: string;
  metadata: JsonRecord;
}

export interface ContextCacheEntry {
  cacheId: string;
  key: ContextCacheKey;
  keyDigest: string;
  value: ContextCacheValue;
  valueDigest: string;
  dependencies: string[];
  generation: number;
  createdAt: number;
  expiresAt: number;
  lastAccessedAt: number;
  hits: number;
  revision: number;
}

export interface ContextCacheLease {
  leaseId: string;
  keyDigest: string;
  holderId: string;
  acquiredAt: number;
  expiresAt: number;
  expectedGeneration: number;
}

export interface ContextCacheLookup {
  status: "hit" | "miss" | "stale" | "building";
  keyDigest: string;
  entry: ContextCacheEntry | null;
  activeLease: ContextCacheLease | null;
}

export interface ContextCacheMetrics {
  hits: number;
  misses: number;
  stale: number;
  builds: number;
  evictions: number;
  invalidations: number;
  currentEntries: number;
  currentBytes: number;
}

export interface ContextCacheSnapshot {
  version: "zyra.context-cache/v1";
  generation: number;
  entries: ContextCacheEntry[];
  leases: ContextCacheLease[];
  invalidationGenerations: Array<{ dependency: string; generation: number }>;
  metrics: ContextCacheMetrics;
  checksum: string;
}

export interface ContextCacheOptions {
  clock?: Clock;
  ids?: IdFactory;
  ttlMilliseconds?: number;
  buildLeaseMilliseconds?: number;
  maximumEntries?: number;
  maximumBytes?: number;
}

export class ContextCacheRuntime {
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly ttlMilliseconds: number;
  private readonly buildLeaseMilliseconds: number;
  private readonly maximumEntries: number;
  private readonly maximumBytes: number;
  private readonly entries = new Map<string, ContextCacheEntry>();
  private readonly leases = new Map<string, ContextCacheLease>();
  private readonly dependencyIndex = new Map<string, Set<string>>();
  private readonly invalidationGenerations = new Map<string, number>();
  private generation = 0;
  private metrics: Omit<ContextCacheMetrics, "currentEntries" | "currentBytes"> = {
    hits: 0,
    misses: 0,
    stale: 0,
    builds: 0,
    evictions: 0,
    invalidations: 0,
  };

  constructor(options: ContextCacheOptions = {}) {
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.ttlMilliseconds = options.ttlMilliseconds ?? 300_000;
    this.buildLeaseMilliseconds = options.buildLeaseMilliseconds ?? 30_000;
    this.maximumEntries = options.maximumEntries ?? 1_000;
    this.maximumBytes = options.maximumBytes ?? 256 * 1024 * 1024;
    assertNonNegativeInteger(this.ttlMilliseconds, "ttlMilliseconds");
    assertNonNegativeInteger(
      this.buildLeaseMilliseconds,
      "buildLeaseMilliseconds",
    );
    assertNonNegativeInteger(this.maximumEntries, "maximumEntries");
    assertNonNegativeInteger(this.maximumBytes, "maximumBytes");
  }

  keyDigest(key: ContextCacheKey): string {
    validateKey(key);
    return digestJson(normalizeKey(key));
  }

  lookup(key: ContextCacheKey): ContextCacheLookup {
    this.expireLeases();
    const keyDigest = this.keyDigest(key);
    const entry = this.entries.get(keyDigest);
    const lease = this.leases.get(keyDigest) ?? null;
    if (entry === undefined) {
      this.metrics.misses += 1;
      return {
        status: lease === null ? "miss" : "building",
        keyDigest,
        entry: null,
        activeLease: lease === null ? null : deepClone(lease),
      };
    }
    if (this.isStale(entry)) {
      this.metrics.stale += 1;
      this.removeEntry(entry.keyDigest, false);
      return {
        status: lease === null ? "stale" : "building",
        keyDigest,
        entry: deepClone(entry),
        activeLease: lease === null ? null : deepClone(lease),
      };
    }
    entry.hits += 1;
    entry.lastAccessedAt = this.clock.now();
    entry.revision += 1;
    this.metrics.hits += 1;
    return {
      status: "hit",
      keyDigest,
      entry: deepClone(entry),
      activeLease: lease === null ? null : deepClone(lease),
    };
  }

  acquireBuildLease(
    key: ContextCacheKey,
    holderId: string,
  ): ContextCacheLease {
    this.expireLeases();
    assertNonEmpty(holderId, "holderId");
    const keyDigest = this.keyDigest(key);
    const current = this.entries.get(keyDigest);
    if (current !== undefined && !this.isStale(current)) {
      throw new RuntimeInvariantError("context_cache_entry_already_fresh", {
        keyDigest,
      });
    }
    const existing = this.leases.get(keyDigest);
    if (existing !== undefined) {
      if (existing.holderId === holderId) {
        return deepClone(existing);
      }
      throw new RuntimeInvariantError("context_cache_build_in_progress", {
        keyDigest,
        holderId: existing.holderId,
        expiresAt: existing.expiresAt,
      });
    }
    const acquiredAt = this.clock.now();
    const lease: ContextCacheLease = {
      leaseId: this.ids.next("context-cache-build"),
      keyDigest,
      holderId,
      acquiredAt,
      expiresAt: acquiredAt + this.buildLeaseMilliseconds,
      expectedGeneration: this.generation,
    };
    this.leases.set(keyDigest, lease);
    return deepClone(lease);
  }

  commit(
    leaseId: string,
    key: ContextCacheKey,
    value: ContextCacheValue,
    dependencies: string[],
  ): ContextCacheEntry {
    this.expireLeases();
    const keyDigest = this.keyDigest(key);
    const lease = this.leases.get(keyDigest);
    if (lease === undefined || lease.leaseId !== leaseId) {
      throw new RuntimeInvariantError("context_cache_lease_missing", {
        leaseId,
        keyDigest,
      });
    }
    validateValue(value);
    const normalizedDependencies = uniqueSorted(dependencies);
    for (const dependency of normalizedDependencies) {
      assertNonEmpty(dependency, "dependency");
      const invalidatedAt = this.invalidationGenerations.get(dependency) ?? 0;
      if (invalidatedAt > lease.expectedGeneration) {
        this.leases.delete(keyDigest);
        throw new RuntimeInvariantError("context_cache_build_invalidated", {
          keyDigest,
          dependency,
          expectedGeneration: lease.expectedGeneration,
          invalidatedAt,
        });
      }
    }
    const now = this.clock.now();
    const entry: ContextCacheEntry = {
      cacheId: this.ids.next("context-cache-entry"),
      key: normalizeKey(key),
      keyDigest,
      value: deepClone(value),
      valueDigest: digestJson(value),
      dependencies: normalizedDependencies,
      generation: this.generation,
      createdAt: now,
      expiresAt: now + this.ttlMilliseconds,
      lastAccessedAt: now,
      hits: 0,
      revision: 1,
    };
    const previous = this.entries.get(keyDigest);
    if (previous !== undefined) {
      this.removeEntry(keyDigest, false);
    }
    this.entries.set(keyDigest, entry);
    for (const dependency of normalizedDependencies) {
      const indexed = this.dependencyIndex.get(dependency) ?? new Set<string>();
      indexed.add(keyDigest);
      this.dependencyIndex.set(dependency, indexed);
    }
    this.leases.delete(keyDigest);
    this.generation += 1;
    this.metrics.builds += 1;
    this.evictToLimits();
    return deepClone(entry);
  }

  abort(leaseId: string): void {
    for (const [keyDigest, lease] of this.leases) {
      if (lease.leaseId === leaseId) {
        this.leases.delete(keyDigest);
        return;
      }
    }
  }

  invalidateDependency(dependency: string): string[] {
    assertNonEmpty(dependency, "dependency");
    this.generation += 1;
    this.invalidationGenerations.set(dependency, this.generation);
    const keyDigests = [...(this.dependencyIndex.get(dependency) ?? [])];
    for (const keyDigest of keyDigests) {
      this.removeEntry(keyDigest, false);
      this.leases.delete(keyDigest);
    }
    this.dependencyIndex.delete(dependency);
    this.metrics.invalidations += keyDigests.length;
    return keyDigests.sort(compareStrings);
  }

  invalidateSession(sessionId: string): string[] {
    assertNonEmpty(sessionId, "sessionId");
    return this.invalidateMatching(
      (entry) => entry.key.sessionId === sessionId,
      `session:${sessionId}`,
    );
  }

  invalidateBranch(sessionId: string, branchId: string): string[] {
    assertNonEmpty(sessionId, "sessionId");
    assertNonEmpty(branchId, "branchId");
    return this.invalidateMatching(
      (entry) =>
        entry.key.sessionId === sessionId && entry.key.branchId === branchId,
      `branch:${sessionId}:${branchId}`,
    );
  }

  invalidatePolicy(policyDigest: string): string[] {
    assertNonEmpty(policyDigest, "policyDigest");
    return this.invalidateMatching(
      (entry) => entry.key.policyDigest === policyDigest,
      `policy:${policyDigest}`,
    );
  }

  get(cacheId: string): ContextCacheEntry {
    const entry = [...this.entries.values()].find(
      (candidate) => candidate.cacheId === cacheId,
    );
    if (entry === undefined) {
      throw new RuntimeInvariantError("unknown_context_cache_entry", { cacheId });
    }
    if (this.isStale(entry)) {
      throw new RuntimeInvariantError("context_cache_entry_stale", { cacheId });
    }
    return deepClone(entry);
  }

  currentMetrics(): ContextCacheMetrics {
    return {
      ...this.metrics,
      currentEntries: this.entries.size,
      currentBytes: this.currentBytes(),
    };
  }

  snapshot(): ContextCacheSnapshot {
    this.expireLeases();
    const body = {
      version: "zyra.context-cache/v1" as const,
      generation: this.generation,
      entries: [...this.entries.values()]
        .sort((left, right) => compareStrings(left.keyDigest, right.keyDigest))
        .map((entry) => deepClone(entry)),
      leases: [...this.leases.values()]
        .sort((left, right) => compareStrings(left.keyDigest, right.keyDigest))
        .map((lease) => deepClone(lease)),
      invalidationGenerations: [...this.invalidationGenerations.entries()]
        .sort(([left], [right]) => compareStrings(left, right))
        .map(([dependency, generation]) => ({ dependency, generation })),
      metrics: this.currentMetrics(),
    };
    return { ...body, checksum: digestJson(body) };
  }

  restore(snapshot: ContextCacheSnapshot): void {
    const { checksum, ...body } = snapshot;
    if (snapshot.version !== "zyra.context-cache/v1") {
      throw new RuntimeInvariantError("unsupported_context_cache_snapshot", {
        version: snapshot.version,
      });
    }
    if (digestJson(body) !== checksum) {
      throw new RuntimeInvariantError("context_cache_snapshot_checksum_mismatch");
    }
    this.entries.clear();
    this.leases.clear();
    this.dependencyIndex.clear();
    this.invalidationGenerations.clear();
    for (const entry of snapshot.entries) {
      validateEntry(entry);
      if (entry.keyDigest !== this.keyDigest(entry.key)) {
        throw new RuntimeInvariantError("context_cache_key_digest_mismatch", {
          cacheId: entry.cacheId,
        });
      }
      if (entry.valueDigest !== digestJson(entry.value)) {
        throw new RuntimeInvariantError("context_cache_value_digest_mismatch", {
          cacheId: entry.cacheId,
        });
      }
      this.entries.set(entry.keyDigest, deepClone(entry));
      for (const dependency of entry.dependencies) {
        const indexed = this.dependencyIndex.get(dependency) ?? new Set<string>();
        indexed.add(entry.keyDigest);
        this.dependencyIndex.set(dependency, indexed);
      }
    }
    for (const lease of snapshot.leases) {
      this.leases.set(lease.keyDigest, deepClone(lease));
    }
    for (const item of snapshot.invalidationGenerations) {
      this.invalidationGenerations.set(item.dependency, item.generation);
    }
    this.generation = snapshot.generation;
    this.metrics = {
      hits: snapshot.metrics.hits,
      misses: snapshot.metrics.misses,
      stale: snapshot.metrics.stale,
      builds: snapshot.metrics.builds,
      evictions: snapshot.metrics.evictions,
      invalidations: snapshot.metrics.invalidations,
    };
    this.expireLeases();
    this.evictToLimits();
  }

  private isStale(entry: ContextCacheEntry): boolean {
    if (entry.expiresAt <= this.clock.now()) {
      return true;
    }
    return entry.dependencies.some(
      (dependency) =>
        (this.invalidationGenerations.get(dependency) ?? 0) >
        entry.generation,
    );
  }

  private invalidateMatching(
    predicate: (entry: ContextCacheEntry) => boolean,
    dependency: string,
  ): string[] {
    this.generation += 1;
    this.invalidationGenerations.set(dependency, this.generation);
    const removed = [...this.entries.values()]
      .filter(predicate)
      .map((entry) => entry.keyDigest);
    for (const keyDigest of removed) {
      this.removeEntry(keyDigest, false);
      this.leases.delete(keyDigest);
    }
    this.metrics.invalidations += removed.length;
    return removed.sort(compareStrings);
  }

  private removeEntry(keyDigest: string, eviction: boolean): void {
    const entry = this.entries.get(keyDigest);
    if (entry === undefined) {
      return;
    }
    this.entries.delete(keyDigest);
    for (const dependency of entry.dependencies) {
      const indexed = this.dependencyIndex.get(dependency);
      indexed?.delete(keyDigest);
      if (indexed?.size === 0) {
        this.dependencyIndex.delete(dependency);
      }
    }
    if (eviction) {
      this.metrics.evictions += 1;
    }
  }

  private evictToLimits(): void {
    const entries = [...this.entries.values()].sort(
      (left, right) =>
        compareNumbers(left.lastAccessedAt, right.lastAccessedAt) ||
        compareNumbers(left.hits, right.hits) ||
        compareStrings(left.keyDigest, right.keyDigest),
    );
    while (
      entries.length > 0 &&
      (this.entries.size > this.maximumEntries ||
        this.currentBytes() > this.maximumBytes)
    ) {
      const entry = entries.shift();
      if (entry !== undefined) {
        this.removeEntry(entry.keyDigest, true);
      }
    }
  }

  private currentBytes(): number {
    let bytes = 0;
    for (const entry of this.entries.values()) {
      bytes += Buffer.byteLength(JSON.stringify(entry), "utf8");
    }
    return bytes;
  }

  private expireLeases(): void {
    const now = this.clock.now();
    for (const [keyDigest, lease] of this.leases) {
      if (lease.expiresAt <= now) {
        this.leases.delete(keyDigest);
      }
    }
  }
}

function validateKey(key: ContextCacheKey): void {
  assertNonEmpty(key.sessionId, "sessionId");
  assertNonEmpty(key.branchId, "branchId");
  assertNonEmpty(key.modelId, "modelId");
  assertNonEmpty(key.policyDigest, "policyDigest");
  assertNonEmpty(key.systemPromptDigest, "systemPromptDigest");
  assertNonEmpty(key.toolSetDigest, "toolSetDigest");
  assertNonEmpty(key.messageBoundaryId, "messageBoundaryId");
  assertNonEmpty(key.disclosureClass, "disclosureClass");
  assertNonNegativeInteger(key.messageSequence, "messageSequence");
  assertNonNegativeInteger(key.compactGeneration, "compactGeneration");
}

function normalizeKey(key: ContextCacheKey): ContextCacheKey {
  validateKey(key);
  return {
    ...deepClone(key),
    sessionId: key.sessionId.trim(),
    branchId: key.branchId.trim(),
    modelId: key.modelId.trim().toLowerCase(),
    disclosureClass: key.disclosureClass.trim().toLowerCase(),
  };
}

function validateValue(value: ContextCacheValue): void {
  assertNonNegativeInteger(value.estimatedInputTokens, "estimatedInputTokens");
  assertNonEmpty(value.provenanceDigest, "provenanceDigest");
  const selected = new Set(value.selectedMessageIds);
  for (const omitted of value.omittedMessageIds) {
    if (selected.has(omitted)) {
      throw new RuntimeInvariantError("context_message_selected_and_omitted", {
        messageId: omitted,
      });
    }
  }
}

function validateEntry(entry: ContextCacheEntry): void {
  assertNonEmpty(entry.cacheId, "cacheId");
  assertNonEmpty(entry.keyDigest, "keyDigest");
  assertNonEmpty(entry.valueDigest, "valueDigest");
  assertNonNegativeInteger(entry.revision, "revision");
  validateKey(entry.key);
  validateValue(entry.value);
}
