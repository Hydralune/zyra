from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


SCHEMA = "zyra.onnx-partition/v1"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_bundle(path: str | Path) -> dict[str, Any]:
    root = Path(path).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA:
        raise ValueError("unsupported inference bundle schema")
    for part in ("full", "front", "back"):
        item = manifest["parts"][part]
        candidate = (root / item["file"]).resolve()
        if candidate.parent != root or candidate.suffix != ".onnx":
            raise ValueError("model must be an ONNX file directly inside the bundle")
        if sha256(candidate) != item["sha256"]:
            raise ValueError(f"model digest mismatch: {part}")
        if candidate.stat().st_size != item["bytes"]:
            raise ValueError(f"model byte size mismatch: {part}")
    return manifest


def partition_model(
    source: str | Path,
    destination: str | Path,
    *,
    cut_after: int,
    model_id: str,
    source_url: str = "",
    license_id: str = "",
) -> dict[str, Any]:
    """Cut a flat topologically ordered graph, including every crossing tensor.

    Weights remain initializers, never a fabricated activation. Control-flow
    subgraphs and external-data models require a separate packaging contract.
    """
    import onnx

    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("partition destination must be empty")
    model = onnx.load(source, load_external_data=False)
    if any(t.external_data for t in model.graph.initializer):
        raise ValueError("external-data models are not supported by this bundle version")
    if {v.name for v in model.graph.input}.intersection(t.name for t in model.graph.initializer):
        raise ValueError("overridable initializer inputs require freezing before partitioning")
    if any(a.type in (onnx.AttributeProto.GRAPH, onnx.AttributeProto.GRAPHS)
           for n in model.graph.node for a in n.attribute):
        raise ValueError("control-flow subgraphs cannot be cut by this flat-graph partitioner")
    onnx.checker.check_model(model)
    model = onnx.shape_inference.infer_shapes(model, strict_mode=True)
    nodes = list(model.graph.node)
    if not 0 <= cut_after < len(nodes) - 1:
        raise ValueError("cut must leave at least one operator in each partition")
    inputs = [v.name for v in model.graph.input
              if v.name not in {t.name for t in model.graph.initializer}]
    outputs = [v.name for v in model.graph.output]
    prefix_outputs = {v for n in nodes[:cut_after + 1] for v in n.output if v}
    suffix_inputs = {v for n in nodes[cut_after + 1:] for v in n.input if v}
    boundary = sorted(prefix_outputs.intersection(suffix_inputs | set(outputs)))
    if not prefix_outputs.intersection(suffix_inputs):
        raise ValueError("cut has no intermediate activation consumed by the back partition")
    prefix_inputs = {v for n in nodes[:cut_after + 1] for v in n.input if v}
    front_inputs = [v for v in inputs if v in prefix_inputs]
    back_inputs = [*boundary, *[v for v in inputs if v in suffix_inputs]]
    extractor = onnx.utils.Extractor(model)
    front = extractor.extract_model(front_inputs, boundary)
    back = extractor.extract_model(back_inputs, outputs)
    # Both partitions must consist solely of their designated original nodes.
    # This rejects unnoticed recomputation caused by an incomplete boundary.
    def encoded(ns):
        return {n.SerializeToString() for n in ns}
    if (not encoded(front.graph.node).issubset(encoded(nodes[:cut_after + 1]))
            or not encoded(back.graph.node).issubset(encoded(nodes[cut_after + 1:]))):
        raise ValueError("partition extraction crossed the declared cut")
    if not front.graph.node or not back.graph.node:
        raise ValueError("both partitions must perform model computation")
    destination.mkdir(parents=True, exist_ok=True)
    parts = {}
    def contract(values):
        return [{"name": value.name, "dtype": onnx.TensorProto.DataType.Name(value.type.tensor_type.elem_type),
                 "shape": [d.dim_value if d.HasField("dim_value") else d.dim_param or None
                           for d in value.type.tensor_type.shape.dim]} for value in values]

    for name, graph in (("full", model), ("front", front), ("back", back)):
        onnx.checker.check_model(graph)
        path = destination / f"{name}.onnx"
        onnx.save_model(graph, path)
        parts[name] = {
            "file": path.name, "sha256": sha256(path), "bytes": path.stat().st_size,
            "operator_count": len(graph.graph.node),
            "inputs": [v.name for v in graph.graph.input],
            "outputs": [v.name for v in graph.graph.output],
            "input_contract": contract(graph.graph.input),
            "output_contract": contract(graph.graph.output),
        }
    manifest = {
        "schema": SCHEMA, "model_id": model_id,
        "source_sha256": sha256(source), "source_url": source_url, "license": license_id,
        "cut_after": cut_after, "boundary_tensors": boundary,
        "raw_inputs_required_by_back": [v for v in inputs if v in suffix_inputs],
        "parts": parts,
        "privacy_note": "Intermediate activations retain the input sensitivity classification.",
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest
