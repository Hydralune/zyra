import { createZyraApi } from "../src/api/index.ts"
import {
  ConnectionPhase,
  TransportKind,
  type ConnectionSnapshot,
  type IngressBatch,
} from "../src/events/ingress/index.ts"

const baseUrl = process.argv[2]
if (!baseUrl) throw new Error("event-ingress-probe requires a base URL")

const api = createZyraApi({
  baseUrl,
  retry: {
    attempts: 2,
    baseDelayMs: 5,
    maxDelayMs: 10,
    jitter: 0,
  },
  timeoutMs: 30_000,
})

function waitFor(
  predicate: () => boolean,
  label: string,
  timeoutMs = 30_000,
): Promise<void> {
  const started = Date.now()
  return new Promise((resolve, reject) => {
    const poll = () => {
      if (predicate()) {
        resolve()
        return
      }
      if (Date.now() - started >= timeoutMs) {
        reject(new Error(`Timed out waiting for ${label}.`))
        return
      }
      setTimeout(poll, 20)
    }
    poll()
  })
}

async function run() {
  const created = await api.lifecycle.create({
    goal: "Exercise real snapshot, SSE, cursor resume, and long-poll event ingress.",
    autoRun: false,
    idempotencyKey: "event-ingress-probe-create",
  })
  const task = created.mutation.task
  const batches: IngressBatch[] = []
  const statuses: ConnectionSnapshot[] = []
  const errors: string[] = []
  const unsubscribe = api.events.subscribe(
    task.taskId,
    {
      batch(batch) {
        batches.push(batch)
      },
      status(snapshot) {
        statuses.push(snapshot)
        if (snapshot.phase === ConnectionPhase.FAILED) {
          errors.push(snapshot.error?.code ?? "failed_without_error")
        }
      },
    },
    {
      transportPreference: [TransportKind.SSE, TransportKind.LONG_POLL],
      heartbeatTimeoutMs: 1_000,
      snapshotPageSize: 1,
      deltaPageSize: 2,
      longPollMs: 100,
      reconnect: {
        attempts: 4,
        baseDelayMs: 10,
        maxDelayMs: 50,
        jitter: 0,
        stableResetMs: 500,
      },
    },
  )
  await waitFor(
    () => batches.some((batch) => batch.events.length > 0 && batch.snapshot),
    "initial snapshot batch",
  ).catch((error) => {
    throw new Error(
      `${error instanceof Error ? error.message : String(error)} ` +
      JSON.stringify(api.events.exportState()),
    )
  })
  const initialSequence = Math.max(...batches.map((batch) => batch.sequence))
  const cancelled = await api.lifecycle.cancel({
    taskId: task.taskId,
    runId: task.runId,
    reason: "Create a second real canonical event for the ingress probe.",
    idempotencyKey: "event-ingress-probe-cancel",
  })
  await waitFor(
    () => batches.some((batch) => batch.sequence > initialSequence),
    "post-snapshot live delta",
  ).catch((error) => {
    throw new Error(
      `${error instanceof Error ? error.message : String(error)} ` +
      JSON.stringify({
        initialSequence,
        batches: batches.map((batch) => ({
          sequence: batch.sequence,
          snapshot: batch.snapshot,
          eventIds: batch.events.map((event) => event.eventId),
        })),
        state: api.events.exportState(),
      }),
    )
  })
  await waitFor(
    () => statuses.some((snapshot) => snapshot.generation >= 2),
    "SSE reconnect generation",
    15_000,
  ).catch((error) => {
    throw new Error(
      `${error instanceof Error ? error.message : String(error)} ` +
      JSON.stringify(api.events.exportState()),
    )
  })
  const sseSnapshot = api.events.snapshot(task.taskId)
  const sseAudit = api.events.audit(task.taskId)
  unsubscribe()

  const longPollBatches: IngressBatch[] = []
  const longPollStatuses: ConnectionSnapshot[] = []
  const unsubscribeLongPoll = api.events.subscribe(
    task.taskId,
    {
      batch(batch) {
        longPollBatches.push(batch)
      },
      status(snapshot) {
        longPollStatuses.push(snapshot)
      },
    },
    {
      transportPreference: [TransportKind.LONG_POLL],
      snapshotPageSize: 1,
      deltaPageSize: 1,
      longPollMs: 50,
      heartbeatTimeoutMs: 5_000,
      reconnect: {
        attempts: 2,
        baseDelayMs: 5,
        maxDelayMs: 20,
        jitter: 0,
      },
    },
  )
  await waitFor(
    () => longPollBatches.some((batch) => batch.events.length > 0),
    "long-poll snapshot ingestion",
  )
  await waitFor(
    () =>
      longPollStatuses.some(
        (snapshot) =>
          snapshot.phase === ConnectionPhase.LIVE &&
          snapshot.transport === TransportKind.LONG_POLL,
      ),
    "long-poll live phase",
  )
  const longPollSnapshot = api.events.snapshot(task.taskId)
  const longPollAudit = api.events.audit(task.taskId)
  unsubscribeLongPoll()

  const survivorCreated = await api.lifecycle.create({
    goal: "A browser unsubscribe must not cancel this backend task.",
    autoRun: false,
    idempotencyKey: "event-ingress-probe-survivor",
  })
  const survivor = survivorCreated.mutation.task
  let survivorObserved = false
  const unsubscribeSurvivor = api.events.subscribe(
    survivor.taskId,
    (batch) => {
      if (batch.events.length) survivorObserved = true
    },
    {
      transportPreference: [TransportKind.LONG_POLL],
      longPollMs: 50,
    },
  )
  await waitFor(() => survivorObserved, "survivor task snapshot").catch((error) => {
    throw new Error(
      `${error instanceof Error ? error.message : String(error)} ` +
      JSON.stringify(api.events.exportState()),
    )
  })
  unsubscribeSurvivor()
  const survivorAfterUnsubscribe = await api.tasks.get(survivor.taskId)

  api.events.disable("disable-path probe")
  let disabledError = ""
  try {
    api.events.subscribe(task.taskId, () => {})
  } catch (error) {
    disabledError =
      error && typeof error === "object" && "code" in error
        ? String((error as { code: unknown }).code)
        : error instanceof Error
          ? error.name
          : String(error)
  }
  api.events.enable()

  const canonicalSequences = batches
    .flatMap((batch) => batch.events)
    .map((event) => event.globalSequence)
  const uniqueEventIds = new Set(
    batches.flatMap((batch) => batch.events).map((event) => event.eventId),
  )
  if (!canonicalSequences.length) throw new Error("SSE ingress delivered no canonical events")
  if (uniqueEventIds.size !== canonicalSequences.length) {
    throw new Error("SSE ingress redelivered a canonical event identity")
  }
  if (errors.length) throw new Error(`SSE ingress failed: ${errors.join(",")}`)
  if (!sseAudit?.ok) throw new Error(`SSE ingress audit failed: ${sseAudit?.findings.join(",")}`)
  if (!longPollAudit?.ok) {
    throw new Error(`Long-poll ingress audit failed: ${longPollAudit?.findings.join(",")}`)
  }
  if (disabledError !== "event_ingress_disabled") {
    throw new Error(`Disabled ingress failed with ${disabledError || "no error"}`)
  }
  if (cancelled.mutation.task.status !== "cancelled") {
    throw new Error(`Cancellation did not reach backend task owner: ${cancelled.mutation.task.status}`)
  }
  if (survivorAfterUnsubscribe.status !== "pending") {
    throw new Error(
      `Browser unsubscribe changed backend task status to ${survivorAfterUnsubscribe.status}`,
    )
  }
  return {
    taskId: task.taskId,
    runId: task.runId,
    sse: {
      batches: batches.length,
      events: canonicalSequences.length,
      sequences: canonicalSequences,
      generations: [...new Set(statuses.map((snapshot) => snapshot.generation))],
      phase: sseSnapshot?.phase,
      cursor: sseSnapshot?.cursor.committedSequence,
      audit: sseAudit?.ok,
    },
    longPoll: {
      batches: longPollBatches.length,
      events: longPollBatches.reduce((total, batch) => total + batch.events.length, 0),
      phase: longPollSnapshot?.phase,
      transport: longPollSnapshot?.transport,
      cursor: longPollSnapshot?.cursor.committedSequence,
      audit: longPollAudit?.ok,
    },
    backend: {
      status: cancelled.mutation.task.status,
      cancellationReceipt: cancelled.receipt.receiptId,
      survivorStatus: survivorAfterUnsubscribe.status,
    },
    disable: {
      error: disabledError,
    },
  }
}

try {
  const result = await run()
  process.stdout.write(`${JSON.stringify(result)}\n`)
} finally {
  api.close("Event ingress probe completed.")
}
