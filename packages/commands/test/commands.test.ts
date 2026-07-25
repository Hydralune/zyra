import { describe, expect, test } from "bun:test"
import {
  auditCommandRuntime,
  buildCommandResultModel,
  CommandCoordinator,
  CommandExecutionPolicy,
  CommandHistoryStore,
  CommandInputEngine,
  CommandPalette,
  CommandProjectionIndex,
  CommandQueueActions,
  CommandQueueProjection,
  CommandQueueRecovery,
  CommandResultStore,
  CommandReceiptLedger,
  SideQuestionSession,
  admitCommandReceipt,
  admitQueueSnapshot,
  applyCommandCompletion,
  argumentSuggestions,
  buildCommandRequest,
  commandReceiptRetryable,
  completionFor,
  correlateCommand,
  createCommandRegistry,
  mergeQueueItems,
  parseCommand,
  queueItemFromProjection,
  retryCommandRequest,
  sideQuestionAudit,
  tokenizeCommand,
  type CommandTaskContext,
  type CommandTransport,
  type CommandTransportRequest,
} from "../src/index.ts"

const context: CommandTaskContext = {
  taskId: "task_01",
  runId: "run_01",
  sessionId: "session_01",
  taskStatus: "running",
  active: true,
  terminal: false,
  transportEnabled: true,
  sealed: false,
  remote: false,
  expectedRevision: 4,
}

function request(text = "/status --scope all"): CommandTransportRequest {
  const registry = createCommandRegistry()
  return buildCommandRequest({
    parsed: parseCommand(text, registry),
    context,
    mode: "enqueue",
  })
}

function rawReceipt(
  input: CommandTransportRequest,
  options: {
    status?: string
    queueId?: string
    error?: Record<string, unknown>
    data?: Record<string, unknown>
    usage?: Record<string, unknown>
    name?: string
  } = {},
): Record<string, unknown> {
  return {
    task: {
      task_id: input.taskId,
      run_id: input.runId,
    },
    control_request: {
      request_id: input.requestId,
      command_id: input.commandId,
      canonical_name: options.name ?? input.text.split(/\s+/, 1)[0],
      created_at: "2026-07-25T00:00:00.000Z",
    },
    command_result: {
      request_id: input.requestId,
      command_id: input.commandId,
      status: options.status ?? "succeeded",
      name: options.name ?? input.text.split(/\s+/, 1)[0],
      summary: "done",
      result: {
        display_text: "done",
        data: options.queueId
          ? {
              queue: {
                queue_id: options.queueId,
              },
              ...(options.data ?? {}),
            }
          : options.data ?? {},
        followup_queue_id: options.queueId,
        usage: options.usage,
      },
      error: options.error,
      metadata: {
        durable: true,
        executed: (options.status ?? "succeeded") === "succeeded",
      },
      event_ids: ["event_01"],
    },
    event: {
      event_id: "event_01",
    },
  }
}

describe("command registry and parser", () => {
  test("contains exactly the required control surface", () => {
    const registry = createCommandRegistry()
    expect(registry.audit()).toEqual({
      valid: true,
      commandCount: 22,
      triggerCount: 63,
      errors: [],
    })
    expect(registry.names()).toEqual([
      "/agents",
      "/tasks",
      "/artifacts",
      "/export",
      "/mcp",
      "/skills",
      "/doctor",
      "/change",
      "/inject",
      "/memory",
      "/graph",
      "/status",
      "/trace",
      "/permissions",
      "/model",
      "/compact",
      "/context",
      "/resume",
      "/rewind",
      "/btw",
      "/eval",
      "/verify",
    ])
  })

  test("tokenizes quoted, escaped, and flag values with source ranges", () => {
    const result = tokenizeCommand(
      `/change "keep edge node" --scope=node_01 --reason escaped\\ value`,
    )
    expect(result.errors).toEqual([])
    expect(result.tokens.map((token) => token.value)).toEqual([
      "/change",
      "keep edge node",
      "--scope=node_01",
      "--reason",
      "escaped value",
    ])
    expect(result.tokens[1]?.quoted).toBe(true)
    expect(result.tokens[1]?.raw).toBe(`"keep edge node"`)
  })

  test("reports malformed input without ordinary-chat fallback", () => {
    const registry = createCommandRegistry()
    const plain = parseCommand("ordinary chat", registry)
    expect(plain.errors.map((entry) => entry.code)).toContain("not-command")
    const unknown = parseCommand("/unknown value", registry)
    expect(unknown.errors.map((entry) => entry.code)).toContain("unknown-command")
    const quote = parseCommand(`/change "unfinished`, registry)
    expect(quote.errors.map((entry) => entry.code)).toContain("unclosed-quote")
  })

  test("binds typed values and rejects invalid or duplicate flags", () => {
    const registry = createCommandRegistry()
    const graph = parseCommand("/graph --focus node_01 --depth 8", registry)
    expect(graph.errors).toEqual([])
    expect(graph.arguments.values).toEqual({
      focus: "node_01",
      depth: 8,
    })
    const invalid = parseCommand("/graph --depth 99 --depth 2", registry)
    expect(invalid.errors.map((entry) => entry.code)).toEqual([
      "duplicate-flag",
      "invalid-value",
      "unexpected-argument",
    ])
  })

  test("requires side question and change text", () => {
    const registry = createCommandRegistry()
    expect(parseCommand("/btw", registry).errors[0]?.code).toBe("missing-value")
    expect(parseCommand("/change", registry).errors[0]?.code).toBe("missing-value")
    expect(parseCommand("/btw why did this route change?", registry).arguments.values)
      .toEqual({ question: "why did this route change?" })
  })
})

describe("completion and palette", () => {
  test("ranks exact command ahead of fuzzy matches and preserves disabled entries", () => {
    const registry = createCommandRegistry()
    const suggestions = registry.search("tr", context, 20)
    expect(suggestions[0]?.descriptor.name).toBe("/trace")
    const disabled = registry.search("permissions", { ...context, sealed: true }, 20)
      .find((entry) => entry.descriptor.name === "/permissions")
    expect(disabled?.descriptor.name).toBe("/permissions")
    expect(disabled?.disabled).toBe(true)
    expect(disabled?.disabledReason).toContain("Sealed")
  })

  test("completes commands, flags, choices, and identities", () => {
    const registry = createCommandRegistry()
    const command = completionFor("/sta", 4, registry)
    expect(command.kind).toBe("command")
    expect(applyCommandCompletion("/sta", command, "/status")).toEqual({
      value: "/status ",
      cursor: 8,
    })
    const parsed = parseCommand("/inject ", registry)
    const flag = completionFor("/inject --ta", 12, registry)
    expect(argumentSuggestions({ parsed, completion: flag }).map((item) => item.value))
      .toContain("--target")
  })

  test("palette keeps stable selection across incremental updates", () => {
    const registry = createCommandRegistry()
    const palette = new CommandPalette({
      registry,
      context: () => context,
    })
    palette.open("/t", 2)
    const first = palette.getSnapshot()
    expect(first.open).toBe(true)
    expect(first.entries.length).toBeGreaterThan(0)
    const selected = first.selectedId
    palette.update("/tr", 3)
    expect(palette.getSnapshot().entries[0]?.label).toBe("/trace")
    expect(palette.getSnapshot().selectedId).toBeTruthy()
    expect(selected).toBeTruthy()
    palette.handleKey({ key: "ArrowDown" })
    const applied = palette.handleKey({ key: "Tab" })
    expect(applied.handled).toBe(true)
    expect(palette.getSnapshot().open).toBe(false)
    palette.destroy()
  })
})

describe("policy and request", () => {
  test("busy mutation queues and side question remains immediate", () => {
    const registry = createCommandRegistry()
    const policy = new CommandExecutionPolicy(registry)
    const change = policy.assert({
      parsed: parseCommand("/change replace the failed edge worker", registry),
      context,
      mode: "enqueue",
      busy: true,
    })
    expect(change.queue).toBe(true)
    expect(change.priority).toBe("next")
    const btw = policy.assert({
      parsed: parseCommand("/btw why is the run still active?", registry),
      context,
      mode: "enqueue",
      busy: true,
    })
    expect(btw.queue).toBe(false)
  })

  test("interrupt is priority now but read-only inspect cannot interrupt", () => {
    const registry = createCommandRegistry()
    const policy = new CommandExecutionPolicy(registry)
    expect(() =>
      policy.assert({
        parsed: parseCommand("/status", registry),
        context,
        mode: "interrupt",
        busy: true,
      })).toThrow("Read-only")
    const change = policy.assert({
      parsed: parseCommand("/change urgent requirement", registry),
      context,
      mode: "interrupt",
      busy: true,
    })
    expect(change.priority).toBe("now")
  })

  test("sealed hybrid commands admit reads and reject lifecycle mutations", () => {
    const registry = createCommandRegistry()
    const policy = new CommandExecutionPolicy(registry)
    const sealedContext = { ...context, sealed: true }
    for (const command of [
      "/mcp list",
      "/mcp show alpha",
      "/skills list",
      "/skills show research",
      "/agents list",
      "/agents show child-01",
    ]) {
      expect(policy.decide({
        parsed: parseCommand(command, registry),
        context: sealedContext,
        mode: "enqueue",
        busy: false,
      }).allowed).toBe(true)
    }
    for (const command of [
      "/mcp disable alpha",
      "/mcp auth-refresh alpha",
      "/skills update research",
      "/skills invoke research",
      "/agents kill child-01",
      "/agents steer child-01 --instruction continue",
    ]) {
      const decision = policy.decide({
        parsed: parseCommand(command, registry),
        context: sealedContext,
        mode: "enqueue",
        busy: false,
      })
      expect(decision.allowed).toBe(false)
      expect(decision.reason).toContain("Sealed autonomous")
    }
  })

  test("builds typed request with canonical arguments and a stable fingerprint", () => {
    const registry = createCommandRegistry()
    const parsed = parseCommand("/inject worker_failure --target worker_01", registry)
    const first = buildCommandRequest({ parsed, context, mode: "steer" })
    const second = buildCommandRequest({ parsed, context, mode: "steer" })
    expect(first.arguments).toMatchObject({
      kind: "worker_failure",
      target: "worker_01",
      argv: ["worker_failure", "--target", "worker_01"],
    })
    expect(first.priority).toBe("now")
    expect(first.idempotencyKey).not.toBe(second.idempotencyKey)
  })

  test("keeps sensitive structured overrides out of command text and raw argv", () => {
    const registry = createCommandRegistry()
    const display = JSON.stringify({
      request_id: "prompt-01",
      response: { token: "[present]" },
    })
    const parsed = parseCommand(
      `/mcp elicit alpha --request prompt-01 --response '${display}'`,
      registry,
    )
    const secret = {
      request_id: "prompt-01",
      response: { token: "not-for-command-history" },
    }
    const built = buildCommandRequest({
      parsed,
      context,
      mode: "enqueue",
      argumentOverrides: { response: secret },
    })
    expect(built.arguments.response).toEqual(secret)
    expect(built.text).not.toContain("not-for-command-history")
    expect(String(built.arguments.raw)).not.toContain("not-for-command-history")
    expect(built.arguments.argv).not.toContain("not-for-command-history")
    const retried = retryCommandRequest({
      parsed,
      context,
      previous: built,
    })
    expect(retried.arguments.response).toEqual(secret)
    expect(retried.text).not.toContain("not-for-command-history")
  })
})

describe("typed receipts, queue truth, and causal lookup", () => {
  test("admits applied and queued receipts and rejects identity mismatch", () => {
    const submitted = request()
    const applied = admitCommandReceipt(rawReceipt(submitted), submitted)
    expect(applied.phase).toBe("applied")
    expect(applied.eventIds).toEqual(["event_01"])
    expect(applied.scope).toBe("all")
    expect(applied.mode).toBe("enqueue")
    expect(applied.priority).toBe("next")
    expect(applied.idempotencyKey).toBe(submitted.idempotencyKey)
    const queued = admitCommandReceipt(
      rawReceipt(submitted, { status: "queued", queueId: "queue_01" }),
      submitted,
    )
    expect(queued.phase).toBe("queued")
    expect(queued.queueId).toBe("queue_01")
    const wrong = rawReceipt(submitted)
    ;(wrong.control_request as Record<string, unknown>).request_id = "request_wrong"
    ;(wrong.command_result as Record<string, unknown>).request_id = "request_wrong"
    expect(() => admitCommandReceipt(wrong, submitted)).toThrow("request mismatch")
  })

  test("derives queue solely from backend, projection, and receipt evidence", () => {
    const backend = admitQueueSnapshot({
      schema: "zyra.command-queue/v1",
      sequence: 3,
      entries: [
        {
          queue_id: "queue_02",
          session_id: "session_01",
          status: "queued",
          priority: "next",
          sequence: 2,
          created_at: "2026-07-25T00:00:02.000Z",
          updated_at: "2026-07-25T00:00:02.000Z",
          payload: {
            request: {
              task_id: "task_01",
              run_id: "run_01",
              request_id: "request_02",
              command_id: "cmd_02",
              canonical_name: "/change",
            },
          },
        },
        {
          queue_id: "queue_01",
          session_id: "session_01",
          status: "queued",
          priority: "now",
          sequence: 3,
          created_at: "2026-07-25T00:00:03.000Z",
          updated_at: "2026-07-25T00:00:03.000Z",
          payload: {
            request: {
              task_id: "task_01",
              run_id: "run_01",
              request_id: "request_01",
              command_id: "cmd_01",
              canonical_name: "/inject",
            },
          },
        },
      ],
    }, {
      taskId: "task_01",
      sessionId: "session_01",
      restored: true,
    })
    expect(backend.items.map((item) => item.queueId)).toEqual(["queue_01", "queue_02"])
    expect(backend.restored).toBe(true)
    const projected = queueItemFromProjection({
      id: "cmd_02",
      taskId: "task_01",
      runId: "run_01",
      sessionId: "session_01",
      lifecycle: "running",
      commandName: "/change",
      controlCommandId: "cmd_02",
      correlationId: "request_02",
      priority: "next",
      sequence: 4,
    })
    const merged = mergeQueueItems(backend.items, [projected])
    expect(merged.find((item) => item.commandId === "cmd_02")?.phase).toBe("running")
    expect(merged.find((item) => item.commandId === "cmd_02")?.source)
      .toBe("canonical-projection")
  })

  test("projection external store restores and updates without becoming a queue owner", () => {
    const projection = new CommandQueueProjection("task_01", "session_01")
    const snapshots: number[] = []
    const unsubscribe = projection.subscribe(() => {
      snapshots.push(projection.getSnapshot().revision)
    })
    projection.reconcile({
      projections: [
        {
          id: "cmd_01",
          taskId: "task_01",
          runId: "run_01",
          lifecycle: "queued",
          commandName: "/change",
          sequence: 1,
        },
      ],
      restored: true,
    })
    expect(projection.getSnapshot().pending).toHaveLength(1)
    expect(projection.getSnapshot().restored).toBe(true)
    expect(snapshots).toEqual([1])
    unsubscribe()
    projection.close()
  })

  test("reverse resolves events, spans, mutations, checkpoints, and artifacts", () => {
    const submitted = request()
    const receipt = admitCommandReceipt(rawReceipt(submitted), submitted)
    const correlation = correlateCommand(receipt, [
      {
        eventId: "event_01",
        eventType: "command_succeeded",
        taskId: "task_01",
        runId: "run_01",
        controlCommandId: submitted.commandId,
        correlationId: submitted.requestId,
        spanId: "span_01",
        mutationId: "mutation_01",
        checkpointId: "checkpoint_01",
        artifactIds: ["artifact_01"],
        sequence: 1,
        effective: true,
        terminal: true,
      },
    ])
    expect(correlation.complete).toBe(true)
    expect(correlation.eventIds).toEqual(["event_01"])
    expect(correlation.spanIds).toEqual(["span_01"])
    expect(correlation.mutationIds).toEqual(["mutation_01"])
    expect(correlation.checkpointIds).toEqual(["checkpoint_01"])
    expect(correlation.artifactIds).toEqual(["artifact_01"])
  })
})

describe("receipt ledger and isolated side question", () => {
  test("deduplicates replay and detects request conflicts", () => {
    const submitted = request()
    const receipt = admitCommandReceipt(rawReceipt(submitted), submitted)
    const ledger = new CommandReceiptLedger()
    ledger.remember(receipt)
    ledger.remember({ ...receipt, replayed: true })
    expect(ledger.getSnapshot().receipts).toHaveLength(1)
    expect(ledger.command(receipt.commandId)?.replayed).toBe(false)
    expect(() =>
      ledger.remember({
        ...receipt,
        commandId: "cmd_conflict",
      })).toThrow("request identity conflict")
    ledger.close()
  })

  test("exposes retryability for rejected, expired, and cancelled receipts", () => {
    const submitted = request()
    const denied = admitCommandReceipt(rawReceipt(submitted, {
      status: "failed",
      error: {
        code: "busy",
        message: "busy",
        retryable: true,
      },
    }), submitted)
    expect(commandReceiptRetryable(denied)).toBe(true)
  })

  test("/btw settles one tool-free answer with usage and close lifecycle", () => {
    const submitted = request("/btw why did this route change?")
    const receipt = admitCommandReceipt(rawReceipt(submitted, {
      data: {
        answer: "The scheduler fenced a stale lease.",
        tools_used: false,
        main_session_mutated: false,
        message_appended: false,
      },
      usage: {
        input_tokens: 12,
        output_tokens: 9,
        cached_tokens: 0,
      },
    }), submitted)
    expect(sideQuestionAudit(receipt)).toEqual({
      isolated: true,
      failures: [],
    })
    const session = new SideQuestionSession()
    session.begin("why did this route change?")
    session.settle(receipt)
    expect(session.getSnapshot().phase).toBe("answered")
    expect(session.getSnapshot().answer).toBe("The scheduler fenced a stale lease.")
    expect(session.getSnapshot().usage?.outputTokens).toBe(9)
    session.close()
    expect(session.getSnapshot().phase).toBe("closed")
  })
})

describe("projection index and semantic result overlay", () => {
  test("joins 01B command projections to causal events by reversible identities", () => {
    const index = new CommandProjectionIndex()
    index.replace([
      {
        id: "projection_command_01",
        taskId: "task_01",
        runId: "run_01",
        sessionId: "session_01",
        lifecycle: "applied",
        priority: "next",
        commandName: "/change",
        controlCommandId: "cmd_01",
        correlationId: "request_01",
        sequence: 5,
        attributes: {
          request_id: "request_01",
          queue_id: "queue_01",
        },
      },
    ], [
      {
        eventId: "event_root",
        eventType: "control_command_received",
        taskId: "task_01",
        runId: "run_01",
        controlCommandId: "cmd_01",
        correlationId: "request_01",
        spanId: "span_01",
        mutationId: "mutation_01",
        sequence: 4,
        effective: true,
      },
      {
        eventId: "event_leaf",
        eventType: "requirement_change_applied",
        taskId: "task_01",
        runId: "run_01",
        controlCommandId: "cmd_01",
        correlationId: "request_01",
        causationId: "event_root",
        spanId: "span_01",
        checkpointId: "checkpoint_01",
        artifactIds: ["artifact_01"],
        sequence: 5,
        effective: true,
        terminal: true,
      },
    ])
    expect(index.find("request_01")?.id).toBe("projection_command_01")
    expect(index.queueForTask("task_01")).toHaveLength(1)
    const trace = index.trace("cmd_01")
    expect(trace?.events.map((event) => event.eventId)).toEqual([
      "event_root",
      "event_leaf",
    ])
    expect(trace?.roots).toEqual(["event_root"])
    expect(trace?.leaves).toEqual(["event_leaf"])
    expect(trace?.correlation.mutationIds).toEqual(["mutation_01"])
    expect(trace?.correlation.checkpointIds).toEqual(["checkpoint_01"])
    expect(index.audit().orphanEventIds).toEqual([])
    index.close()
  })

  test("builds bounded command-specific sections and redacts sensitive values", () => {
    const submitted = request("/doctor")
    const receipt = admitCommandReceipt(rawReceipt(submitted, {
      data: {
        checks: [
          {
            id: "queue-owner",
            name: "Queue owner",
            status: "healthy",
            owner: "PromptQueueRuntime",
          },
        ],
        authorization_token: "must-not-render",
      },
    }), submitted)
    const model = buildCommandResultModel(receipt)
    expect(model.title).toBe("Runtime doctor")
    expect(model.sections.some((section) => section.id === "checks")).toBe(true)
    expect(model.searchText).not.toContain("must-not-render")
    expect(model.sections.flatMap((section) => section.rows)
      .flatMap((row) => row.fields)
      .some((field) => field.value === "[redacted]")).toBe(true)
  })
})

describe("queue recovery, actions, history, and disable behavior", () => {
  test("restores backend queue then reconciles canonical projection updates", async () => {
    let restores = 0
    let projections = [{
      id: "cmd_01",
      taskId: "task_01",
      runId: "run_01",
      sessionId: "session_01",
      lifecycle: "queued",
      commandName: "/change",
      controlCommandId: "cmd_01",
      correlationId: "request_01",
      priority: "next",
      sequence: 1,
    }]
    const queue = new CommandQueueProjection("task_01", "session_01")
    const recovery = new CommandQueueRecovery({
      adapter: {
        async restore() {
          restores += 1
          const snapshot = admitQueueSnapshot({
            sequence: 1,
            entries: [{
              queue_id: "queue_01",
              session_id: "session_01",
              status: "queued",
              priority: "next",
              sequence: 1,
              payload: {
                request: {
                  task_id: "task_01",
                  run_id: "run_01",
                  request_id: "request_01",
                  command_id: "cmd_01",
                  canonical_name: "/change",
                  arguments: { raw: "replace worker" },
                },
              },
            }],
          }, {
            taskId: "task_01",
            sessionId: "session_01",
            restored: true,
          })
          queue.replace(snapshot)
          return snapshot
        },
        reconcile(input) {
          return queue.reconcile(input)
        },
        projections() {
          return projections
        },
      },
    })
    const restored = await recovery.bind("task_01", "session_01")
    expect(restores).toBe(1)
    expect(restored?.items[0]?.text).toBe("/change replace worker")
    projections = [{ ...projections[0]!, lifecycle: "running", sequence: 2 }]
    const live = recovery.projectionChanged()
    expect(live?.items[0]?.phase).toBe("running")
    expect(live?.items[0]?.source).toBe("canonical-projection")
    expect(recovery.getSnapshot().phase).toBe("live")
    recovery.close()
    queue.close()
  })

  test("deduplicates a double cancel and retains typed settlement", async () => {
    const submitted = request("/change replace worker")
    const cancelled = admitCommandReceipt(
      rawReceipt(submitted, { status: "cancelled", queueId: "queue_01" }),
      submitted,
    )
    let resolve!: (receipt: typeof cancelled) => void
    const deferred = new Promise<typeof cancelled>((accept) => {
      resolve = accept
    })
    let calls = 0
    const actions = new CommandQueueActions({
      cancel: async () => {
        calls += 1
        return deferred
      },
      retry: async () => cancelled,
      refresh: async () => undefined,
    })
    const item = {
      ...queueItemFromProjection({
        id: submitted.commandId,
        taskId: submitted.taskId,
        runId: submitted.runId,
        sessionId: submitted.sessionId,
        lifecycle: "queued",
        commandName: "/change",
        controlCommandId: submitted.commandId,
        correlationId: submitted.requestId,
      }),
      queueId: "queue_01",
      requestId: submitted.requestId,
      text: submitted.text,
      cancellable: true,
    }
    const first = actions.cancel(item)
    const second = actions.cancel(item)
    expect(first).toBe(second)
    expect(calls).toBe(1)
    resolve(cancelled)
    expect((await first).phase).toBe("cancelled")
    expect(actions.getSnapshot().recent[0]?.receipt?.commandId)
      .toBe(submitted.commandId)
    actions.close()
  })

  test("redacts BTW history and emits a cross-owner healthy audit", () => {
    const registry = createCommandRegistry()
    const history = new CommandHistoryStore({ now: () => 1000 })
    const entry = history.record({
      text: "/btw what is the secret token?",
      scope: "task_01",
      taskId: "task_01",
    })
    expect(entry.text).toBe("/btw [redacted]")
    expect(history.search("/btw", { scope: "task_01" })[0]?.redacted).toBe(true)
    const projection = new CommandProjectionIndex()
    projection.replace([], [])
    const audit = auditCommandRuntime({
      registry,
      coordinator: {
        enabled: true,
        revision: 0,
        busy: false,
        records: [],
      },
      recovery: {
        phase: "idle",
        generation: 0,
        revision: 0,
        attempts: 0,
        pendingReasons: [],
        online: true,
        visible: true,
        enabled: true,
      },
      actions: {
        enabled: true,
        busy: false,
        revision: 0,
        active: [],
        recent: [],
      },
      projection: projection.audit(),
      history: history.getSnapshot(),
      now: 1000,
    })
    expect(audit.errors).toBe(0)
    expect(audit.checks.find((item) => item.id === "history-sensitive-input")?.passed)
      .toBe(true)
    projection.close()
    void history.close()
  })

  test("disabled coordinator and registry fail closed before transport", async () => {
    let submits = 0
    const transport: CommandTransport = {
      async submit(value) {
        submits += 1
        return rawReceipt(value)
      },
      async queue() {
        return { entries: [] }
      },
      async cancel() {
        return {}
      },
    }
    const registry = createCommandRegistry()
    const coordinator = new CommandCoordinator({
      registry,
      transport,
      context: () => context,
    })
    coordinator.disable("binding removed")
    expect(() => coordinator.submit("/status")).toThrow("binding removed")
    expect(submits).toBe(0)
    coordinator.enable()
    expect((await coordinator.submit("/status")).phase).toBe("applied")
    expect(submits).toBe(1)
    coordinator.close()
  })

  test("captures typed input, applies keyboard delivery mode, and settles history", () => {
    const registry = createCommandRegistry()
    const palette = new CommandPalette({
      registry,
      context: () => context,
    })
    const history = new CommandHistoryStore({ now: () => 2000 })
    const input = new CommandInputEngine({
      registry,
      palette,
      history,
      context: () => context,
      now: () => 2000,
    })
    input.update({
      value: "/change replace worker",
      cursor: 22,
    })
    const decision = input.key({
      key: "Enter",
      altKey: true,
      ctrlKey: true,
    })
    expect(decision.action).toBe("submit")
    expect(decision.mode).toBe("interrupt")
    const capture = input.beginSubmit(
      "/change replace worker",
      decision.mode,
    )
    const submitted = request("/change replace worker")
    const receipt = admitCommandReceipt(rawReceipt(submitted), submitted)
    input.settle(capture.id, receipt)
    expect(input.getSnapshot().lastReceipt?.commandId).toBe(receipt.commandId)
    expect(history.search("/change", { scope: "task_01" })[0]?.mode)
      .toBe("interrupt")
    input.compositionStart()
    expect(input.key({ key: "Enter", composing: true }).handled).toBe(false)
    input.close()
    palette.destroy()
    void history.close()
  })

  test("keeps result overlay lifecycle local while preserving receipt identity", () => {
    const submitted = request("/status")
    const receipt = admitCommandReceipt(rawReceipt(submitted), submitted)
    let now = 100
    const results = new CommandResultStore({ now: () => now })
    const opened = results.open(receipt)
    expect(results.find(receipt.requestId)?.id).toBe(opened.id)
    expect(results.getSnapshot().active?.model.commandId).toBe(receipt.commandId)
    now = 200
    expect(results.close(receipt.commandId)?.phase).toBe("closed")
    expect(results.getSnapshot().active).toBeUndefined()
    now = 300
    expect(results.activate(receipt.requestId)?.viewCount).toBe(2)
    expect(results.filtered(receipt.commandId, "")?.sections.length)
      .toBeGreaterThan(0)
    results.closeStore()
  })
})
