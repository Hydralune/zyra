import {
  cloneJson,
  contractError,
  digest,
  intersectToolScopes,
  normalizeIdentity,
  normalizePolicyDecision,
  normalizeSkillVersion,
  nowIso,
  stableId,
  toolScopeIsSubset,
  uniqueStrings,
  type JsonObject,
  type PolicyDecisionReference,
  type RuntimeIdentity,
  type SkillInvocationOutcomeMemory,
  type SkillVersionReference,
} from "./contracts.ts";

export const SKILL_AUTHORITY_PROTOCOL = "zyra.skill-authority-revalidation/v1" as const;

export type SkillAuthorityAvailability =
  | "available"
  | "disabled"
  | "removed"
  | "invalid"
  | "shadowed";

export type SkillAuthorityDisposition =
  | "current"
  | "version_advanced"
  | "policy_tightened"
  | "version_advanced_and_policy_tightened"
  | "revoked"
  | "rejected"
  | "unavailable";

export interface CurrentSkillAuthority {
  protocol: "zyra.skill-coordinator-authority/v1";
  skillId: string;
  skillName: string;
  availability: SkillAuthorityAvailability;
  registryRevision: number;
  registryRevisionId: string;
  descriptorDigest: string | null;
  bodyDigest: string | null;
  sourceRevision: string | null;
  trust: "internal" | "verified" | "untrusted" | "unknown";
  policy: PolicyDecisionReference | null;
  resolvedAt: string;
  resolutionError: string | null;
  metadata: JsonObject;
}

export interface SkillAuthorityRevalidationInput {
  identity: RuntimeIdentity;
  memory: SkillInvocationOutcomeMemory;
  current: CurrentSkillAuthority;
  parentAllowedTools: string[];
  parentDeniedTools: string[];
  requestedAt?: string;
  causationId?: string;
  metadata?: JsonObject;
}

export interface SkillAuthorityRevalidationReceipt {
  protocol: typeof SKILL_AUTHORITY_PROTOCOL;
  receiptId: string;
  identity: RuntimeIdentity;
  memoryId: string;
  invocationId: string;
  skillId: string;
  skillName: string;
  historicalVersion: SkillVersionReference;
  historicalPolicy: PolicyDecisionReference;
  currentAuthority: CurrentSkillAuthority;
  disposition: SkillAuthorityDisposition;
  executable: boolean;
  currentResolutionRequired: true;
  cachedBodyExecutable: false;
  descriptorChanged: boolean;
  bodyChanged: boolean;
  registryAdvanced: boolean;
  policyChanged: boolean;
  policyTightened: boolean;
  trustAccepted: boolean;
  effectiveTools: string[];
  removedTools: string[];
  deniedTools: string[];
  reasons: string[];
  requestedAt: string;
  completedAt: string;
  causationId: string;
  metadata: JsonObject;
  receiptDigest: string;
}

export interface SkillAuthorityExecutionFence {
  fenceId: string;
  receiptId: string;
  memoryId: string;
  skillId: string;
  registryRevision: number;
  registryRevisionId: string;
  descriptorDigest: string;
  policyDecisionId: string;
  policyRevision: string;
  effectiveTools: string[];
  parentAllowedTools: string[];
  parentDeniedTools: string[];
  issuedAt: string;
  expiresAt: string;
  consumedAt: string | null;
  fenceDigest: string;
}

export interface SkillAuthorityRevalidationSnapshot {
  version: "zyra.skill-authority-revalidation-runtime/v1";
  identity: RuntimeIdentity;
  revision: number;
  receipts: SkillAuthorityRevalidationReceipt[];
  fences: SkillAuthorityExecutionFence[];
  completedReceiptIds: string[];
  capturedAt: string;
  checksum: string;
}

export interface SkillAuthorityAudit {
  ok: boolean;
  receiptId: string;
  findings: string[];
  currentResolutionRequired: true;
  cachedBodyExecutable: false;
}

/**
 * Records the outcome of a *current* 03C resolution without becoming a
 * resolver itself. The caller must obtain CurrentSkillAuthority from the real
 * SkillCoordinator. Historical outcome memory is compared only as evidence;
 * it never supplies a descriptor body, policy, version, or invocation route.
 */
export class SkillAuthorityRevalidationRuntime {
  readonly identity: RuntimeIdentity;
  private readonly receipts = new Map<string, SkillAuthorityRevalidationReceipt>();
  private readonly fences = new Map<string, SkillAuthorityExecutionFence>();
  private readonly completedReceiptIds = new Set<string>();
  private readonly now: () => Date;
  private readonly fenceTtlMilliseconds: number;
  private revision = 0;

  constructor(options: {
    identity: RuntimeIdentity;
    now?: () => Date;
    fenceTtlMilliseconds?: number;
    snapshot?: SkillAuthorityRevalidationSnapshot | null;
  }) {
    this.identity = normalizeIdentity(options.identity);
    this.now = options.now ?? (() => new Date());
    this.fenceTtlMilliseconds = Math.max(
      1_000,
      Math.min(options.fenceTtlMilliseconds ?? 60_000, 60 * 60 * 1_000),
    );
    if (options.snapshot) this.restore(options.snapshot);
  }

  revalidate(inputValue: SkillAuthorityRevalidationInput): SkillAuthorityRevalidationReceipt {
    this.assertEnabled();
    const input = normalizeInput(inputValue, this.identity, this.now);
    const historicalVersion = normalizeSkillVersion(input.memory.version);
    const historicalPolicy = normalizePolicyDecision(input.memory.policy);
    const current = normalizeCurrentAuthority(input.current);
    const reasons: string[] = [];

    if (input.memory.identity.taskId !== this.identity.taskId) {
      reasons.push("memory_task_mismatch");
    }
    if (input.memory.identity.sessionId !== this.identity.sessionId) {
      reasons.push("memory_session_mismatch");
    }
    if (input.memory.state !== "accepted") reasons.push("memory_not_accepted");
    if (input.memory.status !== "completed") reasons.push("outcome_not_completed");
    if (!input.memory.evidence.some((item) => item.trustedRuntime)) {
      reasons.push("trusted_runtime_evidence_missing");
    }
    if (current.skillId !== historicalVersion.skillId) reasons.push("skill_identity_changed");
    if (current.skillName !== historicalVersion.skillName) reasons.push("skill_name_changed");

    const registryAdvanced = current.registryRevision > historicalVersion.registryRevision;
    const descriptorChanged = current.descriptorDigest !== historicalVersion.descriptorDigest;
    const bodyChanged = current.bodyDigest !== historicalVersion.bodyDigest;
    const currentPolicy = current.policy;
    const policyChanged = currentPolicy === null
      || currentPolicy.policyRevision !== historicalPolicy.policyRevision
      || currentPolicy.policyDigest !== historicalPolicy.policyDigest
      || digest(currentPolicy.effectiveTools) !== digest(historicalPolicy.effectiveTools)
      || digest(currentPolicy.deniedTools) !== digest(historicalPolicy.deniedTools);

    if (current.registryRevision < historicalVersion.registryRevision) {
      reasons.push("registry_revision_regressed");
    }
    if (current.availability !== "available") {
      reasons.push(`skill_${current.availability}`);
    }
    if (current.resolutionError) reasons.push("current_resolution_failed");
    if (!currentPolicy) reasons.push("current_policy_missing");
    if (currentPolicy?.effect !== "allow") {
      reasons.push(currentPolicy?.effect === "ask" ? "current_policy_requires_approval" : "current_policy_denied");
    }

    const parentAllowed = uniqueStrings(input.parentAllowedTools);
    const parentDenied = uniqueStrings(input.parentDeniedTools);
    const currentAllowed = currentPolicy?.effectiveTools ?? [];
    const currentDenied = uniqueStrings([
      ...parentDenied,
      ...(currentPolicy?.deniedTools ?? []),
    ]);
    const effectiveTools = intersectToolScopes(parentAllowed, currentAllowed, currentDenied);
    const historicalEffectiveWithinParent = intersectToolScopes(
      parentAllowed,
      historicalPolicy.effectiveTools,
      uniqueStrings([...parentDenied, ...historicalPolicy.deniedTools]),
    );
    const removedTools = historicalEffectiveWithinParent.filter(
      (tool) => !effectiveTools.includes(tool) && tool !== "*",
    );
    const policyTightened = removedTools.length > 0
      || currentDenied.some((tool) => !historicalPolicy.deniedTools.includes(tool))
      || (historicalEffectiveWithinParent.includes("*") && !effectiveTools.includes("*"));

    if (!toolScopeIsSubset(parentAllowed, effectiveTools, parentDenied)) {
      reasons.push("current_scope_widens_parent");
    }
    if (effectiveTools.some((tool) => currentDenied.includes(tool))) {
      reasons.push("current_scope_contains_denied_tool");
    }
    const trustAccepted = current.trust === "internal" || current.trust === "verified";
    if (!trustAccepted) reasons.push("current_skill_trust_unacceptable");

    const hardRejection = reasons.some((reason) => [
      "memory_task_mismatch",
      "memory_session_mismatch",
      "memory_not_accepted",
      "outcome_not_completed",
      "trusted_runtime_evidence_missing",
      "skill_identity_changed",
      "skill_name_changed",
      "registry_revision_regressed",
      "current_resolution_failed",
      "current_policy_missing",
      "current_policy_denied",
      "current_policy_requires_approval",
      "current_scope_widens_parent",
      "current_scope_contains_denied_tool",
      "current_skill_trust_unacceptable",
    ].includes(reason)) || current.availability !== "available";

    const executable = !hardRejection
      && currentPolicy?.effect === "allow"
      && Boolean(current.descriptorDigest)
      && Boolean(current.bodyDigest)
      && current.skillId === historicalVersion.skillId;
    const disposition = authorityDisposition({
      executable,
      availability: current.availability,
      registryAdvanced,
      descriptorChanged,
      bodyChanged,
      policyTightened,
      currentPolicy,
    });
    if (registryAdvanced || descriptorChanged || bodyChanged) reasons.push("current_version_must_replace_historical_version");
    if (policyChanged) reasons.push("current_policy_must_replace_historical_policy");
    if (policyTightened) reasons.push("historical_tool_scope_removed");
    if (executable) reasons.push("03c_current_resolution_accepted");
    reasons.push("03c_execution_still_required");

    const completedAt = nowIso(this.now);
    const unsigned = {
      protocol: SKILL_AUTHORITY_PROTOCOL,
      receiptId: stableId(
        "skill-authority-revalidation",
        input.memory.memoryId,
        current.registryRevision,
        current.registryRevisionId,
        current.descriptorDigest,
        currentPolicy?.decisionId,
        parentAllowed,
        parentDenied,
      ),
      identity: cloneJson(input.identity),
      memoryId: input.memory.memoryId,
      invocationId: input.memory.invocationId,
      skillId: historicalVersion.skillId,
      skillName: historicalVersion.skillName,
      historicalVersion,
      historicalPolicy,
      currentAuthority: current,
      disposition,
      executable,
      currentResolutionRequired: true,
      cachedBodyExecutable: false,
      descriptorChanged,
      bodyChanged,
      registryAdvanced,
      policyChanged,
      policyTightened,
      trustAccepted,
      effectiveTools,
      removedTools: uniqueStrings(removedTools),
      deniedTools: currentDenied,
      reasons: uniqueStrings(reasons),
      requestedAt: input.requestedAt,
      completedAt,
      causationId: input.causationId,
      metadata: {
        ...input.metadata,
        canonical_skill_owner: "03C SkillCoordinator",
        comparison_owner: "SkillAuthorityRevalidationRuntime",
        historical_outcome_is_evidence_only: true,
        executable_skill_body_present: false,
      },
    } satisfies Omit<SkillAuthorityRevalidationReceipt, "receiptDigest">;
    const receipt: SkillAuthorityRevalidationReceipt = {
      ...unsigned,
      receiptDigest: digest(unsigned),
    };
    const existing = this.receipts.get(receipt.receiptId);
    if (existing && existing.receiptDigest !== receipt.receiptDigest) {
      throw contractError(
        "skill_authority_receipt_conflict",
        `authority receipt conflicts: ${receipt.receiptId}`,
      );
    }
    this.receipts.set(receipt.receiptId, cloneJson(receipt));
    this.revision += existing ? 0 : 1;
    return cloneJson(existing ?? receipt);
  }

  issueFence(receiptIdValue: string): SkillAuthorityExecutionFence {
    this.assertEnabled();
    const receiptId = receiptIdValue.trim();
    const receipt = this.receipts.get(receiptId);
    if (!receipt) throw contractError("skill_authority_receipt_missing", `authority receipt not found: ${receiptId}`);
    if (!receipt.executable) {
      throw contractError(
        "skill_authority_execution_rejected",
        `current 03C authority rejected skill execution: ${receipt.reasons.join(", ")}`,
      );
    }
    if (!receipt.currentAuthority.policy || !receipt.currentAuthority.descriptorDigest) {
      throw contractError("skill_authority_fence_incomplete", "current 03C authority cannot issue a complete execution fence");
    }
    const issuedAt = nowIso(this.now);
    const expiresAt = new Date(Date.parse(issuedAt) + this.fenceTtlMilliseconds).toISOString();
    const unsigned = {
      fenceId: stableId(
        "skill-authority-fence",
        receipt.receiptId,
        receipt.currentAuthority.registryRevisionId,
        issuedAt,
      ),
      receiptId: receipt.receiptId,
      memoryId: receipt.memoryId,
      skillId: receipt.skillId,
      registryRevision: receipt.currentAuthority.registryRevision,
      registryRevisionId: receipt.currentAuthority.registryRevisionId,
      descriptorDigest: receipt.currentAuthority.descriptorDigest,
      policyDecisionId: receipt.currentAuthority.policy.decisionId,
      policyRevision: receipt.currentAuthority.policy.policyRevision,
      effectiveTools: cloneJson(receipt.effectiveTools),
      // The fence is deliberately derived from the already revalidated effective
      // scope. Historical metadata is evidence only and can never widen it.
      parentAllowedTools: cloneJson(receipt.effectiveTools),
      parentDeniedTools: cloneJson(receipt.deniedTools),
      issuedAt,
      expiresAt,
      consumedAt: null,
    } satisfies Omit<SkillAuthorityExecutionFence, "fenceDigest">;
    const fence: SkillAuthorityExecutionFence = { ...unsigned, fenceDigest: digest(unsigned) };
    this.fences.set(fence.fenceId, fence);
    this.revision += 1;
    return cloneJson(fence);
  }

  consumeFence(input: {
    fenceId: string;
    current: CurrentSkillAuthority;
    requestedTools: string[];
  }): SkillAuthorityExecutionFence {
    this.assertEnabled();
    const fence = this.fences.get(input.fenceId.trim());
    if (!fence) throw contractError("skill_authority_fence_missing", `authority fence not found: ${input.fenceId}`);
    if (fence.consumedAt) throw contractError("skill_authority_fence_consumed", `authority fence already consumed: ${fence.fenceId}`);
    if (Date.parse(fence.expiresAt) <= this.now().getTime()) {
      throw contractError("skill_authority_fence_expired", `authority fence expired: ${fence.fenceId}`);
    }
    const current = normalizeCurrentAuthority(input.current);
    const policy = current.policy;
    if (
      current.availability !== "available"
      || current.skillId !== fence.skillId
      || current.registryRevision !== fence.registryRevision
      || current.registryRevisionId !== fence.registryRevisionId
      || current.descriptorDigest !== fence.descriptorDigest
      || !policy
      || policy.effect !== "allow"
      || policy.decisionId !== fence.policyDecisionId
      || policy.policyRevision !== fence.policyRevision
    ) {
      throw contractError(
        "skill_authority_fence_stale",
        "03C authority changed after the execution fence was issued",
        { fence_id: fence.fenceId, current_registry_revision: current.registryRevision },
      );
    }
    const requested = uniqueStrings(input.requestedTools);
    if (!toolScopeIsSubset(fence.effectiveTools, requested, fence.parentDeniedTools)) {
      throw contractError("skill_authority_fence_tool_scope", "requested skill tools exceed the current execution fence");
    }
    const completed: SkillAuthorityExecutionFence = {
      ...fence,
      consumedAt: nowIso(this.now),
      fenceDigest: "",
    };
    const { fenceDigest: _ignored, ...unsigned } = completed;
    completed.fenceDigest = digest(unsigned);
    this.fences.set(completed.fenceId, completed);
    this.completedReceiptIds.add(completed.receiptId);
    this.revision += 1;
    return cloneJson(completed);
  }

  audit(receiptIdValue: string): SkillAuthorityAudit {
    const receiptId = receiptIdValue.trim();
    const receipt = this.receipts.get(receiptId);
    if (!receipt) {
      return {
        ok: false,
        receiptId,
        findings: ["receipt_missing"],
        currentResolutionRequired: true,
        cachedBodyExecutable: false,
      };
    }
    const findings: string[] = [];
    const { receiptDigest, ...unsigned } = receipt;
    if (digest(unsigned) !== receiptDigest) findings.push("receipt_digest_mismatch");
    if (!receipt.currentResolutionRequired) findings.push("current_resolution_not_required");
    if (receipt.cachedBodyExecutable) findings.push("cached_body_marked_executable");
    if (receipt.executable && receipt.currentAuthority.availability !== "available") findings.push("unavailable_skill_marked_executable");
    if (receipt.executable && receipt.currentAuthority.policy?.effect !== "allow") findings.push("non_allow_policy_marked_executable");
    if (!toolScopeIsSubset(
      receipt.currentAuthority.policy?.effectiveTools ?? [],
      receipt.effectiveTools,
      receipt.deniedTools,
    )) findings.push("effective_scope_not_current_policy_subset");
    return {
      ok: findings.length === 0,
      receiptId,
      findings,
      currentResolutionRequired: true,
      cachedBodyExecutable: false,
    };
  }

  get(receiptId: string): SkillAuthorityRevalidationReceipt | null {
    const receipt = this.receipts.get(receiptId.trim());
    return receipt ? cloneJson(receipt) : null;
  }

  list(options: {
    skillId?: string;
    disposition?: SkillAuthorityDisposition;
    executable?: boolean;
    limit?: number;
  } = {}): SkillAuthorityRevalidationReceipt[] {
    return [...this.receipts.values()]
      .filter((item) => !options.skillId || item.skillId === options.skillId)
      .filter((item) => !options.disposition || item.disposition === options.disposition)
      .filter((item) => options.executable === undefined || item.executable === options.executable)
      .sort((left, right) => right.completedAt.localeCompare(left.completedAt) || left.receiptId.localeCompare(right.receiptId))
      .slice(0, Math.max(0, Math.min(options.limit ?? 1_000, 100_000)))
      .map(cloneJson);
  }

  health(): JsonObject {
    const receipts = [...this.receipts.values()];
    return {
      protocol: SKILL_AUTHORITY_PROTOCOL,
      canonical_skill_owner: "03C SkillCoordinator",
      revalidation_receipt_owner: "SkillAuthorityRevalidationRuntime",
      revision: this.revision,
      receipt_count: receipts.length,
      executable_count: receipts.filter((item) => item.executable).length,
      revoked_count: receipts.filter((item) => item.disposition === "revoked").length,
      tightened_count: receipts.filter((item) => item.policyTightened).length,
      execution_fence_count: this.fences.size,
      consumed_fence_count: [...this.fences.values()].filter((item) => item.consumedAt).length,
      owns_skill_loader: false,
      owns_skill_policy: false,
      owns_skill_version: false,
      owns_skill_revocation: false,
      cached_body_executable: false,
    };
  }

  snapshot(): SkillAuthorityRevalidationSnapshot {
    const unsigned: Omit<SkillAuthorityRevalidationSnapshot, "checksum"> = {
      version: "zyra.skill-authority-revalidation-runtime/v1",
      identity: cloneJson(this.identity),
      revision: this.revision,
      receipts: this.list({ limit: 100_000 }).sort((left, right) => left.receiptId.localeCompare(right.receiptId)),
      fences: [...this.fences.values()].sort((left, right) => left.fenceId.localeCompare(right.fenceId)).map(cloneJson),
      completedReceiptIds: [...this.completedReceiptIds].sort(),
      capturedAt: nowIso(this.now),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshotValue: SkillAuthorityRevalidationSnapshot): void {
    if (snapshotValue.version !== "zyra.skill-authority-revalidation-runtime/v1") {
      throw contractError("skill_authority_snapshot_version", "unsupported skill authority snapshot version");
    }
    const { checksum, ...unsigned } = snapshotValue;
    if (digest(unsigned) !== checksum) {
      throw contractError("skill_authority_snapshot_checksum", "skill authority snapshot checksum mismatch");
    }
    const identity = normalizeIdentity(snapshotValue.identity);
    if (identity.taskId !== this.identity.taskId || identity.sessionId !== this.identity.sessionId) {
      throw contractError("skill_authority_snapshot_binding", "skill authority snapshot belongs to another task/session");
    }
    const receipts = new Map<string, SkillAuthorityRevalidationReceipt>();
    for (const receipt of snapshotValue.receipts) {
      const { receiptDigest, ...receiptUnsigned } = receipt;
      if (digest(receiptUnsigned) !== receiptDigest) {
        throw contractError("skill_authority_receipt_digest", `authority receipt digest mismatch: ${receipt.receiptId}`);
      }
      if (receipts.has(receipt.receiptId)) {
        throw contractError("skill_authority_receipt_duplicate", `duplicate authority receipt: ${receipt.receiptId}`);
      }
      receipts.set(receipt.receiptId, cloneJson(receipt));
    }
    const fences = new Map<string, SkillAuthorityExecutionFence>();
    for (const fence of snapshotValue.fences) {
      const { fenceDigest, ...fenceUnsigned } = fence;
      if (digest(fenceUnsigned) !== fenceDigest) {
        throw contractError("skill_authority_fence_digest", `authority fence digest mismatch: ${fence.fenceId}`);
      }
      if (!receipts.has(fence.receiptId)) {
        throw contractError("skill_authority_fence_receipt", `authority fence references missing receipt: ${fence.receiptId}`);
      }
      fences.set(fence.fenceId, cloneJson(fence));
    }
    this.receipts.clear();
    this.fences.clear();
    for (const [key, value] of receipts) this.receipts.set(key, value);
    for (const [key, value] of fences) this.fences.set(key, value);
    this.completedReceiptIds.clear();
    for (const value of uniqueStrings(snapshotValue.completedReceiptIds)) {
      if (!receipts.has(value)) {
        throw contractError("skill_authority_completed_receipt", `completed authority receipt missing: ${value}`);
      }
      this.completedReceiptIds.add(value);
    }
    this.revision = snapshotValue.revision;
  }

  private assertEnabled(): void {
    if (process.env.ZYRA_DISABLE_SKILL_AUTHORITY_REVALIDATION === "1") {
      throw contractError("skill_authority_revalidation_disabled", "06C skill authority revalidation is disabled");
    }
  }
}

function normalizeInput(
  value: SkillAuthorityRevalidationInput,
  expected: RuntimeIdentity,
  now: () => Date,
): Required<SkillAuthorityRevalidationInput> {
  const identity = normalizeIdentity(value.identity);
  if (identity.taskId !== expected.taskId || identity.sessionId !== expected.sessionId) {
    throw contractError("skill_authority_input_binding", "authority revalidation belongs to another task/session");
  }
  if (!value.memory.memoryId?.trim()) throw contractError("skill_authority_memory_id", "authority revalidation requires a memory id");
  return {
    identity,
    memory: cloneJson(value.memory),
    current: normalizeCurrentAuthority(value.current),
    parentAllowedTools: uniqueStrings(value.parentAllowedTools),
    parentDeniedTools: uniqueStrings(value.parentDeniedTools),
    requestedAt: value.requestedAt?.trim() || nowIso(now),
    causationId: value.causationId?.trim() || value.memory.memoryId,
    metadata: cloneJson(value.metadata ?? {}),
  };
}

export function normalizeCurrentAuthority(value: CurrentSkillAuthority): CurrentSkillAuthority {
  if (value.protocol !== "zyra.skill-coordinator-authority/v1") {
    throw contractError("skill_authority_protocol", "authority must come from the 03C SkillCoordinator protocol");
  }
  const availability = value.availability;
  if (!["available", "disabled", "removed", "invalid", "shadowed"].includes(availability)) {
    throw contractError("skill_authority_availability", `unsupported skill availability ${availability}`);
  }
  const registryRevision = Number(value.registryRevision);
  if (!Number.isSafeInteger(registryRevision) || registryRevision < 0) {
    throw contractError("skill_authority_registry_revision", "current registry revision must be non-negative");
  }
  const nullableDigest = (input: string | null, label: string): string | null => {
    if (input === null || input === "") return null;
    const normalized = input.replace(/^sha256:/, "").toLowerCase();
    if (!/^[0-9a-f]{64}$/.test(normalized)) {
      throw contractError("skill_authority_digest", `${label} must be sha256`);
    }
    return normalized;
  };
  const trust = value.trust;
  if (!["internal", "verified", "untrusted", "unknown"].includes(trust)) {
    throw contractError("skill_authority_trust", `unsupported skill trust ${trust}`);
  }
  return {
    protocol: "zyra.skill-coordinator-authority/v1",
    skillId: value.skillId?.trim() || "unresolved",
    skillName: value.skillName?.trim() || value.skillId?.trim() || "unresolved",
    availability,
    registryRevision,
    registryRevisionId: value.registryRevisionId?.trim() || "unresolved",
    descriptorDigest: nullableDigest(value.descriptorDigest, "descriptor digest"),
    bodyDigest: nullableDigest(value.bodyDigest, "body digest"),
    sourceRevision: value.sourceRevision?.trim() || null,
    trust,
    policy: value.policy ? normalizePolicyDecision(value.policy) : null,
    resolvedAt: new Date(value.resolvedAt).toISOString(),
    resolutionError: value.resolutionError?.trim() || null,
    metadata: cloneJson(value.metadata ?? {}),
  };
}

function authorityDisposition(input: {
  executable: boolean;
  availability: SkillAuthorityAvailability;
  registryAdvanced: boolean;
  descriptorChanged: boolean;
  bodyChanged: boolean;
  policyTightened: boolean;
  currentPolicy: PolicyDecisionReference | null;
}): SkillAuthorityDisposition {
  if (input.availability === "removed" || input.availability === "disabled") return "revoked";
  if (input.availability !== "available") return "unavailable";
  if (!input.executable || input.currentPolicy?.effect !== "allow") return "rejected";
  const versionChanged = input.registryAdvanced || input.descriptorChanged || input.bodyChanged;
  if (versionChanged && input.policyTightened) return "version_advanced_and_policy_tightened";
  if (input.policyTightened) return "policy_tightened";
  if (versionChanged) return "version_advanced";
  return "current";
}
