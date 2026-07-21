import {
  cloneJson,
  contractError,
  digest,
  nowIso,
  stableId,
  uniqueStrings,
  type CompactRestoreProjection,
  type JsonObject,
  type MemorySignal,
  type ReusableProcedure,
  type RuntimeIdentity,
  type SkillInvocationOutcomeMemory,
} from "./contracts.ts";

export const RESUME_CONTINUITY_PROTOCOL = "zyra.skill-memory-resume-continuity/v1" as const;

export type ResumeContinuityState =
  | "captured"
  | "restored"
  | "verified"
  | "rejected"
  | "superseded";

export interface ResumeContinuityManifest {
  manifestId: string;
  protocol: typeof RESUME_CONTINUITY_PROTOCOL;
  identity: RuntimeIdentity;
  restartEpoch: number;
  contextEpoch: number;
  applicationRevision: number;
  outcomeIds: string[];
  acceptedOutcomeIds: string[];
  failedOutcomeIds: string[];
  procedureIds: string[];
  validatedProcedureIds: string[];
  projectionIds: string[];
  appliedProjectionIds: string[];
  boundaryIds: string[];
  archiveIds: string[];
  signalIds: string[];
  authorityReceiptIds: string[];
  retrievalReceiptIds: string[];
  fidelityComparisonIds: string[];
  directiveIds: string[];
  policyWatermarks: Record<string, string>;
  versionWatermarks: Record<string, string>;
  sourceDigest: string;
  capturedAt: string;
  metadata: JsonObject;
  manifestDigest: string;
}

export interface ResumeContinuityObservation {
  identity: RuntimeIdentity;
  restartEpoch: number;
  contextEpoch: number;
  applicationRevision: number;
  outcomes: SkillInvocationOutcomeMemory[];
  procedures: ReusableProcedure[];
  projections: CompactRestoreProjection[];
  signals: MemorySignal[];
  archiveIds: string[];
  authorityReceiptIds?: string[];
  retrievalReceiptIds?: string[];
  fidelityComparisonIds?: string[];
  directiveIds?: string[];
  metadata?: JsonObject;
}

export interface ResumeContinuityReceipt {
  receiptId: string;
  protocol: typeof RESUME_CONTINUITY_PROTOCOL;
  manifestId: string;
  identity: RuntimeIdentity;
  state: ResumeContinuityState;
  restartEpochBefore: number;
  restartEpochAfter: number;
  contextEpochBefore: number;
  contextEpochAfter: number;
  applicationRevisionBefore: number;
  applicationRevisionAfter: number;
  expectedOutcomeIds: string[];
  actualOutcomeIds: string[];
  missingOutcomeIds: string[];
  unexpectedOutcomeIds: string[];
  expectedProcedureIds: string[];
  actualProcedureIds: string[];
  missingProcedureIds: string[];
  unexpectedProcedureIds: string[];
  expectedProjectionIds: string[];
  actualProjectionIds: string[];
  missingProjectionIds: string[];
  expectedSignalIds: string[];
  actualSignalIds: string[];
  missingSignalIds: string[];
  missingArchiveIds: string[];
  missingAuthorityReceiptIds: string[];
  missingRetrievalReceiptIds: string[];
  missingFidelityComparisonIds: string[];
  missingDirectiveIds: string[];
  policyWatermarkChanges: JsonObject[];
  versionWatermarkChanges: JsonObject[];
  checksumValid: boolean;
  identityValid: boolean;
  epochValid: boolean;
  lossless: boolean;
  executableAuthorityRestored: false;
  currentAuthorityRevalidationRequired: true;
  findings: string[];
  observedAt: string;
  metadata: JsonObject;
  receiptDigest: string;
}

export interface ResumeContinuityGuard {
  guardId: string;
  manifestId: string;
  expectedSnapshotChecksum: string;
  expectedManifestDigest: string;
  taskId: string;
  sessionId: string;
  restartEpoch: number;
  issuedAt: string;
  consumedAt: string | null;
  guardDigest: string;
}

export interface ResumeContinuitySnapshot {
  version: "zyra.skill-memory-resume-continuity-runtime/v1";
  identity: RuntimeIdentity;
  revision: number;
  manifests: ResumeContinuityManifest[];
  receipts: ResumeContinuityReceipt[];
  guards: ResumeContinuityGuard[];
  activeManifestId: string | null;
  capturedAt: string;
  checksum: string;
}

/**
 * Persists a semantic manifest alongside the parent E01 snapshot. Checksums
 * protect byte-level state; this runtime additionally proves that outcome,
 * procedure, compact projection, signal, and integration identities survived
 * the restore. It deliberately does not restore skill execution authority.
 */
export class SkillMemoryResumeContinuityRuntime {
  readonly identity: RuntimeIdentity;
  private readonly manifests = new Map<string, ResumeContinuityManifest>();
  private readonly receipts = new Map<string, ResumeContinuityReceipt>();
  private readonly guards = new Map<string, ResumeContinuityGuard>();
  private readonly now: () => Date;
  private activeManifestId: string | null = null;
  private revision = 0;

  constructor(options: {
    identity: RuntimeIdentity;
    now?: () => Date;
    snapshot?: ResumeContinuitySnapshot | null;
  }) {
    this.identity = cloneJson(options.identity);
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  capture(observationValue: ResumeContinuityObservation): ResumeContinuityManifest {
    this.assertEnabled();
    const observation = normalizeObservation(observationValue, this.identity);
    const policyWatermarks: Record<string, string> = {};
    const versionWatermarks: Record<string, string> = {};
    for (const outcome of observation.outcomes) {
      const existingPolicy = policyWatermarks[outcome.version.skillId];
      const nextPolicy = `${outcome.policy.policyRevision}:${outcome.policy.policyDigest}:${digest(outcome.policy.effectiveTools)}:${digest(outcome.policy.deniedTools)}`;
      if (!existingPolicy || outcome.revision >= latestOutcomeRevision(observation.outcomes, outcome.version.skillId, existingPolicy)) {
        policyWatermarks[outcome.version.skillId] = nextPolicy;
      }
      const existingVersion = versionWatermarks[outcome.version.skillId];
      const nextVersion = `${outcome.version.registryRevision}:${outcome.version.descriptorDigest}:${outcome.version.bodyDigest}:${outcome.version.sourceRevision}`;
      if (!existingVersion || compareVersionWatermarks(existingVersion, nextVersion) <= 0) {
        versionWatermarks[outcome.version.skillId] = nextVersion;
      }
    }
    const capturedAt = nowIso(this.now);
    const sourceProjection = {
      identity: observation.identity,
      restart_epoch: observation.restartEpoch,
      context_epoch: observation.contextEpoch,
      application_revision: observation.applicationRevision,
      outcomes: observation.outcomes.map((item) => [item.memoryId, item.recordDigest, item.revision]),
      procedures: observation.procedures.map((item) => [item.procedureId, item.procedureDigest, item.revision]),
      projections: observation.projections.map((item) => [item.projectionId, item.projectionDigest, item.state]),
      signals: observation.signals.map((item) => [item.signalId, item.signalDigest, item.sequence]),
      archives: observation.archiveIds,
      authority_receipts: observation.authorityReceiptIds,
      retrieval_receipts: observation.retrievalReceiptIds,
      fidelity_comparisons: observation.fidelityComparisonIds,
      directives: observation.directiveIds,
    };
    const unsigned = {
      manifestId: stableId(
        "skill-memory-resume-manifest",
        observation.identity.taskId,
        observation.identity.sessionId,
        observation.restartEpoch,
        observation.contextEpoch,
        digest(sourceProjection),
      ),
      protocol: RESUME_CONTINUITY_PROTOCOL,
      identity: cloneJson(observation.identity),
      restartEpoch: observation.restartEpoch,
      contextEpoch: observation.contextEpoch,
      applicationRevision: observation.applicationRevision,
      outcomeIds: uniqueStrings(observation.outcomes.map((item) => item.memoryId)),
      acceptedOutcomeIds: uniqueStrings(observation.outcomes.filter((item) => item.state === "accepted").map((item) => item.memoryId)),
      failedOutcomeIds: uniqueStrings(observation.outcomes.filter((item) => item.status === "failed").map((item) => item.memoryId)),
      procedureIds: uniqueStrings(observation.procedures.map((item) => item.procedureId)),
      validatedProcedureIds: uniqueStrings(observation.procedures.filter((item) => item.state === "validated").map((item) => item.procedureId)),
      projectionIds: uniqueStrings(observation.projections.map((item) => item.projectionId)),
      appliedProjectionIds: uniqueStrings(observation.projections.filter((item) => item.state === "applied").map((item) => item.projectionId)),
      boundaryIds: uniqueStrings(observation.projections.map((item) => item.boundaryId)),
      archiveIds: uniqueStrings(observation.archiveIds),
      signalIds: uniqueStrings(observation.signals.map((item) => item.signalId)),
      authorityReceiptIds: uniqueStrings(observation.authorityReceiptIds),
      retrievalReceiptIds: uniqueStrings(observation.retrievalReceiptIds),
      fidelityComparisonIds: uniqueStrings(observation.fidelityComparisonIds),
      directiveIds: uniqueStrings(observation.directiveIds),
      policyWatermarks: sortRecord(policyWatermarks),
      versionWatermarks: sortRecord(versionWatermarks),
      sourceDigest: digest(sourceProjection),
      capturedAt,
      metadata: {
        ...observation.metadata,
        executable_skill_authority_captured: false,
        current_03c_resolution_required_after_resume: true,
        canonical_session_owner: "E01 DurableSessionRuntime",
        continuity_owner: "SkillMemoryResumeContinuityRuntime",
      },
    } satisfies Omit<ResumeContinuityManifest, "manifestDigest">;
    const manifest: ResumeContinuityManifest = { ...unsigned, manifestDigest: digest(unsigned) };
    const existing = this.manifests.get(manifest.manifestId);
    if (existing && existing.manifestDigest !== manifest.manifestDigest) {
      throw contractError("resume_continuity_manifest_conflict", "resume continuity manifest id conflict");
    }
    if (this.activeManifestId && this.activeManifestId !== manifest.manifestId) {
      const prior = this.manifests.get(this.activeManifestId);
      if (prior && prior.restartEpoch > manifest.restartEpoch) {
        throw contractError("resume_continuity_epoch_regression", "resume continuity capture restart epoch regressed");
      }
    }
    this.manifests.set(manifest.manifestId, manifest);
    this.activeManifestId = manifest.manifestId;
    this.revision += existing ? 0 : 1;
    return cloneJson(existing ?? manifest);
  }

  issueGuard(manifestIdValue: string, snapshotChecksumValue: string): ResumeContinuityGuard {
    this.assertEnabled();
    const manifestId = manifestIdValue.trim();
    const manifest = this.manifests.get(manifestId);
    if (!manifest) throw contractError("resume_continuity_manifest_missing", `resume continuity manifest not found: ${manifestId}`);
    const snapshotChecksum = snapshotChecksumValue.replace(/^sha256:/, "").toLowerCase();
    if (!/^[0-9a-f]{64}$/.test(snapshotChecksum)) {
      throw contractError("resume_continuity_snapshot_checksum", "resume continuity guard requires a sha256 snapshot checksum");
    }
    const issuedAt = nowIso(this.now);
    const unsigned = {
      guardId: stableId("skill-memory-resume-guard", manifest.manifestId, snapshotChecksum),
      manifestId: manifest.manifestId,
      expectedSnapshotChecksum: snapshotChecksum,
      expectedManifestDigest: manifest.manifestDigest,
      taskId: this.identity.taskId,
      sessionId: this.identity.sessionId,
      restartEpoch: manifest.restartEpoch,
      issuedAt,
      consumedAt: null,
    } satisfies Omit<ResumeContinuityGuard, "guardDigest">;
    const guard: ResumeContinuityGuard = { ...unsigned, guardDigest: digest(unsigned) };
    const existing = this.guards.get(guard.guardId);
    if (existing && existing.guardDigest !== guard.guardDigest) {
      throw contractError("resume_continuity_guard_conflict", "resume continuity guard id conflict");
    }
    this.guards.set(guard.guardId, guard);
    this.revision += existing ? 0 : 1;
    return cloneJson(existing ?? guard);
  }

  verify(input: {
    manifestId: string;
    observation: ResumeContinuityObservation;
    guardId?: string | null;
    restoredSnapshotChecksum?: string | null;
    metadata?: JsonObject;
  }): ResumeContinuityReceipt {
    this.assertEnabled();
    const manifest = this.manifests.get(input.manifestId.trim());
    if (!manifest) throw contractError("resume_continuity_manifest_missing", `resume continuity manifest not found: ${input.manifestId}`);
    const observation = normalizeObservation(input.observation, this.identity);
    const findings: string[] = [];
    const guard = input.guardId ? this.guards.get(input.guardId.trim()) : null;
    let checksumValid = true;
    if (input.guardId && !guard) {
      checksumValid = false;
      findings.push("resume_guard_missing");
    }
    if (guard) {
      if (guard.manifestId !== manifest.manifestId) findings.push("resume_guard_manifest_mismatch");
      if (guard.expectedManifestDigest !== manifest.manifestDigest) findings.push("resume_guard_manifest_digest_mismatch");
      const restoredChecksum = input.restoredSnapshotChecksum?.replace(/^sha256:/, "").toLowerCase() || "";
      if (restoredChecksum !== guard.expectedSnapshotChecksum) {
        checksumValid = false;
        findings.push("restored_snapshot_checksum_mismatch");
      }
    }
    const identityValid = observation.identity.taskId === manifest.identity.taskId
      && observation.identity.sessionId === manifest.identity.sessionId;
    if (!identityValid) findings.push("resume_identity_mismatch");
    const epochValid = observation.restartEpoch >= manifest.restartEpoch
      && observation.contextEpoch >= manifest.contextEpoch
      && observation.applicationRevision >= manifest.applicationRevision;
    if (observation.restartEpoch < manifest.restartEpoch) findings.push("restart_epoch_regressed");
    if (observation.contextEpoch < manifest.contextEpoch) findings.push("context_epoch_regressed");
    if (observation.applicationRevision < manifest.applicationRevision) findings.push("application_revision_regressed");

    const actualOutcomeIds = uniqueStrings(observation.outcomes.map((item) => item.memoryId));
    const actualProcedureIds = uniqueStrings(observation.procedures.map((item) => item.procedureId));
    const actualProjectionIds = uniqueStrings(observation.projections.map((item) => item.projectionId));
    const actualSignalIds = uniqueStrings(observation.signals.map((item) => item.signalId));
    const missingOutcomeIds = difference(manifest.outcomeIds, actualOutcomeIds);
    const unexpectedOutcomeIds = difference(actualOutcomeIds, manifest.outcomeIds);
    const missingProcedureIds = difference(manifest.procedureIds, actualProcedureIds);
    const unexpectedProcedureIds = difference(actualProcedureIds, manifest.procedureIds);
    const missingProjectionIds = difference(manifest.projectionIds, actualProjectionIds);
    const missingSignalIds = difference(manifest.signalIds, actualSignalIds);
    const missingArchiveIds = difference(manifest.archiveIds, observation.archiveIds);
    const missingAuthorityReceiptIds = difference(manifest.authorityReceiptIds, observation.authorityReceiptIds);
    const missingRetrievalReceiptIds = difference(manifest.retrievalReceiptIds, observation.retrievalReceiptIds);
    const missingFidelityComparisonIds = difference(manifest.fidelityComparisonIds, observation.fidelityComparisonIds);
    const missingDirectiveIds = difference(manifest.directiveIds, observation.directiveIds);
    if (missingOutcomeIds.length) findings.push("skill_outcomes_lost_after_resume");
    if (missingProcedureIds.length) findings.push("procedures_lost_after_resume");
    if (missingProjectionIds.length) findings.push("compact_projections_lost_after_resume");
    if (missingSignalIds.length) findings.push("memory_signals_lost_after_resume");
    if (missingArchiveIds.length) findings.push("compact_archives_lost_after_resume");
    if (missingAuthorityReceiptIds.length) findings.push("authority_receipts_lost_after_resume");
    if (missingRetrievalReceiptIds.length) findings.push("retrieval_receipts_lost_after_resume");
    if (missingFidelityComparisonIds.length) findings.push("fidelity_comparisons_lost_after_resume");
    if (missingDirectiveIds.length) findings.push("consumer_directives_lost_after_resume");

    const currentPolicies = currentPolicyWatermarks(observation.outcomes);
    const currentVersions = currentVersionWatermarks(observation.outcomes);
    const policyWatermarkChanges = watermarkChanges(manifest.policyWatermarks, currentPolicies);
    const versionWatermarkChanges = watermarkChanges(manifest.versionWatermarks, currentVersions);
    for (const change of policyWatermarkChanges) {
      if (change.state === "removed") findings.push("skill_policy_watermark_missing_after_resume");
      if (change.state === "changed") findings.push("skill_policy_changed_requires_current_03c_resolution");
    }
    for (const change of versionWatermarkChanges) {
      if (change.state === "removed") findings.push("skill_version_watermark_missing_after_resume");
      if (change.state === "changed") findings.push("skill_version_changed_requires_current_03c_resolution");
    }

    const lossless = checksumValid
      && identityValid
      && epochValid
      && missingOutcomeIds.length === 0
      && missingProcedureIds.length === 0
      && missingProjectionIds.length === 0
      && missingSignalIds.length === 0
      && missingArchiveIds.length === 0
      && missingAuthorityReceiptIds.length === 0
      && missingRetrievalReceiptIds.length === 0
      && missingFidelityComparisonIds.length === 0
      && missingDirectiveIds.length === 0;
    const state: ResumeContinuityState = lossless ? "verified" : "rejected";
    if (lossless) findings.push("resume_continuity_verified");
    findings.push("skill_execution_authority_not_restored");
    findings.push("current_03c_authority_revalidation_required");
    const observedAt = nowIso(this.now);
    const unsigned = {
      receiptId: stableId(
        "skill-memory-resume-receipt",
        manifest.manifestId,
        observation.restartEpoch,
        observation.contextEpoch,
        digest(actualOutcomeIds),
        digest(actualProjectionIds),
        lossless,
      ),
      protocol: RESUME_CONTINUITY_PROTOCOL,
      manifestId: manifest.manifestId,
      identity: cloneJson(observation.identity),
      state,
      restartEpochBefore: manifest.restartEpoch,
      restartEpochAfter: observation.restartEpoch,
      contextEpochBefore: manifest.contextEpoch,
      contextEpochAfter: observation.contextEpoch,
      applicationRevisionBefore: manifest.applicationRevision,
      applicationRevisionAfter: observation.applicationRevision,
      expectedOutcomeIds: cloneJson(manifest.outcomeIds),
      actualOutcomeIds,
      missingOutcomeIds,
      unexpectedOutcomeIds,
      expectedProcedureIds: cloneJson(manifest.procedureIds),
      actualProcedureIds,
      missingProcedureIds,
      unexpectedProcedureIds,
      expectedProjectionIds: cloneJson(manifest.projectionIds),
      actualProjectionIds,
      missingProjectionIds,
      expectedSignalIds: cloneJson(manifest.signalIds),
      actualSignalIds,
      missingSignalIds,
      missingArchiveIds,
      missingAuthorityReceiptIds,
      missingRetrievalReceiptIds,
      missingFidelityComparisonIds,
      missingDirectiveIds,
      policyWatermarkChanges,
      versionWatermarkChanges,
      checksumValid,
      identityValid,
      epochValid,
      lossless,
      executableAuthorityRestored: false,
      currentAuthorityRevalidationRequired: true,
      findings: uniqueStrings(findings),
      observedAt,
      metadata: {
        ...input.metadata,
        manifest_source_digest: manifest.sourceDigest,
        current_source_digest: observationDigest(observation),
        canonical_session_owner: "E01 DurableSessionRuntime",
        continuity_owner: "SkillMemoryResumeContinuityRuntime",
      },
    } satisfies Omit<ResumeContinuityReceipt, "receiptDigest">;
    const receipt: ResumeContinuityReceipt = { ...unsigned, receiptDigest: digest(unsigned) };
    const existing = this.receipts.get(receipt.receiptId);
    if (existing && existing.receiptDigest !== receipt.receiptDigest) {
      throw contractError("resume_continuity_receipt_conflict", "resume continuity receipt id conflict");
    }
    this.receipts.set(receipt.receiptId, receipt);
    if (guard && !guard.consumedAt) {
      const consumed: ResumeContinuityGuard = { ...guard, consumedAt: observedAt, guardDigest: "" };
      const { guardDigest: _ignored, ...guardUnsigned } = consumed;
      consumed.guardDigest = digest(guardUnsigned);
      this.guards.set(consumed.guardId, consumed);
    }
    this.revision += existing ? 0 : 1;
    return cloneJson(existing ?? receipt);
  }

  activeManifest(): ResumeContinuityManifest | null {
    if (!this.activeManifestId) return null;
    const manifest = this.manifests.get(this.activeManifestId);
    return manifest ? cloneJson(manifest) : null;
  }

  listReceipts(limit = 1_000): ResumeContinuityReceipt[] {
    return [...this.receipts.values()]
      .sort((left, right) => right.observedAt.localeCompare(left.observedAt) || left.receiptId.localeCompare(right.receiptId))
      .slice(0, Math.max(0, Math.min(limit, 100_000)))
      .map(cloneJson);
  }

  health(): JsonObject {
    const receipts = [...this.receipts.values()];
    return {
      protocol: RESUME_CONTINUITY_PROTOCOL,
      revision: this.revision,
      manifest_count: this.manifests.size,
      receipt_count: receipts.length,
      guard_count: this.guards.size,
      active_manifest_id: this.activeManifestId,
      verified_count: receipts.filter((item) => item.lossless).length,
      rejected_count: receipts.filter((item) => !item.lossless).length,
      lost_outcome_count: receipts.reduce((total, item) => total + item.missingOutcomeIds.length, 0),
      lost_procedure_count: receipts.reduce((total, item) => total + item.missingProcedureIds.length, 0),
      lost_projection_count: receipts.reduce((total, item) => total + item.missingProjectionIds.length, 0),
      executable_authority_restored: false,
      current_03c_resolution_required: true,
    };
  }

  snapshot(): ResumeContinuitySnapshot {
    const unsigned: Omit<ResumeContinuitySnapshot, "checksum"> = {
      version: "zyra.skill-memory-resume-continuity-runtime/v1",
      identity: cloneJson(this.identity),
      revision: this.revision,
      manifests: [...this.manifests.values()].sort((left, right) => left.manifestId.localeCompare(right.manifestId)).map(cloneJson),
      receipts: this.listReceipts(100_000).sort((left, right) => left.receiptId.localeCompare(right.receiptId)),
      guards: [...this.guards.values()].sort((left, right) => left.guardId.localeCompare(right.guardId)).map(cloneJson),
      activeManifestId: this.activeManifestId,
      capturedAt: nowIso(this.now),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshotValue: ResumeContinuitySnapshot): void {
    if (snapshotValue.version !== "zyra.skill-memory-resume-continuity-runtime/v1") {
      throw contractError("resume_continuity_snapshot_version", "unsupported resume continuity snapshot version");
    }
    const { checksum, ...unsigned } = snapshotValue;
    if (digest(unsigned) !== checksum) {
      throw contractError("resume_continuity_snapshot_checksum", "resume continuity snapshot checksum mismatch");
    }
    if (snapshotValue.identity.taskId !== this.identity.taskId || snapshotValue.identity.sessionId !== this.identity.sessionId) {
      throw contractError("resume_continuity_snapshot_binding", "resume continuity snapshot belongs to another task/session");
    }
    const manifests = new Map<string, ResumeContinuityManifest>();
    for (const manifest of snapshotValue.manifests) {
      const { manifestDigest, ...manifestUnsigned } = manifest;
      if (digest(manifestUnsigned) !== manifestDigest) {
        throw contractError("resume_continuity_manifest_digest", `resume continuity manifest digest mismatch: ${manifest.manifestId}`);
      }
      manifests.set(manifest.manifestId, cloneJson(manifest));
    }
    const receipts = new Map<string, ResumeContinuityReceipt>();
    for (const receipt of snapshotValue.receipts) {
      const { receiptDigest, ...receiptUnsigned } = receipt;
      if (digest(receiptUnsigned) !== receiptDigest) {
        throw contractError("resume_continuity_receipt_digest", `resume continuity receipt digest mismatch: ${receipt.receiptId}`);
      }
      if (!manifests.has(receipt.manifestId)) {
        throw contractError("resume_continuity_receipt_manifest", `resume continuity receipt references missing manifest: ${receipt.manifestId}`);
      }
      receipts.set(receipt.receiptId, cloneJson(receipt));
    }
    const guards = new Map<string, ResumeContinuityGuard>();
    for (const guard of snapshotValue.guards) {
      const { guardDigest, ...guardUnsigned } = guard;
      if (digest(guardUnsigned) !== guardDigest) {
        throw contractError("resume_continuity_guard_digest", `resume continuity guard digest mismatch: ${guard.guardId}`);
      }
      if (!manifests.has(guard.manifestId)) {
        throw contractError("resume_continuity_guard_manifest", `resume continuity guard references missing manifest: ${guard.manifestId}`);
      }
      guards.set(guard.guardId, cloneJson(guard));
    }
    if (snapshotValue.activeManifestId && !manifests.has(snapshotValue.activeManifestId)) {
      throw contractError("resume_continuity_active_manifest", "active resume continuity manifest is missing");
    }
    this.manifests.clear();
    this.receipts.clear();
    this.guards.clear();
    for (const [key, value] of manifests) this.manifests.set(key, value);
    for (const [key, value] of receipts) this.receipts.set(key, value);
    for (const [key, value] of guards) this.guards.set(key, value);
    this.activeManifestId = snapshotValue.activeManifestId;
    this.revision = snapshotValue.revision;
  }

  private assertEnabled(): void {
    if (process.env.ZYRA_DISABLE_SKILL_MEMORY_RESUME_CONTINUITY === "1") {
      throw contractError("resume_continuity_runtime_disabled", "06C skill memory resume continuity runtime is disabled");
    }
  }
}

function normalizeObservation(value: ResumeContinuityObservation, expected: RuntimeIdentity): Required<ResumeContinuityObservation> {
  if (value.identity.taskId !== expected.taskId || value.identity.sessionId !== expected.sessionId) {
    throw contractError("resume_continuity_observation_binding", "resume continuity observation belongs to another task/session");
  }
  const integer = (input: number, label: string): number => {
    if (!Number.isSafeInteger(input) || input < 0) throw contractError("resume_continuity_observation_integer", `${label} must be non-negative`);
    return input;
  };
  return {
    identity: cloneJson(value.identity),
    restartEpoch: integer(value.restartEpoch, "restart epoch"),
    contextEpoch: integer(value.contextEpoch, "context epoch"),
    applicationRevision: integer(value.applicationRevision, "application revision"),
    outcomes: value.outcomes.map(cloneJson),
    procedures: value.procedures.map(cloneJson),
    projections: value.projections.map(cloneJson),
    signals: value.signals.map(cloneJson),
    archiveIds: uniqueStrings(value.archiveIds),
    authorityReceiptIds: uniqueStrings(value.authorityReceiptIds),
    retrievalReceiptIds: uniqueStrings(value.retrievalReceiptIds),
    fidelityComparisonIds: uniqueStrings(value.fidelityComparisonIds),
    directiveIds: uniqueStrings(value.directiveIds),
    metadata: cloneJson(value.metadata ?? {}),
  };
}

function difference(left: string[], right: string[]): string[] {
  const rightSet = new Set(uniqueStrings(right));
  return uniqueStrings(left).filter((item) => !rightSet.has(item));
}

function currentPolicyWatermarks(outcomes: SkillInvocationOutcomeMemory[]): Record<string, string> {
  const output: Record<string, string> = {};
  for (const outcome of [...outcomes].sort((left, right) => left.revision - right.revision)) {
    output[outcome.version.skillId] = `${outcome.policy.policyRevision}:${outcome.policy.policyDigest}:${digest(outcome.policy.effectiveTools)}:${digest(outcome.policy.deniedTools)}`;
  }
  return sortRecord(output);
}

function currentVersionWatermarks(outcomes: SkillInvocationOutcomeMemory[]): Record<string, string> {
  const output: Record<string, string> = {};
  for (const outcome of [...outcomes].sort((left, right) => left.version.registryRevision - right.version.registryRevision || left.revision - right.revision)) {
    output[outcome.version.skillId] = `${outcome.version.registryRevision}:${outcome.version.descriptorDigest}:${outcome.version.bodyDigest}:${outcome.version.sourceRevision}`;
  }
  return sortRecord(output);
}

function watermarkChanges(expected: Record<string, string>, actual: Record<string, string>): JsonObject[] {
  const changes: JsonObject[] = [];
  for (const skillId of uniqueStrings([...Object.keys(expected), ...Object.keys(actual)])) {
    const before = expected[skillId] ?? null;
    const after = actual[skillId] ?? null;
    const state = before === after ? "unchanged" : before === null ? "added" : after === null ? "removed" : "changed";
    changes.push({ skill_id: skillId, before, after, state });
  }
  return changes;
}

function observationDigest(value: Required<ResumeContinuityObservation>): string {
  return digest({
    identity: value.identity,
    restart_epoch: value.restartEpoch,
    context_epoch: value.contextEpoch,
    application_revision: value.applicationRevision,
    outcomes: value.outcomes.map((item) => [item.memoryId, item.recordDigest, item.revision]),
    procedures: value.procedures.map((item) => [item.procedureId, item.procedureDigest, item.revision]),
    projections: value.projections.map((item) => [item.projectionId, item.projectionDigest, item.state]),
    signals: value.signals.map((item) => [item.signalId, item.signalDigest, item.sequence]),
    archives: value.archiveIds,
    authority_receipts: value.authorityReceiptIds,
    retrieval_receipts: value.retrievalReceiptIds,
    fidelity_comparisons: value.fidelityComparisonIds,
    directives: value.directiveIds,
  });
}

function compareVersionWatermarks(left: string, right: string): number {
  const leftRevision = Number(left.split(":", 1)[0] ?? 0);
  const rightRevision = Number(right.split(":", 1)[0] ?? 0);
  return leftRevision - rightRevision || left.localeCompare(right);
}

function latestOutcomeRevision(
  outcomes: SkillInvocationOutcomeMemory[],
  skillId: string,
  _watermark: string,
): number {
  return outcomes
    .filter((item) => item.version.skillId === skillId)
    .reduce((maximum, item) => Math.max(maximum, item.revision), 0);
}

function sortRecord(value: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(value).sort(([left], [right]) => left.localeCompare(right)));
}
