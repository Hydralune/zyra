import {
  ConnectionPhase,
  DeliveryDisposition,
  EVENT_INGRESS_DELTA_SCHEMA,
  EVENT_INGRESS_SNAPSHOT_SCHEMA,
  FrameKind,
  TransportKind,
  normalizeIngressCapacity,
  normalizeReconnectPolicy,
  transportPreference,
  type ConnectionSnapshot,
  type EventIngressDataSource,
  type IngressBatch,
  type IngressCapacity,
  type IngressCapabilities,
  type IngressDiagnostic,
  type IngressEvent,
  type IngressEventFrame,
  type IngressFrame,
  type IngressObserver,
  type IngressPage,
  type IngressSubscriptionOptions,
  type JsonValue,
  type NormalizedIngressFilter,
  type ReconnectPolicy,
  type TransportKindValue,
  type TransportOpenContext,
  type TransportSession,
} from "./contracts.ts"
import {
  EventIngressError,
  IngressErrorCode,
  classifyIngressError,
  isCancellation,
} from "./errors.ts"
import {
  normalizeAnyFrame,
  normalizeCapabilities,
  normalizePage,
} from "./validator.ts"
import { EventCursorLedger } from "./cursor-ledger.ts"
import { BoundedEventIdentityWindow } from "./identity-window.ts"
import { OrderedIngressBuffer } from "./ordered-buffer.ts"
import { PartialEventAssembler } from "./assembly.ts"
import { SubscribeBeforeSnapshotBarrier } from "./snapshot-barrier.ts"
import { EventGapRecovery } from "./gap-recovery.ts"
import { IngressDiagnostics } from "./diagnostics.ts"
import {
  IngressSubscriptionRegistry,
  type SubscriptionHandle,
} from "./subscriptions.ts"
import {
  HeartbeatWatchdog,
  ReconnectSupervisor,
} from "./reconnect.ts"
import { createLongPollSession } from "./transport-sources.ts"

export interface EventIngressCoordinatorOptions {
  cursor?: string
  capacity?: Partial<IngressCapacity>
  reconnect?: Partial<ReconnectPolicy>
  transportPreference?: readonly TransportKindValue[]
  snapshotPageSize?: number
  deltaPageSize?: number
  longPollMs?: number
  heartbeatTimeoutMs?: number
  now?: () => number
  random?: () => number
}

export interface CoordinatorAudit {
  ok: boolean
  findings: readonly string[]
  connection: ConnectionSnapshot
  cursor: ReturnType<EventCursorLedger["audit"]>
  identity: ReturnType<BoundedEventIdentityWindow["audit"]>
  buffer: ReturnType<OrderedIngressBuffer["audit"]>
  assembly: ReturnType<PartialEventAssembler["audit"]>
  barrier: ReturnType<SubscribeBeforeSnapshotBarrier["audit"]>
  gap: ReturnType<EventGapRecovery["audit"]>
}

function boundedPage(value: number | undefined, fallback: number): number {
  if (value === undefined) return fallback
  if (!Number.isFinite(value)) throw new TypeError("Event ingress page size must be finite")
  return Math.min(1000, Math.max(1, Math.floor(value)))
}

function boundedWait(value: number | undefined, fallback: number): number {
  if (value === undefined) return fallback
  if (!Number.isFinite(value)) throw new TypeError("Event ingress wait must be finite")
  return Math.min(25_000, Math.max(0, Math.floor(value)))
}

export class EventIngressCoordinator {
  readonly #taskId: string
  readonly #source: EventIngressDataSource
  readonly #capacity: IngressCapacity
  readonly #reconnectPolicy: ReconnectPolicy
  readonly #preference: readonly TransportKindValue[]
  readonly #snapshotPageSize: number
  readonly #deltaPageSize: number
  readonly #longPollMs: number
  readonly #heartbeatTimeoutMs: number
  readonly #now: () => number
  readonly #cursor: EventCursorLedger
  readonly #identities: BoundedEventIdentityWindow
  readonly #buffer: OrderedIngressBuffer
  readonly #assembly: PartialEventAssembler
  readonly #barrier: SubscribeBeforeSnapshotBarrier
  readonly #gaps: EventGapRecovery
  readonly #diagnostics: IngressDiagnostics
  readonly #subscriptions: IngressSubscriptionRegistry
  readonly #reconnect: ReconnectSupervisor
  readonly #watchdog: HeartbeatWatchdog
  #run: Promise<void> | undefined
  #controller: AbortController | undefined
  #session: TransportSession | undefined
  #capabilities: IngressCapabilities | undefined
  #activeTransport: TransportKindValue | undefined
  #currentFilter: NormalizedIngressFilter
  #filterRevision = 0
  #runningRevision = 0
  #closed = false
  #disabled = false
  #healing = false
  #restartRequested = false

  constructor(
    taskId: string,
    source: EventIngressDataSource,
    options: EventIngressCoordinatorOptions = {},
  ) {
    this.#taskId = String(taskId || "").trim()
    if (!this.#taskId) throw new TypeError("Event ingress taskId must not be empty")
    this.#source = source
    this.#capacity = normalizeIngressCapacity(options.capacity)
    this.#reconnectPolicy = normalizeReconnectPolicy(options.reconnect)
    this.#preference = transportPreference(options.transportPreference)
    this.#snapshotPageSize = boundedPage(options.snapshotPageSize, 512)
    this.#deltaPageSize = boundedPage(options.deltaPageSize, 512)
    this.#longPollMs = boundedWait(options.longPollMs, 750)
    this.#heartbeatTimeoutMs = Math.max(
      1_000,
      Math.min(
        10 * 60_000,
        Math.floor(
          options.heartbeatTimeoutMs ??
          this.#reconnectPolicy.heartbeatTimeoutMs,
        ),
      ),
    )
    this.#now = options.now ?? Date.now
    this.#cursor = new EventCursorLedger(this.#taskId, {
      maximumObservations: this.#capacity.diagnosticsLimit,
      maximumIdentities: this.#capacity.identityWindow,
      now: this.#now,
      initialCursor: options.cursor,
    })
    this.#identities = new BoundedEventIdentityWindow(this.#capacity.identityWindow, {
      now: this.#now,
    })
    this.#buffer = new OrderedIngressBuffer(this.#capacity, { now: this.#now })
    this.#assembly = new PartialEventAssembler(this.#capacity, { now: this.#now })
    this.#barrier = new SubscribeBeforeSnapshotBarrier(this.#taskId, this.#capacity, {
      now: this.#now,
    })
    this.#gaps = new EventGapRecovery(this.#taskId, this.#capacity, { now: this.#now })
    this.#diagnostics = new IngressDiagnostics(
      this.#taskId,
      this.#capacity.diagnosticsLimit,
      { now: this.#now },
    )
    this.#currentFilter = this.#emptyFilter()
    this.#subscriptions = new IngressSubscriptionRegistry(this.#taskId, {
      now: this.#now,
      onChange: (count, filter, revision) => this.#subscriptionsChanged(count, filter, revision),
      onSubscriberError: (error, subscriptionId) => {
        const diagnostic = this.#diagnostics.record(
          "subscription",
          error.code,
          error.message,
          { subscriptionId, retryable: error.retryable },
        )
        this.#subscriptions.dispatchDiagnostic(diagnostic)
      },
    })
    this.#reconnect = new ReconnectSupervisor(this.#reconnectPolicy, {
      now: this.#now,
      random: options.random,
    })
    this.#watchdog = new HeartbeatWatchdog(
      this.#heartbeatTimeoutMs,
      (error) => {
        this.#diagnostics.failure(error)
        // A missed heartbeat invalidates only the current transport session.
        // The coordinator controller owns the whole browser subscription; if
        // it is aborted here, the run loop treats the timeout as an explicit
        // unsubscribe and never executes cursor-based reconnect.
        void this.#closeSession(error)
      },
      { now: this.#now },
    )
  }

  get taskId(): string {
    return this.#taskId
  }

  get closed(): boolean {
    return this.#closed
  }

  get subscriberCount(): number {
    return this.#subscriptions.size
  }

  subscribe(
    observer: IngressObserver,
    options: {
      filter?: IngressSubscriptionOptions["filter"]
      signal?: AbortSignal
    } = {},
  ): SubscriptionHandle {
    this.#assertAvailable()
    return this.#subscriptions.subscribe(observer, options)
  }

  start(): Promise<void> {
    this.#assertAvailable()
    if (this.#subscriptions.size === 0) return Promise.resolve()
    if (this.#run) return this.#run
    this.#controller = new AbortController()
    const controller = this.#controller
    const run = this.#runLoop(controller.signal)
      .catch((error) => {
        if (controller.signal.aborted || isCancellation(error)) return
        const classified = classifyIngressError(error, {
          taskId: this.#taskId,
          generation: this.#cursor.generation,
          transport: this.#activeTransport,
          phase: this.#diagnostics.snapshot().phase,
        })
        this.#diagnostics.failure(classified)
        this.#diagnostics.transition(ConnectionPhase.FAILED, {
          generation: this.#cursor.generation,
          transport: this.#activeTransport,
          message: classified.message,
        })
        this.#publishStatus()
      })
      .finally(() => {
        if (this.#run === run) this.#run = undefined
        if (this.#controller === controller) this.#controller = undefined
        this.#watchdog.stop()
        void this.#closeSession("Event ingress run completed.")
        if (
          !this.#closed &&
          !this.#disabled &&
          this.#subscriptions.size > 0 &&
          this.#restartRequested
        ) {
          this.#restartRequested = false
          queueMicrotask(() => void this.start())
        }
      })
    this.#run = run
    return run
  }

  stop(reason?: unknown): boolean {
    const active = Boolean(this.#run || this.#controller || this.#session)
    this.#restartRequested = false
    this.#watchdog.stop()
    this.#reconnect.stop(reason)
    this.#controller?.abort(reason ?? "Event ingress stopped.")
    void this.#closeSession(reason)
    this.#diagnostics.transition(ConnectionPhase.STOPPING, {
      generation: this.#cursor.generation,
      transport: this.#activeTransport,
      message: String(reason ?? "Event ingress is stopping."),
    })
    this.#diagnostics.transition(ConnectionPhase.STOPPED, {
      generation: this.#cursor.generation,
      message: "Browser event subscription stopped; backend task was not cancelled.",
    })
    this.#publishStatus()
    return active
  }

  restart(reason = "Event ingress subscription changed."): void {
    if (this.#closed || this.#disabled || this.#subscriptions.size === 0) return
    this.#restartRequested = true
    this.#controller?.abort(
      new EventIngressError(
        IngressErrorCode.CANCELLED,
        reason,
        { context: { taskId: this.#taskId } },
      ),
    )
    void this.#closeSession(reason)
    if (!this.#run) {
      this.#restartRequested = false
      void this.start()
    }
  }

  disable(reason = "Event ingress coordinator disabled."): void {
    if (this.#disabled) return
    this.#disabled = true
    this.#cursor.disable()
    this.#identities.disable()
    this.#buffer.disable()
    this.#assembly.disable()
    this.#barrier.disable()
    this.#gaps.disable()
    this.stop(reason)
  }

  enable(): void {
    if (!this.#disabled || this.#closed) return
    this.#disabled = false
    this.#cursor.enable()
    this.#identities.enable()
    this.#buffer.enable()
    this.#assembly.enable()
    this.#barrier.enable()
    this.#gaps.enable()
    this.#reconnect.reset()
    if (this.#subscriptions.size) void this.start()
  }

  close(reason?: unknown): void {
    if (this.#closed) return
    this.#closed = true
    this.stop(reason ?? "Event ingress coordinator closed.")
    this.#subscriptions.close(reason)
    this.#buffer.reset("coordinator_closed")
    this.#assembly.reset("coordinator_closed")
    this.#barrier.reset("coordinator_closed")
    this.#gaps.clear()
  }

  snapshot(): ConnectionSnapshot {
    return this.#diagnostics.snapshot()
  }

  diagnostics(options: {
    afterId?: number
    category?: IngressDiagnostic["category"]
    code?: string
    limit?: number
  } = {}): readonly IngressDiagnostic[] {
    return this.#diagnostics.items(options)
  }

  audit(): CoordinatorAudit {
    const cursor = this.#cursor.audit()
    const identity = this.#identities.audit()
    const buffer = this.#buffer.audit()
    const assembly = this.#assembly.audit()
    const barrier = this.#barrier.audit()
    const gap = this.#gaps.audit()
    const findings = [
      ...cursor.findings.map((item) => `cursor:${item}`),
      ...identity.findings.map((item) => `identity:${item}`),
      ...buffer.findings.map((item) => `buffer:${item}`),
      ...assembly.findings.map((item) => `assembly:${item}`),
      ...barrier.findings.map((item) => `barrier:${item}`),
      ...gap.findings.map((item) => `gap:${item}`),
    ]
    const connection = this.snapshot()
    if (
      connection.cursor.committedSequence !==
      this.#cursor.committedSequence
    ) findings.push("connection_cursor_projection_is_stale")
    if (
      connection.pressure.items !== this.#buffer.size ||
      connection.pressure.bytes !== this.#buffer.bytes
    ) findings.push("connection_pressure_projection_is_stale")
    return {
      ok: findings.length === 0,
      findings: Object.freeze(findings),
      connection,
      cursor,
      identity,
      buffer,
      assembly,
      barrier,
      gap,
    }
  }

  exportState(): JsonValue {
    return {
      taskId: this.#taskId,
      connection: this.snapshot() as unknown as JsonValue,
      capabilities: this.#capabilities as unknown as JsonValue,
      subscriptions: this.#subscriptions.exportState(),
      cursor: this.#cursor.exportState(),
      identities: this.#identities.exportState(),
      buffer: this.#buffer.exportState(),
      assembly: this.#assembly.exportState(),
      barrier: this.#barrier.exportState(),
      gaps: this.#gaps.exportState(),
      reconnect: this.#reconnect.exportState(),
      watchdog: this.#watchdog.snapshot() as unknown as JsonValue,
      diagnostics: this.#diagnostics.exportState(),
      audit: this.audit() as unknown as JsonValue,
      disabled: this.#disabled,
      closed: this.#closed,
    }
  }

  async #runLoop(signal: AbortSignal): Promise<void> {
    this.#reconnect.reset()
    while (!signal.aborted && this.#subscriptions.size > 0) {
      this.#runningRevision = this.#filterRevision
      this.#currentFilter = this.#subscriptions.unionFilter()
      const generation = this.#cursor.beginGeneration({
        cursor: this.#cursor.cursor,
        preserveCommitted: true,
      }).generation
      this.#barrier.beginGeneration(generation)
      this.#gaps.beginGeneration(generation)
      this.#diagnostics.transition(ConnectionPhase.NEGOTIATING, {
        generation,
        message: "Negotiating event ingress transport and schema capabilities.",
      })
      this.#publishStatus()
      let capabilities: IngressCapabilities
      let transport: TransportKindValue
      try {
        const rawCapabilities = await this.#source.capabilities(
          this.#taskId,
          this.#currentFilter,
          signal,
          generation,
          this.#cursor.cursor,
        )
        capabilities = normalizeCapabilities(rawCapabilities, this.#taskId)
        if (capabilities.generation !== generation) {
          throw new EventIngressError(
            IngressErrorCode.STALE_GENERATION,
            "Capability generation does not match the active ingress generation.",
            {
              resyncRequired: true,
              context: {
                taskId: this.#taskId,
                generation: capabilities.generation,
                details: { expectedGeneration: generation },
              },
            },
          )
        }
        this.#capabilities = capabilities
        transport = this.#reconnect.selectTransport(capabilities, this.#preference)
        this.#activeTransport = transport
        await this.#connectGeneration(capabilities, transport, signal)
        if (signal.aborted) return
        throw new EventIngressError(
          IngressErrorCode.TRANSPORT_DISCONNECTED,
          `${transport} event ingress session ended.`,
          {
            retryable: true,
            context: { taskId: this.#taskId, generation, transport },
          },
        )
      } catch (error) {
        if (signal.aborted || isCancellation(error)) return
        const classified = classifyIngressError(error, {
          taskId: this.#taskId,
          generation,
          transport: this.#activeTransport,
        })
        this.#diagnostics.failure(classified)
        const decision = this.#reconnect.failure(classified, {
          transport: this.#activeTransport,
          allowResync: true,
          signal,
        })
        if (decision.action === "fail") {
          const exhausted = new EventIngressError(
            classified.resyncRequired
              ? IngressErrorCode.RESYNC_EXHAUSTED
              : IngressErrorCode.RECONNECT_EXHAUSTED,
            `Event ingress recovery exhausted after ${decision.attempt} attempts: ${classified.message}`,
            {
              retryable: false,
              resyncRequired: classified.resyncRequired,
              context: classified.context,
              cause: classified,
            },
          )
          this.#diagnostics.failure(exhausted)
          throw exhausted
        }
        if (decision.action === "stop") return
        if (decision.action === "resync") {
          this.#diagnostics.attempt("resync", decision.attempt)
          this.#diagnostics.transition(ConnectionPhase.RESYNCING, {
            generation,
            transport: this.#activeTransport,
            resyncAttempt: decision.attempt,
            message: classified.message,
          })
          this.#prepareResync()
        } else {
          this.#diagnostics.attempt("reconnect", decision.attempt)
          this.#diagnostics.transition(ConnectionPhase.BACKING_OFF, {
            generation,
            transport: this.#activeTransport,
            reconnectAttempt: decision.attempt,
            message: classified.message,
          })
        }
        this.#publishStatus()
        await this.#closeSession(classified)
        await this.#reconnect.wait(decision, signal)
      }
      if (this.#runningRevision !== this.#filterRevision) {
        this.#prepareResync()
      }
    }
  }

  async #connectGeneration(
    capabilities: IngressCapabilities,
    transport: TransportKindValue,
    signal: AbortSignal,
  ): Promise<void> {
    const generation = this.#cursor.generation
    this.#diagnostics.transition(ConnectionPhase.SUBSCRIBING, {
      generation,
      transport,
      message: "Opening live delta subscription before requesting the snapshot.",
    })
    this.#cursor.updateCursor(
      capabilities.subscriptionCursor,
      capabilities.subscriptionSequence,
    )
    const liveCursor = capabilities.subscriptionCursor
    const context: TransportOpenContext = {
      taskId: this.#taskId,
      cursor: liveCursor,
      generation,
      filter: this.#currentFilter,
      signal,
      capabilities,
      pageSize: this.#deltaPageSize,
      waitMs: this.#longPollMs,
      heartbeatTimeoutMs: this.#heartbeatTimeoutMs,
    }
    const session = await this.#openSession(transport, context)
    if (signal.aborted) {
      await session.close(signal.reason)
      return
    }
    this.#session = session
    this.#barrier.markSubscribed(generation)
    this.#reconnect.connected(transport)
    this.#watchdog.start(this.#taskId, generation, transport)
    let pumpError: unknown
    const pump = this.#consumeSession(session, signal).catch((error) => {
      pumpError = error
    })
    this.#diagnostics.transition(ConnectionPhase.SNAPSHOTTING, {
      generation,
      transport,
      message: "Live subscription is active; loading a consistent snapshot.",
    })
    this.#publishStatus()
    await this.#loadSnapshot(capabilities, signal)
    if (pumpError !== undefined) throw pumpError
    this.#diagnostics.transition(ConnectionPhase.LIVE, {
      generation,
      transport,
      message: "Snapshot and interleaved deltas converged; ingress is live.",
    })
    this.#publishStatus()
    await pump
    if (pumpError !== undefined) throw pumpError
  }

  async #openSession(
    transport: TransportKindValue,
    context: TransportOpenContext,
  ): Promise<TransportSession> {
    if (transport === TransportKind.SSE) return this.#source.openSse(context)
    if (transport === TransportKind.WEBSOCKET) {
      return this.#source.openWebSocket(context)
    }
    if (transport === TransportKind.LONG_POLL) {
      return createLongPollSession(this.#source, context)
    }
    throw new EventIngressError(
      IngressErrorCode.TRANSPORT_UNAVAILABLE,
      `Unsupported ingress transport ${transport}.`,
      { retryable: true, context: { taskId: this.#taskId, transport } },
    )
  }

  async #consumeSession(
    session: TransportSession,
    signal: AbortSignal,
  ): Promise<void> {
    const transport = session.kind
    for await (const raw of session.frames()) {
      if (signal.aborted) return
      this.#watchdog.beat()
      if (
        raw &&
        typeof raw === "object" &&
        !Array.isArray(raw) &&
        (
          (raw as Record<string, unknown>).schema === EVENT_INGRESS_SNAPSHOT_SCHEMA ||
          (raw as Record<string, unknown>).schema === EVENT_INGRESS_DELTA_SCHEMA
        )
      ) {
        const page = normalizePage(raw, this.#taskId, this.#cursor.generation)
        await this.#processLivePage(page)
        continue
      }
      const frame = normalizeAnyFrame(raw, this.#taskId, this.#cursor.generation)
      await this.#processLiveFrame(frame, transport)
    }
  }

  async #processLivePage(page: IngressPage): Promise<void> {
    this.#diagnostics.frame("event", {
      sequence: page.nextSequence,
      transport: this.#activeTransport,
      generation: page.generation,
    })
    if (!this.#barrier.snapshotComplete) {
      for (const frame of page.frames) {
        this.#barrier.retainLive(frame)
      }
      return
    }
    this.#cursor.acceptPage(page)
    for (const frame of page.frames) {
      await this.#acceptEventFrame(frame, { deferGapHealing: true })
    }
    await this.#drain(page.cursor, false, page.caughtUp ?? !page.hasMore)
    if (this.#gaps.snapshot() && this.#barrier.snapshotComplete) {
      await this.#healGap()
    }
    if (!page.frames.length && page.nextSequence >= this.#cursor.committedSequence) {
      this.#cursor.advanceEmptyPage(page)
      this.#buffer.discardThrough(page.nextSequence, "empty_delta_page_advanced_cursor")
      this.#gaps.resolveThrough(page.nextSequence)
    } else if (page.nextSequence >= this.#cursor.committedSequence) {
      this.#cursor.updateCursor(page.cursor, page.nextSequence)
    }
    this.#refreshDiagnostics()
  }

  async #processLiveFrame(
    frame: IngressFrame,
    transport: TransportKindValue,
  ): Promise<void> {
    this.#diagnostics.frame(frame.kind, {
      sequence: frame.sequence,
      eventId: frame.kind === FrameKind.EVENT ? frame.eventId : undefined,
      transport,
      generation: frame.generation,
    })
    if (frame.kind === FrameKind.READY || frame.kind === FrameKind.HEARTBEAT) {
      this.#watchdog.beat(frame.observedAtMs)
      if (
        frame.kind === FrameKind.HEARTBEAT &&
        frame.cursor &&
        this.#barrier.snapshotComplete &&
        frame.sequence >= this.#cursor.committedSequence &&
        this.#buffer.size === 0 &&
        !this.#gaps.snapshot()
      ) {
        this.#cursor.updateCursor(frame.cursor, frame.sequence)
        this.#refreshDiagnostics()
      }
      return
    }
    if (frame.kind === FrameKind.CLOSE) {
      if (
        frame.cursor &&
        this.#barrier.snapshotComplete &&
        frame.sequence >= this.#cursor.committedSequence
      ) {
        this.#cursor.updateCursor(frame.cursor, frame.sequence)
      }
      if (frame.retryable) {
        throw new EventIngressError(
          IngressErrorCode.TRANSPORT_DISCONNECTED,
          `Server closed event ingress stream: ${frame.reason}`,
          {
            retryable: true,
            context: {
              taskId: this.#taskId,
              generation: frame.generation,
              sequence: frame.sequence,
              transport,
            },
          },
        )
      }
      throw new EventIngressError(
        IngressErrorCode.TRANSPORT_PROTOCOL,
        `Server closed event ingress stream permanently: ${frame.reason}`,
        {
          context: {
            taskId: this.#taskId,
            generation: frame.generation,
            sequence: frame.sequence,
            transport,
          },
        },
      )
    }
    if (frame.kind === FrameKind.ERROR) {
      throw new EventIngressError(frame.code, frame.message, {
        retryable: frame.retryable,
        resyncRequired: frame.resyncRequired,
        context: {
          taskId: this.#taskId,
          generation: frame.generation,
          sequence: frame.sequence,
          transport,
        },
      })
    }
    if (!this.#barrier.snapshotComplete) {
      const receipt = this.#barrier.retainLive(frame)
      this.#publishReceiptDiagnostic(receipt, "snapshot")
      return
    }
    await this.#acceptEventFrame(frame)
    await this.#drain(frame.cursor, false, false)
  }

  async #loadSnapshot(
    capabilities: IngressCapabilities,
    signal: AbortSignal,
  ): Promise<void> {
    const generation = this.#cursor.generation
    this.#barrier.beginSnapshot(generation)
    let cursor: string | undefined
    let pages = 0
    while (!signal.aborted) {
      pages += 1
      if (pages > 1_000_000) {
        throw new EventIngressError(
          IngressErrorCode.SNAPSHOT_FAILED,
          "Snapshot pagination exceeded its safety bound.",
          { resyncRequired: true, context: { taskId: this.#taskId, generation } },
        )
      }
      const raw = await this.#source.snapshot(this.#taskId, {
        cursor,
        generation,
        limit: Math.min(
          this.#snapshotPageSize,
          capabilities.limits.maxPage,
        ),
        filter: this.#currentFilter,
        signal,
      })
      const page = normalizePage(raw, this.#taskId, generation)
      const boundary = page.boundary ?? 0
      if (pages === 1) this.#cursor.beginSnapshot(boundary, page.cursor)
      this.#cursor.acceptPage(page)
      const merged = this.#barrier.mergeSnapshot(
        page,
        this.#cursor.committedSequence,
      )
      for (const receipt of merged.receipts) {
        this.#publishReceiptDiagnostic(receipt, "snapshot")
      }
      for (const frame of merged.frames) {
        await this.#acceptEventFrame(frame, { deferGapHealing: true })
      }
      await this.#drain(page.cursor, true, page.complete === true)
      if (
        page.nextSequence >= this.#cursor.committedSequence &&
        this.#buffer.size === 0 &&
        !this.#gaps.snapshot()
      ) {
        this.#cursor.updateCursor(page.cursor, page.nextSequence)
      }
      this.#refreshDiagnostics()
      if (page.complete) {
        this.#cursor.completeSnapshot(page)
        for (const frame of this.#barrier.releaseAfterBoundary()) {
          await this.#acceptEventFrame(frame, { deferGapHealing: true })
        }
        await this.#drain(undefined, false, true)
        if (this.#gaps.snapshot()) await this.#healGap()
        this.#refreshDiagnostics()
        return
      }
      if (!page.hasMore || !page.cursor) {
        throw new EventIngressError(
          IngressErrorCode.SNAPSHOT_INCOMPLETE,
          "Snapshot ended before reaching its captured boundary.",
          {
            retryable: true,
            resyncRequired: true,
            context: {
              taskId: this.#taskId,
              generation,
              sequence: page.nextSequence,
              details: { boundary, pages },
            },
          },
        )
      }
      cursor = page.cursor
    }
  }

  async #acceptEventFrame(
    frame: IngressEventFrame,
    options: { deferGapHealing?: boolean } = {},
  ): Promise<void> {
    const identity = this.#identities.observe(
      frame,
      this.#cursor.committedSequence,
    )
    if (
      identity.disposition === DeliveryDisposition.DUPLICATE ||
      identity.disposition === DeliveryDisposition.STALE
    ) {
      this.#publishReceiptDiagnostic(identity.receipt, "delivery")
      return
    }
    const transition = this.#cursor.observeFrame(frame)
    const inserted = this.#buffer.insert(
      frame,
      this.#cursor.committedSequence,
    )
    for (const receipt of inserted.evicted) {
      this.#publishReceiptDiagnostic(receipt, "pressure")
    }
    this.#publishReceiptDiagnostic(inserted.receipt, "delivery")
    this.#diagnostics.updatePressure(inserted.pressure)
    if (transition.gap || inserted.disposition === DeliveryDisposition.BUFFERED_GAP) {
      const observation = this.#gaps.observe(
        frame,
        this.#cursor.committedSequence,
      )
      if (observation) {
        this.#diagnostics.updateGap(observation.gap)
        if (
          !options.deferGapHealing &&
          !this.#healing &&
          this.#barrier.snapshotComplete
        ) {
          await this.#healGap()
        }
      }
    }
  }

  async #drain(
    pageCursor: string | undefined,
    snapshot: boolean,
    caughtUp: boolean,
  ): Promise<void> {
    while (true) {
      const drained = this.#buffer.drain(this.#cursor.committedSequence)
      if (!drained.frames.length) break
      const delivered: IngressEvent[] = []
      const receipts = []
      const fromSequence = this.#cursor.committedSequence
      let bytes = 0
      for (const frame of drained.frames) {
        const cursor = frame.cursor ?? pageCursor
        const committed = this.#cursor.commitFrame(frame, cursor)
        receipts.push(this.#identities.commit(frame))
        const assembly = this.#assembly.process(frame)
        receipts.push(...assembly.receipts)
        delivered.push(...assembly.deliver.map((item) => item.event))
        bytes += frame.encodedBytes
        if (committed.advanced) this.#gaps.resolveThrough(frame.sequence)
      }
      if (pageCursor && drained.sequence >= this.#cursor.committedSequence) {
        this.#cursor.updateCursor(pageCursor, drained.sequence)
      }
      this.#refreshDiagnostics()
      if (delivered.length || receipts.length) {
        const batch: IngressBatch = Object.freeze({
          taskId: this.#taskId,
          generation: this.#cursor.generation,
          events: Object.freeze(delivered),
          receipts: Object.freeze(receipts),
          cursor: this.#cursor.cursor,
          fromSequence,
          sequence: this.#cursor.committedSequence,
          highWatermark: this.#cursor.snapshot().highWatermark,
          receivedAt: this.#now(),
          transport: this.#activeTransport ?? TransportKind.LONG_POLL,
          snapshot,
          caughtUp,
        })
        const report = this.#subscriptions.dispatchBatch(batch)
        for (const error of report.failures) this.#diagnostics.failure(error)
        this.#diagnostics.delivery(
          delivered.length,
          this.#cursor.committedSequence,
          bytes,
        )
      }
    }
    const gap = this.#buffer.gapAfter(this.#cursor.committedSequence)
    if (gap) {
      this.#diagnostics.updateGap({
        ...gap,
        firstObservedAtMs: this.#now(),
        lastObservedAtMs: this.#now(),
        attempts: this.#gaps.snapshot()?.attempts ?? 0,
      })
    } else {
      this.#diagnostics.updateGap(this.#gaps.snapshot())
    }
  }

  async #healGap(): Promise<void> {
    if (this.#healing) return
    const gap = this.#gaps.beginHealing()
    if (!gap) return
    const cursor = this.#cursor.cursor
    if (!cursor) {
      throw new EventIngressError(
        IngressErrorCode.CURSOR_MISSING,
        "Cannot heal an event gap without the last committed cursor.",
        {
          resyncRequired: true,
          context: {
            taskId: this.#taskId,
            generation: this.#cursor.generation,
            sequence: this.#cursor.committedSequence,
          },
        },
      )
    }
    this.#healing = true
    try {
      for (let attempt = 1; attempt <= this.#reconnectPolicy.resyncAttempts + 1; attempt += 1) {
        const raw = await this.#source.delta(this.#taskId, {
          cursor: this.#cursor.cursor ?? cursor,
          generation: this.#cursor.generation,
          limit: this.#deltaPageSize,
          waitMs: 0,
          filter: this.#currentFilter,
          signal: this.#controller?.signal,
        })
        const page = normalizePage(raw, this.#taskId, this.#cursor.generation)
        const healing = this.#gaps.applyPage(
          page,
          this.#cursor.committedSequence,
        )
        for (const frame of healing.frames) {
          await this.#acceptEventFrame(frame)
        }
        await this.#drain(page.cursor, false, page.caughtUp ?? !page.hasMore)
        if (healing.resolved) {
          if (page.nextSequence >= this.#cursor.committedSequence) {
            this.#cursor.updateCursor(page.cursor, page.nextSequence)
          }
          this.#diagnostics.updateGap(undefined)
          this.#refreshDiagnostics()
          return
        }
        this.#diagnostics.updateGap(healing.remaining)
        if (!page.hasMore && !page.frames.length) break
      }
      throw (
        this.#gaps.fail("Gap catch-up could not restore a canonical predecessor chain.") ??
        new EventIngressError(
          IngressErrorCode.GAP_UNRECOVERABLE,
          "Gap catch-up ended without resolving the predecessor chain.",
          { retryable: true, resyncRequired: true },
        )
      )
    } finally {
      this.#healing = false
    }
  }

  #prepareResync(): void {
    this.#buffer.reset("resync")
    this.#assembly.reset("resync")
    this.#barrier.reset("resync")
    this.#gaps.clear()
    this.#identities.reset({
      preserveCommitted: true,
      committedSequence: this.#cursor.committedSequence,
    })
  }

  async #closeSession(reason?: unknown): Promise<void> {
    const session = this.#session
    this.#session = undefined
    this.#watchdog.stop()
    if (!session) return
    try {
      await session.close(reason)
    } catch {
      // Session closure is best effort; the generation fence prevents stale delivery.
    }
  }

  #subscriptionsChanged(
    count: number,
    filter: NormalizedIngressFilter,
    revision: number,
  ): void {
    const previousMaterial = this.#currentFilter.digestMaterial
    this.#filterRevision = revision
    this.#currentFilter = filter
    this.#diagnostics.subscribers(count)
    this.#publishStatus()
    if (count === 0) {
      this.stop("Last browser event subscriber removed.")
      return
    }
    if (!this.#run) {
      queueMicrotask(() => void this.start())
      return
    }
    if (
      this.#runningRevision !== 0 &&
      filter.digestMaterial !== previousMaterial
    ) this.restart("Event subscription union changed.")
  }

  #refreshDiagnostics(): void {
    this.#diagnostics.updateCursor(this.#cursor.snapshot())
    this.#diagnostics.updatePressure(this.#buffer.snapshot())
    this.#diagnostics.updateGap(this.#gaps.snapshot())
    this.#publishStatus()
  }

  #publishStatus(): void {
    this.#subscriptions.dispatchStatus(this.#diagnostics.snapshot())
  }

  #publishReceiptDiagnostic(
    receipt: {
      eventId: string
      sequence: number
      disposition: string
      reason: string
      encodedBytes: number
    },
    category: IngressDiagnostic["category"],
  ): void {
    const diagnostic = this.#diagnostics.record(
      category,
      `receipt.${receipt.disposition}`,
      receipt.reason,
      {
        disposition: receipt.disposition,
        encodedBytes: receipt.encodedBytes,
      },
      {
        eventId: receipt.eventId,
        sequence: receipt.sequence,
        transport: this.#activeTransport,
      },
    )
    this.#subscriptions.dispatchDiagnostic(diagnostic)
  }

  #emptyFilter(): NormalizedIngressFilter {
    return {
      eventTypes: Object.freeze([]),
      intents: Object.freeze([]),
      correlationIds: Object.freeze([]),
      artifactIds: Object.freeze([]),
      includeNonEffective: true,
      includeHeartbeats: false,
      digestMaterial: JSON.stringify({
        eventTypes: [],
        intents: [],
        correlationIds: [],
        artifactIds: [],
        includeNonEffective: true,
        includeHeartbeats: false,
      }),
    }
  }

  #assertAvailable(): void {
    if (this.#closed) {
      throw new EventIngressError(
        IngressErrorCode.CLOSED,
        "Event ingress coordinator is closed.",
        { context: { taskId: this.#taskId } },
      )
    }
    if (this.#disabled) {
      throw new EventIngressError(
        IngressErrorCode.DISABLED,
        "Event ingress coordinator is disabled.",
        { context: { taskId: this.#taskId } },
      )
    }
  }
}
