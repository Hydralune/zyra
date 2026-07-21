import { createHash } from "node:crypto";

import type {
	CandidateEnvelope,
	CandidateRelation,
	CandidateRelationKind,
	CandidateValidationResult,
	ConsolidationRequest,
	ConsolidationResult,
	ValidationIssue,
} from "./types";
import { candidateKinds, candidateStates, isRecord, isStringArray } from "./types";

const ALLOWED_LAYERS = new Set(["working", "episodic", "semantic", "skill"]);
const ALLOWED_SCOPES = new Set(["task", "run", "project", "global"]);
const FACT_KEYS = [
	"value",
	"status",
	"outcome",
	"decision",
	"requirement",
	"fact",
	"signature",
	"enabled",
	"available",
] as const;
const NEGATION = /\b(?:not|never|no longer|false|disabled|forbidden|failed|invalid|removed)\b/i;
const TOKEN = /[A-Za-z0-9_./:-]{2,}/g;

export function validateCandidate(value: unknown): CandidateValidationResult {
	if (!isRecord(value)) {
		return invalid([{ code: "schema_invalid", message: "candidate must be an object", field: "$" }]);
	}
	const issues: ValidationIssue[] = [];
	const candidateId = requiredString(value.candidateId, "candidateId", issues);
	const runId = requiredString(value.runId, "runId", issues);
	const taskId = requiredString(value.taskId, "taskId", issues);
	const kind = requiredString(value.kind, "kind", issues);
	const layer = requiredString(value.layer, "layer", issues);
	const scope = requiredString(value.scope, "scope", issues);
	const subject = requiredString(value.subject, "subject", issues);
	const summary = typeof value.summary === "string" ? value.summary.trim() : "";
	const evidenceDigest = requiredString(value.evidenceDigest, "evidenceDigest", issues);
	const idempotencyKey = requiredString(value.idempotencyKey, "idempotencyKey", issues);
	const semanticDigest = requiredString(value.semanticDigest, "semanticDigest", issues);
	const state = requiredString(value.state, "state", issues);
	const expectedMemoryId = typeof value.expectedMemoryId === "string" ? value.expectedMemoryId : "";
	const createdAt = requiredString(value.createdAt, "createdAt", issues);
	if (!candidateKinds().includes(kind as CandidateEnvelope["kind"])) {
		issues.push({ code: "kind_invalid", message: "candidate kind is not supported", field: "kind" });
	}
	if (!ALLOWED_LAYERS.has(layer)) {
		issues.push({ code: "layer_invalid", message: "candidate layer is not supported", field: "layer" });
	}
	if (!ALLOWED_SCOPES.has(scope)) {
		issues.push({ code: "scope_invalid", message: "candidate scope is not supported", field: "scope" });
	}
	if (!candidateStates().includes(state as CandidateEnvelope["state"])) {
		issues.push({ code: "state_invalid", message: "candidate state is not supported", field: "state" });
	}
	if (summary.length === 0 && kind !== "discard") {
		issues.push({ code: "summary_required", message: "candidate summary is required", field: "summary" });
	}
	if (summary.length > 2_000) {
		issues.push({ code: "summary_too_large", message: "candidate summary exceeds bound", field: "summary" });
	}
	if (!isRecord(value.content)) {
		issues.push({ code: "content_invalid", message: "candidate content must be an object", field: "content" });
	}
	if (!isStringArray(value.evidenceIds) || value.evidenceIds.length === 0) {
		issues.push({ code: "evidence_invalid", message: "candidate evidenceIds must be non-empty", field: "evidenceIds" });
	}
	if (evidenceDigest.length !== 64 || !/^[a-f0-9]{64}$/i.test(evidenceDigest)) {
		issues.push({ code: "evidence_digest_invalid", message: "evidence digest must be sha256", field: "evidenceDigest" });
	}
	if (semanticDigest.length !== 64 || !/^[a-f0-9]{64}$/i.test(semanticDigest)) {
		issues.push({ code: "semantic_digest_invalid", message: "semantic digest must be sha256", field: "semanticDigest" });
	}
	const confidence = typeof value.confidence === "number" ? value.confidence : Number.NaN;
	if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
		issues.push({ code: "confidence_invalid", message: "candidate confidence must be 0..1", field: "confidence" });
	}
	const expectedRevision = value.expectedRevision;
	if (!(expectedRevision === null || (Number.isInteger(expectedRevision) && Number(expectedRevision) >= 0))) {
		issues.push({ code: "revision_invalid", message: "expectedRevision must be null or non-negative", field: "expectedRevision" });
	}
	if (issues.length > 0) return invalid(issues);
	const normalized: CandidateEnvelope = {
		candidateId,
		runId,
		taskId,
		kind: kind as CandidateEnvelope["kind"],
		layer,
		scope,
		subject,
		summary,
		content: value.content as Record<string, unknown>,
		evidenceDigest,
		evidenceIds: value.evidenceIds as string[],
		confidence,
		idempotencyKey,
		semanticDigest,
		state: state as CandidateEnvelope["state"],
		expectedMemoryId,
		expectedRevision: expectedRevision as number | null,
		createdAt,
	};
	return { ok: true, issues: [], normalized };
}

export function consolidateCandidates(request: ConsolidationRequest): ConsolidationResult {
	const current = validateCandidateArray(request.candidates, "candidates");
	const prior = validateCandidateArray(request.priorCandidates, "priorCandidates");
	const active: string[] = [];
	const suppressed: string[] = [];
	const relations: CandidateRelation[] = [];
	const allPrior: CandidateEnvelope[] = [...prior];
	for (const candidate of [...current].sort(compareCandidate)) {
		const relation = relationFor(candidate, allPrior);
		if (relation === null) {
			active.push(candidate.candidateId);
			allPrior.push(candidate);
			continue;
		}
		relations.push(relation);
		if (relation.kind === "duplicate_of" || relation.kind === "merged_into") {
			suppressed.push(candidate.candidateId);
			continue;
		}
		active.push(candidate.candidateId);
		allPrior.push(candidate);
	}
	return {
		activeCandidateIds: active,
		suppressedCandidateIds: suppressed,
		relations,
		diagnostics: {
			currentCount: current.length,
			priorCount: prior.length,
			activeCount: active.length,
			suppressedCount: suppressed.length,
			relationCounts: countRelations(relations),
			lastWriteWins: false,
			explicitCollisionResolution: true,
		},
	};
}

export function relationFor(candidate: CandidateEnvelope, prior: readonly CandidateEnvelope[]): CandidateRelation | null {
	const subjectPrior = prior
		.filter(existing => existing.candidateId !== candidate.candidateId)
		.filter(existing => existing.taskId === candidate.taskId)
		.filter(existing => existing.subject === candidate.subject)
		.sort((left, right) => compareCandidate(right, left));
	for (const existing of subjectPrior) {
		if (candidate.semanticDigest === existing.semanticDigest) {
			return relation(candidate, existing, "duplicate_of", "same semantic digest");
		}
		if (candidate.idempotencyKey === existing.idempotencyKey) {
			return relation(candidate, existing, "duplicate_of", "same idempotency key");
		}
		if (candidate.kind === "discard" || existing.kind === "discard") {
			return relation(candidate, existing, "contradicts", "discard conflicts with retained subject");
		}
		const conflictingKeys = contradictionKeys(candidate, existing);
		if (conflictingKeys.length > 0) {
			return relation(candidate, existing, "contradicts", "opposed subject values require merge", {
				conflictingKeys,
				candidateConfidence: candidate.confidence,
				existingConfidence: existing.confidence,
			});
		}
		if (explicitlySupersedes(candidate, existing)) {
			return relation(candidate, existing, "supersedes", "explicit higher expected revision", {
				candidateRevision: candidate.expectedRevision,
				existingRevision: existing.expectedRevision,
			});
		}
		if (nearDuplicate(candidate, existing)) {
			return relation(candidate, existing, "merged_into", "same subject and near-identical summary", {
				summarySimilarity: tokenSimilarity(candidate.summary, existing.summary),
			});
		}
	}
	return null;
}

export function contradictionKeys(left: CandidateEnvelope, right: CandidateEnvelope): readonly string[] {
	const leftFacts = facts(left);
	const rightFacts = facts(right);
	const conflicts: string[] = [];
	for (const key of Object.keys(leftFacts).sort()) {
		if (!(key in rightFacts)) continue;
		if (leftFacts[key] !== rightFacts[key]) conflicts.push(key);
	}
	if (conflicts.length === 0 && opposedSummary(left.summary, right.summary)) {
		conflicts.push("summary_polarity");
	}
	return conflicts;
}

export function facts(candidate: CandidateEnvelope): Readonly<Record<string, string>> {
	const output: Record<string, string> = {};
	for (const key of FACT_KEYS) {
		const value = candidate.content[key];
		if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
			output[key] = String(value).trim().toLowerCase();
		}
	}
	return output;
}

export function tokenSimilarity(left: string, right: string): number {
	const leftTokens = tokens(left);
	const rightTokens = tokens(right);
	if (leftTokens.size === 0 || rightTokens.size === 0) return 0;
	let intersection = 0;
	for (const token of leftTokens) {
		if (rightTokens.has(token)) intersection += 1;
	}
	const union = new Set([...leftTokens, ...rightTokens]).size;
	return intersection / Math.max(1, union);
}

function explicitlySupersedes(candidate: CandidateEnvelope, existing: CandidateEnvelope): boolean {
	return (
		candidate.expectedMemoryId.length > 0 &&
		candidate.expectedMemoryId === existing.expectedMemoryId &&
		candidate.expectedRevision !== null &&
		existing.expectedRevision !== null &&
		candidate.expectedRevision > existing.expectedRevision
	);
}

function nearDuplicate(candidate: CandidateEnvelope, existing: CandidateEnvelope): boolean {
	return candidate.kind === existing.kind && candidate.layer === existing.layer && tokenSimilarity(candidate.summary, existing.summary) >= 0.9;
}

function opposedSummary(left: string, right: string): boolean {
	const overlap = containmentSimilarity(left, right);
	return overlap >= 0.65 && polarity(left) !== polarity(right);
}

function containmentSimilarity(left: string, right: string): number {
	const leftTokens = tokens(left);
	const rightTokens = tokens(right);
	if (leftTokens.size === 0 || rightTokens.size === 0) return 0;
	let intersection = 0;
	for (const token of leftTokens) {
		if (rightTokens.has(token)) intersection += 1;
	}
	return intersection / Math.max(1, Math.min(leftTokens.size, rightTokens.size));
}

function polarity(value: string): "negative" | "positive" {
	return NEGATION.test(value) ? "negative" : "positive";
}

function tokens(value: string): Set<string> {
	const matches = value.match(TOKEN) ?? [];
	return new Set(matches.map(token => token.toLowerCase()).filter(token => token.length > 2));
}

function relation(
	source: CandidateEnvelope,
	target: CandidateEnvelope,
	kind: CandidateRelationKind,
	reason: string,
	metadata: Readonly<Record<string, unknown>> = {},
): CandidateRelation {
	return {
		relationId: stableId("relation", [source.candidateId, target.candidateId, kind, source.evidenceDigest]),
		sourceCandidateId: source.candidateId,
		targetCandidateId: target.candidateId,
		kind,
		reason,
		evidenceDigest: source.evidenceDigest,
		metadata,
	};
}

function stableId(prefix: string, parts: readonly string[]): string {
	const digest = createHash("sha256").update(JSON.stringify(parts)).digest("hex").slice(0, 24);
	return `${prefix}_${digest}`;
}

function validateCandidateArray(values: readonly CandidateEnvelope[], field: string): CandidateEnvelope[] {
	const output: CandidateEnvelope[] = [];
	for (let index = 0; index < values.length; index += 1) {
		const result = validateCandidate(values[index]);
		if (!result.ok || result.normalized === null) {
			const detail = result.issues.map(issue => `${issue.field}:${issue.code}`).join(",");
			throw new Error(`${field}[${index}] invalid: ${detail}`);
		}
		output.push(result.normalized);
	}
	return output;
}

function requiredString(value: unknown, field: string, issues: ValidationIssue[]): string {
	if (typeof value !== "string" || value.trim().length === 0) {
		issues.push({ code: "required", message: `${field} is required`, field });
		return "";
	}
	return value.trim();
}

function invalid(issues: readonly ValidationIssue[]): CandidateValidationResult {
	return { ok: false, issues, normalized: null };
}

function compareCandidate(left: CandidateEnvelope, right: CandidateEnvelope): number {
	const byCreated = left.createdAt.localeCompare(right.createdAt);
	if (byCreated !== 0) return byCreated;
	return left.candidateId.localeCompare(right.candidateId);
}

function countRelations(relations: readonly CandidateRelation[]): Readonly<Record<string, number>> {
	const counts: Record<string, number> = {};
	for (const relation of relations) counts[relation.kind] = (counts[relation.kind] ?? 0) + 1;
	return counts;
}
