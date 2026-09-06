import { expect, test } from "bun:test"
import { PassThrough } from "node:stream"
import { ProductComposer } from "../src/tui/composer.ts"
import { pickProductItem } from "../src/tui/overlay/list-picker.ts"

test("a persistent terminal transfers input between prompts and overlays without entering cooked mode", async () => {
  const input = Object.assign(new PassThrough(), {
    isTTY: true,
    raw: false,
    modes: [] as boolean[],
    setRawMode(value: boolean) { this.raw = value; this.modes.push(value) },
  })
  const output = new PassThrough()
  const composer = new ProductComposer({
    stdin: input, output, retainRawMode: true, bracketedPaste: true,
    running: () => false, onChange() {}, onNotice() {}, onScroll() {},
  })
  try {
    const first = composer.read()
    input.write("/model\r")
    expect((await first).kind).toBe("submit")
    expect(input.raw).toBeTrue()
    const picker = pickProductItem({ stdin: input, output, title: "Model", items: [{id: "deepseek", label: "DeepSeek"}], onChange() {} })
    input.write("\r")
    expect((await picker)?.id).toBe("deepseek")
    const next = composer.read()
    input.write("next task\r")
    expect(await next).toMatchObject({kind: "submit", text: "next task"})
    expect(input.modes).not.toContain(false)
  } finally {
    composer.close()
    composer.releaseTerminal()
  }
  expect(input.raw).toBeFalse()
  expect(input.listenerCount("data")).toBe(0)
})
