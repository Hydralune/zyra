import { normalizeIdentity } from "@zyra/typed-api-client"
import type { CliApi } from "../../api.ts"
import { CliTaskError } from "../../contracts.ts"
import type { ProductTuiShell } from "../../tui/shell.ts"

const ARTIFACT_READ_SCHEMA = "zyra.artifact-read.v2"
const MAX_PREVIEW_BYTES = 64 * 1_024

function object(value: unknown, label: string): Readonly<Record<string, unknown>> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new CliTaskError(`${label} is not an object.`, "artifact_contract_invalid")
  }
  return value as Readonly<Record<string, unknown>>
}

function integer(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) {
    throw new CliTaskError(`${label} is invalid.`, "artifact_contract_invalid")
  }
  return Number(value)
}

export interface ProductArtifactPreview {
  artifactId: string
  title: string
  lines: readonly string[]
  complete: boolean
  totalBytes: number
  redacted: boolean
  quarantined: boolean
}

export function parseProductArtifactPreview(
  value: Readonly<Record<string, unknown>>,
  taskId: string,
  artifactId: string,
): ProductArtifactPreview {
  if (value.schema !== ARTIFACT_READ_SCHEMA || value.task_id !== taskId) {
    throw new CliTaskError("Artifact preview schema or task binding is invalid.", "artifact_contract_binding_invalid")
  }
  const artifact = object(value.artifact, "artifact")
  if (artifact.artifact_id !== artifactId) {
    throw new CliTaskError("Artifact preview identity does not match the request.", "artifact_contract_binding_invalid")
  }
  const range = object(value.range, "artifact range")
  const length = integer(range.length, "artifact range length")
  const totalBytes = integer(range.total_bytes, "artifact total bytes")
  if (length > MAX_PREVIEW_BYTES || totalBytes < length) {
    throw new CliTaskError("Artifact preview exceeds the product byte budget.", "artifact_contract_budget_invalid")
  }
  const content = object(value.content, "artifact content")
  const quarantined = content.quarantined === true
  const redacted = content.server_redacted === true
  const mediaType = typeof artifact.media_type === "string" ? artifact.media_type : "application/octet-stream"
  const title = typeof artifact.title === "string" && artifact.title.trim()
    ? artifact.title.trim().slice(0, 256)
    : artifactId
  const lines: string[] = [
    `artifact ${artifactId}`,
    `media ${mediaType} · ${totalBytes} bytes · ${range.complete === true ? "完整" : "范围预览"}`,
  ]
  if (redacted) lines.push("服务器已从预览中脱敏敏感内容。")
  if (quarantined) {
    lines.push("内容因信任策略或 prompt injection 检测已隔离；TUI 不内联显示，请在 Web 审计视图中检查。")
  } else if (typeof content.text === "string") {
    if (Buffer.byteLength(content.text, "utf8") > MAX_PREVIEW_BYTES) {
      throw new CliTaskError("Artifact text exceeds the admitted range.", "artifact_contract_budget_invalid")
    }
    lines.push("", ...content.text.split(/\r?\n/u))
  } else if (typeof content.base64 === "string") {
    lines.push("二进制内容不在 TUI 内联；使用 /ui 查看或下载受策略保护的 artifact。")
  } else {
    throw new CliTaskError("Artifact content omitted both text and binary payload.", "artifact_contract_invalid")
  }
  if (range.complete !== true) lines.push("", `仅显示前 ${length} bytes；完整内容保留在 canonical artifact。`)
  return Object.freeze({
    artifactId,
    title,
    lines: Object.freeze(lines.slice(0, 20_000)),
    complete: range.complete === true,
    totalBytes,
    redacted,
    quarantined,
  })
}

export async function openProductArtifact(input: {
  api: CliApi
  shell: ProductTuiShell
  taskId: string
  artifactId: string
  signal?: AbortSignal
}): Promise<void> {
  const taskId = normalizeIdentity("task", input.taskId)
  const artifactId = normalizeIdentity("artifact", input.artifactId)
  const raw = await input.api.artifactContent({ taskId, artifactId, length: MAX_PREVIEW_BYTES, signal: input.signal })
  const preview = parseProductArtifactPreview(raw, taskId, artifactId)
  await input.shell.page(preview.title, preview.lines)
}
