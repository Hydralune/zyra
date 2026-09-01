import { CliApi } from "../../apps/cli/src/api.ts"
import { observeProductTask } from "../../apps/cli/src/commands/product.ts"
import { ProductTuiShell } from "../../apps/cli/src/tui/shell.ts"

function option(name: string): string | undefined {
  const prefix = `--${name}=`
  return process.argv.slice(2).find((value) => value.startsWith(prefix))?.slice(prefix.length)
}

const baseUrl = option("base-url")?.trim()
const taskId = option("task-id")?.trim()
if (!baseUrl || !taskId) {
  throw new TypeError("real_daemon_observer requires --base-url and --task-id")
}

const api = new CliApi({ baseUrl, timeoutMs: 15_000 })
const shell = new ProductTuiShell({
  stdin: process.stdin,
  output: process.stdout,
  workspace: process.cwd(),
})
const abort = new AbortController()
const abortObservation = () => {
  if (!abort.signal.aborted) abort.abort(new Error("observer interrupted"))
}
process.once("SIGTERM", abortObservation)

let started = false
try {
  const task = await api.task(taskId)
  shell.start()
  started = true
  const outcome = await observeProductTask({
    api,
    task,
    shell,
    signal: abort.signal,
    resume: false,
  })
  shell.finish()
  started = false
  process.stdout.write(`\nZYRA_OBSERVER_RESULT ${JSON.stringify(outcome)}\n`)
  process.exitCode = outcome.exitCode
} catch (error) {
  if (started) shell.close()
  const rendered = error instanceof Error ? `${error.name}: ${error.message}` : String(error)
  process.stdout.write(`\nZYRA_OBSERVER_ERROR ${JSON.stringify({ error: rendered })}\n`)
  process.exitCode = 1
} finally {
  api.close("real daemon observer complete")
  process.removeListener("SIGTERM", abortObservation)
}
