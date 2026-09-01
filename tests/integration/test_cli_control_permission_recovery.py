from __future__ import annotations

from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any, Iterator
from urllib.error import HTTPError
from urllib.parse import urlencode
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[2]
BUN = ROOT / "node_modules" / ".bin" / ("bun.exe" if os.name == "nt" else "bun")


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=body,
        method=method,
        headers={
            "Accept": "application/json",
            "X-Zyra-Api-Version": "1.0",
            "X-Zyra-Client": "fe-s03-integration",
            "X-Zyra-Client-Version": "0.1.0",
            "X-Request-Id": f"request_{uuid4().hex}",
            **({"Content-Type": "application/json"} if body is not None else {}),
            **dict(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read()), dict(response.headers.items())
    except HTTPError as error:
        return error.code, json.loads(error.read()), dict(error.headers.items())


@contextmanager
def _real_api(tmp_path: Path) -> Iterator[tuple[str, Any]]:
    environment = {
        "ZYRA_SQLITE_PATH": str(tmp_path / "api.sqlite3"),
        "ZYRA_EVENT_LOG": str(tmp_path / "events.jsonl"),
        "ZYRA_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "ZYRA_TOOL_WORKSPACE": str(tmp_path / "tool-workspace"),
        "ZYRA_PERMISSION_STATE": str(tmp_path / "permissions.json"),
        "ZYRA_MCP_STATE": str(tmp_path / "e02-state.json"),
        "ZYRA_WORKER_POOL_STORE": str(tmp_path / "worker-pool.sqlite3"),
        "ZYRA_GRAPH_STATE_STORE": str(tmp_path / "graph.sqlite3"),
        "ZYRA_WORKSPACE_STATE_ROOT": str(tmp_path / "workspace-state"),
        "ZYRA_WORKSPACE_DATA_ROOT": str(tmp_path / "workspace-data"),
        "ZYRA_CONTROL_STATE": str(tmp_path / "control"),
        "ZYRA_SUBAGENT_STATE": str(tmp_path / "subagents"),
        "ZYRA_CLI_STATE_DIR": str(tmp_path / "cli-state"),
        "ZYRA_E02_API_PERMISSION_MODE": "default",
    }
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    package_paths = [
        ROOT,
        ROOT / "apps" / "api",
        *[ROOT / "packages" / name for name in (
            "core", "commands", "orchestration", "memory", "runtime",
            "integrations", "workers", "symbolic", "scheduler", "evaluation",
            "workspace", "code_index",
        )],
    ]
    for path in package_paths:
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    from apps.api.zyra_api import main as api_main

    api_main = importlib.reload(api_main)
    api_main.reset_mcp_runtime()
    api_main.reset_runtime_event_spine_bridge()
    api_main.reset_worker_pool_api()
    server = ThreadingHTTPServer(("127.0.0.1", 0), api_main.ZyraRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        assert _request(base_url, "/health")[1]["service"] == "zyra-api"
        yield base_url, api_main
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=30)
        api_main.reset_mcp_runtime()
        api_main.reset_runtime_event_spine_bridge()
        api_main.reset_worker_pool_api()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _bun_json(script: str, *arguments: str, timeout: int = 90) -> dict[str, Any]:
    completed = subprocess.run(
        [str(BUN), "-e", script, *arguments],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _running_task(api_main: Any, goal: str) -> Any:
    state = api_main.create_task_state(user_goal=goal)
    running = type(state.status).RUNNING
    state.status = running
    state.plan_nodes[state.root_node_id].status = running
    state.metadata["query_session_id"] = f"task:{state.task_id}"
    api_main.get_store().save_checkpoint(state)
    return state


def test_real_cli_web_command_queue_idempotency_revision_and_retry(tmp_path: Path) -> None:
    with _real_api(tmp_path) as (base_url, api_main):
        state = _running_task(api_main, "Exercise FE-S03 canonical control queue.")
        session_id = f"task:{state.task_id}"
        guard = api_main.get_control_dispatcher().mutation_guard
        assert guard.reserve(session_id, "fe-s03-queue-blocker") is True
        try:
            queued = _bun_json(
                """
import {CliApi} from './apps/cli/src/api.ts';
import {CliControlSession} from './apps/cli/src/control/commands.ts';
import {admitCommandReceipt,buildCommandRequest,createCommandRegistry,parseCommand} from '@zyra/commands';
const [baseUrl,taskId]=process.argv.slice(1); const api=new CliApi({baseUrl,timeoutMs:30000});
const task=await api.task(taskId); const registry=createCommandRegistry();
const context={taskId:task.taskId,runId:task.runId,sessionId:task.metadata.query_session_id??task.sessionId??`task:${task.taskId}`,taskStatus:task.status,active:true,terminal:false,transportEnabled:true,sealed:false,remote:true};
const specs=[['/change apply now','interrupt','now'],['/change apply next','enqueue','next'],['/change apply later','enqueue','later']];
const receipts=[];
for(const [text,mode,priority] of specs){let request=buildCommandRequest({parsed:parseCommand(text,registry),context,mode,actorId:'fe-s03-cli'});request={...request,priority};receipts.push(admitCommandReceipt(await api.submitControlCommand(request),request));}
await api.cancelControlCommand({taskId,requestId:receipts[1].requestId,reason:'exercise canonical cancellation'});
const controls=new CliControlSession({api,task}); const retried=await controls.retry(receipts[1].requestId);
const queue=await api.commandQueue({taskId,sessionId:context.sessionId,includeTerminal:true});
console.log(JSON.stringify({receipts:receipts.map(r=>({requestId:r.requestId,phase:r.phase,priority:r.priority,queueId:r.queueId})),retried:{retryOf:retried.retryOf,phase:retried.phase,priority:retried.priority},queue:queue.items.map(i=>({requestId:i.requestId,priority:i.priority,phase:i.phase,retryOf:i.retryOf}))})); api.close();
""",
                base_url,
                state.task_id,
            )
        finally:
            guard.release(session_id, "fe-s03-queue-blocker")

        assert [item["priority"] for item in queued["receipts"]] == ["now", "next", "later"]
        assert all(item["phase"] == "queued" for item in queued["receipts"])
        assert queued["retried"]["retryOf"] == queued["receipts"][1]["requestId"]
        assert queued["retried"]["phase"] == "queued"
        assert [
            item["priority"]
            for item in queued["queue"]
            if item["phase"] == "queued"
        ][:3] == ["now", "next", "later"]
        assert any(item["phase"] == "cancelled" for item in queued["queue"])

        shared = _bun_json(
            """
import {CliApi} from './apps/cli/src/api.ts';
import {ZyraApiClient} from './apps/web/src/api/client.ts'; import {TaskApi} from './apps/web/src/api/task-api.ts';
import {admitCommandReceipt,buildCommandRequest,createCommandRegistry,parseCommand} from '@zyra/commands';
const [baseUrl,taskId]=process.argv.slice(1); const api=new CliApi({baseUrl,timeoutMs:30000}); const task=await api.task(taskId); const registry=createCommandRegistry(); const sessionId=task.metadata.query_session_id??task.sessionId??`task:${task.taskId}`;
const context={taskId:task.taskId,runId:task.runId,sessionId,taskStatus:task.status,active:true,terminal:false,transportEnabled:true,sealed:false,remote:true};
const request=buildCommandRequest({parsed:parseCommand('/status',registry),context,mode:'enqueue',actorId:'fe-s03-cli'});
const first=await api.submitControlCommand(request); const second=await api.submitControlCommand(request); const cliReceipt=admitCommandReceipt(second,request);
const webClient=new ZyraApiClient({baseUrl}); const web=new TaskApi(webClient); const webRequest=`request_${crypto.randomUUID().replaceAll('-','')}`; const webCommand=`cmd_${crypto.randomUUID().replaceAll('-','')}`;
const webRaw=await web.controlCommand({taskId:task.taskId,runId:task.runId,text:'/status',requestId:webRequest,commandId:webCommand,expectedRevision:cliReceipt.revisionAfter,actorId:'fe-s03-web'});
const stale=buildCommandRequest({parsed:parseCommand('/change stale overwrite',registry),context:{...context,expectedRevision:999},mode:'enqueue',actorId:'fe-s03-cli'}); const conflict=admitCommandReceipt(await api.submitControlCommand(stale),stale);
const events=await api.events(taskId); const effectEvents=events.filter(e=>e.payload?.request_id===request.requestId&&e.eventType.includes('succeeded')).length;
console.log(JSON.stringify({firstReplayed:first.receipt_replayed===true,secondReplayed:second.receipt_replayed===true,effectEvents,cliRequest:cliReceipt.requestId,cliRevision:cliReceipt.revisionAfter,webKeys:Object.keys(webRaw),webCommandResult:webRaw.command_result??webRaw.commandResult,conflict})); api.close(); webClient.close();
""",
            base_url,
            state.task_id,
        )
        assert shared["firstReplayed"] is False
        assert shared["effectEvents"] == 1
        assert shared["webCommandResult"]["revision_before"] == shared["cliRevision"], shared
        assert shared["webCommandResult"]["revision_after"] >= shared["webCommandResult"]["revision_before"]
        assert shared["conflict"]["phase"] == "rejected"
        assert shared["conflict"]["error"]["code"] == "revision_conflict"
        assert shared["conflict"]["error"]["details"]["actual"] == shared["cliRevision"]


def test_real_permission_deny_allow_restart_web_consistency_and_sealed_fail_closed(tmp_path: Path) -> None:
    with _real_api(tmp_path) as (base_url, api_main):
        state = _running_task(api_main, "Exercise FE-S03 permission decisions.")
        session_id = f"permission-console:{state.task_id}"
        facade = api_main.get_permission_api_facade(task_id=state.task_id, session_id=session_id)
        custody = facade.open_session(
            session_id=session_id,
            run_id=state.run_id,
            task_id=state.task_id,
        )
        token = custody.body["session"]["bearer_token"]
        auth = {"Authorization": f"Bearer {token}"}

        status, denied_command, _ = _request(
            base_url,
            f"/tasks/{state.task_id}/commands",
            method="POST",
            payload={
                "text": "/e02-reload",
                "actor_id": "fe-s03-permission",
                "tool_call_id": f"toolcall_{uuid4().hex}",
            },
        )
        assert status == 403, denied_command
        first = _bun_json(
            """
import {CliApi} from './apps/cli/src/api.ts'; import {CliPermissionSession} from './apps/cli/src/control/permission.ts';
import {ZyraApiClient} from './apps/web/src/api/client.ts'; import {PermissionApi} from './apps/web/src/api/permission-api.ts';
const [baseUrl,taskId,sessionId,token]=process.argv.slice(1); const api=new CliApi({baseUrl,timeoutMs:30000}); const task=await api.task(taskId); const cli=new CliPermissionSession({api,task,sessionId,custodyToken:token}); const opened=await cli.open(); const pending=await cli.pending(); const resolved=await cli.resolve({requestId:pending[0].requestId,effect:'deny',feedback:'deny exact high-risk action'});
const webClient=new ZyraApiClient({baseUrl}); const web=new PermissionApi(webClient); web.adoptCustody({taskId:task.taskId,runId:task.runId,sessionId},token); const visible=await web.requests({taskId:task.taskId,runId:task.runId,sessionId},{limit:100});
console.log(JSON.stringify({opened,pending:pending.map(p=>p.requestId),receipt:resolved.receipt,web:visible.requests.items.map(i=>({requestId:i.request_id,status:i.status,responseAccepted:i.response_accepted}))})); api.close(); web.close(); webClient.close();
""",
            base_url,
            state.task_id,
            session_id,
            token,
        )
        assert first["opened"] is True
        assert len(first["pending"]) == 1
        assert first["receipt"]["accepted"] is True
        assert first["receipt"]["effect"] == "deny"
        assert not first["receipt"].get("permit_id")
        assert any(item["responseAccepted"] is True for item in first["web"])

        allow_tool_call_id = f"toolcall_{uuid4().hex}"
        status, _, _ = _request(
            base_url,
            f"/tasks/{state.task_id}/commands",
            method="POST",
            payload={
                "text": "/e02-reload",
                "actor_id": "fe-s03-permission",
                "tool_call_id": allow_tool_call_id,
            },
        )
        assert status == 403
        restarted = _bun_json(
            """
import {CliApi} from './apps/cli/src/api.ts'; import {CliPermissionSession} from './apps/cli/src/control/permission.ts';
const [baseUrl,taskId,sessionId,token]=process.argv.slice(1); const api=new CliApi({baseUrl,timeoutMs:30000}); const task=await api.task(taskId); const cli=new CliPermissionSession({api,task,sessionId,custodyToken:token}); const opened=await cli.open(); const pending=await cli.pending(); const resolved=await cli.resolve({requestId:pending[0].requestId,effect:'allow'}); const after=await cli.pending(); console.log(JSON.stringify({opened,pending:pending.map(p=>p.requestId),after:after.length,receipt:resolved.receipt})); api.close();
""",
            base_url,
            state.task_id,
            session_id,
            token,
        )
        assert restarted["opened"] is True
        assert len(restarted["pending"]) == 1
        assert restarted["after"] == 0
        assert restarted["receipt"]["accepted"] is True
        assert restarted["receipt"]["effect"] == "allow"
        assert restarted["receipt"]["permit_id"]
        assert restarted["receipt"]["decision"]["human_intervention_count"] == 1

        allowed_status, allowed_result, _ = _request(
            base_url,
            f"/tasks/{state.task_id}/commands",
            method="POST",
            payload={
                "text": "/e02-reload",
                "actor_id": "fe-s03-permission",
                "tool_call_id": allow_tool_call_id,
                "permit_id": restarted["receipt"]["permit_id"],
            },
        )
        assert allowed_status == 201, allowed_result
        assert allowed_result["command_result"]["status"] == "completed"
        replay_status, replay_result, _ = _request(
            base_url,
            f"/tasks/{state.task_id}/commands",
            method="POST",
            payload={
                "text": "/e02-reload",
                "actor_id": "fe-s03-permission",
                "tool_call_id": allow_tool_call_id,
                "permit_id": restarted["receipt"]["permit_id"],
            },
        )
        assert replay_status == 201, replay_result
        assert replay_result["command_result"]["completedAt"] == allowed_result["command_result"]["completedAt"]
        assert allowed_result["command_result"]["receipt"]["replayed"] is False
        assert replay_result["command_result"]["receipt"]["replayed"] is True
        assert replay_result["command_result"]["receipt"]["executionId"] == allowed_result["command_result"]["receipt"]["executionId"]

        query = urlencode(
            {
                "session_id": session_id,
                "run_id": state.run_id,
                "task_id": state.task_id,
                "limit": 100,
            }
        )
        _, decisions, _ = _request(
            base_url,
            f"/permissions/requests?{query}",
            headers=auth,
        )
        assert all(item["response_accepted"] is True for item in decisions["requests"]["items"])

        sealed = _running_task(api_main, "Sealed permission request must fail closed.")
        sealed.metadata["sealed"] = True
        sealed.metadata["sealed_autonomous"] = True
        sealed.metadata["competition_mode"] = "sealed_autonomous"
        api_main.get_store().save_checkpoint(sealed)
        started = time.monotonic()
        sealed_status, sealed_response, _ = _request(
            base_url,
            f"/tasks/{sealed.task_id}/commands",
            method="POST",
            payload={
                "text": "/e02-reload",
                "actor_id": "fe-s03-sealed",
                "tool_call_id": f"toolcall_{uuid4().hex}",
                "sealed": True,
                "competition_mode": "sealed_autonomous",
            },
        )
        assert time.monotonic() - started < 5
        assert sealed_status == 403, sealed_response
        assert sealed_response.get("human_intervention_count", 0) == 0


def test_real_permission_concurrent_opposite_decisions_have_one_winner(tmp_path: Path) -> None:
    with _real_api(tmp_path) as (base_url, api_main):
        state = _running_task(api_main, "Exercise concurrent product permission decisions.")
        session_id = f"permission-console:{state.task_id}"
        facade = api_main.get_permission_api_facade(task_id=state.task_id, session_id=session_id)
        custody = facade.open_session(
            session_id=session_id,
            run_id=state.run_id,
            task_id=state.task_id,
        )
        token = custody.body["session"]["bearer_token"]

        status, denied_command, _ = _request(
            base_url,
            f"/tasks/{state.task_id}/commands",
            method="POST",
            payload={
                "text": "/e02-reload",
                "actor_id": "product-permission-race",
                "tool_call_id": f"toolcall_{uuid4().hex}",
            },
        )
        assert status == 403, denied_command

        raced = _bun_json(
            """
import {CliApi} from './apps/cli/src/api.ts'; import {CliPermissionSession} from './apps/cli/src/control/permission.ts';
const [baseUrl,taskId,sessionId,token]=process.argv.slice(1); const api=new CliApi({baseUrl,timeoutMs:30000}); const task=await api.task(taskId);
const first=new CliPermissionSession({api,task,sessionId,custodyToken:token}); const second=new CliPermissionSession({api,task,sessionId,custodyToken:token});
const opened=await Promise.all([first.open(),second.open()]); const pending=await first.pending(); const requestId=pending[0].requestId;
const results=await Promise.allSettled([first.resolve({requestId,effect:'allow'}),second.resolve({requestId,effect:'deny'})]); const after=await first.pending();
console.log(JSON.stringify({opened,requestId,after:after.length,results:results.map(item=>item.status==='fulfilled'?{status:item.status,effect:item.value.receipt?.effect}:{status:item.status,httpStatus:item.reason?.status,code:item.reason?.code,message:item.reason?.message})})); api.close();
""",
            base_url,
            state.task_id,
            session_id,
            token,
        )
        assert raced["opened"] == [True, True]
        assert raced["after"] == 0
        fulfilled = [item for item in raced["results"] if item["status"] == "fulfilled"]
        rejected = [item for item in raced["results"] if item["status"] == "rejected"]
        assert len(fulfilled) == 1, raced
        assert len(rejected) == 1, raced
        assert rejected[0]["code"] == "permission_decision_conflict", raced
