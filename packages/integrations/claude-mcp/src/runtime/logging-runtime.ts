import type { JsonObject, JsonValue } from "../contracts.ts";
import type { McpTransportAdapter } from "../connection/contracts.ts";
import { canonicalJson, cloneJson, deterministicMcpId, monotonicNow, redactMcpSecrets, sha256 } from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";
import { McpProtocolCodec } from "../core/protocol.ts";

export type McpLogLevel = "debug" | "info" | "notice" | "warning" | "error" | "critical" | "alert" | "emergency";

export interface McpLogRecord {
  logId: string;
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  level: McpLogLevel;
  logger: string | null;
  data: JsonValue;
  dataDigest: string;
  sequence: number;
  occurredAt: string;
  metadata: JsonObject;
}

export interface McpLoggingSnapshot {
  version: "zyra.mcp-logging-runtime/v1";
  revision: number;
  levels: Record<string, McpLogLevel>;
  records: McpLogRecord[];
  dropped: number;
  digest: string;
  capturedAt: string;
}

const order: Record<McpLogLevel, number> = {
  debug: 10,
  info: 20,
  notice: 30,
  warning: 40,
  error: 50,
  critical: 60,
  alert: 70,
  emergency: 80,
};

export class McpLoggingRuntime {
  private readonly codec = new McpProtocolCodec();
  private readonly levels = new Map<string, McpLogLevel>();
  private readonly records: McpLogRecord[] = [];
  private readonly listeners = new Set<(record: McpLogRecord) => void | Promise<void>>();
  private readonly now: () => Date;
  private readonly maximumRecords: number;
  private readonly maximumBytes: number;
  private revision = 0;
  private sequence = 0;
  private dropped = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumRecords?: number; maximumBytes?: number; snapshot?: McpLoggingSnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumRecords = options.maximumRecords ?? 20_000;
    this.maximumBytes = options.maximumBytes ?? 128 * 1024;
    if (options.snapshot) this.restore(options.snapshot);
  }

  async setLevel(serverId: string, level: McpLogLevel, transport: McpTransportAdapter, signal?: AbortSignal): Promise<void> {
    requireLevel(level);
    const requestId = deterministicMcpId("mcp-logging-level", { server_id: serverId, level });
    const response = await transport.request({
      requestId,
      method: "logging/setLevel",
      message: this.codec.request(requestId, "logging/setLevel", { level }),
      timeoutMs: 30_000,
      idempotent: true,
      idempotencyKey: requestId,
      authorization: null,
      headers: {},
      signal,
      metadata: {},
    });
    if (!("result" in response.message)) {
      const error = "error" in response.message ? response.message.error : null;
      throw loggingError(serverId, "logging_level_rejected", error?.message ?? "logging/setLevel returned no result");
    }
    this.levels.set(serverId, level);
    this.revision += 1;
  }

  async append(input: {
    serverId: string;
    connectionId: string;
    connectionEpoch: number;
    params: JsonObject;
    metadata?: JsonObject;
  }): Promise<McpLogRecord | null> {
    const level = typeof input.params.level === "string" ? input.params.level as McpLogLevel : null;
    if (!level || !(level in order)) throw loggingError(input.serverId, "logging_level_invalid", `invalid MCP log level ${String(input.params.level)}`);
    const minimum = this.levels.get(input.serverId) ?? "info";
    if (order[level] < order[minimum]) {
      this.dropped += 1;
      return null;
    }
    const raw = input.params.data ?? null;
    const redacted = canonicalJson(redactMcpSecrets(raw)) as JsonValue;
    const bytes = Buffer.byteLength(JSON.stringify(redacted), "utf8");
    if (bytes > this.maximumBytes) throw loggingError(input.serverId, "logging_payload_oversized", `MCP log payload ${bytes} exceeds ${this.maximumBytes}`);
    const occurredAt = this.timestamp();
    this.sequence += 1;
    const base = {
      serverId: input.serverId,
      connectionId: input.connectionId,
      connectionEpoch: input.connectionEpoch,
      level,
      logger: typeof input.params.logger === "string" ? input.params.logger : null,
      data: redacted,
      dataDigest: sha256(redacted),
      sequence: this.sequence,
      occurredAt,
      metadata: cloneJson(input.metadata ?? {}),
    };
    const record: McpLogRecord = {
      logId: deterministicMcpId("mcp-log-record", base, 40),
      ...base,
    };
    this.records.push(record);
    while (this.records.length > this.maximumRecords) {
      this.records.shift();
      this.dropped += 1;
    }
    this.revision += 1;
    for (const listener of this.listeners) await listener(cloneJson(record));
    return cloneJson(record);
  }

  onRecord(listener: (record: McpLogRecord) => void | Promise<void>): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  list(options: { serverId?: string; minimumLevel?: McpLogLevel; limit?: number } = {}): McpLogRecord[] {
    const minimum = options.minimumLevel ? order[options.minimumLevel] : 0;
    const values = this.records.filter((record) => (!options.serverId || record.serverId === options.serverId) && order[record.level] >= minimum);
    return values.slice(-(options.limit ?? values.length)).map(cloneJson);
  }

  snapshot(): McpLoggingSnapshot {
    const withoutDigest = {
      version: "zyra.mcp-logging-runtime/v1" as const,
      revision: this.revision,
      levels: Object.fromEntries([...this.levels.entries()].sort(([left], [right]) => left.localeCompare(right))),
      records: this.list(),
      dropped: this.dropped,
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: sha256(withoutDigest) };
  }

  restore(snapshot: McpLoggingSnapshot): void {
    if (snapshot.version !== "zyra.mcp-logging-runtime/v1") throw loggingError("", "unsupported_logging_snapshot", "unsupported logging snapshot version");
    const { digest, ...withoutDigest } = snapshot;
    if (sha256(withoutDigest) !== digest) throw loggingError("", "logging_snapshot_digest_mismatch", "logging snapshot digest mismatch");
    this.levels.clear();
    this.records.splice(0);
    this.revision = snapshot.revision;
    this.dropped = snapshot.dropped;
    this.sequence = 0;
    for (const [serverId, level] of Object.entries(snapshot.levels)) {
      requireLevel(level);
      this.levels.set(serverId, level);
    }
    for (const record of snapshot.records.slice(-this.maximumRecords)) {
      this.records.push(cloneJson(record));
      this.sequence = Math.max(this.sequence, record.sequence);
    }
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function requireLevel(level: string): asserts level is McpLogLevel {
  if (!(level in order)) throw loggingError("", "logging_level_invalid", `invalid MCP log level ${level}`);
}

function loggingError(serverId: string, code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-logging", { server_id: serverId, code, message }),
    category: "protocol",
    code,
    message,
    serverId,
    retryable: false,
    disposition: "terminal",
  });
}
