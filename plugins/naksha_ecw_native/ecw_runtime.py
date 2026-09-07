"""Private, process-isolated ECW runtime. No SDK DLLs enter NakshaAI."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from functools import lru_cache
import zipfile

_WHEEL_NAME = "naksha_ecw_sdk-2.0.0-cp310-abi3-win_amd64.whl"
_TYPES = {1: "uint8", 2: "uint16", 3: "int16", 4: "uint32", 5: "int32", 6: "float32", 7: "float64"}


def prepare_runtime(plugin_dir):
    plugin_dir = Path(plugin_dir)
    wheel = plugin_dir / _WHEEL_NAME
    with wheel.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else _digest(handle)
    root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "NakshaAI" / "ecw-native" / digest[:20]
    runtime = root / "runtime"
    required = ("bin/gdal312.dll", "bin/NCSEcw.dll", "gdalplugins/gdal_ECW_JP2ECW.dll", "proj_data/proj.db")
    if all((runtime / item).is_file() for item in required) and (root / "complete").is_file():
        return runtime
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="extract-", dir=root.parent))
    try:
        with zipfile.ZipFile(wheel) as archive:
            prefix = "naksha_ecw_sdk/runtime/"
            for item in archive.infolist():
                if not item.filename.startswith(prefix) or item.is_dir():
                    continue
                relative = Path(item.filename[len(prefix):])
                target = (staging / "runtime" / relative).resolve()
                if not target.is_relative_to(staging.resolve()):
                    raise ValueError("Unsafe path in SDK wheel")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        if not all((staging / "runtime" / item).is_file() for item in required):
            raise RuntimeError("SDK wheel is missing required ECW runtime files")
        (staging / "complete").write_text(digest, encoding="ascii")
        if root.exists():
            # Incomplete previous extraction: preserve it for diagnosis.
            root.rename(root.with_name(root.name + "-incomplete-" + staging.name))
        staging.rename(root)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return runtime


def _digest(handle):
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024*1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


class NativeClient:
    def __init__(self, plugin_dir, runtime):
        self._lock = threading.RLock()
        self._io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ecw-pipe")
        self._closed = False
        runtime = Path(runtime).resolve()
        env = dict(os.environ)
        for name in list(env):
            if name.startswith(("GDAL_", "PROJ_", "CPL_", "PYTHON")):
                env.pop(name, None)
        system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        env.update(PATH=str(runtime / "bin") + os.pathsep + str(system / "System32"),
                   GDAL_DRIVER_PATH=str(runtime / "gdalplugins"), GDAL_DATA=str(runtime / "gdal_data"),
                   PROJ_DATA=str(runtime / "proj_data"), PROJ_LIB=str(runtime / "proj_data"), PROJ_NETWORK="OFF")
        self._log = tempfile.TemporaryFile()
        self.process = None
        try:
            self.process = subprocess.Popen([str(Path(plugin_dir).resolve() / "ecw_reader.exe"), str(runtime)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
                env=env, cwd=runtime / "bin", creationflags=subprocess.CREATE_NO_WINDOW)
            self.status = self._receive(30)
        except Exception:
            self.close()
            raise

    def _read_response(self):
        line = self.process.stdout.readline(65536)
        if not line:
            raise RuntimeError("ECW reader exited unexpectedly; reload the plugin to restart it")
        result = json.loads(line)
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "ECW reader failed"))
        count = result.get("size", 0)
        if not isinstance(count, int) or not 0 <= count <= 256*1024*1024:
            raise RuntimeError("Invalid ECW pixel buffer size")
        if count:
            data = bytearray(count)
            view = memoryview(data)
            offset = 0
            while offset < count:
                n = self.process.stdout.readinto(view[offset:])
                if not n:
                    raise RuntimeError("ECW reader returned an incomplete pixel buffer")
                offset += n
            result["pixels"] = data
        return result

    def _receive(self, timeout=120):
        future = self._io.submit(self._read_response)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            self.close()
            raise RuntimeError("ECW read timed out; reload the plugin to restart its reader") from None

    def request(self, operation, path, *arguments):
        filename = str(Path(path).resolve()).encode("utf-8")
        with self._lock:
            if self._closed:
                raise RuntimeError("ECW plugin is disabled")
            header = " ".join(map(str, (operation, len(filename), *arguments))) + "\n"
            self.process.stdin.write(header.encode("ascii") + filename)
            self.process.stdin.flush()
            return self._receive()

    @lru_cache(maxsize=64)
    def _info(self, path, mtime, size):
        return self.request("INFO", path)

    def info(self, path):
        path = Path(path).resolve()
        stat = path.stat()
        return self._info(str(path), stat.st_mtime_ns, stat.st_size)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=5)
        self._io.shutdown(wait=True, cancel_futures=True)
        if self.process is not None:
            for pipe in (self.process.stdin, self.process.stdout):
                pipe.close()
        self._log.close()
        self._info.cache_clear()


class NativeDataset:
    """Read-only adapter for the host's rasterio-compatible raster pipeline."""
    def __init__(self, client, path):
        from affine import Affine
        from rasterio.crs import CRS
        from rasterio.coords import BoundingBox
        self.client, self.name, self.closed = client, str(path), False
        info = client.info(path)
        self.width, self.height, self.count = info["width"], info["height"], info["count"]
        self._type = info["dtype"]
        self.dtypes = tuple([_TYPES[self._type]] * self.count)
        self.descriptions = (None,) * self.count
        self.crs = CRS.from_wkt(info["wkt"]) if info["wkt"] else None
        self.transform = Affine.from_gdal(*info["transform"]) if info["transform"] else Affine.identity()
        corners = [self.transform * xy for xy in ((0, 0), (self.width, 0), (0, self.height), (self.width, self.height))]
        self.bounds = BoundingBox(min(p[0] for p in corners), min(p[1] for p in corners),
                                  max(p[0] for p in corners), max(p[1] for p in corners))
        self.indexes = tuple(range(1, self.count+1))
        self.nodata = None

    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def close(self): self.closed = True
    def tags(self, *args, **kwargs): return {}

    def read(self, indexes=None, *, window=None, out_shape=None, resampling=None, masked=False, **kwargs):
        import numpy as np
        if self.closed: raise RuntimeError("ECW dataset is closed")
        if kwargs: raise ValueError("Unsupported ECW read options: " + ", ".join(kwargs))
        scalar = isinstance(indexes, int)
        bands = [indexes] if scalar else list(indexes or self.indexes)
        if window is None:
            x, y, w, h = 0, 0, self.width, self.height
        else:
            x, y, w, h = map(int, (window.col_off, window.row_off, window.width, window.height))
        oh, ow = (h, w) if out_shape is None else tuple(out_shape[-2:])
        method = getattr(resampling, "name", "nearest").upper()
        if method not in ("NEAREST", "BILINEAR"):
            raise ValueError("ECW display supports nearest and bilinear resampling")
        result = self.client.request("READ", self.name, x, y, w, h, ow, oh, self._type,
                                     method, len(bands), *bands)
        data = np.frombuffer(result["pixels"], dtype=self.dtypes[0]).reshape(len(bands), oh, ow)
        if scalar: data = data[0]
        return np.ma.array(data, mask=False, copy=False) if masked else data
