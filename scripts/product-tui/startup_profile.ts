import { CliApi } from "../../apps/cli/src/api.ts"
import { ProductProjection } from "../../apps/cli/src/presentation/projection.ts"
import { ProductSessionState } from "../../apps/cli/src/product/state/session-state.ts"
import { renderProductState } from "../../apps/cli/src/presentation/renderer.ts"

const taskId = process.argv[2]
const baseUrl = process.argv[3] ?? "http://127.0.0.1:8000"
if (!taskId) throw new Error("usage: startup_profile.ts <task-id> [base-url]")

const elapsed = async <T>(operation: () => Promise<T>): Promise<{ value: T; ms: number }> => {
  const started = performance.now()
  const value = await operation()
  return { value, ms: performance.now() - started }
}

const api = new CliApi({ baseUrl, timeoutMs: 30_000 })
try {
  const task = await elapsed(() => api.task(taskId))
  const capabilities = await elapsed(() => api.ingressCapabilities(taskId))
  const projection = new ProductProjection({
    task: task.value,
    generation: capabilities.value.generation,
    cursor: capabilities.value.subscriptionCursor,
  })
  let frameCount = 0
  const snapshot = await elapsed(async () => {
    for await (const page of api.snapshotIngress(taskId, capabilities.value.generation)) {
      for (const frame of page.frames) {
        projection.apply(frame)
        frameCount += 1
      }
    }
  })
  projection.connected()
  const productSnapshot = await elapsed(async () => projection.snapshot())
  const state = new ProductSessionState()
  const reconcile = await elapsed(async () => state.reconcile(productSnapshot.value.events))
  const render = await elapsed(async () => renderProductState(state.snapshot(), {
    width: 100,
    height: 32,
    workspace: process.cwd(),
    composerText: "",
    running: true,
    scrollOffset: 0,
  }))
  process.stdout.write(`${JSON.stringify({
    schema: "zyra.product-tui-startup-profile/v1",
    task_id: taskId,
    frames: frameCount,
    ui_events: productSnapshot.value.events.length,
    frame_lines: render.value.length,
    timings_ms: {
      task: task.ms,
      capabilities: capabilities.ms,
      snapshot_and_projection: snapshot.ms,
      projection_snapshot: productSnapshot.ms,
      state_reconcile: reconcile.ms,
      render: render.ms,
      total: task.ms + capabilities.ms + snapshot.ms + productSnapshot.ms + reconcile.ms + render.ms,
    },
  })}\n`)
} finally {
  api.close()
}
