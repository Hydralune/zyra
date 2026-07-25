import type { JsonObject, JsonValue } from "../contracts.ts";
import {
  canonicalize,
  constantTimeDigestEquals,
  deterministicId,
  digest,
} from "../e02/canonical.ts";

export const PERMISSION_RESPONSE_VERSION = "zyra.permission-response/v1";
export const PERMISSION_RESPONSE_OWNER = "typescript.PermissionCoordinator";

export interface PermissionResponseEnvelopeBinding {
  envelopeId: string;
  requestId: string;
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
  argumentsDigest: string;
  policyRevision: number;
  modeRevision: number;
  expiresAt: string;
  metadata: JsonObject;
}

export interface PermissionResponseChallenge extends JsonObject {
  version: typeof PERMISSION_RESPONSE_VERSION;
  nonce: string;
  canonicalOwner: typeof PERMISSION_RESPONSE_OWNER;
  envelopeId: string;
  requestId: string;
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
  requestFingerprint: string;
  argumentsDigest: string;
  policyRevision: number;
  modeRevision: number;
  expiresAt: string;
}

export interface PermissionResponseProofInput extends JsonObject {
  version: string;
  nonce: string;
  canonicalOwner: string;
  envelopeId: string;
  requestId: string;
  responseId: string;
  effect: "allow" | "deny";
  runId: string;
  taskId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
  requestFingerprint: string;
  argumentsDigest: string;
  policyRevision: number;
  modeRevision: number;
  expiresAt: string;
  proof: string;
}

export interface PermissionResponseProofResult extends JsonObject {
  verified: boolean;
  responseDigest: string;
  challengeDigest: string;
  failureCode: string | null;
  failureMessage: string | null;
  material: JsonObject;
}

export function permissionResponseChallenge(
  envelope: PermissionResponseEnvelopeBinding,
): PermissionResponseChallenge {
  const requestFingerprint = text(
    envelope.metadata.request_fingerprint,
    "request_fingerprint",
  );
  const base = {
    canonicalOwner: PERMISSION_RESPONSE_OWNER,
    envelopeId: required(envelope.envelopeId, "envelope_id"),
    requestId: required(envelope.requestId, "request_id"),
    runId: required(envelope.runId, "run_id"),
    taskId: required(envelope.taskId, "task_id"),
    sessionId: required(envelope.sessionId, "session_id"),
    sessionRevision: revision(envelope.sessionRevision, "session_revision"),
    workerRequestId: required(envelope.workerRequestId, "worker_request_id"),
    toolCallId: required(envelope.toolCallId, "tool_call_id"),
    requestFingerprint,
    argumentsDigest: sha256(envelope.argumentsDigest, "arguments_digest"),
    policyRevision: revision(envelope.policyRevision, "policy_revision"),
    modeRevision: revision(envelope.modeRevision, "mode_revision"),
    expiresAt: timestamp(envelope.expiresAt, "expires_at"),
  };
  return canonicalize({
    version: PERMISSION_RESPONSE_VERSION,
    nonce: deterministicId("permission-response-nonce", base, 48),
    ...base,
  }) as PermissionResponseChallenge;
}

export function permissionResponseMaterial(
  challenge: PermissionResponseChallenge,
  response: Pick<
    PermissionResponseProofInput,
    "responseId" | "effect"
  >,
): JsonObject {
  const responseId = required(response.responseId, "response_id");
  const effect = response.effect;
  if (effect !== "allow" && effect !== "deny") {
    throw responseProofError(
      "permission_response_effect_invalid",
      "permission response effect must be allow or deny",
    );
  }
  return canonicalize({
    version: challenge.version,
    nonce: challenge.nonce,
    canonical_owner: challenge.canonicalOwner,
    envelope_id: challenge.envelopeId,
    request_id: challenge.requestId,
    response_id: responseId,
    effect,
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
  }) as JsonObject;
}

export function permissionResponseProof(
  challenge: PermissionResponseChallenge,
  response: Pick<PermissionResponseProofInput, "responseId" | "effect">,
): string {
  return digest(permissionResponseMaterial(challenge, response));
}

export function parsePermissionResponseProof(
  value: unknown,
): PermissionResponseProofInput {
  const record = object(value, "permission response proof");
  const effect = text(record.effect, "effect");
  if (effect !== "allow" && effect !== "deny") {
    throw responseProofError(
      "permission_response_effect_invalid",
      "permission response proof effect must be allow or deny",
    );
  }
  return canonicalize({
    version: text(record.version, "version"),
    nonce: text(record.nonce, "nonce"),
    canonicalOwner: text(
      record.canonical_owner ?? record.canonicalOwner,
      "canonical_owner",
    ),
    envelopeId: text(record.envelope_id ?? record.envelopeId, "envelope_id"),
    requestId: text(record.request_id ?? record.requestId, "request_id"),
    responseId: text(record.response_id ?? record.responseId, "response_id"),
    effect,
    runId: text(record.run_id ?? record.runId, "run_id"),
    taskId: text(record.task_id ?? record.taskId, "task_id"),
    sessionId: text(record.session_id ?? record.sessionId, "session_id"),
    sessionRevision: revision(
      record.session_revision ?? record.sessionRevision,
      "session_revision",
    ),
    workerRequestId: text(
      record.worker_request_id ?? record.workerRequestId,
      "worker_request_id",
    ),
    toolCallId: text(record.tool_call_id ?? record.toolCallId, "tool_call_id"),
    requestFingerprint: sha256(
      record.request_fingerprint ?? record.requestFingerprint,
      "request_fingerprint",
    ),
    argumentsDigest: sha256(
      record.arguments_digest ?? record.argumentsDigest,
      "arguments_digest",
    ),
    policyRevision: revision(
      record.policy_revision ?? record.policyRevision,
      "policy_revision",
    ),
    modeRevision: revision(
      record.mode_revision ?? record.modeRevision,
      "mode_revision",
    ),
    expiresAt: timestamp(record.expires_at ?? record.expiresAt, "expires_at"),
    proof: sha256(record.proof, "proof"),
  }) as PermissionResponseProofInput;
}

export function verifyPermissionResponseProof(
  envelope: PermissionResponseEnvelopeBinding,
  value: unknown,
  expected: {
    requestId: string;
    responseId: string;
    effect: "allow" | "deny";
    now?: Date;
  },
): PermissionResponseProofResult {
  const challenge = permissionResponseChallenge(envelope);
  const challengeDigest = digest(challenge);
  let provided: PermissionResponseProofInput;
  try {
    provided = parsePermissionResponseProof(value);
  } catch (error) {
    return rejected(
      challengeDigest,
      "permission_response_proof_invalid",
      error instanceof Error ? error.message : String(error),
    );
  }
  const echoFailures: Array<[boolean, string, string]> = [
    [
      provided.version !== challenge.version,
      "permission_response_version_mismatch",
      "permission response version is stale or unsupported",
    ],
    [
      provided.canonicalOwner !== challenge.canonicalOwner,
      "permission_response_owner_mismatch",
      "permission response names a different canonical owner",
    ],
    [
      provided.nonce !== challenge.nonce,
      "permission_response_nonce_mismatch",
      "permission response nonce does not match the active envelope",
    ],
    [
      provided.envelopeId !== challenge.envelopeId,
      "permission_response_envelope_mismatch",
      "permission response envelope identity does not match",
    ],
    [
      provided.requestId !== challenge.requestId
        || provided.requestId !== expected.requestId,
      "permission_response_request_mismatch",
      "permission response request identity does not match",
    ],
    [
      provided.responseId !== expected.responseId,
      "permission_response_id_mismatch",
      "permission response idempotency identity does not match",
    ],
    [
      provided.effect !== expected.effect,
      "permission_response_effect_mismatch",
      "permission response effect changed after proof creation",
    ],
    [
      provided.runId !== challenge.runId,
      "permission_response_run_mismatch",
      "permission response belongs to a different run",
    ],
    [
      provided.taskId !== challenge.taskId,
      "permission_response_task_mismatch",
      "permission response belongs to a different task",
    ],
    [
      provided.sessionId !== challenge.sessionId,
      "permission_response_session_mismatch",
      "permission response belongs to a different session",
    ],
    [
      provided.sessionRevision !== challenge.sessionRevision,
      "permission_response_session_revision_mismatch",
      "permission response session revision is stale",
    ],
    [
      provided.workerRequestId !== challenge.workerRequestId,
      "permission_response_worker_request_mismatch",
      "permission response worker request identity does not match",
    ],
    [
      provided.toolCallId !== challenge.toolCallId,
      "permission_response_tool_call_mismatch",
      "permission response tool call identity does not match",
    ],
    [
      provided.requestFingerprint !== challenge.requestFingerprint,
      "permission_response_fingerprint_mismatch",
      "permission response request fingerprint was forged or is stale",
    ],
    [
      provided.argumentsDigest !== challenge.argumentsDigest,
      "permission_response_arguments_digest_mismatch",
      "permission response arguments digest was forged or is stale",
    ],
    [
      provided.policyRevision !== challenge.policyRevision,
      "permission_response_policy_revision_mismatch",
      "permission response policy revision is stale",
    ],
    [
      provided.modeRevision !== challenge.modeRevision,
      "permission_response_mode_revision_mismatch",
      "permission response mode revision is stale",
    ],
    [
      provided.expiresAt !== challenge.expiresAt,
      "permission_response_expiry_mismatch",
      "permission response expiry was changed",
    ],
  ];
  const failed = echoFailures.find(([condition]) => condition);
  if (failed) return rejected(challengeDigest, failed[1], failed[2]);
  const now = expected.now ?? new Date();
  if (
    !Number.isFinite(now.getTime())
    || Date.parse(challenge.expiresAt) <= now.getTime()
  ) {
    return rejected(
      challengeDigest,
      "permission_response_expired",
      "permission response challenge has expired",
    );
  }
  const material = permissionResponseMaterial(challenge, {
    responseId: provided.responseId,
    effect: provided.effect,
  });
  const expectedProof = digest(material);
  if (!constantTimeDigestEquals(expectedProof, provided.proof)) {
    return rejected(
      challengeDigest,
      "permission_response_proof_mismatch",
      "permission response binding proof does not match its canonical fields",
      material,
    );
  }
  return {
    verified: true,
    responseDigest: expectedProof,
    challengeDigest,
    failureCode: null,
    failureMessage: null,
    material,
  };
}

export function publicPermissionResponseChallenge(
  envelope: PermissionResponseEnvelopeBinding,
): JsonObject {
  const challenge = permissionResponseChallenge(envelope);
  return canonicalize({
    version: challenge.version,
    nonce: challenge.nonce,
    canonical_owner: challenge.canonicalOwner,
    challenge_digest: digest(challenge),
  }) as JsonObject;
}

function rejected(
  challengeDigest: string,
  failureCode: string,
  failureMessage: string,
  material: JsonObject = {},
): PermissionResponseProofResult {
  return {
    verified: false,
    responseDigest: material && Object.keys(material).length
      ? digest(material)
      : digest({ failureCode, challengeDigest }),
    challengeDigest,
    failureCode,
    failureMessage,
    material,
  };
}

function object(value: unknown, label: string): Record<string, JsonValue> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw responseProofError(
      "permission_response_proof_invalid",
      `${label} must be an object`,
    );
  }
  return value as Record<string, JsonValue>;
}

function required(value: unknown, label: string): string {
  const rendered = typeof value === "string" ? value.trim() : "";
  if (!rendered || rendered.length > 512 || /[\u0000\r\n]/.test(rendered)) {
    throw responseProofError(
      "permission_response_field_invalid",
      `${label} must be a bounded non-empty string`,
    );
  }
  return rendered;
}

function text(value: unknown, label: string): string {
  return required(value, label);
}

function revision(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) {
    throw responseProofError(
      "permission_response_revision_invalid",
      `${label} must be a non-negative safe integer`,
    );
  }
  return Number(value);
}

function timestamp(value: unknown, label: string): string {
  const rendered = required(value, label);
  if (Number.isNaN(Date.parse(rendered))) {
    throw responseProofError(
      "permission_response_timestamp_invalid",
      `${label} must be an ISO-8601 timestamp`,
    );
  }
  return new Date(Date.parse(rendered)).toISOString();
}

function sha256(value: unknown, label: string): string {
  const rendered = required(value, label).toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(rendered)) {
    throw responseProofError(
      "permission_response_digest_invalid",
      `${label} must be a SHA-256 hex digest`,
    );
  }
  return rendered;
}

function responseProofError(code: string, message: string): Error {
  return Object.assign(new Error(message), {
    name: "PermissionResponseProofError",
    code,
  });
}
