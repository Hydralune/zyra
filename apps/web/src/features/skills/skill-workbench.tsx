import {
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react"
import type { JsonObject } from "../../events/ingress/index.ts"
import { SkillWorkbenchController } from "./controller.ts"
import {
  buildSkillWorkbenchViewModel,
  compactHash,
  findingTone,
  pageAnnouncement,
  relativeSkillTime,
} from "./view-model.ts"
import type { SkillCatalogQuery, SkillProjection } from "./contracts.ts"

export function SkillWorkbench({
  controller,
  taskId,
  runId,
  sessionId,
}: {
  controller: SkillWorkbenchController
  taskId: string
  runId: string
  sessionId?: string
}) {
  const snapshot = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
    controller.getSnapshot,
  )
  const [query, setQuery] = useState("")
  const [blockedOnly, setBlockedOnly] = useState(false)
  const [argumentsJson, setArgumentsJson] = useState("{}")
  const [busy, setBusy] = useState("")
  const [error, setError] = useState("")
  const [expandedSection, setExpandedSection] = useState<string>()
  const catalogQuery = useMemo<SkillCatalogQuery>(() => ({
    text: query,
    blockedOnly,
    sort: "name",
    limit: 100,
  }), [blockedOnly, query])
  const page = useMemo(
    () => snapshot.catalog
      ? controller.catalogIndex.page(catalogQuery)
      : undefined,
    [catalogQuery, controller, snapshot.catalog?.fingerprint],
  )
  const view = buildSkillWorkbenchViewModel(snapshot)
  const selected = snapshot.catalog?.skills.find(
    (skill) => skill.skillId === snapshot.selectedSkillId,
  )

  useEffect(() => {
    controller.viewerOpened()
    controller.bind(taskId, runId, sessionId)
    const offline = () => controller.disconnected()
    const online = () => controller.reconnected()
    window.addEventListener("offline", offline)
    window.addEventListener("online", online)
    return () => {
      window.removeEventListener("offline", offline)
      window.removeEventListener("online", online)
      controller.viewerClosed("Skill overlay unmounted.")
    }
  }, [controller, runId, sessionId, taskId])

  const act = async (name: string, action: () => Promise<unknown>) => {
    setBusy(name)
    setError("")
    try {
      await action()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setBusy("")
    }
  }

  const invoke = async () => {
    let value: unknown
    try {
      value = JSON.parse(argumentsJson)
    } catch {
      throw new Error("Invocation arguments must be valid JSON.")
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error("Invocation arguments must be a JSON object.")
    }
    await controller.invokeSkill({
      skillId: selected?.skillId,
      arguments: value as JsonObject,
    })
  }

  return (
    <section
      className="detail-section skill-workbench"
      aria-labelledby="skill-workbench-heading"
      data-skill-owner="M1-SkillTool"
      data-projection-owner="M2-01B-CanonicalProjectionStore"
    >
      <div className="section-heading">
        <div>
          <p className="eyebrow">Canonical SkillTool custody</p>
          <h3 id="skill-workbench-heading">{view.headline}</h3>
        </div>
        <span className={`tag tag-${view.tone}`}>{view.status}</span>
      </div>

      {error ? <p className="plan-warning" role="alert">{error}</p> : null}
      {view.controlReason ? (
        <p className="muted-copy" role="status">Controls unavailable: {view.controlReason}</p>
      ) : null}

      <dl className="fact-grid">
        {view.catalogSummary.map((fact) => (
          <div key={fact.label}>
            <dt>{fact.label}</dt>
            <dd>{fact.value}</dd>
          </div>
        ))}
      </dl>

      {view.operation ? (
        <div
          className="console-operation"
          data-operation-id={snapshot.activeOperation?.operationId}
        >
          <span className={`tag tag-${view.operation.tone}`}>
            {view.operation.phase}
          </span>
          <strong>{view.operation.label}</strong>
          <span>{view.operation.summary}</span>
          <small>{view.operation.evidence}</small>
        </div>
      ) : null}

      <div className="settings-grid">
        <article aria-labelledby="skill-catalog-heading">
          <div className="section-heading">
            <h4 id="skill-catalog-heading">Canonical skills</h4>
            <span>{page?.total ?? 0}</span>
          </div>
          <div className="filter-row">
            <input
              value={query}
              placeholder="Search skill, tool, source, dependency…"
              onChange={(event) => setQuery(event.currentTarget.value)}
            />
            <label>
              <input
                type="checkbox"
                checked={blockedOnly}
                onChange={(event) => setBlockedOnly(event.currentTarget.checked)}
              />
              Blocked only
            </label>
          </div>
          <p className="muted-copy" aria-live="polite">
            {page ? pageAnnouncement(page) : "Awaiting canonical skill projection."}
          </p>
          <ol className="compact-list">
            {(page?.rows ?? []).map((skill) => (
              <li
                key={skill.skillId}
                data-skill-id={skill.skillId}
                data-selected={skill.skillId === selected?.skillId}
                data-skill-ready={skill.ready}
              >
                <button
                  type="button"
                  onClick={() => {
                    try {
                      controller.selectSkill(skill.skillId)
                      setError("")
                    } catch (cause) {
                      setError(cause instanceof Error ? cause.message : String(cause))
                    }
                  }}
                >
                  <strong>{skill.displayName}</strong>
                  <span className={`tag tag-${skill.ready ? "success" : "danger"}`}>
                    {skill.ready ? "ready" : "blocked"}
                  </span>
                  <span className="tag tag-muted">{skill.approval.stage}</span>
                  <small>
                    {skill.version.version} · {skill.provenance.sourceKind} ·
                    risk {skill.supplyChain.riskScore}
                  </small>
                </button>
              </li>
            ))}
          </ol>
        </article>

        <article aria-labelledby="skill-detail-heading">
          <div className="section-heading">
            <h4 id="skill-detail-heading">Skill detail</h4>
            <span>{selected?.availability ?? "none"}</span>
          </div>
          {selected && view.selected ? (
            <>
              <h5>{view.selected.title}</h5>
              <p>{view.selected.subtitle}</p>
              <dl className="fact-grid">
                <div><dt>Identity</dt><dd>{view.selected.identity}</dd></div>
                <div><dt>Version</dt><dd>{view.selected.version}</dd></div>
                <div><dt>Content hash</dt><dd title={selected.version.contentHash}>{view.selected.hash}</dd></div>
                <div><dt>Descriptor</dt><dd title={selected.version.descriptorDigest}>{compactHash(selected.version.descriptorDigest)}</dd></div>
                <div><dt>Updated</dt><dd>{relativeSkillTime(selected.lastUpdatedAt)}</dd></div>
                <div><dt>Owner</dt><dd>{selected.canonicalOwner}</dd></div>
              </dl>
              <div className="console-operation">
                <span className={`tag tag-${view.selected.provenanceTone}`}>provenance</span>
                <strong>{view.selected.provenance}</strong>
              </div>
              <div className="console-operation">
                <span className={`tag tag-${view.selected.approvalTone}`}>approval</span>
                <strong>{view.selected.approval}</strong>
              </div>
              <div className="console-operation">
                <span className={`tag tag-${view.selected.riskTone}`}>supply chain</span>
                <strong>{view.selected.risk}</strong>
              </div>
            </>
          ) : (
            <p className="muted-copy">Select a canonical skill.</p>
          )}
        </article>
      </div>

      {selected ? (
        <>
          <div className="settings-grid">
            <article aria-labelledby="skill-body-heading">
              <div className="section-heading">
                <h4 id="skill-body-heading">Body & resources</h4>
                <span>{selected.body.sections.length} sections</span>
              </div>
              <p>{selected.body.summary}</p>
              <ol className="compact-list">
                {selected.body.sections.map((section) => (
                  <li key={section.id}>
                    <button
                      type="button"
                      aria-expanded={expandedSection === section.id}
                      onClick={() =>
                        setExpandedSection(
                          expandedSection === section.id ? undefined : section.id,
                        )}
                    >
                      <strong>{section.heading}</strong>
                      <span>lines {section.lineStart}-{section.lineEnd}</span>
                      <small>
                        {section.codeBlocks} code blocks · {section.resourceRefs.length} resources
                      </small>
                    </button>
                    {expandedSection === section.id ? (
                      <pre className="code-block">{section.body}</pre>
                    ) : null}
                  </li>
                ))}
              </ol>
              <ol className="compact-list">
                {selected.resources.map((resource) => (
                  <li key={resource.resourceId}>
                    <div>
                      <strong>{resource.path}</strong>
                      <span className={`tag tag-${resource.available ? "success" : "danger"}`}>
                        {resource.available ? "available" : "missing"}
                      </span>
                      {resource.private ? <span className="tag tag-warning">private</span> : null}
                    </div>
                    <small>
                      {resource.kind} · {resource.required ? "required" : "optional"} ·
                      {resource.digest ? compactHash(resource.digest) : "no digest"}
                    </small>
                    {resource.warnings.map((warning) => (
                      <p className="danger-copy" key={warning}>{warning}</p>
                    ))}
                  </li>
                ))}
              </ol>
            </article>

            <article aria-labelledby="skill-tools-heading">
              <div className="section-heading">
                <h4 id="skill-tools-heading">Allowed tools & dependencies</h4>
                <span>{selected.toolScope.allowed.length} tools</span>
              </div>
              <dl className="fact-grid">
                <div><dt>Allowed</dt><dd>{selected.toolScope.allowed.length}</dd></div>
                <div><dt>Denied</dt><dd>{selected.toolScope.denied.length}</dd></div>
                <div><dt>Approval</dt><dd>{selected.toolScope.requireApproval.length}</dd></div>
                <div><dt>Max calls</dt><dd>{selected.toolScope.maximumCalls ?? "owner default"}</dd></div>
                <div><dt>Parallel</dt><dd>{selected.toolScope.maximumParallel}</dd></div>
                <div><dt>Read-only</dt><dd>{selected.toolScope.readOnly ? "yes" : "no"}</dd></div>
              </dl>
              <div className="tag-list">
                {selected.toolScope.allowed.map((tool) => (
                  <span className="tag tag-muted" key={tool}>{tool}</span>
                ))}
              </div>
              <ol className="compact-list">
                {selected.dependencies.nodes.map((dependency) => (
                  <li key={dependency.dependencyId}>
                    <div>
                      <strong>{dependency.name}</strong>
                      <span className="tag tag-muted">{dependency.kind}</span>
                      <span className={`tag tag-${dependency.available ? "success" : "danger"}`}>
                        {dependency.available ? "resolved" : "missing"}
                      </span>
                      <span className={`tag tag-${dependency.approved ? "success" : "warning"}`}>
                        {dependency.approved ? "approved" : "unreviewed"}
                      </span>
                    </div>
                    <small>
                      {dependency.versionRange ?? "*"} → {dependency.resolvedVersion ?? "unresolved"}
                    </small>
                    {dependency.warnings.map((warning) => (
                      <p className="danger-copy" key={warning}>{warning}</p>
                    ))}
                  </li>
                ))}
              </ol>
              {selected.dependencies.cycles.map((cycle) => (
                <p className="plan-warning" key={cycle.join(":")}>
                  Cycle: {cycle.join(" → ")}
                </p>
              ))}
            </article>
          </div>

          <div className="settings-grid">
            <article aria-labelledby="skill-supply-heading">
              <div className="section-heading">
                <h4 id="skill-supply-heading">Supply-chain audit</h4>
                <span>{selected.supplyChain.browserDisposition}</span>
              </div>
              <dl className="fact-grid">
                <div><dt>Risk</dt><dd>{selected.supplyChain.riskScore}/100</dd></div>
                <div><dt>Owner allowed</dt><dd>{String(selected.supplyChain.allowedByOwner ?? "unknown")}</dd></div>
                <div><dt>Receipt</dt><dd>{selected.supplyChain.receiptId ?? "absent"}</dd></div>
                <div><dt>Policy</dt><dd>{selected.supplyChain.policyRevision ?? "unknown"}</dd></div>
              </dl>
              <ol className="compact-list">
                {selected.supplyChain.findings.map((finding) => (
                  <li key={finding.findingId}>
                    <div>
                      <strong>{finding.code}</strong>
                      <span className={`tag tag-${findingTone(finding)}`}>
                        {finding.severity}
                      </span>
                      {finding.canonical ? <span className="tag tag-muted">owner</span> : null}
                    </div>
                    <p>{finding.message}</p>
                    <small>{finding.subject ?? finding.source}</small>
                  </li>
                ))}
              </ol>
            </article>

            <article aria-labelledby="skill-invocations-heading">
              <div className="section-heading">
                <h4 id="skill-invocations-heading">Invocations & settlement</h4>
                <span>{selected.invocations.length}</span>
              </div>
              <ol className="compact-list">
                {selected.invocations.map((invocation) => (
                  <li key={invocation.invocationId}>
                    <div>
                      <strong>{invocation.invocationId}</strong>
                      <span className={`tag tag-${
                        invocation.quarantined || invocation.status === "failed"
                          ? "danger"
                          : invocation.status === "completed"
                            ? "success"
                            : "warning"
                      }`}>
                        {invocation.quarantined ? "quarantined" : invocation.status}
                      </span>
                    </div>
                    <small>
                      rev {invocation.registryRevision} · {invocation.toolCalls ?? 0} tool calls ·
                      {invocation.inputTokens ?? 0}/{invocation.outputTokens ?? 0} tokens
                    </small>
                    {invocation.resultSummary ? <p>{invocation.resultSummary}</p> : null}
                    {invocation.errorMessage ? <p className="danger-copy">{invocation.errorMessage}</p> : null}
                    {invocation.quarantineReason ? (
                      <p className="danger-copy">{invocation.quarantineReason}</p>
                    ) : null}
                  </li>
                ))}
              </ol>
              <label>
                Public invocation arguments
                <textarea
                  value={argumentsJson}
                  rows={6}
                  spellCheck={false}
                  onChange={(event) => setArgumentsJson(event.currentTarget.value)}
                  disabled={view.selected?.invokeDisabled}
                />
              </label>
              <div className="task-actions">
                <button
                  className="button button-primary"
                  type="button"
                  disabled={Boolean(busy) || view.selected?.invokeDisabled}
                  onClick={() => void act("invoke", invoke)}
                >
                  {busy === "invoke" ? "Invoking…" : "Invoke through permission"}
                </button>
                <button
                  className="button button-secondary"
                  type="button"
                  disabled={Boolean(busy) || view.selected?.updateDisabled}
                  onClick={() => void act("update", () =>
                    controller.updateSkill({
                      skillId: selected.skillId,
                      expectedHash: selected.version.contentHash,
                    }))}
                >
                  {busy === "update" ? "Updating…" : "Update reviewed version"}
                </button>
              </div>
            </article>
          </div>
        </>
      ) : null}

      {view.warnings.length ? (
        <details>
          <summary>{view.warnings.length} admission and audit warnings</summary>
          <ul>
            {view.warnings.map((warning) => <li key={warning}>{warning}</li>)}
          </ul>
        </details>
      ) : null}
    </section>
  )
}
