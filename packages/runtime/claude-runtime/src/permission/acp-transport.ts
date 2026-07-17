import type { JsonObject } from "../contracts.ts";
import { cloneJson, deterministicId, digest } from "../e02/canonical.ts";
import type {
  PermissionApprovalResponse,
  PermissionContinuationRecord,
} from "../e02/contracts.ts";

export interface AcpPermissionRequest extends JsonObject {
  protocol: "zyra.acp-permission/v1";
  transportRequestId: string;
  requestId: string;
  sessionId: string;
  sessionRevision: number;
  workerRequestId: string;
  toolCallId: string;
  prompt: string;
  options: Array<{ id: "allow" | "deny"; label: string }>;
  expiresAt: string;
  requestDigest: string;
  metadata: JsonObject;
}
export class AcpPermissionTransport {
  private readonly requests = new Map<string, AcpPermissionRequest>();
  private readonly responses = new Map<string, PermissionApprovalResponse>();

  correlate(
    continuation: PermissionContinuationRecord,
    prompt: string,
    metadata: JsonObject = {},
  ): AcpPermissionRequest {
    const transportRequestId = deterministicId("acp-permission", {
      requestId: continuation.requestId,
      sessionId: continuation.sessionId,
      sessionRevision: continuation.sessionRevision,
      toolCallId: continuation.toolCallId,
      expiresAt: continuation.expiresAt,
    }, 40);
    const base = {
      protocol: "zyra.acp-permission/v1" as const,
      transportRequestId,
      requestId: continuation.requestId,
      sessionId: continuation.sessionId,
      sessionRevision: continuation.sessionRevision,
      workerRequestId: continuation.workerRequestId,
      toolCallId: continuation.toolCallId,
      prompt,
      options: [
        { id: "allow" as const, label: "Allow this exact request" },
        { id: "deny" as const, label: "Deny" },
      ],
      expiresAt: continuation.expiresAt,
      metadata: cloneJson(metadata),
    };
    const request: AcpPermissionRequest = { ...base, requestDigest: digest(base) };
    const existing = this.requests.get(transportRequestId);
    if (existing && existing.requestDigest !== request.requestDigest) throw new Error("ACP permission correlation collision");
    this.requests.set(transportRequestId, request);
    return cloneJson(request);
  }

  decodeResponse(value: JsonObject): PermissionApprovalResponse {
    if (value.protocol !== "zyra.acp-permission/v1") throw new Error("unsupported ACP permission response protocol");
    const transportRequestId = stringValue(value.transportRequestId ?? value.transport_request_id, "transport request id");
    const request = this.requests.get(transportRequestId);
    if (!request) throw new Error(`unknown ACP permission transport request ${transportRequestId}`);
    const effect = value.effect === "allow" || value.effect === "deny" ? value.effect : null;
    if (!effect) throw new Error("ACP permission response effect must be allow or deny");
    const response: PermissionApprovalResponse = {
      responseId: stringValue(value.responseId ?? value.response_id, "response id"),
      requestId: stringValue(value.requestId ?? value.request_id, "request id"),
      runId: stringValue(value.runId ?? value.run_id, "run id"),
      sessionId: stringValue(value.sessionId ?? value.session_id, "session id"),
      sessionRevision: numberValue(value.sessionRevision ?? value.session_revision, "session revision"),
      workerRequestId: stringValue(value.workerRequestId ?? value.worker_request_id, "worker request id"),
      toolCallId: stringValue(value.toolCallId ?? value.tool_call_id, "tool call id"),
      effect,
      responder: stringValue(value.responder, "responder"),
      respondedAt: timestampValue(value.respondedAt ?? value.responded_at),
      metadata: value.metadata && typeof value.metadata === "object" && !Array.isArray(value.metadata)
        ? cloneJson(value.metadata as JsonObject)
        : {},
    };
    if (response.requestId !== request.requestId) throw new Error("ACP permission response request id mismatch");
    if (response.sessionId !== request.sessionId) throw new Error("ACP permission response session mismatch");
    if (response.sessionRevision !== request.sessionRevision) throw new Error("ACP permission response revision mismatch");
    if (response.workerRequestId !== request.workerRequestId) throw new Error("ACP permission response worker request mismatch");
    if (response.toolCallId !== request.toolCallId) throw new Error("ACP permission response tool call mismatch");
    const existing = this.responses.get(response.responseId);
    if (existing && digest(existing) !== digest(response)) throw new Error("ACP permission response id reused with a different payload");
    this.responses.set(response.responseId, response);
    return cloneJson(response);
  }

  snapshot(): JsonObject {
    return {
      version: "zyra.e02-acp-permission-transport/v1",
      requests: [...this.requests.values()].map(cloneJson),
      responses: [...this.responses.values()].map(cloneJson),
    };
  }
}

function stringValue(value: unknown, label: string): string {
  if (typeof value !== "string" || !value) throw new Error(`ACP permission ${label} is required`);
  return value;
}

function numberValue(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) throw new Error(`ACP permission ${label} is invalid`);
  return value as number;
}

function timestampValue(value: unknown): string {
  if (typeof value !== "string" || Number.isNaN(Date.parse(value))) throw new Error("ACP permission response timestamp is invalid");
  return new Date(value).toISOString();
}
