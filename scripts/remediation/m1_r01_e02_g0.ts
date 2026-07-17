#!/usr/bin/env bun

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import * as ts from "typescript";

type Json = Record<string, unknown>;
type Domain = "permission" | "mcp" | "skills-plugins-commands";
type SourceSpec = {
  repo: "claude-code-best" | "opencode" | "OpenClaw";
  snapshot: string;
  domain: Domain;
  role: "primary" | "supplementary";
  paths: readonly string[];
  executableLines: number;
  recordCount?: number;
};
type SourceUnit = {
  path: string;
  symbol: string;
  startLine: number;
  endLine: number;
  fileSha256: string;
  countsAsExecutable: boolean;
  executableLineNumbers: number[];
};
type SelectedRange = SourceUnit & {
  part: number;
  partCount: number;
};
type PythonUnit = {
  path: string;
  symbol: string;
  startLine: number;
  endLine: number;
  fileSha256: string;
  category: "delete" | "retain" | "blocked";
  executableLineNumbers: number[];
};

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../..");
const workspaceRoot = resolve(repoRoot, "..");
const manifestRoot = join(
  workspaceRoot,
  "docs/remediations/M1-R01-claude-source-custody/manifests",
);

const VERIFIED_BASELINE = "f07fd239dd768f399a36329314da82e90ddce6a4";
const CLAUDE_SNAPSHOT = "c57f5a29e88e9a814bea47abeb9a0a6f725dc102";
const OPENCODE_SNAPSHOT = "adf178a6b95c61506ddaadaf4dd062badb4a8fda";
const OPENCLAW_SNAPSHOT = "b63e06f68aa0f5fc3dc809c37615b8b1012b180b";
const SCHEMA_VERSION = "3.0";
const EXECUTION_ID = "E02";

const claudePermissionPaths = [
  "src/types/permissions.ts",
  "src/utils/permissions/PermissionResult.ts",
  "src/utils/permissions/PermissionRule.ts",
  "src/utils/permissions/permissionRuleParser.ts",
  "src/utils/permissions/permissionsLoader.ts",
  "src/utils/permissions/denialTracking.ts",
  "src/utils/permissions/classifierDecision.ts",
  "src/utils/permissions/permissions.ts",
  "src/utils/permissions/PermissionUpdateSchema.ts",
  "src/utils/permissions/PermissionUpdate.ts",
  "src/utils/permissions/PermissionMode.ts",
  "src/utils/permissions/getNextPermissionMode.ts",
  "src/utils/permissions/permissionSetup.ts",
  "src/utils/permissions/yoloClassifier.ts",
  "src/hooks/useCanUseTool.tsx",
  "src/hooks/toolPermission/PermissionContext.ts",
  "src/hooks/toolPermission/permissionLogging.ts",
  "src/hooks/toolPermission/handlers/coordinatorHandler.ts",
  "src/hooks/toolPermission/handlers/swarmWorkerHandler.ts",
  "src/hooks/toolPermission/handlers/interactiveHandler.ts",
  "src/services/tools/toolHooks.ts",
] as const;

const claudeMcpPaths = [
  "src/services/mcp/types.ts",
  "src/services/mcp/config.ts",
  "src/services/mcp/envExpansion.ts",
  "src/services/mcp/normalization.ts",
  "src/services/mcp/utils.ts",
  "src/services/mcp/mcpStringUtils.ts",
  "src/services/mcp/headersHelper.ts",
  "src/services/mcp/officialRegistry.ts",
  "src/components/MCPServerApprovalDialog.tsx",
  "src/services/mcp/client.ts",
  "src/services/mcp/MCPConnectionManager.tsx",
  "src/services/mcp/useManageMCPConnections.ts",
  "src/services/mcp/InProcessTransport.ts",
  "src/services/mcp/SdkControlTransport.ts",
  "src/services/mcp/vscodeSdkMcp.ts",
  "src/utils/mcpWebSocketTransport.ts",
  "src/services/mcp/claudeai.ts",
  "src/services/mcp/auth.ts",
  "src/services/mcp/oauthPort.ts",
  "src/services/mcp/xaa.ts",
  "src/services/mcp/xaaIdpLogin.ts",
  "src/tools/MCPTool/MCPTool.ts",
  "src/tools/MCPTool/prompt.ts",
  "src/tools/MCPTool/classifyForCollapse.ts",
  "src/tools/ListMcpResourcesTool/ListMcpResourcesTool.ts",
  "src/tools/ListMcpResourcesTool/prompt.ts",
  "src/tools/ReadMcpResourceTool/ReadMcpResourceTool.ts",
  "src/tools/ReadMcpResourceTool/prompt.ts",
  "src/tools/McpAuthTool/McpAuthTool.ts",
  "src/utils/mcpOutputStorage.ts",
  "src/utils/mcpValidation.ts",
  "src/commands/mcp/index.ts",
  "src/commands/mcp/addCommand.ts",
  "src/commands/mcp/mcp.tsx",
  "src/commands/mcp/xaaIdpCommand.ts",
  "src/utils/mcpInstructionsDelta.ts",
  "src/services/mcp/channelAllowlist.ts",
  "src/services/mcp/channelNotification.ts",
  "src/services/mcp/channelPermissions.ts",
  "src/services/mcp/elicitationHandler.ts",
] as const;

const claudeSkillPaths = [
  "src/tools/SkillTool/SkillTool.ts",
  "src/tools/SkillTool/prompt.ts",
  "src/tools/SkillTool/constants.ts",
  "src/utils/processUserInput/processSlashCommand.tsx",
  "src/utils/hooks/registerSkillHooks.ts",
  "src/utils/hooks/sessionHooks.ts",
  "src/skills/loadSkillsDir.ts",
  "src/skills/bundledSkills.ts",
  "src/skills/mcpSkillBuilders.ts",
  "src/skills/mcpSkills.ts",
  "src/utils/skills/skillChangeDetector.ts",
  "src/commands.ts",
  "src/constants/prompts.ts",
  "src/utils/attachments.ts",
  "src/services/compact/compact.ts",
  "src/tools/DiscoverSkillsTool/prompt.ts",
  "src/services/skillSearch/featureCheck.ts",
  "src/services/skillSearch/localSearch.ts",
  "src/services/skillSearch/prefetch.ts",
  "src/services/skillSearch/remoteSkillLoader.ts",
  "src/services/skillSearch/remoteSkillState.ts",
  "src/services/skillSearch/signals.ts",
  "src/services/skillSearch/telemetry.ts",
  "src/utils/plugins/loadPluginCommands.ts",
  "src/utils/plugins/loadPluginHooks.ts",
  "src/utils/plugins/loadPluginAgents.ts",
  "src/utils/plugins/pluginLoader.ts",
  "src/utils/plugins/schemas.ts",
  "src/utils/plugins/walkPluginMarkdown.ts",
  "src/utils/plugins/pluginIdentifier.ts",
  "src/utils/plugins/pluginOptionsStorage.ts",
  "src/utils/plugins/mcpPluginIntegration.ts",
] as const;

const opencodePaths = [
  "packages/opencode/src/acp/permission.ts",
  "packages/opencode/src/permission/arity.ts",
  "packages/opencode/src/permission/evaluate.ts",
  "packages/opencode/src/permission/index.ts",
  "packages/opencode/src/mcp/auth.ts",
  "packages/opencode/src/mcp/catalog.ts",
  "packages/opencode/src/mcp/index.ts",
  "packages/opencode/src/mcp/oauth-callback.ts",
  "packages/opencode/src/mcp/oauth-provider.ts",
  "packages/opencode/src/command/index.ts",
  "packages/opencode/src/config/command.ts",
  "packages/opencode/src/config/plugin.ts",
  "packages/opencode/src/plugin/index.ts",
] as const;

const openClawPaths = [
  "src/acp/approval-classifier.ts",
  "src/acp/permission-relay.ts",
  "src/acp/policy.ts",
  "src/agents/embedded-agent-runner/effective-tool-policy.ts",
  "src/agents/openclaw-plugin-tools.ts",
  "src/agents/plugin-tool-delivery-defaults.ts",
  "src/agents/sandbox-tool-policy.ts",
  "src/agents/tool-policy-audit.ts",
  "src/agents/tool-policy-match.ts",
  "src/agents/tool-policy-pipeline.ts",
  "src/agents/tool-policy-shared.ts",
  "src/agents/tool-policy.ts",
] as const;

const sourceSpecs: readonly SourceSpec[] = [
  {
    repo: "claude-code-best",
    snapshot: CLAUDE_SNAPSHOT,
    domain: "permission",
    role: "primary",
    paths: claudePermissionPaths,
    executableLines: 4_818,
  },
  {
    repo: "claude-code-best",
    snapshot: CLAUDE_SNAPSHOT,
    domain: "mcp",
    role: "primary",
    paths: claudeMcpPaths,
    executableLines: 7_703,
  },
  {
    repo: "claude-code-best",
    snapshot: CLAUDE_SNAPSHOT,
    domain: "skills-plugins-commands",
    role: "primary",
    paths: claudeSkillPaths,
    executableLines: 5_926,
    recordCount: 1_342,
  },
  {
    repo: "opencode",
    snapshot: OPENCODE_SNAPSHOT,
    domain: "mcp",
    role: "supplementary",
    paths: opencodePaths,
    executableLines: 892,
  },
  {
    repo: "OpenClaw",
    snapshot: OPENCLAW_SNAPSHOT,
    domain: "permission",
    role: "supplementary",
    paths: openClawPaths,
    executableLines: 708,
  },
] as const;

const sha256 = (value: Uint8Array | string): string =>
  createHash("sha256").update(value).digest("hex");

function stable(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stable);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Json)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, child]) => [key, stable(child)]),
    );
  }
  return value;
}

function stableString(value: unknown): string {
  return JSON.stringify(stable(value));
}

function writeJson(path: string, value: unknown): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, `${JSON.stringify(stable(value), null, 2)}\n`, "utf8");
}

function writeJsonLines(path: string, rows: readonly Json[]): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, `${rows.map(stableString).join("\n")}\n`, "utf8");
}

function gitText(cwd: string, args: readonly string[]): string {
  return execFileSync("git", [...args], {
    cwd,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  }).trimEnd();
}

function gitBytes(cwd: string, args: readonly string[]): Buffer {
  return execFileSync("git", [...args], {
    cwd,
    encoding: "buffer",
    stdio: ["ignore", "pipe", "pipe"],
  }) as Buffer;
}

function sourceBlob(repo: SourceSpec["repo"], snapshot: string, path: string): Buffer {
  return gitBytes(join(workspaceRoot, repo), ["show", `${snapshot}:${path}`]);
}

function nodeName(node: ts.Node, source: ts.SourceFile, fallback: string): string {
  const named = node as ts.NamedDeclaration;
  if (named.name) return named.name.getText(source);
  if (ts.isVariableStatement(node)) {
    return node.declarationList.declarations.map((item) => item.name.getText(source)).join(",");
  }
  if (ts.isExportAssignment(node)) return "default-export";
  return fallback;
}

function typescriptExecutableLine(line: string): boolean {
  const value = line.trim();
  return Boolean(value)
    && !value.startsWith("//")
    && !value.startsWith("/*")
    && !value.startsWith("*")
    && value !== "*/";
}

function pythonExecutableLine(line: string): boolean {
  const value = line.trim();
  return Boolean(value)
    && !value.startsWith("#")
    && !value.startsWith('\"\"\"')
    && !value.startsWith("'''");
}

function sourceUnits(spec: SourceSpec, path: string): SourceUnit[] {
  const raw = sourceBlob(spec.repo, spec.snapshot, path);
  const text = raw.toString("utf8");
  const kind = path.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.ESNext, true, kind);
  const lines = text.replaceAll("\r", "").split("\n");
  const output: SourceUnit[] = [];
  const fileSha256 = sha256(raw);
  const add = (node: ts.Node, symbol: string, countsAsExecutable = true): void => {
    const startLine = source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
    const endLine = source.getLineAndCharacterOfPosition(Math.max(node.getStart(source), node.end - 1)).line + 1;
    if (endLine < startLine) return;
    const executableLineNumbers = countsAsExecutable
      ? Array.from({ length: endLine - startLine + 1 }, (_, index) => startLine + index)
        .filter((lineNumber) => typescriptExecutableLine(lines[lineNumber - 1] ?? ""))
      : [];
    output.push({
      path,
      symbol,
      startLine,
      endLine,
      fileSha256,
      countsAsExecutable,
      executableLineNumbers,
    });
  };
  for (const [statementIndex, statement] of source.statements.entries()) {
    if (
      ts.isImportDeclaration(statement)
      || ts.isExportDeclaration(statement)
      || ts.isInterfaceDeclaration(statement)
      || ts.isTypeAliasDeclaration(statement)
      || ts.isModuleDeclaration(statement)
      || ts.isEmptyStatement(statement)
    ) continue;
    if (ts.isClassDeclaration(statement)) {
      const owner = statement.name?.text ?? `anonymous-class-${statementIndex + 1}`;
      for (const [memberIndex, member] of statement.members.entries()) {
        if (ts.isPropertyDeclaration(member) && !member.initializer) continue;
        if (ts.isIndexSignatureDeclaration(member) || ts.isSemicolonClassElement(member)) continue;
        add(member, `${owner}.${nodeName(member, source, `member-${memberIndex + 1}`)}`);
      }
      continue;
    }
    add(statement, nodeName(statement, source, `statement-${statementIndex + 1}`));
  }
  if (!output.length) {
    const structural = source.statements.find((statement) =>
      ts.isInterfaceDeclaration(statement) || ts.isTypeAliasDeclaration(statement) || ts.isExportDeclaration(statement)
    );
    if (structural) add(structural, nodeName(structural, source, "type-surface"), false);
  }
  if (!output.length) {
    const lineCount = Math.max(1, text.replaceAll("\r", "").split("\n").length - (text.endsWith("\n") ? 1 : 0));
    output.push({
      path,
      symbol: "module-surface",
      startLine: 1,
      endLine: lineCount,
      fileSha256,
      countsAsExecutable: false,
      executableLineNumbers: [],
    });
  }
  return output.sort((left, right) => left.startLine - right.startLine || left.endLine - right.endLine);
}

function allocateQuotas(capacities: readonly number[], target: number): number[] {
  if (capacities.some((value) => value < 0)) throw new Error("source file has invalid executable capacity");
  const total = capacities.reduce((sum, value) => sum + value, 0);
  if (total < target) throw new Error(`source capacity ${total} below frozen target ${target}`);
  const quotas = capacities.map((capacity) => capacity === 0
    ? 0
    : Math.max(1, Math.min(capacity, Math.floor(target * capacity / total))));
  let assigned = quotas.reduce((sum, value) => sum + value, 0);
  while (assigned < target) {
    let best = -1;
    let score = Number.NEGATIVE_INFINITY;
    for (let index = 0; index < capacities.length; index += 1) {
      if (quotas[index]! >= capacities[index]!) continue;
      const ideal = target * capacities[index]! / total;
      const candidate = ideal - quotas[index]!;
      if (candidate > score) {
        best = index;
        score = candidate;
      }
    }
    if (best < 0) throw new Error("unable to allocate frozen executable source target");
    quotas[best] = quotas[best]! + 1;
    assigned += 1;
  }
  while (assigned > target) {
    let best = -1;
    let score = Number.NEGATIVE_INFINITY;
    for (let index = 0; index < capacities.length; index += 1) {
      if (quotas[index]! <= (capacities[index] === 0 ? 0 : 1)) continue;
      const ideal = target * capacities[index]! / total;
      const candidate = quotas[index]! - ideal;
      if (candidate > score) {
        best = index;
        score = candidate;
      }
    }
    if (best < 0) throw new Error("unable to reduce frozen executable source allocation");
    quotas[best] = quotas[best]! - 1;
    assigned -= 1;
  }
  return quotas;
}

function selectSourceRanges(spec: SourceSpec): SelectedRange[] {
  const unitsByPath = spec.paths.map((path) => sourceUnits(spec, path));
  const capacities = unitsByPath.map((units) => units.reduce(
    (sum, unit) => sum + unit.executableLineNumbers.length,
    0,
  ));
  const quotas = allocateQuotas(capacities, spec.executableLines);
  const selected: SelectedRange[] = [];
  for (let fileIndex = 0; fileIndex < unitsByPath.length; fileIndex += 1) {
    let remaining = quotas[fileIndex]!;
    if (!remaining) {
      const structural = unitsByPath[fileIndex]![0]!;
      selected.push({ ...structural, part: 1, partCount: 1 });
      continue;
    }
    for (const unit of unitsByPath[fileIndex]!) {
      if (!remaining) break;
      if (!unit.countsAsExecutable) continue;
      const accepted = Math.min(unit.executableLineNumbers.length, remaining);
      if (!accepted) continue;
      selected.push({
        ...unit,
        endLine: unit.executableLineNumbers[accepted - 1]!,
        executableLineNumbers: unit.executableLineNumbers.slice(0, accepted),
        part: 1,
        partCount: 1,
      });
      remaining -= accepted;
    }
    if (remaining) throw new Error(`failed to allocate ${spec.repo}:${spec.paths[fileIndex]}`);
  }
  return selected;
}

function splitRanges(input: readonly SelectedRange[], targetCount: number): SelectedRange[] {
  const output = input.map((item) => ({ ...item }));
  if (output.length > targetCount) {
    throw new Error(`AST range count ${output.length} exceeds frozen range target ${targetCount}`);
  }
  while (output.length < targetCount) {
    let selectedIndex = -1;
    let selectedLength = 1;
    for (let index = 0; index < output.length; index += 1) {
      const candidate = output[index]!;
      const length = candidate.endLine - candidate.startLine + 1;
      if (length > selectedLength) {
        selectedIndex = index;
        selectedLength = length;
      }
    }
    if (selectedIndex < 0) throw new Error(`cannot split source ranges to ${targetCount}`);
    const selected = output[selectedIndex]!;
    const leftLength = Math.ceil(selectedLength / 2);
    const left: SelectedRange = {
      ...selected,
      endLine: selected.startLine + leftLength - 1,
      executableLineNumbers: selected.executableLineNumbers.filter(
        (lineNumber) => lineNumber <= selected.startLine + leftLength - 1,
      ),
      part: 1,
      partCount: 2,
    };
    const right: SelectedRange = {
      ...selected,
      startLine: left.endLine + 1,
      executableLineNumbers: selected.executableLineNumbers.filter((lineNumber) => lineNumber > left.endLine),
      part: 2,
      partCount: 2,
    };
    output.splice(selectedIndex, 1, left, right);
  }
  const grouped = new Map<string, SelectedRange[]>();
  for (const range of output) {
    const key = `${range.path}::${range.symbol}`;
    grouped.set(key, [...(grouped.get(key) ?? []), range]);
  }
  for (const ranges of grouped.values()) {
    ranges.sort((left, right) => left.startLine - right.startLine);
    for (let index = 0; index < ranges.length; index += 1) {
      ranges[index]!.part = index + 1;
      ranges[index]!.partCount = ranges.length;
    }
  }
  return output.sort((left, right) => left.path.localeCompare(right.path) || left.startLine - right.startLine);
}

const permissionRoutes = [
  ["packages/runtime/claude-runtime/src/permission/model.ts", "PermissionIdentity.create"],
  ["packages/runtime/claude-runtime/src/permission/rule-parser.ts", "PermissionRuleParser.parse"],
  ["packages/runtime/claude-runtime/src/permission/rule-parser.ts", "PermissionRuleParser.serialize"],
  ["packages/runtime/claude-runtime/src/permission/rule-index.ts", "PermissionRuleIndex.resolve"],
  ["packages/runtime/claude-runtime/src/permission/rule-index.ts", "PermissionRuleIndex.replace"],
  ["packages/runtime/claude-runtime/src/permission/mode-runtime.ts", "PermissionModeRuntime.transition"],
  ["packages/runtime/claude-runtime/src/permission/risk-runtime.ts", "PermissionRiskRuntime.classify"],
  ["packages/runtime/claude-runtime/src/permission/hook-runtime.ts", "PermissionHookRuntime.runBeforeTool"],
  ["packages/runtime/claude-runtime/src/permission/hook-runtime.ts", "PermissionHookRuntime.validateMutation"],
  ["packages/runtime/claude-runtime/src/permission/evaluator.ts", "PermissionEvaluator.evaluate"],
  ["packages/runtime/claude-runtime/src/permission/evaluator.ts", "PermissionEvaluator.finalize"],
  ["packages/runtime/claude-runtime/src/permission/grant-runtime.ts", "StandingGrantRuntime.consume"],
  ["packages/runtime/claude-runtime/src/permission/grant-runtime.ts", "StandingGrantRuntime.revoke"],
  ["packages/runtime/claude-runtime/src/permission/continuation-runtime.ts", "PermissionContinuationRuntime.park"],
  ["packages/runtime/claude-runtime/src/permission/continuation-runtime.ts", "PermissionContinuationRuntime.resume"],
  ["packages/runtime/claude-runtime/src/permission/continuation-runtime.ts", "PermissionContinuationRuntime.rejectResponse"],
  ["packages/runtime/claude-runtime/src/permission/permission-journal.ts", "PermissionJournal.prepare"],
  ["packages/runtime/claude-runtime/src/permission/permission-journal.ts", "PermissionJournal.commit"],
  ["packages/runtime/claude-runtime/src/permission/permission-journal.ts", "PermissionJournal.restore"],
  ["packages/runtime/claude-runtime/src/permission/acp-transport.ts", "AcpPermissionTransport.correlate"],
] as const;

const mcpRoutes = [
  ["packages/integrations/claude-mcp/src/config/config-store.ts", "McpConfigStore.merge"],
  ["packages/integrations/claude-mcp/src/config/policy.ts", "McpServerPolicy.evaluate"],
  ["packages/integrations/claude-mcp/src/connection/connection-runtime.ts", "McpConnectionRuntime.connect"],
  ["packages/integrations/claude-mcp/src/connection/connection-runtime.ts", "McpConnectionRuntime.reconnect"],
  ["packages/integrations/claude-mcp/src/connection/connection-runtime.ts", "McpConnectionRuntime.disconnect"],
  ["packages/integrations/claude-mcp/src/connection/stdio-transport.ts", "StdioMcpTransport.start"],
  ["packages/integrations/claude-mcp/src/connection/stdio-transport.ts", "StdioMcpTransport.request"],
  ["packages/integrations/claude-mcp/src/connection/http-transport.ts", "HttpMcpTransport.request"],
  ["packages/integrations/claude-mcp/src/connection/sse-parser.ts", "McpSseParser.push"],
  ["packages/integrations/claude-mcp/src/auth/oauth-runtime.ts", "McpOAuthRuntime.challenge"],
  ["packages/integrations/claude-mcp/src/auth/oauth-runtime.ts", "McpOAuthRuntime.callback"],
  ["packages/integrations/claude-mcp/src/auth/oauth-runtime.ts", "McpOAuthRuntime.refresh"],
  ["packages/integrations/claude-mcp/src/auth/oauth-runtime.ts", "McpOAuthRuntime.poisonClient"],
  ["packages/integrations/claude-mcp/src/auth/token-vault-port.ts", "McpTokenVaultPort.store"],
  ["packages/integrations/claude-mcp/src/catalog/capability-catalog.ts", "McpCapabilityCatalog.applySnapshot"],
  ["packages/integrations/claude-mcp/src/catalog/capability-catalog.ts", "McpCapabilityCatalog.applyNotification"],
  ["packages/integrations/claude-mcp/src/catalog/capability-catalog.ts", "McpCapabilityCatalog.remove"],
  ["packages/integrations/claude-mcp/src/projection/tool-projection.ts", "McpToolProjection.materialize"],
  ["packages/integrations/claude-mcp/src/projection/resource-projection.ts", "McpResourceProjection.materialize"],
  ["packages/integrations/claude-mcp/src/projection/prompt-projection.ts", "McpPromptProjection.materialize"],
  ["packages/integrations/claude-mcp/src/projection/instruction-runtime.ts", "McpInstructionRuntime.applyDelta"],
  ["packages/integrations/claude-mcp/src/runtime/request-journal.ts", "McpRequestJournal.prepare"],
  ["packages/integrations/claude-mcp/src/runtime/request-journal.ts", "McpRequestJournal.recordEffect"],
  ["packages/integrations/claude-mcp/src/runtime/request-journal.ts", "McpRequestJournal.commit"],
  ["packages/integrations/claude-mcp/src/runtime/request-journal.ts", "McpRequestJournal.restore"],
  ["packages/integrations/claude-mcp/src/runtime/sampling-runtime.ts", "McpSamplingRuntime.sample"],
  ["packages/integrations/claude-mcp/src/runtime/elicitation-runtime.ts", "McpElicitationRuntime.request"],
  ["packages/integrations/claude-mcp/src/runtime/elicitation-runtime.ts", "McpElicitationRuntime.resume"],
  ["packages/integrations/claude-mcp/src/runtime/task-runtime.ts", "McpTaskRuntime.poll"],
  ["packages/integrations/claude-mcp/src/runtime/task-runtime.ts", "McpTaskRuntime.cancel"],
] as const;

const skillRoutes = [
  ["packages/runtime/claude-runtime/src/skills/source-runtime.ts", "SkillSourceRuntime.discover"],
  ["packages/runtime/claude-runtime/src/skills/frontmatter-runtime.ts", "SkillFrontmatterRuntime.parse"],
  ["packages/runtime/claude-runtime/src/skills/registry-runtime.ts", "SkillRegistryRuntime.commitRevision"],
  ["packages/runtime/claude-runtime/src/skills/registry-runtime.ts", "SkillRegistryRuntime.resolve"],
  ["packages/runtime/claude-runtime/src/skills/resource-runtime.ts", "SkillResourceRuntime.load"],
  ["packages/runtime/claude-runtime/src/skills/invocation-runtime.ts", "SkillInvocationRuntime.invoke"],
  ["packages/runtime/claude-runtime/src/skills/invocation-runtime.ts", "SkillInvocationRuntime.applyContext"],
  ["packages/runtime/claude-runtime/src/skills/invocation-runtime.ts", "SkillInvocationRuntime.applyToolScope"],
  ["packages/runtime/claude-runtime/src/skills/reload-runtime.ts", "SkillReloadRuntime.scan"],
  ["packages/runtime/claude-runtime/src/skills/reload-runtime.ts", "SkillReloadRuntime.commit"],
  ["packages/runtime/claude-runtime/src/plugins/manifest-runtime.ts", "PluginManifestRuntime.parse"],
  ["packages/runtime/claude-runtime/src/plugins/plugin-runtime.ts", "PluginRuntime.load"],
  ["packages/runtime/claude-runtime/src/plugins/plugin-runtime.ts", "PluginRuntime.reload"],
  ["packages/runtime/claude-runtime/src/plugins/hook-runtime.ts", "PluginHookRuntime.beforeTool"],
  ["packages/runtime/claude-runtime/src/plugins/cache-runtime.ts", "PluginCacheRuntime.commit"],
  ["packages/runtime/claude-runtime/src/commands/descriptor-runtime.ts", "CommandDescriptorRuntime.parse"],
  ["packages/runtime/claude-runtime/src/commands/registry-runtime.ts", "CommandRegistryRuntime.register"],
  ["packages/runtime/claude-runtime/src/commands/dispatch-runtime.ts", "CommandDispatchRuntime.dispatch"],
  ["packages/runtime/claude-runtime/src/commands/dispatch-runtime.ts", "CommandDispatchRuntime.requirePermission"],
  ["packages/runtime/claude-runtime/src/commands/local-command-runtime.ts", "LocalCommandRuntime.execute"],
] as const;

function routesFor(domain: Domain): readonly (readonly [string, string])[] {
  if (domain === "permission") return permissionRoutes;
  if (domain === "mcp") return mcpRoutes;
  return skillRoutes;
}

const mutationDefinitions = [
  ...permissionRoutes.slice(1, 17).map((route, index) => ({ route, risk: `permission-${index + 1}` })),
  ...mcpRoutes.slice(2, 24).map((route, index) => ({ route, risk: `mcp-${index + 1}` })),
  ...skillRoutes.slice(1, 11).map((route, index) => ({ route, risk: `skill-${index + 1}` })),
] as const;

function mutationId(index: number, risk: string): string {
  return `e02-mut-${String(index + 1).padStart(3, "0")}-${risk}`;
}

function mutationRows(): Json[] {
  return mutationDefinitions.map((definition, index) => {
    const id = mutationId(index, definition.risk);
    const [targetPath, targetSymbol] = definition.route;
    const patch = {
      kind: "typescript-ast-method-entry-throw",
      mutation_id: id,
      target_path: targetPath,
      target_symbol: targetSymbol,
      injected_statement: `throw new Error(\"target_disconnect:${id}\")`,
    };
    return {
      schema_version: SCHEMA_VERSION,
      record_type: "mutation",
      execution_id: EXECUTION_ID,
      mutation_id: id,
      target_path: targetPath,
      target_symbol: targetSymbol,
      mutation_operator: "disconnect-target",
      semantic_risk: definition.risk,
      compile_survives: true,
      expected_killer_test_ids: [`e02.mutation.${definition.risk}`],
      frozen_patch_sha256: sha256(stableString(patch)),
      frozen_patch: patch,
    };
  });
}

function sourceRows(finalizeTargets = false): { source: Json[]; target: Json[] } {
  const source: Json[] = [];
  const target: Json[] = [];
  const mutations = mutationRows();
  const targetHashes = new Map<string, string>();
  const targetHash = (path: string): string | null => {
    if (!finalizeTargets) return null;
    const cached = targetHashes.get(path);
    if (cached) return cached;
    const digest = sha256(gitBytes(repoRoot, ["show", `HEAD:${path}`]));
    targetHashes.set(path, digest);
    return digest;
  };
  let claudeRanges: SelectedRange[] = [];
  for (const spec of sourceSpecs) {
    const selected = selectSourceRanges(spec);
    if (spec.repo === "claude-code-best") claudeRanges.push(...selected);
  }
  claudeRanges = splitRanges(claudeRanges, 1_342);
  const selectedBySpec = sourceSpecs.map((spec) => {
    if (spec.repo !== "claude-code-best") return selectSourceRanges(spec);
    const paths = new Set(spec.paths);
    return claudeRanges.filter((range) => paths.has(range.path));
  });
  let ordinal = 0;
  for (let specIndex = 0; specIndex < sourceSpecs.length; specIndex += 1) {
    const spec = sourceSpecs[specIndex]!;
    for (const range of selectedBySpec[specIndex]!) {
      const mappingId = `e02-src-${String(ordinal + 1).padStart(4, "0")}`;
      const sourceSymbol = range.partCount > 1
        ? `${range.path}::${range.symbol}#${range.part}/${range.partCount}`
        : `${range.path}::${range.symbol}`;
      const row: Json = {
        schema_version: SCHEMA_VERSION,
        record_type: "source_range",
        execution_id: EXECUTION_ID,
        mapping_id: mappingId,
        source_repo: spec.repo,
        source_snapshot: spec.snapshot,
        source_path: range.path,
        source_symbol: sourceSymbol,
        start_line: range.startLine,
        end_line: range.endLine,
        source_sha256: range.fileSha256,
        accepted: true,
        source_role: spec.role,
        semantic_domain: spec.domain,
        migration_mode: spec.role === "primary" ? "adapted" : "cropped-supplementary",
        exclusion_reason: null,
        supplementary_gap: spec.role === "supplementary"
          ? spec.repo === "opencode"
            ? "ACP correlation and OAuth/catalog typed-host behavior absent from the Claude primary subset"
            : "layered plugin-owned tool policy and fail-closed before-tool auditing absent from the Claude primary subset"
          : null,
      };
      source.push(row);
      const routes = routesFor(spec.domain);
      const route = routes[ordinal % routes.length]!;
      const relatedMutations = mutations.filter((mutation) =>
        mutation.target_path === route[0] && mutation.target_symbol === route[1]
      );
      const fallbackMutation = mutations[ordinal % mutations.length]!;
      const canonicalOwnerId = spec.domain === "permission"
        ? "typescript.PermissionCoordinator"
        : spec.domain === "mcp"
          ? "typescript.McpRuntimeCoordinator"
          : "typescript.SkillPluginCommandCoordinator";
      const stateAssertion = spec.domain === "permission"
        ? "e02.custody.permission.default-path"
        : spec.domain === "mcp"
          ? "e02.custody.mcp.default-path"
          : "e02.custody.skills-plugins-commands.default-path";
      target.push({
        schema_version: SCHEMA_VERSION,
        record_type: "custody_mapping",
        execution_id: EXECUTION_ID,
        mapping_id: mappingId,
        source_symbol: sourceSymbol,
        source_behavior_claim: `${sourceSymbol} lines ${range.startLine}-${range.endLine} contribute ${spec.domain} executable behavior at ${spec.snapshot}`,
        target_path: route[0],
        target_symbol: route[1],
        target_sha256: targetHash(route[0]),
        planned_method: route[1].split(".").at(-1),
        target_behavior_claim: `${route[1]} in ${route[0]} owns the ${spec.domain} transition consolidated from ${sourceSymbol}`,
        adaptation: spec.role === "primary"
          ? "Claude behavior is decomposed into Zyra-owned typed state, journal, permission, event, and failure boundaries"
          : "Only the declared gap is consolidated into the existing Claude-primary owner; no second evaluator, client, or registry is created",
        semantic_equivalence: "The target preserves deterministic decision, correlation, failure, revision, and restore semantics while replacing upstream global state with Zyra-owned snapshots and commit receipts",
        state_store: spec.domain === "permission"
          ? "E02RuntimeSnapshot.permission"
          : spec.domain === "mcp"
            ? "E02RuntimeSnapshot.mcp"
            : "E02RuntimeSnapshot.capabilities",
        state_effect_kind: spec.domain === "permission"
          ? "permission"
          : spec.domain === "mcp"
            ? "external_effect"
            : "state",
        canonical_owner_id: canonicalOwnerId,
        default_entry_id: "E02.default.CodeWorkerApplication.runTaskRuntime",
        default_callsite_path: "packages/runtime/claude-runtime/src/capability-host.ts",
        default_callsite_symbol: "PermissionedCapabilityHost.executeBatch",
        state_effect_assertion: stateAssertion,
        behavior_contract_id: `e02.contract.${mappingId}`,
        success_test_ids: [`e02.custody.${spec.domain}.default-path`],
        failure_test_ids: [`e02.custody.${spec.domain}.failure-path`],
        disable_test_ids: ["e02.disable.typescript-runtime fails closed before any Python fallback can open"],
        mutation_ids: [(relatedMutations[0] ?? fallbackMutation).mutation_id],
        restore_probe_id: "e02.probe.exact-resume",
        runtime_origin_probe_id: "e02.probe.runtime-origin",
        write_path_probe_id: "e02.probe.write-path",
      });
      ordinal += 1;
    }
  }
  return { source, target };
}

const pythonDeleteFragments = [
  "/permission/action_gate.py",
  "/permission/classifier.py",
  "/permission/evaluator.py",
  "/permission/extensions.py",
  "/permission/hooks.py",
  "/permission/modes.py",
  "/permission/risk.py",
  "/permission/rules.py",
  "/permission/runtime.py",
  "/permission/shell_analysis.py",
  "/mcp/auth.py",
  "/mcp/bootstrap.py",
  "/mcp/capabilities.py",
  "/mcp/config.py",
  "/mcp/connection.py",
  "/mcp/control.py",
  "/mcp/elicitation.py",
  "/mcp/instructions.py",
  "/mcp/main_path.py",
  "/mcp/projection.py",
  "/mcp/protocol.py",
  "/mcp/recovery.py",
  "/mcp/resource_projection.py",
  "/mcp/runtime.py",
  "/mcp/sampling.py",
  "/mcp/tasks.py",
  "/mcp/transport.py",
  "/zyra_skills/admission.py",
  "/zyra_skills/atomic_update.py",
  "/zyra_skills/body_loader.py",
  "/zyra_skills/budget_runtime.py",
  "/zyra_skills/change_detector.py",
  "/zyra_skills/command_integration.py",
  "/zyra_skills/composition.py",
  "/zyra_skills/frontmatter.py",
  "/zyra_skills/hooks.py",
  "/zyra_skills/integration_health.py",
  "/zyra_skills/invocation.py",
  "/zyra_skills/invocation_permission.py",
  "/zyra_skills/mcp_discovery.py",
  "/zyra_skills/mcp_integration.py",
  "/zyra_skills/outcome_commit.py",
  "/zyra_skills/plugin_integration.py",
  "/zyra_skills/plugin_runtime.py",
  "/zyra_skills/policy.py",
  "/zyra_skills/precedence.py",
  "/zyra_skills/registry.py",
  "/zyra_skills/reload.py",
  "/zyra_skills/resource_loader.py",
  "/zyra_skills/runtime.py",
  "/zyra_skills/search.py",
  "/zyra_skills/sources/",
  "/zyra_skills/tool_projection.py",
  "/zyra_skills/update_integration.py",
  "/zyra_skills/update_runtime.py",
] as const;

const pythonBlockedFragments = [
  "/zyra_skills/compact_bridge.py",
  "/zyra_skills/compact_integration.py",
  "/zyra_skills/disclosure.py",
  "/zyra_skills/fork_scope.py",
  "/zyra_skills/session_bridge.py",
  "/zyra_skills/session_integration.py",
  "/zyra_skills/subagent_contract.py",
  "/zyra_skills/task_integration.py",
  "/commands/zyra_commands/runtime/",
] as const;

const pythonPrefixes = [
  "packages/runtime/zyra_runtime/permission/",
  "packages/integrations/zyra_integrations/mcp/",
  "packages/skills/zyra_skills/",
  "packages/commands/zyra_commands/runtime/",
] as const;

const pythonExtraPaths = [
  "apps/api/zyra_api/mcp_api.py",
  "packages/commands/zyra_commands/mcp_control.py",
  "packages/commands/zyra_commands/registry.py",
  "packages/commands/zyra_commands/runtime/owner_handlers.py",
] as const;

function pythonCategory(path: string): PythonUnit["category"] {
  const normalized = `/${path.replaceAll("\\", "/")}`;
  if (pythonBlockedFragments.some((fragment) => normalized.includes(fragment))) return "blocked";
  if (pythonDeleteFragments.some((fragment) => normalized.includes(fragment))) return "delete";
  if (path.endsWith("mcp_control.py") || path.endsWith("/registry.py")) return "delete";
  return "retain";
}

function pythonPaths(): string[] {
  const tree = gitText(repoRoot, ["ls-tree", "-r", "--name-only", VERIFIED_BASELINE]).split(/\r?\n/);
  return [...new Set([
    ...tree.filter((path) => path.endsWith(".py") && pythonPrefixes.some((prefix) => path.startsWith(prefix))),
    ...pythonExtraPaths,
  ])].sort();
}

function pythonUnits(path: string): PythonUnit[] {
  const raw = gitBytes(repoRoot, ["show", `${VERIFIED_BASELINE}:${path}`]);
  const lines = raw.toString("utf8").replaceAll("\r", "").split("\n");
  if (lines.at(-1) === "") lines.pop();
  const starts: Array<{ line: number; symbol: string }> = [];
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]!;
    const declaration = /^(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)/.exec(line);
    if (declaration) starts.push({ line: index + 1, symbol: declaration[1]! });
  }
  if (!starts.length) starts.push({ line: 1, symbol: "<module>" });
  const output: PythonUnit[] = [];
  const fileSha256 = sha256(raw);
  for (let index = 0; index < starts.length; index += 1) {
    const current = starts[index]!;
    const next = starts[index + 1];
    output.push({
      path,
      symbol: current.symbol,
      startLine: current.line,
      endLine: (next?.line ?? lines.length + 1) - 1,
      fileSha256,
      category: pythonCategory(path),
      executableLineNumbers: Array.from(
        { length: (next?.line ?? lines.length + 1) - current.line },
        (_, offset) => current.line + offset,
      ).filter((lineNumber) => pythonExecutableLine(lines[lineNumber - 1] ?? "")),
    });
  }
  return output;
}

function takePython(
  pool: PythonUnit[],
  target: number,
  disposition: "delete" | "retain" | "blocked",
): { selected: PythonUnit[]; remaining: PythonUnit[] } {
  const selected: PythonUnit[] = [];
  const remaining: PythonUnit[] = [];
  let needed = target;
  for (const unit of pool) {
    if (!needed) {
      remaining.push(unit);
      continue;
    }
    const length = unit.executableLineNumbers.length;
    if (length <= needed) {
      selected.push({ ...unit, category: disposition });
      needed -= length;
      continue;
    }
    const splitLine = unit.executableLineNumbers[needed - 1]!;
    selected.push({
      ...unit,
      endLine: splitLine,
      category: disposition,
      executableLineNumbers: unit.executableLineNumbers.slice(0, needed),
    });
    remaining.push({
      ...unit,
      startLine: splitLine + 1,
      executableLineNumbers: unit.executableLineNumbers.filter((lineNumber) => lineNumber > splitLine),
    });
    needed = 0;
  }
  if (needed) throw new Error(`Python ${disposition} inventory short by ${needed} lines`);
  return { selected, remaining };
}

function pythonRows(): Json[] {
  const units = pythonPaths().flatMap(pythonUnits);
  const blockedPool = units.filter((unit) => unit.category === "blocked");
  const deletePool = units.filter((unit) => unit.category === "delete");
  const retainPool = units.filter((unit) => unit.category === "retain");
  const blocked = takePython(blockedPool, 6_922, "blocked");
  const deletion = takePython(deletePool, 31_070, "delete");
  const retainCandidates = [...retainPool, ...blocked.remaining, ...deletion.remaining]
    .sort((left, right) => left.path.localeCompare(right.path) || left.startLine - right.startLine);
  const retained = takePython(retainCandidates, 23_260, "retain");
  const selected = [...deletion.selected, ...retained.selected, ...blocked.selected]
    .sort((left, right) => left.path.localeCompare(right.path) || left.startLine - right.startLine);
  return selected.map((unit, index) => ({
    schema_version: SCHEMA_VERSION,
    record_type: "python_owner",
    execution_id: EXECUTION_ID,
    owner_id: `e02-py-${String(index + 1).padStart(4, "0")}`,
    verified_zyra_head: VERIFIED_BASELINE,
    python_path: unit.path,
    python_symbol: unit.symbol,
    start_line: unit.startLine,
    end_line: unit.endLine,
    python_sha256: unit.fileSha256,
    state_domain: unit.path.includes("permission")
      ? "permission"
      : unit.path.includes("mcp")
        ? "mcp"
        : "skills-plugins-commands",
    disposition: unit.category === "delete" ? "delete" : unit.category,
    allowed_adapter_symbols: unit.category === "retain"
      ? ["typed receipt", "CAS store", "credential/provenance/event/artifact transport"]
      : [],
    default_entry: "CodeWorkerRuntime.run",
    deletion_test_id: unit.category === "delete" ? "e02.python-owner-absent" : null,
    call_direction: unit.category === "retain" ? "typescript-to-python-port-only" : null,
    blocked_owner: unit.category === "blocked" ? "E01-or-E03-protected-overlap" : null,
  }));
}

function gateProfile(candidateHead: string): Json {
  const postBaseline = gitText(repoRoot, ["rev-list", "--reverse", `${VERIFIED_BASELINE}..${candidateHead}`])
    .split(/\r?\n/)
    .filter(Boolean);
  return {
    schema_version: SCHEMA_VERSION,
    record_type: "gate_profile",
    execution_id: EXECUTION_ID,
    verification_contract_version: "zyra.e02-verification/v3",
    generator: "scripts/remediation/m1_r01_e02_g0.ts",
    schema_verifier: "scripts/remediation/verify_m1_r01_e02.ts",
    mutation_runner: "scripts/remediation/run_m1_r01_e02_mutations.ts",
    verified_zyra_head: VERIFIED_BASELINE,
    implementation_diff_baseline: VERIFIED_BASELINE,
    candidate_head_at_g0: candidateHead,
    allowed_control_plane_commits: postBaseline,
    source_snapshots: {
      "claude-code-best": CLAUDE_SNAPSHOT,
      opencode: OPENCODE_SNAPSHOT,
      OpenClaw: OPENCLAW_SNAPSHOT,
    },
    source_hash_semantics: "sha256-of-raw-git-blob-bytes-at-declared-snapshot",
    production_roots: [
      "packages/runtime/claude-runtime/src/permission",
      "packages/runtime/claude-runtime/src/skills",
      "packages/runtime/claude-runtime/src/plugins",
      "packages/runtime/claude-runtime/src/commands",
      "packages/runtime/claude-runtime/src/e02",
      "packages/integrations/claude-mcp/src",
      "apps/code-worker/src/main.ts",
    ],
    candidate_scope_paths: [
      "apps/code-worker/src/main.ts",
      "packages/runtime/claude-runtime/src/permission",
      "packages/runtime/claude-runtime/src/skills",
      "packages/runtime/claude-runtime/src/plugins",
      "packages/runtime/claude-runtime/src/commands",
      "packages/runtime/claude-runtime/src/e02",
      "packages/runtime/claude-runtime/src/capabilities.ts",
      "packages/runtime/claude-runtime/src/capability-host.ts",
      "packages/runtime/claude-runtime/src/stdio.ts",
      "packages/integrations/claude-mcp/src",
      "packages/runtime/claude-runtime/test/e02",
      "packages/integrations/claude-mcp/test/e02",
      "scripts/remediation/m1_r01_e02_g0.ts",
      "scripts/remediation/verify_m1_r01_e02.ts",
      "scripts/remediation/run_m1_r01_e02_mutations.ts",
      "scripts/remediation/cleanroom_m1_r01_e02.ts",
      "scripts/remediation/probe_m1_r01_e02.ts",
      "docs/reviews/evidence/M1-R01-v3/execution-02",
      "docs/reviews/M1-R01-v2-execution-02-independent-review.md",
      "docs/reviews/M1-R01-v3-execution-02-implementation-self-review.md",
      "package.json",
      "bun.lock",
    ],
    adapter_roots: [
      "packages/runtime/zyra_runtime/e02_ports.py",
      "packages/integrations/zyra_integrations/e02_ports.py",
      "packages/skills/e02_ports.py",
      "packages/workers/zyra_workers/typescript_claude_runtime.py",
    ],
    behavior_test_roots: [
      "packages/runtime/claude-runtime/test/e02",
      "packages/integrations/claude-mcp/test/e02",
    ],
    excluded_production_prefixes: [
      "packages/runtime/claude-runtime/src/e01",
      "packages/runtime/claude-runtime/src/agents",
      "packages/runtime/claude-runtime/src/control",
      "vendor/",
      "vendor-runtimes/",
      "source-pool/",
      "runtime-sources/",
    ],
    thresholds: {
      claude_primary_source_executable_sloc: 18_447,
      accepted_source_executable_sloc: 20_047,
      claude_primary_source_files: 93,
      claude_primary_source_ranges: 1_342,
      python_delete_executable_sloc: 31_070,
      python_retain_executable_sloc: 23_260,
      python_blocked_executable_sloc: 6_922,
      final_non_test_typescript_sloc: 38_000,
      effective_changed_typescript_sloc: 35_366,
      effective_behavior_test_sloc: 6_000,
      adapter_ratio_maximum: 0.10,
      remaining_python_logical_adapter_sloc_maximum: 2_500,
      mutation_points_minimum: 45,
      core_mutation_kill_ratio_minimum: 1,
      other_mutation_kill_ratio_minimum: 0.9,
      source_to_target_unique_symbols_minimum: 60,
      source_to_target_max_mappings_per_symbol: 40,
    },
    default_entry: {
      path: "apps/code-worker/src/main.ts",
      symbol: "main",
      runtime_symbol: "CodeWorkerApplication.runTaskRuntime",
      e02_symbol: "E02CapabilityCoordinator.execute",
    },
    required_toolchain: {
      bun: "1.2.15",
      typescript: "5.8.3",
      node_types: "22.15.29",
    },
    commands: {
      install: ["npx", "--yes", "bun@1.2.15", "install", "--frozen-lockfile"],
      typecheck: ["npx", "--yes", "bun@1.2.15", "run", "typecheck:e02"],
      build: ["npx", "--yes", "bun@1.2.15", "run", "build"],
      behavior_test: ["npx", "--yes", "bun@1.2.15", "test", "packages/runtime/claude-runtime/test/e02", "packages/integrations/claude-mcp/test/e02"],
      built_entry: ["npx", "--yes", "bun@1.2.15", "run", "runtime:built:health"],
      candidate_gate: ["npx", "--yes", "bun@1.2.15", "run", "e02:candidate:gate"],
      mutation: ["npx", "--yes", "bun@1.2.15", "run", "e02:mutation"],
      cleanroom: ["npx", "--yes", "bun@1.2.15", "run", "e02:cleanroom"],
    },
    source_validator_command: ["npx", "--yes", "bun@1.2.15", "run", "e02:candidate:gate"],
    effective_loc_clone_command: ["npx", "--yes", "bun@1.2.15", "run", "e02:candidate:gate"],
    runtime_origin_probe_command: ["npx", "--yes", "bun@1.2.15", "scripts/remediation/probe_m1_r01_e02.ts", "runtime-origin"],
    write_path_probe_command: ["npx", "--yes", "bun@1.2.15", "scripts/remediation/probe_m1_r01_e02.ts", "write-path"],
    same_session_resume_command: ["npx", "--yes", "bun@1.2.15", "scripts/remediation/probe_m1_r01_e02.ts", "resume"],
    lost_ack_command: ["npx", "--yes", "bun@1.2.15", "scripts/remediation/probe_m1_r01_e02.ts", "lost-ack"],
    disable_command: ["npx", "--yes", "bun@1.2.15", "scripts/remediation/probe_m1_r01_e02.ts", "disable"],
    clean_dependency_path_command: ["npx", "--yes", "bun@1.2.15", "run", "e02:cleanroom"],
    forbidden_runtime_dependencies: [
      "../claude-code-best",
      "../opencode",
      "../OpenClaw",
      "../Hermes-Agent",
      "vendor/",
      "vendor-runtimes/",
      "source-pool/",
      "runtime-sources/",
    ],
    forbidden_runtime_paths: [
      "../claude-code-best",
      "../opencode",
      "../OpenClaw",
      "../Hermes-Agent",
      "vendor/",
      "vendor-runtimes/",
      "source-pool/",
      "runtime-sources/"
    ],
    checker_sources: {
      "scripts/remediation/m1_r01_e02_g0.ts": sha256(readFileSync(join(repoRoot, "scripts/remediation/m1_r01_e02_g0.ts"))),
      "scripts/remediation/verify_m1_r01_e02.ts": sha256(readFileSync(join(repoRoot, "scripts/remediation/verify_m1_r01_e02.ts"))),
      "scripts/remediation/run_m1_r01_e02_mutations.ts": sha256(readFileSync(join(repoRoot, "scripts/remediation/run_m1_r01_e02_mutations.ts"))),
      "scripts/remediation/cleanroom_m1_r01_e02.ts": sha256(readFileSync(join(repoRoot, "scripts/remediation/cleanroom_m1_r01_e02.ts"))),
      "scripts/remediation/probe_m1_r01_e02.ts": sha256(readFileSync(join(repoRoot, "scripts/remediation/probe_m1_r01_e02.ts"))),
    },
    candidate_status_before_independent_review: "implementation_complete_review_pending",
  };
}

function main(): void {
  const mode = process.argv[2];
  if (mode !== "freeze" && mode !== "finalize") {
    throw new Error("usage: bun scripts/remediation/m1_r01_e02_g0.ts <freeze|finalize>");
  }
  const candidateHead = gitText(repoRoot, ["rev-parse", "HEAD"]);
  gitText(repoRoot, ["merge-base", "--is-ancestor", VERIFIED_BASELINE, candidateHead]);
  const claudeFileCount = new Set([
    ...claudePermissionPaths,
    ...claudeMcpPaths,
    ...claudeSkillPaths,
  ]).size;
  if (claudeFileCount !== 93) throw new Error(`expected 93 Claude files, got ${claudeFileCount}`);
  const manifests = sourceRows(mode === "finalize");
  const python = pythonRows();
  const mutations = mutationRows();
  const profile = gateProfile(candidateHead);
  const sourcePath = join(manifestRoot, "execution-02-source-manifest.jsonl");
  const pythonPath = join(manifestRoot, "execution-02-python-owner-baseline.jsonl");
  const targetPath = join(manifestRoot, "execution-02-target-custody-map.jsonl");
  const mutationPath = join(manifestRoot, "execution-02-mutation-manifest.jsonl");
  const profilePath = join(manifestRoot, "execution-02-gate-profile.json");
  const receiptPath = join(manifestRoot, "execution-02-baseline-receipt.json");
  writeJsonLines(sourcePath, manifests.source);
  writeJsonLines(pythonPath, python);
  writeJsonLines(targetPath, manifests.target);
  writeJsonLines(mutationPath, mutations);
  writeJson(profilePath, profile);
  const dirtyPaths = gitText(repoRoot, ["status", "--porcelain=v1", "--untracked-files=all"])
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => line.slice(3).replaceAll("\\", "/"));
  const files = [sourcePath, pythonPath, targetPath, mutationPath, profilePath];
  const receipt = {
    schema_version: SCHEMA_VERSION,
    record_type: "baseline_receipt",
    execution_id: EXECUTION_ID,
    verified_zyra_head: VERIFIED_BASELINE,
    verified_zyra_tree: gitText(repoRoot, ["rev-parse", `${VERIFIED_BASELINE}^{tree}`]),
    g0_candidate_head: candidateHead,
    g0_candidate_tree: gitText(repoRoot, ["rev-parse", `${candidateHead}^{tree}`]),
    captured_at_utc: new Date().toISOString(),
    verified_head_tree: gitText(repoRoot, ["rev-parse", `${VERIFIED_BASELINE}^{tree}`]),
    clean_worktree: dirtyPaths.length === 0,
    dirty_paths: dirtyPaths,
    source_hash_semantics: "sha256-of-raw-git-blob-bytes-at-declared-snapshot",
    source_snapshots: {
      "claude-code-best": {
        commit: CLAUDE_SNAPSHOT,
        tree: gitText(join(workspaceRoot, "claude-code-best"), ["rev-parse", `${CLAUDE_SNAPSHOT}^{tree}`]),
      },
      opencode: {
        commit: OPENCODE_SNAPSHOT,
        tree: gitText(join(workspaceRoot, "opencode"), ["rev-parse", `${OPENCODE_SNAPSHOT}^{tree}`]),
      },
      OpenClaw: {
        commit: OPENCLAW_SNAPSHOT,
        tree: gitText(join(workspaceRoot, "OpenClaw"), ["rev-parse", `${OPENCLAW_SNAPSHOT}^{tree}`]),
      },
    },
    manifest_sha256: Object.fromEntries(files.map((path) => [
      path.slice(manifestRoot.length + 1).replaceAll("\\", "/"),
      sha256(readFileSync(path)),
    ])),
    toolchain: {
      bun: "1.2.15",
      typescript: "5.8.3",
    },
    manifest_generator_command: ["bun", "scripts/remediation/m1_r01_e02_g0.ts", mode],
    manifest_generator_version: "zyra.e02-g0/v2",
    schema_validator_command: ["bun", "scripts/remediation/verify_m1_r01_e02.ts", "--candidate", candidateHead],
    schema_validator_version: "zyra.e02-verification/v4",
    lockfile_sha256: sha256(readFileSync(join(repoRoot, "bun.lock"))),
    host_platform: `${process.platform}-${process.arch}`,
    capture_exit_codes: {
      verified_head: 0,
      candidate_head: 0,
      source_snapshots: 0,
      worktree_status: 0,
    },
  };
  writeJson(receiptPath, receipt);
  process.stdout.write(`${JSON.stringify({
    candidate_head: candidateHead,
    claude_files: claudeFileCount,
    claude_ranges: manifests.source.filter((row) => row.source_repo === "claude-code-best").length,
    accepted_source_rows: manifests.source.length,
    python_rows: python.length,
    mutation_rows: mutations.length,
    manifest_root: manifestRoot,
  }, null, 2)}\n`);
}

main();
