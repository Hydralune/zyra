import { consolidateCandidates, validateCandidate } from "./consolidation";
import { transitionJob } from "./job-state-machine";
import type {
	CandidateEnvelope,
	ConsolidationRequest,
	JobLease,
	JobSnapshot,
	JobTransitionRequest,
	ProtocolRequest,
	ProtocolResponse,
} from "./types";
import { CURATOR_STATE_PROTOCOL, isRecord } from "./types";

export function parseProtocolRequest(value: unknown): ProtocolRequest {
	if (!isRecord(value)) throw new Error("request must be an object");
	if (value.protocol !== CURATOR_STATE_PROTOCOL) throw new Error("protocol mismatch");
	if (typeof value.requestId !== "string" || value.requestId.trim().length === 0) {
		throw new Error("requestId is required");
	}
	if (
		value.operation !== "transition_job" &&
		value.operation !== "consolidate_candidates" &&
		value.operation !== "validate_candidate"
	) {
		throw new Error("operation is not supported");
	}
	if (!isRecord(value.payload)) throw new Error("payload must be an object");
	return {
		protocol: CURATOR_STATE_PROTOCOL,
		requestId: value.requestId,
		operation: value.operation,
		payload: value.payload,
	};
}

export function handleProtocolRequest(value: unknown): ProtocolResponse {
	let requestId = "unknown";
	try {
		const request = parseProtocolRequest(value);
		requestId = request.requestId;
		if (request.operation === "validate_candidate") {
			const result = validateCandidate(request.payload.candidate);
			return success(requestId, {
				ok: result.ok,
				issues: result.issues,
				normalized: result.normalized,
			});
		}
		if (request.operation === "consolidate_candidates") {
			const consolidation = parseConsolidationRequest(request.payload);
			const result = consolidateCandidates(consolidation);
			return success(requestId, {
				activeCandidateIds: result.activeCandidateIds,
				suppressedCandidateIds: result.suppressedCandidateIds,
				relations: result.relations,
				diagnostics: result.diagnostics,
			});
		}
		const transition = parseJobTransitionRequest(request.payload);
		const result = transitionJob(transition);
		return success(requestId, {
			ok: result.ok,
			job: result.job,
			reason: result.reason,
			fenced: result.fenced,
			advancedSuccessWatermark: result.advancedSuccessWatermark,
		});
	} catch (error) {
		return failure(requestId, error instanceof Error ? error.message : String(error));
	}
}

function parseConsolidationRequest(value: Readonly<Record<string, unknown>>): ConsolidationRequest {
	if (!Array.isArray(value.candidates)) throw new Error("candidates must be an array");
	if (!Array.isArray(value.priorCandidates)) throw new Error("priorCandidates must be an array");
	return {
		candidates: value.candidates as CandidateEnvelope[],
		priorCandidates: value.priorCandidates as CandidateEnvelope[],
	};
}

function parseJobTransitionRequest(value: Readonly<Record<string, unknown>>): JobTransitionRequest {
	if (!isRecord(value.job)) throw new Error("job must be an object");
	if (!isRecord(value.lease)) throw new Error("lease must be an object");
	if (typeof value.target !== "string") throw new Error("target must be a string");
	if (typeof value.now !== "number" || !Number.isFinite(value.now)) throw new Error("now must be finite");
	return {
		job: value.job as unknown as JobSnapshot,
		lease: value.lease as unknown as JobLease,
		target: value.target as JobTransitionRequest["target"],
		now: value.now,
		leaseSeconds: optionalNumber(value.leaseSeconds),
		retryable: typeof value.retryable === "boolean" ? value.retryable : undefined,
		retryDelaySeconds: optionalNumber(value.retryDelaySeconds),
		errorCode: typeof value.errorCode === "string" ? value.errorCode : undefined,
		candidateCount: optionalNumber(value.candidateCount),
		committedCount: optionalNumber(value.committedCount),
	};
}

function optionalNumber(value: unknown): number | undefined {
	return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function success(requestId: string, result: Readonly<Record<string, unknown>>): ProtocolResponse {
	return { protocol: CURATOR_STATE_PROTOCOL, requestId, ok: true, result, error: "" };
}

function failure(requestId: string, error: string): ProtocolResponse {
	return { protocol: CURATOR_STATE_PROTOCOL, requestId, ok: false, result: {}, error };
}
