from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import psutil
import pytest

from zyra_orchestration.deployment.models import DeploymentProfile
from zyra_orchestration.deployment.node_client import DeploymentNodeClient
from zyra_orchestration.deployment.profiles import default_profile_policies
from zyra_runtime.sandbox_gateway.command_policy import StructuredCommandPolicy
from zyra_runtime.sandbox_gateway.file_policy import GatewayFilePolicy
from zyra_runtime.sandbox_gateway.integration_host import GatewayHostProcessRuntime
from zyra_runtime.sandbox_gateway.integration_policy import GatewayPolicyConfig, GatewayPolicyRuntime

ROOT = Path(__file__).resolve().parents[2]


def _host(root: Path) -> GatewayHostProcessRuntime:
    policy = GatewayPolicyRuntime(
        GatewayPolicyConfig(workspace_root=root),
        command_policy=StructuredCommandPolicy(),
        file_policy=GatewayFilePolicy(),
    )
    return GatewayHostProcessRuntime(policy, allowed_roots=(root,))


def _alive(identity: dict) -> bool:
    try:
        process = psutil.Process(identity['pid'])
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE and abs(process.create_time() - identity['created']) < .01
    except psutil.NoSuchProcess:
        return False


def _stop(identity: dict) -> None:
    if _alive(identity):
        process = psutil.Process(identity['pid'])
        process.kill()
        try:
            process.wait(timeout=5)
        except psutil.TimeoutExpired:
            pass


# Start an actual deployment node and inject only a synthetic host workload at
# construction. Node HTTP auth, watchdog, server teardown, host spawn and OS
# processes are real; no Provider/model success is simulated or asserted.
NODE_HELPER = r'''
import json, sys
from pathlib import Path
import psutil
from zyra_orchestration.deployment import node_server
from tests.integration.test_host_process_shutdown import _host
original = node_server.DeploymentNodeRuntime
hosts = []
def runtime_with_active_host(**kwargs):
    runtime = original(**kwargs)
    host = _host(Path(kwargs['data_root']))
    process = host.start_interactive(executable=sys.executable, argv=('-c', 'import time; time.sleep(120)'), cwd=kwargs['data_root'])
    runtime.test_host = host
    hosts.append(host)
    identity = {'pid': process.pid, 'created': psutil.Process(process.pid).create_time()}
    (Path(kwargs['data_root'])/'host.json').write_text(json.dumps(identity))
    return runtime
node_server.DeploymentNodeRuntime = runtime_with_active_host
result = node_server.run(sys.argv[1:])
try:
    _host(hosts[0].allowed_roots[0])
except RuntimeError as error:
    assert 'closed' in str(error)
else:
    raise AssertionError('Node shutdown admitted a new host owner')
print('scope-fence-verified', flush=True)
raise SystemExit(result)
'''


@pytest.mark.parametrize('shutdown_mode', ['authenticated_http', 'supervisor_loss'])
def test_node_shutdown_stops_owned_host_without_stopping_unrelated_process(tmp_path: Path, shutdown_mode: str) -> None:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    policy = default_profile_policies(base_port=port)[DeploymentProfile.DEVICE]
    secret = os.urandom(32)
    environment = dict(os.environ)
    environment['ZYRA_DEPLOY_NODE_SECRET_B64'] = base64.b64encode(secret).decode()
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
    supervisor = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
    environment['ZYRA_DEPLOYMENT_SUPERVISOR_PID'] = str(supervisor.pid)
    environment['ZYRA_DEPLOYMENT_SUPERVISOR_CREATE_TIME'] = str(psutil.Process(supervisor.pid).create_time())
    identity = None
    node = subprocess.Popen(
        [sys.executable, '-c', NODE_HELPER, '--node-id', 'host-lifetime-node', '--generation-id', 'generation-one', '--data-root', str(tmp_path), '--policy-json', json.dumps(policy.public_dict())],
        cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    client = DeploymentNodeClient(f'http://127.0.0.1:{port}', secret, timeout_seconds=1)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            assert node.poll() is None, node.communicate(timeout=2)
            try:
                client.request('GET', '/health')
                break
            except Exception:
                time.sleep(.05)
        else:
            pytest.fail('Node did not become ready')
        identity = json.loads((tmp_path/'host.json').read_text())
        assert _alive(identity)
        if shutdown_mode == 'authenticated_http':
            receipt = client.request('POST', '/shutdown', accepted_statuses=(202,))
            assert receipt['accepted'] is True
        else:
            supervisor.kill()
            supervisor.wait(timeout=5)
        stdout, stderr = node.communicate(timeout=15)
        assert node.returncode == 0, (stdout, stderr)
        assert not _alive(identity), 'Owned host outlived its deployment node'
        assert unrelated.poll() is None
        assert 'scope-fence-verified' in stdout
        cleanup = next(
            json.loads(line) for line in stdout.splitlines()
            if 'zyra.deployment-host-process-cleanup/v1' in line
        )
        assert cleanup['remaining_process_ids'] == []
        assert any(item['process_id'] == identity['pid'] and item['stopped'] for item in cleanup['processes'])
    finally:
        # Capture even when startup fails after the host is created.
        if identity is None and (tmp_path/'host.json').exists():
            identity = json.loads((tmp_path/'host.json').read_text())
        for process in (node, supervisor, unrelated):
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        if identity:
            _stop(identity)


def test_host_close_fences_spawn_and_settles_a_concurrent_start(tmp_path: Path, monkeypatch) -> None:
    host = _host(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original = subprocess.Popen
    processes = []
    errors = []

    def delayed_spawn(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    def launch():
        try:
            processes.append(host.start_interactive(executable=sys.executable, argv=('-c', 'import time; time.sleep(120)'), cwd=tmp_path))
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(subprocess, 'Popen', delayed_spawn)
    thread = threading.Thread(target=launch)
    thread.start()
    closer = None
    try:
        assert entered.wait(timeout=5)
        closer = threading.Thread(target=host.close)
        closer.start()
        release.set()
        thread.join(timeout=10)
        closer.join(timeout=10)
        assert not thread.is_alive() and not closer.is_alive()
        assert not errors and len(processes) == 1
        assert processes[0].poll() is not None
        assert host.close() == ()
        with pytest.raises(RuntimeError, match='closed'):
            host.start_interactive(executable=sys.executable, argv=('-c', 'pass'), cwd=tmp_path)
        with pytest.raises(RuntimeError, match='closed'):
            host.run(executable=sys.executable, argv=('-c', 'pass'), cwd=tmp_path)
    finally:
        release.set()
        thread.join(timeout=10)
        if closer:
            closer.join(timeout=10)
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            host.release_interactive(process)
