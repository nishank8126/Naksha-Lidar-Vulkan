"""Random-access NORMAL_DATA adapter for the existing packed-normal GPU path."""
from pathlib import Path
import numpy as np
from .derived_container import DerivedContainer, project_container_path
from .normals import ENCODING_OCT16_X2


class ContainerNormalReader:
    def __init__(self, dataset, expected_layout_fingerprint=None):
        self.path = project_container_path(dataset)
        self.container = DerivedContainer(self.path)
        section = self.container.directory["sections"].get("NORMAL_DATA")
        if section is None:
            raise FileNotFoundError("NORMAL_DATA absent")
        metadata = section["metadata"]
        if metadata["source_set_fingerprint"] != self.container.directory["identity"]:
            raise ValueError("STALE NORMAL_DATA source-set fingerprint")
        self.layout_fingerprint = int(metadata["base_layout_fingerprint"])
        if (expected_layout_fingerprint and self.layout_fingerprint !=
                int(expected_layout_fingerprint)):
            raise ValueError("STALE NORMAL_DATA layout fingerprint")
        from .normal_builder import BUILDER_VERSION
        if metadata["normal_algorithm_version"] != BUILDER_VERSION:
            raise ValueError("STALE NORMAL_DATA algorithm")
        self._index = {int(k): v for k, v in section["blocks"].items()}
        if len(self._index) != metadata["block_count"]:
            raise FileNotFoundError("NORMAL_DATA generation incomplete")
        self.block_count = len(self._index)
        self.source_point_count = int(metadata["source_point_count"])
        self.source_file_count = int(metadata["source_file_count"])
        self.stored_normal_point_count = sum(v["point_count"] for v in self._index.values())
        self.encoding = ENCODING_OCT16_X2
        self.bytes_per_normal = 4
        self.layout_fingerprint_recorded = True

    def has_block(self, block_id):
        return int(block_id) in self._index

    def block_info(self, block_id):
        return self._index.get(int(block_id))

    def read_block_normals(self, block_id, verify_crc=False):
        raw, item = self.container.read_block("NORMAL_DATA", str(int(block_id)))
        if len(raw) != int(item["point_count"]) * 4:
            raise ValueError("NORMAL_DATA point count mismatch")
        return np.frombuffer(raw, dtype="<i2").reshape(-1, 2)

    def stored_bytes(self):
        return sum(v["size"] for v in self._index.values())

    def close(self):
        pass


class ContainerNormalWriter:
    def __init__(self, dataset, source_file_count, source_point_count,
                 layout_fingerprint, block_count):
        from .normal_builder import BUILDER_VERSION
        self.path = project_container_path(dataset)
        self.container = DerivedContainer(self.path)
        self.metadata = dict(NORMAL_SECTION_VERSION=1,
            normal_algorithm_version=BUILDER_VERSION,
            base_layout_fingerprint=int(layout_fingerprint),
            source_set_fingerprint=self.container.directory["identity"],
            encoding="OCT16_X2", block_count=int(block_count),
            source_point_count=int(source_point_count),
            source_file_count=int(source_file_count))
        self.pending = []
        self.pending_bytes = 0
        self.stored_points = 0

    def add_block(self, block_id, lod, packed):
        packed = np.asarray(packed, dtype="<i2")
        raw = packed.tobytes()
        self.pending.append((str(block_id), raw,
                             dict(point_count=len(packed), lod=int(lod), encoding="OCT16_X2")))
        self.pending_bytes += len(raw)
        self.stored_points += len(packed)
        if self.pending_bytes >= 8 << 20:
            self._flush()

    def _flush(self):
        if self.pending:
            self.container.append("NORMAL_DATA", self.metadata, self.pending)
            self.pending.clear()
            self.pending_bytes = 0

    def commit(self):
        self._flush()
        ContainerNormalReader(self.path, self.metadata["base_layout_fingerprint"])
        return self.path

    def abort(self):
        self.pending.clear()
