
"""
Cross-Section Measurement Tool for NakshaAI
--------------------------------------------
Lets the user measure distances by clicking two points directly inside a
cross-section (side) view. Each finished segment:

  1. Is drawn permanently inside that cross-section view (line + label), and
  2. Is mirrored into the main 3D view as a small VALUE-ONLY label (no line —
     a cross-section measurement is a vertical/profile distance, and drawing
     its two endpoints as a full 3D line across a top-down main view is
     misleading), using the true world-space coordinates recovered from the
     cross-section's own local (along, across, elevation) frame.

Independence / safety notes:
  - This tool owns its own VTK observers and is only ever attached to the
    cross-section VTK widgets it is explicitly activated for. It is inert
    (no observers, no behavior change) until `activate()` is called.
  - It never touches `measurement_tools.MeasurementTool`'s interactive state
    (mode, measurement_points, undo/redo stacks, snap cache, etc.). The only
    thing it calls on that tool is the additive
    `MeasurementTool.add_cross_section_measurement()` helper, which appends a
    finished measurement using the same rendering building blocks the main
    tool already uses for its own finalized measurements.
  - It never touches `SectionController`'s drawing/locate logic. While a
    measurement is being taken in a given section view, this tool simply
    consumes (aborts) the click so the section-drawing observer does not
    also react to it in that view — the same precedence pattern the main
    measurement tool already uses against cross-section drawing elsewhere.

Performance notes (why the first version glitched/lagged):
  - Mouse-move no longer runs an expensive cell-pick every event; it only
    ever projects the cursor onto the pending point's local plane (cheap
    display->world math), and is time-throttled. Picking (which walks
    render-pass geometry) only happens twice per segment, at the two clicks.
  - The rubber-band preview reuses one actor/polydata per view and mutates
    its points in place instead of allocating a new mapper/actor every
    mouse-move — this was the main source of stutter.
  - The click-time picker is restricted (PickList) to that view's own
    point-cloud actor when it can be found, so it can never accidentally
    pick a grid label, a DXF/SNT overlay, or one of this tool's own
    previously drawn lines/labels — the main source of "measurement is
    incorrect" / stray lines in earlier testing.
"""

import time

import numpy as np
import vtk

from gui.measure_settings_dialog import load_measure_settings


class CrossSectionMeasurementTool:
    """Click-to-measure tool scoped to cross-section (side) views."""

    _MOVE_THROTTLE_SECONDS = 0.02  # ~50 fps cap on preview updates

    def __init__(self, app):
        self.app = app
        self.active = False
        self._section_observers = {}    # view_index -> {'press', 'move', 'widget'}
        self._pending_local_point = {}  # view_index -> (along, across, elev)
        self._preview = {}              # view_index -> {'actor','points','polydata'}
        self._segment_actors = {}       # view_index -> list of {'line', 'label'}
        self._last_move_time = {}       # view_index -> monotonic seconds
        self._picker = vtk.vtkCellPicker()
        self._picker.SetTolerance(0.01)
        # Same user-configured label style (unit, font size) the main-view
        # measurement tool uses, so cross-section labels match it exactly.
        self._measure_style = load_measure_settings()
        # LIFO history for Ctrl+Z/Ctrl+Y — each entry keeps enough to redraw
        # (view_index, local_p1, local_p2) plus the live handles to remove.
        self._undo_stack = []
        self._redo_stack = []
        print("✅ CrossSectionMeasurementTool initialized")

    # ------------------------------------------------------------------
    # Activation
    # ------------------------------------------------------------------
    def activate(self):
        """Activate measurement mode for all currently open cross-section views."""
        if self.active:
            return
        self.active = True
        section_vtks = getattr(self.app, "section_vtks", None) or {}
        for view_index, vtk_widget in section_vtks.items():
            self.activate_for_section(vtk_widget, view_index)
        print("📏 Cross-section measurement tool ACTIVATED")

    def deactivate(self):
        """Deactivate measurement mode for all cross-section views."""
        if not self.active:
            return
        self.active = False
        for view_index in list(self._section_observers.keys()):
            self.deactivate_for_section(view_index)
        print("📏 Cross-section measurement tool DEACTIVATED")

    def activate_for_section(self, section_vtk_widget, view_index):
        """Attach click/move observers to one cross-section view's interactor."""
        existing = self._section_observers.get(view_index)
        if existing is not None:
            if existing.get("widget") is section_vtk_widget:
                return  # already attached to this exact widget
            # Stale entry pointing at a closed/replaced dock's widget — tear it
            # down first so we never leak an observer onto a dead VTK widget.
            self.deactivate_for_section(view_index)

        try:
            interactor = section_vtk_widget.interactor
            # Priority 2.0 runs BEFORE SectionController's own observers (1.0),
            # matching how the main measurement tool already takes precedence
            # over cross-section drawing when it is active.
            press_tag = interactor.AddObserver(
                "LeftButtonPressEvent",
                lambda obj, evt: self._on_section_click(obj, evt, section_vtk_widget, view_index),
                2.0,
            )
            move_tag = interactor.AddObserver(
                "MouseMoveEvent",
                lambda obj, evt: self._on_section_move(obj, evt, section_vtk_widget, view_index),
                2.0,
            )
            # Right-click ends the current chain (see _on_section_click: a
            # "line" is just a chain of one segment, a "path" is a longer
            # chain — right-click is how the user says "stop here").
            right_tag = interactor.AddObserver(
                "RightButtonPressEvent",
                lambda obj, evt: self._on_section_right_click(obj, evt, section_vtk_widget, view_index),
                2.0,
            )
            self._section_observers[view_index] = {
                "press": press_tag,
                "move": move_tag,
                "right": right_tag,
                "widget": section_vtk_widget,
            }
            print(f"   ✅ Cross-section measurement observers attached (view {view_index + 1})")
        except Exception as e:
            print(f"⚠️ CrossSectionMeasurementTool: failed to attach observers for view {view_index + 1}: {e}")

    def deactivate_for_section(self, view_index):
        """Remove observers for one cross-section view (leaves finished segments in place)."""
        info = self._section_observers.pop(view_index, None)
        if info is None:
            return
        try:
            interactor = info["widget"].interactor
            interactor.RemoveObserver(info["press"])
            interactor.RemoveObserver(info["move"])
            if info.get("right") is not None:
                interactor.RemoveObserver(info["right"])
        except Exception:
            pass
        self._clear_preview(view_index, widget_hint=info.get("widget"))
        self._pending_local_point.pop(view_index, None)
        self._last_move_time.pop(view_index, None)

    def deactivate_all_sections(self):
        for view_index in list(self._section_observers.keys()):
            self.deactivate_for_section(view_index)

    def has_history(self):
        """True while there is a finalized segment left to undo or redo.

        Deactivating (Escape, or unchecking the footer toggle) does not
        clear this — a finalized measurement stays undo-able afterward, the
        same way the main-view tool's own finalized measurements do.
        """
        return bool(self._undo_stack or self._redo_stack)

    # ------------------------------------------------------------------
    # Picking helpers
    # ------------------------------------------------------------------
    def _section_point_actor(self, section_vtk_widget, view_index):
        """Return this view's own point-cloud actor, if it can be located."""
        try:
            actors = getattr(section_vtk_widget, "actors", None) or {}
            name = f"_section_{view_index}_unified"
            actor = actors.get(name)
            if actor is not None:
                return actor
        except Exception:
            pass
        return None

    def _pick_local_point(self, section_vtk_widget, view_index, display_x, display_y):
        """
        Pick a real 3D point (along, across, elev) on the section's point cloud.

        Restricted to that view's own point-cloud actor when it can be found,
        so the picker can never return a point on a grid label, a DXF/SNT
        overlay, or one of this tool's own line/label actors.
        """
        try:
            renderer = section_vtk_widget.renderer
            picker = self._picker
            target_actor = self._section_point_actor(section_vtk_widget, view_index)
            if target_actor is not None:
                picker.InitializePickList()
                picker.AddPickList(target_actor)
                picker.PickFromListOn()
            else:
                picker.PickFromListOff()
            if picker.Pick(display_x, display_y, 0, renderer):
                return tuple(float(v) for v in picker.GetPickPosition())
        except Exception:
            pass
        finally:
            try:
                self._picker.PickFromListOff()
            except Exception:
                pass
        return None

    def _project_to_local_plane(self, section_vtk_widget, display_x, display_y, across_lock=None):
        """
        Cheap cursor projection onto the section plane — used for the live preview.

        The section camera is orthographic and looks straight along the
        across axis, so `along` (screen X) and `elev` (screen Y) unproject
        linearly and reliably regardless of depth — that part must track the
        cursor exactly, or the preview visibly locks to a horizontal/vertical
        line instead of following the mouse. `across` (the view's depth axis)
        is the one coordinate that is NOT reliable from a display-only
        unprojection (it comes out as whatever depth VTK assumed, usually the
        near clip plane) — so it is pinned to the first click's across value
        instead of trusted from this projection, keeping the preview flat in
        the section plane and avoiding depth jitter ("glitchy"/pushing look).
        """
        try:
            renderer = section_vtk_widget.renderer
            coord = vtk.vtkCoordinate()
            coord.SetCoordinateSystemToDisplay()
            coord.SetValue(float(display_x), float(display_y), 0.0)
            local_pt = coord.GetComputedWorldValue(renderer)
            along, across, elev = float(local_pt[0]), float(local_pt[1]), float(local_pt[2])
            if across_lock is not None:
                across = float(across_lock)
            return (along, across, elev)
        except Exception:
            return None

    @staticmethod
    def _abort_event(obj):
        try:
            if hasattr(obj, "AbortFlagOn"):
                obj.AbortFlagOn()
            elif hasattr(obj, "SetAbortFlag"):
                obj.SetAbortFlag(1)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------
    def _on_section_click(self, obj, evt, section_vtk_widget, view_index):
        if not self.active:
            return

        # Yield to classification / cut-section placement if either is active
        # in this view — do not abort, let normal handling proceed untouched.
        if getattr(self.app, "active_classify_tool", None) is not None:
            return
        cut = getattr(self.app, "cut_section_controller", None)
        if cut is not None and getattr(cut, "_state", 0) in (1, 2):
            return

        x, y = obj.GetEventPosition()
        local_pt = self._pick_local_point(section_vtk_widget, view_index, x, y)
        if local_pt is None:
            print("⚠️ Cross-section measure: no geometry under cursor")
            return

        pending = self._pending_local_point.get(view_index)
        if pending is None:
            self._pending_local_point[view_index] = local_pt
            try:
                self.app.statusBar().showMessage(
                    "📏 Cross-section: click next point (right-click or Esc to finish)", 4000
                )
            except Exception:
                pass
        else:
            self._finalize_segment(view_index, section_vtk_widget, pending, local_pt)
            # A "line" measurement is just a chain that stops after one
            # segment; a "path" is a longer chain — so the just-picked point
            # becomes the start of the next segment instead of resetting.
            # Right-click (or Esc) ends the chain — see _on_section_right_click.
            self._pending_local_point[view_index] = local_pt
            self._clear_preview(view_index)

        self._abort_event(obj)

    def _on_section_right_click(self, obj, evt, section_vtk_widget, view_index):
        """Right-click ends the current measurement chain (no-op if nothing is pending)."""
        if not self.active:
            return
        pending = self._pending_local_point.pop(view_index, None)
        if pending is None:
            return  # nothing to end — let normal right-click handling (menus, etc.) proceed
        self._clear_preview(view_index)
        try:
            self.app.statusBar().showMessage("📏 Cross-section measurement finished", 2000)
        except Exception:
            pass
        self._abort_event(obj)

    def _on_section_move(self, obj, evt, section_vtk_widget, view_index):
        if not self.active:
            return
        pending = self._pending_local_point.get(view_index)
        if pending is None:
            return

        now = time.monotonic()
        if (now - self._last_move_time.get(view_index, 0.0)) < self._MOVE_THROTTLE_SECONDS:
            self._abort_event(obj)  # still ours to own — just skip the heavy work this frame
            return
        self._last_move_time[view_index] = now

        # Use the SAME precise pick the eventual click will use, not just a
        # cheap plane projection — otherwise the preview's last frame and the
        # committed point are computed two different ways and can end up at
        # slightly different (along, across, elev), so the line visibly
        # "jumps" the instant the measurement is finalized. The picker is
        # restricted to this view's own point-cloud actor and reused across
        # calls (VTK caches its locator on the actor), and this is already
        # throttled above, so it stays cheap enough for a live preview.
        x, y = obj.GetEventPosition()
        local_pt = self._pick_local_point(section_vtk_widget, view_index, x, y)
        if local_pt is None:
            # Cursor over empty space (no geometry under it) — fall back to a
            # flat plane projection so the preview still follows the cursor.
            local_pt = self._project_to_local_plane(section_vtk_widget, x, y, across_lock=pending[1])
        if local_pt is None:
            return

        self._update_preview(view_index, section_vtk_widget, pending, local_pt)
        self._abort_event(obj)

    # ------------------------------------------------------------------
    # World-space conversion (mirrors SectionController's own local frame:
    # X=along, Y=across, Z=elevation — see finalize_section()/_do_section_locate())
    # ------------------------------------------------------------------
    def _local_to_world(self, view_index, local_pt):
        P1 = getattr(self.app, f"section_{view_index}_P1", None)
        P2 = getattr(self.app, f"section_{view_index}_P2", None)
        if P1 is None or P2 is None:
            return None
        v = np.asarray(P2[:2], dtype=np.float64) - np.asarray(P1[:2], dtype=np.float64)
        length = float(np.linalg.norm(v))
        if length < 1e-9:
            return None
        dir_vec = v / length
        perp = np.array([-dir_vec[1], dir_vec[0]], dtype=np.float64)

        along, across, elev = local_pt
        world_xy = np.asarray(P1[:2], dtype=np.float64) + along * dir_vec + across * perp
        return (float(world_xy[0]), float(world_xy[1]), float(elev))

    # ------------------------------------------------------------------
    # Finalizing a segment
    # ------------------------------------------------------------------
    def _get_or_create_main_measurement_tool(self):
        tool = getattr(self.app, "measurement_tool", None)
        if tool is not None:
            return tool
        try:
            from gui.measurement_tools import MeasurementTool
            self.app.measurement_tool = MeasurementTool(self.app.digitizer)
            return self.app.measurement_tool
        except Exception as e:
            print(f"⚠️ Cross-section measure: could not create main measurement tool: {e}")
            return None

    def _finalize_segment(self, view_index, section_vtk_widget, local_p1, local_p2):
        record = self._create_segment_and_mirror(view_index, section_vtk_widget, local_p1, local_p2)
        if record is None:
            return
        self._undo_stack.append(record)
        self._redo_stack.clear()

    def _create_segment_and_mirror(self, view_index, section_vtk_widget, local_p1, local_p2):
        """Build one segment's visuals (section view + main-view mirror). Returns an undo record."""
        world_p1 = self._local_to_world(view_index, local_p1)
        world_p2 = self._local_to_world(view_index, local_p2)
        if world_p1 is None or world_p2 is None:
            print("⚠️ Cross-section measure: no section geometry (P1/P2) for this view")
            return None

        distance = float(np.linalg.norm(np.asarray(world_p2) - np.asarray(world_p1)))

        # Draw the finished segment inside the section view itself.
        segment = self._draw_section_segment(view_index, section_vtk_widget, local_p1, local_p2, distance)

        # Mirror into the main 3D view as a VALUE-ONLY label (no line): this is
        # a vertical/profile measurement, so a full line drawn across a
        # top-down main view would not represent it meaningfully.
        measurement_tool = self._get_or_create_main_measurement_tool()
        main_entry = None
        if measurement_tool is not None:
            try:
                main_entry = measurement_tool.add_cross_section_measurement(
                    [world_p1, world_p2], source_view=view_index, show_line=False
                )
            except Exception as e:
                print(f"⚠️ Cross-section measure: failed to mirror into main view: {e}")

        msg = f"📏 Cross-section distance: {distance:.2f} m"
        try:
            self.app.statusBar().showMessage(msg, 5000)
        except Exception:
            pass
        print(msg)

        return {"view_index": view_index, "segment": segment, "main_entry": main_entry}

    # ------------------------------------------------------------------
    # Undo / Redo (Ctrl+Z / Ctrl+Y)
    # ------------------------------------------------------------------
    def undo(self):
        """
        Step backward one point at a time, mirroring the main tool's Ctrl+Z:

          - If nothing has been finalized yet (still on the very first click
            of a fresh measurement, nothing drawn), Ctrl+Z simply cancels
            that pending point — there is nothing else to revert to.
          - Otherwise it removes the most recently finished segment (its
            section-view visuals + mirrored main-view label) and rewinds the
            pending point back to that segment's start, so pressing Ctrl+Z
            repeatedly walks all the way back through a chained path.
        """
        if not self._undo_stack:
            if self._pending_local_point:
                for view_index in list(self._pending_local_point.keys()):
                    self._pending_local_point.pop(view_index, None)
                    self._clear_preview(view_index)
                print("↶ Cross-section measure: cancelled pending (unfinished) point")
                return True
            print("⚠️ Cross-section measure: nothing to undo")
            return False

        record = self._undo_stack.pop()
        view_index = record["view_index"]
        self._remove_segment(view_index, record["segment"])

        measurement_tool = getattr(self.app, "measurement_tool", None)
        if measurement_tool is not None and record.get("main_entry") is not None:
            try:
                measurement_tool.remove_measurement_entry(record["main_entry"])
            except Exception as e:
                print(f"⚠️ Cross-section measure: failed to remove mirrored main-view entry: {e}")

        # Rewind the chain: the removed segment's start point becomes the
        # pending point again, so the view visually reverts to "before" that
        # segment was drawn, and the user can keep pressing Ctrl+Z to undo
        # further back through the same chain.
        self._pending_local_point[view_index] = record["segment"]["local_p1"]
        self._clear_preview(view_index)

        self._redo_stack.append(record)
        print(f"↶ Cross-section measure undo (undo stack: {len(self._undo_stack)})")
        return True

    def redo(self):
        """Re-add the most recently undone segment (does not disturb further redo history)."""
        if not self._redo_stack:
            print("⚠️ Cross-section measure: nothing to redo")
            return False

        record = self._redo_stack.pop()
        view_index = record["view_index"]
        widget = getattr(self.app, "section_vtks", {}).get(view_index)
        if widget is None:
            print("⚠️ Cross-section measure: view no longer open, cannot redo")
            return False

        old_segment = record["segment"]
        new_record = self._create_segment_and_mirror(view_index, widget, old_segment["local_p1"], old_segment["local_p2"])
        if new_record is not None:
            self._undo_stack.append(new_record)
            # Restore "chain continues from here" state, same as a fresh click.
            self._pending_local_point[view_index] = old_segment["local_p2"]
            self._clear_preview(view_index)
        print(f"↷ Cross-section measure redo (redo stack: {len(self._redo_stack)})")
        return True

    def _force_section_render(self, view_index, widget):
        """
        Force an immediate repaint of one section view.

        A plain `widget.render()` is not always enough to actually flush a
        frame for a background/inactive cross-section dock in this app —
        app_window.py already has a dedicated `_render_section_view()` for
        exactly this reason (its own docstring calls it "FIXED"). Without
        this, an undo/finalize in a view that isn't the one currently in
        focus can update the data correctly but not visibly repaint until
        some unrelated later action forces a redraw — which looks like
        "undo didn't work" followed by it suddenly "catching up" once you
        act in another view.
        """
        force_render = getattr(self.app, "_render_section_view", None)
        if callable(force_render):
            try:
                force_render(view_index)
                return
            except Exception:
                pass
        if widget is None:
            return
        try:
            widget.GetRenderWindow().Render()
        except Exception:
            try:
                widget.render()
            except Exception:
                pass

    @staticmethod
    def _remove_segment_actor(renderer, actor):
        """Remove one segment prop, using the 2D API for the boxed label (vtkActor2D)."""
        if actor is None:
            return
        try:
            if actor.IsA("vtkActor2D"):
                renderer.RemoveActor2D(actor)
            else:
                renderer.RemoveActor(actor)
        except Exception:
            pass

    def _remove_segment(self, view_index, segment):
        """Remove one segment's actors from its section view."""
        segments = self._segment_actors.get(view_index, [])
        if segment in segments:
            segments.remove(segment)

        widget = getattr(self.app, "section_vtks", {}).get(view_index)
        if widget is None:
            return
        for key in ("line", "label"):
            self._remove_segment_actor(widget.renderer, segment.get(key))
        self._force_section_render(view_index, widget)

    # ------------------------------------------------------------------
    # Section-view rendering (local coordinates == that renderer's own world space)
    # ------------------------------------------------------------------
    def _make_line_actor(self, p1, p2, color=(1.0, 0.55, 0.0), width=3):
        pts = vtk.vtkPoints()
        pts.SetDataTypeToDouble()
        pts.InsertNextPoint(float(p1[0]), float(p1[1]), float(p1[2]))
        pts.InsertNextPoint(float(p2[0]), float(p2[1]), float(p2[2]))

        line = vtk.vtkLine()
        line.GetPointIds().SetId(0, 0)
        line.GetPointIds().SetId(1, 1)

        cells = vtk.vtkCellArray()
        cells.InsertNextCell(line)

        polydata = vtk.vtkPolyData()
        polydata.SetPoints(pts)
        polydata.SetLines(cells)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(polydata)
        try:
            mapper.SetResolveCoincidentTopologyToPolygonOffset()
            mapper.SetResolveCoincidentTopologyPolygonOffsetParameters(-1e5, -1e5)
        except Exception:
            pass

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(float(color[0]), float(color[1]), float(color[2]))
        prop.SetLineWidth(float(width))
        prop.LightingOff()
        try:
            prop.SetDepthTestingEnabled(False)
        except Exception:
            pass
        actor.PickableOff()
        return actor, pts, polydata

    def _make_label_actor(self, position, distance):
        """
        Same boxed label style the main-view measurement tool uses for its
        own finalized distance labels (black box, white border, white bold
        text) — see MeasurementTool._create_distance_label() /
        _create_world_text_label(). Uses vtkTextActor anchored to a world
        position (a vtkActor2D), unlike the plain vtkBillboardTextActor3D
        used elsewhere in this file, so it must be added/removed via the
        renderer's *2D* API (AddActor2D/RemoveActor2D), not AddActor.
        """
        style = self._measure_style or {}
        sec = style.get('line', {})
        font_size = sec.get('label_font_size', 16)
        unit = sec.get('unit', 'm')
        text = f"{distance/1000.0:.3f} km" if unit == 'km' else f"{distance:.2f} m"

        text_actor = vtk.vtkTextActor()
        text_actor.SetInput(text)

        prop = text_actor.GetTextProperty()
        prop.SetColor(1.0, 1.0, 1.0)
        prop.SetFontSize(font_size)
        prop.BoldOn()
        prop.SetJustificationToCentered()
        prop.SetVerticalJustificationToCentered()
        prop.SetBackgroundColor(0.0, 0.0, 0.0)
        prop.SetBackgroundOpacity(0.75)
        prop.SetFrame(True)
        prop.SetFrameColor(1.0, 1.0, 1.0)
        prop.SetFrameWidth(2)

        coord = text_actor.GetActualPositionCoordinate()
        coord.SetCoordinateSystemToWorld()
        coord.SetValue(float(position[0]), float(position[1]), float(position[2]))

        text_actor.GetProperty().SetDisplayLocationToForeground()
        text_actor.PickableOff()
        return text_actor

    def _draw_section_segment(self, view_index, section_vtk_widget, local_p1, local_p2, distance):
        renderer = section_vtk_widget.renderer
        line_actor, _pts, _poly = self._make_line_actor(local_p1, local_p2)
        renderer.AddActor(line_actor)

        mid = tuple((a + b) / 2.0 for a, b in zip(local_p1, local_p2))
        label_actor = self._make_label_actor(mid, distance)
        renderer.AddActor2D(label_actor)

        segment = {
            "line": line_actor,
            "label": label_actor,
            "local_p1": local_p1,
            "local_p2": local_p2,
            "distance": distance,
        }
        self._segment_actors.setdefault(view_index, []).append(segment)
        self._force_section_render(view_index, section_vtk_widget)
        return segment

    def _update_preview(self, view_index, section_vtk_widget, local_p1, local_p2):
        """Reuse one preview actor per view, mutating its points in place."""
        entry = self._preview.get(view_index)
        if entry is None:
            actor, pts, polydata = self._make_line_actor(local_p1, local_p2, color=(0.0, 1.0, 1.0), width=2)
            section_vtk_widget.renderer.AddActor(actor)
            self._preview[view_index] = {"actor": actor, "points": pts, "polydata": polydata}
        else:
            pts = entry["points"]
            pts.SetPoint(0, float(local_p1[0]), float(local_p1[1]), float(local_p1[2]))
            pts.SetPoint(1, float(local_p2[0]), float(local_p2[1]), float(local_p2[2]))
            pts.Modified()
            entry["polydata"].Modified()

        distance = float(np.linalg.norm(np.asarray(local_p2) - np.asarray(local_p1)))
        try:
            self.app.statusBar().showMessage(f"📏 Distance: {distance:.2f} m", 100)
        except Exception:
            pass
        self._force_section_render(view_index, section_vtk_widget)

    def _clear_preview(self, view_index, widget_hint=None):
        entry = self._preview.pop(view_index, None)
        if entry is None:
            return
        widget = widget_hint
        if widget is None:
            info = self._section_observers.get(view_index)
            widget = info.get("widget") if info else None
        if widget is None:
            widget = getattr(self.app, "section_vtks", {}).get(view_index)
        if widget is not None:
            try:
                widget.renderer.RemoveActor(entry["actor"])
            except Exception:
                pass
            self._force_section_render(view_index, widget)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def clear_view(self, view_index):
        """Remove all cross-section measurement visuals for one view."""
        self._clear_preview(view_index)
        self._pending_local_point.pop(view_index, None)
        segments = self._segment_actors.pop(view_index, [])
        widget = getattr(self.app, "section_vtks", {}).get(view_index)
        if widget is None:
            return
        for seg in segments:
            for key in ("line", "label"):
                self._remove_segment_actor(widget.renderer, seg.get(key))
        self._force_section_render(view_index, widget)

    def clear_all(self):
        for view_index in list(self._segment_actors.keys()):
            self.clear_view(view_index)
