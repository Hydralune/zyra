"""Owned local process lifecycle for reproducible validation, not physical hosts."""
from __future__ import annotations

import json
import os
import queue
import secrets
import subprocess
import sys
import threading
from pathlib import Path

import psutil

from .graph import load_bundle


class LocalInferenceCluster:
    def __init__(self, bundle: Path, root: Path, *, constrain_device: bool = False):
        self.bundle, self.root = bundle.resolve(), root.resolve()
        self.constrain_device = constrain_device
        self.token = secrets.token_urlsafe(48)
        self.processes = []
        self.identities = []
        self.logs = []
        self.config_path = self.root / "inference.json"

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        manifest = load_bundle(self.bundle)
        config = {"schema": "zyra.inference-config/v1", "minimum_sensitivity": "public",
                  "maximum_samples": 1000, "timeout_seconds": 15,
                  "models": {manifest["model_id"]: {"bundle": str(self.bundle)}}, "nodes": {}}
        try:
            for location, parts in (("device", "full,front"), ("edge", "back")):
                command = [sys.executable, "-m", "zyra_runtime.inference.node", "--bundle", str(self.bundle),
                           "--node-id", f"local-{location}", "--location", location, "--parts", parts]
                if location == "device" and self.constrain_device:
                    command += ["--max-model-bytes", str(manifest["parts"]["front"]["bytes"])]
                environment = {k: v for k, v in os.environ.items() if not k.endswith("API_KEY")}
                environment["ZYRA_INFERENCE_TOKEN"] = self.token
                log = (self.root / f"{location}.stderr.log").open("w", encoding="utf-8")
                self.logs.append(log)
                process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=log,
                                           text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.processes.append(process)
                ready_queue = queue.Queue()
                threading.Thread(target=lambda p=process, q=ready_queue: q.put(p.stdout.readline()), daemon=True).start()
                line = ready_queue.get(timeout=30)
                if not line:
                    raise RuntimeError(f"{location} inference process failed; see its stderr log")
                ready = json.loads(line)
                self.identities.append(ready["identity"])
                config["nodes"][location] = {"url": f"http://127.0.0.1:{ready['port']}",
                    "allowed_sensitivity": ["public", "internal"], "cold_compute_estimate_ms": 1}
            self.config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            (self.root / "processes.json").write_text(json.dumps(self.identities, indent=2), encoding="utf-8")
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        for process in reversed(self.processes):
            try:
                descendants = psutil.Process(process.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                descendants = []
            for child in reversed(descendants):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
            process.stdout.close()
            psutil.wait_procs(descendants, timeout=5)
        for log in self.logs:
            log.close()
