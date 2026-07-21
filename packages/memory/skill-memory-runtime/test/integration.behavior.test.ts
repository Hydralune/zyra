import { afterEach, describe, expect, test } from "bun:test";

import {
  CONTINUITY_FAILURE_PROTOCOL,
  ProviderRestoreFidelityRuntime,
  SKILL_OUTCOME_PROTOCOL,
  SkillMemoryApplication,
  SkillMemoryContinuityFailureRuntime,
  SkillMemoryIntegrationRuntime,
  compactBlocksFromMessages,
  digest,
  proceduresFromJson,
  type CompactRestoreCandidateInput,
  type CompactRestoreCandidateResult,
  type ContextAssemblySectionInput,
  type CurrentSkillAuthority,
  type JsonObject,
  type RestoreHistoryScenario,
  type RuntimeIdentity,
  type SkillInvocationOutcomeInput,
} from "../src/index.ts";

const identity: RuntimeIdentity = {
  runId: "run-06c02",
  taskId: "task-06c02",
  sessionId: "session-06c02",
  workerRequestId: "worker-06c02",
  epoch: 3,
};

class ContextPort {
  readonly sections: ContextAssemblySectionInput[] = [];

  add(input: ContextAssemblySectionInput) {
    const stored = structuredClone(input);
    this.sections.push(stored);
    return {
      sectionId: stored.sectionId ?? `section-${this.sections.length}`,
      contentDigest: digest(stored.content),
      tokenEstimate: Math.max(1, Math.ceil(stored.text.length / 4)),
      state: "active",
    };
  }

  assemble(maximumTokens = 32_000) {
    return {
      assemblyId: `assembly-${this.sections.length}`,
      revision: this.sections.length,
      systemPrompt: "",
      messages: this.sections.map((item) => ({ role: "user", content: item.text })),
      contextDigest: digest(this.sections),
      selectedTokens: Math.min(maximumTokens, 8_000),
      pairInvariantOk: true,
    };
  }

  snapshot() {
    return structuredClone(this.sections);
  }
}

class RestorePort {
  private readonly candidates = new Map<string, CompactRestoreCandidateResult>();

  discover(input: CompactRestoreCandidateInput): CompactRestoreCandidateResult {
    const candidateId = input.candidateId ?? `candidate-${this.candidates.size + 1}`;
    const result: CompactRestoreCandidateResult = {
      candidateId,
      state: "selected",
      sourceDigest: input.content ? digest(input.content) : null,
      content: input.content ?? null,
      estimatedTokens: Math.max(0, Math.ceil((input.content?.length ?? 0) / 4)),
      required: input.required ?? false,
      metadata: structuredClone(input.metadata ?? {}),
    };
    this.candidates.set(candidateId, result);
    return structuredClone(result);
  }

  provideContent(candidateId: string, content: string) {
    const current = this.candidates.get(candidateId);
    if (!current) throw new Error("candidate_not_found");
    const changed = {
      ...current,
      content,
      sourceDigest: digest(content),
      estimatedTokens: Math.max(1, Math.ceil(content.length / 4)),
    };
    this.candidates.set(candidateId, changed);
    return structuredClone(changed);
  }

  select() {
    return [...this.candidates.values()].filter((item) => item.content).map((item) => ({
      type: "attachment" as const,
      attachmentKind: "file" as const,
      path: null,
      name: item.candidateId,
      content: item.content ?? "",
      sourceDigest: item.sourceDigest ?? digest(""),
      truncated: false,
    }));
  }

  snapshot() {
    return [...this.candidates.values()].map((item) => structuredClone(item));
  }
}

function outcomeInput(): SkillInvocationOutcomeInput {
  const occurredAt = "2026-07-21T10:00:00.000Z";
  return {
    protocol: SKILL_OUTCOME_PROTOCOL,
    identity,
    invocationId: "invocation-06c02",
    toolCallId: "skill-call-06c02",
    compositionId: "composition-06c02",
    status: "completed",
    version: {
      skillId: "repository-review",
      skillName: "repository-review",
      registryRevision: 9,
      descriptorDigest: digest("descriptor-v9"),
      bodyDigest: digest("body-v9"),
      resourceDigests: { "checklist.md": digest("checklist-v9") },
      sourceRevision: "registry-revision-9",
    },
    policy: {
      decisionId: "policy-decision-v9",
      effect: "allow",
      policyRevision: "9:registry-revision-9",
      policyDigest: digest("policy-v9"),
      requestedTools: ["file_read", "shell"],
      effectiveTools: ["file_read", "shell"],
      deniedTools: [],
      approvalId: null,
    },
    evidence: [{
      evidenceId: "evidence-06c02",
      kind: "tool_result",
      sourceId: "skill-call-06c02",
      sourceDigest: digest("verified result"),
      sequence: 41,
      occurredAt,
      toolName: "file_read",
      toolCallId: "nested-file-read",
      artifactId: "artifact-review",
      trustedRuntime: true,
      metadata: { canonical_owner: "03C SkillCoordinator" },
    }],
    artifacts: [{ artifact_id: "artifact-review", kind: "review" }],
    outputDigest: digest("review-output"),
    summary: "完成仓库审查；保留约束 α≤β、路径 C:\\项目\\源码。",
    successReason: "runtime evidence verified",
    failureReason: null,
    reuseConditions: [],
    inputTokens: 90,
    outputTokens: 44,
    toolCalls: 1,
    costMicros: 120,
    startedAt: occurredAt,
    completedAt: occurredAt,
    sourceRecordDigest: digest("03c-source-record"),
    metadata: { current_03c_resolution_required: true },
  };
}

function application() {
  const context = new ContextPort();
  const restore = new RestorePort();
  return {
    context,
    restore,
    runtime: new SkillMemoryApplication({ identity, context, compactRestore: restore }),
  };
}

function acceptedMemory() {
  const { runtime } = application();
  const admitted = runtime.recordSkillOutcome(outcomeInput());
  return runtime.outcomes.get(admitted.memoryId)!;
}

function history() {
  return compactBlocksFromMessages([
    {
      id: "history-call",
      role: "assistant",
      content: [{ type: "tool_use", id: "atomic-1", name: "file_read", text: "读取关键源码，约束 α≤β。" }],
      createdAt: "2026-07-21T10:01:00.000Z",
      turnIndex: 1,
      apiRound: 1,
    },
    {
      id: "history-result",
      role: "tool",
      content: [{ type: "tool_result", tool_use_id: "atomic-1", text: "证据 artifact-review 已验证 ✓" }],
      createdAt: "2026-07-21T10:01:01.000Z",
      turnIndex: 1,
      apiRound: 1,
    },
    {
      id: "history-change",
      role: "user",
      content: [{ type: "text", text: "需求变更：切换 provider 后仍须保留 CJK 与证据引用。" }],
      createdAt: "2026-07-21T10:02:00.000Z",
      turnIndex: 2,
      apiRound: 2,
    },
  ]);
}

function fidelityScenario(overrides: Partial<RestoreHistoryScenario> = {}): RestoreHistoryScenario {
  const memory = acceptedMemory();
  return {
    identity,
    boundaryId: "boundary-fidelity",
    history: history(),
    facts: {
      goal: "继续仓库审查并生成可验证证据",
      constraints: ["不得扩大工具权限", "保留 α≤β 与 C:\\项目\\源码"],
      requirementChanges: ["provider 切换后必须使用文本/引用回退"],
      evidenceReferences: memory.evidence,
      skillVersions: [memory.version],
      policyReferences: [memory.policy],
      artifactIds: ["artifact-review"],
      toolAtomicGroupIds: ["atomic-1"],
      cjkAndSymbols: ["中文恢复 ✓ α≤β C:\\项目\\源码"],
      metadata: {},
    },
    contextWindow: 32_768,
    restoreBudgetTokens: 8_192,
    providerId: "anthropic",
    modelId: "claude-vision",
    providerKind: "anthropic",
    providerCapabilities: ["text", "vision", "artifact_refs", "evidence_refs"],
    gatewayCapabilities: ["image_parts", "frame_hash"],
    frames: [{
      frameId: "frame-1",
      sequence: 1,
      mediaType: "image/png",
      width: 1280,
      height: 720,
      byteSize: 2_048,
      frameHash: digest("frame-1"),
      sourceMessageIds: ["history-change"],
      metadata: { renderer_revision: "renderer-v2" },
    }],
    sourceProviderId: "anthropic",
    sourceModelId: "claude-vision",
    metadata: {},
    ...overrides,
  };
}

afterEach(() => {
  delete process.env.ZYRA_ENABLE_SNAPCOMPACT_EXPERIMENT;
  delete process.env.ZYRA_DISABLE_OMP_COMPACT_ADAPTER;
  delete process.env.ZYRA_DISABLE_RESTORE_FIDELITY_RUNTIME;
  delete process.env.ZYRA_DISABLE_SKILL_MEMORY_INTEGRATION;
  delete process.env.ZYRA_DISABLE_SKILL_MEMORY_RUNTIME;
});

describe("06C-02 compact restore integration", () => {
  test("compares identical history and budgets across text, extractive, and experimental lanes", () => {
    process.env.ZYRA_ENABLE_SNAPCOMPACT_EXPERIMENT = "1";
    const runtime = new ProviderRestoreFidelityRuntime();
    const comparison = runtime.compare(fidelityScenario());
    expect(comparison.envelopes.map((item) => item.strategy).sort()).toEqual([
      "extractive_artifact_refs",
      "snapcompact_experimental",
      "text_summary",
    ]);
    expect(new Set(comparison.envelopes.map((item) => item.budgetTokens))).toEqual(new Set([8_192]));
    expect(comparison.baselineTextRefAvailable).toBe(true);
    expect(comparison.experimentalDefault).toBe(false);
    for (const evaluation of comparison.evaluations) {
      expect(evaluation.recall.goal).toBe(true);
      expect(evaluation.recall.constraints).toBe(2);
      expect(evaluation.recall.requirementChanges).toBe(1);
      expect(evaluation.recall.evidenceReferences).toBe(1);
      expect(evaluation.recall.skillVersions).toBe(1);
      expect(evaluation.recall.policyReferences).toBe(1);
      expect(evaluation.recall.toolAtomicGroups).toBe(1);
      expect(evaluation.recall.cjkAndSymbols).toBe(1);
      expect(evaluation.toolAtomicityPreserved).toBe(true);
      expect(evaluation.baselineAvailable).toBe(true);
    }
    const experimental = comparison.envelopes.find((item) => item.strategy === "snapcompact_experimental")!;
    expect(experimental.imageContentPresent).toBe(true);
    expect(experimental.textFallbackPresent).toBe(true);
  });

  test("uses explicit text/reference fallbacks for vision loss, frame corruption, gateway drop and provider switch", () => {
    process.env.ZYRA_ENABLE_SNAPCOMPACT_EXPERIMENT = "1";
    const runtime = new ProviderRestoreFidelityRuntime();
    const cases: RestoreHistoryScenario[] = [
      fidelityScenario({
        providerId: "compatible",
        modelId: "text-only",
        providerKind: "compatible",
        providerCapabilities: ["text"],
        gatewayCapabilities: [],
      }),
      fidelityScenario({
        frames: [{
          ...fidelityScenario().frames[0]!,
          metadata: { gateway_observed_hash: digest("corrupt-frame") },
        }],
      }),
      fidelityScenario({
        frames: [{
          ...fidelityScenario().frames[0]!,
          metadata: { gateway_dropped: true },
        }],
      }),
      fidelityScenario({
        sourceProviderId: "local",
        sourceModelId: "local-text",
      }),
      fidelityScenario({ frames: [] }),
    ];
    const expectedReasons = [
      "vision_unsupported",
      "frame_hash_mismatch",
      "gateway_dropped_frame",
      "provider_switched",
      "frames_missing",
    ];
    cases.forEach((scenario, index) => {
      const comparison = runtime.compare(scenario);
      const experimental = comparison.envelopes.find((item) => item.strategy === "snapcompact_experimental")!;
      expect(experimental.fallbackReasons).toContain(expectedReasons[index]);
      expect(experimental.fallbackStrategy).toBe("extractive_artifact_refs");
      expect(experimental.textFallbackPresent).toBe(true);
      expect(runtime.failureEvents(comparison.comparisonId).length).toBeGreaterThan(0);
    });
  });

  test("disabling the OMP adapter removes only experimental image behavior", () => {
    process.env.ZYRA_ENABLE_SNAPCOMPACT_EXPERIMENT = "1";
    process.env.ZYRA_DISABLE_OMP_COMPACT_ADAPTER = "1";
    const runtime = new ProviderRestoreFidelityRuntime();
    const comparison = runtime.compare(fidelityScenario());
    const baselines = comparison.envelopes.filter((item) => item.strategy !== "snapcompact_experimental");
    expect(baselines).toHaveLength(2);
    expect(baselines.every((item) => item.textFallbackPresent)).toBe(true);
    const experimental = comparison.envelopes.find((item) => item.strategy === "snapcompact_experimental")!;
    expect(experimental.imageContentPresent).toBe(false);
    expect(experimental.fallbackReasons).toContain("omp_compact_adapter_disabled");
  });

  test("revalidates current 03C authority and never restores revoked or tightened rights", () => {
    const integration = new SkillMemoryIntegrationRuntime({ identity });
    const memory = acceptedMemory();
    const current = (overrides: Partial<CurrentSkillAuthority> = {}): CurrentSkillAuthority => ({
      protocol: "zyra.skill-coordinator-authority/v1",
      skillId: memory.version.skillId,
      skillName: memory.version.skillName,
      availability: "available",
      registryRevision: 10,
      registryRevisionId: "registry-revision-10",
      descriptorDigest: digest("descriptor-v10"),
      bodyDigest: digest("body-v10"),
      sourceRevision: "registry-revision-10",
      trust: "verified",
      policy: {
        decisionId: "policy-decision-v10",
        effect: "allow",
        policyRevision: "10:registry-revision-10",
        policyDigest: digest("policy-v10"),
        requestedTools: ["file_read"],
        effectiveTools: ["file_read"],
        deniedTools: ["shell"],
        approvalId: null,
      },
      resolvedAt: "2026-07-21T10:05:00.000Z",
      resolutionError: null,
      metadata: { canonical_owner: "03C SkillCoordinator" },
      ...overrides,
    });
    const tightened = integration.revalidateMemory({
      memory,
      current: current(),
      parentAllowedTools: ["file_read", "shell"],
      parentDeniedTools: [],
      metadata: { compact_boundary_id: "boundary-authority" },
    });
    expect(tightened.executable).toBe(true);
    expect(tightened.policyTightened).toBe(true);
    expect(tightened.effectiveTools).toEqual(["file_read"]);
    expect(tightened.removedTools).toEqual(["shell"]);
    const fence = integration.authority.issueFence(tightened.receiptId);
    expect(() => integration.authority.consumeFence({
      fenceId: fence.fenceId,
      current: current(),
      requestedTools: ["shell"],
    })).toThrow("exceed");

    const revoked = integration.revalidateMemory({
      memory,
      current: current({ availability: "removed", policy: null, resolutionError: "skill removed" }),
      parentAllowedTools: ["file_read", "shell"],
      parentDeniedTools: [],
      metadata: { compact_boundary_id: "boundary-authority" },
    });
    expect(revoked.executable).toBe(false);
    expect(revoked.disposition).toBe("revoked");
    expect(() => integration.authority.issueFence(revoked.receiptId)).toThrow("rejected");
    const authorityFailure = integration.failures.list({ stage: "authority_revalidation" });
    expect(authorityFailure).toHaveLength(1);
    expect(authorityFailure[0]!.disposition).toBe("replan_without_skill_memory");
  });

  test("composes real 06A hints and 06B procedure projections into one bounded restore", () => {
    const memory = acceptedMemory();
    const procedureProjection = {
      protocol: "zyra.reusable-procedure/v1",
      query_id: "procedure-query-1",
      query_digest: digest("procedure-query"),
      matches: [{
        applicable: true,
        score: 0.97,
        procedure: {
          procedure_id: "procedure-review-retry",
          name: "Review retry",
          summary: "Re-read the failed file and verify the artifact.",
          state: "validated",
          revision: 2,
          steps: [{
            step_id: "step-1",
            ordinal: 1,
            action: "read failed source",
            tool_name: "file_read",
            input_shape_digest: digest("shape"),
            expected_effect: "source evidence captured",
            success_evidence_ids: ["procedure-evidence-1"],
            artifact_kinds: ["review"],
            retryable: true,
            failure_routes: ["replan"],
            state: "verified",
            metadata: {},
          }],
          applicability: {
            required_tools: ["file_read"],
            forbidden_tools: ["shell"],
            provider_capabilities: ["text"],
            minimum_trust: "verified",
          },
          provenance: {
            curator_outcome_id: "curator-outcome-1",
            curator_job_id: "curator-job-1",
            curator_decision_id: "curator-decision-1",
            evidence_bundle_id: "evidence-bundle-1",
            evidence_digest: digest("evidence-bundle"),
            memory_id: "memory-procedure-1",
            memory_revision: 4,
            run_id: identity.runId,
            task_id: identity.taskId,
            session_ids: [identity.sessionId],
            tool_call_ids: ["procedure-tool-1"],
            artifact_ids: ["artifact-procedure"],
            skill_versions: [],
            memory_event_ids: ["memory-event-1"],
          },
          consumers: ["context", "routing", "recovery"],
          confidence: 0.97,
          success_count: 3,
          failure_count: 0,
          validated_at: "2026-07-21T09:00:00.000Z",
          created_at: "2026-07-21T08:00:00.000Z",
          updated_at: "2026-07-21T09:00:00.000Z",
          procedure_digest: digest("procedure-review-retry"),
          metadata: {},
        },
      }],
    };
    expect(proceduresFromJson(procedureProjection).map((item) => item.procedureId)).toEqual([
      "procedure-review-retry",
    ]);
    const integration = new SkillMemoryIntegrationRuntime({ identity });
    const prepared = integration.prepare({
      identity,
      workerKind: "code",
      boundaryId: "boundary-retrieval",
      archive: {
        archiveId: "archive-retrieval",
        artifactId: "artifact-compact",
        boundaryId: "boundary-retrieval",
        sessionId: identity.sessionId,
        compactGeneration: 2,
        contentDigest: digest("compact-content"),
        summaryDigest: digest("compact-summary"),
        summarizedMessageIds: ["history-call", "history-result"],
        preservedMessageIds: ["history-change"],
        tokenCountBefore: 4_000,
        tokenCountAfter: 900,
        createdAt: "2026-07-21T10:03:00.000Z",
        metadata: {},
      },
      history: history(),
      goal: "review the current repository",
      constraints: ["do not use shell"],
      requirementChanges: ["preserve evidence after compact"],
      skillMemories: [memory],
      procedures: [],
      existingAttachments: [],
      runtimeConstraints: {
        memory_retrieval: { query_id: "memory-query-1", index_revision: 4 },
        memory_hints: [{
          memory_id: "canonical-memory-1",
          layer: "semantic",
          title: "Review goal",
          content: "Keep the review evidence and current constraint.",
          source_type: "task_event",
          source_id: "event-1",
          source_revision: "4",
          artifact_ids: ["artifact-memory"],
          score: 1,
          rank: 1,
          index_generation: 4,
        }],
        code_index_query_id: "code-query-1",
        code_index_generation: 7,
        code_index_selected_files: ["src/reviewer.ts"],
        code_index_selected_tests: ["test/reviewer.test.ts"],
        reusable_procedure_context: procedureProjection,
      },
      allowedTools: ["file_read"],
      deniedTools: ["shell"],
      providerId: "local",
      modelId: "local-text",
      providerKind: "local",
      providerCapabilities: ["text", "artifact_refs", "evidence_refs"],
      gatewayCapabilities: [],
      frames: [],
      contextWindow: 32_768,
      maximumTokens: 8_192,
    });
    const receipt = integration.retrieval.receipt(prepared.retrievalReceiptId)!;
    expect(receipt.selectedMemoryIds).toContain("canonical-memory-1");
    expect(receipt.selectedSkillMemoryIds).toContain(memory.memoryId);
    expect(receipt.selectedProcedureIds).toContain("procedure-review-retry");
    expect(receipt.selectedCodeReferenceIds.length).toBeGreaterThan(0);
    expect(receipt.sourceProvenanceComplete).toBe(true);
    expect(prepared.procedureIds).toContain("procedure-review-retry");
    expect(prepared.attachments.some((item) => item.kind === "plan")).toBe(true);
  });

  test("persists continuity failures, opens the retry circuit, and detects resume loss", () => {
    const failures = new SkillMemoryContinuityFailureRuntime({ identity });
    const records = [1, 2, 3].map(() => failures.record({
      identity,
      boundaryId: "boundary-failure",
      stage: "provider_delivery",
      code: "gateway_dropped_image",
      message: "gateway dropped the image frame",
      retryable: true,
      baselineTextReferenceAvailable: true,
      canonicalCheckpointAvailable: true,
      currentAuthorityRequired: true,
      provenanceComplete: true,
    }));
    expect(records[0]!.disposition).toBe("retry_text_reference");
    expect(records[1]!.disposition).toBe("retry_text_reference");
    expect(records[2]!.disposition).toBe("reroute_canonical_checkpoint");
    const claim = failures.claim({
      failureId: records[2]!.failureId,
      workerRequestId: "recovery-worker-1",
    });
    const settled = failures.settle({
      claimId: claim.claimId,
      committed: true,
      terminalEventIds: ["event-recovered"],
    });
    expect(settled.state).toBe("committed");
    expect(failures.event(records[2]!.failureId).protocol).toBe(CONTINUITY_FAILURE_PROTOCOL);
    const restored = new SkillMemoryContinuityFailureRuntime({
      identity,
      snapshot: failures.snapshot(),
    });
    expect(restored.list({ state: "recovered" })).toHaveLength(1);

    const app = application().runtime;
    app.recordSkillOutcome(outcomeInput());
    const observation = {
      identity,
      restartEpoch: 3,
      contextEpoch: 0,
      applicationRevision: 1,
      outcomes: app.listSkillOutcomes(),
      procedures: [],
      projections: [],
      signals: app.signals.list({ limit: 1_000 }),
      archiveIds: [],
      metadata: {},
    };
    const manifest = app.integration.captureContinuity(observation);
    const receipt = app.integration.verifyContinuity({
      manifestId: manifest.manifestId,
      observation: { ...observation, outcomes: [] },
      metadata: { compact_boundary_id: "boundary-resume" },
    });
    expect(receipt.lossless).toBe(false);
    expect(receipt.missingOutcomeIds).toHaveLength(1);
    expect(receipt.executableAuthorityRestored).toBe(false);
    expect(app.integration.failures.list({ stage: "resume_verification" })).toHaveLength(1);
  });

  test("routes foundation signals to consumers and preserves 03C/06C disable semantics", () => {
    const first = application();
    first.runtime.recordSkillOutcome(outcomeInput());
    expect(first.runtime.integration.consumers.list({ consumer: "routing" }).length).toBeGreaterThan(0);
    const snapshot = first.runtime.snapshot();
    const second = new SkillMemoryApplication({
      identity,
      context: new ContextPort(),
      compactRestore: new RestorePort(),
      snapshot,
    });
    const restoredIntegration = second.integration.snapshot();
    expect(restoredIntegration.revision).toBe(snapshot.integration!.revision);
    expect(restoredIntegration.consumers.directives).toEqual(
      snapshot.integration!.consumers.directives,
    );
    expect(restoredIntegration.preparations).toEqual(snapshot.integration!.preparations);
    process.env.ZYRA_DISABLE_SKILL_MEMORY_INTEGRATION = "1";
    expect(() => second.integration.prepare({} as never)).toThrow("disabled");
    delete process.env.ZYRA_DISABLE_SKILL_MEMORY_INTEGRATION;
    process.env.ZYRA_DISABLE_SKILL_MEMORY_RUNTIME = "1";
    expect(() => second.recordSkillOutcome(outcomeInput())).toThrow("disabled");
    expect(second.health()["03c_loader_owner_preserved"]).toBe(true);
    expect(second.health().invokes_skills).toBe(false);
  });
});
