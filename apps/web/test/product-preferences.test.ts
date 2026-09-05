import { expect, test } from "bun:test"
import type { ArtifactProjection } from "../../../packages/core/typed-api-client/src/index.ts"
import { ProductPreferenceStore } from "../src/shell/product-preferences.ts"
import { productArtifacts } from "../src/features/artifacts/product-artifacts.ts"
import { modelChoices } from "../src/components/settings/product-settings.tsx"
import { TaskApi } from "../src/api/task-api.ts"
import type { ZyraApiClient } from "../src/api/client.ts"
import { createIdentity, createRequestId } from "../../../packages/core/typed-api-client/src/index.ts"

test("control commands preserve the owner session key while validating its normalized alias", async () => {
  let captured: { body: Record<string, unknown>; binding: Record<string, unknown> } | undefined
  const api = new TaskApi({ endpoint: async (_operation: string, input: typeof captured) => {
    captured = input
    throw new Error("captured transport boundary")
  } } as unknown as ZyraApiClient)
  await expect(api.controlCommand({ taskId: "task_alias_001", runId: "run_alias_001", sessionId: "task:task_alias_001",
    text: '/rename "Readable title"', arguments: { title: "Readable title" }, requestId: createRequestId(), commandId: createIdentity("control_command"),
  })).rejects.toThrow("captured transport boundary")
  expect(captured?.body.session_id).toBe("task:task_alias_001")
  expect(captured?.binding.sessionId).toBe("session_task_alias_001")
})

test("preferences persist per backend and archive is reversible without losing titles", () => {
  const data = new Map<string, string>()
  const storage = { getItem: (key: string) => data.get(key) ?? null, setItem: (key: string, value: string) => { data.set(key, value) } }
  const store = new ProductPreferenceStore("api-one", storage)
  store.update({ fontSize: 18, execution: { providerId: "deepseek", modelId: "flash", reasoningEffort: "low" } })
  store.rememberTitle("session-one", "Readable title")
  store.archive("session-one", true)
  const restored = new ProductPreferenceStore("api-one", storage)
  expect(restored.getSnapshot()).toEqual(store.getSnapshot())
  restored.archive("session-one", false)
  expect(restored.getSnapshot().archived).toEqual([])
  expect(restored.getSnapshot().titles["session-one"]).toBe("Readable title")
  expect(new ProductPreferenceStore("api-two", storage).getSnapshot().execution).toBeUndefined()
})

test("failed browser storage does not claim a preference was saved", () => {
  const store = new ProductPreferenceStore("api", { getItem: () => "broken json", setItem: () => { throw new Error("quota") } })
  expect(() => store.update({ fontSize: 18 })).toThrow("quota")
  expect(store.getSnapshot().fontSize).toBe(16)
})

test("only known runtime records leave deliverables, including internally labelled user files", () => {
  const rows: ArtifactProjection[] = [
    { artifactId: "manifest", kind: "json", metadata: { domain_result_kind: "code_worker_execution" } },
    { artifactId: "memory", kind: "json", metadata: { domain_result_kind: "memory_continuity" } },
    { artifactId: "legacy", kind: "json", title: "Physical CodeWorker delivery manifest", metadata: {} },
    { artifactId: "report", kind: "text", path: "report.md", metadata: { security_label: "internal" } },
    { artifactId: "unknown", kind: "json", metadata: { domain_result_kind: "new_user_result" } },
  ]
  expect(productArtifacts(rows).map((row) => row.artifactId)).toEqual(["report", "unknown"])
  expect(rows).toHaveLength(5)
})

test("model choices use the authoritative provider catalog and supported reasoning efforts", () => {
  const response = { schema: "zyra.provider-backend-api/v1", state_owner: "typescript.ProviderControlPlaneStore",
    result: [{ providerId: "deepseek", modelId: "flash", supportedReasoningEfforts: ["low", "high", "max"], requestDefaults: { reasoning_effort: "high" } }] }
  expect(modelChoices(response)[0]).toMatchObject({ providerId: "deepseek", modelId: "flash", efforts: ["low", "high", "max"], defaultEffort: "high" })
  expect(() => modelChoices({ result: response.result })).toThrow()
})
