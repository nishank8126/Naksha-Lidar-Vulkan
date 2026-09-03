"""
Central per-section mesh-mode refresh dispatcher.

app_window.py's _on_classification_finished calls
refresh_section_display_after_classification for EVERY open section on
EVERY classification_finished emission (regular classify commits, redo,
AI-dialog classify, By-Class dialog conversions, menu-sidebar operations),
before falling back to the raw point-actor refresh (fast_cross_section_update).

A section currently showing Shaded Classification or Surface mode is
driven by a separate triangulated mesh actor (see
gui/cross_section/section_shaded_surface.py), not the raw point actor --
unlike Main View, that mesh had no live-update mechanism at all, so it
kept showing pre-classify colors indefinitely. This module is the single
place that keeps it in sync: return True (handled, mesh actor refreshed)
for Shaded/Surface sections so the caller skips the point-actor path;
return False for every other display mode so existing behavior for
Class/RGB/Intensity/etc. sections is completely unchanged.
"""


def apply_section_display_mode(app, view_idx, target_mode, quality_mode=None, palette=None, force=False, reason="display_mode_apply"):
    """Apply Shaded Classification ("shaded_class") or Surface ("surface")
    mode to one section, using `palette` as that section's own
    class-visibility/color filter (independent of Main View's).

    Callers (gui/display_mode.py's DisplayMode dialog Apply handler)
    already write `palette` into display_mode_dialog.view_palettes[view_idx+1]
    before calling this -- _section_palette_dict reads from there, so no
    extra bookkeeping is needed here beyond making sure that write landed.
    """
    del force  # independent build is always used now; nothing to force past
    dlg = getattr(app, "display_mode_dialog", None)
    if dlg is not None and palette is not None:
        vp = getattr(dlg, "view_palettes", None)
        if isinstance(vp, dict):
            vp[view_idx + 1] = palette

    if quality_mode is not None:
        app.shading_quality = quality_mode

    mesh_mode = "shaded" if target_mode == "shaded_class" else "surface"
    try:
        from gui.cross_section.section_shaded_surface import build_section_shaded_surface_actor
        ok = build_section_shaded_surface_actor(app, view_idx, mesh_mode, force_independent=True)
    except Exception as exc:
        print(f"SECTION_MESH view={view_idx + 1} mode={mesh_mode} status=apply_exception reason={exc} via={reason}")
        return False
    if ok:
        print(f"SECTION_MESH view={view_idx + 1} mode={mesh_mode} status=applied via={reason}")
    return bool(ok)


def leave_section_mesh_mode(app, view_idx, next_mode="class", render=False):
    """Remove a section's Shaded/Surface mesh actor and restore its normal
    point-cloud actor's visibility, ahead of switching to `next_mode`.

    Thin wrapper around remove_section_shaded_surface_actor -- kept here so
    display_mode.py's call succeeds instead of falling through to its own
    (already-correct) direct call, silencing the geometry_reapply-style
    warning this used to print every time a section left Shaded/Surface.
    """
    del next_mode
    try:
        from gui.cross_section.section_shaded_surface import remove_section_shaded_surface_actor
        remove_section_shaded_surface_actor(app, view_idx)
    except Exception as exc:
        print(f"SECTION_MESH view={view_idx + 1} status=leave_failed reason={exc}")
        return False

    if render:
        vtk_widget = (getattr(app, "section_vtks", None) or {}).get(view_idx)
        if vtk_widget is not None:
            try:
                vtk_widget.render()
            except Exception:
                pass
    return True


def reapply_section_mesh_mode_after_geometry(app, view_idx):
    """Rebuild a section's Shaded/Surface mesh actor after its cutting-line
    geometry changed (section moved/resized -> _plot_section rebuilds the
    point-actor from scratch, which leaves any previous mesh actor stale/
    misaligned with the new slab). No-op for sections not currently in
    Shaded/Surface mode.
    """
    dlg = getattr(app, "display_mode_dialog", None)
    view_color_modes = getattr(dlg, "view_color_modes", {}) if dlg else {}
    slot_idx = view_idx + 1
    mode_idx = int(view_color_modes.get(slot_idx, 0) or 0)
    if mode_idx not in (1, 6):
        return False

    try:
        from gui.cross_section.section_shaded_surface import build_section_shaded_surface_actor
        mesh_mode = "shaded" if mode_idx == 1 else "surface"
        ok = build_section_shaded_surface_actor(app, view_idx, mesh_mode, force_independent=True)
    except Exception as exc:
        print(f"SECTION_MESH view={slot_idx} status=geometry_reapply_failed reason={exc}")
        return False

    if ok:
        print(f"SECTION_MESH view={slot_idx} status=geometry_reapplied mode={mesh_mode}")
    return bool(ok)


def refresh_section_display_after_classification(app, view_idx, changed_mask, operation="classification"):
    dlg = getattr(app, "display_mode_dialog", None)
    view_color_modes = getattr(dlg, "view_color_modes", {}) if dlg else {}
    slot_idx = view_idx + 1
    mode_idx = int(view_color_modes.get(slot_idx, 0) or 0)
    if mode_idx not in (1, 6):
        return False

    try:
        from gui.cross_section.section_shaded_surface import build_section_shaded_surface_actor
        mesh_mode = "shaded" if mode_idx == 1 else "surface"
        ok = build_section_shaded_surface_actor(app, view_idx, mesh_mode, force_independent=True)
    except Exception as exc:
        print(f"SECTION_MESH view={slot_idx} status=refresh_failed operation={operation} reason={exc}")
        return False

    if ok:
        print(f"SECTION_MESH view={slot_idx} status=refreshed mode={mesh_mode} operation={operation}")
    return bool(ok)
