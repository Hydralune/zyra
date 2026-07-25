# M2-S05-02 Incremental Critical Self-Review

## Frozen boundary

- baseline: `df5d0d4b0f4e05f2f188f58aea1ea379d9e62154`
- preimplementation decision: `6f6a12071f00ed20fd3cddb6fbb2d4e31aa122fb`
- final implementation target: `827a9689588eef6e1f75b1ff062c2bcaaeb5aa8e`
- evidence directory: `docs/reviews/evidence/M2-S05-02`
- OpenClaw: `excluded_forward_only`

The 2026-07-26 user boundary supersedes the original provider/model dispatch
gate for this slice. Formal M2-S05-02 execution must not discover, start or use
an installed/authenticated Claude CLI, Codex CLI or other provider/model CLI.
It must not use user credentials or incur a model request. Real
device/edge/cloud and multi-provider/model claims are therefore explicitly
false rather than simulated.

## Delivered behavior and state ownership

The slice adds two live domains under the existing
`ScenarioRunnerService`: software delivery and cross-source research. Scenario
planning, domain adapters, deterministic verifiers, fault scheduling,
placement evidence, causal archive creation and evidence projection live under
`packages/evaluation/zyra_evaluation/scenario_runner`. The existing API task,
event log, workspace, worker-pool lease, fault, recovery, memory and artifact
owners retain canonical custody. `apps/api/zyra_api/live_scenario_owners.py`
only maps those receipts into the scenario contract and receives zero
effective-production credit.

The console consumes the backend evidence manifest. It neither owns scenario
state nor synthesizes completion. Closing or detaching the panel issues no
cancel command.

The user-boundary correction removed the installed-Claude lookup and managed
provider probe from the API composition path. The formal profile requires no
tier or provider execution, exposes
`authenticated_provider_cli_allowed=false`, and verifies both
`external_provider_execution_excluded=true` and
`authenticated_provider_cli_invoked=false`. The integration regression
replaces both external probe methods with call sites that raise immediately;
the formal run still succeeds.

## Live evidence

Both final runs used `scripts/run_embedded_scenario_evidence.py`. This runner is
foreground-only, opens no listener, starts no background process, refuses an
existing state directory, confines state beneath project `.tmp`, removes
provider/API-key variables only from its own process environment and closes the
scenario service gracefully.

| Domain | Run | Effective steps | Archive events | Faults recovered | Routes | Result |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| software delivery | `scenario_13e9390534814e9298e2967c8ee7335d` | 2,164 | 2,158 | 5/5 | 4 | succeeded |
| cross-source research | `scenario_7407264daad9474bbfae40ca43b11c48` | 7,169 | 7,163 | 5/5 | 4 | succeeded |

Both runs started from clean state with new input, used the sealed policy,
recorded zero human and operator interventions, passed domain and evidence
verification, and bound their causal archives to commit
`827a9689588eef6e1f75b1ff062c2bcaaeb5aa8e`.

The software run created a confined workspace patch, executed Git, compiled the
changed target, ran behavior/failure-path tests, applied the requirement
change and delivered checksum-bound patch/report artifacts.

The research run acquired new bytes from RFC Editor, IANA and MDN. Source
receipts record HTTP 200, distinct authorities, `live=true`, `replay=false`,
exact source/wire checksums and citation spans. Its deterministic verifier
proved source, claim, citation, report and artifact integrity and explicitly
reported no uncertainty findings.

## Fault and placement review

Each domain exercised requirement change, timeout/exception, worker/node loss,
provider failure/rate-limit and edge/network loss. Provider faults are
deterministic injected boundaries; they do not claim or initiate a provider
turn. The campaign proves checkpoint creation, exact resume, recovery events,
route/lease replacement and post-recovery reverification.

The first actual research run exposed a real defect: `NETWORK_LOSS` required a
checkpoint but was missing from the route-migration set. Its verifier rejected
the stale route. Commit `827a968` adds `NETWORK_LOSS` to both the migration
runtime and placement-affecting observation invariant. The regression now
requires three research route migrations, and the final research run succeeds
with four distinct canonical routes/leases.

No claim is made for real edge/cloud dispatch or multi-model compatibility in
this slice. Those claim flags remain false and are no longer M2-S05-02 gates.

## Dynamic reachability and disable behavior

- The actual API integration test reaches canonical task, workspace,
  worker-pool, recovery, memory, artifact and event owners.
- Disabling worker-pool integration, memory retrieval, recovery, targeted
  communication or the domain verifier makes a formal live run fail; there is
  no demo or replay fallback.
- The software and research verifiers reject missing artifacts, invalid
  checksums/citations, incomplete actions, unresolved faults, inadequate
  semantic transition coverage and unchanged placement routes.
- The Web projection rejects incomplete live claims and any evidence that does
  not affirm provider execution exclusion.

## Effective-code gate

The exact baseline-to-target audit reports:

| Bucket | Lines |
| --- | ---: |
| raw additions | 14,051 |
| raw deletions | 45 |
| production runtime | 10,170 |
| UI behavior | 6 |
| effective production | 10,176 |
| UI presentation | 75 |
| declarations/types | 300 |
| schema/DTO/data | 615 |
| adapter-only | 1,106 |
| generated | 289 |
| test/mock/fixture | 691 |
| docs/comments/blanks | 799 |
| vendor/source-pool | 0 |

The required minimum is 7,000. Even if the 236 executable lines in the safe
embedded runner receive zero production credit, the conservative total is
9,940. Large-file triggers were reviewed as scenario-domain modules or
zero-credit adapters/tests; no whole upstream tree, generated bundle, DTO
inflation or source-pool content is counted.

## Verification performed

- Python scenario, foundation and API integration regression:
  `21 passed in 142.59s`.
- Scenario Workbench regression: `10 passed`, `58` assertions.
- Web TypeScript typecheck: passed.
- Python compileall for API, scenario runtime and embedded runner: passed.
- Exact diff whitespace check: passed.
- Dependency-boundary scan: no lockfile, package, Docker or dependency change.
- Runtime-path scan: no `../claude-code-best`, `../browser-use`,
  `../OpenHands`, `../opencode` or `../openclaw` dependency.
- Provider-process scan: no Claude executable discovery or process call site.
  The only retained `ZYRA_LIVE_PROVIDER_EXECUTABLE` reference removes that
  variable from the foreground runner's childless process environment.

The complete canonical event archives, raw samples, owner receipts,
environment/configuration, deterministic verification, source/delivery
artifacts and their SHA-256 digests are retained in the evidence ZIP files.
`verification-summary.json` is the compact index.

## Critical non-claims and remaining scope

- This slice does not close M2 exit, ablation, repeated P50/P95 statistics,
  true edge/cloud dispatch or multi-model compatibility.
- Long repeated runs and competition bundle consolidation remain assigned to
  M2-S05-03/M3-02A.
- Root milestone documents were amended to remove the authenticated
  provider/model invocation requirement. They are outside the `zyra` Git
  repository and therefore cannot be included in the Zyra evidence commit.
