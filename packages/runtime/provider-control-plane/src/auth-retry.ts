/**
 * OMP-derived resolver retry contract. This module owns no credential state;
 * it coordinates initial resolve, refresh-same, and rotate-sibling through a
 * caller supplied resolver. ProviderControlPlane still validates the pinned
 * credential version before any request bytes leave the process.
 */
export interface SecretResolveContext {
  readonly lastChance: boolean;
  readonly error: unknown;
  readonly previousFingerprint?: string;
  readonly signal?: AbortSignal;
}

export interface ResolvedSecretCandidate<T> {
  readonly fingerprint: string;
  readonly value: T;
}

export type SecretCandidateResolver<T> = (
  context: SecretResolveContext,
) => Promise<ResolvedSecretCandidate<T> | undefined> | ResolvedSecretCandidate<T> | undefined;

export const AUTH_RETRY_STEPS: readonly boolean[] = [false, true];

export async function resolveRetryCandidate<T>(
  resolver: SecretCandidateResolver<T>,
  lastChance: boolean,
  error: unknown,
  signal?: AbortSignal,
  previousFingerprint?: string,
): Promise<ResolvedSecretCandidate<T> | undefined> {
  try {
    return await resolver({ lastChance, error, signal, previousFingerprint });
  } catch {
    return undefined;
  }
}

export async function withResolvedSecret<TSecret, TResult>(
  resolver: SecretCandidateResolver<TSecret>,
  attempt: (candidate: ResolvedSecretCandidate<TSecret>) => Promise<TResult>,
  options: {
    readonly isRetryableAuthenticationError: (error: unknown) => boolean;
    readonly isUsageLimit?: (error: unknown) => boolean;
    readonly signal?: AbortSignal;
  },
): Promise<TResult> {
  const initial = await resolveRetryCandidate(resolver, false, undefined, options.signal);
  if (initial === undefined) throw new Error("no credential candidate resolved");
  let candidate: ResolvedSecretCandidate<TSecret> = initial;
  let lastError: unknown;
  try {
    return await attempt(candidate);
  } catch (error) {
    if (!options.isRetryableAuthenticationError(error)) throw error;
    lastError = error;
  }
  for (const configuredLastChance of AUTH_RETRY_STEPS) {
    const lastChance = configuredLastChance || Boolean(options.isUsageLimit?.(lastError));
    const next: ResolvedSecretCandidate<TSecret> | undefined = await resolveRetryCandidate(
      resolver,
      lastChance,
      lastError,
      options.signal,
      candidate.fingerprint,
    );
    if (next === undefined || next.fingerprint === candidate.fingerprint) continue;
    candidate = next;
    try {
      return await attempt(candidate);
    } catch (error) {
      if (!options.isRetryableAuthenticationError(error)) throw error;
      lastError = error;
    }
  }
  throw lastError;
}
