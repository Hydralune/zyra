import type { RuntimeHost, ToolExecutionResponse } from "./contracts.ts";
import {
  ToolResultRuntime,
  type BudgetedToolResult,
} from "./tools/result-runtime.ts";

export type { BudgetedToolResult } from "./tools/result-runtime.ts";

export async function applyToolResultBudget(
  host: RuntimeHost,
  result: ToolExecutionResponse,
  maxChars: number,
): Promise<BudgetedToolResult> {
  // Compatibility entry point only. The default query path owns one durable
  // ToolResultRuntime through E01RuntimeCoordinator.
  return new ToolResultRuntime().enforceToolResultBudget(host, result, maxChars);
}
