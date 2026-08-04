import { describe, expect, test } from "bun:test"
import { PassThrough, Writable } from "node:stream"
import { ServerSentEventDecoder } from "@zyra/typed-api-client"
import type { TaskProjection } from "@zyra/typed-api-client"
import { parseCliArgs } from "../src/args.ts"
import { CliApi, type IngressFrame } from "../src/api.ts"
import { observeTask } from "../src/commands/interactive.ts"
import { PromptDraft, PromptHistory } from "../src/input/draft.ts"
import { TerminalPrompt } from "../src/input/terminal-prompt.ts"
import { LineTranscriptRenderer } from "../src/render/line-renderer.ts"
import { SessionProjection } from "../src/session/projection.ts"

class Capture extends Writable {
  text = ""
  columns: number
  isTTY: boolean
  constructor(columns = 120, tty = false) { super(); this.columns = columns; this.isTTY = tty }
  override _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void): void {
    this.text += chunk.toString()
    callback()
  }
}

class TtyInput extends PassThrough {
  isTTY = true
  raw = false
  setRawMode(enabled: boolean): void { this.raw = enabled }
}

function frame(sequence: number, eventType = "runtime.text.delta", overrides: Partial<IngressFrame> = {}): IngressFrame {
  return {
    schema: "zyra.event-ingress-frame/v1",
    kind: "event",
    source: "runtime-event-spine",
    generation: 1,
    taskId: "task_test",
    sequence,
    previousSequence: sequence - 1,
    eventId: `event_${sequence}`,
    eventType,
    event: {
      schema: "zyra.runtime-event/v1",
      eventType,
      identity: { taskId: "task_test", runId: "run_test" },
      inline: eventType === "runtime.text.delta"
        ? { delta_bytes: sequence, content_digest: `digest-${sequence}` }
        : { tool_name: "shell", progress_digest: `progress-${sequence}`, percent: sequence % 100 },
      artifactRefs: [],
    },
    raw: {},
    ...overrides,
  }
}

describe("FE-S02 interactive command surface", () => {
  test("parses the four interactive top-level entries without taking over later ui", () => {
    expect(parseCliArgs([])).toMatchObject({ kind: "interactive", goal: undefined })
    expect(parseCliArgs(["inspect", "this", "workspace"])).toMatchObject({ kind: "interactive", goal: "inspect this workspace" })
    expect(parseCliArgs(["resume", "task_test"])).toMatchObject({ kind: "resume", identity: "task_test" })
    expect(parseCliArgs(["ls", "--limit=25"])).toMatchObject({ kind: "ls", limit: 25 })
    expect(() => parseCliArgs(["ui"])).toThrow("FE-S05")
  })

  test("keeps multiline and large paste drafts atomic and recoverable", () => {
    const draft = new PromptDraft()
    draft.insert("first")
    draft.newline()
    draft.insert("second\n")
    const paste = "large paste\n".repeat(900)
    const snapshot = draft.paste(paste)
    expect(snapshot.display).toContain("@paste:")
    expect(snapshot.display).not.toContain(paste)
    const stashed = draft.cancel()
    if (!stashed) throw new Error("draft stash expected")
    expect(stashed?.text).toContain("first\nsecond")
    expect(draft.submit()).toBeUndefined()
    expect(draft.restore()?.text).toBe(stashed.text)
    const submitted = draft.submit()!
    expect(submitted).toContain(paste)
    expect(submitted).toStartWith("first\nsecond")
  })

  test("limits history movement to multiline boundaries and completes slash/ref tokens", () => {
    const history = new PromptHistory()
    history.push("one")
    history.push("two\nlines")
    expect(history.previous(1)).toBeUndefined()
    expect(history.previous(0)).toBe("two\nlines")
    expect(history.next(0, 2)).toBeUndefined()
    expect(history.next(1, 2)).toBe("")
    expect(history.search("ONE")).toBe("one")
    expect(new PromptDraft().set("/he")).toMatchObject({ text: "/he" })
    const draft = new PromptDraft()
    draft.insert("/he")
    expect(draft.complete(["/help", "/exit", "@README.md"])).toEqual(["/help"])
  })

  test("drives real TTY Ctrl+J, bracketed paste, Escape restore, and history keys atomically", async () => {
    const stdin = new TtyInput()
    const stderr = new Capture(120, true)
    const prompt = new TerminalPrompt({ stdin, stderr, candidates: ["/help", "@README.md"] })

    const multiline = prompt.read()
    stdin.write("first\nsecond\r")
    expect(await multiline).toEqual({ kind: "submit", text: "first\nsecond" })
    expect(stdin.raw).toBeFalse()

    const restored = prompt.read()
    stdin.write("unsent\u001b\u0012\r")
    expect(await restored).toEqual({ kind: "submit", text: "unsent" })

    const historical = prompt.read()
    stdin.write("\u001b[A\r")
    expect(await historical).toEqual({ kind: "submit", text: "unsent" })

    const paste = "pasted\n".repeat(1_300)
    const pasted = prompt.read()
    stdin.write(`\u001b[200~${paste}\u001b[201~\r`)
    expect(await pasted).toEqual({ kind: "submit", text: paste })
    expect(stderr.text).toContain("@paste:")
    expect(stderr.text).not.toContain(paste)
    expect(stderr.text).not.toContain("\u001b[?1049")
  })
})

describe("FE-S02 bounded canonical observation", () => {
  test("deduplicates exact replay and stops on reordering or conflicting duplicates", () => {
    const projection = new SessionProjection({ taskId: "task_test", generation: 1 })
    expect(projection.apply(frame(1))?.sequence).toBe(1)
    expect(projection.apply(frame(1))).toBeUndefined()
    expect(() => projection.apply(frame(1, "runtime.tool.called", { eventId: "conflict" }))).toThrow("duplicated")
    expect(() => projection.apply(frame(3))).toThrow("gap")
  })

  test("handles 2,000+ transitions with a bounded recent search budget and observable degradation", () => {
    const projection = new SessionProjection({ taskId: "task_test", generation: 1, budget: 128 })
    projection.follow(false)
    for (let sequence = 1; sequence <= 2_101; sequence += 1) projection.apply(frame(sequence))
    const snapshot = projection.snapshot()
    expect(snapshot.records).toHaveLength(128)
    expect(snapshot.evicted).toBe(1_973)
    expect(snapshot.unread).toBe(2_101)
    expect(snapshot.revision).toBe("1:2101")
    expect(projection.search("digest-2101")).toHaveLength(1)
    projection.resized()
    expect(projection.snapshot().searchQuery).toBeUndefined()
    projection.disableSearch()
    expect(projection.search("digest-2101")).toEqual([])
    expect(projection.snapshot().lastSequence).toBe(2_101)
  })

  test("folds transient tool progress and never lets tool stdout overwrite line transcript", () => {
    const projection = new SessionProjection({ taskId: "task_test", generation: 1 })
    expect(projection.apply(frame(1, "runtime.tool.progress"))).toBeUndefined()
    expect(projection.apply(frame(2, "runtime.tool.progress"))).toBeUndefined()
    expect(projection.snapshot().records).toHaveLength(0)
    expect(projection.snapshot().action).toBe("runtime.tool.progress")
    expect(projection.snapshot().steps).toBe(2)
  })

  test("fails visibly when SSE is disabled and never invokes a polling fallback", async () => {
    const output = new Capture(120, false)
    let streamOpened = false
    const api = {
      async ingressCapabilities() {
        return { taskId: "task_test", generation: 1, subscriptionCursor: "server-cursor", subscriptionSequence: 0, sseAvailable: false, raw: {} }
      },
      async *snapshotIngress() {
        yield { cursor: "server-cursor", generation: 1, frames: [], hasMore: false, caughtUp: true, nextSequence: 0 }
      },
      async *streamIngress() { streamOpened = true; yield undefined as never },
    } as unknown as CliApi
    const task = {
      taskId: "task_test",
      runId: "run_test",
      status: "pending",
      terminal: false,
    } as TaskProjection
    await expect(observeTask({
      api,
      task,
      cwd: "G:\\agent-zoo",
      output,
      signal: new AbortController().signal,
      resume: true,
    })).rejects.toThrow("SSE event stream is disabled")
    expect(streamOpened).toBeFalse()
    expect(output.text).toContain("[disconnected]")
  })

  test("rejects a missing stream cursor before transport access", async () => {
    const api = new CliApi({ baseUrl: "http://127.0.0.1:1", timeoutMs: 1_000 })
    try {
      await expect(api.streamIngress("task_test", "", 1).next()).rejects.toThrow("requires a server cursor")
    } finally {
      api.close()
    }
  })
})

describe("FE-S02 append-only line renderer and SSE parser", () => {
  test("preserves order and refs at 80/120 columns without alternate-screen control", () => {
    for (const columns of [80, 120]) {
      const capture = new Capture(columns, false)
      const renderer = new LineTranscriptRenderer(capture)
      renderer.header({ cwd: "G:\\agent-zoo", taskId: "task_test", runId: "run_test", revision: "1:0" })
      renderer.record({ sequence: 1, eventId: "one", eventType: "runtime.text.delta", kind: "model", summary: "x".repeat(150), refs: ["content:abc"] })
      renderer.record({ sequence: 2, eventId: "two", eventType: "runtime.artifact.committed", kind: "artifact", summary: "artifact committed", refs: ["artifact:a1"] })
      expect(capture.text.indexOf("000001")).toBeLessThan(capture.text.indexOf("000002"))
      expect(capture.text).toContain("content:abc")
      expect(capture.text).toContain("artifact:a1")
      expect(capture.text).not.toContain("\u001b[?1049")
      expect(renderer.alternateScreenUsed).toBeFalse()
    }
  })

  test("decodes chunked named SSE frames with strict UTF-8 and bounded state", () => {
    const decoder = new ServerSentEventDecoder()
    const bytes = new TextEncoder().encode("retry: 750\nevent: ready\nid: r1\ndata: {\"kind\":\"ready\"}\n\nevent: close\ndata: {\"kind\":\"close\"}\n\n")
    const events = [...decoder.push(bytes.slice(0, 19)), ...decoder.push(bytes.slice(19)), ...decoder.finish()]
    expect(events).toEqual([
      { event: "ready", data: "{\"kind\":\"ready\"}", id: "r1", retry: 750 },
      { event: "close", data: "{\"kind\":\"close\"}", id: "r1", retry: undefined },
    ])
  })
})
