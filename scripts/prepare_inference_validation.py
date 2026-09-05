"""Fetch pinned public model/data and prepare a real held-out inference task."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import struct
from pathlib import Path
from urllib.request import urlopen

import numpy as np

from zyra_runtime.inference.graph import partition_model


SOURCES = {
    "mnist-12.onnx": (
        "https://media.githubusercontent.com/media/onnx/models/main/validated/vision/classification/mnist/model/mnist-12.onnx",
        "5c688690f8bacf667d4c2074af5ad0646ca328d7ab03eccf944a65b320171bdd"),
    "t10k-images-idx3-ubyte.gz": (
        "https://storage.googleapis.com/cvdf-datasets/mnist/t10k-images-idx3-ubyte.gz",
        "8d422c7b0a1c1c79245a5bcf07fe86e33eeafee792b84584aec276f5a2dbc4e6"),
    "t10k-labels-idx1-ubyte.gz": (
        "https://storage.googleapis.com/cvdf-datasets/mnist/t10k-labels-idx1-ubyte.gz",
        "f7ae60f92e00ec6debd23a6088c31dbd2371eca3ffa0defaefb259924204aec6"),
}


def prepare(root: Path, *, count: int = 256, offset: int = 137) -> dict:
    if count < 1 or offset < 0 or offset + count > 10000:
        raise ValueError("requested samples are outside the MNIST test set")
    root.mkdir(parents=True, exist_ok=True)
    for name, (url, expected) in SOURCES.items():
        path = root / name
        if not path.exists():
            with urlopen(url, timeout=60) as response:
                data = response.read(10_000_001)
            if len(data) > 10_000_000 or hashlib.sha256(data).hexdigest() != expected:
                raise ValueError(f"download identity mismatch: {name}")
            path.write_bytes(data)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"cached download identity mismatch: {name}")
    bundle = root / "bundle"
    if not bundle.exists():
        partition_model(root / "mnist-12.onnx", bundle, cut_after=3, model_id="mnist-cnn",
                        source_url=SOURCES["mnist-12.onnx"][0], license_id="MIT")
    images_raw = gzip.decompress((root / "t10k-images-idx3-ubyte.gz").read_bytes())
    labels_raw = gzip.decompress((root / "t10k-labels-idx1-ubyte.gz").read_bytes())
    if struct.unpack(">IIII", images_raw[:16]) != (2051, 10000, 28, 28):
        raise ValueError("unexpected MNIST image header")
    if struct.unpack(">II", labels_raw[:8]) != (2049, 10000):
        raise ValueError("unexpected MNIST label header")
    images = np.frombuffer(images_raw[16:], dtype=np.uint8).reshape(10000, 1, 28, 28)
    labels = np.frombuffer(labels_raw[8:], dtype=np.uint8)
    task = root / "task"
    task.mkdir(exist_ok=True)
    np.savez(task / "images.npz", Input3=images[offset:offset + count].astype(np.float32) / 255)
    (task / "labels.json").write_text(json.dumps(labels[offset:offset + count].tolist()), encoding="utf-8")
    metadata = {"schema": "zyra.mnist-validation-input/v1", "count": count, "test_set_offset": offset,
                "dataset": "MNIST official held-out test set (CVDF mirror)",
                "model": "ONNX Model Zoo pretrained MNIST CNN", "model_license": "MIT",
                "preprocessing": "uint8 / 255 -> float32 [N,1,28,28]; each inference uses [1,1,28,28]",
                "sources": {name: {"url": url, "sha256": digest} for name, (url, digest) in SOURCES.items()}}
    (task / "input-metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--count", type=int, default=256)
    parser.add_argument("--offset", type=int, default=137)
    args = parser.parse_args()
    print(json.dumps(prepare(args.root.resolve(), count=args.count, offset=args.offset), indent=2))
