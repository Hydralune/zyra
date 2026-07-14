import { createHash } from "node:crypto";

import type { JsonObject, JsonValue } from "../contracts.ts";

export function canonicalJson(value: JsonValue): string {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  }
  const keys = Object.keys(value).sort();
  return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
}

export function argumentsDigest(argumentsValue: JsonObject): string {
  return `sha256:zyra-jcs-v1:${sha256(canonicalJson(argumentsValue))}`;
}

export function requestFingerprint(value: {
  sessionId: string;
  runId: string;
  taskId: string;
  toolCallId: string;
  namespace: string;
  toolName: string;
  serverId: string;
  version: string;
  schemaDigest: string;
  argumentsDigest: string;
}): string {
  const payload: JsonObject = {
    schema: "zyra.permission.request-fingerprint.v1",
    session_id: value.sessionId,
    run_id: value.runId,
    task_id: value.taskId,
    tool_use_id: value.toolCallId,
    tool_identity: {
      namespace: value.namespace,
      name: value.toolName,
      server_id: value.serverId,
      version: value.version,
      schema_digest: value.schemaDigest,
    },
    arguments_digest: value.argumentsDigest,
  };
  return `sha256:zyra-permission-request-v1:${sha256(canonicalJson(payload))}`;
}

export function digestObject(value: JsonValue): string {
  return `sha256:${sha256(canonicalJson(value))}`;
}

function sha256(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}
