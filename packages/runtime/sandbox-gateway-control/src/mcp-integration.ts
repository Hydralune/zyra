import {
  canonicalize,
  digestBytes,
  digestValue,
  stableId,
  type GatewayOutcome,
  type WorkerGatewayIdentity,
} from "./integration-contracts.ts";

export interface McpExecutionProvenance {
  readonly namespace: string;
  readonly serverId: string;
  readonly toolName: string;
  readonly version: string;
  readonly externalBoundary: true;
  readonly requiresExactGrant: true;
}

export interface McpGatewayExchange {
  readonly schema: "zyra.gateway-mcp-exchange.v1";
  readonly exchangeId: string;
  readonly identity: WorkerGatewayIdentity;
  readonly provenance: McpExecutionProvenance;
  readonly toolCallId: string;
  readonly requestDigest: string;
  readonly responseDigest: string;
  readonly outcome: GatewayOutcome;
  readonly redactionCount: number;
  readonly binaryCount: number;
  readonly controlMutations: readonly string[];
  readonly artifactRefs: readonly string[];
  readonly startedAt: number;
  readonly finishedAt: number;
}

export interface McpResultInspection {
  readonly allowed: boolean;
  readonly quarantine: boolean;
  readonly safeValue: unknown;
  readonly responseDigest: string;
  readonly contentBytes: number;
  readonly redactionCount: number;
  readonly binaryCount: number;
  readonly controlMutations: readonly string[];
  readonly reason: string;
}

const CONTROL_KEYS = new Set([
  "permission_mode",
  "permission_rules",
  "project_rules",
  "mcp_config",
  "mcp_servers",
  "shell_profile",
  "allowed_commands",
  "bypass_permissions",
]);

const SECRET_KEY =
  /(?:token|secret|password|passwd|api[_-]?key|credential|cookie|authorization)/i;

export function inspectMcpResult(
  value: unknown,
  options: { readonly maximumBytes?: number } = {},
): McpResultInspection {
  const counters = { redactions: 0, binary: 0 };
  const controlMutations: string[] = [];
  const safeValue = sanitize(value, [], counters, controlMutations);
  const serialized = JSON.stringify(canonicalize(safeValue));
  const contentBytes = Buffer.byteLength(serialized, "utf8");
  const maximumBytes = options.maximumBytes ?? 16 * 1024 * 1024;
  const overBudget = contentBytes > maximumBytes;
  const allowed = controlMutations.length === 0 && !overBudget;
  const quarantine = counters.binary > 0 || !allowed;
  const reason =
    controlMutations.length > 0
      ? "MCP result attempts to mutate protected control state"
      : overBudget
        ? "MCP result exceeds the gateway budget"
        : counters.binary > 0
          ? "MCP binary result requires quarantine"
          : "MCP result passed gateway inspection";
  return Object.freeze({
    allowed,
    quarantine,
    safeValue,
    responseDigest: digestValue(safeValue),
    contentBytes,
    redactionCount: counters.redactions,
    binaryCount: counters.binary,
    controlMutations: Object.freeze(controlMutations),
    reason,
  });
}

export function createMcpExchange(input: {
  readonly identity: WorkerGatewayIdentity;
  readonly provenance: McpExecutionProvenance;
  readonly toolCallId: string;
  readonly arguments: unknown;
  readonly inspection: McpResultInspection;
  readonly artifactRefs?: readonly string[];
  readonly startedAt: number;
  readonly finishedAt: number;
}): Readonly<McpGatewayExchange> {
  if (!input.provenance.externalBoundary) {
    throw new Error("MCP provenance lost its external boundary");
  }
  if (!input.provenance.requiresExactGrant) {
    throw new Error("MCP provenance lost exact-grant enforcement");
  }
  if (input.finishedAt < input.startedAt) {
    throw new Error("MCP exchange finished before it started");
  }
  const requestDigest = digestValue({
    serverId: input.provenance.serverId,
    toolName: input.provenance.toolName,
    version: input.provenance.version,
    arguments: input.arguments,
  });
  const outcome: GatewayOutcome = !input.inspection.allowed
    ? "denied"
    : input.inspection.quarantine
      ? "quarantined"
      : "committed";
  return Object.freeze({
    schema: "zyra.gateway-mcp-exchange.v1",
    exchangeId: stableId(
      "gateway-mcp-exchange",
      input.identity,
      input.toolCallId,
      requestDigest,
    ),
    identity: Object.freeze({ ...input.identity }),
    provenance: Object.freeze({ ...input.provenance }),
    toolCallId: input.toolCallId,
    requestDigest,
    responseDigest: input.inspection.responseDigest,
    outcome,
    redactionCount: input.inspection.redactionCount,
    binaryCount: input.inspection.binaryCount,
    controlMutations: input.inspection.controlMutations,
    artifactRefs: Object.freeze([...(input.artifactRefs ?? [])]),
    startedAt: input.startedAt,
    finishedAt: input.finishedAt,
  });
}

function sanitize(
  value: unknown,
  path: readonly string[],
  counters: { redactions: number; binary: number },
  controlMutations: string[],
): unknown {
  if (value instanceof Uint8Array) {
    counters.binary += 1;
    return Object.freeze({
      binary: true,
      contentBytes: value.byteLength,
      contentDigest: digestBytes(value),
    });
  }
  if (Array.isArray(value)) {
    return Object.freeze(
      value.map((item, index) =>
        sanitize(item, [...path, String(index)], counters, controlMutations),
      ),
    );
  }
  if (value !== null && typeof value === "object") {
    const safe: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value)) {
      const nextPath = [...path, key];
      const normalized = key.toLowerCase();
      if (CONTROL_KEYS.has(normalized)) {
        controlMutations.push(nextPath.join("."));
      }
      if (SECRET_KEY.test(key)) {
        counters.redactions += 1;
        safe[key] = "[REDACTED]";
      } else {
        safe[key] = sanitize(item, nextPath, counters, controlMutations);
      }
    }
    return Object.freeze(safe);
  }
  return canonicalize(value);
}
