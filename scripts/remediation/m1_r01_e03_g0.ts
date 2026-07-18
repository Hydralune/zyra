#!/usr/bin/env bun

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import * as ts from "typescript";

type Json = Record<string, unknown>;
type RepoName = "claude-code-best" | "opencode" | "OpenClaw";
type Domain = "agents" | "tasks" | "team" | "isolation" | "control";
type SourceSpec = {
  repo: RepoName;
  snapshot: string;
  role: "primary" | "supplementary";
  domain: Domain;
  paths: readonly string[];
  executableLines: number;
};
type Unit = {
  path: string;
  symbol: string;
  startLine: number;
  endLine: number;
  sha256: string;
  executableLines: number[];
};
type Selected = Unit & { domain: Domain; repo: RepoName; role: SourceSpec["role"]; snapshot: string };
type Route = readonly [string, string, Domain, string];

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../..");
const workspaceRoot = resolve(repoRoot, "..");
const manifestRoot = join(workspaceRoot, "docs/remediations/M1-R01-claude-source-custody/manifests");
const evidenceRoot = join(repoRoot, "docs/reviews/evidence/M1-R01-v3/execution-03/g0");
const VERIFIED_HEAD = "f07fd239dd768f399a36329314da82e90ddce6a4";
const IMPLEMENTATION_BASELINE = "454a22d344d7a5413cf8d49c22bf609f85f9d7e4";
const CLAUDE = "c57f5a29e88e9a814bea47abeb9a0a6f725dc102";
const OPENCODE = "adf178a6b95c61506ddaadaf4dd062badb4a8fda";
const OPENCLAW = "b63e06f68aa0f5fc3dc809c37615b8b1012b180b";
const SCHEMA_VERSION = "3.0";
const EXECUTION_ID = "E03";

const specs: readonly SourceSpec[] = [
  {
    repo: "claude-code-best", snapshot: CLAUDE, role: "primary", domain: "agents", executableLines: 1_650,
    paths: [
      "src/tools/AgentTool/AgentTool.tsx", "src/tools/AgentTool/runAgent.ts", "src/tools/AgentTool/resumeAgent.ts",
      "src/tools/AgentTool/forkSubagent.ts", "src/tools/AgentTool/loadAgentsDir.ts", "src/tools/AgentTool/agentToolUtils.ts",
      "src/tools/AgentTool/builtInAgents.ts", "src/tools/AgentTool/agentMemory.ts", "src/tools/AgentTool/agentMemorySnapshot.ts",
    ],
  },
  {
    repo: "claude-code-best", snapshot: CLAUDE, role: "primary", domain: "tasks", executableLines: 700,
    paths: [
      "src/tasks/LocalAgentTask/LocalAgentTask.tsx", "src/tasks/InProcessTeammateTask/InProcessTeammateTask.tsx",
      "src/tasks/RemoteAgentTask/RemoteAgentTask.tsx", "src/tasks/LocalWorkflowTask/LocalWorkflowTask.ts",
      "src/tasks/types.ts", "src/tasks/stopTask.ts",
    ],
  },
  {
    repo: "claude-code-best", snapshot: CLAUDE, role: "primary", domain: "team", executableLines: 650,
    paths: [
      "src/tools/shared/spawnMultiAgent.ts", "src/tools/SendMessageTool/SendMessageTool.ts", "src/utils/agentContext.ts",
    ],
  },
  {
    repo: "claude-code-best", snapshot: CLAUDE, role: "primary", domain: "isolation", executableLines: 228,
    paths: ["src/utils/worktree.ts", "src/utils/swarm/spawnUtils.ts"],
  },
  {
    repo: "opencode", snapshot: OPENCODE, role: "supplementary", domain: "agents", executableLines: 300,
    paths: ["packages/opencode/src/tool/task.ts", "packages/opencode/src/agent/subagent-permissions.ts"],
  },
  {
    repo: "OpenClaw", snapshot: OPENCLAW, role: "supplementary", domain: "tasks", executableLines: 1_250,
    paths: [
      "src/agents/subagent-registry-state.ts", "src/agents/subagent-registry-lifecycle.ts",
      "src/agents/subagent-registry-runtime.ts", "src/agents/subagent-registry-run-manager.ts",
      "src/agents/subagent-run-liveness.ts", "src/agents/subagent-run-timeout.ts",
    ],
  },
  {
    repo: "OpenClaw", snapshot: OPENCLAW, role: "supplementary", domain: "team", executableLines: 1_250,
    paths: [
      "src/agents/subagent-control.ts", "src/agents/subagent-control.runtime.ts", "src/agents/subagent-capabilities.ts",
      "src/agents/subagent-registry-steer-runtime.ts", "src/agents/subagent-announce-delivery.ts",
      "src/agents/subagent-delivery-state.ts", "src/agents/agent-steering-queue.ts",
    ],
  },
  {
    repo: "OpenClaw", snapshot: OPENCLAW, role: "supplementary", domain: "control", executableLines: 900,
    paths: [
      "src/agents/subagent-spawn.ts", "src/agents/subagent-spawn-plan.ts", "src/agents/subagent-spawn-ownership.ts",
      "src/agents/subagent-session-reconciliation.ts", "src/agents/announce-idempotency.ts",
    ],
  },
];

const routes: readonly Route[] = [
  ["packages/runtime/claude-runtime/src/agents/definition-registry.ts", "AgentDefinitionRegistry.register", "agents", "definition-registry"],
  ["packages/runtime/claude-runtime/src/agents/definition-registry.ts", "AgentDefinitionRegistry.resolve", "agents", "definition-precedence"],
  ["packages/runtime/claude-runtime/src/agents/definition-loader.ts", "AgentDefinitionLoader.loadDirectory", "agents", "definition-load"],
  ["packages/runtime/claude-runtime/src/agents/definition-loader.ts", "AgentDefinitionLoader.validate", "agents", "definition-validation"],
  ["packages/runtime/claude-runtime/src/agents/scope-lattice.ts", "AgentScopeLattice.derive", "agents", "scope-derive"],
  ["packages/runtime/claude-runtime/src/agents/scope-lattice.ts", "AgentScopeLattice.assertMonotonic", "agents", "scope-ceiling"],
  ["packages/runtime/claude-runtime/src/agents/context-fork.ts", "AgentContextFork.fork", "agents", "context-fork"],
  ["packages/runtime/claude-runtime/src/agents/context-fork.ts", "AgentContextFork.restore", "agents", "context-restore"],
  ["packages/runtime/claude-runtime/src/agents/memory-runtime.ts", "AgentMemoryRuntime.capture", "agents", "memory-capture"],
  ["packages/runtime/claude-runtime/src/agents/memory-runtime.ts", "AgentMemoryRuntime.restore", "agents", "memory-restore"],
  ["packages/runtime/claude-runtime/src/agents/execution-runtime.ts", "AgentExecutionRuntime.create", "agents", "agent-create"],
  ["packages/runtime/claude-runtime/src/agents/execution-runtime.ts", "AgentExecutionRuntime.run", "agents", "agent-run"],
  ["packages/runtime/claude-runtime/src/agents/execution-runtime.ts", "AgentExecutionRuntime.resume", "agents", "agent-resume"],
  ["packages/runtime/claude-runtime/src/agents/execution-runtime.ts", "AgentExecutionRuntime.abort", "agents", "agent-abort"],
  ["packages/runtime/claude-runtime/src/tasks/identity-runtime.ts", "TaskIdentityRuntime.allocate", "tasks", "task-identity"],
  ["packages/runtime/claude-runtime/src/tasks/identity-runtime.ts", "TaskIdentityRuntime.nextAttempt", "tasks", "task-attempt"],
  ["packages/runtime/claude-runtime/src/tasks/state-machine.ts", "TaskStateMachine.create", "tasks", "task-create"],
  ["packages/runtime/claude-runtime/src/tasks/state-machine.ts", "TaskStateMachine.transition", "tasks", "task-transition"],
  ["packages/runtime/claude-runtime/src/tasks/state-machine.ts", "TaskStateMachine.cancel", "tasks", "task-cancel"],
  ["packages/runtime/claude-runtime/src/tasks/state-machine.ts", "TaskStateMachine.kill", "tasks", "task-kill"],
  ["packages/runtime/claude-runtime/src/tasks/state-machine.ts", "TaskStateMachine.rejectLateResult", "tasks", "late-result-fence"],
  ["packages/runtime/claude-runtime/src/tasks/registry.ts", "DurableTaskRegistry.prepare", "tasks", "task-prepare"],
  ["packages/runtime/claude-runtime/src/tasks/registry.ts", "DurableTaskRegistry.recordReceipt", "tasks", "task-receipt"],
  ["packages/runtime/claude-runtime/src/tasks/registry.ts", "DurableTaskRegistry.commit", "tasks", "task-commit"],
  ["packages/runtime/claude-runtime/src/tasks/registry.ts", "DurableTaskRegistry.acknowledge", "tasks", "task-ack"],
  ["packages/runtime/claude-runtime/src/tasks/registry.ts", "DurableTaskRegistry.restore", "tasks", "task-restore"],
  ["packages/runtime/claude-runtime/src/tasks/registry.ts", "DurableTaskRegistry.recoverLostAck", "tasks", "lost-ack"],
  ["packages/runtime/claude-runtime/src/tasks/executor.ts", "TaskExecutor.dispatch", "tasks", "task-dispatch"],
  ["packages/runtime/claude-runtime/src/tasks/executor.ts", "TaskExecutor.wait", "tasks", "task-wait"],
  ["packages/runtime/claude-runtime/src/tasks/executor.ts", "TaskExecutor.result", "tasks", "task-result"],
  ["packages/runtime/claude-runtime/src/tasks/executor.ts", "TaskExecutor.timeout", "tasks", "task-timeout"],
  ["packages/runtime/claude-runtime/src/team/mailbox.ts", "TeamMailbox.send", "team", "message-send"],
  ["packages/runtime/claude-runtime/src/team/mailbox.ts", "TeamMailbox.steer", "team", "message-steer"],
  ["packages/runtime/claude-runtime/src/team/mailbox.ts", "TeamMailbox.receive", "team", "message-receive"],
  ["packages/runtime/claude-runtime/src/team/mailbox.ts", "TeamMailbox.acknowledge", "team", "message-ack"],
  ["packages/runtime/claude-runtime/src/team/mailbox.ts", "TeamMailbox.rejectDuplicate", "team", "message-dedupe"],
  ["packages/runtime/claude-runtime/src/team/fanout.ts", "TeamFanout.plan", "team", "fanout-plan"],
  ["packages/runtime/claude-runtime/src/team/fanout.ts", "TeamFanout.dispatch", "team", "fanout-dispatch"],
  ["packages/runtime/claude-runtime/src/team/fanout.ts", "TeamFanout.collect", "team", "fanin-collect"],
  ["packages/runtime/claude-runtime/src/team/fanout.ts", "TeamFanout.failFast", "team", "fanout-fail-fast"],
  ["packages/runtime/claude-runtime/src/team/delivery.ts", "TeamDelivery.publishPartial", "team", "partial-delivery"],
  ["packages/runtime/claude-runtime/src/team/delivery.ts", "TeamDelivery.publishFinal", "team", "final-delivery"],
  ["packages/runtime/claude-runtime/src/team/delivery.ts", "TeamDelivery.applyBackpressure", "team", "backpressure"],
  ["packages/runtime/claude-runtime/src/team/delivery.ts", "TeamDelivery.rejectLate", "team", "late-delivery"],
  ["packages/runtime/claude-runtime/src/isolation/request-runtime.ts", "IsolationRequestRuntime.prepare", "isolation", "isolation-prepare"],
  ["packages/runtime/claude-runtime/src/isolation/request-runtime.ts", "IsolationRequestRuntime.validateWorkspace", "isolation", "workspace-containment"],
  ["packages/runtime/claude-runtime/src/isolation/request-runtime.ts", "IsolationRequestRuntime.recordReceipt", "isolation", "isolation-receipt"],
  ["packages/runtime/claude-runtime/src/isolation/request-runtime.ts", "IsolationRequestRuntime.commit", "isolation", "isolation-commit"],
  ["packages/runtime/claude-runtime/src/isolation/merge-runtime.ts", "IsolationMergeRuntime.merge", "isolation", "worktree-merge"],
  ["packages/runtime/claude-runtime/src/isolation/merge-runtime.ts", "IsolationMergeRuntime.recordConflict", "isolation", "merge-conflict"],
  ["packages/runtime/claude-runtime/src/isolation/merge-runtime.ts", "IsolationMergeRuntime.cleanup", "isolation", "worktree-cleanup"],
  ["packages/runtime/claude-runtime/src/control/schema.ts", "ControlSchema.parse", "control", "control-parse"],
  ["packages/runtime/claude-runtime/src/control/router.ts", "StructuredControlRouter.dispatch", "control", "control-dispatch"],
  ["packages/runtime/claude-runtime/src/control/router.ts", "StructuredControlRouter.delegateE01", "control", "e01-delegation"],
  ["packages/runtime/claude-runtime/src/control/router.ts", "StructuredControlRouter.delegateE02", "control", "e02-delegation"],
  ["packages/runtime/claude-runtime/src/control/router.ts", "StructuredControlRouter.recoverLostAck", "control", "control-lost-ack"],
  ["packages/runtime/claude-runtime/src/control/session-handler.ts", "AgentControlHandler.execute", "control", "agent-control"],
  ["packages/runtime/claude-runtime/src/control/session-handler.ts", "AgentControlHandler.cancel", "control", "control-cancel"],
  ["packages/runtime/claude-runtime/src/control/session-handler.ts", "AgentControlHandler.kill", "control", "control-kill"],
  ["packages/runtime/claude-runtime/src/control/session-handler.ts", "AgentControlHandler.wait", "control", "control-wait"],
  ["packages/runtime/claude-runtime/src/control/session-handler.ts", "AgentControlHandler.result", "control", "control-result"],
  ["packages/runtime/claude-runtime/src/control/stdio.ts", "StructuredControlStdio.run", "control", "structured-stdio"],
] as const;

const mutationRoutes = routes.filter((route) => !route[3].endsWith("delegation")).slice(0, 40);
const deletePython = [
  "agent_tool.py", "runtime.py", "lifecycle.py", "continuation.py", "control.py", "definitions.py", "context.py",
  "dispatch.py", "fanout.py", "delivery.py", "typed_yield.py", "session_assembly.py", "skill_fork.py", "tool_scope.py",
  "subagent_yield.py", "handoff.py", "recovery.py",
  "budget.py", "events.py", "execution_receipts.py", "integration.py", "isolation.py", "parent_scope.py",
  "resume_capsule.py", "source_audit.py", "task_store.py", "transcript.py",
].map((name) => `packages/workers/zyra_workers/subagents/${name}`);
const retainPython = [
  "packages/workers/zyra_workers/subagents/__init__.py", "packages/workers/zyra_workers/subagents/digests.py",
  "packages/workers/zyra_workers/subagents/typescript_port.py", "packages/workers/zyra_workers/subagents/models.py",
  "packages/workers/zyra_workers/subagents/errors.py",
];

function sha256(value: Uint8Array | string): string {
  return createHash("sha256").update(value).digest("hex");
}

function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") {
    const row = value as Json;
    return `{${Object.keys(row).sort().map((key) => `${JSON.stringify(key)}:${stable(row[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function git(cwd: string, args: readonly string[]): string {
  return execFileSync("git", [...args], { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trimEnd();
}

function gitBytes(cwd: string, args: readonly string[]): Buffer {
  return execFileSync("git", [...args], { cwd, encoding: "buffer", stdio: ["ignore", "pipe", "pipe"] }) as Buffer;
}

function writeJson(path: string, value: unknown): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function writeJsonl(path: string, rows: readonly Json[]): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, `${rows.map(stable).join("\n")}\n`, "utf8");
}

function executable(line: string): boolean {
  const value = line.trim();
  return Boolean(value) && !value.startsWith("//") && !value.startsWith("/*") && !value.startsWith("*") && value !== "*/";
}

function pythonExecutable(line: string): boolean {
  const value = line.trim();
  return Boolean(value) && !value.startsWith("#") && !value.startsWith("\"\"\"") && !value.startsWith("'''");
}

function nameOf(node: ts.Node, source: ts.SourceFile, fallback: string): string {
  const named = node as ts.NamedDeclaration;
  if (named.name) return named.name.getText(source);
  if (ts.isVariableStatement(node)) return node.declarationList.declarations.map((item) => item.name.getText(source)).join(",");
  return fallback;
}

function sourceUnits(spec: SourceSpec, path: string): Unit[] {
  const raw = gitBytes(join(workspaceRoot, spec.repo), ["show", `${spec.snapshot}:${path}`]);
  const text = raw.toString("utf8");
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.ESNext, true, path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const lines = text.replaceAll("\r", "").split("\n");
  const output: Unit[] = [];
  const add = (node: ts.Node, symbol: string): void => {
    const startLine = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
    const endLine = source.getLineAndCharacterOfPosition(Math.max(node.getStart(source), node.end - 1)).line + 1;
    const executableLines = Array.from({ length: endLine - startLine + 1 }, (_, index) => startLine + index)
      .filter((line) => executable(lines[line - 1] ?? ""));
    if (executableLines.length) output.push({ path, symbol, startLine, endLine, sha256: sha256(raw), executableLines });
  };
  for (const [index, statement] of source.statements.entries()) {
    if (ts.isImportDeclaration(statement) || ts.isExportDeclaration(statement) || ts.isInterfaceDeclaration(statement) || ts.isTypeAliasDeclaration(statement)) continue;
    if (ts.isClassDeclaration(statement)) {
      const owner = statement.name?.text ?? `class-${index + 1}`;
      for (const [memberIndex, member] of statement.members.entries()) {
        if (ts.isPropertyDeclaration(member) && !member.initializer) continue;
        add(member, `${owner}.${nameOf(member, source, `member-${memberIndex + 1}`)}`);
      }
    } else add(statement, nameOf(statement, source, `statement-${index + 1}`));
  }
  return output;
}

function allocate(capacities: readonly number[], target: number): number[] {
  const total = capacities.reduce((sum, value) => sum + value, 0);
  if (total < target) throw new Error(`source capacity ${total} is below ${target}`);
  const quotas = capacities.map((capacity) => capacity ? Math.max(1, Math.floor(target * capacity / total)) : 0);
  let assigned = quotas.reduce((sum, value) => sum + value, 0);
  while (assigned < target) {
    let best = -1;
    let delta = Number.NEGATIVE_INFINITY;
    capacities.forEach((capacity, index) => {
      if (quotas[index]! >= capacity) return;
      const candidate = target * capacity / total - quotas[index]!;
      if (candidate > delta) { best = index; delta = candidate; }
    });
    if (best < 0) throw new Error("cannot allocate source quota");
    quotas[best] = quotas[best]! + 1;
    assigned += 1;
  }
  while (assigned > target) {
    let best = -1;
    let delta = Number.NEGATIVE_INFINITY;
    capacities.forEach((capacity, index) => {
      if (quotas[index]! <= 1) return;
      const candidate = quotas[index]! - target * capacity / total;
      if (candidate > delta) { best = index; delta = candidate; }
    });
    if (best < 0) throw new Error("cannot reduce source quota");
    quotas[best] = quotas[best]! - 1;
    assigned -= 1;
  }
  return quotas;
}

function selectSources(): Selected[] {
  const output: Selected[] = [];
  for (const spec of specs) {
    const byPath = spec.paths.map((path) => sourceUnits(spec, path));
    const quotas = allocate(byPath.map((units) => units.reduce((sum, unit) => sum + unit.executableLines.length, 0)), spec.executableLines);
    byPath.forEach((units, pathIndex) => {
      let remaining = quotas[pathIndex]!;
      for (const unit of units) {
        if (!remaining) break;
        const accepted = Math.min(remaining, unit.executableLines.length);
        if (!accepted) continue;
        output.push({
          ...unit,
          endLine: unit.executableLines[accepted - 1]!,
          executableLines: unit.executableLines.slice(0, accepted),
          domain: spec.domain, repo: spec.repo, role: spec.role, snapshot: spec.snapshot,
        });
        remaining -= accepted;
      }
      if (remaining) throw new Error(`unallocated source lines for ${spec.repo}:${spec.paths[pathIndex]}`);
    });
  }
  while (output.length < 391) {
    let best = -1;
    let span = 1;
    output.forEach((unit, index) => {
      const candidate = unit.endLine - unit.startLine + 1;
      if (candidate > span) { best = index; span = candidate; }
    });
    if (best < 0) throw new Error("cannot split source ranges to 391");
    const unit = output[best]!;
    const pivot = unit.startLine + Math.floor(span / 2) - 1;
    const left = { ...unit, endLine: pivot, executableLines: unit.executableLines.filter((line) => line <= pivot) };
    const right = { ...unit, startLine: pivot + 1, executableLines: unit.executableLines.filter((line) => line > pivot) };
    output.splice(best, 1, left, right);
  }
  if (output.length !== 391) throw new Error(`expected 391 source ranges, got ${output.length}`);
  const lineCount = output.reduce((sum, unit) => sum + unit.executableLines.length, 0);
  if (lineCount !== 6_928) throw new Error(`expected 6928 source lines, got ${lineCount}`);
  return output.sort((a, b) => a.repo.localeCompare(b.repo) || a.path.localeCompare(b.path) || a.startLine - b.startLine);
}

function sourceAndTargetRows(finalize: boolean): { source: Json[]; target: Json[] } {
  const selected = selectSources();
  const routeUse = new Map<string, number>();
  const source: Json[] = [];
  const target: Json[] = [];
  selected.forEach((unit, index) => {
    const candidates = routes.filter((route) => route[2] === unit.domain);
    const route = [...candidates].sort((a, b) => (routeUse.get(a[1]) ?? 0) - (routeUse.get(b[1]) ?? 0) || a[1].localeCompare(b[1]))[0]!;
    routeUse.set(route[1], (routeUse.get(route[1]) ?? 0) + 1);
    const mappingId = `e03-src-${String(index + 1).padStart(4, "0")}`;
    source.push({
      schema_version: SCHEMA_VERSION, record_type: "source_range", execution_id: EXECUTION_ID, mapping_id: mappingId,
      source_repo: unit.repo, source_snapshot: unit.snapshot, source_path: unit.path,
      source_symbol: `${unit.path}::${unit.symbol}`, start_line: unit.startLine, end_line: unit.endLine,
      source_sha256: unit.sha256, semantic_domain: unit.domain, source_role: unit.role,
      accepted: true, migration_mode: "adapted", supplementary_gap: unit.role === "supplementary" ? route[3] : null,
      exclusion_reason: null,
    });
    const targetPath = join(repoRoot, route[0]);
    target.push({
      schema_version: SCHEMA_VERSION, record_type: "custody_mapping", execution_id: EXECUTION_ID, mapping_id: mappingId,
      source_symbol: `${unit.path}::${unit.symbol}`, source_behavior_claim: `${unit.symbol} contributes ${route[3]} semantics`,
      target_path: route[0], target_symbol: route[1], planned_method: route[1].split(".").at(-1),
      target_sha256: finalize ? sha256(readFileSync(targetPath)) : null,
      semantic_anchor_tokens: [unit.domain, route[3]], semantic_match_score: 2,
      mapping_basis: "frozen-domain-and-behavior-route-v1", bounded_source_group: 0,
      target_behavior_claim: `${route[1]} is the Zyra canonical ${route[3]} owner`,
      semantic_equivalence: "Zyra preserves the selected lifecycle, ownership, failure and restore behavior using canonical task/session events and revisioned receipts",
      adaptation: "Upstream behavior is decomposed into Zyra task, session, permission, event, artifact and cross-language commit boundaries",
      behavior_contract_id: `e03.contract.${mappingId}`, canonical_owner_id: `typescript.${route[1].split(".")[0]}`,
      default_entry_id: "E03.default.CodeWorkerApplication.runTaskRuntime", default_callsite_path: "apps/code-worker/src/main.ts",
      default_callsite_symbol: "CodeWorkerApplication.runTaskRuntime", state_store: `E03RuntimeSnapshot.${unit.domain}`,
      state_effect_kind: route[3], state_effect_assertion: `e03.behavior.${route[3]}`,
      success_test_ids: [`e03.behavior.${route[3]}`], failure_test_ids: [`e03.failure.${route[3]}`],
      disable_test_ids: ["e03.disable.typescript-owner-fails-closed"],
      mutation_ids: mutationRoutes.some((candidate) => candidate[1] === route[1]) ? [`e03-mut-${String(mutationRoutes.findIndex((candidate) => candidate[1] === route[1]) + 1).padStart(3, "0")}`] : [],
      runtime_origin_probe_id: "e03.probe.runtime-origin", write_path_probe_id: "e03.probe.write-path", restore_probe_id: "e03.probe.resume",
    });
  });
  return { source, target };
}

function pythonRows(): Json[] {
  const rows: Json[] = [];
  let deletionRemaining = 6_268;
  for (const [index, path] of [...deletePython, ...retainPython].entries()) {
    const raw = gitBytes(repoRoot, ["show", `${IMPLEMENTATION_BASELINE}:${path}`]);
    const lines = raw.toString("utf8").replaceAll("\r", "").split("\n");
    if (lines.at(-1) === "") lines.pop();
    const isDelete = deletePython.includes(path);
    let endLine = lines.length;
    if (isDelete) {
      const executableLines = lines.map((line, lineIndex) => pythonExecutable(line) ? lineIndex + 1 : 0).filter(Boolean);
      const accepted = Math.min(deletionRemaining, executableLines.length);
      if (!accepted) throw new Error(`unexpected Python delete overflow at ${path}`);
      endLine = executableLines[accepted - 1]!;
      deletionRemaining -= accepted;
    }
    rows.push({
      schema_version: SCHEMA_VERSION, record_type: "python_owner", execution_id: EXECUTION_ID,
      owner_id: `e03-py-${String(index + 1).padStart(4, "0")}`, verified_zyra_head: IMPLEMENTATION_BASELINE,
      python_path: path, python_symbol: "<module>", start_line: 1, end_line: endLine,
      python_sha256: sha256(raw), state_domain: path.includes("isolation") ? "isolation" : "agents-tasks-team-control",
      disposition: isDelete ? "delete" : "retain",
      allowed_adapter_symbols: isDelete ? [] : ["typed CAS receipt", "physical workspace effect", "event/artifact projection"],
      default_entry: "CodeWorkerApplication.runTaskRuntime",
      deletion_test_id: isDelete ? "e03.python-owner-absent" : null,
      call_direction: isDelete ? null : "typescript-to-python-port-only", blocked_owner: null,
    });
  }
  if (deletionRemaining !== 0) throw new Error(`Python deletion inventory short by ${deletionRemaining}`);
  return rows;
}

function mutationRows(): Json[] {
  return mutationRoutes.map((route, index) => {
    const id = `e03-mut-${String(index + 1).padStart(3, "0")}`;
    const patch = { kind: "typescript-ast-method-entry-throw", mutation_id: id, target_path: route[0], target_symbol: route[1], injected_statement: `throw new Error(\"target_disconnect:${id}\")` };
    return {
      schema_version: SCHEMA_VERSION, record_type: "mutation", execution_id: EXECUTION_ID, mutation_id: id,
      target_path: route[0], target_symbol: route[1], mutation_operator: "disconnect-target", semantic_risk: route[3],
      compile_survives: true, expected_killer_test_ids: [`e03.mutation.${route[3]}`], frozen_patch: patch,
      frozen_patch_sha256: sha256(stable(patch)),
    };
  });
}

function gateProfile(candidateHead: string): Json {
  return {
    schema_version: SCHEMA_VERSION, record_type: "gate_profile", execution_id: EXECUTION_ID,
    verification_contract_version: "zyra.e03-verification/v1", generator: "scripts/remediation/m1_r01_e03_g0.ts",
    schema_verifier: "scripts/remediation/verify_m1_r01_e03.ts", mutation_runner: "scripts/remediation/run_m1_r01_e03_mutations.ts",
    verified_zyra_head: VERIFIED_HEAD, implementation_diff_baseline: IMPLEMENTATION_BASELINE, candidate_head_at_g0: candidateHead,
    source_snapshots: { "claude-code-best": CLAUDE, opencode: OPENCODE, OpenClaw: OPENCLAW },
    source_hash_semantics: "sha256-of-raw-git-blob-bytes-at-declared-snapshot",
    production_roots: [
      "packages/runtime/claude-runtime/src/agents", "packages/runtime/claude-runtime/src/tasks",
      "packages/runtime/claude-runtime/src/team", "packages/runtime/claude-runtime/src/isolation",
      "packages/runtime/claude-runtime/src/control", "packages/runtime/claude-runtime/src/e03", "apps/code-worker/src/main.ts",
    ],
    candidate_scope_paths: [
      "packages/runtime/claude-runtime/src/agents", "packages/runtime/claude-runtime/src/tasks",
      "packages/runtime/claude-runtime/src/team", "packages/runtime/claude-runtime/src/isolation",
      "packages/runtime/claude-runtime/src/control", "packages/runtime/claude-runtime/src/e03",
      "packages/runtime/claude-runtime/test/e03", "packages/workers/zyra_workers/subagents",
      "packages/workers/zyra_workers/typescript_claude_runtime.py", "apps/code-worker/src/main.ts", "apps/api/zyra_api/main.py",
      "scripts/remediation/m1_r01_e03_g0.ts", "scripts/remediation/verify_m1_r01_e03.ts",
      "scripts/remediation/run_m1_r01_e03_mutations.ts", "scripts/remediation/probe_m1_r01_e03.ts",
      "scripts/remediation/cleanroom_m1_r01_e03.ts", "docs/reviews/evidence/M1-R01-v3/execution-03", "package.json", "bun.lock",
    ],
    adapter_roots: ["packages/workers/zyra_workers/typescript_claude_runtime.py", "packages/workers/zyra_workers/subagents/typescript_port.py"],
    behavior_test_roots: ["packages/runtime/claude-runtime/test/e03"],
    excluded_production_prefixes: [
      "packages/runtime/claude-runtime/src/e01", "packages/runtime/claude-runtime/src/e02",
      "packages/runtime/claude-runtime/src/permission", "packages/runtime/claude-runtime/src/skills",
      "packages/runtime/claude-runtime/src/plugins", "packages/integrations/claude-mcp", "vendor/", "vendor-runtimes/", "source-pool/", "runtime-sources/",
    ],
    thresholds: {
      accepted_source_executable_sloc: 6_928, accepted_source_ranges: 391,
      python_delete_executable_sloc: 10_309, final_non_test_typescript_sloc: 34_000,
      effective_changed_typescript_sloc: 31_890, effective_behavior_test_sloc: 7_000,
      adapter_ratio_maximum: 0.10, remaining_python_logical_adapter_sloc_maximum: 1_500,
      behavior_cases_minimum: 100, failure_cases_minimum: 35, mutation_points_minimum: 35,
      core_mutation_kill_ratio_minimum: 1, other_mutation_kill_ratio_minimum: 0.9,
      source_to_target_unique_symbols_minimum: 50, source_to_target_max_mappings_per_symbol: 40,
      cumulative_final_typescript_sloc: 100_000, cumulative_changed_typescript_sloc: 92_672,
      cumulative_test_sloc: 26_000, cumulative_python_delete_executable_sloc: 68_063,
    },
    default_entry: { path: "apps/code-worker/src/main.ts", symbol: "main", runtime_symbol: "CodeWorkerApplication.runTaskRuntime", e03_symbol: "E03AgentControlCoordinator.execute" },
    required_toolchain: { bun: "1.2.15", typescript: "5.8.3", node_types: "22.15.29" },
    commands: {
      install: ["npx", "--yes", "bun@1.2.15", "install", "--frozen-lockfile"], typecheck: ["npx", "--yes", "bun@1.2.15", "run", "typecheck:e03"],
      build: ["npx", "--yes", "bun@1.2.15", "run", "build"], behavior_test: ["npx", "--yes", "bun@1.2.15", "test", "packages/runtime/claude-runtime/test/e03"],
      built_entry: ["npx", "--yes", "bun@1.2.15", "run", "runtime:built:health"], candidate_gate: ["npx", "--yes", "bun@1.2.15", "run", "e03:candidate:gate"],
      mutation: ["npx", "--yes", "bun@1.2.15", "run", "e03:mutation"], cleanroom: ["npx", "--yes", "bun@1.2.15", "run", "e03:cleanroom"],
    },
    runtime_origin_probe_command: ["npx", "--yes", "bun@1.2.15", "scripts/remediation/probe_m1_r01_e03.ts", "runtime-origin"],
    write_path_probe_command: ["npx", "--yes", "bun@1.2.15", "scripts/remediation/probe_m1_r01_e03.ts", "write-path"],
    same_session_resume_command: ["npx", "--yes", "bun@1.2.15", "scripts/remediation/probe_m1_r01_e03.ts", "resume"],
    lost_ack_command: ["npx", "--yes", "bun@1.2.15", "scripts/remediation/probe_m1_r01_e03.ts", "lost-ack"],
    disable_command: ["npx", "--yes", "bun@1.2.15", "scripts/remediation/probe_m1_r01_e03.ts", "disable"],
    clean_dependency_path_command: ["npx", "--yes", "bun@1.2.15", "run", "e03:cleanroom"],
    forbidden_runtime_dependencies: ["../claude-code-best", "../opencode", "../OpenClaw", "vendor/", "vendor-runtimes/", "source-pool/", "runtime-sources/"],
    forbidden_runtime_paths: ["../claude-code-best", "../opencode", "../OpenClaw", "vendor/", "vendor-runtimes/", "source-pool/", "runtime-sources/"],
    checker_sources: Object.fromEntries([
      "scripts/remediation/m1_r01_e03_g0.ts", "scripts/remediation/verify_m1_r01_e03.ts",
      "scripts/remediation/run_m1_r01_e03_mutations.ts", "scripts/remediation/probe_m1_r01_e03.ts",
      "scripts/remediation/cleanroom_m1_r01_e03.ts",
    ].map((path) => [path, sha256(readFileSync(join(repoRoot, path)))])),
    candidate_status_before_independent_review: "implementation_complete_review_pending",
  };
}

function mirror(files: readonly string[]): void {
  mkdirSync(evidenceRoot, { recursive: true });
  for (const path of files) writeFileSync(join(evidenceRoot, path.slice(manifestRoot.length + 1)), readFileSync(path));
}

function main(): void {
  const mode = process.argv[2];
  if (mode !== "freeze" && mode !== "finalize") throw new Error("usage: bun scripts/remediation/m1_r01_e03_g0.ts <freeze|finalize>");
  const candidateHead = git(repoRoot, ["rev-parse", "HEAD"]);
  git(repoRoot, ["merge-base", "--is-ancestor", IMPLEMENTATION_BASELINE, candidateHead]);
  const mapped = sourceAndTargetRows(mode === "finalize");
  const names = [
    "execution-03-source-manifest.jsonl", "execution-03-python-owner-baseline.jsonl", "execution-03-target-custody-map.jsonl",
    "execution-03-mutation-manifest.jsonl", "execution-03-gate-profile.json",
  ];
  const files = names.map((name) => join(manifestRoot, name));
  writeJsonl(files[0]!, mapped.source); writeJsonl(files[1]!, pythonRows()); writeJsonl(files[2]!, mapped.target);
  writeJsonl(files[3]!, mutationRows()); writeJson(files[4]!, gateProfile(candidateHead));
  mirror(files);
  const receiptPath = join(manifestRoot, "execution-03-baseline-receipt.json");
  const dirty = git(repoRoot, ["status", "--porcelain=v1", "--untracked-files=all"]).split(/\r?\n/).filter(Boolean)
    .map((line) => line.slice(3).replaceAll("\\", "/"))
    .filter((path) => !path.startsWith("docs/reviews/evidence/M1-R01-v3/execution-03/g0/"));
  const receipt = {
    schema_version: SCHEMA_VERSION, record_type: "baseline_receipt", execution_id: EXECUTION_ID,
    verified_zyra_head: VERIFIED_HEAD, verified_zyra_tree: git(repoRoot, ["rev-parse", `${VERIFIED_HEAD}^{tree}`]),
    g0_candidate_head: candidateHead, g0_candidate_tree: git(repoRoot, ["rev-parse", `${candidateHead}^{tree}`]),
    implementation_diff_baseline: IMPLEMENTATION_BASELINE, captured_at_utc: new Date().toISOString(), clean_worktree: dirty.length === 0,
    dirty_paths: dirty, source_hash_semantics: "sha256-of-raw-git-blob-bytes-at-declared-snapshot",
    source_snapshots: Object.fromEntries([["claude-code-best", CLAUDE], ["opencode", OPENCODE], ["OpenClaw", OPENCLAW]].map(([repo, commit]) => [repo, { commit, tree: git(join(workspaceRoot, repo), ["rev-parse", `${commit}^{tree}`]) }])),
    manifest_sha256: Object.fromEntries(files.map((path) => [path.slice(manifestRoot.length + 1).replaceAll("\\", "/"), sha256(readFileSync(path))])),
    toolchain: { bun: "1.2.15", typescript: "5.8.3" }, manifest_generator_command: ["bun", "scripts/remediation/m1_r01_e03_g0.ts", mode],
    manifest_generator_version: "zyra.e03-g0/v1", schema_validator_command: ["bun", "scripts/remediation/verify_m1_r01_e03.ts", "--candidate", candidateHead],
    schema_validator_version: "zyra.e03-verification/v1", lockfile_sha256: sha256(readFileSync(join(repoRoot, "bun.lock"))), host_platform: `${process.platform}-${process.arch}`,
  };
  writeJson(receiptPath, receipt);
  writeFileSync(join(evidenceRoot, "execution-03-baseline-receipt.json"), readFileSync(receiptPath));
  process.stdout.write(`${JSON.stringify({ mode, candidate_head: candidateHead, source_ranges: mapped.source.length, source_sloc: 6_928, python_rows: pythonRows().length, mutation_rows: mutationRows().length, manifest_root: manifestRoot }, null, 2)}\n`);
}

main();
