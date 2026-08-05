import type { CommandTransportRequest } from "@zyra/commands"
import { createIdentity, createRequestId } from "@zyra/typed-api-client"
import { CliApi } from "../../cli/src/api.ts"
import { createZyraApi } from "../src/api/index.ts"
import { TransportKind } from "../src/events/ingress/index.ts"
import {
  CanonicalProjectionStore,
  MemoryProjectionPersistence,
} from "../src/state/index.ts"

const baseUrl = process.argv[2]
if (!baseUrl) throw new Error("cross-entry-continuity-probe requires a base URL")

const cli = new CliApi({ baseUrl, timeoutMs: 30_000 })
const web = createZyraApi({
  baseUrl,
  timeoutMs: 30_000,
  retry: { attempts: 2, baseDelayMs: 5, maxDelayMs: 20, jitter: 0 },
})

function waitFor(
  predicate: () => boolean,
  label: string,
  timeoutMs = 30_000,
): Promise<void> {
  const started = Date.now()
  return new Promise((resolvePromise, reject) => {
    const poll = () => {
      if (predicate()) return resolvePromise()
      if (Date.now() - started >= timeoutMs) {
        reject(new Error(`Timed out waiting for ${label}.`))
        return
      }
      setTimeout(poll, 20)
    }
    poll()
  })
}

function nested(value: unknown, ...path: string[]): unknown {
  let selected = value
  for (const key of path) {
    if (!selected || typeof selected !== "object" || Array.isArray(selected)) return undefined
    selected = (selected as Record<string, unknown>)[key]
  }
  return selected
}

async function run() {
  const created = await cli.createPendingTask(
    "CLI creates this canonical task; Web must observe the same identity and revision.",
    false,
  )
  const task = created.task
  if (!task.sessionId) throw new Error("Cross-entry task has no canonical session identity.")
  const webDetail = await web.tasks.get(task.taskId)
  const webList = await web.tasks.list({ limit: 100 })
  if (webDetail.runId !== task.runId) throw new Error("Web observed a different canonical run.")
  if (!webList.tasks.some((entry) => entry.taskId === task.taskId)) {
    throw new Error("CLI-created task is absent from the Web task list contract.")
  }

  const store = new CanonicalProjectionStore({
    id: "cross-entry-web-projection",
    persistence: new MemoryProjectionPersistence(),
    restore: false,
    autoPersist: false,
  })
  const binding = store.bind(web.events, task.taskId, {
    transportPreference: [TransportKind.LONG_POLL],
    longPollMs: 50,
    deltaPageSize: 50,
  })
  await waitFor(
    () => Boolean(store.state.tasks[task.taskId])
      && (store.state.cursors[task.taskId]?.committedSequence ?? 0) > 0,
    "Web canonical projection of the CLI-created task",
  )

  const suffix = crypto.randomUUID().replaceAll("-", "")
  const request: CommandTransportRequest = {
    taskId: task.taskId,
    runId: task.runId,
    sessionId: task.sessionId,
    text: "/status",
    arguments: {},
    requestId: createRequestId(),
    commandId: createIdentity("control_command"),
    idempotencyKey: `cross-entry-${suffix}`,
    actorId: "zyra-cross-entry-probe",
    sealed: false,
    priority: "next",
    deliveryMode: "enqueue",
  }
  const webReceipt = await web.tasks.controlCommand({
    taskId: request.taskId,
    runId: request.runId,
    sessionId: request.sessionId,
    text: request.text,
    arguments: request.arguments,
    requestId: request.requestId,
    commandId: request.commandId,
    idempotencyKey: request.idempotencyKey,
    actorId: request.actorId,
    sealed: request.sealed,
    priority: request.priority,
    deliveryMode: request.deliveryMode,
  })
  const cliReplay = await cli.submitControlCommand(request)
  const webRequestId = nested(webReceipt, "control_request", "request_id")
  const cliRequestId = nested(cliReplay, "control_request", "request_id")
  const webCommandId = nested(webReceipt, "command_result", "command_id")
  const cliCommandId = nested(cliReplay, "command_result", "command_id")
  if (webRequestId !== cliRequestId || webCommandId !== cliCommandId) {
    throw new Error("CLI and Web observed different command receipt identities.")
  }
  const events = await cli.events(task.taskId)
  const effectEvents = events.filter((event) =>
    nested(event.payload, "request_id") === request.requestId
      && event.eventType.includes("succeeded")
  )
  if (effectEvents.length !== 1) {
    throw new Error(`Cross-entry retry produced ${effectEvents.length} command effects.`)
  }
  const webRevisionAfter = nested(webReceipt, "command_result", "revision_after")
  const cliRevisionAfter = nested(cliReplay, "command_result", "revision_after")
  if (webRevisionAfter !== cliRevisionAfter) {
    throw new Error("CLI and Web observed different canonical command revisions.")
  }
  await waitFor(
    () => (store.state.cursors[task.taskId]?.committedSequence ?? 0) > 1,
    "Web projection of the shared command receipt",
  )
  const projection = store.state.tasks[task.taskId]
  const cursor = store.state.cursors[task.taskId]
  const command = store.state.commands[request.commandId]
  if (!projection || !cursor || !command) {
    throw new Error("Web canonical projection is missing the shared command identity.")
  }
  binding.close()
  await store.close("cross-entry probe complete")
  return {
    task: {
      taskId: task.taskId,
      runId: task.runId,
      sessionId: task.sessionId,
      cliRevision: task.metadata.revision ?? null,
      webRevision: projection.revision,
      listObserved: true,
    },
    command: {
      requestId: request.requestId,
      commandId: request.commandId,
      webRequestId,
      cliRequestId,
      replayHeaderPresent: cliReplay.receipt_replayed,
      sameReceipt: true,
      effectEvents: effectEvents.length,
      projectedLifecycle: command.lifecycle,
    },
    event: {
      generation: cursor.generation,
      committedSequence: cursor.committedSequence,
      eventCount: (store.state.causality.byTask[task.taskId] ?? []).length,
    },
  }
}

try {
  process.stdout.write(`${JSON.stringify(await run())}\n`)
} finally {
  cli.close("cross-entry probe complete")
  web.close("cross-entry probe complete")
}
