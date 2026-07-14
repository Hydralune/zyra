import {
  Effects,
  Risks,
  evidence,
  type CommandEnvelope,
  type Effect,
  type JsonValue,
  type PolicyEvidence,
} from "./contracts.ts";
import { digestJson, executableName } from "./canonical.ts";

const READ_ONLY = new Set([
  "blame",
  "branch",
  "diff",
  "grep",
  "log",
  "ls-files",
  "ls-tree",
  "merge-base",
  "rev-list",
  "rev-parse",
  "show",
  "status",
  "tag",
]);

const MUTATING = new Set([
  "add",
  "am",
  "apply",
  "bisect",
  "checkout",
  "cherry-pick",
  "clean",
  "clone",
  "commit",
  "fetch",
  "init",
  "merge",
  "mv",
  "pull",
  "push",
  "rebase",
  "reset",
  "restore",
  "revert",
  "rm",
  "stash",
  "submodule",
  "switch",
  "worktree",
]);

const DESTRUCTIVE = new Set(["clean", "push", "reset", "restore", "rm"]);
const NETWORK = new Set(["clone", "fetch", "ls-remote", "pull", "push", "submodule"]);
const DANGEROUS_OPTIONS = new Set([
  "--delete",
  "--force",
  "-f",
  "--hard",
  "--mirror",
  "--prune",
  "--recurse-submodules",
]);
const OPTIONS_WITH_VALUES = new Set([
  "-C",
  "-c",
  "--exec-path",
  "--git-dir",
  "--namespace",
  "--super-prefix",
  "--work-tree",
]);

export interface ParsedGitCommand {
  readonly applicable: boolean;
  readonly subcommand: string;
  readonly globalOptions: readonly string[];
  readonly options: readonly string[];
  readonly operands: readonly string[];
  readonly readOnly: boolean;
  readonly mutating: boolean;
  readonly destructive: boolean;
  readonly network: boolean;
}

export interface GitPolicyDecision {
  readonly effect: Effect;
  readonly parsed: ParsedGitCommand;
  readonly evidence: readonly PolicyEvidence[];
  readonly policyDigest: string;
}

export interface GitPolicyOptions {
  readonly allowReadOnly?: boolean;
  readonly localMutationRequiresApproval?: boolean;
  readonly denyDestructive?: boolean;
  readonly denyNetwork?: boolean;
}

export class GitCommandPolicy {
  readonly options: Required<GitPolicyOptions>;
  readonly policyDigest: string;

  constructor(options: GitPolicyOptions = {}) {
    this.options = Object.freeze({
      allowReadOnly: options.allowReadOnly ?? true,
      localMutationRequiresApproval:
        options.localMutationRequiresApproval ?? true,
      denyDestructive: options.denyDestructive ?? true,
      denyNetwork: options.denyNetwork ?? true,
    });
    this.policyDigest = digestJson({
      id: "zyra.git-command-policy.v1",
      options: this.options as unknown as JsonValue,
      readOnly: [...READ_ONLY].sort(),
      mutating: [...MUTATING].sort(),
      destructive: [...DESTRUCTIVE].sort(),
      network: [...NETWORK].sort(),
    });
  }

  evaluate(envelope: CommandEnvelope): GitPolicyDecision {
    const parsed = parseGitCommand(envelope);
    if (!parsed.applicable) {
      return {
        effect: Effects.allow,
        parsed,
        evidence: [],
        policyDigest: this.policyDigest,
      };
    }
    const findings: PolicyEvidence[] = [];
    if (!parsed.subcommand) {
      findings.push(
        evidence(
          "git.subcommand_missing",
          Effects.ask,
          Risks.medium,
          "Git invocation has no explicit subcommand",
          "typescript.git-policy",
        ),
      );
    } else if (parsed.readOnly) {
      findings.push(
        evidence(
          "git.read_only",
          this.options.allowReadOnly ? Effects.allow : Effects.ask,
          Risks.low,
          "Git subcommand is classified as read-only: " + parsed.subcommand,
          "typescript.git-policy",
          { subcommand: parsed.subcommand },
        ),
      );
    } else if (parsed.mutating) {
      if (parsed.destructive && this.options.denyDestructive) {
        findings.push(
          evidence(
            "git.destructive",
            Effects.deny,
            Risks.critical,
            "Destructive Git operation is denied: " + parsed.subcommand,
            "typescript.git-policy",
            { subcommand: parsed.subcommand },
          ),
        );
      } else if (parsed.network && this.options.denyNetwork) {
        findings.push(
          evidence(
            "git.network",
            Effects.deny,
            Risks.high,
            "Network Git operation requires a separate network profile",
            "typescript.git-policy",
            { subcommand: parsed.subcommand },
          ),
        );
      } else if (this.options.localMutationRequiresApproval) {
        findings.push(
          evidence(
            "git.local_mutation",
            Effects.ask,
            Risks.high,
            "Local Git mutation requires exact one-use approval",
            "typescript.git-policy",
            { subcommand: parsed.subcommand },
          ),
        );
      } else {
        findings.push(
          evidence(
            "git.mutation_denied",
            Effects.deny,
            Risks.high,
            "Git mutation is disabled by deployment policy",
            "typescript.git-policy",
            { subcommand: parsed.subcommand },
          ),
        );
      }
    } else {
      findings.push(
        evidence(
          "git.unknown_subcommand",
          Effects.deny,
          Risks.high,
          "Unknown Git subcommand fails closed: " + parsed.subcommand,
          "typescript.git-policy",
          { subcommand: parsed.subcommand },
        ),
      );
    }
    const effect = findings.some((item) => item.effect === Effects.deny)
      ? Effects.deny
      : findings.some((item) => item.effect === Effects.ask)
        ? Effects.ask
        : Effects.allow;
    return {
      effect,
      parsed,
      evidence: Object.freeze(findings),
      policyDigest: this.policyDigest,
    };
  }
}

export function parseGitCommand(envelope: CommandEnvelope): ParsedGitCommand {
  if (!["git", "git.exe"].includes(executableName(envelope.executable))) {
    return Object.freeze({
      applicable: false,
      subcommand: "",
      globalOptions: [],
      options: [],
      operands: [],
      readOnly: false,
      mutating: false,
      destructive: false,
      network: false,
    });
  }
  const globalOptions: string[] = [];
  const options: string[] = [];
  const operands: string[] = [];
  let subcommand = "";
  for (let index = 0; index < envelope.argv.length; index += 1) {
    const token = envelope.argv[index] as string;
    if (!subcommand && token.startsWith("-")) {
      globalOptions.push(token);
      if (OPTIONS_WITH_VALUES.has(token) && index + 1 < envelope.argv.length) {
        index += 1;
        globalOptions.push(envelope.argv[index] as string);
      }
      continue;
    }
    if (!subcommand) {
      subcommand = token.toLocaleLowerCase("en-US");
    } else if (token.startsWith("-")) {
      options.push(token);
    } else {
      operands.push(token);
    }
  }
  const destructive =
    DESTRUCTIVE.has(subcommand) ||
    options.some((option) => DANGEROUS_OPTIONS.has(option));
  return Object.freeze({
    applicable: true,
    subcommand,
    globalOptions: Object.freeze(globalOptions),
    options: Object.freeze(options),
    operands: Object.freeze(operands),
    readOnly: READ_ONLY.has(subcommand),
    mutating: MUTATING.has(subcommand),
    destructive,
    network: NETWORK.has(subcommand),
  });
}
