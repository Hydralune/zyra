import { afterEach, describe, expect, test } from "bun:test"
import { renderToStaticMarkup } from "react-dom/server"
import type {
  ApiHealth,
  MutationReceipt,
  RuntimeReadiness,
  TaskListProjection,
  TaskProjection,
} from "../../../packages/core/typed-api-client/src/index.ts"
import { CommandCatalog } from "../src/command/catalog.ts"
import { CommandCoordinator } from "../src/command/coordinator.ts"
import { CommandDraftStore } from "../src/command/draft-store.ts"
import { CommandExecutionPolicy } from "../src/command/execution-policy.ts"
import { CommandHistory } from "../src/command/history.ts"
import { decideCommandKey, selectedSuggestionIndex } from "../src/command/keyboard.ts"
import {
  applyArgumentSuggestion,
  argumentSuggestions,
} from "../src/command/argument-completion.ts"
import {
  applyCompletion,
  completionContext,
  parseInput,
  validateCommandArguments,
} from "../src/command/parser.ts"
import { CommandQueue } from "../src/command/queue.ts"
import { EmptyState, ErrorState, LoadingState } from "../src/components/status/request-state.tsx"
import {
  CollectionWindowController,
  collectionWindow,
  nextCollectionIndex,
} from "../src/shell/collection-window.ts"
import { FocusManager } from "../src/shell/focus-manager.ts"
import { LayoutRuntime, type LayoutWindow } from "../src/shell/layout-runtime.ts"
import { NotificationCenter } from "../src/shell/notification-center.ts"
import { OverlayRuntime } from "../src/shell/overlay-runtime.ts"
import { CommandSurfaceRuntime } from "../src/features/commands/runtime.ts"
import { CanonicalProjectionStore } from "../src/state/store.ts"
import { RetrySupervisor } from "../src/shell/retry-supervisor.ts"
import {
  WorkbenchRouter,
  parseWorkbenchRoute,
  taskRoute,
  type WindowLike,
} from "../src/shell/router.ts"
import { WorkbenchRouteLoader } from "../src/shell/route-loader.ts"
import { buildTaskTree, nextTreeNodeId, visibleTaskTree } from "../src/shell/task-tree.ts"
import { queryTasks, taskQuerySummary } from "../src/shell/task-query.ts"
import { taskActionSet } from "../src/shell/task-action-policy.ts"
import { formatDuration, taskHealthLabel, taskMetrics } from "../src/shell/task-metrics.ts"
import {
  WorkbenchController,
  type Clock,
} from "../src/shell/workbench-controller.ts"
import type { ProductExecutionConfig, TaskApi } from "../src/api/task-api.ts"
import type { TaskLifecycleCoordinator } from "../src/api/lifecycle.ts"

function task(
  taskId = "task_demo_001",
  status = "running",
  options: {
    goal?: string
    runId?: string
    sessionId?: string
    updatedAt?: string
    nodes?: TaskProjection["planNodes"]
  } = {},
): TaskProjection {
  const active = ["running", "active", "dispatched", "pending"].includes(status)
  const terminal = ["completed", "failed", "cancelled", "succeeded"].includes(status)
  const runId = options.runId ?? `run_${taskId.slice(5)}`
  const nodes = options.nodes ?? [{
    nodeId: "node_root_001",
    title: "Root",
    description: "Root plan node",
    status: terminal ? status : "running",
    dependsOn: [],
    artifactIds: [],
    metadata: {},
  }]
  return {
    taskId,
    runId,
    sessionId: options.sessionId,
    rootNodeId: "node_root_001",
    userGoal: options.goal ?? `Goal for ${taskId}`,
    status,
    createdAt: "2026-07-23T01:00:00.000Z",
    updatedAt: options.updatedAt ?? "2026-07-23T02:00:00.000Z",
    planNodes: nodes,
    artifacts: [],
    metadata: {},
    binding: { taskId, runId, sessionId: options.sessionId },
    terminal,
    active,
  }
}

function health(): ApiHealth {
  return {
    status: "ok",
    phase: "runtime",
    service: "zyra-api",
    apiVersion: "1.0",
    capabilities: ["tasks"],
    raw: {},
  }
}

function readiness(ready = true): RuntimeReadiness {
  return {
    ready,
    status: ready ? "ready" : "blocked",
    apiVersion: "1.0",
    owners: { task_store: ready },
    blockers: ready ? [] : ["task store unavailable"],
    raw: {},
  }
}

function receipt(value: TaskProjection, action = "task.create"): MutationReceipt {
  return {
    receiptId: `receipt_${value.taskId.slice(5)}`,
    requestId: `request_${value.taskId.slice(5)}`,
    idempotencyKey: `idempotency_${value.taskId.slice(5)}`,
    operation: action,
    status: "committed",
    statusCode: 200,
    replayed: false,
    committedAt: "2026-07-23T02:00:00.000Z",
    binding: { ...value.binding },
  }
}

class MemoryStorage {
  readonly values = new Map<string, string>()
  getItem(key: string) { return this.values.get(key) ?? null }
  setItem(key: string, value: string) { this.values.set(key, value) }
  removeItem(key: string) { this.values.delete(key) }
}

class FakeWindow implements WindowLike {
  readonly listeners = new Set<() => void>()
  readonly location = { pathname: "/tasks", search: "", hash: "" }
  readonly history = {
    state: undefined as unknown,
    pushState: (_data: unknown, _unused: string, url?: string | URL | null) => this.#set(url),
    replaceState: (_data: unknown, _unused: string, url?: string | URL | null) => this.#set(url),
    back: () => {},
  }
  addEventListener(_type: "popstate", listener: () => void) { this.listeners.add(listener) }
  removeEventListener(_type: "popstate", listener: () => void) { this.listeners.delete(listener) }
  pop(path: string) {
    this.#set(path)
    for (const listener of this.listeners) listener()
  }
  #set(url?: string | URL | null) {
    if (!url) return
    const parsed = new URL(String(url), "http://zyra.local")
    this.location.pathname = parsed.pathname
    this.location.search = parsed.search
    this.location.hash = parsed.hash
  }
}

class ManualClock implements Clock {
  value = 1_000
  callbacks: (() => void)[] = []
  now() { return this.value }
  setTimeout(callback: () => void) {
    this.callbacks.push(callback)
    return callback
  }
  clearTimeout(handle: unknown) {
    this.callbacks = this.callbacks.filter((callback) => callback !== handle)
  }
  flush() {
    const callbacks = this.callbacks.splice(0)
    for (const callback of callbacks) callback()
  }
}

function fakeTaskApi(options: {
  tasks?: TaskProjection[]
  healthError?: Error
  getError?: Error
} = {}): TaskApi {
  const values = options.tasks ?? [task()]
  return {
    health: async () => {
      if (options.healthError) throw options.healthError
      return health()
    },
    readiness: async () => readiness(),
    list: async (): Promise<TaskListProjection> => ({
      tasks: values,
      total: values.length,
    }),
    get: async (taskId: string) => {
      if (options.getError) throw options.getError
      const value = values.find((candidate) => candidate.taskId === taskId)
      if (!value) throw new Error("404 task not found")
      return value
    },
  } as unknown as TaskApi
}

describe("workbench router", () => {
  test("normalizes list, detail, query, and invalid routes", () => {
    expect(parseWorkbenchRoute({ pathname: "/", search: "" }).kind).toBe("tasks")
    const detail = parseWorkbenchRoute({
      pathname: "/tasks/task_demo_001/",
      search: "?focus=task-detail&status=RUNNING&ignored=yes",
    })
    expect(detail).toMatchObject({
      kind: "task",
      taskId: "task_demo_001",
      query: { focus: "task-detail", status: "running" },
    })
    expect(parseWorkbenchRoute({
      pathname: "/tasks/task_demo_001/evidence",
      search: "",
    })).toMatchObject({
      kind: "evidence",
      taskId: "task_demo_001",
      path: "/tasks/task_demo_001/evidence",
    })
    expect(parseWorkbenchRoute({ pathname: "/tasks/not-valid", search: "" }).kind).toBe("not-found")
  })

  test("pushes, replaces, pops, recovers, and emits transitions", () => {
    const browser = new FakeWindow()
    const router = new WorkbenchRouter(browser)
    router.start()
    const kinds: string[] = []
    router.listen((_route, transition) => kinds.push(transition.kind))
    router.openTask("task_demo_001")
    expect(browser.location.pathname).toBe("/tasks/task_demo_001")
    router.openEvidence("task_demo_001")
    expect(browser.location.pathname).toBe("/tasks/task_demo_001/evidence")
    router.updateQuery({ overlay: "command-help" })
    expect(browser.location.search).toContain("overlay=command-help")
    browser.pop("/settings")
    expect(router.current.kind).toBe("settings")
    browser.pop("/broken")
    expect(router.recover().kind).toBe("tasks")
    expect(kinds).toEqual(["push", "push", "replace", "pop", "pop", "recover"])
    router.close()
    expect(browser.listeners.size).toBe(0)
  })

  test("rejects invalid task identities", () => {
    expect(() => taskRoute("../../escape")).toThrow("Invalid task route identity")
  })
})

describe("command catalog, parsing, and completion", () => {
  const catalog = new CommandCatalog()

  test("routes MCP, skill, agent, and child-task discovery to task-scoped runtime panels", () => {
    const expected = new Map([
      ["mcp", "command.runtime.mcp"],
      ["skills", "command.runtime.skills"],
      ["agents", "command.runtime.agents"],
      ["tasks", "command.runtime.tasks"],
    ])
    for (const [trigger, id] of expected) {
      const definition = catalog.resolve(trigger)
      expect(definition?.id).toBe(id)
      expect(definition?.execution).toBe("local-overlay")
      expect(definition?.availability).toBe("requires-task")
      expect(catalog.availability(definition!, { taskId: "task_done_001", taskActive: false, taskTerminal: true, transportEnabled: true }).enabled).toBe(true)
    }
    expect(catalog.resolve("home")?.id).toBe("command.navigation.home")
  })

  test("parses prompts, quoted arguments, flags, and unknown commands", () => {
    expect(parseInput("  build a runtime ", catalog)).toMatchObject({
      kind: "prompt",
      text: "build a runtime",
    })
    const parsed = parseInput('/new "build a runtime" --run', catalog)
    expect(parsed).toMatchObject({
      kind: "command",
      trigger: "new",
      arguments: ["build a runtime"],
      flags: { run: true },
    })
    expect(parseInput("/missing", catalog)).toMatchObject({
      kind: "command",
      definition: undefined,
    })
  })

  test("reports missing arguments and command usage", () => {
    const parsed = parseInput("/open", catalog)
    expect(validateCommandArguments(parsed)).toMatchObject({
      valid: false,
      errors: ["Missing required argument: task_id"],
    })
  })

  test("computes command and argument replacement ranges", () => {
    const command = completionContext("/op", 3, catalog)
    expect(command.kind).toBe("command")
    expect(applyCompletion("/op", command, "open")).toEqual({
      value: "/open ",
      cursor: 6,
    })
    const value = "/open task_de"
    const argument = completionContext(value, value.length, catalog)
    const parsed = parseInput(value, catalog)
    const suggestions = argumentSuggestions({
      parsed,
      completion: argument,
      catalog,
      tasks: [task("task_demo_001")],
    })
    expect(suggestions[0]?.value).toBe("task_demo_001")
    expect(applyArgumentSuggestion(value, argument, suggestions[0]!)).toEqual({
      value: "/open task_demo_001 ",
      cursor: 20,
    })
  })

  test("fails closed when availability or transport denies execution", () => {
    const policy = new CommandExecutionPolicy()
    const parsed = parseInput("/cancel reason", catalog)
    expect(policy.decide({
      parsed,
      context: { transportEnabled: true },
      availability: { enabled: false, reason: "Select an active task first." },
      origin: "keyboard",
      busy: false,
      allowQueue: true,
    })).toMatchObject({
      allowed: false,
      code: "active-task-required",
    })
    policy.disable("test disable")
    expect(() => policy.assert({
      parsed: parseInput("create something", catalog),
      context: { transportEnabled: true },
      origin: "keyboard",
      busy: false,
      allowQueue: true,
    })).toThrow("test disable")
  })
})

describe("command keyboard, history, drafts, and queue", () => {
  test("maps keyboard states without stealing multiline cursor movement", () => {
    const base = {
      shift: false,
      alt: false,
      ctrl: false,
      meta: false,
      composing: false,
      suggestionsOpen: false,
      suggestionCount: 0,
      value: "first\nsecond",
      cursor: 3,
      busy: false,
      overlayOpen: false,
      editableQueuedCount: 0,
      inHistory: false,
    }
    expect(decideCommandKey({ ...base, key: "Enter" }).action).toBe("submit")
    expect(decideCommandKey({ ...base, key: "Enter", shift: true }).action).toBe("newline")
    expect(decideCommandKey({ ...base, key: "ArrowUp" }).action).toBe("history-previous")
    expect(decideCommandKey({ ...base, key: "ArrowDown" }).action).toBe("none")
    expect(selectedSuggestionIndex(0, 3, "previous")).toBe(2)
  })

  test("persists bounded history and restores the draft after navigation", () => {
    const storage = new MemoryStorage()
    const history = new CommandHistory({ storage })
    history.add("first")
    history.add("/help")
    expect(history.navigate("up", "", 0).value).toBe("/help")
    expect(history.navigate("up", "/help", 0).value).toBe("first")
    expect(history.navigate("down", "first", 0).value).toBe("/help")
    expect(history.navigate("down", "/help", 5).value).toBe("")
    expect(new CommandHistory({ storage }).list().map((entry) => entry.value)).toEqual(["/help", "first"])
  })

  test("submission capture restores only an unchanged cleared draft", () => {
    const storage = new MemoryStorage()
    const drafts = new CommandDraftStore({ storage })
    const capture = drafts.captureValue("new", "create this", 6)
    drafts.clear("new")
    expect(drafts.restore(capture.id)).toMatchObject({
      value: "create this",
      cursor: 6,
    })
    const second = drafts.captureValue("new", "old value", 9)
    drafts.set("new", "new user input", 14)
    expect(drafts.restore(second.id)).toBeUndefined()
    expect(drafts.get("new")?.value).toBe("new user input")
  })

  test("orders queue by priority then FIFO and protects dispatching entries", () => {
    const queue = new CommandQueue()
    const later = queue.enqueue({ value: "later", priority: "later" })
    const first = queue.enqueue({ value: "first", priority: "next" })
    const now = queue.enqueue({ value: "now", priority: "now" })
    expect(queue.reserve()?.id).toBe(now.id)
    expect(queue.reserve()?.id).toBe(first.id)
    expect(queue.remove(first.id)).toBeUndefined()
    queue.commit(first.id, "receipt_first")
    expect(queue.remove(first.id)?.receiptId).toBe("receipt_first")
    queue.commit(now.id, "receipt_now")
    queue.remove(now.id)
    expect(queue.peek()?.id).toBe(later.id)
  })

  test("pops only editable queued input into the editor", () => {
    const queue = new CommandQueue()
    queue.enqueue({ value: "one", editable: true })
    queue.enqueue({ value: "system", editable: false })
    queue.enqueue({ value: "two", editable: true })
    const popped = queue.popEditable("draft", 5)
    expect(popped?.value).toBe("one\ntwo\ndraft")
    expect(queue.getSnapshot().entries.map((entry) => entry.value)).toEqual(["system"])
  })

  test("editing one queued message preserves other messages and the current draft", () => {
    const queue = new CommandQueue()
    const first = queue.enqueue({ value: "one", editable: true })
    queue.enqueue({ value: "two", editable: true })
    expect(queue.popEditable("draft", 5, first.id)?.value).toBe("one\ndraft")
    expect(queue.getSnapshot().entries.map((entry) => entry.value)).toEqual(["two"])
  })
})

describe("workbench controller and route loader", () => {
  test("list summaries cannot erase loaded answers and prior session turns are hydrated", async () => {
    const first = task("task_first_001", "completed", { sessionId: "session_chat_001" })
    const second = task("task_second_001", "completed", { sessionId: "session_chat_001" })
    first.metadata = { final_answer: "previous answer" }
    second.metadata = { final_answer: "latest answer" }
    const api = fakeTaskApi({ tasks: [first, second] })
    api.list = async () => ({ tasks: [first, second].map((value) => ({
      ...value, metadata: {}, planNodes: [], artifacts: [],
    })), total: 2 })
    const controller = new WorkbenchController(api)
    await controller.loadTask(second.taskId)
    await controller.refreshTasks()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(controller.selectedTask()?.metadata.final_answer).toBe("latest answer")
    expect(controller.getSnapshot().list.tasks.find((row) => row.taskId === first.taskId)?.metadata.final_answer).toBe("previous answer")
    await controller.refreshTasks()
    expect(controller.selectedTask()?.planNodes.length).toBe(1)
    controller.close()
  })
  test("distinguishes missing history from an empty answer and retries failed hydration", async () => {
    const first = task("task_history_001", "completed", { sessionId: "session_history_001" })
    const latest = task("task_latest_001", "completed", { sessionId: first.sessionId })
    const api = fakeTaskApi({ tasks: [first, latest] })
    const originalGet = api.get.bind(api)
    let fail = true
    let requests = 0
    api.get = async (id, options) => {
      if (id === first.taskId) {
        requests++
        if (fail) throw new Error("history temporarily unavailable")
        return { ...first, metadata: { final_answer: "restored reply" } }
      }
      return originalGet(id, options)
    }
    const controller = new WorkbenchController(api)
    await controller.refreshTasks()
    expect(controller.conversationDetail(first.taskId).phase).toBe("loading")
    await controller.loadTask(latest.taskId)
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(controller.conversationDetail(first.taskId).phase).toBe("error")
    await controller.refreshTasks()
    expect(requests).toBe(1)
    fail = false
    await controller.retryConversationDetail(first.taskId)
    expect(controller.conversationDetail(first.taskId).phase).toBe("ready")
    expect(controller.getSnapshot().list.tasks.find((row) => row.taskId === first.taskId)?.metadata.final_answer).toBe("restored reply")
    controller.close()
  })

  test("loads real projections, selects detail, and applies receipt-backed mutation", async () => {
    const api = fakeTaskApi({ tasks: [task("task_demo_001"), task("task_demo_002", "completed")] })
    const controller = new WorkbenchController(api)
    await controller.bootstrap({ taskId: "task_demo_001" })
    expect(controller.getSnapshot().runtime.phase).toBe("ready")
    expect(controller.getSnapshot().list.tasks).toHaveLength(2)
    expect(controller.getSnapshot().detail.task?.taskId).toBe("task_demo_001")
    controller.applyMutation(task("task_demo_003", "running"))
    expect(controller.selectedTask()?.taskId).toBe("task_demo_003")
    expect(controller.getSnapshot().list.tasks.some((value) => value.taskId === "task_demo_003")).toBe(true)
  })

  test("surfaces not-found and retryable runtime failures without fabricating data", async () => {
    const clock = new ManualClock()
    const missing = new WorkbenchController(fakeTaskApi({ getError: new Error("404 not found") }), { clock })
    expect((await missing.loadTask("task_missing_001")).phase).toBe("not-found")
    expect(missing.getSnapshot().detail.task).toBeUndefined()

    const offline = new WorkbenchController(
      fakeTaskApi({ healthError: new TypeError("network disconnected") }),
      { clock },
    )
    await offline.refreshRuntime()
    expect(offline.getSnapshot().runtime.phase).toBe("reconnecting")
    expect(clock.callbacks).toHaveLength(1)
    offline.close()
  })

  test("disable path fails visibly and stops requests", async () => {
    const controller = new WorkbenchController(fakeTaskApi())
    controller.disable("mutation test")
    expect(controller.getSnapshot()).toMatchObject({
      transportEnabled: false,
      list: { phase: "error", failure: { message: "mutation test" } },
    })
    await expect(controller.refreshTasks()).rejects.toThrow("disabled")
  })

  test("route loader cancels superseded detail and settles current list route", async () => {
    const controller = new WorkbenchController(fakeTaskApi())
    const loader = new WorkbenchRouteLoader(controller)
    await loader.load(taskRoute("task_demo_001"))
    const record = await loader.load({ kind: "tasks", path: "/tasks", query: {} })
    expect(record.phase).toBe("settled")
    expect(loader.getSnapshot().active?.routeKind).toBe("tasks")
    loader.close()
  })
})

describe("command coordinator integration", () => {
  const cleanups: (() => void)[] = []
  afterEach(() => {
    for (const cleanup of cleanups.splice(0)) cleanup()
  })

  function harness(options: { deferred?: boolean; runDeferred?: boolean; fail?: boolean; executionConfig?: () => ProductExecutionConfig | undefined } = {}) {
    const browser = new FakeWindow()
    const router = new WorkbenchRouter(browser)
    router.start()
    const workbench = new WorkbenchController(fakeTaskApi())
    const queue = new CommandQueue()
    const history = new CommandHistory()
    const overlays = new OverlayRuntime()
    const catalog = new CommandCatalog()
    let calls = 0
    const createInputs: Array<{ goal: string; sessionId?: string; executionConfig?: ProductExecutionConfig }> = []
    const createKeys: string[] = []
    let release: (() => void) | undefined
    const lifecycle = {
      create: async (input: { goal: string; sessionId?: string; executionConfig?: ProductExecutionConfig; signal?: AbortSignal; idempotencyKey?: string }) => {
        calls += 1
        createInputs.push({ goal: input.goal, sessionId: input.sessionId, ...(input.executionConfig ? { executionConfig: input.executionConfig } : {}) })
        createKeys.push(input.idempotencyKey ?? "")
        if (options.deferred) {
          await new Promise<void>((resolve, reject) => {
            release = resolve
            input.signal?.addEventListener("abort", () => reject(new Error("aborted")), { once: true })
          })
        }
        if (options.fail) throw new Error("backend rejected create")
        const value = task(`task_created_${calls.toString().padStart(3, "0")}`)
        return {
          mutation: { task: value, events: [], receipt: {}, controls: {}, raw: {} },
          receipt: receipt(value),
        }
      },
      cancel: async (input: { taskId: string }) => {
        const value = task(input.taskId, "cancelled")
        return {
          mutation: { task: value, events: [], receipt: {}, controls: {}, raw: {} },
          receipt: receipt(value, "task.cancel"),
        }
      },
      resume: async (input: { taskId: string }) => {
        if (options.runDeferred) await new Promise<void>((resolve) => { release = resolve })
        const value = task(input.taskId, "running")
        return {
          mutation: { task: value, events: [], receipt: {}, controls: {}, raw: {} },
          receipt: receipt(value, "task.resume"),
        }
      },
    } as unknown as TaskLifecycleCoordinator
    const commands = new CommandCoordinator({
      executionConfig: options.executionConfig,
      catalog,
      queue,
      history,
      lifecycle,
      workbench,
      router,
      overlays,
    })
    cleanups.push(() => {
      commands.close()
      queue.close()
      workbench.close()
      overlays.closeRuntime()
      router.close()
    })
    return {
      commands,
      queue,
      workbench,
      overlays,
      browser,
      calls: () => calls,
      createInputs,
      createKeys,
      release: () => release?.(),
    }
  }

  test("coalesces double submit and commits only backend-returned task state", async () => {
    const value = harness({ deferred: true })
    const first = value.commands.submit("Build the console")
    const second = value.commands.submit("Build the console")
    expect(first).toBe(second)
    expect(value.calls()).toBe(1)
    value.release()
    const result = await first
    expect(result.mutation?.receipt.receiptId).toBe("receipt_created_001")
    expect(value.workbench.selectedTask()?.taskId).toBe("task_created_001")
    expect(value.browser.location.pathname).toBe("/tasks/task_created_001")
  })

  test("queues a second prompt while busy and exposes editable preview", async () => {
    const value = harness({ deferred: true })
    const first = value.commands.submit("First task")
    const queued = await value.commands.submit("Second task")
    expect(queued.status).toBe("queued")
    expect(value.commands.getSnapshot().busy).toBe(true)
    expect(value.queue.getSnapshot()).toMatchObject({
      pendingCount: 1,
      visible: [{ value: "Second task", phase: "queued" }],
    })
    value.release()
    await first
  })

  test("captures model settings for queued tasks, including automatic selection", async () => {
    let config: ProductExecutionConfig | undefined = { providerId: "deepseek", modelId: "flash", reasoningEffort: "low" }
    const value = harness({ deferred: true, executionConfig: () => config })
    const first = value.commands.submit("First task")
    await value.commands.submit("Queued custom model")
    config = undefined
    await value.commands.submit("Queued automatic model")
    config = { providerId: "other", modelId: "new-default" }
    expect(value.queue.getSnapshot().visible.map((row) => row.executionConfig)).toEqual([
      { providerId: "deepseek", modelId: "flash", reasoningEffort: "low" }, undefined,
    ])
    value.release()
    await first
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(value.createInputs[1]?.executionConfig).toEqual({ providerId: "deepseek", modelId: "flash", reasoningEffort: "low" })
    value.release()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(value.createInputs[2]?.executionConfig).toBeUndefined()
    value.release()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(value.createInputs).toHaveLength(3)
  })

  test("shows a created task while execution is pending and accepts stop immediately", async () => {
    const value = harness({ runDeferred: true })
    const pending = value.commands.submit("Run an observable task")
    await Promise.resolve()
    expect(value.browser.location.pathname).toBe("/tasks/task_created_001")
    expect(value.commands.getSnapshot().phase).toBe("running")
    const stopped = await value.commands.submit("/cancel Stop this task")
    expect(stopped.mutation?.mutation.task.status).toBe("cancelled")
    expect(value.commands.getSnapshot().busy).toBe(true)
      value.release()
      const completed = await pending
      expect(completed.mutation?.mutation.task.status).toBe("cancelled")
      expect(value.workbench.selectedTask()?.status).toBe("cancelled")
      expect(value.commands.getSnapshot().busy).toBe(false)
  })

  test("separate identical new-task submissions receive different mutation keys", async () => {
    const value = harness()
    await value.commands.submit("/new Identical goal --no-run")
    await value.commands.submit("/new Identical goal --no-run")
    expect(value.createKeys.every(Boolean)).toBe(true)
    expect(new Set(value.createKeys).size).toBe(2)
  })

  test("global status does not require a selected task when shared controls are attached", async () => {
    const value = harness()
    value.commands.attachControls({ resolve: () => true,
      submit: () => { throw new Error("Task-scoped transport must not run") },
    } as unknown as Parameters<CommandCoordinator["attachControls"]>[0])
    await value.commands.submit("/status")
    expect(value.overlays.active()?.kind).toBe("transport-status")
  })

  test("keeps plain follow-ups in the selected canonical session", async () => {
    const value = harness()
    value.workbench.applyMutation(task("task_demo_001", "completed", {
      sessionId: "session_demo_001",
    }))
    await value.commands.submit("Continue with one more check", {
      taskId: "task_demo_001",
      taskTerminal: true,
    })
    expect(value.createInputs[0]).toEqual({
      goal: "Continue with one more check",
      sessionId: "session_demo_001",
    })

    await value.commands.submit("/new Start separately")
    expect(value.createInputs[1]).toEqual({
      goal: "Start separately",
      sessionId: undefined,
    })
  })

  test("new-task route cannot inherit a task left selected by the previous page", async () => {
    const value = harness()
    value.workbench.applyMutation(task("task_previous_001", "completed", { sessionId: "session_previous_001" }))
    value.browser.pop("/tasks/new")
    await value.commands.submit("An independent task")
    expect(value.createInputs[0]?.sessionId).toBeUndefined()
  })

  test("restores failure as an error and local overlay does not call lifecycle", async () => {
    const failed = harness({ fail: true })
    await expect(failed.commands.submit("Will fail")).rejects.toThrow("backend rejected create")
    expect(failed.workbench.getSnapshot().list.tasks).toHaveLength(0)

    const local = harness()
    await local.commands.submit("/help")
    expect(local.overlays.active()?.kind).toBe("command-help")
    expect(local.calls()).toBe(0)
  })

  test("disable path blocks default submit before a fake task can be projected", async () => {
    const value = harness()
    value.commands.disable("coordinator mutation disabled")
    await expect(value.commands.submit("must not run")).rejects.toThrow("coordinator mutation disabled")
    expect(value.calls()).toBe(0)
    expect(value.workbench.getSnapshot().list.tasks).toHaveLength(0)
  })

  test("cancel and resume call lifecycle with selected backend identity", async () => {
    const value = harness()
    value.workbench.applyMutation(task("task_demo_001", "running"))
    await value.commands.submit("/cancel operator request", {
      taskId: "task_demo_001",
      runId: "run_demo_001",
      taskActive: true,
    })
    expect(value.workbench.selectedTask()?.status).toBe("cancelled")
    await value.commands.submit("/resume", {
      taskId: "task_demo_001",
      runId: "run_demo_001",
      taskTerminal: true,
    })
    expect(value.workbench.selectedTask()?.status).toBe("running")
  })

  test("direct hybrid slash mutations inherit canonical sealed mode and record the attempt", async () => {
    let transportCalls = 0
    const api = {
      ...fakeTaskApi(),
      controlCommand: async () => {
        transportCalls += 1
        throw new Error("sealed mutation must not reach transport")
      },
      commandQueue: async () => ({
        schema: "zyra.command-queue/v1",
        task_id: "task_demo_001",
        sequence: 0,
        revision: 0,
        entries: [],
      }),
      cancelControlCommand: async () => ({}),
    } as unknown as TaskApi
    const workbench = new WorkbenchController(api)
    workbench.applyMutation({
      ...task(),
      metadata: {
        sealed: true,
        competition_mode: "sealed_autonomous",
      },
    })
    const overlays = new OverlayRuntime()
    const projections = new CanonicalProjectionStore({
      id: "sealed-command-test",
      autoPersist: false,
      restore: false,
    })
    const attempts: Array<{ value: string; reason: string }> = []
    const controls = new CommandSurfaceRuntime({
      api,
      workbench,
      overlays,
      projections,
      recordSealedMutation(input) {
        attempts.push(input)
      },
    })
    await expect(
      controls.submit("/mcp disable alpha"),
    ).rejects.toThrow("Sealed autonomous")
    expect(transportCalls).toBe(0)
    expect(attempts).toHaveLength(1)
    expect(attempts[0]?.value).toBe("/mcp disable alpha")
    controls.close()
    await projections.close()
    overlays.closeRuntime()
    workbench.close()
  })
})

describe("large state, layout, retry, overlay, and accessibility primitives", () => {
  test("windows large collections and preserves keyboard bounds", () => {
    expect(collectionWindow({
      itemCount: 2_000,
      itemHeight: 40,
      viewportHeight: 400,
      scrollTop: 20_000,
      overscan: 4,
    })).toMatchObject({
      start: 500,
      end: 510,
      overscanStart: 496,
      overscanEnd: 514,
    })
    const controller = new CollectionWindowController({ itemHeight: 40 })
    controller.update({ itemCount: 2_000, viewportHeight: 400 })
    expect(controller.scrollToIndex(1_999, "end")).toBe(79_600)
    expect(nextCollectionIndex(0, 2_000, "page-next", 10)).toBe(10)
  })

  test("builds deterministic plan hierarchy and reports missing dependency state", () => {
    const value = task("task_tree_001", "running", {
      nodes: [
        {
          nodeId: "node_root_001",
          title: "Root",
          description: "",
          status: "running",
          dependsOn: [],
          artifactIds: [],
          metadata: {},
        },
        {
          nodeId: "node_child_001",
          title: "Child",
          description: "",
          status: "pending",
          parentNodeId: "node_root_001",
          dependsOn: ["node_missing_001"],
          artifactIds: [],
          metadata: {},
        },
      ],
    })
    const tree = buildTaskTree(value)
    expect(tree.flat.map((entry) => entry.node.nodeId)).toEqual(["node_root_001", "node_child_001"])
    expect(tree.missingDependencies.node_child_001).toEqual(["node_missing_001"])
    expect(visibleTaskTree(tree, new Set(["node_root_001"]))).toHaveLength(1)
    expect(nextTreeNodeId(tree.flat, "node_root_001", "first-child")).toBe("node_child_001")
  })

  test("queries and sorts large task projections without changing source objects", () => {
    const values = [
      task("task_query_001", "completed", { goal: "Alpha", updatedAt: "2026-07-23T03:00:00Z" }),
      task("task_query_002", "running", { goal: "Beta", updatedAt: "2026-07-23T04:00:00Z" }),
    ]
    const result = queryTasks(values, { text: "beta", sort: "goal" })
    expect(result.map((value) => value.taskId)).toEqual(["task_query_002"])
    expect(taskQuerySummary(values, result, { text: "beta" })).toContain("1 of 2")
    expect(values[0]?.taskId).toBe("task_query_001")
  })

  test("derives task action safety and runtime metrics from canonical projection", () => {
    const value = task("task_metrics_001", "running")
    const actions = taskActionSet(value, { transportEnabled: true })
    expect(actions.cancel.allowed).toBe(true)
    expect(actions.resume.allowed).toBe(false)
    const metrics = taskMetrics(value, Date.parse("2026-07-23T02:01:05.000Z"))
    expect(metrics).toMatchObject({
      nodeCount: 1,
      activeNodeCount: 1,
      progress: 0,
    })
    expect(formatDuration(metrics.elapsedMs)).toBe("1h 1m")
    expect(taskHealthLabel(metrics)).toBe("1 active node")
  })

  test("layout changes modes, panes, and bounded widths", () => {
    const listeners = new Set<() => void>()
    const fake: LayoutWindow = {
      innerWidth: 1400,
      innerHeight: 900,
      addEventListener: (_type, listener) => listeners.add(listener),
      removeEventListener: (_type, listener) => listeners.delete(listener),
      requestAnimationFrame: (callback) => {
        callback(0)
        return 1
      },
      cancelAnimationFrame: () => {},
    }
    const layout = new LayoutRuntime(fake)
    expect(layout.start().mode).toBe("wide")
    layout.setTaskListWidth(600)
    expect(layout.getSnapshot().taskListWidth).toBeLessThanOrEqual(850)
    fake.innerWidth = 600
    for (const listener of listeners) listener()
    layout.showDetail()
    expect(layout.getSnapshot()).toMatchObject({
      mode: "mobile",
      activePane: "detail",
      detailVisible: true,
    })
    layout.close()
  })

  test("retry supervisor exhausts, resets, and honors deterministic delay", () => {
    const retry = new RetrySupervisor({ maximumAttempts: 2, baseDelayMs: 100 })
    const first = retry.failure("runtime", { retryable: true, now: 1000 })
    const second = retry.failure("runtime", { retryable: true, now: 2000 })
    const third = retry.failure("runtime", { retryable: true, now: 3000 })
    expect(first.retry).toBe(true)
    expect(second.delayMs).toBeGreaterThan(first.delayMs)
    expect(third).toMatchObject({ retry: false, exhausted: true })
    retry.success("runtime", 4000)
    expect(retry.state("runtime").attempt).toBe(0)
  })

  test("overlay and notifications publish stable external-store snapshots", () => {
    const overlays = new OverlayRuntime()
    const before = overlays.getSnapshot()
    const opened = overlays.open({ kind: "command-help", title: "Help" })
    expect(overlays.getSnapshot()).not.toBe(before)
    expect(overlays.active()?.id).toBe(opened.id)
    overlays.closeActive()
    expect(overlays.getSnapshot().stack).toHaveLength(0)

    const notifications = new NotificationCenter()
    const notice = notifications.push({
      title: "Committed",
      message: "Backend receipt confirmed.",
      tone: "success",
      durationMs: 0,
    })
    expect(notifications.getSnapshot().unreadCount).toBe(1)
    notifications.acknowledge(notice.id)
    expect(notifications.getSnapshot().unreadCount).toBe(0)
    notifications.close()
  })

  test("focus manager restores initiating control with fallback", () => {
    const calls: string[] = []
    const button = {
      id: "source",
      isConnected: true,
      focus: () => calls.push("source"),
      getAttribute: () => null,
    }
    const fallback = {
      id: "fallback",
      isConnected: true,
      focus: () => calls.push("fallback"),
      getAttribute: () => null,
    }
    const focus = new FocusManager({
      activeElement: null,
      getElementById: (id) => id === "source" ? button as unknown as HTMLElement : null,
      querySelector<E extends Element = Element>(): E | null {
        return fallback as unknown as E
      },
    })
    focus.remember("dialog", { targetId: "source", fallbackSelector: "#fallback" })
    expect(focus.restore("dialog")).toBe(true)
    expect(calls).toEqual(["source"])
  })

  test("loading, empty, and error states expose accessible semantics", () => {
    expect(renderToStaticMarkup(<LoadingState title="Loading" />)).toContain('role="status"')
    expect(renderToStaticMarkup(<EmptyState title="Empty" detail="Nothing here" />)).toContain("Nothing here")
    expect(renderToStaticMarkup(
      <ErrorState
        title="Failed"
        failure={{
          name: "Error",
          message: "broken",
          retryable: true,
          occurredAt: 1,
          attempt: 1,
        }}
        onRetry={() => {}}
      />,
    )).toContain('role="alert"')
  })
})

describe("default production path", () => {
  test("entry mounts the React workbench and workbench code has no direct fetch fallback", async () => {
    const main = await Bun.file(new URL("../src/main.tsx", import.meta.url)).text()
    const runtime = await Bun.file(new URL("../src/app/runtime.ts", import.meta.url)).text()
    expect(main).toContain("createWorkbenchRuntime")
    expect(main).toContain("<WorkbenchApp")
    expect(runtime).toContain("createZyraApi")

    const files = await Promise.all([
      Bun.file(new URL("../src/app/workbench-app.tsx", import.meta.url)).text(),
      Bun.file(new URL("../src/command/coordinator.ts", import.meta.url)).text(),
      Bun.file(new URL("../src/shell/workbench-controller.ts", import.meta.url)).text(),
    ])
    expect(files.join("\n")).not.toMatch(/\bfetch\s*\(/)
  })
})
