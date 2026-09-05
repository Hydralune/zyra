import { describe, expect, test } from "bun:test"
import { renderToStaticMarkup } from "react-dom/server"
import type {
  ArtifactProjection,
  PlanNodeProjection,
  TaskProjection,
} from "../../../packages/core/typed-api-client/src/index.ts"
import type { WorkbenchRuntime } from "../src/app/runtime.ts"
import type { TaskDetailState } from "../src/shell/workbench-controller.ts"
import { ProductTaskDetail } from "../src/components/tasks/product-task-detail.tsx"

function node(nodeId: string, status: string, metadata: Record<string, unknown> = {}): PlanNodeProjection {
  return {
    nodeId,
    title: `Step ${nodeId}`,
    description: `${nodeId} description`,
    status,
    dependsOn: [],
    artifactIds: [],
    metadata,
  }
}

function artifact(artifactId: string): ArtifactProjection {
  return {
    artifactId,
    kind: "code",
    path: `out/${artifactId}.ts`,
    mediaType: "text/plain",
    metadata: {},
  }
}

function task(overrides: Partial<TaskProjection> = {}): TaskProjection {
  return {
    taskId: "task_render_001",
    runId: "run_render_001",
    sessionId: "session_render_001",
    rootNodeId: "node_root",
    userGoal: "审查当前前端交互",
    status: "running",
    createdAt: "2026-08-04T00:00:00.000Z",
    updatedAt: "2026-08-04T00:04:00.000Z",
    planNodes: [
      node("understand", "completed", { tool: "read_files" }),
      node("implement", "running"),
      node("verify", "pending"),
    ],
    artifacts: [artifact("artifact_one"), artifact("artifact_two")],
    metadata: {},
    binding: { taskId: "task_render_001", runId: "run_render_001" },
    terminal: false,
    active: true,
    ...overrides,
  }
}

function store<T>(value: T) {
  const snapshot = () => value
  return { subscribe: () => () => {}, getSnapshot: snapshot }
}

function fakeRuntime(options: {
  activeSubmission?: { value: string; phase: string; taskId?: string }
  queued?: readonly Record<string, unknown>[]
  live?: boolean
  assistant?: Record<string, unknown>
} = {}): WorkbenchRuntime {
  return {
    api: {
      lifecycle: { inFlight: () => [] },
      tasks: { artifactContent: async () => ({}) },
    },
    workbench: store({ transportEnabled: true }),
    commands: store({
      phase: "idle",
      busy: false,
      active: options.activeSubmission,
      recent: [],
      revision: 1,
      enabled: true,
    }),
    queue: store({
      entries: options.queued ?? [],
      visible: options.queued ?? [],
      pendingCount: (options.queued ?? []).length,
      dispatchingCount: 0,
    }),
    liveSync: store({
      live: options.live ?? true,
      paused: false,
      syncing: false,
      consecutiveFailures: 0,
      taskId: "task_render_001",
      assistant: options.assistant,
      revision: 1,
    }),
    overlays: { open: () => {} },
    router: { openTask: () => {}, openTasks: () => {} },
    announcer: { announce: () => {} },
  } as unknown as WorkbenchRuntime
}

function detailState(overrides: Partial<TaskDetailState> = {}): TaskDetailState {
  const value = task()
  return {
    taskId: value.taskId,
    phase: "ready",
    task: value,
    generation: 1,
    ...overrides,
  }
}

describe("product conversation rendering", () => {
  test("opens a dedicated artifact view with previews and a way back to the conversation", () => {
    const current = task()
    const markup = renderToStaticMarkup(<ProductTaskDetail
      runtime={fakeRuntime()}
      state={{ taskId: current.taskId, task: current, phase: "ready", generation: 1 }}
      tasks={[current]}
      view="artifacts"
    />)
    expect(markup).toContain("会话交付物")
    expect(markup).toContain("返回对话")
    expect(markup).toContain("out/artifact_one.ts")
    expect(markup).toContain('class="product-disclosure product-deliverables" open=""')
    expect(markup).not.toContain('aria-label="会话消息"')
  })

  test("renders a live turn with collapsed steps and selectable deliverables", () => {
    const markup = renderToStaticMarkup(
      <ProductTaskDetail runtime={fakeRuntime()} state={detailState()} tasks={[task()]} />,
    )
    expect(markup).toContain("审查当前前端交互")
    expect(markup).toContain("实时更新中")
    // Steps and deliverables are present but secondary.
    expect(markup).toContain("执行过程")
    expect(markup).toContain("交付物")
    expect(markup).toContain('role="tablist"')
    expect(markup).toContain('aria-selected="true"')
    // Progress is exposed as a real progressbar, not a bare coloured div.
    expect(markup).toContain('role="progressbar"')
    expect(markup).toContain('aria-valuenow="33"')
    // Step state has a text equivalent alongside the icon.
    expect(markup).toContain("已完成")
    expect(markup).toContain("进行中")
    expect(markup).toContain("read_files")
  })

  test("keeps the transcript visible while a refresh is in flight or stale", () => {
    const refreshing = renderToStaticMarkup(
      <ProductTaskDetail
        runtime={fakeRuntime()}
        state={detailState({ syncing: true })}
        tasks={[task()]}
      />,
    )
    expect(refreshing).toContain("审查当前前端交互")
    expect(refreshing).toContain('data-busy="true"')

    const stale = renderToStaticMarkup(
      <ProductTaskDetail
        runtime={fakeRuntime()}
        state={detailState({
          staleSince: Date.now(),
          failure: {
            name: "TypeError",
            message: "network disconnected",
            retryable: true,
            occurredAt: Date.now(),
            attempt: 1,
          },
        })}
        tasks={[task()]}
      />,
    )
    expect(stale).toContain("审查当前前端交互")
    expect(stale).toContain("最新状态暂时读取失败")
  })

  test("shows an in-flight message immediately instead of dropping it", () => {
    const markup = renderToStaticMarkup(
      <ProductTaskDetail
        runtime={fakeRuntime({
          activeSubmission: {
            value: "再补充一个校验步骤",
            phase: "dispatching",
            taskId: "task_render_001",
          },
        })}
        state={detailState()}
        tasks={[task()]}
      />,
    )
    expect(markup).toContain("再补充一个校验步骤")
    expect(markup).toContain("正在提交给运行时")
    expect(markup).toContain("product-turn-pending")
  })

  test("renders live product presentation as safe Markdown without exposing raw HTML", () => {
    const assistant = {
      messageId: "answer_render_001",
      streamId: "stream_render_001",
      text: "## 实时结果\n\n- **第一项**\n- [文档](https://example.com)\n\n<script>secret</script>",
      generation: 1,
      firstLiveSequence: 1,
      lastLiveSequence: 4,
      partial: false,
      truncated: false,
      settling: false,
      startedAt: 1,
      updatedAt: 2,
    }
    const markup = renderToStaticMarkup(
      <ProductTaskDetail
        runtime={fakeRuntime({ assistant })}
        state={detailState()}
        tasks={[task()]}
      />,
    )
    expect(markup).toContain("实时结果")
    expect(markup).toContain("<strong>第一项</strong>")
    expect(markup).toContain('href="https://example.com/"')
    expect(markup).not.toContain("<script>")
    expect(markup).toContain("Raw HTML was refused")
    expect(markup).toContain("实时生成中")
  })

  test("renders empty, missing, and failed states without a task projection", () => {
    expect(renderToStaticMarkup(
      <ProductTaskDetail runtime={fakeRuntime()} state={{ phase: "idle", generation: 0 }} />,
    )).toContain("选择一个任务")

    expect(renderToStaticMarkup(
      <ProductTaskDetail
        runtime={fakeRuntime()}
        state={{ taskId: "task_gone", phase: "not-found", generation: 1 }}
      />,
    )).toContain("没有找到任务")

    const failed = renderToStaticMarkup(
      <ProductTaskDetail
        runtime={fakeRuntime()}
        state={{
          taskId: "task_broken",
          phase: "error",
          generation: 1,
          failure: {
            name: "TypeError",
            message: "network disconnected",
            retryable: true,
            occurredAt: 1,
            attempt: 1,
          },
        }}
      />,
    )
    expect(failed).toContain("暂时无法读取任务")
    expect(failed).toContain("重试")

    expect(renderToStaticMarkup(
      <ProductTaskDetail
        runtime={fakeRuntime()}
        state={{ taskId: "task_loading", phase: "loading", generation: 1 }}
      />,
    )).toContain("正在载入会话")
  })

  test("only auto-expands the run detail of the newest turn", () => {
    const first = task({
      taskId: "task_render_000",
      status: "completed",
      terminal: true,
      active: false,
      createdAt: "2026-08-03T00:00:00.000Z",
      updatedAt: "2026-08-03T00:10:00.000Z",
      metadata: { final_answer: "第一轮已经回答完毕。" },
    })
    const markup = renderToStaticMarkup(
      <ProductTaskDetail
        runtime={fakeRuntime()}
        state={detailState()}
        tasks={[first, task()]}
      />,
    )
    expect(markup).toContain("第一轮已经回答完毕。")
    // The finished turn stays collapsed; only the running one opens itself.
    expect([...markup.matchAll(/<details class="product-disclosure product-run-summary" open=""/g)])
      .toHaveLength(1)
  })
})
