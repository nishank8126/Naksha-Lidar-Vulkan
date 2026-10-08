"""Single-owner build lifecycle and the authoritative .buildstate manifest.

Completed work is immutable and checksummed. Recovery validates before
truncating uncheckpointed tails. Cancellation is polled only at safe units.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import time
import uuid
from pathlib import Path

import numpy as np

from . import format as F
from .global_stats import GlobalStats
from .index import IndexWriter

MANIFEST_VERSION = 1
BUILDER_VERSION = "7B-7E/1"
STAGES = ("SPATIALIZE", "SHARDS", "FINALIZE", "OVERVIEW", "STATS",
          "POINT_CACHE_VALIDATE", "INDEX_VALIDATE", "COMMIT")


class BuildSafetyError(RuntimeError):
    pass


class BuildCancelled(BuildSafetyError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest_file(path, offset=0, length=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        f.seek(offset)
        remaining = length
        while remaining is None or remaining:
            chunk = f.read((4 << 20) if remaining is None else min(4 << 20, remaining))
            if not chunk:
                if remaining:
                    raise BuildSafetyError(f"truncated work artifact: {path}")
                break
            h.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return h.hexdigest()


def owner_state(info):
    if info.get("hostname") != socket.gethostname():
        return "UNKNOWN"
    try:
        import psutil
        p = psutil.Process(int(info["pid"]))
        # PID reuse is definitely not the recorded owner.
        if abs(p.create_time() - float(info["process_start_time"])) > .01:
            return "DEAD"
        return "ALIVE" if p.is_running() else "DEAD"
    except (KeyError, ValueError, TypeError):
        return "UNKNOWN"
    except ImportError:
        return "UNKNOWN"
    except Exception as exc:
        import psutil
        return "DEAD" if isinstance(exc, psutil.NoSuchProcess) else "UNKNOWN"


class BuildLock:
    def __init__(self, path, identity):
        import psutil
        self.path = path
        self.token = uuid.uuid4().hex
        self.info = dict(identity, lock_format_version=1, pid=os.getpid(),
                         hostname=socket.gethostname(),
                         process_start_time=psutil.Process().create_time(),
                         build_start_time=time.time(), builder_version=BUILDER_VERSION,
                         token=self.token)
        self.held = False

    def acquire(self, *, recover_stale=False, validate=None):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                old = json.loads(Path(self.path).read_text(encoding="utf-8"))
            except Exception as exc:
                raise BuildSafetyError("build lock unreadable; explicit inspection required") from exc
            state = owner_state(old)
            detail = (f"PID: {old.get('pid')} hostname: {old.get('hostname')} "
                      f"started: {old.get('build_start_time')} source: {old.get('source_paths')} "
                      f"target: {old.get('target_output_path')}")
            if state != "DEAD":
                raise BuildSafetyError(f"BUILD ALREADY IN PROGRESS (owner {state})\n{detail}")
            if not recover_stale or validate is None:
                raise BuildSafetyError(f"STALE BUILD LOCK detected; explicit validated recovery required\n{detail}")
            guard = self.path + ".recovery"
            try:
                recovery_fd = os.open(guard, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise BuildSafetyError("stale-lock recovery already in progress; inspect recovery guard") from exc
            try:
                for key in ("dataset_fingerprint", "layout_fingerprint", "target_output_path", "scratch_path"):
                    if old.get(key) != self.info[key]:
                        raise BuildSafetyError("stale lock identity mismatch: " + key)
                validate()
                if json.loads(Path(self.path).read_text(encoding="utf-8")) != old:
                    raise BuildSafetyError("build lock changed during recovery")
                os.unlink(self.path)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                print("STALE BUILD LOCK RECOVERED", flush=True)
            finally:
                os.close(recovery_fd)
                os.unlink(guard)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(canonical(self.info))
                f.flush()
                os.fsync(f.fileno())
            self.held = True
        except BaseException:
            # An incompletely written lock remains for diagnosis on crash.
            raise

    def release(self):
        if self.held:
            info = json.loads(Path(self.path).read_text(encoding="utf-8"))
            if info.get("token") != self.token:
                raise BuildSafetyError("build lock ownership changed; refusing release")
            os.unlink(self.path)
            self.held = False


def source_identity(paths, rows):
    out = []
    for path, row in zip(paths, rows):
        st = os.stat(path)
        # Include the complete header/VLR region, not just the catalog's short
        # head/tail sample. ctime/mtime_ns catch replacements and interior edits.
        import struct
        with open(path, "rb") as f:
            raw = f.read(100)
        point_offset = struct.unpack_from("<I", raw, 96)[0]
        out.append({"path": os.path.normcase(os.path.realpath(path)),
                    "catalog_row": row.tobytes().hex(),
                    "header_sha256": digest_file(path, 0, point_offset),
                    "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                    "ctime_ns": st.st_ctime_ns, "file_id": st.st_ino,
                    "source_file_id": int(row["source_id"]),
                    "content_sig": int(row["content_sig"]),
                    "point_count": int(row["point_count"]),
                    "expected_point_base": int(row["point_base"])})
    return out


class BuildSession:
    def __init__(self, builder, paths, with_overview, resume, recover_stale):
        self.b = builder
        self.paths = list(paths)
        self.manifest_path = builder.idx_path + ".buildstate"
        self.iw = IndexWriter(builder.idx_path)
        self.config = {key: getattr(builder, key) for key in
                       ("chunk_points", "leaf_target", "overview_target", "max_leaf_extent",
                        "min_leaf_points", "ordering", "lod_levels", "lod_ratio")}
        self.config["with_overview"] = bool(with_overview)
        self.identity = source_identity(paths, builder.sources)
        self.dataset_fp = hashlib.sha256(canonical(self.identity)).hexdigest()
        self.versions = {name: value for name, value, _ in F.SEMANTIC_VERSIONS}
        self.layout_fp = F.layout_fingerprint(versions=self.versions,
                                            shard_depth=0, max_depth=0, node_count=0, block_count=0,
                                            lod_count=builder.lod_levels,
                                            attribute_mask=builder.attribute_mask,
                                            bounds_min=builder.gmin, bounds_max=builder.gmax)
        self.lock = BuildLock(builder.idx_path + ".nakshabuild.lock", {
            "dataset_fingerprint": self.dataset_fp, "layout_fingerprint": self.layout_fp,
            "target_cache_format_version": F.IDX_VERSION, "source_count": len(paths),
            "source_paths": [os.path.abspath(p) for p in paths],
            "target_output_path": os.path.abspath(builder.idx_path),
            "scratch_path": os.path.abspath(builder.temp_dir)})
        self.resume = bool(resume)
        self.recover_stale = bool(recover_stale)
        self.m = None
        self.checkpoint_times = []
        self.pending_hashes = {}
        self.last_checkpoint = time.monotonic()
        self.pending_bytes = 0

    def load(self):
        try:
            m = json.loads(Path(self.manifest_path).read_text(encoding="utf-8"))
            signature = m.pop("manifest_sha256")
            if hashlib.sha256(canonical(m)).hexdigest() != signature:
                raise ValueError("manifest checksum mismatch")
            if m["build_manifest_version"] != MANIFEST_VERSION:
                raise ValueError("manifest version mismatch")
            for key, expected in (("dataset_fingerprint", self.dataset_fp),
                    ("layout_fingerprint", self.layout_fp), ("semantic_versions", self.versions),
                    ("cache_format", F.IDX_VERSION), ("config", self.config),
                    ("sources_identity", self.identity), ("builder_version", BUILDER_VERSION),
                    ("output_path", os.path.abspath(self.b.idx_path)),
                    ("scratch_path", os.path.abspath(self.b.temp_dir))):
                if m[key] != expected:
                    raise ValueError("resume identity/version mismatch: " + key)
            for stage in STAGES:
                if m["stages"][stage] not in ("PENDING", "IN_PROGRESS", "COMPLETE", "FAILED"):
                    raise ValueError("invalid stage state: " + stage)
            self.m = m
        except Exception as exc:
            raise BuildSafetyError(f"resume refused: {exc}") from exc
        self.validate_work()

    def start(self):
        if self.recover_stale and not self.resume:
            raise BuildSafetyError("stale recovery requires resume=True")
        if self.resume:
            self.load()  # read/validate only, no mutations before ownership
        lock_start = time.perf_counter()
        self.lock.acquire(recover_stale=self.recover_stale, validate=self.load)
        self.b.stats["lock_acquire_ms"] = (time.perf_counter() - lock_start) * 1000
        if self.resume:
            self.load()  # recheck after exclusive ownership
        elif os.path.exists(self.manifest_path):
            old = json.loads(Path(self.manifest_path).read_text(encoding="utf-8"))
            if old.get("stages", {}).get("COMMIT") != "COMPLETE":
                raise BuildSafetyError("partial build exists; use resume=True or an explicit new output path")
        if not self.resume:
            uid = uuid.uuid4().hex
            self.m = {"build_manifest_version": MANIFEST_VERSION, "builder_version": BUILDER_VERSION,
                "dataset_fingerprint": self.dataset_fp, "layout_fingerprint": self.layout_fp,
                "semantic_versions": self.versions, "cache_format": F.IDX_VERSION,
                "sources_identity": self.identity, "config": self.config,
                "output_path": os.path.abspath(self.b.idx_path),
                "scratch_path": os.path.abspath(self.b.temp_dir), "build_id": uid,
                "work_dir": os.path.join(os.path.abspath(self.b.temp_dir), "naksha-build-" + uid),
                "work_pc": os.path.join(os.path.dirname(os.path.abspath(self.b.idx_path)), "nkpc-" + uid + ".work"),
                "final_pc": os.path.join(os.path.dirname(os.path.abspath(self.b.idx_path)), "nkpc-" + uid + ".nakshapc"),
                "stages": {s: "PENDING" for s in STAGES},
                "sources": [dict(source_file_id=i, status="PENDING", processed_point_count=0,
                                  chunk_index=0, expected_point_base=int(r["point_base"]),
                                  point_count=int(r["point_count"]), source_signature=int(r["content_sig"]))
                            for i, r in enumerate(self.b.sources)],
                "segments": [], "shards": [], "finalized": [], "pc_offset": 0,
                "stats_blob": None, "overview": None, "overview_bid": None,
                "candidate_index": None, "validation": None}
            os.makedirs(self.m["work_dir"], exist_ok=False)
            self.save()
        self.b.shard_dir = os.path.join(self.m["work_dir"], "shards")
        self.b.pc_path = (self.m["final_pc"] if os.path.isfile(self.m["final_pc"])
                          and not os.path.isfile(self.m["work_pc"]) else self.m["work_pc"])
        if self.m["stats_blob"]:
            self.b.stats_obj = GlobalStats.from_bytes(base64.b64decode(self.m["stats_blob"]))
        return self

    def save(self):
        t = time.perf_counter()
        self.m.pop("manifest_sha256", None)
        self.m["manifest_sha256"] = hashlib.sha256(canonical(self.m)).hexdigest()
        self.iw.write_checkpoint(self.m)
        self.checkpoint_times.append((time.perf_counter() - t) * 1000)
        self.m.pop("manifest_sha256", None)
        self.last_checkpoint = time.monotonic()

    def stage(self, name, state):
        self.m["stages"][name] = state
        self.save()
        self.event(name + "_" + state)

    def event(self, name):
        hook = self.b.safety_hook
        if hook:
            hook(name, self.b)
        if self.b.cancel_check and self.b.cancel_check():
            raise BuildCancelled("build cancelled at safe boundary: " + name)

    def artifact(self, path):
        return {"path": path, "size": os.path.getsize(path), "sha256": digest_file(path)}

    def check_artifact(self, a):
        p = os.path.abspath(a["path"])
        roots = (os.path.abspath(self.m["work_dir"]), os.path.dirname(os.path.abspath(self.b.idx_path)))
        if not any(os.path.commonpath((p, r)) == r for r in roots):
            raise BuildSafetyError("work artifact outside recorded build directories")
        if not os.path.isfile(p) or os.path.getsize(p) != a["size"]:
            raise BuildSafetyError("work artifact missing or wrong size: " + p)
        if digest_file(p) != a["sha256"]:
            raise BuildSafetyError("work artifact checksum mismatch: " + p)

    def validate_work(self):
        m = self.m
        root = os.path.abspath(self.b.temp_dir)
        expected = os.path.join(root, "naksha-build-" + m["build_id"])
        if m["work_dir"] != expected:
            raise BuildSafetyError("manifest work directory identity mismatch")
        output = os.path.dirname(os.path.abspath(self.b.idx_path))
        for key, suffix in (("work_pc", ".work"), ("final_pc", ".nakshapc")):
            if m[key] != os.path.join(output, "nkpc-" + m["build_id"] + suffix):
                raise BuildSafetyError("manifest point-cache path mismatch")
        # Validate every durable segment before any truncation. A partial tail
        # after the last segment is allowed, but never trusted.
        from .builder import SHARD_ITEMSIZE, choose_shard_count
        n_shards = choose_shard_count(self.b.total_points)
        offsets = [0] * n_shards
        for seg in m["segments"]:
            si = seg["shard_id"]
            if (not 0 <= si < n_shards or seg["offset"] != offsets[si]
                    or seg["bytes"] <= 0 or seg["bytes"] % SHARD_ITEMSIZE):
                raise BuildSafetyError("shard segment offset/size invalid")
            p = os.path.join(expected, "shards", f"shard_{seg['shard_id']:05d}.bin")
            if not os.path.isfile(p) or digest_file(p, seg["offset"], seg["bytes"]) != seg["sha256"]:
                raise BuildSafetyError("shard segment checksum mismatch: " + p)
            offsets[si] += seg["bytes"]
        if m["shards"]:
            if len(m["shards"]) != n_shards:
                raise BuildSafetyError("manifest shard count mismatch")
            for si, shard in enumerate(m["shards"]):
                if (shard["shard_id"] != si or shard["byte_size"] != offsets[si]
                        or shard["record_count"] * SHARD_ITEMSIZE != offsets[si]):
                    raise BuildSafetyError("manifest shard size/record count mismatch")
        if len(m["sources"]) != len(self.identity):
            raise BuildSafetyError("manifest source progress count mismatch")
        for i, (source, identity) in enumerate(zip(m["sources"], self.identity)):
            processed = source["processed_point_count"]
            if (source["source_file_id"] != i or not 0 <= processed <= identity["point_count"]
                    or source["expected_point_base"] != identity["expected_point_base"]
                    or source["point_count"] != identity["point_count"]
                    or (source["status"] == "COMPLETE" and processed != identity["point_count"])):
                raise BuildSafetyError("manifest source progress identity/count mismatch")
        processed = sum(s["processed_point_count"] for s in m["sources"])
        if sum(offsets) != processed * SHARD_ITEMSIZE:
            raise BuildSafetyError("checkpoint shard/source point counts differ")
        if m["stats_blob"] and GlobalStats.from_bytes(base64.b64decode(m["stats_blob"])).n_points != processed:
            raise BuildSafetyError("checkpoint statistics count differs from ingest")
        for entry in m["finalized"]:
            self.check_artifact(entry["metadata"])
        if m["overview"]:
            self.check_artifact(m["overview"])
        if m["pc_offset"]:
            pc = m["work_pc"] if os.path.isfile(m["work_pc"]) else m["final_pc"]
            if not os.path.isfile(pc) or os.path.getsize(pc) < m["pc_offset"]:
                raise BuildSafetyError("checkpointed NKPC missing/truncated")
            # Full encoded-block checksums protect all completed shards.
            with open(pc, "rb") as f:
                for entry in m["finalized"]:
                    with np.load(entry["metadata"]["path"], allow_pickle=False) as z:
                        for b in z["blocks"]:
                            self.check_block(f, b)
                for b in m.get("overview_block", []):
                    row = np.frombuffer(bytes.fromhex(b), dtype=F.BLOCK_ENTRY)[0]
                    self.check_block(f, row)
        if m["candidate_index"]:
            self.check_artifact(m["candidate_index"])

    @staticmethod
    def check_block(f, b):
        import zlib
        f.seek(int(b["file_offset"]))
        remaining = int(b["stored_bytes"])
        crc = 0
        while remaining:
            raw = f.read(min(4 << 20, remaining))
            if not raw:
                raise BuildSafetyError("NKPC temp block truncated")
            crc = zlib.crc32(raw, crc)
            remaining -= len(raw)
        if crc & 0xffffffff != int(b["checksum"]):
            raise BuildSafetyError(f"NKPC temp block {int(b['block_id'])} checksum mismatch")

    def append_segment(self, shard_id, payload):
        h = self.pending_hashes.setdefault(shard_id, hashlib.sha256())
        h.update(payload)
        self.pending_bytes += len(payload)

    def checkpoint_source(self, sw, fid, base, chunk_index, complete=False, force=False):
        if not (force or complete or self.pending_bytes >= self.b.checkpoint_bytes
                or time.monotonic() - self.last_checkpoint >= 30
                or (self.b.cancel_check and self.b.cancel_check())):
            return
        sw.flush_all()
        previous = self.m["shards"]
        for si, h in self.pending_hashes.items():
            old_bytes = previous[si]["byte_size"] if previous else 0
            size = int(sw.counts[si]) * sw.itemsize
            self.m["segments"].append({"shard_id": si, "offset": old_bytes,
                                      "bytes": size - old_bytes, "sha256": h.hexdigest()})
        self.m["shards"] = [dict(shard_id=i, record_count=int(n), byte_size=int(n) * sw.itemsize,
                                  status="COMPLETE" if complete and fid == len(self.paths) - 1 else "IN_PROGRESS")
                            for i, n in enumerate(sw.counts)]
        self.m["sources"][fid].update(processed_point_count=base, chunk_index=chunk_index,
                                     status="COMPLETE" if complete else "IN_PROGRESS")
        self.m["stats_blob"] = base64.b64encode(self.b.stats_obj.to_bytes()).decode()
        self.save()
        self.pending_hashes.clear()
        self.pending_bytes = 0
        self.event("SOURCE_COMPLETE" if complete else "SOURCE_CHECKPOINT")

    def write_npz(self, name, **arrays):
        p = os.path.join(self.m["work_dir"], name + ".npz")
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            np.savez(f, **arrays)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        return self.artifact(p)

    def revalidate_sources(self):
        from .source_catalog import build_catalog
        rows, _ = build_catalog(self.paths)
        if source_identity(self.paths, rows) != self.identity:
            raise BuildSafetyError("source changed during build; refusing commit/resume")
