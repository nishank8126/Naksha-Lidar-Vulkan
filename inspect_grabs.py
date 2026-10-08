"""Inspect the full-desktop grabs the harness saved when a crop came back black.

Tells the two possibilities apart: either the window really was black on the
monitor (a present/composition fault), or the captured rectangle no longer
matched where the window is (a harness race). Prints the bounding box of every
non-black pixel in each .full.png.
"""
import glob
import os
import sys

import numpy as np
from PIL import Image

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vulkan_resize_output")
paths = sorted(glob.glob(os.path.join(OUT, "*.full.png"))) + sorted(
    p for p in glob.glob(os.path.join(OUT, "*.png")) if not p.endswith(".full.png"))
if not paths:
    print("no pngs")
    sys.exit(1)
for p in paths:
    a = np.asarray(Image.open(p).convert("RGB")).astype(np.int16)
    nz = np.abs(a).max(axis=2) > 12
    ys, xs = np.nonzero(nz)
    box = ("-" if not len(xs) else
           f"content bbox x{int(xs.min())}-{int(xs.max())} y{int(ys.min())}-{int(ys.max())}")
    print(f"{os.path.basename(p):56s} {a.shape[1]}x{a.shape[0]} "
          f"std={a.std():6.2f} nonblack={nz.mean() * 100:6.2f}% {box}", flush=True)
