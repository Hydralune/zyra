import {
  RPC_SCHEMA,
  GatewayProtocolError,
  type CommandEnvelope,
  type HashlineEdit,
  type JsonValue,
  type RpcFailure,
  type RpcRequest,
  type RpcResponse,
  type RpcSuccess,
  assertCommandEnvelope,
  assertJsonRecord,
  isJsonValue,
} from "./contracts.ts";
import { digestJson, stableId } from "./canonical.ts";
import { StructuredCommandPolicy } from "./command-policy.ts";
import { HashlinePatcher } from "./hashline.ts";

export interface RpcContext {
  readonly requestId: string;
  readonly signal?: AbortSignal;
  readonly metadata: Readonly<Record<string, JsonValue>>;
}

export type RpcHandler = (
  params: JsonValue,
  context: RpcContext,
) => Promise<JsonValue> | JsonValue;

export interface RpcMethodDescriptor {
  readonly method: string;
  readonly description: string;
  readonly mutatesState: boolean;
  readonly permissionOwner: string;
}

interface RegisteredMethod {
  readonly descriptor: RpcMethodDescriptor;
  readonly handler: RpcHandler;
}

export class GatewayControlRpcRouter {
  private readonly methods = new Map<string, RegisteredMethod>();
  readonly maximumRequestBytes: number;

  constructor(options: { readonly maximumRequestBytes?: number } = {}) {
    this.maximumRequestBytes = options.maximumRequestBytes ?? 2 * 1024 * 1024;
  }

  register(
    descriptor: RpcMethodDescriptor,
    handler: RpcHandler,
  ): void {
    if (!/^[a-z][a-z0-9_.-]*$/u.test(descriptor.method)) {
      throw new GatewayProtocolError(
        "rpc_method_invalid",
        "RPC method name is invalid: " + descriptor.method,
      );
    }
    if (this.methods.has(descriptor.method)) {
      throw new GatewayProtocolError(
        "rpc_method_duplicate",
        "RPC method is already registered: " + descriptor.method,
      );
    }
    this.methods.set(descriptor.method, {
      descriptor: Object.freeze({ ...descriptor }),
      handler,
    });
  }

  async handle(
    request: RpcRequest,
    context: Omit<RpcContext, "requestId"> = { metadata: {} },
  ): Promise<RpcResponse> {
    try {
      this.assertRequest(request);
      const registered = this.methods.get(request.method);
      if (!registered) {
        throw new GatewayProtocolError(
          "rpc_method_not_found",
          "RPC method is not registered: " + request.method,
        );
      }
      if (context.signal?.aborted) {
        throw context.signal.reason ??
          new GatewayProtocolError(
            "rpc_cancelled",
            "RPC request was cancelled",
          );
      }
      const result = await registered.handler(request.params, {
        requestId: request.requestId,
        signal: context.signal,
        metadata: context.metadata,
      });
      if (!isJsonValue(result)) {
        throw new GatewayProtocolError(
          "rpc_result_invalid",
          "RPC handler returned a non-JSON value",
        );
      }
      const response: RpcSuccess = Object.freeze({
        schema: RPC_SCHEMA,
        requestId: request.requestId,
        ok: true,
        result,
      });
      return response;
    } catch (error) {
      return rpcFailure(
        typeof request?.requestId === "string"
          ? request.requestId
          : "invalid-request",
        error,
      );
    }
  }

  parse(line: string): RpcRequest {
    if (Buffer.byteLength(line, "utf8") > this.maximumRequestBytes) {
      throw new GatewayProtocolError(
        "rpc_request_too_large",
        "RPC request exceeds byte budget",
      );
    }
    let value: unknown;
    try {
      value = JSON.parse(line);
    } catch (error) {
      throw new GatewayProtocolError(
        "rpc_json_invalid",
        "RPC request is not valid JSON: " + errorName(error),
      );
    }
    if (value === null || Array.isArray(value) || typeof value !== "object") {
      throw new GatewayProtocolError(
        "rpc_request_invalid",
        "RPC request must be an object",
      );
    }
    const record = value as Record<string, unknown>;
    const request: RpcRequest = {
      schema: record.schema as typeof RPC_SCHEMA,
      requestId: String(record.requestId ?? ""),
      method: String(record.method ?? ""),
      params: record.params as JsonValue,
    };
    this.assertRequest(request);
    return request;
  }

  serialize(response: RpcResponse): string {
    return JSON.stringify(response) + "\n";
  }

  listMethods(): readonly RpcMethodDescriptor[] {
    return Object.freeze(
      [...this.methods.values()]
        .map((item) => item.descriptor)
        .sort((left, right) => left.method.localeCompare(right.method)),
    );
  }

  descriptor(): JsonValue {
    return {
      router: "GatewayControlRpcRouter",
      schema: RPC_SCHEMA,
      maximumRequestBytes: this.maximumRequestBytes,
      dynamicImports: false,
      startsSubprocess: false,
      canonicalStateOwner: "SandboxGatewayRuntime",
      methods: this.listMethods() as unknown as JsonValue,
      descriptorDigest: digestJson(
        this.listMethods() as unknown as JsonValue,
      ),
    };
  }

  private assertRequest(request: RpcRequest): void {
    if (request.schema !== RPC_SCHEMA) {
      throw new GatewayProtocolError(
        "rpc_schema_mismatch",
        "RPC request schema mismatch",
      );
    }
    if (!request.requestId || !request.method) {
      throw new GatewayProtocolError(
        "rpc_request_invalid",
        "RPC requestId and method are required",
      );
    }
    if (!isJsonValue(request.params)) {
      throw new GatewayProtocolError(
        "rpc_params_invalid",
        "RPC params must be JSON",
      );
    }
  }
}

export class JsonLineDecoder {
  readonly maximumLineBytes: number;
  private buffer = "";

  constructor(options: { readonly maximumLineBytes?: number } = {}) {
    this.maximumLineBytes = options.maximumLineBytes ?? 2 * 1024 * 1024;
  }

  push(chunk: string | Uint8Array): readonly string[] {
    this.buffer += typeof chunk === "string"
      ? chunk
      : Buffer.from(chunk).toString("utf8");
    if (Buffer.byteLength(this.buffer, "utf8") > this.maximumLineBytes) {
      this.buffer = "";
      throw new GatewayProtocolError(
        "rpc_line_too_large",
        "RPC line exceeds byte budget",
      );
    }
    const lines: string[] = [];
    while (true) {
      const index = this.buffer.indexOf("\n");
      if (index < 0) {
        break;
      }
      const line = this.buffer.slice(0, index).replace(/\r$/u, "");
      this.buffer = this.buffer.slice(index + 1);
      if (line.trim()) {
        lines.push(line);
      }
    }
    return Object.freeze(lines);
  }

  finish(): string | null {
    const value = this.buffer.trim();
    this.buffer = "";
    return value || null;
  }
}

export function createGatewayControlRouter(options: {
  readonly policy?: StructuredCommandPolicy;
  readonly hashline?: HashlinePatcher;
} = {}): GatewayControlRpcRouter {
  const policy = options.policy ?? new StructuredCommandPolicy();
  const hashline = options.hashline ?? new HashlinePatcher();
  const router = new GatewayControlRpcRouter();
  router.register(
    {
      method: "control.describe",
      description: "Describe the supplementary TypeScript control plane",
      mutatesState: false,
      permissionOwner: "none",
    },
    () => ({
      package: "@zyra/sandbox-gateway-control",
      ownerSlice: "M1-S05B-01",
      role: "supplementary",
      canonicalGatewayOwner: "SandboxGatewayRuntime",
      permissionOwner: "typescript.PermissionCoordinator",
      policy: policy.descriptor(),
      hashline: hashline.descriptor(),
      vendorRuntimeRequired: false,
    }),
  );
  router.register(
    {
      method: "policy.evaluate",
      description: "Evaluate structured command evidence",
      mutatesState: false,
      permissionOwner: "typescript.PermissionCoordinator",
    },
    (params) => {
      const record = assertJsonRecord(params, "params");
      const envelope = assertCommandEnvelope(record.envelope);
      const sealed = record.sealed === true;
      return policy.evaluate(envelope, { sealed }) as unknown as JsonValue;
    },
  );
  router.register(
    {
      method: "hashline.snapshot",
      description: "Create content-bound line hashes",
      mutatesState: false,
      permissionOwner: "none",
    },
    (params) => {
      const record = assertJsonRecord(params, "params");
      if (typeof record.content !== "string") {
        throw new GatewayProtocolError(
          "hashline_content_invalid",
          "Hashline content must be a string",
        );
      }
      return hashline.snapshot(record.content) as unknown as JsonValue;
    },
  );
  router.register(
    {
      method: "hashline.apply",
      description: "Apply an exact snapshot-bound edit in memory",
      mutatesState: false,
      permissionOwner: "none",
    },
    (params) => {
      const record = assertJsonRecord(params, "params");
      if (typeof record.content !== "string") {
        throw new GatewayProtocolError(
          "hashline_content_invalid",
          "Hashline content must be a string",
        );
      }
      return hashline.apply(
        record.content,
        record.edit as unknown as HashlineEdit,
      ) as unknown as JsonValue;
    },
  );
  return router;
}

export function createRpcRequest(
  method: string,
  params: JsonValue,
  requestId = stableId("gateway-rpc-request", {
    method,
    params,
    time: Date.now(),
  }),
): RpcRequest {
  return Object.freeze({
    schema: RPC_SCHEMA,
    requestId,
    method,
    params,
  });
}

function rpcFailure(requestId: string, value: unknown): RpcFailure {
  const error = value instanceof GatewayProtocolError
    ? value
    : new GatewayProtocolError(
        "rpc_internal",
        "RPC handler failed: " + errorName(value),
      );
  return Object.freeze({
    schema: RPC_SCHEMA,
    requestId,
    ok: false,
    error: Object.freeze({
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      metadata: error.metadata,
    }),
  });
}

function errorName(value: unknown): string {
  return value instanceof Error ? value.name : typeof value;
}
