import type { JsonObject, JsonValue } from "../contracts.ts";

const GATEWAY_RECEIPT_SCALARS = [
  "schema",
  "receipt_id",
  "receipt_digest",
  "outcome",
  "result_digest",
  "permission_consumption_id",
  "command_receipt_id",
  "patch_receipt_id",
  "owner_epoch_before",
  "owner_epoch_after",
  "backend_generation",
  "started_at",
  "finished_at",
  "failure_code",
  "event_ref_count",
] as const;

const GATEWAY_METADATA_SCALARS = [
  "combined_truncated",
  "output_spill_receipt_id",
  "output_spilled",
  "recovery_required",
  "return_code",
  "stderr_truncated",
  "stdout_truncated",
  "termination",
  "workspace_mutation_committed",
  "workspace_state_after_digest",
  "workspace_state_before_digest",
  "workspace_state_mode",
] as const;

function isJsonScalar(value: JsonValue | undefined): value is string | number | boolean | null {
  return value === null
    || typeof value === "string"
    || typeof value === "number"
    || typeof value === "boolean";
}

function copyScalars(
  source: JsonObject,
  keys: readonly string[],
): JsonObject {
  const result: JsonObject = {};
  for (const key of keys) {
    const value = source[key];
    if (isJsonScalar(value)) result[key] = value;
  }
  return result;
}

function compactGatewayReceipt(value: JsonValue): JsonValue {
  if (!value || typeof value !== "object" || Array.isArray(value)) return value;
  const receipt = value as JsonObject;
  const compact: JsonObject = {
    ...copyScalars(receipt, GATEWAY_RECEIPT_SCALARS),
    compacted_for_runtime: true,
  };
  const invocation = receipt.invocation ?? receipt.invocation_ref;
  if (invocation && typeof invocation === "object" && !Array.isArray(invocation)) {
    compact.invocation_ref = copyScalars(invocation as JsonObject, [
      "invocation_id",
      "tool_call_id",
      "arguments_digest",
      "policy_digest",
      "binding_digest",
    ]);
  }
  const metadata = receipt.metadata;
  if (metadata && typeof metadata === "object" && !Array.isArray(metadata)) {
    compact.metadata = copyScalars(metadata as JsonObject, GATEWAY_METADATA_SCALARS);
  }
  if (Array.isArray(receipt.artifact_refs)) {
    compact.artifact_refs = receipt.artifact_refs.map((item) => projectToolResultValue(item));
  }
  if (Array.isArray(receipt.event_refs)) {
    compact.event_ref_count = receipt.event_refs.length;
  }
  return compact;
}

function projectToolResultValue(value: JsonValue): JsonValue {
  if (Array.isArray(value)) return value.map((item) => projectToolResultValue(item));
  if (!value || typeof value !== "object") return value;
  const projected: JsonObject = {};
  for (const [key, item] of Object.entries(value as JsonObject)) {
    projected[key] = key === "gateway_receipt"
      ? compactGatewayReceipt(item)
      : projectToolResultValue(item);
  }
  return projected;
}

/**
 * Keep execution receipts in their durable gateway owner while projecting only
 * stable receipt identity and terminal facts into the reasoning runtime.  A
 * full gateway receipt can contain invocation envelopes and dozens of event
 * references; duplicating it into every session, iteration, and compact
 * snapshot makes a tiny shell result consume most of the model context.
 */
export function projectToolOutputForRuntime(output: JsonObject): JsonObject {
  return projectToolResultValue(output) as JsonObject;
}
