import { afterEach, describe, expect, test } from "bun:test";

import {
  SNAPCOMPACT_EXPERIMENT_PROTOCOL,
  SKILL_OUTCOME_PROTOCOL,
  SkillMemoryApplication,
  compactBlocksFromMessages,
  digest,
  type CompactRestoreCandidateInput,
  type CompactRestoreCandidateResult,
  type ContextAssemblySectionInput,
  type JsonObject,
  type RuntimeIdentity,
  type SkillInvocationOutcomeInput,
} from "../src/index.ts";

const identity: RuntimeIdentity = {
  runId: "run-skill-memory",
  taskId: "task-skill-memory",
  sessionId: "session-skill-memory",
  workerRequestId: "worker-skill-memory",
  epoch: 0,
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
    const selectedTokens = Math.min(
      maximumTokens,
      this.sections.reduce((total, item) => total + Math.max(1, Math.ceil(item.text.length / 4)), 0),
    );
    return {
      assemblyId: `assembly-${this.sections.length}`,
      revision: this.sections.length,
      systemPrompt: "",
      messages: this.sections.map((item) => ({ role: "user", content: item.text })),
      contextDigest: digest(this.sections),
      selectedTokens,
      pairInvariantOk: true,
    };
  }

  snapshot() {
    return structuredClone(this.sections);
  }
}

class RestorePort {
  readonly candidates = new Map<string, CompactRestoreCandidateResult>();

  discover(input: CompactRestoreCandidateInput): CompactRestoreCandidateResult {
    const candidateId = input.candidateId ?? `candidate-${this.candidates.size + 1}`;
    const result = {
      candidateId,
      state: "selected",
      sourceDigest: input.content ? digest(input.content) : null,
      content: input.content ?? null,
      estimatedTokens: Math.max(0, Math.ceil((input.content?.length ?? 0) / 4)),
      required: input.required ?? false,
      metadata: structuredClone(input.metadata ?? {}) as JsonObject,
    };
    this.candidates.set(candidateId, result);
    return structuredClone(result);
  }

  provideContent(candidateId: string, content: string) {
    const existing = this.candidates.get(candidateId);
    if (!existing) throw new Error("candidate_not_found");
    const changed = {
      ...existing,
      content,
      sourceDigest: digest(content),
      estimatedTokens: Math.ceil(content.length / 4),
    };
    this.candidates.set(candidateId, changed);
    return structuredClone(changed);
  }

  select() {
    return [...this.candidates.values()]
      .filter((item) => item.content !== null)
      .map((item) => ({
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

function outcome(): SkillInvocationOutcomeInput {
  const occurredAt = "2026-07-21T08:00:00.000Z";
  return {
    protocol: SKILL_OUTCOME_PROTOCOL,
    identity,
    invocationId: "skill-invocation-1",
    toolCallId: "skill-tool-call-1",
    compositionId: "composition-1",
    status: "completed",
    version: {
      skillId: "reviewer",
      skillName: "reviewer",
      registryRevision: 7,
      descriptorDigest: digest("descriptor"),
      bodyDigest: digest("body"),
      resourceDigests: { "checklist.md": digest("resource") },
      sourceRevision: "revision-7",
    },
    policy: {
      decisionId: "decision-1",
      effect: "allow",
      policyRevision: "policy-7",
      policyDigest: digest("policy"),
      requestedTools: ["file_read"],
      effectiveTools: ["file_read"],
      deniedTools: ["shell"],
      approvalId: null,
    },
    evidence: [{
      evidenceId: "evidence-tool-1",
      kind: "tool_result",
      sourceId: "skill-tool-call-1",
      sourceDigest: digest("tool result"),
      sequence: 11,
      occurredAt,
      toolName: "file_read",
      toolCallId: "nested-tool-call-1",
      artifactId: null,
      trustedRuntime: true,
      metadata: { owner: "03C SkillCoordinator" },
    }],
    artifacts: [{ artifact_id: "artifact-review-1", kind: "review" }],
    outputDigest: digest({ reviewed: true }),
    summary: "Reviewer completed a repository review with runtime evidence.",
    successReason: "validated skill invocation completed",
    failureReason: null,
    reuseConditions: [{
      conditionId: "condition-language",
      kind: "language",
      operator: "equals",
      key: "language",
      value: "typescript",
      required: true,
      sourceEvidenceIds: ["evidence-tool-1"],
    }],
    inputTokens: 120,
    outputTokens: 48,
    toolCalls: 1,
    costMicros: 300,
    startedAt: "2026-07-21T07:59:59.000Z",
    completedAt: occurredAt,
    sourceRecordDigest: digest("03c-journal-result"),
    metadata: { source_owner: "03C", executable_cache: false },
  };
}

function application() {
  const context = new ContextPort();
  const restore = new RestorePort();
  const runtime = new SkillMemoryApplication({ identity, context, compactRestore: restore });
  return { runtime, context, restore };
}

function compactBlocks() {
  return compactBlocksFromMessages([
    {
      id: "message-1",
      role: "assistant",
      content: [{ type: "tool_use", id: "nested-call", name: "file_read", text: "read a large source file".repeat(80) }],
      createdAt: "2026-07-21T08:01:00.000Z",
      turnIndex: 1,
      apiRound: 1,
    },
    {
      id: "message-2",
      role: "tool",
      content: [{ type: "tool_result", tool_use_id: "nested-call", text: "large verified source output".repeat(80) }],
      createdAt: "2026-07-21T08:01:01.000Z",
      turnIndex: 1,
      apiRound: 1,
    },
    {
      id: "message-3",
      role: "user",
      content: [{ type: "text", text: "Preserve the latest user constraint." }],
      createdAt: "2026-07-21T08:02:00.000Z",
      turnIndex: 2,
      apiRound: 2,
    },
  ]);
}

afterEach(() => {
  delete process.env.ZYRA_DISABLE_SKILL_MEMORY_RUNTIME;
  delete process.env.ZYRA_DISABLE_COMPACT_RESTORE_MEMORY_BRIDGE;
  delete process.env.ZYRA_ENABLE_SNAPCOMPACT_EXPERIMENT;
});

describe("06C skill outcome and compact/restore foundation", () => {
  test("admits immutable outcome provenance without taking 03C invocation ownership", () => {
    const { runtime } = application();
    const receipt = runtime.recordSkillOutcome(outcome());
    expect(receipt.disposition).toBe("accepted");
    const record = runtime.listSkillOutcomes({ reusableOnly: true })[0]!;
    expect(record.version.registryRevision).toBe(7);
    expect(record.version.descriptorDigest).toBe(digest("descriptor"));
    expect(record.policy.effectiveTools).toEqual(["file_read"]);
    expect(record.sourceRecordDigest).toBe(digest("03c-journal-result"));
    expect(runtime.health().invokes_skills).toBe(false);
    expect(runtime.health().owns_skill_version_or_revoke).toBe(false);
  });

  test("keeps tool call/result pairs atomic and feeds restore into next code/browser context", () => {
    const { runtime, context } = application();
    runtime.recordSkillOutcome(outcome());
    const trigger = runtime.observeCompactTrigger({
      identity,
      kind: "manual",
      currentTokens: 9_000,
      contextWindow: 16_384,
      reservedOutputTokens: 2_048,
      thresholdTokens: 8_000,
      activeToolCallIds: [],
      pendingToolResultIds: [],
      idleMilliseconds: 0,
      compactGeneration: 1,
      consecutiveFailures: 0,
      querySource: "behavior-test",
      observedAt: "2026-07-21T08:03:00.000Z",
      metadata: {},
    });
    expect(trigger.decision).toBe("compact");
    const blocks = compactBlocks();
    const plan = runtime.planSafeCut({
      triggerId: trigger.triggerId,
      compactGeneration: 1,
      blocks,
      targetTokens: 128,
      minimumRecentTurns: 1,
    });
    expect(plan.valid).toBe(true);
    expect(plan.summarizedMessageIds).toEqual(["message-1", "message-2"]);
    expect(runtime.safeCuts.audit(plan, blocks).ok).toBe(true);
    const archive = runtime.commitArchive({
      artifactId: "artifact-compact-1",
      boundaryId: "boundary-1",
      sessionId: identity.sessionId,
      compactGeneration: 1,
      contentDigest: digest("canonical compact content"),
      summary: "The paired tool exchange inspected source; preserve current constraints.",
      safeCutPlan: plan,
      tokenCountAfter: 64,
    });
    const archivedSnapshot = runtime.snapshot();
    const prepared = runtime.prepareRestore({
      identity,
      workerKind: "code",
      boundaryId: "boundary-1",
      archive,
      summary: "The paired tool exchange inspected source; preserve current constraints.",
      restoredAttachments: [],
      parentAllowedTools: ["file_read"],
      restoredAllowedTools: ["file_read", "shell"],
      deniedTools: ["shell"],
      maximumTokens: 4_096,
    });
    expect(prepared.projection.allowedToolsAfter).toEqual(["file_read"]);
    expect(prepared.projection.allowedToolsAfter).not.toContain("shell");
    const before = context.sections.length;
    const applied = runtime.applyRestore(prepared.projection.projectionId);
    expect(applied.projection.contextEpochAfter).toBe(1);
    expect(before).toBeGreaterThan(0);
    expect(context.sections.length).toBe(before);
    const providerContent = applied.providerMessage.content as JsonObject[];
    expect(String(providerContent[0]?.text)).toContain("restored context epoch");

    const browserContext = new ContextPort();
    const browserRuntime = new SkillMemoryApplication({
      identity,
      context: browserContext,
      compactRestore: new RestorePort(),
      snapshot: archivedSnapshot,
    });
    const browserPrepared = browserRuntime.prepareRestore({
      identity,
      workerKind: "browser",
      boundaryId: "boundary-1",
      archive,
      summary: "Browser continuation receives the same verified compact boundary.",
      restoredAttachments: [],
      parentAllowedTools: ["browser_read"],
      restoredAllowedTools: ["browser_read", "browser_write"],
      deniedTools: ["browser_write"],
      maximumTokens: 4_096,
    });
    const browserApplied = browserRuntime.applyRestore(browserPrepared.projection.projectionId);
    expect(browserApplied.projection.workerKind).toBe("browser");
    expect(browserApplied.projection.allowedToolsAfter).toEqual(["browser_read"]);
    expect(browserApplied.projection.contextEpochAfter).toBe(1);
    expect(browserContext.sections.length).toBeGreaterThan(0);
  });

  test("restores exact outcome/archive/context epoch state from a checksummed snapshot", () => {
    const first = application();
    first.runtime.recordSkillOutcome(outcome());
    const snapshot = first.runtime.snapshot();
    const secondContext = new ContextPort();
    const second = new SkillMemoryApplication({
      identity,
      context: secondContext,
      compactRestore: new RestorePort(),
      snapshot,
    });
    const restored = second.snapshot();
    expect(restored.revision).toBe(snapshot.revision);
    expect(restored.outcomes.records).toEqual(snapshot.outcomes.records);
    expect(restored.outcomes.receipts).toEqual(snapshot.outcomes.receipts);
    expect(restored.restoreBridge.contextEpoch).toBe(snapshot.restoreBridge.contextEpoch);
    expect(restored.signals.signals).toEqual(snapshot.signals.signals);
    expect(second.listSkillOutcomes({ reusableOnly: true })).toHaveLength(1);
  });

  test("disable switches fail closed while experimental snapcompact remains off by default", () => {
    const { runtime } = application();
    process.env.ZYRA_DISABLE_SKILL_MEMORY_RUNTIME = "1";
    expect(() => runtime.recordSkillOutcome(outcome())).toThrow("disabled");
    delete process.env.ZYRA_DISABLE_SKILL_MEMORY_RUNTIME;
    runtime.snapcompact.register({
      protocol: SNAPCOMPACT_EXPERIMENT_PROTOCOL,
      capabilityId: "vision-frame-summary",
      enabled: true,
      providerIds: ["provider-a"],
      modelPatterns: ["model-*"],
      maximumFrameBytes: 2_048,
      maximumFrames: 4,
      maximumTotalBytes: 4_096,
      acceptedMediaTypes: ["image/png"],
      rendererRevision: "renderer-v1",
      metadata: { experimental: true },
    });
    const evaluated = runtime.snapcompact.evaluate({
      capabilityId: "vision-frame-summary",
      providerId: "provider-a",
      modelId: "model-vision",
      frames: [{
        frameId: "frame-1",
        sequence: 1,
        mediaType: "image/png",
        width: 1280,
        height: 720,
        byteSize: 512,
        frameHash: digest("frame"),
        sourceMessageIds: ["browser-frame-1"],
        metadata: {},
      }],
      contextWindow: 16_384,
      restoreBudgetTokens: 2_048,
    });
    expect(evaluated.eligible).toBe(false);
    expect(evaluated.fallbackReason).toBe("experimental_feature_flag_disabled");
    expect(runtime.snapcompact.health().default_path).toBe(false);
  });
});
