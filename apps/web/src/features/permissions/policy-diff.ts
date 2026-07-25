import { stablePermissionId } from "./canonical.ts"
import type {
  PermissionModeProjection,
  PermissionRequestProjection,
  PermissionRuleProjection,
  PermissionWarningSeverity,
} from "./contracts.ts"

export interface PermissionPolicyDiffRow {
  diffId: string
  kind:
    | "policy_revision"
    | "mode_revision"
    | "mode"
    | "frozen"
    | "rule"
    | "scope"
  label: string
  requested: string
  current: string
  changed: boolean
  severity: PermissionWarningSeverity
  detail: string
}

export interface PermissionPolicyView {
  schema: "zyra.permission-policy-view/v1"
  requestId?: string
  currentPolicyRevision: number
  requestPolicyRevision?: number
  currentModeRevision: number
  requestModeRevision?: number
  policyHash: string
  mode: string
  frozen: boolean
  stale: boolean
  responseAllowed: boolean
  rules: readonly PermissionSafeRuleView[]
  diff: readonly PermissionPolicyDiffRow[]
  rawArgumentsExposed: false
}

export interface PermissionSafeRuleView {
  ruleId: string
  effect: "allow" | "deny" | "ask"
  source: string
  priority: number
  enabled: boolean
  revision: number
  scopeSummary: string
  reason: string
  expiresAt?: string
  expired: boolean
}

export function buildPermissionPolicyView(input: {
  mode: PermissionModeProjection
  rules: readonly PermissionRuleProjection[]
  request?: PermissionRequestProjection
  now?: Date
}): PermissionPolicyView {
  const now = input.now ?? new Date()
  const request = input.request
  const safeRules = input.rules
    .map((rule) => safeRule(rule, now))
    .sort(compareRules)
  const diff = buildPolicyDiff(input.mode, request, safeRules)
  return Object.freeze({
    schema: "zyra.permission-policy-view/v1",
    requestId: request?.requestId,
    currentPolicyRevision: input.mode.policyRevision,
    requestPolicyRevision: request?.policyRevision,
    currentModeRevision: input.mode.revision,
    requestModeRevision: request?.modeRevision,
    policyHash: input.mode.policyHash,
    mode: input.mode.mode,
    frozen: input.mode.frozen,
    stale: request?.stale ?? false,
    responseAllowed:
      input.mode.productMode === "interactive"
      && Boolean(request?.selectable),
    rules: Object.freeze(safeRules),
    diff: Object.freeze(diff),
    rawArgumentsExposed: false,
  })
}

export function buildPolicyDiff(
  mode: PermissionModeProjection,
  request: PermissionRequestProjection | undefined,
  rules: readonly PermissionSafeRuleView[],
): PermissionPolicyDiffRow[] {
  const rows: PermissionPolicyDiffRow[] = []
  if (request) {
    rows.push(
      diffRow({
        kind: "policy_revision",
        label: "Policy revision",
        requested: String(request.policyRevision),
        current: String(mode.policyRevision),
        changed: request.policyRevision !== mode.policyRevision,
        detail:
          request.policyRevision === mode.policyRevision
            ? "The request was evaluated under the current policy revision."
            : "Policy changed after this approval envelope was created; response is stale.",
      }),
      diffRow({
        kind: "mode_revision",
        label: "Mode revision",
        requested: String(request.modeRevision),
        current: String(mode.revision),
        changed: request.modeRevision !== mode.revision,
        detail:
          request.modeRevision === mode.revision
            ? "The request and console agree on the active mode revision."
            : "Mode changed after request creation; the old challenge cannot be answered.",
      }),
    )
  }
  rows.push(
    diffRow({
      kind: "mode",
      label: "Product mode",
      requested: request ? "interactive approval" : "—",
      current: mode.productMode,
      changed: mode.productMode === "sealed",
      detail:
        mode.productMode === "sealed"
          ? "Sealed mode converts ASK into deterministic deny/replan and prohibits human response."
          : "Interactive mode permits one exact response to a current challenge.",
    }),
    diffRow({
      kind: "frozen",
      label: "Policy mutation",
      requested: "no persistent grant",
      current: mode.frozen ? "frozen" : "mutable by owner",
      changed: false,
      detail:
        mode.frozen
          ? "Policy changes are disabled for this run."
          : "This panel still submits only one-shot responses; policy mutation uses a separate owner path.",
    }),
  )
  const enabled = rules.filter((rule) => rule.enabled && !rule.expired)
  const disabled = rules.length - enabled.length
  rows.push(
    diffRow({
      kind: "rule",
      label: "Effective rules",
      requested: request ? "exact request binding" : "all requests",
      current: `${enabled.length} enabled · ${disabled} inactive`,
      changed: disabled > 0,
      detail:
        "Rule display is a safe projection. Raw match inputs and tool arguments are not retained in the browser.",
    }),
  )
  const scoped = enabled.filter(
    (rule) => rule.scopeSummary !== "all exact matches in the bound scope",
  )
  if (scoped.length) {
    rows.push(
      diffRow({
        kind: "scope",
        label: "Scoped rules",
        requested: request?.toolName ?? "—",
        current: `${scoped.length} scoped rule(s)`,
        changed: false,
        detail:
          "Workspace identities are redacted and patterns are presented only as bounded summaries.",
      }),
    )
  }
  return rows
}

function safeRule(
  rule: PermissionRuleProjection,
  now: Date,
): PermissionSafeRuleView {
  const expires = rule.expiresAt ? Date.parse(rule.expiresAt) : Number.NaN
  return Object.freeze({
    ruleId: rule.ruleId,
    effect: rule.effect,
    source: rule.source,
    priority: rule.priority,
    enabled: rule.enabled,
    revision: rule.revision,
    scopeSummary: rule.scopeSummary,
    reason: rule.reason,
    expiresAt: rule.expiresAt,
    expired: Number.isFinite(expires) && expires <= now.getTime(),
  })
}

function diffRow(
  input: Omit<PermissionPolicyDiffRow, "diffId" | "severity">,
): PermissionPolicyDiffRow {
  return Object.freeze({
    ...input,
    diffId: stablePermissionId(
      "permission_policy_diff",
      input.kind,
      input.requested,
      input.current,
    ),
    severity: input.changed ? "warning" : "info",
  })
}

function compareRules(
  left: PermissionSafeRuleView,
  right: PermissionSafeRuleView,
): number {
  if (left.enabled !== right.enabled) return left.enabled ? -1 : 1
  if (left.expired !== right.expired) return left.expired ? 1 : -1
  return (
    right.priority - left.priority
    || right.revision - left.revision
    || left.ruleId.localeCompare(right.ruleId)
  )
}
