"""
ElementSelectTool — MicroStation-style element selection on top of
DigitizeManager + SelectionManager.

Pick methods:
  individual : click an element; hover preview before commit
  block      : drag a rectangle; enclosed elements
  shape      : click polygon vertices, right-click to close; enclosed
  cline      : click two points; elements crossed by that segment

Selection modes (taken from Qt keyboard modifiers each click):
  plain         -> NEW
  Ctrl          -> ADD
  Shift         -> SUBTRACT
  Ctrl+Shift    -> INVERT

The tool installs its own VTK observers and removes them on deactivate;
it never edits or replaces existing DigitizeManager observers. Activating
this tool calls DigitizeManager.set_tool(None) first so no draw tool is
listening to clicks at the same time.
"""

import time

import vtk
from PySide6.QtCore import QObject, QTimer

from gui.selection_manager import SelectionMode


class PickMethod:
    INDIVIDUAL = "individual"
    BLOCK = "block"
    SHAPE = "shape"
    CLINE = "cline"


class EncloseMode:
    """How Block/Shape decide whether a drawing is 'picked'.

    OVERLAP — any vertex inside the fence picks the element (MicroStation default,
              most forgiving — what users intuitively expect).
    INSIDE  — every vertex must be inside the fence (strict containment).
    """
    OVERLAP = "overlap"
    INSIDE = "inside"


class ElementSelectTool(QObject):
    """MicroStation-style element selection layer."""

    HOVER_COLOR = (1.0, 0.85, 0.2)   # warm yellow preview
    HOVER_WIDTH_BOOST = 1.5
    INTERSECT_HISTORY_RENDER_DELAY_MS = 20

    def __init__(self, app, digitizer):
        super().__init__()
        self.app = app
        self.digitizer = digitizer
        self.renderer = digitizer.renderer
        self.overlay_renderer = getattr(digitizer, "overlay_renderer", None) or self.renderer
        self.interactor = digitizer.interactor

        self._active = False
        self._method = PickMethod.INDIVIDUAL
        self._enclose_mode = EncloseMode.OVERLAP  # MicroStation default
        self._observer_ids = []
        self._key_press_observer_id = None
        self._char_observer_id = None

        # Hover state
        self._hover_drawing = None
        self._hover_saved = None  # (color_tuple, width_float)
        self._last_hover_pick_ts = 0.0
        self._last_hover_pick_pos = None
        self._hover_pick_interval_s = 1.0 / 30.0
        self._hover_pick_move_threshold_px = 2

        # Pick caches so hover doesn't rescan all drawings on every move.
        self._cached_pickable_drawings = []
        self._cached_pickable_ids = set()
        self._cached_pickable_stamp = None
        self._cached_actor_owner_map = {}

        # Block-drag state
        self._dragging_block = False
        self._block_start_screen = None
        self._block_overlay_actor = None

        # Shape-polygon state  — stored as WORLD coords so zoom/pan never distorts preview
        self._shape_pts_world = []   # list of (wx, wy, wz)
        self._shape_overlay_actor = None

        # Crossing-line state — stored as WORLD coords
        self._cline_first_pt_world = None   # (wx, wy, wz)
        self._cline_overlay_actor = None

        # Optional: delete-on-click mode (used by Phase 3 Delete Element tool)
        self._delete_on_click = False
        self._digitizer_was_enabled = None
        self._measurement_was_active = False
        self._suspended_pan_button = None

        # Interactive drag-move state
        self._move_mode = False          # True while Move is armed from the panel
        self._drag_active = False        # True while the user is holding LMB during a move
        self._drag_last_world = None     # (wx, wy, wz) — previous mouse world pos
        self._drag_undo_saved = False    # undo snapshot taken for this drag stroke

        # Intersect mode state (multi-figure redesign)
        # _intersect_mode: True when intersect is armed globally
        # _intersect_parts: list of dicts, each representing one splittable sub-segment:
        #   { 'coords': [...world coords...], 'actor': vtkActor2D or None,
        #     'source_drawing': drawing dict, 'deleted': False }
        # _intersect_selected_part: index into _intersect_parts that user left-clicked
        # _intersect_selected_actor: the highlight overlay for the selected part (neon red)
        self._intersect_mode = False
        self._intersect_parts = []           # all split sub-parts across all selected drawings
        self._intersect_part_actors = []     # parallel list of vtkActor overlays (or None)
        self._intersect_selected_part = None # index of left-clicked (chosen) part
        self._intersect_selected_actor = None  # neon-red highlight actor for chosen part
        self._intersect_hovered_part = None  # index of currently hovered part
        self._intersect_hidden_drawings = [] # source drawings hidden during intersect session
        self._intersect_visibility_states = [] # exact pre-intersect visibility for every hidden actor
        self._intersect_delete_history = []  # stack of part indices deleted this session (for Ctrl+Z)
        self._intersect_redo_history = []    # stack of part indices for redo (Ctrl+Y)
        self._intersect_history_render_pending = False
        self._intersect_history_camera_state = None
        self._intersect_history_render_timer = QTimer(self)
        self._intersect_history_render_timer.setSingleShot(True)
        self._intersect_history_render_timer.timeout.connect(
            self._flush_intersect_history_render
        )
        # Keep old single-drawing fields to avoid AttributeError in _cancel_in_progress_pick
        self._intersect_drawing = None
        self._intersect_seg_idx = None
        self._intersect_pt_world = None
        self._intersect_part_a_actor = None
        self._intersect_part_b_actor = None
        self._intersect_chosen_part = None
        self._intersect_chosen_coords = None
        self._intersect_part_a_coords = []
        self._intersect_part_b_coords = []

        # Copy mode state
        self._copy_mode = False
        self._copy_template = []      # list of {coords, source_drawing} dicts frozen at entry
        self._copy_centroid = None    # (cx, cy) world-space anchor
        self._copy_preview_actors = []

        # Reusable picker — created once, not on every mouse move
        self._world_picker = vtk.vtkWorldPointPicker()
        self._overlay_cell_picker = vtk.vtkCellPicker()
        self._overlay_cell_picker.SetTolerance(0.005)
        self._overlay_prop_picker = vtk.vtkPropPicker()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def selection(self):
        sm = getattr(self.digitizer, 'selection_manager', None)
        return sm

    def set_method(self, method):
        """Change pick method without deactivating."""
        if method not in (PickMethod.INDIVIDUAL, PickMethod.BLOCK,
                          PickMethod.SHAPE, PickMethod.CLINE):
            return
        self._cancel_in_progress_pick()
        self._method = method
        self._status(f"Element Select: {method}")

    def set_enclose_mode(self, mode):
        """Switch between OVERLAP (any vertex inside) and INSIDE (all vertices)."""
        if mode in (EncloseMode.OVERLAP, EncloseMode.INSIDE):
            self._enclose_mode = mode
            self._status(f"Fence: {mode}")

    def set_delete_on_click(self, enabled):
        """When True, an individual pick deletes the element immediately
        instead of toggling selection. Used by 'Delete Element' tool."""
        self._delete_on_click = bool(enabled)

    def enter_move_mode(self):
        """Arm the tool for interactive drag-move.

        The next left-press on a selected drawing starts a drag.  The drawing
        follows the cursor until left-release, then the move is committed.
        Escape cancels and restores the pre-drag position via undo.
        """
        if not self._active:
            return
        sm = self.digitizer._selection_manager
        if sm is None or sm.is_empty():
            self._status("Move: nothing selected — pick elements first.")
            return
        self._move_mode = True
        self._drag_active = False
        self._drag_last_world = None
        self._drag_undo_saved = False
        self._status("Move: click a selected element and drag to reposition. Esc to cancel.", timeout=0)
        try:
            from PySide6.QtCore import Qt
            self.app.vtk_widget.setCursor(Qt.SizeAllCursor)
        except Exception:
            pass

    def exit_move_mode(self, cancel=False):
        """Disarm move mode.  If cancel=True and a drag happened, undo it."""
        if not self._move_mode:
            return
        self._move_mode = False
        if cancel and self._drag_undo_saved:
            try:
                self.digitizer.undo()
            except Exception as e:
                print(f"⚠️ move cancel undo failed: {e}")
        self._drag_active = False
        self._drag_last_world = None
        self._drag_undo_saved = False
        self._status("Move complete." if not cancel else "Move cancelled.")
        try:
            from PySide6.QtCore import Qt
            self.app.vtk_widget.setCursor(Qt.CrossCursor)
        except Exception:
            pass

    def enter_copy_mode(self):
        """Arm interactive copy mode.

        A ghost preview of the selection follows the cursor. Left-click places
        a copy at the cursor position; the mode stays active so the user can
        click again to place more copies. Right-click or Esc exits.
        """
        if not self._active:
            return
        sm = self.digitizer._selection_manager
        if sm is None or sm.is_empty():
            self._status("Copy: nothing selected — pick elements first.")
            return
        targets = [d for d in sm.get() if not d.get("classified_fence", False)]
        if not targets:
            self._status("Copy: selection contains only classified fences.")
            return

        self._copy_template = []
        all_pts = []
        for d in targets:
            if hasattr(self.digitizer, "_get_drawing_coords"):
                coords = self.digitizer._get_drawing_coords(d)
            else:
                coords = d.get("coords") or []
            flat = [c for c in (coords or []) if c is not None]
            if flat:
                all_pts.extend(flat)
                self._copy_template.append({"coords": flat, "source_drawing": d})

        if not all_pts:
            self._status("Copy: no coordinates found in selection.")
            return

        cx = sum(c[0] for c in all_pts) / len(all_pts)
        cy = sum(c[1] for c in all_pts) / len(all_pts)
        self._copy_centroid = (cx, cy)
        self._copy_mode = True
        self._status(
            "Copy: move mouse to position, left-click to place, right-click or Esc to exit.",
            timeout=0,
        )
        try:
            from PySide6.QtCore import Qt
            self.app.vtk_widget.setCursor(Qt.CrossCursor)
        except Exception:
            pass

    def exit_copy_mode(self):
        """Disarm copy mode and remove the ghost preview."""
        if not self._copy_mode:
            return
        self._copy_mode = False
        for actor in self._copy_preview_actors:
            self._remove_intersect_actor(actor)
        self._copy_preview_actors = []
        self._copy_template = []
        self._copy_centroid = None
        self._render()
        self._status("Copy: done.")

    def _update_copy_preview(self, world_pt):
        """Rebuild the ghost preview actors at the current cursor position."""
        for actor in self._copy_preview_actors:
            self._remove_intersect_actor(actor)
        self._copy_preview_actors = []

        if not self._copy_template or self._copy_centroid is None:
            return

        cx, cy = self._copy_centroid
        dx = world_pt[0] - cx
        dy = world_pt[1] - cy

        for tmpl in self._copy_template:
            coords = tmpl["coords"]
            if len(coords) < 2:
                continue
            offset_coords = [
                (c[0] + dx, c[1] + dy, c[2] if len(c) > 2 else 0.0) for c in coords
            ]
            actor = self._make_world_polyline(offset_coords, color=(0.4, 0.85, 1.0), width=1.5)
            if actor is not None:
                self._add_intersect_actor(actor)
                self._copy_preview_actors.append(actor)

        self._render()

    def _place_copy_at(self, world_pt):
        """Place a copy of the template at *world_pt* then keep copy mode armed."""
        if self._copy_centroid is None or not self._copy_template:
            return

        originals = [
            tmpl["source_drawing"] for tmpl in self._copy_template
            if self._is_live_drawing(tmpl["source_drawing"])
        ]
        if not originals:
            self._status("Copy: original elements are no longer available.")
            self.exit_copy_mode()
            return

        cx, cy = self._copy_centroid
        dx = world_pt[0] - cx
        dy = world_pt[1] - cy

        sm = self.digitizer._selection_manager
        if sm is not None:
            try:
                sm.apply(originals, mode=SelectionMode.NEW)
            except Exception:
                pass

        try:
            self.digitizer.copy_selection(dx, dy)
        except Exception as e:
            print(f"⚠️ copy_selection failed: {e}")
            return

        # Re-select originals so the next left-click can place another copy.
        if sm is not None:
            try:
                sm.apply(originals, mode=SelectionMode.NEW)
            except Exception:
                pass

        self._status(
            "Copy placed — move and left-click for another, right-click to exit.",
            timeout=0,
        )

    def enter_intersect_mode(self):
        """Arm intersect mode for all selected figures.

        For every selected drawing, find all points where it is crossed by
        any other selected drawing, split it into sub-segments at those
        intersection points, create coloured overlay actors for every sub-
        segment, then wait for the user to:
          left-click  → choose a sub-segment (highlights neon red)
          right-click → confirm deletion of the chosen sub-segment
        The user can repeat select/delete until Intersect is deactivated.
        """
        if not self._active:
            return
        sm = self.digitizer._selection_manager
        if sm is None or sm.is_empty():
            self._status("Intersect: select elements first.")
            return
        selected = [d for d in sm.get()
                    if d.get("type", "") != "text"
                    and len(d.get("interpolated") if d.get("source") == "curve_tool"
                           and d.get("interpolated") is not None
                           else d.get("coords") or []) >= 2]
        if not selected:
            self._status("Intersect: selected elements have no segments.")
            return

        # Split all selected figures at their mutual intersections
        parts = self._split_all_at_intersections(selected)
        if not parts:
            self._status("Intersect: no intersections found between selected elements.")
            return

        # Remove any previous intersect overlays
        self._clear_intersect_overlays()

        # Strip selection highlight (cyan) from every selected drawing now.
        # Drawings that are NOT split stay visible during the session and must
        # show in their original color — not the selection cyan painted by
        # _highlight_line.  sm.clear() calls _unhighlight_safe on every drawing
        # which restores the original actor color.
        if sm is not None:
            sm.clear()

        self._intersect_mode = True
        self._intersect_parts = parts
        self._intersect_part_actors = [None] * len(parts)
        self._intersect_selected_part = None
        self._intersect_selected_actor = None
        self._intersect_hovered_part = None
        self._intersect_delete_history = []  # fresh deletion undo stack for this session
        self._intersect_redo_history = []    # fresh redo stack for this session

        # Hide the source drawings that have parts in the overlay and replace
        # them with overlay actors.  Also hide endpoint/vertex markers so
        # floating spheres don't appear over the split overlays.
        self._intersect_hidden_drawings = []
        self._intersect_visibility_states = []
        sources_with_parts = {id(p["source_drawing"]): p["source_drawing"] for p in parts}
        for src in sources_with_parts.values():
            self._hide_intersect_source_drawing(src)

        # Build overlay actors for every part using the source drawing's color
        for idx, part in enumerate(parts):
            actor = self._make_world_polyline(
                part["coords"],
                color=part.get("orig_color", (1.0, 1.0, 1.0)),
                width=part.get("orig_width", 2.0),
            )
            self._add_intersect_actor(actor)
            self._intersect_part_actors[idx] = actor

        self._render()
        self._status("Intersect Active — left-click a part to select, right-click to delete.", timeout=0)

        # Notify the popup to highlight the Intersect button
        self._notify_intersect_active(True)

    def exit_intersect_mode(self):
        """Commit all pending part deletions then disarm intersect mode."""
        self._cancel_intersect_history_render()
        any_deleted = any(p.get("deleted") for p in self._intersect_parts)

        # Always remove overlays and restore visibility FIRST so the undo
        # snapshot (taken inside _commit_intersect_deletions) captures a
        # clean scene — no ghost overlay actors, all originals visible.
        self._clear_intersect_overlays()
        self._restore_intersect_hidden_drawings()

        if any_deleted:
            try:
                self._commit_intersect_deletions()
            except Exception as e:
                print(f"⚠️ Intersect commit failed: {e}")

        # Hide vertex/endpoint markers on all remaining drawings so they
        # don't appear as floating spheres after intersect deactivation.
        for src in list(self._intersect_hidden_drawings):
            for key in ("start_marker", "end_marker"):
                m = src.get(key)
                if m is not None:
                    try:
                        m.VisibilityOff()
                    except Exception:
                        pass
            for m in src.get("vertex_markers") or []:
                if m is not None:
                    try:
                        m.VisibilityOff()
                    except Exception:
                        pass

        self._intersect_mode = False
        self._intersect_parts = []
        self._intersect_part_actors = []
        self._intersect_selected_part = None
        self._intersect_hovered_part = None
        self._intersect_hidden_drawings = []
        self._intersect_visibility_states = []
        self._intersect_delete_history = []
        self._intersect_redo_history = []
        # Clear legacy fields
        self._intersect_drawing = None
        self._intersect_seg_idx = None
        self._intersect_pt_world = None
        self._intersect_chosen_part = None
        self._intersect_chosen_coords = None
        self._intersect_part_a_coords = []
        self._intersect_part_b_coords = []
        self._render()
        self._notify_intersect_active(False)
        self._status("Intersect Deactivated")

    def _cancel_intersect_mode(self):
        """Discard all pending deletions and disarm intersect mode (no commit).

        Used by Ctrl+Z / Ctrl+Y so that undo/redo operates on the state that
        existed *before* intersect was entered, without first creating a new
        undo entry for partial part deletions.
        """
        self._cancel_intersect_history_render()
        self._clear_intersect_overlays()
        self._restore_intersect_hidden_drawings()
        # Do NOT call _commit_intersect_deletions — throw away all marked deletions
        self._intersect_mode = False
        self._intersect_parts = []
        self._intersect_part_actors = []
        self._intersect_selected_part = None
        self._intersect_hovered_part = None
        self._intersect_hidden_drawings = []
        self._intersect_visibility_states = []
        self._intersect_delete_history = []
        self._intersect_redo_history = []
        self._intersect_drawing = None
        self._intersect_seg_idx = None
        self._intersect_pt_world = None
        self._intersect_chosen_part = None
        self._intersect_chosen_coords = None
        self._intersect_part_a_coords = []
        self._intersect_part_b_coords = []
        self._render()
        self._notify_intersect_active(False)

    @staticmethod
    def _actor_visibility(actor):
        """Return an actor's visibility without assuming a specific VTK actor type."""
        if actor is None:
            return None
        try:
            return bool(actor.GetVisibility())
        except Exception:
            return None

    def _hide_intersect_source_drawing(self, src):
        """Hide one source while remembering every actor's exact prior visibility."""
        actors = []
        seen = set()

        def remember_and_hide(actor):
            if actor is None or id(actor) in seen:
                return
            seen.add(id(actor))
            visible = self._actor_visibility(actor)
            if visible is None:
                return
            actors.append((actor, visible))
            try:
                actor.SetVisibility(False)
            except Exception:
                try:
                    actor.VisibilityOff()
                except Exception:
                    pass

        remember_and_hide(src.get("actor"))
        for key in ("start_marker", "end_marker"):
            remember_and_hide(src.get(key))
        for marker in src.get("vertex_markers") or []:
            remember_and_hide(marker)
        arrows = src.get("arrow_actor")
        if arrows is not None:
            if not isinstance(arrows, (list, tuple)):
                arrows = [arrows]
            for arrow in arrows:
                remember_and_hide(arrow)

        self._intersect_hidden_drawings.append(src)
        self._intersect_visibility_states.append({"drawing": src, "actors": actors})

    def _restore_intersect_hidden_drawings(self):
        """Restore the exact visibility captured when intersect mode started."""
        states = getattr(self, "_intersect_visibility_states", [])
        if states:
            for state in states:
                for actor, was_visible in state.get("actors", []):
                    try:
                        actor.SetVisibility(bool(was_visible))
                    except Exception:
                        try:
                            actor.VisibilityOn() if was_visible else actor.VisibilityOff()
                        except Exception:
                            pass
            return

        # Compatibility fallback for a session created before visibility-state
        # tracking was initialized (for example during a development hot reload).
        for src in getattr(self, "_intersect_hidden_drawings", []):
            actor = src.get("actor")
            if actor is not None:
                try:
                    actor.VisibilityOn()
                except Exception:
                    pass

    def _commit_intersect_deletions(self):
        """Apply all part deletions to the underlying drawings.

        Called after overlays are removed and original visibility is restored,
        so the undo snapshot captures a clean scene.
        Groups surviving parts by source drawing, then for each source:
        - 0 survivors → remove drawing entirely
        - 1 survivor  → update drawing coords in-place
        - N survivors → first reuses drawing; extras become new drawings
        One undo snapshot is taken before any modifications.
        """
        seen_ids = set()
        sources_with_deletion = []
        for p in self._intersect_parts:
            src = p.get("source_drawing")
            if p.get("deleted") and src is not None and id(src) not in seen_ids:
                seen_ids.add(id(src))
                sources_with_deletion.append(src)

        if not sources_with_deletion:
            return

        # Save state NOW: overlays are already gone and originals are visible,
        # so this snapshot is a valid "before" state for undo to restore.
        try:
            self.digitizer._save_state()
        except Exception:
            pass

        for source in sources_with_deletion:
            if not self._is_live_drawing(source):
                continue
            surviving = [
                p["coords"] for p in self._intersect_parts
                if p.get("source_drawing") is source and not p.get("deleted")
            ]
            self._apply_split_to_drawing(source, surviving)

    def _notify_intersect_active(self, active):
        """Tell the SelectionModeDialog to update the Intersect button and label."""
        try:
            popup = getattr(self.app, "_element_selection_dialog", None)
            if popup is None:
                return
            btn = getattr(popup, "btn_intersect", None)
            if btn is not None:
                btn.setChecked(active)
            lbl = getattr(popup, "lbl_count", None)
            if lbl is not None:
                if active:
                    lbl.setText("Intersect Active")
                else:
                    try:
                        count = self.digitizer.selection_manager.count()
                    except Exception:
                        count = 0
                    lbl.setText(f"Selection: {count}")
        except Exception:
            pass

    def _clear_intersect_overlays(self):
        """Remove all intersect overlay actors from the renderer."""
        for actor in self._intersect_part_actors:
            self._remove_intersect_actor(actor)
        self._intersect_part_actors = []
        self._remove_intersect_actor(self._intersect_selected_actor)
        self._intersect_selected_actor = None
        # Also remove legacy two-part actors
        self._remove_overlay("_intersect_part_a_actor")
        self._remove_overlay("_intersect_part_b_actor")

    def _suspend_left_click_pan(self):
        """Reserve left click for every Element Selection pick method."""
        if self.app is None or self._suspended_pan_button is not None:
            return
        current = getattr(self.app, "panning_button", "scroll")
        if current != "left":
            return
        self._suspended_pan_button = current
        self.app.panning_button = "scroll"
        try:
            release_pan = getattr(self.app, "_handle_fast_main_pan_release", None)
            if callable(release_pan):
                release_pan()
        except Exception:
            pass
        try:
            style = self.interactor.GetInteractorStyle()
            if style is not None:
                style.OnMiddleButtonUp()
                style.OnLeftButtonUp()
        except Exception:
            pass
        print("Element Selection active: left-click pan suspended; middle-click pan remains available")

    def _restore_left_click_pan(self):
        """Restore the user's configured pan button after selection exits."""
        if self.app is None or self._suspended_pan_button is None:
            return
        self.app.panning_button = self._suspended_pan_button
        self._suspended_pan_button = None
        print("Element Selection inactive: left-click pan restored")

    def activate(self, method=PickMethod.INDIVIDUAL):
        """Begin element selection. Idempotent — safe to call repeatedly."""
        if self._active:
            self.set_method(method)
            return

        # Always reset this at the start of a fresh activation so a previous
        # stale value can never incorrectly disable the digitizer on deactivate.
        self._digitizer_was_enabled = None

        # Stand down conflicting tools so their observers don't compete.
        try:
            self.digitizer.set_tool(None)
            self._digitizer_was_enabled = bool(getattr(self.digitizer, "enabled", True))
            self.digitizer.enabled = False
        except Exception as e:
            print(f"⚠️ Could not disable digitizer: {e}")

        # Measurement tool — remove its VTK observers completely so they don't
        # compete with element-selection observers (both run at priority 2.0).
        # Track whether it was active so we can restore it on deactivate.
        self._measurement_was_active = False
        mt = getattr(self.app, "measurement_tool", None)
        if mt is not None:
            try:
                self._measurement_was_active = bool(getattr(mt, "active", False))
                # Pause the measurement tool without clearing in-progress or
                # completed measurement geometry. We restore observers on exit.
                if hasattr(mt, "pause_without_clearing"):
                    mt.pause_without_clearing()
                elif hasattr(mt, "deactivate_completely"):
                    mt.deactivate_completely()
                else:
                    mt.deactivate()
            except Exception:
                pass

        # Measurement pause may restore the user's left-pan preference. Claim
        # left click only after conflicting tools have fully stood down.
        self._suspend_left_click_pan()
        srt = getattr(self.app, "select_rectangle_tool", None)
        if srt is not None:
            for attr in ("deactivate", "cancel", "stop"):
                fn = getattr(srt, attr, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
                    break
        ct = getattr(self.app, "curve_tool", None)
        if ct is not None:
            try:
                if getattr(ct, "active", False):
                    ct.deactivate()
                if getattr(ct, "_select_mode", False) and hasattr(ct, "deactivate_select_mode"):
                    ct.deactivate_select_mode()
            except Exception:
                pass
        
        # Temporarily disable grid label system to prevent event conflicts
        grid_mgr = getattr(self.app, "grid_label_manager", None)
        if grid_mgr is not None:
            try:
                if hasattr(grid_mgr, "remove_interactor_observers"):
                    grid_mgr.remove_interactor_observers()
                grid_mgr._element_select_active = True
                print("✅ Grid label system temporarily disabled during element selection")
            except Exception as e:
                print(f"⚠️ Could not disable grid label system: {e}")

        self._method = method
        self._reset_pick_caches()
        self._install_observers()
        self._active = True

        try:
            self.app.vtk_widget.setCursor(self._cursor_for_method())
        except Exception:
            pass

        # ✅ Show usage instructions
        if method == PickMethod.BLOCK:
            self._status(f"Element Select: {method} — Drag rectangle, RIGHT-CLICK to finalize (Overlap mode)")
        else:
            self._status(f"Element Select active — {method} (Overlap mode)")

    def deactivate(self):
        if not self._active:
            self._restore_left_click_pan()
            return
        # A normal tool switch means "finish Intersect", just like pressing the
        # Intersect button a second time.  Escape remains the explicit cancel path.
        # Committing here prevents deleted parts from being resurrected when a
        # drawing tool (Polyline, SmartLine, etc.) takes over.
        if self._intersect_mode:
            self.exit_intersect_mode()
        self._cancel_intersect_history_render()
        self._cancel_in_progress_pick()
        self._clear_hover()
        self._reset_hover_pick_state()
        self._reset_pick_caches()
        self._uninstall_observers()
        self._active = False
        self._restore_left_click_pan()

        # Re-enable digitizer to the state it was in before activation
        try:
            self.digitizer.enabled = self._digitizer_was_enabled if self._digitizer_was_enabled is not None else True
            print("✅ Digitizer re-enabled after element selection")
        except Exception as e:
            print(f"⚠️ Could not re-enable digitizer: {e}")
        finally:
            self._digitizer_was_enabled = None

        # Re-enable grid label system — always clear the flag even if restore fails
        grid_mgr = getattr(self.app, "grid_label_manager", None)
        if grid_mgr is not None:
            try:
                grid_mgr._element_select_active = False
                if hasattr(grid_mgr, "ensure_interactor_observers"):
                    grid_mgr.ensure_interactor_observers()
                    print("✅ Grid label system re-enabled after element selection")
            except Exception as e:
                # Flag is already cleared above; log but don't crash
                print(f"⚠️ Could not re-enable grid label system: {e}")

        # Restore measurement tool observers if it was active before we took over.
        # We called deactivate_completely() on activate, so we must re-install
        # its observers — but NOT call activate() which would wipe measurement state.
        mt = getattr(self.app, "measurement_tool", None)
        if mt is not None and self._measurement_was_active:
            try:
                if hasattr(mt, "reinstall_observers"):
                    mt.reinstall_observers()
                # else: measurement tool is old version without reinstall_observers,
                # leave it as-is — user can re-click the measure button if needed
            except Exception as e:
                print(f"⚠️ Could not restore measurement tool observers: {e}")
        self._measurement_was_active = False
        
        try:
            from PySide6.QtCore import Qt
            self.app.vtk_widget.setCursor(Qt.ArrowCursor)
        except Exception:
            pass
        self._status("Element Select deactivated")

    def is_active(self):
        return self._active

    # ------------------------------------------------------------------
    # Observer install / remove
    # ------------------------------------------------------------------
    def _install_observers(self):
        self._uninstall_observers()
        i = self.interactor
        # Priority 2.0 so we run before the digitizer's priority 1.0 right-press
        left_press_id = i.AddObserver("LeftButtonPressEvent", self._on_left_press, 2.0)
        left_release_id = i.AddObserver("LeftButtonReleaseEvent", self._on_left_release, 2.0)
        mouse_move_id = i.AddObserver("MouseMoveEvent", self._on_mouse_move, 2.0)
        right_press_id = i.AddObserver("RightButtonPressEvent", self._on_right_press, 2.0)
        self._key_press_observer_id = i.AddObserver(
            "KeyPressEvent", self._on_key_press, 2.0
        )
        self._char_observer_id = i.AddObserver("CharEvent", self._on_char, 2.0)
        self._observer_ids = [
            left_press_id,
            left_release_id,
            mouse_move_id,
            right_press_id,
            self._key_press_observer_id,
            self._char_observer_id,
        ]

    def _uninstall_observers(self):
        for oid in self._observer_ids:
            try:
                self.interactor.RemoveObserver(oid)
            except Exception:
                pass
        self._observer_ids = []
        self._key_press_observer_id = None
        self._char_observer_id = None

    def _abort_owned_observer(self, observer_id):
        """Abort lower-priority VTK observers for one owned callback."""
        if observer_id is None:
            return False
        try:
            command = self.interactor.GetCommand(observer_id)
            if command is None:
                return False
            command.AbortFlagOn()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    def _on_left_press(self, obj, evt):
        if not self._active:
            return
        x, y = self.interactor.GetEventPosition()

        # ── Intersect mode: left-click selects a bisected part ────────
        if self._intersect_mode:
            self._intersect_pick_part(x, y)
            try:
                if obj is not None:
                    obj.AbortFlagOn()
            except Exception:
                pass
            return
        # ─────────────────────────────────────────────────────────────

        # ── Copy mode: left-click places a copy at the cursor ────────
        if self._copy_mode:
            world_pt = self._screen_to_world(x, y)
            self._place_copy_at(world_pt)
            try:
                if obj is not None:
                    obj.AbortFlagOn()
            except Exception:
                pass
            return
        # ─────────────────────────────────────────────────────────────

        # ── Interactive drag-move ─────────────────────────────────────
        if self._move_mode:
            world_pt = self._screen_to_world(x, y)
            sm = self.digitizer._selection_manager
            # Start drag on any left-press while move mode is armed,
            # as long as there is at least one selected drawing.
            # We do not require a pick hit — the user already selected
            # the elements, and requiring a pixel-exact hit on a just-
            # created copy (before a render cycle) causes silent failures.
            if sm is not None and not sm.is_empty():
                if not self._drag_undo_saved:
                    try:
                        self.digitizer._save_state()
                    except Exception as e:
                        print(f"⚠️ move drag: _save_state failed: {e}")
                    self._drag_undo_saved = True
                self._drag_active = True
                self._drag_last_world = world_pt
            # If nothing is selected, ignore — right-click still commits.
            try:
                if obj is not None:
                    obj.AbortFlagOn()
            except Exception:
                pass
            return
        # ─────────────────────────────────────────────────────────────

        if self._method == PickMethod.INDIVIDUAL:
            self._handle_individual_pick(x, y)
        elif self._method == PickMethod.BLOCK:
            self._dragging_block = True
            self._block_start_screen = (x, y)
        elif self._method == PickMethod.SHAPE:
            # Store world coordinate so preview stays correct after zoom/pan
            self._shape_pts_world.append(self._screen_to_world(x, y))
            self._refresh_shape_overlay(cursor_screen=(x, y))
        elif self._method == PickMethod.CLINE:
            if self._cline_first_pt_world is None:
                self._cline_first_pt_world = self._screen_to_world(x, y)
                self._refresh_cline_overlay(cursor_screen=(x, y))
            else:
                self._commit_cline(self._cline_first_pt_world, self._screen_to_world(x, y))
                self._cline_first_pt_world = None
                self._remove_overlay("_cline_overlay_actor")
        # Stop lower-priority observers from also reacting.
        # Guard against VTK teardown — obj may be invalid during shutdown.
        try:
            if obj is not None:
                obj.AbortFlagOn()
        except Exception:
            pass

    def _on_left_release(self, obj, evt):
        if not self._active:
            return

        # ── Interactive drag-move release ─────────────────────────────
        if self._move_mode and self._drag_active:
            self._drag_active = False
            self._drag_last_world = None
            # Stay in move mode so the user can drag again or click elsewhere
            # to commit and exit. A re-press on empty space exits (see _on_left_press).
            try:
                if obj is not None:
                    obj.AbortFlagOn()
            except Exception:
                pass
            return
        # ─────────────────────────────────────────────────────────────

        if self._method == PickMethod.BLOCK and self._dragging_block:
            x, y = self.interactor.GetEventPosition()
            self._dragging_block = False
            self._remove_overlay("_block_overlay_actor")
            if self._block_start_screen is not None:
                self._commit_block(self._block_start_screen, (x, y))
            self._block_start_screen = None
            try:
                obj.AbortFlagOn()
            except Exception:
                pass

    def _on_mouse_move(self, obj, evt):
        if not self._active:
            return
        try:
            style = self.interactor.GetInteractorStyle() if hasattr(self.interactor, "GetInteractorStyle") else None
            if style is not None and hasattr(style, "GetState") and style.GetState() != 0:
                return
        except Exception:
            pass
        x, y = self.interactor.GetEventPosition()

        # ── Intersect mode: highlight whichever half the cursor is over ──
        if self._intersect_mode:
            self._intersect_hover(x, y)
            return
        # ─────────────────────────────────────────────────────────────

        # ── Copy mode: ghost preview follows cursor ───────────────────
        if self._copy_mode:
            world_pt = self._screen_to_world(x, y)
            self._update_copy_preview(world_pt)
            return
        # ─────────────────────────────────────────────────────────────

        # ── Interactive drag-move ─────────────────────────────────────
        if self._move_mode and self._drag_active and self._drag_last_world is not None:
            current_world = self._screen_to_world(x, y)
            dx = current_world[0] - self._drag_last_world[0]
            dy = current_world[1] - self._drag_last_world[1]
            if abs(dx) > 1e-10 or abs(dy) > 1e-10:
                try:
                    self.digitizer.move_selection(dx, dy, save_undo=False)
                except Exception as e:
                    print(f"⚠️ drag move failed: {e}")
                self._drag_last_world = current_world
            return
        # ─────────────────────────────────────────────────────────────

        if self._method == PickMethod.INDIVIDUAL:
            self._update_hover(x, y)
        elif self._method == PickMethod.BLOCK and self._dragging_block:
            # ✅ Show visual feedback during block drag
            self._refresh_block_overlay(self._block_start_screen, (x, y))
        elif self._method == PickMethod.SHAPE and self._shape_pts_world:
            self._refresh_shape_overlay(cursor_screen=(x, y))
        elif self._method == PickMethod.CLINE and self._cline_first_pt_world is not None:
            self._refresh_cline_overlay(cursor_screen=(x, y))

    def _on_right_press(self, obj, evt):
        if not self._active:
            return
        handled = False

        # COPY MODE — right-click exits.
        if self._copy_mode:
            self.exit_copy_mode()
            try:
                if obj is not None:
                    obj.AbortFlagOn()
            except Exception:
                pass
            return True

        # INTERSECT — right-click confirms deletion of the chosen part.
        if self._intersect_mode:
            self._intersect_confirm_delete()
            try:
                if obj is not None:
                    obj.AbortFlagOn()
            except Exception:
                pass
            return True

        # MOVE — right-click commits and exits move mode.
        if self._move_mode:
            self.exit_move_mode(cancel=False)
            try:
                if obj is not None:
                    obj.AbortFlagOn()
            except Exception:
                pass
            return True

        # BLOCK — right-click finalizes the drag (MicroStation behavior).
        if self._method == PickMethod.BLOCK and self._dragging_block:
            x, y = self.interactor.GetEventPosition()
            self._dragging_block = False
            self._remove_overlay("_block_overlay_actor")
            if self._block_start_screen is not None:
                self._commit_block(self._block_start_screen, (x, y))
            self._block_start_screen = None
            handled = True

        # SHAPE — right-click closes the polygon (needs >= 3 vertices).
        elif self._method == PickMethod.SHAPE and len(self._shape_pts_world) >= 3:
            pts = list(self._shape_pts_world)
            self._shape_pts_world = []
            self._remove_overlay("_shape_overlay_actor")
            self._commit_shape(pts)
            handled = True

        # CLINE — right-click cancels in-progress crossing line.
        elif self._method == PickMethod.CLINE and self._cline_first_pt_world is not None:
            self._cline_first_pt_world = None
            self._remove_overlay("_cline_overlay_actor")
            handled = True

        # Always swallow the right-press while the tool is active so the
        # digitizer's right-press selection logic doesn't fire underneath us.
        # Guard against VTK teardown.
        try:
            if obj is not None:
                obj.AbortFlagOn()
        except Exception:
            pass
        return handled

    def _on_key_press(self, obj, evt):
        if not self._active:
            return
        try:
            key = self.interactor.GetKeySym() or ""
            ctrl = bool(self.interactor.GetControlKey())
            shift = bool(self.interactor.GetShiftKey())
        except Exception:
            key = ""
            ctrl = False
            shift = False

        if key in ("Escape", "escape"):
            if self._intersect_mode:
                self._cancel_intersect_mode()
                self._status("Intersect: cancelled.")
            elif self._move_mode:
                self.exit_move_mode(cancel=True)
            elif self._copy_mode:
                self.exit_copy_mode()
            else:
                # Fully deactivate the element select tool AND the select
                # rectangle tool / pick-method dialog.  Plain self.deactivate()
                # re-enables the digitizer, which re-arms the Identify-tab
                # Select tool immediately.  Use the app-window teardown to
                # prevent that re-arm.
                try:
                    self.app._deactivate_selection_tools("Escape")
                    self.app._deactivate_identify_tab_tools()
                except Exception:
                    # Fallback: at least deactivate our own tool
                    self.deactivate()
                self._status("Element Select deactivated")
            return

        if ctrl and not shift and key in ("z", "Z"):
            if self._intersect_mode:
                # Inside intersect: undo the last in-session part deletion.
                # Do NOT exit intersect or touch the digitizer undo stack.
                self._intersect_undo_last_delete()
            else:
                try:
                    self.digitizer.undo()
                except Exception as e:
                    print(f"Element Select undo failed: {e}")
            self._abort_owned_observer(self._key_press_observer_id)
            return

        if (ctrl and key in ("y", "Y")) or (ctrl and shift and key in ("z", "Z")):
            if self._intersect_mode:
                # Inside intersect: redo the most recently undone part deletion.
                self._intersect_redo_last_delete()
            else:
                try:
                    self.digitizer.redo()
                except Exception as e:
                    print(f"Element Select redo failed: {e}")
            self._abort_owned_observer(self._key_press_observer_id)
            return

        if key in ("Delete", "delete", "BackSpace"):
            # Refuses classified by default — matches MicroStation Delete behavior.
            try:
                self.digitizer.delete_selection(include_classified=False)
            except Exception as e:
                print(f"⚠️ Delete shortcut failed: {e}")
            return

        if ctrl and key in ("a", "A"):
            try:
                self.selection.select_all(
                    predicate=lambda d: not d.get("classified_fence", False)
                )
                self._status(f"Selected {self.selection.count()} element(s)")
            except Exception as e:
                print(f"⚠️ Ctrl+A failed: {e}")
            return

        if ctrl and key in ("d", "D"):
            try:
                self.selection.clear()
                self._status("Deselected all")
            except Exception as e:
                print(f"⚠️ Ctrl+D failed: {e}")
            return

        if ctrl and key in ("i", "I"):
            try:
                self.selection.invert_all()
                self._status(f"Inverted — {self.selection.count()} selected")
            except Exception as e:
                print(f"⚠️ Ctrl+I failed: {e}")
            return

    def _on_char(self, obj, evt):
        """Block VTK character handling after intersect history shortcuts."""
        if not self._active or not self._intersect_mode:
            return
        try:
            key = self.interactor.GetKeySym() or ""
            ctrl = bool(self.interactor.GetControlKey())
        except Exception:
            return
        if ctrl and key in ("z", "Z", "y", "Y"):
            self._abort_owned_observer(self._char_observer_id)

    # ------------------------------------------------------------------
    # Individual pick + hover preview
    # ------------------------------------------------------------------
    def _handle_individual_pick(self, x, y):
        drawing = self._pick_drawing_at(
            x, y, allow_fallback=True, use_main_renderer_pick=True
        )
        picked_measurement = self._pick_measurement_segment_at(x, y)
        if picked_measurement is not None:
            self._set_selected_measurement_refs([picked_measurement])
        if drawing is None:
            # Click on empty space:
            # - NEW mode  -> clear selection (MicroStation behavior)
            # - other     -> no-op
            mode = self._current_mode()
            if mode == SelectionMode.NEW:
                self.selection.clear()
            return

        if self._delete_on_click:
            # Delete Element tool path — single-shot delete with undo snapshot.
            self._delete_drawing_with_undo(drawing)
            return

        mode = self._current_mode()
        # If hover currently shows this drawing, drop the hover paint first
        # so the SelectionManager's highlight goes on a clean actor.
        self._clear_hover()
        self.selection.apply([drawing], mode=mode)

    def _update_hover(self, x, y):
        if not self._should_refresh_hover_pick(x, y):
            return
        drawing = self._pick_drawing_at(
            x, y, allow_fallback=True, use_main_renderer_pick=False
        )
        if drawing is self._hover_drawing:
            return
        self._clear_hover()
        if drawing is None:
            return
        # Don't hover-paint over already-selected items (their cyan beats yellow)
        sm = self.selection
        if sm is not None and sm.contains(drawing):
            return
        actor = drawing.get("actor")
        if actor is None or not hasattr(actor, "GetProperty"):
            return
        # Guard: drawing may have been deleted between pick and paint
        if not self._is_live_drawing(drawing):
            return
        try:
            prop = actor.GetProperty()
            saved_color = tuple(prop.GetColor())
            saved_width = prop.GetLineWidth() if hasattr(prop, "GetLineWidth") else 2.0
            self._hover_saved = (saved_color, saved_width)
            self._hover_drawing = drawing
            prop.SetColor(*self.HOVER_COLOR)
            if hasattr(prop, "SetLineWidth"):
                prop.SetLineWidth(max(2.0, saved_width * self.HOVER_WIDTH_BOOST))
            actor.Modified()
            self._render()
        except Exception as e:
            # Reset hover state so we don't hold a stale reference
            self._hover_drawing = None
            self._hover_saved = None

    def _clear_hover(self):
        if self._hover_drawing is None or self._hover_saved is None:
            self._hover_drawing = None
            self._hover_saved = None
            return
        d = self._hover_drawing
        saved_color, saved_width = self._hover_saved
        # Always clear state first so any exception below doesn't leave stale refs
        self._hover_drawing = None
        self._hover_saved = None
        # Guard: drawing may have been deleted since hover was set
        if not self._is_live_drawing(d):
            return
        actor = d.get("actor")
        if actor is None or not hasattr(actor, "GetProperty"):
            return
        try:
            # If the SelectionManager has since selected this drawing, leave
            # cyan in place — its highlight took ownership.
            sm = self.selection
            if sm is not None and sm.contains(d):
                return
            prop = actor.GetProperty()
            prop.SetColor(*saved_color)
            if hasattr(prop, "SetLineWidth"):
                prop.SetLineWidth(saved_width)
            actor.Modified()
            self._render()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Block (rectangle) commit
    # ------------------------------------------------------------------
    def _commit_block(self, p0, p1):
        x0, y0 = p0
        x1, y1 = p1
        rx_min, rx_max = sorted((x0, x1))
        ry_min, ry_max = sorted((y0, y1))
        # Tiny rect -> treat as a click on (x1,y1)
        if abs(rx_max - rx_min) < 3 and abs(ry_max - ry_min) < 3:
            self._handle_individual_pick(x1, y1)
            return

        picked = []
        for d in list(self._iter_pickable_drawings()):
            if self._drawing_enclosed_in_screen_rect(d, rx_min, ry_min, rx_max, ry_max):
                picked.append(d)
        picked = [d for d in picked if self._is_live_drawing(d)]
        measure_refs = []
        for m_idx, s_idx, _m, label_data in self._iter_measurement_segments():
            if self._measurement_segment_selected_by_rect(label_data, rx_min, ry_min, rx_max, ry_max):
                measure_refs.append({"measurement_index": m_idx, "segment_index": s_idx})
        if self._delete_on_click and picked:
            self._delete_many_with_undo(picked)
            return
        mode = self._current_mode()
        self.selection.apply(picked, mode=mode)
        self._set_selected_measurement_refs(measure_refs)
        self._report_commit("Block", picked, mode)

    # ------------------------------------------------------------------
    # Shape (polygon) commit  — receives world-coord vertices
    # ------------------------------------------------------------------
    def _commit_shape(self, pts_world):
        if len(pts_world) < 3:
            return
        # Convert world → screen at commit time (camera is stable at this point)
        pts_screen = [sp for sp in (self._world_to_screen(w[0], w[1], w[2]) for w in pts_world) if sp]
        if len(pts_screen) < 3:
            return
        picked = []
        for d in list(self._iter_pickable_drawings()):
            if self._drawing_inside_screen_polygon(d, pts_screen):
                picked.append(d)
        picked = [d for d in picked if self._is_live_drawing(d)]
        measure_refs = []
        for m_idx, s_idx, _m, label_data in self._iter_measurement_segments():
            if self._drawing_inside_screen_polygon({"coords": [label_data.get("p1"), label_data.get("p2")]}, pts_screen):
                measure_refs.append({"measurement_index": m_idx, "segment_index": s_idx})
        if self._delete_on_click and picked:
            self._delete_many_with_undo(picked)
            return
        mode = self._current_mode()
        self.selection.apply(picked, mode=mode)
        self._set_selected_measurement_refs(measure_refs)
        self._report_commit("Shape", picked, mode)

    # ------------------------------------------------------------------
    # Crossing-line commit  — receives world-coord endpoints
    # ------------------------------------------------------------------
    def _commit_cline(self, world_p0, world_p1):
        # Convert world → screen at commit time
        p0 = self._world_to_screen(world_p0[0], world_p0[1], world_p0[2])
        p1 = self._world_to_screen(world_p1[0], world_p1[1], world_p1[2])
        if not p0 or not p1:
            return
        picked = []
        for d in list(self._iter_pickable_drawings()):
            if self._drawing_crossed_by_screen_segment(d, p0, p1):
                picked.append(d)
        picked = [d for d in picked if self._is_live_drawing(d)]
        measure_refs = []
        for m_idx, s_idx, _m, label_data in self._iter_measurement_segments():
            if self._measurement_segment_crossed_by_segment(label_data, p0, p1):
                measure_refs.append({"measurement_index": m_idx, "segment_index": s_idx})
        if self._delete_on_click and picked:
            self._delete_many_with_undo(picked)
            return
        mode = self._current_mode()
        self.selection.apply(picked, mode=mode)
        self._set_selected_measurement_refs(measure_refs)
        self._report_commit("Line", picked, mode)

    def _report_commit(self, method_label, picked, mode):
        n = len(picked)
        total = self.selection.count()
        if n == 0:
            self._status(f"{method_label}: nothing matched (try Overlap mode if Inside)")
        else:
            verb = {"new": "Selected", "add": "Added", "subtract": "Removed",
                    "invert": "Toggled"}.get(mode, "Picked")
            self._status(f"{method_label}: {verb} {n} — total {total} selected")

    def _iter_measurement_segments(self):
        mt = getattr(self.app, "measurement_tool", None)
        if mt is None:
            return []
        segments = []
        for m_idx, measurement in enumerate(getattr(mt, "measurements", []) or []):
            for s_idx, label_data in enumerate(measurement.get("labels", []) or []):
                line = label_data.get("line")
                if line is None:
                    continue
                segments.append((m_idx, s_idx, measurement, label_data))
        return segments

    def _measurement_segment_selected_by_rect(self, label_data, rx_min, ry_min, rx_max, ry_max):
        p1 = self._world_to_screen(*label_data.get("p1", (0, 0, 0)))
        p2 = self._world_to_screen(*label_data.get("p2", (0, 0, 0)))
        if not p1 or not p2:
            return False
        return (
            (rx_min <= p1[0] <= rx_max and ry_min <= p1[1] <= ry_max)
            or (rx_min <= p2[0] <= rx_max and ry_min <= p2[1] <= ry_max)
        )

    def _measurement_segment_crossed_by_segment(self, label_data, q0, q1):
        p1 = self._world_to_screen(*label_data.get("p1", (0, 0, 0)))
        p2 = self._world_to_screen(*label_data.get("p2", (0, 0, 0)))
        if not p1 or not p2:
            return False
        return self._segments_intersect_2d(p1, p2, q0, q1)

    @staticmethod
    def _segments_intersect_2d(a, b, c, d):
        def orient(p, q, r):
            return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

        def on_seg(p, q, r):
            return (
                min(p[0], r[0]) <= q[0] <= max(p[0], r[0])
                and min(p[1], r[1]) <= q[1] <= max(p[1], r[1])
            )

        o1 = orient(a, b, c)
        o2 = orient(a, b, d)
        o3 = orient(c, d, a)
        o4 = orient(c, d, b)
        if o1 == 0 and on_seg(a, c, b):
            return True
        if o2 == 0 and on_seg(a, d, b):
            return True
        if o3 == 0 and on_seg(c, a, d):
            return True
        if o4 == 0 and on_seg(c, b, d):
            return True
        return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)

    def _set_selected_measurement_refs(self, refs):
        mt = getattr(self.app, "measurement_tool", None)
        if mt is None:
            return
        try:
            mt._selected_measurement_refs = list(refs or [])
        except Exception:
            pass
        popup = getattr(self.app, "_element_selection_dialog", None)
        if popup is not None and hasattr(popup, "_refresh_measurement_total"):
            try:
                popup._refresh_measurement_total()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Picking helpers
    # ------------------------------------------------------------------
    def _pick_drawing_at(self, x, y, allow_fallback=True, use_main_renderer_pick=True):
        # Prefer overlay-only picking so we don't interrogate the heavy point
        # cloud actor while element selection is active.
        d = None
        actor = self._pick_overlay_actor(x, y)
        if actor:
            d = self._drawing_owning_actor(actor)

        # Keep the old main-renderer pick as a compatibility fallback for
        # legacy actors that may not live in the digitize overlay.
        if d is None and use_main_renderer_pick:
            try:
                actor = self.digitizer._pick_actor(x, y)
            except Exception:
                actor = None
            if actor:
                d = self._drawing_owning_actor(actor)

        if d is None and allow_fallback:
            # Match the digitizer's own single-click tolerance (25px) so thin
            # lines and smartlines pick on the first try, not the third.
            try:
                d = self.digitizer._get_drawing_under_cursor(x, y, tolerance=25.0)
            except Exception:
                d = None
        return d

    def _pick_overlay_actor(self, x, y):
        renderer = self.overlay_renderer
        if renderer is None:
            return None
        try:
            self._overlay_prop_picker.Pick(x, y, 0, renderer)
            prop = self._overlay_prop_picker.GetViewProp()
            if prop is not None:
                return prop
        except Exception:
            pass
        try:
            self._overlay_cell_picker.Pick(x, y, 0, renderer)
            return self._overlay_cell_picker.GetActor()
        except Exception:
            return None

    def _pick_measurement_segment_at(self, x, y):
        mt = getattr(self.app, "measurement_tool", None)
        if mt is None:
            return None
        try:
            picker = vtk.vtkCellPicker()
            picker.SetTolerance(0.01)
            picker.Pick(x, y, 0, self.overlay_renderer or self.renderer)
            actor = picker.GetActor()
        except Exception:
            actor = None
        if actor is None:
            return None
        for m_idx, measurement in enumerate(getattr(mt, "measurements", []) or []):
            for s_idx, label_data in enumerate(measurement.get("labels", []) or []):
                if actor == label_data.get("line"):
                    return {"measurement_index": m_idx, "segment_index": s_idx}
        return None

    def _iter_pickable_drawings(self):
        drawings = list(getattr(self.digitizer, "drawings", []) or [])
        curve_tool = getattr(self.app, "curve_tool", None)
        finalized = list(getattr(curve_tool, "finalized_actors", []) or [])
        stamp = (
            len(drawings),
            id(drawings[-1]) if drawings else 0,
            len(finalized),
            id(finalized[-1]) if finalized else 0,
        )
        if stamp == self._cached_pickable_stamp:
            return list(self._cached_pickable_drawings)

        if finalized:
            for curve_data in finalized:
                if hasattr(self.digitizer, "_get_drawing_coords"):
                    coords = self.digitizer._get_drawing_coords(curve_data)
                elif isinstance(curve_data, dict):
                    coords = curve_data.get("coords")
                    if coords is None:
                        coords = curve_data.get("interpolated")
                else:
                    coords = []
                if coords is not None and len(coords) > 0:
                    if not any(curve_data is drawing for drawing in drawings):
                        drawings.append(curve_data)
        self._cached_pickable_drawings = list(drawings)
        self._cached_pickable_ids = {id(d) for d in drawings}
        self._cached_pickable_stamp = stamp
        self._rebuild_actor_owner_cache(drawings)
        return drawings

    def _drawing_owning_actor(self, actor):
        """Find the drawing that owns the given actor — match against the main
        actor and ALL marker actors (start_marker, end_marker, vertex_markers,
        arrow_actor). Fixes single-click on Line/SmartLine where the picker
        often lands on an endpoint sphere instead of the line itself.
        """
        if actor is None:
            return None
        self._iter_pickable_drawings()
        drawing = self._cached_actor_owner_map.get(actor)
        if drawing is not None and self._is_live_drawing(drawing):
            return drawing

        drawings = self._iter_pickable_drawings()
        self._rebuild_actor_owner_cache(drawings)
        drawing = self._cached_actor_owner_map.get(actor)
        if drawing is not None and self._is_live_drawing(drawing):
            return drawing
        return None

    def _is_live_drawing(self, drawing):
        if drawing is None:
            return False
        self._iter_pickable_drawings()
        if id(drawing) in self._cached_pickable_ids:
            return True
        drawings = self._iter_pickable_drawings()
        self._cached_pickable_ids = {id(d) for d in drawings}
        return id(drawing) in self._cached_pickable_ids

    def _rebuild_actor_owner_cache(self, drawings):
        owner_map = {}
        for cand in drawings:
            actor = cand.get("actor")
            if actor is not None:
                owner_map[actor] = cand
            for key in ("start_marker", "end_marker"):
                marker = cand.get(key)
                if marker is not None:
                    owner_map[marker] = cand
            for vm in cand.get("vertex_markers") or []:
                if vm is not None:
                    owner_map[vm] = cand
            arrows = cand.get("arrow_actor")
            if isinstance(arrows, list):
                for arrow in arrows:
                    if arrow is not None:
                        owner_map[arrow] = cand
            elif arrows is not None:
                owner_map[arrows] = cand
        self._cached_actor_owner_map = owner_map

    def _reset_pick_caches(self):
        self._cached_pickable_drawings = []
        self._cached_pickable_ids = set()
        self._cached_pickable_stamp = None
        self._cached_actor_owner_map = {}

    def _reset_hover_pick_state(self):
        self._last_hover_pick_ts = 0.0
        self._last_hover_pick_pos = None

    def _should_refresh_hover_pick(self, x, y):
        now = time.monotonic()
        last_pos = self._last_hover_pick_pos
        if last_pos is not None:
            dx = abs(x - last_pos[0])
            dy = abs(y - last_pos[1])
            if (
                dx < self._hover_pick_move_threshold_px
                and dy < self._hover_pick_move_threshold_px
                and (now - self._last_hover_pick_ts) < self._hover_pick_interval_s
            ):
                return False
        self._last_hover_pick_pos = (x, y)
        self._last_hover_pick_ts = now
        return True

    def _world_to_screen(self, wx, wy, wz):
        try:
            self.renderer.SetWorldPoint(wx, wy, wz, 1.0)
            self.renderer.WorldToDisplay()
            dp = self.renderer.GetDisplayPoint()
            return (dp[0], dp[1])
        except Exception:
            return None

    def _screen_to_world(self, sx, sy):
        """Convert a screen pixel position to a world-space point on the Z=0 plane.

        Uses a reusable vtkWorldPointPicker (created once in __init__) so rapid
        clicking never leaks picker objects.
        """
        try:
            self._world_picker.Pick(sx, sy, 0, self.renderer)
            wp = self._world_picker.GetPickPosition()
            return (wp[0], wp[1], 0.0)   # force Z=0 for 2-D drawings
        except Exception:
            return (0.0, 0.0, 0.0)

    def _drawing_screen_points(self, d):
        """Return screen-space points for a drawing used in fence/crossing tests.

        For regular drawings (lines, polylines, rectangles, etc.) this is just
        the projected coords array.

        For TEXT drawings the coords array contains only the anchor point, which
        is often outside the visible bounding box of the rendered text.  We
        therefore return the four screen-space corners of the actor's bounding
        box so that any fence that overlaps the visible text label will pick it.
        """
        dtype = d.get("type", "")

        # ── TEXT: use actor bounding-box corners ──────────────────────────────
        if dtype == "text":
            actor = d.get("actor")
            if actor is not None:
                try:
                    bounds = actor.GetBounds()
                    # bounds = (xmin, xmax, ymin, ymax, zmin, zmax)
                    corners_world = [
                        (bounds[0], bounds[2], bounds[4]),
                        (bounds[1], bounds[2], bounds[4]),
                        (bounds[1], bounds[3], bounds[4]),
                        (bounds[0], bounds[3], bounds[4]),
                    ]
                    out = []
                    for c in corners_world:
                        sp = self._world_to_screen(c[0], c[1], c[2])
                        if sp is not None:
                            out.append(sp)
                    if out:
                        return out
                except Exception:
                    pass
            # Fallback: use anchor point from coords
            coords = d.get("coords") or []
            if coords:
                c = coords[0]
                try:
                    sp = self._world_to_screen(c[0], c[1], c[2] if len(c) > 2 else 0.0)
                    if sp is not None:
                        return [sp]
                except Exception:
                    pass
            return []

        # ── All other drawing types: project coords array ─────────────────────
        coords = d.get("coords") or []
        out = []
        for c in coords:
            if c is None:
                continue
            try:
                if hasattr(c, "__len__") and len(c) >= 3:
                    sp = self._world_to_screen(c[0], c[1], c[2])
                    if sp is not None:
                        out.append(sp)
            except (TypeError, IndexError):
                continue
        return out

    def _drawing_enclosed_in_screen_rect(self, d, rx_min, ry_min, rx_max, ry_max):
        spts = self._drawing_screen_points(d)
        if not spts:
            return False
        if self._enclose_mode == EncloseMode.OVERLAP:
            for (sx, sy) in spts:
                if rx_min <= sx <= rx_max and ry_min <= sy <= ry_max:
                    return True
            # Long line whose endpoints are outside but whose body crosses the rect.
            rect_edges = (
                ((rx_min, ry_min), (rx_max, ry_min)),
                ((rx_max, ry_min), (rx_max, ry_max)),
                ((rx_max, ry_max), (rx_min, ry_max)),
                ((rx_min, ry_max), (rx_min, ry_min)),
            )
            for i in range(len(spts) - 1):
                for (e0, e1) in rect_edges:
                    if self._segments_intersect(spts[i], spts[i + 1], e0, e1):
                        return True
            return False
        # INSIDE — strict containment
        for (sx, sy) in spts:
            if not (rx_min <= sx <= rx_max and ry_min <= sy <= ry_max):
                return False
        return True

    @staticmethod
    def _point_in_polygon(px, py, poly):
        # Ray-casting; poly is list of (x,y) tuples.
        inside = False
        n = len(poly)
        if n < 3:
            return False
        j = n - 1
        for i in range(n):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if ((yi > py) != (yj > py)):
                denom = (yj - yi)
                if denom != 0.0:
                    x_cross = (xj - xi) * (py - yi) / denom + xi
                    if px < x_cross:
                        inside = not inside
            j = i
        return inside

    def _drawing_inside_screen_polygon(self, d, poly):
        spts = self._drawing_screen_points(d)
        if not spts:
            return False
        if self._enclose_mode == EncloseMode.OVERLAP:
            # Any vertex inside, or any segment crossing a polygon edge.
            for (sx, sy) in spts:
                if self._point_in_polygon(sx, sy, poly):
                    return True
            n = len(poly)
            if n >= 2 and len(spts) >= 2:
                for i in range(len(spts) - 1):
                    for j in range(n):
                        if self._segments_intersect(spts[i], spts[i + 1],
                                                    poly[j], poly[(j + 1) % n]):
                            return True
            return False
        # INSIDE — strict containment
        for (sx, sy) in spts:
            if not self._point_in_polygon(sx, sy, poly):
                return False
        return True

    @staticmethod
    def _segments_intersect(p1, p2, p3, p4):
        def ccw(a, b, c):
            return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])
        return (ccw(p1, p3, p4) != ccw(p2, p3, p4) and
                ccw(p1, p2, p3) != ccw(p1, p2, p4))

    def _drawing_crossed_by_screen_segment(self, d, q0, q1):
        spts = self._drawing_screen_points(d)
        if not spts:
            return False
        if len(spts) == 1:
            # Treat as crossed if the segment passes within a few pixels.
            (px, py) = spts[0]
            return ElementSelectTool._point_to_seg_dist(px, py, q0, q1) <= 6.0
        for i in range(len(spts) - 1):
            if self._segments_intersect(spts[i], spts[i + 1], q0, q1):
                return True
        return False

    @staticmethod
    def _point_to_seg_dist(px, py, a, b):
        ax, ay = a
        bx, by = b
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        cx, cy = ax + t * dx, ay + t * dy
        return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5

    # ------------------------------------------------------------------
    # Screen-space overlay actors (rubberband + polygon preview)
    # ------------------------------------------------------------------
    def _refresh_block_overlay(self, p0, p1):
        self._remove_overlay("_block_overlay_actor")
        x0, y0 = p0
        x1, y1 = p1
        self._block_overlay_actor = self._make_screen_polyline(
            [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)],
            color=(0.2, 0.8, 1.0), width=1.5, dashed=True,
        )
        if self._block_overlay_actor is not None:
            try:
                self.renderer.AddActor2D(self._block_overlay_actor)
            except Exception:
                pass
        self._render()

    def _refresh_shape_overlay(self, cursor_screen=None):
        """Rebuild the shape polygon preview.

        World-coord vertices are re-projected to screen on every call so the
        overlay stays locked to geometry after zoom or pan — same pattern as
        the digitizer's own preview actors.
        """
        self._remove_overlay("_shape_overlay_actor")

        # Re-project stored world vertices → current screen positions
        pts = [sp for sp in (self._world_to_screen(w[0], w[1], w[2])
                              for w in self._shape_pts_world) if sp]

        # Append live cursor position as rubber-band endpoint
        if cursor_screen is not None:
            pts = pts + [cursor_screen]

        if len(pts) < 2:
            return

        # When we have 3+ committed vertices + cursor, close the preview polygon
        if len(self._shape_pts_world) >= 2 and cursor_screen is not None:
            pts = pts + [pts[0]]   # close back to first vertex

        self._shape_overlay_actor = self._make_screen_polyline(
            pts, color=(0.2, 1.0, 0.6), width=1.5, dashed=True,
        )
        if self._shape_overlay_actor is not None:
            try:
                self.renderer.AddActor2D(self._shape_overlay_actor)
            except Exception:
                pass
        self._render()

    def _refresh_cline_overlay(self, cursor_screen=None):
        """Rebuild the crossing-line preview.

        First point is stored in world coords and re-projected on every call.
        """
        self._remove_overlay("_cline_overlay_actor")
        if self._cline_first_pt_world is None or cursor_screen is None:
            return

        first_screen = self._world_to_screen(
            self._cline_first_pt_world[0],
            self._cline_first_pt_world[1],
            self._cline_first_pt_world[2],
        )
        if first_screen is None:
            return

        self._cline_overlay_actor = self._make_screen_polyline(
            [first_screen, cursor_screen],
            color=(1.0, 0.4, 0.4), width=1.5, dashed=True,
        )
        if self._cline_overlay_actor is not None:
            try:
                self.renderer.AddActor2D(self._cline_overlay_actor)
            except Exception:
                pass
        self._render()

    def _make_screen_polyline(self, pts_screen, color=(1, 1, 1), width=1.0, dashed=False):
        try:
            pts = vtk.vtkPoints()
            for (sx, sy) in pts_screen:
                pts.InsertNextPoint(sx, sy, 0.0)
            line = vtk.vtkPolyLine()
            line.GetPointIds().SetNumberOfIds(len(pts_screen))
            for i in range(len(pts_screen)):
                line.GetPointIds().SetId(i, i)
            cells = vtk.vtkCellArray()
            cells.InsertNextCell(line)
            pd = vtk.vtkPolyData()
            pd.SetPoints(pts)
            pd.SetLines(cells)
            coord = vtk.vtkCoordinate()
            coord.SetCoordinateSystemToDisplay()
            mapper = vtk.vtkPolyDataMapper2D()
            mapper.SetInputData(pd)
            mapper.SetTransformCoordinate(coord)
            actor = vtk.vtkActor2D()
            actor.SetMapper(mapper)
            prop = actor.GetProperty()
            prop.SetColor(*color)
            prop.SetLineWidth(width)
            if dashed and hasattr(prop, "SetLineStipplePattern"):
                try:
                    prop.SetLineStipplePattern(0xF0F0)
                    prop.SetLineStippleRepeatFactor(1)
                except Exception:
                    pass
            return actor
        except Exception as e:
            print(f"⚠️ overlay actor build failed: {e}")
            return None

    def _remove_overlay(self, attr_name):
        actor = getattr(self, attr_name, None)
        if actor is None:
            return
        try:
            self.renderer.RemoveActor2D(actor)
        except Exception:
            pass
        setattr(self, attr_name, None)

    # ------------------------------------------------------------------
    # Cancel / cleanup helpers
    # ------------------------------------------------------------------
    def _cancel_in_progress_pick(self):
        # Clear copy mode
        if self._copy_mode:
            for actor in self._copy_preview_actors:
                self._remove_intersect_actor(actor)
            self._copy_preview_actors = []
        self._copy_mode = False
        self._copy_template = []
        self._copy_centroid = None

        self._dragging_block = False
        self._block_start_screen = None
        self._remove_overlay("_block_overlay_actor")

        self._shape_pts_world = []
        self._remove_overlay("_shape_overlay_actor")

        self._cline_first_pt_world = None
        self._remove_overlay("_cline_overlay_actor")

        # Clear move-mode drag state without triggering undo
        self._move_mode = False
        self._drag_active = False
        self._drag_last_world = None
        self._drag_undo_saved = False

        # Clear intersect mode (new multi-figure state + legacy fields)
        if self._intersect_mode:
            self._restore_intersect_hidden_drawings()
            self._clear_intersect_overlays()
            self._notify_intersect_active(False)
        self._intersect_mode = False
        self._intersect_parts = []
        self._intersect_part_actors = []
        self._intersect_selected_part = None
        self._intersect_selected_actor = None
        self._intersect_hovered_part = None
        self._intersect_hidden_drawings = []
        self._intersect_visibility_states = []
        self._intersect_delete_history = []
        self._intersect_redo_history = []
        self._intersect_drawing = None
        self._intersect_seg_idx = None
        self._intersect_pt_world = None
        self._intersect_chosen_part = None
        self._intersect_chosen_coords = None
        self._intersect_part_a_coords = []
        self._intersect_part_b_coords = []
        self._remove_overlay("_intersect_part_a_actor")
        self._remove_overlay("_intersect_part_b_actor")

    # ------------------------------------------------------------------
    # Delete helpers used when running as "Delete Element" tool
    # ------------------------------------------------------------------
    def _delete_drawing_with_undo(self, drawing):
        if drawing is None:
            return
        if not self._is_live_drawing(drawing):
            self._status("Nothing to delete.")
            return
        # Refuse classified fences (matches Shift+C behavior elsewhere).
        if drawing.get("classified_fence", False):
            self._status("Classified fence: refused. Use 'Clear Classified' to remove.")
            return
        try:
            self.digitizer._save_state()
        except Exception:
            pass
        try:
            self.digitizer._remove_drawing(drawing)
        except Exception as e:
            print(f"⚠️ delete-on-click failed: {e}")
        self._render()

    def _delete_many_with_undo(self, drawings):
        drawings = [d for d in drawings if self._is_live_drawing(d)]
        targets = [d for d in drawings if not d.get("classified_fence", False)]
        skipped = len(drawings) - len(targets)
        if not targets:
            self._status("Nothing to delete (classified fences refused).")
            return
        try:
            self.digitizer._save_state()
        except Exception:
            pass
        deleted = 0
        for d in targets:
            try:
                self.digitizer._remove_drawing(d)
                deleted += 1
            except Exception as e:
                print(f"⚠️ delete failed for one element: {e}")
        msg = f"Deleted {deleted} element(s)"
        if skipped:
            msg += f" (skipped {skipped} classified)"
        self._status(msg)
        self._render()

    # ------------------------------------------------------------------
    # Intersect helpers
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Multi-figure split helpers (new intersect logic)
    # ------------------------------------------------------------------
    def _split_all_at_intersections(self, selected_drawings):
        """For each drawing in *selected_drawings*, find all points where it is
        crossed by any OTHER drawing in the same set, then split it into sub-
        segments at those intersection points.

        Returns a flat list of part dicts:
          { 'coords': [...], 'source_drawing': drawing, 'deleted': False }
        Drawings with intersections are split into sub-parts at cut points.
        Drawings with no intersections are included as a single whole part so
        they show in the overlay and remain deletable as a unit.

        Uses screen-space detection (same as the rest of the tool) then
        computes precise world-space intersection points.
        """
        # Collect screen projections for all selected drawings (cached per call).
        # For curve-tool drawings use interpolated (rendered) coords so that the
        # screen-space intersection test matches what the user actually sees.
        screen_cache = {}
        for d in selected_drawings:
            interp = d.get("interpolated")
            coords = interp if (d.get("source") == "curve_tool" and interp is not None) \
                else (d.get("coords") or [])
            spts = []
            for c in coords:
                if c is None:
                    spts.append(None)
                    continue
                try:
                    sp = self._world_to_screen(c[0], c[1], c[2] if len(c) > 2 else 0.0)
                    spts.append(sp)
                except Exception:
                    spts.append(None)
            screen_cache[id(d)] = spts

        result_parts = []

        for drawing in selected_drawings:
            # For curve-tool drawings use the interpolated (rendered) polyline for
            # intersection detection and splitting; control-point coords are too sparse.
            interp = drawing.get("interpolated")
            coords = interp if (drawing.get("source") == "curve_tool" and interp is not None) \
                else (drawing.get("coords") or [])
            if len(coords) < 2:
                continue
            own_screen = screen_cache[id(drawing)]

            # Gather all intersection t-parameters on each segment of this drawing
            # segment_cuts[si] = sorted list of t in (0,1) where a cut happens
            segment_cuts = [[] for _ in range(len(coords) - 1)]
            cut_points = [[] for _ in range(len(coords) - 1)]  # world points per segment

            for other in selected_drawings:
                if other is drawing:
                    continue
                o_interp = other.get("interpolated")
                other_coords = o_interp if (other.get("source") == "curve_tool" and o_interp is not None) \
                    else (other.get("coords") or [])
                if len(other_coords) < 2:
                    continue
                other_screen = screen_cache[id(other)]

                for si in range(len(own_screen) - 1):
                    s0, s1 = own_screen[si], own_screen[si + 1]
                    if s0 is None or s1 is None:
                        continue
                    ca0, ca1 = coords[si], coords[si + 1]
                    if ca0 is None or ca1 is None:
                        continue

                    for oi in range(len(other_screen) - 1):
                        o0, o1 = other_screen[oi], other_screen[oi + 1]
                        if o0 is None or o1 is None:
                            continue
                        cb0, cb1 = other_coords[oi], other_coords[oi + 1]
                        if cb0 is None or cb1 is None:
                            continue

                        if not self._segments_intersect(s0, s1, o0, o1):
                            continue

                        pt_w = self._segment_intersection_point_world(ca0, ca1, cb0, cb1)
                        # Compute t along the own segment
                        dx = ca1[0] - ca0[0]
                        dy = ca1[1] - ca0[1]
                        seg_len_sq = dx * dx + dy * dy
                        if seg_len_sq < 1e-20:
                            t = 0.5
                        else:
                            t = ((pt_w[0] - ca0[0]) * dx + (pt_w[1] - ca0[1]) * dy) / seg_len_sq
                        t = max(0.0, min(1.0, t))
                        # Avoid duplicate cuts at effectively the same point (< 1e-6 apart)
                        if not any(abs(t - existing_t) < 1e-6 for existing_t in segment_cuts[si]):
                            segment_cuts[si].append(t)
                            cut_points[si].append(pt_w)

            # Build the sub-part coordinate lists
            parts_coords = self._build_parts_from_cuts(coords, segment_cuts, cut_points)

            had_intersection = any(len(segment_cuts[si]) > 0 for si in range(len(segment_cuts)))

            orig_color = (
                drawing.get("original_color")
                or drawing.get("color")
                or (1.0, 1.0, 1.0)
            )
            orig_width = (
                drawing.get("original_width")
                or drawing.get("width")
                or 2.0
            )
            if not had_intersection:
                # No intersections — include as a single whole part so it
                # appears in the overlay and can be selected/deleted.
                # This is essential for line-tool drawings which create
                # independent 2-point segments: without this, non-crossing
                # segments would remain as original actors outside the overlay
                # and could never be removed via intersect.
                result_parts.append({
                    "coords": list(coords),
                    "source_drawing": drawing,
                    "orig_color": orig_color,
                    "orig_width": orig_width,
                    "deleted": False,
                })
                continue

            for part_c in parts_coords:
                if len(part_c) >= 2:
                    result_parts.append({
                        "coords": part_c,
                        "source_drawing": drawing,
                        "orig_color": orig_color,
                        "orig_width": orig_width,
                        "deleted": False,
                    })

        return result_parts

    @staticmethod
    def _build_parts_from_cuts(coords, segment_cuts, cut_points):
        """Reconstruct a list of coordinate sub-lists from cut parameters.

        *coords*        — original coordinate list of the drawing
        *segment_cuts*  — segment_cuts[i] is a list of t values in (0,1) for segment i
        *cut_points*    — cut_points[i] is the list of world-space intersection points
                          in the same order as segment_cuts[i]

        Returns a list of sub-part coordinate lists.
        """
        current_part = [coords[0]]
        all_parts = []

        for si in range(len(coords) - 1):
            c1 = coords[si + 1]

            cuts = segment_cuts[si]
            pts = cut_points[si]
            if not cuts:
                current_part.append(c1)
                continue

            # Sort cuts by t so we process left-to-right along the segment
            order = sorted(range(len(cuts)), key=lambda k: cuts[k])
            for k in order:
                cut_pt = pts[k]
                current_part.append(cut_pt)
                all_parts.append(current_part)
                current_part = [cut_pt]
            current_part.append(c1)

        all_parts.append(current_part)
        return all_parts

    def _make_world_polyline(self, world_coords, color=(1, 1, 1), width=2.0):
        """Build a 3-D world-space overlay actor so it stays correct on zoom/pan."""
        coords_clean = [c for c in world_coords if c is not None]
        if len(coords_clean) < 2:
            return None
        try:
            actor = self.digitizer._make_polyline_actor(
                coords_clean, color=color, width=width, line_style="solid"
            )
            return actor
        except Exception as e:
            print(f"⚠️ _make_world_polyline failed: {e}")
            return None

    def _add_intersect_actor(self, actor):
        """Add a 3-D intersect overlay actor via the digitizer's overlay renderer."""
        if actor is None:
            return
        try:
            self.digitizer._add_actor_to_overlay(actor)
        except Exception:
            try:
                self.overlay_renderer.AddActor(actor)
            except Exception:
                pass

    def _remove_intersect_actor(self, actor):
        """Remove a 3-D intersect overlay actor from the renderer."""
        if actor is None:
            return
        try:
            self.overlay_renderer.RemoveActor(actor)
        except Exception:
            pass
        try:
            self.renderer.RemoveActor(actor)
        except Exception:
            pass

    def _find_intersection_on_drawing(self, drawing):
        """Search all other drawings for a segment that crosses any segment of
        *drawing*.  Returns (seg_idx, pt_world) where seg_idx is the index of
        the first coord of the intersected segment in drawing['coords'], and
        pt_world is the exact intersection point in world coordinates computed
        directly from world-space coords (no screen roundtrip, no precision loss).
        Returns None if no intersection is found.
        """
        interp = drawing.get("interpolated")
        coords = drawing.get("coords") if drawing.get("coords") is not None \
            else (interp if interp is not None else [])
        if len(coords) < 2:
            return None

        # Build screen-space projection for intersection detection only
        own_screen = []
        for c in coords:
            if c is None:
                own_screen.append(None)
                continue
            try:
                sp = self._world_to_screen(c[0], c[1], c[2] if len(c) > 2 else 0.0)
                own_screen.append(sp)
            except Exception:
                own_screen.append(None)

        for other in list(self._iter_pickable_drawings()):
            if other is drawing:
                continue
            if other.get("type", "") == "text":
                continue
            o_interp = other.get("interpolated")
            other_coords = other.get("coords") if other.get("coords") is not None \
                else (o_interp if o_interp is not None else [])
            if len(other_coords) < 2:
                continue
            # Build other_screen preserving None entries so indices align with other_coords.
            # _drawing_screen_points() compacts out Nones, breaking index correspondence.
            other_screen = []
            for c in other_coords:
                if c is None:
                    other_screen.append(None)
                    continue
                try:
                    sp = self._world_to_screen(c[0], c[1], c[2] if len(c) > 2 else 0.0)
                    other_screen.append(sp)
                except Exception:
                    other_screen.append(None)
            for si in range(len(own_screen) - 1):
                s0, s1 = own_screen[si], own_screen[si + 1]
                if s0 is None or s1 is None:
                    continue
                for oi in range(len(other_screen) - 1):
                    o0, o1 = other_screen[oi], other_screen[oi + 1]
                    if o0 is None or o1 is None:
                        continue
                    if self._segments_intersect(s0, s1, o0, o1):
                        # Indices now align: other_coords[oi] corresponds to other_screen[oi]
                        ca0, ca1 = coords[si], coords[si + 1]
                        cb0, cb1 = other_coords[oi], other_coords[oi + 1]
                        if (ca0 is not None and ca1 is not None and
                                cb0 is not None and cb1 is not None):
                            pt_world = self._segment_intersection_point_world(
                                ca0, ca1, cb0, cb1
                            )
                        else:
                            ix, iy = self._segment_intersection_point(s0, s1, o0, o1)
                            pt_world = self._screen_to_world(ix, iy)
                        return (si, pt_world)
        return None

    @staticmethod
    def _segment_intersection_point_world(a0, a1, b0, b1):
        """Compute the intersection of two segments in world (XY) coordinates.

        Uses the parametric form on the first segment: P = a0 + t*(a1-a0).
        Returns a [x, y, z] list with Z interpolated from a0/a1.
        Falls back to midpoint of a0,a1 if segments are parallel/degenerate.
        """
        x1, y1 = a0[0], a0[1]
        x2, y2 = a1[0], a1[1]
        x3, y3 = b0[0], b0[1]
        x4, y4 = b1[0], b1[1]
        denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(denom) < 1e-14:
            # Parallel — return midpoint of first segment as fallback
            z = (a0[2] if len(a0) > 2 else 0.0)
            return [(x1 + x2) / 2.0, (y1 + y2) / 2.0, z]
        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
        ix = x1 + t * (x2 - x1)
        iy = y1 + t * (y2 - y1)
        z0 = a0[2] if len(a0) > 2 else 0.0
        z1 = a1[2] if len(a1) > 2 else 0.0
        iz = z0 + t * (z1 - z0)
        return [ix, iy, iz]

    @staticmethod
    def _segment_intersection_point(p1, p2, p3, p4):
        """Screen-space intersection point — used only for overlay positioning."""
        x1, y1 = p1
        x2, y2 = p2
        x3, y3 = p3
        x4, y4 = p4
        denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(denom) < 1e-10:
            return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
        ix = x1 + t * (x2 - x1)
        iy = y1 + t * (y2 - y1)
        return (ix, iy)

    def _build_intersect_overlays(self, drawing, seg_idx, pt_world):
        """Create two coloured 2-D overlay actors for the two bisected halves.

        Part A (magenta) — coords[0..seg_idx] + intersection point
        Part B (cyan)    — intersection point + coords[seg_idx+1..]
        Both actors are added to the overlay renderer immediately.
        """
        coords = drawing.get("coords") or []
        # Build world-coord lists for each part
        pt_screen = self._world_to_screen(pt_world[0], pt_world[1], pt_world[2])
        if pt_screen is None:
            return

        # Part A: first vertex through segment start up to intersection
        part_a_world = list(coords[:seg_idx + 1]) + [pt_world]
        # Part B: intersection through segment end to last vertex
        part_b_world = [pt_world] + list(coords[seg_idx + 1:])

        def world_list_to_screen(world_list):
            out = []
            for c in world_list:
                if c is None:
                    continue
                try:
                    sp = self._world_to_screen(c[0], c[1], c[2] if len(c) > 2 else 0.0)
                    if sp is not None:
                        out.append(sp)
                except Exception:
                    pass
            return out

        pts_a = world_list_to_screen(part_a_world)
        pts_b = world_list_to_screen(part_b_world)

        self._remove_overlay("_intersect_part_a_actor")
        self._remove_overlay("_intersect_part_b_actor")

        if len(pts_a) >= 2:
            self._intersect_part_a_actor = self._make_screen_polyline(
                pts_a, color=(1.0, 0.2, 1.0), width=3.0, dashed=False
            )
            if self._intersect_part_a_actor is not None:
                try:
                    self.renderer.AddActor2D(self._intersect_part_a_actor)
                except Exception:
                    pass

        if len(pts_b) >= 2:
            self._intersect_part_b_actor = self._make_screen_polyline(
                pts_b, color=(0.0, 1.0, 1.0), width=3.0, dashed=False
            )
            if self._intersect_part_b_actor is not None:
                try:
                    self.renderer.AddActor2D(self._intersect_part_b_actor)
                except Exception:
                    pass

        # Store world coords for deletion
        self._intersect_part_a_coords = part_a_world
        self._intersect_part_b_coords = part_b_world
        self._render()

    def _intersect_hover(self, x, y):
        """Highlight the sub-part the cursor is nearest to (if none selected yet)."""
        if not self._intersect_mode or not self._intersect_parts:
            return
        # Don't change hover highlighting while a part is already selected
        if self._intersect_selected_part is not None:
            return

        best_idx = None
        best_dist = float("inf")
        for idx, part in enumerate(self._intersect_parts):
            if part.get("deleted"):
                continue
            coords = part["coords"]
            pts = []
            for c in coords:
                if c is None:
                    continue
                try:
                    sp = self._world_to_screen(c[0], c[1], c[2] if len(c) > 2 else 0.0)
                    if sp is not None:
                        pts.append(sp)
                except Exception:
                    pass
            if len(pts) < 2:
                continue
            for i in range(len(pts) - 1):
                d = self._point_to_seg_dist(x, y, pts[i], pts[i + 1])
                if d < best_dist:
                    best_dist = d
                    best_idx = idx

        HOVER_THRESHOLD_PX = 15.0
        if best_dist > HOVER_THRESHOLD_PX:
            best_idx = None

        if best_idx == self._intersect_hovered_part:
            return  # no change — skip redraw

        self._intersect_hovered_part = best_idx
        self._rebuild_intersect_actor_colors()

    def _rebuild_intersect_actor_colors(self):
        """Repaint all part actors with the correct highlight color."""
        selected_idx = self._intersect_selected_part
        hovered_idx = self._intersect_hovered_part

        # Remove old selected actor
        self._remove_intersect_actor(self._intersect_selected_actor)
        self._intersect_selected_actor = None

        for idx, part in enumerate(self._intersect_parts):
            # Remove old actor for this slot
            old_actor = self._intersect_part_actors[idx] if idx < len(self._intersect_part_actors) else None
            self._remove_intersect_actor(old_actor)
            if idx < len(self._intersect_part_actors):
                self._intersect_part_actors[idx] = None

            if part.get("deleted"):
                continue

            if idx == selected_idx:
                color, width = (1.0, 0.0, 0.0), 5.0  # bright red (selected/confirmed)
            elif idx == hovered_idx:
                color, width = (1.0, 0.0, 0.2), 3.5  # neon red hover
            else:
                # Restore original figure color for idle parts
                color = part.get("orig_color", (1.0, 1.0, 1.0))
                width = part.get("orig_width", 2.0)

            actor = self._make_world_polyline(part["coords"], color=color, width=width)
            self._add_intersect_actor(actor)
            if idx < len(self._intersect_part_actors):
                self._intersect_part_actors[idx] = actor

        self._render()

    def _intersect_pick_part(self, x, y):
        """Left-click in intersect mode: select the nearest part (neon red)."""
        if not self._intersect_mode or not self._intersect_parts:
            return

        best_idx = None
        best_dist = float("inf")
        for idx, part in enumerate(self._intersect_parts):
            if part.get("deleted"):
                continue
            coords = part["coords"]
            pts = []
            for c in coords:
                if c is None:
                    continue
                try:
                    sp = self._world_to_screen(c[0], c[1], c[2] if len(c) > 2 else 0.0)
                    if sp is not None:
                        pts.append(sp)
                except Exception:
                    pass
            if len(pts) < 2:
                continue
            for i in range(len(pts) - 1):
                d = self._point_to_seg_dist(x, y, pts[i], pts[i + 1])
                if d < best_dist:
                    best_dist = d
                    best_idx = idx

        CLICK_THRESHOLD_PX = 20.0
        if best_dist > CLICK_THRESHOLD_PX or best_idx is None:
            # Click missed all parts — deselect current
            self._intersect_selected_part = None
            self._rebuild_intersect_actor_colors()
            self._status("Intersect Active — left-click a part to select, right-click to delete.", timeout=0)
            return

        self._intersect_selected_part = best_idx
        self._intersect_hovered_part = None  # clear hover while selected
        self._rebuild_intersect_actor_colors()
        self._status(
            "Intersect: part selected (neon red) — right-click to delete it, Esc to cancel.",
            timeout=0,
        )

    def _intersect_confirm_delete(self):
        """Right-click in intersect mode: mark the selected part as deleted.

        Actual drawing modification is deferred to exit_intersect_mode so
        that every part can be independently selected regardless of how many
        deletions have already happened in this session.
        """
        if self._intersect_selected_part is None:
            self._status("Intersect Active — left-click a part to select, right-click to delete.", timeout=0)
            return

        idx = self._intersect_selected_part
        if idx >= len(self._intersect_parts):
            self._intersect_selected_part = None
            return

        part = self._intersect_parts[idx]

        # Remove only this part's overlay actor — leave all others intact
        old_actor = self._intersect_part_actors[idx] if idx < len(self._intersect_part_actors) else None
        self._remove_intersect_actor(old_actor)
        if idx < len(self._intersect_part_actors):
            self._intersect_part_actors[idx] = None

        # Mark deleted — drawing is NOT touched yet; push to undo history
        part["deleted"] = True
        self._intersect_delete_history.append(idx)
        # Clear redo history since we're making a new forward action
        self._intersect_redo_history = []
        self._intersect_selected_part = None
        self._intersect_hovered_part = None

        self._rebuild_intersect_actor_colors()
        remaining = sum(1 for p in self._intersect_parts if not p.get("deleted"))
        self._status(
            f"Intersect Active — part marked for removal. {remaining} part(s) remain. "
            "Left-click to select another, or click Intersect to finish.",
            timeout=0,
        )

    def _intersect_undo_last_delete(self):
        """Ctrl+Z inside intersect: un-mark the most recently deleted part and restore its visual."""
        if not self._intersect_delete_history:
            self._status("Intersect Active — no deletions to undo.", timeout=2000)
            return
        idx = self._intersect_delete_history.pop()
        if idx >= len(self._intersect_parts):
            return
        part = self._intersect_parts[idx]
        part["deleted"] = False
        
        # Push to redo history so Ctrl+Y can restore this deletion
        self._intersect_redo_history.append(idx)
        
        self._intersect_selected_part = None
        self._intersect_hovered_part = None
        self._remove_intersect_actor(self._intersect_selected_actor)
        self._intersect_selected_actor = None

        # Undo changes one part only.  Recreating every overlay actor here
        # briefly empties the non-erasing renderer and can expose a black
        # native VTK buffer when the action comes from a keyboard shortcut.
        old_actor = self._intersect_part_actors[idx] if idx < len(self._intersect_part_actors) else None
        self._remove_intersect_actor(old_actor)
        actor = self._make_world_polyline(
            part["coords"],
            color=part.get("orig_color", (1.0, 1.0, 1.0)),
            width=part.get("orig_width", 2.0),
        )
        self._add_intersect_actor(actor)
        if idx < len(self._intersect_part_actors):
            self._intersect_part_actors[idx] = actor
        self._schedule_intersect_history_render()
        remaining = sum(1 for p in self._intersect_parts if not p.get("deleted"))
        self._status(
            f"Intersect Active — deletion undone. {remaining} part(s) remain. "
            "Left-click to select another, or click Intersect to finish.",
            timeout=0,
        )

    def _intersect_redo_last_delete(self):
        """Ctrl+Y inside intersect: re-mark the most recently undone part deletion."""
        if not self._intersect_redo_history:
            self._status("Intersect Active — no deletions to redo.", timeout=2000)
            return
        idx = self._intersect_redo_history.pop()
        if idx >= len(self._intersect_parts):
            return
        part = self._intersect_parts[idx]
        
        # Remove the part's overlay actor visually
        old_actor = self._intersect_part_actors[idx] if idx < len(self._intersect_part_actors) else None
        self._remove_intersect_actor(old_actor)
        if idx < len(self._intersect_part_actors):
            self._intersect_part_actors[idx] = None
        
        # Mark deleted and push back to undo history
        part["deleted"] = True
        self._intersect_delete_history.append(idx)
        
        self._intersect_selected_part = None
        self._intersect_hovered_part = None
        self._remove_intersect_actor(self._intersect_selected_actor)
        self._intersect_selected_actor = None

        # Redo already removed the changed part above.  Leave every surviving
        # actor in place instead of tearing down and rebuilding the whole layer.
        self._schedule_intersect_history_render()
        remaining = sum(1 for p in self._intersect_parts if not p.get("deleted"))
        self._status(
            f"Intersect Active — deletion redone. {remaining} part(s) remain. "
            "Left-click to select another, or click Intersect to finish.",
            timeout=0,
        )

    def _apply_split_to_drawing(self, drawing, surviving_coord_lists):
        """Replace or remove the source drawing based on surviving sub-parts.

        If one part survives it updates the drawing in place.
        If multiple parts survive the first reuses the original drawing;
        extras are registered as new independent drawings.
        If none survive, the drawing is removed entirely.
        """
        valid = [cl for cl in surviving_coord_lists if cl and len(cl) >= 2]
        try:
            if not valid:
                self.digitizer._remove_drawing(drawing)
                return

            is_curve = drawing.get("source") == "curve_tool"
            color = drawing.get("original_color") or drawing.get("color") or (1.0, 0.0, 0.0)
            width = drawing.get("original_width") or drawing.get("width") or 2
            line_style = drawing.get("original_style") or "solid"
            # Curve drawings split into straight polyline sub-segments; downgrade
            # to smartline so _rebuild_drawing_actor doesn't try to feed them
            # back into curve_tool._create_curve_actor as control points.
            dtype = "smartline" if is_curve else drawing.get("type", "smartline")

            # First surviving part: update the original drawing in place
            drawing["coords"] = [list(c) for c in valid[0]]
            if is_curve:
                drawing["source"] = None          # stop _rebuild from using curve path
                drawing.pop("control_points", None)
                drawing.pop("interpolated", None)
                drawing["type"] = dtype
            # Remove stale start/end/vertex markers from the old drawing
            # BEFORE rebuild so orphaned spheres don't linger in the scene.
            for key in ("start_marker", "end_marker"):
                old_m = drawing.get(key)
                if old_m is not None:
                    try:
                        self.digitizer._remove_actor_from_overlay(old_m)
                    except Exception:
                        pass
                    drawing[key] = None
            for old_m in drawing.get("vertex_markers") or []:
                if old_m is not None:
                    try:
                        self.digitizer._remove_actor_from_overlay(old_m)
                    except Exception:
                        pass
            drawing["vertex_markers"] = []
            try:
                self.digitizer._rebuild_drawing_actor(drawing)
            except Exception as e:
                print(f"⚠️ Intersect rebuild failed: {e}")

            # Extra surviving parts: build brand-new drawing dicts and
            # register them the same way the digitizer does internally.
            for extra_coords in valid[1:]:
                try:
                    coords_clean = [list(c) for c in extra_coords]
                    new_actor = self.digitizer._make_polyline_actor(
                        coords_clean, color=color, width=width, line_style=line_style
                    )
                    self.digitizer._add_actor_to_overlay(new_actor)
                    new_d = {
                        "type": dtype,
                        "coords": coords_clean,
                        "actor": new_actor,
                        "bounds": new_actor.GetBounds(),
                        "original_color": color,
                        "original_width": width,
                        "original_style": line_style,
                    }
                    # Carry across non-visual metadata (but not curve-specific keys)
                    for key in ("layer", "classified_fence"):
                        if key in drawing:
                            new_d[key] = drawing[key]
                    self.digitizer.drawings.append(new_d)
                except Exception as e:
                    print(f"⚠️ Intersect: could not register extra part: {e}")
        except Exception as e:
            print(f"⚠️ Intersect _apply_split_to_drawing failed: {e}")

        # Clear selection so the scene looks clean
        try:
            self.digitizer._selection_manager.clear()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Misc helpers
    # ------------------------------------------------------------------
    def _current_mode(self):
        try:
            ctrl = bool(self.interactor.GetControlKey())
            shift = bool(self.interactor.GetShiftKey())
        except Exception:
            ctrl, shift = False, False
        if ctrl and shift:
            return SelectionMode.INVERT
        if ctrl:
            return SelectionMode.ADD
        if shift:
            return SelectionMode.SUBTRACT
        return SelectionMode.NEW

    def _cursor_for_method(self):
        from PySide6.QtCore import Qt
        return Qt.CrossCursor

    def _render(self):
        seen = set()
        for renderer in (self.renderer, self.overlay_renderer):
            if renderer is None or id(renderer) in seen:
                continue
            seen.add(id(renderer))
            try:
                renderer.Modified()
            except Exception:
                pass
        try:
            self.app.vtk_widget.render()
        except Exception:
            try:
                self.interactor.GetRenderWindow().Render()
            except Exception:
                pass

    def _schedule_intersect_history_render(self):
        """Queue one complete frame after a Ctrl+Z/Ctrl+Y Qt callback.

        Intersect actors live in a non-erasing overlay renderer.  The short
        delay lets Qt finish shortcut dispatch before VTK touches its native
        surface, while key repeat is coalesced into a single repaint.
        """
        if self._intersect_history_render_pending:
            return
        app = getattr(self, "app", None)
        if app is None or getattr(app, "_shutdown_in_progress", False):
            return

        self._intersect_history_camera_state = self._capture_main_camera_state()
        self._intersect_history_render_pending = True
        try:
            self._intersect_history_render_timer.start(
                self.INTERSECT_HISTORY_RENDER_DELAY_MS
            )
        except Exception:
            self._intersect_history_render_pending = False
            self._intersect_history_camera_state = None

    def _cancel_intersect_history_render(self):
        """Cancel a queued history repaint before intersect/tool teardown."""
        try:
            self._intersect_history_render_timer.stop()
        except Exception:
            pass
        self._intersect_history_render_pending = False
        self._intersect_history_camera_state = None

    def _capture_main_camera_state(self):
        """Capture framing so an intersect history key cannot alter the view."""
        try:
            camera = self.renderer.GetActiveCamera()
            if camera is None:
                return None
            return {
                "position": tuple(camera.GetPosition()),
                "focal_point": tuple(camera.GetFocalPoint()),
                "view_up": tuple(camera.GetViewUp()),
                "parallel_projection": int(camera.GetParallelProjection()),
                "parallel_scale": float(camera.GetParallelScale()),
                "view_angle": float(camera.GetViewAngle()),
                "window_center": tuple(camera.GetWindowCenter()),
                "clipping_range": tuple(camera.GetClippingRange()),
            }
        except Exception:
            return None

    def _restore_main_camera_state(self, state):
        """Restore the exact pre-shortcut camera; return whether it had moved."""
        if not state:
            return False
        try:
            camera = self.renderer.GetActiveCamera()
            if camera is None:
                return False

            current_position = tuple(camera.GetPosition())
            current_focal = tuple(camera.GetFocalPoint())
            current_scale = float(camera.GetParallelScale())
            changed = (
                current_position != state["position"]
                or current_focal != state["focal_point"]
                or abs(current_scale - state["parallel_scale"]) > 1e-12
                or int(camera.GetParallelProjection())
                != state["parallel_projection"]
            )

            camera.SetParallelProjection(state["parallel_projection"])
            camera.SetPosition(*state["position"])
            camera.SetFocalPoint(*state["focal_point"])
            camera.SetViewUp(*state["view_up"])
            camera.SetParallelScale(state["parallel_scale"])
            camera.SetViewAngle(state["view_angle"])
            camera.SetWindowCenter(*state["window_center"])
            camera.SetClippingRange(*state["clipping_range"])
            return changed
        except Exception:
            return False

    def _flush_intersect_history_render(self):
        """Render the rebuilt base and overlay layers when they are still live."""
        self._intersect_history_render_pending = False
        camera_state = self._intersect_history_camera_state
        self._intersect_history_camera_state = None
        app = getattr(self, "app", None)
        if (
            app is None
            or getattr(app, "_shutdown_in_progress", False)
            or not getattr(self, "_active", False)
            or not getattr(self, "_intersect_mode", False)
        ):
            return False

        widget = getattr(app, "vtk_widget", None)
        if widget is None:
            return False

        # Do not cross into VTK when Qt is hiding/destroying the main widget.
        try:
            if bool(getattr(widget, "_naksha_skip_render", False)):
                return False
            if hasattr(widget, "isVisible") and not widget.isVisible():
                return False
            render_window = widget.GetRenderWindow()
            if render_window is None:
                return False
            if hasattr(render_window, "GetInteractor") and render_window.GetInteractor() is None:
                return False
        except Exception:
            return False

        text_overlay_renderer = getattr(
            self.digitizer, "text_overlay_renderer", None
        )
        try:
            if text_overlay_renderer is not None and hasattr(
                text_overlay_renderer, "SetPreserveColorBuffer"
            ):
                text_overlay_renderer.SetPreserveColorBuffer(True)
        except Exception:
            return False

        seen = set()
        for renderer in (
            self.renderer,
            self.overlay_renderer,
            text_overlay_renderer,
        ):
            if renderer is None or id(renderer) in seen:
                continue
            seen.add(id(renderer))
            try:
                renderer.Modified()
            except Exception:
                return False

        # Enforce the core invariant: intersect undo/redo changes geometry only,
        # never the main-view camera. This also repairs any downstream shortcut
        # observer that managed to process Ctrl+Y before this queued frame.
        try:
            camera = self.renderer.GetActiveCamera()
            camera_was_changed = self._restore_main_camera_state(camera_state)
            if camera is not None:
                camera.Modified()
            render_window.Modified()
            if camera_was_changed:
                print("🎯 Intersect history: blocked an unintended main-view camera change")
        except Exception:
            return False

        # Bypass only the pending throttle; GPURenderManager performs its own
        # Qt/VTK lifetime validation before calling the original render method.
        manager = getattr(app, "gpu_render_manager", None)
        force_render = getattr(manager, "force_render", None)
        if callable(force_render):
            try:
                force_render()
            except Exception:
                return False
        else:
            # Compatibility path for builds that do not install GPURenderManager.
            try:
                widget.render()
            except Exception:
                return False

        # RenderWindow.Render updates VTK's buffers; QWidget.update schedules
        # the corresponding Qt paint after this timer callback returns.  This
        # prevents the backing store from leaving the native canvas black.
        try:
            widget.update()
        except Exception:
            return False
        return True

    def _status(self, msg, timeout=2500):
        try:
            self.app.statusBar().showMessage(msg, timeout)
        except Exception:
            pass
        print(f"🎯 {msg}")
