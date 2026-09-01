import { randomUUID } from "node:crypto"
import {
  lstat,
  mkdir,
  readFile,
  readdir,
  realpath,
  rename,
  unlink,
  writeFile,
} from "node:fs/promises"
import {
  dirname,
  isAbsolute,
  relative,
  resolve,
  sep,
} from "node:path"
import type { TaskProjection } from "@zyra/typed-api-client"
import type { CliApi } from "./api.ts"
import { CliTaskError } from "./contracts.ts"

const MAX_STAGED_FILES = 10_000
const MAX_STAGED_BYTES = 256 * 1024 * 1024
// Workspace writes use JSON/base64 and the daemon's default request ceiling is
// 2 MiB. Keep enough room for encoding and request metadata.
const MAX_STAGED_FILE_BYTES = 1_400_000
const EXCLUDED_DIRECTORIES = new Set([
  ".git",
  ".hg",
  ".svn",
  ".venv",
  ".zyra",
  "__pycache__",
  "node_modules",
  "venv",
])

export interface WorkspaceStageReport {
  workspaceId: string
  root: string
  fileCount: number
  bytes: number
  paths: string[]
}

export interface WorkspaceMaterializationReport {
  workspaceId: string
  root: string
  fileCount: number
  bytes: number
  paths: string[]
  deletedPaths: string[]
}

interface WorkspaceFile {
  path: string
  absolutePath: string
  bytes: number
}

function record(value: unknown): Readonly<Record<string, unknown>> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Readonly<Record<string, unknown>>
    : {}
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : []
}

function workspaceId(task: TaskProjection): string {
  const workspaceRef = record(task.metadata.workspace_ref)
  const delivery = record(task.metadata.delivery)
  const selected = workspaceRef.workspace_id ?? delivery.workspace_id
  if (typeof selected !== "string" || !selected.trim()) {
    throw new CliTaskError(
      "Task creation did not return an opaque workspace identity.",
      "contract_workspace_identity_missing",
    )
  }
  return selected
}

function isSensitiveName(name: string): boolean {
  const lower = name.toLowerCase()
  return lower === ".env" || lower.startsWith(".env.")
}

function assertRelativeWorkspacePath(path: string): string {
  const normalized = path.replaceAll("\\", "/").replace(/^\.\//, "")
  const parts = normalized.split("/")
  if (
    !normalized
    || normalized.startsWith("/")
    || /^[a-z]:/i.test(normalized)
    || parts.some((part) => !part || part === "." || part === "..")
  ) {
    throw new CliTaskError(
      `Workspace path is not a safe relative path: ${path}`,
      "contract_workspace_path_invalid",
    )
  }
  return normalized
}

async function collectWorkspaceFiles(root: string): Promise<WorkspaceFile[]> {
  const rootPath = await realpath(root)
  const files: WorkspaceFile[] = []
  let bytes = 0
  const visit = async (directory: string, prefix: string): Promise<void> => {
    const entries = await readdir(directory, { withFileTypes: true })
    entries.sort((left, right) => left.name.localeCompare(right.name))
    for (const entry of entries) {
      if (entry.isSymbolicLink()) continue
      if (entry.isDirectory() && EXCLUDED_DIRECTORIES.has(entry.name.toLowerCase())) continue
      if (isSensitiveName(entry.name)) continue
      const logicalPath = prefix ? `${prefix}/${entry.name}` : entry.name
      const absolutePath = resolve(directory, entry.name)
      const status = await lstat(absolutePath)
      if (status.isSymbolicLink()) continue
      if (status.isDirectory()) {
        await visit(absolutePath, logicalPath)
        continue
      }
      if (!status.isFile()) continue
      if (status.size > MAX_STAGED_FILE_BYTES) {
        throw new CliTaskError(
          `Workspace file exceeds the CLI transfer limit: ${logicalPath}`,
          "workspace_seed_file_too_large",
          { path: logicalPath, bytes: status.size, maximum_bytes: MAX_STAGED_FILE_BYTES },
        )
      }
      files.push({ path: assertRelativeWorkspacePath(logicalPath), absolutePath, bytes: status.size })
      bytes += status.size
      if (files.length > MAX_STAGED_FILES || bytes > MAX_STAGED_BYTES) {
        throw new CliTaskError(
          "Workspace exceeds the bounded CLI transfer budget.",
          "workspace_seed_budget_exceeded",
          { file_count: files.length, bytes },
        )
      }
    }
  }
  await visit(rootPath, "")
  return files
}

export async function stageWorkspace(
  api: CliApi,
  task: TaskProjection,
  root: string,
  signal: AbortSignal,
): Promise<WorkspaceStageReport> {
  const selectedWorkspaceId = workspaceId(task)
  const selectedRoot = await realpath(root)
  const files = await collectWorkspaceFiles(selectedRoot)
  let bytes = 0
  for (const file of files) {
    if (signal.aborted) throw signal.reason
    const content = await readFile(file.absolutePath)
    await api.writeWorkspaceFile(selectedWorkspaceId, file.path, content, signal)
    bytes += content.byteLength
  }
  return {
    workspaceId: selectedWorkspaceId,
    root: selectedRoot,
    fileCount: files.length,
    bytes,
    paths: files.map((file) => file.path),
  }
}

function contained(root: string, candidate: string): boolean {
  const path = relative(root, candidate)
  return path === "" || (!path.startsWith(`..${sep}`) && path !== ".." && !isAbsolute(path))
}

async function guardedDestination(root: string, logicalPath: string): Promise<string> {
  const normalized = assertRelativeWorkspacePath(logicalPath)
  const destination = resolve(root, ...normalized.split("/"))
  if (!contained(root, destination)) {
    throw new CliTaskError(
      `Workspace output escapes the CLI startup root: ${logicalPath}`,
      "workspace_output_path_escape",
    )
  }
  const parts = normalized.split("/")
  let current = root
  for (const part of parts.slice(0, -1)) {
    current = resolve(current, part)
    try {
      const status = await lstat(current)
      if (status.isSymbolicLink() || !status.isDirectory()) {
        throw new CliTaskError(
          `Workspace output parent is not a real directory: ${logicalPath}`,
          "workspace_output_parent_invalid",
        )
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error
      await mkdir(current)
    }
  }
  try {
    const status = await lstat(destination)
    if (status.isSymbolicLink() || !status.isFile()) {
      throw new CliTaskError(
        `Workspace output target is not a regular file: ${logicalPath}`,
        "workspace_output_target_invalid",
      )
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error
  }
  const parent = await realpath(dirname(destination))
  if (!contained(root, parent)) {
    throw new CliTaskError(
      `Workspace output parent resolves outside the CLI startup root: ${logicalPath}`,
      "workspace_output_parent_escape",
    )
  }
  return destination
}

async function writeLocalOutput(destination: string, content: Uint8Array): Promise<void> {
  try {
    const existing = await readFile(destination)
    if (Buffer.from(existing).equals(Buffer.from(content))) return
    // Existing regular files were already attested by guardedDestination.
    await writeFile(destination, content)
    return
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error
  }
  const temporary = `${destination}.zyra-${randomUUID()}.tmp`
  try {
    await writeFile(temporary, content, { flag: "wx" })
    await rename(temporary, destination)
  } catch (error) {
    await unlink(temporary).catch(() => undefined)
    throw error
  }
}

async function deleteLocalOutput(root: string, logicalPath: string): Promise<boolean> {
  const normalized = assertRelativeWorkspacePath(logicalPath)
  const destination = resolve(root, ...normalized.split("/"))
  if (!contained(root, destination)) {
    throw new CliTaskError(
      `Workspace deletion escapes the CLI startup root: ${logicalPath}`,
      "workspace_output_path_escape",
    )
  }
  let current = root
  for (const part of normalized.split("/").slice(0, -1)) {
    current = resolve(current, part)
    try {
      const status = await lstat(current)
      if (status.isSymbolicLink() || !status.isDirectory()) {
        throw new CliTaskError(
          `Workspace deletion parent is not a real directory: ${logicalPath}`,
          "workspace_output_parent_invalid",
        )
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return false
      throw error
    }
  }
  try {
    const status = await lstat(destination)
    if (status.isSymbolicLink() || !status.isFile()) {
      throw new CliTaskError(
        `Workspace deletion target is not a regular file: ${logicalPath}`,
        "workspace_output_target_invalid",
      )
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return false
    throw error
  }
  const parent = await realpath(dirname(destination))
  if (!contained(root, parent)) {
    throw new CliTaskError(
      `Workspace deletion parent resolves outside the CLI startup root: ${logicalPath}`,
      "workspace_output_parent_escape",
    )
  }
  await unlink(destination)
  return true
}

export async function materializeWorkspaceDelivery(
  api: CliApi,
  task: TaskProjection,
  root: string,
  signal: AbortSignal,
): Promise<WorkspaceMaterializationReport> {
  const delivery = record(task.metadata.delivery)
  if (delivery.schema !== "zyra.task-workspace-delivery/v1") {
    return {
      workspaceId: workspaceId(task),
      root: await realpath(root),
      fileCount: 0,
      bytes: 0,
      paths: [],
      deletedPaths: [],
    }
  }
  const selectedWorkspaceId = workspaceId(task)
  const selectedRoot = await realpath(root)
  const deletedPaths = [...new Set(strings(delivery.deleted_paths))]
    .map(assertRelativeWorkspacePath)
    .sort()
  const deleted = new Set(deletedPaths)
  const paths = [...new Set(strings(delivery.changed_paths))]
    .filter((path) => !deleted.has(path))
    .map(assertRelativeWorkspacePath)
    .sort()
  let bytes = 0
  for (const path of paths) {
    if (signal.aborted) throw signal.reason
    const destination = await guardedDestination(selectedRoot, path)
    const content = await api.readWorkspaceFile(selectedWorkspaceId, path, signal)
    await writeLocalOutput(destination, content)
    bytes += content.byteLength
  }
  const appliedDeletions: string[] = []
  for (const path of deletedPaths) {
    if (signal.aborted) throw signal.reason
    if (await deleteLocalOutput(selectedRoot, path)) appliedDeletions.push(path)
  }
  return {
    workspaceId: selectedWorkspaceId,
    root: selectedRoot,
    fileCount: paths.length,
    bytes,
    paths,
    deletedPaths: appliedDeletions,
  }
}
