import { classifyIntent } from "./intent.ts";
import { rankRetrievalCandidates } from "./ranking.ts";
import { parseTemporalConstraint } from "./temporal.ts";
import {
  RETRIEVAL_ALGORITHM_PROTOCOL,
  finiteNumber,
  isRecord,
  nonNegativeInteger,
  requireString,
  type RankingRequest,
  type RetrievalAlgorithmRequest,
  type RetrievalAlgorithmResponse,
  type RetrievalBudget,
  type RetrievalCandidate,
} from "./types.ts";

export function parseProtocolRequest(value: unknown): RetrievalAlgorithmRequest {
  if (!isRecord(value)) throw new Error("request must be an object");
  if (value.protocol !== RETRIEVAL_ALGORITHM_PROTOCOL) throw new Error("protocol mismatch");
  const requestId = requireString(value.requestId, "requestId");
  if (value.operation !== "rank" && value.operation !== "classify" && value.operation !== "temporal" && value.operation !== "analyze") {
    throw new Error("operation is not supported");
  }
  if (!isRecord(value.payload)) throw new Error("payload must be an object");
  return { protocol: RETRIEVAL_ALGORITHM_PROTOCOL, requestId, operation: value.operation, payload: value.payload };
}

export function handleProtocolRequest(value: unknown): RetrievalAlgorithmResponse {
  let requestId = "unknown";
  try {
    const request = parseProtocolRequest(value);
    requestId = request.requestId;
    if (request.operation === "classify") {
      return success(requestId, { intent: classifyIntent(requireString(request.payload.query, "query")) });
    }
    if (request.operation === "temporal") {
      return success(requestId, {
        temporal: parseTemporalConstraint(
          requireString(request.payload.query, "query"),
          requireString(request.payload.requestTime, "requestTime"),
        ),
      });
    }
    if (request.operation === "analyze") {
      const query = requireString(request.payload.query, "query");
      const requestTime = requireString(request.payload.requestTime, "requestTime");
      return success(requestId, {
        intent: classifyIntent(query),
        temporal: parseTemporalConstraint(query, requestTime),
      });
    }
    return success(requestId, rankRetrievalCandidates(parseRankingRequest(request.payload)) as unknown as Record<string, unknown>);
  } catch (error) {
    return failure(requestId, error instanceof Error ? error.message : String(error));
  }
}

function parseRankingRequest(payload: Readonly<Record<string, unknown>>): RankingRequest {
  if (!isRecord(payload.budget)) throw new Error("budget must be an object");
  if (!Array.isArray(payload.candidates)) throw new Error("candidates must be an array");
  return {
    query: requireString(payload.query, "query"),
    requestTime: requireString(payload.requestTime, "requestTime"),
    intent: parseIntent(payload.intent),
    temporal: parseTemporal(payload.temporal),
    budget: parseBudget(payload.budget),
    candidates: payload.candidates.map((value, index) => parseCandidate(value, index)),
  };
}

function parseIntent(value: unknown) {
  if (!isRecord(value)) throw new Error("intent must be an object");
  if (!Array.isArray(value.signals)) throw new Error("intent.signals must be an array");
  const categories = new Set(["temporal", "factual", "entity", "preference", "procedural", "general"]);
  const category = requireString(value.category, "intent.category");
  if (!categories.has(category)) throw new Error("intent.category is invalid");
  const signals = value.signals.map((item) => String(item));
  if (signals.some((item) => !categories.has(item))) throw new Error("intent.signals is invalid");
  return {
    category: category as import("./types.ts").QueryIntentCategory,
    confidence: finiteNumber(value.confidence, "intent.confidence"),
    signals: signals as import("./types.ts").QueryIntentCategory[],
    vectorBias: finiteNumber(value.vectorBias, "intent.vectorBias"),
    ftsBias: finiteNumber(value.ftsBias, "intent.ftsBias"),
    importanceBias: finiteNumber(value.importanceBias, "intent.importanceBias"),
  };
}

function parseTemporal(value: unknown) {
  if (!isRecord(value)) throw new Error("temporal must be an object");
  if (!Array.isArray(value.tags)) throw new Error("temporal.tags must be an array");
  const precision = String(value.precision ?? "unknown");
  if (!["day", "week", "month", "year", "relative", "unknown"].includes(precision)) {
    throw new Error("temporal.precision is invalid");
  }
  return {
    startAt: typeof value.startAt === "string" ? value.startAt : "",
    endAt: typeof value.endAt === "string" ? value.endAt : "",
    tags: value.tags.map((item) => String(item)),
    precision: precision as import("./types.ts").TemporalConstraint["precision"],
    sourceText: typeof value.sourceText === "string" ? value.sourceText : "",
  };
}

function parseBudget(value: Readonly<Record<string, unknown>>): RetrievalBudget {
  const mmrLambda = finiteNumber(value.mmrLambda, "budget.mmrLambda");
  return {
    limit: nonNegativeInteger(value.limit, "budget.limit"),
    candidateLimit: nonNegativeInteger(value.candidateLimit, "budget.candidateLimit"),
    maximumOutputCharacters: nonNegativeInteger(value.maximumOutputCharacters, "budget.maximumOutputCharacters"),
    maximumDocumentCharacters: nonNegativeInteger(value.maximumDocumentCharacters, "budget.maximumDocumentCharacters"),
    mmrLambda,
  };
}

function parseCandidate(value: unknown, index: number): RetrievalCandidate {
  if (!isRecord(value)) throw new Error(`candidate ${index} must be an object`);
  if (value.sourceVoice !== "fts" && value.sourceVoice !== "vector") throw new Error(`candidate ${index} sourceVoice is invalid`);
  return {
    documentId: requireString(value.documentId, `candidate ${index} documentId`),
    title: typeof value.title === "string" ? value.title : "",
    content: typeof value.content === "string" ? value.content : "",
    score: finiteNumber(value.score, `candidate ${index} score`),
    importance: finiteNumber(value.importance, `candidate ${index} importance`),
    eventAt: typeof value.eventAt === "string" ? value.eventAt : "",
    sourceVoice: value.sourceVoice,
    contentDigest: requireString(value.contentDigest, `candidate ${index} contentDigest`),
  };
}

function success(requestId: string, result: Readonly<Record<string, unknown>>): RetrievalAlgorithmResponse {
  return { protocol: RETRIEVAL_ALGORITHM_PROTOCOL, requestId, ok: true, result, error: "" };
}

function failure(requestId: string, error: string): RetrievalAlgorithmResponse {
  return { protocol: RETRIEVAL_ALGORITHM_PROTOCOL, requestId, ok: false, result: {}, error };
}
