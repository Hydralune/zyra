import { CliApi } from "../src/api.ts"
import { projectProductEvents } from "../src/presentation/projector.ts"
import { reduceProductEvents, renderProductSnapshot } from "../src/presentation/renderer.ts"

const [baseUrl, taskId] = process.argv.slice(2)
if (!baseUrl || !taskId) throw new Error("usage: product-local-failure-probe.ts <base-url> <task-id>")

const api = new CliApi({ baseUrl, timeoutMs: 30_000 })
try {
  const [task, ingress] = await Promise.all([
    api.task(taskId),
    api.openIngress(taskId),
  ])
  const events = projectProductEvents({ task, frames: ingress.frames })
  const state = reduceProductEvents(events)
  const rendered = renderProductSnapshot(events, {
    width: 120,
    workspace: process.cwd(),
  })
  process.stdout.write(`${JSON.stringify({
    taskStatus: state.taskStatus,
    taskFailedEvent: events.some((event) => event.type === "task.failed"),
    failedTools: state.tools.filter((tool) => tool.status === "failed"),
    completedTools: state.tools.filter((tool) => tool.status === "completed"),
    recoveryActivities: state.activities.filter((activity) => activity.category === "recovery"),
    renderedLocalFailure: rendered.includes("局部失败 · 任务已完成"),
    renderedCompleted: rendered.includes("任务已完成 · 可继续输入新任务"),
    frameTypes: ingress.frames.map((frame) => frame.eventType),
    presentations: ingress.frames.flatMap((frame) => frame.presentation ? [frame.presentation] : []),
  })}\n`)
} finally {
  api.close()
}
