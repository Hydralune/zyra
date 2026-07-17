import { isAbsolute, resolve, sep } from "node:path";
import {
  createId,
  digest,
  E03RuntimeError,
  unique,
  type E03AgentDefinition,
  type E03Budget,
  type E03CapabilityScope,
  type E03Clock,
  type IsolationMode,
  SystemE03Clock,
} from "../e03/contracts.ts";

export interface ScopeDerivationInput {
  parent: E03CapabilityScope;
  definition: E03AgentDefinition;
  requestedTools?: readonly string[];
  requestedSkills?: readonly string[];
  requestedMcpServers?: readonly string[];
  requestedWorkspaceRoots?: readonly string[];
  requestedIsolation?: IsolationMode;
  requestedBackground?: boolean;
  requestedPermissionMode?: string;
  depth: number;
}

const ISOLATION_RANK: Readonly<Record<IsolationMode, number>> = Object.freeze({
  none: 0,
  workspace: 1,
  worktree: 2,
  sandbox: 3,
  remote: 4,
});

export class AgentScopeLattice {
  private readonly policy = new AgentCapabilityPolicy();
  derive(input: ScopeDerivationInput): E03CapabilityScope {
    const parent = this.normalize(input.parent);
    if (
      input.depth !==
      input.parent.maxDepth - (input.parent.maxDepth - input.depth)
    ) {
      if (!Number.isSafeInteger(input.depth))
        throw new E03RuntimeError(
          "invalid_depth",
          "agent depth must be an integer",
        );
    }
    if (input.depth < 0 || input.depth > parent.maxDepth)
      throw new E03RuntimeError(
        "depth_exceeded",
        `agent depth ${input.depth} exceeds ${parent.maxDepth}`,
      );
    const definition = input.definition;
    const deniedTools = unique([
      ...parent.deniedTools,
      ...definition.deniedTools,
    ]);
    const definitionTools = definition.tools.length
      ? intersection(parent.tools, definition.tools)
      : parent.tools;
    const requestedTools = input.requestedTools?.length
      ? intersection(definitionTools, input.requestedTools)
      : definitionTools;
    const tools = requestedTools.filter((tool) => !deniedTools.includes(tool));
    const skills = definition.skills.length
      ? intersection(parent.skills, definition.skills)
      : parent.skills;
    const selectedSkills = input.requestedSkills?.length
      ? intersection(skills, input.requestedSkills)
      : skills;
    const mcpServers = definition.mcpServers.length
      ? intersection(parent.mcpServers, definition.mcpServers)
      : parent.mcpServers;
    const selectedMcp = input.requestedMcpServers?.length
      ? intersection(mcpServers, input.requestedMcpServers)
      : mcpServers;
    const workspaceRoots = input.requestedWorkspaceRoots?.length
      ? containedRoots(parent.workspaceRoots, input.requestedWorkspaceRoots)
      : [...parent.workspaceRoots];
    const isolation = input.requestedIsolation ?? definition.isolation;
    if (!parent.isolationModes.includes(isolation))
      throw new E03RuntimeError(
        "isolation_scope_exceeded",
        `isolation ${isolation} is not permitted by parent`,
      );
    const isolationModes = parent.isolationModes.filter(
      (mode) => ISOLATION_RANK[mode] >= ISOLATION_RANK[isolation],
    );
    const permissionMode =
      input.requestedPermissionMode ?? definition.permissionMode;
    if (
      parent.permissionMode !== "inherit" &&
      permissionMode !== "inherit" &&
      permissionMode !== parent.permissionMode
    ) {
      throw new E03RuntimeError(
        "permission_ceiling_exceeded",
        `permission mode ${permissionMode} exceeds parent ${parent.permissionMode}`,
      );
    }
    const budget = deriveBudget(parent, definition.budget);
    this.policy.assert(parent, definition, {
      tools,
      skills: selectedSkills,
      mcpServers: selectedMcp,
      workspaceRoots,
      isolation,
      permissionMode:
        permissionMode === "inherit" ? parent.permissionMode : permissionMode,
      background: input.requestedBackground !== false,
      teamMessaging: parent.allowTeamMessaging,
      fanout: parent.allowFanout && budget.maxChildren > 0,
      kill: parent.allowKill,
      depth: input.depth,
      children: 0,
    });
    const payload = {
      tools,
      deniedTools,
      skills: selectedSkills,
      mcpServers: selectedMcp,
      permissionMode:
        permissionMode === "inherit" ? parent.permissionMode : permissionMode,
      permissionCeilingDigest: parent.permissionCeilingDigest,
      workspaceRoots,
      isolationModes,
      allowBackground:
        parent.allowBackground &&
        definition.background &&
        input.requestedBackground !== false,
      allowTeamMessaging: parent.allowTeamMessaging,
      allowFanout: parent.allowFanout && budget.maxChildren > 0,
      allowKill: parent.allowKill,
      maxDepth: Math.min(parent.maxDepth, definition.budget.maxDepth),
      maxChildren: Math.min(parent.maxChildren, definition.budget.maxChildren),
    };
    const scope = { ...payload, digest: digest(payload) };
    this.assertMonotonic(parent, scope);
    return scope;
  }

  assertMonotonic(parent: E03CapabilityScope, child: E03CapabilityScope): void {
    this.policy.assertDerived(parent, child);
    const violations: string[] = [];
    for (const tool of child.tools)
      if (!parent.tools.includes(tool)) violations.push(`tool:${tool}`);
    for (const denied of parent.deniedTools)
      if (!child.deniedTools.includes(denied))
        violations.push(`denied-tool-removed:${denied}`);
    for (const skill of child.skills)
      if (!parent.skills.includes(skill)) violations.push(`skill:${skill}`);
    for (const server of child.mcpServers)
      if (!parent.mcpServers.includes(server)) violations.push(`mcp:${server}`);
    for (const root of child.workspaceRoots)
      if (!isContained(parent.workspaceRoots, root))
        violations.push(`workspace:${root}`);
    for (const mode of child.isolationModes)
      if (!parent.isolationModes.includes(mode))
        violations.push(`isolation:${mode}`);
    if (child.allowBackground && !parent.allowBackground)
      violations.push("background");
    if (child.allowTeamMessaging && !parent.allowTeamMessaging)
      violations.push("team-messaging");
    if (child.allowFanout && !parent.allowFanout) violations.push("fanout");
    if (child.allowKill && !parent.allowKill) violations.push("kill");
    if (child.maxDepth > parent.maxDepth) violations.push("max-depth");
    if (child.maxChildren > parent.maxChildren) violations.push("max-children");
    if (child.permissionCeilingDigest !== parent.permissionCeilingDigest)
      violations.push("permission-ceiling-digest");
    if (violations.length)
      throw new E03RuntimeError(
        "scope_escalation",
        `child scope escalates ${violations.join(", ")}`,
        { violations },
      );
    const normalized = this.normalize(child);
    if (normalized.digest !== child.digest)
      throw new E03RuntimeError(
        "scope_checksum_mismatch",
        "child scope digest does not match its fields",
      );
  }

  normalize(scope: E03CapabilityScope): E03CapabilityScope {
    const payload = {
      tools: unique(scope.tools),
      deniedTools: unique(scope.deniedTools),
      skills: unique(scope.skills),
      mcpServers: unique(scope.mcpServers),
      permissionMode: scope.permissionMode,
      permissionCeilingDigest: scope.permissionCeilingDigest,
      workspaceRoots: unique(scope.workspaceRoots.map(normalizePath)),
      isolationModes: unique(scope.isolationModes) as IsolationMode[],
      allowBackground: scope.allowBackground,
      allowTeamMessaging: scope.allowTeamMessaging,
      allowFanout: scope.allowFanout,
      allowKill: scope.allowKill,
      maxDepth: scope.maxDepth,
      maxChildren: scope.maxChildren,
    };
    return { ...payload, digest: digest(payload) };
  }
}

function deriveBudget(
  parent: E03CapabilityScope,
  requested: E03Budget,
): E03Budget {
  return {
    ...requested,
    maxChildren: Math.min(parent.maxChildren, requested.maxChildren),
    maxDepth: Math.min(parent.maxDepth, requested.maxDepth),
  };
}

function intersection(
  left: readonly string[],
  right: readonly string[],
): string[] {
  const accepted = new Set(right);
  return unique(left.filter((item) => accepted.has(item)));
}

function normalizePath(path: string): string {
  return path.replaceAll("\\", "/").replace(/\/$/, "");
}

function isContained(roots: readonly string[], candidate: string): boolean {
  const path = normalizePath(candidate);
  return roots.some((root) => {
    const normalized = normalizePath(root);
    return path === normalized || path.startsWith(`${normalized}/`);
  });
}

function containedRoots(
  parent: readonly string[],
  requested: readonly string[],
): string[] {
  const roots = unique(requested.map(normalizePath));
  const escaped = roots.filter((root) => !isContained(parent, root));
  if (escaped.length)
    throw new E03RuntimeError(
      "workspace_scope_exceeded",
      `workspace roots escape parent: ${escaped.join(", ")}`,
      { escaped },
    );
  return roots;
}

export function rootCapabilityScope(input: {
  tools: readonly string[];
  deniedTools?: readonly string[];
  skills?: readonly string[];
  mcpServers?: readonly string[];
  permissionMode: string;
  permissionCeilingDigest: string;
  workspaceRoots: readonly string[];
  isolationModes?: readonly IsolationMode[];
  maxDepth?: number;
  maxChildren?: number;
}): E03CapabilityScope {
  const payload = {
    tools: unique(input.tools),
    deniedTools: unique(input.deniedTools ?? []),
    skills: unique(input.skills ?? []),
    mcpServers: unique(input.mcpServers ?? []),
    permissionMode: input.permissionMode,
    permissionCeilingDigest: input.permissionCeilingDigest,
    workspaceRoots: unique(input.workspaceRoots.map(normalizePath)),
    isolationModes: [
      ...(input.isolationModes ?? [
        "workspace",
        "worktree",
        "sandbox",
        "remote",
      ]),
    ] as IsolationMode[],
    allowBackground: true,
    allowTeamMessaging: true,
    allowFanout: true,
    allowKill: true,
    maxDepth: input.maxDepth ?? 3,
    maxChildren: input.maxChildren ?? 8,
  };
  return { ...payload, digest: digest(payload) };
}

export interface CapabilityRequest {
  tools: readonly string[];
  skills: readonly string[];
  mcpServers: readonly string[];
  workspaceRoots: readonly string[];
  isolation: IsolationMode;
  permissionMode: string;
  background: boolean;
  teamMessaging: boolean;
  fanout: boolean;
  kill: boolean;
  depth: number;
  children: number;
}

export interface CapabilityFinding {
  code: string;
  resource: string;
  detail: string;
  fatal: boolean;
}

export interface CapabilityDecision {
  accepted: boolean;
  requested: CapabilityRequest;
  granted: CapabilityRequest;
  findings: CapabilityFinding[];
  parentDigest: string;
  definitionDigest: string;
  decisionDigest: string;
}

const MODE_RANK: Readonly<Record<string, number>> = Object.freeze({
  deny: 0,
  sealed: 0,
  ask: 1,
  prompt: 1,
  inherit: 2,
  allowlist: 2,
  allow: 3,
  bypass: 4,
});

export class AgentCapabilityPolicy {
  evaluate(
    parent: E03CapabilityScope,
    definition: E03AgentDefinition,
    request: CapabilityRequest,
  ): CapabilityDecision {
    this.assertScope(parent);
    this.assertDefinition(definition);
    const findings: CapabilityFinding[] = [];
    const requested = normalizeRequest(request);
    const grantedTools = intersectRequested(
      requested.tools,
      parent.tools,
      definition.tools,
      parent.deniedTools,
      definition.deniedTools,
      findings,
      "tool",
    );
    const grantedSkills = intersectRequested(
      requested.skills,
      parent.skills,
      definition.skills,
      [],
      [],
      findings,
      "skill",
    );
    const grantedMcp = intersectRequested(
      requested.mcpServers,
      parent.mcpServers,
      definition.mcpServers,
      [],
      [],
      findings,
      "mcp_server",
    );
    const workspaceRoots = requested.workspaceRoots.map((root) =>
      resolve(root),
    );
    for (const root of workspaceRoots) {
      if (!isAbsolute(root))
        findings.push(
          finding(
            "relative_workspace",
            root,
            "workspace root must be absolute",
          ),
        );
      if (
        !parent.workspaceRoots.some((allowed) =>
          contains(resolve(allowed), root),
        )
      )
        findings.push(
          finding(
            "workspace_scope_escape",
            root,
            "workspace root escapes parent scope",
          ),
        );
    }
    if (!parent.isolationModes.includes(requested.isolation))
      findings.push(
        finding(
          "isolation_scope_denied",
          requested.isolation,
          "parent scope denies isolation mode",
        ),
      );
    if (definition.isolation !== "none" && requested.isolation === "none")
      findings.push(
        finding(
          "definition_isolation_downgrade",
          requested.isolation,
          `definition requires ${definition.isolation}`,
        ),
      );
    const parentRank = MODE_RANK[parent.permissionMode] ?? 0;
    const requestedRank = MODE_RANK[requested.permissionMode] ?? 0;
    const definitionRank = MODE_RANK[definition.permissionMode] ?? parentRank;
    if (requestedRank > parentRank)
      findings.push(
        finding(
          "permission_escalation",
          requested.permissionMode,
          `parent permission mode is ${parent.permissionMode}`,
        ),
      );
    if (requestedRank > definitionRank)
      findings.push(
        finding(
          "definition_permission_escalation",
          requested.permissionMode,
          `definition permission mode is ${definition.permissionMode}`,
        ),
      );
    if (
      requested.background &&
      (!parent.allowBackground || !definition.background)
    )
      findings.push(
        finding(
          "background_denied",
          "background",
          "parent scope or agent definition denies background execution",
        ),
      );
    if (requested.teamMessaging && !parent.allowTeamMessaging)
      findings.push(
        finding(
          "team_message_denied",
          "team",
          "parent scope denies team messaging",
        ),
      );
    if (requested.fanout && !parent.allowFanout)
      findings.push(
        finding("fanout_denied", "fanout", "parent scope denies fanout"),
      );
    if (requested.kill && !parent.allowKill)
      findings.push(finding("kill_denied", "kill", "parent scope denies kill"));
    if (requested.depth > Math.min(parent.maxDepth, definition.budget.maxDepth))
      findings.push(
        finding(
          "depth_limit",
          String(requested.depth),
          "requested child depth exceeds parent or definition limit",
        ),
      );
    if (
      requested.children >
      Math.min(parent.maxChildren, definition.budget.maxChildren)
    )
      findings.push(
        finding(
          "child_limit",
          String(requested.children),
          "requested children exceed parent or definition limit",
        ),
      );
    const granted: CapabilityRequest = {
      tools: grantedTools,
      skills: grantedSkills,
      mcpServers: grantedMcp,
      workspaceRoots,
      isolation: requested.isolation,
      permissionMode:
        requestedRank <= Math.min(parentRank, definitionRank)
          ? requested.permissionMode
          : parent.permissionMode,
      background:
        requested.background && parent.allowBackground && definition.background,
      teamMessaging: requested.teamMessaging && parent.allowTeamMessaging,
      fanout: requested.fanout && parent.allowFanout,
      kill: requested.kill && parent.allowKill,
      depth: Math.min(
        requested.depth,
        parent.maxDepth,
        definition.budget.maxDepth,
      ),
      children: Math.min(
        requested.children,
        parent.maxChildren,
        definition.budget.maxChildren,
      ),
    };
    const payload = {
      requested,
      granted,
      findings,
      parentDigest: parent.digest,
      definitionDigest: definition.digest,
    };
    return {
      accepted: findings.every((item) => !item.fatal),
      ...payload,
      decisionDigest: digest(payload),
    };
  }

  assert(
    parent: E03CapabilityScope,
    definition: E03AgentDefinition,
    request: CapabilityRequest,
  ): CapabilityDecision {
    const decision = this.evaluate(parent, definition, request);
    if (!decision.accepted)
      throw new E03RuntimeError(
        "capability_scope_denied",
        decision.findings
          .map((item) => `${item.code}:${item.resource}`)
          .join(", "),
        {
          findings:
            decision.findings as unknown as import("../contracts.ts").JsonObject[],
          decisionDigest: decision.decisionDigest,
        },
      );
    return decision;
  }

  assertDerived(parent: E03CapabilityScope, child: E03CapabilityScope): void {
    this.assertScope(parent);
    this.assertScope(child);
    const violations: string[] = [];
    for (const tool of child.tools)
      if (!parent.tools.includes(tool) || parent.deniedTools.includes(tool))
        violations.push(`tool:${tool}`);
    for (const skill of child.skills)
      if (!parent.skills.includes(skill)) violations.push(`skill:${skill}`);
    for (const server of child.mcpServers)
      if (!parent.mcpServers.includes(server)) violations.push(`mcp:${server}`);
    for (const root of child.workspaceRoots)
      if (
        !parent.workspaceRoots.some((allowed) =>
          contains(resolve(allowed), resolve(root)),
        )
      )
        violations.push(`workspace:${root}`);
    for (const mode of child.isolationModes)
      if (!parent.isolationModes.includes(mode))
        violations.push(`isolation:${mode}`);
    if (child.allowBackground && !parent.allowBackground)
      violations.push("background");
    if (child.allowTeamMessaging && !parent.allowTeamMessaging)
      violations.push("team");
    if (child.allowFanout && !parent.allowFanout) violations.push("fanout");
    if (child.allowKill && !parent.allowKill) violations.push("kill");
    if (child.maxDepth > parent.maxDepth) violations.push("depth");
    if (child.maxChildren > parent.maxChildren) violations.push("children");
    if (
      (MODE_RANK[child.permissionMode] ?? 0) >
      (MODE_RANK[parent.permissionMode] ?? 0)
    )
      violations.push("permission");
    if (violations.length)
      throw new E03RuntimeError(
        "capability_scope_escalation",
        `child capability scope escalates ${violations.join(", ")}`,
        { violations, parentDigest: parent.digest, childDigest: child.digest },
      );
  }

  private assertScope(scope: E03CapabilityScope): void {
    const { digest: checksum, ...payload } = scope;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "scope_digest_mismatch",
        "capability scope digest is invalid",
      );
    if (!scope.permissionCeilingDigest || !scope.permissionMode)
      throw new E03RuntimeError(
        "invalid_capability_scope",
        "capability permission boundary is incomplete",
      );
    if (scope.maxDepth < 0 || scope.maxChildren < 0)
      throw new E03RuntimeError(
        "invalid_capability_limit",
        "capability depth or child limit is negative",
      );
  }

  private assertDefinition(definition: E03AgentDefinition): void {
    const { digest: checksum, ...payload } = definition;
    if (digest(payload) !== checksum)
      throw new E03RuntimeError(
        "definition_digest_mismatch",
        `agent definition ${definition.name} digest is invalid`,
      );
    if (!definition.name || !definition.version || !definition.description)
      throw new E03RuntimeError(
        "invalid_agent_definition",
        "agent definition identity is incomplete",
      );
  }
}

function normalizeRequest(value: CapabilityRequest): CapabilityRequest {
  if (
    !Number.isSafeInteger(value.depth) ||
    value.depth < 0 ||
    !Number.isSafeInteger(value.children) ||
    value.children < 0
  )
    throw new E03RuntimeError(
      "invalid_capability_request",
      "capability depth and children must be non-negative integers",
    );
  return {
    ...value,
    tools: uniqueCapabilities(value.tools),
    skills: uniqueCapabilities(value.skills),
    mcpServers: uniqueCapabilities(value.mcpServers),
    workspaceRoots: uniqueCapabilities(value.workspaceRoots),
    permissionMode: value.permissionMode.trim() || "inherit",
  };
}

function intersectRequested(
  requested: readonly string[],
  parent: readonly string[],
  definition: readonly string[],
  parentDenied: readonly string[],
  definitionDenied: readonly string[],
  findings: CapabilityFinding[],
  kind: string,
): string[] {
  const denied = new Set([...parentDenied, ...definitionDenied]);
  const allowedByDefinition = definition.length ? new Set(definition) : null;
  const granted: string[] = [];
  for (const value of requested) {
    if (!parent.includes(value))
      findings.push(
        finding(
          `${kind}_parent_denied`,
          value,
          `${kind} is absent from parent scope`,
        ),
      );
    else if (allowedByDefinition && !allowedByDefinition.has(value))
      findings.push(
        finding(
          `${kind}_definition_denied`,
          value,
          `${kind} is absent from agent definition`,
        ),
      );
    else if (denied.has(value))
      findings.push(
        finding(
          `${kind}_explicitly_denied`,
          value,
          `${kind} is explicitly denied`,
        ),
      );
    else granted.push(value);
  }
  return granted;
}

function finding(
  code: string,
  resource: string,
  detail: string,
  fatal = true,
): CapabilityFinding {
  return { code, resource, detail, fatal };
}

function uniqueCapabilities(values: readonly string[]): string[] {
  return [
    ...new Set(values.map((value) => value.trim()).filter(Boolean)),
  ].sort();
}

function contains(root: string, candidate: string): boolean {
  return root === candidate || candidate.startsWith(`${root}${sep}`);
}

export type CapabilityLeaseState =
  | "offered"
  | "active"
  | "suspended"
  | "revoked"
  | "expired";

export interface CapabilityLease {
  leaseId: string;
  parentLeaseId: string | null;
  taskId: string;
  sessionId: string;
  holderId: string;
  issuerId: string;
  state: CapabilityLeaseState;
  scope: E03CapabilityScope;
  issuedAt: string;
  activatesAt: string;
  expiresAt: string;
  activatedAt: string | null;
  suspendedAt: string | null;
  revokedAt: string | null;
  revocationReason: string | null;
  revision: number;
  digest: string;
}

export interface CapabilityLeaseUse {
  useId: string;
  leaseId: string;
  taskId: string;
  holderId: string;
  capabilityKind:
    | "tool"
    | "skill"
    | "mcp"
    | "workspace"
    | "isolation"
    | "background";
  capability: string;
  decision: "allowed" | "denied";
  reason: string;
  leaseRevision: number;
  occurredAt: string;
  digest: string;
}

function assertCapabilityLease(lease: CapabilityLease): void {
  const { digest: checksum, ...payload } = lease;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "capability_lease_digest",
      `capability lease ${lease.leaseId} digest is invalid`,
    );
  if (
    !lease.leaseId ||
    !lease.taskId ||
    !lease.sessionId ||
    !lease.holderId ||
    !lease.issuerId
  )
    throw new E03RuntimeError(
      "capability_lease_identity",
      "capability lease identity is incomplete",
    );
  if (!Number.isSafeInteger(lease.revision) || lease.revision < 1)
    throw new E03RuntimeError(
      "capability_lease_revision",
      "capability lease revision is invalid",
    );
  if (Date.parse(lease.activatesAt) >= Date.parse(lease.expiresAt))
    throw new E03RuntimeError(
      "capability_lease_interval",
      "capability lease activation must precede expiry",
    );
  if (lease.state === "active" && lease.activatedAt === null)
    throw new E03RuntimeError(
      "capability_lease_state",
      "active capability lease requires activatedAt",
    );
  if (
    lease.state === "revoked" &&
    (!lease.revokedAt || !lease.revocationReason)
  )
    throw new E03RuntimeError(
      "capability_lease_state",
      "revoked capability lease requires revocation metadata",
    );
}

function assertCapabilityLeaseUse(use: CapabilityLeaseUse): void {
  const { digest: checksum, ...payload } = use;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "capability_lease_use_digest",
      `capability lease use ${use.useId} digest is invalid`,
    );
  if (
    !use.useId ||
    !use.leaseId ||
    !use.taskId ||
    !use.holderId ||
    !use.capability
  )
    throw new E03RuntimeError(
      "capability_lease_use_identity",
      "capability lease use identity is incomplete",
    );
  if (!Number.isSafeInteger(use.leaseRevision) || use.leaseRevision < 1)
    throw new E03RuntimeError(
      "capability_lease_use_revision",
      "capability lease use revision is invalid",
    );
}

export class CapabilityLeaseRuntime {
  private leases = new Map<string, CapabilityLease>();
  private uses = new Map<string, CapabilityLeaseUse>();
  private byTask = new Map<string, Set<string>>();
  private children = new Map<string, Set<string>>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  offer(input: {
    taskId: string;
    sessionId: string;
    holderId: string;
    issuerId: string;
    scope: E03CapabilityScope;
    parentLeaseId?: string | null;
    activatesAt?: string;
    expiresAt: string;
  }): CapabilityLease {
    const now = this.clock.now();
    const parent = input.parentLeaseId
      ? this.requireLease(input.parentLeaseId)
      : null;
    if (parent) {
      if (parent.state !== "active")
        throw new E03RuntimeError(
          "capability_lease_parent_inactive",
          `parent capability lease ${parent.leaseId} is not active`,
        );
      if (parent.sessionId !== input.sessionId)
        throw new E03RuntimeError(
          "capability_lease_parent_session",
          "child capability lease belongs to another session",
        );
      new AgentScopeLattice().assertMonotonic(parent.scope, input.scope);
      if (Date.parse(input.expiresAt) > Date.parse(parent.expiresAt))
        throw new E03RuntimeError(
          "capability_lease_parent_expiry",
          "child capability lease outlives its parent",
        );
    }
    const payload = {
      leaseId: createId("capability-lease"),
      parentLeaseId: parent?.leaseId ?? null,
      taskId: input.taskId.trim(),
      sessionId: input.sessionId.trim(),
      holderId: input.holderId.trim(),
      issuerId: input.issuerId.trim(),
      state: "offered" as const,
      scope: structuredClone(input.scope),
      issuedAt: now,
      activatesAt: input.activatesAt ?? now,
      expiresAt: input.expiresAt,
      activatedAt: null,
      suspendedAt: null,
      revokedAt: null,
      revocationReason: null,
      revision: 1,
    };
    const lease = { ...payload, digest: digest(payload) };
    assertCapabilityLease(lease);
    this.leases.set(lease.leaseId, structuredClone(lease));
    const taskLeases = this.byTask.get(lease.taskId) ?? new Set<string>();
    taskLeases.add(lease.leaseId);
    this.byTask.set(lease.taskId, taskLeases);
    if (lease.parentLeaseId) {
      const childLeases =
        this.children.get(lease.parentLeaseId) ?? new Set<string>();
      childLeases.add(lease.leaseId);
      this.children.set(lease.parentLeaseId, childLeases);
    }
    return structuredClone(lease);
  }

  activate(leaseId: string, holderId: string): CapabilityLease {
    const lease = this.requireLease(leaseId);
    if (lease.holderId !== holderId)
      throw new E03RuntimeError(
        "capability_lease_holder",
        `capability lease ${leaseId} belongs to another holder`,
      );
    if (lease.state !== "offered" && lease.state !== "suspended")
      throw new E03RuntimeError(
        "capability_lease_activate_state",
        `capability lease ${leaseId} cannot be activated from ${lease.state}`,
      );
    const now = this.clock.now();
    if (Date.parse(now) < Date.parse(lease.activatesAt))
      throw new E03RuntimeError(
        "capability_lease_not_started",
        `capability lease ${leaseId} has not reached its activation time`,
      );
    if (Date.parse(now) >= Date.parse(lease.expiresAt))
      return this.transition(lease, {
        state: "expired",
      });
    if (lease.parentLeaseId) {
      const parent = this.requireLease(lease.parentLeaseId);
      if (parent.state !== "active")
        throw new E03RuntimeError(
          "capability_lease_parent_inactive",
          `parent capability lease ${parent.leaseId} is not active`,
        );
    }
    return this.transition(lease, {
      state: "active",
      activatedAt: lease.activatedAt ?? now,
      suspendedAt: null,
    });
  }

  suspend(leaseId: string, issuerId: string): CapabilityLease {
    const lease = this.requireIssuer(leaseId, issuerId);
    if (lease.state !== "active")
      throw new E03RuntimeError(
        "capability_lease_suspend_state",
        `capability lease ${leaseId} is not active`,
      );
    const next = this.transition(lease, {
      state: "suspended",
      suspendedAt: this.clock.now(),
    });
    for (const childId of this.descendants(leaseId)) {
      const child = this.requireLease(childId);
      if (child.state === "active")
        this.transition(child, {
          state: "suspended",
          suspendedAt: this.clock.now(),
        });
    }
    return next;
  }

  revoke(leaseId: string, issuerId: string, reason: string): CapabilityLease {
    const lease = this.requireIssuer(leaseId, issuerId);
    if (lease.state === "revoked") return structuredClone(lease);
    if (lease.state === "expired")
      throw new E03RuntimeError(
        "capability_lease_revoke_state",
        `expired capability lease ${leaseId} cannot be revoked`,
      );
    if (!reason.trim())
      throw new E03RuntimeError(
        "capability_lease_revoke_reason",
        "capability lease revocation reason is required",
      );
    const now = this.clock.now();
    const next = this.transition(lease, {
      state: "revoked",
      revokedAt: now,
      revocationReason: reason.trim(),
    });
    for (const childId of this.descendants(leaseId)) {
      const child = this.requireLease(childId);
      if (child.state === "revoked" || child.state === "expired") continue;
      this.transition(child, {
        state: "revoked",
        revokedAt: now,
        revocationReason: `parent ${leaseId} revoked: ${reason.trim()}`,
      });
    }
    return next;
  }

  evaluate(input: {
    leaseId: string;
    holderId: string;
    taskId: string;
    capabilityKind: CapabilityLeaseUse["capabilityKind"];
    capability: string;
  }): CapabilityLeaseUse {
    let lease = this.requireLease(input.leaseId);
    const now = this.clock.now();
    if (
      lease.state === "active" &&
      Date.parse(now) >= Date.parse(lease.expiresAt)
    )
      lease = this.transition(lease, { state: "expired" });
    let decision: CapabilityLeaseUse["decision"] = "allowed";
    let reason = "capability is within active lease";
    if (lease.holderId !== input.holderId || lease.taskId !== input.taskId) {
      decision = "denied";
      reason = "lease custody mismatch";
    } else if (lease.state !== "active") {
      decision = "denied";
      reason = `lease state is ${lease.state}`;
    } else if (
      !this.scopeContains(lease.scope, input.capabilityKind, input.capability)
    ) {
      decision = "denied";
      reason = "capability is outside lease scope";
    }
    const payload = {
      useId: createId("capability-use"),
      leaseId: lease.leaseId,
      taskId: input.taskId,
      holderId: input.holderId,
      capabilityKind: input.capabilityKind,
      capability: input.capability,
      decision,
      reason,
      leaseRevision: lease.revision,
      occurredAt: now,
    };
    const use = { ...payload, digest: digest(payload) };
    assertCapabilityLeaseUse(use);
    this.uses.set(use.useId, use);
    return structuredClone(use);
  }

  active(taskId: string, holderId?: string): CapabilityLease[] {
    return [...(this.byTask.get(taskId) ?? [])]
      .map((leaseId) => this.requireLease(leaseId))
      .filter((lease) => lease.state === "active")
      .filter((lease) => !holderId || lease.holderId === holderId)
      .map((lease) => structuredClone(lease));
  }

  usage(leaseId: string): CapabilityLeaseUse[] {
    this.requireLease(leaseId);
    return [...this.uses.values()]
      .filter((use) => use.leaseId === leaseId)
      .sort((left, right) => left.occurredAt.localeCompare(right.occurredAt))
      .map((use) => structuredClone(use));
  }

  snapshot(): { leases: CapabilityLease[]; uses: CapabilityLeaseUse[] } {
    return {
      leases: [...this.leases.values()].map((lease) => structuredClone(lease)),
      uses: [...this.uses.values()].map((use) => structuredClone(use)),
    };
  }

  restore(input: {
    leases: readonly CapabilityLease[];
    uses: readonly CapabilityLeaseUse[];
  }): void {
    const leases = new Map<string, CapabilityLease>();
    const uses = new Map<string, CapabilityLeaseUse>();
    const byTask = new Map<string, Set<string>>();
    const children = new Map<string, Set<string>>();
    for (const raw of input.leases) {
      const lease = structuredClone(raw);
      assertCapabilityLease(lease);
      if (leases.has(lease.leaseId))
        throw new E03RuntimeError(
          "capability_lease_restore_duplicate",
          `duplicate capability lease ${lease.leaseId}`,
        );
      leases.set(lease.leaseId, lease);
      const taskLeases = byTask.get(lease.taskId) ?? new Set<string>();
      taskLeases.add(lease.leaseId);
      byTask.set(lease.taskId, taskLeases);
      if (lease.parentLeaseId) {
        const childLeases =
          children.get(lease.parentLeaseId) ?? new Set<string>();
        childLeases.add(lease.leaseId);
        children.set(lease.parentLeaseId, childLeases);
      }
    }
    for (const lease of leases.values()) {
      if (!lease.parentLeaseId) continue;
      const parent = leases.get(lease.parentLeaseId);
      if (!parent)
        throw new E03RuntimeError(
          "capability_lease_restore_parent",
          `capability lease ${lease.leaseId} has missing parent`,
        );
      new AgentScopeLattice().assertMonotonic(parent.scope, lease.scope);
    }
    for (const raw of input.uses) {
      const use = structuredClone(raw);
      assertCapabilityLeaseUse(use);
      const lease = leases.get(use.leaseId);
      if (!lease)
        throw new E03RuntimeError(
          "capability_lease_restore_use_orphan",
          `capability use ${use.useId} has missing lease`,
        );
      if (use.taskId !== lease.taskId || use.holderId !== lease.holderId)
        throw new E03RuntimeError(
          "capability_lease_restore_use_custody",
          `capability use ${use.useId} custody does not match lease`,
        );
      if (uses.has(use.useId))
        throw new E03RuntimeError(
          "capability_lease_restore_use_duplicate",
          `duplicate capability use ${use.useId}`,
        );
      uses.set(use.useId, use);
    }
    this.leases = leases;
    this.uses = uses;
    this.byTask = byTask;
    this.children = children;
  }

  private scopeContains(
    scope: E03CapabilityScope,
    kind: CapabilityLeaseUse["capabilityKind"],
    capability: string,
  ): boolean {
    switch (kind) {
      case "tool":
        return (
          scope.tools.includes(capability) &&
          !scope.deniedTools.includes(capability)
        );
      case "skill":
        return scope.skills.includes(capability);
      case "mcp":
        return scope.mcpServers.includes(capability);
      case "workspace":
        return containsAny(scope.workspaceRoots, capability);
      case "isolation":
        return scope.isolationModes.includes(capability as IsolationMode);
      case "background":
        return scope.allowBackground && capability === "background";
      default:
        return false;
    }
  }

  private descendants(leaseId: string): string[] {
    const output: string[] = [];
    const queue = [...(this.children.get(leaseId) ?? [])];
    const seen = new Set<string>();
    while (queue.length) {
      const childId = queue.shift()!;
      if (seen.has(childId))
        throw new E03RuntimeError(
          "capability_lease_cycle",
          `capability lease hierarchy contains a cycle at ${childId}`,
        );
      seen.add(childId);
      output.push(childId);
      queue.push(...(this.children.get(childId) ?? []));
    }
    return output;
  }

  private requireIssuer(leaseId: string, issuerId: string): CapabilityLease {
    const lease = this.requireLease(leaseId);
    if (lease.issuerId !== issuerId)
      throw new E03RuntimeError(
        "capability_lease_issuer",
        `capability lease ${leaseId} belongs to another issuer`,
      );
    return lease;
  }

  private requireLease(leaseId: string): CapabilityLease {
    const lease = this.leases.get(leaseId);
    if (!lease)
      throw new E03RuntimeError(
        "capability_lease_missing",
        `capability lease ${leaseId} does not exist`,
      );
    assertCapabilityLease(lease);
    return lease;
  }

  private transition(
    lease: CapabilityLease,
    patch: Partial<Omit<CapabilityLease, "leaseId" | "revision" | "digest">>,
  ): CapabilityLease {
    const { digest: _, ...prior } = lease;
    const payload = {
      ...prior,
      ...patch,
      leaseId: lease.leaseId,
      revision: lease.revision + 1,
    };
    const next = { ...payload, digest: digest(payload) };
    assertCapabilityLease(next);
    this.leases.set(next.leaseId, structuredClone(next));
    return structuredClone(next);
  }
}

export interface CapabilityEscalation {
  escalationId: string;
  taskId: string;
  sessionId: string;
  requesterId: string;
  approverId: string | null;
  parentScopeDigest: string;
  requestedScopeDigest: string;
  requestedCapabilities: string[];
  reason: string;
  risk: "low" | "medium" | "high" | "critical";
  state: "pending" | "approved" | "denied" | "expired" | "cancelled";
  requestedAt: string;
  expiresAt: string;
  decidedAt: string | null;
  decisionReason: string | null;
  digest: string;
}

function assertCapabilityEscalation(escalation: CapabilityEscalation): void {
  const { digest: checksum, ...payload } = escalation;
  if (digest(payload) !== checksum)
    throw new E03RuntimeError(
      "capability_escalation_digest",
      `capability escalation ${escalation.escalationId} digest is invalid`,
    );
  if (
    !escalation.escalationId ||
    !escalation.taskId ||
    !escalation.sessionId ||
    !escalation.requesterId ||
    !escalation.reason ||
    !escalation.parentScopeDigest ||
    !escalation.requestedScopeDigest
  )
    throw new E03RuntimeError(
      "capability_escalation_identity",
      "capability escalation identity is incomplete",
    );
  if (!escalation.requestedCapabilities.length)
    throw new E03RuntimeError(
      "capability_escalation_empty",
      "capability escalation requires requested capabilities",
    );
  if (escalation.state !== "pending" && escalation.decidedAt === null)
    throw new E03RuntimeError(
      "capability_escalation_state",
      "decided capability escalation requires a decision timestamp",
    );
}

export class CapabilityEscalationRuntime {
  private escalations = new Map<string, CapabilityEscalation>();

  constructor(private readonly clock: E03Clock = new SystemE03Clock()) {}

  request(input: {
    taskId: string;
    sessionId: string;
    requesterId: string;
    parentScope: E03CapabilityScope;
    requestedScope: E03CapabilityScope;
    requestedCapabilities: readonly string[];
    reason: string;
    risk: CapabilityEscalation["risk"];
    expiresAt: string;
  }): CapabilityEscalation {
    const requestedCapabilities = unique(input.requestedCapabilities).filter(
      Boolean,
    );
    const now = this.clock.now();
    if (Date.parse(input.expiresAt) <= Date.parse(now))
      throw new E03RuntimeError(
        "capability_escalation_expiry",
        "capability escalation expiry must be in the future",
      );
    const payload = {
      escalationId: createId("capability-escalation"),
      taskId: input.taskId.trim(),
      sessionId: input.sessionId.trim(),
      requesterId: input.requesterId.trim(),
      approverId: null,
      parentScopeDigest: digest(input.parentScope),
      requestedScopeDigest: digest(input.requestedScope),
      requestedCapabilities,
      reason: input.reason.trim(),
      risk: input.risk,
      state: "pending" as const,
      requestedAt: now,
      expiresAt: input.expiresAt,
      decidedAt: null,
      decisionReason: null,
    };
    const escalation = { ...payload, digest: digest(payload) };
    assertCapabilityEscalation(escalation);
    this.escalations.set(escalation.escalationId, escalation);
    return structuredClone(escalation);
  }

  decide(input: {
    escalationId: string;
    approverId: string;
    decision: "approved" | "denied";
    reason: string;
  }): CapabilityEscalation {
    let escalation = this.require(input.escalationId);
    if (escalation.state !== "pending")
      throw new E03RuntimeError(
        "capability_escalation_decided",
        `capability escalation ${input.escalationId} is already ${escalation.state}`,
      );
    const now = this.clock.now();
    if (Date.parse(now) >= Date.parse(escalation.expiresAt)) {
      escalation = this.reseal(escalation, {
        state: "expired",
        decidedAt: now,
        decisionReason: "decision deadline elapsed",
      });
      this.escalations.set(escalation.escalationId, escalation);
      return structuredClone(escalation);
    }
    if (!input.approverId.trim() || !input.reason.trim())
      throw new E03RuntimeError(
        "capability_escalation_decision",
        "capability escalation decision requires approver and reason",
      );
    const next = this.reseal(escalation, {
      approverId: input.approverId.trim(),
      state: input.decision,
      decidedAt: now,
      decisionReason: input.reason.trim(),
    });
    this.escalations.set(next.escalationId, next);
    return structuredClone(next);
  }

  cancel(
    escalationId: string,
    requesterId: string,
    reason: string,
  ): CapabilityEscalation {
    const escalation = this.require(escalationId);
    if (escalation.requesterId !== requesterId)
      throw new E03RuntimeError(
        "capability_escalation_requester",
        `capability escalation ${escalationId} belongs to another requester`,
      );
    if (escalation.state !== "pending")
      throw new E03RuntimeError(
        "capability_escalation_cancel_state",
        `capability escalation ${escalationId} is already ${escalation.state}`,
      );
    const next = this.reseal(escalation, {
      state: "cancelled",
      decidedAt: this.clock.now(),
      decisionReason: reason.trim() || "cancelled by requester",
    });
    this.escalations.set(next.escalationId, next);
    return structuredClone(next);
  }

  pending(taskId?: string): CapabilityEscalation[] {
    const now = this.clock.now();
    const output: CapabilityEscalation[] = [];
    for (const current of this.escalations.values()) {
      let escalation = current;
      if (
        escalation.state === "pending" &&
        Date.parse(now) >= Date.parse(escalation.expiresAt)
      ) {
        escalation = this.reseal(escalation, {
          state: "expired",
          decidedAt: now,
          decisionReason: "decision deadline elapsed",
        });
        this.escalations.set(escalation.escalationId, escalation);
      }
      if (
        escalation.state === "pending" &&
        (!taskId || escalation.taskId === taskId)
      )
        output.push(structuredClone(escalation));
    }
    return output.sort((left, right) =>
      left.requestedAt.localeCompare(right.requestedAt),
    );
  }

  snapshot(): CapabilityEscalation[] {
    return [...this.escalations.values()].map((value) =>
      structuredClone(value),
    );
  }

  restore(values: readonly CapabilityEscalation[]): void {
    const next = new Map<string, CapabilityEscalation>();
    for (const value of values) {
      assertCapabilityEscalation(value);
      if (next.has(value.escalationId))
        throw new E03RuntimeError(
          "capability_escalation_restore_duplicate",
          `duplicate capability escalation ${value.escalationId}`,
        );
      next.set(value.escalationId, structuredClone(value));
    }
    this.escalations = next;
  }

  private require(escalationId: string): CapabilityEscalation {
    const escalation = this.escalations.get(escalationId);
    if (!escalation)
      throw new E03RuntimeError(
        "capability_escalation_missing",
        `capability escalation ${escalationId} does not exist`,
      );
    assertCapabilityEscalation(escalation);
    return escalation;
  }

  private reseal(
    escalation: CapabilityEscalation,
    patch: Partial<Omit<CapabilityEscalation, "escalationId" | "digest">>,
  ): CapabilityEscalation {
    const { digest: _, ...prior } = escalation;
    const payload = {
      ...prior,
      ...patch,
      escalationId: escalation.escalationId,
    };
    const next = { ...payload, digest: digest(payload) };
    assertCapabilityEscalation(next);
    return next;
  }
}

function containsAny(roots: readonly string[], candidate: string): boolean {
  return roots.some((root) => contains(root, candidate));
}
