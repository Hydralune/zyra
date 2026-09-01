import { describe, expect, test } from "bun:test"
import type { PlanNodeProjection, TaskProjection } from "../../../packages/core/typed-api-client/src/index.ts"
import { SIDEBAR_DRAWER_QUERY, productConversationList } from "../src/app/workbench-app.tsx"
import {
  extractArtifactText,
  pendingConversationTurns,
  productConversationTasks,
  productTaskProgress,
  statusCopy,
} from "../src/components/tasks/product-task-detail.tsx"

function node(nodeId: string, status: string): PlanNodeProjection {
  return {
    nodeId,
    title: nodeId,
    description: `${nodeId} description`,
    status,
    dependsOn: [],
    artifactIds: [],
    metadata: {},
  }
}

function task(
  taskId: string,
  sessionId: string | undefined,
  createdAt: string,
  goal = taskId,
): TaskProjection {
  return {
    taskId,
    runId: `run_${taskId}`,
    sessionId,
    rootNodeId: `node_${taskId}`,
    userGoal: goal,
    status: "completed",
    createdAt,
    updatedAt: createdAt,
    planNodes: [],
    artifacts: [],
    metadata: {},
    binding: { taskId, runId: `run_${taskId}`, sessionId },
    terminal: true,
    active: false,
  }
}

describe("product frontstage", () => {
  test("projects plan completion without inventing runtime progress", () => {
    expect(productTaskProgress({
      terminal: false,
      planNodes: [
        node("understand", "completed"),
        node("implement", "running"),
        node("verify", "pending"),
      ],
    })).toEqual({ completed: 1, total: 3, percentage: 33 })

    expect(productTaskProgress({ terminal: true, planNodes: [] })).toEqual({
      completed: 0,
      total: 0,
      percentage: 100,
    })
  })

  test("previews only real text returned by the artifact content envelope", () => {
    expect(extractArtifactText({ content: { text: "verified result" } })).toBe("verified result")
    expect(extractArtifactText({ content: { bytes: "ZmFrZQ==" } })).toBeUndefined()
    expect(extractArtifactText({ text: "not the API envelope" })).toBeUndefined()
  })

  test("builds chronological conversations only from canonical session identity", () => {
    const first = task("task_first", "session_alpha", "2026-08-03T10:00:00Z", "First goal")
    const second = task("task_second", "session_alpha", "2026-08-03T11:00:00Z", "Follow up")
    const other = task("task_other", "session_beta", "2026-08-03T12:00:00Z")

    expect(productConversationTasks(second, [other, second, first]).map((entry) => entry.taskId))
      .toEqual(["task_first", "task_second"])
    expect(productConversationList([other, second, first])).toMatchObject([
      { key: "session_beta", turnCount: 1 },
      { key: "session_alpha", title: "First goal", turnCount: 2, latest: { taskId: "task_second" } },
    ])
  })

  test("projects only real in-flight submissions as optimistic turns", () => {
    expect(pendingConversationTurns({
      taskId: "task_alpha",
      active: { value: "Follow up on the plan", phase: "dispatching", taskId: "task_alpha" },
      queued: [
        { id: "queue_1", value: "And then verify it", phase: "queued", taskId: "task_alpha" },
        { id: "queue_2", value: "/cancel stop now", phase: "queued", taskId: "task_alpha" },
        { id: "queue_3", value: "Another session", phase: "queued", taskId: "task_beta" },
        { id: "queue_4", value: "Already settled", phase: "committed", taskId: "task_alpha" },
      ],
    })).toEqual([
      { id: "sending:Follow up on the plan", text: "Follow up on the plan", phase: "sending" },
      { id: "queue_1", text: "And then verify it", phase: "queued" },
    ])

    // A settled submission is already a real task; it must not double up.
    expect(pendingConversationTurns({
      taskId: "task_alpha",
      active: { value: "Committed already", phase: "committed", taskId: "task_alpha" },
    })).toEqual([])

    // The queue entry the active record was drained from renders exactly once.
    expect(pendingConversationTurns({
      taskId: "task_alpha",
      active: { value: "Drained", phase: "dispatching", taskId: "task_alpha", queueId: "queue_9" },
      queued: [{ id: "queue_9", value: "Drained", phase: "dispatching", taskId: "task_alpha" }],
    })).toEqual([{ id: "queue_9", text: "Drained", phase: "sending" }])
  })

  test("never reports an unverified run as a finished answer", () => {
    const base = task("task_verify", "session_verify", "2026-08-03T10:00:00Z")
    expect(statusCopy({
      ...base,
      metadata: { goal_contract: { kind: "direct_response" } },
    })).toMatchObject({ title: "没有生成有效回答", tone: "danger" })

    expect(statusCopy({
      ...base,
      metadata: { final_answer: "Here is the answer." },
    })).toMatchObject({ tone: "success" })

    expect(statusCopy({
      ...base,
      status: "running",
      terminal: false,
      active: true,
    })).toMatchObject({ tone: "active" })
  })

  test("keeps engineering evidence behind an explicit advanced surface", async () => {
    const app = await Bun.file(new URL("../src/app/workbench-app.tsx", import.meta.url)).text()
    const detail = await Bun.file(new URL("../src/components/tasks/product-task-detail.tsx", import.meta.url)).text()
    const command = await Bun.file(new URL("../src/components/command-input/command-input.tsx", import.meta.url)).text()
    const styles = await Bun.file(new URL("../src/styles.css", import.meta.url)).text()

    expect(app).toContain('className="app-shell product-shell"')
    expect(app).toContain("今天想让 Zyra 完成什么？")
    expect(app).toContain("高级 Workbench")
    expect(app).toContain("<ProductTaskDetail")
    expect(detail).toContain("<TaskDetail runtime={runtime} state={state} />")
    expect(detail).toContain('className="advanced-drawer"')
    expect(detail).toContain('className="product-answer product-answer-markdown"')
    expect(detail).toContain('className="product-disclosure product-run-summary"')
    expect(detail).toContain('className="product-jump-latest"')
    expect(detail).toContain("没有生成有效回答")
    expect(command).toContain("描述一个任务，或向 Zyra 提问")
    expect(command).toContain("在这个会话里继续，或输入 / 使用命令")
    expect(command).toContain("const selectedTask = taskContext")
    expect(app).toContain('route.kind === "task" || route.kind === "evidence"')
    expect(app).toContain("<EvidenceWorkbench")
    expect(app).toContain('className="product-connection-slot"')
    expect(app).toContain("最近会话")
    expect(styles).toContain(".product-starter-grid")
    expect(styles).toContain(".advanced-drawer-panel")
  })

  test("keeps a rendered conversation on screen while it refreshes", async () => {
    const detail = await Bun.file(new URL("../src/components/tasks/product-task-detail.tsx", import.meta.url)).text()
    // The projection outranks the request phase, so polling cannot replace the
    // transcript with a skeleton.
    expect(detail).toContain("if (state.task && state.task.taskId === state.taskId)")
    expect(detail).not.toContain("<PhaseRegion")
    // Refresh progress is a hairline, and a failed poll is a notice, not a wipe.
    expect(detail).toContain('className="product-sync-bar"')
    expect(detail).toContain('className="product-stale-notice"')
    // Disclosures are controlled, so a background refresh cannot reopen or
    // collapse a section the reader just toggled.
    expect(detail).toContain("function useDisclosure")
    expect(detail).toContain("ResizeObserver")
  })

  test("makes the drawer, disclosures, and deliverables reachable by keyboard", async () => {
    const app = await Bun.file(new URL("../src/app/workbench-app.tsx", import.meta.url)).text()
    const detail = await Bun.file(new URL("../src/components/tasks/product-task-detail.tsx", import.meta.url)).text()
    const command = await Bun.file(new URL("../src/components/command-input/command-input.tsx", import.meta.url)).text()
    const styles = await Bun.file(new URL("../src/styles.css", import.meta.url)).text()

    // The off-canvas sidebar leaves the tab order when it is not on screen, and
    // the stylesheet breakpoint is the single source of truth for "drawer".
    expect(app).toContain("inert={hidden ? true : undefined}")
    expect(app).toContain("aria-hidden={hidden || undefined}")
    expect(SIDEBAR_DRAWER_QUERY).toBe("(max-width: 860px)")
    expect(styles).toContain("@media (max-width: 860px)")

    // The advanced drawer is modal, so it traps Tab and restores focus.
    expect(detail).toContain("new FocusTrap(panelRef.current)")
    expect(detail).toContain("advancedTriggerRef.current?.focus()")

    // Deliverables are selectable, not decorative labels.
    expect(detail).toContain('role="tablist"')
    expect(detail).toContain('role="tabpanel"')

    // Status is never colour-only.
    expect(app).toContain("function statusText")
    expect(detail).toContain("function nodeStateLabel")

    // The composer describes itself without narrating every keystroke.
    expect(command).not.toContain('className="command-footer" aria-live="polite"')
    expect(command).toContain("autoSizeComposer")
  })
})
