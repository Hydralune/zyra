"""Live compatibility gate for the pinned AutoGen and GPTSwarm baselines.

Run this file with the isolated interpreter prepared for the selected baseline.
It never prints or stores the API key. The output is an auditable JSON receipt.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = ROOT / ".env.deepseek.local"
MODEL = "deepseek-flash"
BASE_URL = "https://api.deepseek.com"
TASK = (
    "Return exactly one JSON object and no explanation. Records: "
    '[{"field":"region","revision":2,"status":"active","value":"cn-south"},'
    '{"field":"region","revision":5,"status":"revoked","value":"ap-east"},'
    '{"field":"retries","revision":1,"status":"superseded","value":1},'
    '{"field":"retries","revision":3,"status":"active","value":4}]. '
    "For each field select the active record with the highest revision. "
    'Required output keys are region and retries.'
)
EXPECTED = {"region": "cn-south", "retries": 4}


def load_secret() -> str:
    values: dict[str, str] = {}
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    api_key = values.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError(f"DEEPSEEK_API_KEY is missing from {ENV_FILE.name}")
    return api_key


def extract_json(text: str) -> dict[str, object]:
    left, right = text.find("{"), text.rfind("}")
    if left < 0 or right <= left:
        raise ValueError("No JSON object found in baseline output")
    value = json.loads(text[left : right + 1])
    if not isinstance(value, dict):
        raise ValueError("Baseline output is not a JSON object")
    return value


async def run_autogen(api_key: str) -> tuple[str, dict[str, object]]:
    from autogen_agentchat.agents import AssistantAgent
    from autogen_agentchat.conditions import MaxMessageTermination
    from autogen_agentchat.teams import RoundRobinGroupChat
    from autogen_ext.models.openai import OpenAIChatCompletionClient

    client = OpenAIChatCompletionClient(
        model=MODEL,
        api_key=api_key,
        base_url=BASE_URL,
        temperature=0,
        model_info={
            "vision": False,
            "function_calling": False,
            "json_output": True,
            "family": "unknown",
            "structured_output": False,
        },
    )
    agents = [
        AssistantAgent(
            f"analyst_{index}",
            model_client=client,
            system_message=(
                "Independently solve the user's record-resolution task. Check status and revision "
                "carefully. Return a compact candidate JSON object."
            ),
        )
        for index in range(1, 4)
    ]
    finalizer = AssistantAgent(
        "finalizer",
        model_client=client,
        system_message=(
            "Act as final adjudicator. Use the original task and prior candidates. Return exactly "
            "one flat JSON object with the required keys and no explanation."
        ),
    )
    team = RoundRobinGroupChat(
        [*agents, finalizer], termination_condition=MaxMessageTermination(max_messages=4)
    )
    try:
        result = await team.run(task=TASK)
        output = str(result.messages[-1].content)
        usage = {"framework_messages": len(result.messages)}
        return output, usage
    finally:
        await client.close()


async def run_gptswarm(api_key: str) -> tuple[str, dict[str, object]]:
    # The pinned GPTSwarm backend uses OpenAI's standard environment variables.
    # OpenAI's SDK honors OPENAI_BASE_URL, so no upstream source modification is needed.
    os.environ["OPENAI_API_KEY"] = api_key
    os.environ["OPENAI_BASE_URL"] = BASE_URL

    from swarm.environment.prompt.prompt_set import PromptSet
    from swarm.environment.prompt.prompt_set_registry import PromptSetRegistry

    @PromptSetRegistry.register("zyra_preflight")
    class StructuredPromptSet(PromptSet):
        @staticmethod
        def get_role() -> str:
            return "precise structured-data analyst"

        @staticmethod
        def get_constraint() -> str:
            return "Follow record status and revision rules. Output only the requested JSON object."

        @staticmethod
        def get_format() -> str:
            return "flat JSON object"

        @staticmethod
        def get_answer_prompt(question: str) -> str:
            return question

        @staticmethod
        def get_combine_materials(materials: dict[str, object]) -> str:
            return (
                f"Original task:\n{materials.get('task', TASK)}\n"
                f"Candidate answers:\n{materials.get('DirectAnswer', '')}\n"
                "Resolve disagreements and return exactly the required JSON object."
            )

        get_adversarial_answer_prompt = staticmethod(lambda question: question)
        get_query_prompt = staticmethod(lambda question: question)
        get_file_analysis_prompt = staticmethod(lambda query, file: query)
        get_websearch_prompt = staticmethod(lambda query: query)
        get_distill_websearch_prompt = staticmethod(lambda query, results: query)
        get_reflect_prompt = staticmethod(lambda question, answer: question)

    # Import after registering the task domain; the graph and orchestration remain upstream code.
    from swarm.graph.swarm import Swarm

    swarm = Swarm(
        ["IO", "IO", "IO"],
        "zyra_preflight",
        model_name=MODEL,
        edge_optimize=False,
    )
    answers = await swarm.arun({"task": TASK, "files": []})
    output = str(answers[-1])
    from swarm.utils.globals import CompletionTokens, PromptTokens

    return output, {
        "framework_answers": len(answers),
        "agents": 3,
        "prompt_tokens": PromptTokens.instance().value,
        "completion_tokens": CompletionTokens.instance().value,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", choices=("autogen", "gptswarm"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    api_key = load_secret()
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    if args.baseline == "autogen":
        raw_output, framework = await run_autogen(api_key)
    else:
        raw_output, framework = await run_gptswarm(api_key)
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    parsed = extract_json(raw_output)
    strict_success = parsed == EXPECTED
    receipt = {
        "schema": "zyra.external-baseline-preflight/v1",
        "baseline": args.baseline,
        "model": MODEL,
        "started_at": started_at,
        "elapsed_ms": elapsed_ms,
        "task_digest": hashlib.sha256(TASK.encode("utf-8")).hexdigest(),
        "output_digest": hashlib.sha256(raw_output.encode("utf-8")).hexdigest(),
        "parsed_output": parsed,
        "expected": EXPECTED,
        "strict_success": strict_success,
        "framework": framework,
        "python": sys.version,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"baseline": args.baseline, "strict_success": strict_success, "elapsed_ms": elapsed_ms}))
    if not strict_success:
        raise SystemExit(2)


if __name__ == "__main__":
    asyncio.run(main())
