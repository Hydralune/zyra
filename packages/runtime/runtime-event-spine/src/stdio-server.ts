import { createInterface } from "node:readline";
import { resolve } from "node:path";
import { asEventSpineError, EventSpineError, EventSpineErrorCode } from "./errors.ts";
import { RuntimeEventSpine } from "./runtime.ts";
import { requireInteger, requireRecord, requireString, type JsonValue } from "./canonical.ts";
import { RuntimeEventIntegration } from "./integration-runtime.ts";

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

function command(runtime: RuntimeEventSpine, integration: RuntimeEventIntegration, request: RpcRequest): unknown {
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
    case "append_source_record":
      return integration.appendSource(params.source);
    case "append_source_batch":
      return integration.appendBatch(params.batch);
    case "append_omp_rpc_frame":
      return integration.appendOmpRpcFrame(
        params.frame,
        requireRecord(params.context, "params.context") as never,
      );
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
    case "projection_task_view": {
      const aggregateId = requireString(params.aggregate_id, "params.aggregate_id", 512);
      return { task_view: runtime.projectionDelivery?.taskView(aggregateId) ?? null };
    }
    case "projection_task_view_by_task":
      return {
        task_view: runtime.projectionDelivery?.taskViewByTaskId(
          requireString(params.task_id, "params.task_id", 512),
        ) ?? null,
      };
    case "projection_history":
      return runtime.projectionDelivery?.history(
        requireString(params.aggregate_id, "params.aggregate_id", 512),
        {
          afterGlobalSequence: params.after_global_sequence === undefined
            ? undefined
            : requireInteger(params.after_global_sequence, "params.after_global_sequence", 0),
          limit: params.limit === undefined ? 200 : requireInteger(params.limit, "params.limit", 1),
          eventTypes: Array.isArray(params.event_types)
            ? params.event_types.map((item) => requireString(item, "params.event_types[]", 256))
            : undefined,
        },
      ) ?? null;
    case "projection_stream":
      return runtime.projectionDelivery?.stream(
        requireString(params.aggregate_id, "params.aggregate_id", 512),
        requireRecord(params.cursor ?? {}, "params.cursor") as never,
        params.limit === undefined ? 200 : requireInteger(params.limit, "params.limit", 1),
      ) ?? [];
    case "rebuild_projection":
      return runtime.rebuildProjection(params.aggregate_id === undefined ? undefined : requireString(params.aggregate_id, "params.aggregate_id", 512));
    case "register_subscription":
      return runtime.registerSubscription(params.subscription);
    case "poll": {
      const subscriptionId = requireString(params.subscription_id, "params.subscription_id", 256);
      return runtime.poll(
        subscriptionId,
        params.limit === undefined ? 1 : requireInteger(params.limit, "params.limit", 1),
      ).map(({ delivery, message }) => ({
        ...delivery,
        consumerId: subscriptionId,
        event: runtime.get(delivery.eventId),
        message,
        firstAvailableAt: delivery.createdAt,
      }));
    }
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
    case "catch_up":
      return {
        enqueued: runtime.bus.catchUp(
          requireString(params.subscription_id, "params.subscription_id", 256),
          {
            aggregateId: params.aggregate_id === undefined ? undefined : requireString(params.aggregate_id, "params.aggregate_id", 512),
            taskId: params.task_id === undefined ? undefined : requireString(params.task_id, "params.task_id", 512),
            afterSequence: params.after_global_sequence === undefined ? undefined : requireInteger(params.after_global_sequence, "params.after_global_sequence", 0),
            limit: params.limit === undefined ? 100_000 : requireInteger(params.limit, "params.limit", 1),
          },
        ),
      };
    case "integration_contract":
      return integration.contract();
    case "source_outbox":
      return { items: integration.outbox(params.limit === undefined ? 1000 : requireInteger(params.limit, "params.limit", 1)) };
    case "retry_source_outbox":
      return { results: integration.retryPending(params.limit === undefined ? 1000 : requireInteger(params.limit, "params.limit", 1)) };
    case "reconcile":
      return integration.reconcile(requireRecord(params.options ?? {}, "params.options") as never);
    case "artifact_read":
      return integration.artifacts.read(params.request);
    case "artifact_audit":
      return integration.auditArtifact(requireString(params.artifact_id, "params.artifact_id", 256));
    case "disable_matrix":
      return integration.evaluateDisableMatrix(
        requireString(params.aggregate_id, "params.aggregate_id", 512),
        Array.isArray(params.observations) ? params.observations as never : [],
      );
    case "metrics":
      return runtime.metrics.report();
    case "baselines":
      return runtime.metrics.compare();
    case "replay": {
      const eventId = requireString(params.event_id, "params.event_id", 256);
      const event = runtime.get(eventId);
      if (!event) throw new EventSpineError({ code: EventSpineErrorCode.INVALID_ARGUMENT, message: "replay event not found", details: { event_id: eventId } });
      const projectorDeliveryIds = runtime.projectionDelivery?.admit(event) ?? [];
      let repairedDeliveries = 0;
      for (const spec of runtime.store.subscriptions(true)) {
        if (spec.subscriptionId === runtime.projectionDelivery?.subscriptionId) continue;
        repairedDeliveries += runtime.bus.ensureDelivery(spec.subscriptionId, event, false).length;
      }
      return {
        event,
        committed: false,
        duplicate: true,
        projected: Boolean(runtime.projectionDelivery?.taskView(event.aggregateId)),
        routed: repairedDeliveries > 0,
        deliveryIds: runtime.store.deliveriesForEvent(eventId).map((delivery) => delivery.deliveryId),
        artifactSpillCount: 0,
        warnings: ["replay_existing_event", `repaired_deliveries:${repairedDeliveries}`, `projector_deliveries:${projectorDeliveryIds.length}`],
      };
    }
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
  const integration = new RuntimeEventIntegration(runtime, { registerDefaultConsumers: false });
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
      const result = command(runtime, integration, request);
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
