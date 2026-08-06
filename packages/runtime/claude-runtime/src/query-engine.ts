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
import { normalizeMessages, resolveModelTurns } from "./model-stream.ts";
import { RuntimeSession } from "./session.ts";
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
} from "./loop/model-iteration-runtime.ts";
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
        maximumRounds: config.maxTurns ?? 1_000,
        maximumToolCalls: Math.max(1_000, (config.maxTurns ?? 1_000) * 32),
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
    let ok = true;
    let stoppedReason: string | null = null;
    let continuedFailureReason: string | null = null;
    let permissionSuspended = false;
    let providerRoundIndex = 0;
    let activeIterationRoundId: string | null = null;
    let pendingRestoreProviderMessage: JsonObject | null = null;
    const mutationTargets = new Set<string>();
    let modelMetadata: Record<string, string> = {
      model_stream_ok: "false",
      api_retry_ok: "false",
      api_retry_recovered: "false",
    };

    const emit = async (phase: string, payload: JsonObject = {}): Promise<void> => {
      eventSequence += 1;
      const publicPayload = publicRuntimeEventPayload(phase, payload);
      const event: RuntimeEvent = {
        phase,
        sequence: eventSequence,
        canonical_owner: "typescript",
        runtime_id: "zyra-typescript-claude-runtime",
        session_id: input.sessionId,
        run_id: input.runId,
        task_id: input.taskId,
        worker_request_id: input.workerRequestId,
        ...publicPayload,
      };
      if (phase === "model_stream_report") {
        e01.observeProviderGateway(phase, {
          ...payload,
          provider_base_url: config.runtimeConstraints.model_api_base_url ?? null,
        });
      }
      e01.recordRuntimeEvent(phase, payload);
      await host.emitEvent({ ...event, e01_revision: e01.journal.revision });
      await host.checkpointState?.({
        ...session.snapshot(),
        e01Runtime: e01.snapshot() as unknown as JsonObject,
        modelIteration: iteration.snapshot() as unknown as JsonObject,
        checkpointPhase: phase,
        checkpointEventSequence: eventSequence,
      });
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

    if (ok) {
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
        iteration.acceptProviderResult({
          roundId: iterationRound.roundId,
          providerRequestId: model.providerRequestId,
          model: config.modelName,
          stopReason: model.stopReason,
          finalText: model.finalText,
          steps: model.turns.flat(),
        });
        activeIterationRoundId = model.turns.length > 0 ? iterationRound.roundId : null;
      }
    }

    const turnLimit = config.maxTurns ?? (modelTransport === "http_sse" ? 1_000 : turns.length);
    const restoredActiveTurn = session.activeTurnSnapshot();
    const initialTurnIndex = restoredActiveTurn?.turn_index ?? 0;
    for (let turnIndex = initialTurnIndex; ok && turnIndex < turns.length; turnIndex += 1) {
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
          const toolCustody = e01.completeToolExecution({
            callId: result.tool_call_id,
            toolName: step.tool_name,
            ok: result.ok,
            summary: result.summary,
            output: result.output,
            error: result.error ?? null,
            maximumCharacters: budget,
          });
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
            toolFailureSignals += 1;
            if (result.error === "schema_error" || result.error === "tool_schema_validation_failed") {
              toolSchemaErrors += 1;
            }
            const failureKind = result.error === "permission_approval_required"
              || result.error === "permission_denied"
              ? "permission_denied"
              : result.error || "tool_error";
            const failureRoute = failureKind === "permission_denied"
              ? "permission_runtime"
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
                action: config.continueOnError && !permissionSuspended ? "continue" : "stop",
              },
            });
            if (config.continueOnError && !permissionSuspended) {
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
        if (!turnOk && (!config.continueOnError || permissionSuspended)) {
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
        const compactSource = session.messages.map((item, index) => ({
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
              async () => fallbackCompact.summary,
            )
            : await e01.compact.autoCompactIfNeeded(
              compactSource,
              compactOptions,
              async () => fallbackCompact.summary,
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
      if (!turnOk && (!config.continueOnError || permissionSuspended)) {
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
        && turnIndex + 1 < turnLimit
      ) {
        providerMessages = iteration.buildRevisionMessages(activeIterationRoundId);
        if (pendingRestoreProviderMessage) {
          providerMessages = [...providerMessages, pendingRestoreProviderMessage];
          pendingRestoreProviderMessage = null;
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
          registry.list(),
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
        } else {
          iteration.acceptProviderResult({
            roundId: nextRound.roundId,
            providerRequestId: nextModel.providerRequestId,
            model: config.modelName,
            stopReason: nextModel.stopReason,
            finalText: nextModel.finalText,
            steps: nextModel.turns.flat(),
          });
          if (nextModel.turns.length > 0) {
            turns.push(...nextModel.turns);
            activeIterationRoundId = nextRound.roundId;
          } else {
            activeIterationRoundId = null;
          }
        }
      }
    }

    if (ok && turns.length > turnLimit) {
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
    };
    await emit("query_session_snapshot", {
      snapshot_version: snapshot.version,
      snapshot_checksum: snapshot.checksum,
      snapshot_revision: snapshot.revision,
    });
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
        compact_restore_ok: String(compactRestoreOk),
        runtime_budget_state_ok: String(runtimeBudgetStateOk),
        codeworker_api_foundation_ok: String(codeworkerApiFoundationOk),
        compact_state_projection_ok: String(codeworkerApiFoundationOk),
        compact_restore_contract_id: restoreContractId,
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
