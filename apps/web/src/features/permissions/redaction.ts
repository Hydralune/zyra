import {
  canonicalPermissionJson,
  optionalPermissionRecord,
  secretFieldName,
  sha256Digest,
} from "./canonical.ts"
import type {
  PermissionArgumentPreview,
  PermissionArgumentRow,
} from "./contracts.ts"

const MAX_ROWS = 24
const MAX_VALUE_BYTES = 1_024
const MAX_SUMMARY_BYTES = 2_048
const SECRET_VALUE =
  /(?:bearer\s+[a-z0-9._~+/=-]{8,}|(?:api|access|private|secret)[_-]?key\s*[:=]\s*\S+|password\s*[:=]\s*\S+)/i
const PEM_BLOCK = /-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----/i
const JWT_VALUE = /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/
const HIGH_ENTROPY_TOKEN = /\b[A-Za-z0-9+/=_-]{40,}\b/

export function buildPermissionArgumentPreview(
  bindingValue: unknown,
  argumentsDigestValue: unknown,
): PermissionArgumentPreview {
  const binding = optionalPermissionRecord(bindingValue)
  const argumentsDigest = sha256Digest(
    argumentsDigestValue,
    "permission arguments digest",
  )
  const rows: PermissionArgumentRow[] = []
  const redactedFields = new Set<string>()
  const omittedFields = new Set<string>()
  let secretCount = 0
  let byteCount = 0
  const preferred = [
    "tool_name",
    "namespace",
    "server_id",
    "operation",
    "command_name",
    "resource_uri",
    "workspace_root",
    "schema_digest",
    "capability_revision",
    "plugin_revision",
    "skill_revision",
  ]
  const keys = [
    ...preferred.filter((key) => key in binding),
    ...Object.keys(binding)
      .filter((key) => !preferred.includes(key))
      .sort(),
  ]
  for (const key of keys) {
    if (rows.length >= MAX_ROWS) {
      omittedFields.add(key)
      continue
    }
    const raw = binding[key]
    const row = previewRow(key, raw)
    byteCount += row.sourceBytes
    if (row.redacted) {
      redactedFields.add(key)
      secretCount += 1
    }
    if (row.omitted) omittedFields.add(key)
    if (row.row) rows.push(row.row)
  }
  const summarySource = [
    redactPermissionText(binding.tool_name, {
      key: "tool_name",
      kind: "text",
    }).value,
    redactPermissionText(binding.operation, {
      key: "operation",
      kind: "text",
    }).value,
    redactPermissionText(binding.resource_uri, {
      key: "resource_uri",
      kind: "url",
    }).value,
    redactPermissionText(binding.command_name, {
      key: "command_name",
      kind: "command",
    }).value,
  ]
    .filter(Boolean)
    .join(" · ")
  const summary = truncateText(
    summarySource || "Arguments withheld by the canonical permission runtime",
    MAX_SUMMARY_BYTES,
  ).value
  return Object.freeze({
    summary,
    rows: Object.freeze(rows),
    redactedFields: Object.freeze([...redactedFields].sort()),
    omittedFields: Object.freeze([...omittedFields].sort()),
    secretCount,
    byteCount,
    digest: argumentsDigest,
  })
}

export function redactPermissionText(
  value: unknown,
  options: {
    key?: string
    kind?: PermissionArgumentRow["kind"]
    maximumBytes?: number
  } = {},
): {
  value: string
  redacted: boolean
  truncated: boolean
  reason?: string
} {
  const key = String(options.key ?? "")
  const maximumBytes = Math.max(16, options.maximumBytes ?? MAX_VALUE_BYTES)
  if (secretFieldName(key)) {
    return {
      value: "[redacted:secret-field]",
      redacted: true,
      truncated: false,
      reason: "secret_field_name",
    }
  }
  const rendered = render(value)
  if (
    SECRET_VALUE.test(rendered)
    || PEM_BLOCK.test(rendered)
    || JWT_VALUE.test(rendered)
  ) {
    return {
      value: "[redacted:secret-pattern]",
      redacted: true,
      truncated: false,
      reason: "secret_value_pattern",
    }
  }
  if (
    HIGH_ENTROPY_TOKEN.test(rendered)
    && !/^[0-9a-f]{64}$/i.test(rendered.trim())
  ) {
    return {
      value: replaceHighEntropy(rendered),
      redacted: true,
      truncated: false,
      reason: "high_entropy_token",
    }
  }
  const kind = options.kind ?? inferRowKind(key, rendered)
  const sanitized =
    kind === "url"
      ? redactUrl(rendered)
      : kind === "path"
        ? redactPath(rendered)
        : sanitizeControls(rendered)
  const safeSanitized =
    typeof sanitized === "string"
      ? { value: sanitized, redacted: false, reason: undefined }
      : sanitized
  const truncated = truncateText(safeSanitized.value, maximumBytes)
  return {
    value: truncated.value,
    redacted: safeSanitized.redacted,
    truncated: truncated.truncated,
    reason: safeSanitized.reason,
  }
}

export function permissionPreviewContainsSecret(
  preview: PermissionArgumentPreview,
): boolean {
  if (preview.secretCount > 0 || preview.redactedFields.length > 0) return true
  return preview.rows.some(
    (row) =>
      row.sensitive
      || SECRET_VALUE.test(row.value)
      || PEM_BLOCK.test(row.value)
      || JWT_VALUE.test(row.value),
  )
}

export function assertPermissionPreviewSafe(
  preview: PermissionArgumentPreview,
): void {
  for (const row of preview.rows) {
    if (
      SECRET_VALUE.test(row.value)
      || PEM_BLOCK.test(row.value)
      || JWT_VALUE.test(row.value)
    ) {
      throw new Error(
        `Permission preview leaked a secret-shaped value in ${row.key}.`,
      )
    }
  }
  if (!/^[0-9a-f]{64}$/.test(preview.digest)) {
    throw new Error("Permission preview omitted the canonical arguments digest.")
  }
}

function previewRow(
  key: string,
  raw: unknown,
): {
  row?: PermissionArgumentRow
  redacted: boolean
  omitted: boolean
  sourceBytes: number
} {
  const source = render(raw)
  const sourceBytes = new TextEncoder().encode(source).byteLength
  if (raw === undefined) {
    return { redacted: false, omitted: true, sourceBytes }
  }
  if (isArgumentsKey(key)) {
    return {
      row: Object.freeze({
        key,
        label: labelForKey(key),
        value: "[omitted:canonical-runtime-does-not-project-arguments]",
        kind: "omitted",
        sensitive: true,
        truncated: false,
      }),
      redacted: false,
      omitted: true,
      sourceBytes,
    }
  }
  const kind = inferRowKind(key, source)
  const safe = redactPermissionText(raw, { key, kind })
  return {
    row: Object.freeze({
      key,
      label: labelForKey(key),
      value: safe.value,
      kind: safe.redacted ? "redacted" : kind,
      sensitive: safe.redacted || secretFieldName(key),
      truncated: safe.truncated,
    }),
    redacted: safe.redacted,
    omitted: false,
    sourceBytes,
  }
}

function inferRowKind(
  key: string,
  value: string,
): PermissionArgumentRow["kind"] {
  if (secretFieldName(key)) return "redacted"
  if (/path|workspace|directory|file|cwd/i.test(key)) return "path"
  if (/url|uri|origin|endpoint|host/i.test(key)) return "url"
  if (/command|script|shell|code/i.test(key)) return "command"
  if (/count|size|length|revision|version|attempt/i.test(key)) return "count"
  if (/^(?:true|false)$/i.test(value) || /enabled|allowed|active/i.test(key)) {
    return "boolean"
  }
  if (/id$|_id$|digest|fingerprint|nonce|hash/i.test(key)) return "identifier"
  return "text"
}

function redactUrl(value: string): {
  value: string
  redacted: boolean
  reason?: string
} {
  try {
    const parsed = new URL(value)
    let redacted = false
    if (parsed.username || parsed.password) {
      parsed.username = ""
      parsed.password = ""
      redacted = true
    }
    const sensitiveKeys: string[] = []
    for (const key of parsed.searchParams.keys()) {
      if (secretFieldName(key)) sensitiveKeys.push(key)
    }
    for (const key of sensitiveKeys) {
      parsed.searchParams.set(key, "[redacted]")
      redacted = true
    }
    parsed.hash = ""
    return {
      value: parsed.toString(),
      redacted,
      reason: redacted ? "url_credentials_or_secret_query" : undefined,
    }
  } catch {
    return {
      value: sanitizeControls(value),
      redacted: false,
    }
  }
}

function redactPath(value: string): {
  value: string
  redacted: boolean
  reason?: string
} {
  const clean = sanitizeControls(value)
  const homePatterns = [
    /^([A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/])([^\\/]+)([\\/])/i,
    /^(\/(?:home|Users)\/)([^/]+)(\/)/,
  ]
  for (const pattern of homePatterns) {
    if (pattern.test(clean)) {
      return {
        value: clean.replace(pattern, "$1[user]$3"),
        redacted: true,
        reason: "home_directory_identity",
      }
    }
  }
  return { value: clean, redacted: false }
}

function replaceHighEntropy(value: string): string {
  return value.replace(HIGH_ENTROPY_TOKEN, (match) => {
    const prefix = /^[0-9a-f]+$/i.test(match) ? "digest" : "token"
    return `[redacted:${prefix}:${match.length}]`
  })
}

function truncateText(
  value: string,
  maximumBytes: number,
): { value: string; truncated: boolean } {
  const encoded = new TextEncoder().encode(value)
  if (encoded.byteLength <= maximumBytes) {
    return { value, truncated: false }
  }
  let selected = value
  while (
    selected
    && new TextEncoder().encode(`${selected}…`).byteLength > maximumBytes
  ) {
    selected = selected.slice(0, Math.max(0, selected.length - 16))
  }
  return { value: `${selected}…`, truncated: true }
}

function render(value: unknown): string {
  if (value === undefined) return ""
  if (value === null) return "null"
  if (typeof value === "string") return value
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value)
  }
  try {
    return canonicalPermissionJson(value)
  } catch {
    return "[unrenderable]"
  }
}

function sanitizeControls(value: string): string {
  return value
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "�")
    .replace(/\r\n?/g, "\n")
}

function isArgumentsKey(key: string): boolean {
  return /^(?:arguments?|input|payload|body|content|code|script)$/i.test(key)
}

function labelForKey(key: string): string {
  return key
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase())
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : ""
}
