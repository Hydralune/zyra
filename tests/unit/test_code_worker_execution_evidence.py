from __future__ import annotations

from types import SimpleNamespace

from zyra_workers.code_worker_runtime import _execution_evidence


def test_execution_evidence_preserves_openai_cached_prompt_tokens() -> None:
    result = SimpleNamespace(
        session_snapshot={},
        event_records=(
            SimpleNamespace(
                payload={
                    "query_session": {
                        "phase": "model_stream_report",
                        "model_stream": {
                            "provider_request_id": "provider-request-1",
                            "provider": "zhipu",
                            "model": "glm-5.2",
                            "route_id": "route-1",
                            "transport": "provider_control_plane",
                            "status": 200,
                            "ok": True,
                            "usage": {
                                "prompt_tokens": 1_000,
                                "completion_tokens": 25,
                                "total_tokens": 1_025,
                                "prompt_tokens_details": {
                                    "cached_tokens": 800,
                                },
                            },
                        },
                    }
                }
            ),
        ),
    )

    evidence = _execution_evidence(result)

    assert evidence["provider_called"] is True
    assert evidence["usage"]["input_tokens"] == 1_000
    assert evidence["usage"]["output_tokens"] == 25
    assert evidence["usage"]["cache_read_input_tokens"] == 800
    assert evidence["provider_calls"][0]["usage"]["cache_read_input_tokens"] == 800
