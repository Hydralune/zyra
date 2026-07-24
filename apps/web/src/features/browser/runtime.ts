import type { TaskApi } from "../../api/task-api.ts"
import type {
  ArtifactProjection,
  CausalEventProjection,
  RecoveryProjection,
} from "../../state/contracts.ts"
import {
  parseArtifactCatalogPage,
  type ArtifactContract,
} from "../artifacts/contracts.ts"
import {
  BrowserControlAction,
  BrowserViewerPhase,
  defaultBrowserViewerFilters,
  defaultBrowserViewerWindow,
  emptyBrowserViewerProjection,
  parseBrowserObservabilityEnvelope,
  type BrowserControlActionValue,
  type BrowserControlReceipt,
  type BrowserObservabilityEnvelope,
  type BrowserViewerFilters,
  type BrowserViewerSelection,
  type BrowserViewerState,
} from "./contracts.ts"
import {
  BrowserControlRuntime,
  taskApiBrowserControlTransport,
  type BrowserControlRuntimeSnapshot,
} from "./control/runtime.ts"
import { browserControlDecision } from "./control/policy.ts"
import {
  BrowserHistoryNavigator,
  type BrowserHistoryResult,
} from "./history/navigator.ts"
import { BrowserHistoryVirtualizer } from "./history/virtualizer.ts"
import {
  browserProjectionChanged,
  projectBrowserViewer,
  selectedBrowserSession,
} from "./projection/projector.ts"

type BrowserObservabilityView = NonNullable<
  Parameters<TaskApi["browserObservability"]>[1]
>["view"]

const DEFAULT_VIEWS: readonly BrowserObservabilityView[] = Object.freeze([
  "summary",
  "history",
  "trace",
  "health",
  "artifacts",
  "trajectory",
  "commits",
])

export interface BrowserViewerRuntimeOptions {
  api: Pick<
    TaskApi,
    "browserObservability" | "browserControl" | "artifactCatalog"
  >
  taskId: string
  runId: string
  sealed?: boolean
  actorId?: string
  views?: readonly BrowserObservabilityView[]
  observabilityLimit?: number
  artifactLimit?: number
  refreshIntervalMs?: number
  now?: () => number
}

export interface BrowserCanonicalUpdate {
  events: readonly CausalEventProjection[]
  artifacts: readonly ArtifactProjection[]
  recoveries?: readonly RecoveryProjection[]
  projectionRevision: number
}

type BrowserViewerListener = (state: BrowserViewerState) => void

interface RefreshBatch {
  generation: number
  controller: AbortController
  startedAt: number
  promise: Promise<BrowserViewerState>
}

function boundedInteger(
  value: number | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  const candidate = Number(value)
  if (!Number.isSafeInteger(candidate)) return fallback
  return Math.max(minimum, Math.min(maximum, candidate))
}

function initialState(taskId: string, runId: string): BrowserViewerState {
  return Object.freeze({
    projection: emptyBrowserViewerProjection(
      taskId,
      runId,
      BrowserViewerPhase.IDLE,
    ),
    selection: Object.freeze({ followLive: true }),
    filters: defaultBrowserViewerFilters(),
    window: defaultBrowserViewerWindow(),
    controls: Object.freeze([]),
    activeControlIds: Object.freeze([]),
    online: true,
    revision: 0,
  })
}

function failedState(
  previous: BrowserViewerState,
  error: unknown,
  now: number,
): BrowserViewerState {
  const source =
    error && typeof error === "object"
      ? error as Readonly<Record<string, unknown>>
      : {}
  const message = error instanceof Error ? error.message : String(error)
  const disconnected =
    /network|fetch|offline|disconnect/i.test(
      `${source.code ?? ""} ${message}`,
    )
  return Object.freeze({
    ...previous,
    projection: Object.freeze({
      ...previous.projection,
      phase: disconnected
        ? BrowserViewerPhase.DISCONNECTED
        : BrowserViewerPhase.FAILED,
      refreshedAt: now,
    }),
    online: !disconnected,
    error: Object.freeze({
      code: String(
        source.code
        ?? (disconnected
          ? "browser_viewer_disconnected"
          : "browser_viewer_refresh_failed"),
      ),
      message,
      retryable: true,
      occurredAt: now,
    }),
    revision: previous.revision + 1,
  })
}

export class BrowserViewerRuntime {
  readonly #api: BrowserViewerRuntimeOptions["api"]
  readonly #taskId: string
  readonly #runId: string
  readonly #sealed: boolean
  readonly #actorId: string
  readonly #views: readonly BrowserObservabilityView[]
  readonly #observabilityLimit: number
  readonly #artifactLimit: number
  readonly #refreshIntervalMs: number
  readonly #now: () => number
  readonly #listeners = new Set<BrowserViewerListener>()
  readonly #controls: BrowserControlRuntime
  readonly #virtualizer = new BrowserHistoryVirtualizer()
  #controlUnsubscribe?: () => void
  #navigator: BrowserHistoryNavigator
  #canonical: BrowserCanonicalUpdate = {
    events: Object.freeze([]),
    artifacts: Object.freeze([]),
    recoveries: Object.freeze([]),
    projectionRevision: 0,
  }
  #envelopes: readonly BrowserObservabilityEnvelope[] = Object.freeze([])
  #artifactContracts: readonly ArtifactContract[] = Object.freeze([])
  #state: BrowserViewerState
  #refresh?: RefreshBatch
  #refreshTimer?: ReturnType<typeof setTimeout>
  #generation = 0
  #closed = false
  #disabled = false

  constructor(options: BrowserViewerRuntimeOptions) {
    this.#api = options.api
    this.#taskId = options.taskId
    this.#runId = options.runId
    this.#sealed = options.sealed === true
    this.#actorId = options.actorId ?? "zyra-web-browser"
    this.#views = Object.freeze([
      ...new Set(options.views ?? DEFAULT_VIEWS),
    ])
    this.#observabilityLimit = boundedInteger(
      options.observabilityLimit,
      5_000,
      1,
      5_000,
    )
    this.#artifactLimit = boundedInteger(
      options.artifactLimit,
      500,
      1,
      1_000,
    )
    this.#refreshIntervalMs = boundedInteger(
      options.refreshIntervalMs,
      5_000,
      0,
      300_000,
    )
    this.#now = options.now ?? Date.now
    this.#state = initialState(this.#taskId, this.#runId)
    this.#navigator = new BrowserHistoryNavigator(
      this.#state.projection,
      this.#state.selection,
      this.#state.filters,
    )
    this.#controls = new BrowserControlRuntime({
      transport: taskApiBrowserControlTransport(this.#api),
      now: this.#now,
    })
    this.#controlUnsubscribe = this.#controls.subscribe((snapshot) => {
      this.#applyControlSnapshot(snapshot)
    })
  }

  get state(): BrowserViewerState {
    return this.#state
  }

  get navigation(): BrowserHistoryNavigator {
    return this.#navigator
  }

  get history(): BrowserHistoryResult {
    return this.#navigator.result
  }

  get controlRuntime(): BrowserControlRuntime {
    return this.#controls
  }

  getSnapshot = (): BrowserViewerState => this.#state

  subscribe = (listener: BrowserViewerListener): (() => void) => {
    this.#assertOpen()
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  updateCanonical(update: BrowserCanonicalUpdate): BrowserViewerState {
    this.#assertOpen()
    if (
      update.events.some(
        (event) => event.taskId !== this.#taskId || event.runId !== this.#runId,
      )
    ) throw new TypeError("Browser canonical events crossed task/run ownership.")
    if (update.artifacts.some((artifact) => artifact.taskId !== this.#taskId)) {
      throw new TypeError("Browser canonical artifacts crossed task ownership.")
    }
    this.#canonical = {
      events: Object.freeze([...update.events]),
      artifacts: Object.freeze([...update.artifacts]),
      recoveries: Object.freeze([...(update.recoveries ?? [])]),
      projectionRevision: update.projectionRevision,
    }
    if (this.#envelopes.length) this.#reproject()
    return this.#state
  }

  refresh(
    options: { force?: boolean; reason?: string } = {},
  ): Promise<BrowserViewerState> {
    this.#assertOpen()
    if (this.#disabled) {
      return Promise.reject(
        Object.assign(new Error("Browser viewer runtime is disabled."), {
          code: "browser_viewer_disabled",
        }),
      )
    }
    if (this.#refresh && !options.force) return this.#refresh.promise
    if (this.#refresh && options.force) {
      this.#refresh.controller.abort(
        Object.assign(new Error("Browser viewer refresh superseded."), {
          code: "browser_viewer_refresh_superseded",
        }),
      )
    }
    const generation = ++this.#generation
    const controller = new AbortController()
    const promise = this.#refreshBatch(
      generation,
      controller,
      options.reason ?? "refresh",
    )
    this.#refresh = {
      generation,
      controller,
      startedAt: this.#now(),
      promise,
    }
    this.#commit({
      projection: Object.freeze({
        ...this.#state.projection,
        phase: BrowserViewerPhase.LOADING,
      }),
      error: undefined,
    })
    return promise
  }

  start(): Promise<BrowserViewerState> {
    const result = this.refresh({ reason: "viewer_started" })
    this.#scheduleRefresh()
    return result
  }

  setOnline(online: boolean): void {
    if (this.#closed || this.#state.online === online) return
    this.#commit({ online })
    if (online) {
      void this.refresh({ force: true, reason: "network_reconnected" })
    } else {
      this.#refresh?.controller.abort(
        Object.assign(new Error("Browser viewer is offline."), {
          code: "browser_viewer_offline",
        }),
      )
      this.#commit({
        projection: Object.freeze({
          ...this.#state.projection,
          phase: BrowserViewerPhase.DISCONNECTED,
        }),
      })
    }
  }

  setFilters(patch: Partial<BrowserViewerFilters>): BrowserViewerState {
    this.#navigator.setFilters(patch)
    this.#virtualizer.setSteps(this.#navigator.result.steps)
    this.#commit({
      filters: this.#navigator.filters,
      selection: this.#navigator.selection,
      window: this.#virtualizer.window(0, 720),
    })
    return this.#state
  }

  clearFilters(): BrowserViewerState {
    this.#navigator.clearFilters()
    this.#virtualizer.setSteps(this.#navigator.result.steps)
    this.#commit({
      filters: this.#navigator.filters,
      selection: this.#navigator.selection,
      window: this.#virtualizer.window(0, 720),
    })
    return this.#state
  }

  selectSession(sessionId: string): BrowserViewerSelection {
    const selection = this.#navigator.selectSession(sessionId)
    this.#selectionChanged(selection)
    return selection
  }

  selectStep(stepId: string, actionId?: string): BrowserViewerSelection {
    const selection = this.#navigator.selectStep(stepId, actionId)
    this.#selectionChanged(selection)
    return selection
  }

  selectAction(actionId: string): BrowserViewerSelection {
    const selection = this.#navigator.selectAction(actionId)
    this.#selectionChanged(selection)
    return selection
  }

  selectArtifact(artifactId: string): BrowserViewerSelection {
    const selection = this.#navigator.selectArtifact(artifactId)
    this.#selectionChanged(selection)
    return selection
  }

  nextStep(): BrowserViewerSelection {
    const selection = this.#navigator.next()
    this.#selectionChanged(selection)
    return selection
  }

  previousStep(): BrowserViewerSelection {
    const selection = this.#navigator.previous()
    this.#selectionChanged(selection)
    return selection
  }

  followLive(enabled = true): BrowserViewerSelection {
    const selection = this.#navigator.followLive(enabled)
    this.#selectionChanged(selection)
    return selection
  }

  updateWindow(scrollTop: number, viewportHeight: number): void {
    this.#commit({
      window: this.#virtualizer.window(scrollTop, viewportHeight),
    })
  }

  measureStep(stepId: string, height: number): boolean {
    const changed = this.#virtualizer.measure(stepId, height)
    if (changed) {
      this.#commit({
        window: this.#virtualizer.window(
          this.#state.window.beforeHeight,
          Math.max(1, this.#state.window.itemHeight * 10),
        ),
      })
    }
    return changed
  }

  async control(input: {
    action: BrowserControlActionValue
    url?: string
    reason?: string
    expectedGeneration?: number
    expectedTaskRevision?: number
    timeoutMs?: number
  }): Promise<BrowserControlReceipt> {
    this.#assertOpen()
    if (this.#disabled) {
      throw Object.assign(new Error("Browser viewer runtime is disabled."), {
        code: "browser_viewer_disabled",
      })
    }
    const session = selectedBrowserSession(this.#state.projection)
    if (!session) {
      throw Object.assign(new Error("No browser session is selected."), {
        code: "browser_control_session_missing",
      })
    }
    const decision = browserControlDecision({
      action: input.action,
      session,
      taskId: this.#taskId,
      runId: this.#runId,
      actorId: this.#actorId,
      sealed: this.#sealed,
      url: input.url,
      stepId: this.#state.selection.stepId,
      actionId: this.#state.selection.actionId,
      expectedGeneration: input.expectedGeneration,
      expectedTaskRevision:
        input.expectedTaskRevision ?? this.#canonical.projectionRevision,
      reason: input.reason,
      timeoutMs: input.timeoutMs,
      now: this.#now(),
    })
    if (!decision.allowed || !decision.request) {
      throw Object.assign(
        new Error(decision.reason ?? "Browser control rejected."),
        { code: decision.code ?? "browser_control_rejected" },
      )
    }
    const receipt = await this.#controls.submit(decision.request)
    if (
      receipt.phase === "applied"
      || receipt.phase === "observed"
      || receipt.phase === "denied"
    ) {
      await this.refresh({
        force: true,
        reason: `control_${input.action}_${receipt.phase}`,
      })
    }
    return receipt
  }

  navigate(url: string): Promise<BrowserControlReceipt> {
    return this.control({ action: BrowserControlAction.NAVIGATE, url })
  }

  stop(reason?: string): Promise<BrowserControlReceipt> {
    return this.control({ action: BrowserControlAction.STOP, reason })
  }

  retry(reason?: string): Promise<BrowserControlReceipt> {
    return this.control({ action: BrowserControlAction.RETRY, reason })
  }

  inspect(reason?: string): Promise<BrowserControlReceipt> {
    return this.control({ action: BrowserControlAction.INSPECT, reason })
  }

  disable(reason = "Browser viewer disabled."): void {
    if (this.#disabled || this.#closed) return
    this.#disabled = true
    this.#refresh?.controller.abort(
      Object.assign(new Error(reason), { code: "browser_viewer_disabled" }),
    )
    this.#controls.disable(reason)
    if (this.#refreshTimer) clearTimeout(this.#refreshTimer)
    this.#refreshTimer = undefined
    this.#commit({
      projection: Object.freeze({
        ...this.#state.projection,
        phase: BrowserViewerPhase.FAILED,
      }),
      error: Object.freeze({
        code: "browser_viewer_disabled",
        message: reason,
        retryable: false,
        occurredAt: this.#now(),
      }),
    })
  }

  close(reason = "Browser viewer closed."): void {
    if (this.#closed) return
    this.#closed = true
    if (this.#refreshTimer) clearTimeout(this.#refreshTimer)
    this.#refreshTimer = undefined
    this.#refresh?.controller.abort(
      Object.assign(new Error(reason), {
        code: "browser_viewer_closed",
      }),
    )
    this.#refresh = undefined
    this.#controlUnsubscribe?.()
    this.#controlUnsubscribe = undefined
    this.#controls.close(reason)
    this.#state = Object.freeze({
      ...this.#state,
      projection: Object.freeze({
        ...this.#state.projection,
        phase: BrowserViewerPhase.CLOSED,
      }),
      activeControlIds: Object.freeze([]),
      revision: this.#state.revision + 1,
    })
    for (const listener of this.#listeners) listener(this.#state)
    this.#listeners.clear()
  }

  diagnostics(): Readonly<Record<string, unknown>> {
    return Object.freeze({
      runtime_id: "zyra-browser-viewer-runtime",
      task_id: this.#taskId,
      run_id: this.#runId,
      sealed: this.#sealed,
      closed: this.#closed,
      disabled: this.#disabled,
      generation: this.#generation,
      refresh_active: Boolean(this.#refresh),
      envelope_views: this.#envelopes.map((envelope) => envelope.view),
      artifact_contracts: this.#artifactContracts.length,
      canonical_events: this.#canonical.events.length,
      canonical_artifacts: this.#canonical.artifacts.length,
      canonical_projection_revision: this.#canonical.projectionRevision,
      history: {
        visible_steps: this.#navigator.result.steps.length,
        hidden_steps: this.#navigator.result.hiddenCount,
      },
      virtualizer: this.#virtualizer.snapshot(),
      controls: this.#controls.getSnapshot(),
      owns_browser_state: false,
      owns_artifact_state: false,
      owns_canonical_events: false,
    })
  }

  async #refreshBatch(
    generation: number,
    controller: AbortController,
    reason: string,
  ): Promise<BrowserViewerState> {
    try {
      const [envelopes, catalog] = await Promise.all([
        Promise.all(
          this.#views.map(async (view) => {
            const value = await this.#api.browserObservability(this.#taskId, {
              view,
              limit: this.#observabilityLimit,
              signal: controller.signal,
              timeoutMs: 30_000,
            })
            return parseBrowserObservabilityEnvelope(value, this.#taskId)
          }),
        ),
        this.#api.artifactCatalog(this.#taskId, {
          limit: this.#artifactLimit,
          signal: controller.signal,
          timeoutMs: 30_000,
        }),
      ])
      if (
        this.#closed
        || generation !== this.#generation
        || controller.signal.aborted
      ) return this.#state
      this.#envelopes = Object.freeze(envelopes)
      this.#artifactContracts = parseArtifactCatalogPage(catalog).artifacts
      this.#reproject()
      this.#refresh = undefined
      this.#scheduleRefresh()
      return this.#state
    } catch (error) {
      if (
        this.#closed
        || generation !== this.#generation
        || controller.signal.aborted
      ) return this.#state
      this.#refresh = undefined
      this.#state = failedState(this.#state, error, this.#now())
      this.#notify()
      this.#scheduleRefresh()
      return this.#state
    }
  }

  #reproject(): void {
    const projection = projectBrowserViewer(
      {
        taskId: this.#taskId,
        runId: this.#runId,
        envelopes: this.#envelopes,
        events: this.#canonical.events,
        artifacts: this.#canonical.artifacts,
        projectionRevision: this.#canonical.projectionRevision,
        refreshedAt: this.#now(),
      },
      {
        artifactContracts: this.#artifactContracts,
        recoveries: this.#canonical.recoveries,
        selectedSessionId: this.#state.selection.sessionId,
        selectedStepId: this.#state.selection.stepId,
        selectedActionId: this.#state.selection.actionId,
      },
    )
    const changed = browserProjectionChanged(
      this.#state.projection,
      projection,
    )
    const selection = this.#navigator.update(projection)
    this.#virtualizer.setSteps(this.#navigator.result.steps)
    this.#commit({
      projection,
      selection,
      filters: this.#navigator.filters,
      window: this.#virtualizer.window(
        changed && selection.followLive
          ? this.#virtualizer.totalHeight
          : this.#state.window.beforeHeight,
        Math.max(720, this.#state.window.itemHeight * 10),
      ),
      online: true,
      error: undefined,
    })
  }

  #selectionChanged(selection: BrowserViewerSelection): void {
    this.#commit({ selection })
  }

  #applyControlSnapshot(snapshot: BrowserControlRuntimeSnapshot): void {
    if (this.#closed) return
    const controls = Object.freeze([
      ...snapshot.settled,
      ...snapshot.pending,
    ].sort(
      (left, right) =>
        Math.max(left.completedAt, left.submittedAt)
        - Math.max(right.completedAt, right.submittedAt),
    ))
    this.#commit({
      controls,
      activeControlIds: Object.freeze(
        snapshot.pending.map((receipt) => receipt.commandId),
      ),
    })
  }

  #scheduleRefresh(): void {
    if (
      this.#closed
      || this.#disabled
      || this.#refreshIntervalMs === 0
      || !this.#state.online
    ) return
    if (this.#refreshTimer) clearTimeout(this.#refreshTimer)
    this.#refreshTimer = setTimeout(() => {
      this.#refreshTimer = undefined
      void this.refresh({ reason: "poll_interval" })
    }, this.#refreshIntervalMs)
  }

  #commit(patch: Partial<BrowserViewerState>): void {
    if (this.#closed) return
    this.#state = Object.freeze({
      ...this.#state,
      ...patch,
      revision: this.#state.revision + 1,
    })
    this.#notify()
  }

  #notify(): void {
    for (const listener of this.#listeners) listener(this.#state)
  }

  #assertOpen(): void {
    if (this.#closed) {
      throw Object.assign(new Error("Browser viewer runtime is closed."), {
        code: "browser_viewer_closed",
      })
    }
  }
}
