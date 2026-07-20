import type { TransportProtocol } from "./contracts.ts";
import { assertUrl } from "./canonical.ts";

export interface ProviderResolutionInput {
  readonly requestedProvider?: string | null;
  readonly configuredProvider?: string | null;
  readonly configuredBaseUrl?: string | null;
  readonly explicitBaseUrl?: string | null;
  readonly explicitProtocol?: TransportProtocol | null;
  readonly modelId?: string | null;
  readonly environment?: Readonly<Record<string, string | undefined>>;
}

export interface ProviderResolution {
  readonly providerId: string | null;
  readonly baseUrl: string | null;
  readonly protocol: TransportProtocol | null;
  readonly source: "explicit" | "configured" | "environment" | "catalog";
  readonly diagnostics: readonly string[];
}

/**
 * Hermes-derived resolution precedence without taking credential ownership.
 * Explicit request values win, then matching configured values, then provider-
 * specific environment endpoints. It returns references only; secrets are
 * resolved later by CredentialManager after a route has been pinned.
 */
export function resolveProviderHint(input: ProviderResolutionInput): ProviderResolution {
  const environment = input.environment ?? process.env;
  const requested = normalizeProvider(input.requestedProvider);
  const configured = normalizeProvider(input.configuredProvider);
  const diagnostics: string[] = [];
  let providerId = requested && requested !== "auto" ? requested : configured && configured !== "auto" ? configured : null;
  let source: ProviderResolution["source"] = requested && requested !== "auto" ? "explicit" : configured ? "configured" : "catalog";

  const explicitBase = normalizeUrl(input.explicitBaseUrl, "explicitBaseUrl");
  const configuredBase = configured === providerId || configured === "custom" || configured === "auto"
    ? normalizeUrl(input.configuredBaseUrl, "configuredBaseUrl")
    : null;
  let baseUrl = explicitBase ?? configuredBase;
  if (explicitBase) source = "explicit";

  if (providerId === null) {
    const candidates: readonly [string, string, TransportProtocol][] = [
      ["anthropic", "ANTHROPIC_BASE_URL", "anthropic_messages"],
      ["openai", "OPENAI_BASE_URL", "openai_responses"],
      ["openrouter", "OPENROUTER_BASE_URL", "openai_chat"],
    ];
    for (const [provider, variable, protocol] of candidates) {
      const value = normalizeUrl(environment[variable], variable);
      if (!value) continue;
      providerId = provider;
      baseUrl = value;
      source = "environment";
      diagnostics.push(`selected ${provider} from ${variable}`);
      return { providerId, baseUrl, protocol: input.explicitProtocol ?? protocol, source, diagnostics };
    }
  }

  const protocol = input.explicitProtocol ?? detectProtocol(baseUrl, providerId, input.modelId);
  if (baseUrl && providerId === "anthropic" && !isAnthropicCompatibleBaseUrl(baseUrl)) {
    diagnostics.push("configured base URL is not recognized as Anthropic-compatible");
  }
  return { providerId, baseUrl, protocol, source, diagnostics };
}

export function detectProtocol(
  baseUrl: string | null | undefined,
  providerId: string | null | undefined,
  modelId: string | null | undefined,
): TransportProtocol | null {
  const url = (baseUrl ?? "").toLowerCase();
  const provider = normalizeProvider(providerId);
  const model = (modelId ?? "").toLowerCase();
  if (provider === "anthropic" || url.includes("/anthropic") || url.includes("api.anthropic.com")) return "anthropic_messages";
  if (provider === "openai" || provider === "xai" || /(^|[./])api\.openai\.com/.test(url)) return "openai_responses";
  if (model.startsWith("gpt-5") && provider !== "openrouter") return "openai_responses";
  if (provider || url) return "openai_chat";
  return null;
}

export function isAnthropicCompatibleBaseUrl(value: string): boolean {
  const url = assertUrl(value, "baseUrl");
  const text = `${url.hostname}${url.pathname}`.toLowerCase();
  return text.includes("anthropic") || text.includes("claude") || text.includes("azure.com");
}

function normalizeProvider(value: string | null | undefined): string | null {
  const normalized = (value ?? "").trim().toLowerCase();
  return normalized || null;
}

function normalizeUrl(value: string | null | undefined, name: string): string | null {
  const normalized = (value ?? "").trim();
  if (!normalized) return null;
  return assertUrl(normalized, name).toString().replace(/\/$/, "");
}
