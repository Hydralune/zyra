import { execFile } from "node:child_process"
import { readFile } from "node:fs/promises"
import { isAbsolute, relative, resolve } from "node:path"
import { promisify } from "node:util"
import type { TaskProjection } from "@zyra/typed-api-client"
import { ZYRA_UI_EVENT_SCHEMA, type ZyraUiEvent } from "./events.ts"

const executeFile = promisify(execFile)
const MAX_PATHS = 20
const MAX_LINES = 120
const MAX_LINE_CHARS = 500
const MAX_CAPTURE_BYTES = 256 * 1024
const MAX_CREATED_BYTES = 8 * 1024

function object(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Readonly<Record<string, unknown>>
    : {}
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.flatMap((item) => typeof item === "string" && item.trim() ? [item.trim()] : [])
    : []
}

function safePath(workspace: string, value: string): { relative: string; absolute: string } | undefined {
  if (!value || isAbsolute(value) || /[\u0000\r\n]/u.test(value)) return undefined
  const absolute = resolve(workspace, value)
  const selected = relative(resolve(workspace), absolute).replaceAll("\\", "/")
  if (!selected || selected === "." || selected === ".." || selected.startsWith("../")) return undefined
  return { relative: selected, absolute }
}

function boundedLines(value: string): { lines: string[]; truncated: boolean } {
  const source = value.replaceAll("\r\n", "\n").replaceAll("\r", "\n").split("\n")
  let truncated = source.length > MAX_LINES
  const lines = source.slice(0, MAX_LINES).map((line) => {
    if (line.length <= MAX_LINE_CHARS) return line
    truncated = true
    return `${line.slice(0, MAX_LINE_CHARS)}…`
  })
  while (lines.at(-1) === "") lines.pop()
  return { lines, truncated }
}

async function trackedDiff(workspace: string, paths: readonly string[]): Promise<string> {
  if (!paths.length) return ""
  try {
    const result = await executeFile("git", [
      "diff", "HEAD", "--no-color", "--no-ext-diff", "--unified=3", "--", ...paths,
    ], {
      cwd: workspace,
      encoding: "utf8",
      maxBuffer: MAX_CAPTURE_BYTES,
      timeout: 5_000,
      windowsHide: true,
    })
    return result.stdout
  } catch {
    return ""
  }
}

async function createdDiff(paths: readonly { relative: string; absolute: string }[]): Promise<string> {
  const sections: string[] = []
  for (const path of paths.slice(0, 5)) {
    try {
      const bytes = await readFile(path.absolute)
      if (bytes.includes(0)) {
        sections.push(`--- /dev/null\n+++ b/${path.relative}\n+[二进制文件]`)
        continue
      }
      const text = bytes.subarray(0, MAX_CREATED_BYTES).toString("utf8")
      const content = text.replaceAll("\r\n", "\n").replaceAll("\r", "\n")
        .split("\n").slice(0, 40).map((line) => `+${line}`).join("\n")
      const suffix = bytes.byteLength > MAX_CREATED_BYTES ? "\n+… [文件内容已截断]" : ""
      sections.push(`--- /dev/null\n+++ b/${path.relative}\n${content}${suffix}`)
    } catch {
      // A canonical delivery can describe a path that is intentionally not
      // materialized in this terminal. Web remains the complete diff owner.
    }
  }
  return sections.join("\n")
}

export async function buildBoundedWorkspaceDiff(
  task: TaskProjection,
  workspace: string,
): Promise<ZyraUiEvent | undefined> {
  const delivery = object(task.metadata.delivery)
  if (delivery.schema !== "zyra.task-workspace-delivery/v1") return undefined
  const changed = [...new Set([
    ...strings(delivery.changed_paths),
    ...strings(delivery.modified_paths),
    ...strings(delivery.deleted_paths),
  ])].flatMap((value) => {
    const selected = safePath(workspace, value)
    return selected ? [selected] : []
  }).slice(0, MAX_PATHS)
  const created = [...new Set(strings(delivery.created_paths))].flatMap((value) => {
    const selected = safePath(workspace, value)
    return selected ? [selected] : []
  }).slice(0, MAX_PATHS)
  if (!changed.length && !created.length) return undefined

  const [tracked, added] = await Promise.all([
    trackedDiff(workspace, changed.map((item) => item.relative)),
    createdDiff(created),
  ])
  const bounded = boundedLines([tracked, added].filter(Boolean).join("\n"))
  if (!bounded.lines.length) return undefined
  return Object.freeze({
    schema: ZYRA_UI_EVENT_SCHEMA,
    eventId: `ui:workspace-diff:${task.taskId}:${task.updatedAt}`,
    occurredAt: task.updatedAt,
    type: "workspace.diff",
    lines: Object.freeze(bounded.lines),
    truncated: bounded.truncated || changed.length + created.length >= MAX_PATHS,
    source: "local_workspace",
  })
}
