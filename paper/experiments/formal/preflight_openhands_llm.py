"""Record a live OpenHands SDK-to-model compatibility receipt.

This is deliberately a transport preflight, not an OpenHands-agent benchmark
result: SWE-bench agents require their Docker workspace, which is gated
separately.  No API key is read, displayed, or stored by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path


TASK = "Reply with exactly: ZYRA_OPENHANDS_PREFLIGHT_OK"
EXPECTED = "ZYRA_OPENHANDS_PREFLIGHT_OK"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--llm-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    from benchmarks.utils.llm_config import load_llm_config
    from openhands.sdk.llm import Message, TextContent

    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    llm = load_llm_config(args.llm_config)
    response = llm.completion(
        [Message(role="user", content=[TextContent(text=TASK)])]
    )
    text = "".join(
        item.text for item in response.message.content if isinstance(item, TextContent)
    ).strip()
    receipt = {
        "schema": "zyra.openhands-llm-preflight/v1",
        "baseline": "openhands_official_sdk_transport",
        "scope": "llm-compatibility-only; not an agent benchmark result",
        "model": llm.model,
        "base_url_host": llm.base_url.split("//", 1)[-1].split("/", 1)[0]
        if llm.base_url
        else None,
        "started_at": started_at,
        "elapsed_ms": round((time.perf_counter() - started) * 1000),
        "task_digest": hashlib.sha256(TASK.encode("utf-8")).hexdigest(),
        "output_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "strict_success": text == EXPECTED,
        "usage": response.metrics.model_dump(mode="json"),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"strict_success": receipt["strict_success"], "elapsed_ms": receipt["elapsed_ms"]}))
    if not receipt["strict_success"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
