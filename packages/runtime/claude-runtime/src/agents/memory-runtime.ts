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
  if (
    !Number.isFinite(conflict.confidence) ||
    conflict.confidence < 0 ||
    conflict.confidence > 1
  )
    throw new E03RuntimeError(
      "memory_conflict_confidence",
      "memory conflict confidence is invalid",
    );
  if (!Number.isSafeInteger(conflict.revision) || conflict.revision < 1)
    throw new E03RuntimeError(
      "memory_conflict_revision",
      "memory conflict revision is invalid",
    );
  if (
    conflict.state === "resolved" &&
    (!conflict.resolution || !conflict.resolvedAt)
  )
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
  if (
    !Number.isSafeInteger(consolidation.revision) ||
    consolidation.revision < 1
  )
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
    const overlappingTerms = [...leftTerms].filter((term) =>
      rightTerms.has(term),
    );
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
    const records = sourceMemoryIds.map((memoryId) =>
      this.requireMemory(memoryId),
    );
    if (records.length < 2)
      throw new E03RuntimeError(
        "memory_consolidation_sources",
        "memory consolidation requires at least two records",
      );
    const first = records[0]!;
    if (
      records.some(
        (record) =>
          record.taskId !== first.taskId ||
          record.sessionId !== first.sessionId,
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
        records.reduce((total, record) => total + record.importance, 0) /
          records.length,
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

  validate(
    consolidationId: string,
    expectedRevision: number,
  ): MemoryConsolidation {
    const consolidation = this.requireConsolidation(consolidationId);
    this.assertConsolidationRevision(consolidation, expectedRevision);
    if (consolidation.state !== "planned")
      throw new E03RuntimeError(
        "memory_consolidation_validate_state",
        `memory consolidation ${consolidationId} is ${consolidation.state}`,
      );
    for (const memoryId of consolidation.sourceMemoryIds)
      this.requireMemory(memoryId);
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
      if (!input.mergedMemoryId)
        throw new E03RuntimeError(
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
      records: [...this.records.values()].map((value) =>
        structuredClone(value),
      ),
      conflicts: [...this.conflicts.values()].map((value) =>
        structuredClone(value),
      ),
      consolidations: [...this.consolidations.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }

  restore(snapshot: {
    records: readonly AgentMemoryRecord[];
    conflicts: readonly MemoryConflict[];
    consolidations: readonly MemoryConsolidation[];
  }): void {
    const records = new Map<string, AgentMemoryRecord>();
    const conflicts = new Map<string, MemoryConflict>();
    const consolidations = new Map<string, MemoryConsolidation>();
    for (const record of snapshot.records) {
      assertDigest(record, "digest", `memory ${record.memoryId}`);
      if (records.has(record.memoryId))
        throw new E03RuntimeError(
          "memory_consolidation_restore_duplicate_record",
          `duplicate memory ${record.memoryId}`,
        );
      records.set(record.memoryId, structuredClone(record));
    }
    for (const conflict of snapshot.conflicts) {
      assertMemoryConflict(conflict);
      if (conflicts.has(conflict.conflictId))
        throw new E03RuntimeError(
          "memory_consolidation_restore_duplicate_conflict",
          `duplicate memory conflict ${conflict.conflictId}`,
        );
      if (
        !records.has(conflict.leftMemoryId) ||
        !records.has(conflict.rightMemoryId) ||
        (conflict.mergedMemoryId !== null &&
          !records.has(conflict.mergedMemoryId))
      )
        throw new E03RuntimeError(
          "memory_consolidation_restore_conflict_reference",
          `memory conflict ${conflict.conflictId} has an invalid reference`,
        );
      conflicts.set(conflict.conflictId, structuredClone(conflict));
    }
    for (const consolidation of snapshot.consolidations) {
      assertMemoryConsolidation(consolidation);
      if (consolidations.has(consolidation.consolidationId))
        throw new E03RuntimeError(
          "memory_consolidation_restore_duplicate_plan",
          `duplicate memory consolidation ${consolidation.consolidationId}`,
        );
      if (
        consolidation.sourceMemoryIds.some(
          (memoryId) => !records.has(memoryId),
        ) ||
        (consolidation.resultingMemoryId !== null &&
          !records.has(consolidation.resultingMemoryId))
      )
        throw new E03RuntimeError(
          "memory_consolidation_restore_plan_reference",
          `memory consolidation ${consolidation.consolidationId} has an invalid reference`,
        );
      consolidations.set(
        consolidation.consolidationId,
        structuredClone(consolidation),
      );
    }
    this.records = records;
    this.conflicts = conflicts;
    this.consolidations = consolidations;
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

  private assertConsolidationRevision(
    consolidation: MemoryConsolidation,
    expected: number,
  ): void {
    if (consolidation.revision !== expected)
      throw new E03RuntimeError(
        "memory_consolidation_stale_revision",
        `memory consolidation ${consolidation.consolidationId} revision is stale`,
      );
  }

  private transitionConsolidation(
    consolidation: MemoryConsolidation,
    patch: Partial<
      Omit<MemoryConsolidation, "consolidationId" | "revision" | "digest">
    >,
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

export interface MemoryReplica {
  replicaId: string;
  taskId: string;
  sessionId: string;
  endpointId: string;
  state: "joining" | "active" | "lagging" | "draining" | "removed";
  acknowledgedRevision: number;
  advertisedRevision: number;
  lastHeartbeatAt: string | null;
  leaseExpiresAt: string;
  joinedAt: string;
  removedAt: string | null;
  revision: number;
  digest: string;
}
export interface MemoryReplicationBatch {
  batchId: string;
  taskId: string;
  sessionId: string;
  sourceRevision: number;
  targetRevision: number;
  recordIds: string[];
  removedRecordIds: string[];
  checksum: string;
  state:
    | "prepared"
    | "published"
    | "partially_acknowledged"
    | "committed"
    | "rejected"
    | "expired";
  requiredReplicaIds: string[];
  acknowledgedReplicaIds: string[];
  rejectedReplicaIds: string[];
  preparedAt: string;
  publishedAt: string | null;
  committedAt: string | null;
  expiresAt: string;
  revision: number;
  digest: string;
}
export interface MemoryReplicationAck {
  ackId: string;
  batchId: string;
  replicaId: string;
  outcome: "applied" | "rejected";
  appliedRevision: number;
  observedChecksum: string;
  reason: string | null;
  acknowledgedAt: string;
  digest: string;
}
function assertMemoryReplica(value: MemoryReplica): void {
  assertDigest(value, "digest", `memory replica ${value.replicaId}`);
  if (
    !value.replicaId ||
    !value.taskId ||
    !value.sessionId ||
    !value.endpointId ||
    !Number.isSafeInteger(value.acknowledgedRevision) ||
    value.acknowledgedRevision < 0 ||
    !Number.isSafeInteger(value.advertisedRevision) ||
    value.advertisedRevision < value.acknowledgedRevision ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.leaseExpiresAt))
  )
    throw new E03RuntimeError(
      "memory_replica",
      `memory replica ${value.replicaId} is invalid`,
    );
}
function assertMemoryReplicationBatch(value: MemoryReplicationBatch): void {
  assertDigest(value, "digest", `memory replication batch ${value.batchId}`);
  if (
    !value.batchId ||
    !value.taskId ||
    !value.sessionId ||
    !Number.isSafeInteger(value.sourceRevision) ||
    value.sourceRevision < 0 ||
    !Number.isSafeInteger(value.targetRevision) ||
    value.targetRevision <= value.sourceRevision ||
    !value.checksum ||
    !value.requiredReplicaIds.length ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.expiresAt))
  )
    throw new E03RuntimeError(
      "memory_replication_batch",
      `memory replication batch ${value.batchId} is invalid`,
    );
  if (value.state === "committed" && value.committedAt === null)
    throw new E03RuntimeError(
      "memory_replication_commit_time",
      `committed memory replication batch ${value.batchId} lacks time`,
    );
}
function assertMemoryReplicationAck(value: MemoryReplicationAck): void {
  assertDigest(value, "digest", `memory replication ACK ${value.ackId}`);
  if (
    !value.ackId ||
    !value.batchId ||
    !value.replicaId ||
    !Number.isSafeInteger(value.appliedRevision) ||
    value.appliedRevision < 0 ||
    !value.observedChecksum ||
    (value.outcome === "rejected" && !value.reason)
  )
    throw new E03RuntimeError(
      "memory_replication_ack",
      `memory replication ACK ${value.ackId} is invalid`,
    );
}
export interface MemoryProtectionPolicy {
  policyId: string;
  namespace: string;
  allowedClassifications: string[];
  encryptedClassifications: string[];
  redactedFields: string[];
  maximumReaders: number;
  keyRotationMs: number;
  state: "draft" | "active" | "deprecated" | "retired";
  version: number;
  createdAt: string;
  activatedAt: string;
  revision: number;
  digest: string;
}

export interface MemoryEncryptionKey {
  keyId: string;
  policyId: string;
  generation: number;
  materialDigest: string;
  state: "proposed" | "active" | "retiring" | "retired" | "revoked";
  createdAt: string;
  activatedAt: string;
  expiresAt: string;
  retiredAt: string;
  revision: number;
  digest: string;
}

export interface MemoryProtectionReceipt {
  receiptId: string;
  policyId: string;
  memoryId: string;
  operation:
    | "classify"
    | "redact"
    | "encrypt"
    | "decrypt"
    | "rekey"
    | "destroy";
  actorId: string;
  classification: string;
  inputDigest: string;
  outputDigest: string;
  keyId: string;
  redactedFields: string[];
  accepted: boolean;
  errorCode: string;
  executedAt: string;
  previousDigest: string;
  digest: string;
}

export interface MemoryProtectionGrant {
  grantId: string;
  policyId: string;
  memoryId: string;
  actorId: string;
  operations: MemoryProtectionReceipt["operation"][];
  maximumUses: number;
  used: number;
  expiresAt: string;
  state: "active" | "exhausted" | "expired" | "revoked";
  createdAt: string;
  revision: number;
  digest: string;
}

export interface MemoryProtectionSnapshot {
  policies: MemoryProtectionPolicy[];
  keys: MemoryEncryptionKey[];
  receipts: MemoryProtectionReceipt[];
  grants: MemoryProtectionGrant[];
  activePolicyByNamespace: [string, string][];
  activeKeyByPolicy: [string, string][];
  activeGrantByMemoryActor: [string, string][];
}

function assertMemoryProtectionPolicy(value: MemoryProtectionPolicy): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.policyId ||
    !value.namespace ||
    !value.allowedClassifications.length ||
    value.maximumReaders < 1 ||
    value.keyRotationMs < 1 ||
    value.version < 1 ||
    new Set(value.allowedClassifications).size !==
      value.allowedClassifications.length ||
    new Set(value.encryptedClassifications).size !==
      value.encryptedClassifications.length ||
    new Set(value.redactedFields).size !== value.redactedFields.length ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "memory_protection_policy_corrupt",
      `memory protection policy ${value.policyId || "<empty>"} is corrupt`,
    );
}

function assertMemoryEncryptionKey(value: MemoryEncryptionKey): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.keyId ||
    !value.policyId ||
    value.generation < 1 ||
    !value.materialDigest ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "memory_encryption_key_corrupt",
      `memory encryption key ${value.keyId || "<empty>"} is corrupt`,
    );
}

function assertMemoryProtectionReceipt(value: MemoryProtectionReceipt): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.receiptId ||
    !value.policyId ||
    !value.memoryId ||
    !value.actorId ||
    !value.classification ||
    !value.inputDigest ||
    new Set(value.redactedFields).size !== value.redactedFields.length ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "memory_protection_receipt_corrupt",
      `memory protection receipt ${value.receiptId || "<empty>"} is corrupt`,
    );
}

function assertMemoryProtectionGrant(value: MemoryProtectionGrant): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.grantId ||
    !value.policyId ||
    !value.memoryId ||
    !value.actorId ||
    !value.operations.length ||
    value.maximumUses < 1 ||
    value.used < 0 ||
    value.used > value.maximumUses ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "memory_protection_grant_corrupt",
      `memory protection grant ${value.grantId || "<empty>"} is corrupt`,
    );
}

export class AgentMemoryProtectionRuntime {
  private policies = new Map<string, MemoryProtectionPolicy>();
  private keys = new Map<string, MemoryEncryptionKey[]>();
  private receipts = new Map<string, MemoryProtectionReceipt[]>();
  private grants = new Map<string, MemoryProtectionGrant>();
  private activePolicyByNamespace = new Map<string, string>();
  private activeKeyByPolicy = new Map<string, string>();
  private activeGrantByMemoryActor = new Map<string, string>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  registerPolicy(input: {
    policyId?: string;
    namespace: string;
    allowedClassifications: readonly string[];
    encryptedClassifications: readonly string[];
    redactedFields: readonly string[];
    maximumReaders: number;
    keyRotationMs: number;
    version: number;
  }): MemoryProtectionPolicy {
    const policyId = input.policyId ?? createId("memory-protection-policy");
    const allowedClassifications = [
      ...new Set(input.allowedClassifications),
    ].sort();
    const encryptedClassifications = [
      ...new Set(input.encryptedClassifications),
    ].sort();
    if (
      encryptedClassifications.some(
        (value) => !allowedClassifications.includes(value),
      )
    )
      throw new E03RuntimeError(
        "memory_protection_encryption_classification",
        "encrypted memory classification must be allowed",
      );
    const payload = {
      policyId,
      namespace: input.namespace,
      allowedClassifications,
      encryptedClassifications,
      redactedFields: [...new Set(input.redactedFields)].sort(),
      maximumReaders: input.maximumReaders,
      keyRotationMs: input.keyRotationMs,
      state: "draft" as const,
      version: input.version,
      createdAt: this.clock.now(),
      activatedAt: "",
      revision: 1,
    };
    const policy = { ...payload, digest: digest(payload) };
    assertMemoryProtectionPolicy(policy);
    this.policies.set(policyId, policy);
    this.keys.set(policyId, []);
    return structuredClone(policy);
  }

  activatePolicy(
    policyId: string,
    expectedRevision: number,
  ): MemoryProtectionPolicy {
    const policy = this.requirePolicy(policyId);
    this.assertPolicyRevision(policy, expectedRevision);
    if (policy.state !== "draft")
      throw new E03RuntimeError(
        "memory_protection_policy_activate_state",
        `memory protection policy ${policyId} is ${policy.state}`,
      );
    const activeId = this.activePolicyByNamespace.get(policy.namespace);
    if (activeId) {
      const active = this.requirePolicy(activeId);
      if (active.version >= policy.version)
        throw new E03RuntimeError(
          "memory_protection_policy_version_regression",
          `memory protection policy ${policyId} is stale`,
        );
      this.transitionPolicy(active, { state: "deprecated" });
    }
    const next = this.transitionPolicy(policy, {
      state: "active",
      activatedAt: this.clock.now(),
    });
    this.activePolicyByNamespace.set(policy.namespace, policyId);
    return next;
  }

  proposeKey(input: {
    keyId?: string;
    policyId: string;
    materialDigest: string;
    expiresAt: string;
  }): MemoryEncryptionKey {
    const policy = this.requirePolicy(input.policyId);
    if (policy.state !== "active")
      throw new E03RuntimeError(
        "memory_encryption_key_policy_inactive",
        `memory protection policy ${policy.policyId} is ${policy.state}`,
      );
    const entries = this.keyEntries(policy.policyId);
    const payload = {
      keyId: input.keyId ?? createId("memory-encryption-key"),
      policyId: policy.policyId,
      generation: entries.length + 1,
      materialDigest: input.materialDigest,
      state: "proposed" as const,
      createdAt: this.clock.now(),
      activatedAt: "",
      expiresAt: input.expiresAt,
      retiredAt: "",
      revision: 1,
    };
    const key = { ...payload, digest: digest(payload) };
    assertMemoryEncryptionKey(key);
    entries.push(key);
    this.keys.set(policy.policyId, entries);
    return structuredClone(key);
  }

  activateKey(keyId: string, expectedRevision: number): MemoryEncryptionKey {
    const key = this.requireKey(keyId);
    this.assertKeyRevision(key, expectedRevision);
    if (
      key.state !== "proposed" ||
      Date.parse(key.expiresAt) <= Date.parse(this.clock.now())
    )
      throw new E03RuntimeError(
        "memory_encryption_key_activate_state",
        `memory encryption key ${keyId} cannot activate`,
      );
    const activeId = this.activeKeyByPolicy.get(key.policyId);
    if (activeId) {
      const active = this.requireKey(activeId);
      this.transitionKey(active, { state: "retiring" });
    }
    const next = this.transitionKey(key, {
      state: "active",
      activatedAt: this.clock.now(),
    });
    this.activeKeyByPolicy.set(key.policyId, keyId);
    return next;
  }

  grant(input: {
    grantId?: string;
    policyId: string;
    memoryId: string;
    actorId: string;
    operations: readonly MemoryProtectionReceipt["operation"][];
    maximumUses: number;
    expiresAt: string;
  }): MemoryProtectionGrant {
    const policy = this.requirePolicy(input.policyId);
    if (policy.state !== "active")
      throw new E03RuntimeError(
        "memory_protection_grant_policy_inactive",
        `memory protection policy ${policy.policyId} is ${policy.state}`,
      );
    const key = this.grantKey(input.memoryId, input.actorId);
    const activeId = this.activeGrantByMemoryActor.get(key);
    if (activeId) return structuredClone(this.requireGrant(activeId));
    const grantId = input.grantId ?? createId("memory-protection-grant");
    const payload = {
      grantId,
      policyId: policy.policyId,
      memoryId: input.memoryId,
      actorId: input.actorId,
      operations: [...new Set(input.operations)].sort(),
      maximumUses: input.maximumUses,
      used: 0,
      expiresAt: input.expiresAt,
      state: "active" as const,
      createdAt: this.clock.now(),
      revision: 1,
    };
    const grant = { ...payload, digest: digest(payload) };
    assertMemoryProtectionGrant(grant);
    this.grants.set(grantId, grant);
    this.activeGrantByMemoryActor.set(key, grantId);
    return structuredClone(grant);
  }

  protect(input: {
    receiptId?: string;
    policyId: string;
    memoryId: string;
    actorId: string;
    operation: MemoryProtectionReceipt["operation"];
    classification: string;
    inputDigest: string;
    outputDigest: string;
    redactedFields?: readonly string[];
    keyId?: string;
  }): MemoryProtectionReceipt {
    const policy = this.requirePolicy(input.policyId);
    if (!policy.allowedClassifications.includes(input.classification))
      throw new E03RuntimeError(
        "memory_protection_classification_denied",
        `memory classification ${input.classification} is denied`,
      );
    const grantId = this.activeGrantByMemoryActor.get(
      this.grantKey(input.memoryId, input.actorId),
    );
    const grant = grantId ? this.requireGrant(grantId) : null;
    if (
      !grant ||
      grant.policyId !== policy.policyId ||
      grant.state !== "active" ||
      !grant.operations.includes(input.operation) ||
      Date.parse(grant.expiresAt) <= Date.parse(this.clock.now())
    )
      throw new E03RuntimeError(
        "memory_protection_grant_denied",
        `memory protection operation ${input.operation} is denied`,
      );
    const requiresKey = ["encrypt", "decrypt", "rekey"].includes(
      input.operation,
    );
    const key = input.keyId ? this.requireKey(input.keyId) : null;
    if (
      requiresKey &&
      (!key ||
        key.policyId !== policy.policyId ||
        !["active", "retiring"].includes(key.state))
    )
      throw new E03RuntimeError(
        "memory_protection_key_invalid",
        `memory protection operation ${input.operation} requires active key`,
      );
    const redactedFields = [...new Set(input.redactedFields ?? [])].sort();
    if (
      input.operation === "redact" &&
      redactedFields.some((field) => !policy.redactedFields.includes(field))
    )
      throw new E03RuntimeError(
        "memory_protection_redaction_field_denied",
        "memory redaction contains undeclared field",
      );
    const entries = this.receiptEntries(input.memoryId);
    const payload = {
      receiptId: input.receiptId ?? createId("memory-protection-receipt"),
      policyId: policy.policyId,
      memoryId: input.memoryId,
      operation: input.operation,
      actorId: input.actorId,
      classification: input.classification,
      inputDigest: input.inputDigest,
      outputDigest: input.outputDigest,
      keyId: key?.keyId ?? "",
      redactedFields,
      accepted: true,
      errorCode: "",
      executedAt: this.clock.now(),
      previousDigest: entries.at(-1)?.digest ?? "",
    };
    const receipt = { ...payload, digest: digest(payload) };
    assertMemoryProtectionReceipt(receipt);
    entries.push(receipt);
    this.receipts.set(input.memoryId, entries);
    const used = grant.used + 1;
    const nextGrant = this.transitionGrant(grant, {
      used,
      state: used >= grant.maximumUses ? "exhausted" : "active",
    });
    if (nextGrant.state === "exhausted")
      this.activeGrantByMemoryActor.delete(
        this.grantKey(grant.memoryId, grant.actorId),
      );
    return structuredClone(receipt);
  }

  revokeGrant(
    grantId: string,
    expectedRevision: number,
  ): MemoryProtectionGrant {
    const grant = this.requireGrant(grantId);
    this.assertGrantRevision(grant, expectedRevision);
    if (grant.state !== "active")
      throw new E03RuntimeError(
        "memory_protection_grant_revoke_state",
        `memory protection grant ${grantId} is ${grant.state}`,
      );
    const next = this.transitionGrant(grant, { state: "revoked" });
    this.activeGrantByMemoryActor.delete(
      this.grantKey(grant.memoryId, grant.actorId),
    );
    return next;
  }

  snapshot(): MemoryProtectionSnapshot {
    return {
      policies: [...this.policies.values()].map((value) =>
        structuredClone(value),
      ),
      keys: [...this.keys.values()]
        .flat()
        .map((value) => structuredClone(value)),
      receipts: [...this.receipts.values()]
        .flat()
        .map((value) => structuredClone(value)),
      grants: [...this.grants.values()].map((value) => structuredClone(value)),
      activePolicyByNamespace: [...this.activePolicyByNamespace.entries()],
      activeKeyByPolicy: [...this.activeKeyByPolicy.entries()],
      activeGrantByMemoryActor: [...this.activeGrantByMemoryActor.entries()],
    };
  }

  restore(snapshot: MemoryProtectionSnapshot): void {
    const policies = new Map<string, MemoryProtectionPolicy>();
    const keys = new Map<string, MemoryEncryptionKey[]>();
    const receipts = new Map<string, MemoryProtectionReceipt[]>();
    const grants = new Map<string, MemoryProtectionGrant>();
    for (const value of snapshot.policies) {
      assertMemoryProtectionPolicy(value);
      policies.set(value.policyId, structuredClone(value));
      keys.set(value.policyId, []);
    }
    for (const value of [...snapshot.keys].sort(
      (a, b) => a.generation - b.generation,
    )) {
      assertMemoryEncryptionKey(value);
      const entries = keys.get(value.policyId);
      if (!entries || value.generation !== entries.length + 1)
        throw new E03RuntimeError(
          "memory_protection_restore_key_order",
          `key ${value.keyId} order invalid`,
        );
      entries.push(structuredClone(value));
    }
    for (const value of snapshot.receipts) {
      assertMemoryProtectionReceipt(value);
      if (!policies.has(value.policyId))
        throw new E03RuntimeError(
          "memory_protection_restore_receipt_policy",
          `receipt ${value.receiptId} invalid`,
        );
      const entries = receipts.get(value.memoryId) ?? [];
      if (value.previousDigest !== (entries.at(-1)?.digest ?? ""))
        throw new E03RuntimeError(
          "memory_protection_restore_receipt_chain",
          `receipt ${value.receiptId} breaks chain`,
        );
      entries.push(structuredClone(value));
      receipts.set(value.memoryId, entries);
    }
    for (const value of snapshot.grants) {
      assertMemoryProtectionGrant(value);
      if (!policies.has(value.policyId) || grants.has(value.grantId))
        throw new E03RuntimeError(
          "memory_protection_restore_grant",
          `grant ${value.grantId} invalid`,
        );
      grants.set(value.grantId, structuredClone(value));
    }
    const activePolicyByNamespace = new Map(snapshot.activePolicyByNamespace);
    const activeKeyByPolicy = new Map(snapshot.activeKeyByPolicy);
    const activeGrantByMemoryActor = new Map(snapshot.activeGrantByMemoryActor);
    if (
      activePolicyByNamespace.size !==
        snapshot.activePolicyByNamespace.length ||
      activeKeyByPolicy.size !== snapshot.activeKeyByPolicy.length ||
      activeGrantByMemoryActor.size !== snapshot.activeGrantByMemoryActor.length
    )
      throw new E03RuntimeError(
        "memory_protection_restore_index_duplicate",
        "memory protection indexes duplicate",
      );
    for (const [namespace, policyId] of activePolicyByNamespace) {
      const value = policies.get(policyId);
      if (!value || value.namespace !== namespace || value.state !== "active")
        throw new E03RuntimeError(
          "memory_protection_restore_policy_index",
          `policy index ${namespace} invalid`,
        );
    }
    for (const [policyId, keyId] of activeKeyByPolicy) {
      const value = keys.get(policyId)?.find((entry) => entry.keyId === keyId);
      if (!value || value.state !== "active")
        throw new E03RuntimeError(
          "memory_protection_restore_key_index",
          `key index ${policyId} invalid`,
        );
    }
    for (const [index, grantId] of activeGrantByMemoryActor) {
      const value = grants.get(grantId);
      if (
        !value ||
        index !== this.grantKey(value.memoryId, value.actorId) ||
        value.state !== "active"
      )
        throw new E03RuntimeError(
          "memory_protection_restore_grant_index",
          `grant index ${index} invalid`,
        );
    }
    this.policies = policies;
    this.keys = keys;
    this.receipts = receipts;
    this.grants = grants;
    this.activePolicyByNamespace = activePolicyByNamespace;
    this.activeKeyByPolicy = activeKeyByPolicy;
    this.activeGrantByMemoryActor = activeGrantByMemoryActor;
  }

  private grantKey(memoryId: string, actorId: string): string {
    return `${memoryId}\u0000${actorId}`;
  }

  private keyEntries(policyId: string): MemoryEncryptionKey[] {
    return this.keys.get(policyId) ?? [];
  }

  private receiptEntries(memoryId: string): MemoryProtectionReceipt[] {
    return this.receipts.get(memoryId) ?? [];
  }

  private requirePolicy(id: string): MemoryProtectionPolicy {
    const value = this.policies.get(id);
    if (!value)
      throw new E03RuntimeError(
        "memory_protection_policy_missing",
        `policy ${id} missing`,
      );
    assertMemoryProtectionPolicy(value);
    return value;
  }

  private requireKey(id: string): MemoryEncryptionKey {
    const value = [...this.keys.values()]
      .flat()
      .find((entry) => entry.keyId === id);
    if (!value)
      throw new E03RuntimeError(
        "memory_encryption_key_missing",
        `key ${id} missing`,
      );
    assertMemoryEncryptionKey(value);
    return value;
  }

  private requireGrant(id: string): MemoryProtectionGrant {
    const value = this.grants.get(id);
    if (!value)
      throw new E03RuntimeError(
        "memory_protection_grant_missing",
        `grant ${id} missing`,
      );
    assertMemoryProtectionGrant(value);
    return value;
  }

  private assertPolicyRevision(
    value: MemoryProtectionPolicy,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "memory_protection_policy_stale_revision",
        `policy ${value.policyId} stale`,
      );
  }

  private assertKeyRevision(
    value: MemoryEncryptionKey,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "memory_encryption_key_stale_revision",
        `key ${value.keyId} stale`,
      );
  }

  private assertGrantRevision(
    value: MemoryProtectionGrant,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "memory_protection_grant_stale_revision",
        `grant ${value.grantId} stale`,
      );
  }

  private transitionPolicy(
    value: MemoryProtectionPolicy,
    patch: Partial<
      Omit<MemoryProtectionPolicy, "policyId" | "revision" | "digest">
    >,
  ): MemoryProtectionPolicy {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      policyId: value.policyId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMemoryProtectionPolicy(next);
    this.policies.set(next.policyId, next);
    return structuredClone(next);
  }

  private transitionKey(
    value: MemoryEncryptionKey,
    patch: Partial<Omit<MemoryEncryptionKey, "keyId" | "revision" | "digest">>,
  ): MemoryEncryptionKey {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      keyId: value.keyId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMemoryEncryptionKey(next);
    const entries = this.keyEntries(value.policyId);
    const index = entries.findIndex((entry) => entry.keyId === value.keyId);
    entries[index] = next;
    this.keys.set(value.policyId, entries);
    return structuredClone(next);
  }

  private transitionGrant(
    value: MemoryProtectionGrant,
    patch: Partial<
      Omit<MemoryProtectionGrant, "grantId" | "revision" | "digest">
    >,
  ): MemoryProtectionGrant {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      grantId: value.grantId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMemoryProtectionGrant(next);
    this.grants.set(next.grantId, next);
    return structuredClone(next);
  }
}

export class AgentMemoryReplicationRuntime {
  private replicas = new Map<string, MemoryReplica>();
  private batches = new Map<string, MemoryReplicationBatch>();
  private acknowledgements = new Map<string, MemoryReplicationAck[]>();
  private records = new Map<string, AgentMemoryRecord>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  index(records: readonly AgentMemoryRecord[]): void {
    const next = new Map<string, AgentMemoryRecord>();
    for (const record of records) {
      assertDigest(record, "digest", `memory ${record.memoryId}`);
      if (next.has(record.memoryId))
        throw new E03RuntimeError(
          "memory_replication_record_duplicate",
          `duplicate memory ${record.memoryId}`,
        );
      next.set(record.memoryId, structuredClone(record));
    }
    this.records = next;
  }
  join(input: {
    taskId: string;
    sessionId: string;
    endpointId: string;
    currentRevision: number;
    ttlMs: number;
  }): MemoryReplica {
    if (
      !input.taskId.trim() ||
      !input.sessionId.trim() ||
      !input.endpointId.trim() ||
      !Number.isSafeInteger(input.currentRevision) ||
      input.currentRevision < 0 ||
      !Number.isSafeInteger(input.ttlMs) ||
      input.ttlMs < 1
    )
      throw new E03RuntimeError(
        "memory_replica_join",
        "memory replica join input is invalid",
      );
    const existing = [...this.replicas.values()].find(
      (value) =>
        value.taskId === input.taskId &&
        value.sessionId === input.sessionId &&
        value.endpointId === input.endpointId &&
        value.state !== "removed",
    );
    if (existing) return structuredClone(existing);
    const payload = {
      replicaId: createId("memory-replica"),
      taskId: input.taskId.trim(),
      sessionId: input.sessionId.trim(),
      endpointId: input.endpointId.trim(),
      state: "joining" as const,
      acknowledgedRevision: input.currentRevision,
      advertisedRevision: input.currentRevision,
      lastHeartbeatAt: null,
      leaseExpiresAt: new Date(
        Date.parse(this.clock.now()) + input.ttlMs,
      ).toISOString(),
      joinedAt: this.clock.now(),
      removedAt: null,
      revision: 1,
    };
    const replica = { ...payload, digest: digest(payload) };
    assertMemoryReplica(replica);
    this.replicas.set(replica.replicaId, replica);
    return structuredClone(replica);
  }
  activate(
    replicaId: string,
    expectedRevision: number,
    advertisedRevision: number,
  ): MemoryReplica {
    const replica = this.requireReplica(replicaId);
    this.assertReplicaRevision(replica, expectedRevision);
    if (replica.state !== "joining" && replica.state !== "lagging")
      throw new E03RuntimeError(
        "memory_replica_activate_state",
        `memory replica ${replicaId} is ${replica.state}`,
      );
    if (
      !Number.isSafeInteger(advertisedRevision) ||
      advertisedRevision < replica.acknowledgedRevision
    )
      throw new E03RuntimeError(
        "memory_replica_revision",
        "memory replica advertised revision is invalid",
      );
    return this.transitionReplica(replica, {
      state:
        advertisedRevision === replica.acknowledgedRevision
          ? "active"
          : "lagging",
      advertisedRevision,
      lastHeartbeatAt: this.clock.now(),
    });
  }
  heartbeat(
    replicaId: string,
    expectedRevision: number,
    ttlMs: number,
    advertisedRevision: number,
  ): MemoryReplica {
    const replica = this.requireReplica(replicaId);
    this.assertReplicaRevision(replica, expectedRevision);
    if (replica.state === "removed" || replica.state === "draining")
      throw new E03RuntimeError(
        "memory_replica_heartbeat_state",
        `memory replica ${replicaId} is ${replica.state}`,
      );
    if (
      !Number.isSafeInteger(ttlMs) ||
      ttlMs < 1 ||
      !Number.isSafeInteger(advertisedRevision) ||
      advertisedRevision < replica.acknowledgedRevision
    )
      throw new E03RuntimeError(
        "memory_replica_heartbeat",
        "memory replica heartbeat is invalid",
      );
    return this.transitionReplica(replica, {
      advertisedRevision,
      state:
        advertisedRevision > replica.acknowledgedRevision
          ? "lagging"
          : replica.state,
      lastHeartbeatAt: this.clock.now(),
      leaseExpiresAt: new Date(
        Date.parse(this.clock.now()) + ttlMs,
      ).toISOString(),
    });
  }
  prepare(input: {
    taskId: string;
    sessionId: string;
    sourceRevision: number;
    targetRevision: number;
    recordIds: readonly string[];
    removedRecordIds?: readonly string[];
    requiredReplicaIds: readonly string[];
    ttlMs: number;
  }): MemoryReplicationBatch {
    const requiredReplicaIds = [...new Set(input.requiredReplicaIds)].sort();
    if (
      !requiredReplicaIds.length ||
      !Number.isSafeInteger(input.sourceRevision) ||
      input.sourceRevision < 0 ||
      !Number.isSafeInteger(input.targetRevision) ||
      input.targetRevision <= input.sourceRevision ||
      !Number.isSafeInteger(input.ttlMs) ||
      input.ttlMs < 1
    )
      throw new E03RuntimeError(
        "memory_replication_prepare",
        "memory replication prepare input is invalid",
      );
    const recordIds = [...new Set(input.recordIds)].sort();
    const removedRecordIds = [...new Set(input.removedRecordIds ?? [])].sort();
    if (recordIds.some((id) => removedRecordIds.includes(id)))
      throw new E03RuntimeError(
        "memory_replication_record_overlap",
        "memory replication records overlap removals",
      );
    const records = recordIds.map((id) => {
      const record = this.records.get(id);
      if (
        !record ||
        record.taskId !== input.taskId ||
        record.sessionId !== input.sessionId
      )
        throw new E03RuntimeError(
          "memory_replication_record_missing",
          `memory ${id} is not available for replication`,
        );
      return record;
    });
    for (const replicaId of requiredReplicaIds) {
      const replica = this.requireReplica(replicaId);
      if (
        replica.taskId !== input.taskId ||
        replica.sessionId !== input.sessionId ||
        (replica.state !== "active" && replica.state !== "lagging")
      )
        throw new E03RuntimeError(
          "memory_replication_replica_state",
          `memory replica ${replicaId} cannot receive batch`,
        );
    }
    const checksum = digest({
      sourceRevision: input.sourceRevision,
      targetRevision: input.targetRevision,
      records: records.map((value) => value.digest),
      removedRecordIds,
    });
    const existing = [...this.batches.values()].find(
      (value) =>
        value.taskId === input.taskId &&
        value.sessionId === input.sessionId &&
        value.sourceRevision === input.sourceRevision &&
        value.targetRevision === input.targetRevision &&
        value.checksum === checksum &&
        value.state !== "rejected" &&
        value.state !== "expired",
    );
    if (existing) return structuredClone(existing);
    const payload = {
      batchId: createId("memory-replication-batch"),
      taskId: input.taskId,
      sessionId: input.sessionId,
      sourceRevision: input.sourceRevision,
      targetRevision: input.targetRevision,
      recordIds,
      removedRecordIds,
      checksum,
      state: "prepared" as const,
      requiredReplicaIds,
      acknowledgedReplicaIds: [],
      rejectedReplicaIds: [],
      preparedAt: this.clock.now(),
      publishedAt: null,
      committedAt: null,
      expiresAt: new Date(
        Date.parse(this.clock.now()) + input.ttlMs,
      ).toISOString(),
      revision: 1,
    };
    const batch = { ...payload, digest: digest(payload) };
    assertMemoryReplicationBatch(batch);
    this.batches.set(batch.batchId, batch);
    return structuredClone(batch);
  }
  publish(batchId: string, expectedRevision: number): MemoryReplicationBatch {
    const batch = this.requireBatch(batchId);
    this.assertBatchRevision(batch, expectedRevision);
    if (batch.state !== "prepared")
      throw new E03RuntimeError(
        "memory_replication_publish_state",
        `memory replication batch ${batchId} is ${batch.state}`,
      );
    if (Date.parse(batch.expiresAt) <= Date.parse(this.clock.now()))
      return this.transitionBatch(batch, { state: "expired" });
    return this.transitionBatch(batch, {
      state: "published",
      publishedAt: this.clock.now(),
    });
  }
  acknowledge(input: {
    batchId: string;
    expectedRevision: number;
    replicaId: string;
    outcome: MemoryReplicationAck["outcome"];
    appliedRevision: number;
    observedChecksum: string;
    reason?: string | null;
  }): {
    batch: MemoryReplicationBatch;
    ack: MemoryReplicationAck;
    replica: MemoryReplica;
  } {
    const batch = this.requireBatch(input.batchId);
    this.assertBatchRevision(batch, input.expectedRevision);
    if (batch.state !== "published" && batch.state !== "partially_acknowledged")
      throw new E03RuntimeError(
        "memory_replication_ack_state",
        `memory replication batch ${batch.batchId} is ${batch.state}`,
      );
    if (!batch.requiredReplicaIds.includes(input.replicaId))
      throw new E03RuntimeError(
        "memory_replication_ack_replica",
        `memory replica ${input.replicaId} is not required`,
      );
    const prior = (this.acknowledgements.get(batch.batchId) ?? []).find(
      (value) => value.replicaId === input.replicaId,
    );
    if (prior)
      return {
        batch: structuredClone(batch),
        ack: structuredClone(prior),
        replica: structuredClone(this.requireReplica(input.replicaId)),
      };
    const valid =
      input.outcome === "applied" &&
      input.appliedRevision === batch.targetRevision &&
      input.observedChecksum === batch.checksum;
    const outcome: MemoryReplicationAck["outcome"] = valid
      ? "applied"
      : "rejected";
    const payload = {
      ackId: createId("memory-replication-ack"),
      batchId: batch.batchId,
      replicaId: input.replicaId,
      outcome,
      appliedRevision: input.appliedRevision,
      observedChecksum: input.observedChecksum,
      reason: valid
        ? null
        : input.reason?.trim() || "replica observation mismatch",
      acknowledgedAt: this.clock.now(),
    };
    const ack = { ...payload, digest: digest(payload) };
    assertMemoryReplicationAck(ack);
    const acks = this.acknowledgements.get(batch.batchId) ?? [];
    acks.push(ack);
    this.acknowledgements.set(batch.batchId, acks);
    const acknowledgedReplicaIds = valid
      ? [...batch.acknowledgedReplicaIds, input.replicaId].sort()
      : batch.acknowledgedReplicaIds;
    const rejectedReplicaIds = valid
      ? batch.rejectedReplicaIds
      : [...batch.rejectedReplicaIds, input.replicaId].sort();
    const complete =
      acknowledgedReplicaIds.length + rejectedReplicaIds.length ===
      batch.requiredReplicaIds.length;
    const nextBatch = this.transitionBatch(batch, {
      state: rejectedReplicaIds.length
        ? "rejected"
        : complete
          ? "committed"
          : "partially_acknowledged",
      acknowledgedReplicaIds,
      rejectedReplicaIds,
      committedAt:
        complete && !rejectedReplicaIds.length ? this.clock.now() : null,
    });
    const replica = this.requireReplica(input.replicaId);
    const nextReplica = this.transitionReplica(
      replica,
      valid
        ? {
            acknowledgedRevision: batch.targetRevision,
            advertisedRevision: Math.max(
              replica.advertisedRevision,
              batch.targetRevision,
            ),
            state: "active",
          }
        : { state: "lagging" },
    );
    return {
      batch: nextBatch,
      ack: structuredClone(ack),
      replica: nextReplica,
    };
  }
  drain(replicaId: string, expectedRevision: number): MemoryReplica {
    const replica = this.requireReplica(replicaId);
    this.assertReplicaRevision(replica, expectedRevision);
    if (replica.state !== "active" && replica.state !== "lagging")
      throw new E03RuntimeError(
        "memory_replica_drain_state",
        `memory replica ${replicaId} is ${replica.state}`,
      );
    return this.transitionReplica(replica, { state: "draining" });
  }
  remove(replicaId: string, expectedRevision: number): MemoryReplica {
    const replica = this.requireReplica(replicaId);
    this.assertReplicaRevision(replica, expectedRevision);
    if (
      replica.state !== "draining" &&
      Date.parse(replica.leaseExpiresAt) > Date.parse(this.clock.now())
    )
      throw new E03RuntimeError(
        "memory_replica_remove_state",
        `memory replica ${replicaId} is ${replica.state}`,
      );
    if (
      [...this.batches.values()].some(
        (value) =>
          value.requiredReplicaIds.includes(replicaId) &&
          (value.state === "prepared" ||
            value.state === "published" ||
            value.state === "partially_acknowledged"),
      )
    )
      throw new E03RuntimeError(
        "memory_replica_pending_batch",
        `memory replica ${replicaId} has a pending batch`,
      );
    return this.transitionReplica(replica, {
      state: "removed",
      removedAt: this.clock.now(),
    });
  }
  snapshot(): {
    replicas: MemoryReplica[];
    batches: MemoryReplicationBatch[];
    acknowledgements: MemoryReplicationAck[];
    records: AgentMemoryRecord[];
  } {
    return {
      replicas: [...this.replicas.values()].map((value) =>
        structuredClone(value),
      ),
      batches: [...this.batches.values()].map((value) =>
        structuredClone(value),
      ),
      acknowledgements: [...this.acknowledgements.values()]
        .flat()
        .map((value) => structuredClone(value)),
      records: [...this.records.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }
  restore(snapshot: {
    replicas: readonly MemoryReplica[];
    batches: readonly MemoryReplicationBatch[];
    acknowledgements: readonly MemoryReplicationAck[];
    records: readonly AgentMemoryRecord[];
  }): void {
    const replicas = new Map<string, MemoryReplica>();
    const batches = new Map<string, MemoryReplicationBatch>();
    const acknowledgements = new Map<string, MemoryReplicationAck[]>();
    this.index(snapshot.records);
    for (const value of snapshot.replicas) {
      assertMemoryReplica(value);
      if (replicas.has(value.replicaId))
        throw new E03RuntimeError(
          "memory_replica_restore_duplicate",
          `duplicate memory replica ${value.replicaId}`,
        );
      replicas.set(value.replicaId, structuredClone(value));
    }
    for (const value of snapshot.batches) {
      assertMemoryReplicationBatch(value);
      if (
        batches.has(value.batchId) ||
        value.requiredReplicaIds.some((id) => !replicas.has(id)) ||
        value.recordIds.some((id) => !this.records.has(id))
      )
        throw new E03RuntimeError(
          "memory_replication_batch_restore",
          `memory replication batch ${value.batchId} is invalid`,
        );
      batches.set(value.batchId, structuredClone(value));
    }
    for (const value of snapshot.acknowledgements) {
      assertMemoryReplicationAck(value);
      if (!batches.has(value.batchId) || !replicas.has(value.replicaId))
        throw new E03RuntimeError(
          "memory_replication_ack_restore",
          `memory replication ACK ${value.ackId} is invalid`,
        );
      const entries = acknowledgements.get(value.batchId) ?? [];
      if (entries.some((entry) => entry.replicaId === value.replicaId))
        throw new E03RuntimeError(
          "memory_replication_ack_restore_duplicate",
          `duplicate memory replication ACK for ${value.replicaId}`,
        );
      entries.push(structuredClone(value));
      acknowledgements.set(value.batchId, entries);
    }
    this.replicas = replicas;
    this.batches = batches;
    this.acknowledgements = acknowledgements;
  }
  private requireReplica(id: string): MemoryReplica {
    const value = this.replicas.get(id);
    if (!value)
      throw new E03RuntimeError(
        "memory_replica_missing",
        `memory replica ${id} does not exist`,
      );
    assertMemoryReplica(value);
    return value;
  }
  private requireBatch(id: string): MemoryReplicationBatch {
    const value = this.batches.get(id);
    if (!value)
      throw new E03RuntimeError(
        "memory_replication_batch_missing",
        `memory replication batch ${id} does not exist`,
      );
    assertMemoryReplicationBatch(value);
    return value;
  }
  private assertReplicaRevision(value: MemoryReplica, expected: number): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "memory_replica_stale_revision",
        `memory replica ${value.replicaId} revision is stale`,
      );
  }
  private assertBatchRevision(
    value: MemoryReplicationBatch,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "memory_replication_batch_stale_revision",
        `memory replication batch ${value.batchId} revision is stale`,
      );
  }
  private transitionReplica(
    value: MemoryReplica,
    patch: Partial<Omit<MemoryReplica, "replicaId" | "revision" | "digest">>,
  ): MemoryReplica {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      replicaId: value.replicaId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMemoryReplica(next);
    this.replicas.set(next.replicaId, next);
    return structuredClone(next);
  }
  private transitionBatch(
    value: MemoryReplicationBatch,
    patch: Partial<
      Omit<MemoryReplicationBatch, "batchId" | "revision" | "digest">
    >,
  ): MemoryReplicationBatch {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      batchId: value.batchId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertMemoryReplicationBatch(next);
    this.batches.set(next.batchId, next);
    return structuredClone(next);
  }
}
