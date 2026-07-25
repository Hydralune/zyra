import type {
  PermissionProductMode,
  PermissionRequestProjection,
  PermissionResponseEffect,
} from "../contracts.ts"

export function PermissionDetail(input: {
  request?: PermissionRequestProjection
  mode: PermissionProductMode
  responding: boolean
  feedback: string
  onFeedback(value: string): void
  onRespond(effect: PermissionResponseEffect): void
  onCrossView(selector: string): void
}): React.JSX.Element {
  const request = input.request
  if (!request) {
    return (
      <section
        className="permission-detail"
        aria-label="Permission request detail"
      >
        <p className="muted-copy">
          Select a canonical request to inspect its exact binding and safe
          argument preview.
        </p>
      </section>
    )
  }
  const disabled =
    input.mode === "sealed"
    || input.responding
    || !request.selectable
  return (
    <section
      className="permission-detail"
      aria-labelledby="permission-detail-heading"
      data-permission-request-id={request.requestId}
      data-permission-detail
    >
      <div className="section-heading">
        <div>
          <h4 id="permission-detail-heading">{request.toolName}</h4>
          <p>{request.prompt}</p>
        </div>
        <span className={`tag permission-risk-${request.riskLevel}`}>
          {request.riskLevel} risk
        </span>
      </div>

      <dl className="permission-binding-grid">
        <Fact label="Request" value={request.requestId} />
        <Fact label="Tool call" value={request.toolCallId} />
        <Fact label="Worker request" value={request.workerRequestId} />
        <Fact label="Session" value={`${request.sessionId} · rev ${request.sessionRevision}`} />
        <Fact label="Namespace" value={request.namespace} />
        <Fact label="Operation" value={request.operation} />
        {request.serverId ? <Fact label="MCP server" value={request.serverId} /> : null}
        {request.commandName ? <Fact label="Command" value={request.commandName} /> : null}
        {request.resourceUri ? <Fact label="Resource" value={request.resourceUri} /> : null}
        {request.workspaceRoot ? <Fact label="Workspace" value={request.workspaceRoot} /> : null}
        <Fact label="Arguments digest" value={request.argumentsDigest} mono />
        <Fact label="Request fingerprint" value={request.requestFingerprint} mono />
        <Fact
          label="Response challenge"
          value={request.responseChallenge.challengeDigest}
          mono
        />
        <Fact
          label="Policy / mode"
          value={`${request.policyRevision} / ${request.modeRevision}`}
        />
      </dl>

      <section aria-labelledby="permission-preview-heading">
        <div className="section-heading">
          <h5 id="permission-preview-heading">Safe argument preview</h5>
          <span>
            {request.redactedPreview.secretCount} secret field(s) redacted
          </span>
        </div>
        <p className="permission-preview-summary">
          {request.redactedPreview.summary}
        </p>
        {request.redactedPreview.rows.length ? (
          <dl className="permission-preview-rows">
            {request.redactedPreview.rows.map((row) => (
              <div
                key={`${row.key}:${row.kind}`}
                data-permission-preview-kind={row.kind}
                data-sensitive={row.sensitive ? "true" : "false"}
              >
                <dt>{row.label}</dt>
                <dd className={row.kind === "command" ? "mono" : undefined}>
                  {row.value}
                </dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="muted-copy">
            Raw arguments are intentionally unavailable. The digest remains
            bound to the exact physical call.
          </p>
        )}
      </section>

      {request.warnings.length ? (
        <section
          className="permission-warning-stack"
          aria-label="Permission risk warnings"
        >
          {request.warnings.map((warning) => (
            <article
              key={warning.code}
              className={`permission-warning permission-warning-${warning.severity}`}
              data-permission-warning={warning.code}
            >
              <strong>{warning.title}</strong>
              <p>{warning.detail}</p>
              {warning.evidence.length ? (
                <ul>
                  {warning.evidence.map((evidence) => (
                    <li key={evidence}>{evidence}</li>
                  ))}
                </ul>
              ) : null}
            </article>
          ))}
        </section>
      ) : null}

      <nav
        className="permission-cross-view"
        aria-label="Related operator views"
      >
        {crossViewTargets(request).map((target) => (
          <button
            key={target.selector}
            type="button"
            className="button button-secondary"
            onClick={() => input.onCrossView(target.selector)}
          >
            {target.label}
          </button>
        ))}
      </nav>

      <div className="permission-response-controls">
        {input.mode === "sealed" ? (
          <p className="permission-sealed-notice" role="status">
            Sealed autonomous mode disables human approval. ASK becomes a
            deterministic deny/replan with no approval wait.
          </p>
        ) : null}
        <label>
          Optional audit note
          <textarea
            value={input.feedback}
            maxLength={4_096}
            disabled={disabled}
            onChange={(event) => input.onFeedback(event.currentTarget.value)}
          />
        </label>
        <div className="permission-response-buttons">
          <button
            type="button"
            className="button button-danger"
            disabled={disabled}
            title={disabledReason(input.mode, input.responding, request)}
            onClick={() => input.onRespond("deny")}
          >
            Deny exact call
          </button>
          <button
            type="button"
            className="button button-primary"
            disabled={disabled}
            title={disabledReason(input.mode, input.responding, request)}
            onClick={() => input.onRespond("allow")}
          >
            Allow once
          </button>
        </div>
        <p className="muted-copy">
          “Allow once” issues at most one exact-call permit. This console does
          not create persistent grants.
        </p>
      </div>
    </section>
  )
}

function Fact(input: {
  label: string
  value: string
  mono?: boolean
}): React.JSX.Element {
  return (
    <div>
      <dt>{input.label}</dt>
      <dd className={input.mono ? "mono" : undefined}>{input.value || "—"}</dd>
    </div>
  )
}

function crossViewTargets(
  request: PermissionRequestProjection,
): readonly { label: string; selector: string }[] {
  const targets = [
    {
      label: "Timeline",
      selector:
        `[data-event-request-id="${escapeAttribute(request.requestId)}"],`
        + `[data-permission-id="${escapeAttribute(request.requestId)}"]`,
    },
  ]
  if (request.sourceSurface === "browser") {
    targets.push({
      label: "Browser",
      selector:
        `[data-browser-permission-request-id="${escapeAttribute(request.requestId)}"]`,
    })
  }
  if (request.sourceSurface === "terminal") {
    targets.push({
      label: "Terminal",
      selector:
        `[data-terminal-permission-request-id="${escapeAttribute(request.requestId)}"]`,
    })
  }
  if (request.sourceSurface === "command") {
    targets.push({
      label: "Command queue",
      selector:
        `[data-command-permission-id="${escapeAttribute(request.requestId)}"]`,
    })
  }
  return targets
}

function disabledReason(
  mode: PermissionProductMode,
  responding: boolean,
  request: PermissionRequestProjection,
): string {
  if (mode === "sealed") {
    return "Sealed autonomous mode rejects human permission decisions."
  }
  if (responding) return "An exact response is already in flight."
  if (request.expired) return "This permission request expired."
  if (request.stale) return "Policy or mode revision changed."
  if (!request.selectable) return "The request is not delivered and selectable."
  return ""
}

function escapeAttribute(value: string): string {
  return value.replace(/["\\\n\r\f]/g, (character) => {
    return `\\${character.charCodeAt(0).toString(16)} `
  })
}
