import { useMemo, useState } from "react"
import type {
  PermissionModeProjection,
  PermissionRequestProjection,
  PermissionRuleProjection,
} from "../contracts.ts"
import { buildPermissionPolicyView } from "../policy-diff.ts"

export function PermissionPolicyPanel(input: {
  mode: PermissionModeProjection
  rules: readonly PermissionRuleProjection[]
  request?: PermissionRequestProjection
}): React.JSX.Element {
  const [expanded, setExpanded] = useState(false)
  const view = useMemo(
    () =>
      buildPermissionPolicyView({
        mode: input.mode,
        rules: input.rules,
        request: input.request,
      }),
    [input.mode, input.rules, input.request],
  )
  return (
    <section
      className="permission-policy-panel"
      aria-labelledby="permission-policy-heading"
      data-permission-policy-revision={view.currentPolicyRevision}
      data-permission-mode-revision={view.currentModeRevision}
      data-raw-arguments-exposed="false"
    >
      <div className="section-heading">
        <div>
          <h4 id="permission-policy-heading">Policy status & diff</h4>
          <p>
            {view.mode} · policy {view.currentPolicyRevision} · mode{" "}
            {view.currentModeRevision}
          </p>
        </div>
        <button
          type="button"
          className="button button-secondary"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Hide rules" : `Show ${view.rules.length} rules`}
        </button>
      </div>
      <dl className="permission-policy-diff">
        {view.diff.map((row) => (
          <div
            key={row.diffId}
            data-permission-policy-diff={row.kind}
            data-changed={row.changed ? "true" : "false"}
          >
            <dt>
              {row.label}
              {row.changed ? <span className="tag tag-danger">changed</span> : null}
            </dt>
            <dd>
              <span>{row.requested}</span>
              <span aria-hidden="true">→</span>
              <strong>{row.current}</strong>
            </dd>
            <p>{row.detail}</p>
          </div>
        ))}
      </dl>
      {expanded ? (
        view.rules.length ? (
          <ol className="permission-rule-list">
            {view.rules.map((rule) => (
              <li
                key={rule.ruleId}
                data-permission-rule-id={rule.ruleId}
                data-permission-rule-effect={rule.effect}
                data-permission-rule-active={
                  rule.enabled && !rule.expired ? "true" : "false"
                }
              >
                <div>
                  <strong>{rule.ruleId}</strong>
                  <span
                    className={
                      rule.effect === "deny"
                        ? "tag tag-danger"
                        : "tag"
                    }
                  >
                    {rule.effect}
                  </span>
                  {!rule.enabled ? <span className="tag tag-muted">disabled</span> : null}
                  {rule.expired ? <span className="tag tag-muted">expired</span> : null}
                </div>
                <p>{rule.scopeSummary}</p>
                {rule.reason ? <p>{rule.reason}</p> : null}
                <small>
                  {rule.source} · priority {rule.priority} · revision{" "}
                  {rule.revision}
                </small>
              </li>
            ))}
          </ol>
        ) : (
          <p className="muted-copy">
            No explicit rules are projected; the canonical mode defaults still
            apply.
          </p>
        )
      ) : null}
    </section>
  )
}
