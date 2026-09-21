"""Regression test for the MONASTERO "double block line" (NT-298).

_open_polygon_xy used a closure tolerance scaled by coordinate magnitude
(mag * 1e-6). On UTM northings (~4.42e6) that is 4.4 m, so it deleted the
final *real* vertex of any ring closing within 4.4 m of its start. That made
the block's vertex count disagree with its PRJ twin, _polygon_alignment_error
returned inf, and that one block missed PRJ precision correction while its
neighbours got it -- so the edge they share stopped being coincident and drew
as two lines.

Coordinates below are the real MONASTERO000003 ring start/end.
"""
from __future__ import annotations

import os
import re
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(__file__))
SRC = os.path.join(ROOT, "gui", "snt_attachment.py")


def _load_helper(name: str):
    """Exec one top-level function out of snt_attachment.py without importing Qt."""
    lines = open(SRC, encoding="utf-8").read().splitlines()
    start = max(i for i, l in enumerate(lines) if l.startswith(f"def {name}("))
    end = next(i for i in range(start + 1, len(lines))
               if lines[i] and not lines[i][0].isspace())
    ns = {"np": np, "Dict": dict}
    tol = [l for l in lines if l.startswith("_CLOSING_VERTEX_TOL_M =")][-1]
    exec(tol, ns)
    exec("\n".join(lines[start:end]), ns)
    return ns[name], ns["_CLOSING_VERTEX_TOL_M"]


def test_closure_tolerance_is_absolute_not_magnitude_scaled():
    _, tol = _load_helper("_open_polygon_xy")
    # Must stay far below the spacing of real surveyed vertices.
    assert tol <= 0.01, f"closure tolerance {tol} m is too coarse for survey data"


def test_real_closing_vertex_is_not_deleted():
    open_polygon_xy, _ = _load_helper("_open_polygon_xy")
    # MONASTERO000003: the ring's last vertex sits 0.625 m from its first.
    ring = [
        (568120.6989, 4421400.6039),   # first
        (568117.5850, 4421400.8423),
        (568122.1652, 4421460.6672),
        (568198.7155, 4421459.7889),
        (568207.0398, 4421387.5857),
        (568120.0431, 4421400.4780),   # last -- 0.625 m from first, MUST survive
    ]
    out = open_polygon_xy(ring)
    assert len(out) == len(ring), (
        f"a real vertex 0.625 m from the ring start was deleted "
        f"({len(ring)} -> {len(out)})"
    )


def test_exact_duplicate_closing_vertex_is_still_stripped():
    open_polygon_xy, _ = _load_helper("_open_polygon_xy")
    ring = [
        (568120.6989, 4421400.6039),
        (568117.5850, 4421400.8423),
        (568122.1652, 4421460.6672),
        (568120.6989, 4421400.6039),   # literal repeat of the first
    ]
    out = open_polygon_xy(ring)
    assert len(out) == 3, f"duplicate closing vertex not stripped ({len(out)})"


if __name__ == "__main__":
    test_closure_tolerance_is_absolute_not_magnitude_scaled()
    test_real_closing_vertex_is_not_deleted()
    test_exact_duplicate_closing_vertex_is_still_stripped()
    print("OK")
