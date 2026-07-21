import { describe, expect, test } from "bun:test";

import { consolidateCandidates, validateCandidate } from "../src/consolidation";
import { acquireJob, expireJob, transitionJob, validateLease } from "../src/job-state-machine";
import type { CandidateEnvelope, JobLease, JobSnapshot } from "../src/types";

function job(overrides: Partial<JobSnapshot> = {}): JobSnapshot {
	return {
		jobId: "job-1",
		taskId: "task-1",
		state: "queued",
		inputWatermark: 20,
		lastSuccessWatermark: 10,
		leaseOwner: "",
		ownershipToken: "",
		leaseEpoch: 0,
		leaseExpiresAt: null,
		attempt: 0,
		retryRemaining: 3,
		retryAt: null,
		candidateCount: 0,
		committedCount: 0,
		...overrides,
	};
}

function lease(value: JobSnapshot): JobLease {
	return {
		jobId: value.jobId,
		taskId: value.taskId,
		workerId: value.leaseOwner,
		ownershipToken: value.ownershipToken,
		leaseEpoch: value.leaseEpoch,
		inputWatermark: value.inputWatermark,
		expiresAt: value.leaseExpiresAt ?? 0,
		attempt: value.attempt,
	};
}

function candidate(overrides: Partial<CandidateEnvelope> = {}): CandidateEnvelope {
	return {
		candidateId: "candidate-1",
		runId: "run-1",
		taskId: "task-1",
		kind: "promote",
		layer: "semantic",
		scope: "task",
		subject: "requirement:lease-fence",
		summary: "Lease fencing must remain enabled.",
		content: { fact: "enabled" },
		evidenceDigest: "a".repeat(64),
		evidenceIds: ["evidence-1"],
		confidence: 0.9,
		idempotencyKey: "candidate:1",
		semanticDigest: "b".repeat(64),
		state: "proposed",
		expectedMemoryId: "",
		expectedRevision: null,
		createdAt: "2026-07-21T00:00:00Z",
		...overrides,
	};
}

describe("curator job ownership and watermarks", () => {
	test("claim, advance, succeed and fence a prior lease", () => {
		const claimed = acquireJob(job(), {
			workerId: "worker-a",
			ownershipToken: "token-a",
			now: 100,
			leaseSeconds: 10,
		});
		expect(claimed.ok).toBe(true);
		const claimedLease = lease(claimed.job);
		const extracting = transitionJob({ job: claimed.job, lease: claimedLease, target: "extracting", now: 101 });
		expect(extracting.ok).toBe(true);
		const deciding = transitionJob({ job: extracting.job, lease: claimedLease, target: "deciding", now: 102 });
		expect(deciding.ok).toBe(true);
		const validating = transitionJob({ job: deciding.job, lease: claimedLease, target: "validating", now: 103 });
		expect(validating.ok).toBe(true);
		const committing = transitionJob({ job: validating.job, lease: claimedLease, target: "committing", now: 104 });
		expect(committing.ok).toBe(true);
		const succeeded = transitionJob({
			job: committing.job,
			lease: claimedLease,
			target: "succeeded",
			now: 105,
			candidateCount: 4,
			committedCount: 3,
		});
		expect(succeeded.ok).toBe(true);
		expect(succeeded.job.lastSuccessWatermark).toBe(20);
		expect(succeeded.advancedSuccessWatermark).toBe(true);
		expect(validateLease(succeeded.job, claimedLease, 106).fenced).toBe(true);
	});

	test("expired owner is requeued at the same input watermark", () => {
		const claimed = acquireJob(job(), {
			workerId: "worker-a",
			ownershipToken: "token-a",
			now: 100,
			leaseSeconds: 2,
		});
		const expired = expireJob(claimed.job, 103);
		expect(expired.ok).toBe(true);
		expect(expired.job.state).toBe("retry_wait");
		expect(expired.job.inputWatermark).toBe(20);
		expect(expired.job.lastSuccessWatermark).toBe(10);
		const takeover = acquireJob(expired.job, {
			workerId: "worker-b",
			ownershipToken: "token-b",
			now: 103,
			leaseSeconds: 2,
		});
		expect(takeover.ok).toBe(true);
		expect(takeover.job.leaseEpoch).toBe(2);
		expect(validateLease(takeover.job, lease(claimed.job), 103).fenced).toBe(true);
	});
});

describe("candidate validation and explicit consolidation", () => {
	test("valid candidate is accepted by protocol schema", () => {
		const result = validateCandidate(candidate());
		expect(result.ok).toBe(true);
		expect(result.normalized?.candidateId).toBe("candidate-1");
	});

	test("duplicate and contradiction become explicit relations", () => {
		const prior = candidate();
		const duplicate = candidate({ candidateId: "candidate-2", idempotencyKey: "candidate:2" });
		const contradiction = candidate({
			candidateId: "candidate-3",
			idempotencyKey: "candidate:3",
			semanticDigest: "c".repeat(64),
			summary: "Lease fencing must not remain enabled.",
			content: { fact: "disabled" },
		});
		const result = consolidateCandidates({ candidates: [duplicate, contradiction], priorCandidates: [prior] });
		expect(result.suppressedCandidateIds).toContain("candidate-2");
		expect(result.activeCandidateIds).toContain("candidate-3");
		expect(result.relations.map(item => item.kind)).toEqual(["duplicate_of", "contradicts"]);
		expect(result.diagnostics.lastWriteWins).toBe(false);
	});
});
