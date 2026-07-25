from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_API = "http://127.0.0.1:8000"


class ScenarioCliError(RuntimeError):
    def __init__(self, message: str, *, status: int = 1, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class ScenarioHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = 120.0,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ScenarioCliError("Scenario API URL must use http or https.")
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        self.timeout = max(1.0, min(24 * 60 * 60.0, timeout))

    def get(
        self,
        path: str,
        *,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        suffix = ""
        if query:
            selected = {
                key: value
                for key, value in query.items()
                if value is not None and value != ""
            }
            suffix = "?" + urllib.parse.urlencode(selected)
        return self._request("GET", path + suffix, None)

    def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._request("POST", path, body or {})

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        data = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None
            else None
        )
        headers = {
            "Accept": "application/json",
            "X-Zyra-Api-Version": "1.0",
            "X-Zyra-Client": "first-stage-scenario-cli",
            "X-Zyra-Client-Version": "0.1.0",
            "X-Request-Id": f"request_{uuid4().hex}",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
            headers["Idempotency-Key"] = f"scenario-cli_{uuid4().hex}"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            self.base_url + "/" + path.lstrip("/"),
            method=method,
            data=data,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                return _json_payload(payload, response.status)
        except urllib.error.HTTPError as error:
            payload = error.read()
            body_value = _json_payload(payload, error.code, allow_non_object=True)
            message = (
                str(body_value.get("message") or body_value.get("error"))
                if isinstance(body_value, dict)
                else str(body_value)
            )
            raise ScenarioCliError(
                message or f"Scenario API returned HTTP {error.code}.",
                status=error.code,
                body=body_value,
            ) from error
        except urllib.error.URLError as error:
            raise ScenarioCliError(
                f"Scenario API is unavailable: {error.reason}",
                status=2,
            ) from error


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Control the Zyra first-stage scenario/evidence owner. This CLI "
            "calls the canonical API and never runs the legacy demo harness."
        )
    )
    result.add_argument("--api", default=DEFAULT_API)
    result.add_argument("--token", default="")
    result.add_argument("--timeout", type=float, default=120.0)
    result.add_argument(
        "--output",
        choices=("json", "summary"),
        default="json",
    )
    commands = result.add_subparsers(dest="command", required=True)

    commands.add_parser("registry", help="Read versioned scenario definitions.")

    list_parser = commands.add_parser("list", help="List durable scenario runs.")
    list_parser.add_argument("--include-archived", action="store_true")
    list_parser.add_argument("--limit", type=int, default=100)
    list_parser.add_argument("--offset", type=int, default=0)

    create = commands.add_parser("create", help="Create and preflight a new scenario.")
    create.add_argument("--scenario", default="foundation.short-owner-chain")
    create.add_argument("--version", default="")
    create.add_argument("--profile", default="foundation.local-sealed")
    create.add_argument("--policy", default="sealed-autonomous-foundation")
    create.add_argument("--policy-digest", default="")
    create.add_argument("--mode", choices=("sealed", "interactive"), default="sealed")
    input_group = create.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--input")
    input_group.add_argument("--input-file", type=Path)
    create.add_argument("--seed", type=int, default=0)
    create.add_argument("--label", action="append", default=[])
    create.add_argument("--preflight", type=Path)
    create.add_argument("--faults", type=Path)
    create.add_argument("--start", action="store_true")
    create.add_argument("--wait", action="store_true")
    create.add_argument("--poll-interval", type=float, default=0.25)

    for command in ("status", "evidence", "verify"):
        selected = commands.add_parser(command)
        selected.add_argument("scenario_run_id")

    start = commands.add_parser("start")
    start.add_argument("scenario_run_id")
    start.add_argument("--wait", action="store_true")
    start.add_argument("--poll-interval", type=float, default=0.25)

    cancel = commands.add_parser("cancel")
    cancel.add_argument("scenario_run_id")
    cancel.add_argument("--reason", required=True)

    archive = commands.add_parser("archive")
    archive.add_argument("scenario_run_id")
    archive.add_argument("--reason", required=True)

    return result


def execute(arguments: argparse.Namespace) -> dict[str, Any]:
    client = ScenarioHttpClient(
        arguments.api,
        token=arguments.token,
        timeout=arguments.timeout,
    )
    command = arguments.command
    if command == "registry":
        return client.get("/scenarios/registry")
    if command == "list":
        return client.get(
            "/scenarios/runs",
            query={
                "include_archived": str(arguments.include_archived).lower(),
                "limit": arguments.limit,
                "offset": arguments.offset,
            },
        )
    if command == "create":
        body = _create_body(arguments)
        response = client.post("/scenarios/runs", body)
        if not arguments.start:
            return response
        run_id = str(response["run"]["scenario_run_id"])
        started = client.post(
            f"/scenarios/runs/{urllib.parse.quote(run_id)}/start",
            {"wait": False},
        )
        if arguments.wait:
            return wait_for_terminal(
                client,
                run_id,
                poll_interval=arguments.poll_interval,
                timeout=arguments.timeout,
            )
        return started
    if command == "status":
        return client.get(
            f"/scenarios/runs/{urllib.parse.quote(arguments.scenario_run_id)}"
        )
    if command == "evidence":
        return client.get(
            f"/scenarios/runs/{urllib.parse.quote(arguments.scenario_run_id)}/evidence"
        )
    if command == "start":
        response = client.post(
            f"/scenarios/runs/{urllib.parse.quote(arguments.scenario_run_id)}/start",
            {"wait": False},
        )
        if arguments.wait:
            return wait_for_terminal(
                client,
                arguments.scenario_run_id,
                poll_interval=arguments.poll_interval,
                timeout=arguments.timeout,
            )
        return response
    if command == "cancel":
        return client.post(
            f"/scenarios/runs/{urllib.parse.quote(arguments.scenario_run_id)}/cancel",
            {"reason": arguments.reason},
        )
    if command == "archive":
        return client.post(
            f"/scenarios/runs/{urllib.parse.quote(arguments.scenario_run_id)}/archive",
            {"reason": arguments.reason},
        )
    if command == "verify":
        return client.post(
            f"/scenarios/runs/{urllib.parse.quote(arguments.scenario_run_id)}/verify",
            {},
        )
    raise ScenarioCliError(f"Unsupported scenario command: {command}")


def wait_for_terminal(
    client: ScenarioHttpClient,
    scenario_run_id: str,
    *,
    poll_interval: float,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, timeout)
    interval = max(0.05, min(10.0, poll_interval))
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = client.get(
            f"/scenarios/runs/{urllib.parse.quote(scenario_run_id)}"
        )
        run = last.get("run") if isinstance(last, dict) else None
        if isinstance(run, dict) and run.get("terminal") is True:
            return last
        time.sleep(interval)
    phase = (
        (last.get("run") or {}).get("phase")
        if isinstance(last, dict)
        else "unknown"
    )
    raise ScenarioCliError(
        f"Timed out waiting for scenario {scenario_run_id}; last phase was {phase}.",
        status=3,
        body=last,
    )


def _create_body(arguments: argparse.Namespace) -> dict[str, Any]:
    if arguments.input_file:
        selected = arguments.input_file.expanduser().resolve()
        if not selected.is_file():
            raise ScenarioCliError(f"Scenario input file does not exist: {selected}")
        input_text = selected.read_text(encoding="utf-8")
    else:
        input_text = str(arguments.input or "")
    labels: dict[str, str] = {}
    for value in arguments.label:
        key, separator, selected_value = str(value).partition("=")
        if not separator or not key.strip() or not selected_value.strip():
            raise ScenarioCliError("--label must use key=value.")
        labels[key.strip()] = selected_value.strip()
    body: dict[str, Any] = {
        "scenario_id": arguments.scenario,
        "definition_version": arguments.version or None,
        "profile_id": arguments.profile,
        "policy_id": arguments.policy,
        "policy_digest": arguments.policy_digest or None,
        "mode": arguments.mode,
        "input": input_text,
        "seed": arguments.seed,
        "labels": labels,
    }
    if arguments.preflight:
        body["preflight"] = _read_json_array(arguments.preflight, "preflight")
    if arguments.faults:
        body["faults"] = _read_json_array(arguments.faults, "faults")
    return body


def _read_json_array(path: Path, label: str) -> list[Any]:
    selected = path.expanduser().resolve()
    if not selected.is_file():
        raise ScenarioCliError(f"Scenario {label} file does not exist: {selected}")
    try:
        value = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ScenarioCliError(f"Scenario {label} file is invalid: {error}") from error
    if not isinstance(value, list):
        raise ScenarioCliError(f"Scenario {label} file must contain a JSON array.")
    return value


def _json_payload(
    payload: bytes,
    status: int,
    *,
    allow_non_object: bool = False,
) -> Any:
    try:
        value = json.loads(payload.decode("utf-8")) if payload else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScenarioCliError(
            f"Scenario API returned invalid JSON with HTTP {status}.",
            status=status,
        ) from error
    if not allow_non_object and not isinstance(value, dict):
        raise ScenarioCliError(
            f"Scenario API returned a non-object response with HTTP {status}.",
            status=status,
            body=value,
        )
    return value


def _summary(value: dict[str, Any]) -> str:
    run = value.get("run")
    if isinstance(run, dict):
        manifest = run.get("evidence_manifest")
        steps = (
            (manifest.get("effective_steps") or {}).get("admitted_step_ids")
            if isinstance(manifest, dict)
            else []
        )
        return " ".join(
            [
                f"scenario_run_id={run.get('scenario_run_id', '')}",
                f"phase={run.get('phase', '')}",
                f"task_id={run.get('task_id', '')}",
                f"effective_steps={len(steps or [])}",
                f"human_intervention_count={run.get('human_intervention_count', 0)}",
                f"evidence_valid={bool(run.get('verification_receipt', {}).get('valid'))}",
            ]
        )
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        result = execute(arguments)
    except ScenarioCliError as error:
        payload = {
            "ok": False,
            "error": "scenario_cli_failed",
            "message": str(error),
            "status": error.status,
            "body": error.body,
            "fallback": False,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return error.status if 0 < error.status < 256 else 1
    if arguments.output == "summary":
        print(_summary(result))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
