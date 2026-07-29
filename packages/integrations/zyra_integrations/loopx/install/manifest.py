"""Compatibility facade for the embedded runtime manifest API."""

from ..runtime.manifest import (
    EMBEDDED_ROOT_RELATIVE_PATH,
    LOOPX_SOURCE_COMMIT,
    LOOPX_SOURCE_REF,
    LOOPX_SOURCE_TREE_COMMIT,
    LOOPX_VERSION,
    PACKAGE_LOCK_RELATIVE_PATH,
    PACKAGE_LOCK_SCHEMA,
    SOURCE_MANIFEST_NAME,
    SOURCE_MANIFEST_SCHEMA,
    LoopXPackageLock,
    sha256_file,
    source_manifest_digest,
    stable_digest,
    stable_json,
)

__all__ = [
    "EMBEDDED_ROOT_RELATIVE_PATH",
    "LOOPX_SOURCE_COMMIT",
    "LOOPX_SOURCE_REF",
    "LOOPX_SOURCE_TREE_COMMIT",
    "LOOPX_VERSION",
    "PACKAGE_LOCK_RELATIVE_PATH",
    "PACKAGE_LOCK_SCHEMA",
    "SOURCE_MANIFEST_NAME",
    "SOURCE_MANIFEST_SCHEMA",
    "LoopXPackageLock",
    "sha256_file",
    "source_manifest_digest",
    "stable_digest",
    "stable_json",
]
