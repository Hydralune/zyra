import {
  jsonChars,
  runtimeId,
  type ArtifactReceipt,
  type RuntimeHost,
  type ToolExecutionResponse,
} from "./contracts.ts";

export interface BudgetedToolResult {
  result: ToolExecutionResponse;
  artifact: ArtifactReceipt | null;
  originalChars: number;
  applied: boolean;
}

export async function applyToolResultBudget(
  host: RuntimeHost,
  result: ToolExecutionResponse,
  maxChars: number,
): Promise<BudgetedToolResult> {
  const originalChars = jsonChars(result.output);
  if (originalChars <= maxChars) {
    return {
      result,
      artifact: null,
      originalChars,
      applied: false,
    };
  }
  const serialized = JSON.stringify(result.output);
  const artifact = await host.externalize({
    requestId: runtimeId("artifact_request"),
    title: "CodeWorker tool result " + result.tool_call_id,
    kind: "structured_data",
    extension: ".json",
    content: serialized,
    metadata: {
      source: "typescript_tool_result_budget",
      tool_call_id: result.tool_call_id,
      original_chars: originalChars,
      budget_chars: maxChars,
    },
  });
  const previewChars = Math.max(0, Math.min(maxChars, serialized.length));
  return {
    artifact,
    originalChars,
    applied: true,
    result: {
      ...result,
      output: {
        content_preview: serialized.slice(0, previewChars),
        truncated: true,
        original_chars: originalChars,
        artifact_id: artifact.artifact_id,
      },
      artifacts: [...result.artifacts, artifact],
      metadata: {
        ...result.metadata,
        tool_result_budget_applied: "true",
        tool_result_budget_chars: String(maxChars),
        tool_result_original_chars: String(originalChars),
        tool_result_artifact_id: artifact.artifact_id,
      },
    },
  };
}
