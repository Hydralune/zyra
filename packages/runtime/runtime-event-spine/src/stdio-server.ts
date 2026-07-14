import { createInterface } from "node:readline";
import { resolve } from "node:path";
import { asEventSpineError, EventSpineError, EventSpineErrorCode } from "./errors.ts";
import { RuntimeEventSpine } from "./runtime.ts";
import { requireInteger, requireRecord, requireString, type JsonValue } from "./canonical.ts";

interface RpcRequest {
  id: string | number;
  method: string;
  params?: Record<string, unknown>;
}

interface RpcResponse {
  id: string | number | null;
  ok: boolean;
  result?: unknown;
  error?: Record<string, unknown>;
}

function parseArguments(argv: readonly string[]): { sqlitePath: string; artifactRoot: string } {
  let sqlitePath = "";
  let artifactRoot = "";
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (item === "--db") sqlitePath = argv[++index] ?? "";
    else if (item === "--artifact-root") artifactRoot = argv[++index] ?? "";
  }
  if (!sqlitePath || !artifactRoot) {
    throw new EventSpineError({
      code: EventSpineErrorCode.INVALID_ARGUMENT,
      message: "--db and --artifact-root are required",
    });
  }
  return { sqlitePath: resolve(sqlitePath), artifactRoot: resolve(artifactRoot) };
}

function parseRequest(line: string): RpcRequest {
  let value: unknown;
  try {
    value = JSON.parse(line);
  } catch (error) {
    throw new EventSpineError({
      code: EventSpineErrorCode.RPC_PROTOCOL_ERROR,
      message: "request is not valid JSON",
      details: { line_prefix: line.slice(0, 200) },
      cause: error,
    });
  }
  const record = requireRecord(value, "request");
  const idValue = record.id;
  if (typeof idValue !== "string" && typeof idValue !== "number") {
    throw new EventSpineError({ code: EventSpineErrorCode.RPC_PROTOCOL_ERROR, message: "request id must be string or number" });
  }
  return {
    id: idValue,
    method: requireString(record.method, "request.method", 128),
    params: record.params === undefined ? {} : requireRecord(record.params, "request.params"),
  };
}

function send(response: RpcResponse): void {
  process.stdout.write(`${JSON.stringify(response)}\n`);
}

function command(runtime: RuntimeEventSpine, request: RpcRequest): unknown {
  const params = request.params ?? {};
  switch (request.method) {
    case "ping":
      return { pong: true, protocol: "zyra.runtime-event-spine-rpc/v1" };
    case "health":
      return runtime.health();
    case "append_legacy":
      return runtime.appendLegacy(params.event, requireRecord(params.options ?? {}, "params.options"));
    case "append_legacy_batch": {
      if (!Array.isArray(params.events)) throw new EventSpineError({ code: EventSpineErrorCode.INVALID_ARGUMENT, message: "params.events must be an array" });
      return runtime.appendLegacyBatch(params.events, requireRecord(params.options ?? {}, "params.options"));
    }
    case "append_canonical":
      return runtime.appendCanonical(params.event, requireRecord(params.options ?? {}, "params.options"));
    case "append_omp_frame":
      return runtime.appendOmpFrame(requireRecord(params.frame, "params.frame"));
    case "publish_live":
      return runtime.publishLive(params.event, params.ttl_ms === undefined ? 30_000 : requireInteger(params.ttl_ms, "params.ttl_ms", 100));
    case "query":
      return runtime.query(params.query ?? {});
    case "count":
      return { count: runtime.count(params.query ?? {}) };
    case "get":
      return { event: runtime.get(requireString(params.event_id, "params.event_id", 256)) ?? null };
    case "projection":
      return runtime.projection(requireString(params.aggregate_id, "params.aggregate_id", 512));
    case "rebuild_projection":
      return runtime.rebuildProjection(params.aggregate_id === undefined ? undefined : requireString(params.aggregate_id, "params.aggregate_id", 512));
    case "register_subscription":
      return runtime.registerSubscription(params.subscription);
    case "poll":
      return runtime.poll(
        requireString(params.subscription_id, "params.subscription_id", 256),
        params.limit === undefined ? 1 : requireInteger(params.limit, "params.limit", 1),
      );
    case "poll_live":
      return runtime.bus.pollLive(
        requireString(params.subscription_id, "params.subscription_id", 256),
        params.limit === undefined ? 100 : requireInteger(params.limit, "params.limit", 1),
      );
    case "ack":
      return runtime.acknowledge(
        requireString(params.delivery_id, "params.delivery_id", 256),
        requireString(params.lease_token, "params.lease_token", 512),
      );
    case "nack":
      return runtime.negativeAcknowledge(
        requireString(params.delivery_id, "params.delivery_id", 256),
        requireString(params.lease_token, "params.lease_token", 512),
        requireString(params.error ?? "consumer_rejected", "params.error", 2000),
        params.delay_ms === undefined ? 0 : requireInteger(params.delay_ms, "params.delay_ms", 0),
      );
    case "sweep":
      return { swept: runtime.bus.sweep() };
    case "bus_snapshot":
      return runtime.bus.snapshot();
    case "metrics":
      return runtime.metrics.report(typeof params.task_success === "number" ? params.task_success : 1);
    case "baselines":
      return runtime.metrics.compare(typeof params.task_success === "number" ? params.task_success : 1);
    case "close":
      runtime.close();
      return { closed: true };
    default:
      throw new EventSpineError({
        code: EventSpineErrorCode.RPC_COMMAND_UNKNOWN,
        message: `unknown RPC method ${request.method}`,
        details: { method: request.method },
      });
  }
}

function main(): void {
  const options = parseArguments(process.argv.slice(2));
  const runtime = new RuntimeEventSpine(options);
  const input = createInterface({ input: process.stdin, crlfDelay: Infinity, terminal: false });
  let closing = false;
  const close = (): void => {
    if (closing) return;
    closing = true;
    runtime.close();
  };
  process.once("SIGINT", () => { close(); process.exit(130); });
  process.once("SIGTERM", () => { close(); process.exit(143); });
  process.once("exit", close);
  input.on("line", (line) => {
    if (!line.trim()) return;
    let id: string | number | null = null;
    try {
      const request = parseRequest(line);
      id = request.id;
      const result = command(runtime, request);
      send({ id, ok: true, result });
      if (request.method === "close") {
        input.close();
        closing = true;
      }
    } catch (error) {
      const normalized = asEventSpineError(error, "runtime event RPC failed");
      send({ id, ok: false, error: normalized.toJSON() });
    }
  });
  input.once("close", () => {
    close();
  });
}

main();
