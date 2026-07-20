import { createInterface } from "node:readline";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import type {
  IntegrationDefinition,
  ModelDefinition,
  ProviderDefinition,
  ProviderDispatchRequest,
  RouteRequest,
} from "./contracts.ts";
import { canonicalize } from "./canonical.ts";
import { ProviderControlPlane } from "./control-plane.ts";
import type { CredentialRegistration } from "./credentials.ts";
import { ProviderControlPlaneError } from "./errors.ts";

export const RPC_PROTOCOL = "zyra.provider-control-plane.rpc/v1" as const;

interface RpcRequest {
  readonly protocol: typeof RPC_PROTOCOL;
  readonly requestId: string;
  readonly operation: string;
  readonly payload: Record<string, unknown>;
}

interface RpcResponse {
  readonly protocol: typeof RPC_PROTOCOL;
  readonly requestId: string;
  readonly ok: boolean;
  readonly result?: unknown;
  readonly error?: {
    readonly code: string;
    readonly message: string;
    readonly detail: unknown;
  };
}

export class ProviderControlPlaneRpcServer {
  readonly controlPlane: ProviderControlPlane;

  constructor(controlPlane: ProviderControlPlane) {
    this.controlPlane = controlPlane;
  }

  async handle(raw: unknown): Promise<RpcResponse> {
    let requestId = "unknown";
    try {
      const request = parseRequest(raw);
      requestId = request.requestId;
      const result = await this.dispatch(request.operation, request.payload);
      return { protocol: RPC_PROTOCOL, requestId, ok: true, result: canonicalize(result) };
    } catch (error) {
      const failure = error instanceof ProviderControlPlaneError
        ? { code: error.kind, message: error.message, detail: error.safe() }
        : { code: "provider_rpc_invalid_request", message: error instanceof Error ? error.message : String(error), detail: {} };
      return { protocol: RPC_PROTOCOL, requestId, ok: false, error: failure };
    }
  }

  private async dispatch(operation: string, payload: Record<string, unknown>): Promise<unknown> {
    switch (operation) {
      case "health":
        return this.controlPlane.health();
      case "catalog.snapshot":
        return this.controlPlane.catalog.snapshot();
      case "catalog.compat_v1":
        return this.controlPlane.catalog.compatibilityV1();
      case "catalog.provider.get":
        return this.controlPlane.catalog.provider(String(payload.providerId ?? ""));
      case "catalog.provider.list":
        return this.controlPlane.catalog.providers({ availableOnly: payload.availableOnly === true });
      case "catalog.provider.upsert":
        return this.controlPlane.upsertProvider(
          payload.provider as unknown as ProviderDefinition,
          optionalRevision(payload.expectedRevision),
        );
      case "catalog.provider.remove":
        return this.controlPlane.catalog.removeProvider(
          String(payload.providerId ?? ""),
          optionalRevision(payload.expectedRevision),
        );
      case "catalog.model.get":
        return this.controlPlane.catalog.model(String(payload.providerId ?? ""), String(payload.modelId ?? ""));
      case "catalog.model.list":
        return this.controlPlane.catalog.models({
          providerId: typeof payload.providerId === "string" ? payload.providerId : undefined,
          availableOnly: payload.availableOnly === true,
        });
      case "catalog.model.upsert":
        return this.controlPlane.upsertModel(
          payload.model as unknown as ModelDefinition,
          optionalRevision(payload.expectedRevision),
        );
      case "catalog.model.remove":
        return this.controlPlane.catalog.removeModel(
          String(payload.providerId ?? ""),
          String(payload.modelId ?? ""),
          optionalRevision(payload.expectedRevision),
        );
      case "integration.get":
        return this.controlPlane.catalog.integration(String(payload.integrationId ?? ""));
      case "integration.list":
        return this.controlPlane.catalog.integrations();
      case "integration.upsert":
        return this.controlPlane.upsertIntegration(
          payload.integration as unknown as IntegrationDefinition,
          optionalRevision(payload.expectedRevision),
        );
      case "credential.register":
        rejectSecretBytes(payload);
        return this.controlPlane.registerCredential(payload.credential as unknown as CredentialRegistration);
      case "credential.get":
        return this.controlPlane.credentials.get(String(payload.credentialId ?? ""));
      case "credential.list":
        return this.controlPlane.credentials.list(typeof payload.providerId === "string" ? payload.providerId : undefined);
      case "credential.revoke":
        return this.controlPlane.credentials.revoke(
          String(payload.credentialId ?? ""),
          requiredInteger(payload.expectedVersion, "expectedVersion"),
        );
      case "credential.block":
        return this.controlPlane.credentials.block(
          String(payload.credentialId ?? ""),
          requiredInteger(payload.expectedVersion, "expectedVersion"),
          String(payload.reason ?? "blocked by control API"),
        );
      case "route.acquire":
        return this.controlPlane.acquireRoute(
          payload.request as unknown as RouteRequest,
          typeof payload.previousRouteId === "string" ? payload.previousRouteId : null,
        );
      case "route.get":
        return this.controlPlane.routes.require(String(payload.routeId ?? ""));
      case "route.list":
        return this.controlPlane.routes.list(
          typeof payload.runId === "string" ? payload.runId : undefined,
          typeof payload.taskId === "string" ? payload.taskId : undefined,
        );
      case "dispatch":
        rejectSecretBytes(payload);
        return this.controlPlane.dispatch(payload.request as unknown as ProviderDispatchRequest);
      case "dispatch.attempts":
        return this.controlPlane.store.listAttempts(String(payload.dispatchId ?? ""));
      case "events.list":
        return this.controlPlane.store.listEvents(
          typeof payload.runId === "string" ? payload.runId : undefined,
          typeof payload.taskId === "string" ? payload.taskId : undefined,
        );
      default:
        throw new TypeError(`unsupported provider RPC operation: ${operation}`);
    }
  }
}

export async function runStdioServer(databasePath: string): Promise<void> {
  const server = new ProviderControlPlaneRpcServer(new ProviderControlPlane({ databasePath }));
  const lines = createInterface({ input: process.stdin, crlfDelay: Number.POSITIVE_INFINITY });
  try {
    for await (const line of lines) {
      if (!line.trim()) continue;
      let decoded: unknown;
      try {
        decoded = JSON.parse(line);
      } catch (error) {
        const response: RpcResponse = {
          protocol: RPC_PROTOCOL,
          requestId: "unknown",
          ok: false,
          error: { code: "provider_rpc_invalid_json", message: error instanceof Error ? error.message : String(error), detail: {} },
        };
        process.stdout.write(`${JSON.stringify(response)}\n`);
        continue;
      }
      process.stdout.write(`${JSON.stringify(await server.handle(decoded))}\n`);
    }
  } finally {
    server.controlPlane.close();
  }
}

function parseRequest(raw: unknown): RpcRequest {
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) throw new TypeError("provider RPC request must be an object");
  const value = raw as Record<string, unknown>;
  if (value.protocol !== RPC_PROTOCOL) throw new TypeError("provider RPC protocol mismatch");
  if (typeof value.requestId !== "string" || !value.requestId.trim()) throw new TypeError("provider RPC requestId is required");
  if (typeof value.operation !== "string" || !value.operation.trim()) throw new TypeError("provider RPC operation is required");
  const payload = value.payload;
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) throw new TypeError("provider RPC payload must be an object");
  return { protocol: RPC_PROTOCOL, requestId: value.requestId, operation: value.operation, payload: payload as Record<string, unknown> };
}

function optionalRevision(value: unknown): number | null {
  return value === null || value === undefined ? null : requiredInteger(value, "expectedRevision");
}

function requiredInteger(value: unknown, name: string): number {
  if (!Number.isSafeInteger(value) || Number(value) < 0) throw new TypeError(`${name} must be a non-negative safe integer`);
  return Number(value);
}

function rejectSecretBytes(payload: Record<string, unknown>): void {
  const text = JSON.stringify(payload);
  if (/"(?:apiKey|api_key|secret|accessToken|access_token|refreshToken|refresh_token)"\s*:/i.test(text)) {
    throw new TypeError("provider RPC rejects inline secret bytes; submit an approved secretRef instead");
  }
}

const entry = process.argv[1] ? pathToFileURL(resolve(process.argv[1])).href : "";
if (import.meta.url === entry) {
  const databasePath = process.argv[2] ?? process.env.ZYRA_PROVIDER_CONTROL_DB;
  if (!databasePath) throw new Error("provider control-plane database path is required");
  await runStdioServer(resolve(databasePath));
}
