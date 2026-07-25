export * from "./client.ts"
export * from "./task-api.ts"
export * from "./lifecycle.ts"
export * from "./event-transport.ts"
export * from "./permission-api.ts"
export * from "./scenario-api.ts"

import { ZyraApiClient, type ZyraClientOptions } from "./client.ts"
import { TaskApi } from "./task-api.ts"
import { TaskLifecycleCoordinator } from "./lifecycle.ts"
import { TaskEventTransport } from "./event-transport.ts"
import { PermissionApi } from "./permission-api.ts"
import { ScenarioApi } from "./scenario-api.ts"

export function createZyraApi(options: ZyraClientOptions = {}): {
  client: ZyraApiClient
  tasks: TaskApi
  lifecycle: TaskLifecycleCoordinator
  events: TaskEventTransport
  permissions: PermissionApi
  scenarios: ScenarioApi
  close(reason?: unknown): void
} {
  const client = new ZyraApiClient(options)
  const tasks = new TaskApi(client)
  const lifecycle = new TaskLifecycleCoordinator(tasks)
  const events = new TaskEventTransport(tasks)
  const permissions = new PermissionApi(client)
  const scenarios = new ScenarioApi(client)
  return {
    client,
    tasks,
    lifecycle,
    events,
    permissions,
    scenarios,
    close(reason?: unknown) {
      lifecycle.close(reason)
      events.stopAll(reason)
      permissions.close()
      client.close(reason)
    },
  }
}
