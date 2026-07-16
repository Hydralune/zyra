import { describe, expect, test } from "bun:test";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { runtimeVerificationProjection } from "../../src/stdio.js";

const evidenceDirectory = join(
  "docs",
  "reviews",
  "evidence",
  "M1-R01-v3",
  "execution-01",
);

describe("E01 health verification projection", () => {
  test("keeps productized health incomplete when candidate evidence is unavailable", () => {
    const root = mkdtempSync(join(tmpdir(), "zyra-e01-health-missing-"));
    try {
      const projection = runtimeVerificationProjection(root);
      expect(projection.complete).toBe(false);
      expect(projection.verificationStatus).toBe("candidate_metadata_unavailable");
      expect(projection.effectiveLineCount).toBeNull();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("reports completion only for identity-bound independent PASS evidence", () => {
    const root = mkdtempSync(join(tmpdir(), "zyra-e01-health-pass-"));
    const directory = join(root, evidenceDirectory);
    const candidate = "a".repeat(40);
    const candidateOverride = process.env.E01_IMPLEMENTATION_CANDIDATE;
    try {
      delete process.env.E01_IMPLEMENTATION_CANDIDATE;
      mkdirSync(directory, { recursive: true });
      writeFileSync(
        join(directory, "candidate-metadata.json"),
        JSON.stringify({
          verification_contract_version: "zyra.e01-verification/v7",
          implementation_candidate: candidate,
          cleanroom_target: candidate,
          candidate_evidence_commit: "b".repeat(40),
          independent_review_target: "c".repeat(40),
          independent_review_commit: "d".repeat(40),
          candidate_status: "independent_review_passed",
          independent_review_verdict: "PASS",
          verified_complete: true,
        }),
      );
      writeFileSync(
        join(directory, "strict-gate.json"),
        JSON.stringify({
          ok: true,
          candidate,
          checks: { effective_lines: { effective_changed_typescript: 30_769 } },
        }),
      );
      const projection = runtimeVerificationProjection(root);
      expect(projection.complete).toBe(true);
      expect(projection.verificationStatus).toBe("independent_review_passed");
      expect(projection.effectiveLineCount).toBe(30_769);
    } finally {
      if (candidateOverride === undefined) delete process.env.E01_IMPLEMENTATION_CANDIDATE;
      else process.env.E01_IMPLEMENTATION_CANDIDATE = candidateOverride;
      rmSync(root, { recursive: true, force: true });
    }
  });

  test("preserves identity-bound V7 pending-review health without claiming completion", () => {
    const root = mkdtempSync(join(tmpdir(), "zyra-e01-health-pending-"));
    const directory = join(root, evidenceDirectory);
    const candidate = "1".repeat(40);
    const evidence = "2".repeat(40);
    const reviewTarget = "3".repeat(40);
    const candidateOverride = process.env.E01_IMPLEMENTATION_CANDIDATE;
    try {
      delete process.env.E01_IMPLEMENTATION_CANDIDATE;
      mkdirSync(directory, { recursive: true });
      writeFileSync(join(directory, "candidate-metadata.json"), JSON.stringify({
        verification_contract_version: "zyra.e01-verification/v7",
        implementation_candidate: candidate,
        cleanroom_target: candidate,
        candidate_evidence_commit: evidence,
        independent_review_target: reviewTarget,
        independent_review_commit: null,
        candidate_status: "independent_review_pending",
        independent_review_verdict: "PENDING",
        verified_complete: false,
      }));
      writeFileSync(join(directory, "strict-gate.json"), JSON.stringify({
        ok: true,
        candidate,
        checks: { effective_lines: { effective_changed_typescript: 30_769 } },
      }));
      const projection = runtimeVerificationProjection(root);
      expect(projection.complete).toBe(false);
      expect(projection.verificationStatus).toBe("independent_review_pending");
      expect(projection.implementationCandidate).toBe(candidate);
      expect(projection.evidenceCommit).toBe(evidence);
      expect(projection.reviewTarget).toBe(reviewTarget);
      expect(projection.reviewCommit).toBeNull();
    } finally {
      if (candidateOverride === undefined) delete process.env.E01_IMPLEMENTATION_CANDIDATE;
      else process.env.E01_IMPLEMENTATION_CANDIDATE = candidateOverride;
      rmSync(root, { recursive: true, force: true });
    }
  });
});
