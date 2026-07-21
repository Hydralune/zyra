import type {
	CuratorJobState,
	JobLease,
	JobSnapshot,
	JobTransitionRequest,
	JobTransitionResult,
} from "./types";

const ACTIVE_STATES = new Set<CuratorJobState>([
	"claimed",
	"extracting",
	"deciding",
	"validating",
	"committing",
]);

const TRANSITIONS: Readonly<Record<CuratorJobState, readonly CuratorJobState[]>> = {
	queued: ["claimed", "cancelled"],
	claimed: ["extracting", "failed", "retry_wait", "stale", "cancelled"],
	extracting: ["deciding", "failed", "retry_wait", "stale", "cancelled"],
	deciding: ["validating", "failed", "retry_wait", "stale", "cancelled"],
	validating: ["committing", "failed", "retry_wait", "stale", "cancelled"],
	committing: ["succeeded", "failed", "retry_wait", "stale"],
	succeeded: [],
	failed: [],
	retry_wait: ["claimed", "cancelled"],
	stale: [],
	cancelled: [],
};

export interface LeaseCheck {
	ok: boolean;
	reason: string;
	fenced: boolean;
}

export function activeJobState(state: CuratorJobState): boolean {
	return ACTIVE_STATES.has(state);
}

export function transitionAllowed(before: CuratorJobState, after: CuratorJobState): boolean {
	return TRANSITIONS[before].includes(after);
}

export function validateLease(job: JobSnapshot, lease: JobLease, now: number, requireUnexpired = true): LeaseCheck {
	if (job.jobId !== lease.jobId) return fenced("job_id_changed");
	if (job.taskId !== lease.taskId) return fenced("task_id_changed");
	if (job.leaseOwner !== lease.workerId) return fenced("lease_owner_changed");
	if (job.ownershipToken !== lease.ownershipToken) return fenced("ownership_token_changed");
	if (job.leaseEpoch !== lease.leaseEpoch) return fenced("lease_epoch_changed");
	if (job.inputWatermark !== lease.inputWatermark) return fenced("input_watermark_changed");
	if (job.attempt !== lease.attempt) return fenced("attempt_changed");
	if (!activeJobState(job.state)) return fenced("job_not_active");
	if (requireUnexpired) {
		if (job.leaseExpiresAt === null) return fenced("lease_expiry_missing");
		if (job.leaseExpiresAt <= now) return fenced("lease_expired");
		if (lease.expiresAt <= now) return fenced("lease_snapshot_expired");
	}
	return { ok: true, reason: "ok", fenced: false };
}

export function heartbeatJob(
	job: JobSnapshot,
	lease: JobLease,
	now: number,
	leaseSeconds: number,
): JobTransitionResult {
	const check = validateLease(job, lease, now, true);
	if (!check.ok) return rejected(job, check.reason, check.fenced);
	if (!(leaseSeconds > 0)) return rejected(job, "lease_seconds_not_positive", false);
	const expiresAt = now + leaseSeconds;
	return accepted(
		{
			...job,
			leaseExpiresAt: expiresAt,
		},
		"heartbeat_extended",
		false,
	);
}

export function transitionJob(request: JobTransitionRequest): JobTransitionResult {
	const { job, lease, target, now } = request;
	const requireUnexpired = target !== "failed" && target !== "retry_wait" && target !== "stale";
	const check = validateLease(job, lease, now, requireUnexpired);
	if (!check.ok) return rejected(job, check.reason, check.fenced);
	if (!transitionAllowed(job.state, target)) {
		return rejected(job, `transition_forbidden:${job.state}->${target}`, false);
	}
	if (target === "succeeded") return succeed(request);
	if (target === "failed" || target === "retry_wait") return fail(request);
	if (target === "stale") return stale(request);
	if (target === "cancelled") return cancel(request);
	if (!ACTIVE_STATES.has(target)) return rejected(job, "target_not_active", false);
	return accepted({ ...job, state: target }, `transitioned:${job.state}->${target}`, false);
}

export function acquireJob(
	job: JobSnapshot,
	params: {
		workerId: string;
		ownershipToken: string;
		now: number;
		leaseSeconds: number;
	},
): JobTransitionResult {
	if (job.state !== "queued" && job.state !== "retry_wait") {
		return rejected(job, "job_not_claimable", false);
	}
	if (job.retryAt !== null && job.retryAt > params.now) {
		return rejected(job, "retry_not_due", false);
	}
	if (job.inputWatermark <= job.lastSuccessWatermark) {
		return accepted(
			{
				...job,
				state: "succeeded",
				leaseOwner: "",
				ownershipToken: "",
				leaseExpiresAt: null,
			},
			"watermark_already_satisfied",
			false,
		);
	}
	if (!params.workerId.trim()) return rejected(job, "worker_id_required", false);
	if (!params.ownershipToken.trim()) return rejected(job, "ownership_token_required", false);
	if (!(params.leaseSeconds > 0)) return rejected(job, "lease_seconds_not_positive", false);
	return accepted(
		{
			...job,
			state: "claimed",
			leaseOwner: params.workerId,
			ownershipToken: params.ownershipToken,
			leaseEpoch: job.leaseEpoch + 1,
			leaseExpiresAt: params.now + params.leaseSeconds,
			attempt: job.attempt + 1,
			retryAt: null,
		},
		"claimed",
		false,
	);
}

export function expireJob(job: JobSnapshot, now: number): JobTransitionResult {
	if (!activeJobState(job.state)) return rejected(job, "job_not_active", false);
	if (job.leaseExpiresAt === null || job.leaseExpiresAt > now) {
		return rejected(job, "lease_not_expired", false);
	}
	if (job.retryRemaining > 0) {
		return accepted(
			{
				...clearLease(job),
				state: "retry_wait",
				retryRemaining: job.retryRemaining - 1,
				retryAt: now,
			},
			"expired_requeued_same_watermark",
			false,
		);
	}
	return accepted({ ...clearLease(job), state: "stale" }, "expired_without_retries", false);
}

function succeed(request: JobTransitionRequest): JobTransitionResult {
	const candidateCount = nonNegativeInteger(request.candidateCount, request.job.candidateCount);
	const committedCount = nonNegativeInteger(request.committedCount, request.job.committedCount);
	if (committedCount > candidateCount) {
		return rejected(request.job, "committed_count_exceeds_candidate_count", false);
	}
	const next: JobSnapshot = {
		...clearLease(request.job),
		state: "succeeded",
		lastSuccessWatermark: Math.max(request.job.lastSuccessWatermark, request.job.inputWatermark),
		candidateCount,
		committedCount,
		retryAt: null,
	};
	return accepted(next, "succeeded_and_watermark_advanced", true);
}

function fail(request: JobTransitionRequest): JobTransitionResult {
	const retryable = request.retryable === true;
	const retries = request.job.retryRemaining;
	const shouldRetry = retryable && retries > 0;
	const target: CuratorJobState = shouldRetry ? "retry_wait" : "failed";
	if (request.target !== target && !(request.target === "failed" && !shouldRetry)) {
		return rejected(request.job, `failure_target_mismatch:${request.target}:${target}`, false);
	}
	const delay = Math.max(0, request.retryDelaySeconds ?? 0);
	const next: JobSnapshot = {
		...clearLease(request.job),
		state: target,
		retryRemaining: shouldRetry ? retries - 1 : retries,
		retryAt: shouldRetry ? request.now + delay : null,
	};
	return accepted(next, shouldRetry ? "failed_retry_scheduled" : "failed_terminal", false);
}

function stale(request: JobTransitionRequest): JobTransitionResult {
	return accepted({ ...clearLease(request.job), state: "stale", retryAt: null }, "marked_stale", false);
}

function cancel(request: JobTransitionRequest): JobTransitionResult {
	return accepted({ ...clearLease(request.job), state: "cancelled", retryAt: null }, "cancelled", false);
}

function clearLease(job: JobSnapshot): JobSnapshot {
	return {
		...job,
		leaseOwner: "",
		ownershipToken: "",
		leaseExpiresAt: null,
	};
}

function nonNegativeInteger(value: number | undefined, fallback: number): number {
	if (value === undefined) return fallback;
	if (!Number.isInteger(value) || value < 0) return fallback;
	return value;
}

function fenced(reason: string): LeaseCheck {
	return { ok: false, reason, fenced: true };
}

function accepted(job: JobSnapshot, reason: string, advancedSuccessWatermark: boolean): JobTransitionResult {
	return { ok: true, job, reason, fenced: false, advancedSuccessWatermark };
}

function rejected(job: JobSnapshot, reason: string, fencedValue: boolean): JobTransitionResult {
	return { ok: false, job, reason, fenced: fencedValue, advancedSuccessWatermark: false };
}
