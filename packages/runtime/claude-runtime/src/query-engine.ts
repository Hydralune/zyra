import {
  asBoolean,
  asObject,
  asString,
  positiveInteger,
  runtimeId,
  uniqueArtifacts,
  type ArtifactReceipt,
  type JsonObject,
  type RuntimeConfig,
  type RuntimeEvent,
  type RuntimeHost,
  type RuntimeRunInput,
  type RuntimeRunResult,
  type ToolExecutionRequest,
  type ToolExecutionResponse,
} from "./contracts.ts";
import {
  normalizeMessages,
  resolveModelTurns,
  type ModelStreamResolution,
} from "./model-stream.ts";
import { RuntimeSession } from "./session.ts";
import type {
  CompactContentBlock,
  CompactMessage,
  SummaryRequest,
} from "./compact/context-runtime.ts";
import {
  normalizeTurns,
  RuntimeToolRegistry,
  scheduleToolBatches,
} from "./tools.ts";
import { TypeScriptControlRuntime } from "./control/index.ts";
import { E01RuntimeCoordinator } from "./e01/coordinator.ts";
import {
  ModelIterationRuntime,
  type ModelIterationSnapshot,
  type ModelRoundRecord,
} from "./loop/model-iteration-runtime.ts";
import {
  PROGRESSIVE_EXECUTION_SNAPSHOT_VERSION,
  ProgressiveExecutionRuntime,
  type ProgressiveExecutionSnapshot,
} from "./loop/progressive-execution-runtime.ts";
import {
  compactBlocksFromMessages,
  digest as skillMemoryDigest,
  type CurrentSkillAuthority,
  type JsonObject as SkillMemoryJsonObject,
  type RestoreProviderKind,
  type SkillAuthorityRevalidationReceipt,
} from "@zyra/skill-memory-runtime";

const DEFAULT_CONFIG: RuntimeConfig = {
  maxTurns: null,
  maxToolResultChars: 8000,
  maxTurnToolResultChars: null,
  maxQueryContextChars: 32000,
  continueOnError: false,
  maxReadOnlyConcurrency: 10,
  emitToolUseSummaries: true,
  allowEmptyTurns: false,
  modelName: "zyra-local-code-model",
  runtimeConstraints: {},
  permissionPolicy: {},
  controlCommands: [],
};

// These events are intentionally high-frequency observations. They remain in
// the runtime event/journal evidence, but persisting the complete session for
// every streamed provider chunk turns one model response into hundreds of
// multi-megabyte atomic writes. Recovery only needs the surrounding semantic
// boundaries (request prepared/report, tool and turn transitions).
const TRANSIENT_CHECKPOINT_PHASES = new Set([
  "message_delta",
  "model_stream_frame",
]);
const DEFAULT_MAX_CONSECUTIVE_LENGTH_CONTINUATIONS = 8;
const MAX_CONFIGURED_LENGTH_CONTINUATIONS = 32;

function redactCompactionText(value: string): string {
  return value
    .replace(/\bbearer\s+[a-z0-9._~+/=-]{8,}/gi, "[REDACTED]")
    .replace(/\bsk-[a-z0-9_-]{8,}/gi, "[REDACTED]")
    .replace(/\b([a-z][a-z0-9+.-]*:\/\/)[^/\s:@]+:[^@\s/]+@/gi, "$1[REDACTED]@")
    .replace(
      /((?:api[_-]?key|access[_-]?token|secret|password|authorization)["']?\s*[:=]\s*["']?)[^\s,;}\]\\"']{4,}/gi,
      "$1[REDACTED]",
    );
}

function boundedCompactionText(value: unknown, maximum = 2_000): string {
  const text = redactCompactionText(String(value ?? "").replaceAll("\0", "").trim());
  if (text.length <= maximum) return text;
  const head = Math.floor(maximum / 3);
  const tail = Math.max(0, maximum - head - 28);
  return `${text.slice(0, head)}\n...[summary excerpt omitted]...\n${text.slice(-tail)}`;
}

function compactionBlocks(value: unknown): CompactContentBlock[] {
  if (!Array.isArray(value)) {
    return [{ type: "text", text: boundedCompactionText(value, 8_000) }];
  }
  return value.flatMap((candidate): CompactContentBlock[] => {
    const block = asObject(candidate);
    const type = asString(block.type);
    if (type === "text") {
      return [{ type: "text", text: boundedCompactionText(block.text, 8_000) }];
    }
    if (type === "tool_use") {
      return [{
        type: "tool_use",
        id: asString(block.id, `tool-use-${runtimeId("compact")}`),
        name: asString(block.name, "tool"),
        input: asObject(block.input),
      }];
    }
    if (type === "tool_result") {
      return [{
        type: "tool_result",
        toolUseId: asString(block.toolUseId, asString(block.tool_use_id, "unknown-tool")),
        content: boundedCompactionText(block.content, 8_000),
        isError: asBoolean(block.isError, asBoolean(block.is_error, false)),
        createdAt: asString(block.createdAt, new Date(0).toISOString()),
        compacted: asBoolean(block.compacted, false),
      }];
    }
    return [{ type: "text", text: boundedCompactionText(JSON.stringify(block), 4_000) }];
  });
}

function modelTranscriptForCompaction(
  transcript: readonly JsonObject[],
): CompactMessage[] {
  const createdAt = new Date().toISOString();
  return transcript.map((message, index) => {
    const roleValue = asString(message.role, "user");
    const role = roleValue === "system" || roleValue === "assistant" || roleValue === "tool"
      ? roleValue
      : "user";
    const metadata = asObject(message.metadata);
    return {
      id: asString(message.message_id, `model-transcript-${index}`),
      role,
      content: compactionBlocks(message.content),
      createdAt: asString(message.created_at, createdAt),
      turnIndex: Number.isInteger(message.turn_index) ? Number(message.turn_index) : null,
      apiRound: Number.isInteger(metadata.model_round_index)
        ? Number(metadata.model_round_index)
        : index,
      synthetic: asBoolean(metadata.synthetic, false),
      metadata,
    };
  });
}

function blockSummaryText(block: CompactContentBlock): string {
  if (block.type === "text") return block.text;
  if (block.type === "tool_use") return `requested ${block.name}`;
  if (block.type === "tool_result") {
    return `${block.isError ? "failed" : "completed"} tool ${block.toolUseId}: ${
      typeof block.content === "string" ? block.content : JSON.stringify(block.content)
    }`;
  }
  if (block.type === "thinking") return block.thinking;
  if (block.type === "attachment") return `${block.name}: ${block.content}`;
  return "";
}

export async function durableCompactionSummary(request: SummaryRequest): Promise<string> {
  const objective = request.messages
    .filter((message) => message.role === "user")
    .map((message) => message.content.filter((block) => block.type === "text").map(blockSummaryText).join("\n"))
    .find((value) => value.trim().length > 0) ?? "Continue the current governed task.";
  const assistantNotes = request.messages
    .filter((message) => message.role === "assistant")
    .map((message) => message.content.filter((block) => block.type === "text").map(blockSummaryText).join("\n"))
    .filter((value) => value.trim().length > 0)
    .slice(-5)
    .map((value) => boundedCompactionText(value, 1_600));
  const toolOutcomes = request.messages
    .flatMap((message) => message.content.filter((block) => block.type === "tool_result"))
    .slice(-10)
    .map((block) => boundedCompactionText(blockSummaryText(block), 1_200));
  const previous = boundedCompactionText(request.previousSummary, 3_000);
  const sections = [
    "## Active objective",
    boundedCompactionText(objective, 2_500),
    "",
    "## Historical reasoning and durable progress",
    "Reasoning snippets may include superseded plans or inspections completed by later tool outcomes; they are not an implicit to-do list.",
    ...(previous ? [previous] : []),
    ...(assistantNotes.length > 0 ? assistantNotes.map((item) => `- ${item}`) : ["- No separate assistant note was retained."]),
    "",
    "## Verified tool observations",
    ...(toolOutcomes.length > 0 ? toolOutcomes.map((item) => `- ${item}`) : ["- No completed tool observation was retained."]),
    "",
    "## Open work",
    "Continue from the newest concrete conclusions and verified outcomes. Inspect the persisted diff only when its current state is not already recorded, revalidate externally mutable claims, and finish every unverified delivery requirement without repeating completed inspection.",
  ];
  const maximum = Math.max(4_000, Math.min(16_000, request.tokenBudget * 4));
  return boundedCompactionText(sections.join("\n"), maximum);
}

interface ResourceCloseoutBudget {
  deadlineEpochMs: number;
  closeoutReserveMs: number;
}

function resourceCloseoutBudget(config: RuntimeConfig): ResourceCloseoutBudget | null {
  const deadlineEpochMs = Number(config.runtimeConstraints.external_deadline_epoch_ms);
  const closeoutReserveSeconds = Number(
    config.runtimeConstraints.closeout_reserve_seconds,
  );
  if (
    !Number.isFinite(deadlineEpochMs)
    || deadlineEpochMs <= 0
    || !Number.isFinite(closeoutReserveSeconds)
    || closeoutReserveSeconds <= 0
  ) {
    return null;
  }
  return {
    deadlineEpochMs: Math.floor(deadlineEpochMs),
    closeoutReserveMs: Math.max(1_000, Math.floor(closeoutReserveSeconds * 1_000)),
  };
}

function resourceCloseoutMessage(): string {
  return [
    "The execution has entered a resource-aware closeout window.",
    "Do not start a new side effect. Reconcile any background work and use",
    "the durable artifacts, receipts, and verification evidence already available.",
    "Do not call any tool and do not emit tool-call markup, JSON, XML, DSML, or commands.",
    "Use only the durable observations already returned by completed tools.",
    "Return a concise plain-language final answer to the original request now.",
  ].join(" ");
}

function closeoutFinalTextAttemptsToolCall(text: string): boolean {
  const normalized = text.trim().toLowerCase();
  return normalized.includes("<｜｜dsml｜｜tool_calls>")
    || normalized.includes("<||dsml||tool_calls>")
    || normalized.includes("<tool_call")
    || normalized.includes("<function_calls")
    || /["']tool_calls["']\s*:/.test(normalized);
}

interface SettledProviderResolution {
  model: ModelStreamResolution;
  round: ModelRoundRecord;
  truncationExhausted: boolean;
}

export class ClaudeRuntimeCore {
  async run(input: RuntimeRunInput, host: RuntimeHost): Promise<RuntimeRunResult> {
    // Retain QueryEngine.ask's outer lifecycle ordering in the real default
    // owner: construct the canonical query, submit/iterate, route failure, and
    // always settle query state from finally even when initialization or a
    // provider/tool branch throws before the normal terminal path.
    let sourceQuery: E01RuntimeCoordinator | null = null;
    let sourceQueryOk = false;
    let sourceQueryStopReason: string | null = "runtime_initialization_failed";
    let sourceResult: RuntimeRunResult | null = null;
    try {
    const config = normalizeConfig(input.config);
    const e01 = new E01RuntimeCoordinator(
      input.runId,
      input.sessionId,
      input.taskId,
      input.workerRequestId,
    );
    sourceQuery = e01;
    const restoredE01 = selectRestoredE01Snapshot(input.restoredState);
    if (restoredE01) {
      e01.restore(restoredE01);
    }
    await e01.bootstrap();
    const providerControlPlaneRequired = asBoolean(
      config.runtimeConstraints.provider_control_plane_required,
    );
    const modelTransport = asString(
      config.runtimeConstraints.model_transport
        || config.runtimeConstraints.model_transport_kind,
      "scripted",
    );
    const restoredIteration = selectRestoredModelIterationSnapshot(input.restoredState);
    const iteration = new ModelIterationRuntime({
      sessionId: input.sessionId,
      runId: input.runId,
      taskId: input.taskId,
      workerRequestId: input.workerRequestId,
    });
    let providerMessages = normalizeMessages(input);
    if (restoredIteration?.phase === "ready") {
      iteration.restore(restoredIteration, true);
      providerMessages = structuredClone(restoredIteration.transcript);
    } else {
      iteration.start(providerMessages, {
        // maxTurns bounds tool-bearing turns. HTTP model loops need one
        // additional provider round to turn the final tool observation into
        // a user-facing answer without granting another tool execution.
        maximumRounds: config.maxTurns === null
          ? null
          : config.maxTurns + (modelTransport === "http_sse" ? 1 : 0),
        maximumToolCalls: config.maxTurns === null
          ? null
          : Math.max(1_000, config.maxTurns * 32),
      });
    }
    if (!providerControlPlaneRequired) {
      await e01.configureProviderRuntime({
        providerId: modelTransport === "http_sse" ? "compatible" : "local",
        modelId: config.modelName,
        baseUrl: asString(config.runtimeConstraints.model_api_base_url),
        apiKey: asString(
          config.runtimeConstraints.model_api_key
            || config.runtimeConstraints.api_key,
        ),
        timeoutMs: Math.max(
          100,
          Math.min(
            3_600_000,
            (Number(config.runtimeConstraints.model_api_timeout_seconds) || 30) * 1000,
          ),
        ),
      });
    }
    const registry = new RuntimeToolRegistry(input.tools);
    let turns = normalizeTurns(input.turns);
    const restored = selectRestoredSnapshot(input.restoredState);
    const session = restored
      ? RuntimeSession.restore(restored, {
        sessionId: input.sessionId,
        runId: input.runId,
        taskId: input.taskId,
        workerRequestId: input.workerRequestId,
      })
      : RuntimeSession.create(
        input.sessionId,
        input.runId,
        input.taskId,
        input.workerRequestId,
        input.messages,
      );
    const processedResume = restored
      ? await e01.processResumedConversation(
        restored,
        asString(asObject(input.metadata).cwd, process.cwd()),
      )
      : null;
    e01.attachSessionProjection(
      input.messages.map((message) => message as unknown as JsonObject),
    );
    const artifacts: ArtifactReceipt[] = [];
    const stepSummaries: string[] = [];
    let eventSequence = 0;
    let toolCallCount = 0;
    let mutatingToolCount = 0;
    let turnCount = 0;
    let toolResultExternalizations = 0;
    let toolFailureSignals = 0;
    let toolSchemaErrors = 0;
    let toolConflictProtected = 0;
    let repeatedToolFailureTrips = 0;
    let previousFailedToolSignature = "";
    let repeatedToolFailureCount = 0;
    let consecutiveInvalidArgumentFailures = 0;
    let invalidArgumentRetryTrips = 0;
    let ok = true;
    let stoppedReason: string | null = null;
    let continuedFailureReason: string | null = null;
    let permissionSuspended = false;
    let providerRoundIndex = 0;
    let activeIterationRoundId: string | null = null;
    let pendingRestoreProviderMessage: JsonObject | null = null;
    // Restored workspace bytes are fenced as untrusted and stripped of secrets
    // by the projector.  Count both so a trace can show the defense ran on this
    // run rather than only that the code exists.
    let restoreUntrustedAttachments = 0;
    let restoreRedactedAttachments = 0;
    // The task API projection reports a restore as "applied" from these two
    // counts.  Without them every run reported no restore, including runs where
    // one genuinely landed.
    let restoreApplicationCount = 0;
    let restoreModelMessageCount = 0;
    const mutationTargets = new Set<string>();
    let modelMetadata: Record<string, string> = {
      model_stream_ok: "false",
      api_retry_ok: "false",
      api_retry_recovered: "false",
    };
    const resourceBudget = resourceCloseoutBudget(config);
    let closeoutRequested = false;
    const progressive = new ProgressiveExecutionRuntime({
      constraints: config.runtimeConstraints,
      deliveryContract: asObject(asObject(input.metadata).delivery_contract),
      restored: selectRestoredProgressiveExecutionSnapshot(input.restoredState) ?? undefined,
    });

    const emit = async (phase: string, payload: JsonObject = {}): Promise<void> => {
      eventSequence += 1;
      const publicPayload = publicRuntimeEventPayload(phase, payload);
      const event: RuntimeEvent = {
        ...publicPayload,
        phase,
        sequence: eventSequence,
        canonical_owner: "typescript",
        runtime_id: "zyra-typescript-claude-runtime",
        session_id: input.sessionId,
        run_id: input.runId,
        task_id: input.taskId,
        worker_request_id: input.workerRequestId,
      };
      if (phase === "model_stream_report") {
        e01.observeProviderGateway(phase, {
          ...payload,
          provider_base_url: config.runtimeConstraints.model_api_base_url ?? null,
        });
      }
      e01.recordRuntimeEvent(phase, payload);
      await host.emitEvent({ ...event, e01_revision: e01.journal.revision });
      if (!TRANSIENT_CHECKPOINT_PHASES.has(phase)) {
        await host.checkpointState?.({
          ...session.snapshot(),
          e01Runtime: e01.snapshot() as unknown as JsonObject,
          modelIteration: iteration.snapshot() as unknown as JsonObject,
          progressiveExecution: progressive.snapshot() as unknown as JsonObject,
          checkpointPhase: phase,
          checkpointEventSequence: eventSequence,
        });
      }
    };

    const settleProviderResolution = async (
      initialModel: ModelStreamResolution,
      initialRound: ModelRoundRecord,
      allowLengthContinuation: boolean,
      continuationTools: ReturnType<RuntimeToolRegistry["list"]>,
    ): Promise<SettledProviderResolution> => {
      let model = initialModel;
      let round = initialRound;
      let continuationCount = 0;
      const maximumLengthContinuations = Math.max(
        1,
        Math.min(
          MAX_CONFIGURED_LENGTH_CONTINUATIONS,
          positiveInteger(
            config.runtimeConstraints.max_length_continuations,
            DEFAULT_MAX_CONSECUTIVE_LENGTH_CONTINUATIONS,
          ),
        ),
      );
      const providerRoundLimit = config.maxTurns === null
        ? null
        : config.maxTurns + (modelTransport === "http_sse" ? 1 : 0);
      while (model.ok) {
        const progress = progressive.observeProviderRound(model.finalText, model.turns.flat().length);
        const progressDecision = progressive.decide(session.contextChars(), config.maxQueryContextChars);
        if (
          progressDecision.action === "nudge_action"
          && model.turns.flat().length === 0
          && !providerOutputWasLengthTruncated(model)
          && continuationTools.length > 0
          && (providerRoundLimit === null || providerRoundIndex < providerRoundLimit)
        ) {
          const nudgedProgress = progressive.recordActionNudge();
          iteration.rejectProviderRoundForRetry(round.roundId, "progressive_action_required");
          providerMessages = [
            ...iteration.currentMessages(),
            ...(model.finalText.trim()
              ? [{ role: "assistant", content: model.finalText }]
              : []),
            {
              role: "user",
              content: [
                "The required delivery is still missing and further explanation is not effective progress.",
                "Perform a concrete, proportionate tool action that advances the requested result.",
                "Then validate it and continue from the evidence.",
              ].join(" "),
            },
          ];
          await emit("progressive_action_requested", {
            reason: progressDecision.reason,
            execution_phase: progress.phase,
            action_nudge: nudgedProgress.actionNudgeCount,
            analysis_only_rounds: progress.analysisOnlyRounds,
            repeated_analysis_rounds: progress.repeatedAnalysisRounds,
            required_delivery_missing: progress.requiredDeliveryMissing,
          });
          round = iteration.beginProviderRound({
            requestKey: `${input.workerRequestId}:provider-round:${providerRoundIndex}`,
            model: config.modelName,
            messages: providerMessages,
          });
          model = await resolveModelTurns(
            input,
            config,
            [],
            continuationTools,
            emit,
            (observation) => e01.decideProviderRecovery(observation),
            e01.journal.restartEpoch,
            (observation) => e01.completeProviderRecovery(observation),
            (requestId) => e01.executePreparedProvider(requestId),
            providerRoundIndex,
            providerMessages,
          );
          providerRoundIndex += 1;
          continue;
        }
        iteration.acceptProviderResult({
          roundId: round.roundId,
          providerRequestId: model.providerRequestId,
          model: config.modelName,
          stopReason: model.stopReason,
          finalText: model.finalText,
          steps: model.turns.flat(),
        });
        if (!providerOutputWasLengthTruncated(model)) {
          return { model, round, truncationExhausted: false };
        }
        if (
          !allowLengthContinuation
          || continuationCount >= maximumLengthContinuations
          || (providerRoundLimit !== null && providerRoundIndex >= providerRoundLimit)
        ) {
          return { model, round, truncationExhausted: true };
        }
        continuationCount += 1;
        providerMessages = [
          ...iteration.currentMessages(),
          {
            role: "user",
            content: [
              "The previous provider response reached its output limit before completing the task.",
              "Continue from the durable context without repeating analysis.",
              "Make a concrete tool call now if work remains; otherwise provide the concise final answer.",
            ].join(" "),
          },
        ];
        await emit("provider_length_continuation_requested", {
          provider_round_index: providerRoundIndex,
          continuation_count: continuationCount,
          maximum_continuations: maximumLengthContinuations,
          previous_stop_reason: model.stopReason,
          previous_final_text_present: model.finalText.trim().length > 0,
          tools_advertised: continuationTools.length,
        });
        round = iteration.beginProviderRound({
          requestKey: `${input.workerRequestId}:provider-round:${providerRoundIndex}`,
          model: config.modelName,
          messages: providerMessages,
        });
        model = await resolveModelTurns(
          input,
          config,
          [],
          continuationTools,
          emit,
          (observation) => e01.decideProviderRecovery(observation),
          e01.journal.restartEpoch,
          (observation) => e01.completeProviderRecovery(observation),
          (requestId) => e01.executePreparedProvider(requestId),
          providerRoundIndex,
          providerMessages,
        );
        providerRoundIndex += 1;
        modelMetadata = {
          ...model.metadata,
          model_provider_rounds: String(providerRoundIndex),
        };
      }
      return { model, round, truncationExhausted: false };
    };

    const requestCloseoutFinalResponse = async (
      baseMessages: JsonObject[],
      turnIndex: number,
    ): Promise<void> => {
      if (!resourceBudget) return;
      closeoutRequested = true;
      const remainingMs = Math.max(0, resourceBudget.deadlineEpochMs - Date.now());
      providerMessages = [
        ...baseMessages,
        { role: "user", content: resourceCloseoutMessage() },
      ];
      await emit("execution_closeout_requested", {
        turn_index: turnIndex,
        deadline_epoch_ms: resourceBudget.deadlineEpochMs,
        closeout_reserve_ms: resourceBudget.closeoutReserveMs,
        remaining_ms: remainingMs,
        tools_advertised: 0,
      });
      const maximumAttempts = 2;
      for (let attempt = 1; attempt <= maximumAttempts; attempt += 1) {
        const finalRound = iteration.beginProviderRound({
          requestKey: `${input.workerRequestId}:provider-round:${providerRoundIndex}`,
          model: config.modelName,
          messages: providerMessages,
        });
        const finalModel = await resolveModelTurns(
          input,
          config,
          [],
          [],
          emit,
          (observation) => e01.decideProviderRecovery(observation),
          e01.journal.restartEpoch,
          (observation) => e01.completeProviderRecovery(observation),
          (requestId) => e01.executePreparedProvider(requestId),
          providerRoundIndex,
          providerMessages,
        );
        providerRoundIndex += 1;
        modelMetadata = {
          ...finalModel.metadata,
          model_provider_rounds: String(providerRoundIndex),
        };
        if (!finalModel.ok) {
          iteration.failProviderRound(
            finalRound.roundId,
            finalModel.error ?? "model_stream_failed",
          );
          ok = false;
          stoppedReason = "model_stream_failed";
          await emit("error", {
            error: stoppedReason,
            detail: finalModel.error ?? "resource-aware finalization failed",
            source: "execution_closeout",
          });
          return;
        }
        const attemptedToolCall = finalModel.turns.length > 0
          || closeoutFinalTextAttemptsToolCall(finalModel.finalText);
        if (attemptedToolCall) {
          const retryAllowed = attempt < maximumAttempts
            && resourceBudget.deadlineEpochMs - Date.now() > 5_000;
          if (retryAllowed) {
            iteration.rejectProviderRoundForRetry(
              finalRound.roundId,
              "closeout_tool_call_rejected",
            );
            providerMessages = [
              ...providerMessages,
              {
                role: "user",
                content: [
                  "Your previous response attempted a tool call, but tools are permanently disabled.",
                  "Do not repeat or describe that call.",
                  "Return only a short plain-language completion summary based on prior tool results.",
                ].join(" "),
              },
            ];
            await emit("execution_closeout_retry_requested", {
              turn_index: turnIndex,
              attempt,
              next_attempt: attempt + 1,
              maximum_attempts: maximumAttempts,
              remaining_ms: Math.max(0, resourceBudget.deadlineEpochMs - Date.now()),
              tools_advertised: 0,
            });
            continue;
          }
          iteration.failProviderRound(finalRound.roundId, "closeout_tool_call_rejected");
          ok = false;
          stoppedReason = "closeout_tool_call_rejected";
          await emit("error", {
            error: stoppedReason,
            detail: "provider attempted a tool call when no tools were advertised",
            source: "execution_closeout",
          });
          return;
        }
        const settled = await settleProviderResolution(
          finalModel,
          finalRound,
          false,
          [],
        );
        if (!settled.model.ok || settled.truncationExhausted) {
          iteration.failProviderRound(
            settled.round.roundId,
            settled.model.error ?? "closeout_response_truncated",
          );
          ok = false;
          stoppedReason = settled.truncationExhausted
            ? "closeout_response_truncated"
            : "model_stream_failed";
          await emit("error", {
            error: stoppedReason,
            detail: settled.model.error ?? "resource-aware final response was truncated",
            source: "execution_closeout",
          });
          return;
        }
        activeIterationRoundId = null;
        turns = [];
        await emit("execution_closeout_completed", {
          turn_index: turnIndex,
          attempt,
          remaining_ms: Math.max(0, resourceBudget.deadlineEpochMs - Date.now()),
          tools_advertised: 0,
          provider_round_index: providerRoundIndex - 1,
        });
        return;
      }
    };

    await emit(restored ? "context_restored" : "session_started", {
      restored: Boolean(restored),
      resume_process_id: processedResume?.processId ?? null,
      resume_session_id: processedResume?.sessionId ?? null,
      resume_content_replacements_seeded:
        processedResume?.seededContentReplacements ?? false,
      model_name: config.modelName,
      registry_size: registry.list().length,
    });
    if (resourceBudget) {
      await emit("execution_resource_budget_accepted", {
        deadline_epoch_ms: resourceBudget.deadlineEpochMs,
        closeout_reserve_ms: resourceBudget.closeoutReserveMs,
        remaining_ms: Math.max(0, resourceBudget.deadlineEpochMs - Date.now()),
      });
    }

    const restoredControl = asObject(asObject(input.restoredState).typescriptControl);
    const controlRuntime = new TypeScriptControlRuntime(config.modelName, restoredControl);
    let controlCommandFailed = 0;
    let resumePlanCount = 0;
    for (const rawCommand of config.controlCommands) {
      const command = asObject(rawCommand);
      const receipt = controlRuntime.apply(rawCommand);
      const name = receipt.name;
      const status = receipt.status;
      if (!receipt.accepted) {
        controlCommandFailed += 1;
      }
      if (name === "resume" && receipt.accepted) {
        resumePlanCount += 1;
      }
      let artifactId = "";
      if (command.artifact_policy === "artifact") {
        const artifact = await host.externalize({
          requestId: runtimeId("control_artifact_request"),
          title: "Claude control command: " + name,
          kind: "structured_data",
          extension: ".json",
          content: JSON.stringify({
            name,
            status,
            canonical_owner: "typescript",
            runtime_id: "zyra-typescript-claude-runtime",
            session_id: input.sessionId,
            request_id: receipt.requestId,
            revision_before: receipt.revisionBefore,
            revision_after: receipt.revisionAfter,
            effect: receipt.effect,
          }),
          metadata: {
            source: "typescript_control_command",
            command_name: name,
            command_status: status,
          },
        });
        artifacts.push(artifact);
        artifactId = artifact.artifact_id;
      }
      await emit("control_command", {
        name,
        status,
        accepted: receipt.accepted,
        changed: receipt.changed,
        request_id: receipt.requestId,
        revision_before: receipt.revisionBefore,
        revision_after: receipt.revisionAfter,
        effect: receipt.effect,
        error: receipt.error,
        artifact_id: artifactId,
        control_owner: "typescript",
      });
    }
    config.modelName = controlRuntime.modelName;
    if (controlRuntime.compactRequested) {
      config.runtimeConstraints.force_compact_restore = true;
    }
    if (controlRuntime.cancelled) {
      ok = false;
      stoppedReason = "user_cancelled";
    }

    const disabledComponents = [
      "disable_tool_registry_runtime",
      "disable_tool_execution_runtime",
      "disable_tool_result_budget_runtime",
      "disable_tool_permission_handoff_runtime",
      "disable_compact_restore_runtime",
      "disable_model_stream_runtime",
      "disable_provider_model_runtime",
      "disable_provider_transport_runtime",
      "disable_provider_credential_runtime",
      "disable_runtime_budget_state",
      "disable_context_security_runtime",
      "disable_restore_integration_runtime",
    ].filter((name) => asBoolean(config.runtimeConstraints[name]));
    if (disabledComponents.length > 0) {
      ok = false;
      stoppedReason = disabledComponents.some((name) => name.startsWith("disable_tool_"))
        ? "tool_loop_foundation_disabled"
        : "codeworker_api_foundation_disabled";
      await emit("codeworker_api_foundation", {
        ok: false,
        stopped_reason: stoppedReason,
        disabled_components: disabledComponents,
      });
      await emit("runtime_budget_replay", {
        runtime_budget_replay: { ok: false, disabled_components: disabledComponents },
      });
      await emit("error", {
        error: stoppedReason,
        disabled_components: disabledComponents,
      });
    }

    if (
      ok
      && modelTransport === "http_sse"
      && resourceBudget !== null
      && progressive.decide(session.contextChars(), config.maxQueryContextChars).action === "closeout"
    ) {
      await requestCloseoutFinalResponse(providerMessages, -1);
    } else if (ok) {
      const iterationRound = modelTransport === "http_sse"
        ? iteration.beginProviderRound({
          requestKey: `${input.workerRequestId}:provider-round:${providerRoundIndex}`,
          model: config.modelName,
          messages: providerMessages,
        })
        : null;
      const model = await resolveModelTurns(
        input,
        config,
        turns,
        registry.list(),
        emit,
        (observation) => e01.decideProviderRecovery(observation),
        e01.journal.restartEpoch,
        (observation) => e01.completeProviderRecovery(observation),
        (requestId) => e01.executePreparedProvider(requestId),
        providerRoundIndex,
        providerMessages,
      );
      providerRoundIndex += modelTransport === "http_sse" ? 1 : 0;
      turns = model.turns;
      modelMetadata = model.metadata;
      if (!model.ok) {
        if (iterationRound) iteration.failProviderRound(iterationRound.roundId, model.error ?? "model_stream_failed");
        ok = false;
        stoppedReason = model.error === "model_error" ? "model_error" : "model_stream_failed";
        await emit("error", {
          error: stoppedReason,
          detail: model.error || "model stream failed",
          source: "model_stream",
        });
      } else if (iterationRound) {
        const settled = await settleProviderResolution(
          model,
          iterationRound,
          true,
          registry.list(),
        );
        if (!settled.model.ok) {
          iteration.failProviderRound(
            settled.round.roundId,
            settled.model.error ?? "model_stream_failed",
          );
          ok = false;
          stoppedReason = "model_stream_failed";
          await emit("error", {
            error: stoppedReason,
            detail: settled.model.error ?? "provider length continuation failed",
            source: "model_iteration_runtime",
          });
        } else if (settled.truncationExhausted) {
          iteration.fail("model_output_truncated");
          ok = false;
          stoppedReason = "model_output_truncated";
          await emit("error", {
            error: stoppedReason,
            detail: "provider exhausted bounded length-continuation attempts",
            source: "model_iteration_runtime",
          });
        } else {
          turns = settled.model.turns;
          activeIterationRoundId = turns.length > 0 ? settled.round.roundId : null;
        }
      }
    }

    const turnLimit = config.maxTurns;
    const restoredActiveTurn = session.activeTurnSnapshot();
    const initialTurnIndex = restoredActiveTurn?.turn_index ?? 0;
    for (let turnIndex = initialTurnIndex; ok && turnIndex < turns.length; turnIndex += 1) {
      if (
        modelTransport === "http_sse"
        && activeIterationRoundId
        && resourceBudget !== null
        && progressive.decide(session.contextChars(), config.maxQueryContextChars).action === "closeout"
      ) {
        // The provider proposed this tool turn before the closeout boundary,
        // but the boundary arrived before execution began.  Finalize from the
        // last valid request transcript rather than starting a new side effect.
        iteration.abandonPlannedToolRoundForFinalization(
          activeIterationRoundId,
          "execution_closeout",
        );
        await requestCloseoutFinalResponse(providerMessages, turnIndex);
        break;
      }
      const queryDecision = e01.decideQuery("turn_preflight", {
        turnIndex,
        turnLimit,
        empty: turns[turnIndex].length === 0,
        allowEmpty: config.allowEmptyTurns,
        aborted: host.isAborted()
          || asBoolean(config.runtimeConstraints.abort_before_turn)
          || Number(config.runtimeConstraints.abort_at_turn) === turnIndex,
      });
      if (!queryDecision.accepted && queryDecision.reason === "max_turns_exceeded") {
        ok = false;
        stoppedReason = "max_turns_exceeded";
        await emit("error", {
          error: stoppedReason,
          max_turns: turnLimit,
        });
        break;
      }
      if (
        !queryDecision.accepted
        && queryDecision.reason === "user_cancelled"
      ) {
        ok = false;
        stoppedReason = "user_cancelled";
        await emit("error", {
          error: stoppedReason,
          turn_index: turnIndex,
        });
        break;
      }

      const steps = turns[turnIndex];
      if (!queryDecision.accepted && queryDecision.reason === "empty_query_turn") {
        ok = false;
        stoppedReason = "empty_query_turn";
        await emit("error", {
          error: stoppedReason,
          turn_index: turnIndex,
        });
        break;
      }

      const prompt = steps.map((step) => step.prompt ?? "").filter(Boolean).join("\n");
      const activeTurn = session.activeTurnSnapshot();
      const resumingTurn = activeTurn?.turn_index === turnIndex;
      const turn = resumingTurn && activeTurn
        ? activeTurn
        : session.beginTurn(turnIndex, prompt);
      if (resumingTurn) {
        await emit("turn_resumed", {
          turn_id: turn.turn_id,
          turn_index: turnIndex,
          tool_count: steps.length,
        });
      } else {
        e01.beginCanonicalTurn(turn.turn_id, turnIndex, prompt);
        turnCount += 1;
        await emit("turn_started", {
          turn_id: turn.turn_id,
          turn_index: turnIndex,
          tool_count: steps.length,
          user_content: prompt,
        });
        await emit("turn_start", {
          turn_id: turn.turn_id,
          turn_index: turnIndex,
          tool_count: steps.length,
        });
      }
      await emit("stream_request_start", {
        turn_id: turn.turn_id,
        turn_index: turnIndex,
        model_name: config.modelName,
      });
      await emit("message_delta", {
        turn_id: turn.turn_id,
        turn_index: turnIndex,
        delta: prompt || "Tool execution turn started.",
        delta_kind: "planning",
      });

      const batches = e01.planToolBatches(
        turn.turn_id,
        turnIndex,
        steps.map((step) => ({
          step,
          readOnly: registry.readOnly(step.tool_name),
        })),
        config.maxReadOnlyConcurrency,
      );
      await emit("tool_loop_plan", {
        turn_id: turn.turn_id,
        turn_index: turnIndex,
        batch_count: batches.length,
        tool_count: steps.length,
      });

      let turnResultChars = 0;
      let turnOk = true;
      let turnError: string | null = null;
      let turnMustStop = false;
      for (const batch of batches) {
        e01.startToolBatch(batch);
        let conflictProtected = false;
        for (const step of batch.steps) {
          if (registry.readOnly(step.tool_name)) {
            continue;
          }
          const target = mutationTarget(step);
          if (target && mutationTargets.has(target)) {
            conflictProtected = true;
            toolConflictProtected += 1;
          }
          if (target) {
            mutationTargets.add(target);
          }
        }
        await emit("tool_batch_started", {
          turn_id: turn.turn_id,
          turn_index: turnIndex,
          batch_id: batch.batchId,
          execution_mode: batch.executionMode,
          tool_count: batch.steps.length,
          conflict_protected: String(conflictProtected),
        });

        const hostRequests: ToolExecutionRequest[] = [];
        const immediateResults = new Map<string, ToolExecutionResponse>();
        const callIds = new Map<(typeof batch.steps)[number], string>();
        batch.steps.forEach((step, batchIndex) => {
          const toolCallId = step.step_id || runtimeId("toolcall");
          callIds.set(step, toolCallId);
          toolCallCount += 1;
          if (!registry.readOnly(step.tool_name)) {
            mutatingToolCount += 1;
          }
          if (!turn.tool_call_ids.includes(toolCallId)) {
            session.recordToolCall(toolCallId, step.tool_name);
          }
          if (modelTransport === "http_sse") {
            iteration.markToolRunning(toolCallId, turn.turn_id);
          }
          const failures = registry.validate(step);
          if (failures.length > 0) {
            immediateResults.set(toolCallId, {
              tool_call_id: toolCallId,
              ok: false,
              summary: "Tool arguments failed schema validation.",
              output: {
                schema_failures: failures as unknown as JsonObject,
              },
              artifacts: [],
              error: failures[0].code === "unknown_tool" ? "unknown_tool" : "schema_error",
              metadata: {
                canonical_owner: "typescript",
                schema_validated: "false",
                conflict_protected: String(conflictProtected),
              },
            });
          } else if (
            progressive.inspectionCircuitOpen()
            && isClearlyPreDeliveryInspection(step, registry.readOnly(step.tool_name))
          ) {
            immediateResults.set(toolCallId, {
              tool_call_id: toolCallId,
              ok: false,
              summary: "Pre-delivery inspection circuit is open; this read-only action was not executed.",
              output: {
                guidance: [
                  "Use the concrete evidence already gathered and make the next workspace edit.",
                  "A build, test, or real service command is also allowed when it directly drives that edit.",
                  "Further broad inspection becomes available after a committed delivery or in a fresh task phase.",
                ],
                side_effect_executed: false,
              },
              artifacts: [],
              error: "pre_delivery_inspection_budget_exhausted",
              metadata: {
                canonical_owner: "typescript",
                pre_delivery_inspection_blocked: "true",
                physical_effect_executed: "false",
                model_recovery_allowed: "true",
                termination: "exited",
              },
            });
          } else {
            hostRequests.push({
              toolCallId,
              toolName: step.tool_name,
              arguments: step.arguments,
              turnIndex,
              stepIndex: batchIndex,
              batchId: batch.batchId,
              batchIndex,
              batchSize: batch.steps.length,
              executionMode: batch.executionMode,
              metadata: {
                canonical_owner: "typescript",
                schema_validated: true,
                conflict_protected: conflictProtected,
                ...asObject(step.metadata),
              },
            });
          }
        });

        for (const request of hostRequests) {
          await emit("tool_call_started", {
            turn_id: turn.turn_id,
            turn_index: turnIndex,
            batch_id: batch.batchId,
            tool_call_id: request.toolCallId,
            tool_name: request.toolName,
            execution_mode: batch.executionMode,
          });
        }

        const hostResults = hostRequests.length > 0
          ? await host.executeBatch(batch, hostRequests)
          : [];
        const byCallId = new Map<string, ToolExecutionResponse>(
          hostResults.map((result) => [result.tool_call_id, result]),
        );
        const approvalRequired = hostResults.find(
          (result) => result.error === "permission_approval_required",
        );
        if (approvalRequired) {
          permissionSuspended = true;
          turnOk = false;
          turnError = "permission_suspended";
          continuedFailureReason = turnError;
          toolFailureSignals += 1;
        }

        for (const step of batch.steps) {
          const toolCallId = callIds.get(step) || "";
          let result = immediateResults.get(toolCallId) || byCallId.get(toolCallId);
          if (!result) {
            result = {
              tool_call_id: toolCallId || runtimeId("missing_tool_result"),
              ok: false,
              summary: "Python host did not return the requested tool result.",
              output: {},
              artifacts: [],
              error: "missing_tool_result",
              metadata: {
                canonical_owner: "typescript",
              },
            };
          }

          // ASK is a durable pause, not a failed tool effect.  Keep every call
          // in this batch running and leave its E01 lease/effect/result binding
          // intact so an approval can resume the exact scheduled batch.  No
          // sibling result may be committed while one member awaits approval.
          if (approvalRequired) {
            if (result.error === "permission_approval_required") {
              stepSummaries.push(result.tool_call_id + ":permission_suspended");
              await emit("tool_call_suspended", {
                turn_id: turn.turn_id,
                turn_index: turnIndex,
                batch_id: batch.batchId,
                tool_call_id: result.tool_call_id,
                tool_name: step.tool_name,
                reason: "permission_suspended",
              });
              await emit("tool_failure_signal", {
                turn_id: turn.turn_id,
                turn_index: turnIndex,
                tool_call_id: result.tool_call_id,
                tool_name: step.tool_name,
                signal: {
                  kind: "permission_pending",
                  route: "permission_runtime",
                  error: "permission_approval_required",
                },
              });
            }
            continue;
          }

          const remainingTurnBudget = config.maxTurnToolResultChars === null
            ? config.maxToolResultChars
            : Math.max(1, config.maxTurnToolResultChars - turnResultChars);
          const budget = Math.min(config.maxToolResultChars, remainingTurnBudget);
          const budgeted = await e01.enforceToolResultBudget(host, result, budget);
          result = budgeted.result;
          const invalidArguments = result.error === "schema_error"
            || result.error === "tool_schema_validation_failed";
          if (result.error === "pre_delivery_inspection_budget_exhausted") {
            await emit("pre_delivery_inspection_blocked", {
              turn_id: turn.turn_id,
              turn_index: turnIndex,
              tool_call_id: result.tool_call_id,
              tool_name: step.tool_name,
              action_nudges: progressive.snapshot().actionNudgeCount,
              consecutive_pre_delivery_observations:
                progressive.snapshot().consecutivePreDeliveryObservations,
            });
          }
          if (invalidArguments) toolSchemaErrors += 1;
          consecutiveInvalidArgumentFailures = invalidArguments
            ? consecutiveInvalidArgumentFailures + 1
            : 0;
          if (consecutiveInvalidArgumentFailures >= 3) {
            invalidArgumentRetryTrips += 1;
            result = {
              ...result,
              output: {
                ...result.output,
                invalid_argument_recovery: {
                  attempts: consecutiveInvalidArgumentFailures,
                  retry_allowed: false,
                  side_effect_executed: false,
                },
              },
              error: "invalid_tool_arguments_retry_exhausted",
              metadata: {
                ...result.metadata,
                invalid_argument_retry_exhausted: "true",
                physical_effect_executed: "false",
              },
            };
          }
          if (!result.ok) {
            const failureSignature = JSON.stringify([
              step.tool_name,
              step.arguments,
              result.error || "tool_error",
            ]);
            repeatedToolFailureCount = failureSignature === previousFailedToolSignature
              ? repeatedToolFailureCount + 1
              : 1;
            previousFailedToolSignature = failureSignature;
            if (repeatedToolFailureCount >= 3) {
              repeatedToolFailureTrips += 1;
              result = {
                ...result,
                output: {
                  ...result.output,
                  repeated_failure: {
                    count: repeatedToolFailureCount,
                    original_error: result.error || "tool_error",
                    guidance: "The same failed tool call was attempted three times without changing its arguments.",
                  },
                },
                error: "repeated_tool_failure",
                metadata: {
                  ...result.metadata,
                  repeated_tool_failure: "true",
                  repeated_tool_failure_count: String(repeatedToolFailureCount),
                },
              };
            }
          } else {
            previousFailedToolSignature = "";
            repeatedToolFailureCount = 0;
          }
          const toolCustody = e01.completeToolExecution({
            callId: result.tool_call_id,
            toolName: step.tool_name,
            ok: result.ok,
            summary: result.summary,
            output: result.output,
            error: result.error ?? null,
            maximumCharacters: budget,
          });
          const observedRequest = hostRequests.find(
            (request) => request.toolCallId === result.tool_call_id,
          );
          if (observedRequest) {
            progressive.observeToolResult(
              observedRequest,
              result,
              registry.readOnly(step.tool_name),
            );
          }
          turnResultChars += budgeted.originalChars;
          if (budgeted.artifact) {
            artifacts.push(budgeted.artifact);
            toolResultExternalizations += 1;
            toolFailureSignals += 1;
            await emit("tool_result_budget_exceeded", {
              turn_id: turn.turn_id,
              turn_index: turnIndex,
              tool_call_id: result.tool_call_id,
              original_chars: budgeted.originalChars,
              budget_chars: budget,
              artifact_id: budgeted.artifact.artifact_id,
            });
            await emit("tool_failure_signal", {
              turn_id: turn.turn_id,
              turn_index: turnIndex,
              tool_call_id: result.tool_call_id,
              tool_name: step.tool_name,
              signal: {
                kind: "budget_exceeded",
                route: "artifact_externalized",
                error: "tool_result_budget_exceeded",
              },
            });
            await emit("watchdog_signal", {
              turn_id: turn.turn_id,
              turn_index: turnIndex,
              tool_call_id: result.tool_call_id,
              tool_name: step.tool_name,
              watchdog_signal: {
                kind: "budget_exceeded",
                route: "artifact_externalized",
                action: "continue",
              },
            });
          }
          artifacts.push(...result.artifacts);
          session.recordToolResult(step.tool_name, result);
          if (step.tool_name === "skill") {
            try {
              const memoryReceipt = e01.recordSkillToolOutcome({
                toolCallId: result.tool_call_id,
                toolName: step.tool_name,
                ok: result.ok,
                summary: result.summary,
                output: result.output,
                artifacts: result.artifacts.map((artifact) => artifact as unknown as SkillMemoryJsonObject),
                error: result.error ?? null,
                metadata: {
                  ...result.metadata,
                  workspace_root: asString(asObject(input.metadata).cwd),
                },
                identity: {
                  runId: input.runId,
                  taskId: input.taskId,
                  sessionId: input.sessionId,
                  workerRequestId: input.workerRequestId,
                  epoch: e01.journal.restartEpoch,
                },
                eventSequence: eventSequence + 1,
                occurredAt: new Date().toISOString(),
              });
              await emit("skill_memory_updated", {
                turn_id: turn.turn_id,
                turn_index: turnIndex,
                tool_call_id: result.tool_call_id,
                skill_memory: memoryReceipt,
                source_skill_owner: "03C SkillCoordinator",
                memory_owner: "06C SkillMemoryApplication",
              });
            } catch (error) {
              await emit("skill_memory_rejected", {
                turn_id: turn.turn_id,
                turn_index: turnIndex,
                tool_call_id: result.tool_call_id,
                error_code: error && typeof error === "object" && "code" in error
                  ? String((error as { code?: unknown }).code)
                  : "skill_memory_admission_failed",
                message: error instanceof Error ? error.message : String(error),
                skill_invocation_preserved: true,
                fallback_memory_owner: false,
              });
            }
          }
          if (modelTransport === "http_sse") {
            iteration.recordToolObservation({
              callId: result.tool_call_id,
              turnId: turn.turn_id,
              ok: result.ok,
              summary: result.summary,
              output: result.output,
              error: result.error ?? null,
            });
          }
          stepSummaries.push(result.tool_call_id + ":" + result.summary);
          await emit("tool_call_completed", {
            turn_id: turn.turn_id,
            turn_index: turnIndex,
            batch_id: batch.batchId,
            tool_call_id: result.tool_call_id,
            tool_name: step.tool_name,
            execution_mode: batch.executionMode,
            tool_result: result as unknown as JsonObject,
            tool_custody: toolCustody,
          });
          await emit("message_delta", {
            turn_id: turn.turn_id,
            turn_index: turnIndex,
            tool_call_id: result.tool_call_id,
            delta: result.summary,
            delta_kind: "tool_result",
          });
          if (!result.ok) {
            turnOk = false;
            const permissionAbortLoop = asString(
              result.metadata.permission_abort_loop,
            ).toLowerCase() === "true";
            if (result.error === "permission_approval_required" || permissionAbortLoop) {
              permissionSuspended = true;
            }
            turnError = result.error === "permission_approval_required"
              ? "permission_suspended"
              : result.error || "tool_error";
            continuedFailureReason = turnError;
            const modelRecoveryAllowed = modelTransport === "http_sse"
              && modelCanRecoverToolFailure(result);
            const continueAfterFailure = !permissionSuspended
              && (config.continueOnError || modelRecoveryAllowed);
            if (!continueAfterFailure) turnMustStop = true;
            toolFailureSignals += 1;
            const failureKind = result.error === "permission_approval_required"
              || result.error === "permission_denied"
              ? "permission_denied"
              : result.error || "tool_error";
            const failureRoute = failureKind === "permission_denied"
              ? "permission_runtime"
              : failureKind === "pre_delivery_inspection_budget_exhausted"
                ? "progressive_execution"
              : failureKind === "tool_schema_validation_failed" || failureKind === "schema_error"
                ? "repair_tool_arguments"
                : "recovery_planner";
            await emit("tool_failure_signal", {
              turn_id: turn.turn_id,
              turn_index: turnIndex,
              tool_call_id: result.tool_call_id,
              tool_name: step.tool_name,
              signal: {
                kind: failureKind,
                route: failureRoute,
                error: result.error || "tool_error",
              },
            });
            await emit("watchdog_signal", {
              turn_id: turn.turn_id,
              turn_index: turnIndex,
              tool_call_id: result.tool_call_id,
              tool_name: step.tool_name,
              watchdog_signal: {
                kind: failureKind,
                route: failureRoute,
                action: continueAfterFailure ? "continue" : "stop",
              },
            });
            if (continueAfterFailure) {
              await emit("continue", {
                turn_id: turn.turn_id,
                turn_index: turnIndex,
                tool_call_id: result.tool_call_id,
                reason: failureKind,
              });
            }
          }
        }

        if (permissionSuspended) {
          await emit("tool_batch_suspended", {
            turn_id: turn.turn_id,
            turn_index: turnIndex,
            batch_id: batch.batchId,
            execution_mode: batch.executionMode,
            tool_count: batch.steps.length,
            reason: "permission_suspended",
          });
        } else {
          await emit("tool_batch_completed", {
            turn_id: turn.turn_id,
            turn_index: turnIndex,
            batch_id: batch.batchId,
            execution_mode: batch.executionMode,
            tool_count: batch.steps.length,
            ok: turnOk,
            conflict_protected: String(conflictProtected),
          });
          if (config.emitToolUseSummaries) {
            await emit("tool_use_summary", {
              turn_id: turn.turn_id,
              turn_index: turnIndex,
              batch_id: batch.batchId,
              execution_mode: batch.executionMode,
              tool_count: batch.steps.length,
              ok: turnOk,
            });
          }
        }
        if (!turnOk && (turnMustStop || permissionSuspended)) {
          break;
        }
      }

      if (permissionSuspended) {
        await emit("turn_suspended", {
          turn_id: turn.turn_id,
          turn_index: turnIndex,
          reason: "permission_suspended",
        });
        break;
      }

      const continuationDecision = e01.decideContinuationBudget(
        Math.max(0, Math.ceil(session.contextChars() / 4)),
        Math.max(1, Math.ceil(config.maxQueryContextChars / 4)),
      );
      await emit("continuation_budget_decision", {
        turn_index: turnIndex,
        action: continuationDecision.action,
        nudge_message: continuationDecision.action === "continue"
          ? continuationDecision.nudgeMessage
          : null,
        completion_event: continuationDecision.action === "stop"
          ? continuationDecision.completionEvent as unknown as JsonObject
          : null,
      });

      const configuredContextWindow = Math.max(
        8_192,
        Math.ceil(config.maxQueryContextChars / 4) + 5_000,
      );
      const autoCompactCharacterThreshold = e01.compact.getAutoCompactThreshold(
        configuredContextWindow,
        8_192,
      ) * 4;
      const contextDecision = e01.decideContext(
        session.contextChars(),
        Math.min(config.maxQueryContextChars, autoCompactCharacterThreshold),
        asBoolean(config.runtimeConstraints.force_compact_restore),
        session.compactionCount,
      );
      if (contextDecision.accepted) {
        const fallbackCompact = session.compactCandidates();
        const modelCompactSource = modelTranscriptForCompaction(
          iteration.currentMessages(),
        );
        const compactSource = modelCompactSource.length >= 3
          ? modelCompactSource
          : session.messages.map((item, index) => ({
            id: item.message_id,
            role: item.role,
            content: [{ type: "text" as const, text: item.content }],
            createdAt: item.created_at,
            turnIndex: item.turn_index,
            apiRound: item.turn_index ?? index,
            synthetic: item.metadata.synthetic === true,
            metadata: {
              ...item.metadata,
              runtime_tool_call_id: item.tool_call_id,
            },
          }));
        const skillMemoryContextWindow = Math.max(
          8_192,
          Math.ceil(config.maxQueryContextChars / 4),
        );
        const skillMemoryReservedOutput = Math.min(
          8_192,
          skillMemoryContextWindow - 1,
        );
        const skillMemoryTrigger = e01.skillMemory.observeCompactTrigger({
          identity: {
            runId: input.runId,
            taskId: input.taskId,
            sessionId: input.sessionId,
            workerRequestId: input.workerRequestId,
            epoch: e01.journal.restartEpoch,
          },
          kind: asBoolean(config.runtimeConstraints.force_compact_restore) ? "manual" : "threshold",
          currentTokens: Math.max(0, Math.ceil(session.contextChars() / 4)),
          contextWindow: skillMemoryContextWindow,
          reservedOutputTokens: skillMemoryReservedOutput,
          thresholdTokens: Math.max(1, Math.ceil(autoCompactCharacterThreshold / 4)),
          activeToolCallIds: [],
          pendingToolResultIds: [],
          idleMilliseconds: 0,
          compactGeneration: session.compactionCount,
          consecutiveFailures: 0,
          querySource: "ClaudeRuntimeCore.run",
          observedAt: new Date().toISOString(),
          metadata: {
            turn_id: turn.turn_id,
            turn_index: turnIndex,
            context_decision_reason: contextDecision.reason,
          },
        });
        await emit("skill_memory_compact_trigger", {
          turn_id: turn.turn_id,
          turn_index: turnIndex,
          trigger: skillMemoryTrigger as unknown as JsonObject,
        });
        const compactOptions = {
          trigger: asBoolean(config.runtimeConstraints.force_compact_restore) ? "manual" as const : "auto_threshold" as const,
          model: config.modelName,
          contextWindow: Math.max(8_192, Math.ceil(config.maxQueryContextChars / 4)),
          maxOutputTokens: 8_192,
          targetTokens: Math.max(2_048, Math.ceil(config.maxQueryContextChars / 8)),
          preserveRecentMessages: 4,
          preserveApiRounds: 2,
          systemPrompt: "Zyra CodeWorker runtime context",
          customInstructions: "Preserve tool outcomes, artifacts, failures, and pending work.",
          attachments: [],
          querySource: "ClaudeRuntimeCore.run",
          sessionId: input.sessionId,
        };
        const matureCompact = compactSource.length >= 3
          ? asBoolean(config.runtimeConstraints.force_compact_restore)
            ? await e01.compact.compactConversation(
              compactSource,
              compactOptions,
              durableCompactionSummary,
            )
            : await e01.compact.autoCompactIfNeeded(
              compactSource,
              compactOptions,
              durableCompactionSummary,
            )
          : null;
        if (compactSource.length >= 3 && matureCompact === null) {
          await emit("context_compaction_skipped", {
            turn_id: turn.turn_id,
            turn_index: turnIndex,
            reason: "source_runtime_not_compacted",
            compact_owner: "typescript",
          });
        } else {
          const safeCut = compactSource.length >= 3 && skillMemoryTrigger.decision === "compact"
            ? e01.skillMemory.planSafeCut({
              triggerId: skillMemoryTrigger.triggerId,
              compactGeneration: session.compactionCount,
              blocks: compactBlocksFromMessages(compactSource),
              targetTokens: Math.max(1_024, Math.ceil(config.maxQueryContextChars / 8)),
              minimumRecentTurns: 1,
              preserveMessageIds: matureCompact?.boundary.preservedMessageIds ?? fallbackCompact.preserved.map((item) => item.message_id),
              metadata: {
                turn_id: turn.turn_id,
                turn_index: turnIndex,
                source_compact_boundary_id: matureCompact?.boundary.boundaryId ?? null,
              },
            })
            : null;
        const preservedIds = new Set(matureCompact?.boundary.preservedMessageIds ?? fallbackCompact.preserved.map((item) => item.message_id));
        const preserved = session.messages.filter((item) => preservedIds.has(item.message_id));
        const removed = session.messages.filter((item) => !preservedIds.has(item.message_id));
        const compact = {
          content: JSON.stringify({
            session_id: input.sessionId,
            removed_messages: removed,
            parent_checksum: session.parentChecksum,
            boundary: matureCompact?.boundary ?? null,
          }),
          summary: matureCompact?.boundary.summary ?? fallbackCompact.summary,
          preserved,
          removedCount: removed.length,
        };
        const artifact = await host.externalize({
          requestId: runtimeId("compact_request"),
          title: "CodeWorker context compaction",
          kind: "structured_data",
          extension: ".json",
          content: compact.content,
          metadata: {
            source: "typescript_auto_compact",
            session_id: input.sessionId,
            turn_index: turnIndex,
            removed_message_count: compact.removedCount,
          },
        });
        artifacts.push(artifact);
        const postCompactMessages = matureCompact?.messages.map((item) => ({
          message_id: item.id,
          role: item.role,
          content: item.content
            .map((block) => block.type === "text" ? block.text : JSON.stringify(block))
            .join("\n"),
          turn_index: item.turnIndex,
          tool_call_id: asString(item.metadata.runtime_tool_call_id) || null,
          created_at: item.createdAt,
          metadata: item.metadata,
        })) ?? null;
        session.compact(
          compact.summary,
          artifact.artifact_id,
          compact.preserved,
          postCompactMessages,
        );
        if (safeCut?.valid && safeCut.summarizedTokens > 0) {
          const boundaryId = matureCompact?.boundary.boundaryId
            ?? `compact-boundary-${skillMemoryDigest({
              session_id: input.sessionId,
              compact_generation: session.compactionCount,
              safe_cut_plan_id: safeCut.planId,
            }).slice(0, 24)}`;
          const compactedTokens = Math.min(
            safeCut.sourceTokens - 1,
            Math.max(1, Math.ceil(compact.summary.length / 4) + safeCut.preservedTokens),
          );
          const archive = e01.skillMemory.commitArchive({
            artifactId: artifact.artifact_id,
            boundaryId,
            sessionId: input.sessionId,
            compactGeneration: session.compactionCount,
            contentDigest: skillMemoryDigest(compact.content),
            summary: compact.summary,
            safeCutPlan: safeCut,
            tokenCountAfter: compactedTokens,
            metadata: {
              turn_id: turn.turn_id,
              turn_index: turnIndex,
              context_chars_after: session.contextChars(),
            },
          });
          const identity = {
            runId: input.runId,
            taskId: input.taskId,
            sessionId: input.sessionId,
            workerRequestId: input.workerRequestId,
            epoch: e01.journal.restartEpoch,
          };
          const history = compactBlocksFromMessages(compactSource);
          const allowedTools = registry.list().map((tool) => tool.name);
          const deniedTools = runtimeStringArray(config.runtimeConstraints.denied_tools);
          // A CodeWorker registry scope is not a BrowserWorker grant. Only an
          // explicitly declared browser scope may cross this context handoff;
          // an empty scope restores no authority and leaves the BrowserWorker
          // permission runtime to decide every requested action.
          const browserAllowedTools = runtimeStringArray(
            config.runtimeConstraints.browser_allowed_tools,
          );
          const browserDeniedTools = runtimeStringArray(
            config.runtimeConstraints.browser_denied_tools,
          );
          const skillMemories = e01.skillMemory.outcomes.reusable({
            taskId: input.taskId,
            sessionId: input.sessionId,
            limit: 64,
          });
          const procedures = e01.skillMemory.listProcedures({
            consumers: ["context", "routing", "recovery"],
            validatedOnly: true,
            limit: 64,
          });
          const providerId = asString(
            config.runtimeConstraints.provider_id,
            modelTransport === "http_sse" ? "compatible" : "local",
          );
          const providerKind = restoreProviderKind(
            asString(config.runtimeConstraints.provider_kind),
            providerId,
          );
          const authorityReceipts: SkillAuthorityRevalidationReceipt[] = [];
          for (const memory of skillMemories) {
            const currentAuthority = currentAuthorityFromMemory(memory.metadata);
            if (!currentAuthority) continue;
            try {
              authorityReceipts.push(e01.skillMemory.integration.revalidateMemory({
                memory,
                current: currentAuthority,
                parentAllowedTools: allowedTools,
                parentDeniedTools: deniedTools,
                causationId: boundaryId,
                metadata: {
                  compact_boundary_id: boundaryId,
                  runtime_resolution_required_on_next_invoke: true,
                },
              }));
            } catch (error) {
              await emit("skill_memory_authority_revalidation_failed", {
                boundary_id: boundaryId,
                memory_id: memory.memoryId,
                error: error instanceof Error ? error.message : String(error),
                current_03c_resolution_required: true,
                historical_outcome_executable: false,
              });
            }
          }
          let integratedPreparation: ReturnType<typeof e01.skillMemory.integration.prepare> | null = null;
          try {
            integratedPreparation = e01.skillMemory.integration.prepare({
              identity,
              workerKind: "code",
              boundaryId,
              archive,
              history,
              goal: prompt.trim() || asString(config.runtimeConstraints.goal, "Continue the current task"),
              constraints: runtimeStringArray(config.runtimeConstraints.constraints),
              requirementChanges: runtimeStringArray(config.runtimeConstraints.requirement_changes),
              skillMemories,
              procedures,
              existingAttachments: [],
              runtimeConstraints: config.runtimeConstraints as SkillMemoryJsonObject,
              allowedTools,
              deniedTools,
              providerId,
              modelId: config.modelName,
              providerKind,
              providerCapabilities: restoreCapabilities(config.runtimeConstraints, providerKind),
              gatewayCapabilities: runtimeStringArray(config.runtimeConstraints.gateway_capabilities),
              frames: [],
              sourceProviderId: asString(config.runtimeConstraints.source_provider_id) || null,
              sourceModelId: asString(config.runtimeConstraints.source_model_id) || null,
              contextWindow: skillMemoryContextWindow,
              maximumTokens: Math.max(1_024, Math.ceil(config.maxQueryContextChars / 4)),
              metadata: {
                turn_id: turn.turn_id,
                turn_index: turnIndex,
                compact_artifact_id: artifact.artifact_id,
                same_session_continuity: true,
              },
            });
          } catch (error) {
            const failure = e01.skillMemory.integration.failures.record({
              identity,
              boundaryId,
              stage: "retrieval_composition",
              code: error && typeof error === "object" && "code" in error
                ? String((error as { code?: unknown }).code)
                : "skill_memory_integration_prepare_failed",
              message: error instanceof Error ? error.message : String(error),
              retryable: true,
              baselineTextReferenceAvailable: true,
              canonicalCheckpointAvailable: true,
              currentAuthorityRequired: true,
              provenanceComplete: false,
              relatedIds: [archive.archiveId, artifact.artifact_id],
              causationId: boundaryId,
              details: { baseline_02b_02d_restore_preserved: true },
            });
            await emit("skill_memory_integration_deferred", {
              boundary_id: boundaryId,
              reason: error instanceof Error ? error.message : String(error),
              failure: e01.skillMemory.integration.failures.event(failure.failureId),
              baseline_02b_02d_restore_preserved: true,
              experimental_fallback_used: false,
            });
          }
          const preparedRestore = e01.skillMemory.prepareRestore({
            identity,
            workerKind: "code",
            boundaryId,
            archive,
            summary: compact.summary,
            restoredAttachments: integratedPreparation?.attachments ?? [],
            parentAllowedTools: allowedTools,
            restoredAllowedTools: allowedTools,
            deniedTools,
            maximumTokens: Math.max(1_024, Math.ceil(config.maxQueryContextChars / 4)),
            metadata: {
              turn_id: turn.turn_id,
              turn_index: turnIndex,
              compact_artifact_id: artifact.artifact_id,
            },
          });
          const appliedRestore = e01.skillMemory.applyRestore(
            preparedRestore.projection.projectionId,
          );
          pendingRestoreProviderMessage = mergeRestoreProviderMessages(
            appliedRestore.providerMessage,
            integratedPreparation?.providerMessage ?? null,
          );
          const restoreSecurity = asObject(appliedRestore.providerMessage.metadata);
          restoreUntrustedAttachments += Number(restoreSecurity.untrusted_attachment_count ?? 0);
          restoreRedactedAttachments += Number(restoreSecurity.redacted_attachment_count ?? 0);
          restoreApplicationCount += 1;
          restoreModelMessageCount += pendingRestoreProviderMessage ? 1 : 0;
          if (integratedPreparation) {
            const integratedApplication = e01.skillMemory.integration.apply({
              preparationId: integratedPreparation.preparationId,
              projection: appliedRestore.projection,
              authorityReceipts,
              committed: true,
              reason: "same_session_codeworker_restore_applied",
              metadata: { turn_id: turn.turn_id, turn_index: turnIndex },
            });
            const browserProjection = e01.skillMemory.integration.exportBrowserContext({
              preparationId: integratedPreparation.preparationId,
              projection: appliedRestore.projection,
              summary: compact.summary,
              allowedTools: browserAllowedTools,
              deniedTools: browserDeniedTools,
              providerId,
              modelId: config.modelName,
              metadata: {
                application_id: integratedApplication.applicationId,
                same_session_handoff: true,
                authority_transfer: false,
                browser_scope_declared: browserAllowedTools.length > 0,
              },
            });
            await emit("skill_memory_restore_fidelity", {
              boundary_id: boundaryId,
              preparation: integratedPreparation as unknown as JsonObject,
              application: integratedApplication as unknown as JsonObject,
              baseline_text_ref_available: true,
              experimental_default: false,
            });
            for (const failureEvent of integratedPreparation.failureEvents) {
              await emit("skill_memory_restore_fidelity_failure", {
                boundary_id: boundaryId,
                failure: failureEvent as unknown as JsonObject,
                text_reference_fallback_available: true,
              });
            }
            await emit("skill_memory_browser_context_exported", {
              boundary_id: boundaryId,
              browser_projection: browserProjection as unknown as JsonObject,
              changes_next_browserworker_context: true,
              executable_skill_body_present: false,
            });
          }
          await emit("skill_memory_compact_restored", {
            turn_id: turn.turn_id,
            turn_index: turnIndex,
            projection: appliedRestore.projection as unknown as JsonObject,
            provider_message_digest: appliedRestore.projection.providerMessageDigest,
            changes_next_provider_context: true,
          });
        } else {
          const deferredBoundaryId = matureCompact?.boundary.boundaryId
            ?? `deferred:${skillMemoryTrigger.triggerId}`;
          const failure = e01.skillMemory.integration.failures.record({
            identity: {
              runId: input.runId,
              taskId: input.taskId,
              sessionId: input.sessionId,
              workerRequestId: input.workerRequestId,
              epoch: e01.journal.restartEpoch,
            },
            boundaryId: deferredBoundaryId,
            stage: "safe_cut",
            code: "compact_safe_cut_unavailable",
            message: safeCut?.reason ?? "no safe cut plan was available",
            retryable: true,
            baselineTextReferenceAvailable: true,
            canonicalCheckpointAvailable: true,
            currentAuthorityRequired: true,
            provenanceComplete: true,
            relatedIds: [skillMemoryTrigger.triggerId, safeCut?.planId ?? ""],
            causationId: skillMemoryTrigger.triggerId,
            details: { existing_compact_preserved: true },
          });
          await emit("skill_memory_compact_restore_deferred", {
            turn_id: turn.turn_id,
            turn_index: turnIndex,
            safe_cut_plan_id: safeCut?.planId ?? null,
            reason: safeCut?.reason ?? "no_safe_cut_plan",
            existing_compact_preserved: true,
            failure: e01.skillMemory.integration.failures.event(failure.failureId),
          });
        }
        await emit("context_compacted", {
          turn_id: turn.turn_id,
          turn_index: turnIndex,
          artifact_id: artifact.artifact_id,
          removed_message_count: compact.removedCount,
          context_chars_after: session.contextChars(),
          compact_owner: "typescript",
          compaction_runtime_applied: matureCompact !== null,
          compact_boundary_id: matureCompact?.boundary.boundaryId ?? null,
          compact_generation: matureCompact?.boundary.compactGeneration ?? session.compactionCount,
          compact_summary: compact.summary,
        });
        await emit("next_turn_restore_contract", {
          turn_id: turn.turn_id,
          turn_index: turnIndex,
          artifact_id: artifact.artifact_id,
          restore_owner: "typescript",
          status: "ready",
        });
        }
      }

      session.completeTurn(turnOk, turnError);
      e01.completeCanonicalTurn(turn.turn_id, turnIndex, turnOk, turnError);
      if (turnOk && modelTransport === "http_sse") {
        // A successful model-directed repair settles an earlier observable
        // tool failure. Scripted continue-on-error retains its historical
        // fail-at-end contract.
        continuedFailureReason = null;
      }
      const sourceContinuation = e01.advanceQueryLoop({
        messagesForQuery: [{ turn_id: turn.turn_id, prompt }],
        assistantMessages: [{ ok: turnOk, error: turnError }],
        toolResults: stepSummaries.slice(-steps.length),
        turnCount: turnIndex,
        maxTurns: turnLimit,
      });
      await emit("upstream_query_continuation", sourceContinuation);
      await emit("turn_completed", {
        turn_id: turn.turn_id,
        turn_index: turnIndex,
        mutation_id: turn.turn_id,
        revision: turnIndex,
        effect_committed: true,
        ok: turnOk,
        error: turnError,
      });
      await emit("turn_end", {
        turn_id: turn.turn_id,
        turn_index: turnIndex,
        ok: turnOk,
        error: turnError,
      });
      if (!turnOk && (turnMustStop || permissionSuspended)) {
        ok = false;
        stoppedReason = turnError || "tool_error";
      }
      if (asBoolean(config.runtimeConstraints.abort_after_turn)) {
        ok = false;
        stoppedReason = "user_cancelled";
      }
      if (
        modelTransport === "http_sse"
        && ok
        && activeIterationRoundId
        && (turnLimit === null || turnIndex + 1 <= turnLimit)
      ) {
        providerMessages = iteration.buildRevisionMessages(activeIterationRoundId);
        if (pendingRestoreProviderMessage) {
          providerMessages = [...providerMessages, pendingRestoreProviderMessage];
          pendingRestoreProviderMessage = null;
        }
        const postToolProgressDecision = progressive.decide(
          session.contextChars(),
          config.maxQueryContextChars,
        );
        if (
          postToolProgressDecision.action === "nudge_action"
        ) {
          const nudgedProgress = progressive.recordActionNudge();
          providerMessages = [
            ...providerMessages,
            {
              role: "user",
              content: [
                "The task still requires a concrete delivery, and enough orientation evidence has been gathered.",
                "Stop broad repository inspection and use the latest gathered evidence to choose and execute the next concrete edit now.",
                "Run the relevant build or tests when they directly drive that edit.",
                "Make another read-only call only when a specific pending edit is blocked by a named missing fact or changed state.",
              ].join(" "),
            },
          ];
          await emit("progressive_action_requested", {
            reason: postToolProgressDecision.reason,
            execution_phase: postToolProgressDecision.snapshot.phase,
            action_nudge: nudgedProgress.actionNudgeCount,
            analysis_only_rounds: postToolProgressDecision.snapshot.analysisOnlyRounds,
            repeated_analysis_rounds: postToolProgressDecision.snapshot.repeatedAnalysisRounds,
            pre_delivery_observations: postToolProgressDecision.snapshot.preDeliveryObservationCount,
            consecutive_pre_delivery_observations:
              postToolProgressDecision.snapshot.consecutivePreDeliveryObservations,
            required_delivery_missing: postToolProgressDecision.snapshot.requiredDeliveryMissing,
          });
        }
        if (
          resourceBudget !== null
          && progressive.decide(session.contextChars(), config.maxQueryContextChars).action === "closeout"
        ) {
          await requestCloseoutFinalResponse(providerMessages, turnIndex);
          continue;
        }
        const finalResponseOnly = turnLimit !== null && turnIndex + 1 === turnLimit;
        if (finalResponseOnly) {
          providerMessages = [
            ...providerMessages,
            {
              role: "user",
              content: [
                "The executable tool-turn budget is now exhausted.",
                "Do not call any tool.",
                "Provide the concise final answer to the original request now.",
              ].join(" "),
            },
          ];
          await emit("provider_finalization_requested", {
            turn_index: turnIndex,
            max_turns: turnLimit,
            tools_advertised: 0,
            executable_tool_budget_expanded: false,
          });
        }
        const nextRound = iteration.beginProviderRound({
          requestKey: `${input.workerRequestId}:provider-round:${providerRoundIndex}`,
          model: config.modelName,
          messages: providerMessages,
        });
        const nextModel = await resolveModelTurns(
          input,
          config,
          [],
          finalResponseOnly ? [] : registry.list(),
          emit,
          (observation) => e01.decideProviderRecovery(observation),
          e01.journal.restartEpoch,
          (observation) => e01.completeProviderRecovery(observation),
          (requestId) => e01.executePreparedProvider(requestId),
          providerRoundIndex,
          providerMessages,
        );
        providerRoundIndex += 1;
        modelMetadata = {
          ...nextModel.metadata,
          model_provider_rounds: String(providerRoundIndex),
        };
        if (!nextModel.ok) {
          iteration.failProviderRound(nextRound.roundId, nextModel.error ?? "model_stream_failed");
          ok = false;
          stoppedReason = "model_stream_failed";
          await emit("error", {
            error: stoppedReason,
            detail: nextModel.error ?? "provider revision failed",
            source: "model_iteration_runtime",
          });
        } else if (finalResponseOnly && nextModel.turns.length > 0) {
          iteration.failProviderRound(nextRound.roundId, "max_turns_exceeded");
          ok = false;
          stoppedReason = "max_turns_exceeded";
          await emit("error", {
            error: stoppedReason,
            detail: [
              "provider attempted a tool call during the response-only",
              "finalization round",
            ].join(" "),
            source: "model_iteration_runtime",
          });
        } else {
          const settled = await settleProviderResolution(
            nextModel,
            nextRound,
            !finalResponseOnly,
            finalResponseOnly ? [] : registry.list(),
          );
          if (!settled.model.ok) {
            iteration.failProviderRound(
              settled.round.roundId,
              settled.model.error ?? "model_stream_failed",
            );
            ok = false;
            stoppedReason = "model_stream_failed";
            await emit("error", {
              error: stoppedReason,
              detail: settled.model.error ?? "provider length continuation failed",
              source: "model_iteration_runtime",
            });
          } else if (settled.truncationExhausted) {
            iteration.fail("model_output_truncated");
            ok = false;
            stoppedReason = "model_output_truncated";
            await emit("error", {
              error: stoppedReason,
              detail: "provider exhausted bounded length-continuation attempts",
              source: "model_iteration_runtime",
            });
          } else if (settled.model.turns.length > 0) {
            turns.push(...settled.model.turns);
            activeIterationRoundId = settled.round.roundId;
          } else {
            activeIterationRoundId = null;
          }
        }
      }
    }

    if (ok && turnLimit !== null && turns.length > turnLimit) {
      ok = false;
      stoppedReason = "max_turns_exceeded";
    }
    if (ok && continuedFailureReason) {
      ok = false;
      stoppedReason = continuedFailureReason;
    }
    const compactRestoreOk = !asBoolean(config.runtimeConstraints.disable_compact_restore_runtime);
    const runtimeBudgetStateOk = !asBoolean(config.runtimeConstraints.disable_runtime_budget_state);
    const modelStreamOk = modelMetadata.model_stream_ok === "true";
    const codeworkerApiFoundationOk = compactRestoreOk && runtimeBudgetStateOk && modelStreamOk;
    const restoreContractId = "compact_restore_" + input.sessionId;
    await emit("compact_restore_report", {
      compact_restore: {
        ok: compactRestoreOk,
        restore_contract_id: restoreContractId,
        compaction_count: session.compactionCount,
        owner: "typescript",
      },
    });
    await emit("codeworker_restore_integration", {
      codeworker_restore_integration: {
        ok: compactRestoreOk,
        restore_contract_id: restoreContractId,
        owner: "typescript",
        application_count: restoreApplicationCount,
        model_message_count: restoreModelMessageCount,
        latest_contract_id: restoreContractId,
        untrusted_attachments_fenced: restoreUntrustedAttachments,
        secret_redacted_attachments: restoreRedactedAttachments,
      },
    });
    await emit("compact_state_projection", {
      compact_state: {
        ok: codeworkerApiFoundationOk,
        restore_contract_id: restoreContractId,
        compaction_count: session.compactionCount,
      },
    });
    await emit("runtime_budget_replay", {
      runtime_budget_replay: {
        ok: runtimeBudgetStateOk && modelStreamOk,
        retry_count: Number(modelMetadata.runtime_budget_state_retry_count || "0"),
      },
    });
    await emit("codeworker_api_foundation", {
      ok: codeworkerApiFoundationOk,
      compact_restore_ok: compactRestoreOk,
      model_stream_ok: modelStreamOk,
      runtime_budget_state_ok: runtimeBudgetStateOk,
    });
    sourceQueryOk = ok;
    sourceQueryStopReason = stoppedReason;
    if (!permissionSuspended) session.finish(ok);
    await emit(permissionSuspended ? "session_suspended" : ok ? "session_completed" : "session_failed", {
      ok,
      stopped_reason: stoppedReason,
      turn_count: turnCount,
      tool_call_count: toolCallCount,
      context_compaction_count: session.compactionCount,
    });
    const snapshot = {
      ...session.snapshot(),
      typescriptControl: controlRuntime.snapshot(),
      e01Runtime: e01.snapshot() as unknown as JsonObject,
      modelIteration: iteration.snapshot() as unknown as JsonObject,
      progressiveExecution: progressive.snapshot() as unknown as JsonObject,
    };
    await emit("query_session_snapshot", {
      snapshot_version: snapshot.version,
      snapshot_checksum: snapshot.checksum,
      snapshot_revision: snapshot.revision,
    });
    const progressiveState = progressive.snapshot();
    sourceResult = {
      ok,
      stoppedReason,
      turnCount,
      toolCallCount,
      contextCompactionCount: session.compactionCount,
      stepSummaries,
      artifacts: uniqueArtifacts(artifacts),
      sessionSnapshot: snapshot,
      metadata: {
        loop: "zyra_typescript_query_engine_runtime",
        canonical_runtime_owner: "typescript",
        runtime_id: "zyra-typescript-claude-runtime",
        query_turns: String(turnCount),
        tool_steps: String(toolCallCount),
        context_compactions: String(session.compactionCount),
        max_read_only_concurrency: String(config.maxReadOnlyConcurrency),
        tool_use_summaries: String(config.emitToolUseSummaries
          ? scheduleToolBatches(registry, turns.flat(), 0, config.maxReadOnlyConcurrency).length
          : 0),
        runtime_protocol: "zyra.claude-runtime.v1",
        sidecar_contracts_used: "false",
        query_contract_source: "zyra-claude-productized",
        query_plan_ok: String(ok),
        query_plan_tool_steps: String(toolCallCount),
        restored: String(session.restored),
        runtime_state_ok: "true",
        runtime_state_mutations: String(snapshot.revision),
        runtime_state_control_mutations: String(config.controlCommands.length),
        control_state_revision: String(controlRuntime.revision),
        control_command_count: String(config.controlCommands.length),
        control_command_failed: String(controlCommandFailed),
        tool_runtime_planned: String(toolCallCount),
        tool_runtime_completed: String(toolCallCount),
        tool_runtime_mutating: String(mutatingToolCount),
        session_lifecycle_resume_plans: String(resumePlanCount),
        session_lifecycle_latest_resume_status: resumePlanCount > 0 ? "ready" : "",
        tool_result_externalizations: String(toolResultExternalizations),
        tool_failure_signals: String(toolFailureSignals),
        tool_schema_errors: String(toolSchemaErrors),
        tool_conflict_protected: String(toolConflictProtected),
        repeated_tool_failure_trips: String(repeatedToolFailureTrips),
        invalid_argument_retry_trips: String(invalidArgumentRetryTrips),
        execution_closeout_requested: String(closeoutRequested),
        progressive_execution_phase: progressiveState.phase,
        progressive_real_actions: String(progressiveState.realActionCount),
        progressive_artifacts: String(progressiveState.artifactCount),
        progressive_required_delivery_missing: String(progressiveState.requiredDeliveryMissing),
        progressive_active_background: String(progressiveState.activeBackgroundCount),
        progressive_last_progress_age_ms: String(
          Math.max(0, Date.now() - progressiveState.lastEffectiveProgressAt),
        ),
        progressive_action_nudges: String(progressiveState.actionNudgeCount),
        compact_restore_ok: String(compactRestoreOk),
        runtime_budget_state_ok: String(runtimeBudgetStateOk),
        codeworker_api_foundation_ok: String(codeworkerApiFoundationOk),
        compact_state_projection_ok: String(codeworkerApiFoundationOk),
        compact_restore_contract_id: restoreContractId,
        restore_untrusted_attachments: String(restoreUntrustedAttachments),
        restore_redacted_attachments: String(restoreRedactedAttachments),
        restore_applications: String(restoreApplicationCount),
        restore_model_messages: String(restoreModelMessageCount),
        tool_runtime_gate_failures: disabledComponents.length > 0
          ? disabledComponents.join(",")
          : "",
        ...modelMetadata,
      },
    };
    } catch (error) {
      sourceQueryOk = false;
      sourceQueryStopReason = error instanceof Error ? error.message : String(error);
      throw error;
    } finally {
      if (sourceQueryStopReason !== "permission_suspended") {
        sourceQuery?.finishCanonicalQuery(sourceQueryOk, sourceQueryStopReason);
      }
      if (sourceQuery && sourceResult) {
        sourceResult.sessionSnapshot.e01Runtime = sourceQuery.snapshot() as unknown as JsonObject;
      }
    }
    if (!sourceResult) throw new Error("query source lifecycle settled without a result");
    return sourceResult;
  }
}

function mutationTarget(step: { tool_name: string; arguments: JsonObject }): string {
  const path = asString(step.arguments.path || step.arguments.file_path).trim();
  if (path) {
    return "workspace_path:" + path.replaceAll("\\", "/").toLowerCase();
  }
  return step.tool_name + ":" + JSON.stringify(step.arguments);
}

export function isClearlyPreDeliveryInspection(
  step: { tool_name: string; arguments: JsonObject },
  readOnly: boolean,
): boolean {
  if (step.tool_name === "shell_wait") return false;
  if (readOnly) return true;
  if (step.tool_name !== "shell") return false;
  return isClearlyReadOnlyShellCommand(asString(step.arguments.command));
}

function isClearlyReadOnlyShellCommand(value: string): boolean {
  let command = value.trim();
  if (!command) return false;
  command = command
    .replace(/\b(?:\d?>|&>)\s*(?:\/dev\/null|nul)\b/gi, "")
    .replace(/\s+/g, " ")
    .trim();
  // Any remaining output redirection or an interpreter can create arbitrary
  // state, so let the governed shell and its normal permission policy decide.
  if (/(?:^|\s)(?:>>|>|<<)(?!\s*(?:\/dev\/null|nul)\b)/i.test(command)) return false;
  if (/\b(?:python(?:3)?|node|bun|deno|ruby|perl|pwsh|powershell|cmd(?:\.exe)?)\b/i.test(command)) return false;
  if (/\b(?:apply_patch|patch|tee|touch|mkdir|rmdir|rm|mv|cp|install|chmod|chown)\b/i.test(command)) return false;
  if (/\b(?:pytest|unittest|npm|npx|pnpm|yarn|cargo|go|gradle|mvn|make|cmake)\b/i.test(command)) return false;
  if (/\bgit\s+(?:add|commit|checkout|switch|restore|reset|merge|rebase|apply|am|clean|push|pull|fetch)\b/i.test(command)) return false;
  if (/\bdocker(?:\.exe)?\s+(?:run|exec|start|stop|restart|kill|rm|rmi|build|pull|push)\b/i.test(command)) return false;
  if (/\bdocker(?:\.exe)?\s+compose\b[^;&|]*(?:\bup\b|\bdown\b|\bbuild\b|\brun\b|\bexec\b|\bstart\b|\bstop\b|\brestart\b|\bpull\b|\bkill\b|\brm\b)/i.test(command)) return false;
  if (/\b(?:curl|wget|invoke-webrequest|invoke-restmethod)\b/i.test(command)) return false;

  const segments = command
    .split(/(?:&&|\|\||[;|\n])/)
    .map((part) => part.trim())
    .filter(Boolean);
  if (segments.length === 0) return false;
  return segments.every((segment) => {
    const normalized = segment
      .replace(/^(?:[A-Za-z_][A-Za-z0-9_]*=[^\s]+\s+)*/, "")
      .replace(/^sudo\s+/, "")
      .trim();
    return /^(?:cd|pwd|ls|dir|cat|head|tail|less|more|grep|egrep|fgrep|rg|find|fd|stat|file|wc|which|where|type|echo|printf|sort|uniq|cut|awk|jq|xargs\s+(?:cat|head|tail|grep|egrep|fgrep|rg)|test|true|false|get-content|get-childitem|select-string|resolve-path|test-path)\b/i.test(normalized)
      || /^git\s+(?:status|diff|log|show|branch|rev-parse|ls-files|grep)\b/i.test(normalized)
      || /^docker(?:\.exe)?\s+(?:ps|inspect|logs|images|info|version|stats|top)\b/i.test(normalized)
      || /^docker(?:\.exe)?\s+compose\b[^;&|]*\b(?:ps|config|logs|images|top)\b/i.test(normalized);
  });
}

function providerOutputWasLengthTruncated(model: ModelStreamResolution): boolean {
  if (model.turns.length > 0) return false;
  const normalized = model.stopReason.trim().toLowerCase().replaceAll("-", "_");
  return normalized === "length"
    || normalized === "max_tokens"
    || normalized === "maximum_tokens";
}

function modelCanRecoverToolFailure(result: ToolExecutionResponse): boolean {
  const error = result.error || "tool_error";
  const termination = asString(result.metadata.termination).toLowerCase();
  const settledCommandTimeout = error === "process_timeout"
    && termination === "timed_out"
    && asString(result.metadata.command_timeout_settled).toLowerCase() === "true"
    && asString(result.metadata.model_recovery_allowed).toLowerCase() === "true"
    && asString(result.metadata.process_tree_controlled).toLowerCase() === "true";
  if (settledCommandTimeout) return true;
  if (
    error === "permission_approval_required"
    || error === "missing_tool_result"
    || error === "tool_effect_identity_conflict"
    || error === "tool_execution_timeout"
    || error === "repeated_tool_failure"
    || error === "invalid_tool_arguments_retry_exhausted"
  ) {
    return false;
  }
  if (asString(result.metadata.permission_abort_loop).toLowerCase() === "true") return false;
  if (asString(result.metadata.receipt_validation_failed).toLowerCase() === "true") return false;
  if (asString(result.metadata.recovery_required).toLowerCase() === "true") return false;
  if (asString(result.metadata.late_result_fenced).toLowerCase() === "true") return false;
  return !termination || termination === "exited";
}

function normalizeConfig(value: Partial<RuntimeConfig>): RuntimeConfig {
  return {
    maxTurns: typeof value.maxTurns === "number" && value.maxTurns > 0
      ? Math.floor(value.maxTurns)
      : null,
    maxToolResultChars: positiveInteger(
      value.maxToolResultChars,
      DEFAULT_CONFIG.maxToolResultChars,
    ),
    maxTurnToolResultChars: typeof value.maxTurnToolResultChars === "number"
      && value.maxTurnToolResultChars > 0
      ? Math.floor(value.maxTurnToolResultChars)
      : null,
    maxQueryContextChars: positiveInteger(
      value.maxQueryContextChars,
      DEFAULT_CONFIG.maxQueryContextChars,
    ),
    continueOnError: value.continueOnError ?? DEFAULT_CONFIG.continueOnError,
    maxReadOnlyConcurrency: positiveInteger(
      value.maxReadOnlyConcurrency,
      DEFAULT_CONFIG.maxReadOnlyConcurrency,
    ),
    emitToolUseSummaries: value.emitToolUseSummaries ?? DEFAULT_CONFIG.emitToolUseSummaries,
    allowEmptyTurns: value.allowEmptyTurns ?? DEFAULT_CONFIG.allowEmptyTurns,
    modelName: asString(value.modelName, DEFAULT_CONFIG.modelName),
    runtimeConstraints: asObject(value.runtimeConstraints),
    permissionPolicy: asObject(value.permissionPolicy),
    controlCommands: Array.isArray(value.controlCommands)
      ? value.controlCommands
      : [],
  };
}

function runtimeStringArray(value: unknown): string[] {
  const values = Array.isArray(value) ? value : typeof value === "string" ? [value] : [];
  return [...new Set(values.map((item) => String(item).trim()).filter(Boolean))].sort();
}

function restoreProviderKind(value: string, providerId: string): RestoreProviderKind {
  const normalized = value.trim().toLowerCase();
  if (["anthropic", "compatible", "local", "browser"].includes(normalized)) {
    return normalized as RestoreProviderKind;
  }
  if (/anthropic|claude/.test(providerId.toLowerCase())) return "anthropic";
  if (/browser/.test(providerId.toLowerCase())) return "browser";
  if (/local|scripted/.test(providerId.toLowerCase())) return "local";
  return "compatible";
}

function restoreCapabilities(
  constraints: JsonObject,
  providerKind: RestoreProviderKind,
): string[] {
  const declared = runtimeStringArray(constraints.provider_capabilities);
  const baseline = ["text", "artifact_refs", "evidence_refs"];
  if (providerKind === "anthropic" || providerKind === "browser") baseline.push("vision");
  return [...new Set([...declared, ...baseline])].sort();
}

function currentAuthorityFromMemory(metadata: SkillMemoryJsonObject): CurrentSkillAuthority | null {
  const value = metadata.current_skill_authority;
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const object = value as SkillMemoryJsonObject;
  if (object.protocol !== "zyra.skill-coordinator-authority/v1") return null;
  return object as unknown as CurrentSkillAuthority;
}

function mergeRestoreProviderMessages(
  baseline: JsonObject,
  integrated: SkillMemoryJsonObject | null,
): JsonObject {
  if (!integrated) return baseline;
  const baselineContent = Array.isArray(baseline.content) ? baseline.content : [];
  const integratedContent = Array.isArray(integrated.content) ? integrated.content : [];
  return {
    role: "user",
    content: [...baselineContent, ...integratedContent],
    metadata: {
      ...asObject(baseline.metadata),
      ...asObject(integrated.metadata),
      restore_sources: ["02B/02D", "06C-02"],
      current_skill_authority_required: true,
      historical_skill_body_executable: false,
    },
  };
}

function selectRestoredSnapshot(value: JsonObject | null | undefined): JsonObject | null {
  const root = asObject(value);
  if (root.version === "zyra.typescript-query-session.v1") {
    return sessionChecksumPayload(root);
  }
  const direct = asObject(root.typescript_runtime);
  if (direct.version === "zyra.typescript-query-session.v1") {
    return sessionChecksumPayload(direct);
  }
  const queryEngine = asObject(root.query_engine);
  if (queryEngine.version === "zyra.typescript-query-session.v1") {
    return sessionChecksumPayload(queryEngine);
  }
  const metadata = asObject(root.metadata);
  const pythonProjection = asObject(root.typescript_runtime_snapshot);
  if (pythonProjection.version === "zyra.typescript-query-session.v1") {
    return sessionChecksumPayload(pythonProjection);
  }
  const projected = asObject(metadata.typescript_runtime_snapshot);
  return projected.version === "zyra.typescript-query-session.v1"
    ? sessionChecksumPayload(projected)
    : null;
}

function sessionChecksumPayload(value: JsonObject): JsonObject {
  const selected: JsonObject = {};
  for (const key of [
    "version",
    "runtime_id",
    "canonical_owner",
    "session_id",
    "run_id",
    "task_id",
    "worker_request_id",
    "phase",
    "revision",
    "turn_count",
    "tool_call_count",
    "compaction_count",
    "messages",
    "context",
    "turns",
    "lineage",
    "checksum",
  ]) {
    if (key in value) selected[key] = value[key] ?? null;
  }
  return selected;
}

function publicRuntimeEventPayload(phase: string, payload: JsonObject): JsonObject {
  if (phase !== "model_request_prepared") return payload;
  const request = asObject(payload.provider_request);
  if (Object.keys(request).length === 0) return payload;
  const commitment = { ...request };
  delete commitment.messages;
  delete commitment.tools;
  delete commitment.system;
  return {
    ...payload,
    provider_request: {
      ...commitment,
      prompt_content_persisted: false,
    },
  };
}

function selectRestoredModelIterationSnapshot(
  value: JsonObject | null | undefined,
): ModelIterationSnapshot | null {
  const root = asObject(value);
  const candidates = [
    root,
    asObject(root.typescript_runtime),
    asObject(root.typescript_runtime_snapshot),
    asObject(root.query_engine),
    asObject(asObject(root.metadata).typescript_runtime_snapshot),
  ];
  for (const candidate of candidates) {
    const snapshot = asObject(candidate.modelIteration);
    if (snapshot.version === "zyra.model-iteration/v1") {
      return snapshot as unknown as ModelIterationSnapshot;
    }
  }
  return null;
}

function selectRestoredProgressiveExecutionSnapshot(
  value: JsonObject | null | undefined,
): ProgressiveExecutionSnapshot | null {
  const root = asObject(value);
  const candidates = [
    root,
    asObject(root.typescript_runtime),
    asObject(root.typescript_runtime_snapshot),
    asObject(root.query_engine),
    asObject(asObject(root.metadata).typescript_runtime_snapshot),
  ];
  for (const candidate of candidates) {
    const snapshot = asObject(candidate.progressiveExecution);
    if (snapshot.version === PROGRESSIVE_EXECUTION_SNAPSHOT_VERSION) {
      return snapshot as unknown as ProgressiveExecutionSnapshot;
    }
  }
  return null;
}

function selectRestoredE01Snapshot(
  value: JsonObject | null | undefined,
): import("./e01/coordinator.ts").E01CoordinatorSnapshot | import("./e01/kernel.ts").JournalSnapshot | null {
  const root = asObject(value);
  const candidates = [
    root,
    asObject(root.typescript_runtime),
    asObject(root.typescript_runtime_snapshot),
    asObject(root.query_engine),
    asObject(asObject(root.metadata).typescript_runtime_snapshot),
  ];
  for (const candidate of candidates) {
    const snapshot = asObject(candidate.e01Runtime);
    if (snapshot.version === "zyra.e01-runtime/v6" || snapshot.version === "zyra.e01-runtime/v5") {
      return snapshot as unknown as import("./e01/coordinator.ts").E01CoordinatorSnapshot;
    }
    if (snapshot.version === "zyra.e01-journal/v3") {
      return snapshot as unknown as import("./e01/kernel.ts").JournalSnapshot;
    }
  }
  return null;
}
