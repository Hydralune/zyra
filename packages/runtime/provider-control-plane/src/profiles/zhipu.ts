import type {
  CredentialRecord,
  IntegrationDefinition,
  ModelDefinition,
  ProviderDefinition,
} from "../contracts.ts";
import { fingerprintSecret } from "../canonical.ts";
import type { ProviderControlPlane } from "../control-plane.ts";
import { installProfileCredential, requireProfileEnabled } from "./credential-profile.ts";

export const ZHIPU_PROVIDER_ID = "zhipu";
export const GLM_52_MODEL_ID = "glm-5.2";
export const ZHIPU_INTEGRATION_ID = "zhipu-bearer";
export const ZHIPU_CREDENTIAL_ID = "zhipu-local";
export const ZAI_API_KEY_ENV = "ZAI_API_KEY";
export const GLM_ENABLED_ENV = "ZYRA_GLM_ENABLED";

export interface Glm52Profile {
  readonly integration: IntegrationDefinition;
  readonly provider: ProviderDefinition;
  readonly model: ModelDefinition;
}

export interface InstalledGlm52Profile extends Glm52Profile {
  readonly credential: CredentialRecord;
}

export function glm52Profile(): Glm52Profile {
  const integration: IntegrationDefinition = {
    integrationId: ZHIPU_INTEGRATION_ID,
    displayName: "Zhipu AI API bearer credential",
    kind: "bearer",
    envNames: [ZAI_API_KEY_ENV],
    headerName: null,
    authorizationScheme: "Bearer",
    supportsRefresh: false,
    metadata: {
      secret_custody: "environment-reference-only",
      service: "zhipu-ai",
    },
  };
  const provider: ProviderDefinition = {
    providerId: ZHIPU_PROVIDER_ID,
    displayName: "Zhipu AI",
    integrationId: ZHIPU_INTEGRATION_ID,
    status: "active",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
    protocol: "openai_chat",
    defaultHeaders: {},
    requestDefaults: {},
    allowedHosts: ["open.bigmodel.cn"],
    tags: ["cloud", "coding", "openai-compatible", "real-provider", "pay-as-you-go"],
    metadata: {
      api_reference: "https://docs.bigmodel.cn/cn/guide/develop/openai/introduction",
      profile_revision: "2026-07-29",
      billing_mode: "pay-as-you-go",
      key_source: "open.bigmodel.cn",
      preserve_client_identity: true,
      routing_priority: 200,
    },
  };
  const model: ModelDefinition = {
    providerId: ZHIPU_PROVIDER_ID,
    modelId: GLM_52_MODEL_ID,
    displayName: "GLM-5.2",
    family: "glm-5.2",
    status: "active",
    enabled: true,
    releasedAt: Date.UTC(2026, 5, 16),
    contextWindow: 1_000_000,
    maximumOutputTokens: 131_072,
    capabilities: {
      input: ["text"],
      output: ["text", "tool"],
      tools: true,
      streaming: true,
      reasoning: true,
      structuredOutput: true,
    },
    pricing: [{
      inputPerMillion: 8,
      outputPerMillion: 28,
      cachedInputPerMillion: 2,
      currency: "CNY",
    }],
    endpointPath: "/chat/completions",
    protocol: "openai_chat",
    requestDefaults: {
      thinking: { type: "enabled" },
      reasoning_effort: "max",
    },
    tags: ["coding", "long-context", "always-thinking", "tool-capable", "structured-output"],
    metadata: {
      model_version: "GLM-5.2",
      availability: "account-model-catalog",
      pricing_model: "pay-as-you-go",
      model_catalog_verified_at: "2026-07-29",
      pricing_checked_at: "2026-07-31",
      pricing_reference: "https://bigmodel.cn/pricing",
      normalized_input_usd_per_million: 8,
      normalized_cached_input_usd_per_million: 2,
      normalized_output_usd_per_million: 28,
      normalized_pricing_source: "zyra://pricing/conservative-cny-as-usd-upper-bound",
      input_modality: "text",
      output_modality: "text",
      model_reference: "https://docs.bigmodel.cn/cn/guide/models/text/glm-5.2",
    },
  };
  return { integration, provider, model };
}

export function installGlm52Profile(
  controlPlane: ProviderControlPlane,
  environment: Readonly<Record<string, string | undefined>> = process.env,
): InstalledGlm52Profile {
  requireProfileEnabled(environment, GLM_ENABLED_ENV, "Zhipu AI GLM-5.2");
  const apiKey = String(environment[ZAI_API_KEY_ENV] ?? "").trim();
  if (!apiKey) {
    throw new Error(`${ZAI_API_KEY_ENV} is required for the Zhipu AI GLM-5.2 live profile`);
  }

  const profile = glm52Profile();
  controlPlane.upsertIntegration(profile.integration);
  controlPlane.upsertProvider(profile.provider);
  controlPlane.upsertModel(profile.model);

  const fingerprint = fingerprintSecret(apiKey);
  const secretRef = `env://${ZAI_API_KEY_ENV}`;
  const credential = installProfileCredential(controlPlane, {
    credentialId: ZHIPU_CREDENTIAL_ID,
    integrationId: ZHIPU_INTEGRATION_ID,
    providerId: ZHIPU_PROVIDER_ID,
    accountId: "zhipu-local",
    secretRef,
    fingerprint,
    priority: 100,
    allowedModels: [GLM_52_MODEL_ID],
    scopes: ["chat.completions"],
    expiresAt: null,
    refreshAfter: null,
    metadata: {
      purpose: "glm-5.2-live-provider",
      secret_material_persisted: false,
      billing_mode: "pay-as-you-go",
    },
  });

  return { ...profile, credential };
}
