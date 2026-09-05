import type { DraftSnapshot } from "../../input/draft.ts"
import { encodeSelectedWorkspacePath } from "../../product/files/mention-codec.ts"

export interface CompletionState {
  token: string
  tokenStart: number
  matches: readonly string[]
  selected: number
}

function activeToken(snapshot: DraftSnapshot): { token: string; tokenStart: number } | undefined {
  const before = snapshot.text.slice(0, snapshot.cursor)
  const match = before.match(/(?:^|\s)([/@][^\s]*)$/u)
  const token = match?.[1]
  if (!token) return undefined
  // A slash inside a normal message is a path, not a command invocation.
  if (token.startsWith("/") && before.slice(0, -token.length).trim()) return undefined
  return { token, tokenStart: snapshot.cursor - token.length }
}

function fuzzyScore(candidate: string, query: string): number | undefined {
  if (candidate.startsWith(query)) return 10_000 - candidate.length
  const basename = candidate.slice(Math.max(candidate.lastIndexOf("/"), candidate.lastIndexOf("\\")) + 1)
  if (basename.startsWith(query)) return 8_000 - candidate.length
  let queryIndex = 0
  let score = 0
  let previous = -2
  for (let index = 0; index < candidate.length && queryIndex < query.length; index += 1) {
    if (candidate[index] !== query[queryIndex]) continue
    score += index === previous + 1 ? 12 : 2
    if (index === 0 || "/\\._- ".includes(candidate[index - 1] ?? "")) score += 8
    previous = index
    queryIndex += 1
  }
  return queryIndex === query.length ? score - candidate.length : undefined
}

interface IndexedCandidate {
  candidate: string
  normalized: string
  referencePath: string
}

const candidateIndex = new WeakMap<readonly string[], readonly IndexedCandidate[]>()

function indexedCandidates(candidates: readonly string[]): readonly IndexedCandidate[] {
  const cached = candidateIndex.get(candidates)
  if (cached) return cached
  const indexed = Object.freeze(candidates.map((candidate) => Object.freeze({
    candidate,
    normalized: candidate.toLowerCase(),
    referencePath: candidate.startsWith("@") ? candidate.slice(1).toLowerCase() : "",
  })))
  candidateIndex.set(candidates, indexed)
  return indexed
}

function rankedMatches(candidates: readonly string[], token: string): string[] {
  const query = token.toLowerCase()
  if (token.startsWith("/")) {
    return indexedCandidates(candidates)
      .filter((row) => row.normalized.startsWith(query))
      .map((row) => row.candidate)
      .sort((left, right) => left.localeCompare(right))
      .slice(0, 50)
  }
  const selected: Array<{ candidate: string; score: number }> = []
  const queryPath = query.slice(1)
  for (const row of indexedCandidates(candidates)) {
    const { candidate } = row
    if (!candidate.startsWith("@")) continue
    const score = fuzzyScore(row.referencePath, queryPath)
    if (score === undefined) continue
    const ranked = { candidate, score }
    let low = 0
    let high = selected.length
    while (low < high) {
      const middle = (low + high) >>> 1
      const other = selected[middle]!
      const before = score > other.score || (score === other.score && candidate.localeCompare(other.candidate) < 0)
      if (before) high = middle
      else low = middle + 1
    }
    if (low < 50) selected.splice(low, 0, ranked)
    if (selected.length > 50) selected.pop()
  }
  return selected.map((row) => row.candidate)
}

export function completionState(
  snapshot: DraftSnapshot,
  candidates: readonly string[],
  selected = 0,
): CompletionState | undefined {
  const active = activeToken(snapshot)
  if (!active) return undefined
  const matches = rankedMatches(candidates, active.token)
  if (!matches.length) return undefined
  return Object.freeze({
    ...active,
    matches: Object.freeze(matches),
    selected: Math.max(0, Math.min(matches.length - 1, Math.floor(selected))),
  })
}

export function acceptCompletion(snapshot: DraftSnapshot, completion: CompletionState): {
  text: string
  cursor: number
} {
  const selected = completion.matches[completion.selected]
  if (!selected) return { text: snapshot.text, cursor: snapshot.cursor }
  const filePath = encodeSelectedWorkspacePath(selected)
  const inserted = filePath ?? selected
  const suffix = snapshot.text.slice(snapshot.cursor)
  const separator = filePath && !/^\s/u.test(suffix) ? " " : ""
  return {
    text: `${snapshot.text.slice(0, completion.tokenStart)}${inserted}${separator}${suffix}`,
    cursor: completion.tokenStart + inserted.length + separator.length,
  }
}
