import type {
  CredentialRecord,
  IntegrationDefinition,
  ModelDefinition,
  ProviderDefinition,
} from "../contracts.ts";
import { fingerprintSecret } from "../canonical.ts";
import type { ProviderControlPlane } from "../control-plane.ts";
import { installProfileCredential, requireProfileEnabled } from "./credential-profile.ts";

export const DEEPSEEK_PROVIDER_ID = "deepseek";
export const DEEPSEEK_FLASH_MODEL_ID = "deepseek-flash";
export const DEEPSEEK_INTEGRATION_ID = "deepseek-bearer";
export const DEEPSEEK_CREDENTIAL_ID = "deepseek-local-test";
export const DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY";
export const DEEPSEEK_ENABLED_ENV = "ZYRA_DEEPSEEK_ENABLED";

export interface DeepSeekFlashProfile {
  readonly integration: IntegrationDefinition;
  readonly provider: ProviderDefinition;
  readonly model: ModelDefinition;
}

export interface InstalledDeepSeekFlashProfile extends DeepSeekFlashProfile {
  readonly credential: CredentialRecord;
}

export function deepSeekFlashProfile(): DeepSeekFlashProfile {
  const integration: IntegrationDefinition = {
    integrationId: DEEPSEEK_INTEGRATION_ID,
    displayName: "DeepSeek API bearer credential",
    kind: "bearer",
    envNames: [DEEPSEEK_API_KEY_ENV],
    headerName: null,
    authorizationScheme: "Bearer",
    supportsRefresh: false,
    metadata: {
      secret_custody: "environment-reference-only",
    },
  };
  const provider: ProviderDefinition = {
    providerId: DEEPSEEK_PROVIDER_ID,
    displayName: "DeepSeek",
    integrationId: DEEPSEEK_INTEGRATION_ID,
    status: "active",
    baseUrl: "https://api.deepseek.com",
    protocol: "openai_chat",
    defaultHeaders: {},
    requestDefaults: {},
    allowedHosts: ["api.deepseek.com"],
    tags: ["cloud", "competition-primary", "openai-compatible", "real-provider"],
    metadata: {
      api_reference: "https://api-docs.deepseek.com/api/create-chat-completion",
      profile_revision: "2026-07-31",
      routing_priority: 300,
    },
  };
  const model: ModelDefinition = {
    providerId: DEEPSEEK_PROVIDER_ID,
    modelId: DEEPSEEK_FLASH_MODEL_ID,
    displayName: "DeepSeek Flash",
    family: "deepseek",
    status: "active",
    enabled: true,
    releasedAt: Date.UTC(2026, 3, 24),
    contextWindow: 1_000_000,
    maximumOutputTokens: 384_000,
    capabilities: {
      input: ["text"],
      output: ["text", "tool"],
      tools: true,
      streaming: true,
      reasoning: true,
      structuredOutput: true,
    },
    pricing: [{
      inputPerMillion: 0.14,
      outputPerMillion: 0.28,
      cachedInputPerMillion: 0.0028,
      currency: "USD",
    }],
    endpointPath: "/chat/completions",
    protocol: "openai_chat",
    requestDefaults: {
      thinking: { type: "enabled" },
      reasoning_effort: "high",
    },
    supportedReasoningEfforts: ["low", "high", "max"],
    tags: ["agent-test", "thinking-default", "tool-capable"],
    metadata: {
      model_version: "DeepSeek-Flash",
      pricing_checked_at: "2026-07-31",
      reasoning_effort_verified_at: "2026-09-01",
      reasoning_effort_reference: "https://api-docs.deepseek.com/api/create-chat-completion/",
    },
  };
  return { integration, provider, model };
}

export function installDeepSeekFlashProfile(
  controlPlane: ProviderControlPlane,
  environment: Readonly<Record<string, string | undefined>> = process.env,
): InstalledDeepSeekFlashProfile {
  requireProfileEnabled(environment, DEEPSEEK_ENABLED_ENV, "DeepSeek Flash");
  const apiKey = String(environment[DEEPSEEK_API_KEY_ENV] ?? "").trim();
  if (!apiKey) {
    throw new Error(`${DEEPSEEK_API_KEY_ENV} is required for the DeepSeek live profile`);
  }

  const profile = deepSeekFlashProfile();
  controlPlane.upsertIntegration(profile.integration);
  controlPlane.upsertProvider(profile.provider);
  controlPlane.upsertModel(profile.model);

  const fingerprint = fingerprintSecret(apiKey);
  const secretRef = `env://${DEEPSEEK_API_KEY_ENV}`;
  const credential = installProfileCredential(controlPlane, {
    credentialId: DEEPSEEK_CREDENTIAL_ID,
    integrationId: DEEPSEEK_INTEGRATION_ID,
    providerId: DEEPSEEK_PROVIDER_ID,
    accountId: "deepseek-local-test",
    secretRef,
    fingerprint,
    priority: 100,
    allowedModels: [DEEPSEEK_FLASH_MODEL_ID],
    scopes: ["chat.completions"],
    expiresAt: null,
    refreshAfter: null,
    metadata: {
      purpose: "live-provider-smoke",
      secret_material_persisted: false,
    },
  });

  return { ...profile, credential };
}
