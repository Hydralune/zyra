import {
  RECEIPT_SCHEMA,
  type ApprovalBinding,
  type CommandEnvelope,
  type ControlReceipt,
  type Effect,
  type JsonValue,
  type PolicyDecision,
} from "./contracts.ts";
import {
  commandDigest,
  digestJson,
  stableId,
} from "./canonical.ts";

export function createControlReceipt(input: {
  readonly envelope: CommandEnvelope;
  readonly policy: PolicyDecision;
  readonly binding: ApprovalBinding;
  readonly effect: Effect;
  readonly metadata?: Readonly<Record<string, JsonValue>>;
  readonly createdAt?: number;
}): ControlReceipt {
  const createdAt = input.createdAt ?? Date.now();
  const commandHash = commandDigest(input.envelope);
  const receiptId = stableId("gateway-control-receipt", {
    commandId: input.envelope.commandId,
    commandDigest: commandHash,
    policyDigest: input.policy.policyDigest,
    bindingId: input.binding.bindingId,
    consumptionId: input.binding.consumptionId,
    effect: input.effect,
    createdAt,
  });
  return Object.freeze({
    schema: RECEIPT_SCHEMA,
    receiptId,
    commandId: input.envelope.commandId,
    commandDigest: commandHash,
    policyDigest: input.policy.policyDigest,
    bindingId: input.binding.bindingId,
    consumptionId: input.binding.consumptionId,
    effect: input.effect,
    createdAt,
    metadata: Object.freeze({
      permissionOwner: "ToolPermissionRuntime",
      canonicalGatewayOwner: "SandboxGatewayRuntime",
      ...input.metadata,
    }),
  });
}

export function verifyControlReceipt(
  receipt: ControlReceipt,
  envelope: CommandEnvelope,
  policy: PolicyDecision,
  binding: ApprovalBinding,
): boolean {
  return (
    receipt.schema === RECEIPT_SCHEMA &&
    receipt.commandId === envelope.commandId &&
    receipt.commandDigest === commandDigest(envelope) &&
    receipt.policyDigest === policy.policyDigest &&
    receipt.bindingId === binding.bindingId &&
    receipt.consumptionId === binding.consumptionId
  );
}

export function receiptDigest(receipt: ControlReceipt): string {
  return digestJson(
    receipt as unknown as JsonValue,
    "sha256:zyra-gateway-receipt:",
  );
}
