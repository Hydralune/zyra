import { describe, expect, test } from "bun:test"
import { CONTRACT_NAMES, OPERATION_NAMES } from "./constants.ts"
import { coreProtocolCatalog } from "./protocol.ts"

describe("provider model product contract", () => {
  test("exposes the read-only available model catalog through the typed client", () => {
    const endpoint = coreProtocolCatalog().resolve(OPERATION_NAMES.providerModels, {
      query: { available_only: true, provider_id: "deepseek" },
    })
    expect(endpoint).toMatchObject({
      method: "GET",
      path: "/providers/models",
      contract: CONTRACT_NAMES.providerBackend,
      query: { available_only: true, provider_id: "deepseek" },
    })
  })
})
