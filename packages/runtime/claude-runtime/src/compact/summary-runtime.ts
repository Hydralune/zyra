import {
  type Clock,
  type IdFactory,
  type JsonRecord,
  RandomIdFactory,
  RuntimeInvariantError,
  SystemClock,
  assertNonEmpty,
  assertNonNegativeInteger,
  clamp,
  compareNumbers,
  compareStrings,
  deepClone,
  digestJson,
  uniqueSorted,
} from "../core/runtime-primitives.js";

export type SummaryItemKind =
  | "objective"
  | "fact"
  | "decision"
  | "constraint"
  | "artifact"
  | "tool_effect"
  | "error"
  | "recovery"
  | "open_loop"
  | "preference";

export type SummaryConfidence = "observed" | "inferred" | "reported";
export type SummaryItemStatus = "active" | "superseded" | "resolved" | "retracted";

export interface SummaryCitation {
  eventId: string;
  transitionId: string | null;
  messageId: string | null;
  artifactId: string | null;
  occurredAt: number;
  digest: string;
}

export interface SummaryItem {
  itemId: string;
  kind: SummaryItemKind;
  subject: string;
  predicate: string;
  value: string;
  normalizedKey: string;
  confidence: SummaryConfidence;
  importance: number;
  status: SummaryItemStatus;
  firstObservedAt: number;
  lastObservedAt: number;
  expiresAt: number | null;
  citations: SummaryCitation[];
  tags: string[];
  supersedes: string[];
  revision: number;
  metadata: JsonRecord;
}

export interface SummarySection {
  sectionId: string;
  title: string;
  kinds: SummaryItemKind[];
  order: number;
  maximumItems: number;
  maximumCharacters: number;
  required: boolean;
}

export interface StructuredSummary {
  summaryId: string;
  sessionId: string;
  runId: string;
  generation: number;
  parentSummaryId: string | null;
  coveredSequenceStart: number;
  coveredSequenceEnd: number;
  sourceDigest: string;
  items: SummaryItem[];
  rendered: string;
  renderedCharacters: number;
  omittedItemIds: string[];
  createdAt: number;
  revision: number;
  checksum: string;
}

export interface SummaryCandidate {
  kind: SummaryItemKind;
  subject: string;
  predicate: string;
  value: string;
  confidence: SummaryConfidence;
  importance: number;
  expiresAt?: number | null;
  citations: SummaryCitation[];
  tags?: string[];
  supersedes?: string[];
  metadata?: JsonRecord;
}

export interface SummaryBuildRequest {
  sessionId: string;
  runId: string;
  coveredSequenceStart: number;
  coveredSequenceEnd: number;
  sourceDigest: string;
  candidates: SummaryCandidate[];
  previousSummaryId?: string | null;
  maximumCharacters: number;
  preserveOpenLoops?: boolean;
  preserveErrors?: boolean;
}

export interface SummaryDiff {
  previousSummaryId: string;
  nextSummaryId: string;
  addedItemIds: string[];
  changedItemIds: string[];
  removedItemIds: string[];
  resolvedItemIds: string[];
}

export interface SummaryRuntimeSnapshot {
  version: "zyra.compact-summary/v1";
  sections: SummarySection[];
  summaries: StructuredSummary[];
  items: SummaryItem[];
  latestBySession: Array<{ sessionId: string; summaryId: string }>;
  checksum: string;
}

export interface SummaryRuntimeOptions {
  clock?: Clock;
  ids?: IdFactory;
  sections?: SummarySection[];
  maximumCitationsPerItem?: number;
}

const DEFAULT_SECTIONS: SummarySection[] = [
  {
    sectionId: "objectives",
    title: "Active objectives",
    kinds: ["objective", "constraint"],
    order: 10,
    maximumItems: 30,
    maximumCharacters: 8_000,
    required: true,
  },
  {
    sectionId: "decisions",
    title: "Decisions and durable facts",
    kinds: ["decision", "fact", "preference"],
    order: 20,
    maximumItems: 80,
    maximumCharacters: 16_000,
    required: true,
  },
  {
    sectionId: "effects",
    title: "Artifacts and tool effects",
    kinds: ["artifact", "tool_effect"],
    order: 30,
    maximumItems: 80,
    maximumCharacters: 16_000,
    required: false,
  },
  {
    sectionId: "recovery",
    title: "Failures and recovery",
    kinds: ["error", "recovery"],
    order: 40,
    maximumItems: 50,
    maximumCharacters: 10_000,
    required: false,
  },
  {
    sectionId: "open-loops",
    title: "Open loops",
    kinds: ["open_loop"],
    order: 50,
    maximumItems: 50,
    maximumCharacters: 10_000,
    required: true,
  },
];

export class CompactSummaryRuntime {
  private readonly clock: Clock;
  private readonly ids: IdFactory;
  private readonly sections: SummarySection[];
  private readonly maximumCitationsPerItem: number;
  private readonly summaries = new Map<string, StructuredSummary>();
  private readonly items = new Map<string, SummaryItem>();
  private readonly latestBySession = new Map<string, string>();

  constructor(options: SummaryRuntimeOptions = {}) {
    this.clock = options.clock ?? new SystemClock();
    this.ids = options.ids ?? new RandomIdFactory();
    this.sections = normalizeSections(options.sections ?? DEFAULT_SECTIONS);
    this.maximumCitationsPerItem = options.maximumCitationsPerItem ?? 16;
    assertNonNegativeInteger(
      this.maximumCitationsPerItem,
      "maximumCitationsPerItem",
    );
  }

  build(request: SummaryBuildRequest): StructuredSummary {
    validateBuildRequest(request);
    const previous = this.resolvePrevious(request);
    const generation = (previous?.generation ?? 0) + 1;
    const merged = new Map<string, SummaryItem>();
    if (previous !== null) {
      for (const item of previous.items) {
        if (!this.isExpired(item)) {
          merged.set(item.normalizedKey, deepClone(item));
        }
      }
    }
    for (const candidate of request.candidates) {
      const normalized = normalizeCandidate(candidate);
      const existing = merged.get(normalized.normalizedKey);
      const item =
        existing === undefined
          ? this.createItem(normalized)
          : this.mergeItem(existing, normalized);
      merged.set(item.normalizedKey, item);
      this.items.set(item.itemId, deepClone(item));
    }
    this.applySupersession(merged);
    const candidates = [...merged.values()].filter(
      (item) => item.status === "active" && !this.isExpired(item),
    );
    const selection = this.selectForBudget(candidates, request);
    const rendered = renderSections(
      selection.selected,
      this.sections,
      request.maximumCharacters,
    );
    const body = {
      summaryId: this.ids.next("compact-summary"),
      sessionId: request.sessionId,
      runId: request.runId,
      generation,
      parentSummaryId: previous?.summaryId ?? null,
      coveredSequenceStart:
        previous === null
          ? request.coveredSequenceStart
          : Math.min(previous.coveredSequenceStart, request.coveredSequenceStart),
      coveredSequenceEnd: request.coveredSequenceEnd,
      sourceDigest: request.sourceDigest,
      items: selection.selected,
      rendered,
      renderedCharacters: rendered.length,
      omittedItemIds: selection.omitted.map((item) => item.itemId),
      createdAt: this.clock.now(),
      revision: generation,
    };
    const summary: StructuredSummary = {
      ...body,
      checksum: digestJson(body),
    };
    this.summaries.set(summary.summaryId, deepClone(summary));
    this.latestBySession.set(summary.sessionId, summary.summaryId);
    return deepClone(summary);
  }

  resolveItem(itemId: string, citation: SummaryCitation): SummaryItem {
    const item = this.requireItem(itemId);
    const next: SummaryItem = {
      ...item,
      status: "resolved",
      lastObservedAt: this.clock.now(),
      citations: mergeCitations(
        item.citations,
        [citation],
        this.maximumCitationsPerItem,
      ),
      revision: item.revision + 1,
    };
    this.items.set(itemId, next);
    return deepClone(next);
  }

  retractItem(
    itemId: string,
    reason: string,
    citation: SummaryCitation,
  ): SummaryItem {
    assertNonEmpty(reason, "reason");
    const item = this.requireItem(itemId);
    const next: SummaryItem = {
      ...item,
      status: "retracted",
      lastObservedAt: this.clock.now(),
      citations: mergeCitations(
        item.citations,
        [citation],
        this.maximumCitationsPerItem,
      ),
      revision: item.revision + 1,
      metadata: { ...item.metadata, retractionReason: reason },
    };
    this.items.set(itemId, next);
    return deepClone(next);
  }

  latest(sessionId: string): StructuredSummary | null {
    const summaryId = this.latestBySession.get(sessionId);
    return summaryId === undefined ? null : this.get(summaryId);
  }

  get(summaryId: string): StructuredSummary {
    const summary = this.summaries.get(summaryId);
    if (summary === undefined) {
      throw new RuntimeInvariantError("unknown_compact_summary", { summaryId });
    }
    verifySummary(summary);
    return deepClone(summary);
  }

  diff(previousSummaryId: string, nextSummaryId: string): SummaryDiff {
    const previous = this.get(previousSummaryId);
    const next = this.get(nextSummaryId);
    if (previous.sessionId !== next.sessionId) {
      throw new RuntimeInvariantError("summary_diff_session_mismatch", {
        previousSessionId: previous.sessionId,
        nextSessionId: next.sessionId,
      });
    }
    const previousByKey = new Map(
      previous.items.map((item) => [item.normalizedKey, item]),
    );
    const nextByKey = new Map(
      next.items.map((item) => [item.normalizedKey, item]),
    );
    const addedItemIds: string[] = [];
    const changedItemIds: string[] = [];
    const removedItemIds: string[] = [];
    const resolvedItemIds: string[] = [];
    for (const [key, item] of nextByKey) {
      const old = previousByKey.get(key);
      if (old === undefined) {
        addedItemIds.push(item.itemId);
      } else if (itemDigest(old) !== itemDigest(item)) {
        changedItemIds.push(item.itemId);
        if (item.status === "resolved" && old.status !== "resolved") {
          resolvedItemIds.push(item.itemId);
        }
      }
    }
    for (const [key, item] of previousByKey) {
      if (!nextByKey.has(key)) {
        removedItemIds.push(item.itemId);
      }
    }
    return {
      previousSummaryId,
      nextSummaryId,
      addedItemIds: addedItemIds.sort(compareStrings),
      changedItemIds: changedItemIds.sort(compareStrings),
      removedItemIds: removedItemIds.sort(compareStrings),
      resolvedItemIds: resolvedItemIds.sort(compareStrings),
    };
  }

  coverage(summaryId: string): {
    citedEvents: number;
    citedTransitions: number;
    citedMessages: number;
    citedArtifacts: number;
    uncitedItems: string[];
  } {
    const summary = this.get(summaryId);
    const events = new Set<string>();
    const transitions = new Set<string>();
    const messages = new Set<string>();
    const artifacts = new Set<string>();
    const uncitedItems: string[] = [];
    for (const item of summary.items) {
      if (item.citations.length === 0) {
        uncitedItems.push(item.itemId);
      }
      for (const citation of item.citations) {
        events.add(citation.eventId);
        if (citation.transitionId !== null) {
          transitions.add(citation.transitionId);
        }
        if (citation.messageId !== null) {
          messages.add(citation.messageId);
        }
        if (citation.artifactId !== null) {
          artifacts.add(citation.artifactId);
        }
      }
    }
    return {
      citedEvents: events.size,
      citedTransitions: transitions.size,
      citedMessages: messages.size,
      citedArtifacts: artifacts.size,
      uncitedItems: uncitedItems.sort(compareStrings),
    };
  }

  snapshot(): SummaryRuntimeSnapshot {
    const body = {
      version: "zyra.compact-summary/v1" as const,
      sections: this.sections.map((section) => deepClone(section)),
      summaries: [...this.summaries.values()]
        .sort((left, right) => compareStrings(left.summaryId, right.summaryId))
        .map((summary) => deepClone(summary)),
      items: [...this.items.values()]
        .sort((left, right) => compareStrings(left.itemId, right.itemId))
        .map((item) => deepClone(item)),
      latestBySession: [...this.latestBySession.entries()]
        .sort(([left], [right]) => compareStrings(left, right))
        .map(([sessionId, summaryId]) => ({ sessionId, summaryId })),
    };
    return { ...body, checksum: digestJson(body) };
  }

  restore(snapshot: SummaryRuntimeSnapshot): void {
    const { checksum, ...body } = snapshot;
    if (snapshot.version !== "zyra.compact-summary/v1") {
      throw new RuntimeInvariantError("unsupported_summary_snapshot", {
        version: snapshot.version,
      });
    }
    if (digestJson(body) !== checksum) {
      throw new RuntimeInvariantError("summary_snapshot_checksum_mismatch");
    }
    const sections = normalizeSections(snapshot.sections);
    if (digestJson(sections) !== digestJson(this.sections)) {
      throw new RuntimeInvariantError("summary_section_configuration_mismatch");
    }
    this.summaries.clear();
    this.items.clear();
    this.latestBySession.clear();
    for (const summary of snapshot.summaries) {
      verifySummary(summary);
      this.summaries.set(summary.summaryId, deepClone(summary));
    }
    for (const item of snapshot.items) {
      this.items.set(item.itemId, deepClone(item));
    }
    for (const latest of snapshot.latestBySession) {
      const summary = this.summaries.get(latest.summaryId);
      if (summary?.sessionId !== latest.sessionId) {
        throw new RuntimeInvariantError("invalid_latest_summary_reference", {
          sessionId: latest.sessionId,
          summaryId: latest.summaryId,
        });
      }
      this.latestBySession.set(latest.sessionId, latest.summaryId);
    }
  }

  private resolvePrevious(request: SummaryBuildRequest): StructuredSummary | null {
    if (
      request.previousSummaryId !== undefined &&
      request.previousSummaryId !== null
    ) {
      const previous = this.get(request.previousSummaryId);
      if (previous.sessionId !== request.sessionId) {
        throw new RuntimeInvariantError("summary_parent_session_mismatch", {
          previousSessionId: previous.sessionId,
          sessionId: request.sessionId,
        });
      }
      if (previous.coveredSequenceEnd >= request.coveredSequenceEnd) {
        throw new RuntimeInvariantError("summary_coverage_not_monotonic", {
          previousEnd: previous.coveredSequenceEnd,
          requestedEnd: request.coveredSequenceEnd,
        });
      }
      return previous;
    }
    return this.latest(request.sessionId);
  }

  private createItem(candidate: NormalizedCandidate): SummaryItem {
    const now = this.clock.now();
    return {
      itemId: this.ids.next("summary-item"),
      kind: candidate.kind,
      subject: candidate.subject,
      predicate: candidate.predicate,
      value: candidate.value,
      normalizedKey: candidate.normalizedKey,
      confidence: candidate.confidence,
      importance: candidate.importance,
      status: "active",
      firstObservedAt: now,
      lastObservedAt: now,
      expiresAt: candidate.expiresAt,
      citations: mergeCitations(
        [],
        candidate.citations,
        this.maximumCitationsPerItem,
      ),
      tags: candidate.tags,
      supersedes: candidate.supersedes,
      revision: 1,
      metadata: candidate.metadata,
    };
  }

  private mergeItem(
    existing: SummaryItem,
    candidate: NormalizedCandidate,
  ): SummaryItem {
    const confidence = strongerConfidence(
      existing.confidence,
      candidate.confidence,
    );
    const changed = existing.value !== candidate.value;
    return {
      ...existing,
      value: candidate.value,
      confidence,
      importance: Math.max(existing.importance, candidate.importance),
      status: "active",
      lastObservedAt: this.clock.now(),
      expiresAt: candidate.expiresAt ?? existing.expiresAt,
      citations: mergeCitations(
        existing.citations,
        candidate.citations,
        this.maximumCitationsPerItem,
      ),
      tags: uniqueSorted([...existing.tags, ...candidate.tags]),
      supersedes: uniqueSorted([
        ...existing.supersedes,
        ...candidate.supersedes,
      ]),
      revision: existing.revision + (changed ? 1 : 0),
      metadata: { ...existing.metadata, ...candidate.metadata },
    };
  }

  private applySupersession(items: Map<string, SummaryItem>): void {
    const byId = new Map(
      [...items.values()].map((item) => [item.itemId, item]),
    );
    for (const item of items.values()) {
      for (const supersededId of item.supersedes) {
        const superseded = byId.get(supersededId);
        if (superseded !== undefined && superseded.status === "active") {
          superseded.status = "superseded";
          superseded.revision += 1;
          this.items.set(superseded.itemId, deepClone(superseded));
        }
      }
    }
  }

  private selectForBudget(
    items: SummaryItem[],
    request: SummaryBuildRequest,
  ): { selected: SummaryItem[]; omitted: SummaryItem[] } {
    const required = items.filter(
      (item) =>
        (request.preserveOpenLoops !== false && item.kind === "open_loop") ||
        (request.preserveErrors === true && item.kind === "error") ||
        item.kind === "objective" ||
        item.kind === "constraint",
    );
    const optional = items.filter((item) => !required.includes(item));
    required.sort(itemComparator);
    optional.sort(itemComparator);
    const selected: SummaryItem[] = [];
    const omitted: SummaryItem[] = [];
    let characters = 0;
    for (const item of [...required, ...optional]) {
      const estimate = estimateRenderedCharacters(item);
      if (characters + estimate <= request.maximumCharacters) {
        selected.push(deepClone(item));
        characters += estimate;
      } else {
        omitted.push(deepClone(item));
      }
    }
    selected.sort(sectionAwareComparator(this.sections));
    return { selected, omitted };
  }

  private isExpired(item: SummaryItem): boolean {
    return item.expiresAt !== null && item.expiresAt <= this.clock.now();
  }

  private requireItem(itemId: string): SummaryItem {
    const item = this.items.get(itemId);
    if (item === undefined) {
      throw new RuntimeInvariantError("unknown_summary_item", { itemId });
    }
    return item;
  }
}

interface NormalizedCandidate extends SummaryCandidate {
  normalizedKey: string;
  expiresAt: number | null;
  tags: string[];
  supersedes: string[];
  metadata: JsonRecord;
}

function normalizeCandidate(candidate: SummaryCandidate): NormalizedCandidate {
  assertNonEmpty(candidate.subject, "candidate.subject");
  assertNonEmpty(candidate.predicate, "candidate.predicate");
  assertNonEmpty(candidate.value, "candidate.value");
  if (candidate.importance < 0 || candidate.importance > 1) {
    throw new RuntimeInvariantError("summary_importance_out_of_range", {
      importance: candidate.importance,
    });
  }
  if (
    candidate.citations.length === 0 &&
    candidate.confidence === "observed"
  ) {
    throw new RuntimeInvariantError("observed_summary_item_requires_citation");
  }
  const normalizedKey = `${candidate.kind}:${normalizeText(
    candidate.subject,
  )}:${normalizeText(candidate.predicate)}`;
  return {
    ...deepClone(candidate),
    normalizedKey,
    importance: clamp(candidate.importance, 0, 1),
    expiresAt: candidate.expiresAt ?? null,
    tags: uniqueSorted(candidate.tags ?? []),
    supersedes: uniqueSorted(candidate.supersedes ?? []),
    metadata: deepClone(candidate.metadata ?? {}),
  };
}

function normalizeText(value: string): string {
  return value.trim().toLowerCase().replace(/\s+/g, " ");
}

function normalizeSections(sections: SummarySection[]): SummarySection[] {
  const ids = new Set<string>();
  const claimedKinds = new Set<SummaryItemKind>();
  const normalized = sections.map((section) => {
    assertNonEmpty(section.sectionId, "sectionId");
    assertNonEmpty(section.title, "title");
    if (ids.has(section.sectionId)) {
      throw new RuntimeInvariantError("duplicate_summary_section", {
        sectionId: section.sectionId,
      });
    }
    ids.add(section.sectionId);
    for (const kind of section.kinds) {
      if (claimedKinds.has(kind)) {
        throw new RuntimeInvariantError("summary_kind_in_multiple_sections", {
          kind,
        });
      }
      claimedKinds.add(kind);
    }
    assertNonNegativeInteger(section.maximumItems, "maximumItems");
    assertNonNegativeInteger(
      section.maximumCharacters,
      "maximumCharacters",
    );
    return { ...deepClone(section), kinds: [...section.kinds] };
  });
  return normalized.sort(
    (left, right) =>
      compareNumbers(left.order, right.order) ||
      compareStrings(left.sectionId, right.sectionId),
  );
}

function validateBuildRequest(request: SummaryBuildRequest): void {
  assertNonEmpty(request.sessionId, "sessionId");
  assertNonEmpty(request.runId, "runId");
  assertNonEmpty(request.sourceDigest, "sourceDigest");
  assertNonNegativeInteger(
    request.coveredSequenceStart,
    "coveredSequenceStart",
  );
  assertNonNegativeInteger(request.coveredSequenceEnd, "coveredSequenceEnd");
  if (request.coveredSequenceStart > request.coveredSequenceEnd) {
    throw new RuntimeInvariantError("summary_coverage_range_invalid", {
      start: request.coveredSequenceStart,
      end: request.coveredSequenceEnd,
    });
  }
  assertNonNegativeInteger(request.maximumCharacters, "maximumCharacters");
}

function mergeCitations(
  existing: SummaryCitation[],
  incoming: SummaryCitation[],
  maximum: number,
): SummaryCitation[] {
  const byDigest = new Map<string, SummaryCitation>();
  for (const citation of [...existing, ...incoming]) {
    assertNonEmpty(citation.eventId, "citation.eventId");
    assertNonEmpty(citation.digest, "citation.digest");
    byDigest.set(citation.digest, deepClone(citation));
  }
  return [...byDigest.values()]
    .sort(
      (left, right) =>
        compareNumbers(right.occurredAt, left.occurredAt) ||
        compareStrings(left.digest, right.digest),
    )
    .slice(0, maximum);
}

function strongerConfidence(
  left: SummaryConfidence,
  right: SummaryConfidence,
): SummaryConfidence {
  const rank: Record<SummaryConfidence, number> = {
    reported: 1,
    inferred: 2,
    observed: 3,
  };
  return rank[left] >= rank[right] ? left : right;
}

function itemComparator(left: SummaryItem, right: SummaryItem): number {
  return (
    compareNumbers(right.importance, left.importance) ||
    compareNumbers(right.lastObservedAt, left.lastObservedAt) ||
    compareStrings(left.normalizedKey, right.normalizedKey)
  );
}

function sectionAwareComparator(
  sections: SummarySection[],
): (left: SummaryItem, right: SummaryItem) => number {
  const order = new Map<SummaryItemKind, number>();
  for (const section of sections) {
    for (const kind of section.kinds) {
      order.set(kind, section.order);
    }
  }
  return (left, right) =>
    compareNumbers(
      order.get(left.kind) ?? 1_000,
      order.get(right.kind) ?? 1_000,
    ) || itemComparator(left, right);
}

function estimateRenderedCharacters(item: SummaryItem): number {
  return item.subject.length + item.predicate.length + item.value.length + 12;
}

function renderSections(
  items: SummaryItem[],
  sections: SummarySection[],
  maximumCharacters: number,
): string {
  const lines: string[] = [];
  for (const section of sections) {
    const sectionItems = items
      .filter((item) => section.kinds.includes(item.kind))
      .slice(0, section.maximumItems);
    if (sectionItems.length === 0 && !section.required) {
      continue;
    }
    lines.push(`## ${section.title}`);
    let sectionCharacters = 0;
    for (const item of sectionItems) {
      const line = `- [${item.kind}] ${item.subject} ${item.predicate}: ${item.value}`;
      if (sectionCharacters + line.length > section.maximumCharacters) {
        break;
      }
      lines.push(line);
      sectionCharacters += line.length;
    }
    lines.push("");
  }
  return lines.join("\n").trim().slice(0, maximumCharacters);
}

function itemDigest(item: SummaryItem): string {
  return digestJson({
    normalizedKey: item.normalizedKey,
    value: item.value,
    confidence: item.confidence,
    importance: item.importance,
    status: item.status,
    citations: item.citations,
    tags: item.tags,
    supersedes: item.supersedes,
  });
}

function verifySummary(summary: StructuredSummary): void {
  const { checksum, ...body } = summary;
  if (digestJson(body) !== checksum) {
    throw new RuntimeInvariantError("compact_summary_checksum_mismatch", {
      summaryId: summary.summaryId,
    });
  }
}
