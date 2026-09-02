"""
Cross-section Shaded Classification / Surface via a real mesh slab clip.

Main View's Shaded Classification / Surface mesh is a real triangulated
SURFACE (not a solid), so an exact zero-thickness plane intersection
(vtkCutter) only ever produces a thin 1D curve -- it looked like a wire /
"heartbeat line", not a filled slice. A real "apple cut" needs to show the
actual triangulated faces of the mesh within a thin slab around the
cutting line (the same buffer width already used to select the section's
own point cloud), so it renders as solid, colored, filled geometry.

This module reuses Main View's already-built shaded/surface mesh polydata
when available (``app._shaded_mesh_polydata`` / ``app._surface_mesh_polydata``,
built and owned entirely by gui/shading_display.py and gui/surface_mode.py) --
it only reads that cached polydata, never rebuilds or mutates it, and never
touches Main View's actors/plotter.

Both modes must also work when Main View is currently showing something
else (Class/RGB/...), so when no cached mesh exists each falls back to
calling Main View's own pure, actor-free geometry backend directly
(``_compute_shading_geometry_backend`` / ``_compute_surface_geometry_backend``)
to build an equivalent mesh in memory -- still without touching
``app.vtk_widget``, ``app.display_mode``, or any Main View actor, so
Main View's current display is left completely untouched either way.
"""
import numpy as np
import pyvista as pv


def _section_p1_p2(app, view_idx):
    p1 = getattr(app, f"section_{view_idx}_P1", None)
    p2 = getattr(app, f"section_{view_idx}_P2", None)
    if p1 is None or p2 is None:
        return None, None
    return np.asarray(p1, dtype=np.float64), np.asarray(p2, dtype=np.float64)


def _section_half_width(app, view_idx, default=5.0):
    hw = getattr(app, f"section_{view_idx}_half_width", None)
    try:
        hw = float(hw)
    except (TypeError, ValueError):
        hw = None
    return hw if hw and hw > 0 else default


def _source_polydata(app, view_idx, mode):
    """Return Main View's already-built mesh for `mode` if present (fast
    path, zero extra cost); otherwise build an equivalent mesh independently
    so the section works regardless of Main View's current display mode."""
    attr = "_shaded_mesh_polydata" if mode == "shaded" else "_surface_mesh_polydata"
    mesh = getattr(app, attr, None)
    if mesh is not None:
        return mesh
    if mode == "shaded":
        return _build_independent_shaded_mesh(app, view_idx)
    return _build_independent_surface_mesh(app, view_idx)


def _section_palette_dict(app, view_idx):
    """Return the class-visibility/color palette for THIS section's own
    slot (view_idx+1 in Display Mode's per-slot view_palettes), falling
    back to the global class_palette. Main View's own visibility helpers
    (_get_shading_visibility / surface_palette) are hardcoded to slot 0,
    which made section Shaded/Surface silently ignore a class filter set
    directly on the section instead of on Main View -- this fixes that by
    reading the section's own slot."""
    slot_idx = view_idx + 1
    dlg = getattr(app, "display_mode_dialog", None) or getattr(app, "display_dialog", None)
    if dlg is not None:
        vp = getattr(dlg, "view_palettes", None)
        if isinstance(vp, dict) and vp.get(slot_idx):
            return vp[slot_idx]
    return getattr(app, "class_palette", {}) or {}


def _section_visible_classes(app, view_idx):
    palette = _section_palette_dict(app, view_idx)
    return {int(c) for c, e in palette.items() if e.get("show", True)}


def _independent_shaded_cache_key(app, xyz_raw, vc, azimuth, angle, ambient, quality_mode):
    try:
        from gui.shading_display import _compute_xyz_hash
        data_hash = _compute_xyz_hash(xyz_raw)
    except Exception:
        data_hash = None
    return (data_hash, tuple(sorted(vc)), round(float(azimuth), 3),
            round(float(angle), 3), round(float(ambient), 4), quality_mode)


def _build_independent_shaded_mesh(app, view_idx):
    """Build a Shaded Classification mesh independent of Main View's own
    display mode, so the section's Shaded Classification works even when
    Main View is currently showing Class/RGB/etc. This calls the same
    pure, actor-free geometry backend Main View itself uses
    (gui/shading_display.py's ``_compute_shading_geometry_backend``) --
    it never touches ``app.vtk_widget``, ``app.display_mode``, or any
    Main View actor, so Main View's current display is left untouched.

    Visible classes and colors come from THIS section's own palette
    (view_idx), not Main View's -- so filtering a class on the section
    itself actually affects the section's Shaded/Surface mesh.

    Result is cached on `app` and only rebuilt when the underlying data,
    visible classes, or light settings actually change.
    """
    try:
        from gui.shading_display import (
            _compute_shading_geometry_backend,
            normalize_shading_quality,
        )
        from gui.flight_line_filter import flight_line_visibility_mask
    except Exception as exc:
        print(f"   ⚠️ Section shaded mesh: import failed ({exc})")
        return None

    data = getattr(app, "data", None)
    if not isinstance(data, dict):
        return None
    xyz_raw = data.get("xyz")
    classes_raw = data.get("classification")
    if xyz_raw is None or classes_raw is None:
        return None

    try:
        line_mask = flight_line_visibility_mask(app, len(xyz_raw))
    except Exception:
        line_mask = None
    if line_mask is not None:
        if not np.any(line_mask):
            return None
        if not np.all(line_mask):
            xyz_raw = xyz_raw[line_mask]
            classes_raw = classes_raw[line_mask]

    vc = _section_visible_classes(app, view_idx)
    if not vc:
        return None

    azimuth = float(getattr(app, "last_shade_azimuth", 45.0))
    angle = float(getattr(app, "last_shade_angle", 45.0))
    ambient = float(getattr(app, "shade_ambient", 0.25))
    quality_mode = normalize_shading_quality(getattr(app, "shading_quality", "normal"))

    key = _independent_shaded_cache_key(app, xyz_raw, vc, azimuth, angle, ambient, quality_mode)
    cache_attr = f"_section_{view_idx}_independent_shaded_cache"
    cached = getattr(app, cache_attr, None)
    if cached is not None and cached.get("key") == key:
        return cached.get("mesh")

    try:
        from gui.shading_display import _compute_xyz_hash
        res = _compute_shading_geometry_backend(
            xyz_raw, classes_raw, vc, azimuth, angle, ambient,
            3.0, None, _compute_xyz_hash(xyz_raw), quality_mode=quality_mode,
        )
    except Exception as exc:
        print(f"   ⚠️ Section shaded mesh: independent build failed ({exc})")
        return None

    if res.get("empty", False):
        return None

    faces = np.asarray(res["faces"], dtype=np.int64)
    if len(faces) == 0:
        return None
    xyz_final = np.asarray(res["xyz_final"], dtype=np.float64)
    unique_indices = np.asarray(res["unique_indices"], dtype=np.int64)
    shade = np.asarray(res["shade"], dtype=np.float32)

    cm = classes_raw.astype(np.int32)[unique_indices]
    palette = _section_palette_dict(app, view_idx)
    mc = max(int(cm.max()) + 1, 256)
    lut = np.zeros((mc, 3), dtype=np.float32)
    for c, e in palette.items():
        ci = int(c)
        if ci < mc:
            lut[ci] = e.get("color", (128, 128, 128))

    face_class = cm[faces[:, 0]]
    base_color = lut[face_class]
    face_shade = shade if len(shade) == len(faces) else np.ones(len(faces), dtype=np.float32)
    face_colors = np.clip(base_color * face_shade[:, None], 0, 255).astype(np.uint8)

    fv = np.empty((len(faces), 4), dtype=np.int64)
    fv[:, 0] = 3
    fv[:, 1:] = faces
    mesh = pv.PolyData(xyz_final, fv.ravel())
    mesh.cell_data["RGB"] = face_colors

    setattr(app, cache_attr, {"key": key, "mesh": mesh})
    print(f"   🔨 Section {view_idx+1}: built independent shaded mesh "
          f"({len(xyz_final):,} verts, {len(faces):,} faces, quality={quality_mode}, "
          f"visible_classes={sorted(vc)})")
    return mesh


def _independent_surface_cache_key(app, xyz_all, global_idx, azimuth, angle, ambient, quality_mode):
    try:
        from gui.shading_display import _compute_xyz_hash
        data_hash = _compute_xyz_hash(xyz_all[global_idx]) if len(global_idx) else None
    except Exception:
        data_hash = None
    return (data_hash, int(global_idx.size), round(float(azimuth), 3),
            round(float(angle), 3), round(float(ambient), 4), quality_mode)


def _section_surface_visible_mask(app, view_idx, xyz_all):
    """Same membership logic as gui.surface_mode.surface_visible_mask, but
    sourced from THIS section's own palette (view_idx) instead of Main
    View's slot 0 -- so a class filter set directly on the section
    actually affects the section's Surface mesh."""
    from gui.flight_line_filter import flight_line_visibility_mask
    from gui.surface_mode import _surface_support_entry

    line_mask = flight_line_visibility_mask(app, len(xyz_all))
    classes = (getattr(app, "data", None) or {}).get("classification")
    if classes is None:
        return line_mask

    classes_arr = np.asarray(classes)
    palette = _section_palette_dict(app, view_idx)
    if not palette:
        return line_mask

    if classes_arr.dtype == np.uint8:
        lut = np.zeros(256, dtype=bool)
        for code, entry in palette.items():
            try:
                code_int = int(code)
                if 0 <= code_int < 256:
                    lut[code_int] = _surface_support_entry(code_int, entry)
            except Exception:
                continue
        return lut[classes_arr] & line_mask

    supported = []
    for code, entry in palette.items():
        try:
            code_int = int(code)
        except Exception:
            continue
        if _surface_support_entry(code_int, entry):
            supported.append(code_int)
    if not supported:
        return np.zeros(len(xyz_all), dtype=bool)
    return np.isin(classes_arr, np.asarray(supported, dtype=classes_arr.dtype)) & line_mask


def _build_independent_surface_mesh(app, view_idx):
    """Build a Surface mesh independent of Main View's own display mode,
    the same way _build_independent_shaded_mesh does for Shaded
    Classification -- calls Main View's own pure, actor-free geometry
    backend (gui/surface_mode.py's ``_compute_surface_geometry_backend``)
    directly, never touching ``app.vtk_widget``/``app.display_mode``/any
    Main View actor. Colors come straight from the backend's own result
    (elevation-ramp + lighting), matching Main View's Surface exactly.

    Visible classes come from THIS section's own palette (view_idx), not
    Main View's.
    """
    try:
        from gui.surface_mode import (
            _compute_surface_geometry_backend,
            normalize_surface_quality,
            _surface_quality_target,
            _surface_z_bounds_cached,
            _normalize_surface_ramp,
        )
    except Exception as exc:
        print(f"   ⚠️ Section surface mesh: import failed ({exc})")
        return None

    data = getattr(app, "data", None)
    if not isinstance(data, dict):
        return None
    xyz_all = data.get("xyz")
    if xyz_all is None:
        return None

    try:
        vis_mask = _section_surface_visible_mask(app, view_idx, xyz_all)
    except Exception as exc:
        print(f"   ⚠️ Section surface mesh: visibility mask failed ({exc})")
        vis_mask = None
    if vis_mask is None or len(vis_mask) != len(xyz_all):
        vis_mask = np.ones(len(xyz_all), dtype=bool)
    global_idx = np.flatnonzero(vis_mask)
    if global_idx.size < 3:
        return None

    quality_mode = normalize_surface_quality(getattr(app, "surface_quality", "normal"))
    precision = float(getattr(app, "surface_dedup_precision", 0.0) or 0.0)
    target_max = _surface_quality_target(quality_mode, int(global_idx.size))
    max_edge = float(getattr(app, "surface_max_edge", 0.0) or 0.0)
    z_bounds = _surface_z_bounds_cached(app, xyz_all)
    azimuth = float(getattr(app, "last_shade_azimuth", 45.0))
    angle = float(getattr(app, "last_shade_angle", 45.0))
    ambient = float(getattr(app, "shade_ambient", 0.22))
    ramp = _normalize_surface_ramp(
        getattr(app, "surface_color_ramp", None) or getattr(app, "elevation_color_ramp", None)
    )

    key = _independent_surface_cache_key(app, xyz_all, global_idx, azimuth, angle, ambient, quality_mode)
    cache_attr = f"_section_{view_idx}_independent_surface_cache"
    cached = getattr(app, cache_attr, None)
    if cached is not None and cached.get("key") == key:
        return cached.get("mesh")

    try:
        res = _compute_surface_geometry_backend(
            xyz_all, global_idx, precision, target_max, max_edge,
            azimuth, angle, ambient, ramp, quality_mode=quality_mode, z_bounds=z_bounds,
        )
    except Exception as exc:
        print(f"   ⚠️ Section surface mesh: independent build failed ({exc})")
        return None

    if res.get("empty", False):
        return None

    pts = np.asarray(res["points"], dtype=np.float64)
    faces = np.asarray(res["faces"], dtype=np.int64)
    colors = np.asarray(res["colors"], dtype=np.uint8)
    if len(faces) == 0:
        return None

    fv = np.empty((len(faces), 4), dtype=np.int64)
    fv[:, 0] = 3
    fv[:, 1:] = faces
    mesh = pv.PolyData(pts, fv.ravel())
    if len(colors) == len(faces):
        mesh.cell_data["RGB"] = colors
    elif len(colors) == len(pts):
        mesh.point_data["RGB"] = colors

    setattr(app, cache_attr, {"key": key, "mesh": mesh})
    print(f"   🔨 Section {view_idx+1}: built independent surface mesh "
          f"({len(pts):,} verts, {len(faces):,} faces, quality={quality_mode}, "
          f"eligible_points={global_idx.size:,})")
    return mesh


def _mesh_faces_n3(mesh):
    faces = getattr(mesh, "regular_faces", None)
    if faces is not None and len(faces):
        return np.asarray(faces, dtype=np.int64)
    flat = np.asarray(mesh.faces)
    return flat.reshape(-1, 4)[:, 1:4].astype(np.int64)


def _slab_clip_and_transform(mesh, P1, P2, half_width):
    """Keep only the mesh faces lying within `half_width` of the section's
    cutting line, transform their vertices into the section's existing
    local (along, across, elevation) frame, and return
    (local_points[N,3], faces[M,3], point_rgb|None, cell_rgb|None)."""
    v = P2[:2] - P1[:2]
    length = float(np.linalg.norm(v))
    if length < 1e-9:
        return None
    dir_vec = v / length
    perp = np.array([-dir_vec[1], dir_vec[0]], dtype=np.float64)

    pts = np.asarray(mesh.points, dtype=np.float64)
    rel = pts[:, :2] - P1[:2]
    along = rel @ dir_vec
    across = rel @ perp
    z = pts[:, 2]

    vert_mask = np.abs(across) <= float(half_width)
    if not np.any(vert_mask):
        return None

    faces = _mesh_faces_n3(mesh)
    face_mask = vert_mask[faces].all(axis=1)
    if not np.any(face_mask):
        return None
    kept_faces = faces[face_mask]

    used = np.unique(kept_faces)
    remap = np.full(len(pts), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    new_faces = remap[kept_faces]

    local_pts = np.column_stack(
        [along[used], across[used], z[used]]
    ).astype(np.float32)

    point_rgb = None
    cell_rgb = None
    if "RGB" in mesh.point_data:
        point_rgb = np.clip(np.asarray(mesh.point_data["RGB"])[used], 0, 255).astype(np.uint8)
    elif "RGB" in mesh.cell_data:
        cell_rgb = np.clip(np.asarray(mesh.cell_data["RGB"])[face_mask], 0, 255).astype(np.uint8)

    return local_pts, new_faces, point_rgb, cell_rgb


def _profile_actor_name(view_idx, mode):
    return f"_section_{view_idx}_{mode}_profile"


def build_section_shaded_surface_actor(app, view_idx, mode) -> bool:
    """Build/refresh the mesh-slab profile actor for a section view.

    mode: "shaded" or "surface". Returns True on success.
    """
    if not hasattr(app, "section_vtks") or view_idx not in app.section_vtks:
        return False
    vtk_widget = app.section_vtks[view_idx]
    if vtk_widget is None:
        return False

    P1, P2 = _section_p1_p2(app, view_idx)
    if P1 is None or P2 is None:
        print(f"   ⚠️ Section {view_idx+1}: no cutting line stored yet")
        return False

    source_mesh = _source_polydata(app, view_idx, mode)
    if source_mesh is None:
        label = "Shaded Classification" if mode == "shaded" else "Surface"
        print(f"   ⚠️ Section {view_idx+1}: could not build a {label} mesh "
              f"(no point cloud loaded, or no classes currently visible)")
        return False

    half_width = _section_half_width(app, view_idx)
    result = _slab_clip_and_transform(source_mesh, P1, P2, half_width)
    if result is None:
        print(f"   ⚠️ Section {view_idx+1}: cutting slab does not intersect the mesh")
        return False
    local_pts, faces, point_rgb, cell_rgb = result

    nf = len(faces)
    fv = np.empty((nf, 4), dtype=np.int64)
    fv[:, 0] = 3
    fv[:, 1:] = faces
    profile = pv.PolyData(local_pts, fv.ravel())
    if point_rgb is not None:
        profile.point_data["RGB"] = point_rgb
    elif cell_rgb is not None:
        profile.cell_data["RGB"] = cell_rgb

    # Remove BOTH modes' profile actors first -- only one of Shaded/Surface
    # should ever be visible at once. Removing just the same-name actor
    # left the previous mode's actor behind when switching Surface<->Shaded,
    # showing both overlapping at the same time.
    for other_mode in ("shaded", "surface"):
        other_name = _profile_actor_name(view_idx, other_mode)
        try:
            if hasattr(vtk_widget, "actors") and other_name in vtk_widget.actors:
                vtk_widget.remove_actor(other_name, render=False)
        except Exception:
            pass

    actor_name = _profile_actor_name(view_idx, mode)
    has_rgb = point_rgb is not None or cell_rgb is not None
    new_actor = vtk_widget.add_mesh(
        profile,
        scalars="RGB" if has_rgb else None,
        rgb=has_rgb,
        color=None if has_rgb else "silver",
        show_edges=False,
        lighting=False,
        name=actor_name,
        render=False,
    )
    if new_actor is None:
        return False

    unified_name = f"_section_{view_idx}_unified"
    try:
        unified_actor = vtk_widget.actors.get(unified_name)
        if unified_actor is not None:
            unified_actor.SetVisibility(False)
    except Exception:
        pass

    try:
        vtk_widget.render()
    except Exception:
        pass

    print(f"   ✅ Section {view_idx+1}: {mode} mesh-slab profile built "
          f"({len(local_pts)} verts, {nf} faces, half_width={half_width:.2f}m)")
    return True


def remove_section_shaded_surface_actor(app, view_idx) -> None:
    """Remove any mesh-slab profile actor for this section and restore its
    normal point-cloud actor's visibility."""
    if not hasattr(app, "section_vtks") or view_idx not in app.section_vtks:
        return
    vtk_widget = app.section_vtks[view_idx]
    if vtk_widget is None:
        return

    for mode in ("shaded", "surface"):
        actor_name = _profile_actor_name(view_idx, mode)
        try:
            if hasattr(vtk_widget, "actors") and actor_name in vtk_widget.actors:
                vtk_widget.remove_actor(actor_name, render=False)
        except Exception:
            pass

    unified_name = f"_section_{view_idx}_unified"
    try:
        unified_actor = vtk_widget.actors.get(unified_name)
        if unified_actor is not None:
            unified_actor.SetVisibility(True)
    except Exception:
        pass

    try:
        vtk_widget.render()
    except Exception:
        pass
