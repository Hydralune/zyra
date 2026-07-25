import { describe, expect, test } from "bun:test"
import type { CommandReceipt } from "../../../packages/commands/src/index.ts"
import {
  CanonicalProjectionStore,
} from "../src/state/index.ts"
import {
  normalizeEventFrame,
  type IngressBatch,
  type IngressEvent,
  type JsonObject,
} from "../src/events/ingress/index.ts"
import {
  SkillCatalogIndex,
  SkillWorkbenchController,
  buildSkillCatalogProjection,
  buildSkillDependencyGraph,
} from "../src/features/skills/index.ts"

const TASK = "task-skill-workbench"
const RUN = "run-skill-workbench"
const SESSION = "session-skill-workbench"
const NOW = "2026-07-25T10:00:00.000Z"
const HASH_A = `sha256:${"a".repeat(64)}`
const HASH_B = `sha256:${"b".repeat(64)}`
const DESCRIPTOR_A = `sha256:${"c".repeat(64)}`
const DESCRIPTOR_B = `sha256:${"d".repeat(64)}`
const RESOURCE_HASH = `sha256:${"e".repeat(64)}`

function runtimeEvent(
  sequence: number,
  options: {
    eventId?: string
    eventType?: string
    domain?: string
    toolCallId?: string
    commandId?: string
    inline?: JsonObject
    metadata?: JsonObject
    terminal?: boolean
    effective?: boolean
  } = {},
): IngressEvent {
  const eventId = options.eventId ?? `skill-event-${sequence}`
  const raw = {
    schema: "zyra.runtime-event/v1",
    eventId,
    eventType: options.eventType ?? "skill.registry.projected",
    eventVersion: 1,
    aggregateId: `task:${TASK}`,
    aggregateSequence: sequence,
    globalSequence: sequence,
    producerSequence: sequence,
    idempotencyKey: `skill-event:${eventId}`,
    correlationId: "skill-workbench-correlation",
    createdAt: NOW,
    committedAt: new Date(Date.parse(NOW) + sequence * 1_000).toISOString(),
    durability: "durable",
    effect: options.effective === false ? "non_effective" : "effective",
    identity: {
      taskId: TASK,
      runId: RUN,
      sessionId: SESSION,
      workerId: "worker-skill-owner",
      spanId: `span-skill-${sequence}`,
      toolCallId: options.toolCallId ?? "skill-catalog-owner",
      controlCommandId: options.commandId,
    },
    sender: {
      kind: "runtime",
      id: "typescript-skill-runtime",
      capabilityRefs: ["SkillTool"],
    },
    intent: options.commandId ? "control" : "status",
    topKRecipients: [],
    summary: options.eventType ?? "skill registry projection",
    stateDelta: {
      domain: options.domain ?? "tool",
      operation: "transition",
      path: ["skills", "browser-checkout"],
      beforeDigest: HASH_A,
      afterDigest: HASH_B,
      effective: options.effective !== false,
    },
    evidenceRefs: [],
    artifactRefs: [],
    provenance: {
      sourceRepository: "zyra",
      sourceModule: "typescript-skill-runtime",
      migrationRole: "zyra_owned",
      producerVersion: "v2",
      trust: "internal",
    },
    inline: {
      ...(options.inline ?? {}),
    },
    sourceBytes: 4_096,
    inlineBytes: 2_048,
    envelopeBytes: 6_144,
    contentDigest: HASH_A,
    metadata: {
      canonical_owner: "typescript",
      ...(options.metadata ?? {}),
    },
    settlement: options.terminal ? "final" : "atomic",
    terminal: options.terminal ?? false,
  }
  return normalizeEventFrame(
    JSON.parse(JSON.stringify({
      schema: "zyra.event-ingress-frame/v1",
      kind: "event",
      source: "delta",
      generation: 1,
      taskId: TASK,
      sequence,
      previousSequence: Math.max(0, sequence - 1),
      eventId,
      eventType: raw.eventType,
      correlationId: raw.correlationId,
      observedAtMs: Date.parse(NOW) + sequence * 1_000,
      cursor: `skill-cursor.${sequence}`,
      event: raw,
    })),
    TASK,
    1,
  ).event
}

function batch(events: readonly IngressEvent[]): IngressBatch {
  const sequence = Math.max(...events.map((event) => event.globalSequence))
  return {
    taskId: TASK,
    generation: 1,
    events: Object.freeze([...events]),
    receipts: Object.freeze([]),
    cursor: `skill-cursor.${sequence}`,
    fromSequence: Math.min(...events.map((event) => event.globalSequence)),
    sequence,
    highWatermark: sequence,
    receivedAt: Date.parse(NOW) + sequence * 1_000,
    transport: "long_poll",
    snapshot: false,
    caughtUp: true,
  }
}

function descriptorPayload(
  contentHash = HASH_A,
  registryRevision = 1,
): JsonObject {
  return {
    tool_name: "SkillTool",
    skill_id: "browser-checkout",
    skill_name: "browser-checkout",
    display_name: "Browser checkout",
    description: "Verify a checkout flow with canonical browser evidence.",
    canonical_owner: "typescript",
    canonical_runtime_owner: "typescript",
    availability: "available",
    body:
      "# Checkout\n\nVerify the checkout UI and retain evidence.\n\n" +
      "## Procedure\n\nRead `resources/checklist.md`, use browser.navigate, then report.",
    version: registryRevision === 1 ? "1.0.0" : "1.1.0",
    registry_revision: registryRevision,
    content_hash: contentHash,
    descriptor_digest: registryRevision === 1 ? DESCRIPTOR_A : DESCRIPTOR_B,
    source: {
      source_id: "project-skills",
      source_kind: "project",
      source_priority: 100,
      relative_path: "skills/browser-checkout/SKILL.md",
      signature_status: "local",
      discovered_at: NOW,
    },
    resources: [
      {
        resource_id: "checkout-checklist",
        path: "resources/checklist.md",
        kind: "markdown",
        required: true,
        available: true,
        digest: RESOURCE_HASH,
        size_bytes: 2_048,
      },
    ],
    tool_scope: {
      allowed: ["browser.navigate", "browser.click", "artifact.write"],
      denied: ["terminal.exec"],
      require_approval: ["artifact.write"],
      read_only: false,
      inherit_parent: true,
      maximum_calls: 20,
      maximum_parallel: 2,
    },
    dependencies: [
      {
        dependency_id: "browser-tools",
        name: "browser-tools",
        kind: "tool",
        version_range: ">=1.0.0 <2.0.0",
        resolved_version: "1.4.0",
        required: true,
        available: true,
        approved: true,
        digest: HASH_B,
      },
    ],
    supply_chain: {
      receipt_id: `supply-${registryRevision}`,
      policy_revision: "skill-supply-policy-7",
      manifest_digest: registryRevision === 1 ? DESCRIPTOR_A : DESCRIPTOR_B,
      scanned_at: NOW,
      allowed: true,
      findings: [],
    },
  }
}

function projectionStore(): CanonicalProjectionStore {
  const store = new CanonicalProjectionStore({
    restore: false,
    autoPersist: false,
  })
  store.apply(batch([
    runtimeEvent(1, {
      eventType: "task.started",
      domain: "task",
      toolCallId: undefined,
      inline: {
        task_id: TASK,
        title: "Skill workbench task",
        status: "running",
      },
    }),
    runtimeEvent(2, {
      eventType: "skill.registry.projected",
      toolCallId: "skill-catalog-owner",
      inline: descriptorPayload(),
    }),
  ]))
  const first = buildSkillCatalogProjection(store.state, {
    taskId: TASK,
    runId: RUN,
    sessionId: SESSION,
  })
  const skill = first.skills[0]!
  if (!skill) {
    throw new Error(JSON.stringify({
      tools: store.state.tools,
      rejections: first.rejectedCandidates,
      warnings: first.warnings,
    }))
  }
  store.apply(batch([
    runtimeEvent(3, {
      eventType: "skill.approval.committed",
      toolCallId: "skill-catalog-owner",
      inline: {
        tool_name: "SkillTool",
        skill_id: "browser-checkout",
        canonical_owner: "typescript",
        staged_approval: {
          approval_id: "skill-approval-1",
          approval_revision: 1,
          stage: "approved",
          approved_hash: skill.version.contentHash,
          approved_dependency_digest: skill.dependencies.digest,
          approved_supply_digest: skill.supplyChain.digest,
          reviewer_class: "supply-chain-policy",
          reviewed_at: NOW,
        },
      },
    }),
  ]))
  return store
}

class CommandPort {
  readonly listeners = new Set<() => void>()
  readonly submissions: string[] = []
  lastOverlayReceipt?: CommandReceipt

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  getSnapshot = () => ({
    lastOverlayReceipt: this.lastOverlayReceipt,
  })

  async submit(value: string): Promise<CommandReceipt> {
    this.submissions.push(value)
    const idempotencyKey =
      value.match(/--idempotency-key\s+"([^"]+)"/)?.[1] ??
      "skill-command:idempotency-missing"
    const receipt = {
      schema: "zyra.command-receipt/v1",
      requestId: `request-${this.submissions.length}`,
      commandId: `command-${this.submissions.length}`,
      taskId: TASK,
      runId: RUN,
      sessionId: SESSION,
      name: "/skills",
      scope: "task",
      mode: "enqueue",
      priority: "next",
      idempotencyKey,
      phase: "applied",
      summary: "Skill command accepted by the owner.",
      displayText: "Skill command accepted.",
      data: {},
      eventIds: [],
      replayed: false,
      durable: true,
      executed: true,
      interventionCounted: false,
      humanInterventionCount: 0,
      operatorInterventionAttemptCount: 0,
      createdAt: NOW,
      raw: {},
    } as unknown as CommandReceipt
    this.lastOverlayReceipt = receipt
    for (const listener of this.listeners) listener()
    return receipt
  }
}

describe("skill canonical projection and supply-chain admission", () => {
  test("projects body, resources, tools, provenance, version, dependency graph and staged approval", () => {
    const store = projectionStore()
    const catalog = buildSkillCatalogProjection(store.state, {
      taskId: TASK,
      runId: RUN,
      sessionId: SESSION,
    })
    expect(catalog.skills).toHaveLength(1)
    const skill = catalog.skills[0]!
    expect(skill.body.sections.map((section) => section.heading)).toEqual([
      "Checkout",
      "Procedure",
    ])
    expect(skill.body.resourceRefs).toEqual(["resources/checklist.md"])
    expect(skill.resources[0]?.path).toBe("resources/checklist.md")
    expect(skill.resources[0]?.available).toBe(true)
    expect(skill.toolScope.allowed).toContain("browser.navigate")
    expect(skill.toolScope.denied).toEqual(["terminal.exec"])
    expect(skill.provenance.sourceKind).toBe("project")
    expect(skill.provenance.trustworthy).toBe(true)
    expect(skill.version.contentHash).toBe(HASH_A)
    expect(skill.dependencies.complete).toBe(true)
    expect(skill.dependencies.topologicalOrder).toContain("browser-tools")
    expect(skill.supplyChain.browserDisposition).toBe("pass")
    expect(skill.approval.stage).toBe("approved")
    expect(skill.approval.current).toBe(true)
    expect(skill.ready).toBe(true)
  })

  test("rejects secret-bearing projections and reports owner admission failures", () => {
    const store = projectionStore()
    store.apply(batch([
      runtimeEvent(4, {
        eventType: "skill.registry.projected",
        toolCallId: "skill-secret-owner",
        inline: {
          ...descriptorPayload(),
          skill_id: "secret-skill",
          skill_name: "secret-skill",
          api_token: "sk-this-is-a-secret-token-value",
        },
      }),
      runtimeEvent(5, {
        eventType: "skill.registry.projected",
        toolCallId: "skill-foreign-owner",
        inline: {
          ...descriptorPayload(),
          skill_id: "foreign-skill",
          skill_name: "foreign-skill",
          canonical_owner: "browser.LocalSkillStore",
        },
        metadata: {
          canonical_owner: "browser.LocalSkillStore",
        },
      }),
    ]))
    const catalog = buildSkillCatalogProjection(store.state, {
      taskId: TASK,
      runId: RUN,
      sessionId: SESSION,
    })
    expect(catalog.skills.map((skill) => skill.skillId)).toEqual([
      "browser-checkout",
    ])
    expect(catalog.rejectedCandidates.some((item) =>
      item.code === "secret_material_rejected")).toBe(true)
    expect(catalog.rejectedCandidates.some((item) =>
      item.code === "skill_owner_rejected")).toBe(true)
  })

  test("dependency cycles, missing dependencies and version conflicts fail closed", () => {
    const graph = buildSkillDependencyGraph("root-skill", [{
      dependencies: [
        {
          dependency_id: "dep-a",
          parent_id: "root-skill",
          name: "A",
          kind: "skill",
          required: true,
          available: true,
          approved: true,
          version_range: "^2.0.0",
          resolved_version: "1.3.0",
        },
        {
          dependency_id: "dep-b",
          parent_id: "dep-a",
          name: "B",
          kind: "plugin",
          required: true,
          available: false,
          approved: false,
        },
        {
          dependency_id: "dep-a",
          parent_id: "dep-b",
          name: "A",
          kind: "skill",
          required: true,
          available: true,
          approved: true,
        },
      ],
    }])
    expect(graph.complete).toBe(false)
    expect(graph.missing).toEqual(["dep-b"])
    expect(graph.unapproved).toEqual(["dep-b"])
    expect(graph.versionConflicts).toEqual(["dep-a"])
    expect(graph.cycles[0]).toEqual(["dep-a", "dep-b", "dep-a"])
  })

  test("catalog cursors are bound to canonical revision and query", () => {
    const store = projectionStore()
    const catalog = buildSkillCatalogProjection(store.state, {
      taskId: TASK,
      runId: RUN,
      sessionId: SESSION,
    })
    const index = new SkillCatalogIndex()
    index.replace(catalog)
    const first = index.page({ text: "browser", limit: 1 })
    expect(first.rows[0]?.skillId).toBe("browser-checkout")
    expect(first.queryFingerprint).toMatch(/^skill:/)
    const anotherQuery = {
      ...first,
      nextCursor: undefined,
    }
    expect(() => index.page({
      text: "different",
      limit: 1,
      cursor: first.previousCursor ?? "invalid",
    })).toThrow()
    index.disable("disable mutation")
    expect(() => index.page()).toThrow("disable mutation")
    expect(anotherQuery.rows).toHaveLength(1)
  })
})

describe("skill command, permission and canonical effect reconciliation", () => {
  test("update remains reconciling until a later hash and registry revision are canonical", async () => {
    const store = projectionStore()
    const commands = new CommandPort()
    const controller = new SkillWorkbenchController({
      projections: store,
      commands,
      online: () => true,
      now: () => new Date(NOW),
    })
    controller.bind(TASK, RUN, SESSION)
    const receipt = await controller.updateSkill({
      skillId: "browser-checkout",
      expectedHash: HASH_A,
      nonce: "skill-update-nonce-00000001",
      idempotencyKey: "skill-update-idempotency-00000001",
    })
    expect(receipt.phase).toBe("applied")
    expect(controller.getSnapshot().activeOperation?.phase).toBe("reconciling")
    expect(commands.submissions[0]).toContain("--expected-hash")
    store.apply(batch([
      runtimeEvent(4, {
        eventType: "skill.registry.updated",
        toolCallId: "skill-catalog-owner",
        commandId: receipt.commandId,
        inline: descriptorPayload(HASH_B, 2),
      }),
    ]))
    expect(controller.getSnapshot().activeOperation?.phase).toBe("committed")
    expect(controller.getSnapshot().activeOperation?.effect?.changedFields).toContain(
      "skill.content_hash",
    )
    expect(controller.getSnapshot().activeOperation?.effect?.changedFields).toContain(
      "skill.registry_revision",
    )
    controller.close()
  })

  test("invoke reconciles only a newly exact-bound canonical invocation", async () => {
    const store = projectionStore()
    const commands = new CommandPort()
    const controller = new SkillWorkbenchController({
      projections: store,
      commands,
      online: () => true,
      now: () => new Date(NOW),
    })
    controller.bind(TASK, RUN, SESSION)
    const receipt = await controller.invokeSkill({
      skillId: "browser-checkout",
      arguments: { url: "https://checkout.example.test", retries: 2 },
      nonce: "skill-invoke-nonce-00000001",
      idempotencyKey: "skill-invoke-idempotency-00000001",
    })
    expect(controller.getSnapshot().activeOperation?.phase).toBe("reconciling")
    expect(commands.submissions[0]).toContain("--arguments-digest")
    store.apply(batch([
      runtimeEvent(4, {
        eventType: "skill.invocation.started",
        toolCallId: "skill-invocation-1",
        commandId: receipt.commandId,
        inline: {
          tool_name: "SkillTool",
          canonical_owner: "typescript",
          skill_id: "browser-checkout",
          skill_name: "browser-checkout",
          invocation_id: "skill-invocation-1",
          registry_revision: 1,
          descriptor_digest: DESCRIPTOR_A,
          status: "running",
          started_at: NOW,
        },
      }),
    ]))
    expect(controller.getSnapshot().activeOperation?.phase).toBe("committed")
    expect(
      controller.getSnapshot().activeOperation?.effect?.evidenceInvocationIds,
    ).toEqual(["skill-invocation-1"])
    controller.close()
  })

  test("sealed controls record denial without command submission or intervention count", async () => {
    const store = projectionStore()
    const commands = new CommandPort()
    const sealedRecords: unknown[] = []
    const controller = new SkillWorkbenchController({
      projections: store,
      commands,
      permissions: {
        getSnapshot: () => ({ productMode: "sealed" as const }),
        recordSealedAction: async (input) => {
          sealedRecords.push(input)
          return { denied: true, human_intervention_count: 0 }
        },
      },
      online: () => true,
      sealed: true,
    })
    controller.bind(TASK, RUN, SESSION)
    await expect(controller.invokeSkill({
      skillId: "browser-checkout",
      arguments: {},
      actorId: "operator-1",
    })).rejects.toMatchObject({
      code: "sealed_skill_control_denied",
    })
    expect(commands.submissions).toHaveLength(0)
    expect(sealedRecords).toHaveLength(1)
    expect(JSON.stringify(sealedRecords[0])).toContain("Rejected sealed skill invoke")
    controller.close()
  })

  test("disconnect, viewer close and disable fail closed without killing owner work", async () => {
    const store = projectionStore()
    const commands = new CommandPort()
    const controller = new SkillWorkbenchController({
      projections: store,
      commands,
      online: () => true,
    })
    controller.bind(TASK, RUN, SESSION)
    controller.disconnected("network lost")
    await expect(controller.invokeSkill({
      skillId: "browser-checkout",
      arguments: {},
    })).rejects.toThrow("disconnected")
    expect(commands.submissions).toHaveLength(0)
    controller.reconnected()
    expect(controller.getSnapshot().catalog?.skills).toHaveLength(1)
    controller.viewerClosed()
    expect(controller.getSnapshot().catalog).toBeUndefined()
    expect(store.state.tools["skill-catalog-owner"]).toBeDefined()
    controller.viewerOpened()
    expect(controller.getSnapshot().catalog?.skills).toHaveLength(1)
    controller.disable("skill binding disabled for mutation test")
    await expect(controller.updateSkill({
      skillId: "browser-checkout",
    })).rejects.toThrow("disabled")
    expect(commands.submissions).toHaveLength(0)
    controller.close()
  })
})
