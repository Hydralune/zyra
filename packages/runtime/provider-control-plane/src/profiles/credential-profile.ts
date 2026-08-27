import { digestJson, uniqueSorted } from "../canonical.ts";
import type { ProviderControlPlane } from "../control-plane.ts";
import type { CredentialRecord } from "../contracts.ts";
import type { CredentialRegistration } from "../credentials.ts";

export function profileEnabled(
  environment: Readonly<Record<string, string | undefined>>,
  environmentName: string,
): boolean {
  const raw = String(environment[environmentName] ?? "").trim().toLowerCase();
  if (!raw || ["1", "true", "yes", "on", "enabled"].includes(raw)) return true;
  if (["0", "false", "no", "off", "disabled"].includes(raw)) return false;
  throw new Error(
    `${environmentName} must be true/false, 1/0, yes/no, on/off, or enabled/disabled`,
  );
}

export function requireProfileEnabled(
  environment: Readonly<Record<string, string | undefined>>,
  environmentName: string,
  displayName: string,
): void {
  if (profileEnabled(environment, environmentName)) return;
  throw new Error(`${displayName} is disabled by ${environmentName}`);
}

export function installProfileCredential(
  controlPlane: ProviderControlPlane,
  registration: CredentialRegistration & { readonly credentialId: string },
): CredentialRecord {
  const existing = controlPlane.credentials
    .list(registration.providerId)
    .find((item) => item.credentialId === registration.credentialId);
  if (existing === undefined) return controlPlane.registerCredential(registration);

  if (
    existing.providerId !== registration.providerId
    || existing.integrationId !== registration.integrationId
    || existing.accountId !== registration.accountId
  ) {
    throw new Error(
      `credential identity does not match configured profile: ${registration.credentialId}`,
    );
  }

  const allowedModels = uniqueSorted(registration.allowedModels ?? []);
  const scopes = uniqueSorted(registration.scopes ?? []);
  const metadata = registration.metadata ?? {};
  const priority = registration.priority ?? 0;
  const expiresAt = registration.expiresAt ?? null;
  const refreshAfter = registration.refreshAfter ?? null;
  const matches = existing.secretRef === registration.secretRef
    && existing.fingerprint === registration.fingerprint
    && existing.priority === priority
    && digestJson(existing.allowedModels) === digestJson(allowedModels)
    && digestJson(existing.scopes) === digestJson(scopes)
    && existing.expiresAt === expiresAt
    && existing.refreshAfter === refreshAfter
    && digestJson(existing.metadata) === digestJson(metadata);
  if (matches) return existing;

  return controlPlane.credentials.rotate(existing.credentialId, existing.version, {
    secretRef: registration.secretRef,
    fingerprint: registration.fingerprint,
    priority,
    allowedModels,
    scopes,
    expiresAt,
    refreshAfter,
    metadata,
  });
}
