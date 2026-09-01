import { CliApi } from "../../cli/src/api.ts"
import { createZyraApi } from "../src/api/index.ts"
import { WorkbenchController } from "../src/shell/workbench-controller.ts"
import { TaskLiveSync } from "../src/shell/task-live-sync.ts"
import {
  CanonicalProjectionStore,
  MemoryProjectionPersistence,
} from "../src/state/index.ts"

const [baseUrl, taskId, diffArtifactId] = process.argv.slice(2)
if (!baseUrl || !taskId) {
  throw new Error("product-live-cross-view-probe requires base URL and task id")
}

const cli = new CliApi({ baseUrl, timeoutMs: 30_000 })
const web = createZyraApi({
  baseUrl,
  timeoutMs: 30_000,
  retry: { attempts: 2, baseDelayMs: 10, maxDelayMs: 50, jitter: 0 },
})
const workbench = new WorkbenchController(web.tasks)
const liveSync = new TaskLiveSync(workbench, {
  activeIntervalMs: 120_000,
  pendingIntervalMs: 120_000,
})
const store = new CanonicalProjectionStore({
  id: "product-live-cross-view",
  persistence: new MemoryProjectionPersistence(),
  restore: false,
  autoPersist: false,
})

function waitFor(predicate: () => boolean, label: string, timeoutMs = 30_000): Promise<void> {
  const started = Date.now()
  return new Promise((resolve, reject) => {
    const poll = () => {
      if (predicate()) return resolve()
      if (Date.now() - started >= timeoutMs) {
        reject(new Error(`Timed out waiting for ${label}.`))
        return
      }
      setTimeout(poll, 20)
    }
    poll()
  })
}

let observedLiveText = ""
let observedSettling = false
const unsubscribeLive = liveSync.subscribe(() => {
  const assistant = liveSync.getSnapshot().assistant
  if (!assistant) return
  observedLiveText = assistant.text
  observedSettling ||= assistant.settling
})

let binding: ReturnType<CanonicalProjectionStore["bind"]> | undefined
try {
  const initial = await workbench.loadTask(taskId)
  if (initial.phase !== "ready" || initial.task?.terminal) {
    throw new Error("Cross-view probe requires a running canonical task.")
  }
  liveSync.bind(taskId)
  binding = store.bind(web.events, taskId, {
    live: (frame) => liveSync.observeLive(frame),
    batch: (batch) => liveSync.observeBatch(batch),
    status: (snapshot) => liveSync.observeConnection(snapshot),
  })
  await waitFor(
    () => liveSync.getSnapshot().ingressPhase === "live",
    "Web event ingress live phase",
  )
  process.stdout.write("READY\n")

  await waitFor(
    () => observedLiveText.includes("实时回答") && observedLiveText.endsWith("\n"),
    "exact Web live assistant text",
  )
  await waitFor(
    () => workbench.getSnapshot().detail.task?.terminal === true,
    "Web canonical terminal task reconciliation",
  )
  const [cliTask, webTask] = await Promise.all([
    cli.task(taskId),
    web.tasks.get(taskId),
  ])
  const cliAnswer = cliTask.metadata.final_answer
  const webAnswer = webTask.metadata.final_answer
  if (cliAnswer !== webAnswer || webAnswer !== "实时回答\n") {
    throw new Error("CLI and Web did not converge on the same canonical final answer.")
  }
  const verificationSame = JSON.stringify(cliTask.metadata.verification)
    === JSON.stringify(webTask.metadata.verification)
  if (!verificationSame) {
    throw new Error("CLI and Web observed different canonical verification facts.")
  }
  let diff: Record<string, unknown> | undefined
  if (diffArtifactId) {
    const [cliManifest, webManifest] = await Promise.all([
      cli.diffReviewManifest(taskId, diffArtifactId),
      web.tasks.diffReviewManifest(taskId, diffArtifactId),
    ])
    const cliDigest = JSON.stringify({
      diffId: cliManifest.diff_id,
      source: cliManifest.source,
      files: cliManifest.files,
      totals: cliManifest.totals,
    })
    const webDigest = JSON.stringify({
      diffId: webManifest.diff_id,
      source: webManifest.source,
      files: webManifest.files,
      totals: webManifest.totals,
    })
    if (cliDigest !== webDigest) {
      throw new Error("CLI and Web observed different canonical diff manifests.")
    }
    diff = {
      same: true,
      diffId: cliManifest.diff_id,
      files: Array.isArray(cliManifest.files) ? cliManifest.files.length : 0,
      physicalPathDisclosed: cliManifest.physical_path_disclosed,
    }
  }
  process.stdout.write(`${JSON.stringify({
    taskId,
    liveText: observedLiveText,
    settlingObserved: observedSettling,
    transientCleared: liveSync.getSnapshot().assistant === undefined,
    verificationSame,
    diff,
    cli: {
      status: cliTask.status,
      finalAnswer: cliAnswer,
      updatedAt: cliTask.updatedAt,
    },
    web: {
      status: webTask.status,
      finalAnswer: webAnswer,
      updatedAt: webTask.updatedAt,
    },
    ingress: {
      generation: liveSync.getSnapshot().ingressGeneration,
      phase: liveSync.getSnapshot().ingressPhase,
      projectedSequence: store.state.cursors[taskId]?.committedSequence,
    },
  })}\n`)
} finally {
  unsubscribeLive()
  binding?.close()
  liveSync.close()
  workbench.close("product live cross-view probe complete")
  await store.close("product live cross-view probe complete")
  cli.close("product live cross-view probe complete")
  web.close("product live cross-view probe complete")
}
