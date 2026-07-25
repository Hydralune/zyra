import type { CanonicalProjectionState } from "../../state/contracts.ts"
import {
  buildMemoryConsoleProjection,
  CuratorReceiptLedger,
  MemoryCuratorPlanner,
  MemoryRetrievalSession,
  type CuratorBatch,
  type MemoryConsoleProjection,
  type RetrievalPlan,
} from "../memory/index.ts"
import {
  buildPlacementConsoleProjection,
  PlacementAdmissionEngine,
  type PlacementConsoleProjection,
  type PlacementScenario,
  type WorkloadDemand,
} from "../placement/index.ts"
import {
  buildProviderConsoleProjection,
  ProviderFailoverPlanner,
  ProviderUsageLedger,
  type FailoverPlan,
  type ProviderConsoleProjection,
  type UsageWindow,
  inferProviderFailure,
} from "../providers/index.ts"
import {
  buildSessionConsoleProjection,
  type CompactPreviewOptions,
  type SessionConsoleProjection,
} from "./projection.ts"
import {
  CheckpointLedger,
  CompactReceiptReconciler,
  ContextEpochLedger,
  createResumeIntent,
  type CompactReceiptInput,
  type CompactSettlement,
  type ResumeAdmission,
} from "./checkpoint-ledger.ts"
import {
  ContextAnalyzer,
  type CompactImpact,
  type ContextAnomaly,
  type ContextTrend,
} from "./context-analyzer.ts"
import {
  SessionReconnectSupervisor,
  type ReconnectFrame,
  type ReconnectSettlement,
} from "./reconnect-supervisor.ts"
import {
  buildConsoleLinkGraph,
  type ConsoleLinkGraph,
} from "./cross-panel.ts"
import {
  fingerprint,
  text,
} from "./value.ts"

export interface SessionBehaviorSnapshot {
  taskId?: string
  session?: SessionConsoleProjection
  memory?: MemoryConsoleProjection
  providers?: ProviderConsoleProjection
  placement?: PlacementConsoleProjection
  links?: ConsoleLinkGraph
  contextTrend?: ContextTrend
  compactImpact?: CompactImpact
  contextAnomalies: readonly ContextAnomaly[]
  curatorBatch?: CuratorBatch
  retrievalPlan?: RetrievalPlan
  placementScenario?: PlacementScenario
  latestReconnect?: ReconnectSettlement
  providerUsage: readonly UsageWindow[]
  warnings: readonly string[]
  ready: boolean
  enabled: boolean
  revision: number
  fingerprint: string
}

export interface SessionBehaviorOptions {
  preview?: CompactPreviewOptions
  memoryQuery?: string
  memoryLimit?: number
  requiredCapabilities?: readonly string[]
  maximumProviderCostUsd?: number
  maximumProviderLatencyMs?: number
  workload?: Partial<WorkloadDemand>
  generation?: number
  source?: ReconnectFrame["source"]
  capturedAt?: string
}

export class SessionBehaviorCoordinator {
  readonly checkpoints = new CheckpointLedger()
  readonly epochs = new ContextEpochLedger()
  readonly compactReceipts = new CompactReceiptReconciler()
  readonly context = new ContextAnalyzer()
  readonly reconnect = new SessionReconnectSupervisor()
  readonly retrieval = new MemoryRetrievalSession()
  readonly curator = new MemoryCuratorPlanner()
  readonly curatorReceipts = new CuratorReceiptLedger()
  readonly failover = new ProviderFailoverPlanner()
  readonly usage = new ProviderUsageLedger()
  readonly admission = new PlacementAdmissionEngine()
  readonly #listeners = new Set<() => void>()
  #snapshot: SessionBehaviorSnapshot
  #lastFrame?: ReconnectFrame
  #enabled = true
  #disabledReason = "Session behavior coordinator is disabled."
  #closed = false

  constructor() {
    this.#snapshot = Object.freeze({
      contextAnomalies: Object.freeze([]),
      providerUsage: Object.freeze([]),
      warnings: Object.freeze([]),
      ready: false,
      enabled: true,
      revision: 0,
      fingerprint: fingerprint(["empty-session-behavior"]),
    })
  }

  getSnapshot = (): SessionBehaviorSnapshot => this.#snapshot

  subscribe = (listener: () => void): (() => void) => {
    if (this.#closed) return () => {}
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  update(
    state: CanonicalProjectionState,
    taskId: string,
    sessionId?: string,
    options: SessionBehaviorOptions = {},
  ): SessionBehaviorSnapshot {
    this.#assertAvailable()
    const session = buildSessionConsoleProjection(
      state,
      taskId,
      sessionId,
      options.preview,
    )
    const memory = buildMemoryConsoleProjection(state, taskId, {
      text: options.memoryQuery,
      limit: options.memoryLimit ?? 100,
      includeStale: true,
    })
    const providers = buildProviderConsoleProjection(state, taskId, {
      requiredCapabilities: options.requiredCapabilities,
      maximumCostUsd: options.maximumProviderCostUsd,
      maximumLatencyMs: options.maximumProviderLatencyMs,
    })
    const placement = buildPlacementConsoleProjection(state, taskId)
    this.checkpoints.replace(session)
    this.retrieval.replace(memory.rows)
    const taskEvents = (state.causality.byTask[taskId] ?? [])
      .map((eventId) => state.causality.byEvent[eventId])
      .filter(Boolean)
    if (session.context) {
      this.context.capture({
        budget: session.context,
        revision: session.revision,
        events: taskEvents,
        memoryCount: memory.rows.length,
        artifactCount: Object.values(state.artifacts).filter((artifact) =>
          artifact.taskId === taskId).length,
        capturedAt: options.capturedAt,
        source: options.source,
      })
      this.epochs.append({
        sessionId: session.context.sessionId,
        epoch: session.context.compactEpoch,
        boundaryId: session.context.compactBoundaryId,
        checkpointId: session.checkpoints.find((checkpoint) =>
          checkpoint.sessionId === session.context?.sessionId)?.id,
        revision: session.revision,
        tokens: session.context.used,
        source: session.restoreSource ?? (session.connected ? "live" : "snapshot"),
        eventIds: session.context.sourceEventIds,
        createdAt: options.capturedAt ?? new Date().toISOString(),
      })
    }
    const contextTrend = session.context
      ? this.context.trend(session.context.sessionId)
      : undefined
    const compactImpact = session.preview && session.context
      ? this.context.impact(session.preview, session.context.categories)
      : undefined
    const contextAnomalies = session.context
      ? this.context.anomalies(session.context.sessionId, session.preview)
      : Object.freeze([])
    const retrievalPlan = this.retrieval.plan({
      query: options.memoryQuery ?? "",
      maximumItems: options.memoryLimit ?? 50,
      maximumTokens: Math.max(
        1000,
        Math.min(
          session.context?.reserve ?? 8000,
          Math.round((session.context?.limit ?? 80000) * 0.1),
        ),
      ),
      minimumProvenance: 0,
      includeStale: false,
      includeRejected: false,
    })
    const curatorBatch = this.curator.plan(memory.rows)
    this.usage.ingest(providers, options.capturedAt)
    const providerUsage = this.usage.windows()
    const placementScenario = this.admission.evaluate(
      placement,
      {
        id: `${taskId}:${session.activeSessionId ?? "session"}`,
        memoryMb: placement.constraints.minimumMemoryMb ?? 1024,
        gpuMemoryMb: placement.constraints.minimumGpuMemoryMb ?? 0,
        sensitivity: placement.constraints.sensitivity,
        capabilities: placement.constraints.requiredCapabilities,
        checkpointable: true,
        migratable: true,
        containsSecrets: placement.constraints.sensitivity === "restricted",
        ...options.workload,
      },
      "canonical",
    )
    const links = buildConsoleLinkGraph({
      session,
      memory,
      providers,
      placement,
    })
    const frame = this.reconnect.capture(session, {
      generation: options.generation,
      capturedAt: options.capturedAt,
      source: options.source,
    })
    const latestReconnect =
      this.#lastFrame &&
      (
        frame.generation !== this.#lastFrame.generation ||
        frame.connected !== this.#lastFrame.connected ||
        frame.source !== this.#lastFrame.source
      )
        ? this.reconnect.settle(this.#lastFrame, frame)
        : this.#snapshot.latestReconnect
    this.#lastFrame = frame
    const warnings = behaviorWarnings({
      session,
      memory,
      providers,
      placement,
      contextTrend,
      compactImpact,
      contextAnomalies,
      curatorBatch,
      retrievalPlan,
      placementScenario,
      latestReconnect,
    })
    const next: SessionBehaviorSnapshot = Object.freeze({
      taskId,
      session,
      memory,
      providers,
      placement,
      links,
      contextTrend,
      compactImpact,
      contextAnomalies: Object.freeze([...contextAnomalies]),
      curatorBatch,
      retrievalPlan,
      placementScenario,
      latestReconnect,
      providerUsage: Object.freeze(providerUsage),
      warnings: Object.freeze(warnings),
      ready:
        session.ready &&
        memory.connected &&
        providers.connected &&
        placement.connected,
      enabled: true,
      revision: this.#snapshot.revision + 1,
      fingerprint: fingerprint([
        session.revision,
        memory.fingerprint,
        providers.fingerprint,
        placement.fingerprint,
        retrievalPlan.contextFingerprint,
        curatorBatch.id,
        placementScenario.id,
        links.fingerprint,
        warnings,
      ]),
    })
    this.#replace(next)
    return next
  }

  admitResume(checkpointId: string, input: {
    compactFirst?: boolean
    requestedBy?: string
    requestedAt?: string
    sealed?: boolean
  } = {}): ResumeAdmission {
    this.#assertAvailable()
    const projection = this.#snapshot.session
    if (!projection) throw new Error("Session behavior projection is unavailable.")
    const checkpoint = projection.checkpoints.find((entry) => entry.id === checkpointId)
    if (!checkpoint) throw new Error("Checkpoint is absent from session behavior projection.")
    const intent = createResumeIntent({
      checkpoint,
      projection,
      compactFirst: input.compactFirst,
      requestedBy: input.requestedBy,
      requestedAt: input.requestedAt,
      sealed: input.sealed,
    })
    return this.checkpoints.admit(intent, projection)
  }

  reconcileCompact(
    input: CompactReceiptInput,
    state: CanonicalProjectionState,
  ): CompactSettlement {
    this.#assertAvailable()
    const projection = this.#snapshot.session
    const preview = projection?.preview
    if (!projection || !preview) throw new Error("Compact projection or preview is unavailable.")
    const settlement = this.compactReceipts.reconcile(
      input,
      preview,
      projection,
      state.causality.byEvent,
    )
    if (settlement.phase === "committed" && projection.context) {
      this.context.capture({
        budget: projection.context,
        revision: projection.revision,
        memoryCount: this.#snapshot.memory?.rows.length,
        artifactCount: Object.values(state.artifacts).filter((artifact) =>
          artifact.taskId === projection.taskId).length,
        source: "compact-receipt",
      })
    }
    return settlement
  }

  planFailover(input: {
    providerId: string
    modelId?: string
    routeId?: string
    code?: string
    reason?: string
    occurredAt?: string
    sourceEventId?: string
    requiredCapabilities?: readonly string[]
  }): FailoverPlan {
    this.#assertAvailable()
    const projection = this.#snapshot.providers
    if (!projection) throw new Error("Provider behavior projection is unavailable.")
    const failure = this.failover.recordFailure(inferProviderFailure(input))
    return this.failover.plan(projection, failure, {
      requiredCapabilities: input.requiredCapabilities,
      maximumAttempts: 5,
      allowDegraded: true,
      allowRateLimited: false,
      requireCredential: true,
    })
  }

  disable(reason = "Session behavior coordinator is disabled."): void {
    if (this.#closed) return
    this.#enabled = false
    this.#disabledReason = text(reason, "Session behavior coordinator is disabled.")
    this.checkpoints.disable(reason)
    this.epochs.disable(reason)
    this.compactReceipts.disable(reason)
    this.context.disable(reason)
    this.reconnect.disable(reason)
    this.retrieval.disable(reason)
    this.curator.disable(reason)
    this.curatorReceipts.disable(reason)
    this.failover.disable(reason)
    this.usage.disable(reason)
    this.admission.disable(reason)
    this.#replace(Object.freeze({
      ...this.#snapshot,
      enabled: false,
      ready: false,
      warnings: Object.freeze([
        ...this.#snapshot.warnings,
        this.#disabledReason,
      ]),
      revision: this.#snapshot.revision + 1,
      fingerprint: fingerprint([this.#snapshot.fingerprint, this.#disabledReason]),
    }))
  }

  enable(): void {
    if (this.#closed) return
    this.#enabled = true
    this.checkpoints.enable()
    this.epochs.enable()
    this.compactReceipts.enable()
    this.context.enable()
    this.reconnect.enable()
    this.retrieval.enable()
    this.curator.enable()
    this.curatorReceipts.enable()
    this.failover.enable()
    this.usage.enable()
    this.admission.enable()
    this.#replace(Object.freeze({
      ...this.#snapshot,
      enabled: true,
      revision: this.#snapshot.revision + 1,
      fingerprint: fingerprint([this.#snapshot.fingerprint, "enabled"]),
    }))
  }

  close(reason = "Session behavior coordinator closed."): void {
    if (this.#closed) return
    if (this.#enabled) this.disable(reason)
    this.#closed = true
    this.#listeners.clear()
  }

  #replace(snapshot: SessionBehaviorSnapshot): void {
    this.#snapshot = snapshot
    for (const listener of this.#listeners) {
      try {
        listener()
      } catch {
        // Subscribers cannot alter canonical behavior coordination.
      }
    }
  }

  #assertAvailable(): void {
    if (this.#closed) throw new Error("Session behavior coordinator is closed.")
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

function behaviorWarnings(input: {
  session: SessionConsoleProjection
  memory: MemoryConsoleProjection
  providers: ProviderConsoleProjection
  placement: PlacementConsoleProjection
  contextTrend?: ContextTrend
  compactImpact?: CompactImpact
  contextAnomalies: readonly ContextAnomaly[]
  curatorBatch: CuratorBatch
  retrievalPlan: RetrievalPlan
  placementScenario: PlacementScenario
  latestReconnect?: ReconnectSettlement
}): string[] {
  const warnings: string[] = []
  if (!input.session.connected) warnings.push("session projection disconnected")
  if (!input.session.ready) warnings.push("session projection incomplete")
  if (input.session.conflictCount) warnings.push(`${input.session.conflictCount} checkpoint conflicts`)
  if (input.session.staleCount) warnings.push(`${input.session.staleCount} stale checkpoints`)
  if (input.session.pendingWriteCount) warnings.push(`${input.session.pendingWriteCount} pending checkpoint writes`)
  if (input.session.context?.pressure === "critical") warnings.push("context pressure critical")
  if (input.session.context?.pressure === "exceeded") warnings.push("context limit exceeded")
  if (input.contextTrend) warnings.push(...input.contextTrend.warnings)
  if (input.compactImpact) warnings.push(...input.compactImpact.warnings)
  for (const anomaly of input.contextAnomalies) {
    if (anomaly.severity !== "info") warnings.push(anomaly.message)
  }
  warnings.push(...input.memory.provenanceWarnings)
  warnings.push(...input.curatorBatch.warnings)
  warnings.push(...input.retrievalPlan.warnings)
  if (input.providers.secretLeakCount) warnings.push("provider projection contains secret-like material")
  if (input.providers.totalFailures) warnings.push(`${input.providers.totalFailures} provider failures`)
  if (input.providers.fallbackChain.length) warnings.push("provider fallback chain is active")
  if (!input.placement.privacyCompliant) warnings.push("placement privacy violation")
  if (!input.placement.slaCompliant) warnings.push("placement SLA violation")
  if (!input.placement.costCompliant) warnings.push("placement cost violation")
  warnings.push(...input.placementScenario.warnings)
  if (input.latestReconnect && !input.latestReconnect.safe) warnings.push("reconnect settlement is unsafe")
  return [...new Set(warnings)].sort()
}
