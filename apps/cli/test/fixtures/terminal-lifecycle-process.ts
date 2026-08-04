import { TerminalNodeLifecycle } from "../../src/terminal/lifecycle.ts"

const baseUrl = process.argv[2]
const startupRoot = process.argv[3]
if (!baseUrl || !startupRoot) throw new Error("terminal-lifecycle-process requires base URL and startup root")

const lifecycle = await TerminalNodeLifecycle.create({ baseUrl, startupRoot })
const enabled = await lifecycle.start()
const status = lifecycle.status()
const disabled = await lifecycle.stop("real HTTP lifecycle fixture complete")
process.stdout.write(`${JSON.stringify({
  schema: "zyra.test-terminal-lifecycle/v1",
  enabled,
  status,
  disabled,
})}\n`)
