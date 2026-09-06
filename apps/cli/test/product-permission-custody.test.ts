import { expect, test } from "bun:test"
import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import type { CliApi, PermissionBinding } from "../src/api.ts"
import type { TaskProjection } from "@zyra/typed-api-client"
import { CliPermissionSession } from "../src/control/permission.ts"
import { ProductPermissionCustodyStore } from "../src/product/session/permission-custody.ts"

test("a restarted CLI restores custody for the same origin and conversation only", async () => {
  const directory = await mkdtemp(join(tmpdir(), "zyra-cli-custody-"))
  try {
    let presented: string | undefined
    const api = {
      async openPermissionSession(binding: PermissionBinding, input: {custodyToken?: string}) {
        presented = input.custodyToken
        return { ...binding, custodyToken: "test-only-bearer", created: true, verified: true }
      },
    } as unknown as CliApi
    const task = { taskId: "first", runId: "run-first", sessionId: "conversation", metadata: {} } as TaskProjection
    const store = new ProductPermissionCustodyStore("http://localhost:18846", directory)
    const first = new CliPermissionSession({ api, task, custodyStore: store })
    expect(await first.open()).toBeTrue()
    expect(presented).toBeUndefined()
    const restarted = new CliPermissionSession({ api, task: {...task, taskId: "second", runId: "run-second"}, custodyStore: new ProductPermissionCustodyStore("http://localhost:18846", directory) })
    expect(await restarted.open()).toBeTrue()
    expect(presented).toBe("test-only-bearer")
    expect(await store.load("another-conversation")).toBeUndefined()
    expect(await new ProductPermissionCustodyStore("http://localhost:18848", directory).load("conversation")).toBeUndefined()
  } finally { await rm(directory, {recursive: true, force: true}) }
})
