# Recovery and rollback

The release candidate is immutable and checksum-bound. Recovery first stops
all release-owned processes, verifies that declared ports are free, restores
the last committed product state, and runs doctor plus semantic health before
resuming work.

Topology-policy rollback is explicit:

1. retain the failed strongest receipt and its causal references;
2. resolve `topology_policy/phase1_deterministic_baseline`;
3. emit degradation and rollback events;
4. preserve existing run pins and prevent duplicate commits, claims, spends,
   leases, or side effects;
5. verify the restored graph, memory, scheduler, and LoopX outbox state.

Package rollback uses the existing transactional installer and migration
journal. It restores the preceding committed release rather than editing the
active payload in place. Credentials are never included in the archive,
evidence index, or rollback state.
