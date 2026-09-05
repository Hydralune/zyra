import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type FormEvent,
} from "react"
import type {
  McpCatalogKind,
  McpCatalogPage,
  McpElicitationProjection,
  McpServerProjection,
} from "../contracts.ts"
import type { McpConsoleController } from "../controller.ts"
import { authUrgency } from "../auth.ts"

export interface McpWorkbenchProps {
  controller: McpConsoleController
  taskId?: string
  runId?: string
  sessionId?: string
}

export function McpWorkbench({
  controller,
  taskId,
  runId,
  sessionId,
}: McpWorkbenchProps) {
  const snapshot = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
    controller.getSnapshot,
  )
  const [kind, setKind] = useState<McpCatalogKind>("tool")
  const [query, setQuery] = useState("")
  const [page, setPage] = useState<McpCatalogPage>()
  const [busy, setBusy] = useState<string>()
  const [notice, setNotice] = useState<string>()

  useEffect(() => {
    controller.bind(taskId, runId, sessionId)
  }, [controller, taskId, runId, sessionId])

  useEffect(() => {
    if (!snapshot.selectedServerId || !snapshot.projection) {
      setPage(undefined)
      return
    }
    try {
      setPage(controller.pageCatalog({
        serverId: snapshot.selectedServerId,
        kind,
        query,
        limit: 50,
      }))
    } catch (error) {
      setPage(undefined)
      setNotice(error instanceof Error ? error.message : String(error))
    }
  }, [
    controller,
    kind,
    query,
    snapshot.projection?.fingerprint,
    snapshot.selectedServerId,
  ])

  const selected = snapshot.projection?.selected
  const run = useCallback(
    async (name: string, action: () => Promise<unknown>) => {
      if (busy) return
      setBusy(name)
      setNotice(undefined)
      try {
        await action()
        setNotice(`${name} handed to the canonical MCP owner.`)
      } catch (error) {
        setNotice(error instanceof Error ? error.message : String(error))
      } finally {
        setBusy(undefined)
      }
    },
    [busy],
  )

  if (!taskId) {
    return (
      <section className="panel mcp-workbench" aria-label="MCP workbench">
        <header className="panel-header">
          <div>
            <span className="eyebrow">Runtime extensions</span>
            <h2>MCP</h2>
          </div>
        </header>
        <div className="empty-state">Select a task to inspect canonical MCP state.</div>
      </section>
    )
  }

  return (
    <section
      className="panel mcp-workbench"
      aria-label="MCP workbench"
      data-enabled={snapshot.enabled}
      data-connected={snapshot.connected}
      data-sealed={snapshot.sealed}
    >
      <header className="panel-header">
        <div>
          <span className="eyebrow">Runtime extensions</span>
          <h2>MCP 服务</h2>
          <p className="muted">
            Canonical projection revision{" "}
            {snapshot.projection?.authority.projectionRevision ?? "—"}
          </p>
        </div>
        <div className="status-cluster" aria-label="MCP status summary">
          <Status label="transport" value={snapshot.connected ? "connected" : "offline"} />
          <Status label="mode" value={snapshot.sealed ? "sealed" : "interactive"} />
          <Status
            label="servers"
            value={String(snapshot.projection?.servers.length ?? 0)}
          />
          <Status
            label="needs auth"
            value={String(snapshot.projection?.needsAuth ?? 0)}
          />
        </div>
      </header>

      {notice || snapshot.error ? (
        <div className="inline-notice" role="status">
          {notice ?? snapshot.error}
        </div>
      ) : null}

      {!snapshot.enabled ? (
        <div className="warning-banner" role="alert">
          {snapshot.disabledReason ?? "MCP feature binding is disabled."}
        </div>
      ) : null}

      {snapshot.projection?.warnings.length ? (
        <ul className="warning-list" aria-label="MCP projection warnings">
          {snapshot.projection.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      ) : null}

      <div className="workbench-grid">
        <aside className="panel-subsection mcp-server-list" aria-label="MCP servers">
          <div className="subsection-header">
            <h3>Servers</h3>
            <span>{snapshot.projection?.connectedServers ?? 0} connected</span>
          </div>
          <div className="stack-list">
            {snapshot.projection?.servers.map((server) => (
              <button
                type="button"
                key={server.id}
                className={
                  server.id === snapshot.selectedServerId
                    ? "stack-row selected"
                    : "stack-row"
                }
                onClick={() => controller.selectServer(server.id)}
                aria-pressed={server.id === snapshot.selectedServerId}
              >
                <span>
                  <strong>{server.title}</strong>
                  <small>{server.config.source} · {server.config.transport}</small>
                </span>
                <span className={`status-pill status-${server.state}`}>
                  {server.state}
                </span>
              </button>
            ))}
            {!snapshot.projection?.servers.length ? (
              <div className="empty-state">
                No authoritative MCP server facts were admitted.
              </div>
            ) : null}
          </div>
        </aside>

        <main className="panel-subsection mcp-server-detail">
          {selected ? (
            <>
              <ServerHeader
                server={selected}
                disabled={
                  Boolean(busy) ||
                  !snapshot.enabled ||
                  !snapshot.connected ||
                  snapshot.sealed
                }
                busy={busy}
                onEnable={() => run("Enable", () => controller.enableServer(selected.id))}
                onDisable={() => run("Disable", () => controller.disableServer(selected.id))}
                onReconnect={() => run("Reconnect", () => controller.reconnectServer(selected.id))}
                onRefresh={() => run("Refresh", () => controller.refreshCatalog(selected.id))}
                onAuth={() => run("Auth refresh", () => controller.refreshAuth(selected.id))}
              />
              <ServerFacts server={selected} />
              <Catalog
                kind={kind}
                query={query}
                page={page}
                onKind={setKind}
                onQuery={setQuery}
                onNext={() => {
                  if (!page?.nextCursor) return
                  try {
                    setPage(controller.pageCatalog({
                      serverId: selected.id,
                      kind,
                      query,
                      cursor: page.nextCursor,
                      limit: page.limit,
                    }))
                  } catch (error) {
                    setNotice(error instanceof Error ? error.message : String(error))
                  }
                }}
              />
              <Elicitations
                controller={controller}
                server={selected}
                disabled={
                  Boolean(busy) ||
                  !snapshot.enabled ||
                  !snapshot.connected ||
                  snapshot.sealed
                }
                submit={(requestId) =>
                  run("Elicitation", () => controller.submitElicitation(requestId))}
              />
            </>
          ) : (
            <div className="empty-state">Select an admitted MCP server.</div>
          )}
        </main>
      </div>

      <OperationLedger controller={controller} />
    </section>
  )
}

function ServerHeader({
  server,
  disabled,
  busy,
  onEnable,
  onDisable,
  onReconnect,
  onRefresh,
  onAuth,
}: {
  server: McpServerProjection
  disabled: boolean
  busy?: string
  onEnable(): void
  onDisable(): void
  onReconnect(): void
  onRefresh(): void
  onAuth(): void
}) {
  const urgency = authUrgency(server.auth)
  return (
    <header className="subsection-header mcp-detail-header">
      <div>
        <h3>{server.title}</h3>
        <p className="muted">{server.id} · owner {server.ownerId}</p>
      </div>
      <div className="button-row" aria-label="MCP server controls">
        {server.enabled ? (
          <button type="button" disabled={disabled} onClick={onDisable}>
            {busy === "Disable" ? "Disabling…" : "Disable"}
          </button>
        ) : (
          <button type="button" disabled={disabled} onClick={onEnable}>
            {busy === "Enable" ? "Enabling…" : "Enable"}
          </button>
        )}
        <button
          type="button"
          disabled={disabled || !server.reconnectEligible}
          onClick={onReconnect}
        >
          {busy === "Reconnect" ? "Reconnecting…" : "Reconnect"}
        </button>
        <button type="button" disabled={disabled || !server.enabled} onClick={onRefresh}>
          {busy === "Refresh" ? "Refreshing…" : "Refresh"}
        </button>
        <button
          type="button"
          disabled={disabled || !server.auth.refreshEligible}
          onClick={onAuth}
          title={urgency.reason}
        >
          {busy === "Auth refresh" ? "Refreshing auth…" : "Refresh auth"}
        </button>
      </div>
    </header>
  )
}

function ServerFacts({ server }: { server: McpServerProjection }) {
  const urgency = authUrgency(server.auth)
  const presence = Object.entries(server.config.secretFieldPresence)
    .filter(([, present]) => present)
    .map(([path]) => path)
  return (
    <div className="fact-grid">
      <Fact label="Connection" value={server.state} />
      <Fact label="Epoch" value={String(server.connectionEpoch)} />
      <Fact label="Config source" value={server.config.source} />
      <Fact label="Config revision" value={String(server.config.configRevision)} />
      <Fact label="Transport" value={server.config.transport} />
      <Fact label="Endpoint class" value={server.config.endpointClass ?? "unknown"} />
      <Fact label="Authentication" value={server.auth.state} tone={urgency.level} />
      <Fact label="Auth provider" value={server.auth.provider ?? "—"} />
      <Fact label="Expires" value={server.auth.expiresAt ?? "not projected"} />
      <Fact label="Credential fields" value={presence.length ? `${presence.length} present` : "none"} />
      <Fact label="Capability revision" value={String(server.capabilities.revision)} />
      <Fact
        label="Catalog"
        value={`${server.tools.length} tools · ${server.resources.length} resources · ${server.prompts.length} prompts`}
      />
      {server.disabledReason ? (
        <Fact label="Disabled reason" value={server.disabledReason} tone="warning" />
      ) : null}
      {server.lastErrorMessage ? (
        <Fact label="Last error" value={server.lastErrorMessage} tone="critical" />
      ) : null}
    </div>
  )
}

function Catalog({
  kind,
  query,
  page,
  onKind,
  onQuery,
  onNext,
}: {
  kind: McpCatalogKind
  query: string
  page?: McpCatalogPage
  onKind(kind: McpCatalogKind): void
  onQuery(query: string): void
  onNext(): void
}) {
  return (
    <section className="mcp-catalog" aria-label="MCP capability catalog">
      <div className="subsection-header">
        <div className="tab-row" role="tablist" aria-label="MCP catalogs">
          {(["tool", "resource", "prompt"] as const).map((candidate) => (
            <button
              type="button"
              role="tab"
              aria-selected={kind === candidate}
              key={candidate}
              onClick={() => onKind(candidate)}
            >
              {candidate}s
            </button>
          ))}
        </div>
        <input
          type="search"
          value={query}
          onChange={(event) => onQuery(event.target.value)}
          placeholder={`Filter ${kind}s`}
          aria-label={`Filter MCP ${kind}s`}
        />
      </div>
      <div className="stack-list">
        {page?.items.map((item) => (
          <article className="stack-row" key={item.id}>
            <span>
              <strong>{item.title}</strong>
              <small>{item.uri ?? item.name}</small>
              {item.description ? <p>{item.description}</p> : null}
            </span>
            <span className="muted">
              r{item.revision}
              {item.deprecated ? " · deprecated" : ""}
              {item.sensitive ? " · sensitive" : ""}
            </span>
          </article>
        ))}
        {!page?.items.length ? (
          <div className="empty-state">No {kind}s match this bounded page.</div>
        ) : null}
      </div>
      {page ? (
        <footer className="pagination-footer">
          <span>
            {page.offset + 1}-{page.offset + page.items.length} of {page.total}
          </span>
          <button type="button" disabled={!page.hasNext} onClick={onNext}>
            Next page
          </button>
        </footer>
      ) : null}
    </section>
  )
}

function Elicitations({
  controller,
  server,
  disabled,
  submit,
}: {
  controller: McpConsoleController
  server: McpServerProjection
  disabled: boolean
  submit(requestId: string): void
}) {
  const pending = server.elicitations.filter((request) =>
    ["pending", "permission-pending"].includes(request.state))
  if (!pending.length) return null
  return (
    <section className="mcp-elicitations" aria-label="MCP elicitations">
      <div className="subsection-header">
        <h3>Elicitations</h3>
        <span>{pending.length} pending</span>
      </div>
      {pending.map((request) => (
        <ElicitationForm
          key={request.id}
          controller={controller}
          request={request}
          disabled={disabled}
          submit={() => submit(request.id)}
        />
      ))}
    </section>
  )
}

function ElicitationForm({
  controller,
  request,
  disabled,
  submit,
}: {
  controller: McpConsoleController
  request: McpElicitationProjection
  disabled: boolean
  submit(): void
}) {
  const sensitive = useMemo(() => new Set(request.sensitiveFields), [request.sensitiveFields])
  const [display, setDisplay] = useState<Readonly<Record<string, unknown>>>({})
  const onSubmit = (event: FormEvent) => {
    event.preventDefault()
    submit()
  }
  return (
    <form className="elicitation-card" onSubmit={onSubmit}>
      <h4>{request.title}</h4>
      <p>{request.message}</p>
      <small>
        Request {request.id} · schema {request.schemaDigest}
      </small>
      {request.fieldNames.map((field) => (
        <label key={field}>
          <span>
            {field}
            {request.requiredFields.includes(field) ? " *" : ""}
          </span>
          <input
            type={sensitive.has(field) ? "password" : "text"}
            autoComplete="off"
            value={typeof display[field] === "string" && !sensitive.has(field) ? String(display[field]) : ""}
            placeholder={sensitive.has(field) && display[field] === "[present]" ? "value present" : ""}
            disabled={disabled}
            onChange={(event) =>
              setDisplay(controller.updateElicitationDraft(
                request.id,
                field,
                event.target.value,
              ))}
          />
        </label>
      ))}
      <button type="submit" disabled={disabled}>
        Submit through permission
      </button>
    </form>
  )
}

function OperationLedger({ controller }: { controller: McpConsoleController }) {
  const snapshot = controller.getSnapshot()
  if (!snapshot.operations.length && !snapshot.reconnectPlans.length) return null
  return (
    <section className="panel-subsection mcp-operation-ledger" aria-label="MCP operations">
      <div className="subsection-header">
        <h3>Owner settlement</h3>
        <span>{snapshot.operations.length} controls</span>
      </div>
      <div className="stack-list">
        {snapshot.operations.slice(0, 20).map((operation) => (
          <article key={operation.id} className="stack-row">
            <span>
              <strong>{operation.action} · {operation.serverId}</strong>
              <small>{operation.phase} · {operation.updatedAt}</small>
            </span>
            <span>
              {operation.verification?.message ?? operation.error ?? "Awaiting owner receipt"}
            </span>
          </article>
        ))}
        {snapshot.reconnectPlans.map((plan) => (
          <article key={`${plan.serverId}:${plan.attempt}`} className="stack-row">
            <span>
              <strong>Reconnect {plan.serverId}</strong>
              <small>attempt {plan.attempt}/{plan.maximumAttempts}</small>
            </span>
            <span>{plan.state} · {Math.max(0, plan.delayMs)} ms</span>
          </article>
        ))}
      </div>
    </section>
  )
}

function Fact({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone?: string
}) {
  return (
    <div className={`fact-card${tone ? ` tone-${tone}` : ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

function Status({ label, value }: { label: string; value: string }) {
  return (
    <span className="status-item">
      <small>{label}</small>
      <strong>{value}</strong>
    </span>
  )
}
