import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicMcpId, monotonicNow, sha256 } from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";

export interface McpInstructionRecord {
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  catalogRevision: number;
  text: string;
  digest: string;
  tokenEstimate: number;
  appliedAt: string;
  metadata: JsonObject;
}

export interface McpInstructionDelta {
  deltaId: string;
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  priorRevision: number;
  revision: number;
  priorDigest: string | null;
  digest: string;
  operation: "add" | "replace" | "remove" | "unchanged";
  text: string;
  tokenDelta: number;
  appliedAt: string;
  metadata: JsonObject;
}

export interface McpInstructionSnapshot {
  version: "zyra.mcp-instruction-runtime/v1";
  revision: number;
  records: McpInstructionRecord[];
  digest: string;
  capturedAt: string;
}

export class McpInstructionRuntime {
  private readonly records = new Map<string, McpInstructionRecord>();
  private readonly now: () => Date;
  private readonly maximumInstructionBytes: number;
  private readonly maximumTotalTokens: number;
  private revision = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumInstructionBytes?: number; maximumTotalTokens?: number; snapshot?: McpInstructionSnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumInstructionBytes = options.maximumInstructionBytes ?? 1024 * 1024;
    this.maximumTotalTokens = options.maximumTotalTokens ?? 200_000;
    if (options.snapshot) this.restore(options.snapshot);
  }

  applyDelta(input: {
    serverId: string;
    connectionId: string;
    connectionEpoch: number;
    catalogRevision: number;
    instructions: string | null;
    metadata?: JsonObject;
  }): McpInstructionDelta {
    const prior = this.records.get(input.serverId) ?? null;
    if (prior && input.connectionId === prior.connectionId && input.connectionEpoch < prior.connectionEpoch) {
      throw instructionError(input.serverId, "stale_instruction_epoch", `instruction epoch ${input.connectionEpoch} is older than ${prior.connectionEpoch}`);
    }
    if (prior && input.connectionId === prior.connectionId && input.catalogRevision < prior.catalogRevision) {
      throw instructionError(input.serverId, "stale_instruction_revision", `instruction revision ${input.catalogRevision} is older than ${prior.catalogRevision}`);
    }
    const text = normalizeInstructions(input.instructions ?? "");
    const bytes = Buffer.byteLength(text, "utf8");
    if (bytes > this.maximumInstructionBytes) throw instructionError(input.serverId, "instructions_too_large", `instructions exceed ${this.maximumInstructionBytes} bytes`);
    const tokenEstimate = estimateTokens(text);
    const priorTokens = prior?.tokenEstimate ?? 0;
    const totalTokens = [...this.records.values()].reduce((total, record) => total + record.tokenEstimate, 0) - priorTokens + tokenEstimate;
    if (totalTokens > this.maximumTotalTokens) throw instructionError(input.serverId, "instruction_budget_exceeded", `MCP instructions exceed total token budget ${this.maximumTotalTokens}`);
    const digest = sha256(text);
    const operation = !prior && text ? "add" : prior && !text ? "remove" : prior?.digest === digest ? "unchanged" : "replace";
    const appliedAt = this.timestamp();
    this.revision += 1;
    if (text) {
      this.records.set(input.serverId, {
        serverId: input.serverId,
        connectionId: input.connectionId,
        connectionEpoch: input.connectionEpoch,
        catalogRevision: input.catalogRevision,
        text,
        digest,
        tokenEstimate,
        appliedAt,
        metadata: cloneJson(input.metadata ?? {}),
      });
    } else {
      this.records.delete(input.serverId);
    }
    return {
      deltaId: deterministicMcpId("mcp-instruction-delta", {
        server_id: input.serverId,
        connection_id: input.connectionId,
        epoch: input.connectionEpoch,
        revision: input.catalogRevision,
        prior_digest: prior?.digest ?? null,
        digest,
      }),
      serverId: input.serverId,
      connectionId: input.connectionId,
      connectionEpoch: input.connectionEpoch,
      priorRevision: prior?.catalogRevision ?? 0,
      revision: input.catalogRevision,
      priorDigest: prior?.digest ?? null,
      digest,
      operation,
      text,
      tokenDelta: tokenEstimate - priorTokens,
      appliedAt,
      metadata: cloneJson(input.metadata ?? {}),
    };
  }

  get(serverId: string): McpInstructionRecord | null {
    const record = this.records.get(serverId);
    return record ? cloneJson(record) : null;
  }

  compose(serverIds?: string[]): { text: string; digest: string; tokenEstimate: number; servers: string[] } {
    const selected = (serverIds ?? [...this.records.keys()])
      .map((serverId) => this.records.get(serverId))
      .filter((record): record is McpInstructionRecord => Boolean(record))
      .sort((left, right) => left.serverId.localeCompare(right.serverId));
    const text = selected.map((record) => `<mcp-server id="${escapeXml(record.serverId)}">\n${record.text}\n</mcp-server>`).join("\n\n");
    return {
      text,
      digest: sha256(selected.map((record) => ({ server_id: record.serverId, digest: record.digest }))),
      tokenEstimate: selected.reduce((total, record) => total + record.tokenEstimate, 0),
      servers: selected.map((record) => record.serverId),
    };
  }

  snapshot(): McpInstructionSnapshot {
    const withoutDigest = {
      version: "zyra.mcp-instruction-runtime/v1" as const,
      revision: this.revision,
      records: [...this.records.values()].sort((left, right) => left.serverId.localeCompare(right.serverId)).map(cloneJson),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: sha256(withoutDigest) };
  }

  restore(snapshot: McpInstructionSnapshot): void {
    if (snapshot.version !== "zyra.mcp-instruction-runtime/v1") throw instructionError("", "unsupported_instruction_snapshot", "unsupported instruction snapshot version");
    const { digest, ...withoutDigest } = snapshot;
    if (sha256(withoutDigest) !== digest) throw instructionError("", "instruction_snapshot_digest_mismatch", "instruction snapshot digest mismatch");
    this.records.clear();
    this.revision = snapshot.revision;
    for (const record of snapshot.records) this.records.set(record.serverId, cloneJson(record));
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function normalizeInstructions(value: string): string {
  return value.normalize("NFC").replace(/\r\n?/g, "\n").replace(/[ \t]+$/gm, "").trim();
}

function estimateTokens(value: string): number {
  if (!value) return 0;
  const words = value.match(/[\p{L}\p{N}_]+|[^\s\p{L}\p{N}_]/gu) ?? [];
  return Math.ceil(words.reduce((total, word) => total + (word.length > 12 ? Math.ceil(word.length / 4) : 1), 0) * 1.08);
}

function escapeXml(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function instructionError(serverId: string, code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-instructions", { server_id: serverId, code, message }),
    category: code.includes("stale") ? "conflict" : "capability",
    code,
    message,
    serverId,
    retryable: code.includes("stale"),
    disposition: code.includes("stale") ? "retry_same_connection" : "replan",
  });
}
