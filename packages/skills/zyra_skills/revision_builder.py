from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from .digests import (
    digest_object,
    digest_resource_manifest,
    digest_skill_package,
    estimate_tokens,
    sha256_bytes,
    sha256_text,
)
from .frontmatter import ParsedSkillDocument, parse_skill_document
from .models import (
    SKILL_SCHEMA,
    SkillBodyDescriptor,
    SkillProvenance,
    SkillResourceDescriptor,
    SkillResourceKind,
    SkillRevision,
    SkillVersionRef,
)
from .path_security import normalize_relative_path, read_verified_bytes, read_verified_text, resolve_member


DEFAULT_SKILL_FILE_LIMIT = 2_000_000


def build_revision(
    *,
    skill_root: str | Path,
    provenance: SkillProvenance,
    generation: int,
) -> SkillRevision:
    root = Path(skill_root).resolve(strict=True)
    skill_file = resolve_member(root, "SKILL.md")
    document_text = read_verified_text(
        skill_file,
        root=root,
        max_bytes=DEFAULT_SKILL_FILE_LIMIT,
    )
    parsed = parse_skill_document(document_text, expected_name=root.name)
    return build_revision_from_document(
        root=root,
        skill_file=skill_file,
        parsed=parsed,
        provenance=provenance,
        generation=generation,
    )


def build_revision_from_document(
    *,
    root: Path,
    skill_file: Path,
    parsed: ParsedSkillDocument,
    provenance: SkillProvenance,
    generation: int,
) -> SkillRevision:
    body_bytes = parsed.body.encode("utf-8")
    body_digest = sha256_bytes(body_bytes)
    body_descriptor = SkillBodyDescriptor(
        size_bytes=len(body_bytes),
        token_estimate=estimate_tokens(body_bytes),
        digest=body_digest,
        immutable_ref="",
        line_count=len(parsed.body.splitlines()),
    )
    resources = tuple(
        _resource_descriptor(root, relative, parsed.metadata.context_budget.max_resource_bytes)
        for relative in parsed.metadata.resources
    )
    if len(resources) > parsed.metadata.context_budget.max_resources:
        raise ValueError("skill declares more resources than its context budget permits")
    total_resource_bytes = sum(item.size_bytes for item in resources)
    if total_resource_bytes > parsed.metadata.context_budget.max_total_resource_bytes:
        raise ValueError("skill resource manifest exceeds total byte budget")
    manifest_digest = digest_resource_manifest(item.to_dict() for item in resources)
    policy_digest = digest_object(
        {
            "allowed_tools": None
            if parsed.metadata.allowed_tools is None
            else [item.to_dict() for item in parsed.metadata.allowed_tools],
            "hooks": [item.to_dict() for item in parsed.metadata.hooks],
            "invocation": parsed.metadata.invocation.to_dict(),
            "path_conditions": list(parsed.metadata.path_conditions),
        }
    )
    provenance_digest = digest_object(provenance.to_dict())
    content_digest = digest_skill_package(
        metadata=parsed.metadata.to_dict(),
        body_digest=body_digest,
        resource_manifest_digest=manifest_digest,
        schema=SKILL_SCHEMA,
    )
    qualified_name = qualified_skill_name(parsed.metadata.name, provenance)
    skill_id = digest_object(
        {
            "source_id": provenance.source_id,
            "qualified_name": qualified_name,
            "canonical_root": provenance.canonical_root,
        }
    )[:32]
    version_ref = SkillVersionRef(
        skill_id=skill_id,
        qualified_name=qualified_name,
        source_id=provenance.source_id,
        declared_version=parsed.metadata.declared_version,
        content_digest=content_digest,
        body_digest=body_digest,
        resource_manifest_digest=manifest_digest,
        policy_digest=policy_digest,
        provenance_digest=provenance_digest,
        registry_generation=generation,
        revocation_epoch=0,
    )
    body_descriptor = SkillBodyDescriptor(
        size_bytes=body_descriptor.size_bytes,
        token_estimate=body_descriptor.token_estimate,
        digest=body_descriptor.digest,
        immutable_ref=f"{version_ref.immutable_ref}#body",
        line_count=body_descriptor.line_count,
    )
    resources = tuple(
        SkillResourceDescriptor(
            relative_path=item.relative_path,
            kind=item.kind,
            media_type=item.media_type,
            size_bytes=item.size_bytes,
            digest=item.digest,
            immutable_ref=f"{version_ref.immutable_ref}#resource/{item.relative_path}",
        )
        for item in resources
    )
    return SkillRevision(
        metadata=parsed.metadata,
        provenance=provenance,
        version_ref=version_ref,
        body=body_descriptor,
        resources=resources,
        skill_root=str(root),
        skill_file=str(skill_file),
    )


def qualified_skill_name(name: str, provenance: SkillProvenance) -> str:
    namespace = provenance.source_namespace.strip(":")
    return f"{namespace}:{name}" if namespace else name


def _resource_descriptor(root: Path, relative_path: str, max_bytes: int) -> SkillResourceDescriptor:
    normalized = normalize_relative_path(relative_path)
    path = resolve_member(root, normalized)
    payload = read_verified_bytes(path, root=root, max_bytes=max_bytes)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    kind = _resource_kind(normalized, media_type)
    if kind is not SkillResourceKind.JSON and media_type == "application/octet-stream":
        # Foundation supports read-only text resources. Binary artifacts belong
        # in the artifact store and are referenced by immutable artifact refs.
        try:
            payload.decode("utf-8")
            media_type = "text/plain"
        except UnicodeDecodeError as error:
            raise ValueError(f"binary skill resource is not supported: {normalized}") from error
    return SkillResourceDescriptor(
        relative_path=normalized,
        kind=kind,
        media_type=media_type,
        size_bytes=len(payload),
        digest=sha256_bytes(payload),
        immutable_ref="",
    )


def _resource_kind(relative_path: str, media_type: str) -> SkillResourceKind:
    lowered = relative_path.lower()
    if lowered.endswith(".schema.json"):
        return SkillResourceKind.SCHEMA
    if lowered.endswith(".json") or media_type == "application/json":
        return SkillResourceKind.JSON
    if "/templates/" in f"/{lowered}" or lowered.startswith("templates/"):
        return SkillResourceKind.TEMPLATE
    if "/references/" in f"/{lowered}" or lowered.startswith("references/"):
        return SkillResourceKind.REFERENCE
    return SkillResourceKind.TEXT
