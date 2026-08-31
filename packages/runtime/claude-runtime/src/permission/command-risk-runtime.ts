import { isAbsolute, normalize, relative, resolve } from "node:path";

import type { JsonObject } from "../contracts.ts";
import { canonicalize, cloneJson, digest } from "../e02/index.ts";

export type ShellDialect = "posix" | "powershell" | "cmd" | "unknown";

export type ShellConnector =
  | "start"
  | "sequence"
  | "and"
  | "or"
  | "pipe"
  | "background";

export type CommandRiskLevel = "low" | "medium" | "high" | "unknown";

export interface CommandToken {
  value: string;
  raw: string;
  quoted: boolean;
  quote: "single" | "double" | "none";
  dynamic: boolean;
  start: number;
  end: number;
}

export interface CommandRedirection {
  operator: string;
  sourceFd: number | null;
  target: string;
  append: boolean;
  input: boolean;
  dynamic: boolean;
}

export interface CommandSegment {
  index: number;
  connector: ShellConnector;
  raw: string;
  executable: string;
  arguments: string[];
  tokens: CommandToken[];
  assignments: JsonObject;
  redirections: CommandRedirection[];
  nestedShell: boolean;
  dynamicExpansion: boolean;
  commandSubstitution: boolean;
  parseWarnings: string[];
}

export interface CommandPathBinding {
  value: string;
  normalized: string;
  absolute: string | null;
  workspaceRelative: string | null;
  insideWorkspace: boolean;
  dynamic: boolean;
  source: string;
}

export interface CommandEndpoint {
  scheme: string;
  host: string;
  port: number | null;
  path: string;
  loopback: boolean;
  localNetwork: boolean;
  source: string;
}

export interface CommandRiskSignal {
  id: string;
  level: Exclude<CommandRiskLevel, "unknown">;
  score: number;
  segmentIndex: number | null;
  category: string;
  explanation: string;
  evidenceDigest: string;
  metadata: JsonObject;
}

export interface CommandRiskReport {
  version: "zyra.permission-command-risk/v1";
  dialect: ShellDialect;
  workspaceRoot: string;
  commandDigest: string;
  segments: CommandSegment[];
  paths: CommandPathBinding[];
  endpoints: CommandEndpoint[];
  signals: CommandRiskSignal[];
  score: number;
  level: CommandRiskLevel;
  requiresExplicitRule: boolean;
  containsSideEffect: boolean;
  containsNonIdempotentEffect: boolean;
  digest: string;
}

interface SplitPiece {
  raw: string;
  connector: ShellConnector;
  offset: number;
}

interface ExecutableRule {
  names: ReadonlySet<string>;
  category: string;
  score: number;
  level: "low" | "medium" | "high";
  explanation: string;
  nonIdempotent: boolean;
}

const destructiveExecutables = new Set([
  "rm",
  "rmdir",
  "del",
  "erase",
  "remove-item",
  "format",
  "format-volume",
  "mkfs",
  "diskpart",
  "dd",
  "shred",
  "sdelete",
  "truncate",
  "dropdb",
]);

const privilegeExecutables = new Set([
  "sudo",
  "su",
  "doas",
  "runas",
  "runas.exe",
  "pkexec",
  "set-executionpolicy",
  "takeown",
  "icacls",
]);

const networkExecutables = new Set([
  "curl",
  "wget",
  "invoke-webrequest",
  "iwr",
  "invoke-restmethod",
  "irm",
  "scp",
  "sftp",
  "ftp",
  "rsync",
  "nc",
  "ncat",
  "netcat",
  "ssh",
  "telnet",
]);

const packageExecutables = new Set([
  "npm",
  "npx",
  "pnpm",
  "yarn",
  "bun",
  "pip",
  "pip3",
  "uv",
  "cargo",
  "go",
  "gem",
  "composer",
  "apt",
  "apt-get",
  "dnf",
  "yum",
  "brew",
  "winget",
  "choco",
]);

const shellExecutables = new Set([
  "sh",
  "bash",
  "zsh",
  "fish",
  "cmd",
  "cmd.exe",
  "powershell",
  "powershell.exe",
  "pwsh",
  "pwsh.exe",
]);

const readOnlyExecutables = new Set([
  "ls",
  "dir",
  "get-childitem",
  "gci",
  "cat",
  "type",
  "get-content",
  "gc",
  "head",
  "tail",
  "find",
  "findstr",
  "grep",
  "rg",
  "where",
  "where.exe",
  "which",
  "pwd",
  "get-location",
  "git",
]);

const readOnlyGitSubcommands = new Set([
  "status",
  "diff",
  "show",
  "log",
  "rev-parse",
  "ls-files",
  "ls-tree",
  "cat-file",
  "grep",
]);

const executableRules: ExecutableRule[] = [
  {
    names: destructiveExecutables,
    category: "destructive-executable",
    score: 90,
    level: "high",
    explanation: "command invokes an executable with destructive filesystem or storage semantics",
    nonIdempotent: true,
  },
  {
    names: privilegeExecutables,
    category: "privilege-escalation",
    score: 85,
    level: "high",
    explanation: "command requests elevated privileges or changes execution policy",
    nonIdempotent: true,
  },
  {
    names: networkExecutables,
    category: "network-capability",
    score: 48,
    level: "medium",
    explanation: "command can communicate with a network endpoint",
    nonIdempotent: false,
  },
  {
    names: packageExecutables,
    category: "package-or-build-runtime",
    score: 35,
    level: "medium",
    explanation: "command can download packages or execute package lifecycle hooks",
    nonIdempotent: true,
  },
  {
    names: shellExecutables,
    category: "nested-shell",
    score: 25,
    level: "medium",
    explanation: "command starts another command interpreter",
    nonIdempotent: false,
  },
];

const destructiveGitPairs = new Set([
  "reset --hard",
  "clean -f",
  "clean -fd",
  "clean -fdx",
  "checkout --",
  "restore --staged",
  "branch -d",
  "branch -D",
  "push --force",
  "push -f",
]);

const writeVerbs = new Set([
  "set-content",
  "add-content",
  "out-file",
  "new-item",
  "copy-item",
  "move-item",
  "rename-item",
  "remove-item",
  "mkdir",
  "touch",
  "cp",
  "mv",
  "install",
  "tee",
]);

const secretNames = [
  /(?:^|[_-])api[_-]?key(?:$|[_-])/i,
  /(?:^|[_-])access[_-]?token(?:$|[_-])/i,
  /(?:^|[_-])refresh[_-]?token(?:$|[_-])/i,
  /(?:^|[_-])password(?:$|[_-])/i,
  /(?:^|[_-])secret(?:$|[_-])/i,
  /(?:^|[_-])authorization(?:$|[_-])/i,
  /(?:^|[_-])private[_-]?key(?:$|[_-])/i,
  /(?:^|[_-])cookie(?:$|[_-])/i,
];

const credentialPathPatterns = [
  /(?:^|[\\/])\.ssh(?:[\\/]|$)/i,
  /(?:^|[\\/])\.aws(?:[\\/]|$)/i,
  /(?:^|[\\/])\.azure(?:[\\/]|$)/i,
  /(?:^|[\\/])\.gnupg(?:[\\/]|$)/i,
  /(?:^|[\\/])\.kube(?:[\\/]|$)/i,
  /(?:^|[\\/])\.docker(?:[\\/]|$)/i,
  /(?:^|[\\/])credentials?(?:\.[^\\/]*)?$/i,
  /(?:^|[\\/])id_(?:rsa|ed25519|ecdsa)(?:\.[^\\/]*)?$/i,
  /(?:^|[\\/])\.env(?:\.[^\\/]*)?$/i,
];

export class PermissionCommandRiskRuntime {
  analyze(commandValue: string, options: {
    workspaceRoot?: string;
    dialect?: ShellDialect;
    environmentNames?: readonly string[];
  } = {}): CommandRiskReport {
    const command = normalizeCommand(commandValue);
    const dialect = options.dialect ?? detectDialect(command);
    const workspaceRoot = options.workspaceRoot ? resolve(options.workspaceRoot) : "";
    const pieces = splitCommand(command, dialect);
    const segments = pieces.map((piece, index) => parseSegment(piece, index, dialect));
    const paths = collectPaths(segments, workspaceRoot);
    const endpoints = collectEndpoints(segments);
    const signals: CommandRiskSignal[] = [];
    for (const segment of segments) {
      signals.push(...this.executableSignals(segment));
      signals.push(...this.argumentSignals(segment));
      signals.push(...this.redirectionSignals(segment));
      signals.push(...this.expansionSignals(segment));
    }
    signals.push(...this.pathSignals(paths));
    signals.push(...this.endpointSignals(endpoints));
    signals.push(...this.environmentSignals(segments, options.environmentNames ?? []));
    const uniqueSignals = deduplicateSignals(signals);
    const score = boundedScore(uniqueSignals);
    const containsSideEffect = uniqueSignals.some((signal) => [
      "destructive-executable",
      "privilege-escalation",
      "write-operation",
      "output-redirection",
      "network-upload",
      "package-or-build-runtime",
    ].includes(signal.category));
    const containsNonIdempotentEffect = uniqueSignals.some((signal) => signal.metadata.non_idempotent === true);
    const level = riskLevel(score, uniqueSignals);
    const withoutDigest = {
      version: "zyra.permission-command-risk/v1" as const,
      dialect,
      workspaceRoot,
      commandDigest: digest(command),
      segments: segments.map(cloneJson),
      paths: paths.map(cloneJson),
      endpoints: endpoints.map(cloneJson),
      signals: uniqueSignals.map(cloneJson),
      score,
      level,
      requiresExplicitRule: level === "high" || level === "unknown" || containsNonIdempotentEffect,
      containsSideEffect,
      containsNonIdempotentEffect,
    };
    return {
      ...withoutDigest,
      digest: digest(withoutDigest),
    };
  }

  private executableSignals(segment: CommandSegment): CommandRiskSignal[] {
    if (!segment.executable) {
      return [signal(
        "empty-executable",
        "unknown-command",
        "medium",
        30,
        segment.index,
        "command segment has no statically identifiable executable",
        segment.raw,
        { non_idempotent: false },
      )];
    }
    const executable = basename(segment.executable);
    const output: CommandRiskSignal[] = [];
    for (const rule of executableRules) {
      if (!rule.names.has(executable)) {
        continue;
      }
      output.push(signal(
        `${rule.category}:${segment.index}`,
        rule.category,
        rule.level,
        rule.score,
        segment.index,
        rule.explanation,
        segment.raw,
        {
          executable,
          non_idempotent: rule.nonIdempotent,
        },
      ));
    }
    if (readOnlyExecutables.has(executable)) {
      output.push(signal(
        `read-only-executable:${segment.index}`,
        "read-only-executable",
        "low",
        -18,
        segment.index,
        "command executable is normally observational",
        segment.raw,
        {
          executable,
          non_idempotent: false,
        },
      ));
    }
    if (shellExecutables.has(executable) && transparentReadOnlyShellPayload(segment)) {
      output.push(signal(
        `transparent-read-wrapper:${segment.index}`,
        "transparent-read-wrapper",
        "low",
        -12,
        segment.index,
        "shell interpreter wraps one statically bounded observational command",
        segment.raw,
        {
          executable,
          non_idempotent: false,
        },
      ));
    }
    if (writeVerbs.has(executable)) {
      output.push(signal(
        `write-operation:${segment.index}`,
        "write-operation",
        "medium",
        42,
        segment.index,
        "command writes, moves, creates, or removes workspace content",
        segment.raw,
        {
          executable,
          non_idempotent: true,
        },
      ));
    }
    if (executable === "git") {
      const pair = segment.arguments.slice(0, 2).join(" ");
      const exact = [...destructiveGitPairs].find((candidate) => pair.startsWith(candidate));
      if (exact) {
        output.push(signal(
          `destructive-git:${segment.index}`,
          "destructive-git",
          "high",
          88,
          segment.index,
          "git operation can irreversibly discard or overwrite repository state",
          segment.raw,
          {
            operation: exact,
            non_idempotent: true,
          },
        ));
      }
    }
    if (executable === "docker" || executable === "podman") {
      const operation = segment.arguments[0]?.toLowerCase() ?? "";
      const risky = new Set(["run", "exec", "build", "compose", "system", "volume", "rm"]);
      if (risky.has(operation)) {
        output.push(signal(
          `container-operation:${segment.index}`,
          "container-operation",
          operation === "system" || operation === "rm" ? "high" : "medium",
          operation === "system" || operation === "rm" ? 70 : 45,
          segment.index,
          "container command can start isolated processes or mutate container state",
          segment.raw,
          {
            executable,
            operation,
            non_idempotent: true,
          },
        ));
      }
    }
    return output;
  }

  private argumentSignals(segment: CommandSegment): CommandRiskSignal[] {
    const output: CommandRiskSignal[] = [];
    const joined = segment.arguments.join(" ");
    const executable = basename(segment.executable);
    if (destructiveExecutables.has(executable)) {
      const recursive = segment.arguments.some((value) => /^(?:-[a-z]*r[a-z]*|--recursive|-recurse)$/i.test(value));
      const forced = segment.arguments.some((value) => /^(?:-[a-z]*f[a-z]*|--force|-force)$/i.test(value));
      if (recursive || forced) {
        output.push(signal(
          `destructive-flags:${segment.index}`,
          "destructive-flags",
          "high",
          recursive && forced ? 100 : 92,
          segment.index,
          "destructive command uses recursive or force semantics",
          segment.raw,
          {
            recursive,
            forced,
            non_idempotent: true,
          },
        ));
      }
    }
    if (networkExecutables.has(executable)) {
      const upload = /(?:^|\s)(?:-d|--data|--data-binary|--upload-file|-t|-body|-infile)(?:\s|=|$)/i.test(joined);
      const headerSecret = /(?:authorization|cookie|x-api-key|proxy-authorization)\s*:/i.test(joined);
      if (upload || headerSecret) {
        output.push(signal(
          `network-upload:${segment.index}`,
          "network-upload",
          "high",
          headerSecret ? 82 : 68,
          segment.index,
          headerSecret
            ? "network command carries credential-bearing headers"
            : "network command uploads request data or files",
          segment.raw,
          {
            upload,
            credential_header: headerSecret,
            non_idempotent: true,
          },
        ));
      }
    }
    if (packageExecutables.has(executable)) {
      const operation = segment.arguments.find((value) => !value.startsWith("-"))?.toLowerCase() ?? "";
      const lifecycle = new Set(["install", "add", "update", "upgrade", "exec", "run", "publish", "link"]);
      if (lifecycle.has(operation) || executable === "npx") {
        output.push(signal(
          `package-lifecycle:${segment.index}`,
          "package-lifecycle",
          "high",
          62,
          segment.index,
          "package manager operation can download or execute third-party lifecycle code",
          segment.raw,
          {
            executable,
            operation,
            non_idempotent: true,
          },
        ));
      }
    }
    if (/\b(?:eval|invoke-expression|iex)\b/i.test(segment.raw)) {
      output.push(signal(
        `dynamic-evaluation:${segment.index}`,
        "dynamic-evaluation",
        "high",
        78,
        segment.index,
        "command evaluates dynamically constructed source text",
        segment.raw,
        {
          non_idempotent: true,
        },
      ));
    }
    if (/\b(?:base64\s+(?:-d|--decode)|frombase64string|certutil\s+-decode)\b/i.test(segment.raw)) {
      output.push(signal(
        `encoded-payload:${segment.index}`,
        "encoded-payload",
        "medium",
        44,
        segment.index,
        "command decodes an opaque payload before use",
        segment.raw,
        {
          non_idempotent: false,
        },
      ));
    }
    return output;
  }

  private redirectionSignals(segment: CommandSegment): CommandRiskSignal[] {
    const output: CommandRiskSignal[] = [];
    for (const [index, redirection] of segment.redirections.entries()) {
      if (redirection.input) {
        output.push(signal(
          `input-redirection:${segment.index}:${index}`,
          "input-redirection",
          "low",
          4,
          segment.index,
          "command reads content through shell redirection",
          redirection.target,
          {
            target: redirection.target,
            dynamic: redirection.dynamic,
            non_idempotent: false,
          },
        ));
        continue;
      }
      output.push(signal(
        `output-redirection:${segment.index}:${index}`,
        "output-redirection",
        redirection.dynamic ? "high" : "medium",
        redirection.dynamic ? 58 : 34,
        segment.index,
        redirection.dynamic
          ? "command writes to a dynamically resolved redirection target"
          : "command writes content through shell redirection",
        redirection.target,
        {
          target: redirection.target,
          append: redirection.append,
          dynamic: redirection.dynamic,
          non_idempotent: true,
        },
      ));
    }
    return output;
  }

  private expansionSignals(segment: CommandSegment): CommandRiskSignal[] {
    const output: CommandRiskSignal[] = [];
    if (segment.commandSubstitution) {
      output.push(signal(
        `command-substitution:${segment.index}`,
        "command-substitution",
        "medium",
        36,
        segment.index,
        "command executes a nested expression and substitutes its output",
        segment.raw,
        {
          non_idempotent: false,
        },
      ));
    }
    if (segment.dynamicExpansion) {
      output.push(signal(
        `dynamic-expansion:${segment.index}`,
        "dynamic-expansion",
        "medium",
        22,
        segment.index,
        "command result depends on runtime variable or wildcard expansion",
        segment.raw,
        {
          non_idempotent: false,
        },
      ));
    }
    for (const warning of segment.parseWarnings) {
      output.push(signal(
        `parse-warning:${segment.index}:${digest(warning).slice(0, 8)}`,
        "parse-warning",
        "medium",
        28,
        segment.index,
        "shell command could not be fully parsed and must fail closed",
        warning,
        {
          warning,
          non_idempotent: false,
        },
      ));
    }
    return output;
  }

  private pathSignals(paths: CommandPathBinding[]): CommandRiskSignal[] {
    const output: CommandRiskSignal[] = [];
    for (const [index, path] of paths.entries()) {
      if (path.dynamic) {
        output.push(signal(
          `dynamic-path:${index}`,
          "dynamic-path",
          "medium",
          34,
          null,
          "filesystem path is constructed dynamically",
          path.value,
          {
            source: path.source,
            non_idempotent: false,
          },
        ));
      }
      if (path.absolute && !path.insideWorkspace) {
        output.push(signal(
          `workspace-escape:${index}`,
          "workspace-escape",
          "high",
          88,
          null,
          "filesystem path resolves outside the bound workspace",
          path.absolute,
          {
            source: path.source,
            absolute: path.absolute,
            non_idempotent: true,
          },
        ));
      }
      if (credentialPathPatterns.some((pattern) => pattern.test(path.normalized))) {
        output.push(signal(
          `credential-path:${index}`,
          "credential-path",
          "high",
          72,
          null,
          "filesystem path targets a credential or secret-bearing location",
          path.normalized,
          {
            source: path.source,
            non_idempotent: true,
          },
        ));
      }
    }
    return output;
  }

  private endpointSignals(endpoints: CommandEndpoint[]): CommandRiskSignal[] {
    const output: CommandRiskSignal[] = [];
    for (const [index, endpoint] of endpoints.entries()) {
      if (endpoint.loopback) {
        output.push(signal(
          `loopback-endpoint:${index}`,
          "loopback-endpoint",
          "low",
          4,
          null,
          "network endpoint is bound to the local host",
          endpoint.source,
          {
            host: endpoint.host,
            non_idempotent: false,
          },
        ));
        continue;
      }
      output.push(signal(
        `external-endpoint:${index}`,
        "external-endpoint",
        endpoint.localNetwork ? "medium" : "high",
        endpoint.localNetwork ? 32 : 55,
        null,
        endpoint.localNetwork
          ? "network endpoint is on a private or link-local network"
          : "network endpoint is outside the local trust boundary",
        endpoint.source,
        {
          host: endpoint.host,
          scheme: endpoint.scheme,
          local_network: endpoint.localNetwork,
          non_idempotent: false,
        },
      ));
    }
    return output;
  }

  private environmentSignals(segments: CommandSegment[], allowedNames: readonly string[]): CommandRiskSignal[] {
    const allowed = new Set(allowedNames.map((value) => value.toLowerCase()));
    const output: CommandRiskSignal[] = [];
    for (const segment of segments) {
      const names = environmentReferences(segment.raw);
      for (const name of names) {
        const secretLike = secretNames.some((pattern) => pattern.test(name));
        const allowedName = allowed.has(name.toLowerCase());
        if (!secretLike && allowedName) {
          continue;
        }
        output.push(signal(
          `environment-reference:${segment.index}:${name.toLowerCase()}`,
          secretLike ? "secret-environment-reference" : "unbound-environment-reference",
          secretLike ? "high" : "medium",
          secretLike ? 68 : 20,
          segment.index,
          secretLike
            ? "command references an environment variable whose name implies secret material"
            : "command behavior depends on an environment variable outside the declared binding",
          name,
          {
            name,
            declared: allowedName,
            secret_like: secretLike,
            non_idempotent: false,
          },
        ));
      }
    }
    return output;
  }
}

function transparentReadOnlyShellPayload(segment: CommandSegment): boolean {
  if (
    segment.dynamicExpansion
    || segment.commandSubstitution
    || segment.redirections.length > 0
  ) {
    return false;
  }
  const executable = basename(segment.executable);
  const flags = executable === "cmd" || executable === "cmd.exe"
    ? new Set(["/c"])
    : new Set(["-c", "-command"]);
  const index = segment.arguments.findIndex((argument) => flags.has(argument.toLowerCase()));
  if (index < 0 || index >= segment.arguments.length - 1) return false;
  const payload = segment.arguments.slice(index + 1).join(" ").trim();
  if (!payload || /(?:&&|\|\||[|;`]|\$\(|\r|\n|\x00|(?:^|\s)(?:>{1,2}|<{1,2}|2>|2>>|&>)(?:\s|$))/.test(payload)) {
    return false;
  }
  const tokens = payload.match(/(?:"[^"]*"|'[^']*'|\S+)/g) ?? [];
  const head = basename((tokens[0] ?? "").replace(/^['"]|['"]$/g, ""));
  if (!readOnlyExecutables.has(head)) return false;
  if (head !== "git") return true;
  const subcommand = (tokens.find((token, tokenIndex) =>
    tokenIndex > 0 && !token.startsWith("-")
  ) ?? "").replace(/^['"]|['"]$/g, "").toLowerCase();
  return readOnlyGitSubcommands.has(subcommand);
}

function normalizeCommand(value: string): string {
  if (typeof value !== "string") {
    throw new Error("permission command must be a string");
  }
  if (!value.trim()) {
    throw new Error("permission command must not be empty");
  }
  if (value.includes("\0")) {
    throw new Error("permission command contains a NUL byte");
  }
  if (Buffer.byteLength(value, "utf8") > 1_048_576) {
    throw new Error("permission command exceeds one MiB");
  }
  return value.replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
}

function detectDialect(command: string): ShellDialect {
  if (/\$(?:env:)?[A-Za-z_]|\b(?:Get-|Set-|Remove-|Invoke-|Start-)\w+\b|`[A-Za-z]/i.test(command)) {
    return "powershell";
  }
  if (/%[A-Za-z_][A-Za-z0-9_]*%|\b(?:cmd(?:\.exe)?\s+\/c|set\s+[A-Za-z_]+=)/i.test(command)) {
    return "cmd";
  }
  if (/\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\$\(|\b(?:sh|bash|zsh)\s+-c\b/.test(command)) {
    return "posix";
  }
  return "unknown";
}

function splitCommand(command: string, dialect: ShellDialect): SplitPiece[] {
  const output: SplitPiece[] = [];
  let quote: "single" | "double" | null = null;
  let escaping = false;
  let parenDepth = 0;
  let bracketDepth = 0;
  let start = 0;
  let connector: ShellConnector = "start";
  const emit = (end: number, next: ShellConnector, width: number): void => {
    const raw = command.slice(start, end).trim();
    if (raw) {
      output.push({ raw, connector, offset: start });
    }
    connector = next;
    start = end + width;
  };
  for (let index = 0; index < command.length; index += 1) {
    const char = command[index];
    const next = command[index + 1] ?? "";
    if (escaping) {
      escaping = false;
      continue;
    }
    if (quote === "single") {
      if (char === "'") {
        quote = null;
      }
      continue;
    }
    if (quote === "double") {
      if ((dialect === "powershell" && char === "`") || (dialect !== "powershell" && char === "\\")) {
        escaping = true;
        continue;
      }
      if (char === "\"") {
        quote = null;
      }
      continue;
    }
    if (char === "'") {
      quote = "single";
      continue;
    }
    if (char === "\"") {
      quote = "double";
      continue;
    }
    if ((dialect === "powershell" && char === "`") || (dialect !== "powershell" && char === "\\")) {
      escaping = true;
      continue;
    }
    if (char === "(") {
      parenDepth += 1;
      continue;
    }
    if (char === ")") {
      parenDepth = Math.max(0, parenDepth - 1);
      continue;
    }
    if (char === "[") {
      bracketDepth += 1;
      continue;
    }
    if (char === "]") {
      bracketDepth = Math.max(0, bracketDepth - 1);
      continue;
    }
    if (parenDepth > 0 || bracketDepth > 0) {
      continue;
    }
    if (char === "&" && next === "&") {
      emit(index, "and", 2);
      index += 1;
      continue;
    }
    if (char === "|" && next === "|") {
      emit(index, "or", 2);
      index += 1;
      continue;
    }
    if (char === "|") {
      emit(index, "pipe", 1);
      continue;
    }
    if (char === ";") {
      emit(index, "sequence", 1);
      continue;
    }
    if (char === "&" && dialect !== "powershell") {
      emit(index, "background", 1);
      continue;
    }
    if (char === "\n") {
      emit(index, "sequence", 1);
    }
  }
  const tail = command.slice(start).trim();
  if (tail) {
    output.push({ raw: tail, connector, offset: start });
  }
  if (!output.length) {
    output.push({ raw: command, connector: "start", offset: 0 });
  }
  return output;
}

function parseSegment(piece: SplitPiece, index: number, dialect: ShellDialect): CommandSegment {
  const tokenization = tokenize(piece.raw, piece.offset, dialect);
  const redirections: CommandRedirection[] = [];
  const commandTokens: CommandToken[] = [];
  for (let offset = 0; offset < tokenization.tokens.length; offset += 1) {
    const token = tokenization.tokens[offset];
    const redirection = parseRedirection(token, tokenization.tokens[offset + 1]);
    if (redirection) {
      redirections.push(redirection.value);
      offset += redirection.consumedNext ? 1 : 0;
      continue;
    }
    commandTokens.push(token);
  }
  const assignments: JsonObject = {};
  while (commandTokens.length && isAssignment(commandTokens[0].value, dialect)) {
    const assignment = commandTokens.shift()!;
    const separator = assignment.value.indexOf("=");
    assignments[assignment.value.slice(0, separator)] = assignment.value.slice(separator + 1);
  }
  const executable = commandTokens.shift()?.value ?? "";
  const argumentsValue = commandTokens.map((token) => token.value);
  const dynamicExpansion = tokenization.tokens.some((token) => token.dynamic)
    || wildcardExpansion(piece.raw, dialect);
  const commandSubstitution = /\$\(|`[^`\r\n]+`/.test(piece.raw)
    || (dialect === "powershell" && /\$\([^)]*\)/.test(piece.raw));
  return {
    index,
    connector: piece.connector,
    raw: piece.raw,
    executable,
    arguments: argumentsValue,
    tokens: tokenization.tokens,
    assignments: canonicalize(assignments) as JsonObject,
    redirections,
    nestedShell: shellExecutables.has(basename(executable)),
    dynamicExpansion,
    commandSubstitution,
    parseWarnings: tokenization.warnings,
  };
}

function tokenize(raw: string, baseOffset: number, dialect: ShellDialect): {
  tokens: CommandToken[];
  warnings: string[];
} {
  const tokens: CommandToken[] = [];
  const warnings: string[] = [];
  let value = "";
  let rawValue = "";
  let quote: "single" | "double" | null = null;
  let tokenQuote: CommandToken["quote"] = "none";
  let tokenStart = -1;
  let escaping = false;
  let dynamic = false;
  const begin = (index: number): void => {
    if (tokenStart < 0) {
      tokenStart = index;
    }
  };
  const emit = (end: number): void => {
    if (tokenStart < 0) {
      return;
    }
    tokens.push({
      value,
      raw: rawValue,
      quoted: tokenQuote !== "none",
      quote: tokenQuote,
      dynamic,
      start: baseOffset + tokenStart,
      end: baseOffset + end,
    });
    value = "";
    rawValue = "";
    quote = null;
    tokenQuote = "none";
    tokenStart = -1;
    escaping = false;
    dynamic = false;
  };
  for (let index = 0; index < raw.length; index += 1) {
    const char = raw[index];
    begin(index);
    if (escaping) {
      rawValue += char;
      value += char;
      escaping = false;
      continue;
    }
    if (quote === "single") {
      rawValue += char;
      if (char === "'") {
        quote = null;
      } else {
        value += char;
      }
      continue;
    }
    if (quote === "double") {
      rawValue += char;
      if ((dialect === "powershell" && char === "`") || (dialect !== "powershell" && char === "\\")) {
        escaping = true;
        continue;
      }
      if (char === "\"") {
        quote = null;
      } else {
        value += char;
        dynamic ||= char === "$" || char === "%";
      }
      continue;
    }
    if (/\s/.test(char)) {
      emit(index);
      continue;
    }
    rawValue += char;
    if (char === "'") {
      quote = "single";
      if (tokenQuote === "none") {
        tokenQuote = "single";
      }
      continue;
    }
    if (char === "\"") {
      quote = "double";
      if (tokenQuote === "none") {
        tokenQuote = "double";
      }
      continue;
    }
    if ((dialect === "powershell" && char === "`") || (dialect !== "powershell" && char === "\\")) {
      escaping = true;
      continue;
    }
    value += char;
    dynamic ||= char === "$" || char === "%" || char === "*" || char === "?";
  }
  emit(raw.length);
  if (quote) {
    warnings.push(`unterminated ${quote} quote`);
  }
  if (escaping) {
    warnings.push("trailing escape character");
  }
  return { tokens, warnings };
}

function parseRedirection(token: CommandToken, next: CommandToken | undefined): {
  value: CommandRedirection;
  consumedNext: boolean;
} | null {
  const match = token.value.match(/^(?:(\d+))?(>>?|<<?)(.*)$/);
  if (!match) {
    return null;
  }
  const sourceFd = match[1] ? Number(match[1]) : null;
  const operator = match[2];
  const inline = match[3];
  const target = inline || next?.value || "";
  if (!target) {
    return null;
  }
  return {
    value: {
      operator,
      sourceFd,
      target,
      append: operator === ">>",
      input: operator.startsWith("<"),
      dynamic: /[$%*?`]/.test(target),
    },
    consumedNext: !inline && Boolean(next),
  };
}

function isAssignment(value: string, dialect: ShellDialect): boolean {
  if (!/^[A-Za-z_][A-Za-z0-9_]*=/.test(value)) {
    return false;
  }
  return dialect === "posix" || dialect === "unknown";
}

function wildcardExpansion(raw: string, dialect: ShellDialect): boolean {
  if (dialect === "cmd") {
    return /%[A-Za-z_][A-Za-z0-9_]*%/.test(raw) || /[*?]/.test(raw);
  }
  if (dialect === "powershell") {
    return /\$(?:env:)?[A-Za-z_][A-Za-z0-9_:]*/i.test(raw) || /[*?]/.test(raw);
  }
  return /\$\{?[A-Za-z_][A-Za-z0-9_]*\}?/.test(raw) || /[*?]/.test(raw);
}

function collectPaths(segments: CommandSegment[], workspaceRoot: string): CommandPathBinding[] {
  const output: CommandPathBinding[] = [];
  for (const segment of segments) {
    const candidates: Array<{ value: string; source: string; dynamic: boolean }> = [];
    for (const redirection of segment.redirections) {
      candidates.push({
        value: redirection.target,
        source: `segment:${segment.index}:redirection`,
        dynamic: redirection.dynamic,
      });
    }
    for (const [index, argument] of segment.arguments.entries()) {
      if (!looksLikePath(argument)) {
        continue;
      }
      candidates.push({
        value: argument,
        source: `segment:${segment.index}:argument:${index}`,
        dynamic: /[$%*?`]/.test(argument),
      });
    }
    for (const candidate of candidates) {
      const stripped = stripPathDecorators(candidate.value);
      if (!stripped || looksLikeUrl(stripped)) {
        continue;
      }
      const normalized = normalize(stripped);
      const absolute = candidate.dynamic
        ? null
        : isAbsolute(normalized)
          ? resolve(normalized)
          : workspaceRoot
            ? resolve(workspaceRoot, normalized)
            : null;
      const workspaceRelative = absolute && workspaceRoot
        ? relative(workspaceRoot, absolute)
        : null;
      const insideWorkspace = absolute && workspaceRoot
        ? workspaceRelative !== null
          && (workspaceRelative === ""
            || (!workspaceRelative.startsWith("..") && !isAbsolute(workspaceRelative)))
        : !isAbsolute(normalized) && !normalized.replace(/\\/g, "/").startsWith("../");
      output.push({
        value: candidate.value,
        normalized,
        absolute,
        workspaceRelative,
        insideWorkspace,
        dynamic: candidate.dynamic,
        source: candidate.source,
      });
    }
  }
  const seen = new Set<string>();
  return output.filter((item) => {
    const key = digest(item);
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

function collectEndpoints(segments: CommandSegment[]): CommandEndpoint[] {
  const output: CommandEndpoint[] = [];
  const urlPattern = /\b([a-z][a-z0-9+.-]*):\/\/([^\s/'"<>]+)([^\s'"<>]*)/gi;
  for (const segment of segments) {
    for (const match of segment.raw.matchAll(urlPattern)) {
      const scheme = match[1].toLowerCase();
      const authority = match[2];
      const path = match[3] || "/";
      const hostPort = authority.includes("@")
        ? authority.slice(authority.lastIndexOf("@") + 1)
        : authority;
      const parsed = splitHostPort(hostPort);
      output.push({
        scheme,
        host: parsed.host,
        port: parsed.port,
        path,
        loopback: isLoopback(parsed.host),
        localNetwork: isLocalNetwork(parsed.host),
        source: match[0],
      });
    }
    const executable = basename(segment.executable);
    if (new Set(["ssh", "scp", "sftp", "rsync"]).has(executable)) {
      for (const argument of segment.arguments) {
        const match = argument.match(/^(?:[^@\s]+@)?([^:\s]+):/);
        if (!match) {
          continue;
        }
        const host = match[1].toLowerCase();
        output.push({
          scheme: executable,
          host,
          port: null,
          path: argument.slice(match[0].length - 1),
          loopback: isLoopback(host),
          localNetwork: isLocalNetwork(host),
          source: argument,
        });
      }
    }
  }
  const seen = new Set<string>();
  return output.filter((item) => {
    const key = digest(item);
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

function splitHostPort(value: string): { host: string; port: number | null } {
  if (value.startsWith("[")) {
    const close = value.indexOf("]");
    const host = close >= 0 ? value.slice(1, close) : value;
    const suffix = close >= 0 ? value.slice(close + 1) : "";
    const port = suffix.startsWith(":") && /^:\d+$/.test(suffix)
      ? Number(suffix.slice(1))
      : null;
    return { host: host.toLowerCase(), port };
  }
  const separator = value.lastIndexOf(":");
  if (separator > 0 && /^\d+$/.test(value.slice(separator + 1))) {
    return {
      host: value.slice(0, separator).toLowerCase(),
      port: Number(value.slice(separator + 1)),
    };
  }
  return {
    host: value.toLowerCase(),
    port: null,
  };
}

function isLoopback(host: string): boolean {
  const normalized = host.replace(/^\[|\]$/g, "").toLowerCase();
  return normalized === "localhost"
    || normalized === "::1"
    || normalized === "0:0:0:0:0:0:0:1"
    || /^127(?:\.\d{1,3}){3}$/.test(normalized);
}

function isLocalNetwork(host: string): boolean {
  const normalized = host.replace(/^\[|\]$/g, "").toLowerCase();
  if (isLoopback(normalized)) {
    return true;
  }
  if (/^10(?:\.\d{1,3}){3}$/.test(normalized)) {
    return true;
  }
  if (/^192\.168(?:\.\d{1,3}){2}$/.test(normalized)) {
    return true;
  }
  const match = normalized.match(/^172\.(\d{1,3})(?:\.\d{1,3}){2}$/);
  if (match && Number(match[1]) >= 16 && Number(match[1]) <= 31) {
    return true;
  }
  return normalized.startsWith("fe80:")
    || normalized.startsWith("fc")
    || normalized.startsWith("fd")
    || normalized.endsWith(".local");
}

function environmentReferences(raw: string): string[] {
  const names = new Set<string>();
  for (const match of raw.matchAll(/\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|env:([A-Za-z_][A-Za-z0-9_]*)|([A-Za-z_][A-Za-z0-9_]*))/gi)) {
    const name = match[1] || match[2] || match[3];
    if (name) {
      names.add(name);
    }
  }
  for (const match of raw.matchAll(/%([A-Za-z_][A-Za-z0-9_]*)%/g)) {
    names.add(match[1]);
  }
  return [...names].sort((left, right) => left.localeCompare(right));
}

function looksLikePath(value: string): boolean {
  if (!value || value === "." || value === "..") {
    return true;
  }
  if (looksLikeUrl(value)) {
    return false;
  }
  if (/^(?:[A-Za-z]:[\\/]|\\\\|\/|\.\.?(?:[\\/]|$)|~[\\/])/.test(value)) {
    return true;
  }
  if (/[\\/]/.test(value)) {
    return true;
  }
  if (/\.[A-Za-z0-9][A-Za-z0-9._-]{0,15}$/.test(value)) {
    return true;
  }
  return credentialPathPatterns.some((pattern) => pattern.test(value));
}

function looksLikeUrl(value: string): boolean {
  return /^[a-z][a-z0-9+.-]*:\/\//i.test(value);
}

function stripPathDecorators(value: string): string {
  return value
    .replace(/^["']|["']$/g, "")
    .replace(/^file:\/\//i, "")
    .replace(/^~(?=[\\/])/, "")
    .trim();
}

function basename(value: string): string {
  const normalized = value.replace(/\\/g, "/");
  return (normalized.slice(normalized.lastIndexOf("/") + 1) || normalized).toLowerCase();
}

function signal(
  id: string,
  category: string,
  level: "low" | "medium" | "high",
  score: number,
  segmentIndex: number | null,
  explanation: string,
  evidence: string,
  metadata: JsonObject,
): CommandRiskSignal {
  return {
    id,
    level,
    score,
    segmentIndex,
    category,
    explanation,
    evidenceDigest: digest(evidence),
    metadata: canonicalize(metadata) as JsonObject,
  };
}

function deduplicateSignals(values: CommandRiskSignal[]): CommandRiskSignal[] {
  const byId = new Map<string, CommandRiskSignal>();
  for (const value of values) {
    const current = byId.get(value.id);
    if (!current || value.score > current.score) {
      byId.set(value.id, cloneJson(value));
    }
  }
  return [...byId.values()].sort((left, right) => {
    return right.score - left.score
      || left.category.localeCompare(right.category)
      || left.id.localeCompare(right.id);
  });
}

function boundedScore(signals: CommandRiskSignal[]): number {
  let positive = 0;
  let negative = 0;
  const categoryMaximum = new Map<string, number>();
  for (const item of signals) {
    if (item.score < 0) {
      negative += item.score;
      continue;
    }
    const current = categoryMaximum.get(item.category) ?? 0;
    categoryMaximum.set(item.category, Math.max(current, item.score));
  }
  for (const value of categoryMaximum.values()) {
    positive += value;
  }
  return Math.max(0, Math.min(100, positive + negative));
}

function riskLevel(score: number, signals: CommandRiskSignal[]): CommandRiskLevel {
  if (signals.some((item) => item.category === "unknown-command" || item.category === "parse-warning")) {
    if (score < 60) {
      return "unknown";
    }
  }
  if (score >= 60) {
    return "high";
  }
  if (score >= 25) {
    return "medium";
  }
  if (signals.length) {
    return "low";
  }
  return "unknown";
}
