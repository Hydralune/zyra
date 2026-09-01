import { expect, test } from "bun:test"
import { mkdtemp, readFile, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import {
  PRODUCT_ONBOARDING_SCHEMA,
  ProductOnboardingStore,
} from "../src/product/onboarding/state.ts"

test("product onboarding state is atomic, bounded, and contains no workspace identity", async () => {
  const directory = await mkdtemp(join(tmpdir(), "zyra-onboarding-"))
  const store = new ProductOnboardingStore(directory)
  expect(await store.load()).toEqual({ status: "pending" })
  const completed = await store.complete("explicit-model", new Date("2026-09-01T00:00:00.000Z"))
  expect(completed).toEqual({
    schema: PRODUCT_ONBOARDING_SCHEMA,
    completed_at: "2026-09-01T00:00:00.000Z",
    choice: "explicit-model",
  })
  expect(await store.load()).toEqual({ status: "complete", state: completed })
  const persisted = await readFile(join(directory, "product-onboarding.json"), "utf8")
  expect(persisted).not.toContain("agent-zoo")
  expect(persisted).not.toContain("workspace")
})

test("product onboarding state fails safe on damaged or unknown data", async () => {
  const directory = await mkdtemp(join(tmpdir(), "zyra-onboarding-invalid-"))
  const store = new ProductOnboardingStore(directory)
  await writeFile(join(directory, "product-onboarding.json"), "{broken", "utf8")
  expect(await store.load()).toMatchObject({ status: "invalid", reason: expect.stringContaining("损坏") })
  await writeFile(join(directory, "product-onboarding.json"), JSON.stringify({ schema: "future/v9" }), "utf8")
  expect(await store.load()).toMatchObject({ status: "invalid", reason: expect.stringContaining("未知 schema") })
})
