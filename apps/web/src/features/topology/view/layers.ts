import type {
  GraphNodeRecord,
  LayerDatum,
  LayerSnapshot,
  TopologyGraphModel,
  TopologyLayerId,
} from "./contracts.ts"

interface MutableLayerDatum {
  entityId: string
  value: number
  label: string
  details: string[]
  severity?: LayerDatum["severity"]
  colorToken?: string
}
const LAYER_IDS: readonly TopologyLayerId[] = Object.freeze([
  "structure",
  "route-density",
  "route-churn",
  "broadcast",
  "placement",
  "provider-model",
  "privacy",
  "policy-violation",
])

const LOCATION_WEIGHT: Readonly<Record<string, number>> = Object.freeze({
  unknown: 0,
  device: 0.2,
  local: 0.35,
  edge: 0.65,
  cloud: 1,
})

const PRIVACY_WEIGHT: Readonly<Record<string, number>> = Object.freeze({
  public: 0.05,
  unclassified: 0.2,
  internal: 0.35,
  confidential: 0.65,
  restricted: 0.8,
  secret: 0.9,
  regulated: 1,
})

function finite(value: number | undefined, fallback = 0): number {
  return Number.isFinite(value) ? Number(value) : fallback
}

function frozenDatum(
  input: MutableLayerDatum,
  minimum: number,
  maximum: number,
): LayerDatum {
  const span = maximum - minimum
  const normalized = span <= 0 ? (input.value > 0 ? 1 : 0) : (input.value - minimum) / span
  const severity =
    input.severity ??
    (normalized >= 0.85
      ? "critical"
      : normalized >= 0.55
        ? "warning"
        : normalized > 0
          ? "info"
          : "none")
  return Object.freeze({
    entityId: input.entityId,
    value: input.value,
    normalized: Math.min(1, Math.max(0, normalized)),
    severity,
    label: input.label,
    details: Object.freeze([...input.details]),
    colorToken: input.colorToken ?? colorForSeverity(severity),
  })
}

function colorForSeverity(severity: LayerDatum["severity"]): string {
  if (severity === "critical") return "danger"
  if (severity === "warning") return "warning"
  if (severity === "info") return "accent"
  return "muted"
}

function snapshot(
  id: TopologyLayerId,
  revision: number,
  input: readonly MutableLayerDatum[],
): LayerSnapshot {
  const values = input.map((entry) => entry.value)
  const minimum = values.length === 0 ? 0 : Math.min(...values)
  const maximum = values.length === 0 ? 0 : Math.max(...values)
  const data = new Map<string, LayerDatum>()
  for (const entry of input) {
    const value = frozenDatum(entry, minimum, maximum)
    data.set(value.entityId, value)
  }
  return Object.freeze({
    id,
    revision,
    data,
    minimum,
    maximum,
    average:
      values.length === 0
        ? 0
        : values.reduce((total, value) => total + value, 0) / values.length,
    visibleCount: values.filter((value) => value > 0).length,
    criticalCount: [...data.values()].filter((value) => value.severity === "critical").length,
  })
}

function structureLayer(model: TopologyGraphModel): readonly MutableLayerDatum[] {
  return model.nodes.map((node) => {
    const incoming = model.incomingByNode.get(node.id)?.length ?? 0
    const outgoing = model.outgoingByNode.get(node.id)?.length ?? 0
    const total = incoming + outgoing
    return {
      entityId: node.id,
      value: total,
      label: `${total} structural connections`,
      details: [
        `${incoming} incoming`,
        `${outgoing} outgoing`,
        `${node.childCount} children`,
        `${node.dependencyCount} declared dependencies`,
      ],
    }
  })
}

function routeDensityLayer(model: TopologyGraphModel): readonly MutableLayerDatum[] {
  return model.nodes.map((node) => {
    const routeIds = model.routesByNode.get(node.id) ?? []
    const routes = routeIds
      .map((id) => model.routeById.get(id))
      .filter((value): value is NonNullable<typeof value> => Boolean(value))
    const candidates = routes.reduce((total, route) => total + route.candidateCount, 0)
    const accepted = routes.reduce(
      (total, route) => total + route.acceptedCandidateCount,
      0,
    )
    const density = routes.length + candidates / Math.max(1, node.dependencyCount + 1)
    return {
      entityId: node.id,
      value: density,
      label: `${routes.length} route decisions`,
      details: [
        `${candidates} candidates`,
        `${accepted} accepted candidates`,
        `${Math.max(0, candidates - accepted)} rejected candidates`,
      ],
    }
  })
}

function routeChurnLayer(model: TopologyGraphModel): readonly MutableLayerDatum[] {
  return model.nodes.map((node) => {
    const routes = (model.routesByNode.get(node.id) ?? [])
      .map((id) => model.routeById.get(id))
      .filter((value): value is NonNullable<typeof value> => Boolean(value))
      .sort(
        (left, right) =>
          left.sequence - right.sequence || left.id.localeCompare(right.id),
      )
    let changes = 0
    let previousSignature = ""
    for (const route of routes) {
      const signature = [
        route.selectedWorkerId,
        route.providerId,
        route.modelId,
        route.backendId,
        route.location,
      ].join("\0")
      if (previousSignature && signature !== previousSignature) changes += 1
      if (route.changed) changes += 1
      previousSignature = signature
    }
    const topologyMutations = routes.filter((route) => route.topologyMutation).length
    const value = changes + topologyMutations * 1.5
    return {
      entityId: node.id,
      value,
      label: `${changes} route transitions`,
      details: [
        `${routes.length} observed decisions`,
        `${topologyMutations} topology-changing routes`,
        node.selectedRouteId ? `selected ${node.selectedRouteId}` : "no selected route",
      ],
      severity: value >= 6 ? "critical" : value >= 3 ? "warning" : value > 0 ? "info" : "none",
    }
  })
}

function broadcastLayer(model: TopologyGraphModel): readonly MutableLayerDatum[] {
  return model.nodes.map((node) => {
    const outgoing = (model.outgoingByNode.get(node.id) ?? [])
      .map((id) => model.edgeById.get(id))
      .filter((value): value is NonNullable<typeof value> => Boolean(value))
    const distinctTargets = new Set(outgoing.map((edge) => edge.targetId)).size
    const namespaces = new Set(
      outgoing
        .map((edge) => model.nodeById.get(edge.targetId)?.namespace)
        .filter(Boolean),
    ).size
    const fanout = distinctTargets
    const value = fanout + Math.max(0, namespaces - 1) * 1.5
    return {
      entityId: node.id,
      value,
      label: `${fanout} broadcast targets`,
      details: [
        `${outgoing.length} outgoing edges`,
        `${namespaces} target namespaces`,
        `${outgoing.filter((edge) => edge.runtimeMutation).length} runtime-created edges`,
      ],
      severity:
        fanout >= 20
          ? "critical"
          : fanout >= 8
            ? "warning"
            : fanout > 1
              ? "info"
              : "none",
    }
  })
}

function placementLayer(model: TopologyGraphModel): readonly MutableLayerDatum[] {
  return model.nodes.map((node) => {
    const placements = (model.placementsByNode.get(node.id) ?? [])
      .map((id) => model.placementById.get(id))
      .filter((value): value is NonNullable<typeof value> => Boolean(value))
    const latest = placements.sort(
      (left, right) =>
        right.sequence - left.sequence || right.revision - left.revision,
    )[0]
    const base = LOCATION_WEIGHT[latest?.location ?? node.location] ?? 0
    const violation = latest?.slaSatisfied === false ? 0.4 : 0
    const changed = latest?.changed ? 0.15 : 0
    return {
      entityId: node.id,
      value: Math.min(1.5, base + violation + changed),
      label: latest ? `${latest.location} placement` : `${node.location} route placement`,
      details: [
        latest?.providerId ? `provider ${latest.providerId}` : "provider unavailable",
        latest?.modelId ? `model ${latest.modelId}` : "model unavailable",
        latest?.backendId ? `backend ${latest.backendId}` : "backend unavailable",
        latest?.slaSatisfied === false
          ? `SLA violations: ${latest.violations.join(", ")}`
          : "SLA satisfied or not projected",
      ],
      severity:
        latest?.slaSatisfied === false
          ? "critical"
          : latest?.location === "cloud"
            ? "warning"
            : latest
              ? "info"
              : "none",
      colorToken: latest?.location ?? node.location,
    }
  })
}

function providerModelLayer(model: TopologyGraphModel): readonly MutableLayerDatum[] {
  const signatures = new Map<string, number>()
  for (const node of model.nodes) {
    const signature = [node.providerId ?? "unknown", node.modelId ?? "unknown"].join("/")
    if (signature !== "unknown/unknown") {
      signatures.set(signature, (signatures.get(signature) ?? 0) + 1)
    }
  }
  const ordered = [...signatures.entries()].sort(
    ([leftName, leftCount], [rightName, rightCount]) =>
      rightCount - leftCount || leftName.localeCompare(rightName),
  )
  const rank = new Map(ordered.map(([name], index) => [name, index]))
  return model.nodes.map((node) => {
    const signature = [node.providerId ?? "unknown", node.modelId ?? "unknown"].join("/")
    const population = signatures.get(signature) ?? 0
    const paletteIndex = rank.get(signature) ?? -1
    return {
      entityId: node.id,
      value: paletteIndex < 0 ? 0 : paletteIndex + 1,
      label: signature === "unknown/unknown" ? "provider/model unknown" : signature,
      details: [
        `${population} node${population === 1 ? "" : "s"} share this provider/model`,
        node.backendId ? `backend ${node.backendId}` : "backend unavailable",
        `location ${node.location}`,
      ],
      severity: signature === "unknown/unknown" ? "warning" : "info",
      colorToken: paletteIndex < 0 ? "unknown" : `categorical-${paletteIndex % 12}`,
    }
  })
}

function privacyLayer(model: TopologyGraphModel): readonly MutableLayerDatum[] {
  return model.nodes.map((node) => {
    const privacy = node.privacyClass.toLocaleLowerCase()
    const weight = PRIVACY_WEIGHT[privacy] ?? (privacy.includes("secret") ? 0.9 : 0.4)
    const remotePenalty =
      node.location === "cloud" && weight >= 0.6
        ? 0.35
        : node.location === "edge" && weight >= 0.8
          ? 0.2
          : 0
    const value = Math.min(1.5, weight + remotePenalty)
    return {
      entityId: node.id,
      value,
      label: `${node.privacyClass} on ${node.location}`,
      details: [
        `privacy weight ${weight.toFixed(2)}`,
        remotePenalty > 0
          ? `remote placement penalty ${remotePenalty.toFixed(2)}`
          : "placement compatible with projected privacy class",
        node.providerId ? `provider ${node.providerId}` : "provider unavailable",
      ],
      severity:
        remotePenalty >= 0.35
          ? "critical"
          : remotePenalty > 0
            ? "warning"
            : weight >= 0.8
              ? "info"
              : "none",
      colorToken: privacy || "unclassified",
    }
  })
}

function policyViolationLayer(model: TopologyGraphModel): readonly MutableLayerDatum[] {
  return model.nodes.map((node) => {
    const routeIds = model.routesByNode.get(node.id) ?? []
    const placementIds = model.placementsByNode.get(node.id) ?? []
    const rejectedRoutes = routeIds
      .map((id) => model.routeById.get(id))
      .filter((route) => route && !route.accepted)
    const violatingPlacements = placementIds
      .map((id) => model.placementById.get(id))
      .filter((placement) => placement && !placement.slaSatisfied)
    const missing = node.missingDependencyIds.length
    const stateViolation = ["failed", "rejected", "conflicted"].includes(node.state) ? 1 : 0
    const value =
      rejectedRoutes.length * 1.25 +
      violatingPlacements.length * 1.5 +
      missing +
      stateViolation
    const details: string[] = []
    if (rejectedRoutes.length > 0) details.push(`${rejectedRoutes.length} rejected routes`)
    if (violatingPlacements.length > 0) {
      details.push(`${violatingPlacements.length} SLA-violating placements`)
      details.push(
        ...violatingPlacements.flatMap((placement) => placement!.violations).slice(0, 5),
      )
    }
    if (missing > 0) details.push(`${missing} missing dependencies`)
    if (stateViolation) details.push(`node state ${node.state}`)
    if (details.length === 0) details.push("no projected policy violation")
    return {
      entityId: node.id,
      value,
      label: value > 0 ? `${value.toFixed(2)} policy risk` : "policy satisfied",
      details,
      severity:
        value >= 3 ? "critical" : value > 0 ? "warning" : "none",
      colorToken: value > 0 ? "danger" : "success",
    }
  })
}

export function buildTopologyLayers(
  model: TopologyGraphModel,
): ReadonlyMap<TopologyLayerId, LayerSnapshot> {
  const builders: Readonly<
    Record<TopologyLayerId, (model: TopologyGraphModel) => readonly MutableLayerDatum[]>
  > = Object.freeze({
    structure: structureLayer,
    "route-density": routeDensityLayer,
    "route-churn": routeChurnLayer,
    broadcast: broadcastLayer,
    placement: placementLayer,
    "provider-model": providerModelLayer,
    privacy: privacyLayer,
    "policy-violation": policyViolationLayer,
  })
  const result = new Map<TopologyLayerId, LayerSnapshot>()
  for (const id of LAYER_IDS) {
    result.set(id, snapshot(id, model.projectionRevision, builders[id](model)))
  }
  return result
}

export function topologyLayerIds(): readonly TopologyLayerId[] {
  return LAYER_IDS
}

export function topologyLayerLabel(id: TopologyLayerId): string {
  const labels: Readonly<Record<TopologyLayerId, string>> = Object.freeze({
    structure: "结构关系",
    "route-density": "路由密度",
    "route-churn": "路由变更",
    broadcast: "广播范围",
    placement: "执行位置",
    "provider-model": "服务商 / 模型",
    privacy: "隐私等级",
    "policy-violation": "策略违规",
  })
  return labels[id]
}

export function dominantLayerDatum(
  snapshots: ReadonlyMap<TopologyLayerId, LayerSnapshot>,
  entityId: string,
): { layer: TopologyLayerId; datum: LayerDatum } | undefined {
  let selected: { layer: TopologyLayerId; datum: LayerDatum } | undefined
  for (const id of LAYER_IDS) {
    const datum = snapshots.get(id)?.data.get(entityId)
    if (!datum) continue
    if (
      !selected ||
      severityRank(datum.severity) > severityRank(selected.datum.severity) ||
      (severityRank(datum.severity) === severityRank(selected.datum.severity) &&
        datum.normalized > selected.datum.normalized)
    ) {
      selected = { layer: id, datum }
    }
  }
  return selected
}

function severityRank(value: LayerDatum["severity"]): number {
  if (value === "critical") return 3
  if (value === "warning") return 2
  if (value === "info") return 1
  return 0
}

export function layerSummary(
  layer: LayerSnapshot,
  model: TopologyGraphModel,
): readonly string[] {
  const critical = [...layer.data.values()]
    .filter((datum) => datum.severity === "critical")
    .sort((left, right) => right.value - left.value || left.entityId.localeCompare(right.entityId))
    .slice(0, 5)
  const values = [
    `${layer.visibleCount}/${model.nodes.length} nodes carry a visible value`,
    `${layer.criticalCount} critical`,
    `range ${finite(layer.minimum).toFixed(2)}–${finite(layer.maximum).toFixed(2)}`,
    `average ${finite(layer.average).toFixed(2)}`,
  ]
  if (critical.length > 0) {
    values.push(`highest: ${critical.map((datum) => datum.entityId).join(", ")}`)
  }
  return Object.freeze(values)
}

export function nodeLayerDatum(
  layer: LayerSnapshot | undefined,
  node: GraphNodeRecord,
): LayerDatum | undefined {
  return layer?.data.get(node.id)
}
