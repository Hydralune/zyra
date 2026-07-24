import {
  BrowserStepPhase,
  defaultBrowserViewerFilters,
  type BrowserAction,
  type BrowserSessionProjection,
  type BrowserStep,
  type BrowserViewerFilters,
  type BrowserViewerProjection,
  type BrowserViewerSelection,
} from "../contracts.ts"

export interface BrowserHistoryMatch {
  stepId: string
  actionId?: string
  score: number
  fields: readonly string[]
  preview: string
}

export interface BrowserHistoryResult {
  steps: readonly BrowserStep[]
  matches: readonly BrowserHistoryMatch[]
  actionNames: readonly string[]
  statuses: readonly string[]
  hiddenCount: number
}

function normalizeQuery(value: string): string {
  return value
    .normalize("NFKC")
    .trim()
    .toLocaleLowerCase()
    .replace(/\s+/g, " ")
    .slice(0, 4_096)
}

function queryTokens(value: string): readonly string[] {
  const normalized = normalizeQuery(value)
  if (!normalized) return Object.freeze([])
  return Object.freeze(
    [...new Set(normalized.split(" ").filter(Boolean).slice(0, 64))],
  )
}

function selectedSession(
  projection: BrowserViewerProjection,
  selection: BrowserViewerSelection,
): BrowserSessionProjection | undefined {
  const id =
    selection.sessionId
    ?? projection.selectedSessionId
    ?? projection.activeSessionId
  return projection.sessions.find(
    (session) => session.scope.browserSessionId === id,
  ) ?? projection.sessions.at(-1)
}

function actionMap(
  session: BrowserSessionProjection,
): Map<string, BrowserAction> {
  return new Map(session.actions.map((action) => [action.actionId, action]))
}

function stepSearchFields(
  session: BrowserSessionProjection,
  step: BrowserStep,
  actions: ReadonlyMap<string, BrowserAction>,
): Readonly<Record<string, string>> {
  const stepActions = step.actionIds
    .map((actionId) => actions.get(actionId))
    .filter((value): value is BrowserAction => Boolean(value))
  const results = step.resultIds
    .map((resultId) => session.results.find((result) => result.resultId === resultId))
    .filter((value): value is BrowserSessionProjection["results"][number] => Boolean(value))
  const downloads = step.downloadArtifactIds
    .map((artifactId) =>
      session.downloads.find((download) => download.artifactId === artifactId),
    )
    .filter((value): value is BrowserSessionProjection["downloads"][number] => Boolean(value))
  const target = step.targetId
    ? session.targets.find((candidate) => candidate.targetId === step.targetId)
    : undefined
  return Object.freeze({
    id: step.stepId,
    status: step.status,
    url: step.url ?? target?.url ?? "",
    title: step.title ?? target?.title ?? "",
    action: stepActions.map((action) => action.name).join(" "),
    arguments: stepActions
      .map((action) => JSON.stringify(action.arguments))
      .join(" "),
    result: results
      .map((result) =>
        [
          result.summary,
          result.extractedContent,
          result.errorCode,
          result.errorMessage,
        ].filter(Boolean).join(" "),
      )
      .join(" "),
    download: downloads
      .map((download) =>
        [download.filename, download.mediaType, download.url].filter(Boolean).join(" "),
      )
      .join(" "),
    error: [step.errorCode, step.errorMessage].filter(Boolean).join(" "),
  })
}

function scoreFields(
  fields: Readonly<Record<string, string>>,
  tokens: readonly string[],
): { score: number; fields: string[]; preview: string } {
  if (!tokens.length) return { score: 1, fields: [], preview: "" }
  let score = 0
  const matches: string[] = []
  let preview = ""
  const weights: Readonly<Record<string, number>> = {
    id: 8,
    status: 6,
    url: 7,
    title: 7,
    action: 10,
    arguments: 5,
    result: 4,
    download: 6,
    error: 9,
  }
  for (const [field, raw] of Object.entries(fields)) {
    const normalized = normalizeQuery(raw)
    if (!normalized) continue
    let fieldHits = 0
    for (const token of tokens) {
      const index = normalized.indexOf(token)
      if (index < 0) continue
      fieldHits += 1
      score += weights[field] ?? 1
      if (index === 0) score += 2
      if (!preview) {
        const start = Math.max(0, index - 48)
        const end = Math.min(raw.length, index + token.length + 96)
        preview = `${start > 0 ? "…" : ""}${raw.slice(start, end)}${
          end < raw.length ? "…" : ""
        }`
      }
    }
    if (fieldHits === tokens.length) score += (weights[field] ?? 1) * 2
    if (fieldHits > 0) matches.push(field)
  }
  if (matches.length > 1) score += matches.length
  return { score, fields: matches, preview }
}

function passesStaticFilters(
  session: BrowserSessionProjection,
  step: BrowserStep,
  filters: BrowserViewerFilters,
  actions: ReadonlyMap<string, BrowserAction>,
): boolean {
  if (filters.statuses.length && !filters.statuses.includes(step.status)) {
    return false
  }
  if (
    filters.fromSequence !== undefined
    && step.endSequence < filters.fromSequence
  ) return false
  if (
    filters.toSequence !== undefined
    && step.startSequence > filters.toSequence
  ) return false
  if (
    filters.failuresOnly
    && step.status !== BrowserStepPhase.FAILED
    && !step.errorCode
  ) return false
  if (filters.downloadsOnly && step.downloadArtifactIds.length === 0) return false
  if (
    filters.popupsOnly
    && !session.targets.some(
      (target) =>
        target.kind === "popup"
        && (
          target.targetId === step.targetId
          || target.createdSequence >= step.startSequence
          && target.createdSequence <= step.endSequence
        ),
    )
  ) return false
  if (filters.actionNames.length) {
    const names = new Set(
      step.actionIds
        .map((actionId) => actions.get(actionId)?.name)
        .filter((value): value is string => Boolean(value)),
    )
    if (!filters.actionNames.some((name) => names.has(name))) return false
  }
  return true
}

export function filterBrowserHistory(
  projection: BrowserViewerProjection,
  selection: BrowserViewerSelection,
  filters: BrowserViewerFilters = defaultBrowserViewerFilters(),
): BrowserHistoryResult {
  const session = selectedSession(projection, selection)
  if (!session) {
    return Object.freeze({
      steps: Object.freeze([]),
      matches: Object.freeze([]),
      actionNames: Object.freeze([]),
      statuses: Object.freeze([]),
      hiddenCount: 0,
    })
  }
  const actions = actionMap(session)
  const tokens = queryTokens(filters.query)
  const matches: BrowserHistoryMatch[] = []
  const visible: BrowserStep[] = []
  for (const step of session.steps) {
    if (!passesStaticFilters(session, step, filters, actions)) continue
    const fields = stepSearchFields(session, step, actions)
    const scored = scoreFields(fields, tokens)
    if (tokens.length && scored.score === 0) continue
    visible.push(step)
    if (tokens.length) {
      matches.push(
        Object.freeze({
          stepId: step.stepId,
          actionId: step.actionIds[0],
          score: scored.score,
          fields: Object.freeze(scored.fields),
          preview: scored.preview,
        }),
      )
    }
  }
  if (tokens.length) {
    const matchByStep = new Map(matches.map((match) => [match.stepId, match]))
    visible.sort(
      (left, right) =>
        (matchByStep.get(right.stepId)?.score ?? 0)
        - (matchByStep.get(left.stepId)?.score ?? 0)
        || right.endSequence - left.endSequence,
    )
    matches.sort(
      (left, right) =>
        right.score - left.score
        || left.stepId.localeCompare(right.stepId),
    )
  } else {
    visible.sort(
      (left, right) =>
        left.startSequence - right.startSequence
        || left.stepId.localeCompare(right.stepId),
    )
  }
  return Object.freeze({
    steps: Object.freeze(visible),
    matches: Object.freeze(matches),
    actionNames: Object.freeze([
      ...new Set(session.actions.map((action) => action.name)),
    ].sort()),
    statuses: Object.freeze([
      ...new Set(session.steps.map((step) => step.status)),
    ].sort()),
    hiddenCount: Math.max(0, session.steps.length - visible.length),
  })
}

export class BrowserHistoryNavigator {
  #projection: BrowserViewerProjection
  #selection: BrowserViewerSelection
  #filters: BrowserViewerFilters
  #result: BrowserHistoryResult

  constructor(
    projection: BrowserViewerProjection,
    selection: BrowserViewerSelection = { followLive: true },
    filters: BrowserViewerFilters = defaultBrowserViewerFilters(),
  ) {
    this.#projection = projection
    this.#selection = Object.freeze({ ...selection })
    this.#filters = Object.freeze({ ...filters })
    this.#result = filterBrowserHistory(
      this.#projection,
      this.#selection,
      this.#filters,
    )
    this.#reconcile()
  }

  get projection(): BrowserViewerProjection {
    return this.#projection
  }

  get selection(): BrowserViewerSelection {
    return this.#selection
  }

  get filters(): BrowserViewerFilters {
    return this.#filters
  }

  get result(): BrowserHistoryResult {
    return this.#result
  }

  update(projection: BrowserViewerProjection): BrowserViewerSelection {
    this.#projection = projection
    this.#result = filterBrowserHistory(
      this.#projection,
      this.#selection,
      this.#filters,
    )
    this.#reconcile()
    return this.#selection
  }

  setFilters(
    patch: Partial<BrowserViewerFilters>,
  ): BrowserHistoryResult {
    this.#filters = Object.freeze({
      ...this.#filters,
      ...patch,
      query:
        patch.query === undefined
          ? this.#filters.query
          : normalizeQuery(patch.query),
      statuses: Object.freeze([
        ...new Set(patch.statuses ?? this.#filters.statuses),
      ]),
      actionNames: Object.freeze([
        ...new Set(patch.actionNames ?? this.#filters.actionNames),
      ]),
    })
    this.#result = filterBrowserHistory(
      this.#projection,
      this.#selection,
      this.#filters,
    )
    this.#reconcile()
    return this.#result
  }

  clearFilters(): BrowserHistoryResult {
    this.#filters = defaultBrowserViewerFilters()
    this.#result = filterBrowserHistory(
      this.#projection,
      this.#selection,
      this.#filters,
    )
    this.#reconcile()
    return this.#result
  }

  selectSession(sessionId: string, followLive = true): BrowserViewerSelection {
    const session = this.#projection.sessions.find(
      (candidate) => candidate.scope.browserSessionId === sessionId,
    )
    if (!session) throw new TypeError(`Unknown browser session ${sessionId}.`)
    const step = followLive ? session.steps.at(-1) : session.steps[0]
    const action = step
      ? session.actions.find((candidate) =>
          step.actionIds.includes(candidate.actionId),
        )
      : undefined
    this.#selection = Object.freeze({
      sessionId,
      stepId: step?.stepId,
      actionId: action?.actionId,
      targetId: step?.targetId ?? session.activeTargetId,
      followLive,
    })
    this.#result = filterBrowserHistory(
      this.#projection,
      this.#selection,
      this.#filters,
    )
    return this.#selection
  }

  selectStep(stepId: string, actionId?: string): BrowserViewerSelection {
    const session = selectedSession(this.#projection, this.#selection)
    const step = session?.steps.find((candidate) => candidate.stepId === stepId)
    if (!session || !step) throw new TypeError(`Unknown browser step ${stepId}.`)
    const action =
      (actionId
        ? session.actions.find((candidate) => candidate.actionId === actionId)
        : undefined)
      ?? session.actions.find((candidate) =>
        step.actionIds.includes(candidate.actionId),
      )
    this.#selection = Object.freeze({
      ...this.#selection,
      sessionId: session.scope.browserSessionId,
      stepId,
      actionId: action?.actionId,
      targetId: step.targetId ?? this.#selection.targetId,
      artifactId:
        step.screenshotArtifactIds.at(-1)
        ?? step.downloadArtifactIds.at(-1)
        ?? this.#selection.artifactId,
      followLive: false,
    })
    return this.#selection
  }

  selectAction(actionId: string): BrowserViewerSelection {
    const session = selectedSession(this.#projection, this.#selection)
    const action = session?.actions.find(
      (candidate) => candidate.actionId === actionId,
    )
    if (!session || !action) {
      throw new TypeError(`Unknown browser action ${actionId}.`)
    }
    return this.selectStep(action.stepId, action.actionId)
  }

  selectArtifact(artifactId: string): BrowserViewerSelection {
    const session = selectedSession(this.#projection, this.#selection)
    if (!session) throw new TypeError("No browser session is selected.")
    const step = session.steps.find(
      (candidate) =>
        candidate.screenshotArtifactIds.includes(artifactId)
        || candidate.downloadArtifactIds.includes(artifactId),
    )
    this.#selection = Object.freeze({
      ...this.#selection,
      stepId: step?.stepId ?? this.#selection.stepId,
      artifactId,
      followLive: false,
    })
    return this.#selection
  }

  next(): BrowserViewerSelection {
    return this.#move(1)
  }

  previous(): BrowserViewerSelection {
    return this.#move(-1)
  }

  first(): BrowserViewerSelection {
    const step = this.#result.steps[0]
    return step ? this.selectStep(step.stepId) : this.#selection
  }

  last(followLive = false): BrowserViewerSelection {
    const step = this.#result.steps.at(-1)
    if (!step) return this.#selection
    const selection = this.selectStep(step.stepId)
    if (!followLive) return selection
    this.#selection = Object.freeze({ ...selection, followLive: true })
    return this.#selection
  }

  followLive(enabled = true): BrowserViewerSelection {
    if (!enabled) {
      this.#selection = Object.freeze({
        ...this.#selection,
        followLive: false,
      })
      return this.#selection
    }
    return this.last(true)
  }

  selectedStep(): BrowserStep | undefined {
    const session = selectedSession(this.#projection, this.#selection)
    return session?.steps.find(
      (step) => step.stepId === this.#selection.stepId,
    )
  }

  selectedAction(): BrowserAction | undefined {
    const session = selectedSession(this.#projection, this.#selection)
    return session?.actions.find(
      (action) => action.actionId === this.#selection.actionId,
    )
  }

  #move(delta: -1 | 1): BrowserViewerSelection {
    if (!this.#result.steps.length) return this.#selection
    const current = this.#result.steps.findIndex(
      (step) => step.stepId === this.#selection.stepId,
    )
    const index =
      current < 0
        ? delta > 0
          ? 0
          : this.#result.steps.length - 1
        : Math.min(
            this.#result.steps.length - 1,
            Math.max(0, current + delta),
          )
    const step = this.#result.steps[index]!
    return this.selectStep(step.stepId)
  }

  #reconcile(): void {
    const session = selectedSession(this.#projection, this.#selection)
    if (!session) {
      this.#selection = Object.freeze({ followLive: true })
      return
    }
    if (this.#selection.followLive) {
      const last = this.#result.steps.at(-1) ?? session.steps.at(-1)
      const action = last
        ? session.actions
          .filter((candidate) => last.actionIds.includes(candidate.actionId))
          .at(-1)
        : undefined
      this.#selection = Object.freeze({
        ...this.#selection,
        sessionId: session.scope.browserSessionId,
        stepId: last?.stepId,
        actionId: action?.actionId,
        targetId: last?.targetId ?? session.activeTargetId,
        artifactId:
          last?.screenshotArtifactIds.at(-1)
          ?? last?.downloadArtifactIds.at(-1),
        followLive: true,
      })
      return
    }
    const selectedExists = session.steps.some(
      (step) => step.stepId === this.#selection.stepId,
    )
    if (!selectedExists) {
      const nearest = this.#result.steps.at(-1) ?? session.steps.at(-1)
      this.#selection = Object.freeze({
        ...this.#selection,
        sessionId: session.scope.browserSessionId,
        stepId: nearest?.stepId,
        actionId: nearest?.actionIds[0],
        targetId: nearest?.targetId ?? session.activeTargetId,
      })
    }
  }
}
