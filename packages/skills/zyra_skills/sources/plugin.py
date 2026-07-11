from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..digests import digest_object
from ..errors import PluginManifestError, PluginSecretSubstitutionError
from ..models import SkillSourceKind
from ..path_security import canonical_root, normalize_relative_path, read_verified_text, resolve_member
from .base import SkillSourceScan, SkillSourceState
from .filesystem import FilesystemSkillSource


PLUGIN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
VARIABLE_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*|user_config\.[A-Za-z0-9_.-]+)\}")


@dataclass(frozen=True, slots=True)
class PluginUserConfigField:
    key: str
    sensitive: bool = False
    required: bool = False
    default: str = ""


@dataclass(frozen=True, slots=True)
class PluginManifest:
    plugin_id: str
    version: str
    skills_paths: tuple[str, ...] = ("skills",)
    enabled: bool = True
    user_config: tuple[PluginUserConfigField, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not PLUGIN_ID_PATTERN.fullmatch(self.plugin_id):
            raise PluginManifestError("invalid plugin id", detail={"plugin_id": self.plugin_id})
        if not self.version.strip():
            raise PluginManifestError("plugin version is required")
        for path in self.skills_paths:
            normalize_relative_path(path, allow_directory=True)

    @classmethod
    def load(cls, plugin_root: str | Path) -> "PluginManifest":
        root = canonical_root(plugin_root)
        manifest_path = root / ".zyra-plugin" / "plugin.json"
        if not manifest_path.exists():
            return cls(plugin_id=root.name.lower(), version="0", skills_paths=("skills",))
        manifest_path = resolve_member(root, ".zyra-plugin/plugin.json")
        raw = read_verified_text(manifest_path, root=root, max_bytes=512_000)
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as error:
            raise PluginManifestError("plugin manifest is not valid JSON") from error
        if not isinstance(item, dict):
            raise PluginManifestError("plugin manifest must be a JSON object")
        allowed = {"id", "name", "version", "skills", "enabled", "user_config", "metadata"}
        unknown = set(item) - allowed
        if unknown:
            raise PluginManifestError("plugin manifest has unknown fields", detail={"fields": sorted(unknown)})
        raw_skills = item.get("skills", ["skills"])
        if isinstance(raw_skills, str):
            skills = (raw_skills,)
        elif isinstance(raw_skills, list) and all(isinstance(value, str) for value in raw_skills):
            skills = tuple(raw_skills)
        else:
            raise PluginManifestError("plugin skills must be a path or list of paths")
        fields: list[PluginUserConfigField] = []
        raw_config = item.get("user_config", {})
        if not isinstance(raw_config, dict):
            raise PluginManifestError("plugin user_config must be an object")
        for key, descriptor in raw_config.items():
            if not isinstance(descriptor, dict):
                raise PluginManifestError("plugin user config descriptor must be an object", detail={"key": key})
            fields.append(
                PluginUserConfigField(
                    key=str(key),
                    sensitive=bool(descriptor.get("sensitive", False)),
                    required=bool(descriptor.get("required", False)),
                    default=str(descriptor.get("default") or ""),
                )
            )
        return cls(
            plugin_id=str(item.get("id") or item.get("name") or root.name).lower(),
            version=str(item.get("version") or "0"),
            skills_paths=skills,
            enabled=bool(item.get("enabled", True)),
            user_config=tuple(fields),
            metadata=dict(item.get("metadata") or {}),
        )

    def digest(self) -> str:
        return digest_object(
            {
                "plugin_id": self.plugin_id,
                "version": self.version,
                "skills_paths": self.skills_paths,
                "enabled": self.enabled,
                "user_config": [asdict(item) for item in self.user_config],
                "metadata": self.metadata,
            }
        )


@dataclass(slots=True)
class PluginCapabilitySource:
    plugin_root: Path
    source_id: str = ""
    configured_order: int = 0
    strict: bool = True
    source_kind: SkillSourceKind = field(default=SkillSourceKind.PLUGIN, init=False)

    def __post_init__(self) -> None:
        self.plugin_root = Path(self.plugin_root)
        if not self.source_id:
            self.source_id = f"plugin:{self.plugin_root.name.lower()}"

    def scan(self, *, generation: int) -> SkillSourceScan:
        root = canonical_root(self.plugin_root)
        manifest = PluginManifest.load(root)
        if not manifest.enabled:
            return SkillSourceScan(
                source_id=self.source_id,
                source_kind=self.source_kind,
                state=SkillSourceState.DISABLED,
                revisions=(),
                metadata={"plugin_id": manifest.plugin_id, "plugin_version": manifest.version},
            )
        revisions = []
        errors: list[dict[str, Any]] = []
        for index, relative in enumerate(manifest.skills_paths):
            try:
                normalized = normalize_relative_path(relative, allow_directory=True)
                path = root.joinpath(*normalized.split("/"))
                path = canonical_root(path)
                source = FilesystemSkillSource(
                    root=path,
                    source_kind=SkillSourceKind.PLUGIN,
                    source_id=self.source_id,
                    namespace=f"plugin:{manifest.plugin_id}",
                    configured_order=self.configured_order * 100 + index,
                    strict=self.strict,
                    origin_uri=root.as_uri(),
                )
                scan = source.scan(generation=generation)
                if not scan.ok:
                    errors.extend(scan.errors)
                    continue
                for revision in scan.revisions:
                    provenance = revision.provenance
                    patched = type(provenance)(
                        **{
                            **provenance.to_dict(),
                            "source_kind": SkillSourceKind.PLUGIN,
                            "trust_tier": provenance.trust_tier,
                            "plugin_id": manifest.plugin_id,
                            "plugin_version": manifest.version,
                            "metadata": {
                                **provenance.metadata,
                                "manifest_digest": manifest.digest(),
                                "cache_only": True,
                            },
                        }
                    )
                    # Rebuild so provenance digest and immutable package identity
                    # are bound to the plugin id/version, not just filesystem path.
                    from ..revision_builder import build_revision

                    revisions.append(
                        build_revision(
                            skill_root=revision.skill_root,
                            provenance=patched,
                            generation=generation,
                        )
                    )
            except Exception as error:  # noqa: BLE001 - atomic scan aggregates component errors.
                errors.append(
                    {
                        "code": str(getattr(error, "code", "plugin_skill_invalid")),
                        "message": str(error),
                        "detail": {"skills_path": relative, **dict(getattr(error, "detail", {}) or {})},
                    }
                )
        if errors and self.strict:
            return SkillSourceScan(
                source_id=self.source_id,
                source_kind=self.source_kind,
                state=SkillSourceState.RELOAD_REJECTED,
                revisions=(),
                errors=tuple(errors),
                metadata={"plugin_id": manifest.plugin_id, "plugin_version": manifest.version},
            )
        return SkillSourceScan(
            source_id=self.source_id,
            source_kind=self.source_kind,
            state=SkillSourceState.STAGED,
            revisions=tuple(revisions),
            errors=tuple(errors),
            metadata={
                "plugin_id": manifest.plugin_id,
                "plugin_version": manifest.version,
                "manifest_digest": manifest.digest(),
                "cache_only": True,
            },
        )


def substitute_plugin_content(
    content: str,
    *,
    plugin_root: str | Path,
    plugin_data_root: str | Path,
    manifest: PluginManifest,
    user_config: Mapping[str, str],
    sink: str,
) -> str:
    fields = {field.key: field for field in manifest.user_config}
    values = {field.key: user_config.get(field.key, field.default) for field in manifest.user_config}
    for field in manifest.user_config:
        if field.required and not values[field.key]:
            raise PluginSecretSubstitutionError("required plugin configuration is missing", detail={"key": field.key})

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key == "ZYRA_PLUGIN_ROOT":
            return str(Path(plugin_root).resolve())
        if key == "ZYRA_PLUGIN_DATA":
            return str(Path(plugin_data_root).resolve())
        if not key.startswith("user_config."):
            return match.group(0)
        config_key = key.removeprefix("user_config.")
        descriptor = fields.get(config_key)
        if descriptor is None:
            return match.group(0)
        if descriptor.sensitive and sink == "model":
            return f"<secret:{config_key}:redacted>"
        if descriptor.sensitive and sink not in {"hook_env", "mcp_env"}:
            raise PluginSecretSubstitutionError("sensitive plugin value requested by an unsafe sink", detail={"key": config_key, "sink": sink})
        return values.get(config_key, "")

    return VARIABLE_PATTERN.sub(replace, content)
