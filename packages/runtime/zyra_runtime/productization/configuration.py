from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tomllib
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any


CURRENT_CONFIG_VERSION = 1
CONFIG_SCHEMA = "zyra.runtime-config/v1"
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_PROFILE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SECRET_KEY = re.compile(
    r"(^|[_-])(secret|password|passwd|token|api[_-]?key|private[_-]?key|authorization)($|[_-])",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"^(?:sk-[A-Za-z0-9_-]{12,}|Bearer\s+\S+|Basic\s+\S+|-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----)"
)
_MISSING = object()


class ConfigurationError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str = "",
        source: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.source = source
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.configuration-error/v1",
            "code": self.code,
            "message": str(self),
            "path": self.path,
            "source": self.source,
            "details": _redact_mapping(self.details),
        }


@dataclass(frozen=True, slots=True)
class ConfigOrigin:
    source: str
    key: str
    precedence: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "key": self.key,
            "precedence": self.precedence,
        }


@dataclass(frozen=True, slots=True)
class ResolvedConfiguration:
    project_root: Path
    config_path: Path | None
    values: Mapping[str, Any]
    origins: Mapping[str, ConfigOrigin]
    digest: str
    warnings: tuple[str, ...] = ()

    def get(self, dotted_path: str, default: Any = None) -> Any:
        value: Any = self.values
        for part in _split_path(dotted_path):
            if not isinstance(value, Mapping) or part not in value:
                return default
            value = value[part]
        return _thaw(value)

    def require(self, dotted_path: str) -> Any:
        value = self.get(dotted_path, _MISSING)
        if value is _MISSING:
            raise ConfigurationError(
                "config_required_value_missing",
                f"required configuration value is missing: {dotted_path}",
                path=dotted_path,
            )
        return value

    def path(self, dotted_path: str) -> Path:
        value = self.require(dotted_path)
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationError(
                "config_path_invalid",
                f"configuration path is not a non-empty string: {dotted_path}",
                path=dotted_path,
            )
        return _resolve_path(
            value,
            project_root=self.project_root,
            config_path=self.config_path,
            source=self.origins.get(dotted_path),
        )

    def origin(self, dotted_path: str) -> ConfigOrigin | None:
        return self.origins.get(dotted_path)

    def public_projection(self) -> dict[str, Any]:
        projection = _redact_mapping(self.values)
        projection["schema"] = CONFIG_SCHEMA
        projection["digest"] = self.digest
        projection["config_path"] = (
            str(self.config_path.resolve()) if self.config_path is not None else None
        )
        projection["origins"] = {
            key: origin.to_dict() for key, origin in sorted(self.origins.items())
        }
        projection["warnings"] = list(self.warnings)
        return projection

    def state_paths(self) -> dict[str, Path]:
        return {
            name: self.path(f"state.{name}")
            for name in (
                "event_log",
                "database",
                "worker_pool",
                "graph",
                "fault",
                "recovery",
                "memory_index",
                "code_index",
                "workspace",
                "artifacts",
                "permission_compat",
                "permission",
                "mcp",
                "terminal",
                "control",
                "subagents",
                "gateway",
                "provider",
                "migration_journal",
                "lifecycle_log",
            )
        }


def _defaults() -> dict[str, Any]:
    return {
        "schema_version": CURRENT_CONFIG_VERSION,
        "profile": "default",
        "state": {
            "root": "tmp",
            "event_log": "tmp/events.jsonl",
            "database": "tmp/zyra.sqlite3",
            "worker_pool": "tmp/zyra.worker-pool.sqlite3",
            "graph": "tmp/zyra.graph-state.sqlite3",
            "fault": "tmp/zyra.fault-runtime.sqlite3",
            "recovery": "tmp/zyra.recovery-runtime.sqlite3",
            "memory_index": "tmp/zyra.memory-index.sqlite3",
            "code_index": "tmp/zyra-code-index",
            "workspace": "tmp/workspace",
            "artifacts": "tmp/artifacts",
            "permission_compat": "tmp/permissions.json",
            "permission": "tmp/artifacts/.permission/state.json",
            "mcp": "tmp/artifacts/.mcp/state.json",
            "terminal": "tmp/artifacts/.terminal/sessions.json",
            "control": "tmp/artifacts/.control",
            "subagents": "tmp/artifacts/.subagents",
            "gateway": "tmp/artifacts/.sandbox-gateway",
            "provider": "tmp/artifacts/.provider-control-plane/provider.sqlite3",
            "migration_journal": "tmp/.productization/migrations.sqlite3",
            "lifecycle_log": "tmp/.productization/lifecycle.jsonl",
        },
        "api": {
            "host": "127.0.0.1",
            "port": 8000,
            "cors_origins": [
                "http://127.0.0.1:5173",
                "http://localhost:5173",
            ],
        },
        "runtime": {
            "log_level": "INFO",
            "shutdown_timeout_seconds": 15.0,
            "migration_enabled": True,
            "readiness_requires_migration": True,
            "strict_optional_dependencies": True,
        },
        "features": {
            "provider_dispatch": False,
            "mcp": False,
            "edge_worker": False,
            "browser": True,
            "terminal": True,
            "web": True,
        },
        "credentials": {
            "required": {},
            "optional": {},
        },
        "migration": {
            "target_version": 1,
            "auto_apply": True,
            "legacy_roots": [],
            "backup_retention": 3,
            "lease_seconds": 120,
        },
    }


_ALLOWED_TREE: dict[str, Any] = {
    "schema_version": int,
    "profile": str,
    "state": {
        "root": str,
        "event_log": str,
        "database": str,
        "worker_pool": str,
        "graph": str,
        "fault": str,
        "recovery": str,
        "memory_index": str,
        "code_index": str,
        "workspace": str,
        "artifacts": str,
        "permission_compat": str,
        "permission": str,
        "mcp": str,
        "terminal": str,
        "control": str,
        "subagents": str,
        "gateway": str,
        "provider": str,
        "migration_journal": str,
        "lifecycle_log": str,
    },
    "api": {
        "host": str,
        "port": int,
        "cors_origins": list,
    },
    "runtime": {
        "log_level": str,
        "shutdown_timeout_seconds": (int, float),
        "migration_enabled": bool,
        "readiness_requires_migration": bool,
        "strict_optional_dependencies": bool,
    },
    "features": {
        "provider_dispatch": bool,
        "mcp": bool,
        "edge_worker": bool,
        "browser": bool,
        "terminal": bool,
        "web": bool,
    },
    "credentials": {
        "required": "environment_map",
        "optional": "environment_map",
    },
    "migration": {
        "target_version": int,
        "auto_apply": bool,
        "legacy_roots": list,
        "backup_retention": int,
        "lease_seconds": int,
    },
}


_ENV_BINDINGS: dict[str, tuple[str, Any]] = {
    "ZYRA_PROFILE": ("profile", str),
    "ZYRA_STATE_ROOT": ("state.root", str),
    "ZYRA_EVENT_LOG": ("state.event_log", str),
    "ZYRA_SQLITE_PATH": ("state.database", str),
    "ZYRA_WORKER_POOL_STORE": ("state.worker_pool", str),
    "ZYRA_GRAPH_STATE_STORE": ("state.graph", str),
    "ZYRA_FAULT_RUNTIME_STORE": ("state.fault", str),
    "ZYRA_RECOVERY_RUNTIME_STORE": ("state.recovery", str),
    "ZYRA_MEMORY_INDEX_PATH": ("state.memory_index", str),
    "ZYRA_CODE_INDEX_ROOT": ("state.code_index", str),
    "ZYRA_TOOL_WORKSPACE": ("state.workspace", str),
    "ZYRA_ARTIFACT_ROOT": ("state.artifacts", str),
    "ZYRA_PERMISSION_STORE": ("state.permission_compat", str),
    "ZYRA_PERMISSION_STATE": ("state.permission", str),
    "ZYRA_MCP_STATE": ("state.mcp", str),
    "ZYRA_TERMINAL_STATE": ("state.terminal", str),
    "ZYRA_CONTROL_STATE": ("state.control", str),
    "ZYRA_SUBAGENT_STATE": ("state.subagents", str),
    "ZYRA_SANDBOX_GATEWAY_STATE": ("state.gateway", str),
    "ZYRA_PROVIDER_STATE": ("state.provider", str),
    "ZYRA_MIGRATION_JOURNAL": ("state.migration_journal", str),
    "ZYRA_LIFECYCLE_LOG": ("state.lifecycle_log", str),
    "ZYRA_API_HOST": ("api.host", str),
    "ZYRA_API_PORT": ("api.port", int),
    "ZYRA_CORS_ORIGINS": ("api.cors_origins", "csv"),
    "ZYRA_LOG_LEVEL": ("runtime.log_level", str),
    "ZYRA_SHUTDOWN_TIMEOUT": ("runtime.shutdown_timeout_seconds", float),
    "ZYRA_MIGRATION_ENABLED": ("runtime.migration_enabled", bool),
    "ZYRA_READINESS_REQUIRES_MIGRATION": (
        "runtime.readiness_requires_migration",
        bool,
    ),
    "ZYRA_STRICT_OPTIONAL_DEPENDENCIES": (
        "runtime.strict_optional_dependencies",
        bool,
    ),
    "ZYRA_MIGRATION_AUTO_APPLY": ("migration.auto_apply", bool),
    "ZYRA_MIGRATION_TARGET": ("migration.target_version", int),
    "ZYRA_MIGRATION_BACKUP_RETENTION": ("migration.backup_retention", int),
    "ZYRA_MIGRATION_LEASE_SECONDS": ("migration.lease_seconds", int),
    "ZYRA_PROVIDER_DISPATCH_ENABLED": ("features.provider_dispatch", bool),
    "ZYRA_MCP_ENABLED": ("features.mcp", bool),
    "ZYRA_EDGE_WORKER_ENABLED": ("features.edge_worker", bool),
    "ZYRA_BROWSER_ENABLED": ("features.browser", bool),
    "ZYRA_TERMINAL_ENABLED": ("features.terminal", bool),
    "ZYRA_WEB_ENABLED": ("features.web", bool),
}


def load_runtime_configuration(
    project_root: Path | str,
    *,
    environ: Mapping[str, str] | None = None,
    config_path: Path | str | None = None,
    explicit_overrides: Mapping[str, Any] | None = None,
) -> ResolvedConfiguration:
    root = Path(project_root).resolve()
    environment = dict(os.environ if environ is None else environ)
    selected_path = _select_config_path(
        root,
        environment=environment,
        explicit=config_path,
    )
    values = _defaults()
    origins: dict[str, ConfigOrigin] = {}
    _record_leaf_origins(values, origins, source="defaults", precedence=0)

    warnings: list[str] = []
    if selected_path is not None:
        document = _load_document(selected_path)
        _validate_document(document, source=str(selected_path))
        _merge(
            values,
            document,
            origins=origins,
            source=f"file:{selected_path.resolve()}",
            precedence=10,
        )

    environment_overlay = _environment_overlay(environment)
    _merge(
        values,
        environment_overlay,
        origins=origins,
        source="environment",
        precedence=20,
    )

    if explicit_overrides:
        normalized = _normalize_overrides(explicit_overrides)
        _validate_partial_document(normalized, source="explicit")
        _merge(
            values,
            normalized,
            origins=origins,
            source="explicit",
            precedence=30,
        )

    _derive_paths(
        values,
        origins=origins,
        project_root=root,
        config_path=selected_path,
    )
    _validate_effective(values, project_root=root, config_path=selected_path)
    _reject_secret_material(values, source="effective")

    canonical = json.dumps(
        _redact_mapping(values),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return ResolvedConfiguration(
        project_root=root,
        config_path=selected_path,
        values=_freeze(values),
        origins=MappingProxyType(dict(origins)),
        digest=digest,
        warnings=tuple(warnings),
    )


def _select_config_path(
    root: Path,
    *,
    environment: Mapping[str, str],
    explicit: Path | str | None,
) -> Path | None:
    if explicit is not None:
        raw = str(explicit).strip()
        if not raw:
            raise ConfigurationError(
                "config_path_empty",
                "explicit configuration path cannot be empty",
                source="explicit",
            )
        path = Path(raw)
    else:
        raw = str(environment.get("ZYRA_CONFIG") or "").strip()
        if not raw:
            default = root / "config" / "zyra.toml"
            return default if default.is_file() else None
        path = Path(raw)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if not resolved.is_file():
        raise ConfigurationError(
            "config_file_missing",
            f"configuration file does not exist: {resolved}",
            source="explicit" if explicit is not None else "environment",
        )
    if resolved.suffix.casefold() not in {".toml", ".json"}:
        raise ConfigurationError(
            "config_format_unsupported",
            "configuration file must use .toml or .json",
            source=str(resolved),
        )
    return resolved


def _load_document(path: Path) -> dict[str, Any]:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ConfigurationError(
            "config_file_unreadable",
            f"cannot read configuration file: {path}",
            source=str(path),
            details={"error": type(error).__name__},
        ) from error
    if len(payload) > 1_048_576:
        raise ConfigurationError(
            "config_file_too_large",
            "configuration file exceeds the one MiB limit",
            source=str(path),
            details={"bytes": len(payload)},
        )
    try:
        if path.suffix.casefold() == ".toml":
            value = tomllib.loads(payload.decode("utf-8"))
        else:
            value = json.loads(payload)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(
            "config_parse_failed",
            f"cannot parse configuration file: {path}",
            source=str(path),
            details={"error": type(error).__name__, "message": str(error)[:512]},
        ) from error
    if not isinstance(value, dict):
        raise ConfigurationError(
            "config_root_invalid",
            "configuration root must be an object/table",
            source=str(path),
        )
    return value


def _validate_document(document: Mapping[str, Any], *, source: str) -> None:
    _validate_partial_document(document, source=source)
    version = document.get("schema_version")
    if version is None:
        raise ConfigurationError(
            "config_schema_version_missing",
            "configuration schema_version is required",
            path="schema_version",
            source=source,
        )
    if isinstance(version, bool) or not isinstance(version, int):
        raise ConfigurationError(
            "config_schema_version_invalid",
            "configuration schema_version must be an integer",
            path="schema_version",
            source=source,
        )
    if version != CURRENT_CONFIG_VERSION:
        code = (
            "config_schema_version_future"
            if version > CURRENT_CONFIG_VERSION
            else "config_schema_migration_required"
        )
        raise ConfigurationError(
            code,
            f"configuration schema version {version} is not supported",
            path="schema_version",
            source=source,
            details={"supported": CURRENT_CONFIG_VERSION, "actual": version},
        )


def _validate_partial_document(document: Mapping[str, Any], *, source: str) -> None:
    def visit(value: Mapping[str, Any], schema: Mapping[str, Any], prefix: str) -> None:
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise ConfigurationError(
                    "config_key_invalid",
                    "configuration keys must be non-empty strings",
                    path=prefix,
                    source=source,
                )
            path = f"{prefix}.{key}" if prefix else key
            expected = schema.get(key, _MISSING)
            if expected is _MISSING:
                raise ConfigurationError(
                    "config_unknown_key",
                    f"unknown configuration key: {path}",
                    path=path,
                    source=source,
                )
            if isinstance(expected, Mapping):
                if not isinstance(child, Mapping):
                    raise ConfigurationError(
                        "config_section_invalid",
                        f"configuration section must be an object/table: {path}",
                        path=path,
                        source=source,
                    )
                visit(child, expected, path)
                continue
            if expected == "environment_map":
                _validate_environment_map(child, path=path, source=source)
                continue
            if isinstance(child, bool) and expected is not bool:
                valid = False
            else:
                valid = isinstance(child, expected)
            if not valid:
                type_name = (
                    "|".join(item.__name__ for item in expected)
                    if isinstance(expected, tuple)
                    else expected.__name__
                )
                raise ConfigurationError(
                    "config_value_type_invalid",
                    f"configuration value {path} must be {type_name}",
                    path=path,
                    source=source,
                )

    visit(document, _ALLOWED_TREE, "")
    _reject_secret_material(document, source=source)


def _validate_environment_map(value: Any, *, path: str, source: str) -> None:
    if not isinstance(value, Mapping):
        raise ConfigurationError(
            "credential_binding_map_invalid",
            f"credential binding section must be an object/table: {path}",
            path=path,
            source=source,
        )
    for credential_id, environment_name in value.items():
        item_path = f"{path}.{credential_id}"
        if not isinstance(credential_id, str) or not _PROFILE_NAME.fullmatch(credential_id):
            raise ConfigurationError(
                "credential_id_invalid",
                f"invalid credential binding id: {credential_id!r}",
                path=item_path,
                source=source,
            )
        if not isinstance(environment_name, str) or not _ENVIRONMENT_NAME.fullmatch(
            environment_name
        ):
            raise ConfigurationError(
                "credential_environment_invalid",
                f"credential binding {credential_id} must name an uppercase environment variable",
                path=item_path,
                source=source,
            )


def _environment_overlay(environment: Mapping[str, str]) -> dict[str, Any]:
    overlay: dict[str, Any] = {}
    for environment_name, (path, parser) in _ENV_BINDINGS.items():
        if environment_name not in environment:
            continue
        raw = environment[environment_name]
        if not isinstance(raw, str):
            raise ConfigurationError(
                "config_environment_type_invalid",
                f"environment variable {environment_name} must be text",
                path=path,
                source="environment",
            )
        if parser is str:
            value: Any = raw.strip()
            if not value:
                raise ConfigurationError(
                    "config_environment_empty",
                    f"environment variable {environment_name} cannot be empty",
                    path=path,
                    source="environment",
                )
        elif parser is int:
            value = _parse_integer(raw, environment_name=environment_name, path=path)
        elif parser is float:
            value = _parse_float(raw, environment_name=environment_name, path=path)
        elif parser is bool:
            value = _parse_bool(raw, environment_name=environment_name, path=path)
        elif parser == "csv":
            value = [item.strip() for item in raw.split(",") if item.strip()]
        else:
            raise AssertionError(f"unsupported environment parser: {parser!r}")
        _set_path(overlay, path, value)
    return overlay


def _parse_integer(raw: str, *, environment_name: str, path: str) -> int:
    try:
        value = int(raw.strip(), 10)
    except ValueError as error:
        raise ConfigurationError(
            "config_environment_integer_invalid",
            f"environment variable {environment_name} must be an integer",
            path=path,
            source="environment",
        ) from error
    return value


def _parse_float(raw: str, *, environment_name: str, path: str) -> float:
    try:
        value = float(raw.strip())
    except ValueError as error:
        raise ConfigurationError(
            "config_environment_number_invalid",
            f"environment variable {environment_name} must be a number",
            path=path,
            source="environment",
        ) from error
    if value != value or value in {float("inf"), float("-inf")}:
        raise ConfigurationError(
            "config_environment_number_invalid",
            f"environment variable {environment_name} must be finite",
            path=path,
            source="environment",
        )
    return value


def _parse_bool(raw: str, *, environment_name: str, path: str) -> bool:
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(
        "config_environment_boolean_invalid",
        f"environment variable {environment_name} must be a boolean",
        path=path,
        source="environment",
    )


def _normalize_overrides(overrides: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in overrides.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigurationError(
                "config_override_key_invalid",
                "explicit override keys must be non-empty strings",
                source="explicit",
            )
        if "." in key:
            _set_path(normalized, key, copy.deepcopy(value))
            continue
        if isinstance(value, Mapping):
            normalized[key] = copy.deepcopy(dict(value))
            continue
        normalized[key] = copy.deepcopy(value)
    return normalized


def _derive_paths(
    values: MutableMapping[str, Any],
    *,
    origins: MutableMapping[str, ConfigOrigin],
    project_root: Path,
    config_path: Path | None,
) -> None:
    state = values["state"]
    if not isinstance(state, MutableMapping):
        raise AssertionError("validated state configuration is mutable mapping")
    root_value = str(state["root"])
    root = _resolve_path(
        root_value,
        project_root=project_root,
        config_path=config_path,
        source=origins.get("state.root"),
    )
    derivations = {
        "event_log": "events.jsonl",
        "database": "zyra.sqlite3",
        "worker_pool": "zyra.worker-pool.sqlite3",
        "graph": "zyra.graph-state.sqlite3",
        "fault": "zyra.fault-runtime.sqlite3",
        "recovery": "zyra.recovery-runtime.sqlite3",
        "memory_index": "zyra.memory-index.sqlite3",
        "code_index": "zyra-code-index",
        "workspace": "workspace",
        "artifacts": "artifacts",
        "permission_compat": "permissions.json",
        "migration_journal": ".productization/migrations.sqlite3",
        "lifecycle_log": ".productization/lifecycle.jsonl",
    }
    root_origin = origins.get("state.root", ConfigOrigin("defaults", "state.root", 0))
    for name, relative in derivations.items():
        leaf_path = f"state.{name}"
        origin = origins.get(leaf_path)
        if origin is not None and origin.precedence > root_origin.precedence:
            continue
        state[name] = str(root / relative)
        origins[leaf_path] = ConfigOrigin(
            source=f"derived:{root_origin.source}",
            key=leaf_path,
            precedence=root_origin.precedence,
        )
    artifact_root = _resolve_path(
        str(state["artifacts"]),
        project_root=project_root,
        config_path=config_path,
        source=origins.get("state.artifacts"),
    )
    artifact_derivations = {
        "permission": ".permission/state.json",
        "mcp": ".mcp/state.json",
        "terminal": ".terminal/sessions.json",
        "control": ".control",
        "subagents": ".subagents",
        "gateway": ".sandbox-gateway",
        "provider": ".provider-control-plane/provider.sqlite3",
    }
    artifact_origin = origins.get(
        "state.artifacts", ConfigOrigin("defaults", "state.artifacts", 0)
    )
    for name, relative in artifact_derivations.items():
        leaf_path = f"state.{name}"
        origin = origins.get(leaf_path)
        if origin is not None and origin.precedence > artifact_origin.precedence:
            continue
        state[name] = str(artifact_root / relative)
        origins[leaf_path] = ConfigOrigin(
            source=f"derived:{artifact_origin.source}",
            key=leaf_path,
            precedence=artifact_origin.precedence,
        )


def _validate_effective(
    values: Mapping[str, Any],
    *,
    project_root: Path,
    config_path: Path | None,
) -> None:
    version = values.get("schema_version")
    if version != CURRENT_CONFIG_VERSION:
        raise ConfigurationError(
            "config_schema_version_invalid",
            f"effective configuration version must be {CURRENT_CONFIG_VERSION}",
            path="schema_version",
        )
    profile = values.get("profile")
    if not isinstance(profile, str) or not _PROFILE_NAME.fullmatch(profile):
        raise ConfigurationError(
            "config_profile_invalid",
            "profile must start with a lowercase letter and contain only lowercase letters, digits, '_' or '-'",
            path="profile",
        )
    api = values["api"]
    host = str(api["host"]).strip()
    if not host or any(character.isspace() for character in host):
        raise ConfigurationError(
            "config_api_host_invalid",
            "api.host must be a non-empty host without whitespace",
            path="api.host",
        )
    port = api["port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
        raise ConfigurationError(
            "config_api_port_invalid",
            "api.port must be between 1 and 65535",
            path="api.port",
        )
    origins = api["cors_origins"]
    if not isinstance(origins, list) or any(
        not isinstance(item, str)
        or not item
        or item == "*"
        or "\r" in item
        or "\n" in item
        for item in origins
    ):
        raise ConfigurationError(
            "config_cors_origins_invalid",
            "api.cors_origins must contain explicit non-wildcard origins",
            path="api.cors_origins",
        )
    if len(set(origins)) != len(origins):
        raise ConfigurationError(
            "config_cors_origins_duplicate",
            "api.cors_origins contains duplicate values",
            path="api.cors_origins",
        )
    runtime = values["runtime"]
    level = str(runtime["log_level"]).upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError(
            "config_log_level_invalid",
            "runtime.log_level is invalid",
            path="runtime.log_level",
        )
    timeout = float(runtime["shutdown_timeout_seconds"])
    if timeout <= 0 or timeout > 300:
        raise ConfigurationError(
            "config_shutdown_timeout_invalid",
            "runtime.shutdown_timeout_seconds must be in (0, 300]",
            path="runtime.shutdown_timeout_seconds",
        )
    migration = values["migration"]
    target = migration["target_version"]
    if isinstance(target, bool) or not isinstance(target, int) or target < 1:
        raise ConfigurationError(
            "config_migration_target_invalid",
            "migration.target_version must be a positive integer",
            path="migration.target_version",
        )
    retention = migration["backup_retention"]
    if isinstance(retention, bool) or not isinstance(retention, int) or not 1 <= retention <= 32:
        raise ConfigurationError(
            "config_backup_retention_invalid",
            "migration.backup_retention must be between 1 and 32",
            path="migration.backup_retention",
        )
    lease = migration["lease_seconds"]
    if isinstance(lease, bool) or not isinstance(lease, int) or not 5 <= lease <= 3600:
        raise ConfigurationError(
            "config_migration_lease_invalid",
            "migration.lease_seconds must be between 5 and 3600",
            path="migration.lease_seconds",
        )
    legacy_roots = migration["legacy_roots"]
    if not isinstance(legacy_roots, list) or any(
        not isinstance(item, str) or not item.strip() for item in legacy_roots
    ):
        raise ConfigurationError(
            "config_legacy_roots_invalid",
            "migration.legacy_roots must be a list of non-empty paths",
            path="migration.legacy_roots",
        )
    normalized_roots: set[str] = set()
    for item in legacy_roots:
        path = _resolve_path(
            item,
            project_root=project_root,
            config_path=config_path,
            source=None,
        )
        rendered = str(path).casefold()
        if rendered in normalized_roots:
            raise ConfigurationError(
                "config_legacy_root_duplicate",
                "migration.legacy_roots contains duplicate paths",
                path="migration.legacy_roots",
            )
        normalized_roots.add(rendered)
    state = values["state"]
    resolved_paths: dict[str, Path] = {}
    for name, raw in state.items():
        if not isinstance(raw, str) or not raw.strip():
            raise ConfigurationError(
                "config_state_path_invalid",
                f"state.{name} must be a non-empty path",
                path=f"state.{name}",
            )
        resolved_paths[name] = _resolve_path(
            raw,
            project_root=project_root,
            config_path=config_path,
            source=None,
        )
    journal = resolved_paths["migration_journal"]
    lifecycle = resolved_paths["lifecycle_log"]
    for name, selected in resolved_paths.items():
        if name in {"migration_journal", "lifecycle_log"}:
            continue
        if selected in {journal, lifecycle}:
            raise ConfigurationError(
                "config_state_path_collision",
                f"state.{name} collides with productization control state",
                path=f"state.{name}",
            )
    required = values["credentials"]["required"]
    features = values["features"]
    feature_requirements = {
        "provider_dispatch": "provider",
        "mcp": "mcp",
        "edge_worker": "edge",
    }
    for feature, credential_id in feature_requirements.items():
        if features[feature] and credential_id not in required:
            raise ConfigurationError(
                "config_feature_credential_binding_missing",
                f"enabled feature {feature} requires credentials.required.{credential_id}",
                path=f"features.{feature}",
            )


def _resolve_path(
    raw: str,
    *,
    project_root: Path,
    config_path: Path | None,
    source: ConfigOrigin | None,
) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path.resolve()
    base = project_root
    if (
        source is not None
        and source.source.startswith("file:")
        and config_path is not None
    ):
        base = config_path.parent
    candidate = (base / path).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError as error:
        raise ConfigurationError(
            "config_relative_path_escape",
            f"relative configuration path escapes its base: {raw}",
            path=source.key if source is not None else "",
            source=source.source if source is not None else "",
        ) from error
    return candidate


def _reject_secret_material(value: Any, *, source: str, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            rendered = str(key)
            child_path = f"{path}.{rendered}" if path else rendered
            if _SECRET_KEY.search(rendered) and not child_path.startswith("credentials."):
                raise ConfigurationError(
                    "config_secret_key_forbidden",
                    f"secret-bearing configuration key is forbidden: {child_path}",
                    path=child_path,
                    source=source,
                )
            _reject_secret_material(child, source=source, path=child_path)
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_secret_material(child, source=source, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and _SECRET_VALUE.search(value.strip()):
        raise ConfigurationError(
            "config_secret_material_forbidden",
            f"credential material cannot be stored in configuration: {path}",
            path=path,
            source=source,
        )


def _redact_mapping(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            rendered = str(key)
            if _SECRET_KEY.search(rendered):
                result[rendered] = "[REDACTED]"
            else:
                result[rendered] = _redact_mapping(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact_mapping(item) for item in value]
    if isinstance(value, str) and _SECRET_VALUE.search(value.strip()):
        return "[REDACTED]"
    return copy.deepcopy(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    if isinstance(value, tuple):
        return tuple(_freeze(child) for child in value)
    return copy.deepcopy(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(child) for child in value]
    return copy.deepcopy(value)


def _merge(
    target: MutableMapping[str, Any],
    overlay: Mapping[str, Any],
    *,
    origins: MutableMapping[str, ConfigOrigin],
    source: str,
    precedence: int,
    prefix: str = "",
) -> None:
    for key, child in overlay.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(child, Mapping):
            current = target.get(key)
            if not isinstance(current, MutableMapping):
                current = {}
                target[key] = current
            _merge(
                current,
                child,
                origins=origins,
                source=source,
                precedence=precedence,
                prefix=path,
            )
            continue
        target[key] = copy.deepcopy(child)
        origins[path] = ConfigOrigin(source=source, key=path, precedence=precedence)


def _record_leaf_origins(
    value: Mapping[str, Any],
    origins: MutableMapping[str, ConfigOrigin],
    *,
    source: str,
    precedence: int,
    prefix: str = "",
) -> None:
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(child, Mapping):
            _record_leaf_origins(
                child,
                origins,
                source=source,
                precedence=precedence,
                prefix=path,
            )
        else:
            origins[path] = ConfigOrigin(source=source, key=path, precedence=precedence)


def _set_path(target: MutableMapping[str, Any], dotted_path: str, value: Any) -> None:
    parts = _split_path(dotted_path)
    current = target
    for part in parts[:-1]:
        child = current.get(part)
        if child is None:
            child = {}
            current[part] = child
        if not isinstance(child, MutableMapping):
            raise ConfigurationError(
                "config_override_conflict",
                f"configuration override conflicts with scalar value: {dotted_path}",
                path=dotted_path,
            )
        current = child
    current[parts[-1]] = value


def _split_path(dotted_path: str) -> tuple[str, ...]:
    parts = tuple(item for item in dotted_path.split(".") if item)
    if not parts:
        raise ConfigurationError(
            "config_path_invalid",
            "configuration path cannot be empty",
            path=dotted_path,
        )
    return parts


def describe_environment_bindings() -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "environment": environment_name,
            "config_path": path,
            "parser": (
                parser
                if isinstance(parser, str)
                else getattr(parser, "__name__", str(parser))
            ),
        }
        for environment_name, (path, parser) in sorted(_ENV_BINDINGS.items())
    )


def known_state_path_names() -> tuple[str, ...]:
    state = _defaults()["state"]
    return tuple(sorted(state))


def environment_names_for_paths(paths: Iterable[str]) -> tuple[str, ...]:
    selected = set(paths)
    return tuple(
        environment_name
        for environment_name, (path, _) in sorted(_ENV_BINDINGS.items())
        if path in selected
    )
