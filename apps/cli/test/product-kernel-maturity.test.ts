import { describe, expect, test } from "bun:test"
import { PassThrough, Writable } from "node:stream"
import type { TaskMutationProjection, TaskProjection } from "@zyra/typed-api-client"
import type { CliApi, IngressPage } from "../src/api.ts"
import { executeProductInteractive } from "../src/commands/product.ts"
import { PromptDraft } from "../src/input/draft.ts"
import { ProductSessionState } from "../src/product/state/session-state.ts"
import { parseProductCommand, productCommandHelp } from "../src/product/commands/registry.ts"
import { ZYRA_UI_EVENT_SCHEMA, type ZyraUiEvent } from "../src/presentation/events.ts"
import { renderMarkdown } from "../src/tui/markdown.ts"
import { displayWidth, sanitizeTerminalText } from "../src/tui/text.ts"

class TtyInput extends PassThrough {
  isTTY = true
  raw = false
  setRawMode(value: boolean): void { this.raw = value }
}

class Capture extends Writable {
  text = ""
  override _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void): void {
    this.text += chunk.toString()
    callback()
  }
}

function completedTask(index: number, goal: string, sessionId: string): TaskProjection {
  return {
    taskId: `task_product_${index}`,
    runId: `run_product_${index}`,
    sessionId,
    rootNodeId: `node_product_${index}`,
    userGoal: goal,
    status: "completed",
    createdAt: "2026-09-01T00:00:00.000Z",
    updatedAt: "2026-09-01T00:00:01.000Z",
    planNodes: [],
    artifacts: [],
    metadata: { final_answer: `完成：${goal}` },
    binding: { taskId: `task_product_${index}`, runId: `run_product_${index}`, sessionId },
    terminal: true,
    active: false,
  }
}

function mutation(task: TaskProjection): TaskMutationProjection {
  return { task, events: [], receipt: {}, controls: {}, raw: {} }
}

async function waitUntil(predicate: () => boolean): Promise<void> {
  const deadline = Date.now() + 2_000
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error("test condition timed out")
    await new Promise((resolve) => setTimeout(resolve, 5))
  }
}

describe("product state kernel", () => {
  test("replays 100,000 presentation events into a bounded screen model", () => {
    const events: ZyraUiEvent[] = []
    for (let index = 0; index < 100_000; index += 1) {
      events.push({
        schema: ZYRA_UI_EVENT_SCHEMA,
        eventId: `event_${index}`,
        type: "assistant.message.completed",
        messageId: `message_${index}`,
        text: `message ${index}`,
        source: "stream",
      })
    }
    const state = new ProductSessionState({ messages: 128 })
    state.reconcile(events)
    const snapshot = state.snapshot()
    expect(snapshot.messages).toHaveLength(128)
    expect(snapshot.messages[0]?.messageId).toBe("message_99872")
    expect(snapshot.evicted.messages).toBe(99_872)
    state.reconcile([...events, {
      schema: ZYRA_UI_EVENT_SCHEMA,
      eventId: "event_final",
      type: "assistant.message.completed",
      messageId: "message_final",
      text: "final",
      source: "stream",
    }])
    expect(state.snapshot().messages.at(-1)?.text).toBe("final")
  })

  test("keeps streaming messages, tools, agents, permissions, and terminal state separate", () => {
    const state = new ProductSessionState()
    const events: ZyraUiEvent[] = [
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "session", type: "session.started", sessionId: "session", taskId: "task" },
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "start", type: "assistant.message.started", messageId: "answer" },
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "delta-1", type: "assistant.message.delta", messageId: "answer", text: "你" },
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "delta-2", type: "assistant.message.delta", messageId: "answer", text: "好" },
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "tool", type: "tool.started", toolCallId: "tool", name: "test", summary: "running" },
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "agent", type: "subagent.updated", agentId: "agent", label: "测试代理", status: "running" },
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "permission", type: "permission.requested", request: { requestId: "permission", action: "write", decisions: ["allow", "deny"] } },
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "done", type: "task.completed", taskId: "task", finalAnswer: "你好" },
    ]
    state.reconcile(events)
    expect(state.snapshot()).toMatchObject({
      taskStatus: "completed",
      messages: [{ text: "你好", streaming: true }],
      tools: [{ status: "running" }],
      agents: [{ status: "running" }],
      permissions: [{ requestId: "permission" }],
    })
  })
})

describe("terminal text and composer state", () => {
  test("removes CSI/OSC injection and measures grapheme clusters", () => {
    const injected = "safe\u001b[2J\u001b]0;stolen\u0007text"
    expect(sanitizeTerminalText(injected)).toBe("safetext")
    expect(displayWidth("e\u0301")).toBe(1)
    expect(displayWidth("👨‍👩‍👧‍👦")).toBe(2)
    expect(displayWidth("中文")).toBe(4)
  })

  test("renders Markdown structure without leaking terminal controls", () => {
    const rendered = renderMarkdown("# 标题\n\n- item\n\n> quote\n\n```ts\nconst x = 1\u001b[2J\n```\n\n[a](https://example.com)", 60).join("\n")
    expect(rendered).toContain("◆ 标题")
    expect(rendered).toContain("• item")
    expect(rendered).toContain("│ quote")
    expect(rendered).toContain("const x = 1")
    expect(rendered).toContain("a <https://example.com>")
    expect(rendered).not.toContain("\u001b")
  })

  test("edits whole graphemes and supports undo/redo", () => {
    const draft = new PromptDraft()
    draft.insert("A👨‍👩‍👧‍👦e\u0301")
    draft.deleteBackward()
    expect(draft.snapshot().text).toBe("A👨‍👩‍👧‍👦")
    draft.deleteBackward()
    expect(draft.snapshot().text).toBe("A")
    expect(draft.undo().text).toBe("A👨‍👩‍👧‍👦")
    expect(draft.redo().text).toBe("A")
  })
})

describe("product commands and continuous session", () => {
  test("discovers commands with explicit availability", () => {
    expect(parseProductCommand("/resume session_1")).toMatchObject({ definition: { name: "resume", availability: "idle" }, args: "session_1" })
    expect(parseProductCommand("/subagents")).toMatchObject({ definition: { name: "agents" } })
    expect(productCommandHelp(false)).toContain("/new")
    expect(productCommandHelp(true)).toContain("/redirect")
  })

  test("runs two terminal tasks in one TUI process with one canonical session id", async () => {
    const calls: Array<{ goal: string; sessionId?: string }> = []
    let latest: TaskProjection | undefined
    const api = {
      async createPendingTask(goal: string, _sealed: boolean, sessionId?: string) {
        calls.push({ goal, sessionId })
        latest = completedTask(calls.length, goal, sessionId ?? "missing")
        return mutation(latest)
      },
      async ingressCapabilities(taskId: string) {
        return { taskId, generation: 1, subscriptionCursor: "cursor", subscriptionSequence: 0, sseAvailable: true, raw: {} }
      },
      async *snapshotIngress(): AsyncGenerator<IngressPage> {
        yield { cursor: "cursor", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async task() { return latest! },
      async sessions() { return { sessions: [], total: 0, stateOwner: "task_store_projection" as const } },
    } as unknown as CliApi
    const stdin = new TtyInput()
    const stdout = new Capture()
    const executing = executeProductInteractive({
      command: { kind: "interactive", goal: "第一轮", baseUrl: "http://127.0.0.1:8000", autoStart: false, startupTimeoutMs: 1_000, timeoutMs: 10_000 },
      api,
      stdin,
      stdout,
      signal: new AbortController().signal,
      cwd: "G:\\agent-zoo\\zyra",
    })
    await waitUntil(() => calls.length === 1 && stdin.raw)
    stdin.write("第二轮\r")
    await waitUntil(() => calls.length === 2 && stdin.raw)
    stdin.write("/exit\r")
    const outcome = await executing
    expect(outcome.status).toBe("exited")
    expect(calls.map((item) => item.goal)).toEqual(["第一轮", "第二轮"])
    expect(calls[0]?.sessionId).toMatch(/^product:/)
    expect(calls[1]!.sessionId).toBe(String(calls[0]!.sessionId))
    expect(stdout.text).toContain("第一轮")
    expect(stdout.text).toContain("第二轮")
    expect(stdin.raw).toBe(false)
    expect(stdin.isPaused()).toBe(true)
  })
})
