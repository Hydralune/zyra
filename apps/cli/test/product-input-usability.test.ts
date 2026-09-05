import { expect, test } from "bun:test"
import { PassThrough, Writable } from "node:stream"
import { ProductComposer } from "../src/tui/composer.ts"
import { LiveProductRenderer } from "../src/tui/live-renderer.ts"
import { completionState } from "../src/tui/overlay/completion.ts"
import { PromptDraft } from "../src/input/draft.ts"
import { pageProductText } from "../src/tui/overlay/pager.ts"
import type { ProductOverlay } from "../src/tui/overlay/model.ts"

class Input extends PassThrough {
  isTTY = true
  setRawMode(_enabled: boolean): void {}
}
class Output extends Writable {
  isTTY = true
  columns = 80
  rows = 24
  text = ""
  override _write(chunk: Buffer, _encoding: BufferEncoding, done: () => void): void {
    this.text += chunk.toString()
    done()
  }
}
function setup(running = false) {
  const input = new Input()
  const output = new Output()
  const composer = new ProductComposer({
    stdin: input, output, candidates: ["/mode", "/model"],
    running: () => running, onChange: () => {}, onNotice: () => {}, onScroll: () => {},
  })
  return { input, composer }
}

test("Escape dismisses completion without deleting a draft or interrupting a task", async () => {
  const { input, composer } = setup(true)
  const read = composer.read()
  input.write("/mo")
  input.write("\u001b")
  expect(composer.snapshot.text).toBe("/mo")
  input.write("del")
  input.write("\r")
  expect(await read).toEqual({ kind: "submit", text: "/model", queue: false })
})

test("multi-line arrows edit the intended line and history restores the current draft", async () => {
  const { input, composer } = setup()
  composer.history.push("earlier")
  const read = composer.read()
  input.write("ab\n甲乙")
  input.write("\u001b[A")
  input.write("X")
  expect(composer.snapshot.text).toBe("abX\n甲乙")
  input.write("\u001b[B")
  input.write("Y")
  expect(composer.snapshot.text).toBe("abX\n甲乙Y")
  input.write("\u001b[A\u001b[A")
  expect(composer.snapshot.text).toBe("earlier")
  input.write("\u001b[B")
  expect(composer.snapshot.text).toBe("abX\n甲乙Y")
  input.write("\r")
  expect((await read).kind).toBe("submit")
})

test("Alt+Enter and encoded Shift+Enter insert newlines without submitting", async () => {
  const { input, composer } = setup()
  const read = composer.read()
  input.write("one\u001b\rtwo\u001b[13;2uthree")
  expect(composer.snapshot.text).toBe("one\ntwo\nthree")
  input.write("\r")
  expect(await read).toEqual({ kind: "submit", text: "one\ntwo\nthree", queue: false })
})

test("ordinary paths do not become slash commands and Home stays at the start", () => {
  const draft = new PromptDraft()
  draft.set("请读取 /mo")
  expect(completionState(draft.snapshot(), ["/model"])).toBeUndefined()
  draft.set("\nsecond", 0)
  expect(draft.home().cursor).toBe(0)
})

test("typing repaints only the changed suffix and unchanged frames write nothing", () => {
  const output = new Output()
  let value = "a"
  let column = 3
  const renderer = new LiveProductRenderer(output, () => ({ text: `history\n› ${value}\nhelp\n`, cursor: { row: 1, column } }))
  renderer.start()
  output.text = ""
  renderer.renderNow()
  expect(output.text).toBe("")
  value = "ab"
  column = 4
  renderer.renderNow()
  expect(output.text).not.toContain("history")
  expect(output.text).toContain("› ab")
  output.text = ""
  column = 3
  renderer.renderNow()
  expect(output.text).not.toContain("\u001b[J")
  expect(output.text).not.toContain("› ab")
  renderer.close()
})

test("pager scrolls to the end of a long unbroken line and reflows on resize", async () => {
  const input = new Input()
  const output = new Output()
  let overlay: ProductOverlay | undefined
  const page = pageProductText({ stdin: input, output, title: "artifact", lines: ["甲".repeat(2_000) + "END_MARKER"], onChange: (value) => { overlay = value } })
  expect(overlay!.rows.map(row => row.label).join("")).not.toContain("END_MARKER")
  input.write("\u001b[F")
  expect(overlay!.rows.at(-1)!.label).toContain("END_MARKER")
  output.columns = 40
  output.emit("resize")
  input.write("\u001b[F")
  expect(overlay!.rows.at(-1)!.label).toContain("END_MARKER")
  input.write("\u001b")
  await page
  expect(overlay).toBeUndefined()
})
