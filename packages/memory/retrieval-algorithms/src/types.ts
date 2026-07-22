export const RETRIEVAL_ALGORITHM_PROTOCOL = "zyra.retrieval-algorithms.v1" as const;

export type QueryIntentCategory =
  | "temporal"
  | "factual"
  | "entity"
  | "preference"
  | "procedural"
  | "general";

export type RetrievalVoice = "fts" | "vector" | "importance" | "recency" | "exact";

export interface QueryIntent {
  readonly category: QueryIntentCategory;
  readonly confidence: number;
  readonly signals: readonly QueryIntentCategory[];
  readonly vectorBias: number;
  readonly ftsBias: number;
  readonly importanceBias: number;
}

export interface TemporalConstraint {
  readonly startAt: string;
  readonly endAt: string;
  readonly tags: readonly string[];
  readonly precision: "day" | "week" | "month" | "year" | "relative" | "unknown";
  readonly sourceText: string;
}

export interface RetrievalBudget {
  readonly limit: number;
  readonly candidateLimit: number;
  readonly maximumOutputCharacters: number;
  readonly maximumDocumentCharacters: number;
  readonly mmrLambda: number;
}

export interface RetrievalCandidate {
  readonly documentId: string;
  readonly title: string;
  readonly content: string;
  readonly score: number;
  readonly importance: number;
  readonly eventAt: string;
  readonly sourceVoice: "fts" | "vector";
  readonly contentDigest: string;
}

export interface VoiceScore {
  readonly voice: RetrievalVoice;
  readonly score: number;
  readonly rank: number;
  readonly rawScore: number;
  readonly weight: number;
}

export interface RankedCandidate {
  readonly documentId: string;
  readonly score: number;
  readonly rank: number;
  readonly content: string;
  readonly voices: readonly VoiceScore[];
}

export interface RetrievalAlgorithmRequest {
  readonly protocol: typeof RETRIEVAL_ALGORITHM_PROTOCOL;
  readonly requestId: string;
  readonly operation: "rank" | "classify" | "temporal" | "analyze";
  readonly payload: Readonly<Record<string, unknown>>;
}

export interface RetrievalAlgorithmResponse {
  readonly protocol: typeof RETRIEVAL_ALGORITHM_PROTOCOL;
  readonly requestId: string;
  readonly ok: boolean;
  readonly result: Readonly<Record<string, unknown>>;
  readonly error: string;
}

export interface RankingRequest {
  readonly query: string;
  readonly requestTime: string;
  readonly intent: QueryIntent;
  readonly temporal: TemporalConstraint;
  readonly budget: RetrievalBudget;
  readonly candidates: readonly RetrievalCandidate[];
}

export interface RankingResult {
  readonly intent: QueryIntent;
  readonly temporal: TemporalConstraint;
  readonly hits: readonly RankedCandidate[];
  readonly candidateCount: number;
  readonly fusedCount: number;
  readonly truncated: boolean;
  readonly evidenceDigest: string;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function requireString(value: unknown, name: string): string {
  if (typeof value !== "string" || value.trim().length === 0) throw new Error(`${name} is required`);
  return value;
}

export function finiteNumber(value: unknown, name: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${name} must be finite`);
  return value;
}

export function nonNegativeInteger(value: unknown, name: string): number {
  const resolved = finiteNumber(value, name);
  if (!Number.isSafeInteger(resolved) || resolved < 0) throw new Error(`${name} must be a non-negative integer`);
  return resolved;
}

export function uniqueStrings(values: readonly string[]): string[] {
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))];
}
