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
import { createHash } from "node:crypto";
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
  // Keep enough of modern coding-model context to preserve an actual work
  // phase.  32k characters is only about 8k tokens and caused long-running
  // sessions to compact every few tool calls.
  maxQueryContextChars: 400_000,
  continueOnError: false,
  maxReadOnlyConcurrency: 10,
  emitToolUseSummaries: true,
  allowEmptyTurns: false,
  modelName: "zyra-local-code-model",
  runtimeConstraints: {},
  permissionPolicy: {},
  controlCommands: [],
};

const CONTRACT_PARITY_REPAIR_GUIDANCE =
  "For contract, security, or cross-language failures, map each public contract clause to every enforcement path and compare actual predicates clause-by-clause; verify every qualifier (such as tenant, operation kind, approval, role, and state) is enforced, because matching comments or constants do not prove semantic parity.";

// Runtime events remain in the event/journal evidence, but only semantic
// recovery boundaries warrant serializing the complete durable session. A
// long-running session can grow to tens of megabytes; checkpointing every
// observational event makes persistence dominate the provider and tool work.
const DURABLE_CHECKPOINT_PHASES = new Set([
  "session_started",
  "context_restored",
  "control_command",
  "model_request_prepared",
  "model_stream_report",
  "tool_batch_completed",
  "tool_batch_suspended",
  "turn_end",
  "turn_suspended",
  "context_compacted",
  "next_turn_restore_contract",
  "execution_closeout_completed",
  "error",
  "session_suspended",
  "session_completed",
  "session_failed",
  "query_session_snapshot",
]);

export function shouldCheckpointRuntimePhase(phase: string): boolean {
  return DURABLE_CHECKPOINT_PHASES.has(phase);
}
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

function compactMessagesForProvider(messages: readonly CompactMessage[]): JsonObject[] {
  return messages.map((message) => ({
    role: message.role,
    content: message.content.map((block): JsonObject => {
      if (block.type === "text") return { type: "text", text: block.text };
      if (block.type === "tool_use") {
        return { type: "tool_use", id: block.id, name: block.name, input: block.input };
      }
      if (block.type === "tool_result") {
        return {
          type: "tool_result",
          tool_use_id: block.toolUseId,
          content: block.content as never,
          is_error: block.isError,
        };
      }
      if (block.type === "thinking") return { type: "text", text: block.thinking };
      if (block.type === "attachment") {
        return { type: "text", text: `${block.name}: ${block.content}` };
      }
      if (block.type === "image") {
        return { type: "text", text: `[Compacted image ${block.mediaType}]` };
      }
      return { type: "text", text: `[Compacted document ${block.mediaType}]` };
    }),
    metadata: {
      ...message.metadata,
      compact_message_id: message.id,
      compact_api_round: message.apiRound,
      synthetic: message.synthetic,
    },
  }));
}

function blockSummaryText(block: CompactContentBlock): string {
  if (block.type === "text") return block.text;
  if (block.type === "tool_use") {
    return `requested ${block.name} with ${boundedCompactionText(JSON.stringify(block.input), 1_200)}`;
  }
  if (block.type === "tool_result") {
    return `${block.isError ? "failed" : "completed"} tool ${block.toolUseId}: ${
      typeof block.content === "string" ? block.content : JSON.stringify(block.content)
    }`;
  }
  if (block.type === "thinking") return block.thinking;
  if (block.type === "attachment") return `${block.name}: ${block.content}`;
  return "";
}

function compactSummarySections(value: string): Map<string, string> {
  const sections = new Map<string, string>();
  let heading = "";
  let body: string[] = [];
  const flush = (): void => {
    if (!heading) return;
    sections.set(heading.toLowerCase(), body.join("\n").trim());
  };
  for (const line of value.split(/\r?\n/u)) {
    const match = line.match(/^\s*(?:#{1,4}\s+|\d+\.\s+)([^:\n]+?):?\s*$/u);
    if (match) {
      flush();
      heading = match[1].trim();
      body = [];
      continue;
    }
    if (heading) body.push(line);
  }
  flush();
  return sections;
}

function compactSummarySection(value: string, names: readonly string[]): string {
  const sections = compactSummarySections(value);
  for (const name of names) {
    const exact = sections.get(name.toLowerCase());
    if (exact) return exact;
    const fuzzy = [...sections.entries()].find(([heading]) => heading.includes(name.toLowerCase()));
    if (fuzzy?.[1]) return fuzzy[1];
  }
  return "";
}

export function modelCompactionPrompt(request: SummaryRequest): string {
  return [
    "Create a replacement handoff summary for the autonomous coding task represented by the conversation above.",
    "Respond with text only and do not call tools. The summary replaces older compact summaries: do not quote, recursively embed, or merely append the previous summary.",
    "Preserve concrete continuation state, especially:",
    "- the current objective and binding user constraints;",
    "- decisions already made and why;",
    "- files created, changed, or inspected and the relevant findings;",
    "- for inspected source files that still affect the work, retain concrete symbols, responsibilities, defects, and cross-file relationships—not merely that the file was read;",
    "- commands/tests run and their exact outcomes;",
    "- failures, diagnoses, and fixes already attempted;",
    "- the work in progress, pending requirements, and the immediate next action.",
    "Distinguish verified facts from tentative conclusions. A successful source read must not be summarized as unavailable or unknown. Omit obsolete exploration and repeated listings. Never invent completion.",
    "Use these headings: Active objective; Completed work and decisions; Files and tool effects; Verification and failures; Current work; Pending work and next action.",
    request.customInstructions.trim() ? `Additional instruction: ${request.customInstructions.trim()}` : "",
  ].filter(Boolean).join("\n");
}

function formatModelCompactionSummary(value: string, tokenBudget: number): string {
  let formatted = value.trim();
  const summary = formatted.match(/<summary>\s*([\s\S]*?)\s*<\/summary>/iu);
  if (summary?.[1]) formatted = summary[1].trim();
  formatted = formatted.replace(/<analysis>[\s\S]*?<\/analysis>/giu, "").trim();
  return boundedCompactionText(formatted, Math.max(4_000, Math.min(16_000, tokenBudget * 4)));
}

export async function durableCompactionSummary(request: SummaryRequest): Promise<string> {
  const objective = request.messages
    .filter((message) =>
      message.role === "user"
      && !message.synthetic
      && message.metadata.compact_summary !== true
    )
    .map((message) => message.content.filter((block) => block.type === "text").map(blockSummaryText).join("\n"))
    .find((value) => value.trim().length > 0)
    || compactSummarySection(request.previousSummary, ["Active objective", "Primary Request and Intent"])
    || "Continue the current governed task.";
  const assistantNotes = request.messages
    .filter((message) => message.role === "assistant")
    .map((message) => message.content
      .filter((block) => block.type === "text" || block.type === "thinking")
      .map(blockSummaryText)
      .join("\n"))
    .filter((value) => value.trim().length > 0)
    .slice(-5)
    .map((value) => boundedCompactionText(value, 1_600));
  const toolActions = request.messages
    .flatMap((message) => message.content.filter((block) => block.type === "tool_use"))
    .slice(-16)
    .map((block) => boundedCompactionText(blockSummaryText(block), 1_200));
  const toolOutcomes = request.messages
    .flatMap((message) => message.content.filter((block) => block.type === "tool_result"))
    .slice(-12)
    .map((block) => boundedCompactionText(blockSummaryText(block), 1_200));
  const previousProgress = boundedCompactionText(compactSummarySection(
    request.previousSummary,
    ["Current work", "Completed work and decisions", "Problem Solving", "Historical reasoning and durable progress"],
  ), 1_800);
  const previousVerification = boundedCompactionText(compactSummarySection(
    request.previousSummary,
    ["Verified tool observations", "Verification and failures"],
  ), 2_400);
  const previousOpenWork = boundedCompactionText(compactSummarySection(
    request.previousSummary,
    ["Pending work and next action", "Pending Tasks", "Open work", "Optional Next Step"],
  ), 1_500);
  const sections = [
    "## Active objective",
    boundedCompactionText(objective, 2_500),
    "",
    "## Historical reasoning and durable progress",
    "Reasoning snippets may include superseded plans or inspections completed by later tool outcomes; they are not an implicit to-do list.",
    ...(previousProgress ? [`- Previous handoff progress: ${previousProgress}`] : []),
    ...(assistantNotes.length > 0 ? assistantNotes.map((item) => `- ${item}`) : ["- No separate assistant note was retained."]),
    "",
    "## Files and tool actions",
    ...(toolActions.length > 0 ? toolActions.map((item) => `- ${item}`) : ["- No concrete tool action was retained."]),
    "",
    "## Verified tool observations",
    ...(previousVerification ? [`- Previous verified observations: ${previousVerification}`] : []),
    ...(toolOutcomes.length > 0 ? toolOutcomes.map((item) => `- ${item}`) : ["- No completed tool observation was retained."]),
    "",
    "## Open work",
    ...(previousOpenWork ? [`- ${previousOpenWork}`] : []),
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
    let pendingProviderCompaction: {
      boundaryId: string;
      messages: JsonObject[];
    } | null = null;
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
      continuityProgress: asObject(asObject(input.metadata).task_handoff_progress),
      repairContextId: input.sessionId,
    });
    const restoredVerificationDebt = progressive.verificationDebtSummary();
    if (restoredVerificationDebt) {
      providerMessages = [
        ...providerMessages,
        {
          role: "user",
          content: [
            `Authoritative recovery verification state: ${restoredVerificationDebt}.`,
            "Use the priority failure from the first action of this resumed context.",
            "Do not return to an older opaque scope or broad contract audit until the fresh concrete regression has been diagnosed, repaired, and rerun.",
          ].join(" "),
        },
      ];
    }

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
      e01.recordRuntimeEvent(
        phase,
        providerControlPlaneRequired
          ? e01RuntimeEventPayload(phase, payload)
          : payload,
      );
      await host.emitEvent({ ...event, e01_revision: e01.journal.revision });
      if (shouldCheckpointRuntimePhase(phase)) {
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
                "If an opaque validation has failed repeatedly and no permitted diagnostic gives more detail, stop probing private or inaccessible feedback; instead map every public requirement and acceptance condition to its boundary cases, persistent schema, and cross-language consumers, audit that checklist, and edit the uncovered gaps.",
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
        if (
          progressDecision.action === "nudge_verification"
          && model.turns.flat().length === 0
          && !providerOutputWasLengthTruncated(model)
          && continuationTools.length > 0
          && (providerRoundLimit === null || providerRoundIndex < providerRoundLimit)
        ) {
          const nudgedProgress = progressive.recordVerificationNudge();
          iteration.rejectProviderRoundForRetry(round.roundId, "progressive_verification_required");
          providerMessages = [
            ...iteration.currentMessages(),
            ...(model.finalText.trim()
              ? [{ role: "assistant", content: model.finalText }]
              : []),
            {
              role: "user",
              content: [
                "The workspace changed, but there is no successful behavioral verification for the latest delivered state.",
                `Outstanding verification debt: ${progressDecision.reason}.`,
                "Before finalizing, run a proportionate real verification command such as the relevant tests, build or typecheck, smoke or end-to-end scenario, or the task-provided simulation or acceptance command.",
                "When the debt names an earlier failed semantic scope, rerun that same scope; passing unrelated suites cannot settle it.",
                "File existence, JSON parsing, hashes, git status, and report text are not behavioral verification.",
                "While behavioral verification remains failed, do not regenerate submission/evidence/reports or edit their generators as a substitute for repairing the public-contract or business implementation.",
                "If verification fails, fix the cause and rerun it; if it cannot run, gather the concrete failure evidence and report that honestly.",
              ].join(" "),
            },
          ];
          await emit("progressive_verification_requested", {
            reason: progressDecision.reason,
            execution_phase: progress.phase,
            verification_nudge: nudgedProgress.verificationNudgeCount,
            verification_count: nudgedProgress.verificationCount,
            workspace_mutations: nudgedProgress.workspaceMutationCount,
            artifacts: nudgedProgress.artifactCount,
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
        const availableBackgroundShellSlots = progressive.backgroundShellSlotsRemaining();
        let admittedShellStarts = 0;
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
            step.tool_name === "shell"
            && admittedShellStarts >= availableBackgroundShellSlots
          ) {
            immediateResults.set(toolCallId, {
              tool_call_id: toolCallId,
              ok: false,
              summary: "Active background shell commands must be reconciled before another shell command can start.",
              output: {
                guidance: [
                  "Use shell_wait with a previously returned job_id until a job reaches a terminal state.",
                  "Inspect that terminal result before choosing the next command.",
                  "Do not start replacement diagnostics while earlier commands are still running.",
                ],
                active_background_count: progressive.snapshot().activeBackgroundCount,
                side_effect_executed: false,
              },
              artifacts: [],
              error: "active_background_shell_limit_reached",
              metadata: {
                canonical_owner: "typescript",
                background_reconciliation_required: "true",
                physical_effect_executed: "false",
                model_recovery_allowed: "true",
                termination: "exited",
              },
            });
          } else if (
            progressive.hasUnresolvedVerificationFailures()
            && isGeneratedDeliveryInspection(step, registry.readOnly(step.tool_name))
          ) {
            immediateResults.set(toolCallId, {
              tool_call_id: toolCallId,
              ok: false,
              summary: "Behavioral verification is still failing; stale delivery evidence was not inspected again.",
              output: {
                guidance: [
                  "Use the public contract and concrete business implementation files to diagnose the outstanding failure.",
                  "Do not spend the bounded repair window rereading submission, evidence, manifests, simulations, or their generators.",
                  "Return to delivery evidence only after the failed semantic verification scope passes.",
                ],
                side_effect_executed: false,
              },
              artifacts: [],
              error: "unresolved_verification_evidence_inspection_blocked",
              metadata: {
                canonical_owner: "typescript",
                unresolved_verification_evidence_inspection_blocked: "true",
                physical_effect_executed: "false",
                model_recovery_allowed: "true",
                termination: "exited",
              },
            });
          } else if (
            progressive.hasUnresolvedVerificationFailures()
            && isGeneratedDeliveryMutation(step)
          ) {
            immediateResults.set(toolCallId, {
              tool_call_id: toolCallId,
              ok: false,
              summary: "Behavioral verification is still failing; generated delivery evidence was not rewritten.",
              output: {
                guidance: [
                  "Repair the public-contract or business implementation that can cause the outstanding semantic verification failure.",
                  "Rerun the same failed verification scope after the repair; an unrelated green suite cannot settle this debt.",
                  "Regenerate submission, evidence, manifests, and reports only after the behavioral failure is resolved.",
                ],
                side_effect_executed: false,
              },
              artifacts: [],
              error: "unresolved_verification_evidence_write_blocked",
              metadata: {
                canonical_owner: "typescript",
                unresolved_verification_evidence_write_blocked: "true",
                physical_effect_executed: "false",
                model_recovery_allowed: "true",
                termination: "exited",
              },
            });
          } else if (
            progressive.hasUnresolvedVerificationFailures()
            && isValidationOnlyMutation(step)
          ) {
            immediateResults.set(toolCallId, {
              tool_call_id: toolCallId,
              ok: false,
              summary: "Behavioral verification is still failing; a validation-only edit was not accepted as the required business repair.",
              output: {
                guidance: [
                  "Repair the public-contract, configuration, or business implementation that can change the failing behavior.",
                  "Do not add or rewrite tests, snapshots, or documentation merely to reopen the diagnostic circuit.",
                  "After the implementation repair, rerun the same failed verification scope; add regression tests once the behavior is fixed.",
                ],
                side_effect_executed: false,
              },
              artifacts: [],
              error: "unresolved_verification_validation_only_write_blocked",
              metadata: {
                canonical_owner: "typescript",
                unresolved_verification_validation_only_write_blocked: "true",
                physical_effect_executed: "false",
                model_recovery_allowed: "true",
                termination: "exited",
              },
            });
          } else if (
            progressive.verificationEnvironmentRecoveryAwaitingVerification()
            && isClearlyPreDeliveryInspection(step, registry.readOnly(step.tool_name))
          ) {
            immediateResults.set(toolCallId, {
              tool_call_id: toolCallId,
              ok: false,
              summary: "The task environment recovered successfully; further inspection was deferred until the priority verification is rerun.",
              output: {
                guidance: [
                  "Rerun the same priority verification scope now so the repaired environment produces current behavioral evidence.",
                  "Do not inspect more source, status, logs, or generated reports before that rerun.",
                  "If the rerun fails, use its new diagnostic to choose the next bounded repair.",
                ],
                priority_verification_failure: progressive.verificationDebtSummary(),
                side_effect_executed: false,
              },
              artifacts: [],
              error: "verification_required_after_environment_recovery",
              metadata: {
                canonical_owner: "typescript",
                pre_delivery_inspection_blocked: "true",
                verification_required_after_environment_recovery: "true",
                physical_effect_executed: "false",
                model_recovery_allowed: "true",
                termination: "exited",
              },
            });
          } else if (
            progressive.verificationEnvironmentRecoveryRequired()
            && registry.readOnly(step.tool_name)
            && ["read", "file_read"].includes(step.tool_name)
          ) {
            immediateResults.set(toolCallId, {
              tool_call_id: toolCallId,
              ok: false,
              summary: "The priority verification failure is an unresolved connectivity or service-availability fault; source inspection was deferred.",
              output: {
                guidance: [
                  "Inspect the relevant service state or execute the task-provided bootstrap/start/restart command now.",
                  "Do not audit unrelated source while the priority endpoint is unreachable.",
                  "After services report healthy, rerun the same priority verification scope before returning to older failures.",
                ],
                priority_verification_failure: progressive.verificationDebtSummary(),
                side_effect_executed: false,
              },
              artifacts: [],
              error: "verification_environment_recovery_required",
              metadata: {
                canonical_owner: "typescript",
                pre_delivery_inspection_blocked: "true",
                verification_environment_recovery_required: "true",
                physical_effect_executed: "false",
                model_recovery_allowed: "true",
                termination: "exited",
              },
            });
          } else if (
            progressive.failedVerificationScopeAwaitingRepair(
              verificationScopeForTool(step),
            )
          ) {
            immediateResults.set(toolCallId, {
              tool_call_id: toolCallId,
              ok: false,
              summary: "This verification scope already failed on the current business implementation; it was not rerun without a repair.",
              output: {
                guidance: [
                  "Make a targeted source or configuration repair before rerunning this same failed scope.",
                  CONTRACT_PARITY_REPAIR_GUIDANCE,
                  "Build and service lifecycle commands remain available when needed to deploy that repair.",
                  "A repeated run on unchanged implementation bytes cannot provide new evidence or reopen diagnostics.",
                ],
                side_effect_executed: false,
              },
              artifacts: [],
              error: "repeated_failed_verification_without_repair",
              metadata: {
                canonical_owner: "typescript",
                repeated_failed_verification_blocked: "true",
                physical_effect_executed: "false",
                model_recovery_allowed: "true",
                termination: "exited",
              },
            });
          } else if (
            progressive.hasUnresolvedVerificationFailures()
            && isAlternativeVerificationInspection(
              step,
              progressive.snapshot().unresolvedVerificationScopes,
            )
            && !progressive.consumeRecoveryInspectionAllowance()
          ) {
            immediateResults.set(toolCallId, {
              tool_call_id: toolCallId,
              ok: false,
              summary: "The outstanding semantic verification still fails; the bounded alternate-verification window is exhausted.",
              output: {
                guidance: [
                  "Use the source and contract evidence already gathered to make the next business-implementation repair.",
                  CONTRACT_PARITY_REPAIR_GUIDANCE,
                  "Do not replace the named failing suite with more self-authored smoke, E2E, simulation, or unrelated green checks.",
                  "Build and service lifecycle commands remain available; after the repair, rerun the original failing verification scope.",
                ],
                unresolved_verification_scopes: progressive.snapshot().unresolvedVerificationScopes,
                side_effect_executed: false,
              },
              artifacts: [],
              error: "alternate_verification_budget_exhausted",
              metadata: {
                canonical_owner: "typescript",
                alternate_verification_blocked: "true",
                physical_effect_executed: "false",
                model_recovery_allowed: "true",
                termination: "exited",
              },
            });
          } else if (
            progressive.inspectionCircuitOpen()
            && isClearlyPreDeliveryInspection(step, registry.readOnly(step.tool_name))
            && !progressive.consumeRecoveryInspectionAllowance(
              isTargetedRepairInspection(step, registry.readOnly(step.tool_name)),
            )
          ) {
            immediateResults.set(toolCallId, {
              tool_call_id: toolCallId,
              ok: false,
              summary: "Pre-delivery inspection circuit is open; this action did not demonstrate delivery and was not executed.",
              output: {
                guidance: preDeliveryInspectionGuidance(
                  progressive.snapshot().targetedRepairInspectionAllowance,
                ),
                targeted_repair_inspections_remaining:
                  progressive.snapshot().targetedRepairInspectionAllowance,
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
            if (step.tool_name === "shell") admittedShellStarts += 1;
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
                progressive_delivery_driving_shell: step.tool_name === "shell"
                  && !isClearlyPreDeliveryInspection(step, false),
                progressive_verification_driving:
                  isClearlyVerificationDrivingTool(step),
                progressive_verification_scope:
                  verificationScopeForTool(step),
                progressive_repair_driving:
                  isClearlyRepairDrivingTool(step),
                progressive_environment_recovery_driving:
                  isClearlyEnvironmentRecoveryTool(step),
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
            const verificationDriving = isVerificationDrivingToolResult(
              step,
              result,
              step.tool_name === "shell_wait"
                ? e01.snapshot().query.toolCalls
                : [],
            );
            const verificationScope = verificationScopeForToolResult(
              step,
              result,
              step.tool_name === "shell_wait"
                ? e01.snapshot().query.toolCalls
                : [],
            );
            const environmentRecoveryDriving = isEnvironmentRecoveryToolResult(
              step,
              result,
              step.tool_name === "shell_wait"
                ? e01.snapshot().query.toolCalls
                : [],
            );
            progressive.observeToolResult(
              {
                ...observedRequest,
                metadata: {
                  ...observedRequest.metadata,
                  ...(verificationDriving
                    ? {
                        progressive_verification_driving: true,
                        progressive_verification_scope: verificationScope,
                      }
                    : {}),
                  ...(environmentRecoveryDriving
                    ? { progressive_environment_recovery_driving: true }
                    : {}),
                },
              },
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
      const providerInputTokens = Math.max(
        0,
        Math.floor(Number(modelMetadata.provider_input_tokens) || 0),
      );
      const measuredContextChars = Math.max(
        session.contextChars(),
        providerInputTokens * 4,
      );
      const autoCompactCharacterThreshold = e01.compact.getAutoCompactThreshold(
        configuredContextWindow,
        8_192,
      ) * 4;
      const contextDecision = e01.decideContext(
        measuredContextChars,
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
        const compactSummaryProvider = modelTransport === "http_sse"
          ? async (request: SummaryRequest): Promise<string> => {
            const summaryRoundIndex = providerRoundIndex;
            providerRoundIndex += 1;
            try {
              const summaryModel = await resolveModelTurns(
                input,
                config,
                [],
                [],
                emit,
                (observation) => e01.decideProviderRecovery(observation),
                e01.journal.restartEpoch,
                (observation) => e01.completeProviderRecovery(observation),
                (requestId) => e01.executePreparedProvider(requestId),
                summaryRoundIndex,
                [
                  ...compactMessagesForProvider(request.messages),
                  { role: "user", content: modelCompactionPrompt(request) },
                ],
              );
              const formatted = formatModelCompactionSummary(summaryModel.finalText, request.tokenBudget);
              if (summaryModel.ok && summaryModel.turns.flat().length === 0 && formatted) {
                await emit("context_compaction_summary_generated", {
                  owner: "provider_model",
                  provider_request_id: summaryModel.providerRequestId,
                  source_message_count: request.messages.length,
                  summary_chars: formatted.length,
                });
                return formatted;
              }
              await emit("context_compaction_summary_fallback", {
                owner: "deterministic_fallback",
                provider_request_id: summaryModel.providerRequestId,
                reason: summaryModel.ok ? "provider_summary_not_text_only" : summaryModel.error ?? "provider_summary_failed",
              });
            } catch (error) {
              await emit("context_compaction_summary_fallback", {
                owner: "deterministic_fallback",
                reason: error instanceof Error ? error.message : String(error),
              });
            }
            return durableCompactionSummary(request);
          }
          : durableCompactionSummary;
        const matureCompact = compactSource.length >= 3
          ? await e01.compact.compactConversation(
            compactSource,
            compactOptions,
            compactSummaryProvider,
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
        if (
          matureCompact !== null
          && modelTransport === "http_sse"
          && modelCompactSource.length >= 3
        ) {
          pendingProviderCompaction = {
            boundaryId: matureCompact.boundary.boundaryId,
            messages: compactMessagesForProvider(matureCompact.messages),
          };
        }
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
        if (pendingProviderCompaction !== null) {
          const currentToolResult = providerMessages.at(-1);
          if (!currentToolResult || asString(currentToolResult.role) !== "user") {
            throw new Error("provider compaction requires the settled tool observation");
          }
          providerMessages = iteration.compactTranscript(
            [...pendingProviderCompaction.messages, currentToolResult],
            pendingProviderCompaction.boundaryId,
          );
          pendingProviderCompaction = null;
        }
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
                "If an opaque validation has failed repeatedly and no permitted diagnostic gives more detail, stop probing private or inaccessible feedback; instead map every public requirement and acceptance condition to its boundary cases, persistent schema, and cross-language consumers, audit that checklist, and edit the uncovered gaps.",
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
            no_delivery_observations: postToolProgressDecision.snapshot.noDeliveryObservationCount,
            consecutive_no_delivery_observations:
              postToolProgressDecision.snapshot.consecutiveNoDeliveryObservations,
            required_delivery_missing: postToolProgressDecision.snapshot.requiredDeliveryMissing,
          });
        } else if (postToolProgressDecision.action === "nudge_verification") {
          const nudgedProgress = progressive.recordVerificationNudge();
          providerMessages = [
            ...providerMessages,
            {
              role: "user",
              content: [
                "The workspace changed, but there is no successful behavioral verification for the latest delivered state.",
                `Outstanding verification debt: ${postToolProgressDecision.reason}.`,
                "Before continuing broad inspection, run a proportionate real verification command such as the relevant tests, build or typecheck, smoke or end-to-end scenario, or the task-provided simulation or acceptance command.",
                "When the debt names an earlier failed semantic scope, rerun that same scope; passing unrelated suites cannot settle it.",
                "File existence, JSON parsing, hashes, git status, report text, and merely reading test source are not behavioral verification.",
                "While behavioral verification remains failed, do not regenerate submission/evidence/reports or edit their generators as a substitute for repairing the public-contract or business implementation.",
                CONTRACT_PARITY_REPAIR_GUIDANCE,
                "If verification fails, use its concrete evidence to fix the cause and rerun it.",
              ].join(" "),
            },
          ];
          await emit("progressive_verification_requested", {
            reason: postToolProgressDecision.reason,
            execution_phase: postToolProgressDecision.snapshot.phase,
            verification_nudge: nudgedProgress.verificationNudgeCount,
            verification_count: nudgedProgress.verificationCount,
            workspace_mutations: nudgedProgress.workspaceMutationCount,
            artifacts: nudgedProgress.artifactCount,
            post_tool: true,
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
        progressive_verifications: String(progressiveState.verificationCount),
        progressive_verification_nudges: String(
          progressiveState.verificationNudgeCount,
        ),
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
  // Once repeated inspection has opened the circuit, an opaque shell command
  // is not evidence of delivery merely because the registry classifies the
  // shell tool itself as mutating.  Only commands whose arguments clearly
  // drive an edit, build, test, migration, service, or external state change
  // cross this boundary.  This also closes interpreter-wrapped read bypasses.
  return !isClearlyDeliveryDrivingShellCommand(shellInvocationText(step.arguments));
}

export function isTargetedRepairInspection(
  step: { tool_name: string; arguments: JsonObject },
  readOnly: boolean,
): boolean {
  if (step.tool_name === "shell") {
    return isBoundedTargetedShellInspection(shellInvocationText(step.arguments));
  }
  if (!readOnly || !["read", "file_read"].includes(step.tool_name)) return false;
  const path = asString(step.arguments.path || step.arguments.file_path)
    .trim()
    .replaceAll("\\", "/");
  if (!path || /[*?\[\]]/u.test(path) || path.endsWith("/")) return false;
  if (isGeneratedDeliveryPath(path)) return false;
  if (/(?:^|\/)(?:node_modules|\.git|\.runtime|\.venv|venv|dist|coverage)(?:\/|$)/iu.test(path)) {
    return false;
  }
  const basename = path.slice(path.lastIndexOf("/") + 1);
  // This reserve exists for a named implementation or contract file, not a
  // directory walk or another repository-wide search disguised as a read.
  return /\.[a-z0-9][a-z0-9._-]{0,15}$/iu.test(basename);
}

function isBoundedTargetedShellInspection(command: string): boolean {
  const normalized = command.trim().replace(/\s+/gu, " ");
  if (!normalized || normalized.length > 2_000) return false;
  if (isClearlyDeliveryDrivingShellCommand(normalized)) return false;
  if (shellMutationTargets(normalized).length > 0) return false;

  // Mature coding agents expose a bounded directory-read primitive.  The
  // governed shell is Zyra's equivalent recovery path when an exact file read
  // reports ENOENT: permit one non-recursive listing of a concrete subtree so
  // the model can re-anchor to real source names instead of inventing a file.
  const directoryListingPath = boundedDirectoryListingPath(normalized);
  if (directoryListingPath && isExactTargetedDirectoryPath(directoryListingPath)) return true;

  // A large implementation file can exceed the per-observation budget. Allow
  // one exact source file to be searched or sliced with an explicit line cap,
  // so a named symbol remains inspectable without reopening recursive scans.
  const pipeline = normalized.split(/\s+\|\s+/u).map((part) => part.trim());
  if (
    pipeline.length === 2
    && /^(?:grep|rg)(?:\.exe)?\b/iu.test(pipeline[0])
    && !/(?:^|\s)(?:-[^\s]*r[^\s]*|--recursive)(?:\s|$)/iu.test(pipeline[0])
    && /^(?:head|tail)\s+(?:-n\s+)?-?(\d+)$/iu.test(pipeline[1])
  ) {
    const limit = Number(pipeline[1].match(/(\d+)$/u)?.[1] ?? 0);
    const tokens = pipeline[0].match(/(?:"[^"]*"|'[^']*'|\S+)/gu) ?? [];
    const path = (tokens.at(-1) ?? "").replace(/^["']|["']$/gu, "");
    if (limit > 0 && limit <= 400 && isExactTargetedSourcePath(path)) return true;
  }

  const sedRange = normalized.match(
    /^sed\s+-n\s+["']?(\d+),(\d+)p["']?\s+(["']?[^\s"']+["']?)$/iu,
  );
  if (sedRange) {
    const start = Number(sedRange[1]);
    const end = Number(sedRange[2]);
    const path = sedRange[3].replace(/^["']|["']$/gu, "");
    if (start > 0 && end >= start && end - start < 400 && isExactTargetedSourcePath(path)) {
      return true;
    }
  }

  // A database exception often names the failing column but not the actual
  // deployed schema. Let a bounded psql description or SELECT consume the
  // same repair reserve as a named source read. State-changing SQL, SQL files,
  // and unbounded interactive clients remain outside this path.
  if (/\bpsql\b/iu.test(normalized)) {
    if (!/(?:\s-[a-z]*c(?:=|\s)|\s--command(?:=|\s))/iu.test(normalized)) return false;
    if (/\\(?:i|include|ir|include_relative|copy|gexec|watch)\b/iu.test(normalized)) {
      return false;
    }
    return /(?:\\d(?:[a-z+stvx]*)?\b|\bselect\b|\bshow\b|\binformation_schema\b|\bpg_catalog\b)/iu.test(normalized);
  }

  // Exact, bounded service logs are useful immediately after a concrete
  // runtime failure. Repository-wide searches and pipelines still use the
  // ordinary diagnostic allowance.
  if (/\bdocker(?:\.exe)?\s+(?:compose\s+)?logs\b/iu.test(normalized)) {
    return /(?:--tail(?:=|\s+)\d+|\s-tail\s+\d+)\b/iu.test(normalized)
      && !/[|;&]/u.test(normalized);
  }

  return false;
}

function boundedDirectoryListingPath(command: string): string {
  const bareList = command.match(
    /^ls\s+(?:-[a-z]*[1al][a-z]*\s+)?(?:--\s+)?(["']?[^\s"'|;&*?\[\]{}]+["']?)$/iu,
  );
  if (bareList) return bareList[1].replace(/^['"]|['"]$/gu, "");
  const find = command.match(
    /^find\s+(["']?[^\s"'|;&*?\[\]{}]+["']?)\s+-maxdepth\s+1\s+-type\s+[fd](?:\s+-print)?(?:\s+\|\s+head\s+(?:-n\s+)?\d+)?$/iu,
  );
  return find ? find[1].replace(/^['"]|['"]$/gu, "") : "";
}

function isExactTargetedDirectoryPath(value: string): boolean {
  const path = value.trim().replaceAll("\\", "/").replace(/^\.\//u, "").replace(/\/$/u, "");
  if (!path || path === "." || path.startsWith("-") || /[*?\[\]{}]/u.test(path)) return false;
  if (isGeneratedDeliveryPath(path)) return false;
  if (/(?:^|\/)(?:node_modules|\.git|\.runtime|\.venv|venv|dist|coverage)(?:\/|$)/iu.test(path)) {
    return false;
  }
  return path.split("/").filter(Boolean).length >= 2;
}

function isExactTargetedSourcePath(value: string): boolean {
  const path = value.trim().replaceAll("\\", "/");
  if (!path || /[*?\[\]{}]/u.test(path) || path.endsWith("/")) return false;
  if (path.startsWith("-") || isGeneratedDeliveryPath(path)) return false;
  if (/(?:^|\/)(?:node_modules|\.git|\.runtime|\.venv|venv|dist|coverage)(?:\/|$)/iu.test(path)) {
    return false;
  }
  const basename = path.slice(path.lastIndexOf("/") + 1);
  return /\.[a-z0-9][a-z0-9._-]{0,15}$/iu.test(basename);
}

export function preDeliveryInspectionGuidance(
  targetedRepairInspectionsRemaining: number,
): string[] {
  const guidance = [
    "Use the concrete evidence already gathered and make the next workspace edit.",
    "A build, test, or real service command is also allowed when it directly drives that edit.",
    "Opaque test labels are not a reason to search private test infrastructure; inspect the public contract and a concrete implementation file instead.",
    CONTRACT_PARITY_REPAIR_GUIDANCE,
  ];
  if (targetedRepairInspectionsRemaining > 0) {
    guidance.push(
      `${targetedRepairInspectionsRemaining} targeted diagnostics remain: use read or file_read with an exact implementation or contract file path; when a large file exceeds the observation limit, use grep or rg against one exact file piped to a numeric head/tail limit, or sed -n with a bounded numeric line range. After a concrete database or service failure, a bounded read-only psql schema query or exact tailed service log is also allowed. Broad or recursive searches and shell cat, type, or Get-Content remain blocked.`,
      "If an exact path is missing, use ls on its concrete parent directory or find with -maxdepth 1 before deciding that a new source file is required.",
    );
  }
  guidance.push(
    "Further broad inspection becomes available after a committed business-implementation delivery or in a fresh task phase; tests and documentation alone do not reopen it while behavioral verification is failing.",
  );
  return guidance;
}

export function isClearlyRepairDrivingTool(
  step: { tool_name: string; arguments: JsonObject },
): boolean {
  if (isClearlyVerificationDrivingTool(step)) return false;
  const path = asString(step.arguments.path || step.arguments.file_path).trim();
  if (path) {
    return !isGeneratedDeliveryPath(path) && !isValidationOnlyDeliveryPath(path);
  }
  if (step.tool_name !== "shell") return true;

  const command = shellInvocationText(step.arguments);
  if (!isClearlyDeliveryDrivingShellCommand(command)) return false;
  const explicitTargets = shellMutationTargets(command);
  if (explicitTargets.length > 0) {
    return explicitTargets.some(
      (target) => !isGeneratedDeliveryPath(target) && !isValidationOnlyDeliveryPath(target),
    );
  }
  // Builds, dependency installation and service lifecycle commands are real
  // execution, but their generated files are not evidence that source bytes
  // were repaired. Shell repair progress therefore requires a concrete write
  // target; opaque commands remain delivery-driving without refilling the
  // failed-verification diagnostic circuit.
  return false;
}

export function isValidationOnlyMutation(
  step: { tool_name: string; arguments: JsonObject },
): boolean {
  const path = asString(step.arguments.path || step.arguments.file_path).trim();
  if (path) {
    return ["write", "file_write", "edit", "file_edit"].includes(step.tool_name)
      && isValidationOnlyDeliveryPath(path);
  }
  if (step.tool_name !== "shell") return false;
  const targets = shellMutationTargets(shellInvocationText(step.arguments));
  return targets.length > 0 && targets.every((target) => isValidationOnlyDeliveryPath(target));
}

export function isGeneratedDeliveryMutation(
  step: { tool_name: string; arguments: JsonObject },
): boolean {
  const path = asString(step.arguments.path || step.arguments.file_path).trim();
  if (path) {
    return ["write", "file_write", "edit", "file_edit"].includes(step.tool_name)
      && isGeneratedDeliveryPath(path);
  }
  if (step.tool_name !== "shell") return false;
  const targets = shellMutationTargets(shellInvocationText(step.arguments));
  return targets.length > 0 && targets.every((target) => isGeneratedDeliveryPath(target));
}

export function isGeneratedDeliveryInspection(
  step: { tool_name: string; arguments: JsonObject },
  readOnly: boolean,
): boolean {
  const path = asString(step.arguments.path || step.arguments.file_path).trim();
  if (path) {
    return readOnly && (isGeneratedDeliveryPath(path) || isDeliveryEvidenceGeneratorPath(path));
  }
  if (step.tool_name !== "shell") return false;
  const command = shellInvocationText(step.arguments).replaceAll("\\", "/");
  if (isClearlyVerificationDrivingTool(step) || shellMutationTargets(command).length > 0) return false;
  return /(?:^|[\s"'=])(?:\.\/)?(?:submission|evidence|\.runtime)\//iu.test(command)
    || /(?:^|\/)\b(?:regenerate|generate|update)[-_]?(?:submission|evidence|manifest|report)\b/iu.test(command);
}

function shellMutationTargets(command: string): string[] {
  const targets: string[] = [];
  const patterns = [
    /(?:^|\s)(?:\d?>>|\d?>(?!&)|&>)\s*["']?([^\s"';&|]+)/giu,
    /\btee(?:\s+-a)?\s+["']?([^\s"';&|]+)/giu,
    /\bPath\(\s*["']([^"']+)["']\s*\)\.(?:write_text|write_bytes)\b/giu,
    /\bopen\(\s*["']([^"']+)["']\s*,\s*["'][wax+][^"']*["']/giu,
    /\bsed\b[^;&|\n]*\s-i\S*\s+(?:["'][^"']*["']\s+)?["']?([^\s"';&|]+)["']?(?=\s*(?:$|[;&|]))/gimu,
    /^\*{3}\s+(?:Add|Update|Delete) File:\s*(\S+)\s*$/gimu,
  ];
  for (const pattern of patterns) {
    for (const match of command.matchAll(pattern)) {
      if (match[1] && !isNullDevicePath(match[1])) targets.push(match[1]);
    }
  }
  return targets;
}

function isNullDevicePath(value: string): boolean {
  const normalized = value.trim().replace(/^['"]|['"]$/gu, "").replaceAll("\\", "/").toLowerCase();
  return normalized === "/dev/null" || normalized === "nul" || normalized === "nul:";
}

function isGeneratedDeliveryPath(value: string): boolean {
  const normalized = value.trim().replaceAll("\\", "/").replace(/^\.\//u, "");
  return /(?:^|\/)(?:submission|evidence|\.runtime)(?:\/|$)/iu.test(normalized);
}

function isValidationOnlyDeliveryPath(value: string): boolean {
  const normalized = value.trim().replaceAll("\\", "/").replace(/^\.\//u, "");
  const basename = normalized.slice(normalized.lastIndexOf("/") + 1);
  return /(?:^|\/)(?:tests?|__tests__|snapshots?|docs?)(?:\/|$)/iu.test(normalized)
    || /(?:^|[._-])(?:test|spec|snapshot)(?:[._-]|$)/iu.test(basename)
    || /^(?:readme|changelog|contributing|architecture)(?:\.[^.]+)?$/iu.test(basename)
    || /\.(?:md|mdx|rst|adoc)$/iu.test(basename);
}

function isDeliveryEvidenceGeneratorPath(value: string): boolean {
  const normalized = value.trim().replaceAll("\\", "/");
  const basename = normalized.slice(normalized.lastIndexOf("/") + 1);
  return /^(?:regenerate|generate|update)[-_]?(?:submission|evidence|manifest|report)\b/iu.test(basename);
}

export function isClearlyVerificationDrivingTool(
  step: { tool_name: string; arguments: JsonObject },
): boolean {
  if (step.tool_name !== "shell") return false;
  return isClearlyVerificationDrivingShellCommand(
    shellInvocationText(step.arguments),
  );
}

export function isAlternativeVerificationInspection(
  step: { tool_name: string; arguments: JsonObject },
  unresolvedScopes: readonly string[],
): boolean {
  if (!isClearlyVerificationDrivingTool(step)) return false;
  const scope = verificationScopeForTool(step);
  if (!scope || unresolvedScopes.includes(scope)) return false;

  // Compilation and build commands are frequently required to make a source
  // repair observable by the original suite.  They are repair preparation,
  // not an attempt to substitute a different green score for the failed one.
  const command = shellInvocationText(step.arguments).replaceAll("\\", "/");
  if (/(?:^|\s)(?:\S*\/)?afctl\.py\s+build(?=\s|[;&|]|$)/iu.test(command)) return false;
  if (/\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:build|typecheck|lint)(?=\s|$)/iu.test(command)) return false;
  if (/\bnpx\s+(?:tsc|eslint)\b/iu.test(command)) return false;
  if (/\bpython(?:3)?\s+-m\s+(?:py_compile|compileall)\b/iu.test(command)) return false;
  if (/\b(?:cargo\s+(?:check|build)|go\s+build)\b/iu.test(command)) return false;
  return true;
}

export function verificationScopeForTool(
  step: { tool_name: string; arguments: JsonObject },
): string {
  if (!isClearlyVerificationDrivingTool(step)) return "";
  return verificationScope(shellInvocationText(step.arguments));
}

export function isClearlyEnvironmentRecoveryTool(
  step: { tool_name: string; arguments: JsonObject },
): boolean {
  if (step.tool_name !== "shell") return false;
  const command = shellInvocationText(step.arguments).replace(/\s+/gu, " ").trim();
  return /\bdocker(?:\.exe)?\s+compose\b[^;&|]*\b(?:up|start|restart)\b/iu.test(command)
    || /\bdocker(?:\.exe)?\s+(?:start|restart)\b/iu.test(command)
    || /\bsystemctl\s+(?:start|restart|reload)\s+\S+/iu.test(command)
    || /\bservice\s+\S+\s+(?:start|restart|reload)\b/iu.test(command)
    || /\b(?:python(?:3)?\s+)?\S*afctl\.py\s+(?:bootstrap|up|start|restart)\b/iu.test(command);
}

export function isEnvironmentRecoveryToolResult(
  step: { tool_name: string; arguments: JsonObject },
  response: ToolExecutionResponse,
  historicalCalls: readonly {
    toolCallId: string;
    name: string;
    arguments: JsonObject;
  }[] = [],
): boolean {
  if (isClearlyEnvironmentRecoveryTool(step)) return true;
  if (step.tool_name !== "shell_wait") return false;
  const origin = originatingToolCall(response, historicalCalls);
  return origin !== undefined && isClearlyEnvironmentRecoveryTool({
    tool_name: origin.name,
    arguments: origin.arguments,
  });
}

export function isVerificationDrivingToolResult(
  step: { tool_name: string; arguments: JsonObject },
  response: ToolExecutionResponse,
  historicalCalls: readonly {
    toolCallId: string;
    name: string;
    arguments: JsonObject;
  }[] = [],
): boolean {
  if (isClearlyVerificationDrivingTool(step)) return true;
  if (step.tool_name !== "shell_wait") return false;

  // Long commands cross a provider-turn boundary: `shell` starts the
  // verification job and `shell_wait` receives its terminal report. The
  // gateway receipt durably points back to the originating tool call, so use
  // that lineage instead of treating the wait as an unrelated read.
  const origin = originatingToolCall(response, historicalCalls);
  if (!origin) return false;
  return isClearlyVerificationDrivingTool({
    tool_name: origin.name,
    arguments: origin.arguments,
  });
}

export function verificationScopeForToolResult(
  step: { tool_name: string; arguments: JsonObject },
  response: ToolExecutionResponse,
  historicalCalls: readonly {
    toolCallId: string;
    name: string;
    arguments: JsonObject;
  }[] = [],
): string {
  const direct = verificationScopeForTool(step);
  if (direct) return direct;
  if (step.tool_name !== "shell_wait") return "";

  const origin = originatingToolCall(response, historicalCalls);
  return origin
    ? verificationScopeForTool({ tool_name: origin.name, arguments: origin.arguments })
    : "";
}

function originatingToolCall(
  response: ToolExecutionResponse,
  historicalCalls: readonly {
    toolCallId: string;
    name: string;
    arguments: JsonObject;
  }[],
): { toolCallId: string; name: string; arguments: JsonObject } | undefined {
  const receipt = asObject(response.output.gateway_receipt);
  const invocationRef = asObject(receipt.invocation_ref);
  const invocation = asObject(receipt.invocation);
  const originToolCallId = [
    response.metadata.originating_tool_call_id,
    response.output.originating_tool_call_id,
    invocationRef.tool_call_id,
    invocation.tool_call_id,
    invocation.causation_id,
  ]
    .map((value) => asString(value).trim())
    .find(Boolean) ?? "";
  return originToolCallId
    ? historicalCalls.find((call) => call.toolCallId === originToolCallId)
    : undefined;
}

function shellInvocationText(arguments_: JsonObject): string {
  const command = asString(arguments_.command).trim();
  if (command) {
    // The governed container already starts every shell in /workspace, but
    // coding models often repeat that fact in the command.  Ignore this
    // no-op prefix for policy classification so an exact bounded source read
    // keeps its intended diagnostic cost and cannot change categories merely
    // because the model restated the working directory.
    return command.replace(
      /^\s*cd\s+(?:["']\/workspace["']|\/workspace)\s*(?:&&|;)\s*/iu,
      "",
    ).trim();
  }
  const executable = asString(arguments_.executable).trim();
  const argv = Array.isArray(arguments_.argv)
    ? arguments_.argv
      .filter((item): item is string => typeof item === "string")
      .join(" ")
    : "";
  return [executable, argv].filter(Boolean).join(" ").trim();
}

function verificationScope(value: string): string {
  const normalized = value
    .toLowerCase()
    .replaceAll("\\", "/")
    .replace(/\b[0-9a-f]{8}-[0-9a-f-]{27,}\b/giu, "<uuid>")
    .replace(/\b(?:job|run|task|attempt|request)_[a-z0-9_-]{8,}\b/giu, "<identity>")
    .replace(/\b[0-9a-f]{16,}\b/giu, "<digest>")
    .replace(/\s+/g, " ")
    .trim();
  const semanticScope = semanticVerificationScope(normalized);
  if (semanticScope) return semanticScope;
  return normalized ? `shell:${createHash("sha256").update(normalized).digest("hex").slice(0, 24)}` : "";
}

function semanticVerificationScope(value: string): string {
  // Test runners are frequently wrapped in timeouts, log redirections, tail,
  // grep, and changing temporary filenames.  Hashing the whole command makes
  // every such rerun look like an independent obligation, so a green rerun
  // can never settle the earlier failed suite.  Prefer a stable entry-point
  // identity and retain the hash fallback for commands we cannot classify.
  const afctlScopes = new Set<string>();
  for (const match of value.matchAll(
    /(?:^|\s)(?:\S*\/)?afctl\.py\s+(test\s+[a-z0-9_-]+|build|simulate)(?=\s|[;&|]|$)/giu,
  )) {
    afctlScopes.add(match[1].trim().replace(/\s+/g, ":"));
  }
  if (afctlScopes.size > 0) {
    return `shell:afctl:${[...afctlScopes].sort().join("+")}`;
  }

  const pytestTargets = new Set<string>();
  for (const match of value.matchAll(
    /(?:^|\s)(?:python(?:3)?\s+-m\s+)?(?:pytest|py\.test)\b([^;&|\r\n]*)/giu,
  )) {
    const target = match[1]
      .trim()
      .split(/\s+/)
      .find((item) => !item.startsWith("-") && /(?:^|\/)tests?(?:\/|$)/u.test(item));
    pytestTargets.add(target?.replace(/["']/g, "") ?? "all");
  }
  if (pytestTargets.size > 0) {
    return `shell:pytest:${[...pytestTargets].sort().join("+")}`;
  }

  const packageScript = value.match(
    /(?:^|\s)(npm|pnpm|yarn|bun)\s+(?:run\s+)?(test(?::[a-z0-9_-]+)?|check|lint|build|typecheck|verify|validate|smoke|e2e|integration)(?=\s|$)/iu,
  );
  if (packageScript) {
    return `shell:${packageScript[1]}:${packageScript[2]}`;
  }
  return "";
}

function isClearlyVerificationDrivingShellCommand(value: string): boolean {
  const segments = value
    .split(/&&|\|\||;|\r?\n/)
    .map((segment) => segment.trim().replace(/\s+/g, " "))
    .filter(Boolean);
  return segments.some((segment) => {
    if (/^(?:cat|grep|rg|sed\s+-n|head|tail|find|ls|tree|type|select-string)\b/i.test(segment)) return false;
    if (/\bpython(?:3)?\b[^;&|]*\s-(?:c|e)\b/i.test(segment)) return false;
    if (/\bpython(?:3)?\s+-m\s+(?:pytest|unittest|compileall)\b/i.test(segment)) return true;
    if (/\b(?:pytest|py\.test)\b/i.test(segment)) return true;
    if (/\b(?:npm|pnpm|yarn|bun)\s+(?:test|check|lint|build|typecheck)\b/i.test(segment)) return true;
    if (/\b(?:npm|pnpm|yarn|bun)\s+run\s+(?:test|check|lint|build|typecheck|verify|validate|smoke|e2e|integration)\b/i.test(segment)) return true;
    if (/\bnpx\s+(?:tsc|eslint|jest|vitest|playwright|mocha|ava)\b/i.test(segment)) return true;
    if (/\btsc\b(?:\s|$)/i.test(segment)) return true;
    if (/\bcargo\s+(?:test|check|clippy|build)\b/i.test(segment)) return true;
    if (/\bgo\s+test\b/i.test(segment)) return true;
    if (/\b(?:gradle|gradlew|mvn|mvnw)\b[^;&|]*(?:test|check|verify|build)\b/i.test(segment)) return true;
    if (/\b(?:make|cmake|ctest)\b[^;&|]*(?:test|check|verify|build)\b/i.test(segment)) return true;
    if (/\b(?:sh|bash)\b[^;&|]*(?:test|check|verify|validate|smoke|e2e|integration|build)[^;&|]*\.sh\b/i.test(segment)) return true;
    if (/\bpython(?:3)?\b[^;&|]*(?:^|[^a-z0-9])(?:test|check|verify|validate|smoke|e2e|integration|build|simulate)(?=[^a-z0-9]|$)/i.test(segment)) return true;
    // A script whose executable path is itself a verification entry point is
    // evidence-driving.  Do not scan arbitrary later path arguments: commands
    // such as `cat tests/public/test_metrics.py` only inspect test source and
    // must not discharge verification debt or reset no-progress detection.
    if (/^(?:env\s+(?:[^\s=]+=[^\s]+\s+)+)?(?:\.\/|\/)[^\s]*(?:test|check|verify|validate|smoke|e2e|integration|build|simulate)[^\s]*(?:\.sh|\.py)?(?:\s|$)/i.test(segment)) return true;
    if (/\bdocker(?:\.exe)?\s+compose\b[^;&|]*\brun\b[^;&|]*(?:test|pytest|check|verify|smoke|e2e|integration)\b/i.test(segment)) return true;
    return false;
  });
}

function isClearlyDeliveryDrivingShellCommand(value: string): boolean {
  const command = value.trim();
  if (!command) return false;
  const normalized = command.replace(/\s+/g, " ");

  // Direct filesystem and source mutations.
  if (/(?:^|\s)(?:\d?>>|\d?>(?!&)|&>)(?!\s*(?:\/dev\/null|nul)\b)/i.test(normalized)) return true;
  if (/\b(?:apply_patch|patch|tee|touch|mkdir|rmdir|rm|mv|cp|install|chmod|chown)\b/i.test(normalized)) return true;
  if (/\b(?:sed|perl)\b[^;&|]*\s-i(?:\s|$)/i.test(normalized)) return true;

  // Inline interpreters are opaque by default.  Admit them only when their
  // program text contains an explicit durable-write primitive or a test run.
  if (/\b(?:python(?:3)?|node|bun|deno|ruby|perl|pwsh|powershell|cmd(?:\.exe)?)\b/i.test(normalized)) {
    if (/\b(?:write_text|write_bytes|writeFile|writeFileSync|appendFile|appendFileSync|rename|replace|unlink|mkdir|makedirs)\s*\(/i.test(normalized)) return true;
    if (/\bopen\s*\([^)]*,\s*["'][wax+][^"']*["']/i.test(normalized)) return true;
    if (/\b(?:pytest|unittest|compileall|pip|uv|poetry)\b/i.test(normalized)) return true;
    if (!/(?:^|\s)-(?:c|e|Command)(?:\s|$)/i.test(normalized)
      && /(?:^|\s)(?:bootstrap|build|test|install|up|down|start|stop|restart|simulate|migrate|deploy|apply|rollback|request-acceptance)(?:\s|$)/i.test(normalized)) return true;
    return false;
  }

  // Builds, tests, dependency changes and schema migrations.
  if (/\b(?:pytest|unittest|npm|npx|pnpm|yarn|cargo|go|gradle|mvn|make|cmake|pip|uv|poetry|alembic|flyway|prisma)\b/i.test(normalized)) return true;
  // `psql` is both an inspection client and a migration tool. Schema/listing
  // commands and SELECT queries must stay within the bounded inspection
  // budget, including when transported through docker exec. Admit only an
  // explicit SQL file or a recognisable state-changing statement.
  if (/\bpsql\b[^;&|]*(?:\s(?:-f|--file)(?:=|\s)|\b(?:insert|update|delete|merge|create|alter|drop|truncate|grant|revoke|comment|vacuum|reindex|refresh)\b)/i.test(normalized)) return true;
  if (/\b(?:sh|bash)\b[^;&|]*(?:build|test|install|migrate|deploy|bootstrap|simulate)[^;&|]*\.sh\b/i.test(normalized)) return true;
  if (/\/(?:[^\s/]+\/)*(?:build|test|install|migrate|deploy|bootstrap|simulate)[^\s/]*(?:\.sh)?\b/i.test(normalized)) return true;

  // VCS delivery, real services and explicit state-changing HTTP calls.
  if (/\bgit\s+(?:add|commit|checkout|switch|restore|reset|merge|rebase|apply|am|clean|push|pull|fetch)\b/i.test(normalized)) return true;
  // `docker exec` is only a transport wrapper. Its inner command is already
  // classified by the mutation/build/test rules above; treating every exec
  // as delivery lets `docker exec ... cat` bypass bounded inspection forever.
  if (/\bdocker(?:\.exe)?\s+(?:run|start|stop|restart|kill|rm|rmi|build|pull|push)\b/i.test(normalized)) return true;
  // `docker compose exec`, like `docker exec`, is only a transport wrapper;
  // its inner command must independently demonstrate delivery.
  if (/\bdocker(?:\.exe)?\s+compose\b[^;&|]*(?:\bup\b|\bdown\b|\bbuild\b|\brun\b|\bstart\b|\bstop\b|\brestart\b|\bpull\b|\bkill\b|\brm\b)/i.test(normalized)) return true;
  if (/\b(?:systemctl|service)\s+(?:start|stop|restart|reload|enable|disable)\b/i.test(normalized)) return true;
  if (/\b(?:curl|wget|invoke-webrequest|invoke-restmethod)\b[^;&|]*(?:\s-X\s*(?:POST|PUT|PATCH|DELETE)\b|--request\s+(?:POST|PUT|PATCH|DELETE)\b|--data(?:-binary|-raw|-urlencode)?\b|-d\s)/i.test(normalized)) return true;

  return false;
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

export function e01RuntimeEventPayload(phase: string, payload: JsonObject): JsonObject {
  if (phase !== "model_request_prepared") return payload;
  const request = asObject(payload.provider_request);
  if (Object.keys(request).length === 0) return payload;
  const messages = Array.isArray(request.messages) ? request.messages : [];
  const tools = Array.isArray(request.tools) ? request.tools : [];
  const messagesDigest = asString(request.messages_digest, "unavailable");
  const toolsDigest = asString(request.tools_digest, "unavailable");
  const compactTools = tools.map((candidate) => {
    const tool = asObject(candidate);
    const definition = asObject(tool.function);
    return {
      type: "function",
      function: {
        name: asString(definition.name, "unknown_tool"),
        description: `Provider-owned tool schema; canonical set ${toolsDigest}.`,
        parameters: { type: "object", additionalProperties: true },
      },
    };
  });
  return {
    ...payload,
    provider_request: {
      ...request,
      messages: [{
        role: "user",
        content: [{
          type: "text",
          text: [
            "Provider prompt content is held by the durable provider control plane.",
            `Canonical digest: ${messagesDigest}.`,
            `Message count: ${messages.length}.`,
          ].join(" "),
        }],
      }],
      tools: compactTools,
      system: [],
      prompt_content_externalized: true,
      prompt_content_owner: "provider_control_plane",
      prompt_message_count: messages.length,
      prompt_tool_count: tools.length,
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
