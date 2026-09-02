import { describe, expect, test } from "bun:test"
import { PassThrough, Writable } from "node:stream"
import { ZYRA_UI_EVENT_SCHEMA, type ZyraUiEvent } from "../src/presentation/events.ts"
import { LiveProductRenderer } from "../src/tui/live-renderer.ts"
import { ProductComposer } from "../src/tui/composer.ts"
import { pickProductItem } from "../src/tui/overlay/list-picker.ts"
import { ProductTuiShell } from "../src/tui/shell.ts"
import { emergencyTerminalCleanup, TerminalSessionGuard } from "../src/tui/terminal-session.ts"

class TtyInput extends PassThrough {
  isTTY = true
  raw = false
  setRawMode(enabled: boolean): void { this.raw = enabled }
}

class TtyOutput extends Writable {
  isTTY = true
  columns = 80
  rows = 24
  text = ""
  override _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void): void {
    this.text += chunk.toString()
    callback()
  }
}

class BackpressuredTtyOutput extends Writable {
  isTTY = true
  columns = 80
  rows = 24
  text = ""
  readonly callbacks: Array<(error?: Error | null) => void> = []

  constructor() { super({ highWaterMark: 16 }) }

  override _write(chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void): void {
    this.text += chunk.toString()
    this.callbacks.push(callback)
  }

  release(): void { this.callbacks.shift()?.() }
}

function events(): ZyraUiEvent[] {
  return [
    { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "session", type: "session.started", sessionId: "session_tui", taskId: "task_tui" },
    { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "user", type: "user.message", messageId: "user_1", text: "请检查这个项目" },
    { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "activity", type: "activity.started", activityId: "activity_1", label: "正在检查项目" },
  ]
}

describe("product TUI shell", () => {
  test("emergency cleanup restores every active terminal session idempotently", () => {
    const stdin = new TtyInput()
    const output = new TtyOutput()
    const session = new TerminalSessionGuard(stdin, output, { bracketedPaste: true })
    session.enter()
    expect(stdin.raw).toBe(true)
    emergencyTerminalCleanup()
    emergencyTerminalCleanup()
    expect(session.active).toBe(false)
    expect(stdin.raw).toBe(false)
    expect(stdin.isPaused()).toBe(true)
    expect(output.text.match(/\u001b\[0m\u001b\[\?25h\u001b\[\?2004l/gu)).toHaveLength(2)
  })

  test("renders inline without alternate screen and redraws on resize", () => {
    const stdin = new TtyInput()
    const output = new TtyOutput()
    const shell = new ProductTuiShell({ stdin, output, workspace: "G:\\agent-zoo\\zyra", bracketedPaste: false })
    shell.start()
    shell.update(events())
    output.columns = 120
    output.emit("resize")
    shell.finish()
    expect(shell.alternateScreenUsed).toBe(false)
    expect(output.text).toContain(">_ Zyra")
    expect(output.text).toContain("请检查这个项目")
    expect(output.text).not.toContain("task_tui")
    expect(output.text).toContain("\u001b[J")
    expect(output.text).not.toContain("\u001b[?1049")
    expect(output.text).toContain("输入暂不可用")
  })

  test("advertises composer readiness only while input is actually bound", async () => {
    const stdin = new TtyInput()
    const output = new TtyOutput()
    const shell = new ProductTuiShell({ stdin, output, workspace: "G:\\agent-zoo\\zyra" })
    shell.start()
    expect(output.text).toContain("输入暂不可用")

    const reading = shell.read(false)
    expect(output.text).toContain("? 查看快捷键")
    stdin.write("ready\r")
    await reading

    expect(output.text.slice(-500)).toContain("输入暂不可用")
    shell.close()
  })

  test("submits multiline input and restores raw/bracketed-paste mode", async () => {
    const stdin = new TtyInput()
    const output = new TtyOutput()
    const shell = new ProductTuiShell({ stdin, output, workspace: "G:\\agent-zoo\\zyra", bracketedPaste: true })
    shell.start()
    const reading = shell.read(false)
    stdin.write("第一行\n第二行\r")
    await expect(reading).resolves.toEqual({ kind: "submit", text: "第一行\n第二行", queue: false })
    expect(stdin.raw).toBe(false)
    expect(output.text).toContain("\u001b[?2004h")
    expect(output.text).toContain("\u001b[?2004l")
    shell.close()
  })

  test("accepts rapid unbracketed Windows paste without treating its newline as submit", async () => {
    const stdin = new TtyInput()
    const output = new TtyOutput()
    const shell = new ProductTuiShell({
      stdin,
      output,
      workspace: "G:\\agent-zoo\\zyra",
      bracketedPaste: false,
    })
    shell.start()
    const reading = shell.read(false)
    stdin.write("first line\rsecond line")
    await new Promise((resolve) => setTimeout(resolve, 140))
    expect(output.text).toContain("first line")
    stdin.write("\r")
    await expect(reading).resolves.toEqual({
      kind: "submit",
      text: "first line\nsecond line",
      queue: false,
    })
    expect(output.text).not.toContain("\u001b[?2004h")
    expect(output.text).toContain("\u001b[?2004l")
    shell.close()
  })

  test("parses fragmented control keys without inserting escape bytes", async () => {
    const stdin = new TtyInput()
    const output = new TtyOutput()
    const shell = new ProductTuiShell({ stdin, output, workspace: "G:\\agent-zoo\\zyra" })
    shell.start()
    const reading = shell.read(false)
    stdin.write("甲乙")
    stdin.write("\u001b[1")
    stdin.write(";5D")
    stdin.write("中\r")
    await expect(reading).resolves.toEqual({ kind: "submit", text: "中甲乙", queue: false })
    shell.close()
  })

  test("discards an oversized paste and remains usable", async () => {
    const stdin = new TtyInput()
    const output = new TtyOutput()
    const shell = new ProductTuiShell({ stdin, output, workspace: "G:\\agent-zoo\\zyra" })
    shell.start()
    const reading = shell.read(false)
    stdin.write("\u001b[200~")
    stdin.write("x".repeat(300_000))
    stdin.write("\u001b[201~继续\r")
    await expect(reading).resolves.toEqual({ kind: "submit", text: "继续", queue: false })
    expect(output.text).toContain("粘贴超过 262144 bytes，已丢弃")
    expect(stdin.raw).toBe(false)
    shell.close()
  })

  test("queues with Tab and interrupts a running task with Escape", async () => {
    const stdin = new TtyInput()
    const output = new TtyOutput()
    const shell = new ProductTuiShell({ stdin, output, workspace: "G:\\agent-zoo\\zyra" })
    shell.start()
    const queued = shell.read(true)
    stdin.write("补充检查测试\t")
    await expect(queued).resolves.toEqual({ kind: "submit", text: "补充检查测试", queue: true })

    const interrupted = shell.read(true)
    stdin.write("\u001b")
    await expect(interrupted).resolves.toEqual({ kind: "interrupt" })
    expect(stdin.raw).toBe(false)
    shell.close()
  })

  test("keeps a bounded viewport and accepts PageUp/PageDown while editing", async () => {
    const stdin = new TtyInput()
    const output = new TtyOutput()
    output.rows = 12
    const shell = new ProductTuiShell({ stdin, output, workspace: "G:\\agent-zoo\\zyra" })
    shell.start()
    const transcript: ZyraUiEvent[] = [events()[0]!]
    for (let index = 0; index < 20; index += 1) {
      transcript.push({
        schema: ZYRA_UI_EVENT_SCHEMA,
        eventId: `assistant_${index}`,
        type: "assistant.message.completed",
        messageId: `assistant_${index}`,
        text: `第 ${index + 1} 条较长的历史消息，用来验证终端视口保持有界。`,
        source: "stream",
      })
    }
    shell.update(transcript)
    const reading = shell.read(false)
    stdin.write("\u001b[5~\u001b[6~继续\r")
    await expect(reading).resolves.toMatchObject({ kind: "submit", text: "继续" })
    expect(output.text).toContain("PageUp/PageDown")
    shell.close()
  })

  test("shows filtered slash completion and accepts it without losing the draft", async () => {
    const stdin = new TtyInput()
    const output = new TtyOutput()
    const shell = new ProductTuiShell({
      stdin,
      output,
      workspace: "G:\\agent-zoo\\zyra",
      candidates: ["/resume", "/status", "@apps/cli/src/main.ts"],
    })
    shell.start()
    const reading = shell.read(false)
    stdin.write("/res")
    expect(output.text).toContain("选择并恢复历史会话")
    stdin.write("\t\r")
    await expect(reading).resolves.toEqual({ kind: "submit", text: "/resume", queue: false })
    shell.close()
  })

  test("selects a recent session from a keyboard-driven picker", async () => {
    const stdin = new TtyInput()
    const output = new TtyOutput()
    const shell = new ProductTuiShell({ stdin, output, workspace: "G:\\agent-zoo\\zyra", bracketedPaste: false })
    shell.start()
    const picking = shell.pick("恢复会话", [
      { id: "session_1", label: "第一项", detail: "completed" },
      { id: "session_2", label: "第二项", detail: "running" },
    ])
    stdin.write("\u001b[B\r")
    await expect(picking).resolves.toMatchObject({ id: "session_2" })
    expect(output.text).toContain("恢复会话")
    expect(output.text).not.toContain("\u001b[?2004h")
    expect(output.text).toContain("\u001b[?2004l")
    expect(stdin.raw).toBe(false)
    expect(stdin.isPaused()).toBe(true)
    shell.close()
  })

  test("accepts input at the first observable picker paint", async () => {
    const stdin = new TtyInput()
    const output = new TtyOutput()
    let submitted = false
    const picking = pickProductItem({
      stdin,
      output,
      title: "模型",
      items: [{ id: "model-1", label: "Model 1" }],
      onChange: (overlay) => {
        if (overlay && !submitted) {
          submitted = true
          stdin.write("\r")
        }
      },
    })

    await expect(picking).resolves.toMatchObject({ id: "model-1" })
    expect(stdin.raw).toBe(false)
    expect(stdin.isPaused()).toBe(true)
  })

  test("accepts input at the first observable composer paint", async () => {
    const stdin = new TtyInput()
    const output = new TtyOutput()
    let submitted = false
    const composer = new ProductComposer({
      stdin,
      output,
      running: () => false,
      onChange: () => {
        if (!submitted) {
          submitted = true
          stdin.write("首帧输入\r")
        }
      },
      onNotice: () => undefined,
      onScroll: () => undefined,
    })

    await expect(composer.read()).resolves.toEqual({ kind: "submit", text: "首帧输入", queue: false })
    expect(stdin.raw).toBe(false)
    expect(stdin.isPaused()).toBe(true)
  })

  test("pages bounded content and restores the terminal after Escape", async () => {
    const stdin = new TtyInput()
    const output = new TtyOutput()
    const shell = new ProductTuiShell({ stdin, output, workspace: "G:\\agent-zoo\\zyra" })
    shell.start()
    const paging = shell.page("大型 Diff", Array.from({ length: 40 }, (_, index) => `line ${index + 1}`))
    stdin.write("\u001b[6~\u001b")
    await paging
    expect(output.text).toContain("16–30 / 40")
    expect(stdin.raw).toBe(false)
    expect(stdin.isPaused()).toBe(true)
    shell.close()
  })

  test("preserves fragmented Unicode input during high-frequency event and resize redraws", async () => {
    const stdin = new TtyInput()
    const output = new TtyOutput()
    const shell = new ProductTuiShell({ stdin, output, workspace: "G:\\agent-zoo\\zyra" })
    shell.start()
    const reading = shell.read(false)
    const expected = "中文输入 é 👨‍👩‍👧‍👦 — 异步重绘不丢字"
    const bytes = Buffer.from(expected, "utf8")
    const base = events()[0]!
    let eventSequence = 0
    for (let offset = 0; offset < bytes.length; offset += 1) {
      for (let burst = 0; burst < 100; burst += 1) {
        eventSequence += 1
        shell.update([
          base,
          {
            schema: ZYRA_UI_EVENT_SCHEMA,
            eventId: `async-${eventSequence}`,
            type: "activity.updated",
            activityId: "async-redraw",
            label: "高频后台事件",
            summary: `event ${eventSequence}`,
          },
        ])
        if (eventSequence % 37 === 0) {
          output.columns = 60 + (eventSequence % 141)
          output.rows = 18 + (eventSequence % 43)
          output.emit("resize")
        }
      }
      stdin.write(bytes.subarray(offset, offset + 1))
    }
    stdin.write("\r")

    await expect(reading).resolves.toEqual({ kind: "submit", text: expected, queue: false })
    await Promise.resolve()
    expect(shell.renderDiagnostics.requested).toBeGreaterThan(5_000)
    expect(shell.renderDiagnostics.coalesced).toBeGreaterThan(4_000)
    expect(shell.renderDiagnostics.writes).toBeLessThan(500)
    expect(output.text).not.toContain("�")
    shell.close()
  })

  test("coalesces redraws while the terminal output applies backpressure", async () => {
    const output = new BackpressuredTtyOutput()
    let revision = 0
    const renderer = new LiveProductRenderer(output, () => `frame ${revision}\n${"x".repeat(80)}\n`)
    renderer.start()
    for (revision = 1; revision <= 10_000; revision += 1) renderer.render()

    expect(renderer.diagnostics).toMatchObject({
      requested: 10_001,
      snapshots: 1,
      writes: 1,
      backpressureCount: 1,
    })
    expect(renderer.diagnostics.coalesced).toBeGreaterThan(9_000)

    revision = 10_001
    output.release()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(renderer.diagnostics.snapshots).toBe(2)
    expect(renderer.diagnostics.writes).toBe(2)
    expect(output.text).toContain("frame 10001")
    renderer.close()
    output.release()
  })
})
