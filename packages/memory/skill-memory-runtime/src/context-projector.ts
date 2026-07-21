import {
  COMPACT_PROJECTION_PROTOCOL,
  cloneJson,
  contractError,
  digest,
  estimateTokens,
  intersectToolScopes,
  nowIso,
  stableId,
  toolScopeIsSubset,
  truncateToTokens,
  uniqueStrings,
  type CompactArchiveReference,
  type CompactPolicy,
  type CompactRestoreProjection,
  type ContextWorkerKind,
  type JsonObject,
  type JsonValue,
  type RestoreAttachmentReference,
  type ReusableProcedure,
  type RuntimeIdentity,
  type SkillInvocationOutcomeMemory,
} from "./contracts.ts";
import type {
  CompactRestorePort,
  ContextAssemblyPort,
  ContextAssemblySectionInput,
} from "./ports.ts";

export interface SkillContextProjectionInput {
  identity: RuntimeIdentity;
  workerKind: ContextWorkerKind;
  boundaryId: string;
  archive: CompactArchiveReference;
  summary: string;
  contextEpochBefore: number;
  skillMemories: SkillInvocationOutcomeMemory[];
  procedures: ReusableProcedure[];
  restoredAttachments?: RestoreAttachmentReference[];
  parentAllowedTools: string[];
  restoredAllowedTools: string[];
  deniedTools: string[];
  maximumTokens?: number;
  expiresAt?: string | null;
  metadata?: JsonObject;
}

export interface SkillContextProjectionResult {
  projection: CompactRestoreProjection;
  providerMessage: JsonObject;
  contextDigest: string;
  selectedMemoryIds: string[];
  selectedProcedureIds: string[];
  droppedMemoryIds: string[];
  droppedProcedureIds: string[];
  totalTokens: number;
}

export class SkillContextProjector {
  private readonly context: ContextAssemblyPort;
  private readonly restore: CompactRestorePort;
  private readonly policy: CompactPolicy;
  private readonly now: () => Date;

  constructor(options: {
    context: ContextAssemblyPort;
    restore: CompactRestorePort;
    policy: CompactPolicy;
    now?: () => Date;
  }) {
    this.context = options.context;
    this.restore = options.restore;
    this.policy = cloneJson(options.policy);
    this.now = options.now ?? (() => new Date());
  }

  project(inputValue: SkillContextProjectionInput): SkillContextProjectionResult {
    this.assertEnabled();
    const input = normalizeProjectionInput(inputValue, this.policy);
    const allowedToolsAfter = intersectToolScopes(
      input.parentAllowedTools,
      input.restoredAllowedTools.length > 0 ? input.restoredAllowedTools : input.parentAllowedTools,
      input.deniedTools,
    );
    if (!toolScopeIsSubset(input.parentAllowedTools, allowedToolsAfter, input.deniedTools)) {
      throw contractError("compact_restore_tool_scope_widened", "restore projection attempted to widen the parent tool scope", {
        parent_allowed_tools: input.parentAllowedTools,
        restored_allowed_tools: input.restoredAllowedTools,
        denied_tools: input.deniedTools,
        effective_allowed_tools: allowedToolsAfter,
      });
    }
    const maximumTokens = input.maximumTokens ?? this.policy.maximumRestoreTokens;
    const summaryBudget = Math.max(256, Math.floor(maximumTokens * 0.45));
    const memoryBudget = Math.min(this.policy.maximumSkillMemoryTokens, Math.max(128, Math.floor(maximumTokens * 0.25)));
    const procedureBudget = Math.min(this.policy.maximumProcedureTokens, Math.max(128, Math.floor(maximumTokens * 0.2)));
    const attachmentBudget = Math.max(128, maximumTokens - summaryBudget - memoryBudget - procedureBudget);
    const summary = truncateToTokens(input.summary, summaryBudget);
    const memorySelection = selectMemories(input.skillMemories, memoryBudget);
    const procedureSelection = selectProcedures(input.procedures, procedureBudget);
    const attachmentSelection = selectAttachments(input.restoredAttachments, attachmentBudget);
    const sectionIds: string[] = [];
    const createdAt = nowIso(this.now);
    const expiresAt = input.expiresAt ?? new Date(Date.parse(createdAt) + this.policy.projectionTtlMilliseconds).toISOString();
    sectionIds.push(this.addSection({
      sectionId: stableId("restore-summary-section", input.identity.sessionId, input.boundaryId),
      kind: "memory",
      title: `Restored compact context ${input.boundaryId}`,
      content: {
        boundary_id: input.boundaryId,
        archive_id: input.archive.archiveId,
        summary: summary.text,
        compact_generation: input.archive.compactGeneration,
        summarized_message_ids: input.archive.summarizedMessageIds,
        preserved_message_ids: input.archive.preservedMessageIds,
      },
      text: summary.text,
      priority: 900,
      pinned: false,
      required: true,
      disclosure: "model_only",
      toolPairId: null,
      turnIndex: null,
      provenance: {
        source: "restore",
        sourceId: input.boundaryId,
        sourceDigest: input.archive.summaryDigest,
        parentSectionId: null,
        trust: "verified",
        createdAt,
        expiresAt,
      },
      metadata: {
        compact_archive_id: input.archive.archiveId,
        compact_artifact_id: input.archive.artifactId,
        context_epoch_before: input.contextEpochBefore,
        summary_truncated: summary.truncated,
        canonical_compact_owner: "02D ContextCompactionRuntime",
      },
    }));
    for (const memory of memorySelection.selected) {
      const text = formatSkillMemory(memory);
      sectionIds.push(this.addSection({
        sectionId: stableId("skill-memory-section", input.boundaryId, memory.memoryId),
        kind: "memory",
        title: `Skill outcome: ${memory.version.skillName}`,
        content: {
          memory_id: memory.memoryId,
          skill_id: memory.version.skillId,
          skill_name: memory.version.skillName,
          descriptor_digest: memory.version.descriptorDigest,
          body_digest: memory.version.bodyDigest,
          registry_revision: memory.version.registryRevision,
          status: memory.status,
          summary: memory.summary,
          success_reason: memory.successReason,
          artifact_ids: memory.artifactIds,
          evidence_ids: memory.evidence.map((item) => item.evidenceId),
          reuse_conditions: cloneJson(memory.reuseConditions) as unknown as JsonValue,
        },
        text,
        priority: 700 + Math.min(100, memory.metrics.toolCalls),
        pinned: false,
        required: false,
        disclosure: "model_only",
        toolPairId: null,
        turnIndex: null,
        provenance: {
          source: "skill",
          sourceId: memory.memoryId,
          sourceDigest: memory.recordDigest,
          parentSectionId: sectionIds[0],
          trust: memory.evidence.some((item) => item.trustedRuntime) ? "verified" : "internal",
          createdAt: memory.observedAt,
          expiresAt,
        },
        metadata: {
          invocation_id: memory.invocationId,
          tool_call_id: memory.toolCallId,
          policy_decision_id: memory.policy.decisionId,
          source_record_digest: memory.sourceRecordDigest,
          outcome_only: true,
        },
      }));
    }
    for (const procedure of procedureSelection.selected) {
      const text = formatProcedure(procedure);
      sectionIds.push(this.addSection({
        sectionId: stableId("procedure-section", input.boundaryId, procedure.procedureId),
        kind: "instruction",
        title: `Validated reusable procedure: ${procedure.name}`,
        content: {
          procedure_id: procedure.procedureId,
          summary: procedure.summary,
          steps: cloneJson(procedure.steps) as unknown as JsonValue,
          applicability: cloneJson(procedure.applicability) as unknown as JsonValue,
          confidence: procedure.confidence,
          validation_status: procedure.state,
          provenance: cloneJson(procedure.provenance) as unknown as JsonValue,
        },
        text,
        priority: 650 + Math.round(procedure.confidence * 100),
        pinned: false,
        required: false,
        disclosure: "model_only",
        toolPairId: null,
        turnIndex: null,
        provenance: {
          source: "memory",
          sourceId: procedure.procedureId,
          sourceDigest: procedure.procedureDigest,
          parentSectionId: sectionIds[0],
          trust: procedure.state === "validated" ? "verified" : "internal",
          createdAt: procedure.createdAt,
          expiresAt,
        },
        metadata: {
          curator_outcome_id: procedure.provenance.curatorOutcomeId,
          evidence_bundle_id: procedure.provenance.evidenceBundleId,
          consumer_routing: procedure.consumers.includes("routing"),
          consumer_recovery: procedure.consumers.includes("recovery"),
          active_without_validation: false,
        },
      }));
    }
    for (const attachment of attachmentSelection.selected) {
      const restored = this.restore.discover({
        candidateId: attachment.candidateId,
        kind: attachment.kind === "artifact" || attachment.kind === "other" ? "file" : attachment.kind,
        name: attachment.name,
        sourceId: attachment.sourceId,
        priority: attachment.required ? 1_000 : 500,
        required: attachment.required,
        content: attachment.content,
        metadata: {
          ...attachment.metadata,
          source_digest: attachment.sourceDigest,
          compact_boundary_id: input.boundaryId,
        },
      });
      if (restored.state === "denied" || restored.state === "failed") continue;
      const text = truncateToTokens(attachment.content, attachment.tokenEstimate || attachmentBudget).text;
      sectionIds.push(this.addSection({
        sectionId: stableId("restore-attachment-section", input.boundaryId, attachment.candidateId),
        kind: "attachment",
        title: `Restored ${attachment.kind}: ${attachment.name}`,
        content: {
          candidate_id: attachment.candidateId,
          source_id: attachment.sourceId,
          source_digest: attachment.sourceDigest,
          kind: attachment.kind,
          name: attachment.name,
          content: text,
        },
        text,
        priority: attachment.required ? 850 : 450,
        pinned: false,
        required: attachment.required,
        disclosure: "model_only",
        toolPairId: null,
        turnIndex: null,
        provenance: {
          source: "restore",
          sourceId: attachment.sourceId,
          sourceDigest: attachment.sourceDigest,
          parentSectionId: sectionIds[0],
          trust: "internal",
          createdAt,
          expiresAt,
        },
        metadata: {
          candidate_id: attachment.candidateId,
          restore_state: restored.state,
          required: attachment.required,
        },
      }));
    }
    const assembled = this.context.assemble(maximumTokens);
    if (!assembled.pairInvariantOk) throw contractError("compact_restore_context_pair_invariant", "02B context assembly rejected the restored tool-pair invariant");
    const providerMessage = providerMessageFor({
      boundaryId: input.boundaryId,
      contextEpoch: input.contextEpochBefore + 1,
      workerKind: input.workerKind,
      summary: summary.text,
      memories: memorySelection.selected,
      procedures: procedureSelection.selected,
      attachments: attachmentSelection.selected,
      allowedTools: allowedToolsAfter,
      contextDigest: assembled.contextDigest,
      sectionIds,
    });
    const projectionWithoutDigest = {
      projectionId: stableId("compact-restore-projection", input.identity.sessionId, input.boundaryId, input.contextEpochBefore + 1, input.workerKind),
      protocol: COMPACT_PROJECTION_PROTOCOL,
      identity: cloneJson(input.identity),
      workerKind: input.workerKind,
      state: "prepared" as const,
      boundaryId: input.boundaryId,
      archive: cloneJson(input.archive),
      contextEpochBefore: input.contextEpochBefore,
      contextEpochAfter: input.contextEpochBefore + 1,
      summary: summary.text,
      attachments: attachmentSelection.selected.map(cloneJson),
      skillMemoryIds: memorySelection.selected.map((item) => item.memoryId),
      procedureIds: procedureSelection.selected.map((item) => item.procedureId),
      allowedToolsBefore: input.parentAllowedTools,
      allowedToolsAfter,
      deniedTools: input.deniedTools,
      contextSectionIds: sectionIds,
      providerMessageDigest: digest(providerMessage),
      preparedAt: createdAt,
      appliedAt: null,
      expiresAt,
      rejectionReason: null,
      metadata: {
        ...input.metadata,
        context_assembly_id: assembled.assemblyId,
        context_digest: assembled.contextDigest,
        selected_tokens: assembled.selectedTokens,
        dropped_memory_ids: memorySelection.dropped.map((item) => item.memoryId),
        dropped_procedure_ids: procedureSelection.dropped.map((item) => item.procedureId),
        restore_candidate_count: attachmentSelection.selected.length,
        restored_tool_scope_is_subset: true,
        bridge_bypassed_02b: false,
        bridge_bypassed_02d: false,
      },
    } satisfies Omit<CompactRestoreProjection, "projectionDigest">;
    const projection: CompactRestoreProjection = {
      ...projectionWithoutDigest,
      projectionDigest: digest(projectionWithoutDigest),
    };
    return {
      projection,
      providerMessage,
      contextDigest: assembled.contextDigest,
      selectedMemoryIds: projection.skillMemoryIds,
      selectedProcedureIds: projection.procedureIds,
      droppedMemoryIds: memorySelection.dropped.map((item) => item.memoryId),
      droppedProcedureIds: procedureSelection.dropped.map((item) => item.procedureId),
      totalTokens: assembled.selectedTokens,
    };
  }

  private addSection(input: ContextAssemblySectionInput): string {
    const result = this.context.add(input);
    return result.sectionId;
  }

  private assertEnabled(): void {
    if (process.env.ZYRA_DISABLE_SKILL_CONTEXT_PROJECTOR === "1") {
      throw contractError("skill_context_projector_disabled", "06C skill context projector is disabled");
    }
  }
}

function normalizeProjectionInput(value: SkillContextProjectionInput, policy: CompactPolicy): Required<SkillContextProjectionInput> {
  if (!value.boundaryId?.trim()) throw contractError("compact_restore_boundary_required", "compact restore boundary id is required");
  if (value.archive.boundaryId !== value.boundaryId) throw contractError("compact_restore_archive_boundary", "compact archive does not match projection boundary");
  if (value.archive.sessionId !== value.identity.sessionId) throw contractError("compact_restore_archive_session", "compact archive belongs to another session");
  if (value.contextEpochBefore < 0 || !Number.isSafeInteger(value.contextEpochBefore)) throw contractError("compact_restore_context_epoch", "context epoch must be a non-negative integer");
  if (value.workerKind !== "code" && value.workerKind !== "browser") throw contractError("compact_restore_worker_kind", `unsupported worker kind ${value.workerKind}`);
  return {
    identity: cloneJson(value.identity),
    workerKind: value.workerKind,
    boundaryId: value.boundaryId.trim(),
    archive: cloneJson(value.archive),
    summary: value.summary.trim() || "Prior context was compacted; use the attached evidence and preserved tail.",
    contextEpochBefore: value.contextEpochBefore,
    skillMemories: value.skillMemories.filter((item) => item.state === "accepted" && item.status === "completed").map(cloneJson),
    procedures: value.procedures.filter((item) => item.state === "validated").map(cloneJson),
    restoredAttachments: (value.restoredAttachments ?? []).filter((item) => item.selected).map(cloneJson),
    parentAllowedTools: uniqueStrings(value.parentAllowedTools),
    restoredAllowedTools: uniqueStrings(value.restoredAllowedTools),
    deniedTools: uniqueStrings(value.deniedTools),
    maximumTokens: Math.max(512, Math.min(value.maximumTokens ?? policy.maximumRestoreTokens, policy.maximumRestoreTokens)),
    expiresAt: value.expiresAt ?? null,
    metadata: cloneJson(value.metadata ?? {}),
  };
}

function selectMemories(values: SkillInvocationOutcomeMemory[], budget: number): { selected: SkillInvocationOutcomeMemory[]; dropped: SkillInvocationOutcomeMemory[] } {
  const ordered = [...values].sort((left, right) => {
    const status = Number(right.status === "completed") - Number(left.status === "completed");
    if (status !== 0) return status;
    const tools = right.metrics.toolCalls - left.metrics.toolCalls;
    if (tools !== 0) return tools;
    return (right.completedAt ?? right.observedAt).localeCompare(left.completedAt ?? left.observedAt);
  });
  const selected: SkillInvocationOutcomeMemory[] = [];
  const dropped: SkillInvocationOutcomeMemory[] = [];
  let tokens = 0;
  for (const value of ordered) {
    const estimate = estimateTokens(formatSkillMemory(value));
    if (tokens + estimate > budget) dropped.push(value);
    else { selected.push(value); tokens += estimate; }
  }
  return { selected, dropped };
}

function selectProcedures(values: ReusableProcedure[], budget: number): { selected: ReusableProcedure[]; dropped: ReusableProcedure[] } {
  const ordered = [...values].sort((left, right) => {
    if (right.confidence !== left.confidence) return right.confidence - left.confidence;
    if (right.successCount !== left.successCount) return right.successCount - left.successCount;
    return right.updatedAt.localeCompare(left.updatedAt);
  });
  const selected: ReusableProcedure[] = [];
  const dropped: ReusableProcedure[] = [];
  let tokens = 0;
  for (const value of ordered) {
    const estimate = estimateTokens(formatProcedure(value));
    if (tokens + estimate > budget) dropped.push(value);
    else { selected.push(value); tokens += estimate; }
  }
  return { selected, dropped };
}

function selectAttachments(values: RestoreAttachmentReference[], budget: number): { selected: RestoreAttachmentReference[]; dropped: RestoreAttachmentReference[] } {
  const ordered = [...values].sort((left, right) => Number(right.required) - Number(left.required) || left.candidateId.localeCompare(right.candidateId));
  const selected: RestoreAttachmentReference[] = [];
  const dropped: RestoreAttachmentReference[] = [];
  let tokens = 0;
  for (const value of ordered) {
    const estimate = value.tokenEstimate || estimateTokens(value.content);
    if (!value.required && tokens + estimate > budget) dropped.push(value);
    else { selected.push(value); tokens += Math.min(estimate, budget); }
  }
  return { selected, dropped };
}

function formatSkillMemory(memory: SkillInvocationOutcomeMemory): string {
  const evidence = memory.evidence.map((item) => `${item.kind}:${item.sourceId}`).join(", ");
  const artifacts = memory.artifactIds.length > 0 ? memory.artifactIds.join(", ") : "none";
  return [
    `Skill ${memory.version.skillName} (${memory.version.skillId}) completed under registry revision ${memory.version.registryRevision}.`,
    `Outcome: ${memory.summary}`,
    `Success reason: ${memory.successReason ?? "not supplied"}.`,
    `Artifacts: ${artifacts}.`,
    `Evidence: ${evidence || "no reusable evidence"}.`,
    `Re-resolve this skill through 03C before invoking; this outcome is evidence, not executable skill content.`,
  ].join("\n");
}

function formatProcedure(procedure: ReusableProcedure): string {
  const steps = procedure.steps
    .sort((left, right) => left.ordinal - right.ordinal)
    .map((step) => `${step.ordinal}. ${step.action}${step.toolName ? ` [tool: ${step.toolName}]` : ""} -> ${step.expectedEffect}`)
    .join("\n");
  return [
    `${procedure.name}: ${procedure.summary}`,
    `Validation: ${procedure.state}; confidence=${procedure.confidence.toFixed(3)}; successes=${procedure.successCount}.`,
    steps,
    `Applicability tools: ${procedure.applicability.requiredTools.join(", ") || "none"}.`,
    `Curator provenance: ${procedure.provenance.curatorOutcomeId}/${procedure.provenance.evidenceBundleId}.`,
  ].join("\n");
}

function providerMessageFor(input: {
  boundaryId: string;
  contextEpoch: number;
  workerKind: ContextWorkerKind;
  summary: string;
  memories: SkillInvocationOutcomeMemory[];
  procedures: ReusableProcedure[];
  attachments: RestoreAttachmentReference[];
  allowedTools: string[];
  contextDigest: string;
  sectionIds: string[];
}): JsonObject {
  return {
    role: "user",
    content: [{
      type: "text",
      text: [
        `[Zyra restored context epoch ${input.contextEpoch}; boundary ${input.boundaryId}]`,
        input.summary,
        ...input.memories.map(formatSkillMemory),
        ...input.procedures.map(formatProcedure),
        ...input.attachments.map((item) => `Restored ${item.kind} ${item.name}:\n${item.content}`),
        `Effective tools remain restricted to: ${input.allowedTools.join(", ") || "none"}.`,
      ].join("\n\n"),
    }],
    metadata: {
      zyra_restore_projection: true,
      compact_boundary_id: input.boundaryId,
      context_epoch: input.contextEpoch,
      worker_kind: input.workerKind,
      skill_memory_ids: input.memories.map((item) => item.memoryId),
      procedure_ids: input.procedures.map((item) => item.procedureId),
      attachment_candidate_ids: input.attachments.map((item) => item.candidateId),
      effective_allowed_tools: input.allowedTools,
      context_digest: input.contextDigest,
      context_section_ids: input.sectionIds,
      canonical_owner: "SkillContextProjector",
    },
  };
}
