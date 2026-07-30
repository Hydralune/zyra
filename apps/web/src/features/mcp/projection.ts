import type {
  CanonicalProjectionState,
  CausalEventProjection,
  ProjectionEntity,
  ProjectionSelector,
  ToolProjection,
} from "../../state/contracts.ts"
import { projectionSelector } from "../../state/selectors.ts"
import type {
  McpAuthProjection,
  McpCapabilitySummary,
  McpCatalogEntry,
  McpCatalogKind,
  McpConfigProvenance,
  McpConfigSource,
  McpConnectionState,
  McpConsoleProjection,
  McpElicitationProjection,
  McpProjectionAuthority,
  McpRejectedProjection,
  McpServerProjection,
  McpTransportKind,
} from "./contracts.ts"
import {
  assertSecretSafe,
  booleanFrom,
  clamp,
  collectSecretPresence,
  compareNumber,
  compareText,
  fingerprint,
  integerFrom,
  isoDate,
  listFrom,
  merge,
  object,
  safeDescription,
  safeLabel,
  scalar,
  secretPresent,
  textFrom,
  textList,
  unique,
} from "./value.ts"

const AUTHORITATIVE = new Set(["authoritative", "final"])
const MCP_OWNER = /(?:^|[-_.:/])(mcp|claude-runtime-mcp|typescript-mcp|python-mcp-store)(?:$|[-_.:/])/i
const MCP_SCHEMA = new Set([
  "zyra.mcp-server/v1",
  "zyra.mcp-connection/v1",
  "zyra.mcp-capability/v1",
  "zyra.mcp-catalog/v1",
  "zyra.mcp-auth/v1",
  "zyra.mcp-elicitation/v1",
])
const MCP_ENTITY_KEYS = [
  "mcp",
  "mcp_server",
  "mcpServer",
  "mcp_connection",
  "mcpConnection",
] as const
const MCP_SERVER_LIST_KEYS = [
  "mcp_servers",
  "mcpServers",
  "servers",
] as const

interface Candidate {
  fact: Record<string, unknown>
  entity: ProjectionEntity
  serverId?: string
  source: string
}

interface AcceptedCandidate extends Candidate {
  serverId: string
  ownerId: string
}

interface ServerAccumulator {
  serverId: string
  ownerId: string
  entities: ProjectionEntity[]
  facts: Record<string, unknown>[]
  tools: McpCatalogEntry[]
  resources: McpCatalogEntry[]
  prompts: McpCatalogEntry[]
  elicitations: McpElicitationProjection[]
  eventIds: Set<string>
  warnings: Set<string>
}

export interface McpProjectionOptions {
  selectedServerId?: string
  nowMs?: number
  allowDisconnectedSnapshot?: boolean
}

export function selectMcpConsole(
  taskId: string,
  options: McpProjectionOptions = {},
): ProjectionSelector<McpConsoleProjection> {
  return projectionSelector(
    `mcp-console:${taskId}:${options.selectedServerId ?? "*"}:${options.allowDisconnectedSnapshot === true}`,
    [
      `task:${taskId}`,
      `cursor:${taskId}`,
      "domain:task",
      "domain:session",
      "domain:node",
      "domain:worker",
      "domain:tool",
      "domain:command",
      "domain:permission",
      "domain:event",
    ],
    (state) => buildMcpConsoleProjection(state, taskId, options),
    sameProjection,
  )
}

export function buildMcpConsoleProjection(
  state: CanonicalProjectionState,
  taskId: string,
  options: McpProjectionOptions = {},
): McpConsoleProjection {
  const nowMs = options.nowMs ?? state.committedAtMs
  const task = state.tasks[taskId]
  const cursor = state.cursors[taskId]
  const runtime = state.runtimes[taskId]
  const sessionId = chooseSessionId(state, taskId, task?.sessionId)
  const authority: McpProjectionAuthority = Object.freeze({
    taskId,
    runId: task?.runId ?? "",
    sessionId,
    ownerId: mcpAuthorityOwner(task),
    projectionRevision: state.revision,
    committedSequence: cursor?.committedSequence ?? 0,
    generation: cursor?.generation ?? runtime?.generation ?? 0,
    connected: runtime?.connected ?? false,
    snapshotComplete: cursor?.snapshotComplete ?? false,
  })
  const rejected: McpRejectedProjection[] = []
  const warnings: string[] = []
  if (!task) warnings.push("task_missing")
  if (task && !AUTHORITATIVE.has(task.status)) warnings.push("task_projection_not_authoritative")
  if (!authority.runId) warnings.push("run_identity_missing")
  if (!authority.sessionId) warnings.push("session_identity_missing")
  if (!cursor) warnings.push("projection_cursor_missing")
  else if (!cursor.snapshotComplete) warnings.push("projection_snapshot_incomplete")
  if (!runtime?.connected) warnings.push("projection_transport_disconnected")
  const candidates = collectCandidates(state, taskId)
  const accepted: AcceptedCandidate[] = []
  for (const candidate of candidates) {
    const decision = admitCandidate(candidate, authority)
    if ("code" in decision) {
      rejected.push(Object.freeze(decision))
      continue
    }
    accepted.push(decision)
  }
  const accumulators = new Map<string, ServerAccumulator>()
  for (const candidate of accepted) {
    const accumulator = ensureAccumulator(accumulators, candidate.serverId, candidate.ownerId)
    accumulator.entities.push(candidate.entity)
    accumulator.facts.push(candidate.fact)
    accumulator.eventIds.add(candidate.entity.lastEventId)
    accumulator.eventIds.add(candidate.entity.firstEventId)
    const catalog = catalogFacts(candidate.fact, candidate.entity, candidate.serverId)
    for (const item of catalog) {
      const target = item.kind === "tool"
        ? accumulator.tools
        : item.kind === "resource"
          ? accumulator.resources
          : accumulator.prompts
      upsertCatalog(target, item)
      if (item.eventId) accumulator.eventIds.add(item.eventId)
    }
    for (const elicitation of elicitationFacts(
      candidate.fact,
      candidate.entity,
      candidate.serverId,
      candidate.ownerId,
      nowMs,
    )) {
      upsertElicitation(accumulator.elicitations, elicitation)
      if (elicitation.settledEventId) accumulator.eventIds.add(elicitation.settledEventId)
    }
  }
  for (const tool of Object.values(state.tools)) {
    if (tool.taskId !== taskId || !tool.effective || !AUTHORITATIVE.has(tool.status)) continue
    const parsed = catalogFromToolProjection(tool)
    if (!parsed) continue
    const ownerId = textFrom(
      merge(tool.attributes, tool.metadata),
      ["mcp_owner_id", "mcpOwnerId", "owner_id", "ownerId"],
      "claude-runtime-mcp",
    )
    if (!MCP_OWNER.test(ownerId)) {
      rejected.push(Object.freeze({
        subject: tool.id,
        code: "owner_not_mcp",
        reason: "MCP tool projection does not identify an admitted MCP owner.",
        entityId: tool.id,
        eventId: tool.lastEventId,
      }))
      continue
    }
    const accumulator = ensureAccumulator(accumulators, parsed.serverId, ownerId)
    accumulator.entities.push(tool)
    accumulator.eventIds.add(tool.lastEventId)
    upsertCatalog(accumulator.tools, parsed.entry)
    if (parsed.entry.eventId) accumulator.eventIds.add(parsed.entry.eventId)
  }
  const servers = [...accumulators.values()]
    .map((accumulator) => finalizeServer(state, authority, accumulator, nowMs))
    .filter((server) => {
      try {
        assertSecretSafe(server)
        return true
      } catch (error) {
        rejected.push(Object.freeze({
          subject: server.id,
          code: "secret_projection_rejected",
          reason: error instanceof Error ? error.message : String(error),
        }))
        return false
      }
    })
    .sort(compareServer)
  const byId: Record<string, McpServerProjection> = {}
  for (const server of servers) byId[server.id] = server
  const requested = options.selectedServerId
  const selectedId =
    requested && byId[requested]
      ? requested
      : servers.find((server) => server.state === "needs-auth")?.id ??
        servers.find((server) => server.state === "failed")?.id ??
        servers[0]?.id
  if (requested && !byId[requested]) warnings.push("selected_server_missing")
  const projection = {
    authority,
    servers,
    byId,
    selectedId,
    selected: selectedId ? byId[selectedId] : undefined,
    pendingElicitations: servers.reduce(
      (count, server) =>
        count +
        server.elicitations.filter((item) =>
          ["pending", "permission-pending", "submitted"].includes(item.state)).length,
      0,
    ),
    needsAuth: servers.filter((server) =>
      server.state === "needs-auth" ||
      ["missing", "expired", "failed"].includes(server.auth.state)).length,
    connectedServers: servers.filter((server) => server.state === "connected").length,
    rejected: Object.freeze(sortRejected(rejected)),
    warnings: Object.freeze(unique(warnings)),
  }
  return Object.freeze({
    ...projection,
    servers: Object.freeze(servers),
    byId: Object.freeze(byId),
    fingerprint: fingerprint([
      authority,
      servers.map((server) => server.fingerprint),
      projection.rejected,
      projection.warnings,
    ]),
  })
}

function collectCandidates(
  state: CanonicalProjectionState,
  taskId: string,
): Candidate[] {
  const entities: ProjectionEntity[] = [
    ...Object.values(state.tasks),
    ...Object.values(state.sessions),
    ...Object.values(state.nodes),
    ...Object.values(state.workers),
    ...Object.values(state.tools),
    ...Object.values(state.commands),
    ...Object.values(state.permissions),
    ...Object.values(state.recoveries),
  ].filter((entity) => entity.taskId === taskId)
  const candidates: Candidate[] = []
  for (const entity of entities) {
    const combined = merge(entity.attributes, entity.metadata)
    for (const key of MCP_ENTITY_KEYS) {
      const fact = object(combined[key])
      if (Object.keys(fact).length) {
        candidates.push({
          fact,
          entity,
          serverId: serverIdentity(fact, combined),
          source: key,
        })
      }
    }
    for (const key of MCP_SERVER_LIST_KEYS) {
      const value = combined[key]
      if (!Array.isArray(value)) continue
      for (const item of value.slice(0, 1000)) {
        const fact = object(item)
        if (!Object.keys(fact).length) continue
        candidates.push({
          fact,
          entity,
          serverId: serverIdentity(fact, combined),
          source: key,
        })
      }
    }
    const schema = textFrom(combined, ["schema", "mcp_schema", "mcpSchema"])
    if (MCP_SCHEMA.has(schema)) {
      candidates.push({
        fact: combined,
        entity,
        serverId: serverIdentity(combined, combined),
        source: "entity-schema",
      })
    }
  }
  return candidates
}

function admitCandidate(
  candidate: Candidate,
  authority: McpProjectionAuthority,
): AcceptedCandidate | McpRejectedProjection {
  const reject = (
    code: string,
    reason: string,
  ): McpRejectedProjection => Object.freeze({
    subject: candidate.serverId ?? candidate.entity.id,
    code,
    reason,
    entityId: candidate.entity.id,
    eventId: candidate.entity.lastEventId,
  })
  if (!candidate.entity.effective) {
    return reject("projection_not_effective", "MCP fact is attached to a non-effective projection.")
  }
  if (!AUTHORITATIVE.has(candidate.entity.status)) {
    return reject("projection_not_authoritative", "MCP fact is not attached to an authoritative projection.")
  }
  if (candidate.entity.taskId !== authority.taskId) {
    return reject("task_mismatch", "MCP fact belongs to another task.")
  }
  if (authority.runId && candidate.entity.runId !== authority.runId) {
    return reject("run_mismatch", "MCP fact belongs to another run.")
  }
  const factTaskId = textFrom(candidate.fact, ["task_id", "taskId"])
  const factRunId = textFrom(candidate.fact, ["run_id", "runId"])
  const factSessionId = textFrom(candidate.fact, ["session_id", "sessionId"])
  if (factTaskId && factTaskId !== authority.taskId) {
    return reject("fact_task_mismatch", "MCP fact claims another task identity.")
  }
  if (factRunId && factRunId !== authority.runId) {
    return reject("fact_run_mismatch", "MCP fact claims another run identity.")
  }
  if (factSessionId && authority.sessionId && factSessionId !== authority.sessionId) {
    return reject("fact_session_mismatch", "MCP fact claims another session identity.")
  }
  const serverId = candidate.serverId
  if (!serverId || serverId.length > 512 || /[\u0000-\u001f\u007f]/.test(serverId)) {
    return reject("server_identity_invalid", "MCP fact lacks a valid canonical server identity.")
  }
  const ownerId = textFrom(
    merge(candidate.fact, candidate.entity.attributes, candidate.entity.metadata),
    ["owner_id", "ownerId", "mcp_owner_id", "mcpOwnerId", "canonical_owner", "canonicalOwner"],
  )
  const schema = textFrom(candidate.fact, ["schema", "mcp_schema", "mcpSchema"])
  const toolDerived =
    candidate.entity.domain === "tool" &&
    (candidate.entity as ToolProjection).toolName?.startsWith("mcp")
  const admittedOwner = ownerId || (toolDerived ? "claude-runtime-mcp" : "")
  if (!admittedOwner || !MCP_OWNER.test(admittedOwner)) {
    return reject("owner_not_mcp", "MCP fact does not identify an admitted canonical MCP owner.")
  }
  if (schema && !MCP_SCHEMA.has(schema)) {
    return reject("schema_not_admitted", `MCP fact schema ${schema} is not admitted.`)
  }
  if (!schema && !toolDerived && candidate.source === "entity-schema") {
    return reject("schema_missing", "MCP entity projection lacks its schema.")
  }
  return Object.freeze({
    ...candidate,
    serverId,
    ownerId: admittedOwner,
  })
}

function ensureAccumulator(
  accumulators: Map<string, ServerAccumulator>,
  serverId: string,
  ownerId: string,
): ServerAccumulator {
  const existing = accumulators.get(serverId)
  if (existing) {
    if (existing.ownerId !== ownerId) existing.warnings.add("owner_conflict")
    return existing
  }
  const created: ServerAccumulator = {
    serverId,
    ownerId,
    entities: [],
    facts: [],
    tools: [],
    resources: [],
    prompts: [],
    elicitations: [],
    eventIds: new Set(),
    warnings: new Set(),
  }
  accumulators.set(serverId, created)
  return created
}

function finalizeServer(
  state: CanonicalProjectionState,
  authority: McpProjectionAuthority,
  accumulator: ServerAccumulator,
  nowMs: number,
): McpServerProjection {
  const entities = [...accumulator.entities].sort(
    (left, right) =>
      compareNumber(left.sequence, right.sequence) ||
      compareText(left.id, right.id),
  )
  const facts = accumulator.facts
  const combined = merge(
    ...entities.map((entity) => merge(entity.attributes, entity.metadata)),
    ...facts,
  )
  const latest = entities.at(-1)
  const enabled = booleanFrom(combined, ["enabled", "is_enabled", "isEnabled"], true)
  const disabledReason = safeDescription(
    textFrom(combined, ["disabled_reason", "disabledReason"]),
  ) || undefined
  const auth = projectAuth(combined, nowMs)
  const connection = normalizeConnectionState(
    textFrom(combined, [
      "connection_state",
      "connectionState",
      "status",
      "state",
      "lifecycle",
    ]),
    enabled,
    auth,
    latest?.lifecycle,
  )
  const tools = Object.freeze(sortCatalog(accumulator.tools))
  const resources = Object.freeze(sortCatalog(accumulator.resources))
  const prompts = Object.freeze(sortCatalog(accumulator.prompts))
  const capability = projectCapabilities(
    combined,
    tools,
    resources,
    prompts,
    accumulator.eventIds,
    state,
  )
  const retryAttempt = Math.max(
    0,
    integerFrom(combined, ["retry_attempt", "retryAttempt", "reconnect_attempt", "reconnectAttempt"]),
  )
  const retryAt = isoDate(
    combined.retry_at ?? combined.retryAt ?? combined.next_retry_at ?? combined.nextRetryAt,
  )
  const declaredRetryAfter = integerFrom(
    combined,
    ["retry_after_ms", "retryAfterMs", "backoff_ms", "backoffMs"],
    retryAt ? Math.max(0, Date.parse(retryAt) - nowMs) : 0,
  )
  const retryAfterMs =
    retryAt || declaredRetryAfter > 0
      ? Math.max(0, retryAt ? Date.parse(retryAt) - nowMs : declaredRetryAfter)
      : undefined
  const config = projectConfig(combined)
  const warnings = new Set(accumulator.warnings)
  if (!authority.snapshotComplete) warnings.add("snapshot_incomplete")
  if (!authority.connected) warnings.add("projection_disconnected")
  if (auth.state === "expired") warnings.add("auth_expired")
  if (auth.state === "missing" && auth.required) warnings.add("auth_missing")
  if (connection === "backoff" && retryAfterMs === undefined) warnings.add("backoff_deadline_missing")
  if (connection === "disabled" && !disabledReason) warnings.add("disabled_reason_missing")
  if (latest?.status && !AUTHORITATIVE.has(latest.status)) warnings.add("server_not_authoritative")
  if (tools.length !== capability.tools) warnings.add("tool_count_mismatch")
  if (resources.length !== capability.resources) warnings.add("resource_count_mismatch")
  if (prompts.length !== capability.prompts) warnings.add("prompt_count_mismatch")
  const eventIds = [...accumulator.eventIds]
    .filter((eventId) => Boolean(state.causality.byEvent[eventId]))
    .sort((left, right) =>
      compareNumber(
        state.causality.byEvent[left]?.sequence,
        state.causality.byEvent[right]?.sequence,
      ) || compareText(left, right))
  const server = {
    id: accumulator.serverId,
    name: safeLabel(
      textFrom(combined, ["name", "server_name", "serverName"]),
      accumulator.serverId,
    ),
    title: safeLabel(
      textFrom(combined, ["title", "display_name", "displayName", "name"]),
      accumulator.serverId,
    ),
    taskId: authority.taskId,
    runId: authority.runId,
    sessionId: authority.sessionId,
    ownerId: accumulator.ownerId,
    ownerRevision: Math.max(
      0,
      integerFrom(combined, ["owner_revision", "ownerRevision"], latest?.revision ?? 0),
    ),
    revision: Math.max(
      latest?.revision ?? 0,
      integerFrom(combined, ["revision", "server_revision", "serverRevision"]),
    ),
    sequence: latest?.sequence ?? 0,
    connectionEpoch: Math.max(
      0,
      integerFrom(combined, ["connection_epoch", "connectionEpoch", "epoch"]),
    ),
    state: connection,
    enabled,
    mutable:
      enabled &&
      booleanFrom(combined, ["mutable", "controls_enabled", "controlsEnabled"], true),
    disabledReason,
    lastErrorCode: safeLabel(
      textFrom(combined, ["last_error_code", "lastErrorCode", "error_code", "errorCode"]),
    ) || undefined,
    lastErrorMessage: safeDescription(
      textFrom(combined, ["last_error_message", "lastErrorMessage", "error", "error_message"]),
    ) || undefined,
    lastConnectedAt: isoDate(
      combined.last_connected_at ?? combined.lastConnectedAt ?? combined.connected_at,
    ),
    lastDisconnectedAt: isoDate(
      combined.last_disconnected_at ?? combined.lastDisconnectedAt ?? combined.disconnected_at,
    ),
    lastHeartbeatAt: isoDate(
      combined.last_heartbeat_at ?? combined.lastHeartbeatAt ?? combined.heartbeat_at,
    ),
    retryAttempt,
    retryAt,
    retryAfterMs,
    reconnectEligible:
      enabled &&
      booleanFrom(combined, ["reconnect_eligible", "reconnectEligible"], true) &&
      !["connecting", "disabled"].includes(connection) &&
      auth.state !== "refreshing",
    config,
    auth,
    capabilities: capability,
    tools,
    resources,
    prompts,
    elicitations: Object.freeze(
      [...accumulator.elicitations].sort(
        (left, right) =>
          compareText(right.requestedAt, left.requestedAt) ||
          compareText(left.id, right.id),
      ),
    ),
    eventIds: Object.freeze(eventIds),
    warnings: Object.freeze([...warnings].sort()),
  }
  return Object.freeze({
    ...server,
    fingerprint: fingerprint([
      server.id,
      server.ownerId,
      server.ownerRevision,
      server.revision,
      server.connectionEpoch,
      server.state,
      server.enabled,
      server.config,
      server.auth,
      server.capabilities,
      tools,
      resources,
      prompts,
      server.elicitations,
      server.warnings,
    ]),
  })
}

function projectConfig(source: Record<string, unknown>): McpConfigProvenance {
  const config = merge(
    object(source.config),
    object(source.configuration),
    object(source.config_provenance),
    object(source.configProvenance),
    source,
  )
  const configSource = normalizeConfigSource(
    textFrom(config, ["source", "config_source", "configSource", "source_kind", "sourceKind"]),
  )
  const transport = normalizeTransport(
    textFrom(config, ["transport", "transport_kind", "transportKind"]),
  )
  const secretFieldPresence = collectSecretPresence(config)
  const headerNames = unique([
    ...listFrom(config, ["header_names", "headerNames"]),
    ...Object.keys(object(config.headers)),
  ])
    .filter((name) => !/authorization|cookie|token|secret/i.test(name))
    .sort()
  const environmentKeys = unique([
    ...listFrom(config, ["environment_keys", "environmentKeys", "env_keys", "envKeys"]),
    ...Object.keys(object(config.env)),
    ...Object.keys(object(config.environment)),
  ]).sort()
  return Object.freeze({
    source: configSource,
    sourceId: safeLabel(textFrom(config, ["source_id", "sourceId", "config_path", "configPath"])) || undefined,
    declaredAt: isoDate(config.declared_at ?? config.declaredAt ?? config.created_at),
    configRevision: Math.max(
      0,
      integerFrom(config, ["config_revision", "configRevision", "revision"]),
    ),
    configDigest: safeLabel(
      textFrom(config, ["config_digest", "configDigest", "digest", "hash"]),
    ) || undefined,
    managed: booleanFrom(config, ["managed", "is_managed", "isManaged"], configSource === "managed"),
    inherited: booleanFrom(config, ["inherited", "is_inherited", "isInherited"]),
    overridden: booleanFrom(config, ["overridden", "is_overridden", "isOverridden"]),
    overrideSource: normalizeOptionalConfigSource(
      textFrom(config, ["override_source", "overrideSource"]),
    ),
    transport,
    endpointClass: normalizeEndpointClass(
      textFrom(config, ["endpoint_class", "endpointClass", "network_class", "networkClass"]),
      transport,
    ),
    environmentKeys: Object.freeze(environmentKeys),
    argumentCount: Math.max(
      0,
      integerFrom(config, ["argument_count", "argumentCount"], textList(config.args).length),
    ),
    headerNames: Object.freeze(headerNames),
    secretFieldPresence: Object.freeze(secretFieldPresence),
  })
}

function projectAuth(
  source: Record<string, unknown>,
  nowMs: number,
): McpAuthProjection {
  const auth = merge(
    object(source.authentication),
    object(source.oauth),
    object(source.auth),
  )
  const required = booleanFrom(
    merge(source, auth),
    ["auth_required", "authRequired", "required", "requires_auth", "requiresAuth"],
  )
  const expiresAt = isoDate(
    auth.expires_at ?? auth.expiresAt ?? auth.expiry ?? source.auth_expires_at,
  )
  const expiresInMs = expiresAt
    ? Date.parse(expiresAt) - nowMs
    : undefined
  const credentialPresent = booleanFrom(
    auth,
    ["credential_present", "credentialPresent", "present"],
    secretPresent(auth.credential ?? auth.credentials),
  )
  const accessTokenPresent = booleanFrom(
    auth,
    ["access_token_present", "accessTokenPresent", "token_present", "tokenPresent"],
    secretPresent(auth.access_token ?? auth.accessToken ?? auth.token),
  )
  const refreshTokenPresent = booleanFrom(
    auth,
    ["refresh_token_present", "refreshTokenPresent"],
    secretPresent(auth.refresh_token ?? auth.refreshToken),
  )
  const clientSecretPresent = booleanFrom(
    auth,
    ["client_secret_present", "clientSecretPresent"],
    secretPresent(auth.client_secret ?? auth.clientSecret),
  )
  const refreshInFlight = booleanFrom(
    auth,
    ["refresh_in_flight", "refreshInFlight", "refresh_pending", "refreshPending"],
  )
  const explicit = textFrom(auth, ["state", "status", "auth_state", "authState"])
  const state = normalizeAuthState(
    explicit,
    required,
    credentialPresent || accessTokenPresent,
    expiresInMs,
    refreshInFlight,
  )
  const warnings: string[] = []
  if (required && !credentialPresent && !accessTokenPresent) warnings.push("credential_missing")
  if (expiresInMs !== undefined && expiresInMs <= 0) warnings.push("credential_expired")
  else if (expiresInMs !== undefined && expiresInMs <= 300_000) warnings.push("credential_expires_soon")
  if (state === "failed" && !textFrom(auth, ["last_failure_code", "lastFailureCode", "error_code"])) {
    warnings.push("auth_failure_code_missing")
  }
  return Object.freeze({
    required,
    state,
    provider: safeLabel(textFrom(auth, ["provider", "issuer", "auth_provider", "authProvider"])) || undefined,
    accountLabel: safeLabel(
      textFrom(auth, ["account_label", "accountLabel", "subject_label", "subjectLabel"]),
    ) || undefined,
    scopeClasses: Object.freeze(
      listFrom(auth, ["scope_classes", "scopeClasses", "scopes"])
        .map(classifyScope)
        .filter(Boolean)
        .sort(),
    ),
    credentialPresent,
    accessTokenPresent,
    refreshTokenPresent,
    clientSecretPresent,
    expiresAt,
    expiresInMs,
    refreshEligible:
      required &&
      booleanFrom(auth, ["refresh_eligible", "refreshEligible"], refreshTokenPresent) &&
      !refreshInFlight,
    refreshInFlight,
    lastRefreshAt: isoDate(auth.last_refresh_at ?? auth.lastRefreshAt),
    lastRefreshEventId: safeLabel(
      textFrom(auth, ["last_refresh_event_id", "lastRefreshEventId"]),
    ) || undefined,
    lastFailureCode: safeLabel(
      textFrom(auth, ["last_failure_code", "lastFailureCode", "error_code", "errorCode"]),
    ) || undefined,
    lastFailureMessage: safeDescription(
      textFrom(auth, ["last_failure_message", "lastFailureMessage", "error_message", "error"]),
    ) || undefined,
    authRevision: Math.max(0, integerFrom(auth, ["auth_revision", "authRevision", "revision"])),
    warnings: Object.freeze(unique(warnings)),
  })
}

function projectCapabilities(
  source: Record<string, unknown>,
  tools: readonly McpCatalogEntry[],
  resources: readonly McpCatalogEntry[],
  prompts: readonly McpCatalogEntry[],
  eventIds: ReadonlySet<string>,
  state: CanonicalProjectionState,
): McpCapabilitySummary {
  const capability = merge(
    object(source.capability),
    object(source.capabilities),
    object(source.capability_summary),
    object(source.capabilitySummary),
  )
  const changedEventId = textFrom(
    capability,
    ["changed_event_id", "changedEventId", "event_id", "eventId"],
  ) || latestCapabilityEvent(eventIds, state)?.eventId
  const changedEvent = changedEventId
    ? state.causality.byEvent[changedEventId]
    : undefined
  const warnings: string[] = []
  const revision = Math.max(
    0,
    integerFrom(capability, ["revision", "capability_revision", "capabilityRevision"]),
  )
  if (!revision && (tools.length || resources.length || prompts.length)) {
    warnings.push("capability_revision_missing")
  }
  return Object.freeze({
    revision,
    tools: Math.max(
      0,
      integerFrom(capability, ["tool_count", "toolCount", "tools"], tools.length),
    ),
    resources: Math.max(
      0,
      integerFrom(capability, ["resource_count", "resourceCount", "resources"], resources.length),
    ),
    prompts: Math.max(
      0,
      integerFrom(capability, ["prompt_count", "promptCount", "prompts"], prompts.length),
    ),
    elicitation: booleanFrom(capability, ["elicitation", "supports_elicitation", "supportsElicitation"]),
    resourceSubscriptions: booleanFrom(
      capability,
      ["resource_subscriptions", "resourceSubscriptions", "subscribe"],
    ),
    promptListChanged: booleanFrom(
      capability,
      ["prompt_list_changed", "promptListChanged"],
    ),
    toolListChanged: booleanFrom(
      capability,
      ["tool_list_changed", "toolListChanged"],
    ),
    resourceListChanged: booleanFrom(
      capability,
      ["resource_list_changed", "resourceListChanged"],
    ),
    changedAt: isoDate(
      capability.changed_at ?? capability.changedAt ?? changedEvent?.committedAt,
    ),
    changedEventId,
    added: Object.freeze(
      listFrom(capability, ["added", "added_capabilities", "addedCapabilities"]).sort(),
    ),
    removed: Object.freeze(
      listFrom(capability, ["removed", "removed_capabilities", "removedCapabilities"]).sort(),
    ),
    warnings: Object.freeze(warnings),
  })
}

function catalogFacts(
  fact: Record<string, unknown>,
  entity: ProjectionEntity,
  serverId: string,
): McpCatalogEntry[] {
  const output: McpCatalogEntry[] = []
  const catalogs: [McpCatalogKind, readonly string[]][] = [
    ["tool", ["tools", "tool_catalog", "toolCatalog"]],
    ["resource", ["resources", "resource_catalog", "resourceCatalog"]],
    ["prompt", ["prompts", "prompt_catalog", "promptCatalog"]],
  ]
  for (const [kind, keys] of catalogs) {
    for (const key of keys) {
      const values = fact[key]
      if (!Array.isArray(values)) continue
      for (const value of values.slice(0, 10000)) {
        const item = projectCatalogEntry(object(value), entity, serverId, kind)
        if (item) output.push(item)
      }
    }
  }
  const catalog = object(fact.catalog)
  if (Object.keys(catalog).length) {
    output.push(...catalogFacts(catalog, entity, serverId))
  }
  const kind = normalizeCatalogKind(
    textFrom(fact, ["catalog_kind", "catalogKind", "kind"]),
  )
  if (kind && textFrom(fact, ["name", "tool_name", "resource_uri", "prompt_name"])) {
    const item = projectCatalogEntry(fact, entity, serverId, kind)
    if (item) output.push(item)
  }
  return output
}

function projectCatalogEntry(
  source: Record<string, unknown>,
  entity: ProjectionEntity,
  serverId: string,
  kind: McpCatalogKind,
): McpCatalogEntry | undefined {
  const name = safeLabel(
    textFrom(
      source,
      kind === "resource"
        ? ["name", "uri", "resource_uri", "resourceUri", "id"]
        : kind === "tool"
          ? ["name", "tool_name", "toolName", "id"]
          : ["name", "prompt_name", "promptName", "id"],
    ),
  )
  if (!name) return undefined
  const id = safeLabel(
    textFrom(source, ["id", `${kind}_id`, `${kind}Id`]),
    `${serverId}:${kind}:${name}`,
  )
  const input = merge(
    object(source.input_schema),
    object(source.inputSchema),
    object(source.arguments_schema),
    object(source.argumentsSchema),
  )
  const properties = object(input.properties)
  const argumentNames = unique([
    ...listFrom(source, ["argument_names", "argumentNames"]),
    ...Object.keys(properties),
  ]).sort()
  const requiredArguments = unique([
    ...listFrom(source, ["required_arguments", "requiredArguments"]),
    ...textList(input.required),
  ]).sort()
  return Object.freeze({
    id,
    serverId,
    kind,
    name,
    title: safeLabel(textFrom(source, ["title", "display_name", "displayName"]), name),
    description: safeDescription(textFrom(source, ["description", "summary"])),
    revision: Math.max(
      0,
      integerFrom(source, ["revision", `${kind}_revision`, `${kind}Revision`], entity.revision),
    ),
    sequence: Math.max(
      0,
      integerFrom(source, ["sequence", "aggregate_sequence", "aggregateSequence"], entity.sequence),
    ),
    eventId: safeLabel(textFrom(source, ["event_id", "eventId"]), entity.lastEventId) || undefined,
    toolCallId: safeLabel(textFrom(source, ["tool_call_id", "toolCallId"])) || undefined,
    uri: kind === "resource"
      ? safeLabel(textFrom(source, ["uri", "resource_uri", "resourceUri"])) || undefined
      : undefined,
    mimeType: safeLabel(textFrom(source, ["mime_type", "mimeType", "media_type", "mediaType"])) || undefined,
    argumentNames: Object.freeze(argumentNames),
    requiredArguments: Object.freeze(requiredArguments),
    annotations: Object.freeze(
      listFrom(source, ["annotations", "tags"]).map((item) => safeLabel(item)).filter(Boolean).sort(),
    ),
    capabilityFlags: Object.freeze(
      listFrom(source, ["capability_flags", "capabilityFlags", "capabilities"]).sort(),
    ),
    deprecated: booleanFrom(source, ["deprecated", "is_deprecated", "isDeprecated"]),
    enabled: booleanFrom(source, ["enabled", "is_enabled", "isEnabled"], true),
    readOnly: booleanFrom(source, ["read_only", "readOnly"], kind !== "tool"),
    sensitive: booleanFrom(source, ["sensitive", "contains_sensitive_data", "containsSensitiveData"]),
    inputSchemaDigest: safeLabel(
      textFrom(source, ["input_schema_digest", "inputSchemaDigest"]),
      Object.keys(input).length ? fingerprint(input) : "",
    ) || undefined,
    outputSchemaDigest: safeLabel(
      textFrom(source, ["output_schema_digest", "outputSchemaDigest"]),
    ) || undefined,
    provenanceDigest: safeLabel(
      textFrom(source, ["provenance_digest", "provenanceDigest", "digest"]),
    ) || undefined,
  })
}

function catalogFromToolProjection(
  tool: ToolProjection,
): { serverId: string; entry: McpCatalogEntry } | undefined {
  const source = merge(tool.attributes, tool.metadata)
  const toolName = tool.toolName ?? textFrom(source, ["tool_name", "toolName"])
  if (!toolName) return undefined
  const parsed = parseMcpToolName(toolName)
  const serverId = textFrom(
    source,
    ["mcp_server_id", "mcpServerId", "server_id", "serverId"],
    parsed.serverId,
  )
  if (!serverId) return undefined
  const entry = projectCatalogEntry(
    {
      ...source,
      id: textFrom(
        source,
        ["mcp_tool_id", "mcpToolId", "catalog_entry_id", "catalogEntryId"],
        `${serverId}:tool:${textFrom(source, ["mcp_tool_name", "mcpToolName"], parsed.name)}`,
      ),
      name: textFrom(source, ["mcp_tool_name", "mcpToolName"], parsed.name),
      title: tool.title,
      description: tool.summary,
      revision: tool.revision,
      sequence: tool.sequence,
      event_id: tool.lastEventId,
      tool_call_id: tool.toolCallId,
      enabled: !tool.terminal || tool.lifecycle !== "deleted",
    },
    tool,
    serverId,
    "tool",
  )
  return entry ? { serverId, entry } : undefined
}

function parseMcpToolName(value: string): { serverId: string; name: string } {
  if (value.startsWith("mcp__")) {
    const pieces = value.slice(5).split("__")
    return {
      serverId: pieces.shift() ?? "",
      name: pieces.join("__"),
    }
  }
  if (value.startsWith("mcp_")) {
    const pieces = value.slice(4).split("_")
    return {
      serverId: pieces.shift() ?? "",
      name: pieces.join("_"),
    }
  }
  return { serverId: "", name: "" }
}

function elicitationFacts(
  fact: Record<string, unknown>,
  entity: ProjectionEntity,
  serverId: string,
  ownerId: string,
  nowMs: number,
): McpElicitationProjection[] {
  const values: unknown[] = []
  for (const key of ["elicitations", "elicitation_requests", "elicitationRequests"]) {
    if (Array.isArray(fact[key])) values.push(...(fact[key] as unknown[]).slice(0, 1000))
  }
  if (
    textFrom(fact, ["schema"]) === "zyra.mcp-elicitation/v1" ||
    textFrom(fact, ["kind", "type"]).toLowerCase().includes("elicitation")
  ) {
    values.push(fact)
  }
  const output: McpElicitationProjection[] = []
  for (const value of values) {
    const source = object(value)
    const id = safeLabel(
      textFrom(source, ["id", "request_id", "requestId", "elicitation_id", "elicitationId"]),
    )
    if (!id) continue
    const schema = merge(
      object(source.response_schema),
      object(source.responseSchema),
      object(source.schema_definition),
      object(source.schemaDefinition),
    )
    const properties = object(schema.properties)
    const required = textList(schema.required)
    const sensitive = Object.entries(properties)
      .filter(([key, property]) =>
        /secret|password|token|credential|private/i.test(key) ||
        booleanFrom(object(property), ["sensitive", "writeOnly"]))
      .map(([key]) => key)
    const state = normalizeElicitationState(
      textFrom(source, ["state", "status"]),
      isoDate(source.expires_at ?? source.expiresAt),
      nowMs,
    )
    output.push(Object.freeze({
      id,
      serverId,
      taskId: textFrom(source, ["task_id", "taskId"], entity.taskId),
      runId: textFrom(source, ["run_id", "runId"], entity.runId),
      sessionId: textFrom(source, ["session_id", "sessionId"], entity.sessionId ?? ""),
      ownerId: textFrom(source, ["owner_id", "ownerId"], ownerId),
      requestRevision: Math.max(
        0,
        integerFrom(source, ["request_revision", "requestRevision", "revision"], entity.revision),
      ),
      schemaDigest: safeLabel(
        textFrom(source, ["schema_digest", "schemaDigest"]),
        fingerprint(schema),
      ),
      title: safeLabel(textFrom(source, ["title"]), "MCP request"),
      message: safeDescription(textFrom(source, ["message", "description", "prompt"])),
      requestedAt: isoDate(source.requested_at ?? source.requestedAt ?? entity.createdAt) ?? entity.createdAt,
      expiresAt: isoDate(source.expires_at ?? source.expiresAt),
      state,
      fieldNames: Object.freeze(Object.keys(properties).sort()),
      requiredFields: Object.freeze(required.sort()),
      sensitiveFields: Object.freeze(unique(sensitive).sort()),
      permissionId: safeLabel(textFrom(source, ["permission_id", "permissionId"])) || undefined,
      commandId: safeLabel(textFrom(source, ["command_id", "commandId"])) || undefined,
      responseDigest: safeLabel(textFrom(source, ["response_digest", "responseDigest"])) || undefined,
      settledAt: isoDate(source.settled_at ?? source.settledAt),
      settledEventId: safeLabel(textFrom(source, ["settled_event_id", "settledEventId"])) || undefined,
      failureCode: safeLabel(textFrom(source, ["failure_code", "failureCode", "error_code"])) || undefined,
    }))
  }
  return output
}

function chooseSessionId(
  state: CanonicalProjectionState,
  taskId: string,
  preferred?: string,
): string {
  if (preferred && state.sessions[preferred]?.taskId === taskId) return preferred
  const sessions = Object.values(state.sessions)
    .filter((session) => session.taskId === taskId && session.effective)
    .sort(
      (left, right) =>
        compareNumber(right.sequence, left.sequence) ||
        compareText(left.id, right.id),
    )
  return sessions[0]?.id ?? preferred ?? ""
}

function mcpAuthorityOwner(task: ProjectionEntity | undefined): string {
  if (!task) return "mcp-owner-unresolved"
  const source = merge(task.attributes, task.metadata)
  return textFrom(
    source,
    ["mcp_owner_id", "mcpOwnerId", "mcp_owner", "mcpOwner"],
    "claude-runtime-mcp",
  )
}

function serverIdentity(
  fact: Record<string, unknown>,
  fallback: Record<string, unknown>,
): string {
  return safeLabel(
    textFrom(
      merge(fallback, fact),
      ["server_id", "serverId", "mcp_server_id", "mcpServerId", "id", "name"],
    ),
  )
}

function normalizeConnectionState(
  value: string,
  enabled: boolean,
  auth: McpAuthProjection,
  fallback?: string,
): McpConnectionState {
  if (!enabled) return "disabled"
  if (auth.required && ["missing", "expired", "failed"].includes(auth.state)) return "needs-auth"
  const normalized = (value || fallback || "").toLowerCase().replace(/[_\s]+/g, "-")
  if (["connected", "ready", "running", "active", "available"].includes(normalized)) return "connected"
  if (["connecting", "starting", "reconnecting"].includes(normalized)) return "connecting"
  if (["needs-auth", "auth-required", "unauthorized"].includes(normalized)) return "needs-auth"
  if (["backoff", "retrying", "waiting-retry"].includes(normalized)) return "backoff"
  if (["disabled", "deleted"].includes(normalized)) return "disabled"
  if (["failed", "error", "rejected"].includes(normalized)) return "failed"
  if (["disconnected", "offline", "stopped", "cancelled"].includes(normalized)) return "disconnected"
  return "unknown"
}

function normalizeAuthState(
  value: string,
  required: boolean,
  present: boolean,
  expiresInMs: number | undefined,
  refreshing: boolean,
): McpAuthProjection["state"] {
  if (!required) return "not-required"
  if (refreshing) return "refreshing"
  const normalized = value.toLowerCase().replace(/[_\s]+/g, "-")
  if (["failed", "error", "rejected"].includes(normalized)) return "failed"
  if (["missing", "absent", "needs-auth", "unauthorized"].includes(normalized)) return "missing"
  if (["expired", "invalid"].includes(normalized)) return "expired"
  if (expiresInMs !== undefined && expiresInMs <= 0) return "expired"
  if (!present) return "missing"
  if (["refreshing", "pending"].includes(normalized)) return "refreshing"
  if (expiresInMs !== undefined && expiresInMs <= 300_000) return "expiring"
  if (["valid", "authenticated", "ready", "active"].includes(normalized) || present) return "valid"
  return "unknown"
}

function normalizeConfigSource(value: string): McpConfigSource {
  const normalized = value.toLowerCase().replace(/[_\s]+/g, "-")
  if (["project", "workspace", "repository", "repo"].includes(normalized)) return "project"
  if (["user", "personal", "profile", "global"].includes(normalized)) return "user"
  if (["managed", "policy", "enterprise", "admin"].includes(normalized)) return "managed"
  if (["plugin", "extension", "marketplace"].includes(normalized)) return "plugin"
  if (["runtime", "dynamic", "session"].includes(normalized)) return "runtime"
  if (["environment", "env"].includes(normalized)) return "environment"
  return "unknown"
}

function normalizeOptionalConfigSource(value: string): McpConfigSource | undefined {
  return value ? normalizeConfigSource(value) : undefined
}

function normalizeTransport(value: string): McpTransportKind {
  const normalized = value.toLowerCase().replace(/[_\s]+/g, "-")
  if (["stdio", "process", "subprocess"].includes(normalized)) return "stdio"
  if (["sse", "server-sent-events"].includes(normalized)) return "sse"
  if (["streamable-http", "http", "https"].includes(normalized)) return "streamable-http"
  if (["websocket", "ws", "wss"].includes(normalized)) return "websocket"
  if (["in-process", "inprocess", "embedded"].includes(normalized)) return "in-process"
  return "unknown"
}

function normalizeEndpointClass(
  value: string,
  transport: McpTransportKind,
): McpConfigProvenance["endpointClass"] {
  const normalized = value.toLowerCase().replace(/[_\s]+/g, "-")
  if (["local", "loopback", "stdio", "unix"].includes(normalized)) return "local"
  if (["private", "intranet", "edge"].includes(normalized)) return "private"
  if (["public", "internet", "cloud"].includes(normalized)) return "public"
  if (["managed", "enterprise"].includes(normalized)) return "managed"
  if (transport === "stdio" || transport === "in-process") return "local"
  return "unknown"
}

function normalizeCatalogKind(value: string): McpCatalogKind | undefined {
  const normalized = value.toLowerCase()
  if (normalized.includes("tool")) return "tool"
  if (normalized.includes("resource")) return "resource"
  if (normalized.includes("prompt")) return "prompt"
  return undefined
}

function normalizeElicitationState(
  value: string,
  expiresAt: string | undefined,
  nowMs: number,
): McpElicitationProjection["state"] {
  if (expiresAt && Date.parse(expiresAt) <= nowMs) return "expired"
  const normalized = value.toLowerCase().replace(/[_\s]+/g, "-")
  if (["pending", "open", "requested"].includes(normalized)) return "pending"
  if (["permission-pending", "waiting-policy", "waiting-permission"].includes(normalized)) return "permission-pending"
  if (["submitted", "running", "responded"].includes(normalized)) return "submitted"
  if (["accepted", "completed", "applied"].includes(normalized)) return "accepted"
  if (["declined", "denied", "rejected"].includes(normalized)) return "declined"
  if (["cancelled", "canceled"].includes(normalized)) return "cancelled"
  if (normalized === "expired") return "expired"
  if (["failed", "error"].includes(normalized)) return "failed"
  return "pending"
}

function classifyScope(value: string): string {
  const normalized = value.trim().toLowerCase()
  if (!normalized) return ""
  if (/admin|write|manage|delete|execute|invoke/.test(normalized)) return "mutation"
  if (/offline|refresh/.test(normalized)) return "offline"
  if (/identity|profile|openid|email/.test(normalized)) return "identity"
  if (/read|list|view/.test(normalized)) return "read"
  return "other"
}

function latestCapabilityEvent(
  eventIds: ReadonlySet<string>,
  state: CanonicalProjectionState,
): CausalEventProjection | undefined {
  return [...eventIds]
    .map((eventId) => state.causality.byEvent[eventId])
    .filter((event): event is CausalEventProjection =>
      Boolean(event && /mcp.*(?:capabil|tool|resource|prompt).*(?:change|refresh|list)/i.test(event.eventType)))
    .sort(
      (left, right) =>
        compareNumber(right.sequence, left.sequence) ||
        compareText(left.eventId, right.eventId),
    )[0]
}

function upsertCatalog(target: McpCatalogEntry[], candidate: McpCatalogEntry): void {
  const index = target.findIndex((item) => item.id === candidate.id)
  if (index < 0) {
    target.push(candidate)
    return
  }
  const existing = target[index]!
  if (
    candidate.revision > existing.revision ||
    (candidate.revision === existing.revision && candidate.sequence > existing.sequence)
  ) {
    target[index] = candidate
  }
}

function upsertElicitation(
  target: McpElicitationProjection[],
  candidate: McpElicitationProjection,
): void {
  const index = target.findIndex((item) => item.id === candidate.id)
  if (index < 0) {
    target.push(candidate)
    return
  }
  if (candidate.requestRevision >= target[index]!.requestRevision) {
    target[index] = candidate
  }
}

function sortCatalog(items: readonly McpCatalogEntry[]): McpCatalogEntry[] {
  return [...items].sort(
    (left, right) =>
      compareText(left.name.toLowerCase(), right.name.toLowerCase()) ||
      compareText(left.id, right.id),
  )
}

function compareServer(
  left: McpServerProjection,
  right: McpServerProjection,
): number {
  const rank = (state: McpConnectionState): number => ({
    "needs-auth": 0,
    failed: 1,
    backoff: 2,
    connecting: 3,
    connected: 4,
    disconnected: 5,
    disabled: 6,
    unknown: 7,
  })[state]
  return (
    compareNumber(rank(left.state), rank(right.state)) ||
    compareText(left.title.toLowerCase(), right.title.toLowerCase()) ||
    compareText(left.id, right.id)
  )
}

function sortRejected(
  rejected: readonly McpRejectedProjection[],
): McpRejectedProjection[] {
  const seen = new Set<string>()
  return [...rejected]
    .sort(
      (left, right) =>
        compareText(left.code, right.code) ||
        compareText(left.subject, right.subject) ||
        compareText(left.eventId, right.eventId),
    )
    .filter((item) => {
      const key = `${item.code}\0${item.subject}\0${item.eventId ?? ""}`
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
}

function sameProjection(
  left: McpConsoleProjection,
  right: McpConsoleProjection,
): boolean {
  return left.fingerprint === right.fingerprint
}
