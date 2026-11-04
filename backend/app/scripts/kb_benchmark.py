"""Baseline numbers for the in-process knowledge index, to compare a new backend against.

Synthetic passages (random vectors), so it measures the index, not retrieval quality
(tests/golden_knowledge.py covers that). No API calls, no database.

    python -m app.scripts.kb_benchmark --passages 20000 --queries 50
"""

from __future__ import annotations

import argparse
import time

import numpy as np

from app.core.config import get_settings
from app.ingestion.index import IndexedPassage, SearchIndex


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--passages", type=int, default=6000)
    parser.add_argument("--queries", type=int, default=30)
    args = parser.parse_args()

    dims = get_settings().embedding_dimensions
    rng = np.random.default_rng(1)
    passages = [
        IndexedPassage(f"G1.{i}", f"topic{i % 50} details about subject {i % 200} item{i}", rng.random(dims).tolist())
        for i in range(args.passages)
    ]
    started = time.perf_counter()
    index = SearchIndex(passages, dims)
    build = time.perf_counter() - started

    timings = []
    for n in range(args.queries):
        started = time.perf_counter()
        index.search(f"topic{n % 50} details subject {n % 200}", query_vector=rng.random(dims).tolist())
        timings.append((time.perf_counter() - started) * 1000)
    timings.sort()
    print(f"passages={args.passages} dims={dims}")
    print(f"index build: {build:.2f}s   vector matrix: {index._matrix.nbytes / 1e6:.0f} MB")  # noqa: SLF001
    print(f"query ms: median={timings[len(timings) // 2]:.1f} p95={timings[int(len(timings) * 0.95) - 1]:.1f} max={timings[-1]:.1f}")


if __name__ == "__main__":
    main()
