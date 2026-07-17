import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicId, digest, monotonicNow, normalizeName } from "../e02/index.ts";
import type { SkillDescriptor, SkillRegistrySnapshot } from "./contracts-v2.ts";

export interface SkillSearchDocument {
  skillId: string;
  registryRevision: number;
  descriptorDigest: string;
  name: string;
  aliases: string[];
  tags: string[];
  sourceKind: string;
  pluginId: string | null;
  searchableText: string;
  terms: Record<string, number>;
  length: number;
  indexedAt: string;
  metadata: JsonObject;
}

export interface SkillSearchQuery {
  text: string;
  tags?: string[];
  sourceKinds?: string[];
  pluginIds?: string[];
  includeUnavailable?: boolean;
  maximumResults?: number;
  minimumScore?: number;
  metadata?: JsonObject;
}

export interface SkillSearchHit {
  hitId: string;
  skillId: string;
  name: string;
  score: number;
  lexicalScore: number;
  exactName: boolean;
  aliasMatch: boolean;
  matchedTerms: string[];
  missingTerms: string[];
  descriptorDigest: string;
  registryRevision: number;
  highlights: string[];
  metadata: JsonObject;
}

export interface SkillSearchResult {
  queryId: string;
  query: SkillSearchQuery;
  registryRevision: number;
  indexRevision: number;
  hits: SkillSearchHit[];
  searchedDocuments: number;
  filteredDocuments: number;
  queryTerms: string[];
  completedAt: string;
  digest: string;
}

export interface SkillSearchSnapshot {
  version: "zyra.skill-search-runtime/v1";
  indexRevision: number;
  registryRevision: number;
  documents: SkillSearchDocument[];
  documentFrequency: Record<string, number>;
  averageLength: number;
  digest: string;
  capturedAt: string;
}

export class SkillSearchRuntime {
  private readonly documents = new Map<string, SkillSearchDocument>();
  private readonly documentFrequency = new Map<string, number>();
  private readonly now: () => Date;
  private indexRevision = 0;
  private registryRevision = 0;
  private averageLength = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; snapshot?: SkillSearchSnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  rebuild(registry: SkillRegistrySnapshot): SkillSearchSnapshot {
    if (registry.revision < this.registryRevision) throw new Error(`skill search cannot regress registry revision from ${this.registryRevision} to ${registry.revision}`);
    const documents = new Map<string, SkillSearchDocument>();
    for (const descriptor of registry.activeSkills) documents.set(descriptor.skillId, this.document(descriptor, registry.revision));
    this.documents.clear();
    for (const [id, document] of documents) this.documents.set(id, document);
    this.registryRevision = registry.revision;
    this.indexRevision += 1;
    this.recalculateStatistics();
    return this.snapshot();
  }

  update(descriptorValue: SkillDescriptor, registryRevision: number): SkillSearchDocument {
    if (registryRevision < this.registryRevision) throw new Error(`skill search update revision ${registryRevision} is stale`);
    const descriptor = cloneJson(descriptorValue);
    const document = this.document(descriptor, registryRevision);
    this.documents.set(descriptor.skillId, document);
    this.registryRevision = registryRevision;
    this.indexRevision += 1;
    this.recalculateStatistics();
    return cloneJson(document);
  }

  remove(skillId: string, registryRevision: number): boolean {
    if (registryRevision < this.registryRevision) throw new Error(`skill search removal revision ${registryRevision} is stale`);
    const removed = this.documents.delete(skillId);
    this.registryRevision = registryRevision;
    if (removed) {
      this.indexRevision += 1;
      this.recalculateStatistics();
    }
    return removed;
  }

  search(queryValue: SkillSearchQuery): SkillSearchResult {
    const query = normalizeQuery(queryValue);
    const queryTerms = tokenize(query.text);
    const normalizedQuery = normalizeName(query.text);
    const filtered = [...this.documents.values()].filter((document) => matchesFilters(document, query));
    const hits: SkillSearchHit[] = [];
    for (const document of filtered) {
      const exactName = normalizeName(document.name) === normalizedQuery;
      const aliasMatch = document.aliases.some((alias) => normalizeName(alias) === normalizedQuery);
      const matchedTerms = queryTerms.filter((term) => document.terms[term] !== undefined);
      const missingTerms = queryTerms.filter((term) => document.terms[term] === undefined);
      const lexicalScore = this.bm25(document, queryTerms);
      const searchablePhrase = document.searchableText.trim().normalize("NFKC");
      const phraseBonus = normalizedQuery && searchablePhrase.includes(normalizedQuery) ? 2.5 : 0;
      const score = lexicalScore + (exactName ? 20 : 0) + (aliasMatch ? 12 : 0) + phraseBonus + matchedTerms.length * 0.25;
      if (score < query.minimumScore!) continue;
      const base = {
        skillId: document.skillId,
        name: document.name,
        score: Number(score.toFixed(8)),
        lexicalScore: Number(lexicalScore.toFixed(8)),
        exactName,
        aliasMatch,
        matchedTerms,
        missingTerms,
        descriptorDigest: document.descriptorDigest,
        registryRevision: document.registryRevision,
        highlights: highlights(document.searchableText, matchedTerms),
        metadata: { source_kind: document.sourceKind, plugin_id: document.pluginId },
      };
      hits.push({ hitId: deterministicId("skill-search-hit", base, 32), ...base });
    }
    hits.sort((left, right) => right.score - left.score || left.name.localeCompare(right.name) || left.skillId.localeCompare(right.skillId));
    const resultBase = {
      query,
      registryRevision: this.registryRevision,
      indexRevision: this.indexRevision,
      hits: hits.slice(0, query.maximumResults),
      searchedDocuments: this.documents.size,
      filteredDocuments: filtered.length,
      queryTerms,
      completedAt: this.timestamp(),
    };
    const queryId = deterministicId("skill-search-query", { query, registry_revision: this.registryRevision, index_revision: this.indexRevision }, 40);
    return { queryId, ...resultBase, digest: digest({ queryId, ...resultBase }) };
  }

  suggest(prefixValue: string, maximumResults = 20): SkillSearchHit[] {
    const rawPrefix = prefixValue.trim().normalize("NFKC");
    if (!rawPrefix) return [];
    const prefix = normalizeName(rawPrefix);
    const candidates: SkillSearchHit[] = [];
    for (const document of this.documents.values()) {
      const name = normalizeName(document.name);
      const alias = document.aliases.find((value) => normalizeName(value).startsWith(prefix));
      if (!name.startsWith(prefix) && !alias) continue;
      const base = {
        skillId: document.skillId,
        name: document.name,
        score: name === prefix ? 100 : alias ? 60 : 40 - Math.min(20, name.length - prefix.length),
        lexicalScore: 0,
        exactName: name === prefix,
        aliasMatch: Boolean(alias),
        matchedTerms: [prefix],
        missingTerms: [],
        descriptorDigest: document.descriptorDigest,
        registryRevision: document.registryRevision,
        highlights: [alias ?? document.name],
        metadata: { suggestion: true },
      };
      candidates.push({ hitId: deterministicId("skill-search-suggestion", base, 32), ...base });
    }
    return candidates.sort((left, right) => right.score - left.score || left.name.localeCompare(right.name)).slice(0, maximumResults);
  }

  snapshot(): SkillSearchSnapshot {
    const withoutDigest = {
      version: "zyra.skill-search-runtime/v1" as const,
      indexRevision: this.indexRevision,
      registryRevision: this.registryRevision,
      documents: [...this.documents.values()].sort((left, right) => left.skillId.localeCompare(right.skillId)).map(cloneJson),
      documentFrequency: Object.fromEntries([...this.documentFrequency.entries()].sort(([left], [right]) => left.localeCompare(right))),
      averageLength: this.averageLength,
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: digest(withoutDigest) };
  }

  restore(snapshot: SkillSearchSnapshot): void {
    if (snapshot.version !== "zyra.skill-search-runtime/v1") throw new Error("unsupported skill search snapshot version");
    const { digest: expected, ...withoutDigest } = snapshot;
    if (digest(withoutDigest) !== expected) throw new Error("skill search snapshot digest mismatch");
    this.documents.clear();
    this.documentFrequency.clear();
    this.indexRevision = snapshot.indexRevision;
    this.registryRevision = snapshot.registryRevision;
    this.averageLength = snapshot.averageLength;
    for (const document of snapshot.documents) this.documents.set(document.skillId, cloneJson(document));
    for (const [term, count] of Object.entries(snapshot.documentFrequency)) this.documentFrequency.set(term, count);
    const recalculated = statistics(this.documents.values());
    if (digest(recalculated.frequency) !== digest(snapshot.documentFrequency) || recalculated.averageLength !== snapshot.averageLength) throw new Error("skill search restored statistics mismatch");
  }

  private document(descriptor: SkillDescriptor, registryRevision: number): SkillSearchDocument {
    const searchableText = [descriptor.name, descriptor.displayName, descriptor.description, descriptor.tags.join(" "), descriptor.aliases.join(" "), descriptor.body.slice(0, 64_000)].join("\n");
    const terms: Record<string, number> = {};
    for (const term of tokenize(searchableText)) terms[term] = (terms[term] ?? 0) + 1;
    const length = Object.values(terms).reduce((total, value) => total + value, 0);
    return {
      skillId: descriptor.skillId,
      registryRevision,
      descriptorDigest: descriptor.descriptorDigest,
      name: descriptor.name,
      aliases: [...descriptor.aliases],
      tags: [...descriptor.tags],
      sourceKind: descriptor.source.sourceKind,
      pluginId: descriptor.source.pluginId,
      searchableText,
      terms,
      length,
      indexedAt: this.timestamp(),
      metadata: { availability: descriptor.availability, version: descriptor.version },
    };
  }

  private recalculateStatistics(): void {
    const value = statistics(this.documents.values());
    this.documentFrequency.clear();
    for (const [term, count] of Object.entries(value.frequency)) this.documentFrequency.set(term, count);
    this.averageLength = value.averageLength;
  }

  private bm25(document: SkillSearchDocument, queryTerms: string[]): number {
    const count = Math.max(1, this.documents.size);
    const k1 = 1.5;
    const b = 0.75;
    let score = 0;
    for (const term of queryTerms) {
      const tf = document.terms[term] ?? 0;
      if (!tf) continue;
      const df = this.documentFrequency.get(term) ?? 0;
      const idf = Math.log(1 + (count - df + 0.5) / (df + 0.5));
      const denominator = tf + k1 * (1 - b + b * document.length / Math.max(1, this.averageLength));
      score += idf * (tf * (k1 + 1)) / denominator;
    }
    return score;
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function normalizeQuery(value: SkillSearchQuery): SkillSearchQuery {
  const query = cloneJson(value);
  query.text = query.text?.trim() ?? "";
  query.tags = [...new Set(query.tags ?? [])].sort();
  query.sourceKinds = [...new Set(query.sourceKinds ?? [])].sort();
  query.pluginIds = [...new Set(query.pluginIds ?? [])].sort();
  query.maximumResults = Math.max(1, Math.min(query.maximumResults ?? 50, 1_000));
  query.minimumScore = Math.max(0, query.minimumScore ?? 0.01);
  query.metadata = cloneJson(query.metadata ?? {});
  return query;
}

function matchesFilters(document: SkillSearchDocument, query: SkillSearchQuery): boolean {
  if (!query.includeUnavailable && document.metadata.availability !== "available") return false;
  if (query.tags!.length && !query.tags!.every((tag) => document.tags.includes(tag))) return false;
  if (query.sourceKinds!.length && !query.sourceKinds!.includes(document.sourceKind)) return false;
  if (query.pluginIds!.length && (!document.pluginId || !query.pluginIds!.includes(document.pluginId))) return false;
  return true;
}

function tokenize(value: string): string[] {
  const normalized = value.normalize("NFKC").toLowerCase();
  const output: string[] = [];
  for (const match of normalized.matchAll(/[\p{L}\p{N}][\p{L}\p{N}_.:-]*/gu)) {
    const term = match[0];
    output.push(term);
    if (/^[a-z0-9_.:-]+$/.test(term)) {
      for (const part of term.split(/[_.:-]+/)) if (part.length > 1 && part !== term) output.push(part);
    }
  }
  return output;
}

function statistics(documents: Iterable<SkillSearchDocument>): { frequency: Record<string, number>; averageLength: number } {
  const frequency: Record<string, number> = {};
  let totalLength = 0;
  let count = 0;
  for (const document of documents) {
    count += 1;
    totalLength += document.length;
    for (const term of Object.keys(document.terms)) frequency[term] = (frequency[term] ?? 0) + 1;
  }
  return { frequency, averageLength: count ? totalLength / count : 0 };
}

function highlights(text: string, terms: string[]): string[] {
  const lines = text.split(/\r?\n/);
  const output: string[] = [];
  for (const line of lines) {
    const normalized = line.toLowerCase();
    if (terms.some((term) => normalized.includes(term))) output.push(line.slice(0, 240));
    if (output.length >= 3) break;
  }
  return output;
}
