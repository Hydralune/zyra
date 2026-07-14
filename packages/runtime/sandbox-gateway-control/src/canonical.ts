import { createHash, randomBytes } from "node:crypto";

import {
  COMMAND_SCHEMA,
  DEFAULT_BUDGET,
  GatewayProtocolError,
  type CommandEnvelope,
  type CommandEnvelopeInput,
  type JsonValue,
  assertJsonRecord,
  assertNonEmpty,
  normalizeBudget,
} from "./contracts.ts";

const DRIVE_PATH = /^[a-zA-Z]:/u;
const CONTROL = /[\u0000-\u001f\u007f]/u;

export function canonicalize(value: JsonValue): JsonValue {
  if (
    value === null ||
    typeof value === "boolean" ||
    typeof value === "number"
  ) {
    if (typeof value === "number" && !Number.isFinite(value)) {
      throw new GatewayProtocolError(
        "non_finite_number",
        "Canonical JSON cannot contain non-finite numbers",
      );
    }
    return value;
  }
  if (typeof value === "string") {
    return value.normalize("NFC");
  }
  if (Array.isArray(value)) {
    return value.map((item) => canonicalize(item));
  }
  const result: Record<string, JsonValue> = {};
  for (const key of Object.keys(value).sort((left, right) =>
    left.normalize("NFC").localeCompare(right.normalize("NFC")),
  )) {
    const normalized = key.normalize("NFC");
    if (Object.hasOwn(result, normalized)) {
      throw new GatewayProtocolError(
        "canonical_key_collision",
        "Canonical JSON key collision: " + normalized,
      );
    }
    result[normalized] = canonicalize(value[key] as JsonValue);
  }
  return result;
}

export function canonicalJson(value: JsonValue): string {
  return JSON.stringify(canonicalize(value));
}

export function sha256(value: string | Uint8Array, prefix = "sha256:"): string {
  return (
    prefix +
    createHash("sha256")
      .update(value)
      .digest("hex")
  );
}

export function digestJson(
  value: JsonValue,
  prefix = "sha256:",
): string {
  return sha256(canonicalJson(value), prefix);
}

export function contentDigest(value: string | Uint8Array): string {
  return sha256(value, "sha256:zyra-content:");
}

export function tokenDigest(value: string): string {
  return sha256(value, "sha256:zyra-token:");
}

export function stableId(
  namespace: string,
  material: JsonValue,
  length = 32,
): string {
  const name = assertNonEmpty(namespace, "namespace");
  const hash = digestJson(
    { namespace: name, material },
    "sha256:zyra-stable-id:",
  ).split(":").at(-1) as string;
  return name + "-" + hash.slice(0, length);
}

export function randomNonce(bytes = 32): string {
  if (bytes < 16) {
    throw new GatewayProtocolError(
      "weak_nonce",
      "Gateway nonces require at least 128 bits",
    );
  }
  return randomBytes(bytes).toString("hex");
}

export function canonicalLogicalPath(
  value: string,
  options: { readonly allowRoot?: boolean } = {},
): string {
  const raw = value.normalize("NFC").replaceAll("\\", "/");
  if (CONTROL.test(raw)) {
    throw new GatewayProtocolError(
      "path_invalid",
      "Logical path contains control characters",
    );
  }
  if (raw.startsWith("/") || raw.startsWith("//") || DRIVE_PATH.test(raw)) {
    throw new GatewayProtocolError(
      "path_escape",
      "Absolute, drive-qualified, and UNC paths are forbidden",
    );
  }
  const parts: string[] = [];
  for (const item of raw.split("/")) {
    if (!item || item === ".") {
      continue;
    }
    if (item === "..") {
      throw new GatewayProtocolError(
        "path_escape",
        "Parent path traversal is forbidden",
      );
    }
    if (item.endsWith(" ") || item.endsWith(".")) {
      throw new GatewayProtocolError(
        "path_invalid",
        "Trailing spaces and dots are not portable",
      );
    }
    const lowered = item.toLocaleLowerCase("en-US");
    if (["con", "prn", "aux", "nul"].includes(lowered)) {
      throw new GatewayProtocolError(
        "path_invalid",
        "Reserved device path is forbidden",
      );
    }
    parts.push(item);
  }
  if (parts.length === 0) {
    if (options.allowRoot) {
      return ".";
    }
    throw new GatewayProtocolError(
      "path_invalid",
      "Root is not a valid target path",
    );
  }
  return parts.join("/");
}

export function executableName(value: string): string {
  const normalized = assertNonEmpty(value, "executable").replaceAll("\\", "/");
  return (normalized.split("/").at(-1) as string).toLocaleLowerCase("en-US");
}

export function commandMaterial(
  envelope: CommandEnvelope,
): Readonly<Record<string, JsonValue>> {
  return {
    schema: envelope.schema,
    commandId: envelope.commandId,
    sessionId: envelope.sessionId,
    runId: envelope.runId,
    taskId: envelope.taskId,
    workerId: envelope.workerId,
    toolUseId: envelope.toolUseId,
    executable: envelope.executable,
    argv: envelope.argv,
    cwd: envelope.cwd,
    environmentDigest: envelope.environmentDigest,
    budget: { ...envelope.budget },
    workspaceId: envelope.workspaceId,
    ownerEpoch: envelope.ownerEpoch,
    fenceDigest: envelope.fenceDigest,
    networkProfile: envelope.networkProfile,
    provenanceRef: envelope.provenanceRef,
  };
}

export function commandDigest(envelope: CommandEnvelope): string {
  return digestJson(commandMaterial(envelope), "sha256:zyra-command:");
}

export function requestFingerprint(envelope: CommandEnvelope): string {
  return digestJson(
    {
      schema: "zyra.permission.request-fingerprint.v1",
      sessionId: envelope.sessionId,
      runId: envelope.runId,
      taskId: envelope.taskId,
      toolUseId: envelope.toolUseId,
      toolIdentity: {
        namespace: "gateway",
        name: "sandbox_command",
        version: "v1",
      },
      argumentsDigest: commandDigest(envelope),
    },
    "sha256:zyra-permission-request-v1:",
  );
}

export function createCommandEnvelope(
  input: CommandEnvelopeInput,
): CommandEnvelope {
  const sessionId = assertNonEmpty(input.sessionId, "sessionId");
  const runId = assertNonEmpty(input.runId, "runId");
  const taskId = assertNonEmpty(input.taskId, "taskId");
  const workerId = assertNonEmpty(input.workerId, "workerId");
  const toolUseId = assertNonEmpty(input.toolUseId, "toolUseId");
  const executable = assertNonEmpty(input.executable, "executable");
  const argv = Object.freeze(
    (input.argv ?? []).map((item) => item.normalize("NFC")),
  );
  const cwd = canonicalLogicalPath(input.cwd ?? ".", { allowRoot: true });
  const environmentDigest =
    input.environmentDigest ?? digestJson({}, "sha256:zyra-environment:");
  const budget = normalizeBudget(input.budget ?? DEFAULT_BUDGET);
  const metadata = assertJsonRecord(input.metadata ?? {}, "metadata");
  const identityMaterial: JsonValue = {
    sessionId,
    runId,
    taskId,
    workerId,
    toolUseId,
    executable,
    argv,
    cwd,
    environmentDigest,
    workspaceId: input.workspaceId ?? "",
    ownerEpoch: input.ownerEpoch ?? 0,
    fenceDigest: input.fenceDigest ?? "",
    networkProfile: input.networkProfile ?? "offline",
    provenanceRef: input.provenanceRef ?? "",
    idempotencyKey: input.idempotencyKey ?? "",
  };
  const commandId =
    input.commandId ?? stableId("gateway-command", identityMaterial);
  return Object.freeze({
    schema: COMMAND_SCHEMA,
    commandId,
    sessionId,
    runId,
    taskId,
    workerId,
    toolUseId,
    executable,
    argv,
    cwd,
    environmentDigest,
    budget,
    workspaceId: input.workspaceId ?? "",
    ownerEpoch: input.ownerEpoch ?? 0,
    fenceDigest: input.fenceDigest ?? "",
    networkProfile: input.networkProfile ?? "offline",
    provenanceRef: input.provenanceRef ?? "",
    idempotencyKey: input.idempotencyKey ?? "",
    causationId: input.causationId ?? "",
    correlationId: input.correlationId ?? "",
    metadata: Object.freeze({ ...metadata }),
  });
}

export function assertExactCommand(
  approved: CommandEnvelope,
  replay: CommandEnvelope,
): void {
  if (approved.commandId !== replay.commandId) {
    throw new GatewayProtocolError(
      "command_mutated",
      "commandId changed after approval",
    );
  }
  if (commandDigest(approved) !== commandDigest(replay)) {
    throw new GatewayProtocolError(
      "command_mutated",
      "Executable, argv, cwd, environment, or custody changed after approval",
    );
  }
  if (
    canonicalJson(commandMaterial(approved)) !==
    canonicalJson(commandMaterial(replay))
  ) {
    throw new GatewayProtocolError(
      "command_mutated",
      "Permission-bound command material changed after approval",
    );
  }
}
