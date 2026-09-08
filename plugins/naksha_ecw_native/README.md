# Naksha ECW SDK — Secured Client Package (v2.0.0)

A self-contained Windows wheel for **ECW ↔ GeoTIFF conversion with full
projection preservation**. Everything — the compiled native core, the GDAL
3.12.1 runtime, the ECW SDK binaries (NCSEcw.dll, gdal_ECW_JP2ECW.dll), PROJ
and GDAL data files — ships inside the wheel. No system installs, no QGIS, no
GDAL setup required on the client machine.

> **Security note:** all conversion logic is compiled native code
> (`_core.pyd`, MSVC Release `/O2 /GL`, no debug symbols, no Python source).
> The package cannot be meaningfully reverse-engineered to recover *how* it
> is built.

---

## 1. Requirements

| | |
|---|---|
| OS | Windows 10/11 (64-bit) |
| Python | CPython 3.10, 3.11, 3.12 or 3.13 (64-bit) — one wheel covers all |
| Disk | ~150 MB installed |
| Licence | ECW **encoding** requires your organisation's licensed ECW SDK key (decoding needs none) |

> The compiled core uses the CPython **stable ABI (abi3)**, so the same wheel
> file installs and runs identically on Python 3.10 through 3.13+ — no
> per-version builds needed.

## 2. Installation

```bat
python -m pip install naksha_ecw_sdk-2.0.0-cp310-abi3-win_amd64.whl
```

Offline install works — there are no dependencies on PyPI.

Quick verification:

```python
import naksha_ecw_sdk as sdk
print(sdk.runtime_status())
# {'gdal_version': '3.12.1', 'ecw_present': True,
#  'can_create': True, 'can_create_copy': True, ...}
```

Importing the package automatically configures the bundled runtime
(loads `NCSEcw.dll` + `gdal312.dll`, registers drivers, sets `GDAL_DATA` /
`PROJ` paths). No further setup is needed.

---

## 3. API reference

### `info(path)` — metadata for any raster

```python
info = sdk.info(r"D:\project\ortho.ecw")
info["width"], info["height"], info["bands"]      # size / band count
info["pixel_size_x"], info["pixel_size_y"]        # GSD
info["georef"]["geotransform"]                    # [x0, dx, 0, y0, 0, dy]
info["georef"]["projection_wkt"]                  # full WKT spatial reference
```

Works for ECW, TIFF, JPEG and every format the bundled GDAL understands.

### `ecw_to_tiff(in_ecw, out_tif, creation_options=None)` — ECW → GeoTIFF

```python
sdk.ecw_to_tiff(r"D:\project\ortho.ecw", r"D:\export\ortho.tif")
```

- **Projection + geotransform are preserved automatically.**
- Default output: tiled, DEFLATE-compressed GeoTIFF (open format).
- Optional GDAL creation options:

```python
sdk.ecw_to_tiff(in_ecw, out_tif, ["COMPRESS=LZW", "TILED=YES", "BIGTIFF=IF_SAFER"])
```

### `tiff_to_ecw(in_raster, out_ecw, ...)` — GeoTIFF → ECW

```python
sdk.tiff_to_ecw(
    r"D:\export\ortho.tif",
    r"D:\export\ortho.ecw",
    target=90,          # compression quality, 0-100 (higher = better / larger)
    version=2,          # ECW format 2 or 3
    # key="...", company="...",        # licensed encode credentials
    # projection_name=..., datum_name=..., units=...,   # optional ECW metadata overrides
)
```

- **Projection + geotransform are preserved automatically** (written into the
  ECW header from the TIFF's spatial reference).
- `target=95+` is recommended for photogrammetry data to stay visually
  lossless; `85-90` is a good delivery balance.
- **Encoding requires the licensed ECW SDK key.** Set it once via environment
  or `sdk.encode_defaults()`:

```python
sdk.encode_defaults(key="<your ECW encode key>", company="<your company>")
# afterwards every tiff_to_ecw() call uses these
```

or per call with `key=` / `company=`.

### `clip(source, cutline, output, ...)` — polygon clipping

```python
sdk.clip(
    r"D:\ortho.ecw",                 # source raster (ECW or TIFF)
    r"D:\boundary.shp",               # cutline: SHP / GeoJSON / GPKG / WKT...
    r"D:\ortho_clip.tif",             # .tif -> GeoTIFF, .ecw -> ECW output
    resampling="bilinear",            # near|bilinear|cubic|lanczos|...
    # cutline_crs="EPSG:3301",        # if the cutline has no CRS embedded
    # output_crs="EPSG:3301",         # reproject on clip if desired
)
```

### `set_metadata(ecw_path, ...)` — fix georeferencing in place

Updates projection/geotransform in the ECW header **without recompressing
pixels** (fast, lossless):

```python
sdk.set_metadata(
    r"D:\ortho.ecw",
    geotransform=[6500000.0, 0.05, 0, 658000.0, 0, -0.05],
    projection_wkt=open("prj.wkt").read(),
)
```

### `probe_encoder(...)` — verify a client's encode key

```python
result = sdk.probe_encoder()
print(result)   # {'ok': True, 'error': ''} when the key is valid
```

Performs a real 128×128 test encode — the strongest possible check.

### Other helpers

- `sdk.runtime_status()` — runtime/driver status dict.
- `sdk.configure(path)` — point to a different runtime root (rarely needed).

---

## 4. Integrating into your tool

### Tkinter (e.g. the Naksha 3D Viewer launcher)

```python
import threading, tkinter as tk
import naksha_ecw_sdk as sdk

def export_ecw_to_tiff(entry_path, entry_out, label):
    def run():
        try:
            sdk.ecw_to_tiff(entry_path.get(), entry_out.get())
            label.config(text="Export complete ✔")
        except Exception as e:
            label.config(text=f"Export failed: {e}")
    threading.Thread(target=run, daemon=True).start()
```

### PySide6 / PyQt

```python
from PySide6.QtCore import QThread, Signal
import naksha_ecw_sdk as sdk

class ExportWorker(QThread):
    done = Signal(bool, str)
    def run(self):
        try:
            sdk.ecw_to_tiff(self.src, self.dst)
            self.done.emit(True, "OK")
        except Exception as e:
            self.done.emit(False, str(e))
```

### Web backend (Flask / FastAPI)

```python
@app.post("/convert")
def convert():
    sdk.ecw_to_tiff(req.src, req.dst)          # CPU-bound: run in a worker
    # pool (e.g. fastapi `run_in_threadpool`) to keep the server responsive
```

> The SDK is process-global after import (like GDAL itself). It is
> thread-safe for conversion calls; treat the first `import` as the
> initialisation step and let worker threads/queues serialise heavy jobs.

### Command-line batch

```bat
python -c "import naksha_ecw_sdk as s; s.ecw_to_tiff(r'IN.ecw', r'OUT.tif')"
```

---

## 5. Projection handling — what you get

Both conversion directions preserve the complete georeferencing:

- **ECW → GeoTIFF:** the ECW header's projection/datum/units are written as a
  full WKT spatial reference + geotransform into the TIFF. The output opens in
  QGIS/ArcGIS with the correct CRS — no manual assignment needed.
- **GeoTIFF → ECW:** the TIFF's spatial reference is carried into the ECW
  header (PROJ/DATUM/UNITS metadata + origin/increment). Optional explicit
  overrides exist for legacy ECW consumers that expect named projections.
- `clip()` reprojects only if you pass `output_crs`; otherwise the source CRS
  is kept.

Verify any time with `sdk.info(path)["georef"]`.

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ECW runtime not configured` | The wheel's `runtime/` folder was moved out of the package; reinstall the wheel, or `sdk.configure(r"...\runtime")`. |
| `None of ECW_ENCODE_KEY and ECW_ENCODE_COMPANY were provided` | Normal without a licence — set your key via `sdk.encode_defaults()` or env vars `NAKSHA_ECW_ENCODE_KEY` / `NAKSHA_ECW_ENCODE_COMPANY`. Decoding is unaffected. |
| `RuntimeError: This driver is not registered: ECW` | Another GDAL installation on the machine is conflicting; ensure the bundled `runtime/bin` is first on `PATH` (the SDK does this automatically on import). |
| Output .tif missing CRS | The source ECW had no projection; fix once with `set_metadata()` or pass a WKT when converting. |
| Slow first import (~2-4 s) | One-time driver registration; subsequent imports in the same process are instant. |

---

## 7. Legal / redistribution

- The bundled ECW SDK binaries (NCSEcw.dll etc.) are covered by the Hexagon /
  ERDAS ECW SDK redistribution licence. Redistribution of *this wheel* to your
  clients is permitted under your organisation's ECW SDK agreement; do not
  unpack and redistribute the DLLs separately.
- GDAL 3.12.1 and PROJ are bundled under their MIT / X11-style licences.
- The compiled wrapper is proprietary to Naksha.
