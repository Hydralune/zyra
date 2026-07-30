# Limitations and explicit degraded paths

- `phase2_strongest_v1` is deterministic and evidence-gated; it does not
  reproduce upstream training results or introduce a learned policy.
- Missing, corrupt, stale, `evidence_only`, or `unavailable` readiness fails
  closed to the explicit Phase 1 deterministic baseline and records the
  degradation reason.
- A stale graph snapshot, invalid capability, forbidden placement, privacy or
  permission violation, lease conflict, or budget breach produces no canonical
  mutation.
- Cloud evidence requires live credentials and a real provider response.
  Absent credentials block the live lane; a simulated receipt cannot replace
  it.
- Edge evidence requires an isolated process identity and failure boundary.
  A placement label alone is insufficient.
- The fixed LoopX source is embedded for offline use. Update/self-update paths
  are not required at task runtime and cannot become an external source
  dependency.
- Post-freeze changes are limited to defects exposed by real scenarios. Any
  affected release, cleanroom, regression, or evidence gate must be rerun
  against a new target.
