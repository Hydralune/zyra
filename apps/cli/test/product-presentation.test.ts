import { describe, expect, test } from "bun:test"
import { readFileSync } from "node:fs"
import type { TaskProjection } from "@zyra/typed-api-client"
import type { IngressFrame } from "../src/api.ts"
import { ZYRA_UI_EVENT_SCHEMA, type UiPermissionSnapshot } from "../src/presentation/events.ts"
import { projectProductEvents } from "../src/presentation/projector.ts"
import { displayWidth, reduceProductEvents, renderProductSnapshot } from "../src/presentation/renderer.ts"

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

function frame(sequence: number, eventType: string, inline: Readonly<Record<string, unknown>>): IngressFrame {
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
    event: { inline, artifactRefs: [], identity: { taskId: fixture.task.taskId, runId: fixture.task.runId } },
    raw: {},
  }
}

describe("ZyraUiEvent/v1 product projection", () => {
  test("rebuilds a physical completed task without leaking raw runtime events", () => {
    expect(fixture.capture).toMatchObject({ physical_run: true, sanitized: true, observed_frame_count: 144 })
    expect(fixture.capture.observed_event_type_counts["runtime.agent.message"]).toBe(57)
    expect(fixture.capture.not_observed).toContain("runtime.text.delta")

    const projected = projectProductEvents({ task: fixture.task, frames: fixture.frames })
    expect(projected.every((event) => event.schema === ZYRA_UI_EVENT_SCHEMA)).toBe(true)
    expect(projected.map((event) => event.type)).toEqual([
      "session.started",
      "user.message",
      "activity.completed",
      "activity.completed",
      "activity.completed",
      "activity.completed",
      "activity.completed",
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
