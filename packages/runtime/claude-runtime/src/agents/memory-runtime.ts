import {
  assertDigest,
  createId,
  digest,
  E03RuntimeError,
  type E03Clock,
  type E03ContextSnapshot,
  type E03TaskState,
  SystemE03Clock,
} from "../e03/contracts.ts";

export interface AgentMemoryRecord {
  memoryId: string;
  taskId: string;
  sessionId: string;
  kind: "instruction" | "observation" | "decision" | "result" | "handoff";
  content: string;
  tokenEstimate: number;
  importance: number;
  sourceMessageIds: string[];
  sourceArtifactIds: string[];
  createdAt: string;
  expiresAt: string | null;
  digest: string;
}

export interface AgentMemorySnapshot {
  version: "zyra.e03-agent-memory/v1";
  taskId: string;
  sessionId: string;
  revision: number;
  records: AgentMemoryRecord[];
  totalTokenEstimate: number;
  checksum: string;
}

export class AgentMemoryRuntime {
  private snapshotValue: AgentMemorySnapshot;

  constructor(
    taskId: string,
    sessionId: string,
    private readonly clock: E03Clock = new SystemE03Clock(),
  ) {
    this.snapshotValue = seal({
      version: "zyra.e03-agent-memory/v1",
      taskId,
      sessionId,
      revision: 0,
      records: [],
      totalTokenEstimate: 0,
    });
  }

  capture(input: {
    kind: AgentMemoryRecord["kind"];
    content: string;
    importance?: number;
    sourceMessageIds?: readonly string[];
    sourceArtifactIds?: readonly string[];
    expiresAt?: string | null;
  }): AgentMemoryRecord {
    const content = input.content.trim();
    if (!content)
      throw new E03RuntimeError("empty_memory", "memory content is empty");
    if (content.length > 1_000_000)
      throw new E03RuntimeError(
        "memory_too_large",
        "memory content exceeds one million characters",
      );
    const importance = input.importance ?? 0.5;
    if (!Number.isFinite(importance) || importance < 0 || importance > 1)
      throw new E03RuntimeError(
        "invalid_memory_importance",
        "memory importance must be between zero and one",
      );
    const payload = {
      memoryId: createId("agent-memory"),
      taskId: this.snapshotValue.taskId,
      sessionId: this.snapshotValue.sessionId,
      kind: input.kind,
      content,
      tokenEstimate: Math.max(1, Math.ceil(content.length / 4)),
      importance,
      sourceMessageIds: [...new Set(input.sourceMessageIds ?? [])],
      sourceArtifactIds: [...new Set(input.sourceArtifactIds ?? [])],
      createdAt: this.clock.now(),
      expiresAt: input.expiresAt ?? null,
    };
    const record = { ...payload, digest: digest(payload) };
    const records = [...this.snapshotValue.records, record];
    this.snapshotValue = seal({
      ...this.snapshotValue,
      revision: this.snapshotValue.revision + 1,
      records,
      totalTokenEstimate: records.reduce(
        (sum, item) => sum + item.tokenEstimate,
        0,
      ),
      checksum: undefined,
    });
    return structuredClone(record);
  }

  restore(
    snapshot: AgentMemorySnapshot,
    expected: { taskId: string; sessionId: string },
  ): void {
    validate(snapshot);
    if (
      snapshot.taskId !== expected.taskId ||
      snapshot.sessionId !== expected.sessionId
    )
      throw new E03RuntimeError(
        "memory_identity_mismatch",
        "memory snapshot belongs to another task/session",
      );
    this.snapshotValue = structuredClone(snapshot);
  }

  compact(maximumTokens: number): {
    removed: AgentMemoryRecord[];
    retained: AgentMemoryRecord[];
  } {
    if (!Number.isSafeInteger(maximumTokens) || maximumTokens < 0)
      throw new E03RuntimeError(
        "invalid_memory_budget",
        "memory token budget must be non-negative",
      );
    const now = Date.parse(this.clock.now());
    const candidates = this.snapshotValue.records
      .filter(
        (record) =>
          record.expiresAt === null || Date.parse(record.expiresAt) > now,
      )
      .sort(
        (left, right) =>
          right.importance - left.importance ||
          right.createdAt.localeCompare(left.createdAt),
      );
    const retained: AgentMemoryRecord[] = [];
    const removed: AgentMemoryRecord[] = [];
    let consumed = 0;
    for (const record of candidates) {
      if (consumed + record.tokenEstimate <= maximumTokens) {
        retained.push(record);
        consumed += record.tokenEstimate;
      } else removed.push(record);
    }
    const expired = this.snapshotValue.records.filter(
      (record) => !candidates.includes(record),
    );
    removed.push(...expired);
    retained.sort((left, right) =>
      left.createdAt.localeCompare(right.createdAt),
    );
    this.snapshotValue = seal({
      ...this.snapshotValue,
      revision: this.snapshotValue.revision + 1,
      records: retained,
      totalTokenEstimate: consumed,
      checksum: undefined,
    });
    return {
      removed: structuredClone(removed),
      retained: structuredClone(retained),
    };
  }

  projectContext(context: E03ContextSnapshot): E03ContextSnapshot {
    const references = this.snapshotValue.records.map(
      (record) => record.memoryId,
    );
    const { checksum: _checksum, ...payload } = context;
    const next = {
      ...payload,
      sequence: context.sequence + 1,
      memoryRefs: [...new Set([...context.memoryRefs, ...references])],
      createdAt: this.clock.now(),
    };
    return { ...next, checksum: digest(next) };
  }

  fromTask(task: E03TaskState): void {
    if (
      task.identity.taskId !== this.snapshotValue.taskId ||
      task.identity.sessionId !== this.snapshotValue.sessionId
    )
      throw new E03RuntimeError(
        "memory_task_mismatch",
        "task identity differs from memory runtime",
      );
    for (const message of task.messages)
      if (message.kind === "steer" || message.kind === "clarification")
        this.capture({
          kind: "instruction",
          content: message.body,
          sourceMessageIds: [message.messageId],
          importance: 0.8,
        });
    if (task.result)
      this.capture({
        kind: "result",
        content: JSON.stringify(task.result),
        sourceArtifactIds: task.artifacts.map(
          (artifact) => artifact.artifact_id,
        ),
        importance: 0.9,
      });
  }

  snapshot(): AgentMemorySnapshot {
    return structuredClone(this.snapshotValue);
  }
}

function seal(
  value: Omit<AgentMemorySnapshot, "checksum"> | AgentMemorySnapshot,
): AgentMemorySnapshot {
  const { checksum: _checksum, ...payload } = value as AgentMemorySnapshot;
  return { ...payload, checksum: digest(payload) };
}

function validate(snapshot: AgentMemorySnapshot): void {
  const { checksum, ...payload } = snapshot;
  assertDigest(payload, checksum, "agent-memory");
  const total = snapshot.records.reduce((sum, record) => {
    const { digest: recordDigest, ...recordPayload } = record;
    assertDigest(recordPayload, recordDigest, `memory:${record.memoryId}`);
    return sum + record.tokenEstimate;
  }, 0);
  if (total !== snapshot.totalTokenEstimate)
    throw new E03RuntimeError(
      "memory_total_mismatch",
      "memory total token estimate is corrupt",
    );
}

export type MemoryQueryMode = "lexical" | "recency" | "importance" | "hybrid";

export interface AgentMemoryQuery {
  queryId: string;
  taskId: string;
  sessionId: string;
  text: string;
  terms: string[];
  kinds: AgentMemoryRecord["kind"][];
  minimumImportance: number;
  maximumResults: number;
  maximumTokens: number;
  mode: MemoryQueryMode;
  includeExpired: boolean;
  createdAt: string;
  digest: string;
}

export interface AgentMemoryMatch {
  queryId: string;
  memoryId: string;
  lexicalScore: number;
  recencyScore: number;
  importanceScore: number;
  sourceScore: number;
  finalScore: number;
  matchedTerms: string[];
  selected: boolean;
  exclusionReason: string | null;
  digest: string;
}

export interface AgentMemoryRetrieval {
  retrievalId: string;
  query: AgentMemoryQuery;
  matches: AgentMemoryMatch[];
  selectedMemoryIds: string[];
  selectedTokenEstimate: number;
  indexRevision: number;
  createdAt: string;
  digest: string;
}

function memoryTerms(value: string): string[] {
  return [
    ...new Set(
      value
        .toLocaleLowerCase()
        .split(/[^\p{L}\p{N}_-]+/u)
        .map((term) => term.trim())
        .filter((term) => term.length >= 2),
    ),
  ];
}

function assertMemoryQuery(query: AgentMemoryQuery): void {
  assertDigest(query, "digest", `memory query ${query.queryId}`);
  if (!query.queryId || !query.taskId || !query.sessionId || !query.text)
    throw new E03RuntimeError(
      "memory_query_identity",
      "memory query identity is incomplete",
    );
  if (
    !Number.isSafeInteger(query.maximumResults) ||
    query.maximumResults < 1 ||
    query.maximumResults > 1_000
  )
    throw new E03RuntimeError(
      "memory_query_result_limit",
      "memory query result limit is invalid",
    );
  if (!Number.isSafeInteger(query.maximumTokens) || query.maximumTokens < 1)
    throw new E03RuntimeError(
      "memory_query_token_limit",
      "memory query token limit is invalid",
    );
  if (
    !Number.isFinite(query.minimumImportance) ||
    query.minimumImportance < 0 ||
    query.minimumImportance > 1
  )
    throw new E03RuntimeError(
      "memory_query_importance",
      "memory query minimum importance is invalid",
    );
}

function assertMemoryMatch(match: AgentMemoryMatch): void {
  assertDigest(
    match,
    "digest",
    `memory match ${match.queryId}/${match.memoryId}`,
  );
  if (!match.queryId || !match.memoryId)
    throw new E03RuntimeError(
      "memory_match_identity",
      "memory match identity is incomplete",
    );
  for (const score of [
    match.lexicalScore,
    match.recencyScore,
    match.importanceScore,
    match.sourceScore,
    match.finalScore,
  ])
    if (!Number.isFinite(score) || score < 0 || score > 1)
      throw new E03RuntimeError(
        "memory_match_score",
        "memory match score is outside the supported range",
      );
  if (match.selected && match.exclusionReason !== null)
    throw new E03RuntimeError(
      "memory_match_selection",
      "selected memory match cannot have an exclusion reason",
    );
}

export class AgentMemoryRetrievalRuntime {
  private records = new Map<string, AgentMemoryRecord>();
  private termIndex = new Map<string, Set<string>>();
  private sourceIndex = new Map<string, Set<string>>();
  private retrievals = new Map<string, AgentMemoryRetrieval>();
  private revisionValue = 0;

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  index(records: readonly AgentMemoryRecord[]): number {
    const nextRecords = new Map<string, AgentMemoryRecord>();
    const nextTerms = new Map<string, Set<string>>();
    const nextSources = new Map<string, Set<string>>();
    for (const raw of records) {
      const record = structuredClone(raw);
      assertDigest(record, "digest", `memory ${record.memoryId}`);
      if (nextRecords.has(record.memoryId))
        throw new E03RuntimeError(
          "memory_index_duplicate",
          `duplicate memory ${record.memoryId}`,
        );
      nextRecords.set(record.memoryId, record);
      for (const term of memoryTerms(record.content)) {
        const postings = nextTerms.get(term) ?? new Set<string>();
        postings.add(record.memoryId);
        nextTerms.set(term, postings);
      }
      for (const sourceId of [
        ...record.sourceMessageIds,
        ...record.sourceArtifactIds,
      ]) {
        const postings = nextSources.get(sourceId) ?? new Set<string>();
        postings.add(record.memoryId);
        nextSources.set(sourceId, postings);
      }
    }
    this.records = nextRecords;
    this.termIndex = nextTerms;
    this.sourceIndex = nextSources;
    this.revisionValue += 1;
    return this.revisionValue;
  }

  upsert(record: AgentMemoryRecord): number {
    assertDigest(record, "digest", `memory ${record.memoryId}`);
    const next = [...this.records.values()].filter(
      (candidate) => candidate.memoryId !== record.memoryId,
    );
    next.push(structuredClone(record));
    return this.index(next);
  }

  remove(memoryId: string): number {
    if (!this.records.has(memoryId))
      throw new E03RuntimeError(
        "memory_index_missing",
        `memory ${memoryId} is not indexed`,
      );
    return this.index(
      [...this.records.values()].filter(
        (record) => record.memoryId !== memoryId,
      ),
    );
  }

  query(input: {
    taskId: string;
    sessionId: string;
    text: string;
    kinds?: readonly AgentMemoryRecord["kind"][];
    minimumImportance?: number;
    maximumResults?: number;
    maximumTokens?: number;
    mode?: MemoryQueryMode;
    includeExpired?: boolean;
    sourceIds?: readonly string[];
  }): AgentMemoryRetrieval {
    const terms = memoryTerms(input.text);
    if (!terms.length)
      throw new E03RuntimeError(
        "memory_query_empty",
        "memory query must contain searchable terms",
      );
    const queryPayload = {
      queryId: createId("memory-query"),
      taskId: input.taskId.trim(),
      sessionId: input.sessionId.trim(),
      text: input.text.trim(),
      terms,
      kinds: [...new Set(input.kinds ?? [])],
      minimumImportance: input.minimumImportance ?? 0,
      maximumResults: input.maximumResults ?? 12,
      maximumTokens: input.maximumTokens ?? 8_000,
      mode: input.mode ?? ("hybrid" as const),
      includeExpired: input.includeExpired ?? false,
      createdAt: this.clock.now(),
    };
    const query = { ...queryPayload, digest: digest(queryPayload) };
    assertMemoryQuery(query);
    const candidates = new Set<string>();
    for (const term of query.terms)
      for (const memoryId of this.termIndex.get(term) ?? [])
        candidates.add(memoryId);
    for (const sourceId of input.sourceIds ?? [])
      for (const memoryId of this.sourceIndex.get(sourceId) ?? [])
        candidates.add(memoryId);
    const now = Date.parse(this.clock.now());
    const matches = [...candidates]
      .map((memoryId) => this.records.get(memoryId))
      .filter((record): record is AgentMemoryRecord => Boolean(record))
      .filter(
        (record) =>
          record.taskId === query.taskId &&
          record.sessionId === query.sessionId,
      )
      .map((record) => {
        const recordTerms = new Set(memoryTerms(record.content));
        const matchedTerms = query.terms.filter((term) =>
          recordTerms.has(term),
        );
        const lexicalScore = Math.min(
          1,
          matchedTerms.length / query.terms.length,
        );
        const ageMs = Math.max(0, now - Date.parse(record.createdAt));
        const recencyScore = 1 / (1 + ageMs / 86_400_000);
        const importanceScore = record.importance;
        const sourceScore = (input.sourceIds ?? []).some(
          (sourceId) =>
            record.sourceMessageIds.includes(sourceId) ||
            record.sourceArtifactIds.includes(sourceId),
        )
          ? 1
          : 0;
        const finalScore = Math.max(
          0,
          Math.min(
            1,
            query.mode === "lexical"
              ? lexicalScore
              : query.mode === "recency"
                ? recencyScore
                : query.mode === "importance"
                  ? importanceScore
                  : lexicalScore * 0.5 +
                    recencyScore * 0.2 +
                    importanceScore * 0.2 +
                    sourceScore * 0.1,
          ),
        );
        const expired =
          record.expiresAt !== null && Date.parse(record.expiresAt) <= now;
        let exclusionReason: string | null = null;
        if (query.kinds.length && !query.kinds.includes(record.kind))
          exclusionReason = "kind-filtered";
        else if (record.importance < query.minimumImportance)
          exclusionReason = "importance-filtered";
        else if (expired && !query.includeExpired) exclusionReason = "expired";
        const payload = {
          queryId: query.queryId,
          memoryId: record.memoryId,
          lexicalScore,
          recencyScore,
          importanceScore,
          sourceScore,
          finalScore,
          matchedTerms,
          selected: false,
          exclusionReason,
        };
        const match = { ...payload, digest: digest(payload) };
        assertMemoryMatch(match);
        return match;
      })
      .sort(
        (left, right) =>
          right.finalScore - left.finalScore ||
          left.memoryId.localeCompare(right.memoryId),
      );
    const selectedMemoryIds: string[] = [];
    let selectedTokenEstimate = 0;
    for (let index = 0; index < matches.length; index += 1) {
      const match = matches[index]!;
      if (match.exclusionReason !== null) continue;
      const record = this.records.get(match.memoryId)!;
      if (selectedMemoryIds.length >= query.maximumResults) {
        matches[index] = this.resealMatch(match, {
          exclusionReason: "result-limit",
        });
        continue;
      }
      if (selectedTokenEstimate + record.tokenEstimate > query.maximumTokens) {
        matches[index] = this.resealMatch(match, {
          exclusionReason: "token-limit",
        });
        continue;
      }
      selectedMemoryIds.push(record.memoryId);
      selectedTokenEstimate += record.tokenEstimate;
      matches[index] = this.resealMatch(match, {
        selected: true,
        exclusionReason: null,
      });
    }
    const payload = {
      retrievalId: createId("memory-retrieval"),
      query,
      matches,
      selectedMemoryIds,
      selectedTokenEstimate,
      indexRevision: this.revisionValue,
      createdAt: this.clock.now(),
    };
    const retrieval = { ...payload, digest: digest(payload) };
    this.assertRetrieval(retrieval);
    this.retrievals.set(retrieval.retrievalId, structuredClone(retrieval));
    return structuredClone(retrieval);
  }

  resolve(retrievalId: string): AgentMemoryRecord[] {
    const retrieval = this.retrievals.get(retrievalId);
    if (!retrieval)
      throw new E03RuntimeError(
        "memory_retrieval_missing",
        `memory retrieval ${retrievalId} does not exist`,
      );
    this.assertRetrieval(retrieval);
    if (retrieval.indexRevision !== this.revisionValue)
      throw new E03RuntimeError(
        "memory_retrieval_stale",
        `memory retrieval ${retrievalId} was created against an older index`,
      );
    return retrieval.selectedMemoryIds.map((memoryId) => {
      const record = this.records.get(memoryId);
      if (!record)
        throw new E03RuntimeError(
          "memory_retrieval_record_missing",
          `selected memory ${memoryId} is no longer indexed`,
        );
      return structuredClone(record);
    });
  }

  snapshot(): {
    revision: number;
    records: AgentMemoryRecord[];
    retrievals: AgentMemoryRetrieval[];
  } {
    return {
      revision: this.revisionValue,
      records: [...this.records.values()].map((record) =>
        structuredClone(record),
      ),
      retrievals: [...this.retrievals.values()].map((retrieval) =>
        structuredClone(retrieval),
      ),
    };
  }

  restore(input: {
    revision: number;
    records: readonly AgentMemoryRecord[];
    retrievals: readonly AgentMemoryRetrieval[];
  }): void {
    if (!Number.isSafeInteger(input.revision) || input.revision < 0)
      throw new E03RuntimeError(
        "memory_index_revision",
        "memory index revision is invalid",
      );
    this.index(input.records);
    this.revisionValue = input.revision;
    const nextRetrievals = new Map<string, AgentMemoryRetrieval>();
    for (const retrieval of input.retrievals) {
      this.assertRetrieval(retrieval);
      if (nextRetrievals.has(retrieval.retrievalId))
        throw new E03RuntimeError(
          "memory_retrieval_duplicate",
          `duplicate memory retrieval ${retrieval.retrievalId}`,
        );
      nextRetrievals.set(retrieval.retrievalId, structuredClone(retrieval));
    }
    this.retrievals = nextRetrievals;
  }

  private resealMatch(
    match: AgentMemoryMatch,
    patch: Partial<Omit<AgentMemoryMatch, "queryId" | "memoryId" | "digest">>,
  ): AgentMemoryMatch {
    const { digest: _, ...prior } = match;
    const payload = {
      ...prior,
      ...patch,
      queryId: match.queryId,
      memoryId: match.memoryId,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMemoryMatch(next);
    return next;
  }

  private assertRetrieval(retrieval: AgentMemoryRetrieval): void {
    assertDigest(
      retrieval,
      "digest",
      `memory retrieval ${retrieval.retrievalId}`,
    );
    assertMemoryQuery(retrieval.query);
    for (const match of retrieval.matches) {
      assertMemoryMatch(match);
      if (match.queryId !== retrieval.query.queryId)
        throw new E03RuntimeError(
          "memory_retrieval_match_custody",
          `memory match ${match.memoryId} belongs to another query`,
        );
    }
    const selected = retrieval.matches
      .filter((match) => match.selected)
      .map((match) => match.memoryId);
    if (digest(selected) !== digest(retrieval.selectedMemoryIds))
      throw new E03RuntimeError(
        "memory_retrieval_selection",
        `memory retrieval ${retrieval.retrievalId} selection is inconsistent`,
      );
  }
}

export type MemoryAccessAction =
  | "capture"
  | "retrieve"
  | "promote"
  | "expire"
  | "redact"
  | "restore";

export interface MemoryAccessEntry {
  entryId: string;
  sequence: number;
  taskId: string;
  sessionId: string;
  memoryId: string | null;
  action: MemoryAccessAction;
  actorId: string;
  purpose: string;
  permissionDigest: string;
  outcome: "allowed" | "denied" | "not-found";
  occurredAt: string;
  previousDigest: string;
  digest: string;
}

function assertMemoryAccessEntry(
  entry: MemoryAccessEntry,
  previousDigest: string,
): void {
  assertDigest(entry, "digest", `memory access ${entry.entryId}`);
  if (!entry.entryId || !entry.taskId || !entry.sessionId || !entry.actorId)
    throw new E03RuntimeError(
      "memory_access_identity",
      "memory access identity is incomplete",
    );
  if (!entry.purpose || !entry.permissionDigest)
    throw new E03RuntimeError(
      "memory_access_authority",
      "memory access authority is incomplete",
    );
  if (!Number.isSafeInteger(entry.sequence) || entry.sequence < 1)
    throw new E03RuntimeError(
      "memory_access_sequence",
      "memory access sequence is invalid",
    );
  if (entry.previousDigest !== previousDigest)
    throw new E03RuntimeError(
      "memory_access_chain",
      `memory access ${entry.entryId} does not extend the prior entry`,
    );
}

export class MemoryAccessJournal {
  private entries: MemoryAccessEntry[] = [];

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  append(input: {
    taskId: string;
    sessionId: string;
    memoryId?: string | null;
    action: MemoryAccessAction;
    actorId: string;
    purpose: string;
    permissionDigest: string;
    outcome: MemoryAccessEntry["outcome"];
  }): MemoryAccessEntry {
    const previous = this.entries[this.entries.length - 1];
    const payload = {
      entryId: createId("memory-access"),
      sequence: (previous?.sequence ?? 0) + 1,
      taskId: input.taskId.trim(),
      sessionId: input.sessionId.trim(),
      memoryId: input.memoryId?.trim() || null,
      action: input.action,
      actorId: input.actorId.trim(),
      purpose: input.purpose.trim(),
      permissionDigest: input.permissionDigest.trim(),
      outcome: input.outcome,
      occurredAt: this.clock.now(),
      previousDigest: previous?.digest ?? "root",
    };
    const entry = { ...payload, digest: digest(payload) };
    assertMemoryAccessEntry(entry, previous?.digest ?? "root");
    this.entries.push(entry);
    return structuredClone(entry);
  }

  list(input?: {
    taskId?: string;
    sessionId?: string;
    memoryId?: string;
    actorId?: string;
    action?: MemoryAccessAction;
    afterSequence?: number;
  }): MemoryAccessEntry[] {
    const afterSequence = input?.afterSequence ?? 0;
    if (!Number.isSafeInteger(afterSequence) || afterSequence < 0)
      throw new E03RuntimeError(
        "memory_access_cursor",
        "memory access cursor is invalid",
      );
    return this.entries
      .filter((entry) => entry.sequence > afterSequence)
      .filter((entry) => !input?.taskId || entry.taskId === input.taskId)
      .filter(
        (entry) => !input?.sessionId || entry.sessionId === input.sessionId,
      )
      .filter((entry) => !input?.memoryId || entry.memoryId === input.memoryId)
      .filter((entry) => !input?.actorId || entry.actorId === input.actorId)
      .filter((entry) => !input?.action || entry.action === input.action)
      .map((entry) => structuredClone(entry));
  }

  verify(): { entries: number; headDigest: string } {
    let previousDigest = "root";
    for (const entry of this.entries) {
      assertMemoryAccessEntry(entry, previousDigest);
      previousDigest = entry.digest;
    }
    return { entries: this.entries.length, headDigest: previousDigest };
  }

  snapshot(): MemoryAccessEntry[] {
    this.verify();
    return this.entries.map((entry) => structuredClone(entry));
  }

  restore(entries: readonly MemoryAccessEntry[]): void {
    let previousDigest = "root";
    const next: MemoryAccessEntry[] = [];
    for (const raw of entries) {
      const entry = structuredClone(raw);
      assertMemoryAccessEntry(entry, previousDigest);
      if (entry.sequence !== next.length + 1)
        throw new E03RuntimeError(
          "memory_access_restore_gap",
          `memory access sequence ${entry.sequence} is not contiguous`,
        );
      next.push(entry);
      previousDigest = entry.digest;
    }
    this.entries = next;
  }
}

export interface MemoryRetentionPolicy {
  policyId: string;
  kinds: AgentMemoryRecord["kind"][];
  minimumImportanceToKeep: number;
  maximumAgeMs: number | null;
  maximumRecords: number | null;
  maximumTokens: number | null;
  protectSourceBacked: boolean;
  createdAt: string;
  digest: string;
}

export interface MemoryRetentionDecision {
  decisionId: string;
  policyId: string;
  memoryId: string;
  action: "keep" | "expire" | "evict" | "protect";
  reason: string;
  evaluatedAt: string;
  digest: string;
}

export class MemoryRetentionRuntime {
  private policies = new Map<string, MemoryRetentionPolicy>();
  private decisions = new Map<string, MemoryRetentionDecision>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  register(input: {
    kinds?: readonly AgentMemoryRecord["kind"][];
    minimumImportanceToKeep?: number;
    maximumAgeMs?: number | null;
    maximumRecords?: number | null;
    maximumTokens?: number | null;
    protectSourceBacked?: boolean;
  }): MemoryRetentionPolicy {
    const minimumImportanceToKeep = input.minimumImportanceToKeep ?? 0.75;
    if (
      !Number.isFinite(minimumImportanceToKeep) ||
      minimumImportanceToKeep < 0 ||
      minimumImportanceToKeep > 1
    )
      throw new E03RuntimeError(
        "memory_retention_importance",
        "memory retention importance is invalid",
      );
    for (const [name, value] of [
      ["maximum age", input.maximumAgeMs],
      ["maximum records", input.maximumRecords],
      ["maximum tokens", input.maximumTokens],
    ] as const)
      if (
        value !== undefined &&
        value !== null &&
        (!Number.isSafeInteger(value) || value < 1)
      )
        throw new E03RuntimeError(
          "memory_retention_limit",
          `memory retention ${name} is invalid`,
        );
    const payload = {
      policyId: createId("memory-retention"),
      kinds: [...new Set(input.kinds ?? [])],
      minimumImportanceToKeep,
      maximumAgeMs: input.maximumAgeMs ?? null,
      maximumRecords: input.maximumRecords ?? null,
      maximumTokens: input.maximumTokens ?? null,
      protectSourceBacked: input.protectSourceBacked ?? true,
      createdAt: this.clock.now(),
    };
    const policy = { ...payload, digest: digest(payload) };
    this.assertPolicy(policy);
    this.policies.set(policy.policyId, policy);
    return structuredClone(policy);
  }

  evaluate(
    policyId: string,
    records: readonly AgentMemoryRecord[],
  ): MemoryRetentionDecision[] {
    const policy = this.requirePolicy(policyId);
    const now = Date.parse(this.clock.now());
    const ordered = [...records].sort(
      (left, right) =>
        right.importance - left.importance ||
        right.createdAt.localeCompare(left.createdAt) ||
        left.memoryId.localeCompare(right.memoryId),
    );
    let keptRecords = 0;
    let keptTokens = 0;
    const output: MemoryRetentionDecision[] = [];
    for (const record of ordered) {
      assertDigest(record, "digest", `memory ${record.memoryId}`);
      const applies =
        !policy.kinds.length || policy.kinds.includes(record.kind);
      const sourceBacked =
        record.sourceMessageIds.length > 0 ||
        record.sourceArtifactIds.length > 0;
      const expiredByRecord =
        record.expiresAt !== null && Date.parse(record.expiresAt) <= now;
      const expiredByAge =
        policy.maximumAgeMs !== null &&
        now - Date.parse(record.createdAt) > policy.maximumAgeMs;
      let action: MemoryRetentionDecision["action"] = "keep";
      let reason = "within-policy";
      if (!applies) {
        action = "keep";
        reason = "policy-kind-excluded";
      } else if (policy.protectSourceBacked && sourceBacked) {
        action = "protect";
        reason = "source-backed";
      } else if (record.importance >= policy.minimumImportanceToKeep) {
        action = "protect";
        reason = "importance-floor";
      } else if (expiredByRecord || expiredByAge) {
        action = "expire";
        reason = expiredByRecord ? "record-expiry" : "maximum-age";
      } else if (
        policy.maximumRecords !== null &&
        keptRecords >= policy.maximumRecords
      ) {
        action = "evict";
        reason = "record-budget";
      } else if (
        policy.maximumTokens !== null &&
        keptTokens + record.tokenEstimate > policy.maximumTokens
      ) {
        action = "evict";
        reason = "token-budget";
      }
      if (action === "keep" || action === "protect") {
        keptRecords += 1;
        keptTokens += record.tokenEstimate;
      }
      const payload = {
        decisionId: createId("memory-retention-decision"),
        policyId: policy.policyId,
        memoryId: record.memoryId,
        action,
        reason,
        evaluatedAt: this.clock.now(),
      };
      const decision = { ...payload, digest: digest(payload) };
      this.assertDecision(decision);
      this.decisions.set(decision.decisionId, decision);
      output.push(structuredClone(decision));
    }
    return output;
  }

  snapshot(): {
    policies: MemoryRetentionPolicy[];
    decisions: MemoryRetentionDecision[];
  } {
    return {
      policies: [...this.policies.values()].map((policy) =>
        structuredClone(policy),
      ),
      decisions: [...this.decisions.values()].map((decision) =>
        structuredClone(decision),
      ),
    };
  }

  restore(input: {
    policies: readonly MemoryRetentionPolicy[];
    decisions: readonly MemoryRetentionDecision[];
  }): void {
    const policies = new Map<string, MemoryRetentionPolicy>();
    const decisions = new Map<string, MemoryRetentionDecision>();
    for (const policy of input.policies) {
      this.assertPolicy(policy);
      if (policies.has(policy.policyId))
        throw new E03RuntimeError(
          "memory_retention_policy_duplicate",
          `duplicate memory retention policy ${policy.policyId}`,
        );
      policies.set(policy.policyId, structuredClone(policy));
    }
    for (const decision of input.decisions) {
      this.assertDecision(decision);
      if (!policies.has(decision.policyId))
        throw new E03RuntimeError(
          "memory_retention_decision_orphan",
          `memory retention decision ${decision.decisionId} has no policy`,
        );
      if (decisions.has(decision.decisionId))
        throw new E03RuntimeError(
          "memory_retention_decision_duplicate",
          `duplicate memory retention decision ${decision.decisionId}`,
        );
      decisions.set(decision.decisionId, structuredClone(decision));
    }
    this.policies = policies;
    this.decisions = decisions;
  }

  private requirePolicy(policyId: string): MemoryRetentionPolicy {
    const policy = this.policies.get(policyId);
    if (!policy)
      throw new E03RuntimeError(
        "memory_retention_policy_missing",
        `memory retention policy ${policyId} does not exist`,
      );
    this.assertPolicy(policy);
    return policy;
  }

  private assertPolicy(policy: MemoryRetentionPolicy): void {
    assertDigest(
      policy,
      "digest",
      `memory retention policy ${policy.policyId}`,
    );
    if (!policy.policyId)
      throw new E03RuntimeError(
        "memory_retention_policy_identity",
        "memory retention policy identity is missing",
      );
  }

  private assertDecision(decision: MemoryRetentionDecision): void {
    assertDigest(
      decision,
      "digest",
      `memory retention decision ${decision.decisionId}`,
    );
    if (
      !decision.decisionId ||
      !decision.policyId ||
      !decision.memoryId ||
      !decision.reason
    )
      throw new E03RuntimeError(
        "memory_retention_decision_identity",
        "memory retention decision identity is incomplete",
      );
  }
}

export type MemoryConflictKind =
  | "contradiction"
  | "supersession"
  | "duplicate"
  | "scope-mismatch"
  | "source-divergence";

export interface MemoryConflict {
  conflictId: string;
  taskId: string;
  sessionId: string;
  leftMemoryId: string;
  rightMemoryId: string;
  kind: MemoryConflictKind;
  state: "detected" | "reviewed" | "resolved" | "ignored";
  confidence: number;
  overlappingTerms: string[];
  conflictingTerms: string[];
  resolution: "keep-left" | "keep-right" | "keep-both" | "merge" | null;
  mergedMemoryId: string | null;
  detectedAt: string;
  resolvedAt: string | null;
  revision: number;
  digest: string;
}

export interface MemoryConsolidation {
  consolidationId: string;
  taskId: string;
  sessionId: string;
  sourceMemoryIds: string[];
  targetKind: AgentMemoryRecord["kind"];
  targetContent: string;
  targetImportance: number;
  state: "planned" | "validated" | "committed" | "rejected";
  resultingMemoryId: string | null;
  createdAt: string;
  validatedAt: string | null;
  committedAt: string | null;
  revision: number;
  digest: string;
}

function assertMemoryConflict(conflict: MemoryConflict): void {
  assertDigest(conflict, "digest", `memory conflict ${conflict.conflictId}`);
  if (
    !conflict.conflictId ||
    !conflict.taskId ||
    !conflict.sessionId ||
    !conflict.leftMemoryId ||
    !conflict.rightMemoryId ||
    conflict.leftMemoryId === conflict.rightMemoryId
  )
    throw new E03RuntimeError(
      "memory_conflict_identity",
      "memory conflict identity is invalid",
    );
  if (!Number.isFinite(conflict.confidence) || conflict.confidence < 0 || conflict.confidence > 1)
    throw new E03RuntimeError(
      "memory_conflict_confidence",
      "memory conflict confidence is invalid",
    );
  if (!Number.isSafeInteger(conflict.revision) || conflict.revision < 1)
    throw new E03RuntimeError(
      "memory_conflict_revision",
      "memory conflict revision is invalid",
    );
  if (conflict.state === "resolved" && (!conflict.resolution || !conflict.resolvedAt))
    throw new E03RuntimeError(
      "memory_conflict_state",
      "resolved memory conflict requires resolution metadata",
    );
}

function assertMemoryConsolidation(consolidation: MemoryConsolidation): void {
  assertDigest(
    consolidation,
    "digest",
    `memory consolidation ${consolidation.consolidationId}`,
  );
  if (
    !consolidation.consolidationId ||
    !consolidation.taskId ||
    !consolidation.sessionId ||
    !consolidation.sourceMemoryIds.length ||
    !consolidation.targetContent
  )
    throw new E03RuntimeError(
      "memory_consolidation_identity",
      "memory consolidation identity is incomplete",
    );
  if (
    !Number.isFinite(consolidation.targetImportance) ||
    consolidation.targetImportance < 0 ||
    consolidation.targetImportance > 1
  )
    throw new E03RuntimeError(
      "memory_consolidation_importance",
      "memory consolidation importance is invalid",
    );
  if (!Number.isSafeInteger(consolidation.revision) || consolidation.revision < 1)
    throw new E03RuntimeError(
      "memory_consolidation_revision",
      "memory consolidation revision is invalid",
    );
  if (consolidation.state === "committed" && !consolidation.resultingMemoryId)
    throw new E03RuntimeError(
      "memory_consolidation_state",
      "committed memory consolidation requires a result",
    );
}

export class AgentMemoryConsolidationRuntime {
  private records = new Map<string, AgentMemoryRecord>();
  private conflicts = new Map<string, MemoryConflict>();
  private consolidations = new Map<string, MemoryConsolidation>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  index(records: readonly AgentMemoryRecord[]): void {
    const next = new Map<string, AgentMemoryRecord>();
    for (const record of records) {
      assertDigest(record, "digest", `memory ${record.memoryId}`);
      if (next.has(record.memoryId))
        throw new E03RuntimeError(
          "memory_consolidation_record_duplicate",
          `duplicate memory ${record.memoryId}`,
        );
      next.set(record.memoryId, structuredClone(record));
    }
    this.records = next;
  }

  detect(input: {
    leftMemoryId: string;
    rightMemoryId: string;
    kind: MemoryConflictKind;
    conflictingTerms?: readonly string[];
  }): MemoryConflict {
    const left = this.requireMemory(input.leftMemoryId);
    const right = this.requireMemory(input.rightMemoryId);
    if (left.taskId !== right.taskId || left.sessionId !== right.sessionId)
      throw new E03RuntimeError(
        "memory_conflict_custody",
        "memory conflict candidates belong to different tasks",
      );
    const existing = [...this.conflicts.values()].find(
      (conflict) =>
        conflict.kind === input.kind &&
        ((conflict.leftMemoryId === left.memoryId &&
          conflict.rightMemoryId === right.memoryId) ||
          (conflict.leftMemoryId === right.memoryId &&
            conflict.rightMemoryId === left.memoryId)),
    );
    if (existing) return structuredClone(existing);
    const leftTerms = new Set(memoryTerms(left.content));
    const rightTerms = new Set(memoryTerms(right.content));
    const overlappingTerms = [...leftTerms].filter((term) => rightTerms.has(term));
    const denominator = Math.max(1, Math.min(leftTerms.size, rightTerms.size));
    const confidence = Math.min(
      1,
      Math.max(
        input.kind === "duplicate" ? 0.5 : 0.1,
        overlappingTerms.length / denominator,
      ),
    );
    const payload = {
      conflictId: createId("memory-conflict"),
      taskId: left.taskId,
      sessionId: left.sessionId,
      leftMemoryId: left.memoryId,
      rightMemoryId: right.memoryId,
      kind: input.kind,
      state: "detected" as const,
      confidence,
      overlappingTerms,
      conflictingTerms: [...new Set(input.conflictingTerms ?? [])],
      resolution: null,
      mergedMemoryId: null,
      detectedAt: this.clock.now(),
      resolvedAt: null,
      revision: 1,
    };
    const conflict = { ...payload, digest: digest(payload) };
    assertMemoryConflict(conflict);
    this.conflicts.set(conflict.conflictId, conflict);
    return structuredClone(conflict);
  }

  plan(input: {
    sourceMemoryIds: readonly string[];
    targetKind: AgentMemoryRecord["kind"];
    targetContent: string;
    targetImportance?: number;
  }): MemoryConsolidation {
    const sourceMemoryIds = [...new Set(input.sourceMemoryIds)];
    const records = sourceMemoryIds.map((memoryId) => this.requireMemory(memoryId));
    if (records.length < 2)
      throw new E03RuntimeError(
        "memory_consolidation_sources",
        "memory consolidation requires at least two records",
      );
    const first = records[0]!;
    if (
      records.some(
        (record) => record.taskId !== first.taskId || record.sessionId !== first.sessionId,
      )
    )
      throw new E03RuntimeError(
        "memory_consolidation_custody",
        "memory consolidation records belong to different tasks",
      );
    const payload = {
      consolidationId: createId("memory-consolidation"),
      taskId: first.taskId,
      sessionId: first.sessionId,
      sourceMemoryIds,
      targetKind: input.targetKind,
      targetContent: input.targetContent.trim(),
      targetImportance:
        input.targetImportance ??
        records.reduce((total, record) => total + record.importance, 0) / records.length,
      state: "planned" as const,
      resultingMemoryId: null,
      createdAt: this.clock.now(),
      validatedAt: null,
      committedAt: null,
      revision: 1,
    };
    const consolidation = { ...payload, digest: digest(payload) };
    assertMemoryConsolidation(consolidation);
    this.consolidations.set(consolidation.consolidationId, consolidation);
    return structuredClone(consolidation);
  }

  validate(consolidationId: string, expectedRevision: number): MemoryConsolidation {
    const consolidation = this.requireConsolidation(consolidationId);
    this.assertConsolidationRevision(consolidation, expectedRevision);
    if (consolidation.state !== "planned")
      throw new E03RuntimeError(
        "memory_consolidation_validate_state",
        `memory consolidation ${consolidationId} is ${consolidation.state}`,
      );
    for (const memoryId of consolidation.sourceMemoryIds) this.requireMemory(memoryId);
    const terms = memoryTerms(consolidation.targetContent);
    if (terms.length < 2)
      return this.transitionConsolidation(consolidation, { state: "rejected" });
    return this.transitionConsolidation(consolidation, {
      state: "validated",
      validatedAt: this.clock.now(),
    });
  }

  commit(
    consolidationId: string,
    expectedRevision: number,
    memory: AgentMemoryRecord,
  ): MemoryConsolidation {
    const consolidation = this.requireConsolidation(consolidationId);
    this.assertConsolidationRevision(consolidation, expectedRevision);
    if (consolidation.state !== "validated")
      throw new E03RuntimeError(
        "memory_consolidation_commit_state",
        `memory consolidation ${consolidationId} is ${consolidation.state}`,
      );
    assertDigest(memory, "digest", `memory ${memory.memoryId}`);
    if (
      memory.taskId !== consolidation.taskId ||
      memory.sessionId !== consolidation.sessionId ||
      memory.kind !== consolidation.targetKind ||
      memory.content !== consolidation.targetContent
    )
      throw new E03RuntimeError(
        "memory_consolidation_result",
        "memory consolidation result does not match the plan",
      );
    this.records.set(memory.memoryId, structuredClone(memory));
    return this.transitionConsolidation(consolidation, {
      state: "committed",
      resultingMemoryId: memory.memoryId,
      committedAt: this.clock.now(),
    });
  }

  resolveConflict(input: {
    conflictId: string;
    expectedRevision: number;
    resolution: NonNullable<MemoryConflict["resolution"]>;
    mergedMemoryId?: string | null;
  }): MemoryConflict {
    const conflict = this.requireConflict(input.conflictId);
    if (conflict.revision !== input.expectedRevision)
      throw new E03RuntimeError(
        "memory_conflict_stale_revision",
        `memory conflict ${conflict.conflictId} revision is stale`,
      );
    if (conflict.state === "resolved" || conflict.state === "ignored")
      throw new E03RuntimeError(
        "memory_conflict_resolve_state",
        `memory conflict ${conflict.conflictId} is ${conflict.state}`,
      );
    if (input.resolution === "merge") {
      if (!input.mergedMemoryId) throw new E03RuntimeError(
        "memory_conflict_merge_result",
        "merged conflict resolution requires a memory id",
      );
      this.requireMemory(input.mergedMemoryId);
    }
    const { digest: _, ...prior } = conflict;
    const payload = {
      ...prior,
      state: "resolved" as const,
      resolution: input.resolution,
      mergedMemoryId: input.mergedMemoryId ?? null,
      resolvedAt: this.clock.now(),
      revision: conflict.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMemoryConflict(next);
    this.conflicts.set(next.conflictId, next);
    return structuredClone(next);
  }

  snapshot(): {
    records: AgentMemoryRecord[];
    conflicts: MemoryConflict[];
    consolidations: MemoryConsolidation[];
  } {
    return {
      records: [...this.records.values()].map((value) => structuredClone(value)),
      conflicts: [...this.conflicts.values()].map((value) => structuredClone(value)),
      consolidations: [...this.consolidations.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }

  private requireMemory(memoryId: string): AgentMemoryRecord {
    const memory = this.records.get(memoryId);
    if (!memory)
      throw new E03RuntimeError(
        "memory_consolidation_record_missing",
        `memory ${memoryId} does not exist`,
      );
    return memory;
  }

  private requireConflict(conflictId: string): MemoryConflict {
    const conflict = this.conflicts.get(conflictId);
    if (!conflict)
      throw new E03RuntimeError(
        "memory_conflict_missing",
        `memory conflict ${conflictId} does not exist`,
      );
    assertMemoryConflict(conflict);
    return conflict;
  }

  private requireConsolidation(consolidationId: string): MemoryConsolidation {
    const consolidation = this.consolidations.get(consolidationId);
    if (!consolidation)
      throw new E03RuntimeError(
        "memory_consolidation_missing",
        `memory consolidation ${consolidationId} does not exist`,
      );
    assertMemoryConsolidation(consolidation);
    return consolidation;
  }

  private assertConsolidationRevision(consolidation: MemoryConsolidation, expected: number): void {
    if (consolidation.revision !== expected)
      throw new E03RuntimeError(
        "memory_consolidation_stale_revision",
        `memory consolidation ${consolidation.consolidationId} revision is stale`,
      );
  }

  private transitionConsolidation(
    consolidation: MemoryConsolidation,
    patch: Partial<Omit<MemoryConsolidation, "consolidationId" | "revision" | "digest">>,
  ): MemoryConsolidation {
    const { digest: _, ...prior } = consolidation;
    const payload = {
      ...prior,
      ...patch,
      consolidationId: consolidation.consolidationId,
      revision: consolidation.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMemoryConsolidation(next);
    this.consolidations.set(next.consolidationId, next);
    return structuredClone(next);
  }
}
