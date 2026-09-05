from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

from .graph import partition_model
from .protocol import MAX_BYTES, decode_tensors
from .runtime import PartitionedInferenceRuntime


def main() -> None:
    parser = argparse.ArgumentParser(description="Zyra ONNX model partition and inference CLI")
    commands = parser.add_subparsers(dest="command", required=True)
    partition = commands.add_parser("partition")
    partition.add_argument("--model", type=Path, required=True)
    partition.add_argument("--output", type=Path, required=True)
    partition.add_argument("--cut-after", type=int, required=True, help="Zero-based operator index")
    partition.add_argument("--model-id", required=True)
    partition.add_argument("--source-url", default="")
    partition.add_argument("--license", default="")
    run = commands.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--model-id", required=True)
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--mode", choices=("auto", "full", "split"), default="auto")
    run.add_argument("--sensitivity", choices=("public", "internal", "sensitive", "restricted"), default="internal")
    run.add_argument("--batch", action="store_true")
    run.add_argument("--latency-sla-ms", type=int, default=30000)
    args = parser.parse_args()
    if args.command == "partition":
        report = partition_model(args.model, args.output, cut_after=args.cut_after,
                                 model_id=args.model_id, source_url=args.source_url, license_id=args.license)
    else:
        if args.input.stat().st_size > MAX_BYTES:
            raise ValueError("input archive exceeds the byte budget")
        inputs = decode_tensors(base64.b64encode(args.input.read_bytes()).decode("ascii"))
        report = PartitionedInferenceRuntime(args.config).infer(
            args.model_id, inputs, mode=args.mode, sensitivity=args.sensitivity,
            batch=args.batch, latency_sla_ms=args.latency_sla_ms,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report = {k: v for k, v in report.items() if k not in {"outputs", "stage_receipts"}}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
