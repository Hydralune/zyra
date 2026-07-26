import assert from "node:assert/strict";
import test from "node:test";

import {
  loadRuntimeProcessConfiguration,
  RuntimeProcessConfigurationError,
  runtimeProcessConfigurationFailure,
} from "../src/productization/runtime-config.ts";

test("code-worker has a clean default with no source fallback", () => {
  const configuration = loadRuntimeProcessConfiguration(
    {},
    "C:\\zyra-product",
  );
  assert.equal(configuration.schemaVersion, 1);
  assert.equal(configuration.profile, "default");
  assert.equal(configuration.cleanDefault, true);
  assert.equal(configuration.sourceRuntimeFallback, false);
  assert.equal(configuration.demoRuntimeFallback, false);
  assert.equal(configuration.sourceStoreFallback, false);
  assert.match(configuration.configurationDigest, /^sha256:[a-f0-9]{64}$/);
  assert.equal(
    configuration.providerDatabase,
    "C:\\zyra-product\\tmp\\artifacts\\.provider-control-plane\\provider.sqlite3",
  );
});

test("code-worker consumes the product configuration projection", () => {
  const digest = "sha256:" + "a".repeat(64);
  const configuration = loadRuntimeProcessConfiguration(
    {
      ZYRA_CONFIG_SCHEMA_VERSION: "1",
      ZYRA_CONFIG_DIGEST: digest,
      ZYRA_PROFILE: "sealed",
      ZYRA_STATE_ROOT: "state",
      ZYRA_ARTIFACT_ROOT: "artifact-state",
      ZYRA_PROVIDER_STATE: "providers/provider.sqlite3",
      ZYRA_PROVIDER_DISPATCH_ENABLED: "true",
      ZYRA_REQUIRED_CREDENTIAL_ENVS: "Z_PROVIDER_SECRET",
      Z_PROVIDER_SECRET: "material-never-serialized",
      ZYRA_PROCESS_GENERATION: "generation:test:1",
    },
    "C:\\zyra-product",
  );
  assert.equal(configuration.configurationDigest, digest);
  assert.equal(configuration.profile, "sealed");
  assert.equal(configuration.features.provider_dispatch, true);
  assert.deepEqual(
    configuration.requiredCredentialEnvironments,
    ["Z_PROVIDER_SECRET"],
  );
  assert.deepEqual(
    configuration.credentialPresence,
    { Z_PROVIDER_SECRET: true },
  );
  assert.equal(configuration.processGeneration, "generation:test:1");
  assert.equal(
    JSON.stringify(configuration).includes("material-never-serialized"),
    false,
  );
});

test("code-worker rejects missing credentials and source runtime paths", () => {
  assert.throws(
    () => loadRuntimeProcessConfiguration(
      { ZYRA_REQUIRED_CREDENTIAL_ENVS: "Z_PROVIDER_SECRET" },
      "C:\\zyra-product",
    ),
    (error: unknown) => {
      assert(error instanceof RuntimeProcessConfigurationError);
      assert.equal(error.code, "process_required_credentials_missing");
      const failure = runtimeProcessConfigurationFailure(error);
      assert.equal(failure.lifecycle, "configuration_rejected");
      assert.equal(failure.sourceRuntimeFallback, false);
      return true;
    },
  );
  assert.throws(
    () => loadRuntimeProcessConfiguration(
      { ZYRA_STATE_ROOT: "C:\\agent-zoo\\claude-code-best\\state" },
      "C:\\zyra-product",
    ),
    (error: unknown) => {
      assert(error instanceof RuntimeProcessConfigurationError);
      assert.equal(error.code, "process_config_source_runtime_forbidden");
      return true;
    },
  );
});
