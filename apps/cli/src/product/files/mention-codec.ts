const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/u

export function workspacePathIsMentionSafe(path: string): boolean {
  return path.length > 0
    && path.length <= 4_096
    && !CONTROL_CHARACTERS.test(path)
    && !path.startsWith("/")
    && !path.startsWith("\\")
    && !/^[A-Za-z]:[\\/]/u.test(path)
    && !path.split(/[\\/]/u).some((part) => part === "..")
}

export function workspaceReferenceCandidate(path: string): string | undefined {
  return workspacePathIsMentionSafe(path) ? `@${path}` : undefined
}

export function decodeWorkspaceReferenceCandidate(candidate: string): string | undefined {
  if (!candidate.startsWith("@")) return undefined
  const path = candidate.slice(1)
  return workspacePathIsMentionSafe(path) ? path : undefined
}

/**
 * Codex uses `@` to search, then replaces the active token with the selected path.
 * Keep that distinction: the sigil is UI syntax, not part of the task goal.
 */
export function encodeSelectedWorkspacePath(candidate: string): string | undefined {
  const path = decodeWorkspaceReferenceCandidate(candidate)
  if (!path) return undefined
  return /[\s"']/u.test(path) ? JSON.stringify(path) : path
}
