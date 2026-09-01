import { describe, expect, test } from "bun:test"
import { PassThrough, Writable } from "node:stream"
import { ProductComposer } from "../src/tui/composer.ts"

class TtyInput extends PassThrough {
  isTTY = true
  raw = false
  setRawMode(enabled: boolean): void { this.raw = enabled }
}

class Capture extends Writable {
  isTTY = true
  text = ""
  override _write(chunk: Buffer | string, _encoding: BufferEncoding, done: (error?: Error | null) => void): void {
    this.text += chunk.toString()
    done()
  }
}

function composer(input: TtyInput, output: Capture, editDraft: (initial: string) => Promise<string>, notices: string[]): ProductComposer {
  return new ProductComposer({
    stdin: input,
    output,
    running: () => false,
    editDraft,
    onChange: () => undefined,
    onNotice: (notice) => { if (notice) notices.push(notice) },
    onScroll: () => undefined,
  })
}

describe("product composer external editor", () => {
  test("replaces the draft and restores terminal mode after the editor succeeds", async () => {
    const input = new TtyInput()
    const output = new Capture()
    const notices: string[] = []
    const instance = composer(input, output, async (initial) => `${initial}\n编辑完成 ✅`, notices)

    const result = instance.read()
    input.write("原草稿\u0005")
    await Bun.sleep(10)
    input.write("\r")

    expect(await result).toEqual({ kind: "submit", text: "原草稿\n编辑完成 ✅", queue: false })
    expect(input.raw).toBeFalse()
    expect(output.text).toContain("\u001b[?2004l")
  })

  test("preserves the draft and remains usable after the editor fails", async () => {
    const input = new TtyInput()
    const output = new Capture()
    const notices: string[] = []
    const instance = composer(input, output, async () => { throw new Error("editor exited 7") }, notices)

    const result = instance.read()
    input.write("不能丢失\u0005")
    await Bun.sleep(10)
    input.write("，仍可输入\r")

    expect(await result).toEqual({ kind: "submit", text: "不能丢失，仍可输入", queue: false })
    expect(notices).toContain("外部编辑器未完成：editor exited 7 草稿已保留。")
    expect(input.raw).toBeFalse()
  })
})
