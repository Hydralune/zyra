from types import SimpleNamespace

from apps.api.zyra_api import main as api
from zyra_memory import MemoryLayer


def test_memory_command_uses_parsed_query_layer_and_limit(monkeypatch):
    captured = {}

    class Fabric:
        def memory_view(self, state, events, **kwargs):
            captured.update(kwargs)
            return {"summary": "memory", "data": {}}

    monkeypatch.setattr(api, "_memory_fabric", lambda store: Fabric())
    event = SimpleNamespace(payload={
        "raw": 'search "山茶" --layer episodic --limit 50',
        "command": {"name": "/memory", "arguments": {
            "action": "search", "query": "山茶", "layer": "episodic", "limit": 50,
        }},
    })
    api._command_result_for_event(
        SimpleNamespace(task_id="task-memory"), event,
        SimpleNamespace(task_events=lambda task_id: []),
    )
    assert captured == {"query": "山茶", "limit": 50, "layers": (MemoryLayer.EPISODIC,)}
