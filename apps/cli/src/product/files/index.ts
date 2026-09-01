import { readdir, realpath, stat } from "node:fs/promises"
import { join, relative, sep } from "node:path"
import { workspaceReferenceCandidate } from "./mention-codec.ts"

const IGNORED = new Set([".git", ".hg", ".svn", "node_modules", "target", ".venv", "venv", "dist", "build", ".next", ".cache"])
const MAX_INDEX_ENTRIES = 20_000
const MAX_INDEX_DEPTH = 64

function protectedName(name: string): boolean {
  const lowered = name.toLocaleLowerCase()
  if (lowered === ".env.example" || lowered === ".env.sample" || lowered === ".env.template") return false
  return lowered === ".env"
    || lowered.startsWith(".env.")
    || [".npmrc", ".pypirc", ".netrc", "credentials", "credentials.json", "secrets.json"].includes(lowered)
    || /(?:^|[._-])(?:id_rsa|id_ed25519|private[_-]?key)(?:[._-]|$)/u.test(lowered)
    || /\.(?:pem|p12|pfx|key)$/u.test(lowered)
}

export async function workspaceReferenceCandidates(root: string, limit = 2_000): Promise<string[]> {
  const selectedLimit = Math.max(1, Math.min(MAX_INDEX_ENTRIES, Math.floor(limit)))
  const canonicalRoot = await realpath(root)
  if (!(await stat(canonicalRoot)).isDirectory()) throw new TypeError("workspace reference root must be a directory")
  const output: string[] = []
  const queue: Array<{ path: string; depth: number }> = [{ path: canonicalRoot, depth: 0 }]
  while (queue.length && output.length < selectedLimit) {
    const current = queue.shift()!
    const entries = await readdir(current.path, { withFileTypes: true }).catch(() => [])
    entries.sort((left, right) => left.name.localeCompare(right.name))
    for (const entry of entries) {
      if (output.length >= selectedLimit || IGNORED.has(entry.name) || protectedName(entry.name) || entry.isSymbolicLink()) continue
      const absolute = join(current.path, entry.name)
      const display = relative(canonicalRoot, absolute).split(sep).join("/")
      if (!display || display.startsWith("../") || display === "..") continue
      const candidate = workspaceReferenceCandidate(`${display}${entry.isDirectory() ? "/" : ""}`)
      if (!candidate) continue
      output.push(candidate)
      if (entry.isDirectory() && current.depth < MAX_INDEX_DEPTH) queue.push({ path: absolute, depth: current.depth + 1 })
    }
  }
  return output
}
