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
  reasoning?: Record<string, unknown>
  /** Canonical spine events the execution stream projects over. */
  events?: readonly Record<string, unknown>[]
} = {}): WorkbenchRuntime {
  const events = options.events ?? []
  return {
    api: {
      lifecycle: { inFlight: () => [] },
      tasks: { artifactContent: async () => ({}) },
    },
    // The execution stream reads the canonical event spine through a projection
    // selector; the stub returns the supplied events for any selector, which is
    // all a static render needs.
    projections: {
      externalSelector: () => ({
        subscribe: () => () => {},
        getSnapshot: () => events,
      }),
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
      reasoning: options.reasoning,
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
  test("renders the selected turn as an execution stream, not a chat bubble", () => {
    const markup = renderToStaticMarkup(
      <ProductTaskDetail runtime={fakeRuntime()} state={detailState()} tasks={[task()]} />,
    )
    expect(markup).toContain("执行流")
    // The goal opens the stream.
    expect(markup).toContain('class="stream-entry" data-kind="goal"')
    expect(markup).toContain("审查当前前端交互")
    expect(markup).toContain("实时更新中")
  })

  test("returns an artifact view to the execution stream, not to the conversation", () => {
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
    // The artifact view replaces the stream rather than rendering alongside it.
    expect(markup).not.toContain('aria-label="执行流"')
  })

  test("projects spine events into ordered, typed stream rows", () => {
    const markup = renderToStaticMarkup(
      <ProductTaskDetail
        runtime={fakeRuntime({
          events: [
            {
              eventId: "e1", eventType: "runtime.tool.called", taskId: "task_render_001",
              runId: "run_render_001", toolCallId: "call_1", nodeId: "node_x",
              artifactIds: [], correlationId: "c1", mutationId: "m", sequence: 10,
              aggregateSequence: 1, createdAt: "2026-08-04T00:05:00.000Z",
              committedAt: "2026-08-04T00:05:00.000Z", summary: "file_edit: called",
              terminal: false, effective: true, entityRefs: [],
            },
            {
              eventId: "e2", eventType: "runtime.tool.succeeded", taskId: "task_render_001",
              runId: "run_render_001", toolCallId: "call_1", nodeId: "node_x",
              artifactIds: [], correlationId: "c1", mutationId: "m", sequence: 11,
              aggregateSequence: 2, createdAt: "2026-08-04T00:05:12.000Z",
              committedAt: "2026-08-04T00:05:12.000Z",
              summary: "file_edit: Committed src/flask/blueprints.py through SandboxGateway",
              terminal: false, effective: true, entityRefs: [],
            },
          ],
        })}
        state={detailState()}
        tasks={[task()]}
      />,
    )
    // One row per thing that happened: the call and its result collapse.
    expect(markup).toContain('data-kind="tool"')
    expect(markup).toContain('data-status="completed"')
    expect(markup).toContain("file_edit")
    expect(markup).toContain("Committed src/flask/blueprints.py")
    // The settling event's timestamp yields a real duration.
    expect(markup).toContain("12s")
    expect(markup).toContain("2 条记录")
  })

  test("drops bookkeeping rows that would only add noise", () => {
    const markup = renderToStaticMarkup(
      <ProductTaskDetail
        runtime={fakeRuntime({
          events: [
            {
              eventId: "e1", eventType: "runtime.node.created", taskId: "task_render_001",
              runId: "run_render_001", nodeId: "node_x", artifactIds: [],
              correlationId: "c1", mutationId: "m", sequence: 1, aggregateSequence: 1,
              createdAt: "2026-08-04T00:00:00.000Z", committedAt: "2026-08-04T00:00:00.000Z",
              summary: "Legacy node_created event normalized into the runtime event spine.",
              terminal: false, effective: true, entityRefs: [],
            },
            {
              eventId: "e2", eventType: "runtime.agent.message", taskId: "task_render_001",
              runId: "run_render_001", artifactIds: [], correlationId: "c1",
              mutationId: "m", sequence: 2, aggregateSequence: 2,
              createdAt: "2026-08-04T00:00:01.000Z", committedAt: "2026-08-04T00:00:01.000Z",
              summary: "running", terminal: false, effective: true, entityRefs: [],
            },
          ],
        })}
        state={detailState()}
        tasks={[task()]}
      />,
    )
    expect(markup).not.toContain("Legacy node_created")
    expect(markup).toContain("等待执行输出")
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

  test("renders live text in the stream while it is still arriving", () => {
    const assistant = {
      messageId: "answer_render_001",
      streamId: "stream_render_001",
      text: "正在检查 blueprints.py 的注册路径",
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
    expect(markup).toContain("正在检查 blueprints.py 的注册路径")
    expect(markup).toContain("正在生成…")
    expect(markup).toContain("stream-entry-live")
  })

  test("renders live reasoning on its own stream, separate from the answer", () => {
    const stream = (messageId: string, text: string) => ({
      messageId,
      streamId: `${messageId}:stream`,
      text,
      generation: 1,
      firstLiveSequence: 1,
      lastLiveSequence: 4,
      partial: false,
      truncated: false,
      settling: false,
      startedAt: 1,
      updatedAt: 2,
    })
    const markup = renderToStaticMarkup(
      <ProductTaskDetail
        runtime={fakeRuntime({
          reasoning: stream("reasoning_render_001", "先确认 blueprints.py 的注册顺序"),
          assistant: stream("answer_render_001", "正在检查注册路径"),
        })}
        state={detailState()}
        tasks={[task()]}
      />,
    )
    // Deliberation renders as its own typed row...
    expect(markup).toContain('data-kind="thinking"')
    expect(markup).toContain("先确认 blueprints.py 的注册顺序")
    expect(markup).toContain("正在推理…")
    // ...and the answer keeps its own row, so the two are never merged.
    expect(markup).toContain('data-kind="message"')
    expect(markup).toContain("正在检查注册路径")
    expect(markup.indexOf("先确认 blueprints.py 的注册顺序"))
      .toBeLessThan(markup.indexOf("正在检查注册路径"))
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

  test("keeps earlier turns reachable below the live stream", () => {
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
    // The stream owns the selected turn; the earlier turn stays reachable so a
    // follow-up task does not hide the work that produced its input.
    expect(markup).toContain('class="execution-stream"')
    expect(markup).toContain("第一轮已经回答完毕。")
  })
})
