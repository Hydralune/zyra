import { describe, expect, test } from "bun:test"
import { PassThrough, Writable } from "node:stream"
import { ZYRA_UI_EVENT_SCHEMA, type ZyraUiEvent } from "../src/presentation/events.ts"
import { ProductTuiShell } from "../src/tui/shell.ts"

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

function events(): ZyraUiEvent[] {
  return [
    { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "session", type: "session.started", sessionId: "session_tui", taskId: "task_tui" },
    { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "user", type: "user.message", messageId: "user_1", text: "请检查这个项目" },
    { schema: ZYRA_UI_EVENT_SCHEMA, eventId: "activity", type: "activity.started", activityId: "activity_1", label: "正在检查项目" },
  ]
}

describe("product TUI shell", () => {
  test("renders inline without alternate screen and redraws on resize", () => {
    const stdin = new TtyInput()
    const output = new TtyOutput()
    const shell = new ProductTuiShell({ stdin, output, workspace: "G:\\agent-zoo\\zyra" })
    shell.start()
    shell.update(events())
    output.columns = 120
    output.emit("resize")
    shell.finish()
    expect(shell.alternateScreenUsed).toBe(false)
    expect(output.text).toContain(">_ Zyra")
    expect(output.text).toContain("请检查这个项目")
    expect(output.text).toContain("正在检查项目")
    expect(output.text).toContain("\u001b[J")
    expect(output.text).not.toContain("\u001b[?1049")
  })

  test("submits multiline input and restores raw/bracketed-paste mode", async () => {
    const stdin = new TtyInput()
    const output = new TtyOutput()
    const shell = new ProductTuiShell({ stdin, output, workspace: "G:\\agent-zoo\\zyra" })
    shell.start()
    const reading = shell.read(false)
    stdin.write("第一行\n第二行\r")
    await expect(reading).resolves.toEqual({ kind: "submit", text: "第一行\n第二行", queue: false })
    expect(stdin.raw).toBe(false)
    expect(output.text).toContain("\u001b[?2004h")
    expect(output.text).toContain("\u001b[?2004l")
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
})
