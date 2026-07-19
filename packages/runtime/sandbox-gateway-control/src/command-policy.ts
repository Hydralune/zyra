import {
  Effects,
  Risks,
  aggregateEffects,
  evidence,
  type CommandEnvelope,
  type Effect,
  type JsonValue,
  type PolicyDecision,
  type PolicyEvidence,
} from "./contracts.ts";
import {
  commandDigest,
  digestJson,
  executableName,
} from "./canonical.ts";
import { GitCommandPolicy } from "./git-policy.ts";

const SHELLS = new Set([
  "bash",
  "cmd",
  "cmd.exe",
  "dash",
  "fish",
  "ksh",
  "powershell",
  "powershell.exe",
  "pwsh",
  "sh",
  "zsh",
]);
const INTERPRETERS = new Set([
  ...SHELLS,
  "node",
  "perl",
  "python",
  "python3",
  "ruby",
]);
const PACKAGE_MANAGERS = new Set([
  "bun",
  "cargo",
  "composer",
  "gem",
  "go",
  "npm",
  "npx",
  "pip",
  "pip3",
  "pnpm",
  "poetry",
  "uv",
  "yarn",
]);
const PACKAGE_INSTALL = new Set([
  "add",
  "i",
  "install",
  "remove",
  "rm",
  "uninstall",
  "update",
  "upgrade",
]);
const PACKAGE_EXECUTE = new Set(["dlx", "exec", "run", "run-script", "x"]);
const PACKAGE_PUBLISH = new Set(["login", "logout", "pack", "publish", "token"]);
const NETWORK_EXECUTABLES = new Set([
  "curl",
  "ftp",
  "invoke-restmethod",
  "invoke-webrequest",
  "nc",
  "netcat",
  "scp",
  "sftp",
  "ssh",
  "wget",
]);
const DEFAULT_ALLOWED = new Set([
  "cat",
  "find",
  "findstr",
  "get-childitem",
  "get-content",
  "git",
  "head",
  "node",
  "python",
  "python3",
  "rg",
  "sed",
  "sort",
  "tail",
  "type",
  "wc",
  "where",
  "which",
]);
const HARD_DENIED = new Set([
  "cipher",
  "diskpart",
  "fdisk",
  "format",
  "mkfs",
  "reboot",
  "reg",
  "regedit",
  "shutdown",
]);
const DESTRUCTIVE = new Set(["del", "erase", "rmdir", "rm", "shred"]);
const URL_PATTERN = /\b(?:https?|ftp|ssh|git):\/\/[^\s"']+/giu;
const SHELL_DYNAMIC =
  /(?:&&|\|\||[|;&<>]|\$\(|\$\{|\x60|>\s*\S)/u;
const POWERSHELL_DYNAMIC =
  /\b(?:invoke-expression|iex|start-process|downloadstring|frombase64string)\b|-(?:encodedcommand|enc)\b/iu;
const REDIRECTION = /(?:^|\s)(?:\d?>|>>|<|2>&1|\*>)/u;

export interface CommandPolicyOptions {
  readonly allowedExecutables?: ReadonlySet<string>;
  readonly hardDeniedExecutables?: ReadonlySet<string>;
  readonly destructiveExecutables?: ReadonlySet<string>;
  readonly allowUnknownWithApproval?: boolean;
  readonly sealedDeniesAsk?: boolean;
  readonly maximumArguments?: number;
  readonly maximumArgumentBytes?: number;
  readonly allowNetworkProfiles?: Readonly<Record<string, readonly string[]>>;
}

interface NormalizedPolicyOptions {
  readonly allowedExecutables: ReadonlySet<string>;
  readonly hardDeniedExecutables: ReadonlySet<string>;
  readonly destructiveExecutables: ReadonlySet<string>;
  readonly allowUnknownWithApproval: boolean;
  readonly sealedDeniesAsk: boolean;
  readonly maximumArguments: number;
  readonly maximumArgumentBytes: number;
  readonly allowNetworkProfiles: Readonly<Record<string, readonly string[]>>;
}

export class StructuredCommandPolicy {
  readonly options: NormalizedPolicyOptions;
  readonly gitPolicy: GitCommandPolicy;
  readonly policyDigest: string;

  constructor(
    options: CommandPolicyOptions = {},
    gitPolicy = new GitCommandPolicy(),
  ) {
    this.options = Object.freeze({
      allowedExecutables: options.allowedExecutables ?? DEFAULT_ALLOWED,
      hardDeniedExecutables:
        options.hardDeniedExecutables ?? HARD_DENIED,
      destructiveExecutables:
        options.destructiveExecutables ?? DESTRUCTIVE,
      allowUnknownWithApproval:
        options.allowUnknownWithApproval ?? true,
      sealedDeniesAsk: options.sealedDeniesAsk ?? true,
      maximumArguments: options.maximumArguments ?? 512,
      maximumArgumentBytes: options.maximumArgumentBytes ?? 64 * 1024,
      allowNetworkProfiles: Object.freeze({
        offline: Object.freeze([]),
        ...(options.allowNetworkProfiles ?? {}),
      }),
    });
    this.gitPolicy = gitPolicy;
    this.policyDigest = digestJson(this.descriptor());
  }

  descriptor(): JsonValue {
    return {
      policyId: "zyra.typescript-command-policy.v1",
      ownerSlice: "M1-S05B-01",
      role: "supplementary",
      finalAuthority: "typescript.PermissionCoordinator",
      canonicalGatewayOwner: "SandboxGatewayRuntime",
      callerOverrides: false,
      allowedExecutables: [...this.options.allowedExecutables].sort(),
      hardDeniedExecutables: [
        ...this.options.hardDeniedExecutables,
      ].sort(),
      destructiveExecutables: [
        ...this.options.destructiveExecutables,
      ].sort(),
      allowUnknownWithApproval:
        this.options.allowUnknownWithApproval,
      sealedDeniesAsk: this.options.sealedDeniesAsk,
      maximumArguments: this.options.maximumArguments,
      maximumArgumentBytes: this.options.maximumArgumentBytes,
      networkProfiles: this.options.allowNetworkProfiles,
      gitPolicyDigest: this.gitPolicy.policyDigest,
    };
  }

  evaluate(
    envelope: CommandEnvelope,
    options: { readonly sealed?: boolean } = {},
  ): PolicyDecision {
    const findings: PolicyEvidence[] = [];
    findings.push(...this.evaluateIntegrity(envelope));
    findings.push(...this.evaluateExecutable(envelope));
    findings.push(...this.evaluateShell(envelope));
    findings.push(...this.evaluateInterpreter(envelope));
    findings.push(...this.evaluatePackageManager(envelope));
    findings.push(...this.evaluateNetwork(envelope));
    const git = this.gitPolicy.evaluate(envelope);
    findings.push(...git.evidence);
    let effect = aggregateEffects(findings.map((item) => item.effect));
    const sealed = options.sealed ?? false;
    if (
      sealed &&
      effect === Effects.ask &&
      this.options.sealedDeniesAsk
    ) {
      findings.push(
        evidence(
          "sealed.ask_denied",
          Effects.deny,
          Risks.high,
          "Sealed autonomous mode deterministically denies approval-required commands",
          "typescript.command-policy",
        ),
      );
      effect = Effects.deny;
    }
    const readOnly = this.isReadOnly(findings, git.parsed.readOnly);
    const reason =
      effect === Effects.deny
        ? "Command policy denied one or more high-risk mechanisms"
        : effect === Effects.ask
          ? "Command requires an exact typescript.PermissionCoordinator grant"
          : "Command satisfies deterministic gateway evidence rules";
    const recovery =
      effect === Effects.deny
        ? [
            "Use a structured read-only command",
            "Route file mutation through WorkspaceEditPort",
            "Select an authorized network or dependency workflow",
          ]
        : effect === Effects.ask
          ? ["Obtain an exact one-use permission grant"]
          : [];
    return Object.freeze({
      effect,
      reason,
      commandDigest: commandDigest(envelope),
      policyDigest: this.policyDigest,
      evidence: Object.freeze(findings),
      requiresPermissionRuntime: true,
      eligibleForSealedAutoAllow:
        effect === Effects.allow && readOnly,
      recovery: Object.freeze(recovery),
      metadata: Object.freeze({
        sealed,
        readOnly,
        finalAuthority: "typescript.PermissionCoordinator",
        canonicalGatewayOwner: "SandboxGatewayRuntime",
        gitApplicable: git.parsed.applicable,
        gitSubcommand: git.parsed.subcommand,
      }),
    });
  }

  private evaluateIntegrity(
    envelope: CommandEnvelope,
  ): PolicyEvidence[] {
    const findings: PolicyEvidence[] = [];
    if (!envelope.toolUseId) {
      findings.push(
        evidence(
          "command.tool_use_identity_missing",
          Effects.deny,
          Risks.high,
          "Command must bind to a stable toolUseId",
          "typescript.command-policy",
        ),
      );
    }
    if (envelope.argv.length > this.options.maximumArguments) {
      findings.push(
        evidence(
          "command.argument_count",
          Effects.deny,
          Risks.high,
          "Command exceeds the argument count budget",
          "typescript.command-policy",
        ),
      );
    }
    const bytes = envelope.argv.reduce(
      (total, item) => total + Buffer.byteLength(item, "utf8"),
      0,
    );
    if (bytes > this.options.maximumArgumentBytes) {
      findings.push(
        evidence(
          "command.argument_bytes",
          Effects.deny,
          Risks.high,
          "Command exceeds the argument byte budget",
          "typescript.command-policy",
        ),
      );
    }
    if (
      envelope.metadata.permissionBypass === true ||
      envelope.metadata.autoApprove === true
    ) {
      findings.push(
        evidence(
          "command.untrusted_override_ignored",
          Effects.ask,
          Risks.high,
          "Caller-supplied bypass metadata has no authority",
          "typescript.command-policy",
        ),
      );
    }
    if (
      envelope.workspaceId &&
      (envelope.ownerEpoch <= 0 || !envelope.fenceDigest)
    ) {
      findings.push(
        evidence(
          "command.workspace_fence_missing",
          Effects.deny,
          Risks.high,
          "Workspace command requires an owner epoch and fence digest",
          "typescript.command-policy",
        ),
      );
    }
    return findings;
  }

  private evaluateExecutable(
    envelope: CommandEnvelope,
  ): PolicyEvidence[] {
    const name = executableName(envelope.executable);
    if (this.options.hardDeniedExecutables.has(name)) {
      return [
        evidence(
          "executable.hard_denied",
          Effects.deny,
          Risks.critical,
          "Executable is denied by deployment policy: " + name,
          "typescript.command-policy",
          { executable: name },
        ),
      ];
    }
    if (this.options.destructiveExecutables.has(name)) {
      return [
        evidence(
          "executable.destructive",
          Effects.deny,
          Risks.critical,
          "Destructive executable cannot bypass workspace transactions: " +
            name,
          "typescript.command-policy",
          { executable: name },
        ),
      ];
    }
    if (this.options.allowedExecutables.has(name)) {
      return [
        evidence(
          "executable.registered",
          Effects.allow,
          Risks.low,
          "Executable is registered by deployment policy: " + name,
          "typescript.command-policy",
          { executable: name },
        ),
      ];
    }
    return [
      evidence(
        "executable.unknown",
        this.options.allowUnknownWithApproval
          ? Effects.ask
          : Effects.deny,
        Risks.high,
        "Unregistered executable fails to explicit approval: " + name,
        "typescript.command-policy",
        { executable: name },
      ),
    ];
  }

  private evaluateShell(envelope: CommandEnvelope): PolicyEvidence[] {
    const name = executableName(envelope.executable);
    if (!SHELLS.has(name)) {
      const suspicious = envelope.argv.some((item) =>
        SHELL_DYNAMIC.test(item),
      );
      return suspicious
        ? [
            evidence(
              "argument.shell_like_literal",
              Effects.ask,
              Risks.medium,
              "Shell-like syntax in a structured argument requires review",
              "typescript.command-policy",
            ),
          ]
        : [];
    }
    const findings: PolicyEvidence[] = [
      evidence(
        "shell.interpreter",
        Effects.ask,
        Risks.high,
        "Shell interpreter requires exact immutable command approval",
        "typescript.command-policy",
      ),
    ];
    const command = extractShellCommand(name, envelope.argv);
    if (!command) {
      findings.push(
        evidence(
          "shell.command_missing",
          Effects.deny,
          Risks.high,
          "Shell invocation does not expose a command for analysis",
          "typescript.command-policy",
        ),
      );
      return findings;
    }
    if (POWERSHELL_DYNAMIC.test(command)) {
      findings.push(
        evidence(
          "shell.powershell_dynamic",
          Effects.deny,
          Risks.critical,
          "Dynamic or encoded PowerShell is denied",
          "typescript.command-policy",
        ),
      );
    }
    if (/\$\([^)]*\)|\x60[^\x60]*\x60/u.test(command)) {
      findings.push(
        evidence(
          "shell.command_substitution",
          Effects.deny,
          Risks.critical,
          "Command substitution mutates approved execution material",
          "typescript.command-policy",
        ),
      );
    }
    if (REDIRECTION.test(command)) {
      findings.push(
        evidence(
          "shell.redirection",
          Effects.deny,
          Risks.critical,
          "Shell redirection bypasses WorkspaceEditPort transactions",
          "typescript.command-policy",
        ),
      );
    }
    if (SHELL_DYNAMIC.test(command)) {
      findings.push(
        evidence(
          "shell.compound",
          Effects.ask,
          Risks.high,
          "Compound shell syntax expands the execution graph",
          "typescript.command-policy",
        ),
      );
    }
    return findings;
  }

  private evaluateInterpreter(
    envelope: CommandEnvelope,
  ): PolicyEvidence[] {
    const name = executableName(envelope.executable);
    if (!INTERPRETERS.has(name)) {
      return [];
    }
    if (
      envelope.argv.some((item) =>
        ["-c", "-e", "--eval", "-Command", "-command", "/c"].includes(
          item,
        ),
      )
    ) {
      return [
        evidence(
          "interpreter.inline_program",
          Effects.ask,
          Risks.high,
          "Inline interpreter source is permission-bound command material",
          "typescript.command-policy",
        ),
      ];
    }
    return [];
  }

  private evaluatePackageManager(
    envelope: CommandEnvelope,
  ): PolicyEvidence[] {
    const name = executableName(envelope.executable);
    if (!PACKAGE_MANAGERS.has(name)) {
      return [];
    }
    const subcommand =
      envelope.argv.find((item) => !item.startsWith("-"))?.toLocaleLowerCase(
        "en-US",
      ) ?? "";
    if (PACKAGE_PUBLISH.has(subcommand)) {
      return [
        evidence(
          "package_manager.publish_or_auth",
          Effects.deny,
          Risks.critical,
          "Package publication and authentication are outside the sandbox profile",
          "typescript.command-policy",
          { subcommand },
        ),
      ];
    }
    if (PACKAGE_INSTALL.has(subcommand)) {
      return [
        evidence(
          "package_manager.dependency_mutation",
          Effects.deny,
          Risks.high,
          "Dependency installation requires a dedicated dependency workflow",
          "typescript.command-policy",
          { subcommand },
        ),
      ];
    }
    if (PACKAGE_EXECUTE.has(subcommand) || name === "npx") {
      return [
        evidence(
          "package_manager.script_execution",
          Effects.ask,
          Risks.high,
          "Package-manager script executes project-controlled code",
          "typescript.command-policy",
          { subcommand },
        ),
      ];
    }
    return [
      evidence(
        "package_manager.unknown_subcommand",
        Effects.ask,
        Risks.high,
        "Package-manager subcommand requires explicit review",
        "typescript.command-policy",
        { subcommand },
      ),
    ];
  }

  private evaluateNetwork(envelope: CommandEnvelope): PolicyEvidence[] {
    const name = executableName(envelope.executable);
    const urls = [
      envelope.executable,
      ...envelope.argv,
    ].flatMap((item) => item.match(URL_PATTERN) ?? []);
    const networkCapable = NETWORK_EXECUTABLES.has(name);
    if (!networkCapable && urls.length === 0) {
      return [];
    }
    const profileHosts =
      this.options.allowNetworkProfiles[envelope.networkProfile];
    if (profileHosts === undefined) {
      return [
        evidence(
          "network.profile_unknown",
          Effects.deny,
          Risks.high,
          "Unknown network profile fails closed",
          "typescript.command-policy",
          { profile: envelope.networkProfile },
        ),
      ];
    }
    if (envelope.networkProfile === "offline") {
      return [
        evidence(
          "network.offline",
          Effects.deny,
          Risks.high,
          "Network-capable command is blocked by the offline profile",
          "typescript.command-policy",
        ),
      ];
    }
    const findings: PolicyEvidence[] = [];
    for (const value of urls) {
      let target: URL;
      try {
        target = new URL(value);
      } catch {
        findings.push(
          evidence(
            "network.target_invalid",
            Effects.deny,
            Risks.high,
            "Network target could not be parsed",
            "typescript.command-policy",
          ),
        );
        continue;
      }
      const host = target.hostname.toLocaleLowerCase("en-US");
      const allowed = profileHosts.some((candidate) => {
        const normalized = candidate.toLocaleLowerCase("en-US");
        return host === normalized || host.endsWith("." + normalized);
      });
      findings.push(
        evidence(
          allowed ? "network.target_allowed" : "network.host_not_allowed",
          allowed ? Effects.ask : Effects.deny,
          Risks.high,
          allowed
            ? "Network target matches the profile and requires exact approval"
            : "Network host is not allowlisted",
          "typescript.command-policy",
          {
            hostDigest: digestJson({ host }),
            scheme: target.protocol.replace(":", ""),
          },
        ),
      );
    }
    if (networkCapable && urls.length === 0) {
      findings.push(
        evidence(
          "network.target_unresolved",
          Effects.ask,
          Risks.high,
          "Network-capable command has no statically resolved target",
          "typescript.command-policy",
        ),
      );
    }
    return findings;
  }

  private isReadOnly(
    findings: readonly PolicyEvidence[],
    gitReadOnly: boolean,
  ): boolean {
    const deniedCodes = new Set([
      "executable.destructive",
      "git.destructive",
      "git.local_mutation",
      "network.target_allowed",
      "package_manager.dependency_mutation",
      "package_manager.script_execution",
      "shell.redirection",
    ]);
    if (findings.some((item) => deniedCodes.has(item.code))) {
      return false;
    }
    if (
      findings.some((item) => item.code.startsWith("git.")) &&
      !gitReadOnly
    ) {
      return false;
    }
    return true;
  }
}

function extractShellCommand(
  executable: string,
  argv: readonly string[],
): string {
  if (executable === "cmd" || executable === "cmd.exe") {
    const index = argv.findIndex((item) =>
      ["/c", "/k"].includes(item.toLocaleLowerCase("en-US")),
    );
    return index >= 0 ? argv.slice(index + 1).join(" ") : "";
  }
  const index = argv.findIndex((item) =>
    ["-c", "-Command", "-command"].includes(item),
  );
  if (index >= 0) {
    return argv.slice(index + 1).join(" ");
  }
  const encoded = argv.findIndex((item) =>
    ["-encodedcommand", "-enc"].includes(
      item.toLocaleLowerCase("en-US"),
    ),
  );
  return encoded >= 0 ? argv.slice(encoded).join(" ") : "";
}
