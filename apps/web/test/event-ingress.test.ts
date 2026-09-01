import { describe, expect, test } from "bun:test"
import {
  BoundedEventIdentityWindow,
  DeliveryDisposition,
  EventCursorLedger,
  EventGapRecovery,
  EventIngressCoordinator,
  EventIngressError,
  IngressErrorCode,
  IngressSubscriptionRegistry,
  OrderedIngressBuffer,
  PartialEventAssembler,
  ReconnectSupervisor,
  SseFrameDecoder,
  SubscribeBeforeSnapshotBarrier,
  TransportKind,
  normalizeCapabilities,
  normalizeAnyFrame,
  normalizeEventFrame,
  normalizeIngressCapacity,
  normalizePage,
  normalizeReconnectPolicy,
  type IngressEventFrame,
  type IngressLiveFrame,
  type EventIngressDataSource,
  type IngressObserver,
} from "../src/events/ingress/index.ts"

const TASK = "task_event_ingress_test"
const RUN = "run_event_ingress_test"
const NOW = "2026-07-23T00:00:00.000Z"
const DIGEST = `sha256:${"a".repeat(64)}`

function eventRaw(
  sequence: number,
  options: {
    eventId?: string
    eventType?: string
    effect?: string
    operation?: string
    metadata?: Record<string, unknown>
    inline?: Record<string, unknown>
    taskId?: string
    contentDigest?: string
  } = {},
) {
  const eventId = options.eventId ?? `event_${sequence}`
  return JSON.parse(JSON.stringify({
    schema: "zyra.runtime-event/v1",
    eventId,
    eventType: options.eventType ?? "runtime.state.changed",
    eventVersion: 1,
    aggregateId: `task:${TASK}`,
    aggregateSequence: sequence - 1,
    globalSequence: sequence,
    producerSequence: sequence,
    idempotencyKey: `idempotency-${eventId}`,
    correlationId: "request_event_ingress_test",
    createdAt: NOW,
    committedAt: NOW,
    durability: "durable",
    effect: options.effect ?? "effective",
    identity: {
      taskId: options.taskId ?? TASK,
      runId: RUN,
      sessionId: "session_event_ingress_test",
      spanId: `span_${sequence}`,
      parentSpanId: sequence > 1 ? `span_${sequence - 1}` : undefined,
      toolCallId: options.inline?.tool_call_id,
      checkpointId: options.inline?.checkpoint_id,
    },
    sender: {
      kind: "runtime",
      id: "runtime-test",
      capabilityRefs: [],
    },
    intent: "status",
    topKRecipients: [],
    summary: `event ${sequence}`,
    stateDelta: {
      domain: "task",
      operation: options.operation ?? "transition",
      path: ["status"],
      effective: options.effect !== "non_effective",
    },
    evidenceRefs: [],
    artifactRefs: [],
    provenance: {
      sourceRepository: "zyra",
      sourceModule: "event-ingress-test",
      migrationRole: "zyra_owned",
      producerVersion: "test",
      trust: "internal",
    },
    inline: options.inline ?? { status: "running" },
    sourceBytes: 100,
    inlineBytes: 20,
    envelopeBytes: 1024,
    contentDigest: options.contentDigest ?? DIGEST,
    metadata: options.metadata ?? {},
  }))
}

function frameRaw(
  sequence: number,
  previousSequence: number,
  options: Parameters<typeof eventRaw>[1] & {
    generation?: number
    source?: string
    cursor?: string
    presentation?: Record<string, unknown>
  } = {},
) {
  const event = eventRaw(sequence, options)
  return {
    schema: "zyra.event-ingress-frame/v1",
    kind: "event",
    source: options.source ?? "delta",
    generation: options.generation ?? 1,
    taskId: options.taskId ?? TASK,
    sequence,
    previousSequence,
    eventId: event.eventId,
    eventType: event.eventType,
    correlationId: event.correlationId,
    observedAtMs: 1_000 + sequence,
    cursor: options.cursor ?? `cursor.${sequence}`,
    event,
    ...(options.presentation ? { presentation: options.presentation } : {}),
  }
}

function frame(
  sequence: number,
  previousSequence: number,
  options: Parameters<typeof frameRaw>[2] = {},
): IngressEventFrame {
  return normalizeEventFrame(
    frameRaw(sequence, previousSequence, options),
    options.taskId ?? TASK,
    options.generation ?? 1,
  )
}

function snapshotRaw(
  frames: unknown[],
  options: {
    from?: number
    next?: number
    boundary?: number
    complete?: boolean
    cursor?: string
    generation?: number
  } = {},
) {
  const from = options.from ?? 0
  const next =
    options.next ??
    Number((frames.at(-1) as { sequence?: number } | undefined)?.sequence ?? from)
  const boundary = options.boundary ?? next
  const complete = options.complete ?? next >= boundary
  return {
    schema: "zyra.event-ingress-snapshot/v1",
    protocol: "zyra.event-ingress/v1",
    taskId: TASK,
    generation: options.generation ?? 1,
    snapshotId: "snapshot-test",
    boundary,
    fromSequence: from,
    nextSequence: next,
    cursor: options.cursor ?? `snapshot.${next}`,
    cursorKind: complete ? "delta" : "snapshot",
    complete,
    hasMore: !complete,
    frames,
    observedAtMs: 2_000,
    canonicalOwner: "typescript.RuntimeEventSpine",
    canonicalWriteAllowed: false,
  }
}

describe("event ingress envelope validation", () => {
  test("admits only bounded versioned assistant live presentation", () => {
    const raw = {
      schema: "zyra.event-ingress-frame/v1",
      kind: "live",
      source: "runtime-live-bus",
      generation: 1,
      taskId: TASK,
      sequence: 7,
      liveSequence: 11,
      eventId: "live-assistant-11",
      eventType: "runtime.text.delta",
      observedAtMs: 2_000,
      presentation: {
        schema: "zyra.product-presentation/v1",
        kind: "assistant",
        phase: "delta",
        identity: "message-live-1",
        label: "Assistant",
        streamId: "provider:dispatch-live-1",
        text: "实时回答",
      },
      event: { private: "dropped by the browser validator" },
    }
    const normalized = normalizeAnyFrame(raw, TASK, 1) as IngressLiveFrame
    expect(normalized.kind).toBe("live")
    expect(normalized.presentation.text).toBe("实时回答")
    expect("event" in normalized).toBe(false)

    const registry = new IngressSubscriptionRegistry(TASK)
    const observed: IngressLiveFrame[] = []
    registry.subscribe({ batch() {}, live: (frame) => observed.push(frame) })
    expect(registry.dispatchLive(normalized)).toEqual([])
    expect(observed).toEqual([normalized])

    expect(() => normalizeAnyFrame({
      ...raw,
      presentation: { ...raw.presentation, kind: "reasoning" },
    }, TASK, 1)).toThrow(EventIngressError)
    expect(() => normalizeAnyFrame({
      ...raw,
      presentation: { ...raw.presentation, text: "你".repeat(400) },
    }, TASK, 1)).toThrow(EventIngressError)
  })

  test("normalizes full canonical identity and predecessor chain", () => {
    const normalized = frame(5, 2, {
      inline: {
        tool_call_id: "tool_call_1",
        checkpoint_id: "checkpoint_1",
      },
    })
    expect(normalized.sequence).toBe(5)
    expect(normalized.previousSequence).toBe(2)
    expect(normalized.event.identity.toolCallId).toBe("tool_call_1")
    expect(normalized.event.identity.checkpointId).toBe("checkpoint_1")
    expect(normalized.event.contentDigest).toBe(DIGEST)
    expect(Object.isFrozen(normalized.event)).toBe(true)
  })

  test("preserves only validated durable product presentation and exact whitespace", () => {
    const presentation = {
      schema: "zyra.product-presentation/v1",
      kind: "assistant",
      phase: "completed",
      identity: "message-durable-1",
      label: "Assistant",
      streamId: "stream-durable-1",
      text: "  第一行\n第二行  ",
      privateReasoning: "must be dropped",
    }
    const normalized = frame(2, 1, {
      eventType: "runtime.text.ended",
      presentation,
    })
    expect(normalized.presentation?.text).toBe("  第一行\n第二行  ")
    expect(normalized.presentation?.privateReasoning).toBeUndefined()

    const live = normalizeAnyFrame({
      schema: "zyra.event-ingress-frame/v1",
      kind: "live",
      source: "runtime-live-bus",
      generation: 1,
      taskId: TASK,
      sequence: 2,
      liveSequence: 4,
      eventId: "live-whitespace-4",
      eventType: "runtime.text.delta",
      observedAtMs: 2_000,
      presentation: { ...presentation, phase: "delta", text: " \n" },
    }, TASK, 1) as IngressLiveFrame
    expect(live.presentation.text).toBe(" \n")
  })

  test("admits explicit local failure impact and rejects unknown impact values", () => {
    const presentation = {
      schema: "zyra.product-presentation/v1",
      kind: "tool",
      phase: "failed",
      identity: "tool-local-failure",
      label: "tests",
      summary: "one test failed",
      severity: "error",
      impact: "local",
      code: "exit_1",
    }
    const normalized = frame(2, 1, {
      eventType: "runtime.tool.failed",
      presentation,
    })
    expect(normalized.presentation).toMatchObject({ impact: "local", code: "exit_1" })
    expect(() => frame(2, 1, {
      eventType: "runtime.tool.failed",
      presentation: { ...presentation, impact: "global-maybe" },
    })).toThrow(EventIngressError)
  })

  test("fails closed on schema mismatch, cross-task frame, and digest mutation", () => {
    expect(() =>
      normalizeEventFrame(
        { ...frameRaw(1, 0), schema: "zyra.event-ingress-frame/v2" },
        TASK,
        1,
      ),
    ).toThrow(EventIngressError)
    expect(() =>
      normalizeEventFrame(
        frameRaw(1, 0, { taskId: "task_other" }),
        TASK,
        1,
      ),
    ).toThrow(EventIngressError)
    expect(() =>
      normalizeEventFrame(
        frameRaw(1, 0, { contentDigest: "not-a-digest" }),
        TASK,
        1,
      ),
    ).toThrow(EventIngressError)
  })

  test("validates capability ownership and long-poll fallback", () => {
    const value = {
      schema: "zyra.event-ingress-capabilities/v1",
      protocol: "zyra.event-ingress/v1",
      taskId: TASK,
      canonicalOwner: "typescript.RuntimeEventSpine",
      ingressOwner: "browser.EventIngressCoordinator",
      canonicalWriteAllowed: false,
      snapshotRequired: true,
      subscribeBeforeSnapshot: true,
      subscriptionCursor: "bootstrap.cursor",
      subscriptionSequence: 0,
      subscriptionBoundary: 9,
      generation: 1,
      transports: [
        {
          kind: "sse",
          available: true,
          priority: 10,
          path: `/tasks/${TASK}/event-ingress/sse`,
          cursorMode: "query",
          customHeaders: true,
        },
        {
          kind: "long_poll",
          available: true,
          priority: 20,
          path: `/tasks/${TASK}/event-ingress/delta`,
          cursorMode: "query",
          customHeaders: true,
        },
      ],
      endpoints: {
        capabilities: `/tasks/${TASK}/event-ingress/capabilities`,
        snapshot: `/tasks/${TASK}/event-ingress/snapshot`,
        delta: `/tasks/${TASK}/event-ingress/delta`,
        sse: `/tasks/${TASK}/event-ingress/sse`,
        websocket: `/tasks/${TASK}/event-ingress/websocket`,
      },
      limits: {
        defaultPage: 100,
        maxPage: 1000,
        defaultWaitMs: 100,
        maxWaitMs: 25_000,
        defaultStreamMs: 15_000,
        maxStreamMs: 60_000,
        cursorTtlMs: 1_800_000,
      },
      schemas: {
        cursor: "zyra.event-ingress-cursor/v1",
        frame: "zyra.event-ingress-frame/v1",
        snapshot: "zyra.event-ingress-snapshot/v1",
        delta: "zyra.event-ingress-delta/v1",
        event: "zyra.runtime-event/v1",
      },
      filter: { digest: "filter" },
    }
    const capabilities = normalizeCapabilities(value, TASK)
    expect(capabilities.transports.map((item) => item.kind)).toEqual([
      TransportKind.SSE,
      TransportKind.LONG_POLL,
    ])
    expect(capabilities.subscriptionBoundary).toBe(9)
    expect(() =>
      normalizeCapabilities(
        {
          ...value,
          transports: [value.transports[0]],
        },
        TASK,
      ),
    ).toThrow(EventIngressError)
  })
})

describe("cursor, identity, and ordered delivery", () => {
  test("commits only an explicit predecessor chain and rejects sequence conflicts", () => {
    const ledger = new EventCursorLedger(TASK, { initialCursor: "cursor.0" })
    ledger.beginGeneration()
    const first = frame(3, 0)
    ledger.observeFrame(first)
    expect(ledger.commitFrame(first, first.cursor).advanced).toBe(true)
    expect(ledger.committedSequence).toBe(3)
    const gap = frame(8, 5)
    expect(ledger.observeFrame(gap).gap).toBe(true)
    expect(() => ledger.commitFrame(gap, gap.cursor)).toThrow(EventIngressError)
    const conflict = frame(8, 3, { eventId: "event_conflict", contentDigest: `sha256:${"b".repeat(64)}` })
    expect(() => ledger.observeFrame(conflict)).toThrow(EventIngressError)
    expect(ledger.audit().ok).toBe(true)
  })

  test("deduplicates exact identities and fails on same sequence with another digest", () => {
    const window = new BoundedEventIdentityWindow(64)
    const first = frame(1, 0)
    expect(window.observe(first, 0).disposition).toBe(DeliveryDisposition.ACCEPTED)
    expect(window.observe(first, 0).disposition).toBe(DeliveryDisposition.DUPLICATE)
    window.commit(first)
    expect(window.observe(first, 1).disposition).toBe(DeliveryDisposition.DUPLICATE)
    expect(() =>
      window.observe(
        frame(1, 0, {
          eventId: "event_other",
          contentDigest: `sha256:${"b".repeat(64)}`,
        }),
        1,
      ),
    ).toThrow(EventIngressError)
    expect(window.audit().ok).toBe(true)
  })

  test("buffers out-of-order frames and drains after the missing predecessor arrives", () => {
    const capacity = normalizeIngressCapacity({ maxItems: 64, maxBytes: 512 * 1024 })
    const buffer = new OrderedIngressBuffer(capacity)
    const later = frame(9, 4)
    const first = frame(4, 0)
    expect(buffer.insert(later, 0).disposition).toBe(DeliveryDisposition.BUFFERED_GAP)
    expect(buffer.drain(0).frames).toEqual([])
    expect(buffer.insert(first, 0).disposition).toBe(DeliveryDisposition.ACCEPTED)
    const drained = buffer.drain(0)
    expect(drained.frames.map((item) => item.sequence)).toEqual([4, 9])
    expect(drained.blockedByGap).toBe(false)
    expect(buffer.audit().ok).toBe(true)
  })

  test("reserves bounded capacity for effective events and evicts lower priority", () => {
    const capacity = normalizeIngressCapacity({
      maxItems: 64,
      maxBytes: 256 * 1024,
      terminalReserveItems: 8,
      terminalReserveBytes: 64 * 1024,
    })
    const buffer = new OrderedIngressBuffer(capacity)
    const low = {
      ...frame(1, 0, {
        effect: "non_effective",
        operation: "none",
        eventType: "runtime.telemetry.updated",
        metadata: { part_id: "low-1" },
      }),
      encodedBytes: 190 * 1024,
    }
    buffer.insert(low, 0)
    const high = {
      ...frame(2, 0, {
        eventType: "runtime.task.completed",
        inline: { status: "completed" },
      }),
      encodedBytes: 100 * 1024,
    }
    const inserted = buffer.insert(high, 0)
    expect(inserted.evicted).toHaveLength(1)
    expect(inserted.evicted[0]?.eventId).toBe(low.eventId)
    expect(buffer.snapshot().bytes).toBe(high.encodedBytes)
    expect(buffer.audit().ok).toBe(true)
  })
})

describe("subscribe-before-snapshot and gap recovery", () => {
  test("retains live frames before snapshot and prefers their fresh identity", () => {
    const capacity = normalizeIngressCapacity({ maxGapItems: 64, maxGapBytes: 512 * 1024 })
    const barrier = new SubscribeBeforeSnapshotBarrier(TASK, capacity)
    barrier.beginGeneration(1)
    barrier.markSubscribed(1)
    barrier.retainLive(frame(1, 0, { source: "sse" }))
    barrier.beginSnapshot(1)
    barrier.retainLive(frame(5, 1, { source: "sse" }))
    const page = normalizePage(
      snapshotRaw(
        [
          frameRaw(1, 0, { source: "snapshot" }),
          frameRaw(5, 1, { source: "snapshot" }),
        ],
        { next: 5, boundary: 5, complete: true },
      ),
      TASK,
      1,
    )
    const merged = barrier.mergeSnapshot(page, 0)
    expect(merged.frames.map((item) => item.sequence)).toEqual([1, 5])
    expect(merged.duplicates).toBe(2)
    expect(merged.interleaved).toBe(1)
    expect(barrier.snapshotComplete).toBe(true)
    expect(barrier.audit().ok).toBe(true)
  })

  test("repairs a missing predecessor from a catch-up page", () => {
    const capacity = normalizeIngressCapacity({ maxGapItems: 64, maxGapBytes: 512 * 1024 })
    const gaps = new EventGapRecovery(TASK, capacity)
    gaps.beginGeneration(1)
    gaps.observe(frame(9, 4), 0)
    expect(gaps.beginHealing()?.attempts).toBe(1)
    const page = normalizePage(
      {
        ...snapshotRaw(
          [
            frameRaw(4, 0),
            frameRaw(9, 4),
          ],
          { next: 9, boundary: 9, complete: true },
        ),
        schema: "zyra.event-ingress-delta/v1",
        cursorKind: "delta",
        caughtUp: true,
        highWatermark: 9,
        waitedMs: 0,
      },
      TASK,
      1,
    )
    const healed = gaps.applyPage(page, 0)
    expect(healed.resolved).toBe(true)
    expect(healed.frames.map((item) => item.sequence)).toEqual([4, 9])
    expect(gaps.snapshot()).toBeUndefined()
    expect(gaps.audit().ok).toBe(true)
  })

  test("commits a multi-frame snapshot before deciding that later frames are gaps", async () => {
    const source: EventIngressDataSource = {
      async capabilities(_taskId, _filter, _signal, generation = 1) {
        return {
          schema: "zyra.event-ingress-capabilities/v1",
          protocol: "zyra.event-ingress/v1",
          taskId: TASK,
          canonicalOwner: "typescript.RuntimeEventSpine",
          ingressOwner: "browser.EventIngressCoordinator",
          canonicalWriteAllowed: false,
          snapshotRequired: true,
          subscribeBeforeSnapshot: true,
          subscriptionCursor: "cursor.0",
          subscriptionSequence: 0,
          subscriptionBoundary: 22,
          generation,
          transports: [
            {
              kind: "long_poll",
              available: true,
              priority: 10,
              path: `/tasks/${TASK}/event-ingress/delta`,
              cursorMode: "query",
              customHeaders: true,
            },
          ],
          endpoints: {
            capabilities: `/tasks/${TASK}/event-ingress/capabilities`,
            snapshot: `/tasks/${TASK}/event-ingress/snapshot`,
            delta: `/tasks/${TASK}/event-ingress/delta`,
            sse: `/tasks/${TASK}/event-ingress/sse`,
            websocket: `/tasks/${TASK}/event-ingress/websocket`,
          },
          limits: {
            defaultPage: 100,
            maxPage: 1000,
            defaultWaitMs: 10,
            maxWaitMs: 25_000,
            defaultStreamMs: 15_000,
            maxStreamMs: 60_000,
            cursorTtlMs: 1_800_000,
          },
          schemas: {
            cursor: "zyra.event-ingress-cursor/v1",
            frame: "zyra.event-ingress-frame/v1",
            snapshot: "zyra.event-ingress-snapshot/v1",
            delta: "zyra.event-ingress-delta/v1",
            event: "zyra.runtime-event/v1",
          },
          filter: { digest: "filter" },
        }
      },
      async snapshot(_taskId, options) {
        return snapshotRaw(
          [
            frameRaw(20, 0, { generation: options.generation }),
            frameRaw(21, 20, {
              generation: options.generation,
              eventType: "runtime.text.ended",
              presentation: {
                schema: "zyra.product-presentation/v1",
                kind: "assistant",
                phase: "completed",
                identity: "answer_snapshot_1",
                label: "Assistant",
                streamId: "stream_snapshot_1",
                text: "snapshot answer",
              },
            }),
            frameRaw(22, 21, { generation: options.generation }),
          ],
          {
            next: 22,
            boundary: 22,
            complete: true,
            cursor: "cursor.22",
            generation: options.generation,
          },
        )
      },
      async delta(_taskId, options) {
        await new Promise((resolve) => setTimeout(resolve, 5))
        return {
          schema: "zyra.event-ingress-delta/v1",
          protocol: "zyra.event-ingress/v1",
          taskId: TASK,
          generation: options.generation,
          fromSequence: 0,
          nextSequence: 0,
          highWatermark: 22,
          cursor: options.cursor,
          cursorKind: "delta",
          caughtUp: true,
          hasMore: false,
          frames: [],
          waitedMs: 5,
          observedAtMs: 2_000,
          canonicalOwner: "typescript.RuntimeEventSpine",
          canonicalWriteAllowed: false,
        }
      },
      async openSse() {
        throw new Error("SSE is not selected by this test")
      },
      async openWebSocket() {
        throw new Error("WebSocket is not selected by this test")
      },
    }
    const coordinator = new EventIngressCoordinator(TASK, source, {
      transportPreference: [TransportKind.LONG_POLL],
      longPollMs: 5,
      reconnect: {
        attempts: 0,
        resyncAttempts: 0,
      },
    })
    const delivered = new Promise<{
      sequences: readonly number[]
      presentationText?: unknown
    }>((resolve, reject) => {
      const timeout = setTimeout(
        () => reject(new Error("multi-frame snapshot was not delivered")),
        1_000,
      )
      coordinator.subscribe({
        batch(batch) {
          if (!batch.snapshot || batch.events.length !== 3) return
          clearTimeout(timeout)
          resolve({
            sequences: batch.events.map((event) => event.globalSequence),
            presentationText: batch.presentations?.[0]?.presentation.text,
          })
        },
      })
    })
    void coordinator.start()
    try {
      expect(await delivered).toEqual({
        sequences: [20, 21, 22],
        presentationText: "snapshot answer",
      })
      expect(coordinator.snapshot().resyncAttempt).toBe(0)
      expect(coordinator.audit().ok).toBe(true)
    } finally {
      coordinator.close("multi-frame snapshot test complete")
    }
  })
})

describe("partial, orphan, and tombstone ingestion", () => {
  test("tracks a partial/final chain without becoming the UI reducer", () => {
    const assembler = new PartialEventAssembler(normalizeIngressCapacity(undefined))
    const partial = frame(1, 0, {
      eventType: "message.part.delta",
      effect: "non_effective",
      operation: "none",
      metadata: {
        settlement: "partial",
        part_id: "part-1",
        parent_event_id: "event-parent",
      },
    })
    // Mark the parent as known with an atomic canonical event.
    assembler.process(frame(2, 1, { eventId: "event-parent" }))
    const partialDecision = assembler.process(partial)
    expect(partialDecision.disposition).toBe(DeliveryDisposition.COALESCED)
    const final = frame(3, 2, {
      eventType: "message.part.final",
      metadata: {
        settlement: "final",
        part_id: "part-1",
        parent_event_id: "event-parent",
      },
    })
    const settled = assembler.process(final)
    expect(settled.settledChain?.eventIds).toEqual([partial.eventId, final.eventId])
    expect(assembler.snapshot().chains).toBe(0)
    expect(assembler.audit().ok).toBe(true)
  })

  test("holds an orphan until its parent and applies an explicit tombstone", () => {
    const assembler = new PartialEventAssembler(normalizeIngressCapacity(undefined))
    const orphan = frame(2, 1, {
      eventId: "event_orphan",
      metadata: { parent_event_id: "event_parent_late" },
    })
    expect(assembler.process(orphan).disposition).toBe(
      DeliveryDisposition.BUFFERED_ORPHAN,
    )
    const parent = frame(1, 0, { eventId: "event_parent_late" })
    const released = assembler.process(parent)
    expect(released.deliver.map((item) => item.eventId)).toEqual([
      "event_parent_late",
      "event_orphan",
    ])
    const tombstone = frame(3, 2, {
      eventType: "runtime.event.deleted",
      metadata: {
        settlement: "tombstone",
        tombstone_target_id: "event_orphan",
      },
    })
    expect(assembler.process(tombstone).disposition).toBe(
      DeliveryDisposition.TOMBSTONED,
    )
    expect(assembler.tombstoned("event_orphan")).toBe(true)
    expect(assembler.audit().ok).toBe(true)
  })
})

describe("transport parsing, subscriptions, retry, and disable paths", () => {
  test("parses fragmented SSE and rejects malformed JSON at the transport boundary", () => {
    const decoder = new SseFrameDecoder()
    expect(decoder.push(new TextEncoder().encode("id: 1\nevent: event\nda"))).toEqual([])
    const events = decoder.push(
      new TextEncoder().encode('ta: {"kind":"ready"}\n\n'),
    )
    expect(events).toEqual([
      {
        event: "event",
        data: '{"kind":"ready"}',
        id: "1",
        retry: undefined,
      },
    ])
    expect(decoder.finish()).toEqual([])
  })

  test("isolates a failing observer while delivering to healthy subscribers", () => {
    const registry = new IngressSubscriptionRegistry(TASK)
    let delivered = 0
    const failing: IngressObserver = {
      batch() {
        throw new Error("observer failed")
      },
    }
    registry.subscribe(failing)
    registry.subscribe({
      batch(batch) {
        delivered += batch.events.length
      },
    })
    const event = frame(1, 0).event
    const report = registry.dispatchBatch({
      taskId: TASK,
      generation: 1,
      events: [event],
      receipts: [],
      fromSequence: 0,
      sequence: 1,
      highWatermark: 1,
      receivedAt: 1,
      transport: TransportKind.SSE,
      snapshot: true,
      caughtUp: true,
    })
    expect(report.deliveredObservers).toBe(1)
    expect(report.failures).toHaveLength(1)
    expect(delivered).toBe(1)
  })

  test("backs off retryable failures, cools unhealthy transports, and fails permanent errors", () => {
    const supervisor = new ReconnectSupervisor(
      normalizeReconnectPolicy({
        attempts: 2,
        baseDelayMs: 10,
        maxDelayMs: 20,
        jitter: 0,
        transportFailureThreshold: 1,
      }),
      { now: () => 1_000, random: () => 0.5 },
    )
    const retry = supervisor.failure(
      new EventIngressError(
        IngressErrorCode.TRANSPORT_DISCONNECTED,
        "disconnected",
        { retryable: true },
      ),
      { transport: TransportKind.SSE },
    )
    expect(retry.action).toBe("retry")
    expect(retry.delayMs).toBe(10)
    const permanent = supervisor.failure(
      new EventIngressError(
        IngressErrorCode.SCHEMA_UNSUPPORTED,
        "unsupported",
      ),
      { transport: TransportKind.LONG_POLL },
    )
    expect(permanent.action).toBe("fail")
  })

  test("module disable paths fail visibly instead of falling back", () => {
    const ledger = new EventCursorLedger(TASK)
    ledger.disable()
    expect(() => ledger.beginGeneration()).toThrow(EventIngressError)
    try {
      ledger.beginGeneration()
    } catch (error) {
      expect((error as EventIngressError).code).toBe(IngressErrorCode.DISABLED)
    }
    const buffer = new OrderedIngressBuffer(normalizeIngressCapacity(undefined))
    buffer.disable()
    expect(() => buffer.insert(frame(1, 0), 0)).toThrow(EventIngressError)
    const assembler = new PartialEventAssembler(normalizeIngressCapacity(undefined))
    assembler.disable()
    expect(() => assembler.process(frame(1, 0))).toThrow(EventIngressError)
  })
})
