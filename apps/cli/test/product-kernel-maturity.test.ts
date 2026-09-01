import { describe, expect, test } from "bun:test"
import { PassThrough, Writable } from "node:stream"
import { mkdtemp } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import type { TaskMutationProjection, TaskProjection } from "@zyra/typed-api-client"
import type { CliApi, IngressPage } from "../src/api.ts"
import { agentContextLines, executeProductInteractive, executeProductResume, productWorkflowGoal } from "../src/commands/product.ts"
import { CliExitCode } from "../src/contracts.ts"
import { PromptDraft } from "../src/input/draft.ts"
import { ProductSessionState } from "../src/product/state/session-state.ts"
import { parseProductModels } from "../src/product/config/model.ts"
import { parseProductCommand, productCommandHelp } from "../src/product/commands/registry.ts"
import { parseProductDiffManifest, parseProductDiffPage } from "../src/product/diff/contracts.ts"
import { productPatchArtifacts } from "../src/product/diff/controller.ts"
import { ProductOnboardingStore } from "../src/product/onboarding/state.ts"
import { ZYRA_UI_EVENT_SCHEMA, type ZyraUiEvent } from "../src/presentation/events.ts"
import { renderMarkdown } from "../src/tui/markdown.ts"
import { renderProductState } from "../src/presentation/renderer.ts"
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

  test("virtualizes a 10,000-message transcript before Markdown rendering", () => {
    const state = new ProductSessionState()
    for (let index = 0; index < 10_000; index += 1) {
      state.apply({
        schema: ZYRA_UI_EVENT_SCHEMA,
        eventId: `long_${index}`,
        type: "assistant.message.completed",
        messageId: `long_message_${index}`,
        text: `history ${index} with **markdown**`,
        source: "stream",
      })
    }
    const rendered = renderProductState(state.snapshot(), {
      width: 100,
      height: 40,
      workspace: "G:\\agent-zoo\\zyra",
    })
    expect(rendered).toContain("history 9999 with markdown")
    expect(rendered).toContain("条更早消息虚拟化")
    expect(rendered).not.toContain("history 0 with markdown")
    expect(rendered.split("\n").length).toBeLessThanOrEqual(41)
  })

  test("keeps streaming messages, tools, agents, permissions, and terminal state separate", () => {
    const state = new ProductSessionState()
    const events: ZyraUiEvent[] = [
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "session", type: "session.started", sessionId: "session", taskId: "task" },
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "start", type: "assistant.message.started", messageId: "answer" },
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "delta-1", type: "assistant.message.delta", messageId: "answer", text: "你" },
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "delta-2", type: "assistant.message.delta", messageId: "answer", text: "好" },
      {
        schema: ZYRA_UI_EVENT_SCHEMA,
        eventId: "tool",
        type: "tool.started",
        toolCallId: "tool",
        name: "test",
        summary: "running",
        outputRefs: [{ artifactId: "artifact_stdout", stream: "stdout", title: "stdout", mediaType: "text/plain", sizeBytes: 10 * 1024 * 1024 }],
      },
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "agent", type: "subagent.updated", agentId: "agent", label: "测试代理", status: "running" },
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "permission", type: "permission.requested", request: { requestId: "permission", action: "write", decisions: ["allow", "deny"] } },
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "done", type: "task.completed", taskId: "task", finalAnswer: "你好" },
    ]
    state.reconcile(events)
    expect(state.snapshot()).toMatchObject({
      taskStatus: "completed",
      messages: [{ text: "你好", streaming: true }],
      tools: [{ status: "running", outputRefs: [{ artifactId: "artifact_stdout", stream: "stdout", sizeBytes: 10 * 1024 * 1024 }] }],
      agents: [{ status: "running" }],
      permissions: [{ requestId: "permission" }],
    })
    expect(renderProductState(state.snapshot(), { width: 100, height: 40, workspace: "G:\\agent-zoo\\zyra" })).toContain("stdout 可查看")
  })

  test("aggregates 100 agents while preserving selectable agent context", () => {
    const state = new ProductSessionState()
    const events: ZyraUiEvent[] = [
      { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "session-agents", type: "session.started", sessionId: "session-agents", taskId: "task-agents" },
      {
        schema: ZYRA_UI_EVENT_SCHEMA,
        eventId: "plan-agents",
        type: "plan.updated",
        plan: {
          schema: "zyra.ui-plan/v1",
          revision: 9,
          revisionSource: "canonical_graph",
          graphId: "graph:task-agents",
          changes: [],
          steps: [{
            stepId: "node-agent-099",
            label: "验证最终交付",
            status: "running",
            dependsOn: [],
            assignedAgentId: "agent-099",
          }],
        },
      },
      ...Array.from({ length: 100 }, (_, index): ZyraUiEvent => ({
        schema: ZYRA_UI_EVENT_SCHEMA,
        eventId: `agent-${index}`,
        type: "subagent.updated",
        agentId: `agent-${String(index).padStart(3, "0")}`,
        label: `Worker ${String(index).padStart(3, "0")}`,
        status: index === 98 ? "failed" : index === 99 ? "running" : "completed",
        summary: `bounded summary ${index}`,
        ...(index === 98 ? { impact: "local" as const, code: "worker_timeout", retryable: true, recovery: "replacement dispatched" } : {}),
      })),
    ]
    state.reconcile(events)
    const view = state.snapshot()
    expect(view.agents).toHaveLength(100)
    expect(renderProductState(view, { width: 120, height: 60, workspace: "G:\\agent-zoo\\zyra" })).toContain("100 总计")
    expect(agentContextLines(view, "agent-099")).toEqual(expect.arrayContaining([
      "identity · agent-099",
      "assigned plan steps · 1",
      expect.stringContaining("验证最终交付 · running"),
    ]))
    expect(agentContextLines(view, "agent-098")).toEqual(expect.arrayContaining([
      "impact · local",
      "code · worker_timeout",
      "recovery · replacement dispatched",
    ]))
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

  test("keeps the required Markdown structures bounded at every product width", () => {
    const source = [
      "# Release 标题 ✅",
      "> quoted **decision**",
      "1. ordered item with `inline code`",
      "- [ ] pending item",
      "| Key | Value |",
      "|---|---|",
      "| docs | [link](https://example.test/a/very/long/path) |",
      "```ts",
      "\tconst family = '👨‍👩‍👧‍👦'",
      "```",
    ].join("\n")
    for (const width of [60, 80, 120, 160]) {
      const lines = renderMarkdown(source, width)
      const rendered = lines.join("\n")
      expect(lines.filter((line) => displayWidth(line) > width)).toEqual([])
      expect(rendered).toContain("◆ Release 标题 ✅")
      expect(rendered).toContain("│ quoted decision")
      expect(rendered).toContain("1. ordered item with ‹inline code›")
      expect(rendered).toContain("☐ pending item")
      expect(rendered).toContain("Key │ Value")
      expect(rendered).toContain("docs │ link <https://example.test/a/very/long/path>")
      expect(rendered).toContain("│     const family = '👨‍👩‍👧‍👦'")
    }
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

  test("represents added, deleted, renamed, and binary change semantics", () => {
    const manifest = parseProductDiffManifest({
      schema: "zyra.diff-review-manifest.v1",
      physical_path_disclosed: false,
      diff_id: "diff_semantics",
      source: { artifact_revision: "revision_semantics" },
      files: [
        { file_id: "added", path: "src/added.ts", kind: "added", additions: 2 },
        { file_id: "deleted", path: "src/deleted.ts", kind: "deleted", deletions: 3 },
        {
          file_id: "renamed",
          path: "src/new-name.ts",
          previous_path: "src/old-name.ts",
          kind: "renamed",
        },
        {
          file_id: "binary",
          path: "assets/logo.bin",
          kind: "binary",
          change_kind: "modified",
          binary: true,
        },
      ],
    })

    expect(manifest.files).toEqual([
      expect.objectContaining({ path: "src/added.ts", kind: "added", binary: false }),
      expect.objectContaining({ path: "src/deleted.ts", kind: "deleted", binary: false }),
      expect.objectContaining({ path: "src/new-name.ts", previousPath: "src/old-name.ts", kind: "renamed" }),
      expect.objectContaining({ path: "assets/logo.bin", kind: "modified", binary: true }),
    ])
    expect(() => parseProductDiffManifest({
      schema: "zyra.diff-review-manifest.v1",
      physical_path_disclosed: false,
      diff_id: "diff_escape",
      source: { artifact_revision: "revision_escape" },
      files: [{ file_id: "escape", path: "../secret.txt", kind: "modified" }],
    })).toThrow("workspace-logical path")
    expect(() => parseProductDiffManifest({
      schema: "zyra.diff-review-manifest.v1",
      physical_path_disclosed: false,
      diff_id: "diff_bad_binary",
      source: { artifact_revision: "revision_bad_binary" },
      files: [{ file_id: "bad", path: "logo.bin", kind: "binary", binary: false }],
    })).toThrow("binary kind")
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
  test("guides a clean first launch through canonical readiness and model discovery", async () => {
    const stateDirectory = await mkdtemp(join(tmpdir(), "zyra-first-launch-"))
    const onboardingStore = new ProductOnboardingStore(stateDirectory)
    const api = {
      async readiness() {
        return {
          ready: true,
          status: "ready",
          apiVersion: "1.0",
          owners: { tasks: true, permissions: true, providers: true },
          blockers: [],
        }
      },
      async providerModels() {
        return [{
          providerId: "deepseek",
          modelId: "deepseek-v4-flash",
          displayName: "DeepSeek V4 Flash",
          family: "deepseek",
          contextWindow: 131_072,
          maximumOutputTokens: 16_384,
          reasoning: true,
          supportedReasoningEfforts: ["low", "high", "max"],
          defaultReasoningEffort: "high",
          thinkingEnabled: true,
        }]
      },
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
      draftStore: null,
      onboardingStore,
    })
    await waitUntil(() => stdin.raw && stdout.text.includes("首次使用设置"))
    stdin.write("\r")
    await waitUntil(() => stdout.text.includes("首次使用设置完成"))
    stdin.write("/exit\r")
    expect(await executing).toMatchObject({ status: "exited", exitCode: CliExitCode.SUCCESS })
    expect(stdout.text).toContain("3/3 ready")
    expect(stdout.text).toContain("1 providers · 1 available models")
    expect(await onboardingStore.load()).toMatchObject({
      status: "complete",
      state: { choice: "automatic" },
    })
  })

  test("cancels an idle interactive session and restores terminal state on abort", async () => {
    const controller = new AbortController()
    const stdin = new TtyInput()
    const stdout = new Capture()
    const executing = executeProductInteractive({
      command: { kind: "interactive", baseUrl: "http://127.0.0.1:8000", autoStart: false, startupTimeoutMs: 1_000, timeoutMs: 10_000 },
      api: {} as CliApi,
      stdin,
      stdout,
      signal: controller.signal,
      cwd: "G:\\agent-zoo\\zyra",
      draftStore: null,
    })

    await waitUntil(() => stdin.raw)
    stdin.write("尚未提交的草稿")
    controller.abort(new Error("test interrupt"))
    const outcome = await executing

    expect(outcome).toMatchObject({ status: "cancelled", exitCode: CliExitCode.CANCELLED })
    expect(stdin.raw).toBe(false)
    expect(stdin.isPaused()).toBe(true)
    expect(stdout.text).toContain("\u001b[0m\u001b[?25h\u001b[?2004l")
  })

  test("discovers commands with explicit availability", () => {
    expect(parseProductCommand("/resume session_1")).toMatchObject({ definition: { name: "resume", availability: "idle" }, args: "session_1" })
    expect(parseProductCommand("/subagents")).toMatchObject({ definition: { name: "agents" } })
    expect(productCommandHelp(false)).toContain("/new")
    expect(productCommandHelp(true)).toContain("/redirect")
    expect(productCommandHelp(true)).toContain("/plan")
    expect(productCommandHelp(true)).toContain("/tools")
    expect(productCommandHelp(true)).toContain("/compact [focus]")
    expect(productCommandHelp(false)).toContain("/review [focus]")
    expect(productCommandHelp(false)).toContain("/init [focus]")
    expect(productWorkflowGoal("review", "authentication")).toContain("Do not modify files")
    expect(productWorkflowGoal("init")).toContain("preserve existing user rules")
  })

  test("starts a review command as a canonical task with a bounded product workflow", async () => {
    const calls: string[] = []
    let latest: TaskProjection | undefined
    const api = {
      async createPendingTask(goal: string, _sealed: boolean, sessionId?: string) {
        calls.push(goal)
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
      draftStore: null,
      onboardingStore: null,
    })
    await waitUntil(() => stdin.raw)
    stdin.write("/review authentication boundary\r")
    await waitUntil(() => calls.length === 1 && stdin.raw)
    stdin.write("/exit\r")
    await executing
    expect(calls[0]).toContain("independent code reviewer")
    expect(calls[0]).toContain("Review focus: authentication boundary")
    expect(stdout.text).not.toContain("runtime.")
  })

  test("keeps the session list usable when historical entries are isolated", async () => {
    const api = {
      async sessions() {
        return {
          sessions: [],
          total: 1,
          stateOwner: "task_store_projection" as const,
          degraded: [{ index: 0, code: "session_projection_invalid" as const, message: "legacy record" }],
        }
      },
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
    stdin.write("/sessions\r")
    await waitUntil(() => stdout.text.includes("旧版或损坏会话记录已安全隔离"))
    stdin.write("/exit\r")
    await executing
    expect(stdout.text).toContain("没有可恢复的历史会话")
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
    expect(calls[0]?.sessionId).toMatch(/^session_[0-9a-f]{15}_[0-9a-f]{20}$/)
    expect(calls[1]!.sessionId).toBe(String(calls[0]!.sessionId))
    expect(terminalStarts).toBe(1)
    expect(stdout.text).toContain("第一轮")
    expect(stdout.text).toContain("第二轮")
    expect(stdin.raw).toBe(false)
    expect(stdin.isPaused()).toBe(true)
  })

  test("registers a fresh terminal before a non-terminal resume reacquires execution", async () => {
    const pending: TaskProjection = {
      ...completedTask(1, "继续真实工作区任务", "session_resume"),
      status: "pending",
      terminal: false,
      active: true,
      metadata: {},
    }
    const completed: TaskProjection = {
      ...pending,
      status: "completed",
      terminal: true,
      active: false,
      metadata: { final_answer: "恢复执行完成。" },
    }
    let terminalStarts = 0
    let runCalls = 0
    const api = {
      async resolveTask() { return { task: pending } },
      async ingressCapabilities() {
        return { taskId: pending.taskId, generation: 1, subscriptionCursor: "cursor_0", subscriptionSequence: 0, sseAvailable: true, raw: {} }
      },
      async *snapshotIngress(): AsyncGenerator<IngressPage> {
        yield { cursor: "cursor_0", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async runTask() {
        runCalls += 1
        return mutation(completed)
      },
      async *streamIngress() {
        await Promise.resolve()
        yield { kind: "heartbeat", taskId: pending.taskId, generation: 1, sequence: 0, cursor: "cursor_0" }
      },
      async task() { return completed },
    } as unknown as CliApi
    const stdout = new Capture()

    const outcome = await executeProductResume({
      command: {
        kind: "resume",
        identity: pending.taskId,
        baseUrl: "http://127.0.0.1:8000",
        autoStart: false,
        startupTimeoutMs: 1_000,
        timeoutMs: 10_000,
      },
      api,
      stdin: new PassThrough(),
      stdout,
      signal: new AbortController().signal,
      cwd: "G:\\agent-zoo\\zyra",
      ensureTerminal: async () => { terminalStarts += 1 },
      draftStore: null,
    })

    expect(terminalStarts).toBe(1)
    expect(runCalls).toBe(1)
    expect(outcome.status).toBe("completed")
    expect(stdout.text).toContain("恢复执行完成。")
  })

  test("selects a canonical model and binds it to the next task creation", async () => {
    const calls: Array<{ goal: string; execution?: { providerId: string; modelId: string; reasoningEffort?: string } }> = []
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
          supportedReasoningEfforts: ["low", "high", "max"],
          defaultReasoningEffort: "high",
          thinkingEnabled: true,
        }]
      },
      async createPendingTask(goal: string, _sealed: boolean, sessionId?: string, execution?: { providerId: string; modelId: string; reasoningEffort?: string }) {
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
    await waitUntil(() => stdout.text.includes("选择推理强度"))
    stdin.write("\u001b[B\u001b[B\u001b[B\r")
    await waitUntil(() => stdout.text.includes("后续新 task"))
    stdin.write("使用选择的模型执行\r")
    await waitUntil(() => calls.length === 1 && stdin.raw)
    stdin.write("/exit\r")
    await executing
    expect(calls[0]).toEqual({
      goal: "使用选择的模型执行",
      execution: { providerId: "deepseek", modelId: "deepseek-v4-flash", reasoningEffort: "max" },
    })
    expect(stdout.text).toContain("reasoning · 已选择 max")
    expect(stdout.text).toContain("可选 low/high/max")
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
