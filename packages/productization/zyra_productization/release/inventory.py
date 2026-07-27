from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import re
import sys
import sysconfig
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import ConfigurationViolation, InventoryViolation
from .integrity import (
    BunLock,
    CanonicalTreeWalker,
    PythonLock,
    normalize_relative_path,
    resolve_below,
    sha256_file,
    stable_digest,
)
from .models import FileKind
from .policy import DEFAULT_RELEASE_POLICY, ReleasePolicy


_SPDX = re.compile(r"^[A-Za-z0-9.+-]+(?:\s+(?:AND|OR)\s+[A-Za-z0-9.+-]+)*$")
_SECRET_REFERENCE = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True, slots=True)
class Component:
    component_type: str
    name: str
    version: str
    purl: str
    licenses: tuple[str, ...]
    hashes: tuple[dict[str, str], ...]
    properties: tuple[dict[str, str], ...]
    dependencies: tuple[str, ...] = ()

    @property
    def bom_ref(self) -> str:
        return self.purl or f"zyra:{self.component_type}:{self.name}@{self.version}"

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "type": self.component_type,
            "bom-ref": self.bom_ref,
            "name": self.name,
            "version": self.version,
            "properties": list(self.properties),
        }
        if self.purl:
            value["purl"] = self.purl
        if self.licenses:
            value["licenses"] = [
                {"license": {"id": item}} for item in self.licenses
            ]
        if self.hashes:
            value["hashes"] = list(self.hashes)
        return value


class LicenseNormalizer:
    _ALIASES = {
        "apache 2": "Apache-2.0",
        "apache 2.0": "Apache-2.0",
        "apache software license": "Apache-2.0",
        "bsd": "BSD-3-Clause",
        "bsd license": "BSD-3-Clause",
        "isc license": "ISC",
        "mit": "MIT",
        "mit license": "MIT",
        "mozilla public license 2.0": "MPL-2.0",
        "python software foundation license": "PSF-2.0",
        "the unlicense": "Unlicense",
    }

    def normalize(self, value: str) -> tuple[str, ...]:
        raw = value.strip()
        if not raw:
            return ()
        if _SPDX.fullmatch(raw):
            tokens = [
                item
                for item in re.split(r"\s+(?:AND|OR)\s+", raw)
                if item
            ]
            return tuple(dict.fromkeys(tokens))
        fragments = re.split(r"\s*(?:,|/|\bor\b|\band\b)\s*", raw, flags=re.IGNORECASE)
        output: list[str] = []
        for fragment in fragments:
            cleaned = fragment.strip("() ")
            if not cleaned:
                continue
            normalized = self._ALIASES.get(cleaned.casefold())
            if normalized is None:
                normalized = f"LicenseRef-{re.sub(r'[^A-Za-z0-9.-]+', '-', cleaned).strip('-')}"
            if normalized not in output:
                output.append(normalized)
        return tuple(output)


class PythonComponentInventory:
    def __init__(
        self,
        lock: PythonLock,
        *,
        metadata_provider: Any = importlib.metadata.metadata,
    ) -> None:
        self.lock = lock
        self.metadata_provider = metadata_provider
        self.licenses = LicenseNormalizer()

    def build(self) -> tuple[Component, ...]:
        components: list[Component] = []
        for requirement in sorted(
            self.lock.records,
            key=lambda item: item.canonical_name,
        ):
            metadata: Mapping[str, Any]
            try:
                metadata = self.metadata_provider(requirement.name)
            except importlib.metadata.PackageNotFoundError:
                metadata = {}
            license_value = self._metadata_value(metadata, "License")
            classifiers = self._metadata_values(metadata, "Classifier")
            normalized_licenses = list(self.licenses.normalize(license_value))
            for classifier in classifiers:
                marker = "License ::"
                if marker not in classifier:
                    continue
                tail = classifier.split("::")[-1].strip()
                for item in self.licenses.normalize(tail):
                    if item not in normalized_licenses:
                        normalized_licenses.append(item)
            properties = [
                {"name": "zyra:ecosystem", "value": "python"},
                {
                    "name": "zyra:marker",
                    "value": requirement.marker,
                },
                {
                    "name": "zyra:extras",
                    "value": ",".join(requirement.extras),
                },
            ]
            hashes = tuple(
                {"alg": "SHA-256", "content": digest}
                for digest in requirement.hashes
            )
            dependencies = self._requires_dist(metadata)
            components.append(
                Component(
                    component_type="library",
                    name=requirement.canonical_name,
                    version=requirement.version,
                    purl=f"pkg:pypi/{requirement.canonical_name}@{requirement.version}",
                    licenses=tuple(normalized_licenses),
                    hashes=hashes,
                    properties=tuple(properties),
                    dependencies=dependencies,
                )
            )
        return tuple(components)

    @staticmethod
    def _metadata_value(metadata: Mapping[str, Any], name: str) -> str:
        value = metadata.get(name)
        return str(value or "")

    @staticmethod
    def _metadata_values(metadata: Mapping[str, Any], name: str) -> tuple[str, ...]:
        getter = getattr(metadata, "get_all", None)
        if callable(getter):
            return tuple(str(item) for item in (getter(name) or ()))
        value = metadata.get(name)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return tuple(str(item) for item in value)
        return (str(value),) if value else ()

    @classmethod
    def _requires_dist(cls, metadata: Mapping[str, Any]) -> tuple[str, ...]:
        raw = cls._metadata_values(metadata, "Requires-Dist")
        output: list[str] = []
        for item in raw:
            match = re.match(r"^([A-Za-z0-9._-]+)", item)
            if match is None:
                continue
            name = match.group(1).lower().replace("_", "-").replace(".", "-")
            if name not in output:
                output.append(name)
        return tuple(output)


class JavaScriptComponentInventory:
    def __init__(self, root: Path, lock: BunLock) -> None:
        self.root = root.resolve()
        self.lock = lock
        self.licenses = LicenseNormalizer()

    def build(self) -> tuple[Component, ...]:
        root_manifest = self._manifest(self.root / "package.json")
        workspaces = root_manifest.get("workspaces", ())
        manifests: list[tuple[str, Mapping[str, Any]]] = [(".", root_manifest)]
        if isinstance(workspaces, Sequence) and not isinstance(workspaces, (str, bytes)):
            for workspace in sorted(str(item) for item in workspaces):
                normalized = normalize_relative_path(workspace)
                manifest_path = resolve_below(self.root, normalized) / "package.json"
                manifests.append((normalized, self._manifest(manifest_path)))
        components: dict[str, Component] = {}
        for workspace, manifest in manifests:
            package_name = str(manifest.get("name") or PurePosixPath(workspace).name)
            package_version = str(manifest.get("version") or "0.0.0")
            license_value = str(manifest.get("license") or "")
            dependencies = self._dependency_names(manifest)
            component = Component(
                component_type="application" if workspace == "." else "library",
                name=package_name,
                version=package_version,
                purl=f"pkg:npm/{package_name.replace('@', '%40')}@{package_version}",
                licenses=self.licenses.normalize(license_value),
                hashes=(),
                properties=(
                    {"name": "zyra:ecosystem", "value": "javascript"},
                    {"name": "zyra:workspace", "value": workspace},
                ),
                dependencies=dependencies,
            )
            components[component.bom_ref] = component
            for field in ("dependencies", "devDependencies", "optionalDependencies"):
                raw = manifest.get(field, {})
                if not isinstance(raw, Mapping):
                    continue
                for name, version in raw.items():
                    text = str(version)
                    normalized_version = text.lstrip("^~>=<")
                    if text.startswith("workspace:"):
                        normalized_version = text.split(":", 1)[1].lstrip("^~")
                    dependency = Component(
                        component_type="library",
                        name=str(name),
                        version=normalized_version or "unknown",
                        purl=(
                            f"pkg:npm/{str(name).replace('@', '%40')}"
                            f"@{normalized_version or 'unknown'}"
                        ),
                        licenses=(),
                        hashes=(),
                        properties=(
                            {"name": "zyra:ecosystem", "value": "javascript"},
                            {"name": "zyra:declared-by", "value": workspace},
                            {"name": "zyra:constraint", "value": text},
                        ),
                    )
                    components.setdefault(dependency.bom_ref, dependency)
        return tuple(
            sorted(components.values(), key=lambda item: item.bom_ref)
        )

    @staticmethod
    def _manifest(path: Path) -> Mapping[str, Any]:
        if not path.is_file():
            raise InventoryViolation(
                "JavaScript package manifest is missing.",
                code="javascript_inventory_manifest_missing",
                details={"path": str(path)},
            )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise InventoryViolation(
                "JavaScript package manifest cannot be parsed.",
                code="javascript_inventory_manifest_invalid",
                details={"path": str(path), "error": str(error)},
            ) from error
        if not isinstance(value, Mapping):
            raise InventoryViolation(
                "JavaScript package manifest root must be an object.",
                code="javascript_inventory_manifest_root",
                details={"path": str(path)},
            )
        return value

    @staticmethod
    def _dependency_names(manifest: Mapping[str, Any]) -> tuple[str, ...]:
        output: list[str] = []
        for field in ("dependencies", "devDependencies", "optionalDependencies"):
            raw = manifest.get(field, {})
            if not isinstance(raw, Mapping):
                continue
            for name in raw:
                text = str(name)
                if text not in output:
                    output.append(text)
        return tuple(sorted(output))


class RuntimeInventory:
    def __init__(
        self,
        root: Path,
        *,
        policy: ReleasePolicy = DEFAULT_RELEASE_POLICY,
    ) -> None:
        self.root = root.resolve()
        self.policy = policy

    def build(self) -> dict[str, Any]:
        native: list[dict[str, Any]] = []
        executable: list[dict[str, Any]] = []
        scripts: list[dict[str, Any]] = []
        for entry in CanonicalTreeWalker(self.root, policy=self.policy).walk():
            if entry.is_symlink:
                continue
            suffix = entry.path.suffix.casefold()
            record = {
                "path": entry.relative_path,
                "sha256": sha256_file(entry.path),
                "size": entry.stat_result.st_size,
            }
            if suffix in self.policy.allow_native_extensions:
                record["declared"] = entry.relative_path in self.policy.tracked_native_paths
                native.append(record)
            if entry.executable:
                executable.append(record)
            if entry.relative_path.startswith("scripts/") and suffix in {
                ".py",
                ".ps1",
                ".cmd",
                ".sh",
                ".js",
                ".mjs",
                ".ts",
            }:
                scripts.append(record)
        process_contract = self._process_contract()
        return {
            "schema": "zyra.runtime-inventory/v1",
            "platform": {
                "system": platform.system().lower(),
                "release": platform.release(),
                "machine": platform.machine().lower(),
                "python": platform.python_version(),
                "implementation": platform.python_implementation(),
                "abi": sysconfig.get_config_var("SOABI") or "",
            },
            "native": native,
            "executables": executable,
            "scripts": scripts,
            "process_contract": process_contract,
            "native_count": len(native),
            "undeclared_native_count": sum(
                not item["declared"] for item in native
            ),
            "digest": stable_digest(
                {
                    "native": native,
                    "executables": executable,
                    "scripts": scripts,
                    "process_contract": process_contract,
                }
            ),
        }

    def enforce(self) -> dict[str, Any]:
        report = self.build()
        if report["undeclared_native_count"]:
            raise InventoryViolation(
                "Release includes undeclared native binaries.",
                code="runtime_inventory_native_undeclared",
                details={
                    "native": [
                        item for item in report["native"] if not item["declared"]
                    ]
                },
            )
        return report

    @staticmethod
    def _process_contract() -> list[dict[str, Any]]:
        return [
            {
                "owner": "zyra-api",
                "runtime": "python",
                "entry": "uvicorn zyra_api.main:app",
                "network": "loopback",
            },
            {
                "owner": "zyra-web",
                "runtime": "bun",
                "entry": "bun run --cwd apps/web preview",
                "network": "loopback",
            },
            {
                "owner": "zyra-code-worker",
                "runtime": "bun-or-node",
                "entry": "dist/code-worker/main.js",
                "network": "supervised",
            },
            {
                "owner": "zyra-deployment-nodes",
                "runtime": "python",
                "entry": "zyra_orchestration.deployment.node_server",
                "network": "loopback-authenticated",
            },
        ]


class SbomBuilder:
    def __init__(
        self,
        *,
        project_name: str,
        project_version: str,
        commit: str,
    ) -> None:
        self.project_name = project_name
        self.project_version = project_version
        self.commit = commit

    def build(
        self,
        components: Iterable[Component],
        *,
        runtime_inventory: Mapping[str, Any],
        serial: str,
        timestamp: str,
    ) -> dict[str, Any]:
        unique: dict[str, Component] = {}
        for component in components:
            prior = unique.get(component.bom_ref)
            if prior is not None and prior != component:
                raise InventoryViolation(
                    "SBOM component reference is ambiguous.",
                    code="sbom_component_collision",
                    details={"bom_ref": component.bom_ref},
                )
            unique[component.bom_ref] = component
        root_ref = f"pkg:generic/{self.project_name}@{self.project_version}"
        dependency_graph: dict[str, set[str]] = defaultdict(set)
        by_name: dict[str, str] = {}
        for component in unique.values():
            by_name.setdefault(component.name.casefold(), component.bom_ref)
        for component in unique.values():
            for dependency in component.dependencies:
                target = by_name.get(dependency.casefold())
                if target is not None and target != component.bom_ref:
                    dependency_graph[component.bom_ref].add(target)
        root_dependencies = {
            item.bom_ref
            for item in unique.values()
            if any(
                property_item["name"] == "zyra:declared-by"
                and property_item["value"] == "."
                for property_item in item.properties
            )
            or item.properties
            and item.properties[0].get("value") == "python"
        }
        dependency_graph[root_ref].update(root_dependencies)
        dependency_cycles = self._dependency_cycles(dependency_graph)
        sbom = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "serialNumber": serial,
            "version": 1,
            "metadata": {
                "timestamp": timestamp,
                "component": {
                    "type": "application",
                    "bom-ref": root_ref,
                    "name": self.project_name,
                    "version": self.project_version,
                    "properties": [
                        {"name": "zyra:commit", "value": self.commit},
                        {
                            "name": "zyra:runtime-inventory-digest",
                            "value": str(runtime_inventory.get("digest") or ""),
                        },
                    ],
                },
                "tools": {
                    "components": [
                        {
                            "type": "application",
                            "name": "zyra-release",
                            "version": "1",
                        }
                    ]
                },
            },
            "components": [
                item.to_dict()
                for item in sorted(unique.values(), key=lambda component: component.bom_ref)
            ],
            "dependencies": [
                {
                    "ref": reference,
                    "dependsOn": sorted(dependencies),
                }
                for reference, dependencies in sorted(dependency_graph.items())
            ],
            "properties": [
                {
                    "name": "zyra:native-count",
                    "value": str(runtime_inventory.get("native_count") or 0),
                },
                {
                    "name": "zyra:process-contract-digest",
                    "value": stable_digest(
                        runtime_inventory.get("process_contract") or []
                    ),
                },
                {
                    "name": "zyra:dependency-cycle-count",
                    "value": str(len(dependency_cycles)),
                },
            ],
        }
        sbom["zyra:digest"] = stable_digest(sbom)
        return sbom

    @staticmethod
    def _dependency_cycles(graph: Mapping[str, set[str]]) -> list[str]:
        indegree: dict[str, int] = defaultdict(int)
        outgoing: dict[str, set[str]] = defaultdict(set)
        nodes: set[str] = set(graph)
        for source, targets in graph.items():
            nodes.update(targets)
            for target in targets:
                if target not in outgoing[source]:
                    outgoing[source].add(target)
                    indegree[target] += 1
        queue = deque(node for node in nodes if indegree[node] == 0)
        visited = 0
        while queue:
            node = queue.popleft()
            visited += 1
            for target in outgoing[node]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if visited != len(nodes):
            return sorted(node for node in nodes if indegree[node] > 0)
        return []


class NoticeBuilder:
    def build(
        self,
        components: Iterable[Component],
        *,
        project_name: str,
        project_version: str,
        source_revision: str,
    ) -> str:
        groups: dict[str, list[Component]] = defaultdict(list)
        unknown: list[str] = []
        for component in components:
            if not component.licenses:
                unknown.append(f"{component.name}@{component.version}")
                continue
            for license_id in component.licenses:
                groups[license_id].append(component)
        for component_name in unknown:
            name, _, version = component_name.partition("@")
            groups["LicenseRef-NOASSERTION"].append(
                Component(
                    component_type="library",
                    name=name,
                    version=version,
                    purl="",
                    licenses=("LicenseRef-NOASSERTION",),
                    hashes=(),
                    properties=(),
                )
            )
        lines = [
            f"{project_name} {project_version}",
            "THIRD-PARTY NOTICES",
            "",
            f"Source revision: {source_revision}",
            "",
            "This file is generated from the committed release dependency locks.",
            "It lists declared package licenses; it does not replace license texts.",
            (
                "Entries under LicenseRef-NOASSERTION require the bundled source "
                "license/metadata to determine final redistribution terms."
            ),
            "",
        ]
        for license_id in sorted(groups):
            lines.append(f"## {license_id}")
            lines.append("")
            for component in sorted(
                groups[license_id],
                key=lambda item: (item.name.casefold(), item.version),
            ):
                lines.append(
                    f"- {component.name} {component.version} ({component.purl})"
                )
            lines.append("")
        lines.extend(
            [
                "## Runtime process and native artifact inventory",
                "",
                "See `runtime-inventory.json` and `sbom.cdx.json` in the release root.",
                "",
            ]
        )
        return "\n".join(lines)


class SecretRedactor:
    def __init__(
        self,
        values: Iterable[str] = (),
        *,
        policy: ReleasePolicy = DEFAULT_RELEASE_POLICY,
    ) -> None:
        self.policy = policy
        self.values = tuple(
            sorted(
                {
                    value
                    for value in values
                    if value and len(value) >= 4
                },
                key=len,
                reverse=True,
            )
        )

    def redact_text(self, value: str) -> str:
        output = value
        for secret in self.values:
            output = output.replace(secret, "<redacted>")
        output = re.sub(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|credential)"
            r"\s*([=:])\s*([^\s,;]+)",
            lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
            output,
        )
        return output

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, Mapping):
            output: dict[str, Any] = {}
            for key, item in value.items():
                name = str(key)
                output[name] = (
                    "<redacted>"
                    if self.policy.secret_name(name)
                    else self.redact(item)
                )
            return output
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return [self.redact(item) for item in value]
        return value

    def assert_clean(self, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        leaked = [secret for secret in self.values if secret in encoded]
        if leaked:
            raise ConfigurationViolation(
                "Release receipt contains secret values.",
                code="secret_value_leaked",
                details={"leak_count": len(leaked)},
            )


class ConfigurationProvisioner:
    def __init__(
        self,
        *,
        policy: ReleasePolicy = DEFAULT_RELEASE_POLICY,
    ) -> None:
        self.policy = policy

    def validate_template(self, value: Mapping[str, Any]) -> dict[str, Any]:
        schema = str(value.get("schema") or "")
        if schema != "zyra.release-configuration/v1":
            raise ConfigurationViolation(
                "Release configuration schema is unsupported.",
                code="configuration_schema_invalid",
                details={"schema": schema},
            )
        public = value.get("public", {})
        secrets = value.get("secrets", {})
        profiles = value.get("profiles", {})
        if not isinstance(public, Mapping):
            raise ConfigurationViolation(
                "Public release configuration must be an object.",
                code="configuration_public_invalid",
            )
        if not isinstance(secrets, Mapping):
            raise ConfigurationViolation(
                "Secret reference configuration must be an object.",
                code="configuration_secrets_invalid",
            )
        if not isinstance(profiles, Mapping):
            raise ConfigurationViolation(
                "Profile release configuration must be an object.",
                code="configuration_profiles_invalid",
            )
        secret_references: dict[str, str] = {}
        for key, reference in secrets.items():
            name = str(key)
            raw = str(reference)
            match = _SECRET_REFERENCE.fullmatch(raw)
            if match is None:
                raise ConfigurationViolation(
                    "Secret configuration must contain environment references only.",
                    code="configuration_secret_literal",
                    details={"name": name},
                )
            environment_name = match.group(1)
            if not _ENV_NAME.fullmatch(environment_name):
                raise ConfigurationViolation(
                    "Secret environment reference is invalid.",
                    code="configuration_secret_reference_invalid",
                    details={"name": name, "reference": raw},
                )
            secret_references[name] = environment_name
        self._assert_public_values(public)
        normalized_profiles = self._validate_profiles(profiles)
        return {
            "schema": schema,
            "public": self._normalize_mapping(public),
            "secret_references": secret_references,
            "profiles": normalized_profiles,
            "template_digest": stable_digest(value),
        }

    def materialize(
        self,
        template: Mapping[str, Any],
        *,
        environment: Mapping[str, str],
        destination: Path,
        require_secrets: Iterable[str] = (),
    ) -> dict[str, Any]:
        validated = self.validate_template(template)
        required = set(require_secrets)
        resolved_presence: dict[str, bool] = {}
        missing: list[str] = []
        secret_values: list[str] = []
        for name, environment_name in validated["secret_references"].items():
            raw = environment.get(environment_name, "")
            present = bool(raw)
            resolved_presence[name] = present
            if present:
                secret_values.append(raw)
            if name in required and not present:
                missing.append(name)
        if missing:
            raise ConfigurationViolation(
                "Required release secrets are missing.",
                code="configuration_required_secret_missing",
                details={"missing": sorted(missing)},
            )
        materialized = {
            "schema": "zyra.installed-configuration/v1",
            "public": validated["public"],
            "profiles": validated["profiles"],
            "secret_environment": validated["secret_references"],
            "secret_presence": resolved_presence,
            "template_digest": validated["template_digest"],
        }
        SecretRedactor(secret_values, policy=self.policy).assert_clean(materialized)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
        payload = json.dumps(
            materialized,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        try:
            temporary.write_text(payload, encoding="utf-8")
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return {
            "schema": "zyra.configuration-provision-receipt/v1",
            "ready": True,
            "path": str(destination.resolve()),
            "configuration_digest": stable_digest(materialized),
            "secret_presence": resolved_presence,
            "secret_values_persisted": False,
        }

    def sanitized_environment(
        self,
        environment: Mapping[str, str],
    ) -> dict[str, str]:
        output: dict[str, str] = {}
        for name, value in environment.items():
            if not self.policy.environment_allowed(name):
                continue
            if self.policy.secret_name(name):
                continue
            output[name] = value
        return output

    def _assert_public_values(self, value: Mapping[str, Any], *, prefix: str = "") -> None:
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if self.policy.secret_name(str(key)):
                raise ConfigurationViolation(
                    "Secret-like key appears in public configuration.",
                    code="configuration_secret_in_public",
                    details={"path": path},
                )
            if isinstance(item, Mapping):
                self._assert_public_values(item, prefix=path)
            elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
                for index, child in enumerate(item):
                    if isinstance(child, Mapping):
                        self._assert_public_values(
                            child,
                            prefix=f"{path}[{index}]",
                        )
            elif not isinstance(item, (str, int, float, bool, type(None))):
                raise ConfigurationViolation(
                    "Public configuration contains an unsupported value.",
                    code="configuration_public_value_invalid",
                    details={"path": path, "type": type(item).__name__},
                )

    @staticmethod
    def _normalize_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key in sorted(value):
            item = value[key]
            if isinstance(item, Mapping):
                output[str(key)] = ConfigurationProvisioner._normalize_mapping(item)
            elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
                output[str(key)] = [
                    ConfigurationProvisioner._normalize_mapping(child)
                    if isinstance(child, Mapping)
                    else child
                    for child in item
                ]
            else:
                output[str(key)] = item
        return output

    @staticmethod
    def _validate_profiles(value: Mapping[str, Any]) -> dict[str, Any]:
        required_profiles = {"device", "edge", "cloud"}
        actual_profiles = {str(key) for key in value}
        missing = sorted(required_profiles - actual_profiles)
        unknown = sorted(actual_profiles - required_profiles)
        if missing or unknown:
            raise ConfigurationViolation(
                "Release profile set must be exactly device, edge and cloud.",
                code="configuration_profiles_incomplete",
                details={"missing": missing, "unknown": unknown},
            )
        output: dict[str, Any] = {}
        for profile in sorted(required_profiles):
            raw = value[profile]
            if not isinstance(raw, Mapping):
                raise ConfigurationViolation(
                    "Release profile configuration must be an object.",
                    code="configuration_profile_invalid",
                    details={"profile": profile},
                )
            enabled = bool(raw.get("enabled", True))
            endpoint = str(raw.get("endpoint") or "")
            if endpoint and not endpoint.startswith(("http://127.0.0.1:", "http://localhost:")):
                raise ConfigurationViolation(
                    "Default release profile endpoint must be loopback.",
                    code="configuration_profile_endpoint_unsafe",
                    details={"profile": profile, "endpoint": endpoint},
                )
            output[profile] = {
                "enabled": enabled,
                "endpoint": endpoint,
                "network_mode": str(raw.get("network_mode") or "limited"),
                "resource_limit": dict(raw.get("resource_limit") or {}),
            }
        return output


def default_release_configuration() -> dict[str, Any]:
    return {
        "schema": "zyra.release-configuration/v1",
        "public": {
            "bind_host": "127.0.0.1",
            "log_level": "INFO",
            "offline": False,
            "telemetry": False,
        },
        "secrets": {
            "openai_api_key": "${OPENAI_API_KEY}",
            "deepseek_api_key": "${DEEPSEEK_API_KEY}",
            "deployment_supervisor_secret": "${ZYRA_DEPLOYMENT_SUPERVISOR_SECRET}",
        },
        "profiles": {
            "device": {
                "enabled": True,
                "endpoint": "",
                "network_mode": "offline",
                "resource_limit": {"cpu": 1, "memory_mb": 1024},
            },
            "edge": {
                "enabled": True,
                "endpoint": "",
                "network_mode": "limited",
                "resource_limit": {"cpu": 2, "memory_mb": 2048},
            },
            "cloud": {
                "enabled": True,
                "endpoint": "",
                "network_mode": "public",
                "resource_limit": {"cpu": 4, "memory_mb": 4096},
            },
        },
    }


__all__ = [
    "Component",
    "ConfigurationProvisioner",
    "JavaScriptComponentInventory",
    "LicenseNormalizer",
    "NoticeBuilder",
    "PythonComponentInventory",
    "RuntimeInventory",
    "SbomBuilder",
    "SecretRedactor",
    "default_release_configuration",
]
