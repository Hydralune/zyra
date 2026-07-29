import type {
  CredentialRecord,
  IntegrationDefinition,
  ModelDefinition,
  ProviderDefinition,
} from "../contracts.ts";
import { fingerprintSecret } from "../canonical.ts";
import type { ProviderControlPlane } from "../control-plane.ts";

export const KIMI_PLATFORM_PROVIDER_ID = "kimi-platform";
export const KIMI_K27_CODE_MODEL_ID = "kimi-k2.7-code";
export const KIMI_PLATFORM_INTEGRATION_ID = "kimi-platform-bearer";
export const KIMI_PLATFORM_CREDENTIAL_ID = "kimi-platform-local";
export const KIMI_API_KEY_ENV = "KIMI_API_KEY";

export interface KimiK27CodeProfile {
  readonly integration: IntegrationDefinition;
  readonly provider: ProviderDefinition;
  readonly model: ModelDefinition;
}

export interface InstalledKimiK27CodeProfile extends KimiK27CodeProfile {
  readonly credential: CredentialRecord;
}

export function kimiK27CodeProfile(): KimiK27CodeProfile {
  const integration: IntegrationDefinition = {
    integrationId: KIMI_PLATFORM_INTEGRATION_ID,
    displayName: "Kimi Open Platform API bearer credential",
    kind: "bearer",
    envNames: [KIMI_API_KEY_ENV],
    headerName: null,
    authorizationScheme: "Bearer",
    supportsRefresh: false,
    metadata: {
      secret_custody: "environment-reference-only",
      service: "kimi-open-platform",
    },
  };
  const provider: ProviderDefinition = {
    providerId: KIMI_PLATFORM_PROVIDER_ID,
    displayName: "Kimi Open Platform",
    integrationId: KIMI_PLATFORM_INTEGRATION_ID,
    status: "active",
    baseUrl: "https://api.moonshot.cn/v1",
    protocol: "openai_chat",
    defaultHeaders: {},
    requestDefaults: {},
    allowedHosts: ["api.moonshot.cn"],
    tags: ["cloud", "coding", "openai-compatible", "real-provider", "pay-as-you-go"],
    metadata: {
      api_reference: "https://platform.kimi.com/docs/api/overview",
      profile_revision: "2026-07-29",
      billing_mode: "pay-as-you-go",
      key_source: "platform.kimi.com",
      preserve_client_identity: true,
    },
  };
  const model: ModelDefinition = {
    providerId: KIMI_PLATFORM_PROVIDER_ID,
    modelId: KIMI_K27_CODE_MODEL_ID,
    displayName: "Kimi K2.7 Code",
    family: "kimi-k2.7-code",
    status: "active",
    enabled: true,
    releasedAt: Date.UTC(2026, 5, 12),
    contextWindow: 262_144,
    maximumOutputTokens: 131_072,
    capabilities: {
      input: ["text", "image", "file"],
      output: ["text", "tool"],
      tools: true,
      streaming: true,
      reasoning: true,
      structuredOutput: false,
    },
    pricing: [{
      inputPerMillion: 6.5,
      outputPerMillion: 27,
      cachedInputPerMillion: 1.3,
      currency: "CNY",
    }],
    endpointPath: "/chat/completions",
    protocol: "openai_chat",
    requestDefaults: {
      thinking: { type: "enabled" },
    },
    tags: ["coding", "always-thinking", "tool-capable", "pay-as-you-go"],
    metadata: {
      model_version: "Kimi K2.7 Code",
      video_input: true,
      availability: "account-model-catalog",
      pricing_model: "pay-as-you-go",
      model_catalog_verified_at: "2026-07-29",
    },
  };
  return { integration, provider, model };
}

export function installKimiK27CodeProfile(
  controlPlane: ProviderControlPlane,
  environment: Readonly<Record<string, string | undefined>> = process.env,
): InstalledKimiK27CodeProfile {
  const apiKey = String(environment[KIMI_API_KEY_ENV] ?? "").trim();
  if (!apiKey) {
    throw new Error(`${KIMI_API_KEY_ENV} is required for the Kimi Open Platform live profile`);
  }

  const profile = kimiK27CodeProfile();
  controlPlane.upsertIntegration(profile.integration);
  controlPlane.upsertProvider(profile.provider);
  controlPlane.upsertModel(profile.model);

  const fingerprint = fingerprintSecret(apiKey);
  const secretRef = `env://${KIMI_API_KEY_ENV}`;
  const existing = controlPlane.credentials
    .list(KIMI_PLATFORM_PROVIDER_ID)
    .find((item) => item.credentialId === KIMI_PLATFORM_CREDENTIAL_ID);
  const credential = existing === undefined
    ? controlPlane.registerCredential({
        credentialId: KIMI_PLATFORM_CREDENTIAL_ID,
        integrationId: KIMI_PLATFORM_INTEGRATION_ID,
        providerId: KIMI_PLATFORM_PROVIDER_ID,
        accountId: "kimi-platform-local",
        secretRef,
        fingerprint,
        priority: 100,
        allowedModels: [KIMI_K27_CODE_MODEL_ID],
        scopes: ["chat.completions"],
        metadata: {
          purpose: "kimi-k2.7-code-live-provider",
          secret_material_persisted: false,
          billing_mode: "pay-as-you-go",
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
            purpose: "kimi-k2.7-code-live-provider",
            secret_material_persisted: false,
            billing_mode: "pay-as-you-go",
          },
        });

  return { ...profile, credential };
}
