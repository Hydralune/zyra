import {
  canonicalize,
  digestValue,
  stableId,
} from "./integration-contracts.ts";

export interface GatewayDispatchEnvelope {
  readonly envelopeId: string;
  readonly runId: string;
  readonly taskId: string;
  readonly nodeId?: string;
  readonly runtimeWorker: string;
  readonly backend: string;
  readonly location: string;
  readonly sandbox: string;
  readonly gateway: string;
  readonly workspaceDigest: string;
  readonly artifactRootDigest: string;
  readonly ownerEpoch: number;
  readonly backendGeneration: number;
  readonly decisionId?: string;
}

export interface GatewayDispatchReceipt {
  readonly schema: "zyra.gateway-dispatch-receipt.v1";
  readonly receiptId: string;
  readonly envelopeDigest: string;
  readonly policyDigest: string;
  readonly ownerEpoch: number;
  readonly backendGeneration: number;
  readonly issuedAt: number;
  readonly expiresAt: number;
}

const DENIED_BOUNDARIES = new Set([
  "",
  "none",
  "disabled",
  "direct",
  "bypass",
  "yolo",
  "host-unrestricted",
]);

export function createDispatchReceipt(input: {
  readonly envelope: GatewayDispatchEnvelope;
  readonly policyDigest: string;
  readonly issuedAt?: number;
  readonly ttlSeconds?: number;
}): Readonly<GatewayDispatchReceipt> {
  const envelope = freezeDispatchEnvelope(input.envelope);
  const issuedAt = input.issuedAt ?? Date.now() / 1000;
  const ttlSeconds = input.ttlSeconds ?? 300;
  if (!Number.isFinite(ttlSeconds) || ttlSeconds <= 0 || ttlSeconds > 3600) {
    throw new Error("dispatch receipt TTL is outside the gateway bound");
  }
  const envelopeDigest = digestValue(envelope);
  return Object.freeze({
    schema: "zyra.gateway-dispatch-receipt.v1",
    receiptId: stableId(
      "gateway-dispatch",
      envelope.envelopeId,
      envelopeDigest,
      input.policyDigest,
    ),
    envelopeDigest,
    policyDigest: input.policyDigest,
    ownerEpoch: envelope.ownerEpoch,
    backendGeneration: envelope.backendGeneration,
    issuedAt,
    expiresAt: issuedAt + ttlSeconds,
  });
}

export function validateDispatchReceipt(input: {
  readonly envelope: GatewayDispatchEnvelope;
  readonly receipt: GatewayDispatchReceipt;
  readonly expectedPolicyDigest: string;
  readonly now?: number;
}): void {
  const envelope = freezeDispatchEnvelope(input.envelope);
  const now = input.now ?? Date.now() / 1000;
  if (now >= input.receipt.expiresAt) {
    throw new Error("gateway dispatch receipt expired");
  }
  if (input.receipt.envelopeDigest !== digestValue(envelope)) {
    throw new Error("gateway dispatch envelope changed after attestation");
  }
  if (input.receipt.policyDigest !== input.expectedPolicyDigest) {
    throw new Error("gateway dispatch policy changed after attestation");
  }
  if (input.receipt.ownerEpoch !== envelope.ownerEpoch) {
    throw new Error("workspace owner epoch changed after attestation");
  }
  if (input.receipt.backendGeneration !== envelope.backendGeneration) {
    throw new Error("backend generation changed after attestation");
  }
}

export function freezeDispatchEnvelope(
  value: GatewayDispatchEnvelope,
): Readonly<GatewayDispatchEnvelope> {
  for (const [field, selected] of Object.entries({
    envelopeId: value.envelopeId,
    runId: value.runId,
    taskId: value.taskId,
    runtimeWorker: value.runtimeWorker,
    backend: value.backend,
    location: value.location,
    sandbox: value.sandbox,
    gateway: value.gateway,
    workspaceDigest: value.workspaceDigest,
    artifactRootDigest: value.artifactRootDigest,
  })) {
    if (!String(selected ?? "").trim()) {
      throw new Error(`dispatch envelope is missing ${field}`);
    }
  }
  if (DENIED_BOUNDARIES.has(value.sandbox.toLowerCase())) {
    throw new Error("dispatch sandbox is not enforced");
  }
  if (DENIED_BOUNDARIES.has(value.gateway.toLowerCase())) {
    throw new Error("dispatch gateway is not enforced");
  }
  if (!Number.isSafeInteger(value.ownerEpoch) || value.ownerEpoch < 0) {
    throw new Error("dispatch owner epoch is invalid");
  }
  if (
    !Number.isSafeInteger(value.backendGeneration) ||
    value.backendGeneration < 0
  ) {
    throw new Error("dispatch backend generation is invalid");
  }
  return Object.freeze(canonicalize(value) as GatewayDispatchEnvelope);
}
