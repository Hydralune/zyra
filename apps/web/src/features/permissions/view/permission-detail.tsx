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
        aria-label="审批详情"
      >
        <p className="muted-copy">
          选择待审批操作，查看执行范围和参数。
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
        <Fact label="请求编号" value={request.requestId} />
        <Fact label="工具调用" value={request.toolCallId} />
        <Fact label="执行请求" value={request.workerRequestId} />
        <Fact label="会话" value={`${request.sessionId} · rev ${request.sessionRevision}`} />
        <Fact label="命名空间" value={request.namespace} />
        <Fact label="操作" value={request.operation} />
        {request.serverId ? <Fact label="MCP 服务" value={request.serverId} /> : null}
        {request.commandName ? <Fact label="命令" value={request.commandName} /> : null}
        {request.resourceUri ? <Fact label="资源" value={request.resourceUri} /> : null}
        {request.workspaceRoot ? <Fact label="工作区" value={request.workspaceRoot} /> : null}
        <Fact label="参数摘要" value={request.argumentsDigest} mono />
        <Fact label="请求指纹" value={request.requestFingerprint} mono />
        <Fact
          label="响应校验"
          value={request.responseChallenge.challengeDigest}
          mono
        />
        <Fact
          label="策略 / 模式"
          value={`${request.policyRevision} / ${request.modeRevision}`}
        />
      </dl>

      <section aria-labelledby="permission-preview-heading">
        <div className="section-heading">
          <h5 id="permission-preview-heading">操作参数预览</h5>
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
            敏感参数已隐藏，摘要与本次操作绑定。
          </p>
        )}
      </section>

      {request.warnings.length ? (
        <section
          className="permission-warning-stack"
          aria-label="权限风险提示"
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
        aria-label="相关操作记录"
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
            全自主模式由后端处理权限，不接受人工审批。
          </p>
        ) : null}
        <label>
          审批备注（可选）
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
            拒绝本次操作
          </button>
          <button
            type="button"
            className="button button-primary"
            disabled={disabled}
            title={disabledReason(input.mode, input.responding, request)}
            onClick={() => input.onRespond("allow")}
          >
            仅允许一次
          </button>
        </div>
        <p className="muted-copy">
          仅允许一次只对当前操作生效，不会授予永久权限。
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
      label: "时间线",
      selector:
        `[data-event-request-id="${escapeAttribute(request.requestId)}"],`
        + `[data-permission-id="${escapeAttribute(request.requestId)}"]`,
    },
  ]
  if (request.sourceSurface === "browser") {
    targets.push({
      label: "浏览器",
      selector:
        `[data-browser-permission-request-id="${escapeAttribute(request.requestId)}"]`,
    })
  }
  if (request.sourceSurface === "terminal") {
    targets.push({
      label: "终端",
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
