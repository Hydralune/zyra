import type {
  ExperimentApi,
  ExperimentCreateInput,
  ExperimentRunProjection,
  ExperimentStatusProjection,
} from "../../api/experiment-api.ts"
import type { PolicyApi } from "../../api/policy-api.ts"
import { ExperimentProjectionStore, type ExperimentProjection } from "./projection.ts"

type Listener = () => void

export interface ExperimentRuntimeAudit {
  closed: boolean
  detached: boolean
  refreshes: number
  creates: number
  starts: number
  verifies: number
  archives: number
  failures: number
  backendCancellationOnClose: false
  lastError?: string
}

function errorMessage(value: unknown): string {
  return value instanceof Error
    ? value.message
    : String(value || "Experiment operation failed.")
}

function networkFailure(value: unknown): boolean {
  const message = errorMessage(value).toLowerCase()
  return ["fetch", "network", "offline", "connection", "timeout", "abort"]
    .some((item) => message.includes(item))
}

export class ExperimentWorkbenchRuntime {
  readonly api: ExperimentApi
  readonly policyApi?: PolicyApi
  readonly store = new ExperimentProjectionStore()
  readonly pollIntervalMs: number
  readonly activePollIntervalMs: number
  #listeners = new Set<Listener>()
  #timer?: ReturnType<typeof setTimeout>
  #abort?: AbortController
  #generation = 0
  #closed = false
  #detached = false
  #audit: ExperimentRuntimeAudit = Object.freeze({
    closed: false,
    detached: false,
    refreshes: 0,
    creates: 0,
    starts: 0,
    verifies: 0,
    archives: 0,
    failures: 0,
    backendCancellationOnClose: false,
  })

  constructor(input: {
    api: ExperimentApi
    policyApi?: PolicyApi
    pollIntervalMs?: number
    activePollIntervalMs?: number
  }) {
    this.api = input.api
    this.policyApi = input.policyApi
    this.pollIntervalMs = Math.max(1_000, input.pollIntervalMs ?? 15_000)
    this.activePollIntervalMs = Math.max(
      500,
      input.activePollIntervalMs ?? 1_500,
    )
  }

  getSnapshot = (): ExperimentProjection => this.store.getSnapshot()

  subscribe = (listener: Listener): (() => void) => {
    if (this.#closed) return () => undefined
    this.#listeners.add(listener)
    return () => this.#listeners.delete(listener)
  }

  audit(): ExperimentRuntimeAudit {
    return this.#audit
  }

  async open(): Promise<ExperimentProjection> {
    this.#assertOpen()
    this.#detached = false
    this.#auditUpdate({ detached: false })
    return this.refresh("open")
  }

  async refresh(
    reason: "open" | "poll" | "manual" | "mutation" | "reconnect" = "manual",
  ): Promise<ExperimentProjection> {
    this.#assertOpen()
    const generation = ++this.#generation
    this.#stopTimer()
    this.#abort?.abort("Experiment refresh superseded.")
    this.#abort = new AbortController()
    this.store.connection(
      reason === "reconnect" ? "reconnecting" : "loading",
      reason,
    )
    try {
      const [registry, page] = await Promise.all([
        this.api.registry({ signal: this.#abort.signal }),
        this.api.list({
          includeArchived: true,
          limit: 500,
          signal: this.#abort.signal,
        }),
      ])
      if (generation !== this.#generation || this.#closed) return this.getSnapshot()
      this.store.registry(registry)
      this.store.replaceRuns(page.runs)
      const selectedId =
        this.getSnapshot().selectedRunId
        ?? page.runs[0]?.experiment_id
      if (selectedId) {
        this.store.select(selectedId)
        await this.#refreshSelected(selectedId, this.#abort.signal)
      }
      this.store.connection("online")
      this.#auditUpdate({ refreshes: this.#audit.refreshes + 1, lastError: undefined })
    } catch (error) {
      if (generation !== this.#generation || this.#closed) return this.getSnapshot()
      this.store.connection(
        networkFailure(error) ? "offline" : "online",
        errorMessage(error),
      )
      this.#auditUpdate({
        failures: this.#audit.failures + 1,
        lastError: errorMessage(error),
      })
      throw error
    } finally {
      this.#abort = undefined
      if (!this.#closed && !this.#detached) {
        const active = this.getSnapshot().activeCount > 0
        this.#schedule(active ? this.activePollIntervalMs : this.pollIntervalMs)
      }
      this.#changed()
    }
    return this.getSnapshot()
  }

  async select(experimentId: string): Promise<ExperimentStatusProjection> {
    this.#assertOpen()
    this.store.select(experimentId)
    const status = await this.#refreshSelected(experimentId)
    this.#changed()
    return status
  }

  async create(input: ExperimentCreateInput): Promise<ExperimentRunProjection> {
    this.#assertOpen()
    const run = await this.api.create(input)
    this.store.observeRun(run)
    this.store.select(run.experiment_id)
    this.#auditUpdate({ creates: this.#audit.creates + 1 })
    await this.#refreshSelected(run.experiment_id)
    this.#schedule(this.activePollIntervalMs)
    this.#changed()
    return run
  }

  async start(experimentId: string): Promise<ExperimentRunProjection> {
    this.#assertOpen()
    const run = await this.api.start(experimentId, { wait: false })
    this.store.observeRun(run)
    this.#auditUpdate({ starts: this.#audit.starts + 1 })
    this.#schedule(this.activePollIntervalMs)
    this.#changed()
    return run
  }

  async verify(experimentId: string): Promise<Readonly<Record<string, any>>> {
    this.#assertOpen()
    const receipt = await this.api.verify(experimentId)
    this.#auditUpdate({ verifies: this.#audit.verifies + 1 })
    await this.#refreshSelected(experimentId)
    this.#changed()
    return receipt
  }

  async archive(experimentId: string): Promise<ExperimentRunProjection> {
    this.#assertOpen()
    const run = await this.api.archive(
      experimentId,
      "Archived from the experiment evidence workbench.",
    )
    this.store.observeRun(run)
    this.#auditUpdate({ archives: this.#audit.archives + 1 })
    this.#changed()
    return run
  }

  detach(reason = "Experiment panel detached."): void {
    if (this.#closed) return
    this.#detached = true
    this.#generation += 1
    this.#stopTimer()
    this.#abort?.abort(reason)
    this.#abort = undefined
    this.#auditUpdate({ detached: true })
  }

  close(reason = "Experiment runtime closed."): void {
    if (this.#closed) return
    this.detach(reason)
    this.#closed = true
    this.store.connection("closed", reason)
    this.#auditUpdate({ closed: true })
    this.#listeners.clear()
  }

  async #refreshSelected(
    experimentId: string,
    signal?: AbortSignal,
  ): Promise<ExperimentStatusProjection> {
    const status = await this.api.get(experimentId, { signal })
    this.store.observeStatus(status)
    if (status.run.phase === "succeeded") {
      const [report, bundle, requirements, samples] = await Promise.all([
        this.api.report(experimentId, { signal }),
        this.api.bundle(experimentId, { signal }),
        this.api.requirements(experimentId, { signal }),
        this.api.samples(experimentId, { limit: 10_000, signal }),
      ])
      if (this.getSnapshot().selectedRunId === experimentId) {
        this.store.report(report)
        this.store.bundle(bundle)
        this.store.requirements(requirements)
        this.store.samples(samples)
      }
    }
    return status
  }

  #schedule(delay: number): void {
    this.#stopTimer()
    if (this.#closed || this.#detached) return
    const at = Date.now() + delay
    this.store.nextPoll(at)
    this.#timer = setTimeout(() => {
      this.#timer = undefined
      void this.refresh("poll").catch(() => {
        if (!this.#closed && !this.#detached) this.#schedule(this.pollIntervalMs)
      })
    }, delay)
  }

  #stopTimer(): void {
    if (this.#timer) clearTimeout(this.#timer)
    this.#timer = undefined
    this.store.nextPoll(undefined)
  }

  #assertOpen(): void {
    if (this.#closed) throw new TypeError("Experiment workbench is closed.")
  }

  #auditUpdate(patch: Partial<ExperimentRuntimeAudit>): void {
    this.#audit = Object.freeze({ ...this.#audit, ...patch })
  }

  #changed(): void {
    for (const listener of this.#listeners) listener()
  }
}
