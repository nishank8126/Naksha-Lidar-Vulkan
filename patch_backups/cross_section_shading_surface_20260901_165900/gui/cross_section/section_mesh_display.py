"""Cross-section Shading and Surface presentation.

This module deliberately owns *section presentation only*.  It does not change
classification ownership, Main View Shading/Surface caches, section extraction,
or the existing scalar point-display paths.

The section TIN is built in the section's display plane (along-distance / Z)
using the already-stored section-local points.  Geometry remains in the native
section-local XYZ coordinates so the existing section camera and classification
interactor continue to work.

Shaded Classification uses face-local (duplicated) vertices.  Each triangle gets
one baked face shade while the three class colours remain per-vertex.  Pure
triangles are therefore solid/crisp; mixed-class triangles retain barycentric
class-colour blending without shared-vertex shade bleeding.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
import os
import time
from typing import Any, Dict, Iterable, Optional, Tuple

import numpy as np
import pyvista as pv


MESH_MODES = {"shaded_class", "surface"}
_SHADED_ACTOR = "_section_{view}_shaded_mesh"
_SURFACE_ACTOR = "_section_{view}_surface_mesh"
_UNIFIED_ACTOR = "_section_{view}_unified"
_SHADED_RGB = "section_shaded_rgb"
_SURFACE_RGB = "section_surface_rgb"


@dataclass
class _SectionTopology:
    view_idx: int
    signature: tuple
    points: np.ndarray
    projected: np.ndarray
    global_indices: np.ndarray
    classes: np.ndarray
    section_global_indices_all: np.ndarray
    section_class_snapshot_all: np.ndarray
    faces: np.ndarray
    spacing: float
    max_edge: float
    class_snapshot: np.ndarray
    shade: Optional[np.ndarray] = None
    shade_signature: Optional[tuple] = None
    shaded_mesh: Any = None
    shaded_actor: Any = None
    shaded_rgb: Any = None
    surface_mesh: Any = None
    surface_actor: Any = None


def _log(message: str) -> None:
    print(f"SECTION_MESH {message}")


def _ensure_state(app) -> None:
    if not isinstance(getattr(app, "view_display_modes", None), dict):
        app.view_display_modes = {}
    if not isinstance(getattr(app, "view_display_settings", None), dict):
        app.view_display_settings = {}
    if not isinstance(getattr(app, "_section_mesh_display_cache", None), dict):
        app._section_mesh_display_cache = {}

    # Slot 0 is Main View.  Slots 1..4 are Cross Sections.  Do not overwrite
    # state that a newer build already owns.
    app.view_display_modes.setdefault(
        0, str(getattr(app, "display_mode", "class") or "class").lower()
    )
    for slot in range(1, 5):
        app.view_display_modes.setdefault(slot, "class")


def _normalize_mode(mode: str) -> str:
    token = str(mode or "class").strip().lower()
    aliases = {
        "shading": "shaded_class",
        "shaded": "shaded_class",
        "shaded classification": "shaded_class",
        "classification": "class",
        "by classification": "class",
    }
    return aliases.get(token, token)


def record_section_display_mode(
    app,
    view_idx: int,
    mode: str,
    *,
    quality_mode: Optional[str] = None,
    palette=None,
) -> None:
    """Record section presentation state without touching any actor."""
    _ensure_state(app)
    slot = int(view_idx) + 1
    normalized = _normalize_mode(mode)
    app.view_display_modes[slot] = normalized
    settings = dict(app.view_display_settings.get(slot, {}) or {})
    if quality_mode is not None:
        settings["quality"] = _normalize_quality(quality_mode)
    if palette is not None:
        settings["visible_classes"] = tuple(sorted(_visible_class_codes(palette)))
    app.view_display_settings[slot] = settings


def get_section_display_mode(app, view_idx: int) -> str:
    _ensure_state(app)
    return _normalize_mode(app.view_display_modes.get(int(view_idx) + 1, "class"))


def _normalize_quality(value: str) -> str:
    token = str(value or "normal").strip().lower()
    return token if token in {"fast", "normal", "slow"} else "normal"


def _get_vtk_widget(app, view_idx: int):
    vtks = getattr(app, "section_vtks", None)
    if isinstance(vtks, dict):
        return vtks.get(int(view_idx))
    return None


def _get_palette(app, view_idx: int, palette=None) -> dict:
    if isinstance(palette, dict) and palette:
        return palette
    slot = int(view_idx) + 1
    for owner in (
        getattr(app, "view_palettes", None),
        getattr(getattr(app, "display_mode_dialog", None), "view_palettes", None),
        getattr(getattr(app, "display_dialog", None), "view_palettes", None),
    ):
        if isinstance(owner, dict) and isinstance(owner.get(slot), dict):
            return owner[slot]
    return getattr(app, "class_palette", {}) or {}


def _visible_class_codes(palette: dict) -> set:
    if not isinstance(palette, dict) or not palette:
        return set()
    return {
        int(code)
        for code, info in palette.items()
        if not isinstance(info, dict) or bool(info.get("show", True))
    }


def _class_visible(code: int, palette: dict) -> bool:
    if not palette:
        return True
    info = palette.get(int(code))
    if info is None:
        return True
    if not isinstance(info, dict):
        return True
    return bool(info.get("show", True))


def _palette_rgb(classes: np.ndarray, palette: dict) -> np.ndarray:
    classes = np.asarray(classes, dtype=np.int64).reshape(-1)
    if len(classes) == 0:
        return np.empty((0, 3), dtype=np.uint8)

    # A LUT is much faster than Python per-point mapping and preserves current
    # view-specific class colours.  Missing codes fall back to Main palette,
    # then neutral gray.
    class_max = int(classes.max()) if len(classes) else 0
    max_code = max(class_max, max((int(c) for c in palette), default=0))
    max_code = max(max_code, 255)
    lut = np.full((max_code + 1, 3), 128, dtype=np.uint8)
    main = getattr(_palette_rgb, "_app_palette", None)
    for code in np.unique(classes):
        ci = int(code)
        info = palette.get(ci, {}) if isinstance(palette, dict) else {}
        color = info.get("color") if isinstance(info, dict) else None
        if color is None and isinstance(main, dict):
            minfo = main.get(ci, {})
            color = minfo.get("color") if isinstance(minfo, dict) else None
        if color is None:
            color = (128, 128, 128)
        try:
            lut[ci] = np.asarray(color[:3], dtype=np.uint8)
        except Exception:
            lut[ci] = (128, 128, 128)
    return lut[np.clip(classes, 0, max_code)]


def _section_data(app, view_idx: int) -> Tuple[np.ndarray, np.ndarray]:
    """Return (section-local points, canonical global indices) in the same order."""
    view_idx = int(view_idx)
    pts = getattr(app, f"section_{view_idx}_points_transformed", None)
    if pts is None:
        core = getattr(app, f"section_{view_idx}_core_points", None)
        buf = getattr(app, f"section_{view_idx}_buffer_points", None)
        if core is not None:
            if buf is not None and len(buf):
                pts = np.vstack([core, buf])
            else:
                pts = np.asarray(core)
    if pts is None:
        return np.empty((0, 3), dtype=np.float32), np.empty(0, dtype=np.int64)

    pts = np.asarray(pts)
    if pts.ndim != 2 or pts.shape[1] < 3:
        return np.empty((0, 3), dtype=np.float32), np.empty(0, dtype=np.int64)

    # Prefer the explicit canonical core-first map when available.
    candidates = (
        getattr(app, f"section_{view_idx}_global_indices", None),
        getattr(app, f"section_{view_idx}_indices", None),
    )
    global_idx = None
    for candidate in candidates:
        if candidate is None:
            continue
        arr = np.asarray(candidate, dtype=np.int64).reshape(-1)
        if len(arr) == len(pts):
            global_idx = arr
            break

    if global_idx is None:
        core_idx = getattr(app, f"section_{view_idx}_core_indices", None)
        buf_idx = getattr(app, f"section_{view_idx}_buffer_indices", None)
        if core_idx is not None:
            chunks = [np.asarray(core_idx, dtype=np.int64).reshape(-1)]
            if buf_idx is not None and len(buf_idx):
                chunks.append(np.asarray(buf_idx, dtype=np.int64).reshape(-1))
            arr = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
            if len(arr) == len(pts):
                global_idx = arr

    if global_idx is None:
        combined = getattr(app, f"section_{view_idx}_combined_mask", None)
        if combined is not None:
            arr = np.flatnonzero(np.asarray(combined, dtype=bool))
            if len(arr) == len(pts):
                global_idx = arr.astype(np.int64, copy=False)

    if global_idx is None:
        _log(f"view={view_idx + 1} status=skip reason=index_length_mismatch points={len(pts)}")
        return np.empty((0, 3), dtype=np.float32), np.empty(0, dtype=np.int64)

    finite = np.isfinite(pts[:, :3]).all(axis=1)
    if not np.all(finite):
        pts = pts[finite]
        global_idx = global_idx[finite]

    return np.asarray(pts[:, :3], dtype=np.float32), global_idx.astype(np.int64, copy=False)


def _estimate_spacing(projected: np.ndarray) -> float:
    n = len(projected)
    if n < 2:
        return 1.0
    sample_limit = max(2_000, int(os.environ.get("NAKSHA_SECTION_MESH_SPACING_SAMPLE", "80000")))
    if n > sample_limit:
        ids = np.linspace(0, n - 1, sample_limit, dtype=np.int64)
        sample = np.asarray(projected[ids], dtype=np.float64)
    else:
        sample = np.asarray(projected, dtype=np.float64)
    try:
        from scipy.spatial import cKDTree
        tree = cKDTree(sample)
        d, _ = tree.query(sample, k=2, workers=-1)
        nn = np.asarray(d[:, 1], dtype=np.float64)
        nn = nn[np.isfinite(nn) & (nn > 1e-8)]
        if len(nn):
            return float(np.median(nn))
    except Exception:
        pass

    xr = float(np.ptp(sample[:, 0]))
    zr = float(np.ptp(sample[:, 1]))
    area = max(xr * zr, 1e-6)
    return float(max(np.sqrt(area / max(len(sample), 1)), 1e-4))


def _deduplicate_projection(
    points: np.ndarray,
    global_idx: np.ndarray,
    classes: np.ndarray,
    projected: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    if len(points) < 2:
        return points, global_idx, classes, projected
    tol = float(os.environ.get("NAKSHA_SECTION_MESH_DEDUP", "0.0001"))
    tol = max(tol, 1e-8)
    qx = np.rint(projected[:, 0] / tol).astype(np.int64)
    qz = np.rint(projected[:, 1] / tol).astype(np.int64)
    # Choose the return nearest the center plane for duplicate display-plane
    # positions.  This avoids arbitrary buffer-depth sheets.
    depth = np.abs(points[:, 1].astype(np.float64))
    order = np.lexsort((depth, qz, qx))
    oqx = qx[order]
    oqz = qz[order]
    first = np.ones(len(order), dtype=bool)
    first[1:] = (oqx[1:] != oqx[:-1]) | (oqz[1:] != oqz[:-1])
    keep = order[first]
    keep.sort()
    return points[keep], global_idx[keep], classes[keep], projected[keep]


def _quality_reduce(
    points: np.ndarray,
    global_idx: np.ndarray,
    classes: np.ndarray,
    projected: np.ndarray,
    quality: str,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    quality = _normalize_quality(quality)
    if quality == "slow":
        return points, global_idx, classes, projected
    target = (
        int(os.environ.get("NAKSHA_SECTION_SHADING_FAST_TARGET", "350000"))
        if quality == "fast"
        else int(os.environ.get("NAKSHA_SECTION_SHADING_NORMAL_TARGET", "1000000"))
    )
    target = max(3, target)
    if len(points) <= target:
        return points, global_idx, classes, projected

    # Section extraction is a narrow ribbon; for display quality, the points
    # nearest the center plane are the most representative and deterministic.
    depth = np.abs(points[:, 1].astype(np.float64))
    keep = np.argpartition(depth, target - 1)[:target]
    keep.sort()
    return points[keep], global_idx[keep], classes[keep], projected[keep]


def _triangulate(projected: np.ndarray) -> np.ndarray:
    try:
        from gui.shading_display import _do_triangulate
        return np.asarray(_do_triangulate(projected), dtype=np.int32)
    except Exception:
        try:
            from scipy.spatial import Delaunay
            return np.asarray(Delaunay(projected).simplices, dtype=np.int32)
        except Exception:
            return np.empty((0, 3), dtype=np.int32)


def _filter_faces(points: np.ndarray, projected: np.ndarray, faces: np.ndarray, spacing: float):
    if faces is None or len(faces) == 0:
        return np.empty((0, 3), dtype=np.int32), 0.0
    faces = np.asarray(faces, dtype=np.int32)
    valid = (
        (faces >= 0).all(axis=1)
        & (faces < len(points)).all(axis=1)
        & (faces[:, 0] != faces[:, 1])
        & (faces[:, 1] != faces[:, 2])
        & (faces[:, 2] != faces[:, 0])
    )
    faces = faces[valid]
    if len(faces) == 0:
        return faces, 0.0

    p0 = projected[faces[:, 0]].astype(np.float64)
    p1 = projected[faces[:, 1]].astype(np.float64)
    p2 = projected[faces[:, 2]].astype(np.float64)
    area2 = np.abs((p1[:, 0] - p0[:, 0]) * (p2[:, 1] - p0[:, 1]) -
                   (p1[:, 1] - p0[:, 1]) * (p2[:, 0] - p0[:, 0]))
    min_area2 = float(os.environ.get("NAKSHA_SECTION_MESH_MIN_AREA2", "1e-10"))

    factor = float(os.environ.get("NAKSHA_SECTION_MESH_MAX_EDGE_FACTOR", "15.0"))
    minimum = float(os.environ.get("NAKSHA_SECTION_MESH_MIN_EDGE", "0.25"))
    extent = max(float(np.ptp(projected[:, 0])), float(np.ptp(projected[:, 1])), minimum)
    fraction = float(os.environ.get("NAKSHA_SECTION_MESH_MAX_EDGE_EXTENT_FRACTION", "0.12"))
    max_edge = max(minimum, float(spacing) * factor)
    if fraction > 0:
        max_edge = min(max_edge, max(minimum, extent * fraction))

    max_edge2 = max_edge * max_edge
    e01 = np.sum((p1 - p0) ** 2, axis=1)
    e12 = np.sum((p2 - p1) ** 2, axis=1)
    e20 = np.sum((p0 - p2) ** 2, axis=1)
    keep = (
        (area2 > min_area2)
        & (e01 <= max_edge2)
        & (e12 <= max_edge2)
        & (e20 <= max_edge2)
    )
    return faces[keep], float(max_edge)


def _topology_signature(app, view_idx: int, points: np.ndarray, global_idx: np.ndarray,
                        palette: dict, quality: str) -> tuple:
    visible = tuple(sorted(_visible_class_codes(palette))) if palette else ("ALL",)
    # The stored array object/length changes when SectionController rebuilds a
    # section.  Global endpoints make accidental object-id reuse harmless.
    point_owner = getattr(app, f"section_{int(view_idx)}_points_transformed", None)
    return (
        id(point_owner),
        len(points),
        int(global_idx[0]) if len(global_idx) else -1,
        int(global_idx[-1]) if len(global_idx) else -1,
        visible,
        _normalize_quality(quality),
    )


def _build_topology(app, view_idx: int, palette: dict, quality: str) -> Optional[_SectionTopology]:
    t0 = time.perf_counter()
    points, global_idx = _section_data(app, view_idx)
    data = getattr(app, "data", None)
    if len(points) < 3 or not isinstance(data, dict) or data.get("classification") is None:
        return None
    all_classes = np.asarray(data["classification"])
    if len(all_classes) == 0 or np.max(global_idx, initial=-1) >= len(all_classes):
        return None
    classes = np.asarray(all_classes[global_idx], dtype=np.int32)
    # Keep a compact snapshot for the entire section, including currently
    # hidden classes. Without this, hidden -> visible classification changes
    # would be invisible to a topology containing only visible vertices.
    section_global_indices_all = np.ascontiguousarray(global_idx.copy(), dtype=np.int64)
    section_class_snapshot_all = np.ascontiguousarray(classes.copy(), dtype=np.int32)

    if palette:
        visible = np.fromiter(
            (_class_visible(int(c), palette) for c in classes),
            dtype=bool,
            count=len(classes),
        )
        points = points[visible]
        global_idx = global_idx[visible]
        classes = classes[visible]
    if len(points) < 3:
        return None

    # Cross-section display plane: along-distance (local X) by elevation (Z).
    projected = np.ascontiguousarray(points[:, (0, 2)], dtype=np.float64)
    points, global_idx, classes, projected = _deduplicate_projection(
        points, global_idx, classes, projected
    )
    points, global_idx, classes, projected = _quality_reduce(
        points, global_idx, classes, projected, quality
    )
    if len(points) < 3 or np.ptp(projected[:, 0]) <= 1e-10 or np.ptp(projected[:, 1]) <= 1e-10:
        return None

    spacing = _estimate_spacing(projected)
    faces = _triangulate(projected)
    raw_faces = len(faces)
    faces, max_edge = _filter_faces(points, projected, faces, spacing)
    if len(faces) == 0:
        return None

    signature = _topology_signature(app, view_idx, points, global_idx, palette, quality)
    topo = _SectionTopology(
        view_idx=int(view_idx),
        signature=signature,
        points=np.ascontiguousarray(points, dtype=np.float32),
        projected=np.ascontiguousarray(projected, dtype=np.float64),
        global_indices=np.ascontiguousarray(global_idx, dtype=np.int64),
        classes=np.ascontiguousarray(classes, dtype=np.int32),
        section_global_indices_all=section_global_indices_all,
        section_class_snapshot_all=section_class_snapshot_all,
        faces=np.ascontiguousarray(faces, dtype=np.int32),
        spacing=float(spacing),
        max_edge=float(max_edge),
        class_snapshot=np.ascontiguousarray(classes.copy(), dtype=np.int32),
    )
    _log(
        f"view={view_idx + 1} status=topology_built quality={quality} "
        f"vertices={len(points):,} faces={len(faces):,} raw_faces={raw_faces:,} "
        f"spacing={spacing:.5f} max_edge={max_edge:.3f} "
        f"ms={(time.perf_counter() - t0) * 1000.0:.1f}"
    )
    return topo


def _get_topology(app, view_idx: int, palette: dict, quality: str, *, force=False):
    _ensure_state(app)
    cache = app._section_mesh_display_cache
    old = cache.get(int(view_idx))
    if not force and isinstance(old, _SectionTopology):
        points, global_idx = _section_data(app, view_idx)
        if len(points) and len(global_idx) == len(points):
            sig = _topology_signature(app, view_idx, points, global_idx, palette, quality)
            # The build may quality-reduce; object id/visibility/quality are the
            # decisive fields, while endpoint/length differ only after reduction.
            if (
                old.signature[0] == sig[0]
                and old.signature[4] == sig[4]
                and old.signature[5] == sig[5]
            ):
                # Refresh class snapshot from source so palette recolours use
                # current classification even when no geometry rebuild is needed.
                data = getattr(app, "data", {}) or {}
                cls = data.get("classification")
                if cls is not None and len(old.global_indices):
                    old.classes = np.asarray(cls[old.global_indices], dtype=np.int32)
                    old.class_snapshot = old.classes.copy()
                return old, True

    topo = _build_topology(app, view_idx, palette, quality)
    if topo is not None:
        cache[int(view_idx)] = topo
    return topo, False


def _hide_unified(vtk_widget, view_idx: int, hidden=True) -> None:
    if vtk_widget is None:
        return
    actor = None
    try:
        actor = getattr(vtk_widget, "actors", {}).get(_UNIFIED_ACTOR.format(view=view_idx))
    except Exception:
        actor = None
    if actor is not None:
        try:
            actor.SetVisibility(not hidden)
        except Exception:
            try:
                actor.visibility = not hidden
            except Exception:
                pass


def _remove_named_actor(vtk_widget, name: str) -> None:
    if vtk_widget is None:
        return
    try:
        vtk_widget.remove_actor(name, render=False)
    except Exception:
        pass


def _clear_mesh_actors(vtk_widget, view_idx: int) -> None:
    _remove_named_actor(vtk_widget, _SHADED_ACTOR.format(view=view_idx))
    _remove_named_actor(vtk_widget, _SURFACE_ACTOR.format(view=view_idx))


def _current_shading_params(app) -> tuple:
    azimuth = float(getattr(app, "last_shade_azimuth", 45.0))
    sharpness = float(getattr(app, "shading_sharpness_angle",
                              getattr(app, "last_shade_angle", 45.0)))
    ambient = float(np.clip(getattr(app, "shade_ambient", 0.25), 0.0, 0.95))
    return azimuth, sharpness, ambient


def _section_face_shade(app, topo: _SectionTopology) -> Tuple[np.ndarray, tuple]:
    azimuth, sharpness, ambient = _current_shading_params(app)
    try:
        from gui.shading_display import (
            _compute_face_shade,
            _shading_sharpness_response,
            _shading_effective_light_elevation,
            _shading_fixed_light_elevation,
            _crisp_sharpness_contrast_gain,
            _crisp_blend_ambient_floor,
        )
        _, overdrive = _shading_sharpness_response(sharpness)
        effective_elevation = _shading_effective_light_elevation(app, overdrive)
        signature = (
            round(azimuth, 6), round(sharpness, 6), round(ambient, 6),
            round(effective_elevation, 6),
        )
        if topo.shade is not None and topo.shade_signature == signature:
            return topo.shade, signature

        raw = np.asarray(
            _compute_face_shade(
                topo.points,
                topo.faces,
                azimuth,
                effective_elevation,
                ambient,
            ),
            dtype=np.float32,
        )
        base_elevation = float(_shading_fixed_light_elevation(app))
        shadow_floor = max(ambient, float(_crisp_blend_ambient_floor(app)))
        gain = float(_crisp_sharpness_contrast_gain(sharpness))
        raw_horizontal = max(float(np.sin(np.radians(effective_elevation))), ambient)
        target_horizontal = max(float(np.sin(np.radians(base_elevation))), shadow_floor)
        shade = np.clip(
            target_horizontal + (raw - raw_horizontal) * gain,
            shadow_floor,
            1.0,
        ).astype(np.float32)
    except Exception as exc:
        # Local deterministic fallback.  This is only for older builds where
        # the latest Main Shading helper names do not exist.
        _log(f"view={topo.view_idx + 1} shading_helper=fallback reason={type(exc).__name__}")
        p0 = topo.points[topo.faces[:, 0]].astype(np.float64)
        p1 = topo.points[topo.faces[:, 1]].astype(np.float64)
        p2 = topo.points[topo.faces[:, 2]].astype(np.float64)
        normals = np.cross(p1 - p0, p2 - p0)
        normals /= np.linalg.norm(normals, axis=1, keepdims=True) + 1e-12
        el = np.deg2rad(45.0)
        az = np.deg2rad(360.0 - azimuth + 90.0)
        light = np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])
        raw = np.clip(normals @ light, 0.0, 1.0)
        shade = np.maximum(raw, ambient).astype(np.float32)
        signature = (round(azimuth, 6), round(sharpness, 6), round(ambient, 6), 45.0)

    topo.shade = np.ascontiguousarray(shade, dtype=np.float32)
    topo.shade_signature = signature
    return topo.shade, signature


def _packed_triangle_faces(n_faces: int) -> np.ndarray:
    packed = np.empty((int(n_faces), 4), dtype=np.int64)
    packed[:, 0] = 3
    packed[:, 1:] = np.arange(int(n_faces) * 3, dtype=np.int64).reshape(-1, 3)
    return packed.reshape(-1)


def _build_shaded_actor(app, vtk_widget, topo: _SectionTopology, palette: dict):
    t0 = time.perf_counter()
    _clear_mesh_actors(vtk_widget, topo.view_idx)
    shade, _ = _section_face_shade(app, topo)

    face_vertex_ids = topo.faces.reshape(-1)
    dup_points = np.ascontiguousarray(topo.points[face_vertex_ids], dtype=np.float32)
    face_classes = topo.classes[topo.faces].reshape(-1)
    _palette_rgb._app_palette = getattr(app, "class_palette", {}) or {}
    class_rgb = _palette_rgb(face_classes, palette).astype(np.float32)
    repeated_shade = np.repeat(shade, 3).astype(np.float32)
    rgb = np.clip(class_rgb * repeated_shade[:, None], 0.0, 255.0).astype(np.uint8)

    mesh = pv.PolyData(dup_points, _packed_triangle_faces(len(topo.faces)))
    mesh.point_data[_SHADED_RGB] = rgb
    actor = vtk_widget.add_mesh(
        mesh,
        scalars=_SHADED_RGB,
        rgb=True,
        preference="point",
        lighting=False,
        show_edges=False,
        smooth_shading=False,
        reset_camera=False,
        pickable=True,
        name=_SHADED_ACTOR.format(view=topo.view_idx),
    )
    try:
        actor.GetProperty().EdgeVisibilityOff()
        actor.GetProperty().SetInterpolationToFlat()
    except Exception:
        pass
    topo.shaded_mesh = mesh
    topo.shaded_actor = actor
    topo.shaded_rgb = mesh.point_data[_SHADED_RGB]
    topo.surface_mesh = None
    topo.surface_actor = None
    _log(
        f"view={topo.view_idx + 1} mode=shaded_class status=actor_ready "
        f"faces={len(topo.faces):,} presentation=crisp_barycentric "
        f"ms={(time.perf_counter() - t0) * 1000.0:.1f}"
    )
    return actor


def _surface_params(app):
    azimuth = float(getattr(app, "surface_azimuth", getattr(app, "last_shade_azimuth", 315.0)))
    angle = float(getattr(app, "surface_angle", getattr(app, "surface_light_elevation", 45.0)))
    ambient = float(np.clip(getattr(app, "surface_ambient", getattr(app, "shade_ambient", 0.25)), 0.0, 0.95))
    ramp = getattr(app, "surface_color_ramp", None)
    if ramp is None:
        ramp = getattr(app, "elevation_color_ramp", None)
    if ramp is None:
        ramp = getattr(app, "surface_ramp", None)
    return azimuth, angle, ambient, ramp


def _surface_range(app, z: np.ndarray) -> Tuple[float, float]:
    # Respect explicit/current Surface range if present; otherwise robust local
    # section percentiles avoid one high tree return flattening the whole ramp.
    for lo_name, hi_name in (
        ("surface_z_lo", "surface_z_hi"),
        ("surface_min_elevation", "surface_max_elevation"),
    ):
        lo = getattr(app, lo_name, None)
        hi = getattr(app, hi_name, None)
        if lo is not None and hi is not None:
            try:
                lo = float(lo); hi = float(hi)
                if np.isfinite(lo) and np.isfinite(hi) and hi > lo:
                    return lo, hi
            except Exception:
                pass
    lo = float(np.percentile(z, 1.0))
    hi = float(np.percentile(z, 99.0))
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        lo = float(np.min(z)); hi = float(np.max(z))
    if hi <= lo:
        hi = lo + 1.0
    return lo, hi


def _build_surface_actor(app, vtk_widget, topo: _SectionTopology):
    t0 = time.perf_counter()
    _clear_mesh_actors(vtk_widget, topo.view_idx)
    azimuth, angle, ambient, ramp = _surface_params(app)
    z_lo, z_hi = _surface_range(app, topo.points[:, 2].astype(np.float64))
    try:
        from gui.surface_mode import _compute_surface_face_colors
        face_rgb = _compute_surface_face_colors(
            topo.points,
            topo.faces,
            azimuth,
            angle,
            ambient,
            z_lo,
            z_hi,
            ramp=ramp,
        )
    except Exception as exc:
        _log(f"view={topo.view_idx + 1} surface_helper=fallback reason={type(exc).__name__}")
        zf = topo.points[topo.faces, 2].mean(axis=1)
        norm = np.clip((zf - z_lo) / max(z_hi - z_lo, 1e-12), 0.0, 1.0)
        face_rgb = np.column_stack((255.0 * norm, 255.0 * (1.0 - np.abs(norm - 0.5) * 2.0), 255.0 * (1.0 - norm)))
        face_rgb = np.clip(face_rgb, 0, 255).astype(np.uint8)

    packed = np.empty((len(topo.faces), 4), dtype=np.int64)
    packed[:, 0] = 3
    packed[:, 1:] = topo.faces.astype(np.int64)
    mesh = pv.PolyData(topo.points, packed.reshape(-1))
    mesh.cell_data[_SURFACE_RGB] = np.ascontiguousarray(face_rgb, dtype=np.uint8)
    actor = vtk_widget.add_mesh(
        mesh,
        scalars=_SURFACE_RGB,
        rgb=True,
        preference="cell",
        lighting=False,
        show_edges=False,
        smooth_shading=False,
        reset_camera=False,
        pickable=True,
        name=_SURFACE_ACTOR.format(view=topo.view_idx),
    )
    try:
        actor.GetProperty().EdgeVisibilityOff()
        actor.GetProperty().SetInterpolationToFlat()
    except Exception:
        pass
    topo.surface_mesh = mesh
    topo.surface_actor = actor
    topo.shaded_mesh = None
    topo.shaded_actor = None
    topo.shaded_rgb = None
    _log(
        f"view={topo.view_idx + 1} mode=surface status=actor_ready "
        f"faces={len(topo.faces):,} presentation=solid_cell_surface "
        f"ms={(time.perf_counter() - t0) * 1000.0:.1f}"
    )
    return actor


def apply_section_display_mode(
    app,
    view_idx: int,
    mode: str,
    *,
    quality_mode: str = "normal",
    palette=None,
    force: bool = False,
    reason: str = "apply",
) -> bool:
    """Apply Shaded Classification or Surface to one Cross Section.

    Returns True only when this module handled the requested mode.  Existing
    Class/RGB/Intensity/Elevation/Depth/Line code should keep handling all other
    modes unchanged.
    """
    mode = _normalize_mode(mode)
    if mode not in MESH_MODES:
        return False
    view_idx = int(view_idx)
    quality = _normalize_quality(quality_mode)
    vtk_widget = _get_vtk_widget(app, view_idx)
    if vtk_widget is None:
        _log(f"view={view_idx + 1} mode={mode} status=skip reason=no_vtk")
        return False

    palette = _get_palette(app, view_idx, palette)
    record_section_display_mode(
        app, view_idx, mode, quality_mode=quality, palette=palette
    )
    topo, reused = _get_topology(app, view_idx, palette, quality, force=force)
    if topo is None:
        _log(f"view={view_idx + 1} mode={mode} status=skip reason=no_topology")
        return False

    try:
        _hide_unified(vtk_widget, view_idx, hidden=True)
        if mode == "shaded_class":
            _build_shaded_actor(app, vtk_widget, topo, palette)
        else:
            _build_surface_actor(app, vtk_widget, topo)
        setattr(vtk_widget, "_naksha_section_render_mode", mode)
        vtk_widget.render()
        _log(
            f"view={view_idx + 1} mode={mode} reason={reason} "
            f"topology={'reused' if reused else 'built'} full_main_scan=0"
        )
        return True
    except Exception as exc:
        _hide_unified(vtk_widget, view_idx, hidden=False)
        _log(
            f"view={view_idx + 1} mode={mode} status=failed "
            f"reason={type(exc).__name__}:{exc}"
        )
        import traceback
        traceback.print_exc()
        return False


def leave_section_mesh_mode(app, view_idx: int, *, next_mode="class", render=True) -> bool:
    """Remove only section mesh actors and restore the existing point actor."""
    view_idx = int(view_idx)
    vtk_widget = _get_vtk_widget(app, view_idx)
    record_section_display_mode(app, view_idx, next_mode)
    if vtk_widget is None:
        return False
    _clear_mesh_actors(vtk_widget, view_idx)
    _hide_unified(vtk_widget, view_idx, hidden=False)
    try:
        setattr(vtk_widget, "_naksha_section_render_mode", _normalize_mode(next_mode))
    except Exception:
        pass
    if render:
        try:
            vtk_widget.render()
        except Exception:
            pass
    return True


def _dirty_local_vertices(topo: _SectionTopology, changed_mask, current_classes: np.ndarray) -> np.ndarray:
    if changed_mask is not None:
        try:
            mask = np.asarray(changed_mask, dtype=bool).reshape(-1)
            if len(mask) > int(np.max(topo.global_indices, initial=-1)):
                return np.flatnonzero(mask[topo.global_indices]).astype(np.int64)
        except Exception:
            pass
    # Safe fallback for callbacks without a mask: compare only this section's
    # cached representatives, never the full main cloud.
    if len(topo.class_snapshot) == len(current_classes):
        return np.flatnonzero(topo.class_snapshot != current_classes).astype(np.int64)
    return np.arange(len(current_classes), dtype=np.int64)


def _dirty_section_points(topo: _SectionTopology, changed_mask, current_all: np.ndarray) -> np.ndarray:
    if changed_mask is not None:
        try:
            mask = np.asarray(changed_mask, dtype=bool).reshape(-1)
            if len(mask) > int(np.max(topo.section_global_indices_all, initial=-1)):
                return np.flatnonzero(mask[topo.section_global_indices_all]).astype(np.int64)
        except Exception:
            pass
    if len(topo.section_class_snapshot_all) == len(current_all):
        return np.flatnonzero(topo.section_class_snapshot_all != current_all).astype(np.int64)
    return np.arange(len(current_all), dtype=np.int64)


def _membership_changed(old_classes: np.ndarray, new_classes: np.ndarray, palette: dict) -> bool:
    if len(old_classes) == 0:
        return False
    old_vis = np.fromiter((_class_visible(int(c), palette) for c in old_classes), bool, len(old_classes))
    new_vis = np.fromiter((_class_visible(int(c), palette) for c in new_classes), bool, len(new_classes))
    return bool(np.any(old_vis != new_vis))


def _update_shaded_dirty_faces(app, topo: _SectionTopology, palette: dict, dirty_local: np.ndarray) -> bool:
    if topo.shaded_mesh is None or topo.shaded_rgb is None or len(dirty_local) == 0:
        return False
    t0 = time.perf_counter()
    marked = np.zeros(len(topo.points), dtype=bool)
    marked[dirty_local] = True
    dirty_faces = np.flatnonzero(np.any(marked[topo.faces], axis=1))
    if len(dirty_faces) == 0:
        return True

    shade, _ = _section_face_shade(app, topo)
    face_cls = topo.classes[topo.faces[dirty_faces]].reshape(-1)
    _palette_rgb._app_palette = getattr(app, "class_palette", {}) or {}
    colors = _palette_rgb(face_cls, palette).astype(np.float32)
    local_shade = np.repeat(shade[dirty_faces], 3).astype(np.float32)
    colors = np.clip(colors * local_shade[:, None], 0.0, 255.0).astype(np.uint8)

    base = (dirty_faces[:, None] * 3 + np.arange(3, dtype=np.int64)[None, :]).reshape(-1)
    topo.shaded_rgb[base] = colors
    try:
        topo.shaded_rgb.VTKObject.Modified()
    except Exception:
        pass
    try:
        topo.shaded_mesh.GetPointData().Modified()
        topo.shaded_mesh.Modified()
    except Exception:
        pass
    vtk_widget = _get_vtk_widget(app, topo.view_idx)
    if vtk_widget is not None:
        vtk_widget.render()
    _log(
        f"view={topo.view_idx + 1} mode=shaded_class status=sparse_refresh "
        f"dirty_vertices={len(dirty_local):,} dirty_faces={len(dirty_faces):,} "
        f"topology_rebuild=0 ms={(time.perf_counter() - t0) * 1000.0:.1f}"
    )
    return True


def refresh_section_display_after_classification(
    app,
    view_idx: int,
    changed_mask=None,
    *,
    operation: str = "classification",
) -> bool:
    """Mode-aware section refresh after classify/undo/redo.

    Returns False for non-mesh modes so the caller can run its existing point
    refresh unchanged.
    """
    mode = get_section_display_mode(app, view_idx)
    if mode not in MESH_MODES:
        return False

    # Keep the established section-class mirror coherent even though the mesh
    # actor consumes the visual refresh. This guarantees a later switch back
    # to Class/RGB/etc. never exposes a stale hidden point actor.
    try:
        sync_mirror = getattr(app, "_sync_section_mirror_from_data", None)
        if callable(sync_mirror):
            sync_mirror(int(view_idx))
    except Exception:
        pass

    _ensure_state(app)
    topo = app._section_mesh_display_cache.get(int(view_idx))
    palette = _get_palette(app, view_idx)
    settings = app.view_display_settings.get(int(view_idx) + 1, {}) or {}
    quality = _normalize_quality(settings.get("quality", "normal"))

    if not isinstance(topo, _SectionTopology):
        return apply_section_display_mode(
            app, view_idx, mode, quality_mode=quality, palette=palette,
            force=True, reason=f"{operation}_missing_cache",
        )

    data = getattr(app, "data", None)
    if not isinstance(data, dict) or data.get("classification") is None:
        return False
    classification = np.asarray(data["classification"])

    # First inspect every section point (including hidden/non-representative
    # points) for a visibility-boundary crossing. Geometry membership changes
    # only in that case.
    current_all = classification[topo.section_global_indices_all].astype(np.int32, copy=False)
    dirty_all = _dirty_section_points(topo, changed_mask, current_all)
    if len(dirty_all):
        old_all = topo.section_class_snapshot_all[dirty_all].copy()
        new_all = current_all[dirty_all].copy()
        if _membership_changed(old_all, new_all, palette):
            _log(
                f"view={int(view_idx) + 1} mode={mode} status=membership_changed "
                f"operation={operation} changed={len(dirty_all):,} topology_rebuild=1"
            )
            return apply_section_display_mode(
                app, view_idx, mode, quality_mode=quality, palette=palette,
                force=True, reason=f"{operation}_visibility_boundary",
            )
        topo.section_class_snapshot_all[dirty_all] = new_all

    # For visible representative vertices, a visible -> visible class change is
    # presentation-only. Update only incident duplicated-face RGB.
    current = classification[topo.global_indices].astype(np.int32, copy=False)
    dirty_local = _dirty_local_vertices(topo, changed_mask, current)
    if len(dirty_local) == 0:
        return True
    new = current[dirty_local].copy()
    topo.classes[dirty_local] = new
    topo.class_snapshot[dirty_local] = new
    if mode == "shaded_class":
        if _update_shaded_dirty_faces(app, topo, palette, dirty_local):
            return True
        return apply_section_display_mode(
            app, view_idx, mode, quality_mode=quality, palette=palette,
            force=False, reason=f"{operation}_actor_repair",
        )

    # Surface colour is class-independent.  If old/new classes are both visible
    # (or both hidden), neither geometry membership nor surface colour changed.
    _log(
        f"view={int(view_idx) + 1} mode=surface status=noop_classification "
        f"operation={operation} changed={len(dirty_local):,} topology_rebuild=0"
    )
    return True


def reapply_section_mesh_mode_after_geometry(app, view_idx: int) -> bool:
    """Called after SectionController rebuilds the unified point actor."""
    mode = get_section_display_mode(app, view_idx)
    if mode not in MESH_MODES:
        return False
    settings = (getattr(app, "view_display_settings", {}) or {}).get(int(view_idx) + 1, {}) or {}
    quality = _normalize_quality(settings.get("quality", "normal"))
    # New section coordinates require a new local TIN; Main View remains untouched.
    return apply_section_display_mode(
        app, view_idx, mode, quality_mode=quality, force=True,
        reason="section_geometry_rebuilt",
    )


def inherit_section_display_mode(app, source_idx: int, target_idx: int, *, force=False) -> bool:
    """Optional sync helper: copy only mode/settings, never palette ownership."""
    _ensure_state(app)
    source_slot = int(source_idx) + 1
    target_slot = int(target_idx) + 1
    mode = _normalize_mode(app.view_display_modes.get(source_slot, "class"))
    source_settings = dict(app.view_display_settings.get(source_slot, {}) or {})
    app.view_display_modes[target_slot] = mode
    app.view_display_settings[target_slot] = source_settings.copy()
    if mode not in MESH_MODES:
        return False
    return apply_section_display_mode(
        app,
        target_idx,
        mode,
        quality_mode=source_settings.get("quality", "normal"),
        palette=_get_palette(app, target_idx),
        force=force,
        reason=f"sync_from_view_{int(source_idx) + 1}",
    )


def refresh_all_section_shading_lighting(app) -> int:
    """Re-bake current Azimuth/Sharpness/Ambient on active section shading."""
    _ensure_state(app)
    refreshed = 0
    for view_idx in range(4):
        if get_section_display_mode(app, view_idx) != "shaded_class":
            continue
        topo = app._section_mesh_display_cache.get(view_idx)
        vtk_widget = _get_vtk_widget(app, view_idx)
        if not isinstance(topo, _SectionTopology) or vtk_widget is None:
            continue
        topo.shade = None
        topo.shade_signature = None
        palette = _get_palette(app, view_idx)
        try:
            _build_shaded_actor(app, vtk_widget, topo, palette)
            vtk_widget.render()
            refreshed += 1
        except Exception:
            pass
    return refreshed


def clear_section_display_cache(app, view_idx: Optional[int] = None) -> None:
    """Drop section-only mesh state.  Main Shading/Surface caches are untouched."""
    _ensure_state(app)
    targets = range(4) if view_idx is None else (int(view_idx),)
    for idx in targets:
        vtk_widget = _get_vtk_widget(app, idx)
        if vtk_widget is not None:
            _clear_mesh_actors(vtk_widget, idx)
            _hide_unified(vtk_widget, idx, hidden=False)
        app._section_mesh_display_cache.pop(idx, None)
