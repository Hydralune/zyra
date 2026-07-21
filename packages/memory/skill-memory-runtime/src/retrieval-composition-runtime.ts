import {
  cloneJson,
  contractError,
  digest,
  estimateTokens,
  nowIso,
  stableId,
  truncateToTokens,
  uniqueStrings,
  type JsonObject,
  type JsonValue,
  type RestoreAttachmentReference,
  type ReusableProcedure,
  type RuntimeIdentity,
  type SkillInvocationOutcomeMemory,
} from "./contracts.ts";

export const RETRIEVAL_COMPOSITION_PROTOCOL = "zyra.skill-memory-retrieval-composition/v1" as const;

export type RetrievalCompositionSourceKind =
  | "canonical_memory"
  | "code_index"
  | "skill_outcome"
  | "reusable_procedure"
  | "compact_artifact"
  | "requirement_change";

export interface RetrievalMemoryHint {
  memoryId: string;
  layer: string;
  title: string;
  content: string;
  sourceType: string;
  sourceId: string;
  sourceRevision: string;
  artifactIds: string[];
  score: number;
  rank: number;
  indexGeneration: number;
  metadata: JsonObject;
}

export interface RetrievalCodeReference {
  referenceId: string;
  logicalPath: string;
  lineStart: number;
  lineEnd: number;
  sourceDigest: string;
  workspaceRevision: string;
  indexGeneration: number;
  excerpt: string;
  selectedTests: string[];
  score: number;
  metadata: JsonObject;
}

export interface RetrievalCompositionInput {
  identity: RuntimeIdentity;
  boundaryId: string;
  goal: string;
  requirementChanges: string[];
  memoryQueryId: string | null;
  memoryIndexGeneration: number | null;
  memoryHints: RetrievalMemoryHint[];
  codeQueryId: string | null;
  codeIndexGeneration: number | null;
  codeReferences: RetrievalCodeReference[];
  skillMemories: SkillInvocationOutcomeMemory[];
  procedures: ReusableProcedure[];
  compactArtifactIds: string[];
  allowedTools: string[];
  deniedTools: string[];
  providerCapabilities: string[];
  maximumTokens: number;
  maximumItems?: number;
  metadata?: JsonObject;
}

export interface RetrievalCompositionCandidate {
  candidateId: string;
  sourceKind: RetrievalCompositionSourceKind;
  sourceId: string;
  sourceRevision: string;
  title: string;
  content: string;
  contentDigest: string;
  artifactIds: string[];
  evidenceIds: string[];
  skillIds: string[];
  procedureIds: string[];
  requiredTools: string[];
  forbiddenTools: string[];
  providerCapabilities: string[];
  score: number;
  priority: number;
  tokenEstimate: number;
  required: boolean;
  applicable: boolean;
  rejectionReasons: string[];
  metadata: JsonObject;
}

export interface RetrievalCompositionSelection {
  selectionId: string;
  candidateId: string;
  ordinal: number;
  selected: boolean;
  tokenOffset: number | null;
  tokenLength: number;
  truncated: boolean;
  reason: string;
  selectionDigest: string;
}

export interface RetrievalCompositionReceipt {
  protocol: typeof RETRIEVAL_COMPOSITION_PROTOCOL;
  receiptId: string;
  identity: RuntimeIdentity;
  boundaryId: string;
  goal: string;
  memoryQueryId: string | null;
  memoryIndexGeneration: number | null;
  codeQueryId: string | null;
  codeIndexGeneration: number | null;
  candidates: RetrievalCompositionCandidate[];
  selections: RetrievalCompositionSelection[];
  selectedCandidateIds: string[];
  rejectedCandidateIds: string[];
  attachments: RestoreAttachmentReference[];
  selectedTokens: number;
  budgetTokens: number;
  budgetRemaining: number;
  selectedMemoryIds: string[];
  selectedSkillMemoryIds: string[];
  selectedProcedureIds: string[];
  selectedCodeReferenceIds: string[];
  selectedArtifactIds: string[];
  sourceProvenanceComplete: boolean;
  currentSkillResolutionRequired: true;
  createdAt: string;
  metadata: JsonObject;
  receiptDigest: string;
}

export interface RetrievalCompositionDelivery {
  deliveryId: string;
  receiptId: string;
  workerRequestId: string;
  workerKind: "code" | "browser";
  state: "prepared" | "committed" | "released";
  projectionId: string | null;
  providerRequestIds: string[];
  terminalEventIds: string[];
  preparedAt: string;
  settledAt: string | null;
  reason: string;
  metadata: JsonObject;
  deliveryDigest: string;
}

export interface RetrievalCompositionSnapshot {
  version: "zyra.skill-memory-retrieval-composition-runtime/v1";
  identity: RuntimeIdentity;
  revision: number;
  receipts: RetrievalCompositionReceipt[];
  deliveries: RetrievalCompositionDelivery[];
  capturedAt: string;
  checksum: string;
}

/**
 * Composes bounded projections from the existing 06A and 06B owners. It owns
 * neither canonical memory nor an index. Every selected body retains a query
 * or procedure/outcome source revision and is delivered as an ordinary 02B
 * attachment candidate.
 */
export class SkillMemoryRetrievalCompositionRuntime {
  readonly identity: RuntimeIdentity;
  private readonly receipts = new Map<string, RetrievalCompositionReceipt>();
  private readonly deliveries = new Map<string, RetrievalCompositionDelivery>();
  private readonly now: () => Date;
  private revision = 0;

  constructor(options: {
    identity: RuntimeIdentity;
    now?: () => Date;
    snapshot?: RetrievalCompositionSnapshot | null;
  }) {
    this.identity = cloneJson(options.identity);
    this.now = options.now ?? (() => new Date());
    if (options.snapshot) this.restore(options.snapshot);
  }

  compose(inputValue: RetrievalCompositionInput): RetrievalCompositionReceipt {
    this.assertEnabled();
    const input = normalizeInput(inputValue, this.identity);
    const candidates = this.candidates(input);
    const ordered = [...candidates].sort(compareCandidates);
    const selections: RetrievalCompositionSelection[] = [];
    const attachments: RestoreAttachmentReference[] = [];
    let remaining = input.maximumTokens;
    let ordinal = 0;
    for (const candidate of ordered) {
      ordinal += 1;
      let selected = false;
      let truncated = false;
      let reason = "not_selected";
      let content = candidate.content;
      let tokenLength = 0;
      const tokenOffset = candidate.applicable && remaining > 0 ? input.maximumTokens - remaining : null;
      if (!candidate.applicable) {
        reason = candidate.rejectionReasons.join(",") || "candidate_not_applicable";
      } else if (attachments.length >= input.maximumItems) {
        reason = "maximum_item_count_reached";
      } else if (candidate.tokenEstimate <= remaining) {
        selected = true;
        tokenLength = candidate.tokenEstimate;
        reason = "selected_within_budget";
      } else if (candidate.required && remaining > 0) {
        const shortened = truncateToTokens(candidate.content, remaining);
        if (shortened.text) {
          selected = true;
          truncated = shortened.truncated;
          content = shortened.text;
          tokenLength = shortened.tokens;
          reason = "required_candidate_truncated_to_budget";
        } else {
          reason = "required_candidate_could_not_fit";
        }
      } else {
        reason = "token_budget_exhausted";
      }
      const selectionUnsigned = {
        selectionId: stableId(
          "retrieval-composition-selection",
          input.boundaryId,
          candidate.candidateId,
          selected,
          tokenOffset,
          tokenLength,
        ),
        candidateId: candidate.candidateId,
        ordinal,
        selected,
        tokenOffset: selected ? tokenOffset : null,
        tokenLength,
        truncated,
        reason,
      } satisfies Omit<RetrievalCompositionSelection, "selectionDigest">;
      selections.push({ ...selectionUnsigned, selectionDigest: digest(selectionUnsigned) });
      if (!selected) continue;
      remaining -= tokenLength;
      attachments.push({
        candidateId: candidate.candidateId,
        kind: attachmentKind(candidate.sourceKind),
        name: candidate.title,
        sourceId: candidate.sourceId,
        sourceDigest: candidate.contentDigest,
        content,
        tokenEstimate: tokenLength,
        selected: true,
        required: candidate.required,
        metadata: {
          source_kind: candidate.sourceKind,
          source_revision: candidate.sourceRevision,
          artifact_ids: candidate.artifactIds,
          evidence_ids: candidate.evidenceIds,
          skill_ids: candidate.skillIds,
          procedure_ids: candidate.procedureIds,
          retrieval_composition_candidate_id: candidate.candidateId,
          current_skill_resolution_required: candidate.skillIds.length > 0,
          executable_skill_body_present: false,
          truncated,
        },
      });
    }
    const selectedCandidates = selections
      .filter((item) => item.selected)
      .map((selection) => candidates.find((candidate) => candidate.candidateId === selection.candidateId)!)
      .filter(Boolean);
    const sourceProvenanceComplete = selectedCandidates.every((candidate) =>
      Boolean(candidate.sourceId && candidate.sourceRevision && candidate.contentDigest)
    );
    if (!sourceProvenanceComplete) {
      throw contractError(
        "retrieval_composition_provenance_missing",
        "selected retrieval composition contains a source without provenance",
      );
    }
    const createdAt = nowIso(this.now);
    const unsigned = {
      protocol: RETRIEVAL_COMPOSITION_PROTOCOL,
      receiptId: stableId(
        "retrieval-composition-receipt",
        input.identity.taskId,
        input.identity.sessionId,
        input.boundaryId,
        input.memoryQueryId,
        input.codeQueryId,
        selections.map((item) => item.selectionDigest),
      ),
      identity: cloneJson(input.identity),
      boundaryId: input.boundaryId,
      goal: input.goal,
      memoryQueryId: input.memoryQueryId,
      memoryIndexGeneration: input.memoryIndexGeneration,
      codeQueryId: input.codeQueryId,
      codeIndexGeneration: input.codeIndexGeneration,
      candidates,
      selections,
      selectedCandidateIds: selectedCandidates.map((item) => item.candidateId),
      rejectedCandidateIds: selections.filter((item) => !item.selected).map((item) => item.candidateId),
      attachments,
      selectedTokens: input.maximumTokens - remaining,
      budgetTokens: input.maximumTokens,
      budgetRemaining: remaining,
      selectedMemoryIds: selectedCandidates.filter((item) => item.sourceKind === "canonical_memory").map((item) => item.sourceId),
      selectedSkillMemoryIds: selectedCandidates.filter((item) => item.sourceKind === "skill_outcome").map((item) => item.sourceId),
      selectedProcedureIds: selectedCandidates.flatMap((item) => item.procedureIds),
      selectedCodeReferenceIds: selectedCandidates.filter((item) => item.sourceKind === "code_index").map((item) => item.sourceId),
      selectedArtifactIds: uniqueStrings(selectedCandidates.flatMap((item) => item.artifactIds)),
      sourceProvenanceComplete,
      currentSkillResolutionRequired: true,
      createdAt,
      metadata: {
        ...input.metadata,
        canonical_memory_owner: "SQLiteStore.memory_records",
        retrieval_owner: "M1-06A RetrievalIntegrationRuntime",
        procedure_owner: "ReusableProcedureStore",
        skill_outcome_owner: "SkillMemoryApplication",
        context_owner: "02B ContextAssemblyRuntime",
        copied_index_state: false,
        copied_skill_body: false,
      },
    } satisfies Omit<RetrievalCompositionReceipt, "receiptDigest">;
    const receipt: RetrievalCompositionReceipt = { ...unsigned, receiptDigest: digest(unsigned) };
    const existing = this.receipts.get(receipt.receiptId);
    if (existing && existing.receiptDigest !== receipt.receiptDigest) {
      throw contractError("retrieval_composition_receipt_conflict", "retrieval composition receipt id conflict");
    }
    this.receipts.set(receipt.receiptId, receipt);
    this.revision += existing ? 0 : 1;
    return cloneJson(existing ?? receipt);
  }

  prepareDelivery(input: {
    receiptId: string;
    workerRequestId: string;
    workerKind: "code" | "browser";
    projectionId?: string | null;
    metadata?: JsonObject;
  }): RetrievalCompositionDelivery {
    this.assertEnabled();
    const receipt = this.receipts.get(input.receiptId.trim());
    if (!receipt) throw contractError("retrieval_composition_receipt_missing", `retrieval composition receipt not found: ${input.receiptId}`);
    const workerRequestId = input.workerRequestId.trim();
    if (!workerRequestId) throw contractError("retrieval_composition_worker_request", "retrieval composition delivery requires worker request id");
    const preparedAt = nowIso(this.now);
    const deliveryId = stableId(
      "retrieval-composition-delivery",
      receipt.receiptId,
      workerRequestId,
      input.workerKind,
    );
    const existing = this.deliveries.get(deliveryId);
    if (existing) {
      if (existing.receiptId !== receipt.receiptId || existing.workerRequestId !== workerRequestId) {
        throw contractError("retrieval_composition_delivery_conflict", "retrieval composition delivery identity conflict");
      }
      return cloneJson(existing);
    }
    const unsigned = {
      deliveryId,
      receiptId: receipt.receiptId,
      workerRequestId,
      workerKind: input.workerKind,
      state: "prepared" as const,
      projectionId: input.projectionId?.trim() || null,
      providerRequestIds: [],
      terminalEventIds: [],
      preparedAt,
      settledAt: null,
      reason: "",
      metadata: {
        ...input.metadata,
        selected_candidate_ids: receipt.selectedCandidateIds,
        selected_tokens: receipt.selectedTokens,
      },
    } satisfies Omit<RetrievalCompositionDelivery, "deliveryDigest">;
    const delivery: RetrievalCompositionDelivery = { ...unsigned, deliveryDigest: digest(unsigned) };
    this.deliveries.set(delivery.deliveryId, delivery);
    this.revision += 1;
    return cloneJson(delivery);
  }

  settleDelivery(input: {
    deliveryId: string;
    committed: boolean;
    providerRequestIds?: string[];
    terminalEventIds?: string[];
    reason?: string;
  }): RetrievalCompositionDelivery {
    this.assertEnabled();
    const delivery = this.deliveries.get(input.deliveryId.trim());
    if (!delivery) throw contractError("retrieval_composition_delivery_missing", `retrieval composition delivery not found: ${input.deliveryId}`);
    if (delivery.state !== "prepared") {
      const expectedState = input.committed ? "committed" : "released";
      if (delivery.state !== expectedState) {
        throw contractError("retrieval_composition_delivery_terminal_conflict", "retrieval composition delivery was settled differently");
      }
      return cloneJson(delivery);
    }
    const settled: RetrievalCompositionDelivery = {
      ...delivery,
      state: input.committed ? "committed" : "released",
      providerRequestIds: uniqueStrings(input.providerRequestIds),
      terminalEventIds: uniqueStrings(input.terminalEventIds),
      settledAt: nowIso(this.now),
      reason: input.reason?.trim() || (input.committed ? "provider_context_committed" : "provider_context_released"),
      deliveryDigest: "",
    };
    const { deliveryDigest: _ignored, ...unsigned } = settled;
    settled.deliveryDigest = digest(unsigned);
    this.deliveries.set(settled.deliveryId, settled);
    this.revision += 1;
    return cloneJson(settled);
  }

  receipt(receiptId: string): RetrievalCompositionReceipt | null {
    const receipt = this.receipts.get(receiptId.trim());
    return receipt ? cloneJson(receipt) : null;
  }

  listReceipts(limit = 1_000): RetrievalCompositionReceipt[] {
    return [...this.receipts.values()]
      .sort((left, right) => right.createdAt.localeCompare(left.createdAt) || left.receiptId.localeCompare(right.receiptId))
      .slice(0, Math.max(0, Math.min(limit, 100_000)))
      .map(cloneJson);
  }

  listDeliveries(options: { state?: RetrievalCompositionDelivery["state"]; workerKind?: "code" | "browser"; limit?: number } = {}): RetrievalCompositionDelivery[] {
    return [...this.deliveries.values()]
      .filter((item) => !options.state || item.state === options.state)
      .filter((item) => !options.workerKind || item.workerKind === options.workerKind)
      .sort((left, right) => right.preparedAt.localeCompare(left.preparedAt) || left.deliveryId.localeCompare(right.deliveryId))
      .slice(0, Math.max(0, Math.min(options.limit ?? 1_000, 100_000)))
      .map(cloneJson);
  }

  health(): JsonObject {
    const receipts = [...this.receipts.values()];
    const deliveries = [...this.deliveries.values()];
    return {
      protocol: RETRIEVAL_COMPOSITION_PROTOCOL,
      revision: this.revision,
      receipt_count: receipts.length,
      selected_candidate_count: receipts.reduce((total, item) => total + item.selectedCandidateIds.length, 0),
      selected_token_count: receipts.reduce((total, item) => total + item.selectedTokens, 0),
      delivery_count: deliveries.length,
      prepared_delivery_count: deliveries.filter((item) => item.state === "prepared").length,
      committed_delivery_count: deliveries.filter((item) => item.state === "committed").length,
      released_delivery_count: deliveries.filter((item) => item.state === "released").length,
      missing_provenance_count: receipts.filter((item) => !item.sourceProvenanceComplete).length,
      owns_canonical_memory: false,
      owns_retrieval_index: false,
      owns_procedure_store: false,
      owns_skill_execution: false,
    };
  }

  snapshot(): RetrievalCompositionSnapshot {
    const unsigned: Omit<RetrievalCompositionSnapshot, "checksum"> = {
      version: "zyra.skill-memory-retrieval-composition-runtime/v1",
      identity: cloneJson(this.identity),
      revision: this.revision,
      receipts: this.listReceipts(100_000).sort((left, right) => left.receiptId.localeCompare(right.receiptId)),
      deliveries: this.listDeliveries({ limit: 100_000 }).sort((left, right) => left.deliveryId.localeCompare(right.deliveryId)),
      capturedAt: nowIso(this.now),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshotValue: RetrievalCompositionSnapshot): void {
    if (snapshotValue.version !== "zyra.skill-memory-retrieval-composition-runtime/v1") {
      throw contractError("retrieval_composition_snapshot_version", "unsupported retrieval composition snapshot version");
    }
    const { checksum, ...unsigned } = snapshotValue;
    if (digest(unsigned) !== checksum) {
      throw contractError("retrieval_composition_snapshot_checksum", "retrieval composition snapshot checksum mismatch");
    }
    if (snapshotValue.identity.taskId !== this.identity.taskId || snapshotValue.identity.sessionId !== this.identity.sessionId) {
      throw contractError("retrieval_composition_snapshot_binding", "retrieval composition snapshot belongs to another task/session");
    }
    const receipts = new Map<string, RetrievalCompositionReceipt>();
    for (const receipt of snapshotValue.receipts) {
      const { receiptDigest, ...receiptUnsigned } = receipt;
      if (digest(receiptUnsigned) !== receiptDigest) {
        throw contractError("retrieval_composition_receipt_digest", `retrieval composition receipt digest mismatch: ${receipt.receiptId}`);
      }
      if (!receipt.sourceProvenanceComplete) {
        throw contractError("retrieval_composition_snapshot_provenance", `retrieval composition receipt lacks provenance: ${receipt.receiptId}`);
      }
      receipts.set(receipt.receiptId, cloneJson(receipt));
    }
    const deliveries = new Map<string, RetrievalCompositionDelivery>();
    for (const delivery of snapshotValue.deliveries) {
      const { deliveryDigest, ...deliveryUnsigned } = delivery;
      if (digest(deliveryUnsigned) !== deliveryDigest) {
        throw contractError("retrieval_composition_delivery_digest", `retrieval composition delivery digest mismatch: ${delivery.deliveryId}`);
      }
      if (!receipts.has(delivery.receiptId)) {
        throw contractError("retrieval_composition_delivery_receipt", `retrieval composition delivery references missing receipt: ${delivery.receiptId}`);
      }
      deliveries.set(delivery.deliveryId, cloneJson(delivery));
    }
    this.receipts.clear();
    this.deliveries.clear();
    for (const [key, value] of receipts) this.receipts.set(key, value);
    for (const [key, value] of deliveries) this.deliveries.set(key, value);
    this.revision = snapshotValue.revision;
  }

  private candidates(input: Required<RetrievalCompositionInput>): RetrievalCompositionCandidate[] {
    const candidates: RetrievalCompositionCandidate[] = [];
    for (const hint of input.memoryHints) {
      const reasons: string[] = [];
      if (!input.memoryQueryId) reasons.push("memory_query_id_missing");
      if (input.memoryIndexGeneration === null) reasons.push("memory_index_generation_missing");
      if (!hint.sourceId || !hint.sourceRevision) reasons.push("canonical_memory_provenance_missing");
      candidates.push(candidate({
        sourceKind: "canonical_memory",
        sourceId: hint.memoryId,
        sourceRevision: `${input.memoryIndexGeneration ?? "unknown"}:${hint.sourceRevision}`,
        title: hint.title,
        content: `[CANONICAL MEMORY ${hint.memoryId}]\n${hint.title}\n${hint.content}\nsource=${hint.sourceType}:${hint.sourceId} revision=${hint.sourceRevision}`,
        artifactIds: hint.artifactIds,
        score: finiteScore(hint.score),
        priority: Math.max(100, 800 - hint.rank),
        required: hint.layer === "semantic" || hint.layer === "procedural",
        rejectionReasons: reasons,
        metadata: {
          memory_layer: hint.layer,
          memory_query_id: input.memoryQueryId,
          memory_index_generation: input.memoryIndexGeneration,
          canonical_source_type: hint.sourceType,
          canonical_source_id: hint.sourceId,
          ...hint.metadata,
        },
      }));
    }
    for (const reference of input.codeReferences) {
      const reasons: string[] = [];
      if (!input.codeQueryId) reasons.push("code_query_id_missing");
      if (input.codeIndexGeneration === null) reasons.push("code_index_generation_missing");
      if (!reference.workspaceRevision || !reference.sourceDigest) reasons.push("code_reference_provenance_missing");
      candidates.push(candidate({
        sourceKind: "code_index",
        sourceId: reference.referenceId,
        sourceRevision: `${reference.workspaceRevision}:${reference.indexGeneration}`,
        title: `${reference.logicalPath}:${reference.lineStart}-${reference.lineEnd}`,
        content: `[CODE REFERENCE ${reference.referenceId}]\n${reference.logicalPath}:${reference.lineStart}-${reference.lineEnd}\n${reference.excerpt}\nselected_tests=${reference.selectedTests.join(",") || "none"}`,
        artifactIds: [],
        score: finiteScore(reference.score),
        priority: 600,
        required: false,
        rejectionReasons: reasons,
        metadata: {
          code_query_id: input.codeQueryId,
          code_index_generation: input.codeIndexGeneration,
          logical_path: reference.logicalPath,
          line_start: reference.lineStart,
          line_end: reference.lineEnd,
          source_digest: reference.sourceDigest,
          selected_tests: reference.selectedTests,
          ...reference.metadata,
        },
      }));
    }
    for (const memory of input.skillMemories) {
      const reasons: string[] = [];
      if (memory.state !== "accepted") reasons.push("skill_outcome_not_accepted");
      if (memory.status !== "completed") reasons.push("skill_outcome_not_completed");
      if (!memory.evidence.some((item) => item.trustedRuntime)) reasons.push("skill_outcome_trusted_evidence_missing");
      candidates.push(candidate({
        sourceKind: "skill_outcome",
        sourceId: memory.memoryId,
        sourceRevision: `${memory.revision}:${memory.recordDigest}`,
        title: `Skill outcome ${memory.version.skillName}`,
        content: [
          `[SKILL OUTCOME ${memory.memoryId}]`,
          `skill=${memory.version.skillId} name=${memory.version.skillName}`,
          `registry_revision=${memory.version.registryRevision}`,
          `descriptor_digest=${memory.version.descriptorDigest}`,
          `policy_decision=${memory.policy.decisionId} policy_revision=${memory.policy.policyRevision}`,
          `effective_tools=${memory.policy.effectiveTools.join(",") || "none"}`,
          `denied_tools=${memory.policy.deniedTools.join(",") || "none"}`,
          `summary=${memory.summary}`,
          `evidence_ids=${memory.evidence.map((item) => item.evidenceId).join(",")}`,
          "Historical outcome memory is evidence only. Re-resolve the skill through 03C before execution.",
        ].join("\n"),
        artifactIds: memory.artifactIds,
        evidenceIds: memory.evidence.map((item) => item.evidenceId),
        skillIds: [memory.version.skillId],
        score: memory.status === "completed" ? 1 : 0,
        priority: 950,
        required: true,
        rejectionReasons: reasons,
        metadata: {
          invocation_id: memory.invocationId,
          registry_revision: memory.version.registryRevision,
          descriptor_digest: memory.version.descriptorDigest,
          policy_decision_id: memory.policy.decisionId,
          policy_revision: memory.policy.policyRevision,
          executable_skill_body_present: false,
        },
      }));
    }
    for (const procedure of input.procedures) {
      const reasons: string[] = [];
      if (procedure.state !== "validated") reasons.push("procedure_not_validated");
      if (!procedure.provenance.curatorOutcomeId || !procedure.provenance.evidenceDigest) reasons.push("procedure_provenance_missing");
      const missingTools = procedure.applicability.requiredTools.filter((tool) => !input.allowedTools.includes("*") && !input.allowedTools.includes(tool));
      const forbiddenPresent = procedure.applicability.forbiddenTools.filter((tool) => input.allowedTools.includes(tool));
      const deniedRequired = procedure.applicability.requiredTools.filter((tool) => input.deniedTools.includes(tool));
      if (missingTools.length) reasons.push("procedure_required_tools_missing");
      if (forbiddenPresent.length) reasons.push("procedure_forbidden_tool_present");
      if (deniedRequired.length) reasons.push("procedure_required_tool_denied");
      const missingCapabilities = procedure.applicability.providerCapabilities.filter((item) => !input.providerCapabilities.includes(item));
      if (missingCapabilities.length) reasons.push("procedure_provider_capability_missing");
      candidates.push(candidate({
        sourceKind: "reusable_procedure",
        sourceId: procedure.procedureId,
        sourceRevision: `${procedure.revision}:${procedure.procedureDigest}`,
        title: procedure.name,
        content: [
          `[REUSABLE PROCEDURE ${procedure.procedureId}]`,
          `${procedure.name}: ${procedure.summary}`,
          ...[...procedure.steps].sort((left, right) => left.ordinal - right.ordinal).map((step) =>
            `${step.ordinal}. ${step.action}${step.toolName ? ` [tool=${step.toolName}]` : ""} -> ${step.expectedEffect}`
          ),
          `curator_outcome=${procedure.provenance.curatorOutcomeId}`,
          `evidence_bundle=${procedure.provenance.evidenceBundleId}`,
          `evidence_digest=${procedure.provenance.evidenceDigest}`,
          "Procedure selection is context guidance only; every skill/tool execution requires its current owner.",
        ].join("\n"),
        artifactIds: procedure.provenance.artifactIds,
        evidenceIds: uniqueStrings(procedure.steps.flatMap((step) => step.successEvidenceIds)),
        skillIds: procedure.provenance.skillVersions.map((item) => item.skillId),
        procedureIds: [procedure.procedureId],
        requiredTools: procedure.applicability.requiredTools,
        forbiddenTools: procedure.applicability.forbiddenTools,
        providerCapabilities: procedure.applicability.providerCapabilities,
        score: finiteScore(procedure.confidence),
        priority: 900 + Math.min(49, procedure.successCount),
        required: false,
        rejectionReasons: reasons,
        metadata: {
          curator_outcome_id: procedure.provenance.curatorOutcomeId,
          curator_decision_id: procedure.provenance.curatorDecisionId,
          evidence_bundle_id: procedure.provenance.evidenceBundleId,
          missing_tools: missingTools,
          forbidden_tools_present: forbiddenPresent,
          missing_provider_capabilities: missingCapabilities,
          current_skill_resolution_required: procedure.provenance.skillVersions.length > 0,
        },
      }));
    }
    for (const artifactId of input.compactArtifactIds) {
      candidates.push(candidate({
        sourceKind: "compact_artifact",
        sourceId: artifactId,
        sourceRevision: input.boundaryId,
        title: `Compact artifact ${artifactId}`,
        content: `[COMPACT ARTIFACT REFERENCE] artifact_id=${artifactId} boundary_id=${input.boundaryId}`,
        artifactIds: [artifactId],
        score: 1,
        priority: 980,
        required: true,
        rejectionReasons: [],
        metadata: { boundary_id: input.boundaryId },
      }));
    }
    for (const [index, change] of input.requirementChanges.entries()) {
      candidates.push(candidate({
        sourceKind: "requirement_change",
        sourceId: stableId("requirement-change", input.boundaryId, index, change),
        sourceRevision: input.boundaryId,
        title: `Requirement change ${index + 1}`,
        content: `[REQUIREMENT CHANGE ${index + 1}] ${change}`,
        artifactIds: [],
        score: 1,
        priority: 1_000,
        required: true,
        rejectionReasons: [],
        metadata: { ordinal: index + 1 },
      }));
    }
    return candidates;
  }

  private assertEnabled(): void {
    if (process.env.ZYRA_DISABLE_SKILL_RETRIEVAL_COMPOSITION === "1") {
      throw contractError("retrieval_composition_runtime_disabled", "06C retrieval composition runtime is disabled");
    }
  }
}

type CandidateSeed = Omit<
  RetrievalCompositionCandidate,
  "candidateId" | "contentDigest" | "tokenEstimate" | "applicable"
>;

function candidate(seedValue: Partial<CandidateSeed> & Pick<CandidateSeed, "sourceKind" | "sourceId" | "sourceRevision" | "title" | "content" | "score" | "priority" | "required" | "rejectionReasons">): RetrievalCompositionCandidate {
  const seed: CandidateSeed = {
    sourceKind: seedValue.sourceKind,
    sourceId: seedValue.sourceId.trim(),
    sourceRevision: seedValue.sourceRevision.trim(),
    title: seedValue.title.trim(),
    content: seedValue.content.trim(),
    artifactIds: uniqueStrings(seedValue.artifactIds),
    evidenceIds: uniqueStrings(seedValue.evidenceIds),
    skillIds: uniqueStrings(seedValue.skillIds),
    procedureIds: uniqueStrings(seedValue.procedureIds),
    requiredTools: uniqueStrings(seedValue.requiredTools),
    forbiddenTools: uniqueStrings(seedValue.forbiddenTools),
    providerCapabilities: uniqueStrings(seedValue.providerCapabilities),
    score: finiteScore(seedValue.score),
    priority: Math.max(0, Math.min(10_000, Math.trunc(seedValue.priority))),
    required: Boolean(seedValue.required),
    rejectionReasons: uniqueStrings(seedValue.rejectionReasons),
    metadata: cloneJson(seedValue.metadata ?? {}),
  };
  if (!seed.sourceId || !seed.sourceRevision || !seed.title || !seed.content) {
    seed.rejectionReasons = uniqueStrings([...seed.rejectionReasons, "candidate_identity_or_content_missing"]);
  }
  const contentDigest = digest(seed.content);
  return {
    candidateId: stableId("retrieval-composition-candidate", seed.sourceKind, seed.sourceId, seed.sourceRevision, contentDigest),
    ...seed,
    contentDigest,
    tokenEstimate: estimateTokens(seed.content),
    applicable: seed.rejectionReasons.length === 0,
  };
}

function normalizeInput(value: RetrievalCompositionInput, expected: RuntimeIdentity): Required<RetrievalCompositionInput> {
  if (value.identity.taskId !== expected.taskId || value.identity.sessionId !== expected.sessionId) {
    throw contractError("retrieval_composition_identity", "retrieval composition belongs to another task/session");
  }
  if (!value.boundaryId?.trim()) throw contractError("retrieval_composition_boundary", "retrieval composition boundary id is required");
  if (!value.goal?.trim()) throw contractError("retrieval_composition_goal", "retrieval composition goal is required");
  if (!Number.isSafeInteger(value.maximumTokens) || value.maximumTokens < 256) {
    throw contractError("retrieval_composition_budget", "retrieval composition maximum tokens must be at least 256");
  }
  const nullableGeneration = (input: number | null, label: string): number | null => {
    if (input === null || input === undefined) return null;
    if (!Number.isSafeInteger(input) || input < 0) throw contractError("retrieval_composition_generation", `${label} must be non-negative`);
    return input;
  };
  return {
    identity: cloneJson(value.identity),
    boundaryId: value.boundaryId.trim(),
    goal: value.goal.trim(),
    requirementChanges: uniqueStrings(value.requirementChanges),
    memoryQueryId: value.memoryQueryId?.trim() || null,
    memoryIndexGeneration: nullableGeneration(value.memoryIndexGeneration, "memory index generation"),
    memoryHints: value.memoryHints.map(normalizeMemoryHint),
    codeQueryId: value.codeQueryId?.trim() || null,
    codeIndexGeneration: nullableGeneration(value.codeIndexGeneration, "code index generation"),
    codeReferences: value.codeReferences.map(normalizeCodeReference),
    skillMemories: value.skillMemories.map(cloneJson),
    procedures: value.procedures.map(cloneJson),
    compactArtifactIds: uniqueStrings(value.compactArtifactIds),
    allowedTools: uniqueStrings(value.allowedTools),
    deniedTools: uniqueStrings(value.deniedTools),
    providerCapabilities: uniqueStrings(value.providerCapabilities),
    maximumTokens: value.maximumTokens,
    maximumItems: Math.max(1, Math.min(value.maximumItems ?? 64, 1_000)),
    metadata: cloneJson(value.metadata ?? {}),
  };
}

function normalizeMemoryHint(value: RetrievalMemoryHint): RetrievalMemoryHint {
  return {
    memoryId: value.memoryId?.trim() || "",
    layer: value.layer?.trim() || "unknown",
    title: value.title?.trim() || value.memoryId?.trim() || "memory",
    content: value.content?.trim() || "",
    sourceType: value.sourceType?.trim() || "unknown",
    sourceId: value.sourceId?.trim() || "",
    sourceRevision: value.sourceRevision?.trim() || "",
    artifactIds: uniqueStrings(value.artifactIds),
    score: finiteScore(value.score),
    rank: Math.max(0, Math.trunc(value.rank || 0)),
    indexGeneration: Math.max(0, Math.trunc(value.indexGeneration || 0)),
    metadata: cloneJson(value.metadata ?? {}),
  };
}

function normalizeCodeReference(value: RetrievalCodeReference): RetrievalCodeReference {
  return {
    referenceId: value.referenceId?.trim() || stableId("code-reference", value.logicalPath, value.lineStart, value.lineEnd, value.sourceDigest),
    logicalPath: value.logicalPath?.trim() || "unknown",
    lineStart: Math.max(0, Math.trunc(value.lineStart || 0)),
    lineEnd: Math.max(0, Math.trunc(value.lineEnd || 0)),
    sourceDigest: value.sourceDigest?.replace(/^sha256:/, "").toLowerCase() || "",
    workspaceRevision: value.workspaceRevision?.trim() || "",
    indexGeneration: Math.max(0, Math.trunc(value.indexGeneration || 0)),
    excerpt: value.excerpt?.trim() || "",
    selectedTests: uniqueStrings(value.selectedTests),
    score: finiteScore(value.score),
    metadata: cloneJson(value.metadata ?? {}),
  };
}

function finiteScore(value: number): number {
  const score = Number(value);
  if (!Number.isFinite(score)) return 0;
  return Math.max(0, Math.min(score, 1_000_000));
}

function compareCandidates(left: RetrievalCompositionCandidate, right: RetrievalCompositionCandidate): number {
  return Number(right.required) - Number(left.required)
    || Number(right.applicable) - Number(left.applicable)
    || right.priority - left.priority
    || right.score - left.score
    || left.candidateId.localeCompare(right.candidateId);
}

function attachmentKind(kind: RetrievalCompositionSourceKind): RestoreAttachmentReference["kind"] {
  if (kind === "canonical_memory" || kind === "skill_outcome" || kind === "requirement_change") return "memory";
  if (kind === "reusable_procedure") return "plan";
  if (kind === "compact_artifact") return "artifact";
  if (kind === "code_index") return "file";
  return "other";
}

export function memoryHintsFromJson(value: JsonValue | undefined): RetrievalMemoryHint[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isObject).map((item, index) => normalizeMemoryHint({
    memoryId: stringValue(item.memory_id, `memory-hint-${index}`),
    layer: stringValue(item.layer, "unknown"),
    title: stringValue(item.title, `Memory ${index + 1}`),
    content: stringValue(item.content, ""),
    sourceType: stringValue(item.source_type, "unknown"),
    sourceId: stringValue(item.source_id, ""),
    sourceRevision: stringValue(item.source_revision, ""),
    artifactIds: stringArray(item.artifact_ids),
    score: numberValue(item.score),
    rank: numberValue(item.rank),
    indexGeneration: numberValue(item.index_generation),
    metadata: objectValue(item.metadata),
  }));
}

export function codeReferencesFromJson(value: JsonValue | undefined): RetrievalCodeReference[] {
  if (!Array.isArray(value)) return [];
  return value.filter(isObject).map((item, index) => normalizeCodeReference({
    referenceId: stringValue(item.reference_id, `code-reference-${index}`),
    logicalPath: stringValue(item.logical_path, "unknown"),
    lineStart: numberValue(item.line_start),
    lineEnd: numberValue(item.line_end),
    sourceDigest: stringValue(item.source_digest, ""),
    workspaceRevision: stringValue(item.workspace_revision, ""),
    indexGeneration: numberValue(item.index_generation),
    excerpt: stringValue(item.excerpt, ""),
    selectedTests: stringArray(item.selected_tests),
    score: numberValue(item.score),
    metadata: objectValue(item.metadata),
  }));
}

/**
 * Convert the bounded 06B Python read projection into the shared procedure
 * contract. The projection remains reference/context data: this adapter does
 * not admit it into the canonical procedure store or grant execution rights.
 */
export function proceduresFromJson(value: JsonValue | undefined): ReusableProcedure[] {
  const root = isObject(value ?? null) ? value as JsonObject : {};
  const rawMatches = Array.isArray(root.matches) ? root.matches : [];
  return rawMatches
    .filter(isObject)
    .map((match) => objectValue(match.procedure))
    .filter((procedure) => stringValue(procedure.procedure_id, "") !== "")
    .map((procedure) => {
      const applicability = objectValue(procedure.applicability);
      const provenance = objectValue(procedure.provenance);
      const steps = Array.isArray(procedure.steps) ? procedure.steps.filter(isObject) : [];
      const versions = Array.isArray(provenance.skill_versions)
        ? provenance.skill_versions.filter(isObject)
        : [];
      return {
        procedureId: stringValue(procedure.procedure_id, ""),
        protocol: "zyra.reusable-procedure/v1",
        name: stringValue(procedure.name, "Reusable procedure"),
        summary: stringValue(procedure.summary, ""),
        state: stringValue(procedure.state, "candidate") === "validated" ? "validated" : "candidate",
        revision: Math.max(1, Math.trunc(numberValue(procedure.revision))),
        steps: steps.map((step, index) => ({
          stepId: stringValue(step.step_id, `step-${index + 1}`),
          ordinal: Math.max(1, Math.trunc(numberValue(step.ordinal) || index + 1)),
          action: stringValue(step.action, ""),
          toolName: stringValue(step.tool_name, "") || null,
          inputShapeDigest: stringValue(step.input_shape_digest, "") || null,
          expectedEffect: stringValue(step.expected_effect, ""),
          successEvidenceIds: stringArray(step.success_evidence_ids),
          artifactKinds: stringArray(step.artifact_kinds),
          retryable: step.retryable === true,
          failureRoutes: stringArray(step.failure_routes),
          metadata: {
            ...objectValue(step.metadata),
            source_step_state: stringValue(step.state, "observed"),
          },
        })),
        applicability: {
          languages: stringArray(applicability.languages),
          workspaceKinds: stringArray(applicability.workspace_kinds),
          goalPatterns: stringArray(applicability.goal_patterns),
          requiredTools: stringArray(applicability.required_tools),
          forbiddenTools: stringArray(applicability.forbidden_tools),
          requiredArtifactKinds: stringArray(applicability.required_artifact_kinds),
          providerCapabilities: stringArray(applicability.provider_capabilities),
          minimumTrust: stringValue(applicability.minimum_trust, "verified") === "internal"
            ? "internal" as const
            : "verified" as const,
          constraints: [],
        },
        provenance: {
          curatorOutcomeId: stringValue(provenance.curator_outcome_id, ""),
          curatorJobId: stringValue(provenance.curator_job_id, ""),
          curatorDecisionId: stringValue(provenance.curator_decision_id, ""),
          evidenceBundleId: stringValue(provenance.evidence_bundle_id, ""),
          evidenceDigest: stringValue(provenance.evidence_digest, ""),
          memoryId: stringValue(provenance.memory_id, ""),
          memoryRevision: Math.max(0, Math.trunc(numberValue(provenance.memory_revision))),
          runId: stringValue(provenance.run_id, ""),
          taskId: stringValue(provenance.task_id, ""),
          sessionIds: stringArray(provenance.session_ids),
          toolCallIds: stringArray(provenance.tool_call_ids),
          artifactIds: stringArray(provenance.artifact_ids),
          skillVersions: versions.map((version) => ({
            skillId: stringValue(version.skill_id, ""),
            skillName: stringValue(version.skill_name, ""),
            registryRevision: Math.max(0, Math.trunc(numberValue(version.registry_revision))),
            descriptorDigest: stringValue(version.descriptor_digest, ""),
            bodyDigest: stringValue(version.body_digest, ""),
            resourceDigests: Object.fromEntries(
              Object.entries(objectValue(version.resource_digests))
                .map(([key, item]) => [key, String(item ?? "")]),
            ),
            sourceRevision: stringValue(version.source_revision, ""),
          })),
          memoryEventIds: stringArray(provenance.memory_event_ids),
        },
        consumers: stringArray(procedure.consumers).filter((consumer) =>
          ["routing", "recovery", "context"].includes(consumer)
        ) as ReusableProcedure["consumers"],
        confidence: numberValue(procedure.confidence),
        successCount: Math.max(0, Math.trunc(numberValue(procedure.success_count))),
        failureCount: Math.max(0, Math.trunc(numberValue(procedure.failure_count))),
        validatedAt: stringValue(procedure.validated_at, "") || null,
        createdAt: stringValue(procedure.created_at, ""),
        updatedAt: stringValue(procedure.updated_at, ""),
        supersedesProcedureId: stringValue(procedure.supersedes_procedure_id, "") || null,
        procedureDigest: stringValue(procedure.procedure_digest, ""),
        metadata: {
          ...objectValue(procedure.metadata),
          source_projection: "06B ReusableProcedureRuntime.context",
          context_only: true,
          current_skill_resolution_required: true,
        },
      } satisfies ReusableProcedure;
    });
}

function isObject(value: JsonValue): value is JsonObject {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function stringValue(value: JsonValue | undefined, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function numberValue(value: JsonValue | undefined): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function stringArray(value: JsonValue | undefined): string[] {
  return Array.isArray(value) ? uniqueStrings(value) : [];
}

function objectValue(value: JsonValue | undefined): JsonObject {
  return isObject(value ?? null) ? cloneJson(value as JsonObject) : {};
}
