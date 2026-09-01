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
(``app._shaded_mesh_polydata`` / ``app._surface_mesh_polydata``, built and
owned entirely by gui/shading_display.py and gui/surface_mode.py). It only
reads that cached polydata -- never rebuilds or mutates it, and never
touches Main View's actors/plotter -- so Main View's own behavior is
unaffected regardless of what mode Main View is currently showing.
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


def _source_polydata(app, mode):
    attr = "_shaded_mesh_polydata" if mode == "shaded" else "_surface_mesh_polydata"
    return getattr(app, attr, None)


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

    source_mesh = _source_polydata(app, mode)
    if source_mesh is None:
        label = "Shaded Classification" if mode == "shaded" else "Surface"
        print(f"   ⚠️ Section {view_idx+1}: Main View has no {label} mesh built yet — "
              f"enable {label} in Main View first")
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

    actor_name = _profile_actor_name(view_idx, mode)
    try:
        if hasattr(vtk_widget, "actors") and actor_name in vtk_widget.actors:
            vtk_widget.remove_actor(actor_name, render=False)
    except Exception:
        pass

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
