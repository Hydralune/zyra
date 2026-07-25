import type {
  ScenarioApi,
  ScenarioCreateInput,
  ScenarioRunProjection,
  ScenarioStatusProjection,
} from "../../api/scenario-api.ts"
import {
  canStartScenario,
  formalActionPolicy,
  validateScenarioRegistry,
} from "./admission.ts"
import {
  ScenarioProjectionStore,
  type ScenarioProjection,
} from "./projection.ts"

type Listener = () => void

export interface ScenarioRuntimeOptions {
  pollIntervalMs?: number
  activePollIntervalMs?: number
  requestTimeoutMs?: number
  maximumRuns?: number
  disabled?: boolean
  now?: () => number
}
export interface ScenarioRuntimeAudit {
  disabled: boolean
  closed: boolean
  detached: boolean
  refreshes: number
  reconnects: number
  creates: number
  starts: number
  cancels: number
  archives: number
  verifies: number
  failures: number
  staleResponses: number
  conflictingResponses: number
  backendCancellationOnClose: false
  lastError?: string
}

function bounded(
  value: number | undefined,
  minimum: number,
  maximum: number,
  fallback: number,
): number {
  if (value === undefined || !Number.isFinite(value)) return fallback
  return Math.min(maximum, Math.max(minimum, Math.floor(value)))
}

function errorMessage(value: unknown): string {
  return value instanceof Error
    ? value.message
    : String(value || "Scenario operation failed.")
}

function networkFailure(value: unknown): boolean {
  const message = errorMessage(value).toLowerCase()
  return [
    "fetch",
    "network",
    "offline",
    "connection",
    "socket",
    "timeout",
    "abort",
  ].some((marker) => message.includes(marker))
}

export class ScenarioWorkbenchRuntime {
  readonly api: ScenarioApi
  readonly store: ScenarioProjectionStore
  readonly pollIntervalMs: number
  readonly activePollIntervalMs: number
  readonly requestTimeoutMs: number
  readonly maximumRuns: number
  readonly now: () => number
  readonly disabled: boolean
  #listeners = new Set<Listener>()
  #timer?: ReturnType<typeof setTimeout>
  #abort?: AbortController
  #refresh?: Promise<ScenarioProjection>
  #generation = 0
  #closed = false
  #detached = false
  #audit: ScenarioRuntimeAudit
  #announcements: string[] = []
  #removeNetworkListeners?: () => void

  constructor(
    input: {
      api: ScenarioApi
      options?: ScenarioRuntimeOptions
    },
  ) {
    const options = input.options ?? {}
    this.api = input.api
    this.pollIntervalMs = bounded(
      options.pollIntervalMs,
      500,
      5 * 60_000,
      10_000,
    )
    this.activePollIntervalMs = bounded(
      options.activePollIntervalMs,
      250,
      this.pollIntervalMs,
      1_000,
    )
    this.requestTimeoutMs = bounded(
      options.requestTimeoutMs,
      1_000,
      5 * 60_000,
      30_000,
    )
    this.maximumRuns = bounded(
      options.maximumRuns,
      10,
      10_000,
      500,
    )
    this.now = options.now ?? Date.now
    this.disabled = options.disabled === true
    this.store = new ScenarioProjectionStore({ now: this.now })
    this.#audit = Object.freeze({
      disabled: this.disabled,
      closed: false,
      detached: false,
      refreshes: 0,
      reconnects: 0,
      creates: 0,
      starts: 0,
      cancels: 0,
      archives: 0,
      verifies: 0,
      failures: 0,
      staleResponses: 0,
      conflictingResponses: 0,
      backendCancellationOnClose: false,
    })
    this.#installNetworkListeners()
  }

  getSnapshot = (): ScenarioProjection => this.store.getSnapshot()

  subscribe = (listener: Listener): (() => void) => {
    if (this.#closed) return () => undefined
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  audit(): ScenarioRuntimeAudit {
    const projection = this.getSnapshot()
    return Object.freeze({
      ...this.#audit,
      staleResponses: projection.staleResponses,
      conflictingResponses: projection.conflictingResponses,
    })
  }

  async open(): Promise<ScenarioProjection> {
    this.#assertAvailable()
    this.#detached = false
    this.#auditUpdate({ detached: false })
    return this.refresh("open")
  }

  async refresh(
    reason: "open" | "poll" | "manual" | "reconnect" | "mutation" = "manual",
  ): Promise<ScenarioProjection> {
    this.#assertAvailable()
    if (this.#refresh) return this.#refresh
    const generation = ++this.#generation
    this.#stopTimer()
    this.#abort?.abort("Scenario refresh superseded.")
    this.#abort = new AbortController()
    if (reason === "reconnect") {
      this.store.connection("reconnecting", "Refreshing durable scenario state.")
      this.#auditUpdate({ reconnects: this.#audit.reconnects + 1 })
    } else {
      this.store.connection("loading")
    }
    const completion = this.#performRefresh(
      generation,
      reason,
      this.#abort.signal,
    )
    this.#refresh = completion
    try {
      return await completion
    } finally {
      if (this.#refresh === completion) this.#refresh = undefined
    }
  }

  async create(input: ScenarioCreateInput): Promise<ScenarioRunProjection> {
    this.#assertAvailable()
    const run = await this.#mutation("create", async () => this.api.create(input))
    this.store.observeRun(run)
    this.store.select(run.scenario_run_id)
    this.#announce(`Scenario ${run.scenario_run_id} admitted.`)
    await this.#refreshSelected(run.scenario_run_id)
    this.#changed()
    return run
  }

  async start(runId: string): Promise<ScenarioRunProjection> {
    this.#assertAvailable()
    const run = this.store.run(runId)
    if (!run) throw new TypeError("Scenario must be loaded before start.")
    const admission = canStartScenario(run)
    if (!admission.allowed) throw new TypeError(admission.reason)
    const started = await this.#mutation(
      "start",
      async () => this.api.start(runId, { wait: false }),
    )
    this.store.observeRun(started)
    this.store.select(runId)
    this.#announce(
      `Scenario ${runId} started; backend execution survives browser close.`,
    )
    this.#schedule(this.activePollIntervalMs)
    this.#changed()
    return started
  }

  async cancel(
    runId: string,
    reason: string,
  ): Promise<ScenarioRunProjection> {
    this.#assertAvailable()
    const run = this.store.run(runId)
    if (!run) throw new TypeError("Scenario must be loaded before cancellation.")
    const actions = formalActionPolicy(run)
    if (!actions.mayCancel) {
      throw new TypeError(
        actions.warning
        ?? "Scenario cannot be cancelled from its current phase.",
      )
    }
    const cancelled = await this.#mutation(
      "cancel",
      async () => this.api.cancel(runId, reason),
    )
    this.store.observeRun(cancelled)
    this.#announce(`Scenario ${runId} cancellation committed.`)
    this.#changed()
    return cancelled
  }

  async archive(
    runId: string,
    reason: string,
  ): Promise<ScenarioRunProjection> {
    this.#assertAvailable()
    const run = this.store.run(runId)
    if (!run) throw new TypeError("Scenario must be loaded before archive.")
    if (!formalActionPolicy(run).mayArchive) {
      throw new TypeError("Scenario is not a terminal unarchived run.")
    }
    const archived = await this.#mutation(
      "archive",
      async () => this.api.archive(runId, reason),
    )
    this.store.observeRun(archived)
    this.#announce(`Scenario ${runId} archived.`)
    this.#changed()
    return archived
  }

  async verify(runId: string): Promise<Readonly<Record<string, any>>> {
    this.#assertAvailable()
    const run = this.store.run(runId)
    if (!run) throw new TypeError("Scenario must be loaded before verification.")
    if (!formalActionPolicy(run).mayVerify) {
      throw new TypeError("Scenario evidence is not ready for re-verification.")
    }
    const response = await this.#mutation(
      "verify",
      async () => this.api.verify(runId),
    )
    await this.#refreshSelected(runId)
    this.#announce(`Scenario ${runId} evidence re-verified.`)
    this.#changed()
    return response
  }

  async select(runId?: string): Promise<void> {
    this.#assertAvailable()
    this.store.select(runId)
    this.#changed()
    if (runId) await this.#refreshSelected(runId)
  }

  detach(reason = "Scenario panel detached."): void {
    if (this.#closed || this.#detached) return
    this.#detached = true
    this.#stopTimer()
    this.#abort?.abort(reason)
    this.#abort = undefined
    this.#auditUpdate({ detached: true })
    this.#announce(
      `${reason} Backend scenario execution continues; no cancel was sent.`,
    )
    this.#listeners.clear()
  }

  attach(): void {
    if (this.#closed || !this.#detached) return
    this.#detached = false
    this.#auditUpdate({ detached: false })
    void this.refresh("reconnect")
  }

  close(reason = "Scenario workbench closed."): void {
    if (this.#closed) return
    this.#closed = true
    this.#detached = true
    this.#generation += 1
    this.#stopTimer()
    this.#abort?.abort(reason)
    this.#abort = undefined
    this.#removeNetworkListeners?.()
    this.#removeNetworkListeners = undefined
    this.#listeners.clear()
    this.store.close(
      `${reason} Backend execution remains durable; no cancellation was sent.`,
    )
    this.#auditUpdate({ closed: true, detached: true })
  }

  takeAnnouncements(): readonly string[] {
    if (!this.#announcements.length) return Object.freeze([])
    const values = Object.freeze([...this.#announcements])
    this.#announcements = []
    return values
  }

  async #performRefresh(
    generation: number,
    reason: string,
    signal: AbortSignal,
  ): Promise<ScenarioProjection> {
    try {
      const [registry, page] = await Promise.all([
        this.api.registry({
          signal,
          timeoutMs: this.requestTimeoutMs,
        }),
        this.api.list({
          includeArchived: false,
          limit: this.maximumRuns,
          offset: 0,
          signal,
          timeoutMs: this.requestTimeoutMs,
        }),
      ])
      if (generation !== this.#generation || this.#closed || this.#detached) {
        return this.getSnapshot()
      }
      const registryFindings = validateScenarioRegistry(registry)
      if (registryFindings.some((item) => item.severity === "error")) {
        throw Object.assign(
          new Error(registryFindings[0]?.message ?? "Scenario registry is invalid."),
          { code: registryFindings[0]?.code },
        )
      }
      this.store.registry(registry)
      this.store.replaceRuns(page.runs)
      const selected = this.getSnapshot().selectedRunId
      if (selected) await this.#refreshSelected(selected, signal)
      if (generation !== this.#generation || this.#closed || this.#detached) {
        return this.getSnapshot()
      }
      const active = this.getSnapshot().activeCount > 0
      const delay = active ? this.activePollIntervalMs : this.pollIntervalMs
      this.store.refreshed(this.now() + delay)
      this.#auditUpdate({
        refreshes: this.#audit.refreshes + 1,
        lastError: undefined,
      })
      this.#schedule(delay)
      if (reason === "reconnect") {
        this.#announce("Scenario control plane reconnected to durable state.")
      }
      this.#changed()
      return this.getSnapshot()
    } catch (error) {
      if (signal.aborted || generation !== this.#generation) {
        return this.getSnapshot()
      }
      const offline = networkFailure(error)
      this.store.connection(
        offline ? "offline" : "reconnecting",
        errorMessage(error),
      )
      this.#auditUpdate({
        failures: this.#audit.failures + 1,
        lastError: errorMessage(error),
      })
      this.#schedule(
        Math.min(60_000, this.activePollIntervalMs * 2 ** Math.min(6, this.#audit.failures)),
      )
      this.#changed()
      return this.getSnapshot()
    }
  }

  async #refreshSelected(
    runId: string,
    signal?: AbortSignal,
  ): Promise<ScenarioStatusProjection | undefined> {
    try {
      const status = await this.api.get(runId, {
        signal,
        timeoutMs: this.requestTimeoutMs,
      })
      this.store.observeStatus(status)
      return status
    } catch (error) {
      if (signal?.aborted) return undefined
      throw error
    }
  }

  async #mutation<T>(
    operation: "create" | "start" | "cancel" | "archive" | "verify",
    execute: () => Promise<T>,
  ): Promise<T> {
    this.#assertAvailable()
    try {
      const result = await execute()
      this.#auditUpdate({
        [operation === "create" ? "creates"
          : operation === "start" ? "starts"
          : operation === "cancel" ? "cancels"
          : operation === "archive" ? "archives"
          : "verifies"]:
            this.#audit[
              operation === "create" ? "creates"
              : operation === "start" ? "starts"
              : operation === "cancel" ? "cancels"
              : operation === "archive" ? "archives"
              : "verifies"
            ] + 1,
        lastError: undefined,
      })
      return result
    } catch (error) {
      this.#auditUpdate({
        failures: this.#audit.failures + 1,
        lastError: errorMessage(error),
      })
      this.#announce(`${operation} failed: ${errorMessage(error)}`)
      this.#changed()
      throw error
    }
  }

  #schedule(delay: number): void {
    this.#stopTimer()
    if (this.#closed || this.#detached) return
    const selected = Math.max(100, delay)
    this.store.schedule(this.now() + selected)
    this.#timer = setTimeout(() => {
      this.#timer = undefined
      if (this.#closed || this.#detached) return
      void this.refresh(
        this.getSnapshot().connection === "online" ? "poll" : "reconnect",
      )
    }, selected)
  }

  #stopTimer(): void {
    if (this.#timer !== undefined) clearTimeout(this.#timer)
    this.#timer = undefined
  }

  #installNetworkListeners(): void {
    if (typeof window === "undefined") return
    const online = () => {
      if (this.#closed || this.#detached) return
      void this.refresh("reconnect")
    }
    const offline = () => {
      if (this.#closed) return
      this.#stopTimer()
      this.store.connection(
        "offline",
        "Browser is offline; backend scenario state remains durable.",
      )
      this.#changed()
    }
    window.addEventListener("online", online)
    window.addEventListener("offline", offline)
    this.#removeNetworkListeners = () => {
      window.removeEventListener("online", online)
      window.removeEventListener("offline", offline)
    }
  }

  #announce(message: string): void {
    const selected = message.trim()
    if (!selected || this.#announcements.at(-1) === selected) return
    this.#announcements.push(selected)
    if (this.#announcements.length > 100) {
      this.#announcements.splice(0, this.#announcements.length - 100)
    }
  }

  #auditUpdate(patch: Partial<ScenarioRuntimeAudit>): void {
    this.#audit = Object.freeze({ ...this.#audit, ...patch })
  }

  #changed(): void {
    if (this.#detached) return
    for (const listener of this.#listeners) listener()
  }

  #assertAvailable(): void {
    if (this.#closed) throw new TypeError("Scenario workbench is closed.")
    if (this.disabled) {
      throw new TypeError(
        "Scenario workbench is disabled; no demo or replay fallback is available.",
      )
    }
  }
}
