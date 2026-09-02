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
      await input.shell.page("文件变更预览", preview.lines)
      return true
    }
    return false
  }
  let artifact = artifacts.length === 1 ? artifacts[0] : undefined
  if (!artifact) {
    const selected = await input.shell.pick("选择变更记录", artifacts.map((item) => ({
      id: item.artifactId,
      label: item.title ?? item.path ?? item.artifactId,
      detail: `${item.kind}${item.sizeBytes ? ` · ${item.sizeBytes} 字节` : ""}`,
      keywords: [item.artifactId, item.mediaType ?? ""],
    })), "一个任务可能包含多份独立的文件变更记录")
    artifact = selected ? artifacts.find((item) => item.artifactId === selected.id) : undefined
  }
  if (!artifact) return false
  const manifest = parseProductDiffManifest(await input.api.diffReviewManifest(task.taskId, artifact.artifactId, input.signal))
  const choices = [
    ...(manifest.files.length > 1 ? [{
      id: "__all",
      label: `查看全部 ${manifest.files.length} 个文件`,
      detail: `+${manifest.files.reduce((sum, file) => sum + file.additions, 0)} -${manifest.files.reduce((sum, file) => sum + file.deletions, 0)}`,
      keywords: ["all", "全部"],
    }] : []),
    ...manifest.files.map((file) => ({
      id: file.fileId,
      label: file.previousPath ? `${file.previousPath} → ${file.path}` : file.path,
      detail: `${file.kind}${file.binary ? " · 二进制" : ""} · +${file.additions} -${file.deletions}`,
      keywords: [file.previousPath ?? "", file.kind],
    })),
  ]
  const selectedFile = await input.shell.pick("查看文件变更", choices, "选择一个文件，或合并查看本次任务的全部变更")
  if (!selectedFile) return false
  const selectedFiles = selectedFile.id === "__all"
    ? manifest.files
    : manifest.files.filter((item) => item.fileId === selectedFile.id)
  if (!selectedFiles.length) return false

  const lines: string[] = []
  for (const file of selectedFiles) {
    const prior = file.previousPath ?? file.path
    if (selectedFiles.length > 1) lines.push(`diff --git a/${prior} b/${file.path}`)
    lines.push(`--- ${file.kind === "added" ? "/dev/null" : `a/${prior}`}`)
    lines.push(`+++ ${file.kind === "deleted" ? "/dev/null" : `b/${file.path}`}`)
    if (file.binary) {
      lines.push("[二进制文件：终端不显示内容]", "")
      continue
    }
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
      lines.push("… 变更内容超出终端安全上限；使用 /ui 查看完整记录。")
    }
    if (selectedFiles.length > 1) lines.push("")
    if (lines.length >= 20_000) break
  }
  const title = selectedFiles.length > 1
    ? `${selectedFiles.length} 个文件 · +${selectedFiles.reduce((sum, file) => sum + file.additions, 0)} -${selectedFiles.reduce((sum, file) => sum + file.deletions, 0)}`
    : `${selectedFiles[0]!.previousPath ? `${selectedFiles[0]!.previousPath} → ` : ""}${selectedFiles[0]!.path} · +${selectedFiles[0]!.additions} -${selectedFiles[0]!.deletions}`
  await input.shell.page(title, lines)
  return true
}
