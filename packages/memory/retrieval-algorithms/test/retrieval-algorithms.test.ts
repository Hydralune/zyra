import { describe, expect, test } from "bun:test";
import { classifyIntent } from "../src/intent.ts";
import { handleProtocolRequest } from "../src/protocol.ts";
import { lexicalSimilarity, rankRetrievalCandidates } from "../src/ranking.ts";
import { parseTemporalConstraint, temporalMatches } from "../src/temporal.ts";
import { RETRIEVAL_ALGORITHM_PROTOCOL, type RetrievalCandidate } from "../src/types.ts";

const requestTime = "2026-07-22T12:00:00.000Z";

function candidate(
  documentId: string,
  score: number,
  sourceVoice: "fts" | "vector",
  content: string,
  importance = 0,
  eventAt = "",
): RetrievalCandidate {
  return {
    documentId,
    title: documentId,
    content,
    score,
    importance,
    eventAt,
    sourceVoice,
    contentDigest: documentId.padEnd(64, "0").slice(0, 64),
  };
}

describe("retained Mnemopi retrieval algorithms", () => {
  test("classifies intent and normalizes deterministic weights", () => {
    const temporal = classifyIntent("what happened yesterday during deployment");
    expect(temporal.category).toBe("temporal");
    expect(temporal.ftsBias).toBe(1.5);
    const procedural = classifyIntent("how to configure and deploy the worker");
    expect(procedural.category).toBe("procedural");
    expect(procedural.vectorBias).toBe(1.3);
  });

  test("parses bounded temporal windows in UTC", () => {
    const yesterday = parseTemporalConstraint("show failures yesterday", requestTime);
    expect(yesterday.startAt).toBe("2026-07-21T00:00:00.000Z");
    expect(yesterday.endAt).toBe("2026-07-22T00:00:00.000Z");
    const nextMonth = parseTemporalConstraint("plan this next month", requestTime);
    expect(nextMonth.startAt).toBe("2026-08-01T00:00:00.000Z");
    expect(nextMonth.endAt).toBe("2026-09-01T00:00:00.000Z");
    expect(temporalMatches("2026-07-21T00:00:00.000Z", yesterday)).toBeTrue();
    expect(temporalMatches("2026-07-21T23:59:59.999Z", yesterday)).toBeTrue();
    expect(temporalMatches("2026-07-22T00:00:00.000Z", yesterday)).toBeFalse();
  });

  test("fuses voices and uses MMR to avoid duplicate context", () => {
    const result = rankRetrievalCandidates({
      query: "how to repair provider route",
      requestTime,
      intent: classifyIntent("how to repair provider route"),
      temporal: parseTemporalConstraint("how to repair provider route", requestTime),
      budget: {
        limit: 2,
        candidateLimit: 3,
        maximumOutputCharacters: 10_000,
        maximumDocumentCharacters: 10_000,
        mmrLambda: 0.35,
      },
      candidates: [
        candidate("a", 1, "fts", "provider route repair retry timeout", 0.9, "2026-07-22T10:00:00Z"),
        candidate("b", 0.99, "fts", "provider route repair retry timeout duplicate", 0.8, "2026-07-22T09:00:00Z"),
        candidate("c", 0.7, "fts", "credential rotation and secret refresh", 0.6, "2026-07-21T09:00:00Z"),
        candidate("a", 0.8, "vector", "provider route repair retry timeout", 0.9, "2026-07-22T10:00:00Z"),
      ],
    });
    expect(result.hits.map((hit) => hit.documentId)).toEqual(["a", "c"]);
    expect(result.hits[0]?.voices.some((voice) => voice.voice === "vector")).toBeTrue();
    expect(result.evidenceDigest).toHaveLength(64);
  });

  test("enforces output budgets in the algorithm owner", () => {
    const result = rankRetrievalCandidates({
      query: "explain provider state",
      requestTime,
      intent: classifyIntent("explain provider state"),
      temporal: parseTemporalConstraint("explain provider state", requestTime),
      budget: {
        limit: 5,
        candidateLimit: 5,
        maximumOutputCharacters: 170,
        maximumDocumentCharacters: 1_000,
        mmrLambda: 0.72,
      },
      candidates: [candidate("a", 1, "fts", "x".repeat(500))],
    });
    expect(result.hits).toHaveLength(1);
    expect(result.hits[0]?.content.length).toBeLessThan(100);
    expect(result.truncated).toBeTrue();
  });

  test("typed protocol rejects malformed and duplicate candidates", () => {
    const malformed = handleProtocolRequest({
      protocol: RETRIEVAL_ALGORITHM_PROTOCOL,
      requestId: "request-1",
      operation: "rank",
      payload: { query: "x", requestTime, budget: {}, candidates: [] },
    });
    expect(malformed.ok).toBeFalse();
    const duplicate = handleProtocolRequest({
      protocol: RETRIEVAL_ALGORITHM_PROTOCOL,
      requestId: "request-2",
      operation: "rank",
      payload: {
        query: "x",
        requestTime,
        intent: classifyIntent("x"),
        temporal: parseTemporalConstraint("x", requestTime),
        budget: {
          limit: 1,
          candidateLimit: 1,
          maximumOutputCharacters: 1000,
          maximumDocumentCharacters: 1000,
          mmrLambda: 0.7,
        },
        candidates: [candidate("a", 1, "fts", "x"), candidate("a", 0.5, "fts", "x")],
      },
    });
    expect(duplicate.ok).toBeFalse();
  });

  test("lexical similarity is Unicode-aware and symmetric", () => {
    const left = lexicalSimilarity("provider 路由 recovery", "provider 路由 retry");
    const right = lexicalSimilarity("provider 路由 retry", "provider 路由 recovery");
    expect(left).toBe(right);
    expect(left).toBeGreaterThan(0.3);
  });
});
