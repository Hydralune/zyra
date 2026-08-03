# Zyra bundled source provenance

This directory is immutable, non-runtime evidence. It contains only the exact source files reviewed by Zyra, their source graphs, frozen authority manifests, Phase 2 mechanism analyses, and the pinned LoopX source archive. Runtime code must never import or execute files from this directory. Rebuild it only through `scripts/build_source_provenance.py --workspace-root <explicit-source-workspace>`.
