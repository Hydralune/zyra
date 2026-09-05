import { createHash } from "node:crypto";
import { normalizedIntentWeights } from "./intent.ts";
import { temporalRecency } from "./temporal.ts";
import type {
  RankedCandidate,
  RankingRequest,
  RankingResult,
  RetrievalCandidate,
  RetrievalVoice,
  VoiceScore,
} from "./types.ts";

interface VoiceCandidate {
  readonly candidate: RetrievalCandidate;
  readonly rawScore: number;
}

// Cropped from Mnemopi core/mmr.ts and PolyphonicRecallEngine.combineVoices.
// Zyra adds stable document-id tie breaks and accepts caller-supplied voices so
// this package cannot become a database, embedding, or hydration owner.

interface MutableAggregate {
  readonly candidate: RetrievalCandidate;
  score: number;
  readonly voices: VoiceScore[];
}

function clamp(value: number, minimum = 0, maximum = 1): number {
  if (!Number.isFinite(value)) return minimum;
  return Math.max(minimum, Math.min(maximum, value));
}

function normalizedImportance(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 0;
  return value / (1 + value);
}

function words(value: string): Set<string> {
  const normalized = value.normalize("NFKC").toLocaleLowerCase();
  const tokens = normalized.match(/[\p{L}\p{N}_-]+/gu) ?? [];
  return new Set(tokens.map((token) => token.replace(/^[-_]+|[-_]+$/g, "")).filter((token) => token.length >= 2));
}

export function lexicalSimilarity(left: string, right: string): number {
  return tokenSimilarity(words(left), words(right));
}

function tokenSimilarity(leftWords: ReadonlySet<string>, rightWords: ReadonlySet<string>): number {
  if (leftWords.size === 0 || rightWords.size === 0) return 0;
  let intersection = 0;
  for (const word of leftWords) if (rightWords.has(word)) intersection += 1;
  return intersection / (leftWords.size + rightWords.size - intersection);
}

function deterministicCandidates(values: readonly RetrievalCandidate[]): RetrievalCandidate[] {
  const byVoiceAndId = new Map<string, RetrievalCandidate>();
  for (const candidate of values) {
    const key = `${candidate.sourceVoice}\0${candidate.documentId}`;
    const existing = byVoiceAndId.get(key);
    if (existing === undefined || candidate.score > existing.score) byVoiceAndId.set(key, candidate);
  }
  return [...byVoiceAndId.values()].sort((left, right) => {
    const voice = left.sourceVoice.localeCompare(right.sourceVoice);
    if (voice !== 0) return voice;
    const score = right.score - left.score;
    if (score !== 0) return score;
    return left.documentId.localeCompare(right.documentId);
  });
}

function voiceRows(
  candidates: readonly RetrievalCandidate[],
  voice: RetrievalVoice,
  score: (candidate: RetrievalCandidate) => number,
): VoiceCandidate[] {
  const best = new Map<string, VoiceCandidate>();
  for (const candidate of candidates) {
    const rawScore = score(candidate);
    if (!Number.isFinite(rawScore) || rawScore <= 0) continue;
    const existing = best.get(candidate.documentId);
    if (existing === undefined || rawScore > existing.rawScore) best.set(candidate.documentId, { candidate, rawScore });
  }
  return [...best.values()].sort((left, right) => {
    const difference = right.rawScore - left.rawScore;
    return difference === 0 ? left.candidate.documentId.localeCompare(right.candidate.documentId) : difference;
  });
}

function fuseVoice(
  aggregates: Map<string, MutableAggregate>,
  rows: readonly VoiceCandidate[],
  voice: RetrievalVoice,
  weight: number,
  rankConstant = 60,
): void {
  if (weight <= 0) return;
  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index];
    if (!row) continue;
    const rank = index + 1;
    const contribution = weight / (rankConstant + rank);
    const aggregate = aggregates.get(row.candidate.documentId) ?? {
      candidate: row.candidate,
      score: 0,
      voices: [],
    };
    aggregate.score += contribution;
    aggregate.voices.push({ voice, score: contribution, rank, rawScore: row.rawScore, weight });
    aggregates.set(row.candidate.documentId, aggregate);
  }
}

function fuseCandidates(request: RankingRequest): MutableAggregate[] {
  const candidates = deterministicCandidates(request.candidates);
  const intent = request.intent;
  const [vectorWeight, ftsWeight, importanceWeight] = normalizedIntentWeights(intent);
  const aggregates = new Map<string, MutableAggregate>();
  fuseVoice(aggregates, voiceRows(candidates.filter((item) => item.sourceVoice === "fts"), "fts", (item) => item.score), "fts", ftsWeight);
  fuseVoice(aggregates, voiceRows(candidates.filter((item) => item.sourceVoice === "vector"), "vector", (item) => item.score), "vector", vectorWeight);
  fuseVoice(aggregates, voiceRows(candidates, "importance", (item) => normalizedImportance(item.importance)), "importance", importanceWeight);
  const recencyWeight = intent.category === "temporal" ? 0.1 : 0.03;
  fuseVoice(aggregates, voiceRows(candidates, "recency", (item) => temporalRecency(item.eventAt, request.requestTime)), "recency", recencyWeight);
  return [...aggregates.values()].sort((left, right) => {
    const score = right.score - left.score;
    return score === 0 ? left.candidate.documentId.localeCompare(right.candidate.documentId) : score;
  });
}

function mmrRerank(
  values: readonly MutableAggregate[],
  limit: number,
  lambdaValue: number,
): MutableAggregate[] {
  // Tokenize each document once, and compare each pair only once. Updating
  // the running maximum preserves the original MMR ordering and tie breaks.
  const remaining = values.map((value) => ({
    value,
    tokens: words(`${value.candidate.title}\n${value.candidate.content}`),
    maximumSimilarity: 0,
  }));
  const selected: MutableAggregate[] = [];
  const safeLimit = Math.max(0, Math.trunc(limit));
  const lambda = clamp(lambdaValue);
  while (remaining.length > 0 && selected.length < safeLimit) {
    let bestIndex = 0;
    let bestScore = Number.NEGATIVE_INFINITY;
    let bestIdentity = "";
    for (let index = 0; index < remaining.length; index += 1) {
      const candidate = remaining[index];
      if (!candidate) continue;
      const score = lambda * candidate.value.score - (1 - lambda) * candidate.maximumSimilarity;
      if (score > bestScore || (score === bestScore && candidate.value.candidate.documentId < bestIdentity)) {
        bestIndex = index;
        bestScore = score;
        bestIdentity = candidate.value.candidate.documentId;
      }
    }
    const chosen = remaining.splice(bestIndex, 1)[0];
    if (chosen) {
      selected.push(chosen.value);
      for (const candidate of remaining) {
        candidate.maximumSimilarity = Math.max(candidate.maximumSimilarity, tokenSimilarity(candidate.tokens, chosen.tokens));
      }
    }
  }
  return selected;
}

function applyOutputBudget(
  values: readonly MutableAggregate[],
  maximumCharacters: number,
  maximumDocumentCharacters: number,
  limit: number,
): { readonly hits: RankedCandidate[]; readonly truncated: boolean } {
  const hits: RankedCandidate[] = [];
  let consumed = 0;
  let truncated = false;
  for (const value of values) {
    let content = value.candidate.content.slice(0, maximumDocumentCharacters);
    const overhead = value.candidate.title.length + 128;
    if (hits.length > 0 && consumed + overhead + content.length > maximumCharacters) {
      truncated = true;
      break;
    }
    if (hits.length === 0 && overhead + content.length > maximumCharacters) {
      content = content.slice(0, Math.max(0, maximumCharacters - overhead));
      truncated = content.length < value.candidate.content.length;
    }
    hits.push({
      documentId: value.candidate.documentId,
      score: value.score,
      rank: hits.length + 1,
      content,
      voices: [...value.voices].sort((left, right) => left.voice.localeCompare(right.voice) || left.rank - right.rank),
    });
    consumed += overhead + content.length;
    if (hits.length >= limit) {
      truncated = truncated || values.length > hits.length;
      break;
    }
  }
  return { hits, truncated };
}

function digest(value: unknown): string {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

export function rankRetrievalCandidates(request: RankingRequest): RankingResult {
  validateRankingRequest(request);
  const intent = request.intent;
  const temporal = request.temporal;
  const fused = fuseCandidates(request);
  const diverse = mmrRerank(fused, request.budget.candidateLimit, request.budget.mmrLambda);
  const budgeted = applyOutputBudget(
    diverse,
    request.budget.maximumOutputCharacters,
    request.budget.maximumDocumentCharacters,
    request.budget.limit,
  );
  const evidence = {
    requestId: request.query,
    requestTime: request.requestTime,
    intent,
    temporal,
    candidates: deterministicCandidates(request.candidates).map((candidate) => ({
      documentId: candidate.documentId,
      sourceVoice: candidate.sourceVoice,
      score: candidate.score,
      contentDigest: candidate.contentDigest,
    })),
    hits: budgeted.hits,
  };
  return {
    intent,
    temporal,
    hits: budgeted.hits,
    candidateCount: new Set(request.candidates.map((candidate) => candidate.documentId)).size,
    fusedCount: fused.length,
    truncated: budgeted.truncated,
    evidenceDigest: digest(evidence),
  };
}

function validateRankingRequest(request: RankingRequest): void {
  if (!request.query.trim()) throw new Error("query is required");
  if (!Number.isFinite(Date.parse(request.requestTime))) throw new Error("requestTime must be a valid timestamp");
  const budget = request.budget;
  if (!Number.isSafeInteger(budget.limit) || budget.limit < 0 || budget.limit > 1000) throw new Error("limit out of range");
  if (!Number.isSafeInteger(budget.candidateLimit) || budget.candidateLimit < budget.limit || budget.candidateLimit > 10000) throw new Error("candidateLimit out of range");
  if (!Number.isSafeInteger(budget.maximumOutputCharacters) || budget.maximumOutputCharacters < 0) throw new Error("maximumOutputCharacters out of range");
  if (!Number.isSafeInteger(budget.maximumDocumentCharacters) || budget.maximumDocumentCharacters < 0) throw new Error("maximumDocumentCharacters out of range");
  if (!Number.isFinite(budget.mmrLambda) || budget.mmrLambda < 0 || budget.mmrLambda > 1) throw new Error("mmrLambda out of range");
  const identities = new Set<string>();
  for (const candidate of request.candidates) {
    if (!candidate.documentId.trim()) throw new Error("candidate documentId is required");
    if (!Number.isFinite(candidate.score)) throw new Error("candidate score must be finite");
    if (!Number.isFinite(candidate.importance)) throw new Error("candidate importance must be finite");
    if (!candidate.contentDigest.trim()) throw new Error("candidate contentDigest is required");
    const identity = `${candidate.sourceVoice}\0${candidate.documentId}`;
    if (identities.has(identity)) throw new Error(`duplicate candidate identity: ${candidate.documentId}/${candidate.sourceVoice}`);
    identities.add(identity);
  }
}
