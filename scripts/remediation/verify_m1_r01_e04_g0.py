"""Stable entry point for the immutable M1-R01 Execution 04 G0 verifier."""

from __future__ import annotations

import json

from m1_r01_e04_g0 import verify


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, sort_keys=True, indent=2))
