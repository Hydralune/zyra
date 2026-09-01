import { extname } from "node:path";

import type { JsonObject } from "../contracts.ts";
import { cloneJson, digest, optionalObject } from "../e02/canonical.ts";
import type { PermissionEffect, PermissionRiskAssessment } from "../e02/contracts.ts";
import type { PermissionIdentityRecord } from "./model.ts";
import { safeWorkspaceBinding } from "./model.ts";
import { PermissionCommandRiskRuntime } from "./command-risk-runtime.ts";

export interface PermissionClassifierSuggestion {
  effect: PermissionEffect;
  confidence: number;
  explanation: string;
  model: string;
  inputDigest: string;
  outputDigest: string;
}

export type PermissionClassifier = (
  identity: PermissionIdentityRecord,
  deterministic: PermissionRiskAssessment,
) => PermissionClassifierSuggestion | Promise<PermissionClassifierSuggestion>;

const LOW_RISK_TOOLS = new Set([
  "file_read",
  "list_skills",
  "list_commands",
  "list_plugins",
  "read_skill_resource",
  "mcp_list_resources",
  "mcp_read_resource",
  "mcp_list_prompts",
  "mcp_get_prompt",
  "agent_status",
]);

const HIGH_RISK_TOOLS = new Set([
  "shell",
  "bash",
  "browser",
  "file_write",
  "file_edit",
  "plugin_command",
  "command",
  "Agent",
  "Task",
  "agent_cancel",
]);

const DESTRUCTIVE_SHELL = [
  /(?:^|[;&|]\s*)rm\s+(?:-[^\s]*r[^\s]*\s+|--recursive\b)/i,
  /(?:^|[;&|]\s*)del\s+\/s\b/i,
  /(?:^|[;&|]\s*)remove-item\b[^\r\n]*-recurse\b/i,
  /(?:^|[;&|]\s*)format(?:-volume)?\b/i,
  /(?:^|[;&|]\s*)mkfs(?:\.|\s)/i,
  /git\s+(?:reset\s+--hard|clean\s+-[^\s]*f|checkout\s+--\s+)/i,
  /drop\s+(?:database|table|schema)\b/i,
  /truncate\s+table\b/i,
];

const PRIVILEGE_ESCALATION = [
  /(?:^|\s)sudo(?:\s|$)/i,
  /runas(?:\.exe)?\s+\/user:/i,
  /start-process\b[^\r\n]*-verb\s+runas/i,
  /set-executionpolicy\b/i,
  /chmod\s+(?:777|[ugo]*\+s)\b/i,
];

const NETWORK_EXFILTRATION = [
  /(?:curl|wget|invoke-webrequest|iwr)\b[^\r\n]*(?:--data|-d\b|-body\b|--upload-file|-t\b)/i,
  /(?:nc|netcat|ncat)\b/i,
  /(?:scp|rsync)\b[^\r\n]*:/i,
  /https?:\/\/(?!localhost\b|127\.0\.0\.1\b|\[::1\])/i,
];

const HIGH_CONFIDENCE_SECRET_PATTERNS = [
  /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/,
  /\b(?:ghp|github_pat|sk-[A-Za-z0-9])[-_A-Za-z0-9]{16,}\b/,
];

const LABELED_SECRET_VALUE_PATTERN = /(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)\s*(?:=|:)\s*["']?(?!\[?redacted\]?|<[^>]+>)[^\s"',}\\]{8,}/i;
const SOURCE_EDIT_TOOLS = new Set(["file_write", "file_edit"]);

const EXECUTABLE_EXTENSIONS = new Set([
  ".exe",
  ".dll",
  ".so",
  ".dylib",
  ".bat",
  ".cmd",
  ".ps1",
  ".sh",
  ".msi",
  ".deb",
  ".rpm",
  ".appimage",
]);

export class PermissionRiskRuntime {
  private readonly classifier: PermissionClassifier | null;
  private readonly commandRisk = new PermissionCommandRiskRuntime();

  constructor(classifier: PermissionClassifier | null = null) {
    this.classifier = classifier;
  }

  classify(identity: PermissionIdentityRecord): PermissionRiskAssessment {
    const deterministic = this.deterministic(identity);
    return deterministic;
  }

  async classifyAsync(identity: PermissionIdentityRecord): Promise<PermissionRiskAssessment> {
    const deterministic = this.deterministic(identity);
    if (!this.classifier) return deterministic;
    try {
      const suggestion = await this.classifier(cloneJson(identity), cloneJson(deterministic));
      validateSuggestion(suggestion, identity);
      return {
        ...deterministic,
        classifierSuggestion: suggestion.effect,
        classifierConfidence: suggestion.confidence,
        classifierExplanation: suggestion.explanation,
        classifierCanOverride: false,
      };
    } catch (error) {
      return {
        ...deterministic,
        reasons: [...deterministic.reasons, "classifier_failed_closed"],
        deterministicSignals: [...deterministic.deterministicSignals, "classifier:error"],
        classifierSuggestion: null,
        classifierConfidence: null,
        classifierExplanation: error instanceof Error ? error.message : String(error),
        classifierCanOverride: false,
      };
    }
  }

  private deterministic(identity: PermissionIdentityRecord): PermissionRiskAssessment {
    const context = identity.context;
    const argumentsText = JSON.stringify(context.arguments);
    const reasons: string[] = [];
    const signals: string[] = [];
    let score = 0;
    const add = (points: number, signal: string, reason: string): void => {
      score += points;
      signals.push(signal);
      reasons.push(reason);
    };
    if (LOW_RISK_TOOLS.has(context.toolName) || context.operation === "read") {
      add(-25, "operation:read-only", "declared read-only operation");
    }
    if (HIGH_RISK_TOOLS.has(context.toolName)) {
      add(40, `tool:${context.toolName}`, "tool belongs to a high-risk capability family");
    }
    if (context.namespace === "mcp") {
      const readOnly = context.operation === "read" || optionalObject(context.metadata).read_only === true;
      add(readOnly ? 5 : 35, readOnly ? "mcp:read" : "mcp:remote-effect", readOnly
        ? "MCP read crosses a remote capability boundary"
        : "MCP call can perform a remote side effect");
    }
    if (context.namespace === "plugin") add(30, "plugin:capability", "plugin-owned capability requires explicit policy");
    if (context.namespace === "skill" && context.toolName === "skill") add(20, "skill:context-mutation", "skill invocation mutates context and tool scope");
    if (!safeWorkspaceBinding(identity)) {
      // Crossing the granted workspace boundary remains high risk even when
      // the requested operation is nominally read-only.  Offset the read-only
      // discount instead of allowing path escape to be classified as medium.
      add(85, "workspace:path-escape", "requested path is outside the bound workspace");
    }
    const command = commandText(context.arguments);
    if (command) {
      const commandReport = this.commandRisk.analyze(command, {
        workspaceRoot: context.workspaceRoot,
        dialect: commandDialect(context.arguments),
        environmentNames: environmentNames(context.metadata),
      });
      for (const item of commandReport.signals) {
        add(
          item.score,
          `command:${item.category}:${item.id}`,
          item.explanation,
        );
      }
      for (const [patterns, points, prefix, reason] of [
        [DESTRUCTIVE_SHELL, 80, "shell:destructive", "shell command contains a destructive operation"],
        [PRIVILEGE_ESCALATION, 70, "shell:privilege", "shell command requests privilege escalation"],
        [NETWORK_EXFILTRATION, 55, "shell:network", "shell command can send data to an external endpoint"],
      ] as const) {
        for (const [index, pattern] of patterns.entries()) {
          if (pattern.test(command)) add(points, `${prefix}:${index}`, reason);
        }
      }
      if (/[;&|]\s*(?:sh|bash|zsh|cmd|powershell|pwsh)(?:\.exe)?\b/i.test(command)) {
        add(20, "shell:nested-shell", "shell command starts a nested command interpreter");
      }
      if (/\$\(|`[^`]+`|%[^%]+%|\$env:/i.test(command)) {
        add(15, "shell:dynamic-expansion", "shell command contains dynamic expansion");
      }
      if (optionalObject(context.metadata).progressive_verification_driving === true) {
        // QueryEngine computes this field after model metadata is merged, so
        // the model cannot self-label an arbitrary command as verification.
        // The local sandbox and its destructive/network/path guards remain
        // authoritative; this discount only prevents ordinary test runners
        // and interpreter-wrapped reproduction scripts from becoming high
        // risk solely because they execute code inside that sandbox.
        add(
          -25,
          "runtime:canonical-verification",
          "canonical runtime classified the command as behavioral verification",
        );
      }
    }
    if (HIGH_CONFIDENCE_SECRET_PATTERNS.some((pattern) => pattern.test(argumentsText))) {
      add(50, "arguments:secret-material", "arguments appear to contain secret material");
    } else if (LABELED_SECRET_VALUE_PATTERN.test(argumentsText)) {
      // Source edits routinely implement credential handling and therefore
      // contain identifiers such as `secret`, `password`, or `api_key`.
      // Treat those ambiguous literals as reviewable context, not as leaked
      // credentials. High-confidence token/private-key formats above still
      // fail closed for every tool.
      const sourceEdit = SOURCE_EDIT_TOOLS.has(context.toolName);
      add(
        sourceEdit ? 15 : 50,
        sourceEdit ? "arguments:secret-reference" : "arguments:secret-material",
        sourceEdit
          ? "source edit references a secret-bearing field without high-confidence secret material"
          : "arguments appear to contain secret material",
      );
    }
    const paths = extractPaths(context.arguments);
    for (const path of paths) {
      const extension = extname(path).toLowerCase();
      if (EXECUTABLE_EXTENSIONS.has(extension) && context.operation !== "read") {
        const sourceWrite = SOURCE_EDIT_TOOLS.has(context.toolName) && context.operation === "write";
        add(
          sourceWrite ? 15 : 35,
          `file:executable:${extension}`,
          sourceWrite
            ? "workspace edit writes script source whose later execution remains separately controlled"
            : "operation writes or executes an executable file type",
        );
      }
      if (/\.(?:ssh|aws|config|gnupg)(?:[\\/]|$)/i.test(path)) {
        add(45, "file:sensitive-config", "path targets a sensitive credential/configuration directory");
      }
    }
    const annotations = optionalObject(context.metadata.annotations);
    if (annotations.readOnlyHint === true) add(-10, "annotation:read-only", "capability declares a read-only hint");
    if (annotations.destructiveHint === true) add(55, "annotation:destructive", "capability declares a destructive hint");
    if (annotations.openWorldHint === true) add(20, "annotation:open-world", "capability can interact with an open-world system");
    if (annotations.idempotentHint === false) add(15, "annotation:non-idempotent", "capability declares a non-idempotent effect");
    const boundedScore = Math.max(0, Math.min(100, score));
    const level = signals.length === 0
      ? "unknown"
      : boundedScore >= 60
        ? "high"
        : boundedScore >= 25
          ? "medium"
          : "low";
    return {
      level,
      score: boundedScore,
      reasons: [...new Set(reasons)],
      deterministicSignals: [...new Set(signals)],
      classifierSuggestion: null,
      classifierConfidence: null,
      classifierExplanation: null,
      classifierCanOverride: false,
      assessedArgumentsHash: identity.argumentsDigest,
    };
  }
}

function validateSuggestion(suggestion: PermissionClassifierSuggestion, identity: PermissionIdentityRecord): void {
  if (!new Set(["allow", "deny", "ask"]).has(suggestion.effect)) throw new Error("classifier returned an invalid effect");
  if (!Number.isFinite(suggestion.confidence) || suggestion.confidence < 0 || suggestion.confidence > 1) {
    throw new Error("classifier confidence must be in [0,1]");
  }
  if (suggestion.inputDigest !== identity.requestFingerprint && suggestion.inputDigest !== identity.argumentsDigest) {
    throw new Error("classifier suggestion is not bound to the permission request");
  }
  if (!suggestion.outputDigest) throw new Error("classifier suggestion has no output digest");
}

function commandText(argumentsValue: JsonObject): string {
  for (const key of ["command", "cmd", "script", "shell_command"]) {
    const value = argumentsValue[key];
    if (typeof value === "string") return value;
    if (Array.isArray(value) && value.every((part) => typeof part === "string")) return value.join(" ");
  }
  const executable = argumentsValue.executable;
  const argv = argumentsValue.argv;
  if (
    typeof executable === "string"
    && (argv === undefined || (Array.isArray(argv) && argv.every((part) => typeof part === "string")))
  ) {
    return [executable, ...((argv ?? []) as string[])].join(" ");
  }
  return "";
}

function extractPaths(argumentsValue: JsonObject): string[] {
  const output: string[] = [];
  for (const [key, value] of Object.entries(argumentsValue)) {
    if (!/path|file|target|destination|directory|cwd/i.test(key)) continue;
    if (typeof value === "string") output.push(value);
    if (Array.isArray(value)) output.push(...value.filter((item): item is string => typeof item === "string"));
  }
  return output;
}

function commandDialect(argumentsValue: JsonObject): "posix" | "powershell" | "cmd" | "unknown" {
  const value = argumentsValue.dialect
    ?? argumentsValue.shell
    ?? argumentsValue.shell_kind
    ?? argumentsValue.executable;
  if (typeof value !== "string") return "unknown";
  const normalized = value.toLowerCase();
  if (normalized.includes("powershell") || normalized.includes("pwsh")) return "powershell";
  if (normalized === "cmd" || normalized.includes("cmd.exe")) return "cmd";
  if (["bash", "sh", "zsh", "fish", "posix"].some((name) => normalized.includes(name))) return "posix";
  return "unknown";
}

function environmentNames(metadata: JsonObject): string[] {
  const value = metadata.environment_names ?? metadata.environmentNames;
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === "string");
}
