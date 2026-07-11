from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

from .digests import digest_object
from .errors import PluginManifestError
from .models import utc_now
from .sources.plugin import PluginCapabilitySource, PluginManifest


@dataclass(frozen=True, slots=True)
class PluginCapabilitySnapshot:
    generation: int
    plugins: dict[str, dict[str, Any]]
    disabled: dict[str, str]
    errors: tuple[dict[str, Any], ...]
    snapshot_id: str
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "plugins": {key: dict(value) for key, value in self.plugins.items()},
            "disabled": dict(self.disabled),
            "errors": [dict(value) for value in self.errors],
            "snapshot_id": self.snapshot_id,
            "created_at": self.created_at,
        }


class PluginRuntime:
    """Cache-only plugin capability owner for 03C foundation.

    It never installs npm packages, clones marketplaces, imports arbitrary
    modules, or launches subprocesses. Explicit refresh validates every
    manifest and swaps a complete capability snapshot atomically.
    """

    def __init__(self, plugin_roots: Iterable[str | Path] = ()) -> None:
        self._lock = RLock()
        self._roots = tuple(Path(path) for path in plugin_roots)
        self._disabled: dict[str, str] = {}
        self._snapshot = PluginCapabilitySnapshot(
            generation=0,
            plugins={},
            disabled={},
            errors=(),
            snapshot_id=digest_object({"generation": 0, "plugins": {}}),
        )

    def snapshot(self) -> PluginCapabilitySnapshot:
        with self._lock:
            return self._snapshot

    def configure_roots(self, roots: Iterable[str | Path]) -> None:
        with self._lock:
            self._roots = tuple(Path(path) for path in roots)

    def disable(self, plugin_id: str, *, reason: str) -> None:
        with self._lock:
            self._disabled[str(plugin_id)] = reason

    def enable(self, plugin_id: str) -> bool:
        with self._lock:
            return self._disabled.pop(str(plugin_id), None) is not None

    def refresh(self) -> PluginCapabilitySnapshot:
        previous = self.snapshot()
        plugins: dict[str, dict[str, Any]] = {}
        errors: list[dict[str, Any]] = []
        for order, root in enumerate(self._roots):
            try:
                manifest = PluginManifest.load(root)
                if manifest.plugin_id in self._disabled:
                    continue
                if manifest.plugin_id in plugins:
                    raise PluginManifestError(
                        "duplicate plugin id in cache-only capability roots",
                        detail={"plugin_id": manifest.plugin_id},
                    )
                plugins[manifest.plugin_id] = {
                    "plugin_id": manifest.plugin_id,
                    "version": manifest.version,
                    "root": str(root.resolve()),
                    "skills_paths": list(manifest.skills_paths),
                    "manifest_digest": manifest.digest(),
                    "configured_order": order,
                    "cache_only": True,
                    "dynamic_install": False,
                }
            except Exception as error:  # noqa: BLE001 - reject entire refresh below.
                errors.append(
                    {
                        "code": str(getattr(error, "code", "plugin_manifest_invalid")),
                        "message": str(error),
                        "root": str(root),
                        "detail": dict(getattr(error, "detail", {}) or {}),
                    }
                )
        if errors:
            return PluginCapabilitySnapshot(
                generation=previous.generation,
                plugins=dict(previous.plugins),
                disabled=dict(self._disabled),
                errors=tuple(errors),
                snapshot_id=previous.snapshot_id,
            )
        generation = previous.generation + 1
        payload = {
            "generation": generation,
            "plugins": plugins,
            "disabled": self._disabled,
        }
        candidate = PluginCapabilitySnapshot(
            generation=generation,
            plugins=plugins,
            disabled=dict(self._disabled),
            errors=(),
            snapshot_id=digest_object(payload),
        )
        with self._lock:
            if self._snapshot.snapshot_id != previous.snapshot_id:
                return self.refresh()
            self._snapshot = candidate
        return candidate

    def skill_sources(self) -> tuple[PluginCapabilitySource, ...]:
        snapshot = self.snapshot()
        return tuple(
            PluginCapabilitySource(
                plugin_root=Path(item["root"]),
                source_id=f"plugin:{plugin_id}",
                configured_order=int(item["configured_order"]),
            )
            for plugin_id, item in sorted(snapshot.plugins.items())
        )
