import type { JsonObject, JsonValue } from "../contracts.ts";
import {
  canonicalJson,
  cloneJson,
  deterministicMcpId,
  hashChain,
  monotonicNow,
  sha256,
  sortedUnique,
  verifyHashChain,
} from "../core/canonical.ts";
import { McpRuntimeError } from "../core/failure.ts";
import type {
  McpInitializeResult,
  McpPrompt,
  McpResource,
  McpResourceTemplate,
  McpServerCapabilities,
  McpTool,
} from "../core/protocol.ts";

export type McpCatalogKind = "tool" | "resource" | "resource_template" | "prompt" | "instruction";
export type McpCatalogEventKind =
  | "snapshot_applied"
  | "capability_added"
  | "capability_updated"
  | "capability_removed"
  | "list_invalidated"
  | "instructions_updated"
  | "server_removed";

export interface McpCatalogServerSnapshot {
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  protocolVersion: string;
  serverInfo: JsonObject;
  capabilities: McpServerCapabilities;
  instructions: string;
  tools: McpTool[];
  resources: McpResource[];
  resourceTemplates: McpResourceTemplate[];
  prompts: McpPrompt[];
  invalidatedKinds: McpCatalogKind[];
  revision: number;
  digest: string;
  refreshedAt: string;
  metadata: JsonObject;
}

export interface McpCatalogSnapshotInput {
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  initialize: McpInitializeResult;
  tools: McpTool[];
  resources: McpResource[];
  resourceTemplates: McpResourceTemplate[];
  prompts: McpPrompt[];
  expectedRevision?: number;
  metadata?: JsonObject;
}

export interface McpCatalogNotification {
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  method: string;
  params: JsonObject;
  observedAt?: string;
  metadata?: JsonObject;
}

export interface McpCatalogEvent {
  eventId: string;
  sequence: number;
  serverId: string;
  connectionId: string;
  connectionEpoch: number;
  revision: number;
  kind: McpCatalogEventKind;
  capabilityKind: McpCatalogKind | null;
  capabilityKey: string | null;
  beforeDigest: string | null;
  afterDigest: string | null;
  details: JsonObject;
  occurredAt: string;
  previousHash: string;
  eventHash: string;
}

export interface McpCatalogDiff {
  serverId: string;
  priorRevision: number;
  revision: number;
  added: Record<McpCatalogKind, string[]>;
  updated: Record<McpCatalogKind, string[]>;
  removed: Record<McpCatalogKind, string[]>;
  invalidated: McpCatalogKind[];
  events: McpCatalogEvent[];
  digest: string;
}

export interface McpCapabilityCatalogSnapshot {
  version: "zyra.mcp-capability-catalog/v1";
  revision: number;
  sequence: number;
  servers: McpCatalogServerSnapshot[];
  events: McpCatalogEvent[];
  digest: string;
  capturedAt: string;
}

const eventGenesis = "sha256:zyra-mcp-catalog-genesis";
const catalogKinds: McpCatalogKind[] = ["tool", "resource", "resource_template", "prompt", "instruction"];

export class McpCapabilityCatalog {
  private readonly servers = new Map<string, McpCatalogServerSnapshot>();
  private readonly events: McpCatalogEvent[] = [];
  private readonly listeners = new Set<(event: McpCatalogEvent) => void | Promise<void>>();
  private readonly now: () => Date;
  private readonly maximumEvents: number;
  private revision = 0;
  private sequence = 0;
  private lastTimestamp: string | null = null;

  constructor(options: { now?: () => Date; maximumEvents?: number; snapshot?: McpCapabilityCatalogSnapshot | null } = {}) {
    this.now = options.now ?? (() => new Date());
    this.maximumEvents = options.maximumEvents ?? 100_000;
    if (options.snapshot) this.restore(options.snapshot);
  }

  applySnapshot(inputValue: McpCatalogSnapshotInput): McpCatalogDiff {
    const input = normalizeInput(inputValue);
    const prior = this.servers.get(input.serverId) ?? null;
    if (prior && input.expectedRevision !== undefined && input.expectedRevision !== prior.revision) {
      throw catalogError(input.serverId, "catalog_revision_conflict", `catalog revision ${input.expectedRevision} does not match ${prior.revision}`);
    }
    if (prior && input.connectionId === prior.connectionId && input.connectionEpoch < prior.connectionEpoch) {
      throw catalogError(input.serverId, "stale_connection_epoch", `catalog epoch ${input.connectionEpoch} is older than ${prior.connectionEpoch}`);
    }
    const nextRevision = (prior?.revision ?? 0) + 1;
    const nextWithoutDigest = {
      serverId: input.serverId,
      connectionId: input.connectionId,
      connectionEpoch: input.connectionEpoch,
      protocolVersion: input.initialize.protocolVersion,
      serverInfo: canonicalJson(input.initialize.serverInfo) as JsonObject,
      capabilities: cloneJson(input.initialize.capabilities),
      instructions: input.initialize.instructions ?? "",
      tools: sortedUnique(input.tools, (tool) => tool.name).map(cloneJson),
      resources: sortedUnique(input.resources, (resource) => resource.uri).map(cloneJson),
      resourceTemplates: sortedUnique(input.resourceTemplates, (resource) => resource.uriTemplate).map(cloneJson),
      prompts: sortedUnique(input.prompts, (prompt) => prompt.name).map(cloneJson),
      invalidatedKinds: [] as McpCatalogKind[],
      revision: nextRevision,
      refreshedAt: this.timestamp(),
      metadata: cloneJson(input.metadata ?? {}),
    };
    const next: McpCatalogServerSnapshot = {
      ...nextWithoutDigest,
      digest: sha256(nextWithoutDigest),
    };
    const diff = emptyDiff(input.serverId, prior?.revision ?? 0, nextRevision);
    const emitted: McpCatalogEvent[] = [];
    for (const kind of catalogKinds) {
      const before = entriesFor(prior, kind);
      const after = entriesFor(next, kind);
      const keys = new Set([...before.keys(), ...after.keys()]);
      for (const key of [...keys].sort()) {
        const oldValue = before.get(key);
        const newValue = after.get(key);
        if (!oldValue && newValue) {
          diff.added[kind].push(key);
          emitted.push(this.emit(next, "capability_added", kind, key, null, sha256(newValue), { value: newValue }));
        } else if (oldValue && !newValue) {
          diff.removed[kind].push(key);
          emitted.push(this.emit(next, "capability_removed", kind, key, sha256(oldValue), null, {}));
        } else if (oldValue && newValue && sha256(oldValue) !== sha256(newValue)) {
          diff.updated[kind].push(key);
          emitted.push(this.emit(next, "capability_updated", kind, key, sha256(oldValue), sha256(newValue), { value: newValue }));
        }
      }
    }
    emitted.push(this.emit(next, "snapshot_applied", null, null, prior?.digest ?? null, next.digest, {
      added_count: Object.values(diff.added).reduce((total, values) => total + values.length, 0),
      updated_count: Object.values(diff.updated).reduce((total, values) => total + values.length, 0),
      removed_count: Object.values(diff.removed).reduce((total, values) => total + values.length, 0),
    }));
    this.servers.set(input.serverId, next);
    this.revision += 1;
    diff.events = emitted.map(cloneJson);
    diff.digest = sha256({ ...diff, digest: undefined });
    return diff;
  }

  applyNotification(notificationValue: McpCatalogNotification): McpCatalogDiff {
    const notification = cloneJson(notificationValue);
    const server = this.require(notification.serverId);
    if (notification.connectionId !== server.connectionId || notification.connectionEpoch !== server.connectionEpoch) {
      throw catalogError(notification.serverId, "notification_connection_mismatch", "catalog notification belongs to another connection epoch");
    }
    const kind = notificationKind(notification.method);
    const diff = emptyDiff(server.serverId, server.revision, server.revision + 1);
    const events: McpCatalogEvent[] = [];
    const next: McpCatalogServerSnapshot = cloneJson(server);
    next.revision += 1;
    next.refreshedAt = notification.observedAt ?? this.timestamp();
    if (kind) {
      next.invalidatedKinds = [...new Set([...next.invalidatedKinds, kind])].sort();
      diff.invalidated.push(kind);
      events.push(this.emit(next, "list_invalidated", kind, null, server.digest, null, {
        method: notification.method,
        params: notification.params,
      }));
    } else if (notification.method === "notifications/message") {
      next.metadata = {
        ...next.metadata,
        last_server_message: notification.params,
        last_server_message_at: next.refreshedAt,
      };
    } else {
      throw catalogError(notification.serverId, "unsupported_catalog_notification", `notification ${notification.method} does not mutate catalog state`);
    }
    next.digest = serverDigest(next);
    this.servers.set(server.serverId, next);
    this.revision += 1;
    diff.events = events;
    diff.digest = sha256({ ...diff, digest: undefined });
    return diff;
  }

  applyIncremental(
    serverId: string,
    kind: Exclude<McpCatalogKind, "instruction">,
    values: readonly (McpTool | McpResource | McpResourceTemplate | McpPrompt)[],
    expectedRevision: number,
  ): McpCatalogDiff {
    const server = this.require(serverId);
    if (server.revision !== expectedRevision) throw catalogError(serverId, "catalog_revision_conflict", `catalog revision ${expectedRevision} does not match ${server.revision}`);
    const next = cloneJson(server);
    if (kind === "tool") next.tools = sortedUnique(values as McpTool[], (value) => value.name).map(cloneJson);
    else if (kind === "resource") next.resources = sortedUnique(values as McpResource[], (value) => value.uri).map(cloneJson);
    else if (kind === "resource_template") next.resourceTemplates = sortedUnique(values as McpResourceTemplate[], (value) => value.uriTemplate).map(cloneJson);
    else next.prompts = sortedUnique(values as McpPrompt[], (value) => value.name).map(cloneJson);
    next.invalidatedKinds = next.invalidatedKinds.filter((value) => value !== kind);
    return this.applySnapshot({
      serverId,
      connectionId: next.connectionId,
      connectionEpoch: next.connectionEpoch,
      initialize: {
        protocolVersion: next.protocolVersion,
        capabilities: next.capabilities,
        serverInfo: parseServerInfo(next.serverInfo),
        instructions: next.instructions,
        meta: {},
      },
      tools: next.tools,
      resources: next.resources,
      resourceTemplates: next.resourceTemplates,
      prompts: next.prompts,
      expectedRevision,
      metadata: next.metadata,
    });
  }

  updateInstructions(serverId: string, instructions: string, expectedRevision: number): McpCatalogEvent {
    const server = this.require(serverId);
    if (server.revision !== expectedRevision) throw catalogError(serverId, "catalog_revision_conflict", `catalog revision ${expectedRevision} does not match ${server.revision}`);
    const next = cloneJson(server);
    const beforeDigest = sha256(next.instructions);
    next.instructions = instructions;
    next.revision += 1;
    next.refreshedAt = this.timestamp();
    next.invalidatedKinds = next.invalidatedKinds.filter((kind) => kind !== "instruction");
    next.digest = serverDigest(next);
    const event = this.emit(next, "instructions_updated", "instruction", "instructions", beforeDigest, sha256(instructions), {});
    this.servers.set(serverId, next);
    this.revision += 1;
    return event;
  }

  remove(serverId: string, reason = "connection_removed"): McpCatalogEvent | null {
    const server = this.servers.get(serverId);
    if (!server) return null;
    const event = this.emit(server, "server_removed", null, null, server.digest, null, { reason });
    this.servers.delete(serverId);
    this.revision += 1;
    return event;
  }

  get(serverId: string): McpCatalogServerSnapshot | null {
    const value = this.servers.get(serverId);
    return value ? cloneJson(value) : null;
  }

  require(serverId: string): McpCatalogServerSnapshot {
    const value = this.servers.get(serverId);
    if (!value) throw catalogError(serverId, "catalog_not_found", `MCP catalog for ${serverId} was not found`);
    return cloneJson(value);
  }

  list(): McpCatalogServerSnapshot[] {
    return [...this.servers.values()].sort((left, right) => left.serverId.localeCompare(right.serverId)).map(cloneJson);
  }

  findTool(serverId: string, name: string): McpTool | null {
    return this.servers.get(serverId)?.tools.find((tool) => tool.name === name) ?? null;
  }

  findResource(serverId: string, uri: string): McpResource | null {
    return this.servers.get(serverId)?.resources.find((resource) => resource.uri === uri) ?? null;
  }

  findPrompt(serverId: string, name: string): McpPrompt | null {
    return this.servers.get(serverId)?.prompts.find((prompt) => prompt.name === name) ?? null;
  }

  onEvent(listener: (event: McpCatalogEvent) => void | Promise<void>): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  snapshot(): McpCapabilityCatalogSnapshot {
    const withoutDigest = {
      version: "zyra.mcp-capability-catalog/v1" as const,
      revision: this.revision,
      sequence: this.sequence,
      servers: this.list(),
      events: this.events.map(cloneJson),
      capturedAt: this.timestamp(),
    };
    return { ...withoutDigest, digest: sha256(withoutDigest) };
  }

  restore(snapshot: McpCapabilityCatalogSnapshot): void {
    if (snapshot.version !== "zyra.mcp-capability-catalog/v1") throw catalogError("", "unsupported_catalog_snapshot", "unsupported catalog snapshot version");
    const { digest, ...withoutDigest } = snapshot;
    if (sha256(withoutDigest) !== digest) throw catalogError("", "catalog_snapshot_digest_mismatch", "catalog snapshot digest mismatch");
    verifyHashChain(snapshot.events, eventGenesis, (event) => event.previousHash, (event) => event.eventHash, eventPayload);
    this.servers.clear();
    this.events.splice(0);
    this.revision = snapshot.revision;
    this.sequence = snapshot.sequence;
    for (const server of snapshot.servers) {
      if (serverDigest(server) !== server.digest) throw catalogError(server.serverId, "server_catalog_digest_mismatch", `catalog digest mismatch for ${server.serverId}`);
      this.servers.set(server.serverId, cloneJson(server));
    }
    for (const event of snapshot.events) this.events.push(cloneJson(event));
  }

  private emit(
    server: McpCatalogServerSnapshot,
    kind: McpCatalogEventKind,
    capabilityKind: McpCatalogKind | null,
    capabilityKey: string | null,
    beforeDigest: string | null,
    afterDigest: string | null,
    details: JsonObject,
  ): McpCatalogEvent {
    this.sequence += 1;
    const previousHash = this.events.at(-1)?.eventHash ?? eventGenesis;
    const base = {
      eventId: deterministicMcpId("mcp-catalog-event", {
        server_id: server.serverId,
        connection_id: server.connectionId,
        sequence: this.sequence,
        kind,
        capability_kind: capabilityKind,
        capability_key: capabilityKey,
      }),
      sequence: this.sequence,
      serverId: server.serverId,
      connectionId: server.connectionId,
      connectionEpoch: server.connectionEpoch,
      revision: server.revision,
      kind,
      capabilityKind,
      capabilityKey,
      beforeDigest,
      afterDigest,
      details: canonicalJson(details) as JsonObject,
      occurredAt: this.timestamp(),
      previousHash,
    };
    const event: McpCatalogEvent = { ...base, eventHash: hashChain(previousHash, eventPayload(base)) };
    this.events.push(event);
    if (this.events.length > this.maximumEvents) this.events.splice(0, this.events.length - this.maximumEvents);
    for (const listener of this.listeners) void Promise.resolve(listener(cloneJson(event)));
    return cloneJson(event);
  }

  private timestamp(): string {
    const value = monotonicNow(this.lastTimestamp, this.now);
    this.lastTimestamp = value;
    return value;
  }
}

function normalizeInput(input: McpCatalogSnapshotInput): McpCatalogSnapshotInput {
  if (!input.serverId || !input.connectionId) throw catalogError(input.serverId, "invalid_catalog_identity", "catalog server and connection ids are required");
  if (!Number.isSafeInteger(input.connectionEpoch) || input.connectionEpoch < 1) throw catalogError(input.serverId, "invalid_connection_epoch", "catalog connection epoch must be positive integer");
  return cloneJson(input);
}

function entriesFor(server: McpCatalogServerSnapshot | null, kind: McpCatalogKind): Map<string, JsonValue> {
  const output = new Map<string, JsonValue>();
  if (!server) return output;
  if (kind === "tool") for (const value of server.tools) output.set(value.name, canonicalJson(value));
  else if (kind === "resource") for (const value of server.resources) output.set(value.uri, canonicalJson(value));
  else if (kind === "resource_template") for (const value of server.resourceTemplates) output.set(value.uriTemplate, canonicalJson(value));
  else if (kind === "prompt") for (const value of server.prompts) output.set(value.name, canonicalJson(value));
  else output.set("instructions", server.instructions);
  return output;
}

function emptyDiff(serverId: string, priorRevision: number, revision: number): McpCatalogDiff {
  const record = (): Record<McpCatalogKind, string[]> => ({
    tool: [],
    resource: [],
    resource_template: [],
    prompt: [],
    instruction: [],
  });
  return {
    serverId,
    priorRevision,
    revision,
    added: record(),
    updated: record(),
    removed: record(),
    invalidated: [],
    events: [],
    digest: "",
  };
}

function notificationKind(method: string): McpCatalogKind | null {
  if (method === "notifications/tools/list_changed") return "tool";
  if (method === "notifications/resources/list_changed") return "resource";
  if (method === "notifications/prompts/list_changed") return "prompt";
  return null;
}

function serverDigest(server: McpCatalogServerSnapshot): string {
  const { digest: _ignored, ...value } = server;
  return sha256(value);
}

function eventPayload(event: Omit<McpCatalogEvent, "eventHash"> | McpCatalogEvent): JsonObject {
  const { eventHash: _ignored, ...value } = event as McpCatalogEvent;
  return canonicalJson(value) as JsonObject;
}

function parseServerInfo(value: JsonObject) {
  return {
    name: typeof value.name === "string" ? value.name : "unknown",
    title: typeof value.title === "string" ? value.title : null,
    version: typeof value.version === "string" ? value.version : "unknown",
    websiteUrl: typeof value.websiteUrl === "string" ? value.websiteUrl : null,
    icons: Array.isArray(value.icons) ? value.icons as never[] : [],
  };
}

function catalogError(serverId: string, code: string, message: string): McpRuntimeError {
  return new McpRuntimeError({
    failureId: deterministicMcpId("mcp-catalog", { server_id: serverId, code, message }),
    category: code.includes("conflict") || code.includes("stale") ? "conflict" : "capability",
    code,
    message,
    serverId,
    retryable: code.includes("conflict") || code.includes("stale"),
    disposition: code.includes("conflict") || code.includes("stale") ? "retry_same_connection" : "replan",
  });
}
