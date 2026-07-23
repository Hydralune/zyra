import { createZyraApi } from "../src/api/index.ts"
import { TransportKind } from "../src/events/ingress/index.ts"
import {
  CanonicalProjectionStore,
  MemoryProjectionPersistence,
  ProjectionError,
  auditProjectionIntegrity,
  selectTaskSummary,
} from "../src/state/index.ts"

const baseUrl = process.argv[2]
if (!baseUrl) throw new Error("canonical-projection-probe requires a base URL")

const api = createZyraApi({
  baseUrl,
  retry: {
    attempts: 2,
    baseDelayMs: 5,
    maxDelayMs: 20,
    jitter: 0,
  },
  timeoutMs: 30_000,
})
const persistence = new MemoryProjectionPersistence()

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
    goal: "Exercise real canonical projection restore and reconnect.",
    autoRun: false,
    idempotencyKey: "canonical-projection-probe-create",
  })
  const task = created.mutation.task
  const first = new CanonicalProjectionStore({
    id: "integration-projection",
    persistence,
    restore: false,
    autoPersist: false,
  })
  const firstBinding = first.bind(api.events, task.taskId, {
    transportPreference: [TransportKind.LONG_POLL],
    snapshotPageSize: 1,
    deltaPageSize: 2,
    longPollMs: 50,
    heartbeatTimeoutMs: 5_000,
  })
  await waitFor(
    () =>
      Boolean(first.state.tasks[task.taskId]) &&
      (first.state.cursors[task.taskId]?.committedSequence ?? 0) > 0,
    "first canonical snapshot projection",
  )
  const initialSequence =
    first.state.cursors[task.taskId]?.committedSequence ?? 0
  const initialEventCount = Object.keys(first.state.causality.byEvent).length
  const cancelled = await api.lifecycle.cancel({
    taskId: task.taskId,
    runId: task.runId,
    reason: "Create an authoritative terminal projection.",
    idempotencyKey: "canonical-projection-probe-cancel",
  })
  await waitFor(
    () =>
      (first.state.cursors[task.taskId]?.committedSequence ?? 0) >
        initialSequence &&
      first.state.tasks[task.taskId]?.terminal === true,
    "terminal delta projection",
  ).catch((error) => {
    throw new Error(
      `${error instanceof Error ? error.message : String(error)} ` +
        JSON.stringify({
          task: first.state.tasks[task.taskId],
          cursor: first.state.cursors[task.taskId],
          events: Object.values(first.state.causality.byEvent),
          commands: first.state.commands,
        }),
    )
  })
  const beforeClose = first.select(selectTaskSummary(task.taskId))
  const terminalEventCount = Object.keys(
    first.state.causality.byEvent,
  ).length
  const beforeIntegrity = auditProjectionIntegrity(first.state)
  if (!beforeIntegrity.valid) {
    throw new Error(
      `First projection integrity failed: ${JSON.stringify(beforeIntegrity.issues)}`,
    )
  }
  await first.persist()
  firstBinding.close()
  await first.close("browser tab closed")

  const survivorCreated = await api.lifecycle.create({
    goal: "Browser projection close must not cancel this pending task.",
    autoRun: false,
    idempotencyKey: "canonical-projection-probe-survivor",
  })
  const survivor = survivorCreated.mutation.task
  const survivorStore = new CanonicalProjectionStore({
    id: "survivor-projection",
    persistence: new MemoryProjectionPersistence(),
    restore: false,
    autoPersist: false,
  })
  survivorStore.bind(api.events, survivor.taskId, {
    transportPreference: [TransportKind.LONG_POLL],
    longPollMs: 50,
  })
  await waitFor(
    () => Boolean(survivorStore.state.tasks[survivor.taskId]),
    "survivor projection snapshot",
  )
  await survivorStore.close("survivor browser tab closed")
  const survivorAfterClose = await api.tasks.get(survivor.taskId)

  const reopened = new CanonicalProjectionStore({
    id: "integration-projection",
    persistence,
    restore: true,
    autoPersist: false,
  })
  await reopened.ready
  const restoredSequence =
    reopened.state.cursors[task.taskId]?.committedSequence ?? 0
  const restoredEventCount = Object.keys(reopened.state.causality.byEvent).length
  const restoredEntityRevision = reopened.state.tasks[task.taskId]?.revision
  const reopenedBinding = reopened.bind(api.events, task.taskId, {
    transportPreference: [TransportKind.LONG_POLL],
    deltaPageSize: 2,
    longPollMs: 50,
  })
  await waitFor(
    () =>
      reopenedBinding.connection()?.phase === "live" &&
      (reopened.state.cursors[task.taskId]?.committedSequence ?? 0) >=
        restoredSequence,
    "restored cursor reconnect",
  )
  await new Promise((resolve) => setTimeout(resolve, 100))
  const afterReconnect = reopened.select(selectTaskSummary(task.taskId))
  const afterIntegrity = reopened.integrity()
  if (!afterIntegrity.valid) {
    throw new Error(
      `Restored projection integrity failed: ${JSON.stringify(afterIntegrity.issues)}`,
    )
  }
  if (reopened.state.tasks[task.taskId]?.revision !== restoredEntityRevision) {
    throw new Error("Reconnect replay changed the restored task entity revision.")
  }
  if (
    Object.keys(reopened.state.causality.byEvent).length !== restoredEventCount
  ) {
    throw new Error("Reconnect replay duplicated a canonical event identity.")
  }

  reopened.disable("integration disable behavior")
  let disableError = ""
  try {
    reopened.apply({
      taskId: task.taskId,
      generation: 1,
      events: [],
      receipts: [],
      cursor: reopened.state.cursors[task.taskId]?.cursor,
      fromSequence: restoredSequence,
      sequence: restoredSequence,
      highWatermark: restoredSequence,
      receivedAt: Date.now(),
      transport: TransportKind.LONG_POLL,
      snapshot: false,
      caughtUp: true,
    })
  } catch (error) {
    disableError =
      error instanceof ProjectionError ? error.code : String(error)
  }
  reopened.enable()
  reopenedBinding.close()
  await reopened.close("projection probe completed")

  if (cancelled.mutation.task.status !== "cancelled") {
    throw new Error("Backend cancellation did not commit.")
  }
  if (survivorAfterClose.status !== "pending") {
    throw new Error(
      `Browser close changed pending backend task to ${survivorAfterClose.status}.`,
    )
  }
  if (disableError !== "PROJECTION_DISABLED") {
    throw new Error(`Projection disable failed with ${disableError || "no error"}.`)
  }
  return {
    taskId: task.taskId,
    backend: {
      status: cancelled.mutation.task.status,
      survivorStatus: survivorAfterClose.status,
    },
    projection: {
      initialSequence,
      restoredSequence,
      reconnectSequence: afterReconnect.lastSequence,
      initialEventCount,
      terminalEventCount,
      restoredEventCount,
      entityRevision: restoredEntityRevision,
      lifecycle: beforeClose.task?.lifecycle,
      pendingPermissions: beforeClose.pendingPermissions,
      integrity: afterIntegrity.valid,
    },
    disable: {
      error: disableError,
    },
  }
}

try {
  const result = await run()
  process.stdout.write(`${JSON.stringify(result)}\n`)
} finally {
  api.close("Canonical projection probe completed.")
}
