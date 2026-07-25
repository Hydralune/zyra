import type {
  SkillCatalogPage,
  SkillControlOperation,
  SkillProjection,
  SkillRiskSeverity,
  SkillSupplyChainFinding,
  SkillWorkbenchSnapshot,
} from "./contracts.ts"

export interface SkillWorkbenchViewModel {
  headline: string
  status: string
  tone: "success" | "warning" | "danger" | "muted"
  controlsDisabled: boolean
  controlReason?: string
  catalogSummary: readonly { label: string; value: string }[]
  selected?: SkillDetailViewModel
  operation?: SkillOperationViewModel
  warnings: readonly string[]
}

export interface SkillDetailViewModel {
  identity: string
  title: string
  subtitle: string
  version: string
  hash: string
  provenance: string
  provenanceTone: "success" | "warning" | "danger"
  approval: string
  approvalTone: "success" | "warning" | "danger"
  risk: string
  riskTone: "success" | "warning" | "danger"
  resourceSummary: string
  dependencySummary: string
  toolSummary: string
  invocationSummary: string
  updateDisabled: boolean
  invokeDisabled: boolean
}

export interface SkillOperationViewModel {
  label: string
  phase: string
  tone: "success" | "warning" | "danger" | "muted"
  summary: string
  evidence: string
}

export function buildSkillWorkbenchViewModel(
  snapshot: SkillWorkbenchSnapshot,
): SkillWorkbenchViewModel {
  const selected = snapshot.catalog?.skills.find(
    (skill) => skill.skillId === snapshot.selectedSkillId,
  )
  const controlsDisabled =
    snapshot.closed ||
    !snapshot.enabled ||
    !snapshot.viewerOpen ||
    !snapshot.connected ||
    snapshot.sealed ||
    !selected
  const controlReason = snapshot.closed
    ? "controller closed"
    : !snapshot.enabled
      ? snapshot.disabledReason ?? "feature disabled"
      : !snapshot.viewerOpen
        ? "viewer detached"
        : !snapshot.connected
          ? "projection disconnected"
          : snapshot.sealed
            ? "sealed mode is read-only"
            : !selected
              ? "select a canonical skill"
              : undefined
  const status = snapshot.closed
    ? "closed"
    : !snapshot.enabled
      ? "disabled"
      : !snapshot.viewerOpen
        ? "detached"
        : !snapshot.connected
          ? "disconnected"
          : snapshot.sealed
            ? "sealed"
            : "live"
  const tone = status === "live"
    ? "success"
    : status === "sealed"
      ? "warning"
      : status === "detached"
        ? "muted"
        : "danger"
  return Object.freeze({
    headline: "Skills, provenance & supply chain",
    status,
    tone,
    controlsDisabled,
    controlReason,
    catalogSummary: Object.freeze([
      {
        label: "Skills",
        value: String(snapshot.catalog?.totalSkills ?? 0),
      },
      {
        label: "Available",
        value: String(snapshot.catalog?.availableSkills ?? 0),
      },
      {
        label: "Blocked",
        value: String(snapshot.catalog?.blockedSkills ?? 0),
      },
      {
        label: "Active invocations",
        value: String(snapshot.catalog?.activeInvocations ?? 0),
      },
      {
        label: "Projection revision",
        value: String(snapshot.catalog?.revision ?? 0),
      },
    ]),
    selected: selected ? buildSkillDetailViewModel(selected, controlsDisabled) : undefined,
    operation: snapshot.activeOperation
      ? buildSkillOperationViewModel(snapshot.activeOperation)
      : undefined,
    warnings: Object.freeze([
      ...(snapshot.catalog?.warnings ?? []),
      ...(selected?.warnings ?? []),
    ].slice(0, 100)),
  })
}

export function buildSkillDetailViewModel(
  skill: SkillProjection,
  controlsDisabled = false,
): SkillDetailViewModel {
  const riskTone =
    skill.supplyChain.browserDisposition === "pass"
      ? "success"
      : skill.supplyChain.browserDisposition === "review"
        ? "warning"
        : "danger"
  const approvalTone =
    skill.approval.invokeEligible
      ? "success"
      : skill.approval.stage === "rejected" ||
          skill.approval.stage === "expired"
        ? "danger"
        : "warning"
  const provenanceTone =
    skill.provenance.trustworthy
      ? "success"
      : skill.provenance.warnings.some((warning) =>
          /outside|traversal|owner/.test(warning))
        ? "danger"
        : "warning"
  const requiredResources = skill.resources.filter((resource) => resource.required)
  const availableResources = requiredResources.filter((resource) => resource.available)
  const terminalInvocations = skill.invocations.filter((invocation) =>
    ["completed", "failed", "cancelled"].includes(invocation.status))
  return Object.freeze({
    identity: skill.skillId,
    title: skill.displayName,
    subtitle: skill.description || skill.body.summary,
    version: `${skill.version.version} · registry ${skill.version.registryRevision}`,
    hash: compactHash(skill.version.contentHash),
    provenance: [
      skill.provenance.sourceKind,
      skill.provenance.sourceId,
      skill.provenance.signatureStatus,
    ].filter(Boolean).join(" · "),
    provenanceTone,
    approval: `${skill.approval.stage}${skill.approval.current ? " · current" : ""}`,
    approvalTone,
    risk: `${skill.supplyChain.browserDisposition} · ${skill.supplyChain.riskScore}/100`,
    riskTone,
    resourceSummary:
      `${availableResources.length}/${requiredResources.length} required available · ` +
      `${skill.resources.length} total`,
    dependencySummary:
      `${skill.dependencies.nodes.length} nodes · ${skill.dependencies.maximumDepth} depth · ` +
      `${skill.dependencies.cycles.length} cycles`,
    toolSummary:
      `${skill.toolScope.allowed.length} allowed · ` +
      `${skill.toolScope.denied.length} denied · ` +
      `${skill.toolScope.requireApproval.length} approval-bound`,
    invocationSummary:
      `${skill.activeInvocationIds.length} active · ` +
      `${terminalInvocations.length} terminal · ${skill.errorCount} errors`,
    updateDisabled: controlsDisabled || !skill.approval.updateEligible,
    invokeDisabled: controlsDisabled || !skill.approval.invokeEligible,
  })
}

export function buildSkillOperationViewModel(
  operation: SkillControlOperation,
): SkillOperationViewModel {
  const tone =
    operation.phase === "committed"
      ? "success"
      : ["failed", "quarantined", "disabled", "disconnected"].includes(operation.phase)
        ? "danger"
        : ["queued", "permission_pending", "reconciling"].includes(operation.phase)
          ? "warning"
          : "muted"
  const summary =
    operation.errorMessage ??
    operation.receipt?.summary ??
    `${operation.kind} submitted for ${operation.skillId}`
  const evidence = operation.effect
    ? [
        ...operation.effect.changedFields,
        ...operation.effect.evidenceInvocationIds,
        ...operation.effect.evidenceEventIds,
      ].slice(0, 12).join(" · ")
    : operation.canonicalEventIds.slice(0, 12).join(" · ")
  return Object.freeze({
    label: `${operation.kind} ${operation.skillId}`,
    phase: operation.phase,
    tone,
    summary,
    evidence: evidence || "Awaiting canonical evidence",
  })
}

export function findingTone(
  finding: SkillSupplyChainFinding,
): "success" | "warning" | "danger" | "muted" {
  if (finding.severity === "critical" || finding.severity === "high") return "danger"
  if (finding.severity === "medium" || finding.severity === "low") return "warning"
  return "muted"
}

export function severityRank(severity: SkillRiskSeverity): number {
  return {
    info: 0,
    low: 1,
    medium: 2,
    high: 3,
    critical: 4,
  }[severity]
}

export function pageAnnouncement(page: SkillCatalogPage): string {
  if (!page.total) return "No canonical skills match the current filters."
  const first = page.offset + 1
  const last = Math.min(page.total, page.offset + page.rows.length)
  return `Showing skills ${first} through ${last} of ${page.total}.`
}

export function compactHash(value: string): string {
  if (value.length <= 24) return value
  return `${value.slice(0, 15)}…${value.slice(-8)}`
}

export function relativeSkillTime(value: string, now = Date.now()): string {
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return "unknown"
  const delta = now - timestamp
  const future = delta < 0
  const absolute = Math.abs(delta)
  const units: [number, string][] = [
    [86_400_000, "day"],
    [3_600_000, "hour"],
    [60_000, "minute"],
    [1_000, "second"],
  ]
  for (const [size, label] of units) {
    if (absolute < size && label !== "second") continue
    const count = Math.max(1, Math.round(absolute / size))
    return future
      ? `in ${count} ${label}${count === 1 ? "" : "s"}`
      : `${count} ${label}${count === 1 ? "" : "s"} ago`
  }
  return "just now"
}
