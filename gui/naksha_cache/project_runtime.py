"""Canonical single/multi-source project naming, validation and bounded build."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile


def source_paths(value):
    paths = [os.path.abspath(str(p)) for p in
             (value if isinstance(value, (list, tuple)) else [value])]
    return sorted(paths, key=os.path.normcase)


def project_path(value):
    paths = source_paths(value)
    if len(paths) == 1:
        return Path(paths[0]).with_suffix(".naksha")
    from .source_catalog import build_catalog
    rows, _ = build_catalog(paths)
    signature = hashlib.sha256(rows.tobytes()).hexdigest()[:16]
    return Path(paths[0]).parent / f"naksha_project_{signature}.naksha"


def container_validity(value, *, container_path=None):
    from .dataset_mode import CacheValidity, _source_probe
    from .reader import NakshaPointCacheReader
    from .format import BUILD_FINALIZED
    paths = source_paths(value)
    path = Path(container_path) if container_path is not None else project_path(value)
    bad = CacheValidity(False, "no production .naksha", source_path=str(path),
                        index_path=str(path), pc_path=str(path))
    if not path.is_file():
        return bad
    reader = NakshaPointCacheReader(str(path), load_edits=False)
    try:
        if reader.container is None:
            bad.reason = "legacy derived wrapper requires migration"
            return bad
        idx = reader.index
        if idx.build_state != BUILD_FINALIZED:
            bad.reason = "container base is not finalized"
            return bad
        if paths[0].lower().endswith(".naksha"):
            paths = [bytes(row["path"]).rstrip(b"\0").decode("utf-8") for row in idx.sources]
        if len(paths) != len(idx.sources):
            bad.reason = "source catalog count changed"
            return bad
        for path_source, row in zip(paths, idx.sources):
            pc, _, _, size, _, fingerprint = _source_probe(path_source)
            if (pc != int(row["point_count"]) or size != int(row["file_size"])
                    or fingerprint != int(row["fingerprint"])):
                bad.reason = "source fingerprint changed (stale container)"
                return bad
        hdr = idx.header
        return CacheValidity(True, "single-file BASE_DATA HIT; header-only source validation",
            source_path=str(path), index_path=str(path), pc_path=str(path),
            edit_path=str(path) + ".nakshaedit", total_points=idx.total_points,
            source_point_count=idx.total_points, lod_count=int(hdr["lod_count"]),
            overview_block_id=idx.overview_block_id,
            bounds_min=tuple(idx.bounds_min), bounds_max=tuple(idx.bounds_max),
            crs_wkt=bytes(hdr["crs_wkt"]).rstrip(b"\0").decode("utf-8"),
            pc_bytes=path.stat().st_size, ram_bytes=0)
    finally:
        reader.close()


class NakshaWriter:
    """Production writer: staging pair is private and removed after commit."""
    def __init__(self, output, *, chunk_points=1_000_000, cancel_check=None,
                 report_path=None):
        self.output = Path(output).resolve()
        self.chunk_points = int(chunk_points)
        self.cancel_check = cancel_check
        self.report_path = report_path

    def build(self, paths):
        from .builder import NakshaPointCacheBuilder
        from .index import IndexReader, resolve_point_cache
        from .container_base import repack_base
        paths = source_paths(paths)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        from .source_catalog import build_catalog
        from .disk_admission import estimate_build, DiskAdmissionError
        rows, catalog = build_catalog(paths)
        estimate = estimate_build(catalog.total_points, len(paths))
        # Staging payload and the compressed replacement coexist until commit.
        # Allow worst-case compression growth, derived payload and scratch.
        required = int(estimate["final_pc"] * 2.05 + estimate["shards"]
            + estimate["checkpoint"] + estimate["index"] * 3
            + estimate["safety"] + catalog.total_points * 12)
        available = shutil.disk_usage(self.output.parent).free
        preflight = dict(passed=available >= required, source_bytes=sum(os.path.getsize(p) for p in paths),
            total_points=catalog.total_points, source_count=len(paths), estimate=estimate,
            estimated_naksha_bytes=int(estimate["final_pc"] * 1.05),
            atomic_overlap_bytes=estimate["final_pc"], derived_bytes=catalog.total_points * 12,
            available_disk_bytes=available, required_disk_bytes=required)
        print("[NAKSHA DISK PREFLIGHT]", preflight, flush=True)
        if not preflight["passed"]:
            raise OSError(f"NAKSHA disk preflight failed: requires {required:,} bytes; available {available:,}")
        from .derived_container import exclusive_writer
        with exclusive_writer(self.output):
            return self._build_locked(paths, preflight)

    def _build_locked(self, paths, preflight):
        from .builder import NakshaPointCacheBuilder
        from .index import IndexReader, resolve_point_cache
        from .container_base import repack_base
        work = Path(tempfile.mkdtemp(prefix=".naksha-build-", dir=self.output.parent)).resolve()
        staging = work / "base"
        builder = NakshaPointCacheBuilder(str(staging), output_path=str(staging),
            scratch_path=str(work), chunk_points=self.chunk_points,
            cancel_check=self.cancel_check)
        stats = builder.build(paths, with_overview=True)
        idx = IndexReader(builder.idx_path)
        pc = resolve_point_cache(builder.idx_path, idx.point_cache_name, builder.pc_path)
        idx.close()
        class Cancellation:
            def is_set(inner):
                return bool(self.cancel_check and self.cancel_check())
        compression = repack_base(builder.idx_path, pc, self.output,
            report_path=self.report_path, cancel=Cancellation())
        validity = container_validity(str(self.output))
        if not validity.ok:
            raise ValueError(validity.reason)
        # Only this writer's explicitly created private staging directory.
        if work.parent != self.output.parent or not work.name.startswith(".naksha-build-"):
            raise ValueError("unsafe staging cleanup target")
        shutil.rmtree(work)
        return dict(stats=stats, compression=compression, preflight=preflight, path=str(self.output),
                    committed=True, total_points=validity.total_points)
