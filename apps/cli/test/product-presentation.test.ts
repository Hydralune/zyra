import { describe, expect, test } from "bun:test"
import { readFileSync } from "node:fs"
import type { TaskProjection } from "@zyra/typed-api-client"
import type { IngressFrame } from "../src/api.ts"
import { ZYRA_UI_EVENT_SCHEMA, type UiPermissionSnapshot, type UiUserInputRequest } from "../src/presentation/events.ts"
import { projectProductEvents } from "../src/presentation/projector.ts"
import { ProductProjection } from "../src/presentation/projection.ts"
import { displayWidth, reduceProductEvents, renderProductSnapshot, renderProductState } from "../src/presentation/renderer.ts"

interface RealTaskFixture {
  capture: {
    physical_run: boolean
    sanitized: boolean
    observed_frame_count: number
    observed_event_type_counts: Readonly<Record<string, number>>
    not_observed: readonly string[]
  }
  task: TaskProjection
  frames: IngressFrame[]
}

const fixture = JSON.parse(readFileSync(new URL("./fixtures/product-tui-real-task.v1.json", import.meta.url), "utf8")) as RealTaskFixture

function snapshot(name: string): string {
  return readFileSync(new URL(`./snapshots/${name}`, import.meta.url), "utf8").replaceAll("\r\n", "\n")
}

function frame(sequence: number, eventType: string, inline: Readonly<Record<string, unknown>>, createdAt?: string): IngressFrame {
  return {
    schema: "zyra.event-ingress-frame/v1",
    kind: "event",
    source: "runtime-event-spine",
    generation: 1,
    taskId: fixture.task.taskId,
    sequence,
    previousSequence: sequence - 1,
    eventId: `event_contract_${sequence}`,
    eventType,
    event: { inline, artifactRefs: [], identity: { taskId: fixture.task.taskId, runId: fixture.task.runId }, createdAt },
    raw: {},
  }
}

describe("ZyraUiEvent/v2 product projection", () => {
  test("rebuilds a physical completed task without leaking raw runtime events", () => {
    expect(fixture.capture).toMatchObject({ physical_run: true, sanitized: true, observed_frame_count: 144 })
    expect(fixture.capture.observed_event_type_counts["runtime.agent.message"]).toBe(57)
    expect(fixture.capture.not_observed).toContain("runtime.text.delta")

    const projected = projectProductEvents({ task: fixture.task, frames: fixture.frames })
    expect(projected.every((event) => event.schema === ZYRA_UI_EVENT_SCHEMA)).toBe(true)
    expect(projected.map((event) => event.type)).toEqual([
      "session.started",
      "user.message",
      "plan.updated",
      "activity.completed",
      "activity.completed",
      "activity.completed",
      "activity.completed",
      "activity.completed",
      "verification.updated",
      "assistant.message.completed",
      "task.completed",
    ])
    expect(projected.find((event) => event.type === "assistant.message.completed")).toMatchObject({
      source: "canonical_final_answer",
      text: fixture.task.metadata.final_answer,
    })
    expect(projected.some((event) => event.type === "task.failed")).toBe(false)
    expect(JSON.stringify(projected)).not.toContain("runtime.")
    expect(JSON.stringify(projected)).not.toContain("superseded by task resume")
  })

  test("is deterministic under replay, duplicate frames, and input reordering", () => {
    const once = projectProductEvents({ task: fixture.task, frames: fixture.frames })
    const replayed = projectProductEvents({
      task: fixture.task,
      frames: [...fixture.frames].reverse().flatMap((item) => [item, { ...item }]),
    })
    expect(replayed).toEqual(once)
  })

  test("projects canonical plan revision, ordered steps, and bounded change history", () => {
    const root = fixture.task.planNodes.find((node) => node.nodeId === fixture.task.rootNodeId)!
    const [first, second, ...rest] = fixture.task.planNodes.filter((node) => node.nodeId !== fixture.task.rootNodeId)
    const task: TaskProjection = {
      ...fixture.task,
      updatedAt: "2026-09-01T12:00:00.000Z",
      planNodes: [root!, { ...first!, status: "superseded" }, { ...second!, status: "running", assignedWorkerId: "worker-review" }, ...rest],
      metadata: {
        ...fixture.task.metadata,
        stage_order: [second!.nodeId, first!.nodeId, ...rest.map((node) => node.nodeId)],
        dynamic_graph_ref: { graph_id: "graph:task-plan", revision: 7 },
        requirement_changes: [{
          event_id: "event_requirement_1",
          text: "先修复权限冲突，再继续验证",
          affected_node_ids: [first!.nodeId],
          replan_node_id: second!.nodeId,
          created_at: "2026-09-01T11:59:00.000Z",
        }],
      },
    }
    const event = projectProductEvents({ task }).find((item) => item.type === "plan.updated")
    expect(event).toMatchObject({
      type: "plan.updated",
      plan: {
        schema: "zyra.ui-plan/v1",
        revision: 7,
        revisionSource: "canonical_graph",
        graphId: "graph:task-plan",
        changes: [{ kind: "requirement_change", summary: "先修复权限冲突，再继续验证" }],
      },
    })
    if (event?.type !== "plan.updated") throw new Error("plan event missing")
    expect(event.plan.steps[0]).toMatchObject({ stepId: second!.nodeId, status: "running", assignedAgentId: "worker-review" })
    expect(event.plan.steps[1]).toMatchObject({ stepId: first!.nodeId, status: "superseded" })
    const state = reduceProductEvents(projectProductEvents({ task }))
    expect(state.plan?.revision).toBe(7)
    const rendered = renderProductSnapshot(projectProductEvents({ task }), { width: 120, workspace: "G:\agent-zoo\zyra" })
    expect(rendered).toContain("已更新计划")
    expect(rendered).not.toContain("计划 v7")
  })

  test("projects real context capacity into the footer without inventing usage", () => {
    const task: TaskProjection = {
      ...fixture.task,
      metadata: {
        ...fixture.task.metadata,
        last_code_worker_api_projection: {
          compact_state: {
            compact_needed: false,
            context_usage: {
              active_chars: 30_000,
              active_limit_chars: 100_000,
              ratio: 0.3,
              pressure: "normal",
            },
          },
          model_api: { input_tokens: 7_500, output_tokens: 1_250 },
        },
      },
    }
    const state = reduceProductEvents(projectProductEvents({ task }))
    expect(state.context).toEqual({
      activeChars: 30_000,
      activeLimitChars: 100_000,
      usedPercent: 30,
      remainingPercent: 70,
      compactNeeded: false,
      pressure: "normal",
      inputTokens: 7_500,
      outputTokens: 1_250,
    })
    expect(renderProductState(state, { width: 100, workspace: "G:\\agent-zoo\\zyra" })).toContain("70% 上下文")
  })

  test("projects explicit assistant text but never invents text from byte counts or digests", () => {
    const runningTask = { ...fixture.task, status: "running", terminal: false, active: true, metadata: {} }
    const projected = projectProductEvents({
      task: runningTask,
      frames: [
        frame(1, "runtime.text.started", { stream_id: "answer_1" }),
        frame(2, "runtime.text.delta", { stream_id: "answer_1", delta_bytes: 6, content_digest: "redacted" }),
        frame(3, "runtime.text.delta", { stream_id: "answer_1", presentation_text: "你好" }),
        frame(4, "runtime.text.delta", { stream_id: "answer_1", presentation_text: " world" }),
        frame(5, "runtime.text.ended", { stream_id: "answer_1" }),
      ],
    })
    expect(projected.filter((event) => event.type === "assistant.message.delta")).toMatchObject([{ text: "你好" }, { text: " world" }])
    expect(projected.find((event) => event.type === "assistant.message.completed")).toMatchObject({ text: "你好 world", source: "stream" })
    expect(JSON.stringify(projected)).not.toContain("redacted")
  })

  test("assembles only versioned assistant presentation and converges with canonical final text", () => {
    const finalAnswer = "你好\n\n**world**"
    const runningTask = { ...fixture.task, status: "completed", terminal: true, metadata: { ...fixture.task.metadata, final_answer: finalAnswer } }
    const frames: IngressFrame[] = [
      {
        ...frame(11, "runtime.text.started", { presentation_text: "raw-must-not-win" }),
        presentation: {
          schema: "zyra.product-presentation/v1",
          kind: "assistant",
          phase: "started",
          identity: "message_1",
          label: "Assistant",
          streamId: "answer_1",
        },
      },
      {
        ...frame(12, "runtime.text.delta", { presentation_text: "raw-must-not-win" }),
        presentation: {
          schema: "zyra.product-presentation/v1",
          kind: "assistant",
          phase: "delta",
          identity: "message_1",
          label: "Assistant",
          streamId: "answer_1",
          text: "你好\n\n",
        },
      },
      {
        ...frame(13, "runtime.text.delta", { presentation_text: "raw-must-not-win" }),
        presentation: {
          schema: "zyra.product-presentation/v1",
          kind: "assistant",
          phase: "delta",
          identity: "message_1",
          label: "Assistant",
          streamId: "answer_1",
          text: "**world**",
        },
      },
      {
        ...frame(14, "runtime.text.ended", { final_text: "raw-must-not-win" }),
        presentation: {
          schema: "zyra.product-presentation/v1",
          kind: "assistant",
          phase: "completed",
          identity: "message_1",
          label: "Assistant",
          streamId: "answer_1",
        },
      },
    ]
    const projected = projectProductEvents({ task: runningTask, frames })
    const assistant = projected.filter((event) => event.type.startsWith("assistant.message"))
    expect(assistant.at(-1)).toMatchObject({ type: "assistant.message.completed", text: finalAnswer, source: "stream" })
    expect(assistant.filter((event) => event.type === "assistant.message.completed")).toHaveLength(1)
    expect(JSON.stringify(projected)).not.toContain("raw-must-not-win")
  })

  test("reconciles a mismatched streamed completion onto the same message identity", () => {
    const task = { ...fixture.task, status: "completed", terminal: true, metadata: { ...fixture.task.metadata, final_answer: "canonical answer" } }
    const frames: IngressFrame[] = [{
      ...frame(21, "runtime.text.ended", {}),
      presentation: {
        schema: "zyra.product-presentation/v1",
        kind: "assistant",
        phase: "completed",
        identity: "message_mismatch",
        label: "Assistant",
        streamId: "answer_mismatch",
        text: "partial answer",
      },
    }]
    const projected = projectProductEvents({ task, frames })
    const completed = projected.filter((event) => event.type === "assistant.message.completed")
    expect(completed).toHaveLength(2)
    expect(completed[0]).toMatchObject({ messageId: "message:assistant:message_mismatch", text: "partial answer", source: "stream" })
    expect(completed[1]).toMatchObject({ messageId: "message:assistant:message_mismatch", text: "canonical answer", source: "canonical_final_answer" })
    expect(reduceProductEvents(projected).messages.filter((message) => message.role === "assistant")).toEqual([
      expect.objectContaining({ messageId: "message:assistant:message_mismatch", text: "canonical answer", streaming: false }),
    ])
  })

  test("consumes only versioned backend product presentation for worker, tool, and issue state", () => {
    const frames: IngressFrame[] = [
      {
        ...frame(201, "runtime.backend.dispatch.requested", { authorization: "must-not-leak" }),
        presentation: {
          schema: "zyra.product-presentation/v1",
          kind: "worker",
          phase: "dispatched",
          identity: "provider-code-worker",
          label: "provider-code-worker",
          summary: "backend local-sandbox-gateway",
          severity: "info",
        },
      },
      {
        ...frame(202, "runtime.tool.succeeded", { raw_stdout: "must-not-leak" }),
        presentation: {
          schema: "zyra.product-presentation/v1",
          kind: "tool",
          phase: "completed",
          identity: "tool_call_1",
          label: "tests",
          summary: "12 tests passed",
          durationMs: 1532,
          artifactIds: ["artifact_test_report"],
          outputArtifacts: [{
            artifactId: "artifact_test_stdout",
            stream: "stdout",
            title: "Test stdout",
            mediaType: "text/plain",
            sizeBytes: 10 * 1024 * 1024,
          }],
          severity: "info",
        },
      },
      {
        ...frame(203, "runtime.agent.message", { authorization: "must-not-leak" }),
        presentation: {
          schema: "zyra.product-presentation/v1",
          kind: "issue",
          phase: "failed",
          identity: "issue_provider",
          label: "Provider unavailable.",
          code: "provider_unavailable",
          retryable: true,
          severity: "error",
        },
      },
    ]
    const projected = projectProductEvents({ task: { ...fixture.task, status: "running", terminal: false }, frames })
    const state = reduceProductEvents(projected)
    expect(state.agents).toEqual([{ agentId: "provider-code-worker", label: "provider-code-worker", status: "dispatched", summary: "backend local-sandbox-gateway" }])
    expect(state.tools).toEqual([{
      toolCallId: "tool_call_1",
      name: "tests",
      summary: "12 tests passed",
      status: "completed",
      durationMs: 1532,
      artifactIds: ["artifact_test_report"],
      outputRefs: [{
        artifactId: "artifact_test_stdout",
        stream: "stdout",
        title: "Test stdout",
        mediaType: "text/plain",
        sizeBytes: 10 * 1024 * 1024,
      }],
    }])
    expect(state.issues).toEqual([{ issueId: "issue_provider", severity: "error", message: "Provider unavailable.", code: "provider_unavailable", retryable: true, recovery: undefined, impact: "task" }])
    expect(JSON.stringify(projected)).not.toContain("must-not-leak")
  })

  test("keeps local tool and worker failures distinct from the canonical task outcome", () => {
    const task = {
      ...fixture.task,
      status: "completed",
      terminal: true,
      metadata: { ...fixture.task.metadata, final_answer: "Recovered and completed." },
    }
    const frames: IngressFrame[] = [
      {
        ...frame(301, "runtime.tool.failed", {}),
        presentation: {
          schema: "zyra.product-presentation/v1",
          kind: "tool",
          phase: "failed",
          identity: "tool_local_failure",
          label: "tests",
          summary: "one test failed",
          severity: "error",
          impact: "local",
          code: "exit_1",
        },
      },
      {
        ...frame(302, "runtime.node.failed", {}),
        presentation: {
          schema: "zyra.product-presentation/v1",
          kind: "worker",
          phase: "failed",
          identity: "worker_failed",
          label: "worker-a",
          summary: "heartbeat timeout",
          severity: "warning",
          impact: "local",
        },
      },
      {
        ...frame(303, "runtime.recovery.completed", {}),
        presentation: {
          schema: "zyra.product-presentation/v1",
          kind: "activity",
          phase: "completed",
          identity: "recovery_worker_a",
          label: "Local failure recovery",
          summary: "recovery completed (replace_worker)",
          category: "recovery",
          severity: "info",
          impact: "local",
        },
      },
    ]
    const projected = projectProductEvents({ task, frames })
    const state = reduceProductEvents(projected)
    const rendered = renderProductSnapshot(projected, { width: 120, workspace: "G:\\agent-zoo\\zyra" })

    expect(state.taskStatus).toBe("completed")
    expect(state.tools).toContainEqual(expect.objectContaining({ status: "failed", impact: "local", code: "exit_1" }))
    expect(state.agents).toContainEqual(expect.objectContaining({ status: "failed", impact: "local" }))
    expect(state.activities).toContainEqual(expect.objectContaining({ category: "recovery", impact: "local", status: "completed" }))
    expect(projected.some((event) => event.type === "task.failed")).toBe(false)
    expect(rendered).toContain("本次工具调用失败，但任务仍可继续")
    expect(rendered).toContain("局部问题已隔离，任务整体状态不受影响")
    expect(rendered).toContain("Local failure recovery")
    expect(rendered).toContain("Recovered and completed.")
    expect(rendered).not.toContain("task_fixture_simple")
  })

  test("bounds hostile presentation labels, summaries, and artifact identities before state admission", () => {
    const huge = "x".repeat(10 * 1024 * 1024)
    const projected = projectProductEvents({
      task: { ...fixture.task, status: "running", terminal: false, active: true, metadata: {} },
      frames: [{
        ...frame(1, "runtime.tool.succeeded", { raw_stdout: huge }),
        presentation: {
          schema: "zyra.product-presentation/v1",
          kind: "tool",
          phase: "completed",
          identity: huge,
          label: huge,
          summary: huge,
          artifactIds: [huge],
          outputArtifacts: Array.from({ length: 32 }, (_, index) => ({
            artifactId: `artifact_output_${index}${huge}`,
            stream: index % 2 ? "stderr" : "stdout",
            title: huge,
            mediaType: huge,
            sizeBytes: Number.MAX_SAFE_INTEGER,
          })),
        },
      }],
    })
    const tool = reduceProductEvents(projected).tools[0]!
    expect(tool.toolCallId).toHaveLength(256)
    expect(tool.name).toHaveLength(256)
    expect(tool.summary).toHaveLength(2_000)
    expect(tool.artifactIds?.[0]).toHaveLength(256)
    expect(tool.outputRefs).toHaveLength(16)
    expect(tool.outputRefs?.[0]).toMatchObject({ stream: "stdout", sizeBytes: undefined })
    expect(tool.outputRefs?.[0]?.artifactId).toHaveLength(256)
    expect(tool.outputRefs?.[0]?.title).toHaveLength(256)
    expect(tool.outputRefs?.[0]?.mediaType).toHaveLength(128)
    expect(JSON.stringify(projected).length).toBeLessThan(20_000)
  })

  test("prefers canonical permission custody snapshots and keeps decisions fail-closed", () => {
    const permission: UiPermissionSnapshot = {
      requestId: "permission_fixture",
      status: "delivered",
      prompt: "写入项目文件",
      reason: "任务需要保存修改",
      target: "src/example.ts",
      risk: "medium",
      expiresAt: "2099-01-01T00:00:00.000Z",
      selectable: true,
    }
    const projected = projectProductEvents({
      task: { ...fixture.task, status: "running", terminal: false, active: true, metadata: {} },
      frames: [frame(1, "runtime.permission.pending", { permission_id: permission.requestId, action_digest: "must-not-render" })],
      permissions: [permission],
    })
    expect(projected.filter((event) => event.type === "permission.requested")).toEqual([
      expect.objectContaining({ request: expect.objectContaining({ requestId: permission.requestId, action: "写入项目文件", decisions: ["allow", "deny"] }) }),
    ])
    expect(JSON.stringify(projected)).not.toContain("must-not-render")
  })

  test("places a resolved structured question at its canonical time before later tool work", () => {
    const request: UiUserInputRequest = {
      requestId: "request_timeline",
      status: "answered",
      revision: 1,
      createdAt: "2026-09-02T10:00:00.000Z",
      updatedAt: "2026-09-02T10:02:00.000Z",
      questions: [{
        id: "database",
        header: "数据库",
        question: "请选择数据库。",
        options: [
          { label: "SQLite", description: "单机。" },
          { label: "PostgreSQL", description: "共享部署。" },
        ],
      }],
      answers: { database: { answers: ["PostgreSQL"] } },
    }
    const projected = projectProductEvents({
      task: { ...fixture.task, status: "running", terminal: false, active: true, metadata: {} },
      frames: [
        frame(1, "runtime.tool.called", { tool_call_id: "tool_question", tool_name: "request_user_input" }, "2026-09-02T10:01:00.000Z"),
        frame(2, "runtime.tool.succeeded", { tool_call_id: "tool_question", tool_name: "request_user_input" }, "2026-09-02T10:03:00.000Z"),
      ],
      userInputs: [request],
    })
    const types = projected.map((event) => event.type)
    expect(types.indexOf("tool.started")).toBeLessThan(types.indexOf("user_input.resolved"))
    expect(types.indexOf("user_input.resolved")).toBeLessThan(types.indexOf("tool.completed"))
  })
})

describe("stateful product projection recovery", () => {
  const runningTask: TaskProjection = { ...fixture.task, status: "running", terminal: false, active: true, metadata: {} }

  test("accepts exact replay and rejects a conflicting sequence or gap", () => {
    const first = frame(1, "runtime.text.started", { stream_id: "answer_1" })
    const projection = new ProductProjection({ task: runningTask, generation: 1 })
    expect(projection.apply(first)).toBe(true)
    expect(projection.apply({ ...first })).toBe(false)
    expect(() => projection.apply({ ...frame(1, "runtime.audit.finding", {}), eventId: "event_conflict" })).toThrow("reordered")
    expect(() => projection.apply(frame(3, "runtime.text.delta", { stream_id: "answer_1", presentation_text: "gap" }))).toThrow("gap")
    expect(projection.snapshot()).toMatchObject({ revision: "1:1", frameCount: 1 })
  })

  test("renders transient assistant deltas and converges on the durable end frame", () => {
    const presentation = (phase: "started" | "delta" | "completed", text?: string) => ({
      schema: "zyra.product-presentation/v1",
      kind: "assistant",
      phase,
      identity: "message_live_1",
      label: "Assistant",
      streamId: "provider:dispatch-live-1",
      ...(text ? { text } : {}),
    })
    const started = {
      ...frame(1, "runtime.text.started", {}),
      presentation: presentation("started"),
    }
    const live = {
      ...frame(1, "runtime.text.delta", { presentation_text: "实时" }),
      previousSequence: 1,
      liveSequence: 7,
      eventId: "live_delta_7",
      presentation: presentation("delta", "实时"),
    }
    const ended = {
      ...frame(2, "runtime.text.ended", { presentation_text: "实时" }),
      presentation: presentation("completed", "实时"),
    }
    const projection = new ProductProjection({ task: runningTask, generation: 1 })

    projection.apply(started)
    expect(projection.applyLive(live)).toBe(true)
    expect(projection.applyLive({ ...live })).toBe(false)
    expect(projection.snapshot().events).toContainEqual(
      expect.objectContaining({ type: "assistant.message.delta", text: "实时" }),
    )

    projection.apply(ended)
    const assistant = projection.snapshot().events.filter((event) =>
      event.type.startsWith("assistant.message")
    )
    expect(assistant).toEqual([
      expect.objectContaining({ type: "assistant.message.started" }),
      expect.objectContaining({ type: "assistant.message.completed", text: "实时" }),
    ])
  })

  test("retains a bounded raw-frame recovery window across 100,000 live events", () => {
    const projection = new ProductProjection({ task: runningTask, generation: 1 })
    for (let sequence = 1; sequence <= 100_000; sequence += 1) {
      projection.apply(frame(sequence, "runtime.agent.message", {}))
    }
    const projected = projection.snapshot()
    expect(projected).toMatchObject({
      revision: "1:100000",
      frameCount: 100_000,
      retainedFrameCount: 20_000,
    })
    expect(projected.events.length).toBeLessThan(20)
  })

  test("replaces generation from a canonical snapshot and converges with offline replay", () => {
    const replacementFrames = [
      { ...frame(1, "runtime.text.started", { stream_id: "answer_2" }), generation: 2 },
      { ...frame(2, "runtime.text.delta", { stream_id: "answer_2", presentation_text: "恢复成功" }), generation: 2 },
      { ...frame(3, "runtime.text.ended", { stream_id: "answer_2" }), generation: 2 },
    ]
    const projection = new ProductProjection({ task: runningTask, generation: 1 })
    projection.reconnecting(1)
    projection.replaceSnapshot({ task: runningTask, generation: 2, frames: [...replacementFrames].reverse(), cursor: "cursor_generation_2" })
    projection.connected()
    expect(projection.snapshot()).toMatchObject({ generation: 2, revision: "2:3", cursor: "cursor_generation_2", connection: "connected" })
    expect(reduceProductEvents(projection.snapshot().events).messages).toContainEqual(expect.objectContaining({ role: "assistant", text: "恢复成功" }))
  })

  test("produces the same product sequence for online apply and offline replay", () => {
    const frames = [
      frame(1, "runtime.text.started", { stream_id: "answer_online" }),
      frame(2, "runtime.text.delta", { stream_id: "answer_online", presentation_text: "一致" }),
      frame(3, "runtime.text.ended", { stream_id: "answer_online" }),
      frame(4, "runtime.node.failed", { reason: "recovered internal lease" }),
    ]
    const online = new ProductProjection({ task: runningTask, generation: 1 })
    for (const item of frames) online.apply(item)
    expect(online.snapshot().events).toEqual(projectProductEvents({ task: runningTask, frames }))
  })

  test("refreshes canonical terminal state when no raw terminal event exists", () => {
    const projection = new ProductProjection({ task: runningTask, generation: 1, frames: fixture.frames.map((item) => ({ ...item, taskId: runningTask.taskId })) })
    projection.refreshTask(fixture.task)
    const snapshot = projection.snapshot()
    expect(snapshot.connection).toBe("complete")
    expect(snapshot.events.at(-1)).toMatchObject({ type: "task.completed", finalAnswer: fixture.task.metadata.final_answer })
    expect(snapshot.events.some((event) => event.type === "task.failed")).toBe(false)
  })

  test("preserves canonical blocked state when the API omits its terminal flag", () => {
    const blocked: TaskProjection = {
      ...runningTask,
      status: "blocked",
      terminal: false,
      active: false,
      metadata: { failure_reason: "No provider route satisfied the canonical constraints." },
    }
    const projection = new ProductProjection({ task: runningTask, generation: 1 })
    projection.connected()
    projection.refreshTask(blocked)
    projection.complete()
    const events = projection.snapshot().events
    const state = reduceProductEvents(events)
    const rendered = renderProductSnapshot(events, { width: 100, workspace: "G:\\agent-zoo\\zyra" })

    expect(events).toContainEqual(expect.objectContaining({ type: "task.failed", status: "blocked" }))
    expect(state.taskStatus).toBe("blocked")
    expect(rendered).toContain("使用 /resume 选择并恢复该会话")
    expect(rendered).not.toContain(blocked.taskId)
    expect(rendered).not.toContain("task_product · running")
  })

  test("rebuilds canonical permission state without duplicating requests", () => {
    const projection = new ProductProjection({ task: runningTask, generation: 1 })
    projection.permissions([
      { requestId: "permission_2", status: "delivered", prompt: "运行测试", selectable: true },
      { requestId: "permission_2", status: "delivered", prompt: "运行测试", selectable: true },
    ])
    expect(projection.snapshot().events.filter((event) => event.type === "permission.requested")).toHaveLength(1)
    projection.permissions([{ requestId: "permission_2", status: "denied", prompt: "运行测试" }])
    expect(reduceProductEvents(projection.snapshot().events).permissions).toHaveLength(0)
  })
})

describe("product TUI render prototype", () => {
  const projected = projectProductEvents({ task: fixture.task, frames: fixture.frames })

  test("matches the 80-column golden snapshot", () => {
    const rendered = renderProductSnapshot(projected, { width: 80, workspace: "G:\\agent-zoo\\zyra" })
    expect(rendered).toBe(snapshot("product-tui-80.snap"))
    expect(rendered.split("\n").every((line) => displayWidth(line) <= 80)).toBe(true)
    expect(rendered).not.toContain("runtime.")
  })

  test("matches the 120-column golden snapshot", () => {
    const rendered = renderProductSnapshot(projected, { width: 120, workspace: "G:\\agent-zoo\\zyra" })
    expect(rendered).toBe(snapshot("product-tui-120.snap"))
    expect(rendered.split("\n").every((line) => displayWidth(line) <= 120)).toBe(true)
  })

  test("renders Chinese, emoji, code blocks, and reconnection within a narrow terminal", () => {
    const events = [
      ...projected.slice(0, 2),
      {
        schema: ZYRA_UI_EVENT_SCHEMA,
        eventId: "ui:assistant:code",
        type: "assistant.message.completed" as const,
        messageId: "message:assistant:code",
        text: "中文与 emoji 🧭\n\n```ts\nconst answer = 42\n```",
        source: "stream" as const,
      },
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "ui:reconnect", type: "transport.reconnecting" as const, attempt: 2 },
    ]
    const rendered = renderProductSnapshot(events, { width: 40, workspace: "G:\\agent-zoo\\zyra" })
    expect(rendered).toContain("中文与 emoji 🧭")
    expect(rendered).toContain("const answer = 42")
    expect(rendered).toContain("正在重连")
    expect(rendered.split("\n").every((line) => displayWidth(line) <= 40)).toBe(true)
    expect(reduceProductEvents(events).connection).toBe("reconnecting")
  })
})
