import { readdir } from "node:fs/promises"
import { join, relative, sep } from "node:path"

const IGNORED = new Set([".git", ".hg", ".svn", "node_modules", "target", ".venv", "venv", "dist", "build", ".next", ".cache"])

export async function workspaceReferenceCandidates(root: string, limit = 2_000): Promise<string[]> {
  const output: string[] = []
  const queue: Array<{ path: string; depth: number }> = [{ path: root, depth: 0 }]
  while (queue.length && output.length < Math.max(1, limit)) {
    const current = queue.shift()!
    const entries = await readdir(current.path, { withFileTypes: true }).catch(() => [])
    entries.sort((left, right) => left.name.localeCompare(right.name))
    for (const entry of entries) {
      if (output.length >= limit || IGNORED.has(entry.name) || entry.isSymbolicLink()) continue
      const absolute = join(current.path, entry.name)
      const display = relative(root, absolute).split(sep).join("/")
      if (!display || display.startsWith("../") || display === "..") continue
      output.push(`@${display}${entry.isDirectory() ? "/" : ""}`)
      if (entry.isDirectory() && current.depth < 5) queue.push({ path: absolute, depth: current.depth + 1 })
    }
  }
  return output
}
