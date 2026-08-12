import type {
  CredentialRecord,
  IntegrationDefinition,
  ModelDefinition,
  ProviderDefinition,
} from "../contracts.ts";
import { fingerprintSecret } from "../canonical.ts";
import type { ProviderControlPlane } from "../control-plane.ts";

export const DEEPSEEK_PROVIDER_ID = "deepseek";
export const DEEPSEEK_V4_PRO_MODEL_ID = "deepseek-v4-pro";
export const DEEPSEEK_INTEGRATION_ID = "deepseek-bearer";
export const DEEPSEEK_CREDENTIAL_ID = "deepseek-local-test";
export const DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY";

export interface DeepSeekV4ProProfile {
  readonly integration: IntegrationDefinition;
  readonly provider: ProviderDefinition;
  readonly model: ModelDefinition;
}

export interface InstalledDeepSeekV4ProProfile extends DeepSeekV4ProProfile {
  readonly credential: CredentialRecord;
}

export function deepSeekV4ProProfile(): DeepSeekV4ProProfile {
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
      profile_revision: "2026-08-13",
      routing_priority: 300,
    },
  };
  const model: ModelDefinition = {
    providerId: DEEPSEEK_PROVIDER_ID,
    modelId: DEEPSEEK_V4_PRO_MODEL_ID,
    displayName: "DeepSeek V4 Pro 0813",
    family: "deepseek-v4",
    status: "active",
    enabled: true,
    releasedAt: Date.UTC(2026, 7, 13),
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
      inputPerMillion: 0.435,
      outputPerMillion: 0.87,
      cachedInputPerMillion: 0.003625,
      currency: "USD",
    }],
    endpointPath: "/chat/completions",
    protocol: "openai_chat",
    requestDefaults: {
      thinking: { type: "enabled" },
    },
    tags: ["agent-test", "thinking-default", "tool-capable"],
    metadata: {
      model_version: "DeepSeek-V4-Pro-0813",
      pricing_checked_at: "2026-08-13",
    },
  };
  return { integration, provider, model };
}

export function installDeepSeekV4ProProfile(
  controlPlane: ProviderControlPlane,
  environment: Readonly<Record<string, string | undefined>> = process.env,
): InstalledDeepSeekV4ProProfile {
  const apiKey = String(environment[DEEPSEEK_API_KEY_ENV] ?? "").trim();
  if (!apiKey) {
    throw new Error(`${DEEPSEEK_API_KEY_ENV} is required for the DeepSeek live profile`);
  }

  const profile = deepSeekV4ProProfile();
  controlPlane.upsertIntegration(profile.integration);
  controlPlane.upsertProvider(profile.provider);
  controlPlane.upsertModel(profile.model);

  const fingerprint = fingerprintSecret(apiKey);
  const secretRef = `env://${DEEPSEEK_API_KEY_ENV}`;
  const existing = controlPlane.credentials
    .list(DEEPSEEK_PROVIDER_ID)
    .find((item) => item.credentialId === DEEPSEEK_CREDENTIAL_ID);
  const credential = existing === undefined
    ? controlPlane.registerCredential({
        credentialId: DEEPSEEK_CREDENTIAL_ID,
        integrationId: DEEPSEEK_INTEGRATION_ID,
        providerId: DEEPSEEK_PROVIDER_ID,
        accountId: "deepseek-local-test",
        secretRef,
        fingerprint,
        priority: 100,
        allowedModels: [DEEPSEEK_V4_PRO_MODEL_ID],
        scopes: ["chat.completions"],
        metadata: {
          purpose: "live-provider-smoke",
          secret_material_persisted: false,
        },
      })
    : existing.fingerprint === fingerprint && existing.secretRef === secretRef
      ? existing
      : controlPlane.credentials.rotate(existing.credentialId, existing.version, {
          secretRef,
          fingerprint,
          expiresAt: null,
          refreshAfter: null,
          scopes: ["chat.completions"],
          metadata: {
            purpose: "live-provider-smoke",
            secret_material_persisted: false,
          },
        });

  return { ...profile, credential };
}
