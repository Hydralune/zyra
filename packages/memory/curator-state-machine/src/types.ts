export const CURATOR_STATE_PROTOCOL = "zyra.memory-curator-state.v1" as const;

export type CuratorJobState =
	| "queued"
	| "claimed"
	| "extracting"
	| "deciding"
	| "validating"
	| "committing"
	| "succeeded"
	| "failed"
	| "retry_wait"
	| "stale"
	| "cancelled";

export type CandidateState =
	| "proposed"
	| "validating"
	| "accepted"
	| "rejected"
	| "committed"
	| "superseded"
	| "merged";

export type CandidateKind =
	| "promote"
	| "discard"
	| "compress"
	| "skill_candidate"
	| "failure_pattern";

export type CandidateRelationKind =
	| "duplicate_of"
	| "contradicts"
	| "supersedes"
	| "merged_into"
	| "supports";

export interface JobSnapshot {
	jobId: string;
	taskId: string;
	state: CuratorJobState;
	inputWatermark: number;
	lastSuccessWatermark: number;
	leaseOwner: string;
	ownershipToken: string;
	leaseEpoch: number;
	leaseExpiresAt: number | null;
	attempt: number;
	retryRemaining: number;
	retryAt: number | null;
	candidateCount: number;
	committedCount: number;
}

export interface JobLease {
	jobId: string;
	taskId: string;
	workerId: string;
	ownershipToken: string;
	leaseEpoch: number;
	inputWatermark: number;
	expiresAt: number;
	attempt: number;
}

export interface JobTransitionRequest {
	job: JobSnapshot;
	lease: JobLease;
	target: CuratorJobState;
	now: number;
	leaseSeconds?: number;
	retryable?: boolean;
	retryDelaySeconds?: number;
	errorCode?: string;
	candidateCount?: number;
	committedCount?: number;
}

export interface JobTransitionResult {
	ok: boolean;
	job: JobSnapshot;
	reason: string;
	fenced: boolean;
	advancedSuccessWatermark: boolean;
}

export interface CandidateEnvelope {
	candidateId: string;
	runId: string;
	taskId: string;
	kind: CandidateKind;
	layer: string;
	scope: string;
	subject: string;
	summary: string;
	content: Readonly<Record<string, unknown>>;
	evidenceDigest: string;
	evidenceIds: readonly string[];
	confidence: number;
	idempotencyKey: string;
	semanticDigest: string;
	state: CandidateState;
	expectedMemoryId: string;
	expectedRevision: number | null;
	createdAt: string;
}

export interface CandidateRelation {
	relationId: string;
	sourceCandidateId: string;
	targetCandidateId: string;
	kind: CandidateRelationKind;
	reason: string;
	evidenceDigest: string;
	metadata: Readonly<Record<string, unknown>>;
}

export interface ConsolidationRequest {
	candidates: readonly CandidateEnvelope[];
	priorCandidates: readonly CandidateEnvelope[];
}

export interface ConsolidationResult {
	activeCandidateIds: readonly string[];
	suppressedCandidateIds: readonly string[];
	relations: readonly CandidateRelation[];
	diagnostics: Readonly<Record<string, unknown>>;
}

export interface ProtocolRequest {
	protocol: typeof CURATOR_STATE_PROTOCOL;
	requestId: string;
	operation: "transition_job" | "consolidate_candidates" | "validate_candidate";
	payload: Readonly<Record<string, unknown>>;
}

export interface ProtocolResponse {
	protocol: typeof CURATOR_STATE_PROTOCOL;
	requestId: string;
	ok: boolean;
	result: Readonly<Record<string, unknown>>;
	error: string;
}

export interface ValidationIssue {
	code: string;
	message: string;
	field: string;
}

export interface CandidateValidationResult {
	ok: boolean;
	issues: readonly ValidationIssue[];
	normalized: CandidateEnvelope | null;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isStringArray(value: unknown): value is string[] {
	return Array.isArray(value) && value.every(item => typeof item === "string");
}

export function finiteNumber(value: unknown): value is number {
	return typeof value === "number" && Number.isFinite(value);
}

export function optionalFiniteNumber(value: unknown): value is number | null | undefined {
	return value === null || value === undefined || finiteNumber(value);
}

export function jobStates(): readonly CuratorJobState[] {
	return [
		"queued",
		"claimed",
		"extracting",
		"deciding",
		"validating",
		"committing",
		"succeeded",
		"failed",
		"retry_wait",
		"stale",
		"cancelled",
	];
}

export function candidateStates(): readonly CandidateState[] {
	return ["proposed", "validating", "accepted", "rejected", "committed", "superseded", "merged"];
}

export function candidateKinds(): readonly CandidateKind[] {
	return ["promote", "discard", "compress", "skill_candidate", "failure_pattern"];
}
