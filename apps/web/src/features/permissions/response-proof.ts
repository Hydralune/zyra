import {
  canonicalPermissionJson,
  freshPermissionId,
  identifier,
  isoTimestamp,
  sha256Digest,
  sha256PermissionValue,
} from "./canonical.ts"
import {
  PERMISSION_CANONICAL_OWNER,
  PERMISSION_RESPONSE_VERSION,
  LEGACY_PERMISSION_RESPONSE_VERSION,
  type PermissionRequestProjection,
  type PermissionResponseDraft,
  type PermissionResponseEffect,
  type PermissionResponseProof,
} from "./contracts.ts"

export function createPermissionResponseDraft(
  request: PermissionRequestProjection,
  input: {
    effect: PermissionResponseEffect
    displayResponder: string
    feedback?: string
    now: Date
    responseId?: string
  },
): PermissionResponseDraft {
  assertRespondableRequest(request, input.now)
  if (input.effect !== "allow" && input.effect !== "deny") {
    throw responseProofError(
      "permission_response_effect_invalid",
      "Permission response effect must be allow or deny.",
    )
  }
  const displayResponder = String(input.displayResponder || "").trim()
  if (
    !displayResponder
    || /[\u0000\r\n]/.test(displayResponder)
    || new TextEncoder().encode(displayResponder).byteLength > 256
  ) {
    throw responseProofError(
      "permission_response_responder_invalid",
      "Permission response requires a bounded named responder.",
    )
  }
  const responseId = identifier(
    input.responseId
      ?? freshPermissionId("request_permission_response", input.now),
    "permission response id",
  )
  const feedback = input.feedback?.trim()
  if (feedback && new TextEncoder().encode(feedback).byteLength > 4_096) {
    throw responseProofError(
      "permission_response_feedback_too_large",
      "Permission response feedback exceeds 4 KiB.",
    )
  }
  return Object.freeze({
    request,
    responseId,
    effect: input.effect,
    displayResponder,
    feedback: feedback || undefined,
    createdAt: input.now.toISOString(),
  })
}

export async function createPermissionResponseProof(
  draft: PermissionResponseDraft,
  now: Date = new Date(),
): Promise<PermissionResponseProof> {
  assertRespondableRequest(draft.request, now)
  const challenge = draft.request.responseChallenge
  if (![PERMISSION_RESPONSE_VERSION, LEGACY_PERMISSION_RESPONSE_VERSION].includes(challenge.version)) {
    throw responseProofError(
      "permission_response_version_mismatch",
      "Permission request uses an unsupported response version.",
    )
  }
  if (challenge.canonicalOwner !== PERMISSION_CANONICAL_OWNER) {
    throw responseProofError(
      "permission_response_owner_mismatch",
      "Permission request names a different canonical owner.",
    )
  }
  if (!challenge.nonce) {
    throw responseProofError(
      "permission_response_nonce_missing",
      "Permission request did not include a response nonce.",
    )
  }
  const material = permissionResponseMaterial(
    draft.request,
    draft.responseId,
    draft.effect,
  )
  const proof = await sha256PermissionValue(material)
  return Object.freeze({
    version: challenge.version,
    ...(challenge.version === PERMISSION_RESPONSE_VERSION ? { decision_scope: "once" as const } : {}),
    nonce: challenge.nonce,
    canonical_owner: PERMISSION_CANONICAL_OWNER,
    envelope_id: draft.request.envelopeId,
    request_id: draft.request.requestId,
    response_id: draft.responseId,
    effect: draft.effect,
    run_id: draft.request.runId,
    task_id: draft.request.taskId,
    session_id: draft.request.sessionId,
    session_revision: draft.request.sessionRevision,
    worker_request_id: draft.request.workerRequestId,
    tool_call_id: draft.request.toolCallId,
    request_fingerprint: draft.request.requestFingerprint,
    arguments_digest: draft.request.argumentsDigest,
    policy_revision: draft.request.policyRevision,
    mode_revision: draft.request.modeRevision,
    expires_at: draft.request.expiresAt,
    proof,
  })
}

export function permissionResponseMaterial(
  request: PermissionRequestProjection,
  responseIdValue: string,
  effect: PermissionResponseEffect,
): Readonly<Record<string, unknown>> {
  const responseId = identifier(responseIdValue, "permission response id")
  if (effect !== "allow" && effect !== "deny") {
    throw responseProofError(
      "permission_response_effect_invalid",
      "Permission response effect must be allow or deny.",
    )
  }
  const challenge = request.responseChallenge
  return Object.freeze({
    version: challenge.version,
    nonce: challenge.nonce,
    canonical_owner: challenge.canonicalOwner,
    envelope_id: request.envelopeId,
    request_id: request.requestId,
    response_id: responseId,
    effect,
    ...(challenge.version === PERMISSION_RESPONSE_VERSION ? { decision_scope: "once" } : {}),
    run_id: request.runId,
    task_id: request.taskId,
    session_id: request.sessionId,
    session_revision: request.sessionRevision,
    worker_request_id: request.workerRequestId,
    tool_call_id: request.toolCallId,
    request_fingerprint: request.requestFingerprint,
    arguments_digest: request.argumentsDigest,
    policy_revision: request.policyRevision,
    mode_revision: request.modeRevision,
    expires_at: request.expiresAt,
  })
}

export async function verifyLocalPermissionResponseProof(
  request: PermissionRequestProjection,
  proof: PermissionResponseProof,
): Promise<boolean> {
  if (
    proof.version !== request.responseChallenge.version
    || (proof.version === PERMISSION_RESPONSE_VERSION && proof.decision_scope !== "once")
    || proof.canonical_owner !== PERMISSION_CANONICAL_OWNER
    || proof.nonce !== request.responseChallenge.nonce
    || proof.envelope_id !== request.envelopeId
    || proof.request_id !== request.requestId
    || proof.run_id !== request.runId
    || proof.task_id !== request.taskId
    || proof.session_id !== request.sessionId
    || proof.session_revision !== request.sessionRevision
    || proof.worker_request_id !== request.workerRequestId
    || proof.tool_call_id !== request.toolCallId
    || proof.request_fingerprint !== request.requestFingerprint
    || proof.arguments_digest !== request.argumentsDigest
    || proof.policy_revision !== request.policyRevision
    || proof.mode_revision !== request.modeRevision
    || proof.expires_at !== request.expiresAt
  ) {
    return false
  }
  const expected = await sha256PermissionValue(
    permissionResponseMaterial(
      request,
      proof.response_id,
      proof.effect,
    ),
  )
  return constantTimeTextEquals(expected, proof.proof)
}

export function responseProofSummary(
  proof: PermissionResponseProof,
): Readonly<Record<string, string | number>> {
  return Object.freeze({
    version: proof.version,
    canonical_owner: proof.canonical_owner,
    request_id: proof.request_id,
    response_id: proof.response_id,
    effect: proof.effect,
    session_revision: proof.session_revision,
    policy_revision: proof.policy_revision,
    mode_revision: proof.mode_revision,
    arguments_digest: proof.arguments_digest,
    request_fingerprint: proof.request_fingerprint,
    expires_at: proof.expires_at,
    proof: proof.proof,
  })
}

export function assertRespondableRequest(
  request: PermissionRequestProjection,
  now: Date,
): void {
  if (!request.selectable) {
    throw responseProofError(
      request.terminal
        ? "permission_request_terminal"
        : request.stale
          ? "permission_request_stale"
          : "permission_request_not_delivered",
      "Permission request is not active and selectable.",
    )
  }
  if (request.canonicalOwner !== PERMISSION_CANONICAL_OWNER) {
    throw responseProofError(
      "permission_request_owner_mismatch",
      "Permission request is not owned by the canonical runtime.",
    )
  }
  if (Date.parse(request.expiresAt) <= now.getTime()) {
    throw responseProofError(
      "permission_request_expired",
      "Permission request expired before response submission.",
    )
  }
  sha256Digest(
    request.requestFingerprint,
    "permission request fingerprint",
  )
  sha256Digest(request.argumentsDigest, "permission arguments digest")
  isoTimestamp(request.expiresAt, "permission request expiry")
}

export function responseProofFingerprint(
  proof: PermissionResponseProof,
): string {
  const source = canonicalPermissionJson(responseProofSummary(proof))
  let hash = 0x811c9dc5
  for (const byte of new TextEncoder().encode(source)) {
    hash = Math.imul(hash ^ byte, 0x01000193) >>> 0
  }
  return hash.toString(16).padStart(8, "0")
}

function constantTimeTextEquals(left: string, right: string): boolean {
  if (left.length !== right.length) return false
  let difference = 0
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index)
  }
  return difference === 0
}

function responseProofError(code: string, message: string): Error {
  return Object.assign(new Error(message), {
    name: "PermissionResponseProofError",
    code,
  })
}
