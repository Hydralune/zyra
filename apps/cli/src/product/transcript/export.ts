import { spawn } from "node:child_process"
import { lstat, open, realpath } from "node:fs/promises"
import { basename, dirname, isAbsolute, join, relative, resolve } from "node:path"
import type { ProductMessageState, ProductViewState } from "../state/session-state.ts"
import { CliTaskError } from "../../contracts.ts"

const MAX_CLIPBOARD_BYTES = 4 * 1024 * 1024
const MAX_RAW_BYTES = 8 * 1024 * 1024
const MAX_EXPORT_BYTES = 256 * 1024 * 1024

export interface TranscriptExportResult {
  path: string
  messageCount: number
  byteCount: number
  truncated: boolean
}

export interface ClipboardResult {
  byteCount: number
  messageId: string
}

type ClipboardWriter = (text: string) => Promise<void>

function within(root: string, candidate: string): boolean {
  const selected = relative(root, candidate)
  return selected === "" || (!selected.startsWith("..") && !isAbsolute(selected))
}

function byteLength(value: string): number {
  return Buffer.byteLength(value, "utf8")
}

function boundedUtf8(value: string, maximumBytes: number): { text: string; truncated: boolean } {
  const encoded = Buffer.from(value, "utf8")
  if (encoded.byteLength <= maximumBytes) return { text: value, truncated: false }
  let end = maximumBytes
  while (end > 0 && (encoded[end] ?? 0) >= 0x80 && (encoded[end] ?? 0) < 0xc0) end -= 1
  return { text: encoded.subarray(0, end).toString("utf8"), truncated: true }
}

function exportName(now = new Date()): string {
  return `zyra-transcript-${now.toISOString().replace(/[:.]/gu, "-")}.md`
}

async function exportPath(workspace: string, requested?: string): Promise<string> {
  const root = await realpath(workspace)
  const target = resolve(requested?.trim()
    ? (isAbsolute(requested) ? requested : join(root, requested))
    : join(root, exportName()))
  if (!within(root, target) || basename(target) === "") {
    throw new CliTaskError("导出路径必须位于当前 workspace 内。", "transcript_export_path_outside_workspace")
  }
  const parent = await realpath(dirname(target)).catch(() => undefined)
  if (!parent || !within(root, parent)) {
    throw new CliTaskError("导出目录不存在或越过 workspace 边界。", "transcript_export_parent_invalid")
  }
  try {
    const existing = await lstat(target)
    throw new CliTaskError(
      existing.isSymbolicLink() ? "拒绝写入符号链接导出目标。" : "导出目标已存在；不会覆盖现有文件。",
      existing.isSymbolicLink() ? "transcript_export_symlink_forbidden" : "transcript_export_exists",
    )
  } catch (error) {
    if (error instanceof CliTaskError) throw error
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error
  }
  return target
}

function transcriptHeader(view: ProductViewState): string {
  const evicted = view.evicted.messages
  return [
    "# Zyra transcript",
    "",
    `- Session: ${view.sessionId ?? "unbound"}`,
    `- Task: ${view.taskId ?? "unbound"}`,
    `- Status: ${view.taskStatus}`,
    `- Exported: ${new Date().toISOString()}`,
    `- Retained messages: ${view.messages.length}`,
    `- Evicted messages: ${evicted}`,
    "",
    evicted ? "> Earlier messages were evicted from the bounded local product view and are not included." : "",
    evicted ? "" : "",
  ].join("\n")
}

function messageBlock(message: ProductMessageState): string {
  const role = message.role === "assistant" ? "Assistant" : "User"
  const flags = [message.streaming ? "streaming" : "", message.truncated ? "source truncated" : ""].filter(Boolean)
  return `## ${role}${flags.length ? ` (${flags.join(", ")})` : ""}\n\n${message.text}\n\n`
}

export function rawTranscriptLines(view: ProductViewState): readonly string[] {
  const lines: string[] = []
  let remaining = MAX_RAW_BYTES
  if (view.evicted.messages) {
    const marker = `[${view.evicted.messages} earlier messages evicted from local view]`
    lines.push(marker, "")
    remaining -= byteLength(`${marker}\n\n`)
  }
  for (const message of view.messages) {
    const prefix = message.role === "assistant" ? "assistant> " : "user> "
    const block = `${prefix}${message.text.replaceAll("\r\n", "\n")}\n`
    const bounded = boundedUtf8(block, Math.max(0, remaining))
    lines.push(...bounded.text.split("\n"))
    remaining -= byteLength(bounded.text)
    if (bounded.truncated || remaining <= 0) {
      lines.push("…[raw transcript reached the 8 MiB local display limit; use /export or /ui]")
      break
    }
  }
  if (!view.messages.length) lines.push("当前 transcript 为空。")
  return Object.freeze(lines)
}

export async function exportProductTranscript(input: {
  view: ProductViewState
  workspace: string
  requestedPath?: string
}): Promise<TranscriptExportResult> {
  const selected = await exportPath(input.workspace, input.requestedPath)
  const handle = await open(selected, "wx", 0o600)
  let byteCount = 0
  let messageCount = 0
  let truncated = false
  try {
    for (const block of [transcriptHeader(input.view), ...input.view.messages.map(messageBlock)]) {
      const remaining = MAX_EXPORT_BYTES - byteCount
      if (remaining <= 0) {
        truncated = true
        break
      }
      const bounded = boundedUtf8(block, remaining)
      await handle.write(bounded.text)
      byteCount += byteLength(bounded.text)
      if (block.startsWith("## ")) messageCount += 1
      if (bounded.truncated) {
        truncated = true
        break
      }
    }
    if (truncated) {
      const marker = "\n\n> Export stopped at the 256 MiB safety limit. Use canonical artifacts or the Web dashboard for omitted content.\n"
      if (byteCount + byteLength(marker) <= MAX_EXPORT_BYTES) {
        await handle.write(marker)
        byteCount += byteLength(marker)
      }
    }
    await handle.sync()
  } catch (error) {
    await handle.close().catch(() => undefined)
    throw error
  }
  await handle.close()
  return Object.freeze({ path: selected, messageCount, byteCount, truncated })
}

function clipboardCommand(): { command: string; args: readonly string[] } {
  if (process.platform === "win32") return { command: "clip.exe", args: [] }
  if (process.platform === "darwin") return { command: "pbcopy", args: [] }
  return { command: "xclip", args: ["-selection", "clipboard"] }
}

async function systemClipboardWriter(text: string): Promise<void> {
  const selected = clipboardCommand()
  await new Promise<void>((resolvePromise, reject) => {
    const child = spawn(selected.command, selected.args, { stdio: ["pipe", "ignore", "ignore"], windowsHide: true })
    child.once("error", (error) => reject(new CliTaskError(
      `系统剪贴板不可用：${error.message}`,
      "clipboard_unavailable",
    )))
    child.once("close", (code) => {
      if (code === 0) resolvePromise()
      else reject(new CliTaskError(
        `系统剪贴板命令失败（exit ${code ?? "unknown"}）。`,
        "clipboard_write_failed",
      ))
    })
    child.stdin?.end(text, "utf8")
  })
}

export async function copyLatestAssistantMessage(
  view: ProductViewState,
  writer: ClipboardWriter = systemClipboardWriter,
): Promise<ClipboardResult> {
  const message = [...view.messages].reverse().find((item) => item.role === "assistant" && item.text)
  if (!message) throw new CliTaskError("当前 transcript 中没有可复制的助手回答。", "clipboard_message_missing")
  const size = byteLength(message.text)
  if (size > MAX_CLIPBOARD_BYTES) {
    throw new CliTaskError(
      "最近助手回答超过 4 MiB 剪贴板安全上限；请使用 /export 或 /artifact。",
      "clipboard_message_too_large",
      { message_id: message.messageId, byte_count: size },
    )
  }
  await writer(message.text)
  return Object.freeze({ byteCount: size, messageId: message.messageId })
}
