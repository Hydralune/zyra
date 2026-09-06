from __future__ import annotations

import hashlib
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping

from zyra_core import ArtifactKind, ArtifactRef, new_id, to_jsonable


ARTIFACT_CONTRACT = "zyra.artifact.v2"
ARTIFACT_REVISION_PREFIX = "sha256:"
DEFAULT_READ_CHUNK_BYTES = 256 * 1024
MAX_READ_CHUNK_BYTES = 1024 * 1024

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
    ".ini",
    ".js",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".py",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}

SAFE_INLINE_MEDIA_TYPES = {
    "image/avif",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
    "audio/aac",
    "audio/flac",
    "audio/mpeg",
    "audio/ogg",
    "audio/wav",
    "video/mp4",
    "video/ogg",
    "video/webm",
}

BLOCKED_EXECUTABLE_SUFFIXES = {
    ".app",
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".dmg",
    ".exe",
    ".hta",
    ".jar",
    ".msi",
    ".ps1",
    ".scr",
    ".sh",
}

_BOM_ENCODINGS: tuple[tuple[bytes, str], ...] = (
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xfe\xff", "utf-16-be"),
    (b"\xff\xfe", "utf-16-le"),
)

_SAFE_TOKEN = re.compile(r"^[a-zA-Z0-9._:@/+-]{1,512}$")


@dataclass(frozen=True, slots=True)
class ArtifactObservedContent:
    path: Path
    size_bytes: int
    sha256: str
    content_type: str
    content_family: str
    encoding: str | None
    byte_order_mark: str | None
    line_endings: tuple[str, ...]
    text_candidate: bool
    inline_safe: bool
    executable_risk: bool

    @property
    def revision(self) -> str:
        return f"{ARTIFACT_REVISION_PREFIX}{self.sha256}"

    def to_metadata(self) -> dict[str, Any]:
        return {
            "contract": ARTIFACT_CONTRACT,
            "sha256": self.sha256,
            "revision": self.revision,
            "size_bytes": self.size_bytes,
            "media_type": self.content_type,
            "content_family": self.content_family,
            "encoding": self.encoding,
            "byte_order_mark": self.byte_order_mark,
            "line_endings": list(self.line_endings),
            "text_candidate": self.text_candidate,
            "inline_safe": self.inline_safe,
            "executable_risk": self.executable_risk,
            "immutable": True,
        }


@dataclass(frozen=True, slots=True)
class ArtifactByteRange:
    offset: int
    length: int
    total_bytes: int
    content: bytes
    complete: bool

    @property
    def end_exclusive(self) -> int:
        return self.offset + len(self.content)


def normalize_security_label(value: Any) -> str:
    selected = str(value or "internal").strip().lower().replace("-", "_")
    if selected in {"public", "internal", "confidential", "secret"}:
        return selected
    return "internal"


def normalize_trust_disposition(value: Any) -> str:
    selected = str(value or "trusted").strip().lower().replace("-", "_")
    if selected in {"trusted", "untrusted", "quarantined"}:
        return selected
    return "untrusted"


def normalize_retention_policy(value: Any) -> str:
    selected = str(value or "task").strip().lower().replace("-", "_")
    if selected in {"ephemeral", "task", "run", "project", "submission"}:
        return selected
    return "task"


def normalize_download_policy(value: Any, *, security_label: str) -> str:
    selected = str(value or "allow").strip().lower().replace("-", "_")
    if security_label == "secret":
        return "deny"
    if selected in {"allow", "confirm", "deny"}:
        return selected
    return "confirm"


def normalize_producer_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    producer_node_id: str | None,
) -> dict[str, Any]:
    source = dict(metadata or {})
    result: dict[str, Any] = {}
    aliases = {
        "producer_node_id": producer_node_id or source.get("producer_node_id"),
        "producer_span_id": source.get("producer_span_id") or source.get("span_id"),
        "producer_tool_call_id": source.get("producer_tool_call_id") or source.get("tool_call_id"),
        "producer_worker_id": source.get("producer_worker_id") or source.get("worker_id"),
    }
    for key, value in aliases.items():
        rendered = str(value or "").strip()
        if rendered and _SAFE_TOKEN.fullmatch(rendered):
            result[key] = rendered
    return result


class LocalArtifactStore:
    """Zyra-owned artifact byte and immutable metadata custody.

    The task/event stores own references to committed artifacts. This class
    owns local bytes and the metadata derived from exactly those bytes. Reads
    never accept a client-supplied filesystem path.
    """

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
        metadata: dict[str, Any] | None = None,
        encoding: str = "utf-8",
    ) -> ArtifactRef:
        normalized_encoding = normalize_text_encoding(encoding)
        encoded = content.encode(normalized_encoding)
        return self._commit_bytes(
            run_id=run_id,
            task_id=task_id,
            content=encoded,
            title=title,
            kind=kind,
            extension=extension,
            producer_node_id=producer_node_id,
            metadata=metadata,
            declared_encoding=normalized_encoding,
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
        return self._commit_bytes(
            run_id=run_id,
            task_id=task_id,
            content=bytes(content),
            title=title,
            kind=kind,
            extension=extension,
            producer_node_id=producer_node_id,
            metadata=metadata,
            declared_encoding=None,
        )

    def write_from_path(
        self,
        *,
        run_id: str,
        task_id: str,
        source_path: str | Path,
        title: str,
        kind: ArtifactKind = ArtifactKind.FILE,
        extension: str | None = None,
        producer_node_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        source = Path(source_path).resolve()
        if not source.is_file():
            raise ValueError("artifact source must be an existing regular file")
        artifact_id = new_id("artifact")
        suffix = extension if extension is not None else source.suffix
        target = self._target_path(
            run_id=run_id,
            task_id=task_id,
            artifact_id=artifact_id,
            extension=suffix or ".bin",
        )
        self._atomic_copy(source, target)
        observed = observe_artifact_path(target, kind=kind)
        artifact_metadata = self._metadata_for_commit(
            target=target,
            observed=observed,
            producer_node_id=producer_node_id,
            metadata=metadata,
            declared_encoding=None,
        )
        return ArtifactRef(
            artifact_id=artifact_id,
            kind=kind,
            uri=str(target),
            title=title,
            producer_node_id=producer_node_id,
            metadata=artifact_metadata,
        )

    def describe(self, artifact: ArtifactRef, *, verify: bool = False) -> dict[str, Any]:
        path = self.resolve_path(artifact)
        exists = path.exists()
        is_file = path.is_file() if exists else False
        expected = expected_artifact_integrity(artifact)
        base = {
            "artifact": to_jsonable(artifact),
            "contract": str(artifact.metadata.get("contract") or "zyra.artifact.legacy"),
            "relative_path": str(path.relative_to(self.root)),
            "exists": exists,
            "is_file": is_file,
            "size_bytes": path.stat().st_size if is_file else 0,
            "content_type": _content_type(path),
            "content_family": _content_family(_content_type(path), path),
            "previewable": _is_text_artifact(artifact, path),
            "expected_revision": expected["revision"],
            "expected_sha256": expected["sha256"],
            "expected_size_bytes": expected["size_bytes"],
            "integrity": "unverified" if is_file else "missing",
        }
        if not is_file or not verify:
            return base
        observed = observe_artifact_path(path, kind=artifact.kind)
        integrity = compare_artifact_integrity(artifact, observed)
        return {
            **base,
            **observed.to_metadata(),
            "observed_revision": observed.revision,
            "observed_sha256": observed.sha256,
            "observed_size_bytes": observed.size_bytes,
            "integrity": integrity,
        }

    def admit_refs(self, values: Any, *, run_id: str, task_id: str) -> list[ArtifactRef]:
        """Admit worker references only to verified bytes in this task's custody."""
        if not isinstance(values, (list, tuple)):
            return []
        admitted: dict[str, ArtifactRef] = {}
        task_root = (self.root / run_id / task_id).resolve()
        if not task_root.is_relative_to(self.root):
            raise ValueError("artifact task root is outside custody")
        for value in values:
            if not isinstance(value, Mapping):
                continue
            artifact = ArtifactRef(
                artifact_id=str(value.get("artifact_id") or ""),
                kind=ArtifactKind(str(value.get("kind") or "file")),
                uri=str(value.get("uri") or ""),
                title=str(value.get("title") or ""),
                producer_node_id=value.get("producer_node_id"),
                created_at=str(value.get("created_at") or ""),
                metadata=dict(value.get("metadata") or {}),
            )
            path = self.resolve_path(artifact)
            if not artifact.artifact_id or not path.is_relative_to(task_root):
                raise ValueError("worker artifact is outside the active task")
            expected = expected_artifact_integrity(artifact)
            if (
                artifact.metadata.get("contract") != ARTIFACT_CONTRACT
                or not expected["sha256"]
                or not expected["revision"]
                or expected["size_bytes"] is None
            ):
                raise ValueError("worker artifact lacks committed integrity metadata")
            self.verify(artifact)
            admitted[artifact.artifact_id] = artifact
        return list(admitted.values())

    def verify(
        self,
        artifact: ArtifactRef,
        *,
        expected_revision: str | None = None,
    ) -> ArtifactObservedContent:
        path = self.resolve_path(artifact)
        if not path.exists():
            raise FileNotFoundError("artifact content is missing")
        if not path.is_file():
            raise ValueError("artifact content is not a regular file")
        before = path.stat()
        observed = observe_artifact_path(path, kind=artifact.kind)
        after = path.stat()
        if before.st_ino != after.st_ino or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise ArtifactIntegrityError("artifact changed while its integrity was being verified")
        comparison = compare_artifact_integrity(artifact, observed)
        if comparison in {"size_mismatch", "digest_mismatch", "revision_mismatch"}:
            raise ArtifactIntegrityError(comparison)
        requested = str(expected_revision or "").strip()
        if requested and requested != observed.revision:
            raise ArtifactRevisionError("requested artifact revision does not match committed bytes")
        return observed

    def read_range(
        self,
        artifact: ArtifactRef,
        *,
        offset: int = 0,
        length: int = DEFAULT_READ_CHUNK_BYTES,
        expected_revision: str | None = None,
    ) -> tuple[ArtifactObservedContent, ArtifactByteRange]:
        bounded_offset = max(0, int(offset))
        bounded_length = max(1, min(MAX_READ_CHUNK_BYTES, int(length)))
        observed = self.verify(artifact, expected_revision=expected_revision)
        if bounded_offset > observed.size_bytes:
            raise ArtifactRangeError("artifact byte offset exceeds content length")
        path = observed.path
        before = path.stat()
        with path.open("rb") as handle:
            handle.seek(bounded_offset)
            content = handle.read(bounded_length)
        after = path.stat()
        if before.st_ino != after.st_ino or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise ArtifactIntegrityError("artifact changed during bounded range read")
        return observed, ArtifactByteRange(
            offset=bounded_offset,
            length=bounded_length,
            total_bytes=observed.size_bytes,
            content=content,
            complete=bounded_offset + len(content) >= observed.size_bytes,
        )

    def iter_bytes(
        self,
        artifact: ArtifactRef,
        *,
        expected_revision: str | None = None,
        chunk_bytes: int = DEFAULT_READ_CHUNK_BYTES,
    ) -> Iterator[bytes]:
        observed = self.verify(artifact, expected_revision=expected_revision)
        bounded = max(1, min(MAX_READ_CHUNK_BYTES, int(chunk_bytes)))
        path = observed.path
        before = path.stat()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(bounded)
                if not chunk:
                    break
                yield chunk
        after = path.stat()
        if before.st_ino != after.st_ino or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise ArtifactIntegrityError("artifact changed during streaming read")

    def read_preview(self, artifact: ArtifactRef, *, max_chars: int = 20000) -> dict[str, Any]:
        entry = self.describe(artifact)
        if not entry["exists"]:
            return {**entry, "content": None, "truncated": False, "binary": False, "error": "missing_artifact"}
        if not entry["is_file"]:
            return {**entry, "content": None, "truncated": False, "binary": False, "error": "not_a_file"}
        try:
            observed, selected = self.read_range(
                artifact,
                length=min(MAX_READ_CHUNK_BYTES, max(4, max_chars * 4)),
            )
        except ArtifactIntegrityError as error:
            return {**entry, "content": None, "truncated": False, "binary": False, "error": str(error)}
        if not observed.text_candidate or observed.encoding is None:
            return {
                **entry,
                **observed.to_metadata(),
                "content": None,
                "truncated": False,
                "binary": True,
                "error": None,
            }
        try:
            content = selected.content.decode(observed.encoding, errors="strict")
        except UnicodeDecodeError:
            return {
                **entry,
                **observed.to_metadata(),
                "content": None,
                "truncated": False,
                "binary": True,
                "error": "invalid_text_encoding",
            }
        return {
            **entry,
            **observed.to_metadata(),
            "content": content[:max_chars],
            "truncated": selected.end_exclusive < observed.size_bytes or len(content) > max_chars,
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
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise ValueError("artifact path escapes the configured artifact root") from error
        return path

    def _commit_bytes(
        self,
        *,
        run_id: str,
        task_id: str,
        content: bytes,
        title: str,
        kind: ArtifactKind,
        extension: str,
        producer_node_id: str | None,
        metadata: dict[str, Any] | None,
        declared_encoding: str | None,
    ) -> ArtifactRef:
        artifact_id = new_id("artifact")
        target = self._target_path(
            run_id=run_id,
            task_id=task_id,
            artifact_id=artifact_id,
            extension=extension,
        )
        self._atomic_write(target, content)
        observed = observe_artifact_path(target, kind=kind)
        artifact_metadata = self._metadata_for_commit(
            target=target,
            observed=observed,
            producer_node_id=producer_node_id,
            metadata=metadata,
            declared_encoding=declared_encoding,
        )
        return ArtifactRef(
            artifact_id=artifact_id,
            kind=kind,
            uri=str(target),
            title=title,
            producer_node_id=producer_node_id,
            metadata=artifact_metadata,
        )

    def _target_path(
        self,
        *,
        run_id: str,
        task_id: str,
        artifact_id: str,
        extension: str,
    ) -> Path:
        safe_run = safe_path_segment(run_id, "run")
        safe_task = safe_path_segment(task_id, "task")
        suffix = normalize_extension(extension)
        directory = self.root / safe_run / safe_task
        directory.mkdir(parents=True, exist_ok=True)
        target = (directory / f"{artifact_id}{suffix}").resolve()
        target.relative_to(self.root)
        return target

    def _metadata_for_commit(
        self,
        *,
        target: Path,
        observed: ArtifactObservedContent,
        producer_node_id: str | None,
        metadata: Mapping[str, Any] | None,
        declared_encoding: str | None,
    ) -> dict[str, Any]:
        source = dict(metadata or {})
        security_label = normalize_security_label(source.get("security_label"))
        trust = normalize_trust_disposition(source.get("trust_disposition"))
        retention = normalize_retention_policy(source.get("retention_policy"))
        download = normalize_download_policy(
            source.get("download_policy"),
            security_label=security_label,
        )
        reserved = {
            "contract",
            "sha256",
            "revision",
            "size_bytes",
            "media_type",
            "content_family",
            "encoding",
            "byte_order_mark",
            "line_endings",
            "text_candidate",
            "inline_safe",
            "executable_risk",
            "immutable",
            "storage",
            "relative_path",
            "producer_node_id",
            "producer_span_id",
            "producer_tool_call_id",
            "producer_worker_id",
            "security_label",
            "trust_disposition",
            "retention_policy",
            "download_policy",
        }
        passthrough = {
            str(key): value
            for key, value in source.items()
            if str(key) not in reserved and is_json_metadata_value(value)
        }
        result = {
            **passthrough,
            "storage": "local",
            "relative_path": str(target.relative_to(self.root)),
            **observed.to_metadata(),
            **normalize_producer_metadata(source, producer_node_id=producer_node_id),
            "security_label": security_label,
            "trust_disposition": trust,
            "retention_policy": retention,
            "download_policy": download,
        }
        if declared_encoding:
            result["declared_encoding"] = declared_encoding
        expiry = str(source.get("retention_expires_at") or "").strip()
        if expiry:
            result["retention_expires_at"] = expiry[:128]
        return result

    @staticmethod
    def _atomic_write(target: Path, content: bytes) -> None:
        temporary = target.with_name(f".{target.name}.tmp.{os.getpid()}")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            _sync_parent_directory(target.parent)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _atomic_copy(source: Path, target: Path) -> None:
        temporary = target.with_name(f".{target.name}.tmp.{os.getpid()}")
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                while True:
                    chunk = reader.read(DEFAULT_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary, target)
            _sync_parent_directory(target.parent)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


class ArtifactIntegrityError(ValueError):
    pass


class ArtifactRevisionError(ValueError):
    pass


class ArtifactRangeError(ValueError):
    pass


def observe_artifact_path(
    path: Path,
    *,
    kind: ArtifactKind | None = None,
) -> ArtifactObservedContent:
    digest = hashlib.sha256()
    size = 0
    prefix = bytearray()
    newline_sample = bytearray()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(DEFAULT_READ_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if len(prefix) < 8_192:
                prefix.extend(chunk[: 8_192 - len(prefix)])
            if len(newline_sample) < 1024 * 1024:
                newline_sample.extend(chunk[: 1024 * 1024 - len(newline_sample)])
    media_type = _content_type(path)
    encoding, bom = detect_text_encoding(bytes(prefix), path=path, kind=kind)
    text_candidate = encoding is not None
    line_endings = detect_line_endings(bytes(newline_sample), encoding=encoding)
    family = _content_family(media_type, path)
    executable_risk = path.suffix.lower() in BLOCKED_EXECUTABLE_SUFFIXES or family in {
        "executable",
        "html",
        "svg",
        "archive",
    }
    inline_safe = (
        family in {"text", "markdown", "json"}
        or media_type in SAFE_INLINE_MEDIA_TYPES
    ) and not executable_risk
    return ArtifactObservedContent(
        path=path,
        size_bytes=size,
        sha256=digest.hexdigest(),
        content_type=media_type,
        content_family=family,
        encoding=encoding,
        byte_order_mark=bom,
        line_endings=line_endings,
        text_candidate=text_candidate,
        inline_safe=inline_safe,
        executable_risk=executable_risk,
    )


def expected_artifact_integrity(artifact: ArtifactRef) -> dict[str, Any]:
    metadata = artifact.metadata
    digest = str(metadata.get("sha256") or "").strip().lower()
    if digest and not re.fullmatch(r"[0-9a-f]{64}", digest):
        digest = ""
    revision = str(metadata.get("revision") or "").strip()
    size_value = metadata.get("size_bytes")
    size: int | None
    try:
        size = int(size_value) if size_value is not None else None
    except (TypeError, ValueError):
        size = None
    return {"sha256": digest or None, "revision": revision or None, "size_bytes": size}


def compare_artifact_integrity(
    artifact: ArtifactRef,
    observed: ArtifactObservedContent,
) -> str:
    expected = expected_artifact_integrity(artifact)
    if expected["size_bytes"] is not None and expected["size_bytes"] != observed.size_bytes:
        return "size_mismatch"
    if expected["sha256"] and expected["sha256"] != observed.sha256:
        return "digest_mismatch"
    if expected["revision"] and expected["revision"] != observed.revision:
        return "revision_mismatch"
    if not expected["sha256"] or not expected["revision"] or expected["size_bytes"] is None:
        return "legacy_incomplete"
    return "verified"


def detect_text_encoding(
    prefix: bytes,
    *,
    path: Path,
    kind: ArtifactKind | None,
) -> tuple[str | None, str | None]:
    for marker, encoding in _BOM_ENCODINGS:
        if prefix.startswith(marker):
            return encoding, marker.hex()
    textual = kind in TEXT_ARTIFACT_KINDS if kind is not None else False
    textual = textual or path.suffix.lower() in TEXT_SUFFIXES
    if not textual:
        if b"\x00" in prefix:
            return None, None
        if prefix:
            control = sum(
                1
                for value in prefix
                if value < 32 and value not in {9, 10, 12, 13}
            )
            if control / len(prefix) > 0.01:
                return None, None
    try:
        prefix.decode("utf-8", errors="strict")
        return "utf-8", None
    except UnicodeDecodeError:
        return (None, None) if not textual else ("utf-8", None)


def detect_line_endings(
    sample: bytes,
    *,
    encoding: str | None,
) -> tuple[str, ...]:
    if not sample or encoding is None:
        return ()
    try:
        text = sample.decode(encoding, errors="ignore")
    except LookupError:
        return ()
    endings: list[str] = []
    if "\r\n" in text:
        endings.append("crlf")
    without_crlf = text.replace("\r\n", "")
    if "\n" in without_crlf:
        endings.append("lf")
    if "\r" in without_crlf:
        endings.append("cr")
    return tuple(endings)


def normalize_text_encoding(value: str) -> str:
    selected = str(value or "utf-8").strip().lower().replace("_", "-")
    aliases = {
        "utf8": "utf-8",
        "utf-8": "utf-8",
        "utf-8-sig": "utf-8-sig",
        "utf16": "utf-16",
        "utf-16": "utf-16",
        "utf-16-le": "utf-16-le",
        "utf-16-be": "utf-16-be",
        "utf32": "utf-32",
        "utf-32": "utf-32",
        "utf-32-le": "utf-32-le",
        "utf-32-be": "utf-32-be",
    }
    if selected not in aliases:
        raise ValueError("artifact text encoding is not supported")
    return aliases[selected]


def safe_path_segment(value: str, label: str) -> str:
    selected = str(value or "").strip()
    if not selected or selected in {".", ".."}:
        raise ValueError(f"{label} identity must not be empty")
    if (
        "/" in selected
        or "\\" in selected
        or any(character in selected for character in '<>:"|?*')
        or not _SAFE_TOKEN.fullmatch(selected)
    ):
        raise ValueError(f"{label} identity is unsafe for artifact custody")
    return selected


def normalize_extension(value: str) -> str:
    selected = str(value or ".bin").strip().lower()
    if not selected.startswith("."):
        selected = f".{selected}"
    if not re.fullmatch(r"\.[a-z0-9][a-z0-9._-]{0,31}", selected):
        raise ValueError("artifact extension is invalid")
    return selected


def is_json_metadata_value(value: Any, *, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, (list, tuple)):
        return len(value) <= 256 and all(is_json_metadata_value(item, depth=depth + 1) for item in value)
    if isinstance(value, Mapping):
        return len(value) <= 256 and all(
            isinstance(key, str) and is_json_metadata_value(item, depth=depth + 1)
            for key, item in value.items()
        )
    return False


def _is_text_artifact(artifact: ArtifactRef, path: Path) -> bool:
    return artifact.kind in TEXT_ARTIFACT_KINDS or path.suffix.lower() in TEXT_SUFFIXES


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    overrides = {
        ".html": "text/html",
        ".htm": "text/html",
        ".json": "application/json",
        ".jsonl": "application/x-ndjson",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".svg": "image/svg+xml",
        ".tsx": "text/plain",
        ".ts": "text/plain",
        ".yaml": "application/yaml",
        ".yml": "application/yaml",
    }
    if suffix in overrides:
        return overrides[suffix]
    guessed, _ = mimetypes.guess_type(path.name, strict=False)
    if guessed:
        return guessed.lower()
    if suffix in TEXT_SUFFIXES:
        return "text/plain"
    return "application/octet-stream"


def _content_family(media_type: str, path: Path) -> str:
    selected = media_type.split(";", 1)[0].strip().lower()
    suffix = path.suffix.lower()
    if suffix in BLOCKED_EXECUTABLE_SUFFIXES:
        return "executable"
    if selected == "text/html":
        return "html"
    if selected == "image/svg+xml":
        return "svg"
    if selected in {"application/zip", "application/x-tar", "application/gzip", "application/x-7z-compressed"}:
        return "archive"
    if selected in {"application/json", "application/x-ndjson"}:
        return "json"
    if selected in {"text/markdown", "text/x-markdown"}:
        return "markdown"
    if selected.startswith("text/") or suffix in TEXT_SUFFIXES:
        return "text"
    if selected.startswith("image/"):
        return "image"
    if selected.startswith("audio/"):
        return "audio"
    if selected.startswith("video/"):
        return "video"
    if selected == "application/pdf":
        return "document"
    return "binary"


def _sync_parent_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sha256_stream(handle: BinaryIO, *, chunk_bytes: int = DEFAULT_READ_CHUNK_BYTES) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    bounded = max(1, min(MAX_READ_CHUNK_BYTES, int(chunk_bytes)))
    while True:
        chunk = handle.read(bounded)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size
