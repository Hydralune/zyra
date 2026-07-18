import { normalize } from "node:path";
import { isAbsolute, relative, resolve, sep } from "node:path";

import {
  assertDigest,
  createId,
  digest,
  E03RuntimeError,
  type E03Clock,
  type E03IsolationReceipt,
  type E03IsolationRequest,
  type E03TaskState,
  type IsolationMode,
  SystemE03Clock,
} from "../e03/contracts.ts";

export interface IsolationPrepareInput {
  mode: IsolationMode;
  workspaceRoot: string;
  baseRevision: string;
  branchName?: string;
  expectedArtifacts?: readonly string[];
  allowDirtyBaseline?: boolean;
  allowNestedRepository?: boolean;
  idempotencyKey: string;
}

export class IsolationRequestRuntime {
  private readonly policy = new WorkspaceIsolationPolicy();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  prepare(
    task: E03TaskState,
    input: IsolationPrepareInput,
  ): E03IsolationRequest {
    if (!task.scope.isolationModes.includes(input.mode))
      throw new E03RuntimeError(
        "isolation_not_permitted",
        `task scope denies ${input.mode}`,
      );
    const decision = this.policy.assert(
      task,
      {
        permittedRoots: task.scope.workspaceRoots,
        permittedModes: task.scope.isolationModes,
        allowDirtyBaseline: true,
        allowNestedRepository: true,
      },
      input,
    );
    const workspaceRoot = this.validateWorkspace(
      decision.workspaceRoot,
      task.scope.workspaceRoots,
    );
    const baseRevision = input.baseRevision.trim();
    if (input.mode === "worktree" && !baseRevision)
      throw new E03RuntimeError(
        "missing_base_revision",
        "worktree isolation requires a base revision",
      );
    const idempotencyKey = input.idempotencyKey.trim();
    if (!idempotencyKey)
      throw new E03RuntimeError(
        "missing_idempotency_key",
        "isolation request requires an idempotency key",
      );
    const payload = {
      requestId: `isolation-${digest({ taskId: task.identity.taskId, idempotencyKey }).slice(0, 32)}`,
      taskId: task.identity.taskId,
      leaseId: task.identity.leaseId,
      mode: input.mode,
      workspaceRoot,
      baseRevision,
      branchName: sanitizeBranch(
        input.branchName ??
          `zyra/${task.identity.taskId}/${task.identity.attempt}`,
      ),
      expectedArtifacts: [...new Set(input.expectedArtifacts ?? [])].map(
        (path) => validateRelativeArtifact(path),
      ),
      allowDirtyBaseline: input.allowDirtyBaseline === true,
      allowNestedRepository: input.allowNestedRepository === true,
      idempotencyKey,
      preparedAt: this.clock.now(),
    };
    return { ...payload, digest: digest(payload) };
  }

  validateWorkspace(
    candidate: string,
    permittedRoots: readonly string[],
  ): string {
    if (!candidate.trim())
      throw new E03RuntimeError("empty_workspace", "workspace root is empty");
    const workspace = resolve(candidate);
    if (!isAbsolute(workspace))
      throw new E03RuntimeError(
        "relative_workspace",
        "workspace root must be absolute",
      );
    const permitted = permittedRoots.map((root) => resolve(root));
    const accepted = permitted.some(
      (root) => workspace === root || workspace.startsWith(`${root}${sep}`),
    );
    if (!accepted)
      throw new E03RuntimeError(
        "workspace_escape",
        `workspace ${workspace} escapes permitted roots`,
        { workspace, permitted },
      );
    return workspace;
  }

  recordReceipt(
    request: E03IsolationRequest,
    receipt: E03IsolationReceipt,
  ): E03IsolationReceipt {
    this.validateRequest(request);
    const { digest: receiptDigest, ...payload } = receipt;
    assertDigest(
      payload,
      receiptDigest,
      `isolation-receipt:${receipt.receiptId}`,
    );
    if (
      receipt.requestId !== request.requestId ||
      receipt.taskId !== request.taskId ||
      receipt.leaseId !== request.leaseId
    )
      throw new E03RuntimeError(
        "isolation_receipt_identity",
        "isolation receipt identity differs from request",
      );
    if (receipt.accepted && !receipt.workspacePath)
      throw new E03RuntimeError(
        "isolation_receipt_workspace",
        "accepted isolation receipt lacks workspace path",
      );
    if (receipt.accepted) {
      const workspacePath = resolve(receipt.workspacePath);
      const base = resolve(request.workspaceRoot);
      if (
        request.mode !== "remote" &&
        workspacePath !== base &&
        !workspacePath.startsWith(`${base}${sep}`) &&
        request.mode !== "worktree"
      )
        throw new E03RuntimeError(
          "isolation_receipt_escape",
          "physical workspace receipt escaped requested root",
        );
    }
    this.policy.assertReceipt(request, receipt);
    if (receipt.dirtyBaseline && !request.allowDirtyBaseline)
      throw new E03RuntimeError(
        "dirty_baseline",
        "physical port found a dirty baseline that request forbids",
      );
    if (receipt.nestedRepository && !request.allowNestedRepository)
      throw new E03RuntimeError(
        "nested_repository",
        "physical port found a nested repository that request forbids",
      );
    return structuredClone(receipt);
  }

  commit(
    task: E03TaskState,
    request: E03IsolationRequest,
    receipt: E03IsolationReceipt,
  ): E03TaskState {
    const accepted = this.recordReceipt(request, receipt);
    if (!accepted.accepted)
      throw new E03RuntimeError(
        "isolation_rejected",
        accepted.error || "physical isolation request was rejected",
      );
    if (
      task.identity.taskId !== request.taskId ||
      task.identity.leaseId !== request.leaseId
    )
      throw new E03RuntimeError(
        "isolation_task_identity",
        "task identity differs from isolation request",
      );
    const { sealTask } = isolationHelpers;
    return sealTask({
      ...task,
      isolation: request,
      isolationReceipt: accepted,
      sequence: task.sequence + 1,
      updatedAt: this.clock.now(),
      checksum: "",
    });
  }

  validateRequest(request: E03IsolationRequest): void {
    const { digest: requestDigest, ...payload } = request;
    assertDigest(
      payload,
      requestDigest,
      `isolation-request:${request.requestId}`,
    );
    if (
      !request.requestId ||
      !request.taskId ||
      !request.leaseId ||
      !request.idempotencyKey
    )
      throw new E03RuntimeError(
        "invalid_isolation_request",
        "isolation request identity is incomplete",
      );
  }
}

const isolationHelpers = {
  sealTask: (task: E03TaskState): E03TaskState => {
    const { checksum: _checksum, ...payload } = task;
    return { ...payload, checksum: digest(payload) };
  },
};

function sanitizeBranch(value: string): string {
  const normalized = value
    .trim()
    .replaceAll("\\", "/")
    .replace(/[^A-Za-z0-9._/-]+/g, "-")
    .replace(/\.{2,}/g, ".")
    .replace(/^[/.-]+|[/.-]+$/g, "");
  if (
    !normalized ||
    normalized.length > 240 ||
    normalized.includes("@{") ||
    normalized.endsWith(".lock")
  )
    throw new E03RuntimeError(
      "invalid_worktree_branch",
      `invalid worktree branch ${value}`,
    );
  return normalized;
}

function validateRelativeArtifact(value: string): string {
  const normalized = value.trim().replaceAll("\\", "/");
  if (
    !normalized ||
    normalized.startsWith("/") ||
    /^[A-Za-z]:/.test(normalized) ||
    normalized.split("/").includes("..")
  )
    throw new E03RuntimeError(
      "artifact_path_escape",
      `expected artifact path ${value} must be workspace-relative`,
    );
  return normalized;
}

export interface WorkspacePolicyInput {
  permittedRoots: readonly string[];
  deniedRoots?: readonly string[];
  permittedModes: readonly IsolationMode[];
  allowDirtyBaseline: boolean;
  allowNestedRepository: boolean;
  maximumPathLength?: number;
  maximumArtifacts?: number;
}

export interface WorkspacePolicyDecision {
  accepted: boolean;
  code: string;
  workspaceRoot: string;
  relativeRoot: string;
  mode: IsolationMode;
  effectiveDirtyBaseline: boolean;
  effectiveNestedRepository: boolean;
  policyDigest: string;
  findings: string[];
}

export class WorkspaceIsolationPolicy {
  evaluate(
    task: E03TaskState,
    input: WorkspacePolicyInput,
    request: {
      workspaceRoot: string;
      mode: IsolationMode;
      allowDirtyBaseline?: boolean;
      allowNestedRepository?: boolean;
      expectedArtifacts?: readonly string[];
    },
  ): WorkspacePolicyDecision {
    const findings: string[] = [];
    const maximumPathLength = input.maximumPathLength ?? 1024;
    const maximumArtifacts = input.maximumArtifacts ?? 10_000;
    const rawWorkspaceRoot = request.workspaceRoot.trim();
    const workspaceRoot = resolve(rawWorkspaceRoot || ".");
    if (!request.workspaceRoot.trim()) findings.push("empty_workspace");
    if (rawWorkspaceRoot && !isAbsolute(rawWorkspaceRoot))
      findings.push("relative_workspace");
    if (workspaceRoot.length > maximumPathLength)
      findings.push("workspace_path_too_long");
    const permitted = input.permittedRoots.map((root) => resolve(root));
    const denied = (input.deniedRoots ?? []).map((root) => resolve(root));
    const owningRoot = permitted
      .filter((root) => contains(root, workspaceRoot))
      .sort((left, right) => right.length - left.length)[0];
    if (!owningRoot) findings.push("workspace_escape");
    if (denied.some((root) => contains(root, workspaceRoot)))
      findings.push("workspace_denied");
    if (
      !input.permittedModes.includes(request.mode) ||
      !task.scope.isolationModes.includes(request.mode)
    )
      findings.push("isolation_not_permitted");
    const effectiveDirtyBaseline =
      request.allowDirtyBaseline === true && input.allowDirtyBaseline;
    const effectiveNestedRepository =
      request.allowNestedRepository === true && input.allowNestedRepository;
    if (request.allowDirtyBaseline && !input.allowDirtyBaseline)
      findings.push("dirty_baseline_policy_denied");
    if (request.allowNestedRepository && !input.allowNestedRepository)
      findings.push("nested_repository_policy_denied");
    const artifacts = request.expectedArtifacts ?? [];
    if (artifacts.length > maximumArtifacts)
      findings.push("expected_artifact_limit");
    if (new Set(artifacts).size !== artifacts.length)
      findings.push("duplicate_expected_artifact");
    for (const artifact of artifacts) {
      const normalized = normalizeArtifact(artifact);
      if (
        !normalized ||
        normalized.startsWith("../") ||
        normalized === ".." ||
        isAbsolute(normalized) ||
        /^[A-Za-z]:/.test(normalized)
      )
        findings.push(`artifact_path_escape:${artifact}`);
    }
    if (request.mode === "none" && workspaceRoot !== owningRoot)
      findings.push("none_isolation_requires_root");
    if (
      request.mode === "worktree" &&
      task.executionMode !== "background" &&
      task.scope.workspaceRoots.length > 1
    )
      findings.push("ambiguous_foreground_worktree");
    if (
      (request.mode === "sandbox" || request.mode === "remote") &&
      !task.scope.allowBackground
    )
      findings.push("isolated_background_not_permitted");
    const relativeRoot = owningRoot
      ? relative(owningRoot, workspaceRoot).replaceAll("\\", "/") || "."
      : "";
    const policyPayload = {
      taskId: task.identity.taskId,
      leaseId: task.identity.leaseId,
      permitted,
      denied,
      workspaceRoot,
      relativeRoot,
      mode: request.mode,
      effectiveDirtyBaseline,
      effectiveNestedRepository,
      expectedArtifacts: [...artifacts].sort(),
    };
    return {
      accepted: findings.length === 0,
      code: findings[0] ?? "workspace_policy_accepted",
      workspaceRoot,
      relativeRoot,
      mode: request.mode,
      effectiveDirtyBaseline,
      effectiveNestedRepository,
      policyDigest: digest(policyPayload),
      findings,
    };
  }

  assert(
    task: E03TaskState,
    input: WorkspacePolicyInput,
    request: {
      workspaceRoot: string;
      mode: IsolationMode;
      allowDirtyBaseline?: boolean;
      allowNestedRepository?: boolean;
      expectedArtifacts?: readonly string[];
    },
  ): WorkspacePolicyDecision {
    const decision = this.evaluate(task, input, request);
    if (!decision.accepted)
      throw new E03RuntimeError(
        decision.code,
        `workspace isolation policy rejected: ${decision.findings.join(", ")}`,
        {
          findings: decision.findings,
          workspaceRoot: decision.workspaceRoot,
          mode: decision.mode,
          policyDigest: decision.policyDigest,
        },
      );
    return decision;
  }

  assertReceipt(
    request: E03IsolationRequest,
    receipt: {
      workspacePath: string;
      dirtyBaseline: boolean;
      nestedRepository: boolean;
      accepted: boolean;
      error: string;
    },
  ): void {
    if (!receipt.accepted) {
      if (!receipt.error.trim())
        throw new E03RuntimeError(
          "isolation_rejection_reason_missing",
          "rejected isolation receipt has no error",
        );
      return;
    }
    if (!receipt.workspacePath.trim())
      throw new E03RuntimeError(
        "isolation_workspace_missing",
        "accepted isolation receipt has no workspace path",
      );
    if (
      request.mode !== "remote" &&
      request.mode !== "worktree" &&
      !contains(resolve(request.workspaceRoot), resolve(receipt.workspacePath))
    )
      throw new E03RuntimeError(
        "isolation_receipt_escape",
        "physical workspace escaped the requested root",
      );
    if (receipt.dirtyBaseline && !request.allowDirtyBaseline)
      throw new E03RuntimeError(
        "dirty_baseline",
        "physical workspace is dirty but request forbids it",
      );
    if (receipt.nestedRepository && !request.allowNestedRepository)
      throw new E03RuntimeError(
        "nested_repository",
        "physical workspace contains nested repository but request forbids it",
      );
  }
}

function contains(root: string, candidate: string): boolean {
  return candidate === root || candidate.startsWith(`${root}${sep}`);
}

function normalizeArtifact(value: string): string {
  return normalize(value.trim()).replaceAll("\\", "/");
}

export type WorkspaceAccessKind =
  | "read"
  | "write"
  | "execute"
  | "create"
  | "delete"
  | "rename"
  | "artifact";

export interface WorkspacePathGrant {
  grantId: string;
  taskId: string;
  sessionId: string;
  leaseId: string;
  workspaceRoot: string;
  path: string;
  relativePath: string;
  access: WorkspaceAccessKind;
  recursive: boolean;
  allowSymlink: boolean;
  expiresAt: string;
  revision: number;
  reason: string;
  issuedAt: string;
  digest: string;
}

export interface WorkspacePathDecision {
  accepted: boolean;
  code: string;
  taskId: string;
  grantId: string;
  requestedPath: string;
  resolvedPath: string;
  relativePath: string;
  access: WorkspaceAccessKind;
  leaseMatched: boolean;
  revisionMatched: boolean;
  expired: boolean;
  escaped: boolean;
  symlinkDenied: boolean;
  decidedAt: string;
  digest: string;
}

function assertWorkspacePathGrant(grant: WorkspacePathGrant): void {
  const { digest: checksum, ...payload } = grant;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "workspace_grant_checksum",
      `workspace grant ${grant.grantId} checksum mismatch`,
    );
  if (!isAbsolute(grant.workspaceRoot) || !isAbsolute(grant.path))
    throw new E03RuntimeError(
      "workspace_grant_absolute_path",
      "workspace grant paths must be absolute",
    );
  if (!contains(grant.workspaceRoot, grant.path))
    throw new E03RuntimeError(
      "workspace_grant_escape",
      `workspace grant ${grant.grantId} escapes its root`,
    );
  if (
    grant.revision < 1 ||
    Date.parse(grant.expiresAt) <= Date.parse(grant.issuedAt)
  )
    throw new E03RuntimeError(
      "workspace_grant_window",
      `workspace grant ${grant.grantId} has an invalid validity window`,
    );
}

export class WorkspacePathCustodyRuntime {
  private grants = new Map<string, WorkspacePathGrant>();

  issue(input: {
    task: E03TaskState;
    workspaceRoot: string;
    path: string;
    access: WorkspaceAccessKind;
    recursive?: boolean;
    allowSymlink?: boolean;
    ttlMs: number;
    reason: string;
    now?: string;
  }): WorkspacePathGrant {
    const workspaceRoot = resolve(input.workspaceRoot);
    const path = resolve(workspaceRoot, input.path);
    if (!input.path.trim())
      throw new E03RuntimeError(
        "workspace_grant_path_missing",
        "workspace grant path is required",
      );
    if (!contains(workspaceRoot, path))
      throw new E03RuntimeError(
        "workspace_grant_escape",
        `workspace path ${path} escapes ${workspaceRoot}`,
      );
    if (
      !input.task.scope.workspaceRoots.some((root) =>
        contains(resolve(root), workspaceRoot),
      )
    )
      throw new E03RuntimeError(
        "workspace_grant_root_denied",
        `workspace root ${workspaceRoot} is outside task scope`,
      );
    if (!Number.isSafeInteger(input.ttlMs) || input.ttlMs < 1)
      throw new E03RuntimeError(
        "workspace_grant_ttl_invalid",
        "workspace grant TTL is invalid",
      );
    const issuedAt = input.now ?? new Date().toISOString();
    const relativePath =
      relative(workspaceRoot, path).replaceAll("\\", "/") || ".";
    const payload = {
      grantId: `workspace-grant-${digest({
        taskId: input.task.identity.taskId,
        leaseId: input.task.identity.leaseId,
        path,
        access: input.access,
        revision: input.task.revision,
      }).slice(0, 32)}`,
      taskId: input.task.identity.taskId,
      sessionId: input.task.identity.sessionId,
      leaseId: input.task.identity.leaseId,
      workspaceRoot,
      path,
      relativePath,
      access: input.access,
      recursive: input.recursive ?? false,
      allowSymlink: input.allowSymlink ?? false,
      expiresAt: new Date(Date.parse(issuedAt) + input.ttlMs).toISOString(),
      revision: input.task.revision,
      reason: input.reason.trim() || `${input.access}_workspace_access`,
      issuedAt,
    };
    const grant = { ...payload, digest: digest(payload) };
    assertWorkspacePathGrant(grant);
    const existing = this.grants.get(grant.grantId);
    if (existing) return structuredClone(existing);
    this.grants.set(grant.grantId, grant);
    return structuredClone(grant);
  }

  decide(input: {
    task: E03TaskState;
    grantId: string;
    path: string;
    access: WorkspaceAccessKind;
    symlink?: boolean;
    now?: string;
  }): WorkspacePathDecision {
    const grant = this.grants.get(input.grantId);
    if (!grant)
      throw new E03RuntimeError(
        "workspace_grant_missing",
        `workspace grant ${input.grantId} is missing`,
      );
    assertWorkspacePathGrant(grant);
    const now = input.now ?? new Date().toISOString();
    const resolvedPath = resolve(grant.workspaceRoot, input.path);
    const relativePath = relative(grant.workspaceRoot, resolvedPath).replaceAll(
      "\\",
      "/",
    );
    const leaseMatched = grant.leaseId === input.task.identity.leaseId;
    const revisionMatched = grant.revision <= input.task.revision;
    const expired = Date.parse(now) >= Date.parse(grant.expiresAt);
    const escaped = !contains(grant.workspaceRoot, resolvedPath);
    const symlinkDenied = input.symlink === true && !grant.allowSymlink;
    const grantedPath = grant.recursive
      ? contains(grant.path, resolvedPath)
      : grant.path === resolvedPath;
    let code = "workspace_access_accepted";
    if (grant.taskId !== input.task.identity.taskId)
      code = "workspace_grant_task_mismatch";
    else if (!leaseMatched) code = "workspace_grant_stale_lease";
    else if (!revisionMatched) code = "workspace_grant_stale_revision";
    else if (expired) code = "workspace_grant_expired";
    else if (escaped) code = "workspace_access_escape";
    else if (!grantedPath) code = "workspace_path_not_granted";
    else if (grant.access !== input.access)
      code = "workspace_access_kind_denied";
    else if (symlinkDenied) code = "workspace_symlink_denied";
    const payload = {
      accepted: code === "workspace_access_accepted",
      code,
      taskId: input.task.identity.taskId,
      grantId: input.grantId,
      requestedPath: input.path,
      resolvedPath,
      relativePath,
      access: input.access,
      leaseMatched,
      revisionMatched,
      expired,
      escaped,
      symlinkDenied,
      decidedAt: now,
    };
    return { ...payload, digest: digest(payload) };
  }

  assert(input: {
    task: E03TaskState;
    grantId: string;
    path: string;
    access: WorkspaceAccessKind;
    symlink?: boolean;
    now?: string;
  }): WorkspacePathDecision {
    const decision = this.decide(input);
    if (!decision.accepted)
      throw new E03RuntimeError(
        decision.code,
        `workspace access to ${decision.resolvedPath} was rejected`,
        {
          grantId: decision.grantId,
          taskId: decision.taskId,
          access: decision.access,
        },
      );
    return decision;
  }

  revoke(grantId: string): WorkspacePathGrant | null {
    const grant = this.grants.get(grantId);
    if (!grant) return null;
    this.grants.delete(grantId);
    return structuredClone(grant);
  }

  revokeTask(taskId: string): WorkspacePathGrant[] {
    const revoked: WorkspacePathGrant[] = [];
    for (const grant of [...this.grants.values()]) {
      if (grant.taskId !== taskId) continue;
      this.grants.delete(grant.grantId);
      revoked.push(structuredClone(grant));
    }
    return revoked.sort((left, right) =>
      left.grantId.localeCompare(right.grantId),
    );
  }

  restore(grants: readonly WorkspacePathGrant[]): void {
    const next = new Map<string, WorkspacePathGrant>();
    for (const raw of grants) {
      const grant = structuredClone(raw);
      assertWorkspacePathGrant(grant);
      if (next.has(grant.grantId))
        throw new E03RuntimeError(
          "duplicate_workspace_grant",
          `workspace grant ${grant.grantId} repeats`,
        );
      next.set(grant.grantId, grant);
    }
    this.grants = next;
  }

  snapshot(taskId?: string): WorkspacePathGrant[] {
    return [...this.grants.values()]
      .filter((grant) => !taskId || grant.taskId === taskId)
      .sort(
        (left, right) =>
          left.expiresAt.localeCompare(right.expiresAt) ||
          left.grantId.localeCompare(right.grantId),
      )
      .map((grant) => structuredClone(grant));
  }
}

export type IsolationRequestPhase =
  | "prepared"
  | "effect_started"
  | "effect_completed"
  | "receipt_recorded"
  | "committed"
  | "acknowledged"
  | "rejected";

export interface IsolationRequestJournalEntry {
  journalId: string;
  requestId: string;
  taskId: string;
  leaseId: string;
  idempotencyKey: string;
  phase: IsolationRequestPhase;
  priorPhase: IsolationRequestPhase | null;
  revision: number;
  requestDigest: string;
  receiptDigest: string | null;
  effectId: string | null;
  errorCode: string;
  errorDigest: string;
  recordedAt: string;
  previousDigest: string;
  digest: string;
}

const ISOLATION_PHASES: Readonly<
  Record<IsolationRequestPhase, readonly IsolationRequestPhase[]>
> = Object.freeze({
  prepared: ["effect_started", "rejected"],
  effect_started: ["effect_completed", "rejected"],
  effect_completed: ["receipt_recorded", "rejected"],
  receipt_recorded: ["committed", "rejected"],
  committed: ["acknowledged"],
  acknowledged: [],
  rejected: [],
});

function assertIsolationJournalEntry(
  entry: IsolationRequestJournalEntry,
): void {
  const { digest: checksum, ...payload } = entry;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "isolation_journal_checksum",
      `isolation journal ${entry.journalId} checksum mismatch`,
    );
  if (!entry.requestId || !entry.taskId || !entry.leaseId)
    throw new E03RuntimeError(
      "isolation_journal_identity",
      "isolation journal identity is incomplete",
    );
  if (entry.revision < 1)
    throw new E03RuntimeError(
      "isolation_journal_revision",
      "isolation journal revision is invalid",
    );
}

export class IsolationRequestJournal {
  private entries: IsolationRequestJournalEntry[] = [];
  private phaseByRequest = new Map<string, IsolationRequestPhase>();
  private idempotency = new Map<string, string>();

  append(input: {
    request: E03IsolationRequest;
    phase: IsolationRequestPhase;
    receipt?: E03IsolationReceipt;
    effectId?: string;
    errorCode?: string;
    error?: string;
    now?: string;
  }): IsolationRequestJournalEntry {
    const priorPhase = this.phaseByRequest.get(input.request.requestId) ?? null;
    if (!priorPhase && input.phase !== "prepared")
      throw new E03RuntimeError(
        "isolation_journal_missing_prepare",
        "isolation journal must begin with prepared",
      );
    if (priorPhase === input.phase) {
      return structuredClone(
        this.entries.find(
          (entry) =>
            entry.requestId === input.request.requestId &&
            entry.phase === input.phase,
        )!,
      );
    }
    if (priorPhase && !ISOLATION_PHASES[priorPhase].includes(input.phase))
      throw new E03RuntimeError(
        "isolation_journal_phase",
        `isolation request cannot move ${priorPhase}->${input.phase}`,
      );
    const boundRequest = this.idempotency.get(input.request.idempotencyKey);
    if (boundRequest && boundRequest !== input.request.requestId)
      throw new E03RuntimeError(
        "isolation_journal_idempotency_conflict",
        "isolation idempotency key identifies another request",
      );
    if (
      input.receipt &&
      (input.receipt.requestId !== input.request.requestId ||
        input.receipt.taskId !== input.request.taskId ||
        input.receipt.leaseId !== input.request.leaseId)
    )
      throw new E03RuntimeError(
        "isolation_journal_receipt_identity",
        "isolation receipt identity differs from request",
      );
    const previousDigest = this.entries.at(-1)?.digest ?? "";
    const payload = {
      journalId: `isolation-journal-${digest({
        requestId: input.request.requestId,
        phase: input.phase,
        previousDigest,
      }).slice(0, 32)}`,
      requestId: input.request.requestId,
      taskId: input.request.taskId,
      leaseId: input.request.leaseId,
      idempotencyKey: input.request.idempotencyKey,
      phase: input.phase,
      priorPhase,
      revision:
        this.entries.filter(
          (entry) => entry.requestId === input.request.requestId,
        ).length + 1,
      requestDigest: input.request.digest,
      receiptDigest: input.receipt?.digest ?? null,
      effectId: input.effectId?.trim() || null,
      errorCode: input.errorCode?.trim() || "",
      errorDigest: input.error ? digest(input.error) : "",
      recordedAt: input.now ?? new Date().toISOString(),
      previousDigest,
    };
    const entry = { ...payload, digest: digest(payload) };
    assertIsolationJournalEntry(entry);
    this.entries.push(entry);
    this.phaseByRequest.set(input.request.requestId, input.phase);
    this.idempotency.set(input.request.idempotencyKey, input.request.requestId);
    return structuredClone(entry);
  }

  phase(requestId: string): IsolationRequestPhase | null {
    return this.phaseByRequest.get(requestId) ?? null;
  }

  restore(entries: readonly IsolationRequestJournalEntry[]): void {
    const next: IsolationRequestJournalEntry[] = [];
    const phases = new Map<string, IsolationRequestPhase>();
    const idempotency = new Map<string, string>();
    let previousDigest = "";
    for (const raw of entries) {
      const entry = structuredClone(raw);
      assertIsolationJournalEntry(entry);
      if (entry.previousDigest !== previousDigest)
        throw new E03RuntimeError(
          "isolation_journal_chain",
          `isolation journal ${entry.journalId} breaks the digest chain`,
        );
      const priorPhase = phases.get(entry.requestId) ?? null;
      if (entry.priorPhase !== priorPhase)
        throw new E03RuntimeError(
          "isolation_journal_prior_phase",
          `isolation journal ${entry.journalId} prior phase mismatch`,
        );
      if (priorPhase && !ISOLATION_PHASES[priorPhase].includes(entry.phase))
        throw new E03RuntimeError(
          "isolation_journal_phase",
          `isolation request cannot restore ${priorPhase}->${entry.phase}`,
        );
      const requestId = idempotency.get(entry.idempotencyKey);
      if (requestId && requestId !== entry.requestId)
        throw new E03RuntimeError(
          "isolation_journal_idempotency_conflict",
          "isolation journal idempotency key is conflicting",
        );
      next.push(entry);
      phases.set(entry.requestId, entry.phase);
      idempotency.set(entry.idempotencyKey, entry.requestId);
      previousDigest = entry.digest;
    }
    this.entries = next;
    this.phaseByRequest = phases;
    this.idempotency = idempotency;
  }

  snapshot(requestId?: string): IsolationRequestJournalEntry[] {
    return this.entries
      .filter((entry) => !requestId || entry.requestId === requestId)
      .map((entry) => structuredClone(entry));
  }
}

export type RemoteRuntimeClass = "edge" | "cloud" | "workstation";

export interface RemoteIsolationEndpoint {
  endpointId: string;
  runtimeClass: RemoteRuntimeClass;
  region: string;
  transport: "stdio" | "https" | "websocket";
  address: string;
  trustDomain: string;
  capabilities: string[];
  workspaceModes: IsolationMode[];
  maximumConcurrentTasks: number;
  maximumWorkspaceBytes: number;
  maximumWallTimeMs: number;
  maximumArtifactBytes: number;
  labels: Record<string, string>;
  enabled: boolean;
  registeredAt: string;
  revision: number;
  digest: string;
}

export interface RemoteIsolationSelection {
  selectionId: string;
  requestId: string;
  taskId: string;
  endpointId: string;
  runtimeClass: RemoteRuntimeClass;
  region: string;
  score: number;
  matchedCapabilities: string[];
  missingCapabilities: string[];
  matchedLabels: Record<string, string>;
  load: number;
  accepted: boolean;
  code: string;
  selectedAt: string;
  digest: string;
}

export interface RemoteIsolationLease {
  remoteLeaseId: string;
  requestId: string;
  taskId: string;
  taskLeaseId: string;
  endpointId: string;
  expectedRevision: number;
  workspaceToken: string;
  artifactToken: string;
  processToken: string;
  acquiredAt: string;
  expiresAt: string;
  releasedAt: string | null;
  releaseReason: string;
  revision: number;
  digest: string;
}

function assertRemoteEndpoint(endpoint: RemoteIsolationEndpoint): void {
  const { digest: checksum, ...payload } = endpoint;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "remote_endpoint_checksum",
      `remote endpoint ${endpoint.endpointId} checksum mismatch`,
    );
  if (!endpoint.endpointId || !endpoint.address || !endpoint.trustDomain)
    throw new E03RuntimeError(
      "remote_endpoint_identity",
      "remote endpoint identity is incomplete",
    );
  if (
    endpoint.maximumConcurrentTasks < 1 ||
    endpoint.maximumWorkspaceBytes < 1 ||
    endpoint.maximumWallTimeMs < 1 ||
    endpoint.maximumArtifactBytes < 1 ||
    endpoint.revision < 1
  )
    throw new E03RuntimeError(
      "remote_endpoint_capacity",
      `remote endpoint ${endpoint.endpointId} capacity is invalid`,
    );
}

function assertRemoteLease(lease: RemoteIsolationLease): void {
  const { digest: checksum, ...payload } = lease;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "remote_lease_checksum",
      `remote lease ${lease.remoteLeaseId} checksum mismatch`,
    );
  if (Date.parse(lease.expiresAt) <= Date.parse(lease.acquiredAt))
    throw new E03RuntimeError(
      "remote_lease_window",
      `remote lease ${lease.remoteLeaseId} has an invalid validity window`,
    );
  if (lease.revision < 1)
    throw new E03RuntimeError(
      "remote_lease_revision",
      `remote lease ${lease.remoteLeaseId} revision is invalid`,
    );
}

export class RemoteIsolationDirectory {
  private endpoints = new Map<string, RemoteIsolationEndpoint>();
  private activeByEndpoint = new Map<string, Set<string>>();
  private leases = new Map<string, RemoteIsolationLease>();

  register(
    input: Omit<
      RemoteIsolationEndpoint,
      "registeredAt" | "revision" | "digest"
    > & { registeredAt?: string },
  ): RemoteIsolationEndpoint {
    const existing = this.endpoints.get(input.endpointId);
    const payload = {
      ...structuredClone(input),
      capabilities: [
        ...new Set(input.capabilities.map((value) => value.trim())),
      ]
        .filter(Boolean)
        .sort(),
      workspaceModes: [...new Set(input.workspaceModes)].sort(),
      labels: Object.fromEntries(
        Object.entries(input.labels)
          .map(([key, value]) => [key.trim(), value.trim()] as const)
          .filter(([key, value]) => key && value)
          .sort(([left], [right]) => left.localeCompare(right)),
      ),
      registeredAt:
        existing?.registeredAt ??
        input.registeredAt ??
        new Date().toISOString(),
      revision: (existing?.revision ?? 0) + 1,
    };
    const endpoint = { ...payload, digest: digest(payload) };
    assertRemoteEndpoint(endpoint);
    this.endpoints.set(endpoint.endpointId, endpoint);
    if (!this.activeByEndpoint.has(endpoint.endpointId))
      this.activeByEndpoint.set(endpoint.endpointId, new Set<string>());
    return structuredClone(endpoint);
  }

  select(input: {
    request: E03IsolationRequest;
    requiredClass?: RemoteRuntimeClass;
    preferredRegions?: readonly string[];
    requiredCapabilities?: readonly string[];
    requiredLabels?: Readonly<Record<string, string>>;
    trustDomain?: string;
    workspaceBytes: number;
    artifactBytes: number;
    wallTimeMs: number;
    now?: string;
  }): RemoteIsolationSelection {
    if (input.request.mode !== "remote")
      throw new E03RuntimeError(
        "remote_selection_mode",
        "remote endpoint selection requires remote isolation mode",
      );
    if (
      !Number.isSafeInteger(input.workspaceBytes) ||
      !Number.isSafeInteger(input.artifactBytes) ||
      !Number.isSafeInteger(input.wallTimeMs) ||
      input.workspaceBytes < 0 ||
      input.artifactBytes < 0 ||
      input.wallTimeMs < 1
    )
      throw new E03RuntimeError(
        "remote_selection_capacity_request",
        "remote capacity request is invalid",
      );
    const capabilities = [...new Set(input.requiredCapabilities ?? [])].sort();
    const labels = Object.fromEntries(
      Object.entries(input.requiredLabels ?? {}).sort(([left], [right]) =>
        left.localeCompare(right),
      ),
    );
    const regions = [...new Set(input.preferredRegions ?? [])];
    const candidates = [...this.endpoints.values()].map((endpoint) => {
      assertRemoteEndpoint(endpoint);
      const active = this.activeByEndpoint.get(endpoint.endpointId)?.size ?? 0;
      const load = active / endpoint.maximumConcurrentTasks;
      const missingCapabilities = capabilities.filter(
        (capability) => !endpoint.capabilities.includes(capability),
      );
      const matchedCapabilities = capabilities.filter((capability) =>
        endpoint.capabilities.includes(capability),
      );
      const matchedLabels = Object.fromEntries(
        Object.entries(labels).filter(
          ([key, value]) => endpoint.labels[key] === value,
        ),
      );
      let code = "remote_endpoint_accepted";
      if (!endpoint.enabled) code = "remote_endpoint_disabled";
      else if (!endpoint.workspaceModes.includes("remote"))
        code = "remote_endpoint_mode_denied";
      else if (
        input.requiredClass &&
        endpoint.runtimeClass !== input.requiredClass
      )
        code = "remote_endpoint_class_mismatch";
      else if (input.trustDomain && endpoint.trustDomain !== input.trustDomain)
        code = "remote_endpoint_trust_mismatch";
      else if (missingCapabilities.length)
        code = "remote_endpoint_capability_missing";
      else if (Object.keys(matchedLabels).length !== Object.keys(labels).length)
        code = "remote_endpoint_label_mismatch";
      else if (active >= endpoint.maximumConcurrentTasks)
        code = "remote_endpoint_saturated";
      else if (input.workspaceBytes > endpoint.maximumWorkspaceBytes)
        code = "remote_endpoint_workspace_capacity";
      else if (input.artifactBytes > endpoint.maximumArtifactBytes)
        code = "remote_endpoint_artifact_capacity";
      else if (input.wallTimeMs > endpoint.maximumWallTimeMs)
        code = "remote_endpoint_wall_time_capacity";
      const regionRank = regions.indexOf(endpoint.region);
      const score =
        (regionRank < 0 ? regions.length + 1 : regionRank) * 10_000 +
        Math.round(load * 1_000) +
        (endpoint.runtimeClass === "edge"
          ? 0
          : endpoint.runtimeClass === "workstation"
            ? 10
            : 20);
      return {
        endpoint,
        active,
        load,
        missingCapabilities,
        matchedCapabilities,
        matchedLabels,
        code,
        score,
      };
    });
    const accepted = candidates
      .filter((candidate) => candidate.code === "remote_endpoint_accepted")
      .sort(
        (left, right) =>
          left.score - right.score ||
          left.endpoint.endpointId.localeCompare(right.endpoint.endpointId),
      )[0];
    const fallback = candidates.sort(
      (left, right) =>
        left.score - right.score ||
        left.endpoint.endpointId.localeCompare(right.endpoint.endpointId),
    )[0];
    const chosen = accepted ?? fallback;
    if (!chosen)
      throw new E03RuntimeError(
        "remote_endpoint_unavailable",
        "no remote isolation endpoints are registered",
      );
    const payload = {
      selectionId: `remote-selection-${digest({
        requestId: input.request.requestId,
        endpointId: chosen.endpoint.endpointId,
        endpointRevision: chosen.endpoint.revision,
      }).slice(0, 32)}`,
      requestId: input.request.requestId,
      taskId: input.request.taskId,
      endpointId: chosen.endpoint.endpointId,
      runtimeClass: chosen.endpoint.runtimeClass,
      region: chosen.endpoint.region,
      score: chosen.score,
      matchedCapabilities: chosen.matchedCapabilities,
      missingCapabilities: chosen.missingCapabilities,
      matchedLabels: chosen.matchedLabels,
      load: chosen.load,
      accepted: Boolean(accepted),
      code: chosen.code,
      selectedAt: input.now ?? new Date().toISOString(),
    };
    return { ...payload, digest: digest(payload) };
  }

  acquire(input: {
    request: E03IsolationRequest;
    selection: RemoteIsolationSelection;
    expectedRevision: number;
    ttlMs: number;
    now?: string;
  }): RemoteIsolationLease {
    if (!input.selection.accepted)
      throw new E03RuntimeError(
        input.selection.code,
        "cannot acquire a rejected remote selection",
      );
    if (
      input.selection.requestId !== input.request.requestId ||
      input.selection.taskId !== input.request.taskId
    )
      throw new E03RuntimeError(
        "remote_selection_request_mismatch",
        "remote selection belongs to another request",
      );
    const endpoint = this.endpoints.get(input.selection.endpointId);
    if (!endpoint)
      throw new E03RuntimeError(
        "remote_endpoint_lost",
        `remote endpoint ${input.selection.endpointId} is unavailable`,
      );
    const active =
      this.activeByEndpoint.get(endpoint.endpointId) ?? new Set<string>();
    if (active.size >= endpoint.maximumConcurrentTasks)
      throw new E03RuntimeError(
        "remote_endpoint_saturated",
        `remote endpoint ${endpoint.endpointId} became saturated`,
      );
    if (!Number.isSafeInteger(input.ttlMs) || input.ttlMs < 1)
      throw new E03RuntimeError(
        "remote_lease_ttl_invalid",
        "remote lease TTL is invalid",
      );
    const acquiredAt = input.now ?? new Date().toISOString();
    const remoteLeaseId = `remote-lease-${digest({
      requestId: input.request.requestId,
      endpointId: endpoint.endpointId,
      taskLeaseId: input.request.leaseId,
    }).slice(0, 32)}`;
    const existing = this.leases.get(remoteLeaseId);
    if (existing) return structuredClone(existing);
    const payload = {
      remoteLeaseId,
      requestId: input.request.requestId,
      taskId: input.request.taskId,
      taskLeaseId: input.request.leaseId,
      endpointId: endpoint.endpointId,
      expectedRevision: input.expectedRevision,
      workspaceToken: digest({ remoteLeaseId, purpose: "workspace" }),
      artifactToken: digest({ remoteLeaseId, purpose: "artifact" }),
      processToken: digest({ remoteLeaseId, purpose: "process" }),
      acquiredAt,
      expiresAt: new Date(Date.parse(acquiredAt) + input.ttlMs).toISOString(),
      releasedAt: null,
      releaseReason: "",
      revision: 1,
    };
    const lease = { ...payload, digest: digest(payload) };
    assertRemoteLease(lease);
    this.leases.set(remoteLeaseId, lease);
    active.add(remoteLeaseId);
    this.activeByEndpoint.set(endpoint.endpointId, active);
    return structuredClone(lease);
  }

  release(
    remoteLeaseId: string,
    reason: string,
    now = new Date().toISOString(),
  ): RemoteIsolationLease {
    const current = this.leases.get(remoteLeaseId);
    if (!current)
      throw new E03RuntimeError(
        "remote_lease_missing",
        `remote lease ${remoteLeaseId} is missing`,
      );
    assertRemoteLease(current);
    if (current.releasedAt) return structuredClone(current);
    const { digest: _, ...prior } = current;
    const payload = {
      ...prior,
      releasedAt: now,
      releaseReason: reason.trim() || "released",
      revision: current.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertRemoteLease(next);
    this.leases.set(remoteLeaseId, next);
    this.activeByEndpoint.get(current.endpointId)?.delete(remoteLeaseId);
    return structuredClone(next);
  }

  assertLease(input: {
    lease: RemoteIsolationLease;
    request: E03IsolationRequest;
    expectedRevision: number;
    token: string;
    purpose: "workspace" | "artifact" | "process";
    now?: string;
  }): void {
    assertRemoteLease(input.lease);
    if (
      input.lease.requestId !== input.request.requestId ||
      input.lease.taskId !== input.request.taskId ||
      input.lease.taskLeaseId !== input.request.leaseId
    )
      throw new E03RuntimeError(
        "remote_lease_request_mismatch",
        "remote lease belongs to another isolation request",
      );
    if (input.lease.releasedAt)
      throw new E03RuntimeError(
        "remote_lease_released",
        `remote lease ${input.lease.remoteLeaseId} was released`,
      );
    if (
      Date.parse(input.now ?? new Date().toISOString()) >=
      Date.parse(input.lease.expiresAt)
    )
      throw new E03RuntimeError(
        "remote_lease_expired",
        `remote lease ${input.lease.remoteLeaseId} expired`,
      );
    if (input.lease.expectedRevision !== input.expectedRevision)
      throw new E03RuntimeError(
        "remote_lease_stale_revision",
        "remote lease revision fence failed",
      );
    const expectedToken =
      input.purpose === "workspace"
        ? input.lease.workspaceToken
        : input.purpose === "artifact"
          ? input.lease.artifactToken
          : input.lease.processToken;
    if (expectedToken !== input.token)
      throw new E03RuntimeError(
        "remote_lease_token_denied",
        `remote ${input.purpose} token is invalid`,
      );
  }

  restore(input: {
    endpoints: readonly RemoteIsolationEndpoint[];
    leases: readonly RemoteIsolationLease[];
  }): void {
    const endpoints = new Map<string, RemoteIsolationEndpoint>();
    const active = new Map<string, Set<string>>();
    for (const raw of input.endpoints) {
      const endpoint = structuredClone(raw);
      assertRemoteEndpoint(endpoint);
      if (endpoints.has(endpoint.endpointId))
        throw new E03RuntimeError(
          "duplicate_remote_endpoint",
          `remote endpoint ${endpoint.endpointId} repeats`,
        );
      endpoints.set(endpoint.endpointId, endpoint);
      active.set(endpoint.endpointId, new Set<string>());
    }
    const leases = new Map<string, RemoteIsolationLease>();
    for (const raw of input.leases) {
      const lease = structuredClone(raw);
      assertRemoteLease(lease);
      if (!endpoints.has(lease.endpointId))
        throw new E03RuntimeError(
          "remote_lease_endpoint_missing",
          `remote lease ${lease.remoteLeaseId} endpoint is missing`,
        );
      if (leases.has(lease.remoteLeaseId))
        throw new E03RuntimeError(
          "duplicate_remote_lease",
          `remote lease ${lease.remoteLeaseId} repeats`,
        );
      leases.set(lease.remoteLeaseId, lease);
      if (!lease.releasedAt)
        active.get(lease.endpointId)!.add(lease.remoteLeaseId);
    }
    for (const [endpointId, remoteLeases] of active) {
      const endpoint = endpoints.get(endpointId)!;
      if (remoteLeases.size > endpoint.maximumConcurrentTasks)
        throw new E03RuntimeError(
          "remote_endpoint_restore_overcommitted",
          `remote endpoint ${endpointId} has too many restored leases`,
        );
    }
    this.endpoints = endpoints;
    this.activeByEndpoint = active;
    this.leases = leases;
  }

  snapshot(): {
    endpoints: RemoteIsolationEndpoint[];
    leases: RemoteIsolationLease[];
  } {
    return {
      endpoints: [...this.endpoints.values()]
        .sort((left, right) => left.endpointId.localeCompare(right.endpointId))
        .map((endpoint) => structuredClone(endpoint)),
      leases: [...this.leases.values()]
        .sort((left, right) =>
          left.remoteLeaseId.localeCompare(right.remoteLeaseId),
        )
        .map((lease) => structuredClone(lease)),
    };
  }
}

export type SandboxNetworkMode = "none" | "loopback" | "allowlist";
export type SandboxFilesystemMode = "readonly" | "workspace-write" | "isolated";

export interface SandboxResourceLimit {
  cpuMillis: number;
  memoryBytes: number;
  processCount: number;
  fileCount: number;
  openFileCount: number;
  stdoutBytes: number;
  stderrBytes: number;
  workspaceBytes: number;
  artifactBytes: number;
  wallTimeMs: number;
}

export interface SandboxExecutionPolicy {
  policyId: string;
  taskId: string;
  sessionId: string;
  leaseId: string;
  expectedRevision: number;
  filesystemMode: SandboxFilesystemMode;
  networkMode: SandboxNetworkMode;
  workspaceRoot: string;
  readonlyRoots: string[];
  writableRoots: string[];
  deniedRoots: string[];
  allowedHosts: string[];
  allowedPorts: number[];
  allowedExecutables: string[];
  deniedExecutables: string[];
  inheritedEnvironment: string[];
  explicitEnvironment: Record<string, string>;
  deniedEnvironment: string[];
  resourceLimit: SandboxResourceLimit;
  allowSubprocess: boolean;
  allowPty: boolean;
  allowShell: boolean;
  allowSymlink: boolean;
  allowDeviceAccess: boolean;
  allowCredentialForwarding: boolean;
  createdAt: string;
  expiresAt: string;
  revision: number;
  digest: string;
}

export interface SandboxExecutionRequest {
  executionId: string;
  policyId: string;
  taskId: string;
  leaseId: string;
  expectedRevision: number;
  executable: string;
  arguments: string[];
  workingDirectory: string;
  environment: Record<string, string>;
  requestedHosts: string[];
  requestedPorts: number[];
  stdinDigest: string;
  pty: boolean;
  timeoutMs: number;
  idempotencyKey: string;
  preparedAt: string;
  digest: string;
}

export interface SandboxExecutionDecision {
  accepted: boolean;
  code: string;
  executionId: string;
  policyId: string;
  taskId: string;
  executable: string;
  workingDirectory: string;
  effectiveEnvironment: Record<string, string>;
  deniedEnvironmentKeys: string[];
  deniedHosts: string[];
  deniedPorts: number[];
  leaseMatched: boolean;
  revisionMatched: boolean;
  expired: boolean;
  decidedAt: string;
  digest: string;
}

function assertResourceLimit(limit: SandboxResourceLimit): void {
  for (const [name, value] of Object.entries(limit))
    if (!Number.isSafeInteger(value) || value < 1)
      throw new E03RuntimeError(
        "sandbox_resource_limit_invalid",
        `sandbox resource limit ${name} is invalid`,
      );
}

function assertSandboxPolicy(policy: SandboxExecutionPolicy): void {
  const { digest: checksum, ...payload } = policy;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "sandbox_policy_checksum",
      `sandbox policy ${policy.policyId} checksum mismatch`,
    );
  assertResourceLimit(policy.resourceLimit);
  if (!isAbsolute(policy.workspaceRoot))
    throw new E03RuntimeError(
      "sandbox_policy_workspace_relative",
      "sandbox policy workspace root must be absolute",
    );
  if (Date.parse(policy.expiresAt) <= Date.parse(policy.createdAt))
    throw new E03RuntimeError(
      "sandbox_policy_window",
      `sandbox policy ${policy.policyId} has an invalid validity window`,
    );
  if (policy.revision < 1 || policy.expectedRevision < 1)
    throw new E03RuntimeError(
      "sandbox_policy_revision",
      `sandbox policy ${policy.policyId} revision is invalid`,
    );
}

function assertSandboxRequest(request: SandboxExecutionRequest): void {
  const { digest: checksum, ...payload } = request;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "sandbox_request_checksum",
      `sandbox request ${request.executionId} checksum mismatch`,
    );
  if (
    !request.executable ||
    !request.workingDirectory ||
    !request.idempotencyKey
  )
    throw new E03RuntimeError(
      "sandbox_request_identity",
      "sandbox request executable, working directory or idempotency key is missing",
    );
  if (!Number.isSafeInteger(request.timeoutMs) || request.timeoutMs < 1)
    throw new E03RuntimeError(
      "sandbox_request_timeout",
      "sandbox request timeout is invalid",
    );
}

function normalizedStringSet(values: readonly string[]): string[] {
  return [
    ...new Set(values.map((value) => value.trim()).filter(Boolean)),
  ].sort();
}

function normalizedPathSet(values: readonly string[]): string[] {
  return [...new Set(values.map((value) => resolve(value)))].sort();
}

export class SandboxExecutionPolicyRuntime {
  private policies = new Map<string, SandboxExecutionPolicy>();
  private requests = new Map<string, SandboxExecutionRequest>();
  private idempotency = new Map<string, string>();

  create(input: {
    task: E03TaskState;
    workspaceRoot: string;
    filesystemMode: SandboxFilesystemMode;
    networkMode: SandboxNetworkMode;
    readonlyRoots?: readonly string[];
    writableRoots?: readonly string[];
    deniedRoots?: readonly string[];
    allowedHosts?: readonly string[];
    allowedPorts?: readonly number[];
    allowedExecutables: readonly string[];
    deniedExecutables?: readonly string[];
    inheritedEnvironment?: readonly string[];
    explicitEnvironment?: Readonly<Record<string, string>>;
    deniedEnvironment?: readonly string[];
    resourceLimit: SandboxResourceLimit;
    allowSubprocess?: boolean;
    allowPty?: boolean;
    allowShell?: boolean;
    allowSymlink?: boolean;
    allowDeviceAccess?: boolean;
    allowCredentialForwarding?: boolean;
    ttlMs: number;
    now?: string;
  }): SandboxExecutionPolicy {
    const workspaceRoot = resolve(input.workspaceRoot);
    if (
      !input.task.scope.workspaceRoots.some((root) =>
        contains(resolve(root), workspaceRoot),
      )
    )
      throw new E03RuntimeError(
        "sandbox_workspace_denied",
        `sandbox workspace ${workspaceRoot} is outside task scope`,
      );
    if (!Number.isSafeInteger(input.ttlMs) || input.ttlMs < 1)
      throw new E03RuntimeError(
        "sandbox_policy_ttl",
        "sandbox policy TTL is invalid",
      );
    assertResourceLimit(input.resourceLimit);
    const writableRoots = normalizedPathSet(
      input.writableRoots ??
        (input.filesystemMode === "readonly" ? [] : [workspaceRoot]),
    );
    for (const path of writableRoots)
      if (!contains(workspaceRoot, path))
        throw new E03RuntimeError(
          "sandbox_writable_root_escape",
          `sandbox writable root ${path} escapes workspace`,
        );
    const readonlyRoots = normalizedPathSet(input.readonlyRoots ?? []);
    const deniedRoots = normalizedPathSet(input.deniedRoots ?? []);
    const allowedPorts = [...new Set(input.allowedPorts ?? [])].sort(
      (left, right) => left - right,
    );
    for (const port of allowedPorts)
      if (!Number.isSafeInteger(port) || port < 1 || port > 65_535)
        throw new E03RuntimeError(
          "sandbox_network_port_invalid",
          `sandbox network port ${port} is invalid`,
        );
    if (
      input.networkMode === "none" &&
      (input.allowedHosts?.length || allowedPorts.length)
    )
      throw new E03RuntimeError(
        "sandbox_network_none_allowlist",
        "network-none policy cannot include hosts or ports",
      );
    const allowedExecutables = normalizedStringSet(input.allowedExecutables);
    if (!allowedExecutables.length)
      throw new E03RuntimeError(
        "sandbox_executable_allowlist_empty",
        "sandbox executable allowlist is empty",
      );
    const createdAt = input.now ?? new Date().toISOString();
    const payload = {
      policyId: `sandbox-policy-${digest({
        taskId: input.task.identity.taskId,
        leaseId: input.task.identity.leaseId,
        revision: input.task.revision,
        workspaceRoot,
      }).slice(0, 32)}`,
      taskId: input.task.identity.taskId,
      sessionId: input.task.identity.sessionId,
      leaseId: input.task.identity.leaseId,
      expectedRevision: input.task.revision,
      filesystemMode: input.filesystemMode,
      networkMode: input.networkMode,
      workspaceRoot,
      readonlyRoots,
      writableRoots,
      deniedRoots,
      allowedHosts: normalizedStringSet(input.allowedHosts ?? []),
      allowedPorts,
      allowedExecutables,
      deniedExecutables: normalizedStringSet(input.deniedExecutables ?? []),
      inheritedEnvironment: normalizedStringSet(
        input.inheritedEnvironment ?? [],
      ),
      explicitEnvironment: Object.fromEntries(
        Object.entries(input.explicitEnvironment ?? {})
          .map(([key, value]) => [key.trim(), value] as const)
          .filter(([key]) => key)
          .sort(([left], [right]) => left.localeCompare(right)),
      ),
      deniedEnvironment: normalizedStringSet(input.deniedEnvironment ?? []),
      resourceLimit: structuredClone(input.resourceLimit),
      allowSubprocess: input.allowSubprocess ?? false,
      allowPty: input.allowPty ?? false,
      allowShell: input.allowShell ?? false,
      allowSymlink: input.allowSymlink ?? false,
      allowDeviceAccess: input.allowDeviceAccess ?? false,
      allowCredentialForwarding: input.allowCredentialForwarding ?? false,
      createdAt,
      expiresAt: new Date(Date.parse(createdAt) + input.ttlMs).toISOString(),
      revision: 1,
    };
    const policy = { ...payload, digest: digest(payload) };
    assertSandboxPolicy(policy);
    this.policies.set(policy.policyId, policy);
    return structuredClone(policy);
  }

  prepare(input: {
    task: E03TaskState;
    policyId: string;
    executable: string;
    arguments?: readonly string[];
    workingDirectory?: string;
    environment?: Readonly<Record<string, string>>;
    requestedHosts?: readonly string[];
    requestedPorts?: readonly number[];
    stdin?: string;
    pty?: boolean;
    timeoutMs: number;
    idempotencyKey: string;
    now?: string;
  }): SandboxExecutionRequest {
    const policy = this.requirePolicy(input.policyId);
    if (policy.taskId !== input.task.identity.taskId)
      throw new E03RuntimeError(
        "sandbox_policy_task_mismatch",
        "sandbox policy belongs to another task",
      );
    const idempotencyKey = input.idempotencyKey.trim();
    if (!idempotencyKey)
      throw new E03RuntimeError(
        "sandbox_idempotency_missing",
        "sandbox request idempotency key is required",
      );
    const bound = this.idempotency.get(idempotencyKey);
    if (bound) return structuredClone(this.requests.get(bound)!);
    const executable = input.executable.trim();
    const workingDirectory = resolve(
      policy.workspaceRoot,
      input.workingDirectory ?? policy.workspaceRoot,
    );
    const payload = {
      executionId: `sandbox-execution-${digest({
        policyId: policy.policyId,
        executable,
        arguments: input.arguments ?? [],
        workingDirectory,
        idempotencyKey,
      }).slice(0, 32)}`,
      policyId: policy.policyId,
      taskId: input.task.identity.taskId,
      leaseId: input.task.identity.leaseId,
      expectedRevision: input.task.revision,
      executable,
      arguments: [...(input.arguments ?? [])],
      workingDirectory,
      environment: Object.fromEntries(
        Object.entries(input.environment ?? {}).sort(([left], [right]) =>
          left.localeCompare(right),
        ),
      ),
      requestedHosts: normalizedStringSet(input.requestedHosts ?? []),
      requestedPorts: [...new Set(input.requestedPorts ?? [])].sort(
        (left, right) => left - right,
      ),
      stdinDigest: digest(input.stdin ?? ""),
      pty: input.pty ?? false,
      timeoutMs: input.timeoutMs,
      idempotencyKey,
      preparedAt: input.now ?? new Date().toISOString(),
    };
    const request = { ...payload, digest: digest(payload) };
    assertSandboxRequest(request);
    this.requests.set(request.executionId, request);
    this.idempotency.set(idempotencyKey, request.executionId);
    return structuredClone(request);
  }

  decide(input: {
    task: E03TaskState;
    request: SandboxExecutionRequest;
    inheritedEnvironment?: Readonly<Record<string, string>>;
    now?: string;
  }): SandboxExecutionDecision {
    assertSandboxRequest(input.request);
    const policy = this.requirePolicy(input.request.policyId);
    const now = input.now ?? new Date().toISOString();
    const leaseMatched =
      policy.leaseId === input.task.identity.leaseId &&
      input.request.leaseId === input.task.identity.leaseId;
    const revisionMatched =
      policy.expectedRevision <= input.task.revision &&
      input.request.expectedRevision === input.task.revision;
    const expired = Date.parse(now) >= Date.parse(policy.expiresAt);
    const executableName = input.request.executable
      .replaceAll("\\", "/")
      .split("/")
      .at(-1)!;
    const allowedExecutable = policy.allowedExecutables.some(
      (candidate) =>
        candidate === input.request.executable || candidate === executableName,
    );
    const deniedExecutable = policy.deniedExecutables.some(
      (candidate) =>
        candidate === input.request.executable || candidate === executableName,
    );
    const insideWorkspace = contains(
      policy.workspaceRoot,
      input.request.workingDirectory,
    );
    const deniedRoot = policy.deniedRoots.some((root) =>
      contains(root, input.request.workingDirectory),
    );
    const deniedHosts = input.request.requestedHosts.filter((host) => {
      if (policy.networkMode === "none") return true;
      if (policy.networkMode === "loopback")
        return !["localhost", "127.0.0.1", "::1"].includes(host);
      return !policy.allowedHosts.includes(host);
    });
    const deniedPorts = input.request.requestedPorts.filter(
      (port) => !policy.allowedPorts.includes(port),
    );
    const inherited = input.inheritedEnvironment ?? {};
    const effectiveEnvironment: Record<string, string> = {
      ...policy.explicitEnvironment,
    };
    for (const key of policy.inheritedEnvironment)
      if (inherited[key] !== undefined)
        effectiveEnvironment[key] = inherited[key]!;
    for (const [key, value] of Object.entries(input.request.environment))
      if (!policy.deniedEnvironment.includes(key))
        effectiveEnvironment[key] = value;
    const deniedEnvironmentKeys = Object.keys(input.request.environment)
      .filter((key) => policy.deniedEnvironment.includes(key))
      .sort();
    let code = "sandbox_execution_accepted";
    if (policy.taskId !== input.task.identity.taskId)
      code = "sandbox_execution_task_mismatch";
    else if (!leaseMatched) code = "sandbox_execution_stale_lease";
    else if (!revisionMatched) code = "sandbox_execution_stale_revision";
    else if (expired) code = "sandbox_execution_policy_expired";
    else if (!allowedExecutable || deniedExecutable)
      code = "sandbox_executable_denied";
    else if (!insideWorkspace || deniedRoot)
      code = "sandbox_working_directory_denied";
    else if (input.request.pty && !policy.allowPty) code = "sandbox_pty_denied";
    else if (input.request.timeoutMs > policy.resourceLimit.wallTimeMs)
      code = "sandbox_timeout_exceeds_limit";
    else if (deniedHosts.length) code = "sandbox_network_host_denied";
    else if (deniedPorts.length) code = "sandbox_network_port_denied";
    else if (deniedEnvironmentKeys.length) code = "sandbox_environment_denied";
    const payload = {
      accepted: code === "sandbox_execution_accepted",
      code,
      executionId: input.request.executionId,
      policyId: policy.policyId,
      taskId: input.task.identity.taskId,
      executable: input.request.executable,
      workingDirectory: input.request.workingDirectory,
      effectiveEnvironment,
      deniedEnvironmentKeys,
      deniedHosts,
      deniedPorts,
      leaseMatched,
      revisionMatched,
      expired,
      decidedAt: now,
    };
    return { ...payload, digest: digest(payload) };
  }

  assert(input: {
    task: E03TaskState;
    request: SandboxExecutionRequest;
    inheritedEnvironment?: Readonly<Record<string, string>>;
    now?: string;
  }): SandboxExecutionDecision {
    const decision = this.decide(input);
    if (!decision.accepted)
      throw new E03RuntimeError(
        decision.code,
        `sandbox execution ${decision.executionId} was rejected`,
        {
          executable: decision.executable,
          workingDirectory: decision.workingDirectory,
          deniedHosts: decision.deniedHosts,
          deniedPorts: decision.deniedPorts,
          deniedEnvironmentKeys: decision.deniedEnvironmentKeys,
        },
      );
    return decision;
  }

  restore(input: {
    policies: readonly SandboxExecutionPolicy[];
    requests: readonly SandboxExecutionRequest[];
  }): void {
    const policies = new Map<string, SandboxExecutionPolicy>();
    for (const raw of input.policies) {
      const policy = structuredClone(raw);
      assertSandboxPolicy(policy);
      if (policies.has(policy.policyId))
        throw new E03RuntimeError(
          "duplicate_sandbox_policy",
          `sandbox policy ${policy.policyId} repeats`,
        );
      policies.set(policy.policyId, policy);
    }
    const requests = new Map<string, SandboxExecutionRequest>();
    const idempotency = new Map<string, string>();
    for (const raw of input.requests) {
      const request = structuredClone(raw);
      assertSandboxRequest(request);
      if (!policies.has(request.policyId))
        throw new E03RuntimeError(
          "sandbox_request_policy_missing",
          `sandbox request ${request.executionId} policy is missing`,
        );
      if (
        requests.has(request.executionId) ||
        idempotency.has(request.idempotencyKey)
      )
        throw new E03RuntimeError(
          "duplicate_sandbox_request",
          `sandbox request ${request.executionId} repeats`,
        );
      requests.set(request.executionId, request);
      idempotency.set(request.idempotencyKey, request.executionId);
    }
    this.policies = policies;
    this.requests = requests;
    this.idempotency = idempotency;
  }

  snapshot(): {
    policies: SandboxExecutionPolicy[];
    requests: SandboxExecutionRequest[];
  } {
    return {
      policies: [...this.policies.values()]
        .sort((left, right) => left.policyId.localeCompare(right.policyId))
        .map((policy) => structuredClone(policy)),
      requests: [...this.requests.values()]
        .sort((left, right) =>
          left.executionId.localeCompare(right.executionId),
        )
        .map((request) => structuredClone(request)),
    };
  }

  private requirePolicy(policyId: string): SandboxExecutionPolicy {
    const policy = this.policies.get(policyId);
    if (!policy)
      throw new E03RuntimeError(
        "sandbox_policy_missing",
        `sandbox policy ${policyId} is missing`,
      );
    assertSandboxPolicy(policy);
    return policy;
  }
}

export type IsolationExecutionSessionState =
  | "prepared"
  | "starting"
  | "running"
  | "stopping"
  | "stopped"
  | "failed"
  | "expired";

export interface IsolationExecutionSession {
  executionSessionId: string;
  isolationRequestId: string;
  isolationReceiptId: string;
  taskId: string;
  leaseId: string;
  mode: IsolationMode;
  workspaceRoot: string;
  runtimeEndpointId: string | null;
  state: IsolationExecutionSessionState;
  environmentDigest: string;
  policyDigest: string;
  processIds: string[];
  commandIds: string[];
  openedAt: string;
  startedAt: string | null;
  lastHeartbeatAt: string | null;
  stopRequestedAt: string | null;
  stoppedAt: string | null;
  expiresAt: string;
  failureCode: string | null;
  failureMessage: string | null;
  revision: number;
  digest: string;
}

export interface IsolationCommandRecord {
  commandId: string;
  executionSessionId: string;
  taskId: string;
  leaseId: string;
  executable: string;
  arguments: string[];
  workingDirectory: string;
  environmentKeys: string[];
  stdinDigest: string | null;
  state:
    | "prepared"
    | "running"
    | "succeeded"
    | "failed"
    | "cancelled"
    | "timed-out";
  processId: string | null;
  exitCode: number | null;
  signal: string | null;
  stdoutDigest: string | null;
  stderrDigest: string | null;
  preparedAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  deadlineAt: string;
  revision: number;
  digest: string;
}

function terminalIsolationCommand(
  state: IsolationCommandRecord["state"],
): boolean {
  return ["succeeded", "failed", "cancelled", "timed-out"].includes(state);
}

function assertIsolationExecutionSession(
  session: IsolationExecutionSession,
): void {
  assertDigest(
    session,
    "digest",
    `isolation execution session ${session.executionSessionId}`,
  );
  if (
    !session.executionSessionId ||
    !session.isolationRequestId ||
    !session.isolationReceiptId ||
    !session.taskId ||
    !session.leaseId ||
    !session.workspaceRoot ||
    !session.environmentDigest ||
    !session.policyDigest
  )
    throw new E03RuntimeError(
      "isolation_execution_session_identity",
      "isolation execution session identity is incomplete",
    );
  if (Date.parse(session.openedAt) >= Date.parse(session.expiresAt))
    throw new E03RuntimeError(
      "isolation_execution_session_expiry",
      "isolation execution session expiry is invalid",
    );
  if (!Number.isSafeInteger(session.revision) || session.revision < 1)
    throw new E03RuntimeError(
      "isolation_execution_session_revision",
      "isolation execution session revision is invalid",
    );
  if (
    session.state === "running" &&
    (!session.startedAt || !session.lastHeartbeatAt)
  )
    throw new E03RuntimeError(
      "isolation_execution_session_state",
      "running isolation execution session requires start and heartbeat times",
    );
  if (
    session.state === "failed" &&
    (!session.failureCode || !session.failureMessage)
  )
    throw new E03RuntimeError(
      "isolation_execution_session_failure",
      "failed isolation execution session requires failure metadata",
    );
}

function assertIsolationCommandRecord(record: IsolationCommandRecord): void {
  assertDigest(record, "digest", `isolation command ${record.commandId}`);
  if (
    !record.commandId ||
    !record.executionSessionId ||
    !record.taskId ||
    !record.leaseId ||
    !record.executable ||
    !record.workingDirectory
  )
    throw new E03RuntimeError(
      "isolation_command_identity",
      "isolation command identity is incomplete",
    );
  if (!Number.isSafeInteger(record.revision) || record.revision < 1)
    throw new E03RuntimeError(
      "isolation_command_revision",
      "isolation command revision is invalid",
    );
  if (record.state === "running" && (!record.processId || !record.startedAt))
    throw new E03RuntimeError(
      "isolation_command_state",
      "running isolation command requires process and start time",
    );
  if (terminalIsolationCommand(record.state) && record.finishedAt === null)
    throw new E03RuntimeError(
      "isolation_command_state",
      "terminal isolation command requires finish time",
    );
  if (record.state === "succeeded" && record.exitCode !== 0)
    throw new E03RuntimeError(
      "isolation_command_exit",
      "successful isolation command requires zero exit code",
    );
}

export class IsolationExecutionSessionRuntime {
  private sessions = new Map<string, IsolationExecutionSession>();
  private commands = new Map<string, IsolationCommandRecord>();
  private byTask = new Map<string, string[]>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  open(input: {
    task: E03TaskState;
    request: E03IsolationRequest;
    receipt: E03IsolationReceipt;
    runtimeEndpointId?: string | null;
    environment: Readonly<Record<string, string>>;
    policyDigest: string;
    expiresAt: string;
  }): IsolationExecutionSession {
    if (
      input.request.taskId !== input.task.identity.taskId ||
      input.request.leaseId !== input.task.identity.leaseId ||
      input.receipt.requestId !== input.request.requestId
    )
      throw new E03RuntimeError(
        "isolation_execution_session_custody",
        "isolation execution input custody is inconsistent",
      );
    const active = (this.byTask.get(input.task.identity.taskId) ?? [])
      .map((sessionId) => this.requireSession(sessionId))
      .find(
        (session) => !["stopped", "failed", "expired"].includes(session.state),
      );
    if (active)
      throw new E03RuntimeError(
        "isolation_execution_session_active",
        `task already has active isolation session ${active.executionSessionId}`,
      );
    const now = this.clock.now();
    const payload = {
      executionSessionId: createId("isolation-execution-session"),
      isolationRequestId: input.request.requestId,
      isolationReceiptId: input.receipt.receiptId,
      taskId: input.task.identity.taskId,
      leaseId: input.task.identity.leaseId,
      mode: input.request.mode,
      workspaceRoot: input.request.workspaceRoot,
      runtimeEndpointId: input.runtimeEndpointId?.trim() || null,
      state: "prepared" as const,
      environmentDigest: digest(
        Object.fromEntries(
          Object.entries(input.environment).sort(([a], [b]) =>
            a.localeCompare(b),
          ),
        ),
      ),
      policyDigest: input.policyDigest.trim(),
      processIds: [],
      commandIds: [],
      openedAt: now,
      startedAt: null,
      lastHeartbeatAt: null,
      stopRequestedAt: null,
      stoppedAt: null,
      expiresAt: input.expiresAt,
      failureCode: null,
      failureMessage: null,
      revision: 1,
    };
    const session = { ...payload, digest: digest(payload) };
    assertIsolationExecutionSession(session);
    this.sessions.set(session.executionSessionId, session);
    this.byTask.set(session.taskId, [
      ...(this.byTask.get(session.taskId) ?? []),
      session.executionSessionId,
    ]);
    return structuredClone(session);
  }

  start(
    sessionId: string,
    expectedRevision: number,
  ): IsolationExecutionSession {
    let session = this.requireSession(sessionId);
    this.assertSessionRevision(session, expectedRevision);
    if (session.state !== "prepared" && session.state !== "starting")
      throw new E03RuntimeError(
        "isolation_execution_session_start_state",
        `isolation execution session ${sessionId} is ${session.state}`,
      );
    const now = this.clock.now();
    if (Date.parse(now) >= Date.parse(session.expiresAt))
      return this.transitionSession(session, {
        state: "expired",
        stoppedAt: now,
      });
    if (session.state === "prepared")
      session = this.transitionSession(session, { state: "starting" });
    return this.transitionSession(session, {
      state: "running",
      startedAt: now,
      lastHeartbeatAt: now,
    });
  }

  prepareCommand(input: {
    sessionId: string;
    expectedSessionRevision: number;
    executable: string;
    arguments?: readonly string[];
    workingDirectory?: string;
    environmentKeys?: readonly string[];
    stdin?: string | null;
    deadlineAt: string;
  }): { session: IsolationExecutionSession; command: IsolationCommandRecord } {
    const session = this.requireSession(input.sessionId);
    this.assertSessionRevision(session, input.expectedSessionRevision);
    if (session.state !== "running")
      throw new E03RuntimeError(
        "isolation_command_session_state",
        `isolation execution session ${session.executionSessionId} is ${session.state}`,
      );
    const workingDirectory = resolve(
      session.workspaceRoot,
      input.workingDirectory ?? ".",
    );
    if (!contains(session.workspaceRoot, workingDirectory))
      throw new E03RuntimeError(
        "isolation_command_workspace_escape",
        "isolation command working directory escapes workspace",
      );
    const now = this.clock.now();
    if (Date.parse(input.deadlineAt) <= Date.parse(now))
      throw new E03RuntimeError(
        "isolation_command_deadline",
        "isolation command deadline must be in the future",
      );
    const payload = {
      commandId: createId("isolation-command"),
      executionSessionId: session.executionSessionId,
      taskId: session.taskId,
      leaseId: session.leaseId,
      executable: input.executable.trim(),
      arguments: [...(input.arguments ?? [])],
      workingDirectory,
      environmentKeys: [...new Set(input.environmentKeys ?? [])].sort(),
      stdinDigest:
        input.stdin === undefined || input.stdin === null
          ? null
          : digest(input.stdin),
      state: "prepared" as const,
      processId: null,
      exitCode: null,
      signal: null,
      stdoutDigest: null,
      stderrDigest: null,
      preparedAt: now,
      startedAt: null,
      finishedAt: null,
      deadlineAt: input.deadlineAt,
      revision: 1,
    };
    const command = { ...payload, digest: digest(payload) };
    assertIsolationCommandRecord(command);
    this.commands.set(command.commandId, command);
    const nextSession = this.transitionSession(session, {
      commandIds: [...session.commandIds, command.commandId],
    });
    return { session: nextSession, command: structuredClone(command) };
  }

  commandStarted(input: {
    commandId: string;
    expectedCommandRevision: number;
    expectedSessionRevision: number;
    processId: string;
  }): { session: IsolationExecutionSession; command: IsolationCommandRecord } {
    const command = this.requireCommand(input.commandId);
    this.assertCommandRevision(command, input.expectedCommandRevision);
    const session = this.requireSession(command.executionSessionId);
    this.assertSessionRevision(session, input.expectedSessionRevision);
    if (command.state !== "prepared" || session.state !== "running")
      throw new E03RuntimeError(
        "isolation_command_start_state",
        `isolation command ${command.commandId} is not ready`,
      );
    const now = this.clock.now();
    if (Date.parse(now) >= Date.parse(command.deadlineAt))
      return {
        session: structuredClone(session),
        command: this.transitionCommand(command, {
          state: "timed-out",
          finishedAt: now,
        }),
      };
    const nextCommand = this.transitionCommand(command, {
      state: "running",
      processId: input.processId.trim(),
      startedAt: now,
    });
    const nextSession = this.transitionSession(session, {
      processIds: [...new Set([...session.processIds, input.processId.trim()])],
      lastHeartbeatAt: now,
    });
    return { session: nextSession, command: nextCommand };
  }

  commandFinished(input: {
    commandId: string;
    expectedCommandRevision: number;
    expectedSessionRevision: number;
    exitCode: number | null;
    signal?: string | null;
    stdout?: string | null;
    stderr?: string | null;
  }): { session: IsolationExecutionSession; command: IsolationCommandRecord } {
    const command = this.requireCommand(input.commandId);
    this.assertCommandRevision(command, input.expectedCommandRevision);
    const session = this.requireSession(command.executionSessionId);
    this.assertSessionRevision(session, input.expectedSessionRevision);
    if (command.state !== "running")
      throw new E03RuntimeError(
        "isolation_command_finish_state",
        `isolation command ${command.commandId} is ${command.state}`,
      );
    const now = this.clock.now();
    const succeeded = input.exitCode === 0 && !input.signal;
    const nextCommand = this.transitionCommand(command, {
      state: succeeded ? "succeeded" : "failed",
      exitCode: input.exitCode,
      signal: input.signal?.trim() || null,
      stdoutDigest:
        input.stdout === undefined || input.stdout === null
          ? null
          : digest(input.stdout),
      stderrDigest:
        input.stderr === undefined || input.stderr === null
          ? null
          : digest(input.stderr),
      finishedAt: now,
    });
    const nextSession = this.transitionSession(session, {
      processIds: session.processIds.filter(
        (processId) => processId !== command.processId,
      ),
      lastHeartbeatAt: now,
    });
    return { session: nextSession, command: nextCommand };
  }

  heartbeat(
    sessionId: string,
    expectedRevision: number,
  ): IsolationExecutionSession {
    const session = this.requireSession(sessionId);
    this.assertSessionRevision(session, expectedRevision);
    if (session.state !== "running")
      throw new E03RuntimeError(
        "isolation_execution_heartbeat_state",
        `isolation execution session ${sessionId} is ${session.state}`,
      );
    return this.transitionSession(session, {
      lastHeartbeatAt: this.clock.now(),
    });
  }

  requestStop(
    sessionId: string,
    expectedRevision: number,
  ): IsolationExecutionSession {
    const session = this.requireSession(sessionId);
    this.assertSessionRevision(session, expectedRevision);
    if (session.state !== "running")
      throw new E03RuntimeError(
        "isolation_execution_stop_state",
        `isolation execution session ${sessionId} is ${session.state}`,
      );
    return this.transitionSession(session, {
      state: "stopping",
      stopRequestedAt: this.clock.now(),
    });
  }

  stopped(
    sessionId: string,
    expectedRevision: number,
  ): IsolationExecutionSession {
    const session = this.requireSession(sessionId);
    this.assertSessionRevision(session, expectedRevision);
    if (session.state !== "stopping")
      throw new E03RuntimeError(
        "isolation_execution_stopped_state",
        `isolation execution session ${sessionId} is ${session.state}`,
      );
    const active = session.commandIds
      .map((commandId) => this.requireCommand(commandId))
      .filter((command) => !terminalIsolationCommand(command.state));
    if (active.length)
      throw new E03RuntimeError(
        "isolation_execution_stopped_commands",
        `isolation execution session ${sessionId} has active commands`,
      );
    return this.transitionSession(session, {
      state: "stopped",
      stoppedAt: this.clock.now(),
      processIds: [],
    });
  }

  fail(
    sessionId: string,
    expectedRevision: number,
    code: string,
    message: string,
  ): IsolationExecutionSession {
    const session = this.requireSession(sessionId);
    this.assertSessionRevision(session, expectedRevision);
    if (["stopped", "failed", "expired"].includes(session.state))
      throw new E03RuntimeError(
        "isolation_execution_fail_state",
        `isolation execution session ${sessionId} is ${session.state}`,
      );
    return this.transitionSession(session, {
      state: "failed",
      stoppedAt: this.clock.now(),
      failureCode: code.trim(),
      failureMessage: message.trim(),
    });
  }

  snapshot(): {
    sessions: IsolationExecutionSession[];
    commands: IsolationCommandRecord[];
  } {
    return {
      sessions: [...this.sessions.values()].map((value) =>
        structuredClone(value),
      ),
      commands: [...this.commands.values()].map((value) =>
        structuredClone(value),
      ),
    };
  }

  restore(input: {
    sessions: readonly IsolationExecutionSession[];
    commands: readonly IsolationCommandRecord[];
  }): void {
    const sessions = new Map<string, IsolationExecutionSession>();
    const commands = new Map<string, IsolationCommandRecord>();
    const byTask = new Map<string, string[]>();
    for (const session of input.sessions) {
      assertIsolationExecutionSession(session);
      if (sessions.has(session.executionSessionId))
        throw new E03RuntimeError(
          "isolation_execution_session_restore_duplicate",
          `duplicate isolation execution session ${session.executionSessionId}`,
        );
      sessions.set(session.executionSessionId, structuredClone(session));
      byTask.set(session.taskId, [
        ...(byTask.get(session.taskId) ?? []),
        session.executionSessionId,
      ]);
    }
    for (const command of input.commands) {
      assertIsolationCommandRecord(command);
      const session = sessions.get(command.executionSessionId);
      if (
        !session ||
        session.taskId !== command.taskId ||
        session.leaseId !== command.leaseId ||
        !session.commandIds.includes(command.commandId)
      )
        throw new E03RuntimeError(
          "isolation_command_restore_session",
          `isolation command ${command.commandId} has invalid session custody`,
        );
      if (commands.has(command.commandId))
        throw new E03RuntimeError(
          "isolation_command_restore_duplicate",
          `duplicate isolation command ${command.commandId}`,
        );
      commands.set(command.commandId, structuredClone(command));
    }
    this.sessions = sessions;
    this.commands = commands;
    this.byTask = byTask;
  }

  private requireSession(sessionId: string): IsolationExecutionSession {
    const session = this.sessions.get(sessionId);
    if (!session)
      throw new E03RuntimeError(
        "isolation_execution_session_missing",
        `isolation execution session ${sessionId} does not exist`,
      );
    assertIsolationExecutionSession(session);
    return session;
  }

  private requireCommand(commandId: string): IsolationCommandRecord {
    const command = this.commands.get(commandId);
    if (!command)
      throw new E03RuntimeError(
        "isolation_command_missing",
        `isolation command ${commandId} does not exist`,
      );
    assertIsolationCommandRecord(command);
    return command;
  }

  private assertSessionRevision(
    session: IsolationExecutionSession,
    expected: number,
  ): void {
    if (session.revision !== expected)
      throw new E03RuntimeError(
        "isolation_execution_session_stale_revision",
        `isolation execution session ${session.executionSessionId} revision is stale`,
      );
  }

  private assertCommandRevision(
    command: IsolationCommandRecord,
    expected: number,
  ): void {
    if (command.revision !== expected)
      throw new E03RuntimeError(
        "isolation_command_stale_revision",
        `isolation command ${command.commandId} revision is stale`,
      );
  }

  private transitionSession(
    session: IsolationExecutionSession,
    patch: Partial<
      Omit<
        IsolationExecutionSession,
        "executionSessionId" | "revision" | "digest"
      >
    >,
  ): IsolationExecutionSession {
    const { digest: _, ...prior } = session;
    const payload = {
      ...prior,
      ...patch,
      executionSessionId: session.executionSessionId,
      revision: session.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertIsolationExecutionSession(next);
    this.sessions.set(next.executionSessionId, next);
    return structuredClone(next);
  }

  private transitionCommand(
    command: IsolationCommandRecord,
    patch: Partial<
      Omit<IsolationCommandRecord, "commandId" | "revision" | "digest">
    >,
  ): IsolationCommandRecord {
    const { digest: _, ...prior } = command;
    const payload = {
      ...prior,
      ...patch,
      commandId: command.commandId,
      revision: command.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertIsolationCommandRecord(next);
    this.commands.set(next.commandId, next);
    return structuredClone(next);
  }
}

export interface IsolationWorkspaceMount {
  mountId: string;
  sessionId: string;
  taskId: string;
  sourceRoot: string;
  targetPath: string;
  mode: "readonly" | "workspace-write" | "ephemeral";
  state: "requested" | "mounted" | "revoked" | "unmounted" | "failed";
  allowedPatterns: string[];
  deniedPatterns: string[];
  byteQuota: number;
  bytesWritten: number;
  operationQuota: number;
  operationsUsed: number;
  leaseExpiresAt: string;
  mountedAt: string | null;
  revokedAt: string | null;
  unmountedAt: string | null;
  failure: string | null;
  revision: number;
  digest: string;
}
export interface IsolationMountAccess {
  accessId: string;
  mountId: string;
  taskId: string;
  operation: "read" | "write" | "create" | "delete" | "execute" | "list";
  relativePath: string;
  requestedBytes: number;
  outcome: "allowed" | "denied";
  reason: string;
  sequence: number;
  occurredAt: string;
  previousDigest: string;
  digest: string;
}
function assertIsolationMount(value: IsolationWorkspaceMount): void {
  assertDigest(value, "digest", `isolation mount ${value.mountId}`);
  if (
    !value.mountId ||
    !value.sessionId ||
    !value.taskId ||
    !value.sourceRoot ||
    !value.targetPath ||
    !Number.isSafeInteger(value.byteQuota) ||
    value.byteQuota < 0 ||
    !Number.isSafeInteger(value.bytesWritten) ||
    value.bytesWritten < 0 ||
    value.bytesWritten > value.byteQuota ||
    !Number.isSafeInteger(value.operationQuota) ||
    value.operationQuota < 1 ||
    !Number.isSafeInteger(value.operationsUsed) ||
    value.operationsUsed < 0 ||
    value.operationsUsed > value.operationQuota ||
    !Number.isSafeInteger(value.revision) ||
    value.revision < 1 ||
    Number.isNaN(Date.parse(value.leaseExpiresAt))
  )
    throw new E03RuntimeError(
      "isolation_mount",
      `isolation mount ${value.mountId} is invalid`,
    );
  if (value.state === "mounted" && value.mountedAt === null)
    throw new E03RuntimeError(
      "isolation_mount_time",
      `mounted workspace ${value.mountId} lacks time`,
    );
}
function assertMountAccess(value: IsolationMountAccess): void {
  assertDigest(value, "digest", `isolation mount access ${value.accessId}`);
  if (
    !value.accessId ||
    !value.mountId ||
    !value.taskId ||
    !value.relativePath ||
    !Number.isSafeInteger(value.requestedBytes) ||
    value.requestedBytes < 0 ||
    !Number.isSafeInteger(value.sequence) ||
    value.sequence < 1 ||
    !value.reason
  )
    throw new E03RuntimeError(
      "isolation_mount_access",
      `isolation mount access ${value.accessId} is invalid`,
    );
}
export type IsolationTransferState =
  | "planned"
  | "uploading"
  | "verifying"
  | "sealed"
  | "imported"
  | "failed"
  | "cancelled";

export interface IsolationArtifactDescriptor {
  artifactId: string;
  taskId: string;
  workspaceId: string;
  logicalPath: string;
  contentDigest: string;
  byteLength: number;
  mediaType: string;
  executable: boolean;
  confidential: boolean;
  createdAt: string;
  revision: number;
  digest: string;
}

export interface IsolationArtifactTransfer {
  transferId: string;
  artifactId: string;
  taskId: string;
  sourceWorkspaceId: string;
  targetWorkspaceId: string;
  idempotencyKey: string;
  state: IsolationTransferState;
  chunkSize: number;
  chunkCount: number;
  receivedChunks: number;
  receivedBytes: number;
  expectedContentDigest: string;
  observedContentDigest: string;
  createdAt: string;
  updatedAt: string;
  sealedAt: string;
  importedAt: string;
  errorCode: string;
  revision: number;
  digest: string;
}

export interface IsolationArtifactChunk {
  chunkId: string;
  transferId: string;
  ordinal: number;
  offset: number;
  byteLength: number;
  contentDigest: string;
  previousDigest: string;
  receivedAt: string;
  receiptToken: string;
  digest: string;
}

export interface IsolationArtifactImportReceipt {
  receiptId: string;
  transferId: string;
  artifactId: string;
  targetWorkspaceId: string;
  physicalPath: string;
  contentDigest: string;
  workerId: string;
  fencingToken: number;
  importedAt: string;
  previousDigest: string;
  digest: string;
}

export interface IsolationArtifactTransferSnapshot {
  descriptors: IsolationArtifactDescriptor[];
  transfers: IsolationArtifactTransfer[];
  chunks: IsolationArtifactChunk[];
  receipts: IsolationArtifactImportReceipt[];
  transferByIdempotencyKey: [string, string][];
  activeTransferByArtifactTarget: [string, string][];
  nextFenceByTargetWorkspace: [string, number][];
}

function assertIsolationArtifactDescriptor(
  value: IsolationArtifactDescriptor,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.artifactId ||
    !value.taskId ||
    !value.workspaceId ||
    !value.logicalPath ||
    value.logicalPath.startsWith("/") ||
    value.logicalPath.includes("..") ||
    !value.contentDigest ||
    value.byteLength < 0 ||
    !Number.isSafeInteger(value.byteLength) ||
    !value.mediaType ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "isolation_artifact_descriptor_corrupt",
      `isolation artifact ${value.artifactId || "<empty>"} is corrupt`,
    );
}

function assertIsolationArtifactTransfer(
  value: IsolationArtifactTransfer,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.transferId ||
    !value.artifactId ||
    !value.taskId ||
    !value.sourceWorkspaceId ||
    !value.targetWorkspaceId ||
    value.sourceWorkspaceId === value.targetWorkspaceId ||
    !value.idempotencyKey ||
    value.chunkSize < 1 ||
    value.chunkCount < 0 ||
    value.receivedChunks < 0 ||
    value.receivedChunks > value.chunkCount ||
    value.receivedBytes < 0 ||
    !value.expectedContentDigest ||
    value.revision < 1 ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "isolation_artifact_transfer_corrupt",
      `isolation artifact transfer ${value.transferId || "<empty>"} is corrupt`,
    );
}

function assertIsolationArtifactChunk(value: IsolationArtifactChunk): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.chunkId ||
    !value.transferId ||
    value.ordinal < 0 ||
    value.offset < 0 ||
    value.byteLength < 0 ||
    !Number.isSafeInteger(value.ordinal) ||
    !Number.isSafeInteger(value.offset) ||
    !Number.isSafeInteger(value.byteLength) ||
    !value.contentDigest ||
    !value.receiptToken ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "isolation_artifact_chunk_corrupt",
      `isolation artifact chunk ${value.chunkId || "<empty>"} is corrupt`,
    );
}

function assertIsolationImportReceipt(
  value: IsolationArtifactImportReceipt,
): void {
  const { digest: expected, ...payload } = value;
  if (
    !value.receiptId ||
    !value.transferId ||
    !value.artifactId ||
    !value.targetWorkspaceId ||
    !value.physicalPath ||
    !value.contentDigest ||
    !value.workerId ||
    value.fencingToken < 1 ||
    !Number.isSafeInteger(value.fencingToken) ||
    digest(payload) !== expected
  )
    throw new E03RuntimeError(
      "isolation_artifact_import_receipt_corrupt",
      `isolation artifact receipt ${value.receiptId || "<empty>"} is corrupt`,
    );
}

export class IsolationArtifactTransferRuntime {
  private descriptors = new Map<string, IsolationArtifactDescriptor>();
  private transfers = new Map<string, IsolationArtifactTransfer>();
  private chunks = new Map<string, IsolationArtifactChunk[]>();
  private receipts = new Map<string, IsolationArtifactImportReceipt[]>();
  private transferByIdempotencyKey = new Map<string, string>();
  private activeTransferByArtifactTarget = new Map<string, string>();
  private nextFenceByTargetWorkspace = new Map<string, number>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  describe(input: {
    artifactId?: string;
    taskId: string;
    workspaceId: string;
    logicalPath: string;
    contentDigest: string;
    byteLength: number;
    mediaType: string;
    executable?: boolean;
    confidential?: boolean;
  }): IsolationArtifactDescriptor {
    const artifactId = input.artifactId ?? createId("isolation-artifact");
    if (this.descriptors.has(artifactId))
      throw new E03RuntimeError(
        "isolation_artifact_descriptor_duplicate",
        `isolation artifact ${artifactId} already exists`,
      );
    const logicalPath = input.logicalPath.replaceAll("\\", "/");
    const payload = {
      artifactId,
      taskId: input.taskId,
      workspaceId: input.workspaceId,
      logicalPath,
      contentDigest: input.contentDigest,
      byteLength: input.byteLength,
      mediaType: input.mediaType,
      executable: input.executable ?? false,
      confidential: input.confidential ?? false,
      createdAt: this.clock.now(),
      revision: 1,
    };
    const descriptor = { ...payload, digest: digest(payload) };
    assertIsolationArtifactDescriptor(descriptor);
    this.descriptors.set(artifactId, descriptor);
    return structuredClone(descriptor);
  }

  plan(input: {
    transferId?: string;
    artifactId: string;
    targetWorkspaceId: string;
    idempotencyKey: string;
    chunkSize: number;
  }): IsolationArtifactTransfer {
    const duplicate = this.transferByIdempotencyKey.get(input.idempotencyKey);
    if (duplicate) return structuredClone(this.requireTransfer(duplicate));
    const descriptor = this.requireDescriptor(input.artifactId);
    if (descriptor.workspaceId === input.targetWorkspaceId)
      throw new E03RuntimeError(
        "isolation_artifact_transfer_same_workspace",
        "artifact transfer target must differ from source",
      );
    if (!Number.isSafeInteger(input.chunkSize) || input.chunkSize < 1)
      throw new E03RuntimeError(
        "isolation_artifact_transfer_chunk_size",
        "artifact transfer chunk size must be positive",
      );
    const activeKey = this.activeKey(
      descriptor.artifactId,
      input.targetWorkspaceId,
    );
    if (this.activeTransferByArtifactTarget.has(activeKey))
      throw new E03RuntimeError(
        "isolation_artifact_transfer_active",
        `artifact ${descriptor.artifactId} already transfers to target`,
      );
    const transferId =
      input.transferId ?? createId("isolation-artifact-transfer");
    if (this.transfers.has(transferId))
      throw new E03RuntimeError(
        "isolation_artifact_transfer_duplicate",
        `artifact transfer ${transferId} already exists`,
      );
    const now = this.clock.now();
    const payload = {
      transferId,
      artifactId: descriptor.artifactId,
      taskId: descriptor.taskId,
      sourceWorkspaceId: descriptor.workspaceId,
      targetWorkspaceId: input.targetWorkspaceId,
      idempotencyKey: input.idempotencyKey,
      state: "planned" as const,
      chunkSize: input.chunkSize,
      chunkCount: Math.ceil(descriptor.byteLength / input.chunkSize),
      receivedChunks: 0,
      receivedBytes: 0,
      expectedContentDigest: descriptor.contentDigest,
      observedContentDigest: "",
      createdAt: now,
      updatedAt: now,
      sealedAt: "",
      importedAt: "",
      errorCode: "",
      revision: 1,
    };
    const transfer = { ...payload, digest: digest(payload) };
    assertIsolationArtifactTransfer(transfer);
    this.transfers.set(transferId, transfer);
    this.chunks.set(transferId, []);
    this.receipts.set(transferId, []);
    this.transferByIdempotencyKey.set(input.idempotencyKey, transferId);
    this.activeTransferByArtifactTarget.set(activeKey, transferId);
    return structuredClone(transfer);
  }

  begin(
    transferId: string,
    expectedRevision: number,
  ): IsolationArtifactTransfer {
    const transfer = this.requireTransfer(transferId);
    this.assertTransferRevision(transfer, expectedRevision);
    if (transfer.state !== "planned")
      throw new E03RuntimeError(
        "isolation_artifact_transfer_begin_state",
        `artifact transfer ${transferId} is ${transfer.state}`,
      );
    return this.transitionTransfer(transfer, { state: "uploading" });
  }

  acceptChunk(input: {
    transferId: string;
    expectedRevision: number;
    ordinal: number;
    offset: number;
    byteLength: number;
    contentDigest: string;
    receiptToken: string;
  }): IsolationArtifactChunk {
    const transfer = this.requireTransfer(input.transferId);
    this.assertTransferRevision(transfer, input.expectedRevision);
    if (transfer.state !== "uploading")
      throw new E03RuntimeError(
        "isolation_artifact_transfer_chunk_state",
        `artifact transfer ${transfer.transferId} is ${transfer.state}`,
      );
    const entries = this.chunkEntries(transfer.transferId);
    const duplicate = entries.find((value) => value.ordinal === input.ordinal);
    if (duplicate) {
      if (
        duplicate.offset !== input.offset ||
        duplicate.byteLength !== input.byteLength ||
        duplicate.contentDigest !== input.contentDigest ||
        duplicate.receiptToken !== input.receiptToken
      )
        throw new E03RuntimeError(
          "isolation_artifact_chunk_idempotency_conflict",
          `artifact transfer chunk ${input.ordinal} conflicts`,
        );
      return structuredClone(duplicate);
    }
    const descriptor = this.requireDescriptor(transfer.artifactId);
    const expectedOffset = input.ordinal * transfer.chunkSize;
    const expectedLength = Math.min(
      transfer.chunkSize,
      descriptor.byteLength - expectedOffset,
    );
    if (
      input.ordinal !== entries.length ||
      input.ordinal >= transfer.chunkCount ||
      input.offset !== expectedOffset ||
      input.byteLength !== expectedLength
    )
      throw new E03RuntimeError(
        "isolation_artifact_chunk_geometry",
        `artifact transfer chunk ${input.ordinal} geometry is invalid`,
      );
    const payload = {
      chunkId: createId("isolation-artifact-chunk"),
      transferId: transfer.transferId,
      ordinal: input.ordinal,
      offset: input.offset,
      byteLength: input.byteLength,
      contentDigest: input.contentDigest,
      previousDigest: entries.at(-1)?.digest ?? "",
      receivedAt: this.clock.now(),
      receiptToken: input.receiptToken,
    };
    const chunk = { ...payload, digest: digest(payload) };
    assertIsolationArtifactChunk(chunk);
    entries.push(chunk);
    this.chunks.set(transfer.transferId, entries);
    this.transitionTransfer(transfer, {
      receivedChunks: entries.length,
      receivedBytes: entries.reduce((sum, value) => sum + value.byteLength, 0),
    });
    return structuredClone(chunk);
  }

  verify(
    transferId: string,
    expectedRevision: number,
    observedContentDigest: string,
  ): IsolationArtifactTransfer {
    const transfer = this.requireTransfer(transferId);
    this.assertTransferRevision(transfer, expectedRevision);
    if (transfer.state !== "uploading")
      throw new E03RuntimeError(
        "isolation_artifact_transfer_verify_state",
        `artifact transfer ${transferId} is ${transfer.state}`,
      );
    const descriptor = this.requireDescriptor(transfer.artifactId);
    if (
      transfer.receivedChunks !== transfer.chunkCount ||
      transfer.receivedBytes !== descriptor.byteLength
    )
      throw new E03RuntimeError(
        "isolation_artifact_transfer_incomplete",
        `artifact transfer ${transferId} is incomplete`,
      );
    this.assertChunkChain(this.chunkEntries(transferId));
    if (observedContentDigest !== transfer.expectedContentDigest)
      return this.transitionTransfer(transfer, {
        state: "failed",
        observedContentDigest,
        errorCode: "content_digest_mismatch",
      });
    return this.transitionTransfer(transfer, {
      state: "sealed",
      observedContentDigest,
      sealedAt: this.clock.now(),
    });
  }

  importReceipt(input: {
    receiptId?: string;
    transferId: string;
    expectedRevision: number;
    physicalPath: string;
    contentDigest: string;
    workerId: string;
    fencingToken: number;
  }): IsolationArtifactImportReceipt {
    const transfer = this.requireTransfer(input.transferId);
    this.assertTransferRevision(transfer, input.expectedRevision);
    if (transfer.state !== "sealed")
      throw new E03RuntimeError(
        "isolation_artifact_import_state",
        `artifact transfer ${transfer.transferId} is ${transfer.state}`,
      );
    if (input.contentDigest !== transfer.expectedContentDigest)
      throw new E03RuntimeError(
        "isolation_artifact_import_digest",
        "artifact import receipt digest is invalid",
      );
    const nextFence =
      (this.nextFenceByTargetWorkspace.get(transfer.targetWorkspaceId) ?? 0) +
      1;
    if (input.fencingToken !== nextFence)
      throw new E03RuntimeError(
        "isolation_artifact_import_fence",
        `artifact import expected fence ${nextFence}`,
      );
    const entries = this.receiptEntries(transfer.transferId);
    const receiptId = input.receiptId ?? createId("isolation-artifact-import");
    const payload = {
      receiptId,
      transferId: transfer.transferId,
      artifactId: transfer.artifactId,
      targetWorkspaceId: transfer.targetWorkspaceId,
      physicalPath: input.physicalPath,
      contentDigest: input.contentDigest,
      workerId: input.workerId,
      fencingToken: input.fencingToken,
      importedAt: this.clock.now(),
      previousDigest: entries.at(-1)?.digest ?? "",
    };
    const receipt = { ...payload, digest: digest(payload) };
    assertIsolationImportReceipt(receipt);
    entries.push(receipt);
    this.receipts.set(transfer.transferId, entries);
    this.nextFenceByTargetWorkspace.set(
      transfer.targetWorkspaceId,
      input.fencingToken,
    );
    this.transitionTransfer(transfer, {
      state: "imported",
      importedAt: receipt.importedAt,
    });
    this.activeTransferByArtifactTarget.delete(
      this.activeKey(transfer.artifactId, transfer.targetWorkspaceId),
    );
    return structuredClone(receipt);
  }

  terminate(
    transferId: string,
    expectedRevision: number,
    state: "failed" | "cancelled",
    errorCode: string,
  ): IsolationArtifactTransfer {
    const transfer = this.requireTransfer(transferId);
    this.assertTransferRevision(transfer, expectedRevision);
    if (["sealed", "imported", "failed", "cancelled"].includes(transfer.state))
      throw new E03RuntimeError(
        "isolation_artifact_transfer_terminal",
        `artifact transfer ${transferId} is terminal`,
      );
    const next = this.transitionTransfer(transfer, { state, errorCode });
    this.activeTransferByArtifactTarget.delete(
      this.activeKey(transfer.artifactId, transfer.targetWorkspaceId),
    );
    return next;
  }

  snapshot(): IsolationArtifactTransferSnapshot {
    return {
      descriptors: [...this.descriptors.values()].map((value) =>
        structuredClone(value),
      ),
      transfers: [...this.transfers.values()].map((value) =>
        structuredClone(value),
      ),
      chunks: [...this.chunks.values()]
        .flat()
        .map((value) => structuredClone(value)),
      receipts: [...this.receipts.values()]
        .flat()
        .map((value) => structuredClone(value)),
      transferByIdempotencyKey: [...this.transferByIdempotencyKey.entries()],
      activeTransferByArtifactTarget: [
        ...this.activeTransferByArtifactTarget.entries(),
      ],
      nextFenceByTargetWorkspace: [
        ...this.nextFenceByTargetWorkspace.entries(),
      ],
    };
  }

  restore(snapshot: IsolationArtifactTransferSnapshot): void {
    const descriptors = new Map<string, IsolationArtifactDescriptor>();
    const transfers = new Map<string, IsolationArtifactTransfer>();
    const chunks = new Map<string, IsolationArtifactChunk[]>();
    const receipts = new Map<string, IsolationArtifactImportReceipt[]>();
    for (const value of snapshot.descriptors) {
      assertIsolationArtifactDescriptor(value);
      if (descriptors.has(value.artifactId))
        throw new E03RuntimeError(
          "isolation_artifact_restore_duplicate",
          `duplicate isolation artifact ${value.artifactId}`,
        );
      descriptors.set(value.artifactId, structuredClone(value));
    }
    for (const value of snapshot.transfers) {
      assertIsolationArtifactTransfer(value);
      const descriptor = descriptors.get(value.artifactId);
      if (
        !descriptor ||
        descriptor.taskId !== value.taskId ||
        descriptor.workspaceId !== value.sourceWorkspaceId ||
        transfers.has(value.transferId)
      )
        throw new E03RuntimeError(
          "isolation_artifact_transfer_restore",
          `artifact transfer ${value.transferId} is invalid`,
        );
      transfers.set(value.transferId, structuredClone(value));
      chunks.set(value.transferId, []);
      receipts.set(value.transferId, []);
    }
    for (const value of [...snapshot.chunks].sort(
      (a, b) => a.ordinal - b.ordinal,
    )) {
      assertIsolationArtifactChunk(value);
      const entries = chunks.get(value.transferId);
      if (
        !entries ||
        value.ordinal !== entries.length ||
        value.previousDigest !== (entries.at(-1)?.digest ?? "")
      )
        throw new E03RuntimeError(
          "isolation_artifact_chunk_restore_chain",
          `artifact chunk ${value.chunkId} breaks chain`,
        );
      entries.push(structuredClone(value));
    }
    for (const value of snapshot.receipts) {
      assertIsolationImportReceipt(value);
      const transfer = transfers.get(value.transferId);
      const entries = receipts.get(value.transferId);
      if (
        !transfer ||
        !entries ||
        transfer.artifactId !== value.artifactId ||
        value.previousDigest !== (entries.at(-1)?.digest ?? "")
      )
        throw new E03RuntimeError(
          "isolation_artifact_receipt_restore_chain",
          `artifact receipt ${value.receiptId} breaks chain`,
        );
      entries.push(structuredClone(value));
    }
    const transferByIdempotencyKey = new Map(snapshot.transferByIdempotencyKey);
    const activeTransferByArtifactTarget = new Map(
      snapshot.activeTransferByArtifactTarget,
    );
    const nextFenceByTargetWorkspace = new Map(
      snapshot.nextFenceByTargetWorkspace,
    );
    if (
      transferByIdempotencyKey.size !== snapshot.transferByIdempotencyKey.length
    )
      throw new E03RuntimeError(
        "isolation_artifact_restore_idempotency_duplicate",
        "artifact transfer idempotency index is duplicated",
      );
    for (const [key, transferId] of transferByIdempotencyKey) {
      const transfer = transfers.get(transferId);
      if (!transfer || transfer.idempotencyKey !== key)
        throw new E03RuntimeError(
          "isolation_artifact_restore_idempotency",
          `artifact transfer idempotency index ${key} is invalid`,
        );
    }
    for (const [key, transferId] of activeTransferByArtifactTarget) {
      const transfer = transfers.get(transferId);
      if (
        !transfer ||
        key !==
          this.activeKey(transfer.artifactId, transfer.targetWorkspaceId) ||
        ["imported", "failed", "cancelled"].includes(transfer.state)
      )
        throw new E03RuntimeError(
          "isolation_artifact_restore_active",
          `artifact transfer active index ${key} is invalid`,
        );
    }
    for (const transfer of transfers.values()) {
      const entries = chunks.get(transfer.transferId)!;
      if (
        entries.length !== transfer.receivedChunks ||
        entries.reduce((sum, value) => sum + value.byteLength, 0) !==
          transfer.receivedBytes
      )
        throw new E03RuntimeError(
          "isolation_artifact_restore_counts",
          `artifact transfer ${transfer.transferId} counters are invalid`,
        );
      this.assertChunkChain(entries);
    }
    this.descriptors = descriptors;
    this.transfers = transfers;
    this.chunks = chunks;
    this.receipts = receipts;
    this.transferByIdempotencyKey = transferByIdempotencyKey;
    this.activeTransferByArtifactTarget = activeTransferByArtifactTarget;
    this.nextFenceByTargetWorkspace = nextFenceByTargetWorkspace;
  }

  private activeKey(artifactId: string, targetWorkspaceId: string): string {
    return `${artifactId}\u0000${targetWorkspaceId}`;
  }

  private chunkEntries(transferId: string): IsolationArtifactChunk[] {
    return this.chunks.get(transferId) ?? [];
  }

  private receiptEntries(transferId: string): IsolationArtifactImportReceipt[] {
    return this.receipts.get(transferId) ?? [];
  }

  private assertChunkChain(entries: readonly IsolationArtifactChunk[]): void {
    let previousDigest = "";
    let offset = 0;
    for (const [ordinal, value] of entries.entries()) {
      assertIsolationArtifactChunk(value);
      if (
        value.ordinal !== ordinal ||
        value.offset !== offset ||
        value.previousDigest !== previousDigest
      )
        throw new E03RuntimeError(
          "isolation_artifact_chunk_chain",
          `artifact chunk ${value.chunkId} breaks transfer chain`,
        );
      previousDigest = value.digest;
      offset += value.byteLength;
    }
  }

  private requireDescriptor(id: string): IsolationArtifactDescriptor {
    const value = this.descriptors.get(id);
    if (!value)
      throw new E03RuntimeError(
        "isolation_artifact_descriptor_missing",
        `isolation artifact ${id} does not exist`,
      );
    assertIsolationArtifactDescriptor(value);
    return value;
  }

  private requireTransfer(id: string): IsolationArtifactTransfer {
    const value = this.transfers.get(id);
    if (!value)
      throw new E03RuntimeError(
        "isolation_artifact_transfer_missing",
        `isolation artifact transfer ${id} does not exist`,
      );
    assertIsolationArtifactTransfer(value);
    return value;
  }

  private assertTransferRevision(
    value: IsolationArtifactTransfer,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "isolation_artifact_transfer_stale_revision",
        `isolation artifact transfer ${value.transferId} revision is stale`,
      );
  }

  private transitionTransfer(
    value: IsolationArtifactTransfer,
    patch: Partial<
      Omit<IsolationArtifactTransfer, "transferId" | "revision" | "digest">
    >,
  ): IsolationArtifactTransfer {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      transferId: value.transferId,
      updatedAt: this.clock.now(),
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertIsolationArtifactTransfer(next);
    this.transfers.set(next.transferId, next);
    return structuredClone(next);
  }
}

export class IsolationWorkspaceMountRuntime {
  private mounts = new Map<string, IsolationWorkspaceMount>();
  private accesses = new Map<string, IsolationMountAccess[]>();
  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}
  request(input: {
    session: IsolationExecutionSession;
    sourceRoot: string;
    targetPath: string;
    mode: IsolationWorkspaceMount["mode"];
    allowedPatterns?: readonly string[];
    deniedPatterns?: readonly string[];
    byteQuota: number;
    operationQuota: number;
    ttlMs: number;
  }): IsolationWorkspaceMount {
    if (input.session.state !== "prepared" && input.session.state !== "running")
      throw new E03RuntimeError(
        "isolation_mount_session_state",
        `isolation session ${input.session.executionSessionId} is ${input.session.state}`,
      );
    const sourceRoot = resolve(input.sourceRoot);
    const targetPath = normalize(input.targetPath).replaceAll("\\", "/");
    if (
      !sourceRoot ||
      !targetPath ||
      targetPath.startsWith("../") ||
      targetPath === ".." ||
      targetPath.includes("/../") ||
      !Number.isSafeInteger(input.byteQuota) ||
      input.byteQuota < 0 ||
      !Number.isSafeInteger(input.operationQuota) ||
      input.operationQuota < 1 ||
      !Number.isSafeInteger(input.ttlMs) ||
      input.ttlMs < 1
    )
      throw new E03RuntimeError(
        "isolation_mount_request",
        "isolation workspace mount request is invalid",
      );
    if (
      [...this.mounts.values()].some(
        (value) =>
          value.sessionId === input.session.executionSessionId &&
          value.targetPath === targetPath &&
          value.state !== "unmounted" &&
          value.state !== "failed",
      )
    )
      throw new E03RuntimeError(
        "isolation_mount_target_conflict",
        `isolation target ${targetPath} is already mounted`,
      );
    const allowedPatterns = [
      ...new Set(input.allowedPatterns ?? ["**/*"]),
    ].sort();
    const deniedPatterns = [...new Set(input.deniedPatterns ?? [])].sort();
    const payload = {
      mountId: createId("isolation-workspace-mount"),
      sessionId: input.session.executionSessionId,
      taskId: input.session.taskId,
      sourceRoot,
      targetPath,
      mode: input.mode,
      state: "requested" as const,
      allowedPatterns,
      deniedPatterns,
      byteQuota: input.byteQuota,
      bytesWritten: 0,
      operationQuota: input.operationQuota,
      operationsUsed: 0,
      leaseExpiresAt: new Date(
        Date.parse(this.clock.now()) + input.ttlMs,
      ).toISOString(),
      mountedAt: null,
      revokedAt: null,
      unmountedAt: null,
      failure: null,
      revision: 1,
    };
    const mount = { ...payload, digest: digest(payload) };
    assertIsolationMount(mount);
    this.mounts.set(mount.mountId, mount);
    return structuredClone(mount);
  }
  confirm(
    mountId: string,
    expectedRevision: number,
    observedSourceRoot: string,
    observedTargetPath: string,
  ): IsolationWorkspaceMount {
    const mount = this.requireMount(mountId);
    this.assertMountRevision(mount, expectedRevision);
    if (mount.state !== "requested")
      throw new E03RuntimeError(
        "isolation_mount_confirm_state",
        `isolation mount ${mountId} is ${mount.state}`,
      );
    if (
      resolve(observedSourceRoot) !== mount.sourceRoot ||
      normalize(observedTargetPath).replaceAll("\\", "/") !== mount.targetPath
    )
      return this.transitionMount(mount, {
        state: "failed",
        failure: "mount observation mismatch",
      });
    if (Date.parse(mount.leaseExpiresAt) <= Date.parse(this.clock.now()))
      return this.transitionMount(mount, {
        state: "failed",
        failure: "mount lease expired before confirmation",
      });
    return this.transitionMount(mount, {
      state: "mounted",
      mountedAt: this.clock.now(),
    });
  }
  authorize(input: {
    mountId: string;
    taskId: string;
    operation: IsolationMountAccess["operation"];
    relativePath: string;
    requestedBytes?: number;
  }): { mount: IsolationWorkspaceMount; access: IsolationMountAccess } {
    const mount = this.requireMount(input.mountId);
    const relativePath = normalize(input.relativePath).replaceAll("\\", "/");
    const requestedBytes = input.requestedBytes ?? 0;
    let allowed = true;
    let reason = "mount policy allowed operation";
    if (mount.taskId !== input.taskId) {
      allowed = false;
      reason = "mount task custody mismatch";
    } else if (mount.state !== "mounted") {
      allowed = false;
      reason = `mount is ${mount.state}`;
    } else if (
      Date.parse(mount.leaseExpiresAt) <= Date.parse(this.clock.now())
    ) {
      allowed = false;
      reason = "mount lease expired";
    } else if (
      !relativePath ||
      relativePath.startsWith("../") ||
      relativePath === ".." ||
      relativePath.includes("/../")
    ) {
      allowed = false;
      reason = "mount path escapes target";
    } else if (mount.operationsUsed >= mount.operationQuota) {
      allowed = false;
      reason = "mount operation quota exhausted";
    } else if (!Number.isSafeInteger(requestedBytes) || requestedBytes < 0) {
      allowed = false;
      reason = "mount byte request is invalid";
    } else if (
      input.operation !== "read" &&
      input.operation !== "list" &&
      mount.mode === "readonly"
    ) {
      allowed = false;
      reason = "readonly mount denies mutation";
    } else if (
      mount.bytesWritten + requestedBytes > mount.byteQuota &&
      input.operation !== "read" &&
      input.operation !== "list"
    ) {
      allowed = false;
      reason = "mount byte quota exceeded";
    } else if (
      !this.matchesAny(relativePath, mount.allowedPatterns) ||
      this.matchesAny(relativePath, mount.deniedPatterns)
    ) {
      allowed = false;
      reason = "mount path policy denied operation";
    }
    const entries = this.accesses.get(mount.mountId) ?? [];
    const payload = {
      accessId: createId("isolation-mount-access"),
      mountId: mount.mountId,
      taskId: input.taskId,
      operation: input.operation,
      relativePath,
      requestedBytes,
      outcome: allowed ? ("allowed" as const) : ("denied" as const),
      reason,
      sequence: entries.length + 1,
      occurredAt: this.clock.now(),
      previousDigest: entries[entries.length - 1]?.digest ?? "root",
    };
    const access = { ...payload, digest: digest(payload) };
    assertMountAccess(access);
    entries.push(access);
    this.accesses.set(mount.mountId, entries);
    const mutated =
      allowed && input.operation !== "read" && input.operation !== "list";
    const nextMount = allowed
      ? this.transitionMount(mount, {
          operationsUsed: mount.operationsUsed + 1,
          bytesWritten: mount.bytesWritten + (mutated ? requestedBytes : 0),
        })
      : structuredClone(mount);
    return { mount: nextMount, access: structuredClone(access) };
  }
  revoke(
    mountId: string,
    expectedRevision: number,
    reason: string,
  ): IsolationWorkspaceMount {
    const mount = this.requireMount(mountId);
    this.assertMountRevision(mount, expectedRevision);
    if (
      mount.state === "revoked" ||
      mount.state === "unmounted" ||
      mount.state === "failed"
    )
      return structuredClone(mount);
    if (!reason.trim())
      throw new E03RuntimeError(
        "isolation_mount_revoke_reason",
        "isolation mount revoke reason is required",
      );
    return this.transitionMount(mount, {
      state: "revoked",
      revokedAt: this.clock.now(),
      failure: reason.trim(),
    });
  }
  unmount(mountId: string, expectedRevision: number): IsolationWorkspaceMount {
    const mount = this.requireMount(mountId);
    this.assertMountRevision(mount, expectedRevision);
    if (
      mount.state !== "mounted" &&
      mount.state !== "revoked" &&
      mount.state !== "failed"
    )
      throw new E03RuntimeError(
        "isolation_mount_unmount_state",
        `isolation mount ${mountId} is ${mount.state}`,
      );
    return this.transitionMount(mount, {
      state: "unmounted",
      unmountedAt: this.clock.now(),
    });
  }
  verifyAccess(mountId?: string): void {
    const groups = mountId
      ? [[mountId, this.accesses.get(mountId) ?? []] as const]
      : [...this.accesses.entries()];
    for (const [id, entries] of groups) {
      let previousDigest = "root";
      let sequence = 1;
      for (const access of entries) {
        assertMountAccess(access);
        if (
          access.mountId !== id ||
          access.sequence !== sequence ||
          access.previousDigest !== previousDigest
        )
          throw new E03RuntimeError(
            "isolation_mount_access_chain",
            `isolation mount access ${access.accessId} breaks chain`,
          );
        previousDigest = access.digest;
        sequence += 1;
      }
    }
  }
  snapshot(): {
    mounts: IsolationWorkspaceMount[];
    accesses: IsolationMountAccess[];
  } {
    this.verifyAccess();
    return {
      mounts: [...this.mounts.values()].map((value) => structuredClone(value)),
      accesses: [...this.accesses.values()]
        .flat()
        .map((value) => structuredClone(value)),
    };
  }
  restore(snapshot: {
    mounts: readonly IsolationWorkspaceMount[];
    accesses: readonly IsolationMountAccess[];
  }): void {
    const mounts = new Map<string, IsolationWorkspaceMount>();
    const accesses = new Map<string, IsolationMountAccess[]>();
    for (const value of snapshot.mounts) {
      assertIsolationMount(value);
      if (mounts.has(value.mountId))
        throw new E03RuntimeError(
          "isolation_mount_restore_duplicate",
          `duplicate isolation mount ${value.mountId}`,
        );
      mounts.set(value.mountId, structuredClone(value));
    }
    for (const value of snapshot.accesses) {
      assertMountAccess(value);
      if (!mounts.has(value.mountId))
        throw new E03RuntimeError(
          "isolation_mount_access_restore",
          `isolation mount access ${value.accessId} has no mount`,
        );
      const entries = accesses.get(value.mountId) ?? [];
      if (entries.some((entry) => entry.accessId === value.accessId))
        throw new E03RuntimeError(
          "isolation_mount_access_restore_duplicate",
          `duplicate isolation mount access ${value.accessId}`,
        );
      entries.push(structuredClone(value));
      accesses.set(value.mountId, entries);
    }
    for (const entries of accesses.values())
      entries.sort((left, right) => left.sequence - right.sequence);
    this.mounts = mounts;
    this.accesses = accesses;
    this.verifyAccess();
  }
  private matchesAny(path: string, patterns: readonly string[]): boolean {
    return patterns.some((pattern) => {
      const escaped = pattern
        .replace(/[.+^${}()|[\]\\]/g, "\\$&")
        .replaceAll("**", "@@DOUBLE@@")
        .replaceAll("*", "[^/]*")
        .replaceAll("@@DOUBLE@@", ".*")
        .replaceAll("?", ".");
      return new RegExp(`^${escaped}$`).test(path);
    });
  }
  private requireMount(id: string): IsolationWorkspaceMount {
    const value = this.mounts.get(id);
    if (!value)
      throw new E03RuntimeError(
        "isolation_mount_missing",
        `isolation mount ${id} does not exist`,
      );
    assertIsolationMount(value);
    return value;
  }
  private assertMountRevision(
    value: IsolationWorkspaceMount,
    expected: number,
  ): void {
    if (value.revision !== expected)
      throw new E03RuntimeError(
        "isolation_mount_stale_revision",
        `isolation mount ${value.mountId} revision is stale`,
      );
  }
  private transitionMount(
    value: IsolationWorkspaceMount,
    patch: Partial<
      Omit<IsolationWorkspaceMount, "mountId" | "revision" | "digest">
    >,
  ): IsolationWorkspaceMount {
    const { digest: _, ...prior } = value;
    const payload = {
      ...prior,
      ...patch,
      mountId: value.mountId,
      revision: value.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertIsolationMount(next);
    this.mounts.set(next.mountId, next);
    return structuredClone(next);
  }
}
