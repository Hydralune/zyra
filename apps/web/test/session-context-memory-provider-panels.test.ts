import { describe, expect, test } from "bun:test"
import type {
  CanonicalProjectionState,
  CausalEventProjection,
  MemoryProjection,
  SchedulerProjection,
  SessionProjection,
  WorkerProjection,
} from "../src/state/contracts.ts"
import { createEmptyProjectionState } from "../src/state/state.ts"
import {
  buildCompactPreview,
  buildContextBudget,
  buildSessionConsoleProjection,
} from "../src/features/session/projection.ts"
import { buildMemoryConsoleProjection } from "../src/features/memory/projection.ts"
import { buildProviderConsoleProjection } from "../src/features/providers/projection.ts"
import { buildPlacementConsoleProjection } from "../src/features/placement/projection.ts"
import { SessionConsoleRuntime } from "../src/features/session/runtime.ts"

const NOW = "2026-07-25T08:00:00.000Z"

function entity(overrides: Record<string, unknown>) {
  return {
    id: "entity",
    domain: "session",
    taskId: "task-1",
    runId: "run-1",
    sessionId: "session-1",
    lifecycle: "running",
    status: "authoritative",
    revision: 1,
    sequence: 1,
    aggregateSequence: 1,
    createdAt: NOW,
    updatedAt: NOW,
    firstEventId: "event-1",
    lastEventId: "event-1",
    correlationId: "correlation-1",
    artifactIds: [],
    title: "entity",
    summary: "entity",
    terminal: false,
    effective: true,
    attributes: {},
    metadata: {},
    ...overrides,
  }
}

function event(
  eventId: string,
  sequence: number,
  eventType: string,
  overrides: Record<string, unknown> = {},
): CausalEventProjection {
  return {
    eventId,
    eventType,
    taskId: "task-1",
    runId: "run-1",
    sessionId: "session-1",
    artifactIds: [],
    correlationId: "correlation-1",
    mutationId: `mutation-${eventId}`,
    sequence,
    aggregateSequence: sequence,
    createdAt: NOW,
    committedAt: NOW,
    summary: eventType,
    terminal: false,
    effective: true,
    entityRefs: ["session:session-1"],
    ...overrides,
  }
}

function fixture(): CanonicalProjectionState {
  const empty = createEmptyProjectionState(Date.parse(NOW))
  const parent = entity({
    id: "session-parent",
    parentSessionId: undefined,
    childSessionIds: ["session-1"],
    contextTokens: 20000,
    contextLimit: 100000,
    compactCount: 0,
    sequence: 1,
  }) as unknown as SessionProjection
  const session = entity({
    id: "session-1",
    parentSessionId: "session-parent",
    childSessionIds: [],
    contextTokens: 82000,
    contextLimit: 100000,
    compactCount: 2,
    checkpointId: "checkpoint-2",
    restoredCheckpointId: "checkpoint-1",
    sequence: 2,
    attributes: {
      compact_epoch: 2,
      compact_boundary_id: "boundary-2",
      context_categories: [
        { id: "system", label: "System", tokens: 5000, compactable: false },
        { id: "messages", label: "Conversation", tokens: 45000, compactable: true },
        { id: "tools", label: "Tools", tokens: 25000, compactable: true },
        { id: "memory", label: "Memory", tokens: 7000, compactable: false },
      ],
    },
  }) as unknown as SessionProjection
  const checkpointCommitted = event(
    "event-checkpoint-1",
    3,
    "checkpoint_committed",
    {
      checkpointId: "checkpoint-1",
      entityRefs: ["session:session-1", "checkpoint:checkpoint-1"],
    },
  )
  const checkpointRestored = event(
    "event-restore-1",
    4,
    "checkpoint_resumed",
    {
      checkpointId: "checkpoint-1",
      recoveryId: "recovery-1",
      entityRefs: ["session:session-1", "checkpoint:checkpoint-1"],
    },
  )
  const checkpointPending = event(
    "event-checkpoint-2",
    5,
    "checkpoint_pending_write",
    {
      checkpointId: "checkpoint-2",
      entityRefs: ["session:session-1", "checkpoint:checkpoint-2"],
    },
  )
  const messages = Array.from({ length: 20 }, (_, index) =>
    event(`event-message-${index}`, 10 + index, "assistant_message", {
      summary: `Conversation message ${index} with enough content to consume context tokens.`,
    }))
  const toolResults = Array.from({ length: 8 }, (_, index) =>
    event(`event-tool-${index}`, 40 + index, "tool_result_committed", {
      toolCallId: `tool-${index}`,
      summary: `Large tool result ${index} retained by canonical event reference.`,
      entityRefs: ["session:session-1", `tool:tool-${index}`],
    }))
  const memory = entity({
    id: "memory-1",
    domain: "memory",
    memoryKind: "semantic",
    namespace: "task",
    key: "verified-fact",
    score: 0.9,
    tokenCount: 240,
    sourceArtifactIds: ["artifact-1"],
    firstEventId: "event-memory-1",
    lastEventId: "event-memory-1",
    sequence: 50,
    title: "Verified project fact",
    summary: "The project requires an exact checkpoint restore.",
    attributes: {
      veracity_score: 0.95,
      tags: ["checkpoint", "recovery"],
      source_event_ids: ["event-memory-1"],
    },
  }) as unknown as MemoryProjection
  const memoryEvent = event("event-memory-1", 50, "memory_retrieved", {
    entityRefs: ["session:session-1", "memory:memory-1", "artifact:artifact-1"],
    artifactIds: ["artifact-1"],
  })
  const scheduler = entity({
    id: "scheduler-1",
    domain: "scheduler",
    routeId: "route-cloud",
    placementId: "placement-cloud",
    providerId: "provider-cloud",
    modelId: "model-cloud",
    resourceClass: "cloud",
    privacyClass: "internal",
    candidateIds: ["placement-device", "placement-edge", "placement-cloud"],
    latencyEstimateMs: 160,
    costEstimate: 0.4,
    sequence: 60,
    attributes: {
      sensitivity: "restricted",
      allow_cloud: false,
      requires_isolation: true,
      requires_encryption: true,
      requires_trusted_execution: true,
      maximum_latency_ms: 100,
      minimum_memory_mb: 4096,
      required_capabilities: ["tools"],
      providers: [
        {
          id: "provider-cloud",
          name: "Cloud provider",
          health: "healthy",
          credential_present: true,
          quota: { limit: 1000, remaining: 800, used: 200 },
          models: [
            {
              id: "model-cloud",
              capabilities: ["tools", "vision"],
              context_window: 200000,
              input_price_per_million: 2,
              output_price_per_million: 8,
              credential_present: true,
            },
          ],
        },
        {
          id: "provider-edge",
          name: "Edge provider",
          health: "degraded",
          credential_present: true,
          models: [
            {
              id: "model-edge",
              capabilities: ["tools"],
              context_window: 64000,
              credential_present: true,
            },
          ],
        },
      ],
      placement_candidates: [
        {
          id: "placement-device",
          tier: "device",
          online: true,
          isolated: true,
          encrypted: true,
          trusted_execution: true,
          memory_mb: 16384,
          memory_used_mb: 4096,
          cpu_cores: 8,
          cpu_utilization: 0.2,
          process_slots: 4,
          process_slots_used: 1,
          network_latency_ms: 5,
          capabilities: ["tools"],
          supported_sensitivity: ["public", "internal", "confidential", "restricted"],
        },
        {
          id: "placement-edge",
          tier: "edge",
          online: true,
          isolated: true,
          encrypted: true,
          trusted_execution: false,
          memory_mb: 8192,
          memory_used_mb: 2048,
          cpu_cores: 4,
          cpu_utilization: 0.3,
          process_slots: 2,
          process_slots_used: 0,
          network_latency_ms: 25,
          capabilities: ["tools"],
          supported_sensitivity: ["public", "internal", "confidential"],
        },
        {
          id: "placement-cloud",
          tier: "cloud",
          online: true,
          isolated: false,
          encrypted: true,
          trusted_execution: false,
          memory_mb: 32768,
          memory_used_mb: 4000,
          cpu_cores: 16,
          cpu_utilization: 0.1,
          process_slots: 16,
          process_slots_used: 2,
          network_latency_ms: 160,
          capabilities: ["tools", "vision"],
          supported_sensitivity: ["public", "internal"],
        },
      ],
    },
  }) as unknown as SchedulerProjection
  const worker = entity({
    id: "worker-1",
    domain: "worker",
    placementId: "placement-cloud",
    routeId: "route-cloud",
    health: "healthy",
    capabilityRefs: ["tools"],
    sequence: 61,
    attributes: {
      tier: "cloud",
      memory_mb: 32768,
      network_latency_ms: 160,
    },
  }) as unknown as WorkerProjection
  const providerFailure = event("event-provider-failure", 62, "provider_failure", {
    entityRefs: [
      "session:session-1",
      "provider:provider-cloud",
      "model:model-cloud",
      "route:route-cloud",
    ],
  })
  const providerFallback = event("event-provider-fallback", 63, "provider_fallback_selected", {
    entityRefs: [
      "session:session-1",
      "provider:provider-edge",
      "model:model-edge",
      "route:route-edge",
    ],
  })
  const events = [
    checkpointCommitted,
    checkpointRestored,
    checkpointPending,
    ...messages,
    ...toolResults,
    memoryEvent,
    providerFailure,
    providerFallback,
  ]
  return {
    ...empty,
    revision: 10,
    tasks: {
      "task-1": entity({
        id: "task-1",
        domain: "task",
        sessionId: "session-1",
        activeNodeIds: [],
        workerIds: ["worker-1"],
        sessionIds: ["session-parent", "session-1"],
        artifactIds: ["artifact-1"],
        pendingPermissionIds: [],
        commandIds: [],
        recoveryIds: ["recovery-1"],
        lastCheckpointId: "checkpoint-2",
      }) as never,
    },
    sessions: {
      "session-parent": parent,
      "session-1": session,
    },
    memories: { "memory-1": memory },
    schedulers: { "scheduler-1": scheduler },
    workers: { "worker-1": worker },
    artifacts: {
      "artifact-1": entity({
        id: "artifact-1",
        domain: "artifact",
        digest: "a".repeat(64),
        mediaType: "application/json",
        sizeBytes: 4096,
        producerEventId: "event-memory-1",
        version: 1,
        deleted: false,
        sequence: 50,
      }) as never,
    },
    recoveries: {
      "recovery-1": entity({
        id: "recovery-1",
        domain: "recovery",
        attempt: 1,
        resumedCheckpointId: "checkpoint-1",
        strategy: "exact_resume",
        sequence: 4,
      }) as never,
    },
    causality: {
      ...empty.causality,
      byEvent: Object.fromEntries(events.map((entry) => [entry.eventId, entry])),
      byTask: { "task-1": events.map((entry) => entry.eventId) },
      byRun: { "run-1": events.map((entry) => entry.eventId) },
      bySession: { "session-1": events.map((entry) => entry.eventId) },
      byCheckpoint: {
        "checkpoint-1": ["event-checkpoint-1", "event-restore-1"],
        "checkpoint-2": ["event-checkpoint-2"],
      },
      byCorrelation: { "correlation-1": events.map((entry) => entry.eventId) },
      eventOrder: events.map((entry) => entry.eventId),
    },
    runtimes: {
      "task-1": {
        taskId: "task-1",
        generation: 1,
        firstSequence: 1,
        lastSequence: 63,
        eventCount: events.length,
        duplicateCount: 0,
        staleCount: 0,
        orphanCount: 0,
        tombstoneCount: 0,
        lastEventAtMs: Date.parse(NOW),
        pinned: true,
        connected: true,
      },
    },
    cursors: {
      "task-1": {
        taskId: "task-1",
        generation: 1,
        committedSequence: 63,
        highWatermark: 63,
        snapshotComplete: true,
        committedAtMs: Date.parse(NOW),
      },
    },
  }
}

describe("M2-S04B-01 session/context/memory/provider panels", () => {
  test("projects parent/fork/resume lineage and separates pending from committed checkpoints", () => {
    const projection = buildSessionConsoleProjection(fixture(), "task-1", "session-1")
    expect(projection.activeSessionId).toBe("session-1")
    expect(projection.lineage.find((entry) => entry.id === "session-1")?.forked).toBe(true)
    expect(projection.checkpoints.find((entry) => entry.id === "checkpoint-1")?.disposition).toBe("restored")
    expect(projection.checkpoints.find((entry) => entry.id === "checkpoint-2")?.disposition).toBe("pending")
    expect(projection.pendingWriteCount).toBeGreaterThan(0)
    expect(projection.restoreSource).toBe("recovery")
  })

  test("compact preview retains protected refs and drops/summarizes real context segments", () => {
    const state = fixture()
    const context = buildContextBudget(state, "task-1", "session-1")
    const events = (state.causality.byTask["task-1"] ?? []).map((id) => state.causality.byEvent[id]!)
    const preview = buildCompactPreview(state, context, events, {
      targetTokens: 60000,
      preserveRecentEvents: 4,
    })
    expect(context.pressure).toBe("watch")
    expect(preview.eligible).toBe(true)
    expect(preview.savedTokens).toBeGreaterThan(0)
    expect(preview.projectedTokens).toBeLessThan(preview.currentTokens)
    expect(preview.segments.some((segment) => segment.decision === "summarize" || segment.decision === "drop")).toBe(true)
    expect(preview.segments.filter((segment) => segment.kind === "checkpoint").every((segment) => segment.protected)).toBe(true)
  })

  test("memory query exposes layered provenance/veracity and changes selected context", () => {
    const state = fixture()
    const hit = buildMemoryConsoleProjection(state, "task-1", {
      text: "exact checkpoint",
      layers: ["semantic"],
    })
    const miss = buildMemoryConsoleProjection(state, "task-1", {
      text: "unrelated absent phrase",
    })
    expect(hit.rows.map((row) => row.id)).toEqual(["memory-1"])
    expect(hit.rows[0]?.veracity).toBe("verified")
    expect(hit.rows[0]?.provenanceScore).toBeGreaterThan(0)
    expect(hit.rows[0]?.sourceArtifactIds).toContain("artifact-1")
    expect(miss.rows).toHaveLength(0)
    expect(hit.fingerprint).not.toBe(miss.fingerprint)
  })

  test("provider failure builds a fallback chain without exposing credential values", () => {
    const projection = buildProviderConsoleProjection(fixture(), "task-1", {
      requiredCapabilities: ["tools"],
    })
    expect(projection.providers).toHaveLength(2)
    expect(projection.providers.every((provider) => provider.credentialPresent)).toBe(true)
    expect(projection.fallbackChain.some((route) => route.providerId === "provider-edge")).toBe(true)
    expect(JSON.stringify(projection.providers)).not.toContain("api_key")
    expect(JSON.stringify(projection.providers)).not.toContain("Bearer")
    expect(projection.candidates.some((candidate) => candidate.modelId === "model-edge")).toBe(true)
  })

  test("restricted privacy and latency constraints reject cloud and favor the device", () => {
    const projection = buildPlacementConsoleProjection(fixture(), "task-1")
    const cloud = projection.candidates.find((candidate) => candidate.profileId === "placement-cloud")
    const device = projection.candidates.find((candidate) => candidate.profileId === "placement-device")
    expect(cloud?.violations).toContain("cloud disallowed")
    expect(cloud?.violations).toContain("trusted execution required")
    expect(cloud?.violations).toContain("latency SLA exceeded")
    expect(device?.violations).toEqual([])
    expect(device?.score).toBeGreaterThan(cloud?.score ?? 1)
    expect(projection.privacyCompliant).toBe(false)
  })

  test("disconnect and disable make the real session controls unavailable", async () => {
    const state = fixture()
    let listener: (() => void) | undefined
    const projections = {
      state,
      subscribe(callback: () => void) {
        listener = callback
        return () => {
          listener = undefined
        }
      },
    }
    const commands = {
      subscribe() {
        return () => {}
      },
      getSnapshot() {
        return {}
      },
      async submit() {
        throw new Error("submit should not be reached while disconnected")
      },
    }
    const runtime = new SessionConsoleRuntime({
      projections: projections as never,
      commands: commands as never,
      online: () => true,
      now: () => new Date(NOW),
    })
    runtime.bind("task-1", "run-1", "session-1")
    runtime.disconnected("test disconnect")
    await expect(runtime.inspectContext()).rejects.toThrow("disconnected")
    runtime.reconnected()
    runtime.disable("module disabled for disconnect proof")
    await expect(runtime.queryMemory({ query: "checkpoint" })).rejects.toThrow("module disabled")
    expect(runtime.getSnapshot().projection?.lineage.length).toBe(2)
    runtime.close()
    expect(listener).toBeUndefined()
  })
})
