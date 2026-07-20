# Zyra Code Index

This package owns rebuildable file, content, symbol, reference, and call-edge
indexes for a workspace that has already been authorized by
`WorkspaceManagerRuntime`. Canonical file bytes, hashes, binding revisions, and
patch transactions stay in the workspace subsystem.

The production constructor is `CodeIndexRuntime.from_workspace_manager`.
`BoundWorkspaceSource.for_test` exists only for isolated tests and is explicitly
named so it cannot be mistaken for the application path.
