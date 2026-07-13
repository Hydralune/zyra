from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from zyra_core import ArtifactKind, ArtifactRef, new_id, to_jsonable


TEXT_ARTIFACT_KINDS = {
    ArtifactKind.TEXT,
    ArtifactKind.MARKDOWN,
    ArtifactKind.CODE,
    ArtifactKind.REPORT,
    ArtifactKind.TRACE,
    ArtifactKind.DATASET,
    ArtifactKind.STRUCTURED_DATA,
}

TEXT_SUFFIXES = {
    ".cfg",
    ".csv",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".py",
    ".toml",
    ".ts",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


class LocalArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def write_text(
        self,
        *,
        run_id: str,
        task_id: str,
        content: str,
        title: str,
        kind: ArtifactKind = ArtifactKind.TEXT,
        extension: str = ".txt",
        producer_node_id: str | None = None,
    ) -> ArtifactRef:
        artifact_id = new_id("artifact")
        directory = self.root / run_id / task_id
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"{artifact_id}{extension if extension.startswith('.') else f'.{extension}'}"
        target = directory / filename
        self._atomic_write(target, content.encode("utf-8"))
        return ArtifactRef(
            artifact_id=artifact_id,
            kind=kind,
            uri=str(target),
            title=title,
            producer_node_id=producer_node_id,
            metadata={"storage": "local", "relative_path": str(target.relative_to(self.root))},
        )

    def write_bytes(
        self,
        *,
        run_id: str,
        task_id: str,
        content: bytes,
        title: str,
        kind: ArtifactKind = ArtifactKind.FILE,
        extension: str = ".bin",
        producer_node_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        artifact_id = new_id("artifact")
        directory = self.root / run_id / task_id
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"{artifact_id}{extension if extension.startswith('.') else f'.{extension}'}"
        target = directory / filename
        self._atomic_write(target, bytes(content))
        artifact_metadata = {
            "storage": "local",
            "relative_path": str(target.relative_to(self.root)),
            "size_bytes": len(content),
            **(metadata or {}),
        }
        return ArtifactRef(
            artifact_id=artifact_id,
            kind=kind,
            uri=str(target),
            title=title,
            producer_node_id=producer_node_id,
            metadata=artifact_metadata,
        )

    def describe(self, artifact: ArtifactRef) -> dict[str, Any]:
        path = self.resolve_path(artifact)
        exists = path.exists()
        is_file = path.is_file() if exists else False
        return {
            "artifact": to_jsonable(artifact),
            "relative_path": str(path.relative_to(self.root)),
            "exists": exists,
            "is_file": is_file,
            "size_bytes": path.stat().st_size if is_file else 0,
            "content_type": _content_type(path),
            "previewable": _is_text_artifact(artifact, path),
        }

    def read_preview(self, artifact: ArtifactRef, *, max_chars: int = 20000) -> dict[str, Any]:
        entry = self.describe(artifact)
        path = self.resolve_path(artifact)
        if not entry["exists"]:
            return {**entry, "content": None, "truncated": False, "binary": False, "error": "missing_artifact"}
        if not entry["is_file"]:
            return {**entry, "content": None, "truncated": False, "binary": False, "error": "not_a_file"}
        if not _is_text_artifact(artifact, path):
            return {**entry, "content": None, "truncated": False, "binary": True, "error": None}

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {**entry, "content": None, "truncated": False, "binary": True, "error": None}

        return {
            **entry,
            "content": content[:max_chars],
            "truncated": len(content) > max_chars,
            "binary": False,
            "error": None,
        }

    def resolve_path(self, artifact: ArtifactRef) -> Path:
        relative_path = artifact.metadata.get("relative_path")
        if isinstance(relative_path, str) and relative_path:
            path = (self.root / relative_path).resolve()
        elif artifact.uri:
            path = Path(artifact.uri).resolve()
        else:
            raise ValueError("artifact has no local path")

        path.relative_to(self.root)
        return path

    @staticmethod
    def _atomic_write(target: Path, content: bytes) -> None:
        """Commit artifact bytes without exposing a partial final file.

        The temporary file lives beside the destination so ``os.replace`` is
        an atomic same-filesystem rename.  A failed write or replace removes
        the temporary file and leaves an existing destination untouched.
        """

        temporary = target.with_name(f".{target.name}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            # Persist the directory entry on platforms that support opening a
            # directory descriptor.  Windows raises OSError and already gives
            # atomic replace semantics for the file entry.
            try:
                descriptor = os.open(target.parent, os.O_RDONLY)
            except OSError:
                descriptor = -1
            if descriptor >= 0:
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


def _is_text_artifact(artifact: ArtifactRef, path: Path) -> bool:
    return artifact.kind in TEXT_ARTIFACT_KINDS or path.suffix.lower() in TEXT_SUFFIXES


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".html":
        return "text/html"
    if suffix == ".json":
        return "application/json"
    if suffix == ".md":
        return "text/markdown"
    if suffix in TEXT_SUFFIXES:
        return "text/plain"
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".pdf":
        return "application/pdf"
    return "application/octet-stream"
