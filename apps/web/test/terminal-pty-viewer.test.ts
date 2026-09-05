import { describe, expect, test } from "bun:test"

test("redrawing CJK over a shifted wide-cell row preserves every glyph and combining mark", () => {
  const screen = new TerminalScreen({ rows: 4, cols: 30 })
  screen.write("x甲乙丙丁\r中文测试完成\u001b[K")
  expect(terminalLineText(screen.snapshot().lines[0]!)).toStartWith("中文测试完成")
  screen.write("\r甲\u0301乙\u001b[K")
  expect(terminalLineText(screen.snapshot().lines[0]!)).toStartWith("甲\u0301乙")
})
import type { TaskApi } from "../src/api/task-api.ts"
import {
  TERMINAL_PROTOCOL,
  TerminalContractError,
  decodeTerminalFrameData,
  encodeTerminalClientCommand,
  parseTerminalServerFrame,
  parseTerminalSession,
  sameTerminalBinding,
  type TerminalBinding,
  type TerminalOutputFrame,
} from "../src/features/terminal/contracts.ts"
import {
  AnsiStreamParser,
  stripUnsafeTerminalControls,
} from "../src/features/terminal/ansi.ts"
import {
  TerminalScreen,
  terminalLineText,
  terminalVisibleRows,
} from "../src/features/terminal/screen.ts"
import {
  indexedTerminalColor,
  renderTerminalViewport,
  safeTerminalHyperlink,
  searchTerminalScreen,
  terminalColorCss,
  terminalRenderRuns,
} from "../src/features/terminal/render.ts"
import {
  TerminalInputBudget,
  normalizeTerminalKey,
  normalizeTerminalPaste,
  splitTerminalInput,
} from "../src/features/terminal/input.ts"
import { TerminalOrderedWriter } from "../src/features/terminal/writer.ts"
import { TerminalResizeCoordinator } from "../src/features/terminal/resize.ts"
import { TerminalReplayWindow } from "../src/features/terminal/backpressure.ts"
import {
  TerminalReconnectCoordinator,
  terminalSocketUrl,
} from "../src/features/terminal/reconnect.ts"
import {
  TerminalTabStore,
  migrateTerminalTabs,
  type TerminalTabState,
  type TerminalTabStorage,
} from "../src/features/terminal/tabs.ts"
import {
  classifyTerminalLine,
  redactTerminalText,
  TerminalStructuredOutput,
} from "../src/features/terminal/structured.ts"
import { TerminalTranscript } from "../src/features/terminal/transcript.ts"
import {
  extractTerminalSelection,
  searchTerminalSelections,
  TerminalSelectionModel,
  terminalLineSelection,
  terminalWordSelection,
} from "../src/features/terminal/selection.ts"
import { TerminalDiagnostics } from "../src/features/terminal/diagnostics.ts"
import { TerminalProtocolAudit } from "../src/features/terminal/protocol-audit.ts"
import {
  TerminalRuntime,
  type TerminalSocket,
  type TerminalSocketEventMap,
} from "../src/features/terminal/runtime.ts"


const DIGEST = "a".repeat(64)

function binding(overrides: Partial<TerminalBinding> = {}): TerminalBinding {
  return Object.freeze({
    taskId: "task-terminal-web",
    runId: "run-terminal-web",
    terminalId: "terminal-web",
    sessionId: "session-terminal-web",
    workspaceId: "workspace-terminal-web",
    workspaceRevision: 4,
    workerId: "worker-terminal-web",
    commandId: "command-terminal-web",
    toolCallId: "tool-terminal-web",
    spanId: "span-terminal-web",
    ...overrides,
  })
}

function bindingWire(value = binding()): Record<string, unknown> {
  return {
    task_id: value.taskId,
    run_id: value.runId,
    terminal_id: value.terminalId,
    session_id: value.sessionId,
    workspace_id: value.workspaceId,
    workspace_revision: value.workspaceRevision,
    worker_id: value.workerId,
    command_id: value.commandId,
    tool_call_id: value.toolCallId,
    span_id: value.spanId,
  }
}

function statusWire(
  phase = "running",
  patch: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    phase,
    pid: 42_002,
    cwd: ".",
    title: "Terminal",
    rows: 24,
    cols: 80,
    cursor: 0,
    earliest_cursor: 0,
    exit_code: null,
    signal: null,
    started_at: "2026-07-24T00:00:00.000Z",
    updated_at: "2026-07-24T00:00:00.000Z",
    exited_at: null,
    viewers: 0,
    spilled_bytes: 0,
    binary_bytes: 0,
    input_sequence: 0,
    resize_sequence: 0,
    state_mutation_id: `mutation-${phase}`,
    ...patch,
  }
}

function sessionWire(options: {
  phase?: string
  cursor?: number
  earliestCursor?: number
  ticket?: string
} = {}): Record<string, unknown> {
  const cursor = options.cursor ?? 0
  const earliest = options.earliestCursor ?? 0
  return {
    protocol: TERMINAL_PROTOCOL,
    binding: bindingWire(),
    status: statusWire(options.phase ?? "running", {
      cursor,
      earliest_cursor: earliest,
    }),
    permission: {
      effect: "allow",
      decision_id: "decision-terminal-web",
      request_id: null,
      permit_id: null,
      reason_code: "permission.allow",
      reason: "allowed",
      canonical_owner: "typescript.PermissionCoordinator",
    },
    socket_path: "/tasks/task-terminal-web/terminals/terminal-web/connect",
    ticket: options.ticket ?? null,
    ticket_expires_at: options.ticket
      ? "2026-07-24T00:01:00.000Z"
      : null,
    correlation_id: "correlation-terminal-web",
    causation_id: "cause-terminal-web",
    event_id: "event-terminal-web",
    intervention_counted: false,
    human_intervention_count: 0,
  }
}

function outputFrame(
  firstCursor: number,
  text: string,
  sequence: number,
): TerminalOutputFrame {
  return {
    kind: "output",
    protocol: TERMINAL_PROTOCOL,
    binding: binding(),
    firstCursor,
    nextCursor: firstCursor + new TextEncoder().encode(text).byteLength,
    text,
    byteLength: new TextEncoder().encode(text).byteLength,
    sha256: DIGEST,
    redacted: false,
    sequence,
  }
}

class MemoryTabs implements TerminalTabStorage {
  readonly values = new Map<string, TerminalTabState>()

  load(key: string): unknown {
    return this.values.get(key)
  }

  save(key: string, value: TerminalTabState): void {
    this.values.set(key, value)
  }

  remove(key: string): void {
    this.values.delete(key)
  }
}

class FakeSocket implements TerminalSocket {
  readonly OPEN = 1
  readyState = 1
  binaryType: BinaryType = "arraybuffer"
  readonly sent: string[] = []
  readonly closes: Array<{ code?: number; reason?: string }> = []
  readonly #listeners = new Map<
    keyof TerminalSocketEventMap,
    Set<(event: never) => void>
  >()

  send(data: string | ArrayBufferLike | Blob | ArrayBufferView): void {
    if (typeof data !== "string") throw new TypeError("test socket expects text")
    this.sent.push(data)
  }

  close(code?: number, reason?: string): void {
    this.readyState = 3
    this.closes.push({ code, reason })
  }

  addEventListener<K extends keyof TerminalSocketEventMap>(
    kind: K,
    listener: (event: TerminalSocketEventMap[K]) => void,
  ): void {
    const selected = this.#listeners.get(kind) ?? new Set()
    selected.add(listener as (event: never) => void)
    this.#listeners.set(kind, selected)
  }

  removeEventListener<K extends keyof TerminalSocketEventMap>(
    kind: K,
    listener: (event: TerminalSocketEventMap[K]) => void,
  ): void {
    this.#listeners.get(kind)?.delete(listener as (event: never) => void)
  }

  emit<K extends keyof TerminalSocketEventMap>(
    kind: K,
    event: TerminalSocketEventMap[K],
  ): void {
    for (const listener of this.#listeners.get(kind) ?? []) {
      listener(event as never)
    }
  }
}

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0))
  await new Promise((resolve) => setTimeout(resolve, 0))
}


describe("terminal protocol contracts", () => {
  test("normalizes snake-case sessions and rejects owner/path/cursor drift", () => {
    const parsed = parseTerminalSession(sessionWire())
    expect(parsed.binding).toEqual(binding())
    expect(parsed.status.phase).toBe("running")
    expect(parsed.permission.canonicalOwner).toBe(
      "typescript.PermissionCoordinator",
    )
    expect(sameTerminalBinding(parsed.binding, binding())).toBe(true)

    const wrongOwner = structuredClone(sessionWire())
    ;(wrongOwner.permission as Record<string, unknown>).canonical_owner =
      "python.LocalPermission"
    expect(() => parseTerminalSession(wrongOwner)).toThrow(
      "typescript.PermissionCoordinator",
    )

    const unsafePath = structuredClone(sessionWire())
    unsafePath.socket_path = "/tasks/other/terminals/terminal-web/connect"
    expect(() => parseTerminalSession(unsafePath)).toThrow(
      TerminalContractError,
    )

    const badCursor = structuredClone(sessionWire())
    ;(badCursor.status as Record<string, unknown>).earliest_cursor = 2
    expect(() => parseTerminalSession(badCursor)).toThrow(
      "earliest cursor exceeds",
    )
  })

  test("binds server frames and encodes bounded client commands", async () => {
    const frame = parseTerminalServerFrame({
      kind: "output",
      protocol: "zyra.terminal.v1" as const,
      binding: bindingWire(),
      first_cursor: 0,
      next_cursor: 5,
      text: "hello",
      byte_length: 5,
      sha256: DIGEST,
      redacted: false,
      sequence: 1,
    }, binding())
    expect(frame.kind).toBe("output")

    const redacted = parseTerminalServerFrame({
      kind: "output",
      protocol: TERMINAL_PROTOCOL,
      binding: bindingWire(),
      first_cursor: 5,
      next_cursor: 9,
      text: "****",
      byte_length: 4,
      sha256: DIGEST,
      redacted: true,
      sequence: 2,
    }, binding())
    expect(redacted.kind).toBe("output")

    const binary = parseTerminalServerFrame({
      kind: "output",
      protocol: TERMINAL_PROTOCOL,
      binding: bindingWire(),
      first_cursor: 9,
      next_cursor: 10,
      text: "<",
      byte_length: 1,
      sha256: DIGEST,
      redacted: false,
      sequence: 3,
    }, binding())
    expect(binary.kind).toBe("output")

    expect(() => parseTerminalServerFrame({
      kind: "cursor",
      protocol: TERMINAL_PROTOCOL,
      binding: bindingWire(binding({ terminalId: "another-terminal" })),
      cursor: 1,
      sequence: 1,
    }, binding())).toThrow("another session")

    const encoded = JSON.parse(encodeTerminalClientCommand({
      kind: "input",
      protocol: TERMINAL_PROTOCOL,
      terminalId: "terminal-web",
      connectionId: "connection-web",
      sequence: 1,
      data: "echo",
      actorId: "operator",
    }))
    expect(encoded.terminal_id).toBe("terminal-web")
    expect(encoded.connection_id).toBe("connection-web")
    expect(encoded.actor_id).toBe("operator")
    expect(encoded.terminalId).toBeUndefined()

    await expect(decodeTerminalFrameData("{broken")).rejects.toThrow(
      "not valid JSON",
    )
    await expect(decodeTerminalFrameData(JSON.stringify({
      kind: "pong",
    }))).resolves.toEqual({ kind: "pong" })
  })
})

describe("ANSI screen and rendering", () => {
  test("parses fragmented CSI/OSC safely and preserves styles", () => {
    const parser = new AnsiStreamParser()
    const first = parser.push("plain\u001b[38;2;10")
    const second = parser.push(";20;30mcolor\u001b[0m\u001b]0;Build")
    const third = parser.push(" terminal\u0007")
    expect(first.some((token) => token.kind === "text")).toBe(true)
    expect(second.some((token) => token.kind === "csi")).toBe(true)
    expect(third.some((token) => token.kind === "osc")).toBe(true)
    expect(parser.snapshot().state).toBe("ground")
    expect(parser.snapshot().pendingText).toBe("")
  })

  test("applies cursor, erase, SGR, alternate screen and scrollback", () => {
    const screen = new TerminalScreen({
      rows: 3,
      cols: 12,
      maximumScrollback: 20,
    })
    screen.write("alpha\r\nbeta\r\ngamma\r\ndelta")
    expect(screen.snapshot().scrollback.length).toBeGreaterThan(0)
    screen.write("\u001b[2;1H\u001b[31;1mRED\u001b[0m")
    const selected = terminalVisibleRows(screen.snapshot(), 0, 100)
    expect(selected.map((line) => terminalLineText(line)).join("\n")).toContain(
      "RED",
    )
    const redCell = selected.flatMap((line) => line.cells)
      .find((cell) => cell.text === "R")
    expect(redCell?.style.bold).toBe(true)
    expect(redCell?.style.foreground).toEqual({
      kind: "indexed",
      value: 1,
    })

    screen.write("\u001b]0;PTY viewer\u0007")
    expect(screen.snapshot().title).toBe("PTY viewer")
    screen.write("\u001b[?1049halternate")
    expect(screen.snapshot().alternate).toBe(true)
    screen.write("\u001b[?1049l")
    expect(screen.snapshot().alternate).toBe(false)
  })

  test("renders viewport runs, colors, search, and safe links", () => {
    const screen = new TerminalScreen({ rows: 4, cols: 20 })
    screen.write("first\r\n\u001b[38;5;196msecond match\u001b[0m\r\nthird")
    const snapshot = screen.snapshot()
    const runs = terminalRenderRuns(snapshot.lines[1]!, 1)
    expect(runs.some((run) => run.text.includes("second"))).toBe(true)
    expect(indexedTerminalColor(196)).toBe("rgb(255 0 0)")
    expect(terminalColorCss({
      kind: "rgb",
      value: [10, 20, 30],
    }, "rgb(255 255 255)")).toBe("rgb(10 20 30)")
    expect(searchTerminalScreen(snapshot, "match")).toHaveLength(1)
    expect(renderTerminalViewport(snapshot, {
      firstRow: 0,
      visibleRows: 2,
      followOutput: false,
      overscan: 0,
    }).rows).toHaveLength(2)
    expect(safeTerminalHyperlink("https://example.test/path")).toBe(
      "https://example.test/path",
    )
    expect(safeTerminalHyperlink("javascript:alert(1)")).toBeUndefined()
    expect(stripUnsafeTerminalControls(
      "safe\u001b]52;c;clipboard\u0007text",
    )).not.toContain("clipboard")
  })
})

describe("input, ordering, replay and reconnect", () => {
  test("normalizes keys/paste and enforces byte budgets", () => {
    expect(normalizeTerminalKey({ key: "ArrowUp" }, {
      applicationCursorKeys: false,
      bracketedPaste: false,
    })).toBe("\u001b[A")
    expect(normalizeTerminalKey({ key: "ArrowUp", ctrlKey: true }, {
      applicationCursorKeys: false,
      bracketedPaste: false,
    })).toBe("\u001b[1;5A")
    expect(normalizeTerminalKey({ key: "c", ctrlKey: true }, {
      applicationCursorKeys: false,
      bracketedPaste: false,
    })).toBe("\u0003")
    expect(normalizeTerminalPaste("one\n\u001b[201~two", {
      applicationCursorKeys: false,
      bracketedPaste: true,
    })).toBe("\u001b[200~one\rtwo\u001b[201~")
    expect(splitTerminalInput("ééé", 4)).toEqual(["éé", "é"])

    const budget = new TerminalInputBudget({
      maximumFrameBytes: 256,
      maximumBurstBytes: 256,
      refillBytesPerSecond: 100,
      maximumPasteBytes: 300,
    }, 1_000)
    expect(budget.reserve("x".repeat(200), { now: 1_000 }).accepted).toBe(true)
    expect(budget.reserve("x".repeat(100), { now: 1_000 }).reason).toBe(
      "burst_exhausted",
    )
    expect(budget.reserve("x".repeat(100), { now: 2_000 }).accepted).toBe(true)
    expect(budget.reserve("x".repeat(301), {
      paste: true,
      now: 2_000,
    }).reason).toBe("paste_too_large")
  })

  test("serializes writes and coalesces sequenced resizes", async () => {
    const calls: string[] = []
    const writer = new TerminalOrderedWriter(async (value) => {
      await new Promise((resolve) => setTimeout(resolve, value === "one" ? 5 : 0))
      calls.push(value)
    }, { maximumQueuedBytes: 1_024 })
    const receipts = await Promise.all([
      writer.push("one"),
      writer.push("two"),
      writer.push("three"),
    ])
    expect(calls).toEqual(["one", "two", "three"])
    expect(receipts.map((item) => item.sequence)).toEqual([1, 2, 3])
    await writer.flush()

    const sizes: Array<{ rows: number; cols: number; sequence: number }> = []
    const resize = new TerminalResizeCoordinator((value) => {
      sizes.push(value)
    }, { delayMs: 10 })
    resize.schedule({ rows: 1, cols: 2_000 })
    await new Promise((resolve) => setTimeout(resolve, 20))
    resize.schedule({ rows: 30, cols: 100 })
    resize.schedule({ rows: 31, cols: 101 })
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(sizes[0]).toMatchObject({ rows: 2, cols: 1_000, sequence: 1 })
    expect(sizes.at(-1)).toMatchObject({ rows: 31, cols: 101, sequence: 2 })
    resize.close()
  })

  test("detects replay gaps, pauses on budget and resumes after ACK", () => {
    const replay = new TerminalReplayWindow({
      maximumUnackedBytes: 1_024,
    })
    const first = outputFrame(0, "a".repeat(600), 1)
    expect(replay.admit(first).accepted).toBe(true)
    const exhausted = replay.admit(outputFrame(600, "b".repeat(500), 2))
    expect(exhausted.reason).toBe("acknowledgement_exhausted")
    expect(exhausted.state.paused).toBe(true)
    replay.acknowledge(600)
    expect(replay.admit(outputFrame(600, "b".repeat(500), 2)).accepted).toBe(
      true,
    )
    const gap = replay.admit(outputFrame(1_200, "gap", 3))
    expect(gap.needsResync).toBe(true)
    expect(gap.reason).toBe("cursor_gap")
    expect(replay.admit(outputFrame(1_100, "dup", 2)).duplicate).toBe(true)
  })

  test("uses deterministic reconnect generations and safe same-origin URLs", () => {
    const reconnect = new TerminalReconnectCoordinator({
      baseDelayMs: 100,
      maximumDelayMs: 1_000,
      jitter: () => 0,
    })
    const ticket = reconnect.requestTicket(1_000)
    reconnect.connecting(ticket.generation)
    reconnect.opened(ticket.generation, 10, 1_001)
    const backoff = reconnect.disconnected({
      code: 1006,
      reason: "network",
      now: 2_000,
    })
    expect(backoff.phase).toBe("backoff")
    expect(backoff.retryAt).toBe(2_100)
    expect(reconnect.ready(2_099)).toBe(false)
    expect(reconnect.retry(2_100).phase).toBe("ticket")

    const url = terminalSocketUrl(
      "https://console.example.test/api",
      "/tasks/task/terminals/terminal/connect",
      { ticket: "ticket", cursor: 12 },
    )
    expect(url).toStartWith("wss://console.example.test/")
    expect(url).toContain("cursor=12")
    expect(() => terminalSocketUrl(
      "https://console.example.test",
      "//evil.example.test/socket",
      { ticket: "ticket", cursor: 0 },
    )).toThrow("unsafe")
  })
})

describe("tabs, transcript, selection and diagnostics", () => {
  test("migrates durable refs without process truth and numbers tabs", () => {
    const migrated = migrateTerminalTabs({
      active: "terminal-web",
      tabs: [{
        id: "terminal-web",
        task_id: "task-terminal-web",
        workspace_id: "workspace-terminal-web",
        session_id: "session-terminal-web",
        title: "Restored",
        phase: "running",
        cursor: 44,
        rows: 30,
        cols: 100,
        pid: 999,
      }],
    }, "workspace-terminal-web")
    expect(migrated.tabs[0]?.acknowledgedCursor).toBe(44)
    expect(migrated.tabs[0]).not.toHaveProperty("pid")

    const memory = new MemoryTabs()
    const tabs = new TerminalTabStore("workspace-terminal-web", {
      storage: memory,
      maximumTabs: 2,
    })
    tabs.open(binding(), { title: "One", phase: "running" })
    tabs.open(binding({ terminalId: "terminal-two" }), { title: "Two" })
    tabs.update("terminal-web", { acknowledgedCursor: 91, pinned: true })
    tabs.open(binding({ terminalId: "terminal-three" }), { title: "Three" })
    expect(tabs.snapshot().tabs).toHaveLength(2)
    expect(tabs.snapshot().tabs.some((tab) => tab.terminalId === "terminal-web"))
      .toBe(true)
    tabs.close("terminal-three")
    expect(tabs.snapshot().active).toBe("terminal-web")
  })

  test("classifies structured lines and redacts secrets", () => {
    expect(classifyTerminalLine(
      "fatal: authentication failed",
    ).level).toBe("error")
    expect(classifyTerminalLine(
      "warning: retrying request",
    ).level).toBe("warning")
    expect(redactTerminalText(
      "access_token=secret-value exact-secret",
      ["exact-secret"],
    )).toEqual({
      text: "access_token=[REDACTED] [REDACTED]",
      redacted: true,
    })
    const output = new TerminalStructuredOutput(3)
    output.push("one\nwarning: two\nfatal: three\nfour")
    output.finish()
    expect(output.snapshot().lines).toHaveLength(3)
    expect(output.snapshot().lines.filter((line) => line.level === "warning")).toHaveLength(1)
    expect(output.snapshot().lines.filter((line) => line.level === "error")).toHaveLength(1)
  })

  test("bounds transcript and supports selection/search semantics", () => {
    const transcript = new TerminalTranscript(binding(), {
      maximumEntries: 8,
      maximumBytes: 4_096,
    })
    transcript.status(parseTerminalSession(sessionWire()).status)
    transcript.input("echo alpha", 1)
    transcript.output("alpha beta\nsecond line", {
      firstCursor: 0,
      nextCursor: 22,
      sequence: 1,
      redacted: false,
    })
    transcript.resize(30, 100, 1)
    transcript.backpressure(2_048, 4_096, true)
    expect(transcript.snapshot().entries.length).toBeGreaterThan(3)

    const screen = new TerminalScreen({ rows: 3, cols: 20 })
    screen.write("alpha beta\r\nsecond alpha")
    const snapshot = screen.snapshot()
    const word = terminalWordSelection(snapshot, { row: 0, column: 7 })
    expect(extractTerminalSelection(snapshot, word)).toBe("beta")
    expect(extractTerminalSelection(
      snapshot,
      terminalLineSelection(snapshot, 1),
    )).toContain("second alpha")
    expect(searchTerminalSelections(snapshot, "alpha").matches).toHaveLength(2)

    const model = new TerminalSelectionModel(snapshot)
    model.begin({ row: 0, column: 0 })
    model.extend({ row: 0, column: 5 })
    expect(model.snapshot().text).toBe("alpha")
    expect(model.search("alpha").matches).toHaveLength(2)
    expect(model.next().activeIndex).toBe(1)
  })

  test("diagnostics and protocol audit detect gaps and duplicate commands", () => {
    let now = 1_000
    const diagnostics = new TerminalDiagnostics(binding(), {
      now: () => now,
    })
    diagnostics.connected(1, {
      acceptedCursor: 0,
      earliestCursor: 0,
      maximumUnackedBytes: 1_024,
    })
    diagnostics.output(outputFrame(0, "hello", 1))
    diagnostics.acknowledge(5)
    now += 10
    diagnostics.output(outputFrame(8, "gap", 2))
    expect(diagnostics.snapshot().records.some(
      (record) => record.code.includes("gap"),
    )).toBe(true)

    const audit = new TerminalProtocolAudit(binding(), { now: () => now })
    expect(audit.server(outputFrame(0, "hello", 1))).toBe("accepted")
    expect(audit.server(outputFrame(0, "hello", 1))).toBe("duplicate")
    const input = {
      kind: "input" as const,
      protocol: "zyra.terminal.v1" as const,
      terminalId: "terminal-web",
      connectionId: "connection-web",
      sequence: 1,
      data: "x",
      actorId: "operator",
    }
    expect(audit.client(input)).toBe("accepted")
    expect(audit.client(input)).toBe("duplicate")
    expect(audit.snapshot().duplicateFrames).toBeGreaterThanOrEqual(2)
  })
})

describe("terminal runtime", () => {
  test("connects, renders output, ACKs, resizes, inputs and only kills explicitly", async () => {
    const socket = new FakeSocket()
    const calls = {
      create: 0,
      ticket: 0,
      kill: 0,
      urls: [] as string[],
    }
    const taskApi = {
      async terminalCreate() {
        calls.create += 1
        return { terminal: sessionWire() }
      },
      async terminalGet() {
        return { terminal: sessionWire() }
      },
      async terminalTicket() {
        calls.ticket += 1
        return { terminal: sessionWire({ ticket: "signed-ticket" }) }
      },
      async terminalKill() {
        calls.kill += 1
        return { terminal: sessionWire({ phase: "killed" }) }
      },
      terminalWebSocketUrl(
        _taskId: string,
        _terminalId: string,
        path: string,
        options: { ticket: string; cursor: number; protocol: string },
      ) {
        const url = terminalSocketUrl(
          "https://console.example.test",
          path,
          options,
        )
        calls.urls.push(url)
        return url
      },
    } as unknown as TaskApi
    const tabs = new TerminalTabStore("workspace-terminal-web", {
      storage: new MemoryTabs(),
    })
    const runtime = new TerminalRuntime({
      taskApi,
      tabs,
      socket: () => socket,
      origin: () => "https://console.example.test",
      reconnect: false,
    })

    const created = await runtime.create({
      taskId: "task-terminal-web",
      runId: "run-terminal-web",
      sessionId: "session-terminal-web",
      workerId: "worker-terminal-web",
      commandId: "command-terminal-web",
      toolCallId: "tool-terminal-web",
      spanId: "span-terminal-web",
      actorId: "operator",
      command: "python -i",
      title: "Terminal",
      cwd: ".",
      shell: "",
      rows: 24,
      cols: 80,
      sealed: false,
      idempotencyKey: "terminal-create-web",
    })
    expect(created.binding?.terminalId).toBe("terminal-web")
    expect(calls.create).toBe(1)
    expect(calls.ticket).toBe(1)
    expect(calls.urls[0]).toContain("signed-ticket")

    socket.emit("open", {} as Event)
    socket.emit("message", {
      data: JSON.stringify({
        kind: "hello",
        protocol: TERMINAL_PROTOCOL,
        binding: bindingWire(),
        status: statusWire("running"),
        accepted_cursor: 0,
        earliest_cursor: 0,
        connection_id: "connection-web",
        heartbeat_ms: 5_000,
        maximum_unacked_bytes: 4 * 1_024 * 1_024,
      }),
    } as MessageEvent)
    await settle()
    expect(runtime.snapshot("terminal-web").connected).toBe(true)

    expect(runtime.input("terminal-web", "echo runtime\r", {
      permissionPermitId: "permit-terminal-input",
    })).toEqual([1])
    expect(JSON.parse(socket.sent.at(-1)!)).toMatchObject({
      kind: "input",
      permission_permit_id: "permit-terminal-input",
    })
    runtime.resize("terminal-web", 30, 100)
    await settle()
    expect(socket.sent.map((value) => JSON.parse(value).kind)).toContain(
      "resize",
    )

    socket.emit("message", {
      data: JSON.stringify({
        kind: "output",
        protocol: TERMINAL_PROTOCOL,
        binding: bindingWire(),
        first_cursor: 0,
        next_cursor: 13,
        text: "runtime ready",
        byte_length: 13,
        sha256: DIGEST,
        redacted: false,
        sequence: 1,
      }),
    } as MessageEvent)
    await settle()
    const snapshot = runtime.snapshot("terminal-web")
    expect(snapshot.replay.acknowledgedCursor).toBe(13)
    expect(snapshot.screen.lines.some(
      (line) => terminalLineText(line).includes("runtime ready"),
    )).toBe(true)
    expect(snapshot.transcript.entries.some(
      (entry) => entry.kind === "output",
    )).toBe(true)
    expect(snapshot.diagnostics.acknowledgedCursor).toBe(13)
    expect(snapshot.protocolAudit.acceptedFrames).toBeGreaterThan(0)
    expect(JSON.parse(socket.sent.at(-1)!).kind).toBe("ack")

    runtime.closeTab("terminal-web")
    expect(socket.closes.at(-1)?.code).toBe(1000)
    expect(calls.kill).toBe(0)

    await runtime.adopt(sessionWire(), { connect: false })
    await runtime.kill({
      taskId: "task-terminal-web",
      runId: "run-terminal-web",
      terminalId: "terminal-web",
      sessionId: "session-terminal-web",
      workerId: "worker-terminal-web",
      toolCallId: "tool-terminal-web",
      spanId: "span-terminal-web",
      actorId: "operator",
      reason: "explicit kill",
      sealed: false,
      idempotencyKey: "terminal-kill-web",
    })
    expect(calls.kill).toBe(1)
    expect(runtime.snapshot("terminal-web").status?.phase).toBe("killed")
    runtime.dispose()
  })

  test("serializes ticket issue and cancels a pending connect when its tab closes", async () => {
    let resolveTicket: (
      value: Readonly<Record<string, unknown>>,
    ) => void = () => undefined
    const pendingTicket = new Promise<Readonly<Record<string, unknown>>>(
      (resolve) => { resolveTicket = resolve },
    )
    let ticketCalls = 0
    const sockets: FakeSocket[] = []
    const taskApi = {
      async terminalTicket() {
        ticketCalls += 1
        return pendingTicket
      },
      terminalWebSocketUrl(
        _taskId: string,
        _terminalId: string,
        path: string,
        options: { ticket: string; cursor: number; protocol: string },
      ) {
        return terminalSocketUrl(
          "https://console.example.test",
          path,
          options,
        )
      },
    } as unknown as TaskApi
    const runtime = new TerminalRuntime({
      taskApi,
      tabs: new TerminalTabStore("workspace-terminal-connect"),
      socket: () => {
        const socket = new FakeSocket()
        sockets.push(socket)
        return socket
      },
      origin: () => "https://console.example.test",
      reconnect: false,
    })
    await runtime.adopt(sessionWire(), { connect: false })
    const first = runtime.connect("terminal-web")
    const second = runtime.connect("terminal-web")
    expect(ticketCalls).toBe(1)
    runtime.closeTab("terminal-web")
    const firstFailure = first.then(
      () => "",
      (failure) => failure instanceof Error ? failure.message : String(failure),
    )
    const secondFailure = second.then(
      () => "",
      (failure) => failure instanceof Error ? failure.message : String(failure),
    )
    resolveTicket({ terminal: sessionWire({ ticket: "signed-ticket" }) })
    expect(await firstFailure).toContain("cancelled")
    expect(await secondFailure).toContain("cancelled")
    expect(sockets).toHaveLength(0)
    expect(runtime.snapshot("terminal-web").reconnect.phase).toBe("closed")
    runtime.dispose()
  })

  test("records ticket failures and permits only one concurrent socket attempt", async () => {
    let ticketCalls = 0
    const socket = new FakeSocket()
    const taskApi = {
      async terminalTicket() {
        ticketCalls += 1
        if (ticketCalls === 1) throw new Error("ticket owner unavailable")
        return { terminal: sessionWire({ ticket: "signed-ticket" }) }
      },
      terminalWebSocketUrl(
        _taskId: string,
        _terminalId: string,
        path: string,
        options: { ticket: string; cursor: number; protocol: string },
      ) {
        return terminalSocketUrl(
          "https://console.example.test",
          path,
          options,
        )
      },
    } as unknown as TaskApi
    const runtime = new TerminalRuntime({
      taskApi,
      tabs: new TerminalTabStore("workspace-terminal-ticket-failure"),
      socket: () => socket,
      origin: () => "https://console.example.test",
      reconnect: false,
    })
    await runtime.adopt(sessionWire(), { connect: false })
    await expect(runtime.connect("terminal-web")).rejects.toThrow(
      "ticket owner unavailable",
    )
    expect(runtime.snapshot("terminal-web").reconnect.phase).toBe("failed")
    expect(runtime.snapshot("terminal-web").error?.code).toBe(
      "terminal_ticket_failed",
    )

    const first = runtime.connect("terminal-web")
    const second = runtime.connect("terminal-web")
    await Promise.all([first, second])
    expect(ticketCalls).toBe(2)
    socket.emit("open", {} as Event)
    expect(runtime.snapshot("terminal-web").reconnect.phase).toBe("open")
    runtime.dispose()
  })

  test("rejects cursor gaps without repainting them", async () => {
    const socket = new FakeSocket()
    const taskApi = {
      async terminalTicket() {
        return { terminal: sessionWire({ ticket: "signed-ticket" }) }
      },
      terminalWebSocketUrl(
        _taskId: string,
        _terminalId: string,
        path: string,
        options: { ticket: string; cursor: number; protocol: string },
      ) {
        return terminalSocketUrl("http://127.0.0.1:8000", path, options)
      },
    } as unknown as TaskApi
    const runtime = new TerminalRuntime({
      taskApi,
      tabs: new TerminalTabStore("workspace-terminal-web"),
      socket: () => socket,
      reconnect: false,
    })
    await runtime.adopt(sessionWire(), { connect: true })
    socket.emit("open", {} as Event)
    socket.emit("message", {
      data: JSON.stringify({
        kind: "hello",
        protocol: TERMINAL_PROTOCOL,
        binding: bindingWire(),
        status: statusWire(),
        accepted_cursor: 0,
        earliest_cursor: 0,
        connection_id: "connection-web",
        heartbeat_ms: 5_000,
        maximum_unacked_bytes: 4 * 1_024 * 1_024,
      }),
    } as MessageEvent)
    await settle()
    socket.emit("message", {
      data: JSON.stringify({
        kind: "output",
        protocol: TERMINAL_PROTOCOL,
        binding: bindingWire(),
        first_cursor: 5,
        next_cursor: 8,
        text: "gap",
        byte_length: 3,
        sha256: DIGEST,
        redacted: false,
        sequence: 1,
      }),
    } as MessageEvent)
    await settle()
    const snapshot = runtime.snapshot("terminal-web")
    expect(snapshot.error?.code).toBe("cursor_gap")
    expect(snapshot.replay.cursor).toBe(0)
    expect(snapshot.screen.lines.every(
      (line) => !terminalLineText(line).includes("gap"),
    )).toBe(true)
    runtime.dispose()
  })
})
