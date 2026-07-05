from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import EventRecord, to_jsonable


def append_event(event: EventRecord, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as file:
        file.write(json.dumps(to_jsonable(event), ensure_ascii=False, sort_keys=True))
        file.write("\n")
    return target


def read_events(path: str | Path, limit: int | None = None) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []

    lines = target.read_text(encoding="utf-8").splitlines()
    if limit is not None:
        lines = lines[-limit:]

    events: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        events.append(json.loads(line))
    return events
