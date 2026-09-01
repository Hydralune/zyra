import { randomUUID } from "node:crypto"
import { copyLatestAssistantMessage } from "../../apps/cli/src/product/transcript/export.ts"
import type { ProductViewState } from "../../apps/cli/src/product/state/session-state.ts"

if (process.platform !== "win32") throw new Error("windows_clipboard_gate.ts requires Windows")
if (process.env.ZYRA_CLIPBOARD_GATE_CUSTODY !== "powershell-restore-v1") {
  throw new Error("Run windows_clipboard_gate.ps1 so the previous clipboard is restored.")
}

const marker = `Zyra clipboard ${randomUUID()} · 中文 · emoji ✅`
const view = {
  messages: Object.freeze([Object.freeze({
    messageId: "clipboard-gate",
    role: "assistant",
    text: marker,
  })]),
} as unknown as ProductViewState
const receipt = await copyLatestAssistantMessage(view)
process.stdout.write(`${JSON.stringify({ schema: "zyra.windows-clipboard-gate/v1", marker, ...receipt })}\n`)
