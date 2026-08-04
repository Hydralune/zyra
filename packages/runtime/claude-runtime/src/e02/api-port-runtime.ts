import { randomUUID } from "node:crypto";
import {
  mkdir,
  open,
  readFile,
  rename,
  rm,
  stat,
  writeFile,
  type FileHandle,
} from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { createInterface } from "node:readline";

import {
  asObject,
  asString,
  cloneJson,
  type JsonObject,
  type JsonValue,
  type RuntimeRunInput,
} from "../contracts.ts";
import {
  E02CapabilityCoordinator,
  type E02CapabilityCoordinatorSnapshot,
  type E02AuthorizationInput,
  type E02ExecutionContext,
} from "./coordinator.ts";
import { digest } from "./canonical.ts";
import type {
  PermissionApprovalResponse,
  PermissionMode,
} from "./contracts.ts";
import {
  publicPermissionResponseChallenge,
  verifyPermissionResponseProof,
  type PermissionResponseEnvelopeBinding,
} from "../permission/response-proof.ts";

export const E02_API_PROTOCOL_VERSION = "zyra.e02-api-port/v1";

export interface E02ApiPortInitialization {
  type: "initialize";
  request_id: string;
  workspace_root: string;
  state_path: string;
  artifact_root?: string;
  permission_mode?: string;
  sealed_autonomous?: boolean;
  runtime_constraints?: JsonObject;
}

export interface E02ApiPortRequest {
  type: "request";
  request_id: string;
  operation: string;
  payload?: JsonObject;
}

export interface E02ApiPortResponse {
  type: "ready" | "response" | "error";
  request_id: string;
  ok: boolean;
  protocol: typeof E02_API_PROTOCOL_VERSION;
  payload?: JsonValue;
  error?: JsonObject;
}

interface E02ApiStateEnvelope {
  version: "zyra.e02-api-state/v1";
  generation: number;
  snapshot: E02CapabilityCoordinatorSnapshot;
  snapshot_hash: string;
  written_at: string;
  writer_pid: number;
}

interface HttpProjection {
  status: number;
  body: JsonObject;
  headers: JsonObject;
}

const NO_STORE_HEADERS: JsonObject = {
  "Cache-Control": "no-store, max-age=0",
  Pragma: "no-cache",
  "X-Zyra-E02-State-Owner": "E02CapabilityCoordinator",
  "X-Zyra-MCP-State-Owner": "McpRuntimeCoordinator",
  "X-Zyra-Python-Decision-Fallback": "false",
};

export class E02ApiStateStore {
  readonly path: string;
  readonly lockPath: string;
  private lockHandle: FileHandle | null = null;
  private generation = 0;

  constructor(pathValue: string) {
    if (!pathValue.trim()) throw apiError("e02_api_state_path_missing", "E02 API state path is required");
    this.path = resolve(pathValue);
    this.lockPath = `${this.path}.lock`;
  }

  async acquire(): Promise<void> {
    if (this.lockHandle) return;
    await mkdir(dirname(this.path), { recursive: true });
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        this.lockHandle = await open(this.lockPath, "wx", 0o600);
        await this.lockHandle.writeFile(JSON.stringify({
          version: "zyra.e02-api-lock/v1",
          pid: process.pid,
          acquired_at: new Date().toISOString(),
          state_path: this.path,
        }));
        await this.lockHandle.sync();
        return;
      } catch (error) {
        if (!isNodeError(error, "EEXIST")) throw error;
        const owner = await this.lockOwner();
        if (owner !== null && processIsAlive(owner)) {
          throw apiError(
            "e02_api_state_locked",
            `E02 API state is already owned by process ${owner}`,
            { state_path: this.path, owner_pid: owner },
          );
        }
        await rm(this.lockPath, { force: true });
      }
    }
    throw apiError("e02_api_state_lock_failed", "Unable to acquire the E02 API state lock");
  }

  async load(): Promise<E02CapabilityCoordinatorSnapshot | null> {
    this.requireLock();
    try {
      const parsed = JSON.parse(await readFile(this.path, "utf8")) as unknown;
      const root = asObject(parsed);
      if (root.version !== "zyra.e02-api-state/v1") return null;
      const snapshot = asObject(root.snapshot);
      if (snapshot.version !== "zyra.e02-runtime/v1") {
        throw apiError("e02_api_state_snapshot_version", "Persisted E02 API snapshot has an unsupported version");
      }
      if (asString(root.snapshot_hash) !== asString(snapshot.snapshotHash)) {
        throw apiError("e02_api_state_snapshot_hash", "Persisted E02 API snapshot hash binding is invalid");
      }
      this.generation = nonNegativeInteger(root.generation, 0);
      return jsonClone(snapshot) as unknown as E02CapabilityCoordinatorSnapshot;
    } catch (error) {
      if (isNodeError(error, "ENOENT")) return null;
      if (error instanceof SyntaxError) {
        throw apiError("e02_api_state_json_invalid", "Persisted E02 API state is not valid JSON");
      }
      throw error;
    }
  }

  async save(snapshotValue: E02CapabilityCoordinatorSnapshot): Promise<void> {
    this.requireLock();
    const snapshot = jsonClone(snapshotValue);
    if (snapshot.version !== "zyra.e02-runtime/v1" || !snapshot.snapshotHash) {
      throw apiError("e02_api_checkpoint_invalid", "E02 API checkpoint is missing its version or snapshot hash");
    }
    this.generation += 1;
    const envelope: E02ApiStateEnvelope = {
      version: "zyra.e02-api-state/v1",
      generation: this.generation,
      snapshot,
      snapshot_hash: snapshot.snapshotHash,
      written_at: new Date().toISOString(),
      writer_pid: process.pid,
    };
    const temporary = `${this.path}.tmp-${process.pid}-${randomUUID()}`;
    await writeFile(temporary, JSON.stringify(envelope), { encoding: "utf8", mode: 0o600, flag: "wx" });
    try {
      await rename(temporary, this.path);
    } catch (error) {
      if (process.platform !== "win32") throw error;
      await rm(this.path, { force: true });
      await rename(temporary, this.path);
    } finally {
      await rm(temporary, { force: true });
    }
  }

  async release(): Promise<void> {
    const handle = this.lockHandle;
    this.lockHandle = null;
    if (handle) await handle.close();
    await rm(this.lockPath, { force: true });
  }

  health(): JsonObject {
    return {
      canonical_owner: "typescript",
      state_path: this.path,
      lock_path: this.lockPath,
      lock_held: this.lockHandle !== null,
      generation: this.generation,
      python_state_writer: false,
      atomic_checkpoint_replace: true,
    };
  }

  private async lockOwner(): Promise<number | null> {
    try {
      const value = asObject(JSON.parse(await readFile(this.lockPath, "utf8")));
      return positiveInteger(value.pid, 0) || null;
    } catch {
      try {
        const value = await stat(this.lockPath);
        return Date.now() - value.mtimeMs < 30_000 ? -1 : null;
      } catch {
        return null;
      }
    }
  }

  private requireLock(): void {
    if (!this.lockHandle) throw apiError("e02_api_state_lock_required", "E02 API state access requires the TypeScript lock");
  }
}

export class E02ApiPortRuntime {
  readonly coordinator: E02CapabilityCoordinator;
  readonly state: E02ApiStateStore;
  private readonly initialization: E02ApiPortInitialization;
  private closed = false;

  private constructor(
    initialization: E02ApiPortInitialization,
    state: E02ApiStateStore,
    coordinator: E02CapabilityCoordinator,
  ) {
    this.initialization = jsonClone(initialization);
    this.state = state;
    this.coordinator = coordinator;
  }

  static async open(initializationValue: E02ApiPortInitialization): Promise<E02ApiPortRuntime> {
    const initialization = normalizeInitialization(initializationValue);
    const state = new E02ApiStateStore(initialization.state_path);
    await state.acquire();
    try {
      const restoredState = await state.load();
      const input = runtimeInput(initialization, restoredState);
      const coordinator = await E02CapabilityCoordinator.open(input, {
        checkpoint: async (snapshot) => state.save(snapshot),
        // The API process exposes envelopes for an authenticated transport to
        // poll.  It never decides the response; resume remains TS-owned.
        approvalTransport: async (envelope) => ({
          transport: "e02-api-poll",
          transportRequestId: envelope.requestId,
          accepted: true,
          metadata: {
            canonical_continuation_owner: "typescript",
            python_transport_only: true,
          },
        }),
      });
      await state.save(coordinator.snapshot());
      return new E02ApiPortRuntime(initialization, state, coordinator);
    } catch (error) {
      await state.release();
      throw error;
    }
  }

  async dispatch(requestValue: E02ApiPortRequest): Promise<JsonValue> {
    if (this.closed) throw apiError("e02_api_runtime_closed", "E02 API runtime is closed");
    const request = normalizeRequest(requestValue);
    const payload = asObject(request.payload);
    let recognized = true;
    let result: JsonValue;
    try {
      if (request.operation === "health") result = this.health();
      else if (request.operation === "snapshot") result = this.snapshotProjection(payload);
      else if (request.operation === "tools") result = this.toolsProjection(payload);
      else if (request.operation === "skills") result = this.skillsProjection(payload);
      else if (request.operation === "plugins") result = this.pluginsProjection(payload);
      else if (request.operation === "commands") result = this.commandsProjection(payload);
      else if (request.operation === "permission.get") result = this.permissionProjection(payload);
      else if (request.operation === "permission.enforce") result = await this.permissionEnforce(payload);
      else if (request.operation === "permission.claim") result = this.permissionClaim(payload);
      else if (request.operation === "permission.respond") result = this.permissionRespond(payload, request.request_id);
      else if (request.operation === "permission.cancel") result = this.permissionCancel(payload);
      else if (request.operation === "permission.expire") result = this.permissionExpire();
      else if (request.operation === "permission.policy") result = this.permissionPolicy(payload);
      else if (request.operation === "mcp.http.get") result = transportJson(this.mcpHttpGet(payload));
      else if (request.operation === "mcp.http.post") result = transportJson(await this.mcpHttpPost(payload, request.request_id));
      else if (request.operation === "execute") result = await this.execute(payload, request.request_id);
      else {
        recognized = false;
        throw apiError("e02_api_operation_unknown", `Unknown E02 API operation ${request.operation}`);
      }
      return jsonClone(result);
    } finally {
      // Permission ASK is a real state transition even though execute returns
      // an approval-required error.  Persist every recognized operation, but
      // never checkpoint an unknown request as a fallback decision.
      if (recognized) await this.state.save(this.coordinator.snapshot());
    }
  }

  health(): JsonObject {
    return {
      protocol: E02_API_PROTOCOL_VERSION,
      canonical_entrypoint: "E02CapabilityCoordinator.execute",
      canonical_owner: "typescript",
      process_id: process.pid,
      runtime: this.coordinator.health(),
      state: this.state.health(),
      python_permission_fallback: false,
      python_mcp_fallback: false,
      python_skill_fallback: false,
      python_plugin_fallback: false,
      python_command_fallback: false,
    };
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    try {
      await this.coordinator.close();
      await this.state.save(this.coordinator.snapshot());
    } finally {
      await this.state.release();
    }
  }

  private snapshotProjection(payload: JsonObject): JsonObject {
    const snapshot = this.coordinator.snapshot();
    const requested = stringArray(payload.domains);
    if (!requested.length) return transportObject(snapshot);
    const projection: JsonObject = {
      version: snapshot.version,
      runtime: cloneJson(snapshot.runtime),
      opened: snapshot.opened,
      snapshotHash: snapshot.snapshotHash,
    };
    const source = snapshot as unknown as JsonObject;
    for (const domain of requested) {
      if (domain in source) projection[domain] = cloneJson(source[domain] as JsonValue);
    }
    return projection;
  }

  private toolsProjection(payload: JsonObject): JsonObject {
    const namespace = asString(payload.namespace).trim();
    const tools = this.coordinator.toolSpecs().filter((tool) => {
      if (!namespace) return true;
      return tool.source === namespace || asString(tool.metadata.namespace) === namespace;
    });
    return {
      tools: transportJson(tools),
      count: tools.length,
      route_revision: this.coordinator.routes.snapshot().revision,
      canonical_owner: "typescript",
    };
  }

  private skillsProjection(payload: JsonObject): JsonObject {
    const snapshot = this.coordinator.skills.snapshot();
    const query = asString(payload.query ?? payload.q).trim().toLowerCase();
    const tools = this.coordinator.skills.toolSpecs().filter((tool) => {
      if (!query) return true;
      return `${tool.name} ${tool.purpose}`.toLowerCase().includes(query);
    });
    return {
      skills: transportJson(tools),
      registry: transportJson(snapshot.registry),
      roots: transportJson(snapshot.roots),
      journal: transportJson(snapshot.journal),
      health: this.coordinator.skills.health(),
      body_in_projection: false,
      canonical_owner: "typescript",
    };
  }

  private pluginsProjection(_payload: JsonObject): JsonObject {
    const snapshot = this.coordinator.plugins.snapshot();
    return {
      plugins: transportJson(snapshot),
      tools: transportJson(this.coordinator.plugins.toolSpecs()),
      health: this.coordinator.plugins.health(),
      canonical_owner: "typescript",
    };
  }

  private commandsProjection(_payload: JsonObject): JsonObject {
    const snapshot = this.coordinator.commands.snapshot();
    return {
      commands: transportJson(snapshot),
      tools: transportJson(this.coordinator.commands.toolSpecs()),
      health: this.coordinator.commands.health(),
      dispatch_owner: "typescript",
      python_parser_enabled: false,
    };
  }

  private permissionProjection(payload: JsonObject): JsonObject {
    const view = asString(payload.view).trim().toLowerCase() || "summary";
    const requestedId = asString(payload.request_id).trim();
    const requestedStatus = asString(payload.status).trim();
    const maximum = Math.min(1_000, Math.max(1, nonNegativeInteger(payload.limit, 100)));
    let approvals = this.coordinator.permission.approvals
      .list()
      .filter((envelope) => !requestedStatus || envelope.status === requestedStatus)
      .filter((envelope) => !requestedId || envelope.requestId === requestedId)
      .slice(-maximum)
      .map(safePermissionEnvelope);
    const evaluator = asObject(this.coordinator.permission.snapshot().evaluator);
    const decisions = (Array.isArray(evaluator.decisions) ? evaluator.decisions : [])
      .map((value) => safePermissionDecision(asObject(value)))
      .slice(-maximum);
    if (view === "request" && requestedId && approvals.length === 0) {
      throw apiError("permission_request_not_found", `Permission request ${requestedId} was not found`);
    }
    if (view === "mode") approvals = [];
    return {
      schema: "zyra.e02-permission-api/v1",
      canonical_owner: "typescript.PermissionCoordinator",
      canonical_entrypoint: "E02CapabilityCoordinator.resumePermission",
      python_decision_fallback: false,
      health: this.coordinator.permission.health(),
      mode: transportJson(this.coordinator.permission.evaluator.modes.state),
      rules: view === "mode" || view === "requests" || view === "request"
        ? []
        : transportJson(this.coordinator.permission.evaluator.rules.list()),
      requests: view === "rules" || view === "mode" || view === "decisions" ? [] : transportJson(approvals),
      decisions: view === "decisions" || view === "summary" ? transportJson(decisions) : [],
    };
  }

  private async permissionEnforce(payload: JsonObject): Promise<JsonObject> {
    if (asString(payload.tool_name ?? payload.tool) === "phase2.strongest-control") {
      const requested = asObject(payload.arguments).requested_permissions;
      if (
        asString(payload.namespace) !== "builtin"
        || asString(payload.operation) !== "phase2.strongest.activate"
        || !Array.isArray(requested)
        || requested.length !== 2
        || requested[0] !== "graph.write"
        || requested[1] !== "worker.dispatch"
      ) {
        throw apiError(
          "phase2_strongest_permission_scope_invalid",
          "The managed Phase 2 allowlist accepts only graph.write and worker.dispatch in canonical order",
        );
      }
    }
    const input = permissionInput(payload, this.coordinator.workspaceRoot);
    const enforcement = await this.coordinator.permission.enforce(input);
    const requestId = enforcement.decision.continuationRequestId;
    const approval = requestId
      ? this.coordinator.permission.approvals.get(requestId)
      : null;
    return {
      schema: "zyra.e02-permission-enforcement-receipt/v1",
      decision: transportJson(enforcement.decision),
      allowed: enforcement.allowed,
      blocked: enforcement.blocked,
      pending_approval: enforcement.pendingApproval,
      final_arguments: transportJson(enforcement.finalArguments),
      recovery_input: enforcement.recoveryInput
        ? transportJson(enforcement.recoveryInput)
        : null,
      replan_required: enforcement.replanRequired,
      approval_request: approval ? safePermissionEnvelope(approval) : null,
      state_digest: enforcement.stateDigest,
      canonical_owner: "typescript.PermissionCoordinator",
      canonical_entrypoint: "PermissionCoordinator.enforce",
      python_decision_fallback: false,
    };
  }

  private permissionClaim(payload: JsonObject): JsonObject {
    const input = permissionInput(payload, this.coordinator.workspaceRoot);
    const argumentsDigest = digest(input.arguments);
    const requestedPermitId = asString(payload.permit_id).trim();
    const permits = this.coordinator.executionLedger.snapshot().permits
      .filter((permit) => permit.status === "issued")
      .filter((permit) => !requestedPermitId || permit.permitId === requestedPermitId)
      .filter((permit) => (
        permit.runId === input.runId
        && permit.taskId === input.taskId
        && permit.sessionId === input.sessionId
        && permit.sessionRevision === (input.sessionRevision ?? 0)
        && permit.workerRequestId === input.workerRequestId
        && permit.toolCallId === input.toolCallId
        && permit.toolName === input.toolName
        && permit.namespace === (input.namespace ?? "builtin")
        && permit.argumentsDigest === argumentsDigest
      ))
      .sort((left, right) => left.issuedAt.localeCompare(right.issuedAt));
    const selected = permits.at(-1) ?? null;
    if (!selected) {
      return {
        schema: "zyra.e02-external-permission-claim/v1",
        claimed: false,
        canonical_owner: "typescript.PermissionCoordinator",
        python_decision_fallback: false,
      };
    }
    const evaluator = asObject(this.coordinator.permission.snapshot().evaluator);
    const decision = (Array.isArray(evaluator.decisions) ? evaluator.decisions : [])
      .map((value) => asObject(value))
      .find((value) => asString(value.decisionId) === selected.decisionId);
    if (!decision || asString(decision.canonicalOwner) !== "typescript" || decision.effect !== "allow") {
      throw apiError(
        "permission_external_permit_decision_invalid",
        `External permission permit ${selected.permitId} has no canonical allow decision`,
      );
    }
    const binding = asObject(decision.requestBinding);
    if (
      asString(binding.run_id) !== input.runId
      || asString(binding.task_id) !== input.taskId
      || asString(binding.session_id) !== input.sessionId
      || nonNegativeInteger(binding.session_revision, -1) !== (input.sessionRevision ?? 0)
      || asString(binding.worker_request_id) !== input.workerRequestId
      || asString(binding.tool_call_id) !== input.toolCallId
      || asString(binding.tool_name) !== input.toolName
      || asString(binding.namespace) !== (input.namespace ?? "builtin")
      || asString(binding.server_id) !== (input.serverId ?? "")
      || asString(binding.operation) !== (input.operation ?? "")
      || resolve(asString(binding.workspace_root)) !== resolve(input.workspaceRoot ?? this.coordinator.workspaceRoot)
      || asString(binding.arguments_digest) !== argumentsDigest
      || asString(decision.finalArgumentsDigest) !== argumentsDigest
    ) {
      throw apiError(
        "permission_external_permit_binding_mismatch",
        `External permission permit ${selected.permitId} does not match the exact physical call`,
      );
    }
    const consumed = this.coordinator.executionLedger.consumePermit(selected.permitId, {
      runId: input.runId,
      sessionId: input.sessionId,
      sessionRevision: input.sessionRevision ?? 0,
      workerRequestId: input.workerRequestId,
      toolCallId: input.toolCallId,
      toolName: input.toolName,
      argumentsDigest,
    });
    return {
      schema: "zyra.e02-external-permission-claim/v1",
      claimed: true,
      decision: transportJson(decision),
      final_arguments: transportJson(asObject(decision.finalArguments)),
      permit: transportJson(consumed),
      permit_id: consumed.permitId,
      canonical_owner: "typescript.PermissionCoordinator",
      canonical_entrypoint: "CapabilityExecutionLedger.consumePermit",
      python_decision_fallback: false,
    };
  }

  private permissionRespond(payload: JsonObject, requestId: string): JsonObject {
    const continuationRequestId = asString(payload.request_id).trim();
    const effect = asString(payload.effect).trim().toLowerCase();
    if (!continuationRequestId) {
      throw apiError("permission_request_id_missing", "Permission response requires request_id");
    }
    if (effect !== "allow" && effect !== "deny") {
      throw apiError("permission_response_effect_invalid", "Permission response effect must be allow or deny");
    }
    const envelope = this.coordinator.permission.approvals.get(continuationRequestId);
    if (!envelope) {
      throw apiError("permission_request_not_found", `Permission request ${continuationRequestId} was not found`);
    }
    const metadata = asObject(payload.metadata);
    const consoleResponse = asObject(metadata.console_response);
    let proofResult: ReturnType<typeof verifyPermissionResponseProof> | null = null;
    if (Object.keys(consoleResponse).length > 0) {
      proofResult = verifyPermissionResponseProof(
        envelope,
        consoleResponse,
        {
          requestId: continuationRequestId,
          responseId: asString(payload.response_id).trim(),
          effect,
        },
      );
      if (!proofResult.verified) {
        throw apiError(
          proofResult.failureCode ?? "permission_response_proof_rejected",
          proofResult.failureMessage ?? "permission response proof was rejected",
          {
            request_id: continuationRequestId,
            response_id: asString(payload.response_id).trim(),
            challenge_digest: proofResult.challengeDigest,
            response_digest: proofResult.responseDigest,
          },
        );
      }
    }
    const response: PermissionApprovalResponse = {
      responseId: asString(payload.response_id).trim() || `api-response-${randomUUID()}`,
      requestId: envelope.requestId,
      runId: envelope.runId,
      sessionId: envelope.sessionId,
      sessionRevision: envelope.sessionRevision,
      workerRequestId: envelope.workerRequestId,
      toolCallId: envelope.toolCallId,
      effect,
      responder: asString(payload.responder ?? payload.actor_id).trim() || "api-operator",
      respondedAt: new Date().toISOString(),
      metadata: {
        ...metadata,
        api_request_id: requestId,
        transport: "python-http-forwarder",
        python_decision: false,
        response_proof_verified: proofResult?.verified === true,
        response_proof_digest: proofResult?.responseDigest ?? null,
        response_challenge_digest: proofResult?.challengeDigest ?? null,
      },
    };
    const result = this.coordinator.resumePermission(response);
    return {
      schema: "zyra.e02-permission-decision-receipt/v1",
      accepted: result.enforcement.allowed || result.enforcement.blocked,
      effect,
      request_id: continuationRequestId,
      response_id: response.responseId,
      decision: safePermissionDecision(result.enforcement.decision as unknown as JsonObject),
      permit: result.permit ? transportJson(result.permit) : null,
      permit_id: result.permitId,
      final_arguments_digest: result.enforcement.decision.finalArgumentsDigest,
      state_digest: result.stateDigest,
      response_proof_verified: proofResult?.verified === true,
      response_proof_digest: proofResult?.responseDigest ?? null,
      response_challenge_digest: proofResult?.challengeDigest ?? null,
      canonical_owner: "typescript.PermissionCoordinator",
      python_decision_fallback: false,
    };
  }

  private permissionCancel(payload: JsonObject): JsonObject {
    const requestId = asString(payload.request_id).trim();
    if (!requestId) throw apiError("permission_request_id_missing", "Permission cancellation requires request_id");
    const envelope = this.coordinator.permission.approvals.cancel(
      requestId,
      asString(payload.reason).trim() || "cancelled by API transport",
    );
    return {
      schema: "zyra.e02-permission-cancel-receipt/v1",
      request: safePermissionEnvelope(envelope),
      canonical_owner: "typescript.PermissionCoordinator",
      python_decision_fallback: false,
    };
  }

  private permissionExpire(): JsonObject {
    const expired = this.coordinator.permission.approvals.expire().map(safePermissionEnvelope);
    return {
      schema: "zyra.e02-permission-expiry-receipt/v1",
      expired: transportJson(expired),
      count: expired.length,
      canonical_owner: "typescript.PermissionCoordinator",
      python_decision_fallback: false,
    };
  }

  private permissionPolicy(payload: JsonObject): JsonObject {
    const action = asString(payload.action).trim().toLowerCase();
    const expectedRevision = nonNegativeInteger(payload.expected_revision, -1);
    if (action === "mode") {
      const transition = this.coordinator.permission.transitionMode(
        permissionModeValue(payload.mode),
        {
          actor: asString(payload.actor_id).trim() || "api-operator",
          reason: asString(payload.reason).trim() || "permission API mode update",
          ...(expectedRevision >= 0 ? { expectedRevision } : {}),
          metadata: {
            transport: "python-http-forwarder",
            python_decision: false,
          },
        },
      );
      return {
        schema: "zyra.e02-permission-policy-receipt/v1",
        action,
        transition: transportJson(transition),
        canonical_owner: "typescript.PermissionCoordinator",
        python_decision_fallback: false,
      };
    }
    if (action === "replace_rules") {
      if (!Array.isArray(payload.rules)) {
        throw apiError("permission_rules_revision_required", "Rule updates require a complete rules array");
      }
      const commit = this.coordinator.permission.replaceRules(
        payload.rules as Array<string | JsonObject>,
        expectedRevision >= 0 ? expectedRevision : this.coordinator.permission.evaluator.rules.revision,
        {
          actor: asString(payload.actor_id).trim() || "api-operator",
          transport: "python-http-forwarder",
          python_decision: false,
        },
      );
      return {
        schema: "zyra.e02-permission-policy-receipt/v1",
        action,
        commit: transportJson(commit),
        canonical_owner: "typescript.PermissionCoordinator",
        python_decision_fallback: false,
      };
    }
    if (action === "remove_rule") {
      const ruleId = asString(payload.rule_id).trim();
      if (!ruleId) throw apiError("permission_rule_id_missing", "Rule removal requires rule_id");
      const rules = this.coordinator.permission.evaluator.rules.list();
      if (!rules.some((rule) => rule.ruleId === ruleId)) {
        throw apiError("permission_rule_not_found", `Permission rule ${ruleId} was not found`);
      }
      const commit = this.coordinator.permission.replaceRules(
        rules.filter((rule) => rule.ruleId !== ruleId),
        expectedRevision >= 0 ? expectedRevision : this.coordinator.permission.evaluator.rules.revision,
        {
          actor: asString(payload.actor_id).trim() || "api-operator",
          removed_rule_id: ruleId,
          transport: "python-http-forwarder",
          python_decision: false,
        },
      );
      return {
        schema: "zyra.e02-permission-policy-receipt/v1",
        action,
        commit: transportJson(commit),
        canonical_owner: "typescript.PermissionCoordinator",
        python_decision_fallback: false,
      };
    }
    throw apiError("permission_policy_action_unknown", `Unknown permission policy action ${action || "<empty>"}`);
  }

  private mcpHttpGet(payload: JsonObject): HttpProjection {
    const parts = stringArray(payload.parts);
    if (parts[0] !== "mcp") throw apiError("e02_api_mcp_path_invalid", "MCP API path must begin with mcp");
    this.coordinator.mcp.assertSourceRuntimeEnabled();
    const snapshot = this.coordinator.mcp.snapshot();
    const tail = parts.slice(1);
    let body: JsonObject;
    if (!tail.length || tail[0] === "health") {
      body = {
        health: this.coordinator.mcp.health(),
        runtime: this.coordinator.health(),
        state_owner: "McpRuntimeCoordinator",
        default_entrypoint: "E02CapabilityCoordinator.execute",
      };
    } else if (tail[0] === "config") body = { config: transportJson(snapshot.config) };
    else if (tail[0] === "servers" && tail.length === 1) {
      body = {
        servers: transportJson(this.coordinator.mcp.config.list()),
        connections: transportJson(this.coordinator.mcp.connections.list()),
      };
    } else if (tail[0] === "servers" && tail[1]) {
      const serverId = tail[1];
      const config = this.coordinator.mcp.config.get(serverId);
      if (!config) return httpProjection(404, { error: "mcp_server_not_found", server_id: serverId });
      body = {
        server: transportJson(config),
        connection: transportJson(this.coordinator.mcp.connections.get(serverId)),
        catalog: transportJson(this.coordinator.mcp.catalog.get(serverId)),
      };
    } else if (tail[0] === "catalog") body = { catalog: transportJson(snapshot.catalog) };
    else if (tail[0] === "tools") body = { tools: transportJson(this.coordinator.mcp.toolSpecs()) };
    else if (tail[0] === "resources") {
      body = {
        resources: transportJson(snapshot.catalog.servers.flatMap((server) => server.resources.map((resource) => ({
          server_id: server.serverId,
          ...resource,
        })))),
        resource_templates: transportJson(snapshot.catalog.servers.flatMap((server) => server.resourceTemplates.map((resource) => ({
          server_id: server.serverId,
          ...resource,
        })))),
      };
    } else if (tail[0] === "prompts") {
      body = {
        prompts: transportJson(snapshot.catalog.servers.flatMap((server) => server.prompts.map((prompt) => ({
          server_id: server.serverId,
          ...prompt,
        })))),
      };
    } else if (tail[0] === "elicitations") body = { elicitations: transportJson(snapshot.elicitation) };
    else return httpProjection(404, { error: "mcp_route_not_found", path: parts.join("/") });
    return httpProjection(200, body);
  }

  private async mcpHttpPost(payload: JsonObject, requestId: string): Promise<HttpProjection> {
    const parts = stringArray(payload.parts);
    const body = asObject(payload.body);
    if (parts[0] !== "mcp") throw apiError("e02_api_mcp_path_invalid", "MCP API path must begin with mcp");
    if (parts.length === 2 && parts[1] === "resources" && body.action === "read") {
      return this.executeHttpCapability("mcp_read_resource", body, requestId, 200);
    }
    if (parts.length === 3 && parts[1] === "resources" && parts[2] === "read") {
      return this.executeHttpCapability("mcp_read_resource", body, requestId, 200);
    }
    if (parts.length === 3 && parts[1] === "prompts" && parts[2] === "get") {
      return this.executeHttpCapability("mcp_get_prompt", body, requestId, 200);
    }
    if (parts.length === 2 && parts[1] === "reload") {
      return this.executeHttpCapability("e02_control", {
        operation: "mcp.reload",
        payload: {
          actor: asString(body.actor_id ?? body.actor) || "api",
          reason: asString(body.reason) || "HTTP MCP reload",
        },
        idempotency_key: asString(body.idempotency_key) || requestId,
      }, requestId, 200);
    }
    return httpProjection(409, {
      error: "mcp_mutation_requires_typescript_control_operation",
      message: "This MCP mutation has no registered E02 control operation and was not executed.",
      path: parts.join("/"),
      canonical_permission_owner: "typescript",
      python_mutation_fallback: false,
    });
  }

  private async executeHttpCapability(
    toolName: string,
    argumentsValue: JsonObject,
    requestId: string,
    successStatus: number,
  ): Promise<HttpProjection> {
    try {
      const receipt = await this.coordinator.execute(
        toolName,
        argumentsValue,
        executionContext(this.coordinator, requestId, argumentsValue),
      );
      return httpProjection(successStatus, {
        ok: true,
        receipt: transportJson(receipt),
        canonical_entrypoint: "E02CapabilityCoordinator.execute",
      });
    } catch (error) {
      const serialized = serializeError(error);
      const code = asString(serialized.code);
      const status = code.includes("permission") ? 403 : code.includes("not_found") ? 404 : 409;
      return httpProjection(status, {
        ok: false,
        error: code || "e02_capability_failed",
        detail: serialized,
        canonical_permission_owner: "typescript",
        python_fallback: false,
      });
    }
  }

  private async execute(payload: JsonObject, requestId: string): Promise<JsonObject> {
    const toolName = asString(payload.tool_name ?? payload.tool).trim();
    if (!toolName) throw apiError("e02_api_tool_name_missing", "E02 execute requires tool_name");
    const argumentsValue = asObject(payload.arguments);
    const context = executionContext(this.coordinator, requestId, payload);
    const receipt = await this.coordinator.execute(toolName, argumentsValue, context);
    return {
      receipt: transportJson(receipt),
      snapshot_hash: this.coordinator.snapshot().snapshotHash,
      canonical_entrypoint: "E02CapabilityCoordinator.execute",
    };
  }
}

export async function runE02ApiPort(): Promise<void> {
  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  let runtime: E02ApiPortRuntime | null = null;
  try {
    for await (const line of lines) {
      if (!line.trim()) continue;
      let frame: JsonObject;
      try {
        frame = asObject(JSON.parse(line));
      } catch (error) {
        writeFrame(errorFrame("", apiError("e02_api_frame_json_invalid", "E02 API frame is not valid JSON", serializeError(error))));
        continue;
      }
      const requestId = asString(frame.request_id);
      try {
        if (!runtime) {
          if (frame.type !== "initialize") {
            throw apiError("e02_api_initialize_required", "The first E02 API frame must initialize the runtime");
          }
          runtime = await E02ApiPortRuntime.open(frame as unknown as E02ApiPortInitialization);
          writeFrame({
            type: "ready",
            request_id: requestId,
            ok: true,
            protocol: E02_API_PROTOCOL_VERSION,
            payload: runtime.health(),
          });
          continue;
        }
        if (frame.type !== "request") throw apiError("e02_api_request_type_invalid", "Expected an E02 API request frame");
        const payload = await runtime.dispatch(frame as unknown as E02ApiPortRequest);
        writeFrame({
          type: "response",
          request_id: requestId,
          ok: true,
          protocol: E02_API_PROTOCOL_VERSION,
          payload,
        });
      } catch (error) {
        writeFrame(errorFrame(requestId, error));
      }
    }
  } finally {
    await runtime?.close();
    lines.close();
  }
}

function runtimeInput(
  initialization: E02ApiPortInitialization,
  restoredState: E02CapabilityCoordinatorSnapshot | null,
): RuntimeRunInput {
  const configuredConstraints = asObject(initialization.runtime_constraints);
  const runtimeConstraints = {
    ...configuredConstraints,
    workspaceRoot: initialization.workspace_root,
    projectRoot: asString(configuredConstraints.projectRoot) || initialization.workspace_root,
    sealedAutonomous: initialization.sealed_autonomous === true,
    watchSkills: true,
    watchPlugins: true,
  };
  return {
    runId: "e02-api-control-run",
    taskId: "e02-api-control-task",
    nodeId: "e02-api-control-node",
    workerRequestId: "e02-api-control-worker",
    sessionId: "e02-api-control-session",
    messages: [],
    turns: [],
    tools: [],
    config: {
      runtimeConstraints,
      permissionPolicy: {
        version: "zyra.e02-api-permission-policy/v1",
        canonical_owner: "typescript",
        mode: initialization.permission_mode || (initialization.sealed_autonomous ? "sealed" : "default"),
        rules: [{
          rule_id: "managed-phase2-strongest-control",
          effect: "allow",
          source: "managed",
          tool_pattern: "phase2.strongest-control",
          namespace_pattern: "builtin",
          operation_pattern: "phase2.strongest.activate",
          argument_pattern: "*",
          priority: 1000,
          enabled: true,
          max_uses: null,
          use_count: 0,
          scope: { workspace_root: initialization.workspace_root },
          reason: "managed low-risk allowlist for the deterministic Phase 2 control path",
          metadata: {
            canonical_owner: "typescript.PermissionCoordinator",
            low_risk_allowlist: true,
            extra_permissions_denied: true,
          },
        }],
        python_policy_fallback: false,
      },
    },
    restoredState: restoredState as unknown as JsonObject | null,
    metadata: {
      surface: "api-control-port",
      artifact_root: initialization.artifact_root ?? dirname(initialization.state_path),
      canonical_capability_owner: "typescript",
      python_decision_fallback: false,
    },
  };
}

function executionContext(
  coordinator: E02CapabilityCoordinator,
  requestId: string,
  payload: JsonObject,
): E02ExecutionContext {
  const argumentsValue = asObject(payload.arguments);
  const toolName = asString(payload.tool_name ?? payload.tool);
  const canonicalDomain = toolName ? coordinator.routes.domain(toolName) : null;
  return {
    runId: coordinator.runtime.runId,
    taskId: coordinator.runtime.taskId,
    sessionId: coordinator.runtime.sessionId,
    sessionRevision: nonNegativeInteger(payload.session_revision, 0),
    workerRequestId: coordinator.runtime.workerRequestId,
    toolCallId: asString(payload.tool_call_id) || `api:${requestId}`,
    namespace: asString(payload.namespace ?? argumentsValue.namespace) || canonicalDomain || "control",
    serverId: asString(payload.server_id ?? argumentsValue.server_id),
    commandName: asString(payload.command_name ?? argumentsValue.command_name),
    resourceUri: asString(payload.uri ?? payload.resource_uri ?? argumentsValue.uri ?? argumentsValue.resource_uri),
    operation: asString(payload.operation ?? argumentsValue.operation),
    permitId: asString(payload.permit_id) || null,
    metadata: {
      actor: asString(payload.actor_id ?? payload.actor) || "api",
      correlation_id: asString(payload.correlation_id) || requestId,
      api_request_id: requestId,
      python_transport_only: true,
    },
  };
}

function permissionInput(payload: JsonObject, workspaceRoot: string): E02AuthorizationInput {
  const input: E02AuthorizationInput = {
    runId: asString(payload.run_id).trim(),
    taskId: asString(payload.task_id).trim(),
    sessionId: asString(payload.session_id).trim(),
    sessionRevision: nonNegativeInteger(payload.session_revision, 0),
    workerRequestId: asString(payload.worker_request_id).trim(),
    toolCallId: asString(payload.tool_call_id).trim(),
    toolName: asString(payload.tool_name).trim(),
    namespace: asString(payload.namespace).trim() || "builtin",
    serverId: asString(payload.server_id).trim(),
    commandName: asString(payload.command_name).trim(),
    resourceUri: asString(payload.resource_uri ?? payload.uri).trim(),
    operation: asString(payload.operation).trim(),
    workspaceRoot: resolve(asString(payload.workspace_root).trim() || workspaceRoot),
    arguments: asObject(payload.arguments),
    metadata: {
      ...asObject(payload.metadata),
      transport: "python-typed-effect-port",
      python_decision: false,
    },
    awaitApprovalDelivery: payload.await_approval_delivery !== false,
  };
  if (
    !input.runId
    || !input.taskId
    || !input.sessionId
    || !input.workerRequestId
    || !input.toolCallId
    || !input.toolName
    || !input.operation
  ) {
    throw apiError(
      "permission_external_identity_incomplete",
      "External permission enforcement requires exact run, task, session, worker, tool-call, tool, and operation identity",
    );
  }
  return input;
}

function normalizeInitialization(value: E02ApiPortInitialization): E02ApiPortInitialization {
  const result = jsonClone(value);
  if (result.type !== "initialize" || !result.request_id || !result.workspace_root || !result.state_path) {
    throw apiError("e02_api_initialization_invalid", "E02 API initialization requires request, workspace, and state identity");
  }
  result.workspace_root = resolve(result.workspace_root);
  result.state_path = resolve(result.state_path);
  result.artifact_root = result.artifact_root ? resolve(result.artifact_root) : dirname(result.state_path);
  return result;
}

function normalizeRequest(value: E02ApiPortRequest): E02ApiPortRequest {
  const result = jsonClone(value);
  if (result.type !== "request" || !result.request_id || !result.operation) {
    throw apiError("e02_api_request_invalid", "E02 API request requires request_id and operation");
  }
  return result;
}

function httpProjection(status: number, body: JsonObject): HttpProjection {
  return { status, body, headers: cloneJson(NO_STORE_HEADERS) };
}

function writeFrame(frame: E02ApiPortResponse): void {
  process.stdout.write(`${JSON.stringify(frame)}\n`);
}

function errorFrame(requestId: string, error: unknown): E02ApiPortResponse {
  return {
    type: "error",
    request_id: requestId,
    ok: false,
    protocol: E02_API_PROTOCOL_VERSION,
    error: serializeError(error),
  };
}

function serializeError(error: unknown): JsonObject {
  if (error && typeof error === "object") {
    const value = error as Record<string, unknown>;
    return {
      name: asString(value.name) || "Error",
      code: asString(value.code) || "e02_api_runtime_error",
      message: asString(value.message) || String(error),
      detail: asObject(value.detail ?? value.details),
    };
  }
  return { name: "Error", code: "e02_api_runtime_error", message: String(error), detail: {} };
}

function apiError(code: string, message: string, detail: JsonObject = {}): Error {
  const error = new Error(message) as Error & { code: string; detail: JsonObject };
  error.name = "E02ApiPortError";
  error.code = code;
  error.detail = cloneJson(detail);
  return error;
}

function safePermissionEnvelope(value: unknown): JsonObject {
  const envelope = asObject(value);
  const metadata = asObject(envelope.metadata);
  const challenge = publicPermissionResponseChallenge(
    permissionEnvelopeBinding(envelope),
  );
  return {
    envelope_id: asString(envelope.envelopeId),
    request_id: asString(envelope.requestId),
    decision_id: asString(envelope.decisionId),
    run_id: asString(envelope.runId),
    task_id: asString(envelope.taskId),
    session_id: asString(envelope.sessionId),
    session_revision: nonNegativeInteger(envelope.sessionRevision, 0),
    worker_request_id: asString(envelope.workerRequestId),
    tool_call_id: asString(envelope.toolCallId),
    prompt: asString(envelope.prompt),
    reason: asString(envelope.reason),
    tool_name: asString(envelope.toolName),
    namespace: asString(envelope.namespace),
    server_id: asString(envelope.serverId),
    operation: asString(envelope.operation),
    arguments_digest: asString(envelope.argumentsDigest),
    request_binding: transportJson(asObject(envelope.requestBinding)),
    policy_revision: nonNegativeInteger(envelope.policyRevision, 0),
    mode_revision: nonNegativeInteger(envelope.modeRevision, 0),
    expires_at: asString(envelope.expiresAt),
    status: asString(envelope.status),
    delivery_attempt: nonNegativeInteger(envelope.deliveryAttempt, 0),
    delivery_receipt_id: asString(envelope.deliveryReceiptId) || null,
    created_at: asString(envelope.createdAt),
    updated_at: asString(envelope.updatedAt),
    request_fingerprint: asString(metadata.request_fingerprint),
    response_challenge: challenge,
    response_id: asString(metadata.response_id) || null,
    response_effect: asString(metadata.response_effect) || null,
    response_accepted: metadata.response_accepted === true,
    responder: asString(metadata.responder) || null,
    final_arguments_projected: false,
    canonical_owner: "typescript.PermissionCoordinator",
  };
}

function permissionEnvelopeBinding(
  envelope: JsonObject,
): PermissionResponseEnvelopeBinding {
  return {
    envelopeId: asString(envelope.envelopeId),
    requestId: asString(envelope.requestId),
    runId: asString(envelope.runId),
    taskId: asString(envelope.taskId),
    sessionId: asString(envelope.sessionId),
    sessionRevision: nonNegativeInteger(envelope.sessionRevision, 0),
    workerRequestId: asString(envelope.workerRequestId),
    toolCallId: asString(envelope.toolCallId),
    argumentsDigest: asString(envelope.argumentsDigest),
    policyRevision: nonNegativeInteger(envelope.policyRevision, 0),
    modeRevision: nonNegativeInteger(envelope.modeRevision, 0),
    expiresAt: asString(envelope.expiresAt),
    metadata: transportObject(envelope.metadata),
  };
}

function safePermissionDecision(value: unknown): JsonObject {
  const decision = asObject(value);
  return {
    decision_id: asString(decision.decisionId),
    effect: asString(decision.effect),
    reason_code: asString(decision.reasonCode),
    reason: asString(decision.reason),
    risk: asString(decision.risk),
    policy_revision: nonNegativeInteger(decision.policyRevision, 0),
    mode_revision: nonNegativeInteger(decision.modeRevision, 0),
    request_fingerprint: asString(decision.requestFingerprint),
    original_arguments_digest: asString(decision.originalArgumentsDigest),
    final_arguments_digest: asString(decision.finalArgumentsDigest),
    human_intervention_count: nonNegativeInteger(decision.humanInterventionCount, 0),
    continuation_request_id: asString(decision.continuationRequestId) || null,
    evaluated_at: asString(decision.evaluatedAt),
    request_binding: transportJson(asObject(decision.requestBinding)),
    final_arguments_projected: false,
    canonical_owner: "typescript.PermissionCoordinator",
  };
}

function permissionModeValue(value: JsonValue | undefined): PermissionMode {
  const aliases: Record<string, PermissionMode> = {
    accept_edits: "acceptEdits",
    dont_ask: "dontAsk",
    bypass: "bypassPermissions",
    bypass_permissions: "bypassPermissions",
    autonomous: "auto",
  };
  const text = asString(value).trim();
  const mode = aliases[text] ?? text;
  if (!["default", "acceptEdits", "dontAsk", "plan", "bypassPermissions", "auto", "sealed"].includes(mode)) {
    throw apiError("permission_mode_invalid", `Unsupported permission mode ${text || "<empty>"}`);
  }
  return mode as PermissionMode;
}

function stringArray(value: JsonValue | undefined): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && item.length > 0)
    : [];
}

function nonNegativeInteger(value: JsonValue | undefined, fallback: number): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : fallback;
}

function positiveInteger(value: JsonValue | undefined, fallback: number): number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0 ? value : fallback;
}

function processIsAlive(pid: number): boolean {
  if (pid === -1) return true;
  if (!Number.isSafeInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return isNodeError(error, "EPERM");
  }
}

function isNodeError(error: unknown, code: string): boolean {
  return Boolean(error && typeof error === "object" && (error as NodeJS.ErrnoException).code === code);
}

function jsonClone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function transportJson(value: unknown): JsonValue {
  return JSON.parse(JSON.stringify(value)) as JsonValue;
}

function transportObject(value: unknown): JsonObject {
  return asObject(transportJson(value));
}
