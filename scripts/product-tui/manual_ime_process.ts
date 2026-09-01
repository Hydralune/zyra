import { ZYRA_UI_EVENT_SCHEMA, type ZyraUiEvent } from "../../apps/cli/src/presentation/events.ts"
import { ProductTuiShell } from "../../apps/cli/src/tui/shell.ts"

if (!process.stdin.isTTY || !process.stdout.isTTY) {
  throw new Error("Run this gate in an interactive Windows Terminal or PowerShell terminal.")
}

const shell = new ProductTuiShell({
  stdin: process.stdin,
  output: process.stdout,
  workspace: process.cwd(),
})
const session: ZyraUiEvent = {
  schema: ZYRA_UI_EVENT_SCHEMA,
  eventId: "manual-ime-session",
  type: "session.started",
  sessionId: "manual-ime-session",
  taskId: "manual-ime-task",
}
let updates = 0
const timer = setInterval(() => {
  updates += 1
  shell.update([
    session,
    {
      schema: ZYRA_UI_EVENT_SCHEMA,
      eventId: `manual-ime-update-${updates}`,
      type: "activity.updated",
      activityId: "manual-ime-pressure",
      label: "异步重绘压力",
      summary: `后台更新 ${updates}`,
    },
  ])
}, 100)
timer.unref()

shell.start()
shell.notice("请启用真实中文 IME，完成候选选择后输入 emoji/组合字符，再按 Enter；期间后台会持续重绘。")
try {
  const submitted = await shell.read(false)
  if (submitted.kind !== "submit") throw new Error(`IME gate ended before submission: ${submitted.kind}`)
  shell.notice(`人工核对提交文本：${submitted.text}\n若与输入完全一致，输入 /exit；不一致请 Ctrl+C 退出并记录现象。`)
  while (true) {
    const result = await shell.read(false)
    if (result.kind === "exit" || result.kind === "closed" || result.kind === "interrupt") break
    if (result.kind === "submit" && result.text.trim() === "/exit") break
    shell.notice("请核对上一行文本；确认一致后输入 /exit。")
  }
} finally {
  clearInterval(timer)
  shell.close()
}
