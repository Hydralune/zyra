import { describe, expect, test } from "bun:test"
import { mkdtemp, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { Readable, Writable } from "node:stream"
import { parseCliArgs } from "../src/args.ts"
import { CliUsageError } from "../src/contracts.ts"
import { JsonlWriter } from "../src/output.ts"
import { externalDeadlineDelayMs, runMain, stopTerminalBestEffort } from "../src/main.ts"
import { goalFrom } from "../src/runner.ts"

class Capture extends Writable {
  text = ""

  override _write(
    chunk: Buffer | string,
    _encoding: BufferEncoding,
    callback: (error?: Error | null) => void,
  ): void {
    this.text += chunk.toString()
    callback()
  }
}

class BrokenPipe extends Writable {
  override _write(
    _chunk: Buffer | string,
    _encoding: BufferEncoding,
    callback: (error?: Error | null) => void,
  ): void {
    const error = new Error("pipe closed") as NodeJS.ErrnoException
    error.code = "EPIPE"
    callback(error)
  }
}

describe("FE-S01 CLI argument and output contract", () => {
  test("keeps help/version flags separate from product prompt text", () => {
    expect(parseCliArgs(["--help"]).kind).toBe("help")
    expect(parseCliArgs(["-h"]).kind).toBe("help")
    expect(parseCliArgs(["--version"]).kind).toBe("version")
    expect(parseCliArgs(["-V"]).kind).toBe("version")
    expect(parseCliArgs(["help"])).toMatchObject({ kind: "interactive", goal: "help" })
    expect(parseCliArgs(["version"])).toMatchObject({ kind: "interactive", goal: "version" })
    expect(parseCliArgs(["doctor"])).toMatchObject({ kind: "doctor", autoStart: false })
    expect(parseCliArgs(["doctor", "--autostart=true", "--bundle=doctor.json"]))
      .toMatchObject({ kind: "doctor", autoStart: true, bundle: "doctor.json" })
  })

  test("uses @zyra/commands argument binding for run and scenario", () => {
    const run = parseCliArgs([
      "run",
      "--base-url=http://127.0.0.1:9010",
      "--autostart=false",
      "--timeout=90s",
      "return",
      "a",
      "verified",
      "answer",
    ])
    expect(run.kind).toBe("run")
    if (run.kind !== "run") throw new Error("run command expected")
    expect(run.goal).toBe("return a verified answer")
    expect(run.autoStart).toBeFalse()
    expect(run.timeoutMs).toBe(90_000)

    const scenario = parseCliArgs([
      "scenario",
      "create",
      "--scenario-id=foundation.short-owner-chain",
      "--labels={\"source\":\"cli\"}",
      "exercise",
      "the",
      "real",
      "owners",
    ])
    expect(scenario.kind).toBe("scenario")
    if (scenario.kind !== "scenario") throw new Error("scenario command expected")
    expect(scenario.input).toBe("exercise the real owners")
    expect(scenario.labels).toEqual({ source: "cli" })
  })

  test("accepts long-horizon timeouts within the bounded client window", () => {
    const run = parseCliArgs(["run", "--timeout=14m", "complete", "the", "task"])
    expect(run.kind).toBe("run")
    if (run.kind !== "run") throw new Error("run command expected")
    expect(run.timeoutMs).toBe(840_000)
    const longRun = parseCliArgs(["run", "--timeout=2600s", "complete", "the", "long", "task"])
    expect(longRun.kind).toBe("run")
    if (longRun.kind !== "run") throw new Error("run command expected")
    expect(longRun.timeoutMs).toBe(2_600_000)
    const ultraLongRun = parseCliArgs(["run", "--timeout=150m", "complete", "the", "ultra-long", "task"])
    expect(ultraLongRun.kind).toBe("run")
    if (ultraLongRun.kind !== "run") throw new Error("run command expected")
    expect(ultraLongRun.timeoutMs).toBe(9_000_000)
    expect(() => parseCliArgs(["run", "--timeout=241m", "task"])).toThrow(CliUsageError)
  })

  test("accepts bounded piped stdin and rejects invalid origins", async () => {
    const command = parseCliArgs(["run"])
    expect(command.kind).toBe("run")
    if (command.kind !== "run") throw new Error("run command expected")
    const goal = await goalFrom(
      command,
      Readable.from(["piped ", "goal\n"]),
      new AbortController().signal,
    )
    expect(goal).toBe("piped goal")
    const tty = Readable.from([]) as Readable & { isTTY?: boolean }
    tty.isTTY = true
    await expect(goalFrom(command, tty, new AbortController().signal)).rejects.toBeInstanceOf(CliUsageError)
    expect(() => parseCliArgs(["run", "--base-url=file:///tmp/socket", "goal"])).toThrow(CliUsageError)
    expect(() => parseCliArgs(["run", "--file=a", "goal"])).toThrow(CliUsageError)
  })

  test("preserves benchmark task syntax through stdin without argv parsing", async () => {
    const command = parseCliArgs(["run", "--timeout=0ms"])
    expect(command.kind).toBe("run")
    if (command.kind !== "run") throw new Error("run command expected")
    const source = "- leading option-like text\nquoted \"值\" and 'single'\n第二行"
    expect(await goalFrom(
      command,
      Readable.from([Buffer.from(source, "utf8")]),
      new AbortController().signal,
    )).toBe(source)
    expect(command.timeoutMs).toBe(0)
  })

  test("reads a bounded UTF-8 goal file without exporting its path", async () => {
    const directory = await mkdtemp(join(tmpdir(), "zyra-cli-goal-"))
    const path = join(directory, "goal.txt")
    try {
      await writeFile(path, "file supplied goal\n", "utf8")
      const command = parseCliArgs(["run", `--file=${path}`])
      expect(command.kind).toBe("run")
      if (command.kind !== "run") throw new Error("run command expected")
      expect(await goalFrom(
        command,
        Readable.from([]),
        new AbortController().signal,
      )).toBe("file supplied goal")
    } finally {
      await rm(directory, { recursive: true, force: true })
    }
  })

  test("writes parseable JSONL, strips ANSI, and redacts tokens and paths", () => {
    const capture = new Capture()
    const writer = new JsonlWriter(capture)
    expect(writer.write({
      schema: "example/v1",
      message: "\u001b[31mplain\u001b[0m",
      token: "top-secret",
      cwd: "G:\\private\\workspace",
      detail: "failed at G:\\private\\workspace\\secret.txt and /home/user/private.txt",
      endpoint: "http://127.0.0.1:1234/capability/opaque-secret/v1/dispatch",
      credential_url: "https://alice:secret@example.invalid/path",
      nested: "Authorization: Bearer top-secret token=also-secret",
    })).toBeTrue()
    const line = capture.text.trim()
    expect(line).not.toContain("\u001b")
    expect(line).not.toContain("top-secret")
    expect(line).not.toContain("G:\\private")
    expect(line).not.toContain("/home/user")
    expect(line).not.toContain("opaque-secret")
    expect(line).not.toContain("alice:secret")
    expect(line).not.toContain("top-secret")
    expect(line).not.toContain("also-secret")
    expect(JSON.parse(line)).toEqual({
      schema: "example/v1",
      message: "plain",
      token: "[redacted]",
      cwd: "[redacted]",
      detail: "failed at [redacted-path] and [redacted-path]",
      endpoint: "http://127.0.0.1:1234/capability/[redacted]/v1/dispatch",
      credential_url: "[redacted]",
      nested: "Authorization: Bearer [redacted] token=[redacted]",
    })
  })

  test("stops writing quietly when a pipe consumer closes", async () => {
    const writer = new JsonlWriter(new BrokenPipe())
    expect(writer.write({ schema: "example/v1" })).toBeTrue()
    await Bun.sleep(0)
    expect(writer.closed).toBeTrue()
    expect(writer.error).toBeUndefined()
    expect(writer.write({ schema: "example/v1", ignored: true })).toBeFalse()
  })

  test("keeps human help on stderr and stdout as JSONL only", async () => {
    const stdout = new Capture()
    const stderr = new Capture()
    const code = await runMain(["--help"], { stdout, stderr })
    expect(code).toBe(0)
    expect(stderr.text).toContain("Zyra CLI command surface")
    const lines = stdout.text.trim().split("\n").map((line) => JSON.parse(line))
    expect(lines).toHaveLength(2)
    expect(lines[0].type).toBe("event")
    expect(lines[1]).toMatchObject({ type: "result", exit_code: 0 })
    expect(stdout.text).not.toContain("\u001b")
  })

  test("maps usage and unavailable daemon to deterministic exit codes", async () => {
    const badOut = new Capture()
    const badErr = new Capture()
    const tty = Readable.from([]) as Readable & { isTTY?: boolean }
    tty.isTTY = true
    expect(await runMain(["run"], { stdout: badOut, stderr: badErr, stdin: tty })).toBe(2)
    expect(JSON.parse(badOut.text.trim()).exit_code).toBe(2)

    const offlineOut = new Capture()
    const offlineErr = new Capture()
    const code = await runMain([
      "run",
      "--base-url=http://127.0.0.1:18992",
      "--autostart=false",
      "goal",
    ], { stdout: offlineOut, stderr: offlineErr })
    expect(code).toBe(3)
    const result = JSON.parse(offlineOut.text.trim().split("\n").at(-1)!)
    expect(result).toMatchObject({ type: "result", exit_code: 3 })
    expect(result.task_id).toBeUndefined()
  })

  test("terminal cleanup failures are diagnostic and preserve the task result", async () => {
    const warning = await stopTerminalBestEffort({
      async stop() {
        throw new Error("controlled terminal cleanup failure")
      },
    })
    expect(warning).toEqual({
      schema: "zyra.cli-terminal-cleanup-warning.v1",
      error: "Error",
      message: "controlled terminal cleanup failure",
      task_result_preserved: true,
    })
  })

  test("derives an explicit CLI cancellation delay only from a valid external deadline", () => {
    expect(externalDeadlineDelayMs({ ZYRA_EXTERNAL_DEADLINE_EPOCH_MS: "2500" }, 1_000)).toBe(1_500)
    expect(externalDeadlineDelayMs({ ZYRA_EXTERNAL_DEADLINE_EPOCH_MS: "500" }, 1_000)).toBe(0)
    expect(externalDeadlineDelayMs({ ZYRA_EXTERNAL_DEADLINE_EPOCH_MS: "invalid" }, 1_000)).toBeUndefined()
    expect(externalDeadlineDelayMs({}, 1_000)).toBeUndefined()
  })
})
