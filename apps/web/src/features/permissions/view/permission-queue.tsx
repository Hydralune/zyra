import type {
  PermissionQueueRow,
  PermissionRequestProjection,
} from "../contracts.ts"

export function PermissionQueue(input: {
  rows: readonly PermissionQueueRow[]
  selectedRequestId?: string
  onSelect(requestId: string): void
}): React.JSX.Element {
  return (
    <section
      className="permission-queue"
      aria-labelledby="permission-queue-heading"
      data-permission-queue
    >
      <div className="section-heading">
        <h4 id="permission-queue-heading">待审批操作</h4>
        <span>{input.rows.length} 项待处理</span>
      </div>
      {input.rows.length ? (
        <ol className="permission-queue-list">
          {input.rows.map((row) => (
            <PermissionQueueItem
              key={row.request.requestId}
              row={row}
              selected={row.request.requestId === input.selectedRequestId}
              onSelect={input.onSelect}
            />
          ))}
        </ol>
      ) : (
        <p className="muted-copy">
          当前没有待审批操作。
        </p>
      )}
    </section>
  )
}

function PermissionQueueItem(input: {
  row: PermissionQueueRow
  selected: boolean
  onSelect(requestId: string): void
}): React.JSX.Element {
  const request = input.row.request
  return (
    <li
      className={`permission-queue-item ${input.selected ? "is-selected" : ""}`}
      data-permission-request-id={request.requestId}
      data-permission-source={request.sourceSurface}
      data-permission-risk={request.riskLevel}
      data-permission-urgency={input.row.urgency}
      tabIndex={-1}
    >
      <button
        type="button"
        className="permission-queue-select"
        aria-current={input.selected ? "true" : undefined}
        onClick={() => input.onSelect(request.requestId)}
      >
        <span className="permission-queue-title">
          <strong>{request.toolName}</strong>
          <span className={`tag permission-risk-${request.riskLevel}`}>
            {request.riskLevel}
          </span>
          <span className="tag tag-muted">{request.sourceSurface}</span>
        </span>
        <span>{request.prompt}</span>
        <PermissionQueueMetadata request={request} row={input.row} />
      </button>
    </li>
  )
}

function PermissionQueueMetadata(input: {
  request: PermissionRequestProjection
  row: PermissionQueueRow
}): React.JSX.Element {
  const remaining =
    input.row.secondsRemaining <= 0
      ? "expired"
      : input.row.secondsRemaining < 60
        ? `${input.row.secondsRemaining}s left`
        : `${Math.ceil(input.row.secondsRemaining / 60)}m left`
  return (
    <span className="permission-queue-meta">
      <span>{remaining}</span>
      <span>rev {input.request.sessionRevision}</span>
      <span>{shortDigest(input.request.argumentsDigest)}</span>
      {input.row.responsePending ? (
        <span className="tag">提交中</span>
      ) : null}
    </span>
  )
}

function shortDigest(value: string): string {
  return value.length > 14
    ? `${value.slice(0, 8)}…${value.slice(-4)}`
    : value
}
