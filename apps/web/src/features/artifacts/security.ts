import {
  artifactRangeKey,
  artifactRevisionKey,
  isActiveContentFamily,
  type ArtifactContract,
  type ArtifactPromptFinding,
  type ArtifactReadReceipt,
  type ArtifactReadResponse,
  type ArtifactRedactionFinding,
} from "./contracts.ts"

export interface ClientRedactionFinding {
  kind:
    | "credential"
    | "credential_assignment"
    | "private_key"
    | "connection_string"
    | "authorization_header"
  start: number
  end: number
  digest: string
  replacement: string
}

export interface ClientPromptFinding {
  kind:
    | "instruction_override"
    | "authority_impersonation"
    | "tool_invocation_request"
    | "secret_exfiltration_request"
    | "permission_bypass_request"
    | "encoded_instruction"
    | "remote_instruction"
  start: number
  end: number
  excerpt: string
  digest: string
  severity: "notice" | "warning" | "critical"
}

export interface ArtifactSafeText {
  text: string
  artifactId: string
  revision: string
  rangeKey: string
  clientRedacted: boolean
  serverRedacted: boolean
  quarantined: boolean
  redactions: readonly ClientRedactionFinding[]
  serverRedactions: readonly ArtifactRedactionFinding[]
  promptFindings: readonly ClientPromptFinding[]
  serverPromptFindings: readonly ArtifactPromptFinding[]
  transformations: readonly string[]
}

export interface ArtifactAdmission {
  allowed: boolean
  mode: "text" | "media" | "binary" | "metadata"
  quarantined: boolean
  reasons: readonly string[]
  artifactKey: string
  receiptKey: string
}

export interface SafeExternalLink {
  allowed: boolean
  href?: string
  display: string
  external: boolean
  reason?: string
}

interface Replacement {
  start: number
  end: number
  kind: ClientRedactionFinding["kind"]
  secret: string
}

const credentialPattern =
  /(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|passwd)\s*(?::|=|\s)\s*(?:bearer\s+)?([^\s,;"']{6,})/gim

const authorizationPattern =
  /\bauthorization\s*:\s*(?:bearer|basic)\s+([A-Za-z0-9+/_=.-]{6,})/gim

const assignmentPattern =
  /^(?:export\s+)?([A-Z][A-Z0-9_]{2,}(?:TOKEN|SECRET|PASSWORD|PASSWD|KEY))\s*=\s*(.+)$/gim

const privateKeyPattern =
  /-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----/g

const connectionStringPattern =
  /\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp):\/\/[^/\s:@]+:([^@\s/]{4,})@[^\s]+/gim

const promptRules: readonly {
  kind: ClientPromptFinding["kind"]
  expression: RegExp
  severity: ClientPromptFinding["severity"]
}[] = [
  {
    kind: "instruction_override",
    expression:
      /\b(?:ignore|disregard|forget|override|replace)\b.{0,64}\b(?:previous|prior|system|developer|instructions?|rules?)\b/gim,
    severity: "critical",
  },
  {
    kind: "authority_impersonation",
    expression:
      /^\s*(?:system|developer|assistant|tool)\s*(?:message|instruction|prompt)?\s*:/gim,
    severity: "critical",
  },
  {
    kind: "tool_invocation_request",
    expression:
      /\b(?:run|execute|call|invoke|use|launch)\b.{0,48}\b(?:tool|command|shell|terminal|powershell|bash|browser)\b/gim,
    severity: "warning",
  },
  {
    kind: "secret_exfiltration_request",
    expression:
      /\b(?:reveal|print|send|upload|exfiltrate|show|copy)\b.{0,64}\b(?:secret|token|credential|password|environment|ssh key)\b/gim,
    severity: "critical",
  },
  {
    kind: "permission_bypass_request",
    expression:
      /\b(?:bypass|disable|skip|ignore|circumvent)\b.{0,48}\b(?:permission|approval|policy|sandbox|guard|safety)\b/gim,
    severity: "critical",
  },
  {
    kind: "encoded_instruction",
    expression:
      /\b(?:base64|rot13|hex|unicode)\b.{0,48}\b(?:decode|instruction|execute|command)\b/gim,
    severity: "warning",
  },
  {
    kind: "remote_instruction",
    expression:
      /\b(?:fetch|open|visit|download)\b.{0,80}\bhttps?:\/\/[^\s)]+/gim,
    severity: "warning",
  },
]

const safeBitmapMedia = new Set([
  "image/avif",
  "image/gif",
  "image/jpeg",
  "image/png",
  "image/webp",
])

const safeAudioMedia = new Set([
  "audio/aac",
  "audio/flac",
  "audio/mpeg",
  "audio/ogg",
  "audio/wav",
])

const safeVideoMedia = new Set([
  "video/mp4",
  "video/ogg",
  "video/webm",
])

const safeLinkProtocols = new Set(["http:", "https:"])

export class ArtifactReceiptAdmission {
  readonly #highestSequence = new Map<string, number>()
  readonly #receipts = new Map<string, ArtifactReadReceipt>()
  readonly #rangeReceipts = new Map<string, string>()
  #disabled = false

  disable(): void {
    this.#disabled = true
    this.#highestSequence.clear()
    this.#receipts.clear()
    this.#rangeReceipts.clear()
  }

  admit(response: ArtifactReadResponse): ArtifactAdmission {
    if (this.#disabled) {
      throw new ArtifactSecurityError(
        "receipt_admission_disabled",
        "Artifact receipt admission is disabled.",
      )
    }
    const artifact = response.artifact
    const receipt = response.receipt
    const artifactKey = artifactRevisionKey(artifact)
    const receiptKey = `${receipt.artifactId}@${receipt.revision}:${receipt.receiptDigest}`
    if (receipt.decision !== "allow") {
      throw new ArtifactSecurityError(
        "receipt_denied",
        receipt.reason || "Artifact receipt denied content.",
      )
    }
    if (response.taskId !== receipt.taskId) {
      throw new ArtifactSecurityError(
        "receipt_task_mismatch",
        "Artifact receipt belongs to a different task.",
      )
    }
    if (artifact.artifactId !== receipt.artifactId) {
      throw new ArtifactSecurityError(
        "receipt_artifact_mismatch",
        "Artifact receipt belongs to a different artifact.",
      )
    }
    if (artifact.revision !== receipt.revision) {
      throw new ArtifactSecurityError(
        "receipt_revision_mismatch",
        "Artifact receipt belongs to a different revision.",
      )
    }
    if (receipt.sha256 && receipt.sha256 !== artifact.sha256) {
      throw new ArtifactSecurityError(
        "receipt_digest_mismatch",
        "Artifact receipt digest does not match the immutable revision.",
      )
    }
    if (artifact.status.integrity !== "verified") {
      throw new ArtifactSecurityError(
        "artifact_unverified",
        `Artifact integrity is ${artifact.status.integrity}.`,
      )
    }
    if (artifact.security.label === "secret") {
      throw new ArtifactSecurityError(
        "secret_content_refused",
        "Secret artifact content cannot enter the browser viewer.",
      )
    }
    if (
      !response.policy.allowInline
      || artifact.status.executableRisk
      || isActiveContentFamily(artifact.contentFamily)
    ) {
      throw new ArtifactSecurityError(
        "active_content_refused",
        "Executable or active artifact content cannot enter the browser viewer.",
      )
    }
    const highest = this.#highestSequence.get(artifactKey) ?? 0
    const existing = this.#receipts.get(receiptKey)
    if (existing) {
      if (!sameReceipt(existing, receipt)) {
        throw new ArtifactSecurityError(
          "receipt_replay_conflict",
          "Artifact receipt digest was replayed with different content.",
        )
      }
    } else if (receipt.sequence < highest) {
      throw new ArtifactSecurityError(
        "stale_receipt",
        "Artifact receipt sequence is older than admitted state.",
      )
    }
    if (response.range) {
      const rangeKey = artifactRangeKey(artifact, response.range)
      const admitted = this.#rangeReceipts.get(rangeKey)
      if (admitted && admitted !== receipt.receiptDigest) {
        throw new ArtifactSecurityError(
          "range_receipt_conflict",
          "The same immutable range has conflicting read receipts.",
        )
      }
      this.#rangeReceipts.set(rangeKey, receipt.receiptDigest)
    }
    this.#highestSequence.set(
      artifactKey,
      Math.max(highest, receipt.sequence),
    )
    this.#receipts.set(receiptKey, receipt)
    const reasons: string[] = []
    if (response.policy.quarantine) reasons.push("server trust quarantine")
    if (response.content?.promptFindings.length) {
      reasons.push("server prompt-injection finding")
    }
    if (artifact.security.trust !== "trusted") {
      reasons.push(`artifact trust is ${artifact.security.trust}`)
    }
    const mode = admissionMode(artifact)
    const quarantined =
      response.policy.quarantine
      || response.content?.quarantined === true
      || artifact.security.trust !== "trusted"
    return Object.freeze({
      allowed: true,
      mode,
      quarantined,
      reasons: Object.freeze(reasons),
      artifactKey,
      receiptKey,
    })
  }

  snapshot(): {
    artifacts: number
    receipts: number
    ranges: number
    disabled: boolean
  } {
    return Object.freeze({
      artifacts: this.#highestSequence.size,
      receipts: this.#receipts.size,
      ranges: this.#rangeReceipts.size,
      disabled: this.#disabled,
    })
  }
}

export class ArtifactSecurityError extends Error {
  readonly code: string

  constructor(code: string, message: string) {
    super(message)
    this.name = "ArtifactSecurityError"
    this.code = code
  }
}

export async function secureArtifactText(
  response: ArtifactReadResponse,
  admission: ArtifactAdmission,
): Promise<ArtifactSafeText> {
  const content = response.content
  const range = response.range
  if (!content || !range || content.text === undefined) {
    throw new ArtifactSecurityError(
      "text_content_required",
      "Artifact response does not contain admitted text.",
    )
  }
  if (!admission.allowed || admission.artifactKey !== artifactRevisionKey(response.artifact)) {
    throw new ArtifactSecurityError(
      "admission_identity_mismatch",
      "Artifact text does not match its security admission.",
    )
  }
  const redacted = await redactClientSecrets(content.text)
  const promptFindings = await scanClientPromptInjection(redacted.text)
  const transformations = new Set(response.receipt.transformations)
  if (redacted.findings.length) transformations.add("client_secret_redaction")
  if (promptFindings.length) transformations.add("client_prompt_quarantine")
  const quarantined =
    admission.quarantined
    || content.quarantined
    || promptFindings.length > 0
    || content.promptFindings.length > 0
  return Object.freeze({
    text: redacted.text,
    artifactId: response.artifact.artifactId,
    revision: response.artifact.revision,
    rangeKey: artifactRangeKey(response.artifact, range),
    clientRedacted: redacted.findings.length > 0,
    serverRedacted: content.serverRedacted,
    quarantined,
    redactions: Object.freeze(redacted.findings),
    serverRedactions: Object.freeze([...content.redactions]),
    promptFindings: Object.freeze(promptFindings),
    serverPromptFindings: Object.freeze([...content.promptFindings]),
    transformations: Object.freeze([...transformations].sort()),
  })
}

export async function redactClientSecrets(
  text: string,
): Promise<{ text: string; findings: ClientRedactionFinding[] }> {
  const replacements: Replacement[] = []
  collectCaptureReplacements(
    text,
    credentialPattern,
    1,
    "credential",
    replacements,
  )
  collectCaptureReplacements(
    text,
    authorizationPattern,
    1,
    "authorization_header",
    replacements,
  )
  collectCaptureReplacements(
    text,
    assignmentPattern,
    2,
    "credential_assignment",
    replacements,
  )
  collectCaptureReplacements(
    text,
    connectionStringPattern,
    1,
    "connection_string",
    replacements,
  )
  for (const match of text.matchAll(privateKeyPattern)) {
    if (match.index === undefined || !match[0]) continue
    replacements.push({
      start: match.index,
      end: match.index + match[0].length,
      kind: "private_key",
      secret: match[0],
    })
  }
  replacements.sort((left, right) => {
    if (left.start !== right.start) return left.start - right.start
    return right.end - left.end
  })
  const nonOverlapping: Replacement[] = []
  let boundary = -1
  for (const replacement of replacements) {
    if (replacement.start < boundary) continue
    nonOverlapping.push(replacement)
    boundary = replacement.end
    if (nonOverlapping.length >= 512) break
  }
  const findings: ClientRedactionFinding[] = []
  const fragments: string[] = []
  let cursor = 0
  for (const replacement of nonOverlapping) {
    fragments.push(text.slice(cursor, replacement.start))
    const digest = await sha256Text(replacement.secret)
    const marker =
      replacement.kind === "private_key"
        ? `[CLIENT_REDACTED_PRIVATE_KEY:${digest.slice(0, 12)}]`
        : `[CLIENT_REDACTED:${digest.slice(0, 12)}]`
    fragments.push(marker)
    findings.push(
      Object.freeze({
        kind: replacement.kind,
        start: replacement.start,
        end: replacement.end,
        digest,
        replacement: marker,
      }),
    )
    cursor = replacement.end
  }
  fragments.push(text.slice(cursor))
  return {
    text: fragments.join(""),
    findings,
  }
}

export async function scanClientPromptInjection(
  text: string,
): Promise<ClientPromptFinding[]> {
  const findings: ClientPromptFinding[] = []
  for (const rule of promptRules) {
    rule.expression.lastIndex = 0
    for (const match of text.matchAll(rule.expression)) {
      if (match.index === undefined || !match[0]) continue
      const excerpt = boundedExcerpt(text, match.index, match[0].length)
      findings.push(
        Object.freeze({
          kind: rule.kind,
          start: match.index,
          end: match.index + match[0].length,
          excerpt,
          digest: await sha256Text(match[0]),
          severity: rule.severity,
        }),
      )
      if (findings.length >= 256) break
    }
    if (findings.length >= 256) break
  }
  findings.sort((left, right) => {
    if (left.start !== right.start) return left.start - right.start
    if (left.end !== right.end) return left.end - right.end
    return left.kind.localeCompare(right.kind)
  })
  return deduplicatePromptFindings(findings)
}

export function safeExternalLink(
  value: string,
  options: {
    baseUrl?: string
    maximumBytes?: number
  } = {},
): SafeExternalLink {
  const display = String(value || "").trim()
  const maximum = options.maximumBytes ?? 4096
  if (!display) {
    return Object.freeze({
      allowed: false,
      display,
      external: false,
      reason: "empty_link",
    })
  }
  if (new TextEncoder().encode(display).byteLength > maximum) {
    return Object.freeze({
      allowed: false,
      display: display.slice(0, maximum),
      external: false,
      reason: "link_too_large",
    })
  }
  if (/[\r\n\u0000]/.test(display)) {
    return Object.freeze({
      allowed: false,
      display,
      external: false,
      reason: "link_control_character",
    })
  }
  let url: URL
  try {
    url = new URL(display, options.baseUrl ?? "https://artifact.invalid/")
  } catch {
    return Object.freeze({
      allowed: false,
      display,
      external: false,
      reason: "link_invalid",
    })
  }
  if (!safeLinkProtocols.has(url.protocol)) {
    return Object.freeze({
      allowed: false,
      display,
      external: false,
      reason: "link_protocol_refused",
    })
  }
  url.username = ""
  url.password = ""
  const base = options.baseUrl ? new URL(options.baseUrl) : undefined
  const external = !base || url.origin !== base.origin
  return Object.freeze({
    allowed: true,
    href: url.toString(),
    display,
    external,
  })
}

export function assertArtifactMediaSafe(artifact: ArtifactContract): void {
  if (artifact.security.label === "secret") {
    throw new ArtifactSecurityError(
      "secret_media_refused",
      "Secret media cannot enter the browser.",
    )
  }
  if (artifact.status.integrity !== "verified") {
    throw new ArtifactSecurityError(
      "unverified_media_refused",
      "Media must pass artifact integrity verification.",
    )
  }
  if (artifact.status.executableRisk || isActiveContentFamily(artifact.contentFamily)) {
    throw new ArtifactSecurityError(
      "active_media_refused",
      "Active or executable artifact content cannot be rendered.",
    )
  }
  const allowed =
    safeBitmapMedia.has(artifact.mediaType)
    || safeAudioMedia.has(artifact.mediaType)
    || safeVideoMedia.has(artifact.mediaType)
  if (!allowed) {
    throw new ArtifactSecurityError(
      "media_type_refused",
      `Media type ${artifact.mediaType} is not allowlisted.`,
    )
  }
}

export function downloadDecision(
  artifact: ArtifactContract,
): {
  allowed: boolean
  confirmationRequired: boolean
  reasons: readonly string[]
} {
  const reasons: string[] = []
  if (artifact.security.label === "secret") reasons.push("secret artifact")
  if (artifact.security.downloadPolicy === "deny") reasons.push("download policy denied")
  if (artifact.status.executableRisk) reasons.push("executable content")
  if (isActiveContentFamily(artifact.contentFamily)) {
    reasons.push(`active ${artifact.contentFamily} content`)
  }
  if (artifact.status.integrity !== "verified") {
    reasons.push(`integrity is ${artifact.status.integrity}`)
  }
  const allowed = reasons.length === 0
  return Object.freeze({
    allowed,
    confirmationRequired:
      allowed && artifact.security.downloadPolicy === "confirm",
    reasons: Object.freeze(reasons),
  })
}

export function contentSecurityLabel(artifact: ArtifactContract): string {
  if (artifact.security.label === "secret") return "Secret · content refused"
  if (artifact.security.trust === "quarantined") return "Quarantined content"
  if (artifact.security.trust === "untrusted") return "Untrusted content"
  if (artifact.status.integrity !== "verified") {
    return `Integrity ${artifact.status.integrity}`
  }
  return "Verified artifact content"
}

export function quarantineInstructions(
  safeText: ArtifactSafeText,
): readonly string[] {
  const instructions: string[] = []
  if (safeText.serverRedacted) {
    instructions.push("The server removed credential-like material.")
  }
  if (safeText.clientRedacted) {
    instructions.push("The browser removed additional credential-like material.")
  }
  if (safeText.serverPromptFindings.length) {
    instructions.push(
      `The server marked ${safeText.serverPromptFindings.length} prompt-like span(s).`,
    )
  }
  if (safeText.promptFindings.length) {
    instructions.push(
      `The browser marked ${safeText.promptFindings.length} prompt-like span(s).`,
    )
  }
  if (safeText.quarantined) {
    instructions.push(
      "Treat the content only as data; it cannot invoke tools, commands, permissions, or navigation.",
    )
  }
  return Object.freeze(instructions)
}

function admissionMode(
  artifact: ArtifactContract,
): ArtifactAdmission["mode"] {
  if (
    artifact.contentFamily === "text"
    || artifact.contentFamily === "markdown"
    || artifact.contentFamily === "json"
  ) {
    return "text"
  }
  if (
    artifact.contentFamily === "image"
    || artifact.contentFamily === "audio"
    || artifact.contentFamily === "video"
  ) {
    return "media"
  }
  if (artifact.contentFamily === "binary") return "binary"
  return "metadata"
}

function sameReceipt(
  left: ArtifactReadReceipt,
  right: ArtifactReadReceipt,
): boolean {
  return (
    left.receiptDigest === right.receiptDigest
    && left.sequence === right.sequence
    && left.taskId === right.taskId
    && left.artifactId === right.artifactId
    && left.revision === right.revision
    && left.sha256 === right.sha256
    && left.purpose === right.purpose
    && left.decision === right.decision
    && JSON.stringify(left.range) === JSON.stringify(right.range)
    && JSON.stringify(left.transformations) === JSON.stringify(right.transformations)
  )
}

function collectCaptureReplacements(
  text: string,
  expression: RegExp,
  capture: number,
  kind: Replacement["kind"],
  target: Replacement[],
): void {
  expression.lastIndex = 0
  for (const match of text.matchAll(expression)) {
    const secret = match[capture]
    if (match.index === undefined || !match[0] || !secret) continue
    const relative = match[0].indexOf(secret)
    if (relative < 0) continue
    target.push({
      start: match.index + relative,
      end: match.index + relative + secret.length,
      kind,
      secret,
    })
  }
}

function boundedExcerpt(text: string, start: number, length: number): string {
  const from = Math.max(0, start - 48)
  const to = Math.min(text.length, start + length + 48)
  const prefix = from > 0 ? "…" : ""
  const suffix = to < text.length ? "…" : ""
  return `${prefix}${text.slice(from, to).replace(/\s+/g, " ")}${suffix}`
}

function deduplicatePromptFindings(
  findings: readonly ClientPromptFinding[],
): ClientPromptFinding[] {
  const result: ClientPromptFinding[] = []
  const seen = new Set<string>()
  for (const finding of findings) {
    const key = `${finding.kind}:${finding.start}:${finding.end}:${finding.digest}`
    if (seen.has(key)) continue
    seen.add(key)
    result.push(finding)
  }
  return result
}

async function sha256Text(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value)
  const subtle = globalThis.crypto?.subtle
  if (subtle) {
    const digest = await subtle.digest("SHA-256", bytes)
    return [...new Uint8Array(digest)]
      .map((item) => item.toString(16).padStart(2, "0"))
      .join("")
  }
  return deterministicFallbackHash(bytes)
}

function deterministicFallbackHash(bytes: Uint8Array): string {
  let left = 0x811c9dc5
  let right = 0x9e3779b9
  let third = 0x85ebca6b
  let fourth = 0xc2b2ae35
  for (let index = 0; index < bytes.length; index += 1) {
    const value = bytes[index]!
    left = Math.imul(left ^ value, 0x01000193)
    right = Math.imul(right ^ (value + index), 0x27d4eb2d)
    third = Math.imul(third ^ (value << (index % 8)), 0x165667b1)
    fourth = Math.imul(fourth ^ (value + left), 0x9e3779b1)
  }
  const chunks = [
    left,
    right,
    third,
    fourth,
    left ^ third,
    right ^ fourth,
    left ^ right ^ third,
    fourth ^ third,
  ]
  return chunks
    .map((value) => (value >>> 0).toString(16).padStart(8, "0"))
    .join("")
}
