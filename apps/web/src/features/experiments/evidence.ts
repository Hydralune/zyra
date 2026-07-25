export interface BundleAssessment {
  valid: boolean
  bundleDigest: string
  merkleRoot: string
  memberCount: number
  categories: readonly string[]
  path: string
  findings: readonly string[]
}

function record(value: unknown): Readonly<Record<string, any>> {
  return value && typeof value === "object"
    ? value as Readonly<Record<string, any>>
    : {}
}

function array(value: unknown): readonly Readonly<Record<string, any>>[] {
  return Array.isArray(value)
    ? value.filter(
        (item): item is Readonly<Record<string, any>> =>
          Boolean(item) && typeof item === "object",
      )
    : []
}

export function assessBundle(value: unknown): BundleAssessment {
  const response = record(value)
  const bundle = record(response.bundle ?? response)
  const manifest = record(bundle.manifest ?? bundle.bundle_manifest ?? bundle)
  const members = array(manifest.members)
  const categories = [...new Set(
    members.map((item) => String(item.category || "")).filter(Boolean),
  )].sort()
  const findings: string[] = []
  const requiredCategories = [
    "source_manifest",
    "canonical_event",
    "raw_metric",
    "metric_summary",
    "comparison",
    "requirement",
    "report",
    "configuration",
    "verification",
    "navigation",
  ]
  for (const category of requiredCategories) {
    if (!categories.includes(category)) {
      findings.push(`Bundle category ${category} is missing.`)
    }
  }
  const bundleDigest = String(
    bundle.bundle_digest
      ?? bundle.sha256
      ?? manifest.bundle_digest
      ?? manifest.manifest_digest
      ?? "",
  )
  const merkleRoot = String(
    bundle.merkle_root
      ?? manifest.merkle_root
      ?? manifest.root_digest
      ?? "",
  )
  if (bundleDigest && !/^[a-f0-9]{64}$/i.test(bundleDigest)) {
    findings.push("Bundle digest is malformed.")
  }
  if (!/^[a-f0-9]{64}$/i.test(merkleRoot)) {
    findings.push("Merkle root is missing or malformed.")
  }
  const explicitValid =
    bundle.valid === true
    || record(bundle.verification).valid === true
    || record(bundle.verification_receipt).valid === true
    || record(response.verification_receipt).valid === true
  if (!explicitValid) findings.push("Bundle has no valid verifier receipt.")
  if (members.length === 0) findings.push("Bundle manifest contains no members.")
  return Object.freeze({
    valid: findings.length === 0,
    bundleDigest,
    merkleRoot,
    memberCount: members.length,
    categories: Object.freeze(categories),
    path: String(bundle.path ?? bundle.bundle_path ?? ""),
    findings: Object.freeze(findings),
  })
}

export interface ReviewerNode {
  nodeId: string
  kind: string
  label: string
  route: string
  digest: string
}

export interface ReviewerEdge {
  from: string
  to: string
  relation: string
}

export function reviewerGraph(reportValue: unknown): {
  nodes: readonly ReviewerNode[]
  edges: readonly ReviewerEdge[]
  backendLogRequired: boolean
} {
  const response = record(reportValue)
  const report = record(response.report ?? response)
  const navigation = record(report.reviewer_navigation)
  const nodes = array(navigation.nodes).map((item) => Object.freeze({
    nodeId: String(item.node_id || ""),
    kind: String(item.kind || ""),
    label: String(item.label || item.node_id || ""),
    route: String(item.route || ""),
    digest: String(item.digest || ""),
  }))
  const edges = array(navigation.edges).map((item) => Object.freeze({
    from: String(item.from || ""),
    to: String(item.to || ""),
    relation: String(item.relation || ""),
  }))
  return Object.freeze({
    nodes: Object.freeze(nodes),
    edges: Object.freeze(edges),
    backendLogRequired: navigation.backend_log_required === true,
  })
}
