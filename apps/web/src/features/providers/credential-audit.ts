import type {
  ModelRow,
  ProviderConsoleProjection,
  ProviderRow,
  ProviderRoute,
  ProviderRouteCandidate,
} from "./projection.ts"
import {
  compareNumber,
  compareText,
  fingerprint,
  record,
  secretKey,
  sortStable,
  text,
  unique,
} from "../session/value.ts"

export interface CredentialPresence {
  providerId: string
  modelId?: string
  present: boolean
  version?: number
  fingerprint?: string
  integrationIds: readonly string[]
  safe: boolean
  unsafePaths: readonly string[]
  warnings: readonly string[]
}

export interface CredentialRouteAdmission {
  id: string
  candidateId: string
  providerId: string
  modelId: string
  allowed: boolean
  reasons: readonly string[]
  warnings: readonly string[]
  credential: CredentialPresence
  routeCredentialVersion?: number
  routeCredentialFingerprint?: string
  versionMatched: boolean
  fingerprintMatched: boolean
}

export interface CredentialAuditReport {
  id: string
  taskId: string
  providers: readonly CredentialPresence[]
  models: readonly CredentialPresence[]
  admissions: readonly CredentialRouteAdmission[]
  selectedAdmission?: CredentialRouteAdmission
  safe: boolean
  presentCount: number
  absentCount: number
  unsafeCount: number
  versionMismatchCount: number
  fingerprintMismatchCount: number
  unsafePaths: readonly string[]
  warnings: readonly string[]
}

export class ProviderCredentialAuditor {
  readonly #reports = new Map<string, CredentialAuditReport>()
  #enabled = true
  #disabledReason = "Provider credential auditor is disabled."

  audit(projection: ProviderConsoleProjection): CredentialAuditReport {
    this.#assertEnabled()
    const providers = projection.providers.map((provider) =>
      providerPresence(provider))
    const models = projection.models.map((model) =>
      modelPresence(model, projection.providers))
    const providerById = new Map(providers.map((provider) => [provider.providerId, provider]))
    const modelById = new Map(models.map((model) => [
      `${model.providerId}\u001f${model.modelId}`,
      model,
    ]))
    const admissions = projection.candidates.map((candidate) =>
      routeAdmission(
        candidate,
        projection.selectedRoute,
        providerById.get(candidate.providerId),
        modelById.get(`${candidate.providerId}\u001f${candidate.modelId}`),
      ))
    const selectedAdmission = admissions.find((admission) =>
      projection.selectedRoute &&
      admission.providerId === projection.selectedRoute.providerId &&
      admission.modelId === projection.selectedRoute.modelId)
    const allPresence = [...providers, ...models]
    const unsafePaths = unique(allPresence.flatMap((presence) => presence.unsafePaths))
    const warnings = unique([
      ...allPresence.flatMap((presence) => presence.warnings),
      ...admissions.flatMap((admission) => admission.warnings),
      ...admissions.filter((admission) => !admission.allowed).flatMap((admission) =>
        admission.reasons.map((reason) => `${admission.providerId}/${admission.modelId}: ${reason}`)),
    ])
    if (!selectedAdmission && projection.selectedRoute) {
      warnings.push("selected route has no credential admission")
    }
    if (selectedAdmission && !selectedAdmission.allowed) {
      warnings.push("selected route failed credential admission")
    }
    const report: CredentialAuditReport = Object.freeze({
      id: fingerprint([
        projection.taskId,
        projection.revision,
        allPresence,
        admissions.map((admission) => [
          admission.id,
          admission.allowed,
          admission.versionMatched,
          admission.fingerprintMatched,
        ]),
      ]),
      taskId: projection.taskId,
      providers: Object.freeze(providers),
      models: Object.freeze(models),
      admissions: Object.freeze(sortStable(admissions, (left, right) =>
        Number(right.allowed) - Number(left.allowed) ||
        compareText(left.providerId, right.providerId) ||
        compareText(left.modelId, right.modelId))),
      selectedAdmission,
      safe:
        unsafePaths.length === 0 &&
        (!selectedAdmission || selectedAdmission.allowed),
      presentCount: allPresence.filter((presence) => presence.present).length,
      absentCount: allPresence.filter((presence) => !presence.present).length,
      unsafeCount: allPresence.filter((presence) => !presence.safe).length,
      versionMismatchCount: admissions.filter((admission) => !admission.versionMatched).length,
      fingerprintMismatchCount: admissions.filter((admission) => !admission.fingerprintMatched).length,
      unsafePaths: Object.freeze(unsafePaths),
      warnings: Object.freeze(warnings.sort()),
    })
    this.#reports.set(report.id, report)
    return report
  }

  get(reportId: string): CredentialAuditReport | undefined {
    this.#assertEnabled()
    return this.#reports.get(reportId)
  }

  list(): readonly CredentialAuditReport[] {
    this.#assertEnabled()
    return Object.freeze([...this.#reports.values()])
  }

  latest(taskId?: string): CredentialAuditReport | undefined {
    this.#assertEnabled()
    return [...this.#reports.values()]
      .reverse()
      .find((report) => !taskId || report.taskId === taskId)
  }

  assertSafe(report: CredentialAuditReport): void {
    this.#assertEnabled()
    if (report.unsafePaths.length) {
      throw new Error(`Provider projection exposes secret-like fields: ${report.unsafePaths.join(", ")}`)
    }
    if (report.selectedAdmission && !report.selectedAdmission.allowed) {
      throw new Error(`Selected route credential admission failed: ${report.selectedAdmission.reasons.join("; ")}`)
    }
  }

  disable(reason = "Provider credential auditor is disabled."): void {
    this.#enabled = false
    this.#disabledReason = text(reason, "Provider credential auditor is disabled.")
  }

  enable(): void {
    this.#enabled = true
  }

  clear(): void {
    this.#assertEnabled()
    this.#reports.clear()
  }

  #assertEnabled(): void {
    if (!this.#enabled) throw new Error(this.#disabledReason)
  }
}

export function scanSecretPaths(
  value: unknown,
  prefix = "",
  depth = 0,
): string[] {
  if (depth > 20) return []
  if (!value || typeof value !== "object") return []
  const paths: string[] = []
  if (Array.isArray(value)) {
    value.forEach((item, index) => {
      paths.push(...scanSecretPaths(item, `${prefix}[${index}]`, depth + 1))
    })
    return paths
  }
  for (const [key, item] of Object.entries(record(value))) {
    const path = prefix ? `${prefix}.${key}` : key
    if (secretKey(key)) {
      if (isCredentialPresenceMetadata(key, item)) continue
      if (item !== undefined && item !== null && item !== "" && item !== false) paths.push(path)
      continue
    }
    paths.push(...scanSecretPaths(item, path, depth + 1))
  }
  return paths
}

function isCredentialPresenceMetadata(key: string, value: unknown): boolean {
  const normalized = key.trim().toLowerCase().replaceAll("-", "_")
  const presenceValue =
    typeof value === "boolean" ||
    value === "[present]" ||
    value === "[absent]"
  return (
    presenceValue &&
    (
      normalized === "credential_present" ||
      normalized === "credentials_present" ||
      normalized === "has_credential" ||
      normalized === "has_credentials"
    )
  )
}

export function assertCredentialProjectionSafe(value: unknown): void {
  const unsafe = scanSecretPaths(value)
  if (unsafe.length) {
    throw new Error(`Credential projection contains secret-like value paths: ${unsafe.join(", ")}`)
  }
  const serialized = JSON.stringify(value)
  if (/bearer\s+[a-z0-9._~+/-]{8,}/i.test(serialized)) {
    throw new Error("Credential projection contains a bearer token.")
  }
  if (/-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/.test(serialized)) {
    throw new Error("Credential projection contains a private key.")
  }
  if (/(?:sk|pk|api)[_-][a-z0-9]{16,}/i.test(serialized)) {
    throw new Error("Credential projection contains an API-key-like value.")
  }
}

function providerPresence(provider: ProviderRow): CredentialPresence {
  const unsafePaths = scanSecretPaths(provider)
  const warnings: string[] = []
  if (!provider.credentialPresent) warnings.push(`${provider.id}: credential absent`)
  if (provider.credentialPresent && !provider.credentialFingerprint) {
    warnings.push(`${provider.id}: credential fingerprint unavailable`)
  }
  if (provider.secretLeakDetected) warnings.push(`${provider.id}: projection reported secret leak`)
  if (unsafePaths.length) warnings.push(`${provider.id}: secret-like values are present`)
  return Object.freeze({
    providerId: provider.id,
    present: provider.credentialPresent,
    version: provider.credentialVersion,
    fingerprint: provider.credentialFingerprint,
    integrationIds: Object.freeze([...provider.integrationIds]),
    safe: unsafePaths.length === 0 && !provider.secretLeakDetected,
    unsafePaths: Object.freeze(unsafePaths),
    warnings: Object.freeze(warnings),
  })
}

function modelPresence(
  model: ModelRow,
  providers: readonly ProviderRow[],
): CredentialPresence {
  const provider = providers.find((entry) => entry.id === model.providerId)
  const unsafePaths = scanSecretPaths(model)
  const present = model.credentialPresent || provider?.credentialPresent === true
  const warnings: string[] = []
  if (!present) warnings.push(`${model.providerId}/${model.id}: credential absent`)
  if (model.deprecated) warnings.push(`${model.providerId}/${model.id}: model deprecated`)
  if (!model.available) warnings.push(`${model.providerId}/${model.id}: model unavailable`)
  if (unsafePaths.length) warnings.push(`${model.providerId}/${model.id}: secret-like values are present`)
  return Object.freeze({
    providerId: model.providerId,
    modelId: model.id,
    present,
    version: provider?.credentialVersion,
    fingerprint: provider?.credentialFingerprint,
    integrationIds: Object.freeze(unique([
      ...model.integrationIds,
      ...(provider?.integrationIds ?? []),
    ])),
    safe: unsafePaths.length === 0,
    unsafePaths: Object.freeze(unsafePaths),
    warnings: Object.freeze(warnings),
  })
}

function routeAdmission(
  candidate: ProviderRouteCandidate,
  selectedRoute: ProviderRoute | undefined,
  provider: CredentialPresence | undefined,
  model: CredentialPresence | undefined,
): CredentialRouteAdmission {
  const credential = mergePresence(candidate.providerId, candidate.modelId, provider, model)
  const selected =
    selectedRoute?.providerId === candidate.providerId &&
    selectedRoute.modelId === candidate.modelId
  const reasons: string[] = []
  const warnings: string[] = []
  if (!credential.present) reasons.push("credential absent")
  if (!credential.safe) reasons.push("credential projection unsafe")
  if (!candidate.credentialPresent) reasons.push("route candidate reports credential absent")
  if (candidate.admission === "rejected" || candidate.admission === "failed") {
    reasons.push(`candidate ${candidate.admission}`)
  }
  if (candidate.missingCapabilities.length) reasons.push("candidate misses required capabilities")
  if (!candidate.quotaAvailable) reasons.push("candidate quota unavailable")
  const routeCredentialVersion = selected ? selectedRoute?.credentialVersion : undefined
  const routeCredentialFingerprint = selected ? selectedRoute?.credentialFingerprint : undefined
  const versionMatched =
    routeCredentialVersion === undefined ||
    credential.version === undefined ||
    routeCredentialVersion === credential.version
  const fingerprintMatched =
    !routeCredentialFingerprint ||
    !credential.fingerprint ||
    routeCredentialFingerprint === credential.fingerprint
  if (!versionMatched) reasons.push("credential version differs from selected route")
  if (!fingerprintMatched) reasons.push("credential fingerprint differs from selected route")
  if (credential.present && credential.integrationIds.length === 0) {
    warnings.push("credential has no integration identity")
  }
  if (credential.present && !credential.fingerprint) {
    warnings.push("credential fingerprint unavailable")
  }
  return Object.freeze({
    id: fingerprint([
      candidate.id,
      credential,
      routeCredentialVersion,
      routeCredentialFingerprint,
    ]),
    candidateId: candidate.id,
    providerId: candidate.providerId,
    modelId: candidate.modelId,
    allowed: reasons.length === 0,
    reasons: Object.freeze(unique(reasons)),
    warnings: Object.freeze(unique([...warnings, ...credential.warnings])),
    credential,
    routeCredentialVersion,
    routeCredentialFingerprint,
    versionMatched,
    fingerprintMatched,
  })
}

function mergePresence(
  providerId: string,
  modelId: string,
  provider: CredentialPresence | undefined,
  model: CredentialPresence | undefined,
): CredentialPresence {
  return Object.freeze({
    providerId,
    modelId,
    present: provider?.present === true || model?.present === true,
    version: model?.version ?? provider?.version,
    fingerprint: model?.fingerprint ?? provider?.fingerprint,
    integrationIds: Object.freeze(unique([
      ...(provider?.integrationIds ?? []),
      ...(model?.integrationIds ?? []),
    ])),
    safe: provider?.safe !== false && model?.safe !== false,
    unsafePaths: Object.freeze(unique([
      ...(provider?.unsafePaths ?? []),
      ...(model?.unsafePaths ?? []),
    ])),
    warnings: Object.freeze(unique([
      ...(provider?.warnings ?? []),
      ...(model?.warnings ?? []),
    ])),
  })
}
