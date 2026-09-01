import { writeFileSync } from "node:fs"
import { ZYRA_UI_EVENT_SCHEMA, type ZyraUiEvent } from "../../src/presentation/events.ts"
import { ProductTuiShell } from "../../src/tui/shell.ts"

const shell = new ProductTuiShell({
  stdin: process.stdin,
  output: process.stdout,
  workspace: process.cwd(),
})
const session: ZyraUiEvent = {
  schema: ZYRA_UI_EVENT_SCHEMA,
  eventId: "async-redraw-session",
  type: "session.started",
  sessionId: "async-redraw-session",
  taskId: "async-redraw-task",
}

shell.start()
const reading = shell.read(false)
let pumping = true
let eventUpdates = 0
const pump = () => {
  if (!pumping) return
  for (let burst = 0; burst < 25; burst += 1) {
    eventUpdates += 1
    shell.update([
      session,
      {
        schema: ZYRA_UI_EVENT_SCHEMA,
        eventId: `async-redraw-${eventUpdates}`,
        type: "activity.updated",
        activityId: "async-redraw-activity",
        label: "Background event pressure",
        summary: `event ${eventUpdates}`,
      },
    ])
  }
  setImmediate(pump)
}
setImmediate(pump)
shell.notice("ZYRA_ASYNC_REDRAW_READY")

const result = await reading
pumping = false
shell.close()
const resultPath = process.env.ZYRA_ASYNC_REDRAW_RESULT_PATH
if (!resultPath) throw new Error("ZYRA_ASYNC_REDRAW_RESULT_PATH is required")
writeFileSync(resultPath, JSON.stringify({
  result,
  eventUpdates,
  diagnostics: shell.renderDiagnostics,
}), "utf8")
await new Promise<void>((resolve, reject) => process.stdout.write(
  "\nZYRA_ASYNC_REDRAW_RESULT_WRITTEN\n",
  (error) => error ? reject(error) : resolve(),
))
