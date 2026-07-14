import type { JsonObject } from "../contracts.ts";

export interface ControlReceipt {
  requestId: string;
  name: string;
  action: string;
  accepted: boolean;
  changed: boolean;
  revisionBefore: number;
  revisionAfter: number;
  status: "ok" | "unsupported" | "conflict" | "rejected";
  effect: JsonObject;
  error: string;
}

export interface ControlRuntimeState {
  revision: number;
  contextEpoch: number;
  compactRequested: boolean;
  clearRequested: boolean;
  cancelled: boolean;
  modelName: string;
  receipts: ControlReceipt[];
}
