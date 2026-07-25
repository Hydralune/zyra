import {
  boundedPermissionText,
  comparePermissionTime,
  identifier,
  isoTimestamp,
  nonNegativeRevision,
  optionalPermissionRecord,
  optionalPermissionText,
  permissionArray,
  permissionRecord,
  safePermissionClone,
  sha256Digest,
  stablePermissionId,
} from "./canonical.ts"
import {
  PERMISSION_CANONICAL_OWNER,
  PERMISSION_RESPONSE_VERSION,
  type PermissionDecisionProjection,
  type PermissionDecisionReceipt,
  type PermissionEffect,
  type PermissionModeProjection,
  type PermissionRequestBinding,
  type PermissionRequestProjection,
  type PermissionRequestStatus,
  type PermissionResponseChallenge,
  type PermissionResponseEffect,
  type PermissionRuleProjection,
} from "./contracts.ts"
import {
  assertPermissionPreviewSafe,
  buildPermissionArgumentPreview,
  redactPermissionText,
} from "./redaction.ts"
import {
  detectPermissionHazards,
  permissionRiskLevel,
  permissionSourceSurface,
  permissionWarnings,
} from "./warning-policy.ts"

export interface PermissionProjectionContext {
  now: Date
  productMode: "interactive" | "sealed"
  policyFrozen: boolean
  defaultPolicyHash?: string
  defaultPolicyRevision?: number
  humanInterventionCount?: number
}

export interface PermissionBackendProjection {
  requests: readonly PermissionRequestProjection[]
  mode: PermissionModeProjection
  rules: readonly PermissionRuleProjection[]
  decisions: readonly PermissionDecisionProjection[]
  sourceOwner: string
  sourceSchema: string
}

export function projectPermissionBackend(
  bodyValue: unknown,
  context: PermissionProjectionContext,
): PermissionBackendProjection {
  const body = permissionRecord(bodyValue, "permission API response")
  const sourceOwner = text(
    body.state_owner ?? body.stateOwner,
    PERMISSION_CANONICAL_OWNER,
  )
  const sourceSchema = text(body.schema, "zyra.permission-api.v2")
  const mode = projectPermissionMode(body.mode, body.health, {
    ...context,
    sourceOwner,
  })
  const requestContainer = optionalPermissionRecord(body.requests)
  const requestItems =
    body.request !== undefined && body.request !== null
      ? [body.request]
      : permissionArray(requestContainer.items ?? body.requests)
  const requests = requestItems
    .map((item) => {
      try {
        return projectPermissionRequest(item, { ...context, mode })
      } catch {
        return undefined
      }
    })
    .filter(
      (item): item is PermissionRequestProjection => item !== undefined,
    )
    .sort(compareRequests)
  const rules = permissionArray(body.rules)
    .map((item) => projectPermissionRule(item))
    .filter((item): item is PermissionRuleProjection => Boolean(item))
    .sort(
      (left, right) =>
        right.priority - left.priority
        || left.ruleId.localeCompare(right.ruleId),
    )
  const decisionContainer = optionalPermissionRecord(body.decisions)
  const decisions = permissionArray(
    decisionContainer.items ?? body.decisions,
  )
    .map((item) => projectPermissionDecision(item))
    .filter((item): item is PermissionDecisionProjection => Boolean(item))
    .sort(
      (left, right) =>
        comparePermissionTime(left.evaluatedAt, right.evaluatedAt)
        || left.decisionId.localeCompare(right.decisionId),
    )
  return Object.freeze({
    requests: Object.freeze(requests),
    mode,
    rules: Object.freeze(rules),
    decisions: Object.freeze(decisions),
    sourceOwner,
    sourceSchema,
  })
}

export function projectPermissionRequest(
  value: unknown,
  context: PermissionProjectionContext & { mode: PermissionModeProjection },
): PermissionRequestProjection {
  const item = permissionRecord(value, "permission request")
  const requestBinding = optionalPermissionRecord(
    item.request_binding ?? item.requestBinding,
  )
  const toolIdentity = optionalPermissionRecord(
    item.tool_identity ?? item.toolIdentity,
  )
  const scope = optionalPermissionRecord(item.scope)
  const binding: PermissionRequestBinding = {
    toolName: text(
      item.tool_name
        ?? item.toolName
        ?? requestBinding.tool_name
        ?? toolIdentity.name,
      "unknown-tool",
    ),
    namespace: text(
      item.namespace
        ?? requestBinding.namespace
        ?? toolIdentity.namespace,
      "builtin",
    ),
    serverId: text(
      item.server_id
        ?? item.serverId
        ?? requestBinding.server_id
        ?? toolIdentity.server_id,
      "",
    ),
    operation: text(
      item.operation
        ?? requestBinding.operation
        ?? scope.operation_pattern,
      "execute",
    ),
    commandName: safeBindingText(
      requestBinding.command_name
        ?? toolIdentity.command_name
        ?? item.command_name,
      "command_name",
      "command",
    ),
    resourceUri: safeBindingText(
      requestBinding.resource_uri
        ?? toolIdentity.resource_uri
        ?? item.resource_uri,
      "resource_uri",
      "url",
    ),
    workspaceRoot: safeBindingText(
      requestBinding.workspace_root
        ?? scope.workspace_pattern
        ?? item.workspace_root,
      "workspace_root",
      "path",
    ),
    capabilityRevision: optionalRevision(
      requestBinding.capability_revision
        ?? item.capability_revision,
    ),
    pluginRevision: optionalRevision(
      requestBinding.plugin_revision ?? item.plugin_revision,
    ),
    skillRevision: optionalRevision(
      requestBinding.skill_revision ?? item.skill_revision,
    ),
    schemaDigest: optionalDigest(
      requestBinding.schema_digest ?? item.schema_digest,
    ),
  }
  const requestId = identifier(
    item.request_id ?? item.requestId,
    "permission request id",
  )
  const argumentsDigest = sha256Digest(
    item.arguments_digest
      ?? item.argumentsDigest
      ?? requestBinding.arguments_digest,
    "permission arguments digest",
  )
  const requestFingerprint = sha256Digest(
    item.request_fingerprint
      ?? item.requestFingerprint
      ?? requestBinding.request_fingerprint,
    "permission request fingerprint",
  )
  const expiresAt = isoTimestamp(
    item.expires_at ?? item.expiresAt,
    "permission expiry",
  )
  const status = permissionRequestStatus(
    item.status ?? item.phase,
  )
  const nowMs = context.now.getTime()
  const expired = Date.parse(expiresAt) <= nowMs || status === "expired"
  const terminal =
    expired
    || status === "responded"
    || status === "cancelled"
  const policyRevision = revisionOr(
    item.policy_revision ?? item.policyRevision,
    context.mode.policyRevision,
  )
  const modeRevision = revisionOr(
    item.mode_revision ?? item.modeRevision,
    context.mode.revision,
  )
  const challenge = permissionResponseChallenge(item)
  const preview = buildPermissionArgumentPreview(
    requestBinding,
    argumentsDigest,
  )
  assertPermissionPreviewSafe(preview)
  const sourceSurface = permissionSourceSurface(binding)
  const warnings = permissionWarnings(binding, preview, {
    policyFrozen: context.mode.frozen,
    sealed: context.mode.productMode === "sealed",
    expiresAt,
    now: context.now,
    hazards: detectPermissionHazards(requestBinding.arguments),
  })
  const responseAccepted =
    item.response_accepted === true || item.responseAccepted === true
  const stale =
    policyRevision !== context.mode.policyRevision
    || modeRevision !== context.mode.revision
    || challenge.canonicalOwner !== PERMISSION_CANONICAL_OWNER
  return Object.freeze({
    envelopeId: identifier(
      item.envelope_id
        ?? item.envelopeId
        ?? stablePermissionId("envelope", requestId),
      "permission envelope id",
    ),
    requestId,
    decisionId: identifier(
      item.decision_id
        ?? item.decisionId
        ?? stablePermissionId("decision", requestId),
      "permission decision id",
    ),
    runId: identifier(
      item.run_id ?? item.runId ?? requestBinding.run_id,
      "permission run id",
    ),
    taskId: identifier(
      item.task_id ?? item.taskId ?? requestBinding.task_id,
      "permission task id",
    ),
    sessionId: identifier(
      item.session_id ?? item.sessionId ?? requestBinding.session_id,
      "permission session id",
    ),
    sessionRevision: revisionOr(
      item.session_revision
        ?? item.sessionRevision
        ?? requestBinding.session_revision,
      0,
    ),
    workerRequestId: identifier(
      item.worker_request_id
        ?? item.workerRequestId
        ?? requestBinding.worker_request_id
        ?? item.tool_use_id,
      "permission worker request id",
    ),
    toolCallId: identifier(
      item.tool_call_id
        ?? item.toolCallId
        ?? requestBinding.tool_call_id
        ?? item.tool_use_id,
      "permission tool call id",
    ),
    requestFingerprint,
    argumentsDigest,
    policyRevision,
    modeRevision,
    expiresAt,
    canonicalOwner: PERMISSION_CANONICAL_OWNER,
    ...binding,
    status,
    prompt: safeNarrative(
      item.prompt,
      `Allow ${binding.toolName} for this exact request?`,
      4_096,
    ),
    reason: safeNarrative(
      item.reason ?? item.reason_code,
      "The canonical permission runtime requires an exact response.",
      8_192,
    ),
    createdAt: isoTimestamp(
      item.created_at ?? item.createdAt,
      "permission created timestamp",
      context.now,
    ),
    updatedAt: isoTimestamp(
      item.updated_at
        ?? item.updatedAt
        ?? item.delivered_at
        ?? item.created_at,
      "permission updated timestamp",
      context.now,
    ),
    deliveredAt: optionalTimestamp(
      item.delivered_at ?? item.deliveredAt,
    ),
    deliveryAttempt: revisionOr(
      item.delivery_attempt ?? item.deliveryAttempt,
      status === "delivered" ? 1 : 0,
    ),
    deliveryReceiptId: optionalId(
      item.delivery_receipt_id ?? item.deliveryReceiptId,
      "permission delivery receipt id",
    ),
    responseId: optionalId(
      item.response_id ?? item.responseId,
      "permission response id",
    ),
    responseEffect: responseEffect(
      item.response_effect
        ?? item.responseEffect
        ?? item.resolution_effect,
    ),
    responseAccepted,
    responder: optionalPermissionText(
      item.responder ?? item.resolved_by,
      "permission responder",
      256,
    ),
    sourceSurface,
    riskLevel: permissionRiskLevel(binding, preview),
    responseChallenge: challenge,
    redactedPreview: preview,
    warnings: Object.freeze(warnings),
    terminal,
    expired,
    stale,
    selectable:
      status === "delivered"
      && !terminal
      && !stale
      && challenge.version === PERMISSION_RESPONSE_VERSION
      && challenge.nonce.length > 0,
    raw: safeRequestAudit(item, binding, challenge),
  })
}

export function projectPermissionMode(
  value: unknown,
  healthValue: unknown,
  context: PermissionProjectionContext & { sourceOwner: string },
): PermissionModeProjection {
  const modeRecord = optionalPermissionRecord(value)
  const health = optionalPermissionRecord(healthValue)
  const mode = text(
    modeRecord.mode
      ?? modeRecord.current
      ?? modeRecord.value
      ?? value,
    context.productMode === "sealed" ? "sealed" : "default",
  )
  const productMode =
    context.productMode === "sealed" || mode === "sealed"
      ? "sealed"
      : "interactive"
  const revision = revisionOr(
    modeRecord.revision
      ?? modeRecord.mode_revision
      ?? health.mode_revision,
    0,
  )
  const policyRevision = revisionOr(
    modeRecord.policy_revision
      ?? health.policy_revision
      ?? context.defaultPolicyRevision,
    0,
  )
  const policyHash = optionalDigest(
    modeRecord.policy_hash
      ?? modeRecord.policy_digest
      ?? health.policy_digest
      ?? context.defaultPolicyHash,
  ) ?? "0".repeat(64)
  const frozen =
    productMode === "sealed"
    || modeRecord.frozen === true
    || modeRecord.policy_frozen === true
  const interventionCount = revisionOr(
    modeRecord.human_intervention_count
      ?? health.human_intervention_count
      ?? context.humanInterventionCount,
    0,
  )
  return Object.freeze({
    mode,
    productMode,
    revision,
    policyRevision,
    policyHash,
    frozen,
    noHuman: productMode === "sealed",
    askWillDeny: productMode === "sealed",
    humanInterventionCount: interventionCount,
    canonicalOwner: context.sourceOwner,
  })
}

export function projectPermissionRule(
  value: unknown,
): PermissionRuleProjection | undefined {
  const item = optionalPermissionRecord(value)
  if (!Object.keys(item).length) return undefined
  const ruleId = optionalId(
    item.rule_id ?? item.ruleId,
    "permission rule id",
  )
  if (!ruleId) return undefined
  const scope = optionalPermissionRecord(item.scope)
  return Object.freeze({
    ruleId,
    effect: permissionEffect(item.effect),
    source: bounded(item.source, "policy", 128),
    priority: signedInteger(item.priority),
    enabled: item.enabled !== false,
    reason: safeNarrative(item.reason, "", 4_096),
    revision: revisionOr(item.revision, 0),
    expiresAt: optionalTimestamp(item.expires_at ?? item.expiresAt),
    scopeSummary: scopeSummary(scope),
  })
}

export function projectPermissionDecision(
  value: unknown,
): PermissionDecisionProjection | undefined {
  const item = optionalPermissionRecord(value)
  if (!Object.keys(item).length) return undefined
  const decisionId = optionalId(
    item.decision_id ?? item.decisionId,
    "permission decision id",
  )
  if (!decisionId) return undefined
  const metadata = optionalPermissionRecord(item.metadata)
  const binding = optionalPermissionRecord(
    item.request_binding ?? item.requestBinding,
  )
  return Object.freeze({
    decisionId,
    requestId: optionalId(
      item.request_id
        ?? item.continuation_request_id
        ?? metadata.continuation_request_id,
      "permission request id",
    ),
    toolCallId: optionalId(
      item.tool_call_id ?? binding.tool_call_id,
      "permission tool call id",
    ),
    effect: permissionEffect(item.effect),
    reasonCode: bounded(
      item.reason_code ?? item.reasonCode,
      "permission_decision",
      256,
    ),
    reason: safeNarrative(item.reason, "", 8_192),
    policyRevision: revisionOr(
      item.policy_revision ?? item.policyRevision,
      0,
    ),
    modeRevision: revisionOr(
      item.mode_revision ?? item.modeRevision,
      0,
    ),
    evaluatedAt: isoTimestamp(
      item.evaluated_at ?? item.evaluatedAt,
      "permission decision timestamp",
      new Date(0),
    ),
    exactBinding:
      metadata.exact_approval_binding === true
      || item.exact_binding === true,
    responseId: optionalId(
      metadata.approval_response_id ?? item.response_id,
      "permission response id",
    ),
    permitId: optionalId(
      metadata.permit_id ?? item.permit_id,
      "permission permit id",
    ),
    humanInterventionCount: revisionOr(
      item.human_intervention_count
        ?? item.humanInterventionCount,
      0,
    ),
  })
}

export function projectPermissionReceipt(
  bodyValue: unknown,
  expected: {
    requestId: string
    responseId: string
    effect: PermissionResponseEffect
    receivedAt: Date
  },
): PermissionDecisionReceipt {
  const body = permissionRecord(bodyValue, "permission resolve response")
  const receipt = optionalPermissionRecord(body.receipt)
  const nested = optionalPermissionRecord(
    receipt.typescript ?? receipt.result ?? receipt,
  )
  const decision = optionalPermissionRecord(nested.decision)
  const permit = optionalPermissionRecord(nested.permit)
  const accepted =
    nested.accepted === true
    || (
      body.ok === true
      && (
        nested.effect === "allow"
        || nested.effect === "deny"
        || Object.keys(nested).length > 0
      )
    )
  const responseId = text(
    nested.response_id ?? nested.responseId,
    expected.responseId,
  )
  const requestId = text(
    nested.request_id ?? nested.requestId,
    expected.requestId,
  )
  return Object.freeze({
    requestId,
    responseId,
    effect: responseEffect(nested.effect) ?? expected.effect,
    accepted,
    replayed:
      nested.replayed === true
      || body.receipt_replayed === true,
    responseProofVerified:
      nested.response_proof_verified === true,
    responseProofDigest: optionalDigest(
      nested.response_proof_digest,
    ),
    responseChallengeDigest: optionalDigest(
      nested.response_challenge_digest,
    ),
    permitId: optionalId(
      nested.permit_id ?? permit.permitId ?? permit.permit_id,
      "permission permit id",
    ),
    decisionId: optionalId(
      decision.decision_id ?? decision.decisionId,
      "permission decision id",
    ),
    finalArgumentsDigest: optionalDigest(
      nested.final_arguments_digest
        ?? decision.final_arguments_digest,
    ),
    stateDigest: optionalDigest(nested.state_digest),
    canonicalOwner: text(
      nested.canonical_owner ?? body.state_owner,
      PERMISSION_CANONICAL_OWNER,
    ),
    resumed: accepted && (
      expected.effect === "deny"
      || Boolean(nested.permit_id ?? permit.permitId ?? permit.permit_id)
    ),
    receivedAt: expected.receivedAt.toISOString(),
    raw: Object.freeze(safePermissionClone(body)),
  })
}

function permissionResponseChallenge(
  item: Record<string, unknown>,
): PermissionResponseChallenge {
  const challenge = optionalPermissionRecord(
    item.response_challenge ?? item.responseChallenge,
  )
  const version = text(challenge.version, "")
  const owner = text(
    challenge.canonical_owner ?? challenge.canonicalOwner,
    "",
  )
  const nonce = text(challenge.nonce, "")
  const digest = optionalDigest(
    challenge.challenge_digest ?? challenge.challengeDigest,
  ) ?? "0".repeat(64)
  const compatible =
    version === PERMISSION_RESPONSE_VERSION
    && owner === PERMISSION_CANONICAL_OWNER
  return Object.freeze({
    version: PERMISSION_RESPONSE_VERSION,
    nonce: compatible ? nonce : "",
    canonicalOwner: PERMISSION_CANONICAL_OWNER,
    challengeDigest: digest,
  })
}

function permissionRequestStatus(value: unknown): PermissionRequestStatus {
  const selected = text(value, "unknown").toLowerCase()
  const aliases: Record<string, PermissionRequestStatus> = {
    pending: "delivered",
    created: "pending_delivery",
    approved: "responded",
    denied: "responded",
    resolved: "responded",
    aborted: "cancelled",
  }
  const normalized = aliases[selected] ?? selected
  return [
    "pending_delivery",
    "delivered",
    "delivery_failed",
    "responded",
    "expired",
    "cancelled",
  ].includes(normalized)
    ? (normalized as PermissionRequestStatus)
    : "unknown"
}

function safeNarrative(
  value: unknown,
  fallback: string,
  maximumBytes: number,
): string {
  const rendered = bounded(value, fallback, maximumBytes)
  return redactPermissionText(rendered, {
    kind: "text",
    maximumBytes,
  }).value
}

function safeBindingText(
  value: unknown,
  key: string,
  kind: "text" | "path" | "url" | "command",
): string {
  if (value === undefined || value === null || value === "") return ""
  return redactPermissionText(value, {
    key,
    kind,
    maximumBytes: 4_096,
  }).value
}

function safeRequestAudit(
  item: Record<string, unknown>,
  binding: PermissionRequestBinding,
  challenge: PermissionResponseChallenge,
): Readonly<Record<string, unknown>> {
  return Object.freeze(
    safePermissionClone({
      request_id: item.request_id ?? item.requestId,
      envelope_id: item.envelope_id ?? item.envelopeId,
      decision_id: item.decision_id ?? item.decisionId,
      run_id: item.run_id ?? item.runId,
      task_id: item.task_id ?? item.taskId,
      session_id: item.session_id ?? item.sessionId,
      session_revision: item.session_revision ?? item.sessionRevision,
      worker_request_id:
        item.worker_request_id ?? item.workerRequestId ?? item.tool_use_id,
      tool_call_id:
        item.tool_call_id ?? item.toolCallId ?? item.tool_use_id,
      request_fingerprint:
        item.request_fingerprint ?? item.requestFingerprint,
      arguments_digest: item.arguments_digest ?? item.argumentsDigest,
      policy_revision: item.policy_revision ?? item.policyRevision,
      mode_revision: item.mode_revision ?? item.modeRevision,
      expires_at: item.expires_at ?? item.expiresAt,
      status: item.status ?? item.phase,
      delivery_attempt:
        item.delivery_attempt ?? item.deliveryAttempt,
      delivery_receipt_id:
        item.delivery_receipt_id ?? item.deliveryReceiptId,
      response_id: item.response_id ?? item.responseId,
      response_effect:
        item.response_effect ?? item.responseEffect,
      response_accepted:
        item.response_accepted ?? item.responseAccepted,
      tool_identity: {
        name: binding.toolName,
        namespace: binding.namespace,
        server_id: binding.serverId,
        operation: binding.operation,
        command_name: binding.commandName,
        resource_uri: binding.resourceUri,
        workspace_root: binding.workspaceRoot,
      },
      response_challenge: {
        version: challenge.version,
        canonical_owner: challenge.canonicalOwner,
        challenge_digest: challenge.challengeDigest,
        nonce_present: Boolean(challenge.nonce),
      },
      raw_arguments_exposed: false,
    }),
  )
}

function permissionEffect(value: unknown): PermissionEffect {
  const selected = text(value, "ask").toLowerCase()
  return selected === "allow" || selected === "deny" ? selected : "ask"
}

function responseEffect(
  value: unknown,
): PermissionResponseEffect | undefined {
  const selected = text(value, "").toLowerCase()
  return selected === "allow" || selected === "deny" ? selected : undefined
}

function compareRequests(
  left: PermissionRequestProjection,
  right: PermissionRequestProjection,
): number {
  if (left.terminal !== right.terminal) return left.terminal ? 1 : -1
  if (left.expired !== right.expired) return left.expired ? -1 : 1
  return (
    comparePermissionTime(left.expiresAt, right.expiresAt)
    || comparePermissionTime(left.createdAt, right.createdAt)
    || left.requestId.localeCompare(right.requestId)
  )
}

function scopeSummary(scope: Record<string, unknown>): string {
  const keys = [
    "tool_pattern",
    "namespace_pattern",
    "server_pattern",
    "operation_pattern",
    "workspace_pattern",
    "session_pattern",
  ]
  const values = keys
    .map((key) => {
      const kind = key === "workspace_pattern" ? "path" : "text"
      return safeBindingText(scope[key], key, kind)
    })
    .filter((value) => value && value !== "*")
  return values.join(" · ") || "all exact matches in the bound scope"
}

function signedInteger(value: unknown): number {
  const parsed =
    typeof value === "number"
      ? value
      : typeof value === "string"
        ? Number(value)
        : 0
  return Number.isSafeInteger(parsed) ? parsed : 0
}

function revisionOr(value: unknown, fallback: number): number {
  try {
    return nonNegativeRevision(value, "permission revision")
  } catch {
    return nonNegativeRevision(fallback, "permission revision fallback")
  }
}

function optionalRevision(value: unknown): number | undefined {
  if (value === undefined || value === null || value === "") return undefined
  try {
    return nonNegativeRevision(value, "permission optional revision")
  } catch {
    return undefined
  }
}

function optionalDigest(value: unknown): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  try {
    return sha256Digest(value, "permission optional digest")
  } catch {
    return undefined
  }
}

function optionalTimestamp(value: unknown): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  try {
    return isoTimestamp(value, "permission optional timestamp")
  } catch {
    return undefined
  }
}

function optionalId(value: unknown, label: string): string | undefined {
  if (value === undefined || value === null || value === "") return undefined
  try {
    return identifier(value, label)
  } catch {
    return undefined
  }
}

function bounded(
  value: unknown,
  fallback: string,
  maximumBytes: number,
): string {
  try {
    return boundedPermissionText(value ?? fallback, {
      label: "permission text",
      maximumBytes,
      allowEmpty: true,
    })
  } catch {
    return fallback
  }
}

function text(value: unknown, fallback: string): string {
  return typeof value === "string" && value.trim() ? value.trim() : fallback
}
