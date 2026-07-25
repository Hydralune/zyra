import type { JsonObject } from "../../events/ingress/index.ts"
import type {
  SkillBodyProjection,
  SkillDependencyGraph,
  SkillProvenanceProjection,
  SkillResourceProjection,
  SkillStagedApproval,
  SkillSupplyChainAudit,
  SkillSupplyChainFinding,
  SkillRiskSeverity,
  SkillVersionProjection,
} from "./contracts.ts"
import {
  firstSkillArray,
  firstSkillRecord,
  firstSkillText,
  optionalSkillHash,
  optionalSkillTimestamp,
  skillBoolean,
  skillFingerprint,
  skillInteger,
  skillRecord,
  skillRecords,
  skillText,
  uniqueSkillStrings,
} from "./value.ts"

const SEVERITY_SCORE: Record<SkillRiskSeverity, number> = {
  info: 0,
  low: 5,
  medium: 20,
  high: 45,
  critical: 80,
}

export function auditSkillSupplyChain(input: {
  skillId: string
  records: readonly JsonObject[]
  body: SkillBodyProjection
  resources: readonly SkillResourceProjection[]
  provenance: SkillProvenanceProjection
  dependencies: SkillDependencyGraph
  version: SkillVersionProjection
}): SkillSupplyChainAudit {
  const owner = canonicalSupplyRecord(input.records)
  const findings = new Map<string, SkillSupplyChainFinding>()
  for (const finding of projectCanonicalFindings(owner)) {
    findings.set(finding.findingId, finding)
  }
  const derived = deriveBrowserFindings(input)
  for (const finding of derived) {
    const existing = [...findings.values()].find((item) =>
      item.code === finding.code && item.subject === finding.subject)
    if (existing) continue
    findings.set(finding.findingId, finding)
  }
  const ordered = [...findings.values()].sort((left, right) =>
    severityRank(right.severity) - severityRank(left.severity) ||
    left.code.localeCompare(right.code) ||
    (left.subject ?? "").localeCompare(right.subject ?? ""))
  const blockingFindingIds = ordered
    .filter((finding) => finding.blocking)
    .map((finding) => finding.findingId)
  const warnings: string[] = []
  if (!Object.keys(owner).length) {
    warnings.push("canonical owner supplied no supply-chain receipt")
  }
  if (owner.allowed === true && blockingFindingIds.length) {
    warnings.push("owner allowed receipt conflicts with browser-visible blocking facts")
  }
  const allowedByOwner = typeof owner.allowed === "boolean"
    ? owner.allowed
    : typeof owner.passed === "boolean"
      ? owner.passed
      : undefined
  const riskScore = Math.min(
    100,
    ordered.reduce((score, finding) => score + SEVERITY_SCORE[finding.severity], 0),
  )
  const browserDisposition: SkillSupplyChainAudit["browserDisposition"] =
    blockingFindingIds.length || allowedByOwner === false
      ? "block"
      : ordered.some((finding) => ["medium", "high"].includes(finding.severity)) ||
          allowedByOwner === undefined
        ? "review"
        : "pass"
  const core = {
    policyRevision: firstSkillText(
      [owner],
      ["policy_revision", "policyRevision"],
    ) || undefined,
    receiptId: firstSkillText([owner], ["receipt_id", "receiptId", "id"]) || undefined,
    scannedAt: optionalSkillTimestamp(owner.scanned_at ?? owner.scannedAt),
    manifestDigest: optionalSkillHash(
      firstSkillText([owner], ["manifest_digest", "manifestDigest"]),
      "skill supply-chain manifest digest",
    ),
    allowedByOwner,
    browserDisposition,
    riskScore,
    findings: ordered,
    blockingFindingIds,
    warnings,
  }
  return Object.freeze({
    ...core,
    findings: Object.freeze(ordered),
    blockingFindingIds: Object.freeze(blockingFindingIds),
    warnings: Object.freeze(warnings),
    digest: skillFingerprint([core]),
  })
}

function canonicalSupplyRecord(records: readonly JsonObject[]): JsonObject {
  return firstSkillRecord(records, [
    "supply_chain",
    "supplyChain",
    "supply_chain_receipt",
    "supply_receipt",
    "security_audit",
  ])
}

function projectCanonicalFindings(owner: JsonObject): SkillSupplyChainFinding[] {
  const result: SkillSupplyChainFinding[] = []
  const values = [
    ...skillRecords(owner.findings),
    ...skillRecords(owner.violations),
    ...skillRecords(owner.warnings),
  ]
  for (let index = 0; index < values.length; index += 1) {
    const item = values[index]!
    const code = firstSkillText(
      [item],
      ["code", "finding_code", "rule_id"],
      `owner_finding_${index}`,
    )
    const severity = normalizeSeverity(
      firstSkillText([item], ["severity", "level", "risk"], "medium"),
    )
    const subject = firstSkillText(
      [item],
      ["subject", "path", "dependency_id", "resource_id"],
    ) || undefined
    const blocking = skillBoolean(
      item.blocking ?? item.blocked ?? item.denied,
      severity === "high" || severity === "critical",
    )
    result.push(Object.freeze({
      findingId: firstSkillText([item], ["finding_id", "findingId", "id"]) ||
        skillFingerprint(["owner", code, subject, index]),
      code,
      severity,
      message: firstSkillText(
        [item],
        ["message", "summary", "reason"],
        code.replace(/[_-]/g, " "),
      ),
      source: firstSkillText([item], ["source", "scanner"], "canonical-owner"),
      subject,
      evidenceRefs: uniqueSkillStrings(
        skillStringsFromFinding(item),
      ),
      blocking,
      canonical: true,
    }))
  }
  return result
}

function deriveBrowserFindings(input: {
  skillId: string
  body: SkillBodyProjection
  resources: readonly SkillResourceProjection[]
  provenance: SkillProvenanceProjection
  dependencies: SkillDependencyGraph
  version: SkillVersionProjection
}): SkillSupplyChainFinding[] {
  const result: SkillSupplyChainFinding[] = []
  const add = (
    code: string,
    severity: SkillRiskSeverity,
    message: string,
    subject?: string,
    evidenceRefs: readonly string[] = [],
    blocking = severity === "critical" || severity === "high",
  ): void => {
    result.push(Object.freeze({
      findingId: skillFingerprint(["browser", input.skillId, code, subject]),
      code,
      severity,
      message,
      source: "zyra-browser-audit",
      subject,
      evidenceRefs: Object.freeze([...evidenceRefs]),
      blocking,
      canonical: false,
    }))
  }
  if (!input.provenance.trustworthy) {
    add(
      "provenance_untrusted",
      "high",
      "Skill provenance is incomplete or outside the admitted trust policy.",
      input.provenance.sourceId,
      input.provenance.warnings,
    )
  }
  if (!input.version.contentHash || !input.version.descriptorDigest) {
    add(
      "hash_missing",
      "critical",
      "Skill version lacks a content or descriptor digest.",
    )
  }
  for (const fragment of input.body.suspiciousFragments) {
    add(
      "prompt_injection_pattern",
      "high",
      "Skill body contains a directive that can weaken system or permission boundaries.",
      fragment.split(":")[0],
      [fragment],
    )
  }
  if (input.body.truncated) {
    add(
      "body_projection_truncated",
      "medium",
      "Skill body exceeded the browser projection budget and requires owner-side review.",
      undefined,
      [String(input.body.byteLength)],
      true,
    )
  }
  for (const resource of input.resources) {
    if (resource.required && !resource.available) {
      add(
        "required_resource_missing",
        "high",
        "Required skill resource is unavailable.",
        resource.path,
        resource.warnings,
      )
    }
    if (resource.private) {
      add(
        "private_resource_declared",
        "medium",
        "Skill declares a private resource; browser content access remains disabled.",
        resource.path,
        [],
        false,
      )
    }
    if (!resource.digest) {
      add(
        "resource_digest_missing",
        resource.required ? "high" : "medium",
        "Skill resource has no canonical digest.",
        resource.path,
        resource.warnings,
        resource.required,
      )
    }
    if (resource.warnings.some((warning) => /outside|escape|traversal/.test(warning))) {
      add(
        "resource_boundary_escape",
        "critical",
        "Skill resource may resolve outside its owning skill directory.",
        resource.path,
        resource.warnings,
      )
    }
  }
  for (const dependencyId of input.dependencies.missing) {
    add(
      "dependency_missing",
      "high",
      "Required dependency is missing.",
      dependencyId,
    )
  }
  for (const dependencyId of input.dependencies.unapproved) {
    add(
      "dependency_unapproved",
      "high",
      "Dependency has not passed staged approval.",
      dependencyId,
    )
  }
  for (const dependencyId of input.dependencies.versionConflicts) {
    add(
      "dependency_version_conflict",
      "high",
      "Resolved dependency version violates the declared range.",
      dependencyId,
    )
  }
  for (const cycle of input.dependencies.cycles) {
    add(
      "dependency_cycle",
      "high",
      "Skill dependency graph contains a cycle.",
      cycle.join(" -> "),
      cycle,
    )
  }
  return result
}

export function projectSkillStagedApproval(input: {
  records: readonly JsonObject[]
  version: SkillVersionProjection
  dependencies: SkillDependencyGraph
  supplyChain: SkillSupplyChainAudit
  availability: string
  observedAt: string
}): SkillStagedApproval {
  const approval = firstSkillRecord(input.records, [
    "staged_approval",
    "approval",
    "review",
    "supply_chain_approval",
  ])
  const rawStage = firstSkillText(
    [approval],
    ["stage", "status", "approval_stage"],
    "unreviewed",
  )
  const stage = normalizeApprovalStage(rawStage)
  const approvedHash = optionalSkillHash(
    firstSkillText([approval], ["approved_hash", "content_hash", "skill_hash"]),
    "approved skill hash",
  )
  const approvedDependencyDigest = normalizeAuditDigest(
    firstSkillText([approval], [
      "approved_dependency_digest",
      "dependency_digest",
    ]),
  )
  const approvedSupplyDigest = normalizeAuditDigest(
    firstSkillText([approval], [
      "approved_supply_digest",
      "supply_digest",
    ]),
  )
  const expiresAt = optionalSkillTimestamp(approval.expires_at ?? approval.expiresAt)
  const expired = expiresAt ? Date.parse(expiresAt) <= Date.parse(input.observedAt) : false
  const hashCurrent = Boolean(approvedHash && approvedHash === input.version.contentHash)
  const dependenciesCurrent = Boolean(
    approvedDependencyDigest &&
    approvedDependencyDigest === input.dependencies.digest,
  )
  const supplyCurrent = Boolean(
    approvedSupplyDigest &&
    approvedSupplyDigest === input.supplyChain.digest,
  )
  const reasons: string[] = []
  if (stage !== "approved") reasons.push(`approval stage is ${stage}`)
  if (!hashCurrent) reasons.push("approved content hash does not match current content")
  if (!dependenciesCurrent) reasons.push("approved dependency graph does not match current graph")
  if (!supplyCurrent) reasons.push("approved supply-chain audit does not match current audit")
  if (expired) reasons.push("staged approval has expired")
  if (!input.dependencies.complete) reasons.push("dependency graph is incomplete")
  if (input.supplyChain.browserDisposition === "block") reasons.push("supply-chain audit is blocking")
  if (input.supplyChain.allowedByOwner === false) reasons.push("canonical owner denied supply chain")
  if (input.availability !== "available") reasons.push(`skill availability is ${input.availability}`)
  const current =
    stage === "approved" &&
    hashCurrent &&
    dependenciesCurrent &&
    supplyCurrent &&
    !expired
  const safe =
    current &&
    input.dependencies.complete &&
    input.supplyChain.browserDisposition !== "block" &&
    input.supplyChain.allowedByOwner !== false &&
    input.availability === "available"
  return Object.freeze({
    stage: expired ? "expired" : stage,
    approvalId: firstSkillText([approval], ["approval_id", "approvalId", "id"]) || undefined,
    approvalRevision: optionalApprovalRevision(
      approval.approval_revision ?? approval.revision,
    ),
    approvedHash,
    approvedDependencyDigest,
    approvedSupplyDigest,
    reviewerClass: firstSkillText(
      [approval],
      ["reviewer_class", "reviewerClass", "actor_class"],
    ) || undefined,
    reviewedAt: optionalSkillTimestamp(approval.reviewed_at ?? approval.reviewedAt),
    expiresAt,
    rejectedReason: firstSkillText(
      [approval],
      ["rejected_reason", "reason", "error"],
    ) || undefined,
    current,
    updateEligible: safe,
    invokeEligible: safe,
    reasons: Object.freeze(uniqueSkillStrings(reasons)),
  })
}

function normalizeSeverity(value: string): SkillRiskSeverity {
  const normalized = value.toLowerCase()
  if (normalized === "critical" || normalized === "fatal") return "critical"
  if (normalized === "high" || normalized === "error") return "high"
  if (normalized === "medium" || normalized === "warning" || normalized === "warn") return "medium"
  if (normalized === "low") return "low"
  return "info"
}

function severityRank(value: SkillRiskSeverity): number {
  return ["info", "low", "medium", "high", "critical"].indexOf(value)
}

function skillStringsFromFinding(value: JsonObject): string[] {
  const raw = [
    ...normalizeStringArray(value.evidence_refs),
    ...normalizeStringArray(value.evidence),
    ...normalizeStringArray(value.artifact_ids),
    ...normalizeStringArray(value.event_ids),
  ]
  return [...new Set(raw)].sort()
}

function normalizeStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .map((item) => skillText(item, "", 1_024))
    .filter(Boolean)
    .slice(0, 256)
}

function normalizeApprovalStage(value: string): SkillStagedApproval["stage"] {
  const normalized = value.toLowerCase().replace(/[\s-]+/g, "_")
  if (
    [
      "unreviewed",
      "provenance_reviewed",
      "dependencies_reviewed",
      "supply_chain_reviewed",
      "approved",
      "rejected",
      "expired",
    ].includes(normalized)
  ) {
    return normalized as SkillStagedApproval["stage"]
  }
  if (/allow|accept|pass|complete/.test(normalized)) return "approved"
  if (/deny|reject|block|fail/.test(normalized)) return "rejected"
  return "unreviewed"
}

function normalizeAuditDigest(value: string): string | undefined {
  if (!value) return undefined
  if (/^(?:sha(?:256|384|512):)?[a-f0-9]{32,128}$/i.test(value)) {
    return value.includes(":") ? value.toLowerCase() : `sha256:${value.toLowerCase()}`
  }
  if (/^skill:[a-f0-9]{16,128}$/i.test(value)) return value.toLowerCase()
  return undefined
}

function optionalApprovalRevision(value: unknown): number | undefined {
  if (value === undefined || value === null || value === "") return undefined
  return skillInteger(value, 0, 0, Number.MAX_SAFE_INTEGER)
}
