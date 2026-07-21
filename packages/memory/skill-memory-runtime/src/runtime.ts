import {
  SKILL_MEMORY_PROTOCOL,
  cloneJson,
  contractError,
  digest,
  normalizeCompactPolicy,
  normalizeIdentity,
  normalizeSkillMemoryPolicy,
  nowIso,
  type CompactArchiveReference,
  type CompactMessageBlock,
  type CompactPolicy,
  type CompactRestoreProjection,
  type CompactTriggerObservation,
  type CompactTriggerReceipt,
  type JsonObject,
  type ReusableProcedure,
  type RuntimeIdentity,
  type SafeCutPlan,
  type SkillInvocationOutcomeInput,
  type SkillInvocationOutcomeMemory,
  type SkillMemoryPolicy,
} from "./contracts.ts";
import { CompactArchiveRuntime, type CompactArchiveInput, type CompactArchiveSnapshot } from "./archive-runtime.ts";
import { CompactTriggerRuntime, type CompactTriggerSnapshot } from "./compact-trigger-runtime.ts";
import { SkillContextProjector, type SkillContextProjectionInput, type SkillContextProjectionResult } from "./context-projector.ts";
import { SkillOutcomeRuntime, type SkillOutcomeAdmissionReceipt, type SkillOutcomeQuery, type SkillOutcomeRuntimeSnapshot } from "./outcome-runtime.ts";
import type { CompactRestorePort, ContextAssemblyPort, MemorySignalTransport } from "./ports.ts";
import { CompactRestoreMemoryBridge, type CompactRestoreBridgeSnapshot } from "./restore-bridge.ts";
import { SafeCutRuntime, type SafeCutRequest } from "./safe-cut-runtime.ts";
import { MemorySignalEmitter, type MemorySignalInput, type MemorySignalSnapshot } from "./signal-emitter.ts";
import { SnapcompactExperimentalRuntime, type SnapcompactExperimentalSnapshot } from "./snapcompact-experimental.ts";
import { SkillMemoryIntegrationRuntime, type SkillMemoryIntegrationSnapshot } from "./integration-runtime.ts";

export interface SkillMemoryApplicationSnapshot {
  version: typeof SKILL_MEMORY_PROTOCOL;
  identity: RuntimeIdentity;
  revision: number;
  skillMemoryPolicy: SkillMemoryPolicy;
  compactPolicy: CompactPolicy;
  outcomes: SkillOutcomeRuntimeSnapshot;
  compactTriggers: CompactTriggerSnapshot;
  safeCutPlans: SafeCutPlan[];
  archives: CompactArchiveSnapshot;
  restoreBridge: CompactRestoreBridgeSnapshot;
  signals: MemorySignalSnapshot;
  snapcompact: SnapcompactExperimentalSnapshot;
  /** Absent only on snapshots produced before M1-S06C-02. */
  integration?: SkillMemoryIntegrationSnapshot;
  procedures: ReusableProcedure[];
  capturedAt: string;
  checksum: string;
}

export interface SkillMemoryApplicationOptions {
  identity: RuntimeIdentity;
  context: ContextAssemblyPort;
  compactRestore: CompactRestorePort;
  signalTransport?: MemorySignalTransport | null;
  skillMemoryPolicy?: Partial<SkillMemoryPolicy>;
  compactPolicy?: Partial<CompactPolicy>;
  now?: () => Date;
  snapshot?: SkillMemoryApplicationSnapshot | null;
}

export class SkillMemoryApplication {
  readonly identity: RuntimeIdentity;
  readonly outcomes: SkillOutcomeRuntime;
  readonly compactTriggers: CompactTriggerRuntime;
  readonly safeCuts: SafeCutRuntime;
  readonly archives: CompactArchiveRuntime;
  readonly signals: MemorySignalEmitter;
  readonly snapcompact: SnapcompactExperimentalRuntime;
  readonly projector: SkillContextProjector;
  readonly restoreBridge: CompactRestoreMemoryBridge;
  readonly integration: SkillMemoryIntegrationRuntime;
  private skillMemoryPolicy: SkillMemoryPolicy;
  private compactPolicy: CompactPolicy;
  private readonly procedures = new Map<string, ReusableProcedure>();
  private readonly now: () => Date;
  private revision = 0;

  constructor(options: SkillMemoryApplicationOptions) {
    this.identity = normalizeIdentity(options.identity);
    this.now = options.now ?? (() => new Date());
    this.skillMemoryPolicy = normalizeSkillMemoryPolicy(options.skillMemoryPolicy);
    this.compactPolicy = normalizeCompactPolicy(options.compactPolicy);
    const snapshot = options.snapshot ?? null;
    this.outcomes = new SkillOutcomeRuntime({
      identity: this.identity,
      policy: this.skillMemoryPolicy,
      now: this.now,
      snapshot: snapshot?.outcomes ?? null,
    });
    this.compactTriggers = new CompactTriggerRuntime({
      identity: this.identity,
      policy: this.compactPolicy,
      now: this.now,
      snapshot: snapshot?.compactTriggers ?? null,
    });
    this.safeCuts = new SafeCutRuntime(this.now);
    if (snapshot) this.safeCuts.restore(snapshot.safeCutPlans);
    this.archives = new CompactArchiveRuntime(this.now);
    if (snapshot) this.archives.restore(snapshot.archives);
    this.signals = new MemorySignalEmitter({
      identity: this.identity,
      transport: options.signalTransport,
      now: this.now,
      snapshot: snapshot?.signals ?? null,
    });
    this.snapcompact = new SnapcompactExperimentalRuntime(this.now);
    if (snapshot) this.snapcompact.restore(snapshot.snapcompact);
    this.projector = new SkillContextProjector({
      context: options.context,
      restore: options.compactRestore,
      policy: this.compactPolicy,
      now: this.now,
    });
    this.restoreBridge = new CompactRestoreMemoryBridge({
      projector: this.projector,
      policy: this.compactPolicy,
      initialContextEpoch: snapshot?.restoreBridge.contextEpoch ?? 0,
      now: this.now,
      snapshot: snapshot?.restoreBridge ?? null,
    });
    this.integration = new SkillMemoryIntegrationRuntime({
      identity: this.identity,
      now: this.now,
      snapshot: snapshot?.integration ?? null,
    });
    if (snapshot) {
      this.validateSnapshot(snapshot);
      for (const procedure of snapshot.procedures) this.admitProcedureInternal(procedure);
      this.revision = snapshot.revision;
    }
  }

  recordSkillOutcome(input: SkillInvocationOutcomeInput): SkillOutcomeAdmissionReceipt {
    this.assertEnabled();
    const receipt = this.outcomes.admit(input);
    if (receipt.disposition === "accepted") {
      const record = this.outcomes.get(receipt.memoryId)!;
      this.emitAndRoute({
        kind: "skill_memory_updated",
        identity: this.identity,
        aggregateId: record.memoryId,
        causationId: record.invocationId,
        priority: record.status === "completed" ? "normal" : "high",
        payload: {
          memory_id: record.memoryId,
          invocation_id: record.invocationId,
          skill_id: record.version.skillId,
          skill_name: record.version.skillName,
          status: record.status,
          state: record.state,
          artifact_ids: record.artifactIds,
          evidence_ids: record.evidence.map((item) => item.evidenceId),
          registry_revision: record.version.registryRevision,
          descriptor_digest: record.version.descriptorDigest,
          policy_decision_id: record.policy.decisionId,
          routing_reusable: record.state === "accepted" && record.status === "completed",
        },
      });
    } else if (receipt.disposition === "quarantined") {
      this.emitAndRoute({
        kind: "skill_memory_rejected",
        identity: this.identity,
        aggregateId: receipt.invocationId || this.identity.sessionId,
        causationId: receipt.receiptId,
        priority: "high",
        payload: {
          invocation_id: receipt.invocationId,
          memory_id: receipt.memoryId,
          reason: receipt.reason,
          outcome_cache_can_invoke: false,
        },
      });
    }
    this.revision += 1;
    return receipt;
  }

  admitProcedure(procedure: ReusableProcedure): ReusableProcedure {
    this.assertEnabled();
    const admitted = this.admitProcedureInternal(procedure);
    this.emitAndRoute({
      kind: admitted.state === "validated" ? "procedure_mined" : "procedure_rejected",
      identity: this.identity,
      aggregateId: admitted.procedureId,
      causationId: admitted.provenance.curatorOutcomeId,
      priority: admitted.state === "validated" ? "normal" : "high",
      payload: {
        procedure_id: admitted.procedureId,
        validation_status: admitted.state,
        confidence: admitted.confidence,
        curator_outcome_id: admitted.provenance.curatorOutcomeId,
        evidence_bundle_id: admitted.provenance.evidenceBundleId,
        tool_call_ids: admitted.provenance.toolCallIds,
        artifact_ids: admitted.provenance.artifactIds,
        consumers: admitted.consumers,
        static_document_source: false,
        fixture_source: false,
      },
    });
    this.revision += 1;
    return admitted;
  }

  observeCompactTrigger(input: CompactTriggerObservation): CompactTriggerReceipt {
    this.assertEnabled();
    const receipt = this.compactTriggers.observe(input);
    this.emitAndRoute({
      kind: receipt.decision === "compact" ? "compact_triggered" : "compact_deferred",
      identity: this.identity,
      aggregateId: receipt.triggerId,
      causationId: receipt.triggerId,
      priority: receipt.decision === "compact" ? "normal" : receipt.decision === "circuit_open" ? "critical" : "low",
      payload: {
        trigger_id: receipt.triggerId,
        decision: receipt.decision,
        reason: receipt.reason,
        effective_tokens: receipt.effectiveTokens,
        headroom_tokens: receipt.headroomTokens,
        safe_to_cut: receipt.safeToCut,
        deferred_tool_pair_ids: receipt.deferredToolPairIds,
        compact_generation: receipt.compactGeneration,
      },
    });
    this.revision += 1;
    return receipt;
  }

  planSafeCut(input: SafeCutRequest): SafeCutPlan {
    this.assertEnabled();
    const trigger = this.compactTriggers.history(100_000).find((item) => item.triggerId === input.triggerId);
    if (!trigger) throw contractError("skill_memory_safe_cut_trigger_missing", "safe cut requires an admitted compact trigger");
    if (trigger.decision !== "compact") throw contractError("skill_memory_safe_cut_trigger_not_ready", `compact trigger is ${trigger.decision}, not compact`);
    const plan = this.safeCuts.plan(input);
    this.revision += 1;
    return plan;
  }

  commitArchive(input: CompactArchiveInput): CompactArchiveReference {
    this.assertEnabled();
    const plan = this.safeCuts.get(input.safeCutPlan.planId);
    if (!plan || plan.planDigest !== input.safeCutPlan.planDigest) throw contractError("skill_memory_archive_safe_cut_missing", "compact archive requires an admitted safe cut plan");
    const archive = this.archives.commit(input);
    this.compactTriggers.recordSuccess(input.safeCutPlan.triggerId, input.compactGeneration);
    this.revision += 1;
    return archive;
  }

  prepareRestore(input: Omit<SkillContextProjectionInput, "contextEpochBefore" | "skillMemories" | "procedures"> & {
    skillMemories?: SkillInvocationOutcomeMemory[];
    procedures?: ReusableProcedure[];
  }): SkillContextProjectionResult {
    this.assertEnabled();
    const archive = this.archives.get(input.archive.archiveId);
    if (!archive || archive.boundaryId !== input.boundaryId) throw contractError("skill_memory_restore_archive_missing", "restore projection requires an admitted compact archive");
    const memories = input.skillMemories ?? this.outcomes.reusable({ taskId: this.identity.taskId, sessionId: this.identity.sessionId, limit: 64 });
    const procedures = input.procedures ?? this.listProcedures({ consumers: ["context", "routing", "recovery"], validatedOnly: true, limit: 64 });
    const result = this.restoreBridge.prepare({
      ...input,
      contextEpochBefore: this.restoreBridge.currentContextEpoch(),
      skillMemories: memories,
      procedures,
    });
    this.revision += 1;
    return result;
  }

  applyRestore(projectionId: string): { projection: CompactRestoreProjection; providerMessage: JsonObject } {
    this.assertEnabled();
    const applied = this.restoreBridge.apply(projectionId);
    this.emitAndRoute({
      kind: "compact_restored",
      identity: this.identity,
      aggregateId: applied.projection.projectionId,
      causationId: applied.projection.boundaryId,
      priority: "high",
      payload: {
        projection_id: applied.projection.projectionId,
        boundary_id: applied.projection.boundaryId,
        worker_kind: applied.projection.workerKind,
        context_epoch_before: applied.projection.contextEpochBefore,
        context_epoch_after: applied.projection.contextEpochAfter,
        skill_memory_ids: applied.projection.skillMemoryIds,
        procedure_ids: applied.projection.procedureIds,
        context_section_ids: applied.projection.contextSectionIds,
        provider_message_digest: applied.projection.providerMessageDigest,
        allowed_tools_before: applied.projection.allowedToolsBefore,
        allowed_tools_after: applied.projection.allowedToolsAfter,
        tool_scope_widened: false,
      },
    });
    this.emitAndRoute({
      kind: "context_epoch_advanced",
      identity: this.identity,
      aggregateId: this.identity.sessionId,
      causationId: applied.projection.projectionId,
      priority: "normal",
      payload: {
        context_epoch_before: applied.projection.contextEpochBefore,
        context_epoch_after: applied.projection.contextEpochAfter,
        compact_boundary_id: applied.projection.boundaryId,
        worker_kind: applied.projection.workerKind,
      },
    });
    this.revision += 1;
    return { projection: applied.projection, providerMessage: applied.providerMessage };
  }

  listSkillOutcomes(query: SkillOutcomeQuery = {}): SkillInvocationOutcomeMemory[] {
    return this.outcomes.query(query);
  }

  listProcedures(options: {
    consumers?: ReusableProcedure["consumers"];
    validatedOnly?: boolean;
    limit?: number;
  } = {}): ReusableProcedure[] {
    const consumers = new Set(options.consumers ?? []);
    return [...this.procedures.values()]
      .filter((item) => !options.validatedOnly || item.state === "validated")
      .filter((item) => consumers.size === 0 || item.consumers.some((consumer) => consumers.has(consumer)))
      .sort((left, right) => right.confidence - left.confidence || right.updatedAt.localeCompare(left.updatedAt))
      .slice(0, Math.max(0, Math.min(options.limit ?? 1_000, 100_000)))
      .map(cloneJson);
  }

  async flushSignals(limit = 1_000): Promise<void> {
    await this.signals.drain(limit);
  }

  health(): JsonObject {
    return {
      protocol: SKILL_MEMORY_PROTOCOL,
      canonical_owner: "SkillMemoryApplication",
      identity: cloneJson(this.identity) as unknown as JsonObject,
      revision: this.revision,
      context_epoch: this.restoreBridge.currentContextEpoch(),
      outcomes: this.outcomes.health(),
      procedures: {
        count: this.procedures.size,
        validated_count: [...this.procedures.values()].filter((item) => item.state === "validated").length,
        source: "06B deterministic curator outcomes only",
      },
      compact_triggers: this.compactTriggers.health(),
      restore_bridge: this.restoreBridge.health(),
      signals: this.signals.health(),
      snapcompact: this.snapcompact.health(),
      integration: cloneJson(this.integration.health()) as unknown as JsonObject,
      "03c_loader_owner_preserved": true,
      invokes_skills: false,
      owns_skill_catalog: false,
      owns_skill_version_or_revoke: false,
      model_can_activate_procedure: false,
    };
  }

  snapshot(): SkillMemoryApplicationSnapshot {
    const unsigned: Omit<SkillMemoryApplicationSnapshot, "checksum"> = {
      version: SKILL_MEMORY_PROTOCOL,
      identity: cloneJson(this.identity),
      revision: this.revision,
      skillMemoryPolicy: cloneJson(this.skillMemoryPolicy),
      compactPolicy: cloneJson(this.compactPolicy),
      outcomes: this.outcomes.snapshot(),
      compactTriggers: this.compactTriggers.snapshot(),
      safeCutPlans: this.safeCuts.list(100_000),
      archives: this.archives.snapshot(),
      restoreBridge: this.restoreBridge.snapshot(),
      signals: this.signals.snapshot(),
      snapcompact: this.snapcompact.snapshot(),
      integration: this.integration.snapshot(),
      procedures: this.listProcedures({ limit: 100_000 }),
      capturedAt: nowIso(this.now),
    };
    return { ...unsigned, checksum: digest(unsigned) };
  }

  restore(snapshot: SkillMemoryApplicationSnapshot): void {
    this.validateSnapshot(snapshot);
    this.outcomes.restore(snapshot.outcomes);
    this.compactTriggers.restore(snapshot.compactTriggers);
    this.safeCuts.restore(snapshot.safeCutPlans);
    this.archives.restore(snapshot.archives);
    this.restoreBridge.restore(snapshot.restoreBridge);
    this.signals.restore(snapshot.signals);
    this.snapcompact.restore(snapshot.snapcompact);
    if (snapshot.integration) this.integration.restore(snapshot.integration);
    this.procedures.clear();
    for (const procedure of snapshot.procedures) this.admitProcedureInternal(procedure);
    this.revision = snapshot.revision;
  }

  private admitProcedureInternal(procedureValue: ReusableProcedure): ReusableProcedure {
    const procedure = validateProcedure(procedureValue, this.identity);
    const existing = this.procedures.get(procedure.procedureId);
    if (existing) {
      if (existing.procedureDigest !== procedure.procedureDigest) throw contractError("procedure_identity_conflict", `procedure id conflicts: ${procedure.procedureId}`);
      return cloneJson(existing);
    }
    this.procedures.set(procedure.procedureId, procedure);
    return cloneJson(procedure);
  }

  private emitAndRoute(input: MemorySignalInput): void {
    const signal = this.signals.emit(input);
    this.integration.consumeSignals([signal]);
  }

  private validateSnapshot(snapshot: SkillMemoryApplicationSnapshot): void {
    if (snapshot.version !== SKILL_MEMORY_PROTOCOL) throw contractError("skill_memory_snapshot_version", "unsupported skill memory application snapshot version");
    const { checksum, ...unsigned } = snapshot;
    if (digest(unsigned) !== checksum) throw contractError("skill_memory_snapshot_checksum", "skill memory application snapshot checksum mismatch");
    const identity = normalizeIdentity(snapshot.identity);
    if (identity.taskId !== this.identity.taskId || identity.sessionId !== this.identity.sessionId) throw contractError("skill_memory_snapshot_binding", "skill memory application snapshot belongs to another task/session");
    this.skillMemoryPolicy = normalizeSkillMemoryPolicy(snapshot.skillMemoryPolicy);
    this.compactPolicy = normalizeCompactPolicy(snapshot.compactPolicy);
  }

  private assertEnabled(): void {
    if (process.env.ZYRA_DISABLE_SKILL_MEMORY_RUNTIME === "1") throw contractError("skill_memory_runtime_disabled", "06C skill memory application is disabled");
  }
}

export function compactBlocksFromMessages(messages: Array<{
  id: string;
  role: string;
  content: Array<{ type: string; text?: string; id?: string; tool_use_id?: string; name?: string }>;
  createdAt: string;
  turnIndex: number | null;
  apiRound: number | null;
  metadata?: JsonObject;
}>): CompactMessageBlock[] {
  const blocks: CompactMessageBlock[] = [];
  for (const message of messages) {
    for (let index = 0; index < message.content.length; index += 1) {
      const block = message.content[index];
      const kind = block.type === "tool_use" ? "tool_call" : block.type === "tool_result" ? "tool_result" : block.type === "attachment" ? "attachment" : "text";
      const toolPairId = kind === "tool_call" ? block.id ?? null : kind === "tool_result" ? block.tool_use_id ?? null : null;
      const text = block.text ?? (kind === "tool_call" ? `${block.name ?? "tool"} call ${JSON.stringify(block)}` : JSON.stringify(block));
      blocks.push({
        blockId: `${message.id}:block:${index}`,
        messageId: message.id,
        role: message.role === "system" || message.role === "user" || message.role === "assistant" || message.role === "tool" ? message.role : "user",
        kind,
        text,
        toolPairId,
        turnIndex: message.turnIndex,
        apiRound: message.apiRound,
        tokenEstimate: Math.max(1, Math.ceil(text.length / 4)),
        createdAt: message.createdAt,
        sourceDigest: digest(block),
        metadata: cloneJson(message.metadata ?? {}),
      });
    }
  }
  return blocks;
}

function validateProcedure(value: ReusableProcedure, identity: RuntimeIdentity): ReusableProcedure {
  const procedure = cloneJson(value);
  if (procedure.protocol !== "zyra.reusable-procedure/v1") throw contractError("procedure_protocol", "unsupported reusable procedure protocol");
  if (!procedure.procedureId || !procedure.name || !procedure.summary) throw contractError("procedure_identity", "reusable procedure requires id, name and summary");
  if (procedure.provenance.taskId !== identity.taskId) throw contractError("procedure_task_binding", "reusable procedure belongs to another task");
  if (!procedure.provenance.curatorOutcomeId || !procedure.provenance.curatorDecisionId || !procedure.provenance.evidenceBundleId || !procedure.provenance.evidenceDigest) {
    throw contractError("procedure_curator_provenance", "reusable procedure requires 06B curator outcome, decision and evidence provenance");
  }
  if (procedure.state === "validated") {
    if (!procedure.validatedAt) throw contractError("procedure_validation_timestamp", "validated procedure requires validated_at");
    if (procedure.provenance.toolCallIds.length === 0) throw contractError("procedure_tool_provenance", "validated procedure requires tool call provenance");
    if (procedure.provenance.memoryEventIds.length === 0) throw contractError("procedure_memory_event_provenance", "validated procedure requires memory event provenance");
    if (procedure.steps.length === 0) throw contractError("procedure_steps", "validated procedure requires executable steps");
  }
  const { procedureDigest, ...unsigned } = procedure;
  if (digest(unsigned) !== procedureDigest) throw contractError("procedure_digest", `reusable procedure digest mismatch: ${procedure.procedureId}`);
  return procedure;
}
