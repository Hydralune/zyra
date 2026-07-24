import {
  booleanValue,
  firstDefined,
  integerValue,
  isRecord,
  optionalRecord,
  optionalString,
  recordArray,
  safeObservedUrl,
  stableDigest,
  type BrowserFrame,
  type BrowserScope,
  type BrowserTarget,
} from "../contracts.ts"
import {
  deepString,
  deepStrings,
  firstDeep,
  valuesDeep,
  type BrowserRecord,
} from "./reader.ts"

interface TargetDraft {
  targetId: string
  browserSessionId: string
  url: string
  title: string
  kind: BrowserTarget["kind"]
  parentTargetId?: string
  openerTargetId?: string
  frameId?: string
  parentFrameId?: string
  active: boolean
  attached: boolean
  crashed: boolean
  createdSequence: number
  updatedSequence: number
  sourceRecordIds: Set<string>
  findings: Set<string>
}

interface FrameDraft {
  frameId: string
  targetId?: string
  parentFrameId?: string
  browserSessionId: string
  url: string
  name?: string
  securityOrigin?: string
  mimeType?: string
  main: boolean
  attached: boolean
  sequence: number
  sourceRecordIds: Set<string>
}

export interface BrowserTargetProjection {
  targets: readonly BrowserTarget[]
  frames: readonly BrowserFrame[]
  activeTargetId?: string
  url?: string
  title?: string
  findings: readonly string[]
}

function targetKind(
  source: Readonly<Record<string, unknown>>,
  parentTargetId?: string,
  frameId?: string,
): BrowserTarget["kind"] {
  const raw = String(
    firstDefined(source, "type", "target_type", "kind", "targetKind") ?? "",
  ).toLowerCase()
  if (raw.includes("popup")) return "popup"
  if (raw.includes("frame") || raw.includes("iframe")) return "frame"
  if (raw.includes("worker")) return "worker"
  if (raw.includes("page") || raw.includes("tab")) {
    return parentTargetId ? "popup" : "page"
  }
  if (frameId) return "frame"
  if (parentTargetId) return "popup"
  return "unknown"
}

function targetCandidates(record: BrowserRecord): readonly Readonly<Record<string, unknown>>[] {
  const output: Readonly<Record<string, unknown>>[] = []
  const seen = new Set<string>()
  const accept = (candidate: unknown) => {
    if (!isRecord(candidate)) return
    const targetId = optionalString(
      firstDefined(
        candidate,
        "target_id",
        "targetId",
        "tab_id",
        "tabId",
      ),
      "$.target_id",
      512,
    )
    const url = optionalString(
      firstDefined(candidate, "url", "target_url", "page_url"),
      "$.url",
      16 * 1024,
    )
    if (!targetId && !url) return
    const key = targetId ?? `url:${url}`
    if (seen.has(key)) return
    seen.add(key)
    output.push(candidate)
  }
  accept(record.payload.target)
  accept(record.payload.tab)
  accept(record.payload.page)
  accept(record.payload.browser_state)
  accept(record.payload.browserState)
  for (const key of ["targets", "tabs", "pages", "target_infos", "targetInfos"]) {
    for (const candidate of recordArray(record.payload[key], 1_000)) accept(candidate)
  }
  for (const candidate of valuesDeep(
    record.payload,
    ["target", "tab", "page"],
    6,
    1_000,
  )) accept(candidate)
  return Object.freeze(output)
}

function frameCandidates(record: BrowserRecord): readonly Readonly<Record<string, unknown>>[] {
  const output: Readonly<Record<string, unknown>>[] = []
  const seen = new Set<string>()
  const accept = (candidate: unknown) => {
    if (!isRecord(candidate)) return
    const frameId = optionalString(
      firstDefined(candidate, "frame_id", "frameId", "id"),
      "$.frame_id",
      512,
    )
    if (!frameId || seen.has(frameId)) return
    const marker = String(
      firstDefined(candidate, "type", "kind", "node_type") ?? "",
    ).toLowerCase()
    if (
      !candidate.frame_id
      && !candidate.frameId
      && !marker.includes("frame")
      && !candidate.parent_frame_id
      && !candidate.parentFrameId
    ) return
    seen.add(frameId)
    output.push(candidate)
  }
  accept(record.payload.frame)
  accept(record.payload.frame_tree)
  accept(record.payload.frameTree)
  for (const key of ["frames", "frame_tree", "frameTree"]) {
    for (const candidate of recordArray(record.payload[key], 5_000)) {
      accept(candidate)
      const childFrames = recordArray(
        firstDefined(candidate, "child_frames", "childFrames", "children"),
        5_000,
      )
      for (const child of childFrames) accept(child)
    }
  }
  for (const candidate of valuesDeep(
    record.payload,
    ["frame", "frames"],
    8,
    5_000,
  )) {
    if (Array.isArray(candidate)) {
      for (const child of candidate) accept(child)
    } else {
      accept(candidate)
    }
  }
  return Object.freeze(output)
}

function targetIdFor(
  candidate: Readonly<Record<string, unknown>>,
  record: BrowserRecord,
  index: number,
): string {
  return (
    optionalString(
      firstDefined(
        candidate,
        "target_id",
        "targetId",
        "tab_id",
        "tabId",
        "id",
      ),
      "$.target_id",
      512,
    )
    ?? `target:${stableDigest({
      session: record.scope.browserSessionId,
      url: candidate.url,
      sequence: record.sequence,
      index,
    })}`
  )
}

function updateTarget(
  drafts: Map<string, TargetDraft>,
  candidate: Readonly<Record<string, unknown>>,
  record: BrowserRecord,
  index: number,
): void {
  const targetId = targetIdFor(candidate, record, index)
  const parentTargetId = optionalString(
    firstDefined(
      candidate,
      "parent_target_id",
      "parentTargetId",
      "parent_tab_id",
      "parentTabId",
    ),
    "$.parent_target_id",
    512,
  )
  const openerTargetId = optionalString(
    firstDefined(
      candidate,
      "opener_target_id",
      "openerTargetId",
      "opener_id",
    ),
    "$.opener_target_id",
    512,
  )
  const frameId = optionalString(
    firstDefined(candidate, "frame_id", "frameId"),
    "$.frame_id",
    512,
  )
  const parentFrameId = optionalString(
    firstDefined(candidate, "parent_frame_id", "parentFrameId"),
    "$.parent_frame_id",
    512,
  )
  const url = safeObservedUrl(
    firstDefined(candidate, "url", "target_url", "page_url"),
  )
  const title =
    optionalString(
      firstDefined(candidate, "title", "name", "page_title"),
      "$.title",
      16 * 1024,
    ) ?? ""
  const recordKind = record.kind.toLowerCase()
  const destroyed =
    recordKind.includes("destroy")
    || recordKind.includes("detach")
    || recordKind.includes("close")
    || booleanValue(firstDefined(candidate, "closed", "destroyed", "detached"))
  const crashed =
    recordKind.includes("crash")
    || booleanValue(firstDefined(candidate, "crashed", "process_crashed"))
  const attached =
    !destroyed
    && !booleanValue(firstDefined(candidate, "detached", "closed"))
  const active =
    booleanValue(
      firstDefined(
        candidate,
        "active",
        "selected",
        "focused",
        "is_active",
        "isActive",
      ),
    )
    || recordKind.includes("target_selected")
    || recordKind.includes("target_activated")
  const existing = drafts.get(targetId)
  if (!existing) {
    drafts.set(targetId, {
      targetId,
      browserSessionId: record.scope.browserSessionId,
      url,
      title,
      kind: targetKind(candidate, parentTargetId, frameId),
      parentTargetId,
      openerTargetId,
      frameId,
      parentFrameId,
      active,
      attached,
      crashed,
      createdSequence: record.sequence,
      updatedSequence: record.sequence,
      sourceRecordIds: new Set([record.recordId]),
      findings: new Set(),
    })
    return
  }
  if (existing.browserSessionId !== record.scope.browserSessionId) {
    existing.findings.add("target_identity_crossed_browser_session")
    return
  }
  if (url) existing.url = url
  if (title) existing.title = title
  existing.kind =
    existing.kind === "unknown"
      ? targetKind(candidate, parentTargetId, frameId)
      : existing.kind
  existing.parentTargetId ??= parentTargetId
  existing.openerTargetId ??= openerTargetId
  existing.frameId ??= frameId
  existing.parentFrameId ??= parentFrameId
  existing.active = active || (existing.active && !destroyed)
  existing.attached = attached
  existing.crashed = crashed || existing.crashed
  existing.updatedSequence = Math.max(existing.updatedSequence, record.sequence)
  existing.sourceRecordIds.add(record.recordId)
}

function updateFrame(
  drafts: Map<string, FrameDraft>,
  candidate: Readonly<Record<string, unknown>>,
  record: BrowserRecord,
): void {
  const frameId = optionalString(
    firstDefined(candidate, "frame_id", "frameId", "id"),
    "$.frame_id",
    512,
  )
  if (!frameId) return
  const targetId = optionalString(
    firstDefined(candidate, "target_id", "targetId"),
    "$.target_id",
    512,
  )
  const parentFrameId = optionalString(
    firstDefined(
      candidate,
      "parent_frame_id",
      "parentFrameId",
      "parent_id",
    ),
    "$.parent_frame_id",
    512,
  )
  const detached =
    record.kind.includes("detach")
    || record.kind.includes("destroy")
    || booleanValue(firstDefined(candidate, "detached", "closed"))
  const url = safeObservedUrl(
    firstDefined(candidate, "url", "frame_url", "document_url"),
  )
  const existing = drafts.get(frameId)
  if (!existing) {
    drafts.set(frameId, {
      frameId,
      targetId,
      parentFrameId,
      browserSessionId: record.scope.browserSessionId,
      url,
      name: optionalString(candidate.name, "$.frame.name", 4_096),
      securityOrigin: optionalString(
        firstDefined(candidate, "security_origin", "securityOrigin"),
        "$.frame.security_origin",
        16 * 1024,
      ),
      mimeType: optionalString(
        firstDefined(candidate, "mime_type", "mimeType"),
        "$.frame.mime_type",
        512,
      ),
      main: !parentFrameId || booleanValue(firstDefined(candidate, "main", "is_main")),
      attached: !detached,
      sequence: record.sequence,
      sourceRecordIds: new Set([record.recordId]),
    })
    return
  }
  if (existing.browserSessionId !== record.scope.browserSessionId) return
  existing.targetId ??= targetId
  existing.parentFrameId ??= parentFrameId
  if (url) existing.url = url
  existing.name ??= optionalString(candidate.name, "$.frame.name", 4_096)
  existing.securityOrigin ??= optionalString(
    firstDefined(candidate, "security_origin", "securityOrigin"),
    "$.frame.security_origin",
    16 * 1024,
  )
  existing.mimeType ??= optionalString(
    firstDefined(candidate, "mime_type", "mimeType"),
    "$.frame.mime_type",
    512,
  )
  existing.main ||= !parentFrameId
  existing.attached = !detached
  existing.sequence = Math.max(existing.sequence, record.sequence)
  existing.sourceRecordIds.add(record.recordId)
}

function depthForTarget(
  targetId: string,
  drafts: ReadonlyMap<string, TargetDraft>,
): number {
  const visited = new Set<string>()
  let current = drafts.get(targetId)
  let depth = 0
  while (current?.parentTargetId && depth < 64) {
    if (visited.has(current.parentTargetId)) return 0
    visited.add(current.parentTargetId)
    depth += 1
    current = drafts.get(current.parentTargetId)
  }
  return depth
}

function depthForFrame(
  frameId: string,
  drafts: ReadonlyMap<string, FrameDraft>,
): number {
  const visited = new Set<string>()
  let current = drafts.get(frameId)
  let depth = 0
  while (current?.parentFrameId && depth < 128) {
    if (visited.has(current.parentFrameId)) return 0
    visited.add(current.parentFrameId)
    depth += 1
    current = drafts.get(current.parentFrameId)
  }
  return depth
}

function targetFindings(
  drafts: ReadonlyMap<string, TargetDraft>,
): string[] {
  const findings: string[] = []
  for (const target of drafts.values()) {
    if (
      target.parentTargetId
      && target.parentTargetId !== target.targetId
      && !drafts.has(target.parentTargetId)
    ) {
      findings.push(
        `target_parent_missing:${target.targetId}:${target.parentTargetId}`,
      )
      target.findings.add("parent target is not present in admitted history")
    }
    if (target.parentTargetId === target.targetId) {
      findings.push(`target_self_parent:${target.targetId}`)
      target.findings.add("target cannot parent itself")
    }
    if (target.kind === "popup" && !target.parentTargetId && !target.openerTargetId) {
      findings.push(`popup_opener_missing:${target.targetId}`)
      target.findings.add("popup has no parent/opener correlation")
    }
    if (target.crashed && target.attached) {
      findings.push(`crashed_target_still_attached:${target.targetId}`)
      target.findings.add("crashed target remains attached")
    }
  }
  return findings
}

function finalizeTargets(
  drafts: ReadonlyMap<string, TargetDraft>,
): BrowserTarget[] {
  return [...drafts.values()]
    .map((draft) =>
      Object.freeze({
        targetId: draft.targetId,
        browserSessionId: draft.browserSessionId,
        url: draft.url,
        title: draft.title,
        kind: draft.kind,
        parentTargetId: draft.parentTargetId,
        openerTargetId: draft.openerTargetId,
        frameId: draft.frameId,
        parentFrameId: draft.parentFrameId,
        depth: depthForTarget(draft.targetId, drafts),
        active: draft.active,
        attached: draft.attached,
        crashed: draft.crashed,
        createdSequence: draft.createdSequence,
        updatedSequence: draft.updatedSequence,
        sourceRecordIds: Object.freeze([...draft.sourceRecordIds]),
        findings: Object.freeze([...draft.findings]),
      }),
    )
    .sort(
      (left, right) =>
        Number(right.active) - Number(left.active)
        || Number(right.attached) - Number(left.attached)
        || left.depth - right.depth
        || left.createdSequence - right.createdSequence
        || left.targetId.localeCompare(right.targetId),
    )
}

function finalizeFrames(
  drafts: ReadonlyMap<string, FrameDraft>,
): BrowserFrame[] {
  return [...drafts.values()]
    .map((draft) =>
      Object.freeze({
        frameId: draft.frameId,
        targetId: draft.targetId,
        parentFrameId: draft.parentFrameId,
        browserSessionId: draft.browserSessionId,
        url: draft.url,
        name: draft.name,
        securityOrigin: draft.securityOrigin,
        mimeType: draft.mimeType,
        depth: depthForFrame(draft.frameId, drafts),
        main: draft.main,
        attached: draft.attached,
        sequence: draft.sequence,
        sourceRecordIds: Object.freeze([...draft.sourceRecordIds]),
      }),
    )
    .sort(
      (left, right) =>
        Number(right.main) - Number(left.main)
        || left.depth - right.depth
        || left.sequence - right.sequence
        || left.frameId.localeCompare(right.frameId),
    )
}

function synthesizeObservedTarget(
  scope: BrowserScope,
  records: readonly BrowserRecord[],
  drafts: Map<string, TargetDraft>,
): void {
  if (drafts.size > 0) return
  let url = ""
  let title = ""
  let recordId = ""
  let sequence = 0
  for (const record of records) {
    const candidateUrl =
      safeObservedUrl(
        firstDeep(record.payload, ["url", "current_url", "page_url"]),
      )
    const candidateTitle = deepString(
      record.payload,
      ["title", "page_title"],
      16 * 1024,
    )
    if (candidateUrl) {
      url = candidateUrl
      recordId = record.recordId
      sequence = record.sequence
    }
    if (candidateTitle) title = candidateTitle
  }
  if (!url && !title) return
  const targetId = `observed-target:${stableDigest({
    session: scope.browserSessionId,
    worker: scope.workerRequestId,
    url,
  })}`
  drafts.set(targetId, {
    targetId,
    browserSessionId: scope.browserSessionId,
    url,
    title,
    kind: "page",
    active: true,
    attached: true,
    crashed: false,
    createdSequence: sequence,
    updatedSequence: sequence,
    sourceRecordIds: new Set(recordId ? [recordId] : []),
    findings: new Set(["target identity synthesized from observed browser state"]),
  })
}

export function projectBrowserTargets(
  scope: BrowserScope,
  input: readonly BrowserRecord[],
): BrowserTargetProjection {
  const records = input
    .filter(
      (record) =>
        record.scope.browserSessionId === scope.browserSessionId
        && record.scope.workerRequestId === scope.workerRequestId,
    )
    .sort(
      (left, right) =>
        left.sequence - right.sequence
        || left.recordId.localeCompare(right.recordId),
    )
  const targets = new Map<string, TargetDraft>()
  const frames = new Map<string, FrameDraft>()
  for (const record of records) {
    for (const [index, candidate] of targetCandidates(record).entries()) {
      updateTarget(targets, candidate, record, index)
    }
    for (const candidate of frameCandidates(record)) {
      updateFrame(frames, candidate, record)
    }
  }
  synthesizeObservedTarget(scope, records, targets)
  const findings = targetFindings(targets)
  const finalizedTargets = finalizeTargets(targets)
  const finalizedFrames = finalizeFrames(frames)
  const active =
    finalizedTargets.find((target) => target.active && target.attached)
    ?? [...finalizedTargets]
      .filter((target) => target.attached)
      .sort((left, right) => right.updatedSequence - left.updatedSequence)[0]
    ?? finalizedTargets.at(-1)
  return Object.freeze({
    targets: Object.freeze(finalizedTargets),
    frames: Object.freeze(finalizedFrames),
    activeTargetId: active?.targetId,
    url: active?.url || undefined,
    title: active?.title || undefined,
    findings: Object.freeze(findings),
  })
}

export function popupTargets(
  projection: BrowserTargetProjection,
): readonly BrowserTarget[] {
  return Object.freeze(
    projection.targets.filter(
      (target) => target.kind === "popup" || Boolean(target.openerTargetId),
    ),
  )
}

export function targetAncestors(
  targetId: string,
  projection: BrowserTargetProjection,
): readonly BrowserTarget[] {
  const byId = new Map(projection.targets.map((target) => [target.targetId, target]))
  const output: BrowserTarget[] = []
  const visited = new Set<string>()
  let current = byId.get(targetId)
  while (current?.parentTargetId) {
    if (visited.has(current.parentTargetId)) break
    visited.add(current.parentTargetId)
    const parent = byId.get(current.parentTargetId)
    if (!parent) break
    output.unshift(parent)
    current = parent
  }
  return Object.freeze(output)
}
