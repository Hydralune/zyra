import { describe, expect, test } from "bun:test"
import { PassThrough, Writable } from "node:stream"
import type { TaskMutationProjection, TaskProjection } from "@zyra/typed-api-client"
import type { CliApi, IngressPage } from "../src/api.ts"
import { executeProductInteractive } from "../src/commands/product.ts"
import { PromptDraft } from "../src/input/draft.ts"
import { ProductSessionState } from "../src/product/state/session-state.ts"
import { parseProductModels } from "../src/product/config/model.ts"
import { parseProductCommand, productCommandHelp } from "../src/product/commands/registry.ts"
import { parseProductDiffManifest, parseProductDiffPage } from "../src/product/diff/contracts.ts"
import { productPatchArtifacts } from "../src/product/diff/controller.ts"
import { ZYRA_UI_EVENT_SCHEMA, type ZyraUiEvent } from "../src/presentation/events.ts"
import { renderMarkdown } from "../src/tui/markdown.ts"
import { displayWidth, sanitizeTerminalText } from "../src/tui/text.ts"

class TtyInput extends PassThrough {
  isTTY = true
  raw = false
  setRawMode(value: boolean): void { this.raw = value }
}

class Capture extends Writable {
  isTTY = true
  columns = 100
  rows = 40
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

  test("bounds individual message content with an explicit truncation marker", () => {
    const state = new ProductSessionState({ messageCharacters: 16 })
    state.apply({
      schema: ZYRA_UI_EVENT_SCHEMA,
      eventId: "large_message",
      type: "assistant.message.completed",
      messageId: "message_large",
      text: "0123456789abcdef-must-not-remain-in-memory",
      source: "canonical_final_answer",
    })
    const message = state.snapshot().messages[0]!
    expect(message).toMatchObject({ truncated: true })
    expect(message.text).toContain("0123456789abcdef\n…[内容已截断")
    expect(message.text).not.toContain("must-not-remain-in-memory")
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

  test("holds incomplete inline Markdown while rendering fenced code incrementally", () => {
    expect(renderMarkdown("Use [documentation](https://exa", 60, { streaming: true }).join("\n")).toBe("Use")
    expect(renderMarkdown("Use [documentation](https://example.test)", 60, { streaming: false }).join("\n")).toContain("documentation <https://example.test>")
    expect(renderMarkdown("Value is `part", 60, { streaming: true }).join("\n")).toBe("Value is")
    const code = renderMarkdown("~~~ts\nconst value = 1", 60, { streaming: true }).join("\n")
    expect(code).toContain("┌─ ts")
    expect(code).toContain("│ const value = 1")
  })

  test("renders nested lists, task items, horizontal rules, and tables readably", () => {
    const rendered = renderMarkdown("- parent\n  - child\n- [x] done\n---\n| A | B |\n|---|---|\n| 1 | 2 |", 60).join("\n")
    expect(rendered).toContain("• parent")
    expect(rendered).toContain("  • child")
    expect(rendered).toContain("☑ done")
    expect(rendered).toContain("A │ B")
    expect(rendered).toContain("1 │ 2")
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

describe("canonical diff review contracts", () => {
  test("parses file and paged hunk facts without accepting physical paths", () => {
    const manifest = parseProductDiffManifest({
      schema: "zyra.diff-review-manifest.v1",
      physical_path_disclosed: false,
      diff_id: "diff_1",
      source: { artifact_revision: "revision_1" },
      files: [{
        file_id: "file_1",
        path: "src/main.ts",
        kind: "modified",
        additions: 1,
        deletions: 1,
        hunk_count: 1,
        page_count: 1,
      }],
    })
    const page = parseProductDiffPage({
      schema: "zyra.diff-review-page.v1",
      physical_path_disclosed: false,
      page_index: 0,
      page_count: 1,
      hunks: [{
        header: "@@ -1 +1 @@",
        lines: [
          { kind: "deleted", text: "old" },
          { kind: "added", text: "new" },
          { kind: "context", text: "" },
        ],
      }],
    })
    expect(manifest.files[0]).toMatchObject({ path: "src/main.ts", additions: 1, deletions: 1 })
    expect(page.lines).toEqual(["@@ -1 +1 @@", "-old", "+new", " "])
    expect(() => parseProductDiffManifest({
      schema: "zyra.diff-review-manifest.v1",
      physical_path_disclosed: true,
      source: {},
      files: [],
    })).toThrow("path-disclosure")
  })

  test("refuses secret, quarantined, and unverified patch artifacts", () => {
    const task = completedTask(7, "review", "session_review")
    task.artifacts = [
      { artifactId: "safe", kind: "patch", mediaType: "text/x-diff", metadata: { status: { integrity: "verified", exists: true, is_file: true } } },
      { artifactId: "secret", kind: "patch", mediaType: "text/x-diff", metadata: { security: { label: "secret" } } },
      { artifactId: "quarantined", kind: "patch", mediaType: "text/x-diff", metadata: { security: { trust: "quarantined" } } },
      { artifactId: "corrupt", kind: "patch", mediaType: "text/x-diff", metadata: { status: { integrity: "failed" } } },
    ]
    expect(productPatchArtifacts(task).map((artifact) => artifact.artifactId)).toEqual(["safe"])
  })
})

describe("canonical provider model catalog", () => {
  test("admits only canonical owner model projections", () => {
    const models = parseProductModels({
      schema: "zyra.provider-backend-api/v1",
      state_owner: "typescript.ProviderControlPlaneStore",
      result: [{
        providerId: "deepseek",
        modelId: "deepseek-v4-flash",
        displayName: "DeepSeek V4 Flash",
        family: "deepseek",
        contextWindow: 131_072,
        maximumOutputTokens: 16_384,
        capabilities: { reasoning: true },
        requestDefaults: { reasoning_effort: "high", thinking: { type: "enabled" } },
      }],
    })
    expect(models[0]).toMatchObject({
      providerId: "deepseek",
      modelId: "deepseek-v4-flash",
      reasoning: true,
      defaultReasoningEffort: "high",
      thinkingEnabled: true,
    })
    expect(() => parseProductModels({ schema: "zyra.provider-backend-api/v1", state_owner: "python", result: [] })).toThrow("canonical owner")
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
    let terminalStarts = 0
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
      ensureTerminal: async () => { terminalStarts += 1 },
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
    expect(terminalStarts).toBe(1)
    expect(stdout.text).toContain("第一轮")
    expect(stdout.text).toContain("第二轮")
    expect(stdin.raw).toBe(false)
    expect(stdin.isPaused()).toBe(true)
  })

  test("selects a canonical model and binds it to the next task creation", async () => {
    const calls: Array<{ goal: string; execution?: { providerId: string; modelId: string } }> = []
    let latest: TaskProjection | undefined
    let modelRequests = 0
    const api = {
      async providerModels() {
        modelRequests += 1
        return [{
          providerId: "deepseek",
          modelId: "deepseek-v4-flash",
          displayName: "DeepSeek V4 Flash",
          family: "deepseek",
          contextWindow: 131_072,
          maximumOutputTokens: 16_384,
          reasoning: true,
          defaultReasoningEffort: "high",
          thinkingEnabled: true,
        }]
      },
      async createPendingTask(goal: string, _sealed: boolean, sessionId?: string, execution?: { providerId: string; modelId: string }) {
        calls.push({ goal, execution })
        latest = completedTask(1, goal, sessionId ?? "missing")
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
      command: { kind: "interactive", baseUrl: "http://127.0.0.1:8000", autoStart: false, startupTimeoutMs: 1_000, timeoutMs: 10_000 },
      api,
      stdin,
      stdout,
      signal: new AbortController().signal,
      cwd: "G:\\agent-zoo\\zyra",
    })
    await waitUntil(() => stdin.raw)
    stdin.write("/model\r")
    await waitUntil(() => modelRequests === 1 && stdout.text.includes("选择后续任务模型"))
    stdin.write("\r")
    await waitUntil(() => stdout.text.includes("后续新 task"))
    stdin.write("使用选择的模型执行\r")
    await waitUntil(() => calls.length === 1 && stdin.raw)
    stdin.write("/exit\r")
    await executing
    expect(calls[0]).toEqual({
      goal: "使用选择的模型执行",
      execution: { providerId: "deepseek", modelId: "deepseek-v4-flash" },
    })
    expect(stdout.text).toContain("provider 默认 high")
    expect(stdout.text).toContain("catalog 尚未公布 supportedReasoningEfforts")
  })

  test("selects sealed autonomous mode and binds it to the next task creation", async () => {
    const calls: Array<{ goal: string; sealed: boolean }> = []
    let latest: TaskProjection | undefined
    const api = {
      async createPendingTask(goal: string, sealed: boolean, sessionId?: string) {
        calls.push({ goal, sealed })
        latest = completedTask(1, goal, sessionId ?? "missing")
        latest.metadata = {
          ...latest.metadata,
          sealed,
          competition_mode: sealed ? "sealed_autonomous" : "standard",
        }
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
      command: { kind: "interactive", baseUrl: "http://127.0.0.1:8000", autoStart: false, startupTimeoutMs: 1_000, timeoutMs: 10_000 },
      api,
      stdin,
      stdout,
      signal: new AbortController().signal,
      cwd: "G:\\agent-zoo\\zyra",
    })
    await waitUntil(() => stdin.raw)
    stdin.write("/mode\r")
    await waitUntil(() => stdout.text.includes("选择后续任务执行模式"))
    stdin.write("\u001b[B\r")
    await waitUntil(() => stdout.text.includes("execution · sealed_autonomous"))
    stdin.write("执行封闭任务\r")
    await waitUntil(() => calls.length === 1 && stdin.raw)
    stdin.write("/exit\r")
    await executing
    expect(calls).toEqual([{ goal: "执行封闭任务", sealed: true }])
  })
})
