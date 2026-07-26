import { createHash } from "node:crypto";
import { isAbsolute, relative, resolve } from "node:path";

export const PROCESS_CONFIG_VERSION = 1;

export type RuntimeFeature =
  | "browser"
  | "edge_worker"
  | "mcp"
  | "provider_dispatch"
  | "terminal"
  | "web";

export interface RuntimeProcessConfigurationSnapshot {
  readonly schema: "zyra.process-runtime-config/v1";
  readonly schemaVersion: number;
  readonly profile: string;
  readonly stateRoot: string;
  readonly providerDatabase: string;
  readonly artifactRoot: string;
  readonly configurationDigest: string;
  readonly processGeneration: string;
  readonly features: Readonly<Record<RuntimeFeature, boolean>>;
  readonly requiredCredentialEnvironments: readonly string[];
  readonly credentialPresence: Readonly<Record<string, boolean>>;
  readonly cleanDefault: boolean;
  readonly sourceRuntimeFallback: false;
  readonly demoRuntimeFallback: false;
  readonly sourceStoreFallback: false;
}

export class RuntimeProcessConfigurationError extends Error {
  readonly code: string;
  readonly key: string;
  readonly retryable: boolean;

  constructor(
    code: string,
    message: string,
    options: {
      readonly key?: string;
      readonly retryable?: boolean;
      readonly cause?: unknown;
    } = {},
  ) {
    super(message, { cause: options.cause });
    this.name = "RuntimeProcessConfigurationError";
    this.code = code;
    this.key = options.key ?? "";
    this.retryable = options.retryable ?? false;
  }

  toJSON(): Record<string, unknown> {
    return {
      schema: "zyra.process-runtime-config-error/v1",
      code: this.code,
      message: this.message,
      key: this.key,
      retryable: this.retryable,
    };
  }
}

type RuntimeEnvironment = Readonly<Record<string, string | undefined>>;

const ENVIRONMENT_NAME = /^[A-Z][A-Z0-9_]{1,127}$/;
const PROFILE_NAME = /^[a-z][a-z0-9_-]{0,63}$/;
const SHA256_DIGEST = /^sha256:[a-f0-9]{64}$/;
const FORBIDDEN_SOURCE_PARTS = Object.freeze([
  "claude-code-best",
  "openhands",
  "browser-use",
  "opencode",
  "source-graphs",
  "source-pool",
  "runtime-sources",
  "vendor-runtimes",
]);

const FEATURE_ENVIRONMENTS: Readonly<
  Record<RuntimeFeature, { readonly name: string; readonly fallback: boolean }>
> = Object.freeze({
  browser: { name: "ZYRA_BROWSER_ENABLED", fallback: true },
  edge_worker: { name: "ZYRA_EDGE_WORKER_ENABLED", fallback: false },
  mcp: { name: "ZYRA_MCP_ENABLED", fallback: false },
  provider_dispatch: {
    name: "ZYRA_PROVIDER_DISPATCH_ENABLED",
    fallback: false,
  },
  terminal: { name: "ZYRA_TERMINAL_ENABLED", fallback: true },
  web: { name: "ZYRA_WEB_ENABLED", fallback: true },
});

function normalizedValue(
  environment: RuntimeEnvironment,
  name: string,
): string | undefined {
  const raw = environment[name];
  if (raw === undefined) return undefined;
  const value = raw.trim();
  if (!value) {
    throw new RuntimeProcessConfigurationError(
      "process_config_environment_empty",
      `environment variable ${name} cannot be empty`,
      { key: name },
    );
  }
  return value;
}

function parseVersion(environment: RuntimeEnvironment): number {
  const raw = normalizedValue(environment, "ZYRA_CONFIG_SCHEMA_VERSION");
  if (raw === undefined) return PROCESS_CONFIG_VERSION;
  if (!/^[0-9]+$/.test(raw)) {
    throw new RuntimeProcessConfigurationError(
      "process_config_version_invalid",
      "ZYRA_CONFIG_SCHEMA_VERSION must be a positive integer",
      { key: "ZYRA_CONFIG_SCHEMA_VERSION" },
    );
  }
  const version = Number(raw);
  if (version !== PROCESS_CONFIG_VERSION) {
    throw new RuntimeProcessConfigurationError(
      version > PROCESS_CONFIG_VERSION
        ? "process_config_version_future"
        : "process_config_version_unsupported",
      `runtime process configuration version ${version} is not supported`,
      { key: "ZYRA_CONFIG_SCHEMA_VERSION" },
    );
  }
  return version;
}

function parseBoolean(
  environment: RuntimeEnvironment,
  name: string,
  fallback: boolean,
): boolean {
  const raw = normalizedValue(environment, name);
  if (raw === undefined) return fallback;
  const normalized = raw.toLowerCase();
  if (["1", "true", "yes", "on"].includes(normalized)) return true;
  if (["0", "false", "no", "off"].includes(normalized)) return false;
  throw new RuntimeProcessConfigurationError(
    "process_config_boolean_invalid",
    `environment variable ${name} must be a boolean`,
    { key: name },
  );
}

function parseProfile(environment: RuntimeEnvironment): string {
  const profile = normalizedValue(environment, "ZYRA_PROFILE") ?? "default";
  if (!PROFILE_NAME.test(profile)) {
    throw new RuntimeProcessConfigurationError(
      "process_config_profile_invalid",
      "ZYRA_PROFILE must use lowercase letters, digits, '_' or '-'",
      { key: "ZYRA_PROFILE" },
    );
  }
  return profile;
}

function resolveStatePath(
  raw: string | undefined,
  fallback: string,
  stateRoot: string,
  name: string,
): string {
  const selected = raw ?? fallback;
  const candidate = resolve(isAbsolute(selected) ? selected : stateRoot, isAbsolute(selected) ? "." : selected);
  const relation = relative(stateRoot, candidate);
  if (
    !isAbsolute(selected)
    && (relation === ".." || relation.startsWith(`..\\`) || relation.startsWith("../"))
  ) {
    throw new RuntimeProcessConfigurationError(
      "process_config_path_escape",
      `${name} escapes the configured state root`,
      { key: name },
    );
  }
  assertNoSourceRuntimePath(candidate, name);
  return candidate;
}

function assertNoSourceRuntimePath(path: string, key: string): void {
  const normalized = path.replaceAll("\\", "/").toLowerCase();
  const matched = FORBIDDEN_SOURCE_PARTS.find((part) =>
    normalized.split("/").includes(part)
  );
  if (matched !== undefined) {
    throw new RuntimeProcessConfigurationError(
      "process_config_source_runtime_forbidden",
      `${key} cannot reference source or vendored runtime path '${matched}'`,
      { key },
    );
  }
}

function parseCredentialEnvironmentNames(
  environment: RuntimeEnvironment,
): string[] {
  const raw = normalizedValue(environment, "ZYRA_REQUIRED_CREDENTIAL_ENVS");
  if (raw === undefined) return [];
  const names = [...new Set(raw.split(",").map((item) => item.trim()))].sort();
  for (const name of names) {
    if (!ENVIRONMENT_NAME.test(name)) {
      throw new RuntimeProcessConfigurationError(
        "process_config_credential_environment_invalid",
        `invalid required credential environment name: ${name}`,
        { key: "ZYRA_REQUIRED_CREDENTIAL_ENVS" },
      );
    }
  }
  return names;
}

function configurationDigest(
  environment: RuntimeEnvironment,
  publicConfiguration: object,
): string {
  const supplied = normalizedValue(environment, "ZYRA_CONFIG_DIGEST");
  if (supplied !== undefined) {
    if (!SHA256_DIGEST.test(supplied)) {
      throw new RuntimeProcessConfigurationError(
        "process_config_digest_invalid",
        "ZYRA_CONFIG_DIGEST must contain a lowercase sha256 digest",
        { key: "ZYRA_CONFIG_DIGEST" },
      );
    }
    return supplied;
  }
  return "sha256:" + createHash("sha256")
    .update(JSON.stringify(publicConfiguration))
    .digest("hex");
}

function processGeneration(environment: RuntimeEnvironment): string {
  const supplied = normalizedValue(environment, "ZYRA_PROCESS_GENERATION");
  if (supplied !== undefined) {
    if (!/^[A-Za-z0-9][A-Za-z0-9:._-]{3,255}$/.test(supplied)) {
      throw new RuntimeProcessConfigurationError(
        "process_generation_invalid",
        "ZYRA_PROCESS_GENERATION has an invalid format",
        { key: "ZYRA_PROCESS_GENERATION" },
      );
    }
    return supplied;
  }
  return `code-worker:${process.pid}:${Date.now()}`;
}

function credentialPresence(
  names: readonly string[],
  environment: RuntimeEnvironment,
): Record<string, boolean> {
  const presence: Record<string, boolean> = {};
  const missing: string[] = [];
  for (const name of names) {
    const present = Boolean(environment[name]?.trim());
    presence[name] = present;
    if (!present) missing.push(name);
  }
  if (missing.length > 0) {
    throw new RuntimeProcessConfigurationError(
      "process_required_credentials_missing",
      `required runtime credentials are unavailable: ${missing.join(", ")}`,
      {
        key: "ZYRA_REQUIRED_CREDENTIAL_ENVS",
        retryable: true,
      },
    );
  }
  return presence;
}

export function loadRuntimeProcessConfiguration(
  environment: RuntimeEnvironment = process.env,
  workingDirectory: string = process.cwd(),
): RuntimeProcessConfigurationSnapshot {
  const schemaVersion = parseVersion(environment);
  const profile = parseProfile(environment);
  const rootRaw = normalizedValue(environment, "ZYRA_STATE_ROOT") ?? "tmp";
  const stateRoot = resolve(
    isAbsolute(rootRaw) ? rootRaw : workingDirectory,
    isAbsolute(rootRaw) ? "." : rootRaw,
  );
  assertNoSourceRuntimePath(stateRoot, "ZYRA_STATE_ROOT");
  const artifactRoot = resolveStatePath(
    normalizedValue(environment, "ZYRA_ARTIFACT_ROOT"),
    "artifacts",
    stateRoot,
    "ZYRA_ARTIFACT_ROOT",
  );
  const providerDatabase = resolveStatePath(
    normalizedValue(environment, "ZYRA_PROVIDER_STATE"),
    ".provider-control-plane/provider.sqlite3",
    artifactRoot,
    "ZYRA_PROVIDER_STATE",
  );
  const features = Object.fromEntries(
    Object.entries(FEATURE_ENVIRONMENTS).map(([feature, binding]) => [
      feature,
      parseBoolean(environment, binding.name, binding.fallback),
    ]),
  ) as Record<RuntimeFeature, boolean>;
  const requiredCredentialEnvironments =
    parseCredentialEnvironmentNames(environment);
  const presence = credentialPresence(
    requiredCredentialEnvironments,
    environment,
  );
  const cleanDefault = normalizedValue(environment, "ZYRA_CONFIG_DIGEST")
    === undefined
    && normalizedValue(environment, "ZYRA_STATE_ROOT") === undefined
    && normalizedValue(environment, "ZYRA_ARTIFACT_ROOT") === undefined
    && normalizedValue(environment, "ZYRA_PROVIDER_STATE") === undefined;
  const digest = configurationDigest(environment, {
    schemaVersion,
    profile,
    stateRoot,
    artifactRoot,
    providerDatabase,
    features,
    requiredCredentialEnvironments,
  });
  return Object.freeze({
    schema: "zyra.process-runtime-config/v1",
    schemaVersion,
    profile,
    stateRoot,
    providerDatabase,
    artifactRoot,
    configurationDigest: digest,
    processGeneration: processGeneration(environment),
    features: Object.freeze(features),
    requiredCredentialEnvironments: Object.freeze(
      requiredCredentialEnvironments,
    ),
    credentialPresence: Object.freeze(presence),
    cleanDefault,
    sourceRuntimeFallback: false,
    demoRuntimeFallback: false,
    sourceStoreFallback: false,
  });
}

export function runtimeProcessConfigurationFailure(
  error: unknown,
): Record<string, unknown> {
  if (error instanceof RuntimeProcessConfigurationError) {
    return {
      ok: false,
      lifecycle: "configuration_rejected",
      error: error.toJSON(),
      sourceRuntimeFallback: false,
      demoRuntimeFallback: false,
      sourceStoreFallback: false,
    };
  }
  return {
    ok: false,
    lifecycle: "configuration_rejected",
    error: {
      schema: "zyra.process-runtime-config-error/v1",
      code: "process_config_unexpected_error",
      message: error instanceof Error
        ? error.message
        : "unexpected runtime process configuration error",
      key: "",
      retryable: false,
    },
    sourceRuntimeFallback: false,
    demoRuntimeFallback: false,
    sourceStoreFallback: false,
  };
}
