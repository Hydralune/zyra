import type {
  PermissionArgumentPreview,
  PermissionRequestBinding,
  PermissionRiskLevel,
  PermissionSourceSurface,
  PermissionWarning,
  PermissionWarningSeverity,
} from "./contracts.ts"
import { permissionPreviewContainsSecret } from "./redaction.ts"
import { canonicalPermissionJson } from "./canonical.ts"

export interface PermissionHazardSignals {
  injection: readonly string[]
  supplyChain: readonly string[]
}

const INJECTION_MARKERS: ReadonlyArray<{
  code: string
  pattern: RegExp
  detail: string
}> = [
  {
    code: "instruction_override",
    pattern:
      /\b(ignore|disregard|override|forget)\b.{0,48}\b(previous|prior|system|developer|policy|instructions?)\b/i,
    detail: "Content resembles an instruction-override attempt.",
  },
  {
    code: "credential_request",
    pattern:
      /\b(show|print|send|upload|exfiltrate|reveal|return)\b.{0,48}\b(token|secret|password|credential|cookie|private key)\b/i,
    detail: "Content asks to expose credential-shaped data.",
  },
  {
    code: "tool_impersonation",
    pattern:
      /\b(system message|developer message|tool result|permission granted|approved by)\b/i,
    detail: "Content may be impersonating a trusted runtime message.",
  },
  {
    code: "encoded_payload",
    pattern:
      /\b(base64|decode|powershell\s+-enc|fromcharcode|eval\s*\(|exec\s*\()\b/i,
    detail: "Content contains an encoded or dynamically evaluated payload.",
  },
  {
    code: "policy_bypass",
    pattern:
      /\b(disable|bypass|skip|turn off)\b.{0,40}\b(permission|sandbox|guard|policy|approval)\b/i,
    detail: "Content requests bypassing a permission or sandbox control.",
  },
]

const SUPPLY_CHAIN_MARKERS: ReadonlyArray<{
  code: string
  pattern: RegExp
  detail: string
}> = [
  {
    code: "remote_install",
    pattern:
      /\b(curl|wget|irm|invoke-webrequest)\b.{0,160}\|\s*(sh|bash|zsh|pwsh|powershell)\b/i,
    detail: "A remote payload appears to be piped directly to a shell.",
  },
  {
    code: "package_global_install",
    pattern:
      /\b(npm|pnpm|yarn|pip|pipx|cargo|gem|brew|winget|choco)\b.{0,32}\b(install|add)\b/i,
    detail: "The action may install or change executable dependencies.",
  },
  {
    code: "unpinned_remote",
    pattern:
      /\b(git clone|npx|bunx|uvx|docker pull|uses:)\b(?![^\n]{0,80}(?:@[0-9a-f]{12,}|:[0-9]+\.[0-9]+|sha256:))/i,
    detail: "A remote dependency appears unpinned or mutable.",
  },
  {
    code: "plugin_or_skill_load",
    pattern:
      /\b(plugin|extension|skill|hook|mcp server)\b.{0,48}\b(install|enable|load|connect|trust)\b/i,
    detail: "The action changes executable extension or tool supply.",
  },
  {
    code: "container_privilege",
    pattern:
      /\b(docker|podman|kubectl)\b.{0,80}(--privileged|hostnetwork|hostpath|docker\.sock|cluster-admin)\b/i,
    detail: "Container or cluster access requests an elevated boundary.",
  },
]

export function permissionSourceSurface(
  binding: PermissionRequestBinding,
): PermissionSourceSurface {
  const namespace = binding.namespace.toLowerCase()
  const tool = binding.toolName.toLowerCase()
  const operation = binding.operation.toLowerCase()
  if (
    namespace === "browser"
    || /browser|computer|navigate|click|screenshot|webfetch|websearch/.test(tool)
  ) {
    return "browser"
  }
  if (
    namespace === "terminal"
    || /shell|bash|powershell|terminal|execute_command|exec/.test(tool)
  ) {
    return "terminal"
  }
  if (namespace === "mcp" || tool.startsWith("mcp_") || binding.serverId) {
    return "mcp"
  }
  if (namespace === "command" || /command/.test(tool)) return "command"
  if (namespace === "plugin" || /plugin|extension|hook/.test(tool)) return "plugin"
  if (namespace === "skill" || /skill/.test(tool)) return "skill"
  if (namespace === "agent" || /agent|task|subagent/.test(tool)) return "agent"
  if (operation || tool) return "tool"
  return "unknown"
}

export function permissionRiskLevel(
  binding: PermissionRequestBinding,
  preview: PermissionArgumentPreview,
): PermissionRiskLevel {
  const material = searchable(binding, preview)
  if (
    /\b(delete|remove|destroy|reset|format|publish|deploy|push|release|send|upload|payment|credential|sudo|admin|privileged)\b/i.test(
      material,
    )
    || permissionPreviewContainsSecret(preview)
  ) {
    return "high"
  }
  if (
    /\b(write|edit|create|install|execute|shell|terminal|browser|network|download|connect|mutate|restart)\b/i.test(
      material,
    )
  ) {
    return "medium"
  }
  if (
    /\b(read|list|get|inspect|search|status|health|preview)\b/i.test(material)
  ) {
    return "low"
  }
  return "unknown"
}

export function permissionWarnings(
  binding: PermissionRequestBinding,
  preview: PermissionArgumentPreview,
  context: {
    policyFrozen: boolean
    sealed: boolean
    expiresAt: string
    now: Date
    hazards?: PermissionHazardSignals
  },
): PermissionWarning[] {
  const warnings: PermissionWarning[] = []
  const material = searchable(binding, preview)
  const surface = permissionSourceSurface(binding)
  const risk = permissionRiskLevel(binding, preview)
  if (permissionPreviewContainsSecret(preview)) {
    warnings.push(
      warning(
        "secret_redacted",
        "danger",
        "Secret-shaped values were redacted",
        "The console shows only a digest and redacted metadata. Inspect the originating task before deciding.",
        preview.redactedFields,
        "preview",
        true,
      ),
    )
  }
  for (const marker of INJECTION_MARKERS) {
    const withheld = context.hazards?.injection.includes(marker.code) === true
    if (!marker.pattern.test(material) && !withheld) continue
    warnings.push(
      warning(
        `injection_${marker.code}`,
        "danger",
        "Possible prompt or tool injection",
        marker.detail,
        withheld
          ? [`${marker.code} detected in withheld canonical arguments`]
          : injectionEvidence(material, marker.pattern),
        "preview",
        true,
      ),
    )
  }
  for (const marker of SUPPLY_CHAIN_MARKERS) {
    const withheld =
      context.hazards?.supplyChain.includes(marker.code) === true
    if (!marker.pattern.test(material) && !withheld) continue
    warnings.push(
      warning(
        `supply_chain_${marker.code}`,
        "danger",
        "Supply-chain boundary change",
        marker.detail,
        withheld
          ? [`${marker.code} detected in withheld canonical arguments`]
          : injectionEvidence(material, marker.pattern),
        "binding",
        true,
      ),
    )
  }
  if (surface === "mcp") {
    warnings.push(
      warning(
        "mcp_remote_capability",
        risk === "low" ? "warning" : "danger",
        "MCP server capability",
        binding.serverId
          ? `This call is routed through MCP server ${binding.serverId}. Server identity does not prove tool safety.`
          : "This call is routed through an MCP server whose identifier is not projected.",
        [binding.serverId || "server_id_missing", binding.operation],
        "binding",
        risk !== "low",
      ),
    )
  }
  if (surface === "browser") {
    warnings.push(
      warning(
        "browser_external_content",
        risk === "high" ? "danger" : "warning",
        "Browser content is untrusted input",
        "Page text, downloads, redirects and accessibility labels can contain hostile instructions. The permission applies only to this exact action digest.",
        [binding.resourceUri, binding.operation].filter(Boolean),
        "runtime",
        true,
      ),
    )
  }
  if (surface === "terminal") {
    warnings.push(
      warning(
        "terminal_side_effect",
        risk === "high" ? "danger" : "warning",
        "Terminal action can change the host",
        "The projected command body is intentionally withheld. Confirm the task, tool call and arguments digest before allowing.",
        [binding.commandName, preview.digest].filter(Boolean),
        "runtime",
        true,
      ),
    )
  }
  if (surface === "plugin" || surface === "skill") {
    warnings.push(
      warning(
        "extension_code_boundary",
        "danger",
        "Executable extension boundary",
        "Plugins, hooks and skills may introduce code or instructions. An exact-call grant must not become a standing grant.",
        [
          binding.toolName,
          String(binding.pluginRevision ?? binding.skillRevision ?? ""),
        ].filter(Boolean),
        "binding",
        true,
      ),
    )
  }
  if (risk === "unknown") {
    warnings.push(
      warning(
        "unknown_risk",
        "danger",
        "Risk could not be classified deterministically",
        "Unknown actions fail closed in sealed mode and should be denied or replanned unless their capability is verified.",
        [binding.toolName, binding.namespace, binding.operation].filter(Boolean),
        "policy",
        true,
      ),
    )
  }
  if (context.policyFrozen && !context.sealed) {
    warnings.push(
      warning(
        "frozen_policy_interactive",
        "info",
        "Policy revision is frozen",
        "This response grants or denies one exact call. It cannot change the frozen policy.",
        [],
        "policy",
        true,
      ),
    )
  }
  if (context.sealed) {
    warnings.push(
      warning(
        "sealed_no_human",
        "danger",
        "Sealed autonomous run",
        "Human approve, deny, steer, retry and mode changes are prohibited. ASK resolves through deterministic deny/replan.",
        [],
        "policy",
        true,
      ),
    )
  }
  const remaining = Date.parse(context.expiresAt) - context.now.getTime()
  if (Number.isFinite(remaining) && remaining <= 0) {
    warnings.push(
      warning(
        "request_expired",
        "danger",
        "Request expired",
        "The backend will reject a late response. Refresh to observe the canonical timeout/default-deny outcome.",
        [context.expiresAt],
        "runtime",
        true,
      ),
    )
  } else if (Number.isFinite(remaining) && remaining < 30_000) {
    warnings.push(
      warning(
        "request_expiring",
        "warning",
        "Request expires soon",
        `The response challenge expires in ${Math.max(1, Math.ceil(remaining / 1_000))} seconds.`,
        [context.expiresAt],
        "runtime",
        true,
      ),
    )
  }
  return dedupeWarnings(warnings)
}

export function detectPermissionHazards(
  value: unknown,
): PermissionHazardSignals {
  let material = ""
  try {
    material = canonicalPermissionJson(value).slice(0, 256 * 1_024)
  } catch {
    material = ""
  }
  return Object.freeze({
    injection: Object.freeze(
      INJECTION_MARKERS
        .filter((marker) => marker.pattern.test(material))
        .map((marker) => marker.code),
    ),
    supplyChain: Object.freeze(
      SUPPLY_CHAIN_MARKERS
        .filter((marker) => marker.pattern.test(material))
        .map((marker) => marker.code),
    ),
  })
}

export function highestWarningSeverity(
  warnings: readonly PermissionWarning[],
): PermissionWarningSeverity {
  if (warnings.some((warning) => warning.severity === "danger")) return "danger"
  if (warnings.some((warning) => warning.severity === "warning")) return "warning"
  return "info"
}

export function warningsBlockPersistentGrant(
  warnings: readonly PermissionWarning[],
): boolean {
  return warnings.some((warning) => warning.blocksPersistentGrant)
}

function searchable(
  binding: PermissionRequestBinding,
  preview: PermissionArgumentPreview,
): string {
  return [
    binding.toolName,
    binding.namespace,
    binding.serverId,
    binding.operation,
    binding.commandName,
    binding.resourceUri,
    binding.workspaceRoot,
    preview.summary,
    ...preview.rows.map((row) => `${row.key} ${row.value}`),
  ]
    .filter(Boolean)
    .join("\n")
    .slice(0, 32_768)
}

function warning(
  code: string,
  severity: PermissionWarningSeverity,
  title: string,
  detail: string,
  evidence: readonly string[],
  source: PermissionWarning["source"],
  blocksPersistentGrant: boolean,
): PermissionWarning {
  return Object.freeze({
    code,
    severity,
    title,
    detail,
    evidence: Object.freeze(
      [...new Set(evidence.map((value) => safeEvidence(value)).filter(Boolean))],
    ),
    source,
    blocksPersistentGrant,
  })
}

function safeEvidence(value: string): string {
  const compact = String(value || "")
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
  if (!compact) return ""
  return compact.length > 180 ? `${compact.slice(0, 179)}…` : compact
}

function injectionEvidence(value: string, pattern: RegExp): string[] {
  const match = value.match(pattern)
  if (!match?.[0]) return []
  return [safeEvidence(match[0])]
}

function dedupeWarnings(
  warnings: readonly PermissionWarning[],
): PermissionWarning[] {
  const byCode = new Map<string, PermissionWarning>()
  for (const item of warnings) {
    const current = byCode.get(item.code)
    if (!current || severityRank(item.severity) > severityRank(current.severity)) {
      byCode.set(item.code, item)
    }
  }
  return [...byCode.values()].sort(
    (left, right) =>
      severityRank(right.severity) - severityRank(left.severity)
      || left.code.localeCompare(right.code),
  )
}

function severityRank(value: PermissionWarningSeverity): number {
  return value === "danger" ? 3 : value === "warning" ? 2 : 1
}
