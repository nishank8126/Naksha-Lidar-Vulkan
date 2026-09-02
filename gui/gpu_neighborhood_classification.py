"""Optional CUDA neighborhood engines for LiDAR noise classification.

The public helpers return ``None`` when CUDA is unavailable or the Cartesian
query/reference workload is too large for a bounded brute-force pass. Callers
then retain their established scipy.cKDTree implementation.
"""

from __future__ import annotations

import os
import time

import numpy as np


_MAX_GPU_PAIRS = 2_000_000_000
_MAX_RADIUS_GPU_PAIRS = 250_000_000
_TARGET_DISTANCE_BYTES = 384 * 1024 * 1024


def _cuda_torch():
    if str(os.environ.get("NAKSHA_GPU_CLASSIFICATION", "1")).lower() in {
        "0", "false", "no", "off",
    }:
        return None
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        return torch
    except Exception:
        return None


def _batch_size(reference_count, requested, bytes_per_pair=8):
    by_memory = _TARGET_DISTANCE_BYTES // max(
        1, int(reference_count) * int(bytes_per_pair)
    )
    return max(1, min(int(requested), int(by_memory)))


def cuda_radius_counts(
    xyz,
    candidate_idx,
    reference_idx,
    radius,
    *,
    dimensions,
    candidate_is_reference,
    batch_size,
    progress_cb=None,
    abort_check=None,
):
    """Count other reference points inside each candidate's CUDA sphere."""
    torch = _cuda_torch()
    candidates = np.asarray(candidate_idx, dtype=np.intp)
    references = np.asarray(reference_idx, dtype=np.intp)
    pair_count = int(len(candidates)) * int(len(references))
    if (
        torch is None
        or not len(candidates)
        or not len(references)
        or pair_count > _MAX_RADIUS_GPU_PAIRS
    ):
        return None

    dims = int(dimensions)
    origin = np.asarray(xyz[references[0], :dims], dtype=np.float64)
    ref_np = np.asarray(xyz[references, :dims] - origin, dtype=np.float32)
    chunk = _batch_size(len(references), batch_size, bytes_per_pair=6)
    device = torch.device("cuda:0")
    started = time.perf_counter()

    try:
        ref_gpu = torch.as_tensor(ref_np, device=device)
        counts = np.empty(len(candidates), dtype=np.int32)
        processed = 0
        aborted = False
        for start in range(0, len(candidates), chunk):
            if abort_check and abort_check():
                aborted = True
                break
            stop = min(start + chunk, len(candidates))
            current = candidates[start:stop]
            query_np = np.asarray(
                xyz[current, :dims] - origin,
                dtype=np.float32,
            )
            query_gpu = torch.as_tensor(query_np, device=device)
            distances = torch.cdist(query_gpu, ref_gpu, p=2)
            batch_counts = torch.count_nonzero(
                distances <= float(radius), dim=1
            ).to(dtype=torch.int64)
            batch_counts = batch_counts.cpu().numpy().astype(np.int32, copy=False)
            batch_counts -= np.asarray(
                candidate_is_reference[current], dtype=np.int32
            )
            counts[start:stop] = batch_counts
            processed = stop
            if progress_cb:
                progress_cb(
                    min(95, 5 + int(90 * stop / len(candidates))),
                    f"CUDA counted {stop:,}/{len(candidates):,} candidates",
                )
            del query_gpu, distances, batch_counts

        torch.cuda.synchronize(device)
        return {
            "counts": counts[:processed],
            "processed": processed,
            "aborted": aborted,
            "backend": "cuda_torch",
            "seconds": time.perf_counter() - started,
            "batch_size": chunk,
        }
    except (RuntimeError, MemoryError) as exc:
        print(f"GPU_CLASSIFICATION isolated fallback=cpu reason={exc}")
        return None
    finally:
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass


def cuda_low_point_tiers(
    xyz,
    candidate_idx,
    source_idx,
    candidate_allowed,
    radius,
    separation,
    group_limit,
    mode,
    *,
    batch_size,
    progress_cb=None,
    abort_check=None,
):
    """Evaluate Low Points radius/tier rules on CUDA with bounded batches."""
    torch = _cuda_torch()
    candidates = np.asarray(candidate_idx, dtype=np.intp)
    sources = np.asarray(source_idx, dtype=np.intp)
    pair_count = int(len(candidates)) * int(len(sources))
    if (
        torch is None
        or not len(candidates)
        or len(sources) < 2
        or pair_count > _MAX_GPU_PAIRS
    ):
        return None

    origin_xy = np.asarray(xyz[sources[0], :2], dtype=np.float64)
    origin_z = float(np.min(xyz[sources, 2]))
    src_xy = np.asarray(xyz[sources, :2] - origin_xy, dtype=np.float32)
    src_z = np.asarray(xyz[sources, 2] - origin_z, dtype=np.float32)
    chunk = _batch_size(len(sources), batch_size, bytes_per_pair=14)
    device = torch.device("cuda:0")
    accepted = np.zeros(len(xyz), dtype=bool)
    no_surrounding = 0
    evaluated = 0
    processed = 0
    aborted = False
    started = time.perf_counter()

    try:
        src_xy_gpu = torch.as_tensor(src_xy, device=device)
        src_z_gpu = torch.as_tensor(src_z, device=device)
        source_global_gpu = torch.as_tensor(
            sources.astype(np.int64, copy=False), device=device
        )
        top_count = min(len(sources), int(group_limit) + 1)

        for start in range(0, len(candidates), chunk):
            if abort_check and abort_check():
                aborted = True
                break
            stop = min(start + chunk, len(candidates))
            current = candidates[start:stop]
            query_xy = torch.as_tensor(
                np.asarray(xyz[current, :2] - origin_xy, dtype=np.float32),
                device=device,
            )
            distances = torch.cdist(query_xy, src_xy_gpu, p=2)
            inside = distances <= float(radius)
            neighbor_counts = torch.count_nonzero(inside, dim=1)

            if mode == "single":
                not_self = source_global_gpu.unsqueeze(0) != torch.as_tensor(
                    current.astype(np.int64, copy=False), device=device
                ).unsqueeze(1)
                other_inside = inside & not_self
                other_counts = torch.count_nonzero(other_inside, dim=1)
                z_values = src_z_gpu.unsqueeze(0).expand(len(current), -1)
                other_min = torch.amin(
                    z_values.masked_fill(~other_inside, float("inf")), dim=1
                )
                query_z = torch.as_tensor(
                    np.asarray(xyz[current, 2] - origin_z, dtype=np.float32),
                    device=device,
                )
                valid = other_counts > 0
                accepted_current = valid & (
                    (other_min - query_z) > float(separation)
                )
                accepted[current[accepted_current.cpu().numpy()]] = True
                no_surrounding += int(torch.count_nonzero(~valid).item())
                evaluated += int(torch.count_nonzero(valid).item())
                del not_self, other_inside, other_counts, other_min, query_z
            else:
                z_values = src_z_gpu.unsqueeze(0).expand(len(current), -1)
                masked_z = z_values.masked_fill(~inside, float("inf"))
                top_z, top_local = torch.topk(
                    masked_z,
                    k=top_count,
                    dim=1,
                    largest=False,
                    sorted=True,
                )
                counts_cpu = neighbor_counts.cpu().numpy()
                top_z_cpu = top_z.cpu().numpy()
                top_local_cpu = top_local.cpu().numpy()
                for row, center_global in enumerate(current):
                    if counts_cpu[row] <= 1:
                        no_surrounding += 1
                        continue
                    evaluated += 1
                    values = top_z_cpu[row]
                    finite_pairs = np.isfinite(values[:-1]) & np.isfinite(values[1:])
                    clear = np.flatnonzero(
                        finite_pairs
                        & (np.diff(values) > float(separation))
                    )
                    if not len(clear):
                        continue
                    low_count = int(clear[0]) + 1
                    low_global = sources[top_local_cpu[row, :low_count]]
                    if np.any(low_global == center_global):
                        accepted[low_global[candidate_allowed[low_global]]] = True
                del masked_z, top_z, top_local

            processed = stop
            if progress_cb:
                progress_cb(
                    min(95, 12 + int(83 * stop / len(candidates))),
                    f"CUDA checked {stop:,}/{len(candidates):,}; "
                    f"accepted {int(np.count_nonzero(accepted)):,}",
                )
            del query_xy, distances, inside, neighbor_counts, z_values

        torch.cuda.synchronize(device)
        return {
            "detected": np.flatnonzero(accepted).astype(np.intp, copy=False),
            "processed": processed,
            "no_surrounding": no_surrounding,
            "evaluated": evaluated,
            "aborted": aborted,
            "backend": "cuda_torch",
            "seconds": time.perf_counter() - started,
            "batch_size": chunk,
        }
    except (RuntimeError, MemoryError) as exc:
        print(f"GPU_CLASSIFICATION low_points fallback=cpu reason={exc}")
        return None
    finally:
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass
