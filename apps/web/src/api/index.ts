export * from "./client.ts"
export * from "./task-api.ts"
export * from "./lifecycle.ts"
export * from "./event-transport.ts"

import { ZyraApiClient, type ZyraClientOptions } from "./client.ts"
import { TaskApi } from "./task-api.ts"
import { TaskLifecycleCoordinator } from "./lifecycle.ts"
import { TaskEventTransport } from "./event-transport.ts"

export function createZyraApi(options: ZyraClientOptions = {}): {
  client: ZyraApiClient
  tasks: TaskApi
  lifecycle: TaskLifecycleCoordinator
  events: TaskEventTransport
  close(reason?: unknown): void
} {
  const client = new ZyraApiClient(options)
  const tasks = new TaskApi(client)
  const lifecycle = new TaskLifecycleCoordinator(tasks)
  const events = new TaskEventTransport(tasks)
  return {
    client,
    tasks,
    lifecycle,
    events,
    close(reason?: unknown) {
      lifecycle.close(reason)
      events.stopAll(reason)
      client.close(reason)
    },
  }
}
