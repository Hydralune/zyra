import { createZyraApi } from "../src/api/index.ts"

const baseUrl = process.argv[2]
const token = process.argv[3]
if (!baseUrl) throw new Error("embedded-client-probe requires a base URL")

const api = createZyraApi({
  baseUrl,
  token,
  retry: {
    attempts: 2,
    baseDelayMs: 5,
    maxDelayMs: 10,
    jitter: 0,
  },
  timeoutMs: 30_000,
})

async function run() {
  const health = await api.tasks.health()
  const readiness = await api.tasks.readiness()

  const createKey = "embedded-create-transport-contract"
  const firstCreate = await api.lifecycle.create({
    goal: "Verify the typed M2 transport against the real Zyra task store.",
    autoRun: false,
    idempotencyKey: createKey,
  })
  const replayCreate = await api.lifecycle.create({
    goal: "Verify the typed M2 transport against the real Zyra task store.",
    autoRun: false,
    idempotencyKey: createKey,
  })
  const task = firstCreate.mutation.task
  if (task.taskId !== replayCreate.mutation.task.taskId) {
    throw new Error("Idempotent task create produced different task ids")
  }
  if (firstCreate.receipt.receiptId !== replayCreate.receipt.receiptId) {
    throw new Error("Idempotent task create produced different receipt ids")
  }
  if (!replayCreate.receipt.replayed) throw new Error("Duplicate create was not marked replayed")

  const detail = await api.tasks.get(task.taskId)
  const page = await api.tasks.list({ limit: 1 })
  const eventsBefore = await api.tasks.events(task.taskId)

  const resumeKey = "embedded-resume-transport-contract"
  const resumed = await api.lifecycle.resume({
    taskId: task.taskId,
    runId: task.runId,
    idempotencyKey: resumeKey,
  })
  const replayResume = await api.lifecycle.resume({
    taskId: task.taskId,
    runId: task.runId,
    idempotencyKey: resumeKey,
  })
  if (resumed.receipt.receiptId !== replayResume.receipt.receiptId) {
    throw new Error("Idempotent task resume produced different receipt ids")
  }
  if (!replayResume.receipt.replayed) throw new Error("Duplicate resume was not marked replayed")

  const cancelCreated = await api.lifecycle.create({
    goal: "Verify typed cancellation reaches the real control path.",
    autoRun: false,
    idempotencyKey: "embedded-create-cancel-target",
  })
  const cancelTask = cancelCreated.mutation.task
  const cancelled = await api.lifecycle.cancel({
    taskId: cancelTask.taskId,
    runId: cancelTask.runId,
    reason: "Embedded transport cancellation proof.",
    idempotencyKey: "embedded-cancel-transport-contract",
  })
  const replayCancel = await api.lifecycle.cancel({
    taskId: cancelTask.taskId,
    runId: cancelTask.runId,
    reason: "Embedded transport cancellation proof.",
    idempotencyKey: "embedded-cancel-transport-contract",
  })
  if (cancelled.receipt.receiptId !== replayCancel.receipt.receiptId) {
    throw new Error("Idempotent task cancellation produced different receipt ids")
  }
  if (!replayCancel.receipt.replayed) throw new Error("Duplicate cancellation was not marked replayed")

  const taskCountBeforeDisable = (await api.tasks.list({ limit: 100 })).total
  api.client.disableTransport("embedded disable-path proof")
  let disableError = ""
  try {
    await api.lifecycle.create({
      goal: "This task must not be created while transport is disabled.",
      autoRun: false,
      idempotencyKey: "embedded-disabled-create",
    })
  } catch (error) {
    disableError =
      error && typeof error === "object" && "code" in error
        ? String((error as { code: unknown }).code)
        : error instanceof Error
          ? error.name
          : String(error)
  }
  if (disableError !== "transport_disabled") {
    throw new Error(`Disabled client failed with ${disableError || "no error"}`)
  }

  return {
    health: {
      status: health.status,
      service: health.service,
      apiVersion: health.apiVersion,
    },
    readiness: {
      ready: readiness.ready,
      owners: readiness.owners,
    },
    create: {
      taskId: task.taskId,
      runId: task.runId,
      initialStatus: detail.status,
      receiptId: firstCreate.receipt.receiptId,
      replayed: replayCreate.receipt.replayed,
      pageContainsTask: page.tasks.some((entry) => entry.taskId === task.taskId),
      eventCountBeforeResume: eventsBefore.length,
    },
    resume: {
      status: resumed.mutation.task.status,
      receiptId: resumed.receipt.receiptId,
      replayed: replayResume.receipt.replayed,
    },
    cancel: {
      taskId: cancelTask.taskId,
      status: cancelled.mutation.task.status,
      receiptId: cancelled.receipt.receiptId,
      replayed: replayCancel.receipt.replayed,
    },
    disable: {
      error: disableError,
      taskCountBeforeDisable,
    },
    lifecycleHistory: api.lifecycle.history(),
    telemetry: api.client.telemetry.summary(),
  }
}

try {
  const result = await run()
  process.stdout.write(`${JSON.stringify(result)}\n`)
} finally {
  api.close("Embedded probe completed.")
}
