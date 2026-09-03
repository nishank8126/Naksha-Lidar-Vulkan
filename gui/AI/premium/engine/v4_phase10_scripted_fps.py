#!/usr/bin/env python3
"""TorchScript FPS candidate for V4 Phase-10 GPU exactness benchmark."""
from __future__ import annotations

import torch


@torch.jit.script
def farthest_point_sample_scripted(xyz: torch.Tensor, npoint: int) -> torch.Tensor:
    """Same operations/order as frozen model.py farthest_point_sample, scripted only."""
    device = xyz.device
    b = xyz.shape[0]
    n = xyz.shape[1]

    centroids = torch.zeros((b, npoint), dtype=torch.long, device=device)
    distance = torch.full((b, n), 1e10, dtype=torch.float32, device=device)
    farthest = torch.randint(0, n, (b,), dtype=torch.long, device=device)
    batch_idx = torch.arange(b, dtype=torch.long, device=device)

    # Frozen model disables autocast inside FPS and converts xyz to float32.
    # Using an explicit float32 tensor preserves the arithmetic contract while
    # allowing TorchScript to own the fixed Python loop.
    xyz_f32 = xyz.float()
    for i in range(npoint):
        centroids[:, i] = farthest
        centroid = xyz_f32[batch_idx, farthest, :].unsqueeze(1)
        dist = torch.sum((xyz_f32 - centroid) ** 2, dim=-1)
        distance = torch.min(distance, dist)
        farthest = torch.max(distance, dim=-1)[1]

    return centroids
