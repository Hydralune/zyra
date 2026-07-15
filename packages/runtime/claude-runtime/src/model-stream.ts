import {
  asObject,
  asString,
  type JsonObject,
  type RuntimeConfig,
  type RuntimeRunInput,
  type ToolSpecContract,
  type ToolStep,
} from "./contracts.ts";

type EmitRuntimeEvent = (phase: string, payload?: JsonObject) => Promise<void>;

export interface ModelStreamResolution {
  ok: boolean;
  turns: ToolStep[][];
  metadata: Record<string, string>;
  error: string | null;
}

interface AttemptRecord extends JsonObject {
  attempt: number;
  model: string;
  ok: boolean;
  status: number;
  decision: string;
  fallback_model: string;
  error: string;
}

export async function resolveModelTurns(
  input: RuntimeRunInput,
  config: RuntimeConfig,
  scriptedTurns: ToolStep[][],
  tools: ToolSpecContract[],
  emit: EmitRuntimeEvent,
): Promise<ModelStreamResolution> {
  const constraints = config.runtimeConstraints;
  const transport = asString(
    constraints.model_transport || constraints.model_transport_kind,
    "scripted",
  );
  const requestMessages = normalizeMessages(input);
  const requestTools = tools.map(openAiTool);
  if (transport !== "http_sse") {
    const requestId = `${input.workerRequestId}:model:1`;
    await emit("model_request_prepared", {
      provider_request: {
        request_id: requestId,
        provider: "local",
        model: config.modelName,
        messages: requestMessages,
        tools: requestTools,
        system: [],
      },
    });
    await emit("model_stream_frame", {
      model_stream_frame: {
        kind: "scripted_turn_contract",
        model: config.modelName,
        turn_count: scriptedTurns.length,
      },
    });
    await emit("model_stream_report", {
      model_stream: {
        request_id: requestId,
        ok: true,
        transport: "scripted",
        model: config.modelName,
        tool_call_count: scriptedTurns.flat().length,
        usage: {
          input_tokens: 0,
          output_tokens: 0,
          cache_read_input_tokens: 0,
          cache_creation_input_tokens: 0,
          server_tool_use_tokens: 0,
        },
      },
    });
    await emit("api_retry_report", {
      api_retry: {
        ok: true,
        status: "not_needed",
        attempts: [],
        final_model: config.modelName,
        fallback_used: false,
        recovered: true,
      },
    });
    await emit("api_retry_playbook", {
      api_retry_playbook: {
        ok: true,
        status: "not_needed",
        fallback_count: 0,
      },
    });
    await emit("model_stream_watchdog", {
      model_stream_watchdog: { ok: true, status: "not_needed" },
    });
    return {
      ok: true,
      turns: scriptedTurns,
      error: null,
      metadata: modelMetadata({
        ok: true,
        status: "not_needed",
        finalModel: config.modelName,
        fallbackUsed: false,
        recovered: true,
        retryCount: 0,
      }),
    };
  }

  const baseUrl = asString(constraints.model_api_base_url).trim();
  if (!baseUrl) {
    return failedWithoutRequest(
      scriptedTurns,
      config.modelName,
      "model_api_base_url_missing",
      emit,
    );
  }
  const fallbackModels = stringList(constraints.api_retry_fallback_models);
  const maxAttempts = Math.max(
    1,
    Math.min(8, Number(constraints.api_retry_max_attempts) || 1),
  );
  const models = [config.modelName, ...fallbackModels];
  const attempts: AttemptRecord[] = [];
  const timeoutMs = Math.max(
    100,
    Math.min(120_000, (Number(constraints.model_api_timeout_seconds) || 30) * 1000),
  );
  let finalError = "model_stream_failed";

  for (let index = 0; index < maxAttempts; index += 1) {
    const model = models[Math.min(index, models.length - 1)] || config.modelName;
    const requestId = `${input.workerRequestId}:model:${index + 1}`;
    const nextModel = index + 1 < maxAttempts
      ? models[Math.min(index + 1, models.length - 1)] || model
      : "";
    await emit("model_request_prepared", {
      provider_request: {
        request_id: requestId,
        provider: "compatible",
        model,
        messages: requestMessages,
        tools: requestTools,
        system: [],
      },
    });
    await emit("model_stream_frame", {
      model_stream_frame: {
        kind: "request_started",
        attempt: index + 1,
        model,
        transport: "http_sse",
      },
    });
    try {
      const response = await fetch(modelEndpoint(baseUrl), {
        method: "POST",
        headers: modelHeaders(constraints),
        body: JSON.stringify({
          model,
          messages: requestMessages,
          tools: requestTools,
          tool_choice: "auto",
          stream: true,
        }),
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (!response.ok) {
        const responseBody = await response.text();
        finalError = "model_http_" + String(response.status);
        const record: AttemptRecord = {
          attempt: index + 1,
          model,
          ok: false,
          status: response.status,
          decision: nextModel ? "retry_fallback_model" : "stop_exhausted",
          fallback_model: nextModel,
          error: responseBody.slice(0, 1000) || finalError,
        };
        attempts.push(record);
        await emit("model_stream_report", {
          model_stream: {
            ...record,
            request_id: requestId,
            provider: "compatible",
            usage: emptyUsage(),
          },
        });
        continue;
      }

      const parsed = await parseSseToolCalls(response, index + 1, model, emit);
      const record: AttemptRecord = {
        attempt: index + 1,
        model,
        ok: true,
        status: response.status,
        decision: index > 0 ? "fallback_selected" : "primary_selected",
        fallback_model: "",
        error: "",
      };
      attempts.push(record);
      await emit("model_stream_report", {
        model_stream: {
          ...record,
          request_id: requestId,
          provider: "compatible",
          frame_count: parsed.frameCount,
          tool_call_count: parsed.steps.length,
          usage: parsed.usage,
        },
      });
      const fallbackUsed = index > 0;
      const status = fallbackUsed ? "fallback_selected" : "primary_selected";
      await emitFinalReports(
        emit,
        attempts,
        true,
        status,
        model,
        fallbackUsed,
      );
      return {
        ok: true,
        turns: [parsed.steps],
        error: null,
        metadata: modelMetadata({
          ok: true,
          status,
          finalModel: model,
          fallbackUsed,
          recovered: true,
          retryCount: index,
        }),
      };
    } catch (error) {
      finalError = error instanceof Error ? error.message : String(error);
      const record: AttemptRecord = {
        attempt: index + 1,
        model,
        ok: false,
        status: 0,
        decision: nextModel ? "retry_fallback_model" : "stop_exhausted",
        fallback_model: nextModel,
        error: finalError,
      };
      attempts.push(record);
      await emit("model_stream_report", {
        model_stream: {
          ...record,
          request_id: requestId,
          provider: "compatible",
          usage: emptyUsage(),
        },
      });
    }
  }

  const finalModel = attempts.at(-1)?.model || config.modelName;
  await emitFinalReports(
    emit,
    attempts,
    false,
    "exhausted",
    finalModel,
    attempts.length > 1,
  );
  return {
    ok: false,
    turns: [],
    error: finalError,
    metadata: modelMetadata({
      ok: false,
      status: "exhausted",
      finalModel,
      fallbackUsed: attempts.length > 1,
      recovered: false,
      retryCount: Math.max(0, attempts.length - 1),
    }),
  };
}

async function failedWithoutRequest(
  turns: ToolStep[][],
  model: string,
  error: string,
  emit: EmitRuntimeEvent,
): Promise<ModelStreamResolution> {
  await emit("model_stream_report", {
    model_stream: { ok: false, transport: "http_sse", model, error },
  });
  await emitFinalReports(emit, [], false, "configuration_error", model, false);
  return {
    ok: false,
    turns,
    error,
    metadata: modelMetadata({
      ok: false,
      status: "configuration_error",
      finalModel: model,
      fallbackUsed: false,
      recovered: false,
      retryCount: 0,
    }),
  };
}

async function emitFinalReports(
  emit: EmitRuntimeEvent,
  attempts: AttemptRecord[],
  ok: boolean,
  status: string,
  finalModel: string,
  fallbackUsed: boolean,
): Promise<void> {
  await emit("api_retry_report", {
    api_retry: {
      ok,
      status,
      attempts,
      final_model: finalModel,
      fallback_used: fallbackUsed,
      recovered: ok,
    },
  });
  await emit("api_retry_playbook", {
    api_retry_playbook: {
      ok,
      status: ok ? (fallbackUsed ? "recovered" : "not_needed") : "exhausted",
      fallback_count: fallbackUsed ? 1 : 0,
    },
  });
  await emit("model_stream_watchdog", {
    model_stream_watchdog: {
      ok,
      status: ok ? (fallbackUsed ? "recovered" : "healthy") : "exhausted",
    },
  });
}

async function parseSseToolCalls(
  response: Response,
  attempt: number,
  model: string,
  emit: EmitRuntimeEvent,
): Promise<{ steps: ToolStep[]; frameCount: number; usage: JsonObject }> {
  const text = await response.text();
  const calls = new Map<number, { id: string; name: string; arguments: string }>();
  let frameCount = 0;
  const usage = emptyUsage();
  for (const rawLine of text.split(/\r?\n/u)) {
    const line = rawLine.trim();
    if (!line.startsWith("data:")) {
      continue;
    }
    const data = line.slice(5).trim();
    if (!data || data === "[DONE]") {
      continue;
    }
    const chunk = asObject(JSON.parse(data));
    const chunkUsage = asObject(chunk.usage);
    if (Object.keys(chunkUsage).length > 0) {
      const promptDetails = asObject(chunkUsage.prompt_tokens_details);
      usage.input_tokens = nonnegativeInteger(chunkUsage.prompt_tokens);
      usage.output_tokens = nonnegativeInteger(chunkUsage.completion_tokens);
      usage.cache_read_input_tokens = nonnegativeInteger(promptDetails.cached_tokens);
    }
    frameCount += 1;
    await emit("model_stream_frame", {
      model_stream_frame: {
        kind: "sse_chunk",
        attempt,
        model,
        frame_index: frameCount,
        chunk,
      },
    });
    const choices = Array.isArray(chunk.choices) ? chunk.choices : [];
    for (const choiceValue of choices) {
      const delta = asObject(asObject(choiceValue).delta);
      const toolCalls = Array.isArray(delta.tool_calls) ? delta.tool_calls : [];
      for (const rawCall of toolCalls) {
        const call = asObject(rawCall);
        const callIndex = Number.isInteger(call.index) ? Number(call.index) : calls.size;
        const current = calls.get(callIndex) || { id: "", name: "", arguments: "" };
        const fn = asObject(call.function);
        current.id = asString(call.id) || current.id;
        current.name += asString(fn.name);
        current.arguments += asString(fn.arguments);
        calls.set(callIndex, current);
      }
    }
  }
  const steps: ToolStep[] = [];
  for (const [index, call] of [...calls.entries()].sort(([left], [right]) => left - right)) {
    if (!call.name) {
      continue;
    }
    let args: JsonObject = {};
    try {
      args = asObject(JSON.parse(call.arguments || "{}"));
    } catch {
      throw new Error("model_tool_arguments_invalid_json:" + call.name);
    }
    steps.push({
      tool_name: call.name,
      arguments: args,
      step_id: call.id || "model_tool_" + String(index),
      metadata: {
        source: "http_sse_model_stream",
        model,
        attempt,
      },
    });
  }
  return { steps, frameCount, usage };
}

function emptyUsage(): JsonObject {
  return {
    input_tokens: 0,
    output_tokens: 0,
    cache_read_input_tokens: 0,
    cache_creation_input_tokens: 0,
    server_tool_use_tokens: 0,
  };
}

function nonnegativeInteger(value: unknown): number {
  const selected = Number(value);
  return Number.isFinite(selected) ? Math.max(0, Math.floor(selected)) : 0;
}

function normalizeMessages(input: RuntimeRunInput): JsonObject[] {
  if (input.messages.length > 0) {
    return input.messages;
  }
  return [{
    role: "user",
    content: asString(asObject(input.metadata).raw_input, "Execute the requested coding task."),
  }];
}

function openAiTool(tool: ToolSpecContract): JsonObject {
  return {
    type: "function",
    function: {
      name: tool.name,
      description: tool.purpose,
      parameters: tool.input_schema,
    },
  };
}

function modelEndpoint(baseUrl: string): string {
  const normalized = baseUrl.replace(/\/+$/u, "");
  return normalized.endsWith("/chat/completions")
    ? normalized
    : normalized + "/chat/completions";
}

function modelHeaders(constraints: JsonObject): Record<string, string> {
  const headers: Record<string, string> = {
    "content-type": "application/json",
    accept: "text/event-stream",
  };
  const apiKey = asString(constraints.model_api_key).trim();
  if (apiKey) {
    headers.authorization = "Bearer " + apiKey;
  }
  return headers;
}

function stringList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => asString(item).trim()).filter(Boolean);
  }
  return asString(value)
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function modelMetadata(value: {
  ok: boolean;
  status: string;
  finalModel: string;
  fallbackUsed: boolean;
  recovered: boolean;
  retryCount: number;
}): Record<string, string> {
  return {
    model_stream_ok: String(value.ok),
    api_retry_ok: String(value.ok),
    api_retry_status: value.status,
    api_retry_fallback_used: String(value.fallbackUsed),
    api_retry_final_model: value.finalModel,
    api_retry_recovered: String(value.recovered),
    api_retry_playbook_ok: String(value.ok),
    api_retry_playbook_status: value.ok
      ? (value.fallbackUsed ? "recovered" : "not_needed")
      : "exhausted",
    model_stream_watchdog_ok: String(value.ok),
    model_stream_watchdog_status: value.ok
      ? (value.fallbackUsed ? "recovered" : "healthy")
      : "exhausted",
    runtime_budget_state_retry_count: String(value.retryCount),
    runtime_budget_replay_ok: String(value.ok),
  };
}
