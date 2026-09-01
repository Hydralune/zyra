import type { TaskProjection } from "@zyra/typed-api-client"
import type { CliApi } from "../../api.ts"
import type { ProductTuiShell } from "../../tui/shell.ts"
import { parseProductDiffManifest, parseProductDiffPage } from "./contracts.ts"

export function productPatchArtifacts(task: TaskProjection) {
  return task.artifacts.filter((artifact) => {
    const metadata = artifact.metadata ?? {}
    const security = metadata.security && typeof metadata.security === "object" && !Array.isArray(metadata.security)
      ? metadata.security as Record<string, unknown>
      : {}
    const status = metadata.status && typeof metadata.status === "object" && !Array.isArray(metadata.status)
      ? metadata.status as Record<string, unknown>
      : {}
    const label = String(security.label ?? metadata.security_label ?? "").toLocaleLowerCase()
    const trust = String(security.trust ?? metadata.trust ?? "").toLocaleLowerCase()
    const integrity = String(status.integrity ?? metadata.integrity ?? "").toLocaleLowerCase()
    if (label === "secret" || trust === "quarantined" || metadata.quarantined === true) return false
    if (integrity && integrity !== "verified") return false
    if (status.exists === false || status.is_file === false || status.isFile === false) return false
    const searchable = [artifact.kind, artifact.title, artifact.mediaType, artifact.path].filter(Boolean).join(" ").toLocaleLowerCase()
    return searchable.includes("patch") || searchable.includes("diff") || artifact.mediaType === "text/x-diff" || artifact.mediaType === "text/x-patch"
  })
}

export async function openProductDiff(input: {
  api: CliApi
  shell: ProductTuiShell
  task: TaskProjection
  signal?: AbortSignal
}): Promise<boolean> {
  const task = await input.api.task(input.task.taskId).catch(() => input.task)
  const artifacts = productPatchArtifacts(task)
  if (!artifacts.length) {
    const preview = input.shell.view.diff
    if (preview?.lines.length) {
      await input.shell.page("Diff 预览（canonical artifact 不可用）", preview.lines)
      return true
    }
    return false
  }
  let artifact = artifacts.length === 1 ? artifacts[0] : undefined
  if (!artifact) {
    const selected = await input.shell.pick("选择 Patch artifact", artifacts.map((item) => ({
      id: item.artifactId,
      label: item.title ?? item.path ?? item.artifactId,
      detail: `${item.kind}${item.sizeBytes ? ` · ${item.sizeBytes} bytes` : ""}`,
      keywords: [item.artifactId, item.mediaType ?? ""],
    })))
    artifact = selected ? artifacts.find((item) => item.artifactId === selected.id) : undefined
  }
  if (!artifact) return false
  const manifest = parseProductDiffManifest(await input.api.diffReviewManifest(task.taskId, artifact.artifactId, input.signal))
  const selectedFile = await input.shell.pick("选择变更文件", manifest.files.map((file) => ({
    id: file.fileId,
    label: file.previousPath ? `${file.previousPath} → ${file.path}` : file.path,
    detail: `${file.kind}${file.binary ? " · binary" : ""} · +${file.additions} -${file.deletions}`,
    keywords: [file.previousPath ?? "", file.kind],
  })))
  const file = selectedFile ? manifest.files.find((item) => item.fileId === selectedFile.id) : undefined
  if (!file) return false
  if (file.binary) {
    await input.shell.page(
      file.previousPath ? `${file.previousPath} → ${file.path}` : file.path,
      [
        "[二进制文件：终端不显示内容]",
        `变更类型：${file.kind}`,
        ...(file.previousPath ? [`重命名：${file.previousPath} → ${file.path}`] : []),
      ],
    )
    return true
  }
  const lines: string[] = [`--- ${file.previousPath ?? file.path}`, `+++ ${file.path}`]
  const pageBudget = Math.min(file.pageCount, 1_000)
  for (let pageIndex = 0; pageIndex < pageBudget && lines.length < 20_000; pageIndex += 1) {
    const page = parseProductDiffPage(await input.api.diffReviewPage({
      taskId: task.taskId,
      artifactId: artifact.artifactId,
      fileId: file.fileId,
      revision: manifest.artifactRevision,
      page: pageIndex,
      maximumBytes: 512 * 1024,
      maximumLines: 2_000,
      signal: input.signal,
    }))
    lines.push(...page.lines.slice(0, 20_000 - lines.length))
    if (pageIndex + 1 >= page.pageCount) break
  }
  if (file.truncated || file.oversized || file.pageCount > pageBudget || lines.length >= 20_000) {
    lines.push("… diff 超出终端安全预算；使用 /ui 查看完整审查。")
  }
  await input.shell.page(`${file.previousPath ? `${file.previousPath} → ` : ""}${file.path} · ${file.kind} · +${file.additions} -${file.deletions}`, lines)
  return true
}
