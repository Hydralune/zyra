import {
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type FormEvent,
} from "react"
import { formalActionPolicy } from "../admission.ts"
import type { ScenarioWorkbenchRuntime } from "../runtime.ts"

function duration(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "—"
  if (value < 1_000) return `${Math.floor(value)} ms`
  const seconds = value / 1_000
  if (seconds < 60) return `${seconds.toFixed(1)} s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m ${Math.floor(seconds % 60)}s`
}

function shortDigest(value: unknown): string {
  const selected = String(value || "")
  return selected.length > 18
    ? `${selected.slice(0, 10)}…${selected.slice(-6)}`
    : selected || "—"
}

export function ScenarioWorkbench({
  runtime,
}: {
  runtime: ScenarioWorkbenchRuntime
}) {
  const snapshot = useSyncExternalStore(
    runtime.subscribe,
    runtime.getSnapshot,
    runtime.getSnapshot,
  )
  const [input, setInput] = useState("")
  const [seed, setSeed] = useState("0")
  const [scenarioId, setScenarioId] = useState("live.software-delivery")
  const [busy, setBusy] = useState("")
  const [error, setError] = useState("")
  const selected = snapshot.selected
  const actions = selected
    ? formalActionPolicy(selected.run)
    : undefined
  const definition = snapshot.definitions.find(
    (item) => item.scenario_id === scenarioId,
  ) ?? snapshot.definitions[0]
  const liveEvidence = selected?.evidence.live
  const effects = useMemo(
    () => Object.entries(selected?.evidence.effects ?? {}),
    [selected?.evidence.effects],
  )

  useEffect(() => {
    void runtime.open().catch((reason) => {
      setError(reason instanceof Error ? reason.message : String(reason))
    })
    return () => runtime.detach("Scenario panel unmounted.")
  }, [runtime])

  const create = async (event: FormEvent) => {
    event.preventDefault()
    setBusy("create")
    setError("")
    try {
      const run = await runtime.create({
        scenarioId: definition?.scenario_id,
        definitionVersion: definition?.version,
        mode: "sealed",
        input,
        seed: Number(seed),
        labels: {
          surface: "zyra-workbench",
          slice: "M2-S05-02",
        },
      })
      setInput("")
      await runtime.select(run.scenario_run_id)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy("")
    }
  }

  const mutate = async (
    operation: "start" | "archive" | "verify",
  ) => {
    if (!selected) return
    setBusy(operation)
    setError("")
    try {
      if (operation === "start") {
        await runtime.start(selected.run.scenario_run_id)
      } else if (operation === "archive") {
        await runtime.archive(
          selected.run.scenario_run_id,
          "Archived from the scenario workbench.",
        )
      } else {
        await runtime.verify(selected.run.scenario_run_id)
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy("")
    }
  }

  return (
    <section
      className="detail-section"
      aria-labelledby="scenario-workbench-heading"
      data-scenario-connection={snapshot.connection}
      data-scenario-active-count={snapshot.activeCount}
      data-scenario-evidence-valid-count={snapshot.evidenceValidCount}
    >
      <div className="section-heading">
        <div>
          <p className="eyebrow">Dual-domain live evidence</p>
          <h2 id="scenario-workbench-heading">Long-run scenario runner</h2>
        </div>
        <span className="tag" data-phase={snapshot.connection}>
          {snapshot.connection}
        </span>
      </div>
      <p className="muted-copy">
        Runs are owned by the backend and continue after this browser closes.
        Formal software-delivery and cross-source research runs require 2,000+
        effective transitions, representative recovery and canonical
        route/placement migration. Authenticated provider/model CLI execution
        is excluded by the M2-S05-02 user boundary.
      </p>

      <form className="settings-grid" onSubmit={create}>
        <label>
          Scenario domain
          <select
            value={definition?.scenario_id ?? ""}
            onChange={(event) => setScenarioId(event.currentTarget.value)}
            disabled={Boolean(busy)}
          >
            {snapshot.definitions.map((item) => (
              <option key={`${item.scenario_id}:${item.version}`} value={item.scenario_id}>
                {item.title}
              </option>
            ))}
          </select>
        </label>
        <label>
          New scenario input
          <textarea
            value={input}
            onChange={(event) => setInput(event.currentTarget.value)}
            placeholder={
              definition?.scenario_id === "live.cross-source-research"
                ? "State a new research question with claims, contradiction and citation requirements."
                : "State a new software change with verifiable behavior and delivery requirements."
            }
            required
            rows={4}
            disabled={Boolean(busy)}
          />
        </label>
        <label>
          Deterministic seed
          <input
            type="number"
            min="0"
            step="1"
            value={seed}
            onChange={(event) => setSeed(event.currentTarget.value)}
            disabled={Boolean(busy)}
          />
        </label>
        <div>
          <button
            className="button button-primary"
            type="submit"
            disabled={Boolean(busy) || !input.trim() || !definition}
          >
            {busy === "create" ? "Admitting…" : "Create sealed run"}
          </button>
          <button
            className="button button-secondary"
            type="button"
            disabled={Boolean(busy)}
            onClick={() => void runtime.refresh("manual")}
          >
            Refresh
          </button>
        </div>
      </form>

      {error ? (
        <div className="plan-warning" role="alert">{error}</div>
      ) : null}

      <div className="settings-grid">
        <article>
          <h3>Durable runs</h3>
          {snapshot.rows.length ? (
            <ol className="plan-list">
              {snapshot.rows.map((row) => (
                <li
                  className="plan-node"
                  key={row.run.scenario_run_id}
                  data-selected={row.selected}
                >
                  <span
                    className={`status-marker status-${
                      row.run.phase === "succeeded"
                        ? "completed"
                        : row.run.phase === "failed"
                          ? "failed"
                          : "running"
                    }`}
                    aria-hidden="true"
                  />
                  <button
                    type="button"
                    className="button button-secondary"
                    onClick={() => void runtime.select(row.run.scenario_run_id)}
                  >
                    <strong>{row.run.configuration.scenario_id}</strong>
                    <span>{row.run.phase}</span>
                    <small>{row.run.scenario_run_id}</small>
                  </button>
                </li>
              ))}
            </ol>
          ) : (
            <p className="muted-copy">
              No scenario run has been admitted in this clean state.
            </p>
          )}
        </article>

        <article>
          <h3>Admission and evidence</h3>
          {selected ? (
            <>
              <dl className="fact-grid">
                <div>
                  <dt>Phase</dt>
                  <dd>{selected.run.phase}</dd>
                </div>
                <div>
                  <dt>Elapsed</dt>
                  <dd>{duration(selected.elapsedMs)}</dd>
                </div>
                <div>
                  <dt>Clean state</dt>
                  <dd>{selected.admission.clean ? "verified" : "failed"}</dd>
                </div>
                <div>
                  <dt>New input</dt>
                  <dd>{selected.admission.newInput ? "verified" : "replay"}</dd>
                </div>
                <div>
                  <dt>Policy</dt>
                  <dd>{shortDigest(
                    selected.run.configuration.policy?.policy_digest,
                  )}</dd>
                </div>
                <div>
                  <dt>Human interventions</dt>
                  <dd>{selected.admission.humanInterventionCount}</dd>
                </div>
                <div>
                  <dt>Effective steps</dt>
                  <dd>{selected.evidence.effectiveStepCount}</dd>
                </div>
                <div>
                  <dt>Invalid steps</dt>
                  <dd>{selected.evidence.invalidStepCount}</dd>
                </div>
                <div>
                  <dt>Artifacts</dt>
                  <dd>{selected.evidence.artifactCount}</dd>
                </div>
                <div>
                  <dt>Evidence</dt>
                  <dd>{selected.evidence.valid ? "verified" : "pending"}</dd>
                </div>
              </dl>
              <div className="task-actions">
                <button
                  className="button button-primary"
                  type="button"
                  disabled={Boolean(busy) || !actions?.mayStart}
                  onClick={() => void mutate("start")}
                >
                  {busy === "start" ? "Starting…" : "Start"}
                </button>
                <button
                  className="button button-secondary"
                  type="button"
                  disabled={Boolean(busy) || !actions?.mayVerify}
                  onClick={() => void mutate("verify")}
                >
                  {busy === "verify" ? "Verifying…" : "Verify evidence"}
                </button>
                <button
                  className="button button-secondary"
                  type="button"
                  disabled={Boolean(busy) || !actions?.mayArchive}
                  onClick={() => void mutate("archive")}
                >
                  Archive
                </button>
              </div>
              {actions?.warning ? (
                <p className="plan-warning">{actions.warning}</p>
              ) : null}
              {selected.admission.findings.map((item) => (
                <p className="plan-warning" key={item.code}>
                  {item.message}
                </p>
              ))}
            </>
          ) : (
            <p className="muted-copy">Select a scenario run to inspect it.</p>
          )}
        </article>
      </div>

      {selected?.evidence.valid ? (
        <section aria-label="Effective step dimensions">
          <div className="section-heading">
            <h3>Semantic effects</h3>
            <span>{selected.evidence.effectiveStepCount}</span>
          </div>
          <dl className="fact-grid">
            {effects.map(([effect, count]) => (
              <div key={effect}>
                <dt>{effect}</dt>
                <dd>{count}</dd>
              </div>
            ))}
          </dl>
        </section>
      ) : null}

      {liveEvidence ? (
        <section aria-label="Formal live scenario evidence">
          <div className="section-heading">
            <div>
              <p className="eyebrow">Deterministic acceptance</p>
              <h3>Fault, placement and domain proof</h3>
            </div>
            <span className="tag" data-phase={liveEvidence.complete ? "succeeded" : "running"}>
              {liveEvidence.complete ? "formal" : "incomplete"}
            </span>
          </div>
          <dl className="fact-grid">
            <div>
              <dt>Domain</dt>
              <dd>{liveEvidence.domain ?? "—"}</dd>
            </div>
            <div>
              <dt>Canonical transitions</dt>
              <dd>{liveEvidence.effectiveTransitionCount}</dd>
            </div>
            <div>
              <dt>Recovered faults</dt>
              <dd>
                {liveEvidence.recoveredFaultCount}/{liveEvidence.faultCount}
              </dd>
            </div>
            <div>
              <dt>Execution tiers</dt>
              <dd>{liveEvidence.tierCount}/3</dd>
            </div>
            <div>
              <dt>Provider/model capabilities</dt>
              <dd>{liveEvidence.providerModelCapabilityCount}/2</dd>
            </div>
            <div>
              <dt>Domain verifier</dt>
              <dd>{liveEvidence.verificationValid ? "verified" : "failed"}</dd>
            </div>
            <div>
              <dt>Placement verifier</dt>
              <dd>{liveEvidence.placementValid ? "verified" : "failed"}</dd>
            </div>
            <div>
              <dt>Causal archive</dt>
              <dd>{shortDigest(liveEvidence.archiveDigest)}</dd>
            </div>
          </dl>
        </section>
      ) : null}

      {selected?.sourceAudit ? (
        <section aria-label="M2 source role audit">
          <div className="section-heading">
            <h3>Role-aware source audit</h3>
            <span>{selected.sourceAudit.valid ? "valid" : "invalid"}</span>
          </div>
          <p className="muted-copy">
            {selected.sourceAudit.active.length} active owner rows ·{" "}
            {selected.sourceAudit.inactiveNotMissing.length} inactive roles
            (not missing capabilities) · OpenClaw{" "}
            {selected.sourceAudit.openclaw}
          </p>
        </section>
      ) : null}
    </section>
  )
}
