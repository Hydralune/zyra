import {
  TraceNodeKind,
  TraceSemanticKind,
  type CausalTraceProjection,
  type TraceEdge,
  type TraceFilteredProjection,
  type TraceFoldGroup,
  type TraceFoldState,
  type TraceFoldedProjection,
  type TraceNode,
} from "../contracts.ts"

interface MutableFoldGroup {
  key: string
  parentKey?: string
  members: TraceNode[]
  visible: Set<string>
  hidden: Set<string>
  boundaryEdges: Set<string>
  collapsed: boolean
}

function primarySemantic(node: TraceNode): string {
  const priority = [
    TraceSemanticKind.FAULT,
    TraceSemanticKind.RECOVERY,
    TraceSemanticKind.PERMISSION,
    TraceSemanticKind.PROVIDER_RETRY,
    TraceSemanticKind.MCP_RECONNECT,
    TraceSemanticKind.SUBAGENT_YIELD,
    TraceSemanticKind.PROVIDER,
    TraceSemanticKind.TOOL,
    TraceSemanticKind.BROWSER,
    TraceSemanticKind.TERMINAL,
    TraceSemanticKind.ARTIFACT,
    TraceSemanticKind.MUTATION,
  ]
  return priority.find((semantic) => node.semantics.includes(semantic)) ??
    node.semantics[0] ?? TraceSemanticKind.OTHER
}

function foldGroupKey(node: TraceNode, state: TraceFoldState): string {
  switch (state.mode) {
    case "span":
      return node.refs.spanId ? `fold:span:${node.refs.spanId}` : `fold:unspanned:${node.key}`
    case "worker":
      return node.refs.workerId ? `fold:worker:${node.refs.workerId}` : `fold:unowned:${node.key}`
    case "semantic":
      return `fold:semantic:${primarySemantic(node)}`
    default:
      return node.parentKey ? `fold:parent:${node.parentKey}` : `fold:root:${node.key}`
  }
}

function foldParentKey(node: TraceNode, state: TraceFoldState): string | undefined {
  switch (state.mode) {
    case "span":
      return node.refs.parentSpanId ? `span:${node.refs.parentSpanId}` : node.parentKey
    case "worker":
      return node.refs.workerId ? `worker:${node.refs.workerId}` : node.parentKey
    case "semantic":
      return node.parentKey
    default:
      return node.parentKey
  }
}

function isFailure(node: TraceNode): boolean {
  return node.semantics.includes(TraceSemanticKind.FAULT) || Boolean(node.refs.failureId)
}

function shouldPreserve(node: TraceNode, state: TraceFoldState): boolean {
  if (state.preserveCritical && node.critical) return true
  if (state.preserveFailures && isFailure(node)) return true
  if (node.identity.kind === TraceNodeKind.ISSUE) return true
  return false
}

function sortedMembers(nodes: readonly TraceNode[]): TraceNode[] {
  return [...nodes].sort((left, right) =>
    left.sequence - right.sequence ||
    left.endSequence - right.endSequence ||
    left.key.localeCompare(right.key),
  )
}

function buildGroups(
  nodes: readonly TraceNode[],
  state: TraceFoldState,
): Map<string, MutableFoldGroup> {
  const groups = new Map<string, MutableFoldGroup>()
  for (const node of nodes) {
    const key = foldGroupKey(node, state)
    let group = groups.get(key)
    if (!group) {
      group = {
        key,
        parentKey: foldParentKey(node, state),
        members: [],
        visible: new Set(),
        hidden: new Set(),
        boundaryEdges: new Set(),
        collapsed: state.collapsedKeys.has(key) || state.collapsedKeys.has(node.key),
      }
      groups.set(key, group)
    }
    group.members.push(node)
  }
  for (const group of groups.values()) group.members = sortedMembers(group.members)
  return groups
}

function chooseVisibleMembers(group: MutableFoldGroup, state: TraceFoldState): void {
  if (!group.collapsed && group.members.length <= state.maximumVisibleChildren) {
    group.members.forEach((node) => group.visible.add(node.key))
    return
  }
  const preserved = group.members.filter((node) => shouldPreserve(node, state))
  preserved.forEach((node) => group.visible.add(node.key))
  const candidates = group.members.filter((node) => !group.visible.has(node.key))
  if (!candidates.length) return
  if (group.collapsed) {
    const first = candidates[0]
    const last = candidates.at(-1)
    if (first) group.visible.add(first.key)
    if (last) group.visible.add(last.key)
  } else {
    const budget = Math.max(1, state.maximumVisibleChildren - group.visible.size)
    if (candidates.length <= budget) candidates.forEach((node) => group.visible.add(node.key))
    else {
      const stride = Math.max(1, Math.floor(candidates.length / budget))
      for (let index = 0; index < candidates.length && group.visible.size < state.maximumVisibleChildren; index += stride) {
        const node = candidates[index]
        if (node) group.visible.add(node.key)
      }
      const last = candidates.at(-1)
      if (last && group.visible.size < state.maximumVisibleChildren) group.visible.add(last.key)
    }
  }
  group.members.forEach((node) => {
    if (!group.visible.has(node.key)) group.hidden.add(node.key)
  })
}

function representativeForHidden(
  hiddenKey: string,
  group: MutableFoldGroup,
  projection: CausalTraceProjection,
): string | undefined {
  const hidden = projection.nodesByKey[hiddenKey]
  if (!hidden || !group.visible.size) return undefined
  const visible = [...group.visible]
    .map((key) => projection.nodesByKey[key])
    .filter((node): node is TraceNode => Boolean(node))
  const preceding = visible
    .filter((node) => node.sequence <= hidden.sequence)
    .sort((left, right) => right.sequence - left.sequence)[0]
  if (preceding) return preceding.key
  return visible.sort((left, right) => left.sequence - right.sequence)[0]?.key
}

function groupByMember(groups: ReadonlyMap<string, MutableFoldGroup>): Map<string, MutableFoldGroup> {
  const output = new Map<string, MutableFoldGroup>()
  for (const group of groups.values()) {
    for (const member of group.members) output.set(member.key, group)
  }
  return output
}

function resolveEdgeVisibility(
  edge: TraceEdge,
  projection: CausalTraceProjection,
  visible: ReadonlySet<string>,
  members: ReadonlyMap<string, MutableFoldGroup>,
): { visible: boolean; boundary: boolean; sourceRepresentative?: string; targetRepresentative?: string } {
  const sourceVisible = visible.has(edge.sourceKey)
  const targetVisible = visible.has(edge.targetKey)
  if (sourceVisible && targetVisible) return { visible: true, boundary: false }
  const sourceGroup = members.get(edge.sourceKey)
  const targetGroup = members.get(edge.targetKey)
  const sourceRepresentative = sourceVisible
    ? edge.sourceKey
    : sourceGroup
      ? representativeForHidden(edge.sourceKey, sourceGroup, projection)
      : undefined
  const targetRepresentative = targetVisible
    ? edge.targetKey
    : targetGroup
      ? representativeForHidden(edge.targetKey, targetGroup, projection)
      : undefined
  if (!sourceRepresentative || !targetRepresentative) {
    return { visible: false, boundary: false, sourceRepresentative, targetRepresentative }
  }
  if (sourceRepresentative === targetRepresentative) {
    return { visible: false, boundary: false, sourceRepresentative, targetRepresentative }
  }
  return {
    visible: true,
    boundary: !sourceVisible || !targetVisible,
    sourceRepresentative,
    targetRepresentative,
  }
}

function summarizeGroup(group: MutableFoldGroup): string {
  const semantics = new Map<string, number>()
  for (const member of group.members) {
    for (const semantic of member.semantics) {
      semantics.set(semantic, (semantics.get(semantic) ?? 0) + 1)
    }
  }
  const dominant = [...semantics.entries()]
    .sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
    .slice(0, 3)
    .map(([semantic, count]) => `${semantic} ${count}`)
  return `${group.members.length} node(s), ${group.hidden.size} folded${dominant.length ? ` · ${dominant.join(" · ")}` : ""}`
}

function freezeGroup(group: MutableFoldGroup): TraceFoldGroup {
  return Object.freeze({
    key: group.key,
    parentKey: group.parentKey,
    memberKeys: Object.freeze(group.members.map((node) => node.key)),
    visibleMemberKeys: Object.freeze([...group.visible]),
    hiddenMemberKeys: Object.freeze([...group.hidden]),
    boundaryEdgeIds: Object.freeze([...group.boundaryEdges].sort()),
    collapsed: group.collapsed,
    summary: summarizeGroup(group),
    criticalCount: group.members.filter((node) => node.critical).length,
    failureCount: group.members.filter(isFailure).length,
    partialCount: group.members.filter((node) => node.completeness !== "complete").length,
  })
}

export function defaultTraceFoldState(): TraceFoldState {
  return Object.freeze({
    collapsedKeys: new Set<string>(),
    mode: "span",
    preserveCritical: true,
    preserveFailures: true,
    maximumVisibleChildren: 80,
  })
}

export function normalizeTraceFoldState(state: Partial<TraceFoldState>): TraceFoldState {
  const mode = state.mode === "manual" || state.mode === "worker" || state.mode === "semantic"
    ? state.mode
    : "span"
  const maximumVisibleChildren = Number.isSafeInteger(state.maximumVisibleChildren)
    ? Math.max(2, Math.min(10_000, Number(state.maximumVisibleChildren)))
    : 80
  return Object.freeze({
    collapsedKeys: new Set(state.collapsedKeys ?? []),
    mode,
    preserveCritical: state.preserveCritical !== false,
    preserveFailures: state.preserveFailures !== false,
    maximumVisibleChildren,
  })
}

export function foldCausalTrace(
  projection: CausalTraceProjection,
  filtered: TraceFilteredProjection,
  inputState: Partial<TraceFoldState> = {},
): TraceFoldedProjection {
  const state = normalizeTraceFoldState(inputState)
  const groups = buildGroups(filtered.nodes, state)
  for (const group of groups.values()) chooseVisibleMembers(group, state)
  const visible = new Set<string>()
  const hidden = new Set<string>()
  for (const group of groups.values()) {
    group.visible.forEach((key) => visible.add(key))
    group.hidden.forEach((key) => hidden.add(key))
  }
  const members = groupByMember(groups)
  const visibleEdgeIds: string[] = []
  const boundaryEdgeIds: string[] = []
  for (const edgeId of filtered.visibleEdgeIds) {
    const edge = projection.edgesById[edgeId]
    if (!edge) continue
    const resolution = resolveEdgeVisibility(edge, projection, visible, members)
    if (!resolution.visible) continue
    visibleEdgeIds.push(edge.id)
    if (resolution.boundary) {
      boundaryEdgeIds.push(edge.id)
      members.get(edge.sourceKey)?.boundaryEdges.add(edge.id)
      members.get(edge.targetKey)?.boundaryEdges.add(edge.id)
    }
  }
  const nodes = Object.freeze(filtered.nodes.filter((node) => visible.has(node.key)))
  const frozenGroups = Object.freeze([...groups.values()]
    .sort((left, right) =>
      (left.members[0]?.sequence ?? 0) - (right.members[0]?.sequence ?? 0) ||
      left.key.localeCompare(right.key),
    )
    .map(freezeGroup))
  return Object.freeze({
    nodes,
    groups: frozenGroups,
    visibleNodeKeys: Object.freeze(nodes.map((node) => node.key)),
    hiddenNodeKeys: Object.freeze([...hidden]),
    visibleEdgeIds: Object.freeze(visibleEdgeIds),
    boundaryEdgeIds: Object.freeze([...new Set(boundaryEdgeIds)]),
  })
}

export function toggleTraceFold(
  state: TraceFoldState,
  key: string,
): TraceFoldState {
  const collapsed = new Set(state.collapsedKeys)
  if (collapsed.has(key)) collapsed.delete(key)
  else collapsed.add(key)
  return Object.freeze({ ...state, collapsedKeys: collapsed })
}

export function expandTraceAncestors(
  state: TraceFoldState,
  projection: CausalTraceProjection,
  nodeKey: string,
): TraceFoldState {
  const collapsed = new Set(state.collapsedKeys)
  collapsed.delete(nodeKey)
  for (const ancestor of projection.hierarchy.ancestorsByKey[nodeKey] ?? []) {
    collapsed.delete(ancestor)
    collapsed.delete(`fold:parent:${ancestor}`)
    const node = projection.nodesByKey[ancestor]
    if (node?.refs.spanId) collapsed.delete(`fold:span:${node.refs.spanId}`)
    if (node?.refs.workerId) collapsed.delete(`fold:worker:${node.refs.workerId}`)
    if (node) collapsed.delete(`fold:semantic:${primarySemantic(node)}`)
  }
  const target = projection.nodesByKey[nodeKey]
  if (target?.refs.spanId) collapsed.delete(`fold:span:${target.refs.spanId}`)
  if (target?.refs.workerId) collapsed.delete(`fold:worker:${target.refs.workerId}`)
  if (target) collapsed.delete(`fold:semantic:${primarySemantic(target)}`)
  return Object.freeze({ ...state, collapsedKeys: collapsed })
}

export function collapseTraceNoise(
  projection: CausalTraceProjection,
  filtered: TraceFilteredProjection,
  state: TraceFoldState,
): TraceFoldState {
  const collapsed = new Set(state.collapsedKeys)
  const groups = buildGroups(filtered.nodes, state)
  for (const group of groups.values()) {
    const significant = group.members.some((node) => shouldPreserve(node, state))
    const allRoutine = group.members.every((node) =>
      !node.semantics.includes(TraceSemanticKind.FAULT) &&
      !node.semantics.includes(TraceSemanticKind.RECOVERY) &&
      !node.semantics.includes(TraceSemanticKind.PERMISSION) &&
      node.completeness === "complete",
    )
    if (allRoutine && !significant && group.members.length > 2) collapsed.add(group.key)
  }
  return Object.freeze({ ...state, collapsedKeys: collapsed })
}

export function traceFoldGroupForNode(
  folded: TraceFoldedProjection,
  nodeKey: string,
): TraceFoldGroup | undefined {
  return folded.groups.find((group) => group.memberKeys.includes(nodeKey))
}

export function traceFoldBoundaryEdges(
  folded: TraceFoldedProjection,
  projection: CausalTraceProjection,
): readonly TraceEdge[] {
  return Object.freeze(folded.boundaryEdgeIds
    .map((edgeId) => projection.edgesById[edgeId])
    .filter((edge): edge is TraceEdge => Boolean(edge)))
}
