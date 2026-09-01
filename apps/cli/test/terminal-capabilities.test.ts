import { expect, test } from "bun:test"
import { PassThrough, Writable } from "node:stream"
import { probeTerminalCapabilities } from "../src/tui/terminal-capabilities.ts"

class Output extends Writable {
  isTTY = true
  columns = 120
  rows = 40
  getColorDepth(): number { return 8 }
  override _write(_chunk: Buffer | string, _encoding: BufferEncoding, callback: (error?: Error | null) => void): void { callback() }
}

test("terminal capability probe uses conservative Windows product modes without consuming input", () => {
  const stdin = new PassThrough() as PassThrough & { isTTY: boolean }
  stdin.isTTY = true
  stdin.write("用户预输入")
  const capabilities = probeTerminalCapabilities({
    stdin,
    output: new Output(),
    platform: "win32",
    env: { WT_SESSION: "terminal-session" },
  })
  expect(capabilities).toMatchObject({
    inputTty: true,
    outputTty: true,
    columns: 120,
    rows: 40,
    colorLevel: 3,
    trueColor: true,
    unicode: true,
    inline: true,
    alternateScreen: false,
    bracketedPaste: false,
    terminalFamily: "windows-terminal",
  })
  expect(stdin.read()?.toString()).toBe("用户预输入")
})

test("terminal capability probe degrades redirected and dumb terminals safely", () => {
  const stdin = new PassThrough()
  const output = new Output()
  output.isTTY = false
  output.columns = 10_000
  output.rows = 1
  expect(probeTerminalCapabilities({
    stdin,
    output,
    platform: "linux",
    env: { TERM: "dumb", NO_COLOR: "1" },
  })).toMatchObject({
    inputTty: false,
    outputTty: false,
    columns: 500,
    rows: 8,
    colorLevel: 0,
    unicode: false,
    bracketedPaste: false,
    terminalFamily: "dumb",
  })
})
