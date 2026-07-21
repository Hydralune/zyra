import {
  cloneJson,
  contractError,
  digest,
  nowIso,
  stableId,
  uniqueStrings,
  type CompactArchiveReference,
  type CompactMessageBlock,
  type CompactRestoreProjection,
  type JsonObject,
  type JsonValue,
  type MemorySignal,
  type RestoreAttachmentReference,
  type ReusableProcedure,
  type RuntimeIdentity,
  type SkillInvocationOutcomeMemory,
  type SnapcompactFrame,
} from "./contracts.ts";
import {
  SkillAuthorityRevalidationRuntime,
  type CurrentSkillAuthority,
  type SkillAuthorityRevalidationInput,
  type SkillAuthorityRevalidationReceipt,
  type SkillAuthorityRevalidationSnapshot,
} from "./authority-runtime.ts";
import {
  MemorySignalConsumerRuntime,
  type MemoryConsumerDirective,
  type MemoryConsumerDirectiveSnapshot,
} from "./consumer-directive-runtime.ts";
import {
  SkillMemoryContinuityFailureRuntime,
  type ContinuityFailureSnapshot,
} from "./continuity-failure-runtime.ts";
import {
  ProviderRestoreFidelityRuntime,
  type ProviderRestoreEnvelope,
  type RestoreFidelityComparison,
  type RestoreFidelityRuntimeSnapshot,
  type RestoreHistoryScenario,
  type RestoreProviderKind,
  type RestoreSemanticFactSet,
} from "./fidelity-runtime.ts";
import {
  SkillMemoryRetrievalCompositionRuntime,
  codeReferencesFromJson,
  memoryHintsFromJson,
  proceduresFromJson,
  type RetrievalCodeReference,
  type RetrievalCompositionDelivery,
  type RetrievalCompositionInput,
  type RetrievalCompositionReceipt,
  type RetrievalCompositionSnapshot,
  type RetrievalMemoryHint,
} from "./retrieval-composition-runtime.ts";
import {
  SkillMemoryResumeContinuityRuntime,
  type ResumeContinuityManifest,
  type ResumeContinuityObservation,
  type ResumeContinuityReceipt,
  type ResumeContinuitySnapshot,
} from "./resume-continuity-runtime.ts";

export const SKILL_MEMORY_INTEGRATION_PROTOCOL = "zyra.skill-memory-integration/v1" as const;

export interface SkillMemoryIntegrationSnapshot {
  version: "zyra.skill-memory-integration-runtime/v1";
  identity: RuntimeIdentity;
  revision: number;
  authority: SkillAuthorityRevalidationSnapshot;
  fidelity: RestoreFidelityRuntimeSnapshot;
  retrieval: RetrievalCompositionSnapshot;
  resume: ResumeContinuitySnapshot;
  consumers: MemoryConsumerDirectiveSnapshot;
  failures: ContinuityFailureSnapshot;
  preparations: SkillMemoryIntegratedPreparation[];
  applications: SkillMemoryIntegratedApplication[];
  browserExports: BrowserSkillMemoryContextProjection[];
  capturedAt: string;
  checksum: string;
}

export interface SkillMemoryIntegratedPreparationInput {
  identity: RuntimeIdentity;
  workerKind: "code" | "browser";
  boundaryId: string;
  archive: CompactArchiveReference;
  history: CompactMessageBlock[];
  goal: string;
  constraints: string[];
  requirementChanges: string[];
  skillMemories: SkillInvocationOutcomeMemory[];
  procedures: ReusableProcedure[];
  existingAttachments: RestoreAttachmentReference[];
  runtimeConstraints: JsonObject;
  allowedTools: string[];
  deniedTools: string[];
  providerId: string;
  modelId: string;
  providerKind: RestoreProviderKind;
  providerCapabilities: string[];
  gatewayCapabilities: string[];
  frames: SnapcompactFrame[];
  sourceProviderId?: string | null;
  sourceModelId?: string | null;
  contextWindow: number;
  maximumTokens: number;
  metadata?: JsonObject;
}

export interface SkillMemoryIntegratedPreparation {
  preparationId: string;
  protocol: typeof SKILL_MEMORY_INTEGRATION_PROTOCOL;
  identity: RuntimeIdentity;
  workerKind: "code" | "browser";
  boundaryId: string;
  archiveId: string;
  retrievalReceiptId: string;
  retrievalDeliveryId: string;
  fidelityComparisonId: string;
  selectedStrategy: string;
  selectedEnvelopeId: string;
  attachments: RestoreAttachmentReference[];
  providerMessage: JsonObject;
  memoryIds: string[];
  procedureIds: string[];
  evidenceIds: string[];
  artifactIds: string[];
  authoritySkillIds: string[];
  currentAuthorityRequired: true;
  baselineTextRefAvailable: true;
  failureEvents: JsonObject[];
  createdAt: string;
  metadata: JsonObject;
  preparationDigest: string;
}

export interface SkillMemoryIntegratedApplication {
  applicationId: string;
  protocol: typeof SKILL_MEMORY_INTEGRATION_PROTOCOL;
  preparationId: string;
  projectionId: string;
  workerKind: "code" | "browser";
  boundaryId: string;
  contextEpochBefore: number;
  contextEpochAfter: number;
  retrievalDeliveryId: string;
  retrievalDeliveryState: "committed" | "released";
  providerRequestIds: string[];
  terminalEventIds: string[];
  authorityReceiptIds: string[];
  consumerDirectiveIds: string[];
  applied: boolean;
  reason: string;
  appliedAt: string;
  metadata: JsonObject;
  applicationDigest: string;
}

export interface BrowserSkillMemoryContextProjection {
  projectionId: string;
  protocol: "zyra.browser-skill-memory-context/v1";
  identity: RuntimeIdentity;
  boundaryId: string;
  compactProjectionId: string;
  contextEpoch: number;
  preparationId: string;
  retrievalReceiptId: string;
  fidelityComparisonId: string;
  selectedStrategy: string;
  summary: string;
  memoryIds: string[];
  procedureIds: string[];
  evidenceIds: string[];
  artifactIds: string[];
  authoritySkillIds: string[];
  allowedTools: string[];
  deniedTools: string[];
  providerId: string;
  modelId: string;
  providerMessage: JsonObject;
  attachments: RestoreAttachmentReference[];
  currentAuthorityRequired: true;
  executableSkillBodyPresent: false;
  createdAt: string;
  metadata: JsonObject;
  projectionDigest: string;
}

export interface SkillMemoryIntegrationHealth {
  protocol: typeof SKILL_MEMORY_INTEGRATION_PROTOCOL;
  revision: number;
  preparationCount: number;
  applicationCount: number;
  browserExportCount: number;
  authority: JsonObject;
  fidelity: JsonObject;
  retrieval: JsonObject;
  resume: JsonObject;
  consumers: JsonObject;
  failures: JsonObject;
  canonicalSkillOwner: "03C SkillCoordinator";
  canonicalCompactOwner: "02D ContextCompactionRuntime";
  canonicalContextOwner: "02B ContextAssemblyRuntime";
  canonicalMemoryOwner: "SQLiteStore.memory_records";
  canonicalProcedureOwner: "ReusableProcedureStore";
}

/**
 * Integrates the 06C-01 owners. This class does not replace them: it prepares
 * inputs for SkillContextProjector/CompactRestoreMemoryBridge and stores the
 * cross-provider, retrieval, resume, and downstream-consumer evidence that
 * must travel with the same E01 snapshot.
 */
export class SkillMemoryIntegrationRuntime {
  readonly identity: RuntimeIdentity;
  readonly authority: SkillAuthorityRevalidationRuntime;
  readonly fidelity: ProviderRestoreFidelityRuntime;
  readonly retrieval: SkillMemoryRetrievalCompositionRuntime;
  readonly resume: SkillMemoryResumeContinuityRuntime;
  readonly consumers: MemorySignalConsumerRuntime;
  readonly failures: SkillMemoryContinuityFailureRuntime;
  private readonly preparations = new Map<string, SkillMemoryIntegratedPreparation>();
  private readonly applications = new Map<string, SkillMemoryIntegratedApplication>();
  private readonly browserExports = new Map<string, BrowserSkillMemoryContextProjection>();
  private readonly now: () => Date;
  private revision = 0;

  constructor(options: {
    identity: RuntimeIdentity;
    now?: () => Date;
    snapshot?: SkillMemoryIntegrationSnapshot | null;
  }) {
    this.identity = cloneJson(options.identity);
    this.now = options.now ?? (() => new Date());
    const snapshot = options.snapshot ?? null;
    this.authority = new SkillAuthorityRevalidationRuntime({
      identity: this.identity,
      now: this.now,
      snapshot: snapshot?.authority ?? null,
    });
    this.fidelity = new ProviderRestoreFidelityRuntime({
      now: this.now,
      snapshot: snapshot?.fidelity ?? null,
    });
    this.retrieval = new SkillMemoryRetrievalCompositionRuntime({
      identity: this.identity,
      now: this.now,
      snapshot: snapshot?.retrieval ?? null,
    });
    this.resume = new SkillMemoryResumeContinuityRuntime({
      identity: this.identity,
      now: this.now,
      snapshot: snapshot?.resume ?? null,
    });
    this.consumers = new MemorySignalConsumerRuntime({
      identity: this.identity,
      now: this.now,
      snapshot: snapshot?.consumers ?? null,
    });
    this.failures = new SkillMemoryContinuityFailureRuntime({
      identity: this.identity,
      now: this.now,
      snapshot: snapshot?.failures ?? null,
    });
    if (snapshot) {
      this.validateSnapshot(snapshot);
      for (const preparation of snapshot.preparations) this.restorePreparation(preparation);
      for (const application of snapshot.applications) this.restoreApplication(application);
      for (const projection of snapshot.browserExports) this.restoreBrowserExport(projection);
      this.revision = snapshot.revision;
    }
  }

  prepare(inputValue: SkillMemoryIntegratedPreparationInput): SkillMemoryIntegratedPreparation {
    this.assertEnabled();
    const input = normalizePreparationInput(inputValue, this.identity);
    const retrievalInput = this.retrievalInput(input);
    const retrievalReceipt = this.retrieval.compose(retrievalInput);
    const retrievalDelivery = this.retrieval.prepareDelivery({
      receiptId: retrievalReceipt.receiptId,
      workerRequestId: input.identity.workerRequestId,
      workerKind: input.workerKind,
      metadata: {
        boundary_id: input.boundaryId,
        archive_id: input.archive.archiveId,
      },
    });
    const fidelityScenario = this.fidelityScenario(input);
    const comparison = this.fidelity.compare(fidelityScenario);
    const selectedEnvelope = this.fidelity.selectedEnvelope(comparison.comparisonId);
    const providerMessage = this.fidelity.providerMessage(selectedEnvelope);
    const attachments = mergeAttachments(
      input.existingAttachments,
      retrievalReceipt.attachments,
      envelopeAttachments(selectedEnvelope),
    );
    const failureEvents = this.fidelity.failureEvents(comparison.comparisonId);
    for (const failureEvent of failureEvents) {
      this.failures.record({
        identity: input.identity,
        boundaryId: input.boundaryId,
        stage: "restore_fidelity",
        code: stringValue(failureEvent.code) || stringValue(failureEvent.kind) || "restore_fidelity_fallback",
        message: stringValue(failureEvent.message) || stringValue(failureEvent.reason) || "provider restore fidelity required a fallback",
        retryable: true,
        baselineTextReferenceAvailable: true,
        canonicalCheckpointAvailable: true,
        currentAuthorityRequired: true,
        provenanceComplete: true,
        relatedIds: [comparison.comparisonId, selectedEnvelope.envelopeId],
        details: failureEvent,
        causationId: input.boundaryId,
      });
    }
    const evidenceIds = uniqueStrings([
      ...input.skillMemories.flatMap((item) => item.evidence.map((evidence) => evidence.evidenceId)),
      ...retrievalReceipt.candidates.flatMap((item) => item.evidenceIds),
    ]);
    const artifactIds = uniqueStrings([
      input.archive.artifactId,
      ...input.skillMemories.flatMap((item) => item.artifactIds),
      ...input.procedures.flatMap((item) => item.provenance.artifactIds),
      ...retrievalReceipt.selectedArtifactIds,
    ]);
    const createdAt = nowIso(this.now);
    const unsigned = {
      preparationId: stableId(
        "skill-memory-integrated-preparation",
        input.identity.taskId,
        input.identity.sessionId,
        input.boundaryId,
        retrievalReceipt.receiptId,
        comparison.comparisonId,
      ),
      protocol: SKILL_MEMORY_INTEGRATION_PROTOCOL,
      identity: cloneJson(input.identity),
      workerKind: input.workerKind,
      boundaryId: input.boundaryId,
      archiveId: input.archive.archiveId,
      retrievalReceiptId: retrievalReceipt.receiptId,
      retrievalDeliveryId: retrievalDelivery.deliveryId,
      fidelityComparisonId: comparison.comparisonId,
      selectedStrategy: comparison.selectedStrategy,
      selectedEnvelopeId: selectedEnvelope.envelopeId,
      attachments,
      providerMessage,
      memoryIds: uniqueStrings(input.skillMemories.map((item) => item.memoryId)),
      procedureIds: uniqueStrings([
        ...input.procedures.map((item) => item.procedureId),
        ...retrievalReceipt.selectedProcedureIds,
      ]),
      evidenceIds,
      artifactIds,
      authoritySkillIds: uniqueStrings([
        ...input.skillMemories.map((item) => item.version.skillId),
        ...input.procedures.flatMap((item) => item.provenance.skillVersions.map((version) => version.skillId)),
      ]),
      currentAuthorityRequired: true,
      baselineTextRefAvailable: true,
      failureEvents,
      createdAt,
      metadata: {
        ...input.metadata,
        provider_id: input.providerId,
        model_id: input.modelId,
        provider_kind: input.providerKind,
        source_provider_id: input.sourceProviderId,
        source_model_id: input.sourceModelId,
        memory_query_id: retrievalReceipt.memoryQueryId,
        code_query_id: retrievalReceipt.codeQueryId,
        selected_candidate_ids: retrievalReceipt.selectedCandidateIds,
        selected_tokens: retrievalReceipt.selectedTokens,
        fallback_events_explicit: failureEvents.length,
        canonical_skill_owner: "03C SkillCoordinator",
        canonical_compact_owner: "02D ContextCompactionRuntime",
        canonical_context_owner: "02B ContextAssemblyRuntime",
      },
    } satisfies Omit<SkillMemoryIntegratedPreparation, "preparationDigest">;
    const preparation: SkillMemoryIntegratedPreparation = {
      ...unsigned,
      preparationDigest: digest(unsigned),
    };
    const existing = this.preparations.get(preparation.preparationId);
    if (existing && existing.preparationDigest !== preparation.preparationDigest) {
      throw contractError("skill_memory_integration_preparation_conflict", "skill memory integration preparation id conflict");
    }
    this.preparations.set(preparation.preparationId, preparation);
    this.revision += existing ? 0 : 1;
    return cloneJson(existing ?? preparation);
  }

  apply(input: {
    preparationId: string;
    projection: CompactRestoreProjection;
    providerRequestIds?: string[];
    terminalEventIds?: string[];
    authorityReceipts?: SkillAuthorityRevalidationReceipt[];
    committed?: boolean;
    reason?: string;
    metadata?: JsonObject;
  }): SkillMemoryIntegratedApplication {
    this.assertEnabled();
    const preparation = this.preparations.get(input.preparationId.trim());
    if (!preparation) throw contractError("skill_memory_integration_preparation_missing", `skill memory integration preparation not found: ${input.preparationId}`);
    if (input.projection.boundaryId !== preparation.boundaryId) {
      throw contractError("skill_memory_integration_projection_boundary", "compact projection does not match integration preparation boundary");
    }
    if (input.projection.identity.taskId !== this.identity.taskId || input.projection.identity.sessionId !== this.identity.sessionId) {
      throw contractError("skill_memory_integration_projection_binding", "compact projection belongs to another task/session");
    }
    const committed = input.committed ?? input.projection.state === "applied";
    const delivery = this.retrieval.settleDelivery({
      deliveryId: preparation.retrievalDeliveryId,
      committed,
      providerRequestIds: input.providerRequestIds,
      terminalEventIds: input.terminalEventIds,
      reason: input.reason,
    });
    const directives = this.consumers.list({ limit: 100_000 }).filter((item) =>
      item.compactBoundaryId === preparation.boundaryId
      || preparation.memoryIds.includes(item.aggregateId)
      || preparation.procedureIds.includes(item.aggregateId)
    );
    const appliedAt = nowIso(this.now);
    const unsigned = {
      applicationId: stableId(
        "skill-memory-integrated-application",
        preparation.preparationId,
        input.projection.projectionId,
        committed,
      ),
      protocol: SKILL_MEMORY_INTEGRATION_PROTOCOL,
      preparationId: preparation.preparationId,
      projectionId: input.projection.projectionId,
      workerKind: preparation.workerKind,
      boundaryId: preparation.boundaryId,
      contextEpochBefore: input.projection.contextEpochBefore,
      contextEpochAfter: input.projection.contextEpochAfter,
      retrievalDeliveryId: delivery.deliveryId,
      retrievalDeliveryState: delivery.state as "committed" | "released",
      providerRequestIds: uniqueStrings(input.providerRequestIds),
      terminalEventIds: uniqueStrings(input.terminalEventIds),
      authorityReceiptIds: uniqueStrings((input.authorityReceipts ?? []).map((item) => item.receiptId)),
      consumerDirectiveIds: uniqueStrings(directives.map((item) => item.directiveId)),
      applied: committed,
      reason: input.reason?.trim() || (committed ? "compact_restore_applied" : "compact_restore_released"),
      appliedAt,
      metadata: {
        ...input.metadata,
        selected_strategy: preparation.selectedStrategy,
        selected_envelope_id: preparation.selectedEnvelopeId,
        baseline_text_ref_available: true,
        current_authority_required: true,
      },
    } satisfies Omit<SkillMemoryIntegratedApplication, "applicationDigest">;
    const application: SkillMemoryIntegratedApplication = {
      ...unsigned,
      applicationDigest: digest(unsigned),
    };
    const existing = this.applications.get(application.applicationId);
    if (existing && existing.applicationDigest !== application.applicationDigest) {
      throw contractError("skill_memory_integration_application_conflict", "skill memory integration application id conflict");
    }
    this.applications.set(application.applicationId, application);
    this.revision += existing ? 0 : 1;
    return cloneJson(existing ?? application);
  }

  revalidate(input: SkillAuthorityRevalidationInput): SkillAuthorityRevalidationReceipt {
    const receipt = this.authority.revalidate(input);
    if (!receipt.executable) {
      this.failures.record({
        identity: input.identity,
        boundaryId: stringValue(input.metadata?.compact_boundary_id) || input.causationId || input.memory.memoryId,
        stage: "authority_revalidation",
        code: `skill_authority_${receipt.disposition}`,
        message: receipt.reasons.join(", ") || "current 03C skill authority rejected historical memory",
        retryable: false,
        baselineTextReferenceAvailable: true,
        canonicalCheckpointAvailable: true,
        currentAuthorityRequired: true,
        provenanceComplete: true,
        relatedIds: [receipt.receiptId, receipt.memoryId, receipt.skillId],
        details: { disposition: receipt.disposition, reasons: receipt.reasons },
        causationId: receipt.causationId,
      });
    }
    this.revision += 1;
    return receipt;
  }

  revalidateMemory(input: {
    memory: SkillInvocationOutcomeMemory;
    current: CurrentSkillAuthority;
    parentAllowedTools: string[];
    parentDeniedTools: string[];
    causationId?: string;
    metadata?: JsonObject;
  }): SkillAuthorityRevalidationReceipt {
    return this.revalidate({
      identity: this.identity,
      memory: input.memory,
      current: input.current,
      parentAllowedTools: input.parentAllowedTools,
      parentDeniedTools: input.parentDeniedTools,
      causationId: input.causationId,
      metadata: input.metadata,
    });
  }

  consumeSignals(signals: MemorySignal[]): MemoryConsumerDirective[] {
    const directives = this.consumers.consumeMany(signals);
    if (directives.length) this.revision += 1;
    return directives;
  }

  captureContinuity(observation: ResumeContinuityObservation): ResumeContinuityManifest {
    const manifest = this.resume.capture({
      ...observation,
      authorityReceiptIds: this.authority.list({ limit: 100_000 }).map((item) => item.receiptId),
      retrievalReceiptIds: this.retrieval.listReceipts(100_000).map((item) => item.receiptId),
      fidelityComparisonIds: this.fidelity.list(100_000).map((item) => item.comparisonId),
      directiveIds: this.consumers.list({ limit: 100_000 }).map((item) => item.directiveId),
    });
    this.revision += 1;
    return manifest;
  }

  verifyContinuity(input: {
    manifestId: string;
    observation: ResumeContinuityObservation;
    guardId?: string | null;
    restoredSnapshotChecksum?: string | null;
    metadata?: JsonObject;
  }): ResumeContinuityReceipt {
    const receipt = this.resume.verify({
      ...input,
      observation: {
        ...input.observation,
        authorityReceiptIds: this.authority.list({ limit: 100_000 }).map((item) => item.receiptId),
        retrievalReceiptIds: this.retrieval.listReceipts(100_000).map((item) => item.receiptId),
        fidelityComparisonIds: this.fidelity.list(100_000).map((item) => item.comparisonId),
        directiveIds: this.consumers.list({ limit: 100_000 }).map((item) => item.directiveId),
      },
    });
    if (!receipt.lossless) {
      this.failures.record({
        identity: input.observation.identity,
        boundaryId: stringValue(input.metadata?.compact_boundary_id) || input.manifestId,
        stage: "resume_verification",
        code: "skill_memory_resume_loss",
        message: receipt.findings.join(", ") || "skill-memory continuity was not lossless",
        retryable: false,
        baselineTextReferenceAvailable: true,
        canonicalCheckpointAvailable: true,
        currentAuthorityRequired: true,
        provenanceComplete: receipt.missingRetrievalReceiptIds.length === 0
          && receipt.missingFidelityComparisonIds.length === 0,
        relatedIds: [receipt.receiptId, receipt.manifestId],
        details: {
          missing_outcome_ids: receipt.missingOutcomeIds,
          missing_procedure_ids: receipt.missingProcedureIds,
          missing_projection_ids: receipt.missingProjectionIds,
          missing_signal_ids: receipt.missingSignalIds,
          findings: receipt.findings,
        },
        causationId: receipt.receiptId,
      });
    }
    this.revision += 1;
    return receipt;
  }

  exportBrowserContext(input: {
    preparationId: string;
    projection: CompactRestoreProjection;
    summary: string;
    allowedTools: string[];
    deniedTools: string[];
    providerId: string;
    modelId: string;
    metadata?: JsonObject;
  }): BrowserSkillMemoryContextProjection {
    this.assertEnabled();
    const preparation = this.preparations.get(input.preparationId.trim());
    if (!preparation) throw contractError("skill_memory_browser_preparation_missing", `integration preparation not found: ${input.preparationId}`);
    if (input.projection.boundaryId !== preparation.boundaryId) {
      throw contractError("skill_memory_browser_boundary", "browser context export boundary mismatch");
    }
    if (input.projection.state !== "applied") {
      throw contractError("skill_memory_browser_projection_state", "browser context export requires an applied compact projection");
    }
    const createdAt = nowIso(this.now);
    const unsigned = {
      projectionId: stableId(
        "browser-skill-memory-context",
        preparation.preparationId,
        input.projection.projectionId,
        input.projection.contextEpochAfter,
      ),
      protocol: "zyra.browser-skill-memory-context/v1" as const,
      identity: cloneJson(preparation.identity),
      boundaryId: preparation.boundaryId,
      compactProjectionId: input.projection.projectionId,
      contextEpoch: input.projection.contextEpochAfter,
      preparationId: preparation.preparationId,
      retrievalReceiptId: preparation.retrievalReceiptId,
      fidelityComparisonId: preparation.fidelityComparisonId,
      selectedStrategy: preparation.selectedStrategy,
      summary: input.summary.trim(),
      memoryIds: cloneJson(preparation.memoryIds),
      procedureIds: cloneJson(preparation.procedureIds),
      evidenceIds: cloneJson(preparation.evidenceIds),
      artifactIds: cloneJson(preparation.artifactIds),
      authoritySkillIds: cloneJson(preparation.authoritySkillIds),
      allowedTools: uniqueStrings(input.allowedTools),
      deniedTools: uniqueStrings(input.deniedTools),
      providerId: input.providerId.trim(),
      modelId: input.modelId.trim(),
      providerMessage: cloneJson(preparation.providerMessage),
      attachments: cloneJson(preparation.attachments),
      currentAuthorityRequired: true,
      executableSkillBodyPresent: false,
      createdAt,
      metadata: {
        ...input.metadata,
        source_worker_request_id: preparation.identity.workerRequestId,
        source_epoch: preparation.identity.epoch,
        source_worker_kind: preparation.workerKind,
        target_worker_kind: "browser",
        baseline_text_ref_available: true,
        canonical_context_owner: "BrowserContextTaskIntegrationRuntime/02B",
        canonical_skill_owner: "03C SkillCoordinator",
      },
    } satisfies Omit<BrowserSkillMemoryContextProjection, "projectionDigest">;
    const projection: BrowserSkillMemoryContextProjection = {
      ...unsigned,
      projectionDigest: digest(unsigned),
    };
    const existing = this.browserExports.get(projection.projectionId);
    if (existing && existing.projectionDigest !== projection.projectionDigest) {
      throw contractError("skill_memory_browser_projection_conflict", "browser skill memory projection id conflict");
    }
    this.browserExports.set(projection.projectionId, projection);
    this.revision += existing ? 0 : 1;
    return cloneJson(existing ?? projection);
  }

  preparation(preparationId: string): SkillMemoryIntegratedPreparation | null {
    const value = this.preparations.get(preparationId.trim());
    return value ? cloneJson(value) : null;
  }

  latestBrowserContext(): BrowserSkillMemoryContextProjection | null {
    const values = [...this.browserExports.values()].sort((left, right) => right.createdAt.localeCompare(left.createdAt));
    return values[0] ? cloneJson(values[0]) : null;
  }

  listPreparations(limit = 1_000): SkillMemoryIntegratedPreparation[] {
    return [...this.preparations.values()]
      .sort((left, right) => right.createdAt.localeCompare(left.createdAt) || left.preparationId.localeCompare(right.preparationId))
      .slice(0, Math.max(0, Math.min(limit, 100_000)))
      .map(cloneJson);
  }

  listApplications(limit = 1_000): SkillMemoryIntegratedApplication[] {
    return [...this.applications.values()]
      .sort((left, right) => right.appliedAt.localeCompare(left.appliedAt) || left.applicationId.localeCompare(right.applicationId))
      .slice(0, Math.max(0, Math.min(limit, 100_000)))
      .map(cloneJson);
  }

  health(): SkillMemoryIntegrationHealth {
    return {
      protocol: SKILL_MEMORY_INTEGRATION_PROTOCOL,
      revision: this.revision,
      preparationCount: this.preparations.size,
      applicationCount: this.applications.size,
      browserExportCount: this.browserExports.size,
      authority: this.authority.health(),
      fidelity: this.fidelity.health(),
      retrieval: this.retrieval.health(),
      resume: this.resume.health(),
      consumers: this.consumers.health(),
      failures: this.failures.health(),
      canonicalSkillOwner: "03C SkillCoordinator",
      canonicalCompactOwner: "02D ContextCompactionRuntime",
      canonicalContextOwner: "02B ContextAssemblyRuntime",
      canonicalMemoryOwner: "SQLiteStore.memory_records",
      canonicalProcedureOwner: "ReusableProcedureStore",
    };
  }

  snapshot(): SkillMemoryIntegrationSnapshot {
    const unsigned: Omit<SkillMemoryIntegrationSnapshot, "checksum"> = {
      version: "zyra.skill-memory-integration-runtime/v1",
      identity: cloneJson(this.identity),
      revision: this.revision,
      authority: this.authority.snapshot(),
      fidelity: this.fidelity.snapshot(),
      retrieval: this.retrieval.snapshot(),
      resume: this.resume.snapshot(),
      consumers: this.consumers.snapshot(),
      failures: this.failures.snapshot(),
      preparations: this.listPreparations(100_000).sort((left, right) => left.preparationId.localeCompare(right.preparationId)),
      applications: this.listApplications(100_000).sort((left, right) => left.applicationId.localeCompare(right.applicationId)),
      browserExports: [...this.browserExports.values()].sort((left, right) => left.projectionId.localeCompare(right.projectionId)).map(cloneJson),
      capturedAt: nowIso(this.now),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshotValue: SkillMemoryIntegrationSnapshot): void {
    this.validateSnapshot(snapshotValue);
    this.authority.restore(snapshotValue.authority);
    this.fidelity.restore(snapshotValue.fidelity);
    this.retrieval.restore(snapshotValue.retrieval);
    this.resume.restore(snapshotValue.resume);
    this.consumers.restore(snapshotValue.consumers);
    this.failures.restore(snapshotValue.failures);
    this.preparations.clear();
    this.applications.clear();
    this.browserExports.clear();
    for (const preparation of snapshotValue.preparations) this.restorePreparation(preparation);
    for (const application of snapshotValue.applications) this.restoreApplication(application);
    for (const projection of snapshotValue.browserExports) this.restoreBrowserExport(projection);
    this.revision = snapshotValue.revision;
  }

  private retrievalInput(input: Required<SkillMemoryIntegratedPreparationInput>): RetrievalCompositionInput {
    const memoryRetrieval = objectValue(input.runtimeConstraints.memory_retrieval);
    const codeReferences = codeReferencesFromRuntimeConstraints(input.runtimeConstraints);
    return {
      identity: input.identity,
      boundaryId: input.boundaryId,
      goal: input.goal,
      requirementChanges: input.requirementChanges,
      memoryQueryId: stringValue(memoryRetrieval.query_id) || null,
      memoryIndexGeneration: nullableInteger(memoryRetrieval.index_revision),
      memoryHints: memoryHintsFromJson(input.runtimeConstraints.memory_hints),
      codeQueryId: stringValue(input.runtimeConstraints.code_index_query_id) || null,
      codeIndexGeneration: nullableInteger(input.runtimeConstraints.code_index_generation),
      codeReferences,
      skillMemories: input.skillMemories,
      procedures: dedupeByProcedureId([
        ...input.procedures,
        ...proceduresFromJson(input.runtimeConstraints.reusable_procedure_context),
      ]),
      compactArtifactIds: [input.archive.artifactId],
      allowedTools: input.allowedTools,
      deniedTools: input.deniedTools,
      providerCapabilities: input.providerCapabilities,
      maximumTokens: Math.max(256, Math.floor(input.maximumTokens * 0.55)),
      maximumItems: 64,
      metadata: {
        worker_kind: input.workerKind,
        provider_id: input.providerId,
        model_id: input.modelId,
      },
    };
  }

  private fidelityScenario(input: Required<SkillMemoryIntegratedPreparationInput>): RestoreHistoryScenario {
    const facts: RestoreSemanticFactSet = {
      goal: input.goal,
      constraints: input.constraints,
      requirementChanges: input.requirementChanges,
      evidenceReferences: input.skillMemories.flatMap((item) => item.evidence),
      skillVersions: dedupeByDigest(input.skillMemories.map((item) => item.version)),
      policyReferences: dedupeByDigest(input.skillMemories.map((item) => item.policy)),
      artifactIds: uniqueStrings([
        input.archive.artifactId,
        ...input.skillMemories.flatMap((item) => item.artifactIds),
        ...input.procedures.flatMap((item) => item.provenance.artifactIds),
      ]),
      toolAtomicGroupIds: uniqueStrings(input.history.map((item) => item.toolPairId).filter(Boolean) as string[]),
      cjkAndSymbols: uniqueStrings([
        ...input.constraints.filter(containsCjkOrSymbols),
        ...input.requirementChanges.filter(containsCjkOrSymbols),
        ...input.history.map((item) => item.text).filter(containsCjkOrSymbols).slice(-16),
      ]),
      metadata: {
        worker_kind: input.workerKind,
        archive_id: input.archive.archiveId,
      },
    };
    return {
      identity: input.identity,
      boundaryId: input.boundaryId,
      history: input.history,
      facts,
      contextWindow: input.contextWindow,
      restoreBudgetTokens: input.maximumTokens,
      providerId: input.providerId,
      modelId: input.modelId,
      providerKind: input.providerKind,
      providerCapabilities: input.providerCapabilities,
      gatewayCapabilities: input.gatewayCapabilities,
      frames: input.frames,
      sourceProviderId: input.sourceProviderId,
      sourceModelId: input.sourceModelId,
      metadata: input.metadata,
    };
  }

  private validateSnapshot(snapshot: SkillMemoryIntegrationSnapshot): void {
    if (snapshot.version !== "zyra.skill-memory-integration-runtime/v1") {
      throw contractError("skill_memory_integration_snapshot_version", "unsupported skill memory integration snapshot version");
    }
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) {
      throw contractError("skill_memory_integration_snapshot_checksum", "skill memory integration snapshot checksum mismatch");
    }
    if (snapshot.identity.taskId !== this.identity.taskId || snapshot.identity.sessionId !== this.identity.sessionId) {
      throw contractError("skill_memory_integration_snapshot_binding", "skill memory integration snapshot belongs to another task/session");
    }
  }

  private restorePreparation(value: SkillMemoryIntegratedPreparation): void {
    const { preparationDigest, ...unsigned } = value;
    if (digest(unsigned) !== preparationDigest) {
      throw contractError("skill_memory_integration_preparation_digest", `integration preparation digest mismatch: ${value.preparationId}`);
    }
    if (!value.baselineTextRefAvailable || !value.currentAuthorityRequired) {
      throw contractError("skill_memory_integration_preparation_invariant", `integration preparation invariants failed: ${value.preparationId}`);
    }
    this.preparations.set(value.preparationId, cloneJson(value));
  }

  private restoreApplication(value: SkillMemoryIntegratedApplication): void {
    const { applicationDigest, ...unsigned } = value;
    if (digest(unsigned) !== applicationDigest) {
      throw contractError("skill_memory_integration_application_digest", `integration application digest mismatch: ${value.applicationId}`);
    }
    if (!this.preparations.has(value.preparationId)) {
      throw contractError("skill_memory_integration_application_preparation", `integration application references missing preparation: ${value.preparationId}`);
    }
    this.applications.set(value.applicationId, cloneJson(value));
  }

  private restoreBrowserExport(value: BrowserSkillMemoryContextProjection): void {
    const { projectionDigest, ...unsigned } = value;
    if (digest(unsigned) !== projectionDigest) {
      throw contractError("skill_memory_browser_projection_digest", `browser skill memory projection digest mismatch: ${value.projectionId}`);
    }
    if (value.executableSkillBodyPresent || !value.currentAuthorityRequired) {
      throw contractError("skill_memory_browser_projection_authority", `browser skill memory projection contains invalid authority: ${value.projectionId}`);
    }
    this.browserExports.set(value.projectionId, cloneJson(value));
  }

  private assertEnabled(): void {
    if (process.env.ZYRA_DISABLE_SKILL_MEMORY_INTEGRATION === "1") {
      throw contractError("skill_memory_integration_runtime_disabled", "06C skill memory integration runtime is disabled");
    }
  }
}

function normalizePreparationInput(
  value: SkillMemoryIntegratedPreparationInput,
  expected: RuntimeIdentity,
): Required<SkillMemoryIntegratedPreparationInput> {
  if (value.identity.taskId !== expected.taskId || value.identity.sessionId !== expected.sessionId) {
    throw contractError("skill_memory_integration_identity", "skill memory integration input belongs to another task/session");
  }
  if (!value.boundaryId?.trim() || value.archive.boundaryId !== value.boundaryId) {
    throw contractError("skill_memory_integration_boundary", "skill memory integration boundary is invalid");
  }
  if (!value.goal?.trim()) throw contractError("skill_memory_integration_goal", "skill memory integration goal is required");
  if (!value.providerId?.trim() || !value.modelId?.trim()) {
    throw contractError("skill_memory_integration_provider", "skill memory integration provider and model are required");
  }
  if (!Number.isSafeInteger(value.contextWindow) || value.contextWindow < 1) {
    throw contractError("skill_memory_integration_context_window", "skill memory integration context window must be positive");
  }
  if (!Number.isSafeInteger(value.maximumTokens) || value.maximumTokens < 512) {
    throw contractError("skill_memory_integration_budget", "skill memory integration budget must be at least 512 tokens");
  }
  return {
    identity: cloneJson(value.identity),
    workerKind: value.workerKind,
    boundaryId: value.boundaryId.trim(),
    archive: cloneJson(value.archive),
    history: value.history.map(cloneJson),
    goal: value.goal.trim(),
    constraints: uniqueStrings(value.constraints),
    requirementChanges: uniqueStrings(value.requirementChanges),
    skillMemories: value.skillMemories.filter((item) => item.state === "accepted").map(cloneJson),
    procedures: value.procedures.filter((item) => item.state === "validated").map(cloneJson),
    existingAttachments: value.existingAttachments.filter((item) => item.selected).map(cloneJson),
    runtimeConstraints: cloneJson(value.runtimeConstraints),
    allowedTools: uniqueStrings(value.allowedTools),
    deniedTools: uniqueStrings(value.deniedTools),
    providerId: value.providerId.trim(),
    modelId: value.modelId.trim(),
    providerKind: value.providerKind,
    providerCapabilities: uniqueStrings(value.providerCapabilities),
    gatewayCapabilities: uniqueStrings(value.gatewayCapabilities),
    frames: value.frames.map(cloneJson),
    sourceProviderId: value.sourceProviderId?.trim() || null,
    sourceModelId: value.sourceModelId?.trim() || null,
    contextWindow: value.contextWindow,
    maximumTokens: value.maximumTokens,
    metadata: cloneJson(value.metadata ?? {}),
  };
}

function mergeAttachments(...groups: RestoreAttachmentReference[][]): RestoreAttachmentReference[] {
  const output = new Map<string, RestoreAttachmentReference>();
  for (const attachment of groups.flat()) {
    const existing = output.get(attachment.candidateId);
    if (existing && (existing.sourceDigest !== attachment.sourceDigest || existing.content !== attachment.content)) {
      throw contractError("skill_memory_integration_attachment_conflict", `restore attachment conflicts: ${attachment.candidateId}`);
    }
    output.set(attachment.candidateId, cloneJson(existing ?? attachment));
  }
  return [...output.values()].sort((left, right) => Number(right.required) - Number(left.required) || left.candidateId.localeCompare(right.candidateId));
}

function envelopeAttachments(envelope: ProviderRestoreEnvelope): RestoreAttachmentReference[] {
  return envelope.parts
    .filter((part) => part.text)
    .map((part) => ({
      candidateId: `fidelity:${part.partId}`,
      kind: part.kind === "artifact_ref" ? "artifact" as const : "memory" as const,
      name: `Restore fidelity ${part.kind}`,
      sourceId: envelope.envelopeId,
      sourceDigest: digest(part),
      content: part.text!,
      tokenEstimate: part.tokenEstimate,
      selected: true,
      required: part.required,
      metadata: {
        restore_strategy: envelope.strategy,
        provider_id: envelope.providerId,
        model_id: envelope.modelId,
        fidelity_part_id: part.partId,
        fallback_strategy: envelope.fallbackStrategy,
      },
    }));
}

function codeReferencesFromRuntimeConstraints(value: JsonObject): RetrievalCodeReference[] {
  const direct = codeReferencesFromJson(value.code_index_references);
  if (direct.length) return direct;
  const files = stringArray(value.code_index_selected_files);
  const tests = stringArray(value.code_index_selected_tests);
  const generation = nullableInteger(value.code_index_generation) ?? 0;
  return files.map((path, index) => ({
    referenceId: stableId("code-reference", value.code_index_query_id, path, generation),
    logicalPath: path,
    lineStart: 0,
    lineEnd: 0,
    sourceDigest: digest({ path, generation, query_id: value.code_index_query_id }),
    workspaceRevision: stringValue(value.workspace_revision) || `generation-${generation}`,
    indexGeneration: generation,
    excerpt: `Current code-index selection references ${path}; hydrate through the 06A code index before use.`,
    selectedTests: index === 0 ? tests : [],
    score: Math.max(0, 1 - index / Math.max(files.length, 1)),
    metadata: {
      reference_only: true,
      workspace_content_embedded: false,
      code_query_id: value.code_index_query_id ?? null,
    },
  }));
}

function containsCjkOrSymbols(value: string): boolean {
  return /[\u3000-\u30ff\u3400-\u9fff\uf900-\ufaff]|[^\p{L}\p{N}\p{Z}\p{P}]/u.test(value);
}

function dedupeByDigest<T>(values: T[]): T[] {
  const output = new Map<string, T>();
  for (const value of values) output.set(digest(value), cloneJson(value));
  return [...output.values()];
}

function dedupeByProcedureId(values: ReusableProcedure[]): ReusableProcedure[] {
  const output = new Map<string, ReusableProcedure>();
  for (const value of values) {
    const existing = output.get(value.procedureId);
    if (!existing || value.revision > existing.revision) {
      output.set(value.procedureId, cloneJson(value));
    }
  }
  return [...output.values()];
}

function objectValue(value: JsonValue | undefined): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? cloneJson(value) as JsonObject : {};
}

function stringValue(value: JsonValue | undefined): string {
  return typeof value === "string" ? value.trim() : "";
}

function nullableInteger(value: JsonValue | undefined): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function stringArray(value: JsonValue | undefined): string[] {
  return Array.isArray(value) ? uniqueStrings(value) : [];
}
