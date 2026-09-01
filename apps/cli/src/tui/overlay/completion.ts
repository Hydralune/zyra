import type { DraftSnapshot } from "../../input/draft.ts"

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
  return { token, tokenStart: snapshot.cursor - token.length }
}

export function completionState(
  snapshot: DraftSnapshot,
  candidates: readonly string[],
  selected = 0,
): CompletionState | undefined {
  const active = activeToken(snapshot)
  if (!active) return undefined
  const query = active.token.toLocaleLowerCase()
  const matches = candidates
    .filter((candidate) => candidate.toLocaleLowerCase().startsWith(query))
    .sort((left, right) => left.localeCompare(right))
    .slice(0, 50)
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
  return {
    text: `${snapshot.text.slice(0, completion.tokenStart)}${selected}${snapshot.text.slice(snapshot.cursor)}`,
    cursor: completion.tokenStart + selected.length,
  }
}
