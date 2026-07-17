import type { JsonObject, JsonValue } from "../contracts.ts";
import {
  canonicalJson,
  canonicalObject,
  cloneJson,
  deterministicMcpId,
  httpUrl,
  identifier,
  mergeJson,
  monotonicNow,
  optionalText,
  positiveInteger,
  requiredText,
  sha256,
  stringList,
} from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";

export type McpConfigSource = "managed" | "user" | "project" | "plugin" | "session";
export type McpTransportKind = "stdio" | "streamable_http" | "sse" | "in_process";
export type McpRestartPolicy = "never" | "on_failure" | "always";
export type McpNetworkPolicy = "offline" | "loopback" | "allowlisted" | "unrestricted";

export interface McpRetryPolicy {
  maximumAttempts: number;
  initialDelayMs: number;
  maximumDelayMs: number;
  multiplier: number;
  jitterRatio: number;
  retryStatusCodes: number[];
  retryJsonRpcCodes: number[];
}

export interface McpTimeoutPolicy {
  connectMs: number;
  initializeMs: number;
  requestMs: number;
  idleMs: number;
  shutdownMs: number;
  authMs: number;
  taskPollMs: number;
}

export interface McpRateLimitPolicy {
  maximumConcurrent: number;
  requestsPerMinute: number;
  burst: number;
  queueCapacity: number;
  queueTimeoutMs: number;
}

export interface McpStdioTransportConfig {
  kind: "stdio";
  command: string;
  arguments: string[];
  cwd: string | null;
  environmentHandles: Record<string, string>;
  inheritEnvironment: string[];
  stderrLimitBytes: number;
}

export interface McpHttpTransportConfig {
  kind: "streamable_http" | "sse";
  url: string;
  headers: Record<string, string>;
  credentialHandles: Record<string, string>;
  allowedRedirectOrigins: string[];
  maximumRedirects: number;
  preferSse: boolean;
}

export interface McpInProcessTransportConfig {
  kind: "in_process";
  factoryId: string;
  options: JsonObject;
}

export type McpTransportConfig =
  | McpStdioTransportConfig
  | McpHttpTransportConfig
  | McpInProcessTransportConfig;

export interface McpServerConfigRecord {
  serverId: string;
  displayName: string;
  enabled: boolean;
  source: McpConfigSource;
  sourceRevision: number;
  transport: McpTransportConfig;
  retry: McpRetryPolicy;
  timeouts: McpTimeoutPolicy;
  rateLimit: McpRateLimitPolicy;
  restartPolicy: McpRestartPolicy;
  networkPolicy: McpNetworkPolicy;
  allowedOperations: string[];
  deniedOperations: string[];
  allowedRoots: string[];
  authProviderId: string | null;
  protocolVersions: string[];
  autoInitialize: boolean;
  autoReconnect: boolean;
  allowSampling: boolean;
  allowElicitation: boolean;
  allowTasks: boolean;
  metadata: JsonObject;
  configDigest: string;
  createdAt: string;
  updatedAt: string;
}

export interface McpConfigLayer {
  layerId: string;
  source: McpConfigSource;
  revision: number;
  servers: JsonObject;
  tombstones: string[];
  metadata: JsonObject;
}

export interface McpConfigSnapshot {
  version: "zyra.mcp-config-store/v1";
  revision: number;
  digest: string;
  layers: McpConfigLayer[];
  servers: McpServerConfigRecord[];
  tombstones: string[];
  updatedAt: string;
}

export interface McpConfigMergeResult {
  revision: number;
  digest: string;
  added: string[];
  updated: string[];
  removed: string[];
  unchanged: string[];
  source: McpConfigSource;
  layerId: string;
  servers: McpServerConfigRecord[];
}

const sourcePrecedence: Record<McpConfigSource, number> = {
  managed: 500,
  session: 400,
  project: 300,
  user: 200,
  plugin: 100,
};

const defaultRetry: McpRetryPolicy = {
  maximumAttempts: 4,
  initialDelayMs: 250,
  maximumDelayMs: 30_000,
  multiplier: 2,
  jitterRatio: 0.2,
  retryStatusCodes: [408, 425, 429, 500, 502, 503, 504],
  retryJsonRpcCodes: [-32001, -32002, -32003],
};

const defaultTimeouts: McpTimeoutPolicy = {
  connectMs: 15_000,
  initializeMs: 30_000,
  requestMs: 60_000,
  idleMs: 300_000,
  shutdownMs: 5_000,
  authMs: 120_000,
  taskPollMs: 30_000,
};

const defaultRateLimit: McpRateLimitPolicy = {
  maximumConcurrent: 16,
  requestsPerMinute: 600,
  burst: 32,
  queueCapacity: 1_024,
  queueTimeoutMs: 30_000,
};

export class McpConfigStore {
  private revisionValue = 0;
  private digestValue = "";
  private updatedAtValue = "1970-01-01T00:00:00.000Z";
  private readonly layers = new Map<string, McpConfigLayer>();
  private readonly servers = new Map<string, McpServerConfigRecord>();
  private readonly tombstones = new Set<string>();

  constructor(snapshot?: McpConfigSnapshot | JsonObject | null) {
    if (snapshot) this.restore(snapshot);
    else this.digestValue = this.computeDigest();
  }

  get revision(): number {
    return this.revisionValue;
  }

  get digest(): string {
    return this.digestValue;
  }

  merge(layerValue: McpConfigLayer | JsonObject, expectedRevision = this.revisionValue): McpConfigMergeResult {
    if (expectedRevision !== this.revisionValue) {
      throw new McpRuntimeError({
        failureId: `mcp-config-conflict-${expectedRevision}-${this.revisionValue}`,
        category: "conflict",
        code: "config_revision_conflict",
        message: `MCP config revision ${expectedRevision} does not match ${this.revisionValue}`,
        retryable: true,
        disposition: "retry_same_connection",
        details: { expected_revision: expectedRevision, actual_revision: this.revisionValue },
      });
    }
    const layer = normalizeLayer(layerValue);
    const priorLayer = this.layers.get(layer.layerId);
    if (priorLayer && layer.revision <= priorLayer.revision) {
      throw new McpRuntimeError({
        failureId: `mcp-config-layer-stale-${layer.layerId}-${layer.revision}`,
        category: "conflict",
        code: "stale_config_layer",
        message: `layer ${layer.layerId} revision ${layer.revision} is not newer than ${priorLayer.revision}`,
        retryable: false,
        disposition: "terminal",
      });
    }
    const before = new Map(this.servers);
    this.layers.set(layer.layerId, cloneJson(layer));
    this.rebuild();
    this.revisionValue += 1;
    this.updatedAtValue = monotonicNow(this.updatedAtValue);
    this.digestValue = this.computeDigest();
    const added: string[] = [];
    const updated: string[] = [];
    const removed: string[] = [];
    const unchanged: string[] = [];
    for (const serverId of new Set([...before.keys(), ...this.servers.keys()])) {
      const oldValue = before.get(serverId);
      const newValue = this.servers.get(serverId);
      if (!oldValue && newValue) added.push(serverId);
      else if (oldValue && !newValue) removed.push(serverId);
      else if (oldValue && newValue && oldValue.configDigest !== newValue.configDigest) updated.push(serverId);
      else unchanged.push(serverId);
    }
    return {
      revision: this.revisionValue,
      digest: this.digestValue,
      added: added.sort(),
      updated: updated.sort(),
      removed: removed.sort(),
      unchanged: unchanged.sort(),
      source: layer.source,
      layerId: layer.layerId,
      servers: this.list(),
    };
  }

  removeLayer(layerId: string, expectedRevision = this.revisionValue): McpConfigMergeResult {
    if (expectedRevision !== this.revisionValue) {
      return this.merge({
        layerId: `conflict-${layerId}`,
        source: "session",
        revision: 1,
        servers: {},
        tombstones: [],
        metadata: {},
      }, expectedRevision);
    }
    const layer = this.layers.get(layerId);
    if (!layer) {
      throw new McpRuntimeError({
        failureId: `mcp-config-layer-missing-${layerId}`,
        category: "configuration",
        code: "config_layer_not_found",
        message: `MCP config layer ${layerId} was not found`,
        retryable: false,
        disposition: "terminal",
      });
    }
    const before = new Map(this.servers);
    this.layers.delete(layerId);
    this.rebuild();
    this.revisionValue += 1;
    this.updatedAtValue = monotonicNow(this.updatedAtValue);
    this.digestValue = this.computeDigest();
    return diffResult(before, this.servers, this.revisionValue, this.digestValue, layer.source, layerId);
  }

  get(serverId: string): McpServerConfigRecord | null {
    const value = this.servers.get(serverId);
    return value ? cloneJson(value) : null;
  }

  require(serverId: string): McpServerConfigRecord {
    const value = this.get(serverId);
    if (!value) {
      throw new McpRuntimeError({
        failureId: `mcp-config-server-missing-${serverId}`,
        category: "configuration",
        code: "server_config_not_found",
        message: `MCP server ${serverId} is not configured`,
        serverId,
        retryable: false,
        disposition: "reconfigure",
      });
    }
    return value;
  }

  list(options: { enabledOnly?: boolean; source?: McpConfigSource; transport?: McpTransportKind } = {}): McpServerConfigRecord[] {
    return [...this.servers.values()]
      .filter((server) => !options.enabledOnly || server.enabled)
      .filter((server) => !options.source || server.source === options.source)
      .filter((server) => !options.transport || server.transport.kind === options.transport)
      .sort((left, right) => left.serverId.localeCompare(right.serverId))
      .map(cloneJson);
  }

  has(serverId: string): boolean {
    return this.servers.has(serverId);
  }

  snapshot(): McpConfigSnapshot {
    return {
      version: "zyra.mcp-config-store/v1",
      revision: this.revisionValue,
      digest: this.digestValue,
      layers: [...this.layers.values()]
        .sort(compareLayer)
        .map(cloneJson),
      servers: this.list(),
      tombstones: [...this.tombstones].sort(),
      updatedAt: this.updatedAtValue,
    };
  }

  restore(snapshotValue: McpConfigSnapshot | JsonObject): void {
    const snapshot = normalizeSnapshot(snapshotValue);
    const layers = new Map<string, McpConfigLayer>();
    for (const layer of snapshot.layers) {
      if (layers.has(layer.layerId)) throw configError("duplicate_config_layer", `duplicate layer ${layer.layerId}`);
      layers.set(layer.layerId, cloneJson(layer));
    }
    const currentLayers = new Map(this.layers);
    const currentRevision = this.revisionValue;
    const currentDigest = this.digestValue;
    const currentUpdatedAt = this.updatedAtValue;
    try {
      this.layers.clear();
      for (const [id, layer] of layers) this.layers.set(id, layer);
      this.revisionValue = snapshot.revision;
      this.updatedAtValue = snapshot.updatedAt;
      this.rebuild();
      const rebuiltProjection = this.list().map(serverSemanticProjection);
      const snapshotProjection = snapshot.servers.map(serverSemanticProjection);
      if (sha256(rebuiltProjection) !== sha256(snapshotProjection)) {
        throw configError("config_snapshot_projection_mismatch", "MCP config snapshot layers do not match server projection");
      }
      this.servers.clear();
      for (const server of snapshot.servers) this.servers.set(server.serverId, cloneJson(server));
      const calculated = this.computeDigest();
      if (calculated !== snapshot.digest) {
        throw configError("config_snapshot_digest_mismatch", "MCP config snapshot digest mismatch");
      }
      this.digestValue = calculated;
      if (sha256(this.list()) !== sha256(snapshot.servers)) {
        throw configError("config_snapshot_projection_mismatch", "MCP config snapshot server projection mismatch");
      }
    } catch (error) {
      this.layers.clear();
      for (const [id, layer] of currentLayers) this.layers.set(id, layer);
      this.revisionValue = currentRevision;
      this.digestValue = currentDigest;
      this.updatedAtValue = currentUpdatedAt;
      this.rebuild();
      throw error;
    }
  }

  private rebuild(): void {
    const priorServers = new Map(this.servers);
    this.servers.clear();
    this.tombstones.clear();
    const sortedLayers = [...this.layers.values()].sort(compareLayer);
    const rawByServer = new Map<string, { value: JsonObject; source: McpConfigSource; revision: number }>();
    for (const layer of sortedLayers) {
      for (const serverId of layer.tombstones) {
        this.tombstones.add(serverId);
        rawByServer.delete(serverId);
      }
      for (const [serverIdRaw, serverValue] of Object.entries(layer.servers)) {
        const serverId = identifier(serverIdRaw, "server id", 256);
        const patch = canonicalObject(serverValue, `server ${serverId}`);
        const prior = rawByServer.get(serverId);
        rawByServer.set(serverId, {
          value: prior ? mergeJson(prior.value, patch) : patch,
          source: layer.source,
          revision: layer.revision,
        });
        this.tombstones.delete(serverId);
      }
    }
    for (const [serverId, raw] of rawByServer) {
      const prior = priorServers.get(serverId);
      const value = cloneJson(raw.value);
      if (prior && value.createdAt === undefined && value.created_at === undefined) value.createdAt = prior.createdAt;
      if (prior && value.updatedAt === undefined && value.updated_at === undefined) value.updatedAt = prior.updatedAt;
      this.servers.set(serverId, normalizeServer(serverId, value, raw.source, raw.revision));
    }
  }

  private computeDigest(): string {
    return sha256({
      revision: this.revisionValue,
      layers: [...this.layers.values()].sort(compareLayer),
      servers: this.list(),
      tombstones: [...this.tombstones].sort(),
      updated_at: this.updatedAtValue,
    });
  }
}

function serverSemanticProjection(server: McpServerConfigRecord): JsonObject {
  const { createdAt: _createdAt, updatedAt: _updatedAt, configDigest: _configDigest, ...semantic } = server;
  return canonicalJson(semantic) as JsonObject;
}

function normalizeLayer(value: McpConfigLayer | JsonObject): McpConfigLayer {
  const object = canonicalObject(value, "config layer");
  const source = normalizeSource(object.source);
  return {
    layerId: identifier(object.layerId ?? object.layer_id, "layerId", 256),
    source,
    revision: positiveInteger(object.revision, "layer revision"),
    servers: canonicalObject(object.servers ?? {}, "servers"),
    tombstones: stringList(object.tombstones ?? [], "tombstones", 10_000)
      .map((id) => identifier(id, "tombstone server id", 256)),
    metadata: canonicalObject(object.metadata ?? {}, "layer metadata"),
  };
}

function normalizeSnapshot(value: McpConfigSnapshot | JsonObject): McpConfigSnapshot {
  const object = canonicalObject(value, "config snapshot");
  if (object.version !== "zyra.mcp-config-store/v1") throw configError("unsupported_config_snapshot", "unsupported config snapshot version");
  const layersValue = object.layers;
  if (!Array.isArray(layersValue)) throw configError("invalid_config_snapshot", "snapshot layers must be an array");
  const serversValue = object.servers;
  if (!Array.isArray(serversValue)) throw configError("invalid_config_snapshot", "snapshot servers must be an array");
  return {
    version: "zyra.mcp-config-store/v1",
    revision: Number(object.revision),
    digest: requiredText(object.digest, "snapshot digest", 128),
    layers: layersValue.map((layer) => normalizeLayer(canonicalObject(layer))),
    servers: serversValue.map((server) => normalizeServerSnapshot(server)),
    tombstones: stringList(object.tombstones ?? [], "snapshot tombstones"),
    updatedAt: requiredText(object.updatedAt ?? object.updated_at, "snapshot updatedAt", 64),
  };
}

function normalizeServer(
  serverId: string,
  value: JsonObject,
  source: McpConfigSource,
  sourceRevision: number,
): McpServerConfigRecord {
  const transport = normalizeTransport(value.transport ?? value, serverId);
  const createdAt = optionalText(value.created_at ?? value.createdAt, "createdAt", 64) ?? new Date().toISOString();
  const updatedAt = optionalText(value.updated_at ?? value.updatedAt, "updatedAt", 64) ?? createdAt;
  const base: Omit<McpServerConfigRecord, "configDigest"> = {
    serverId,
    displayName: optionalText(value.display_name ?? value.displayName, "displayName", 1_024) ?? serverId,
    enabled: value.enabled !== false,
    source,
    sourceRevision,
    transport,
    retry: normalizeRetry(value.retry),
    timeouts: normalizeTimeouts(value.timeouts),
    rateLimit: normalizeRateLimit(value.rate_limit ?? value.rateLimit),
    restartPolicy: normalizeRestart(value.restart_policy ?? value.restartPolicy),
    networkPolicy: normalizeNetwork(value.network_policy ?? value.networkPolicy, transport),
    allowedOperations: normalizePatterns(value.allowed_operations ?? value.allowedOperations, ["*"]),
    deniedOperations: normalizePatterns(value.denied_operations ?? value.deniedOperations, []),
    allowedRoots: normalizeStringArray(value.allowed_roots ?? value.allowedRoots, []),
    authProviderId: optionalText(value.auth_provider_id ?? value.authProviderId, "authProviderId", 256),
    protocolVersions: normalizeStringArray(value.protocol_versions ?? value.protocolVersions, ["2025-06-18"]),
    autoInitialize: value.auto_initialize !== false && value.autoInitialize !== false,
    autoReconnect: value.auto_reconnect !== false && value.autoReconnect !== false,
    allowSampling: value.allow_sampling === true || value.allowSampling === true,
    allowElicitation: value.allow_elicitation !== false && value.allowElicitation !== false,
    allowTasks: value.allow_tasks !== false && value.allowTasks !== false,
    metadata: canonicalObject(value.metadata ?? {}, "server metadata"),
    createdAt,
    updatedAt,
  };
  return { ...base, configDigest: sha256(base) };
}

function normalizeServerSnapshot(value: unknown): McpServerConfigRecord {
  const object = canonicalObject(value, "server snapshot");
  const serverId = identifier(object.serverId ?? object.server_id, "serverId", 256);
  const source = normalizeSource(object.source);
  const normalized = normalizeServer(serverId, object, source, Number(object.sourceRevision ?? object.source_revision));
  const digest = requiredText(object.configDigest ?? object.config_digest, "configDigest", 128);
  if (normalized.configDigest !== digest) throw configError("server_config_digest_mismatch", `server ${serverId} digest mismatch`);
  return normalized;
}

function normalizeTransport(value: unknown, serverId: string): McpTransportConfig {
  const object = canonicalObject(value, `server ${serverId} transport`);
  const kind = String(object.kind ?? object.type ?? object.transport ?? "stdio");
  if (kind === "stdio") {
    const commandValue = object.command;
    const command = Array.isArray(commandValue)
      ? requiredText(commandValue[0], "stdio command", 8_192)
      : requiredText(commandValue, "stdio command", 8_192);
    const argumentsValue = Array.isArray(commandValue)
      ? commandValue.slice(1)
      : Array.isArray(object.arguments)
        ? object.arguments
        : Array.isArray(object.args)
          ? object.args
          : [];
    return {
      kind,
      command,
      arguments: argumentsValue.map((argument, index) => requiredText(argument, `stdio argument ${index}`, 32_768)),
      cwd: optionalText(object.cwd, "stdio cwd", 32_768),
      environmentHandles: normalizeStringRecord(object.environment_handles ?? object.envHandles ?? object.env, "environment handles"),
      inheritEnvironment: normalizeStringArray(object.inherit_environment ?? object.inheritEnvironment, []),
      stderrLimitBytes: normalizeBoundedNumber(object.stderr_limit_bytes ?? object.stderrLimitBytes, 1_048_576, 1_024, 64 * 1024 * 1024),
    };
  }
  if (kind === "streamable_http" || kind === "http" || kind === "sse") {
    const normalizedKind = kind === "http" ? "streamable_http" : kind;
    return {
      kind: normalizedKind,
      url: httpUrl(object.url, "MCP server URL"),
      headers: normalizeStringRecord(object.headers, "headers"),
      credentialHandles: normalizeStringRecord(object.credential_handles ?? object.credentialHandles, "credential handles"),
      allowedRedirectOrigins: normalizeStringArray(object.allowed_redirect_origins ?? object.allowedRedirectOrigins, []),
      maximumRedirects: normalizeBoundedNumber(object.maximum_redirects ?? object.maximumRedirects, 3, 0, 20),
      preferSse: object.prefer_sse === true || object.preferSse === true || normalizedKind === "sse",
    };
  }
  if (kind === "in_process") {
    return {
      kind,
      factoryId: identifier(object.factory_id ?? object.factoryId, "factoryId", 256),
      options: canonicalObject(object.options ?? {}, "in-process options"),
    };
  }
  throw configError("unsupported_transport", `server ${serverId} transport ${kind} is unsupported`);
}

function normalizeRetry(value: unknown): McpRetryPolicy {
  const object = value === undefined ? {} : canonicalObject(value, "retry policy");
  const multiplier = typeof object.multiplier === "number" ? object.multiplier : defaultRetry.multiplier;
  const jitter = typeof object.jitterRatio === "number"
    ? object.jitterRatio
    : typeof object.jitter_ratio === "number"
      ? object.jitter_ratio
      : defaultRetry.jitterRatio;
  if (!Number.isFinite(multiplier) || multiplier < 1 || multiplier > 10) throw configError("invalid_retry_multiplier", "retry multiplier must be in [1, 10]");
  if (!Number.isFinite(jitter) || jitter < 0 || jitter > 1) throw configError("invalid_retry_jitter", "retry jitter must be in [0, 1]");
  return {
    maximumAttempts: normalizeBoundedNumber(object.maximumAttempts ?? object.maximum_attempts, defaultRetry.maximumAttempts, 1, 100),
    initialDelayMs: normalizeBoundedNumber(object.initialDelayMs ?? object.initial_delay_ms, defaultRetry.initialDelayMs, 0, 3_600_000),
    maximumDelayMs: normalizeBoundedNumber(object.maximumDelayMs ?? object.maximum_delay_ms, defaultRetry.maximumDelayMs, 0, 3_600_000),
    multiplier,
    jitterRatio: jitter,
    retryStatusCodes: normalizeNumberArray(object.retryStatusCodes ?? object.retry_status_codes, defaultRetry.retryStatusCodes),
    retryJsonRpcCodes: normalizeNumberArray(object.retryJsonRpcCodes ?? object.retry_json_rpc_codes, defaultRetry.retryJsonRpcCodes),
  };
}

function normalizeTimeouts(value: unknown): McpTimeoutPolicy {
  const object = value === undefined ? {} : canonicalObject(value, "timeout policy");
  return {
    connectMs: timeoutValue(object.connectMs ?? object.connect_ms, defaultTimeouts.connectMs),
    initializeMs: timeoutValue(object.initializeMs ?? object.initialize_ms, defaultTimeouts.initializeMs),
    requestMs: timeoutValue(object.requestMs ?? object.request_ms, defaultTimeouts.requestMs),
    idleMs: timeoutValue(object.idleMs ?? object.idle_ms, defaultTimeouts.idleMs),
    shutdownMs: timeoutValue(object.shutdownMs ?? object.shutdown_ms, defaultTimeouts.shutdownMs),
    authMs: timeoutValue(object.authMs ?? object.auth_ms, defaultTimeouts.authMs),
    taskPollMs: timeoutValue(object.taskPollMs ?? object.task_poll_ms, defaultTimeouts.taskPollMs),
  };
}

function normalizeRateLimit(value: unknown): McpRateLimitPolicy {
  const object = value === undefined ? {} : canonicalObject(value, "rate limit policy");
  return {
    maximumConcurrent: normalizeBoundedNumber(object.maximumConcurrent ?? object.maximum_concurrent, defaultRateLimit.maximumConcurrent, 1, 10_000),
    requestsPerMinute: normalizeBoundedNumber(object.requestsPerMinute ?? object.requests_per_minute, defaultRateLimit.requestsPerMinute, 1, 1_000_000),
    burst: normalizeBoundedNumber(object.burst, defaultRateLimit.burst, 1, 100_000),
    queueCapacity: normalizeBoundedNumber(object.queueCapacity ?? object.queue_capacity, defaultRateLimit.queueCapacity, 0, 1_000_000),
    queueTimeoutMs: normalizeBoundedNumber(object.queueTimeoutMs ?? object.queue_timeout_ms, defaultRateLimit.queueTimeoutMs, 0, 3_600_000),
  };
}

function normalizeSource(value: unknown): McpConfigSource {
  if (value === "managed" || value === "user" || value === "project" || value === "plugin" || value === "session") return value;
  throw configError("invalid_config_source", `invalid MCP config source ${String(value)}`);
}

function normalizeRestart(value: unknown): McpRestartPolicy {
  if (value === undefined || value === null || value === "") return "on_failure";
  if (value === "never" || value === "on_failure" || value === "always") return value;
  throw configError("invalid_restart_policy", `invalid restart policy ${String(value)}`);
}

function normalizeNetwork(value: unknown, transport: McpTransportConfig): McpNetworkPolicy {
  if (value === undefined || value === null || value === "") {
    if (transport.kind === "stdio" || transport.kind === "in_process") return "offline";
    const host = new URL(transport.url).hostname;
    return host === "localhost" || host === "127.0.0.1" || host === "::1" ? "loopback" : "allowlisted";
  }
  if (value === "offline" || value === "loopback" || value === "allowlisted" || value === "unrestricted") return value;
  throw configError("invalid_network_policy", `invalid network policy ${String(value)}`);
}

function normalizeStringRecord(value: unknown, label: string): Record<string, string> {
  if (value === undefined || value === null) return {};
  const object = canonicalObject(value, label);
  const output: Record<string, string> = {};
  for (const [key, child] of Object.entries(object)) output[requiredText(key, `${label} key`, 512)] = requiredText(child, `${label}.${key}`, 32_768);
  return output;
}

function normalizeStringArray(value: unknown, fallback: string[]): string[] {
  if (value === undefined || value === null) return [...fallback];
  if (!Array.isArray(value)) throw configError("invalid_string_array", "expected a string array");
  return [...new Set(value.map((entry, index) => requiredText(entry, `string array item ${index}`, 32_768)))];
}

function normalizePatterns(value: unknown, fallback: string[]): string[] {
  return normalizeStringArray(value, fallback).map((pattern) => pattern.normalize("NFKC"));
}

function normalizeNumberArray(value: unknown, fallback: number[]): number[] {
  if (value === undefined || value === null) return [...fallback];
  if (!Array.isArray(value) || value.some((entry) => !Number.isSafeInteger(entry))) throw configError("invalid_number_array", "expected an integer array");
  return [...new Set(value as number[])];
}

function normalizeBoundedNumber(value: unknown, fallback: number, minimum: number, maximum: number): number {
  if (value === undefined || value === null) return fallback;
  if (!Number.isSafeInteger(value) || (value as number) < minimum || (value as number) > maximum) {
    throw configError("number_out_of_range", `number must be in [${minimum}, ${maximum}]`);
  }
  return value as number;
}

function timeoutValue(value: unknown, fallback: number): number {
  return normalizeBoundedNumber(value, fallback, 100, 86_400_000);
}

function compareLayer(left: McpConfigLayer, right: McpConfigLayer): number {
  const precedence = sourcePrecedence[left.source] - sourcePrecedence[right.source];
  if (precedence !== 0) return precedence;
  const revision = left.revision - right.revision;
  if (revision !== 0) return revision;
  return left.layerId.localeCompare(right.layerId);
}

function diffResult(
  before: Map<string, McpServerConfigRecord>,
  after: Map<string, McpServerConfigRecord>,
  revision: number,
  digest: string,
  source: McpConfigSource,
  layerId: string,
): McpConfigMergeResult {
  const added: string[] = [];
  const updated: string[] = [];
  const removed: string[] = [];
  const unchanged: string[] = [];
  for (const id of new Set([...before.keys(), ...after.keys()])) {
    const oldValue = before.get(id);
    const newValue = after.get(id);
    if (!oldValue && newValue) added.push(id);
    else if (oldValue && !newValue) removed.push(id);
    else if (oldValue?.configDigest !== newValue?.configDigest) updated.push(id);
    else unchanged.push(id);
  }
  return {
    revision,
    digest,
    added: added.sort(),
    updated: updated.sort(),
    removed: removed.sort(),
    unchanged: unchanged.sort(),
    source,
    layerId,
    servers: [...after.values()].sort((left, right) => left.serverId.localeCompare(right.serverId)).map(cloneJson),
  };
}

function configError(code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-config", { code, message }),
    category: "configuration",
    code,
    message,
    retryable: false,
    disposition: "reconfigure",
  });
}

export function legacyServerConfig(value: JsonObject, source: McpConfigSource = "session"): McpServerConfigRecord {
  const id = identifier(value.id, "legacy server id", 256);
  return normalizeServer(id, value, source, 1);
}

export function configAsLegacy(record: McpServerConfigRecord): JsonObject {
  if (record.transport.kind === "stdio") {
    return canonicalJson({
      id: record.serverId,
      transport: "stdio",
      command: [record.transport.command, ...record.transport.arguments],
      cwd: record.transport.cwd,
      envHandles: record.transport.environmentHandles,
      enabled: record.enabled,
      requestTimeoutMs: record.timeouts.requestMs,
      reconnectAttempts: record.retry.maximumAttempts,
      metadata: record.metadata,
    }) as JsonObject;
  }
  if (record.transport.kind === "streamable_http" || record.transport.kind === "sse") {
    return canonicalJson({
      id: record.serverId,
      transport: "http",
      url: record.transport.url,
      headers: record.transport.headers,
      enabled: record.enabled,
      requestTimeoutMs: record.timeouts.requestMs,
      reconnectAttempts: record.retry.maximumAttempts,
      metadata: record.metadata,
    }) as JsonObject;
  }
  if (record.transport.kind !== "in_process") {
    throw configError("unsupported_transport_projection", `cannot project transport ${String((record.transport as { kind?: unknown }).kind)}`);
  }
  return canonicalJson({
    id: record.serverId,
    transport: "in_process",
    factory_id: record.transport.factoryId,
    options: record.transport.options,
    enabled: record.enabled,
    metadata: record.metadata,
  }) as JsonObject;
}

export function expandConfigPlaceholders(
  value: JsonValue,
  handles: Record<string, string>,
  allowedEnvironment: Record<string, string> = {},
): JsonValue {
  if (Array.isArray(value)) return value.map((entry) => expandConfigPlaceholders(entry, handles, allowedEnvironment));
  if (value !== null && typeof value === "object") {
    const output: JsonObject = {};
    for (const [key, child] of Object.entries(value)) output[key] = expandConfigPlaceholders(child, handles, allowedEnvironment);
    return output;
  }
  if (typeof value !== "string") return value;
  return value.replace(/\$\{(handle|env):([A-Za-z_][A-Za-z0-9_.-]*)\}/g, (_match, kind: string, name: string) => {
    const source = kind === "handle" ? handles : allowedEnvironment;
    const replacement = source[name];
    if (replacement === undefined) throw configError("missing_config_placeholder", `missing ${kind} placeholder ${name}`);
    return replacement;
  });
}
