import assert from "node:assert/strict";
import test from "node:test";

import {
  permissionResponseChallenge,
  permissionResponseProof,
  publicPermissionResponseChallenge,
  verifyPermissionResponseProof,
  type PermissionResponseEnvelopeBinding,
} from "../src/permission/index.ts";

const NOW = new Date("2026-07-25T08:00:00.000Z");

function envelope(
  overrides: Partial<PermissionResponseEnvelopeBinding> = {},
): PermissionResponseEnvelopeBinding {
  return {
    envelopeId: "approval-envelope-1",
    requestId: "approval-request-1",
    runId: "run-1",
    taskId: "task-1",
    sessionId: "session-1",
    sessionRevision: 17,
    workerRequestId: "worker-request-1",
    toolCallId: "tool-call-1",
    argumentsDigest: "a".repeat(64),
    policyRevision: 9,
    modeRevision: 4,
    expiresAt: "2026-07-25T08:05:00.000Z",
    metadata: { request_fingerprint: "b".repeat(64) },
    ...overrides,
  };
}

function proofInput(
  binding: PermissionResponseEnvelopeBinding,
  effect: "allow" | "deny" = "allow",
) {
  const challenge = permissionResponseChallenge(binding);
  const responseId = "permission-response-1";
  return {
    version: challenge.version,
    nonce: challenge.nonce,
    canonical_owner: challenge.canonicalOwner,
    envelope_id: challenge.envelopeId,
    request_id: challenge.requestId,
    response_id: responseId,
    effect,
    decision_scope: "once",
    run_id: challenge.runId,
    task_id: challenge.taskId,
    session_id: challenge.sessionId,
    session_revision: challenge.sessionRevision,
    worker_request_id: challenge.workerRequestId,
    tool_call_id: challenge.toolCallId,
    request_fingerprint: challenge.requestFingerprint,
    arguments_digest: challenge.argumentsDigest,
    policy_revision: challenge.policyRevision,
    mode_revision: challenge.modeRevision,
    expires_at: challenge.expiresAt,
    proof: permissionResponseProof(challenge, { responseId, effect, decisionScope: "once" }),
  };
}

test("permission response challenge proves the exact active envelope", () => {
  const binding = envelope();
  const input = proofInput(binding);
  const result = verifyPermissionResponseProof(binding, input, {
    requestId: binding.requestId,
    responseId: input.response_id,
    effect: "allow",
    now: NOW,
  });
  assert.equal(result.verified, true);
  assert.equal(result.failureCode, null);
  assert.equal(result.responseDigest, input.proof);
  assert.match(result.challengeDigest, /^[0-9a-f]{64}$/);
  assert.equal(result.material.arguments_digest, binding.argumentsDigest);
  assert.equal(result.material.session_revision, binding.sessionRevision);
});

test("v2 proof binds persistent decision scope and legacy v1 remains once-only", () => {
  const binding = envelope();
  const challenge = permissionResponseChallenge(binding);
  const responseId = "permission-response-session";
  const sessionProof = {
    ...proofInput(binding),
    response_id: responseId,
    decision_scope: "session",
    proof: permissionResponseProof(challenge, {
      responseId,
      effect: "allow",
      decisionScope: "session",
    }),
  };
  const verified = verifyPermissionResponseProof(binding, sessionProof, {
    requestId: binding.requestId,
    responseId,
    effect: "allow",
    decisionScope: "session",
    now: NOW,
  });
  assert.equal(verified.verified, true);
  const widened = verifyPermissionResponseProof(binding, sessionProof, {
    requestId: binding.requestId,
    responseId,
    effect: "allow",
    decisionScope: "workspace",
    now: NOW,
  });
  assert.equal(widened.verified, false);
  assert.equal(widened.failureCode, "permission_response_scope_mismatch");

  const legacyChallenge = permissionResponseChallenge(binding, "zyra.permission-response/v1");
  const legacyResponseId = "permission-response-legacy";
  const legacyMaterial = {
    ...proofInput(binding),
    version: legacyChallenge.version,
    nonce: legacyChallenge.nonce,
    response_id: legacyResponseId,
  } as Record<string, unknown>;
  delete legacyMaterial.decision_scope;
  legacyMaterial.proof = permissionResponseProof(legacyChallenge, {
    responseId: legacyResponseId,
    effect: "allow",
  });
  const legacyOnce = verifyPermissionResponseProof(binding, legacyMaterial, {
    requestId: binding.requestId,
    responseId: legacyResponseId,
    effect: "allow",
    decisionScope: "once",
    now: NOW,
  });
  assert.equal(legacyOnce.verified, true);
  const legacyPersistent = verifyPermissionResponseProof(binding, legacyMaterial, {
    requestId: binding.requestId,
    responseId: legacyResponseId,
    effect: "allow",
    decisionScope: "workspace",
    now: NOW,
  });
  assert.equal(legacyPersistent.verified, false);
  assert.equal(legacyPersistent.failureCode, "permission_response_scope_mismatch");
});

test("public challenge is deterministic and omits response material", () => {
  const binding = envelope();
  const left = publicPermissionResponseChallenge(binding);
  const right = publicPermissionResponseChallenge(binding);
  assert.deepEqual(left, right);
  assert.equal(left.canonical_owner, "typescript.PermissionCoordinator");
  assert.match(String(left.nonce), /^permission-response-nonce-/);
  assert.match(String(left.challenge_digest), /^[0-9a-f]{64}$/);
  assert.equal("arguments_digest" in left, false);
  assert.equal("request_fingerprint" in left, false);
});

test("proof rejects stale revision, changed effect, and changed digest", () => {
  const binding = envelope();
  const input = proofInput(binding);
  const stale = verifyPermissionResponseProof(
    binding,
    { ...input, session_revision: binding.sessionRevision - 1 },
    {
      requestId: binding.requestId,
      responseId: input.response_id,
      effect: "allow",
      now: NOW,
    },
  );
  assert.equal(stale.verified, false);
  assert.equal(
    stale.failureCode,
    "permission_response_session_revision_mismatch",
  );

  const changedEffect = verifyPermissionResponseProof(
    binding,
    { ...input, effect: "deny" },
    {
      requestId: binding.requestId,
      responseId: input.response_id,
      effect: "allow",
      now: NOW,
    },
  );
  assert.equal(changedEffect.verified, false);
  assert.equal(
    changedEffect.failureCode,
    "permission_response_effect_mismatch",
  );

  const changedDigest = verifyPermissionResponseProof(
    binding,
    { ...input, arguments_digest: "c".repeat(64) },
    {
      requestId: binding.requestId,
      responseId: input.response_id,
      effect: "allow",
      now: NOW,
    },
  );
  assert.equal(changedDigest.verified, false);
  assert.equal(
    changedDigest.failureCode,
    "permission_response_arguments_digest_mismatch",
  );
});

test("proof rejects expired and replayed request identities", () => {
  const binding = envelope();
  const input = proofInput(binding, "deny");
  const expired = verifyPermissionResponseProof(binding, input, {
    requestId: binding.requestId,
    responseId: input.response_id,
    effect: "deny",
    now: new Date(binding.expiresAt),
  });
  assert.equal(expired.verified, false);
  assert.equal(expired.failureCode, "permission_response_expired");

  const replayedElsewhere = verifyPermissionResponseProof(binding, input, {
    requestId: "approval-request-other",
    responseId: input.response_id,
    effect: "deny",
    now: NOW,
  });
  assert.equal(replayedElsewhere.verified, false);
  assert.equal(
    replayedElsewhere.failureCode,
    "permission_response_request_mismatch",
  );
});
