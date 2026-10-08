"""global_stats.py - mergeable dataset-level statistics for chunked ingest.

Computed in the EXISTING ingest pass (one `add_chunk` per source chunk, constant
memory), merged across chunks/files by plain addition, and persisted in the
.nakshaidx so display ranges never depend on what happens to be resident:

  * Intensity : EXACT uint16 histogram (65 536 bins) -> exact min / max /
                zero-count and exact percentiles. No sketch needed: the value
                domain is only 2^16.
  * Z         : 8 192-bin histogram over the catalog's header Z range, plus
                EXACT running min/max, so percentile error <= range / 8192 and
                a wrong header range cannot hide data (out-of-range counts are
                recorded, values clip into the end bins).
  * Class     : 256-bin counts, global and per source file.

All of it is integer addition, so merge(a, b) == stats(a + b) exactly for the
histograms and counts (tests/test_global_stats.py).
"""
from __future__ import annotations

import struct
import zlib

import numpy as np

STATS_MAGIC = b"NKSTAT01"
INTENSITY_BINS = 65536
Z_BINS = 8192
CLASS_BINS = 256
PERCENTILES = (0.5, 1.0, 5.0, 25.0, 50.0, 75.0, 95.0, 99.0, 99.8)


class GlobalStats:
    def __init__(self, n_sources: int, z_lo: float, z_hi: float):
        if not np.isfinite(z_lo) or not np.isfinite(z_hi):
            raise ValueError("Z range must be finite")
        if z_hi <= z_lo:
            z_hi = z_lo + 1.0
        self.n_sources = int(n_sources)
        self.z_lo = float(z_lo)
        self.z_hi = float(z_hi)
        self.n_points = 0
        self.inten_hist = np.zeros(INTENSITY_BINS, np.uint64)
        self.z_hist = np.zeros(Z_BINS, np.uint64)
        self.class_counts = np.zeros(CLASS_BINS, np.uint64)
        self.source_points = np.zeros(self.n_sources, np.uint64)
        self.source_class = np.zeros((self.n_sources, CLASS_BINS), np.uint64)
        self.z_min = np.inf
        self.z_max = -np.inf
        self.z_out_of_range = 0

    # ------------------------------------------------------------- ingest
    def add_chunk(self, file_id: int, z, intensity, classification) -> None:
        z = np.asarray(z, dtype=np.float64)
        n = int(z.size)
        if n == 0:
            return
        self.n_points += n
        self.source_points[file_id] += n
        self.inten_hist += np.bincount(
            np.asarray(intensity, dtype=np.int64),
            minlength=INTENSITY_BINS).astype(np.uint64)[:INTENSITY_BINS]
        cls = np.bincount(np.asarray(classification, dtype=np.int64),
                          minlength=CLASS_BINS).astype(np.uint64)[:CLASS_BINS]
        self.class_counts += cls
        self.source_class[file_id] += cls
        zmin, zmax = float(z.min()), float(z.max())
        self.z_min = min(self.z_min, zmin)
        self.z_max = max(self.z_max, zmax)
        scale = Z_BINS / (self.z_hi - self.z_lo)
        raw = np.floor((z - self.z_lo) * scale).astype(np.int64)
        self.z_out_of_range += int(((raw < 0) | (raw >= Z_BINS)).sum())
        self.z_hist += np.bincount(np.clip(raw, 0, Z_BINS - 1),
                                   minlength=Z_BINS).astype(np.uint64)

    def merge(self, other: "GlobalStats") -> "GlobalStats":
        if (other.n_sources != self.n_sources or other.z_lo != self.z_lo
                or other.z_hi != self.z_hi):
            raise ValueError("cannot merge statistics with different "
                             "source count / Z binning")
        self.n_points += other.n_points
        self.inten_hist += other.inten_hist
        self.z_hist += other.z_hist
        self.class_counts += other.class_counts
        self.source_points += other.source_points
        self.source_class += other.source_class
        self.z_min = min(self.z_min, other.z_min)
        self.z_max = max(self.z_max, other.z_max)
        self.z_out_of_range += other.z_out_of_range
        return self

    # ------------------------------------------------------------- queries
    @property
    def intensity_min(self) -> int:
        nz = np.flatnonzero(self.inten_hist)
        return int(nz[0]) if nz.size else 0

    @property
    def intensity_max(self) -> int:
        nz = np.flatnonzero(self.inten_hist)
        return int(nz[-1]) if nz.size else 0

    @property
    def intensity_zero_count(self) -> int:
        return int(self.inten_hist[0])

    def intensity_percentile(self, p: float) -> int:
        """Exact: the smallest value v with CDF(v) >= p% of the points."""
        if self.n_points == 0:
            return 0
        cdf = np.cumsum(self.inten_hist)
        target = np.ceil(self.n_points * (p / 100.0))
        return int(np.searchsorted(cdf, max(target, 1), side="left"))

    def z_percentile(self, p: float) -> float:
        """Histogram percentile, linear inside the bin; clamped to [min, max]."""
        if self.n_points == 0:
            return 0.0
        cdf = np.cumsum(self.z_hist).astype(np.float64)
        target = self.n_points * (p / 100.0)
        b = int(np.searchsorted(cdf, target, side="left"))
        b = min(max(b, 0), Z_BINS - 1)
        prev = cdf[b - 1] if b > 0 else 0.0
        cnt = float(self.z_hist[b]) or 1.0
        frac = min(max((target - prev) / cnt, 0.0), 1.0)
        step = (self.z_hi - self.z_lo) / Z_BINS
        v = self.z_lo + (b + frac) * step
        return float(min(max(v, self.z_min), self.z_max))

    @property
    def z_percentile_error(self) -> float:
        return (self.z_hi - self.z_lo) / Z_BINS

    def summary(self) -> dict:
        return {
            "points": int(self.n_points),
            "intensity": {"min": self.intensity_min, "max": self.intensity_max,
                          "zero_count": self.intensity_zero_count,
                          **{f"p{p:g}": self.intensity_percentile(p)
                             for p in PERCENTILES}},
            "z": {"min": float(self.z_min), "max": float(self.z_max),
                  "out_of_header_range": int(self.z_out_of_range),
                  "percentile_error_m": self.z_percentile_error,
                  **{f"p{p:g}": self.z_percentile(p) for p in PERCENTILES}},
            "classes": {int(c): int(n) for c, n
                        in enumerate(self.class_counts) if n},
        }

    # --------------------------------------------------------- serialise
    def to_bytes(self) -> bytes:
        body = b"".join((
            self.inten_hist.tobytes(), self.z_hist.tobytes(),
            self.class_counts.tobytes(), self.source_points.tobytes(),
            self.source_class.tobytes()))
        head = struct.pack("<8sIIddQddQ", STATS_MAGIC, 1, self.n_sources,
                           self.z_lo, self.z_hi, self.n_points,
                           self.z_min, self.z_max, self.z_out_of_range)
        comp = zlib.compress(body, 6)
        return head + struct.pack("<QI", len(comp),
                                  zlib.crc32(body) & 0xFFFFFFFF) + comp

    @classmethod
    def from_bytes(cls, data: bytes) -> "GlobalStats":
        hs = struct.calcsize("<8sIIddQddQ")
        try:
            (magic, ver, n_src, z_lo, z_hi, n_pts, z_min, z_max,
             oor) = struct.unpack_from("<8sIIddQddQ", data, 0)
            if magic != STATS_MAGIC or ver != 1:
                raise ValueError("bad statistics block (magic/version)")
            clen, crc = struct.unpack_from("<QI", data, hs)
            if len(data) < hs + 12 + clen:
                raise ValueError("statistics block truncated")
            body = zlib.decompress(data[hs + 12: hs + 12 + clen])
        except (struct.error, zlib.error) as exc:
            raise ValueError(f"corrupt statistics block: {exc}") from exc
        if (zlib.crc32(body) & 0xFFFFFFFF) != crc:
            raise ValueError("statistics block checksum mismatch")
        s = cls(n_src, z_lo, z_hi)
        o = 0
        for name, shape in (("inten_hist", (INTENSITY_BINS,)),
                            ("z_hist", (Z_BINS,)),
                            ("class_counts", (CLASS_BINS,)),
                            ("source_points", (n_src,)),
                            ("source_class", (n_src, CLASS_BINS))):
            n = int(np.prod(shape)) * 8
            setattr(s, name, np.frombuffer(body, np.uint64, int(np.prod(shape)),
                                           o).reshape(shape).copy())
            o += n
        s.n_points, s.z_min, s.z_max, s.z_out_of_range = (
            int(n_pts), float(z_min), float(z_max), int(oor))
        return s
