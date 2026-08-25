"""
SelectionManager — MicroStation-style centralized selection set.

Single source of truth for "which digitized elements are currently selected".
Sits beside DigitizeManager.drawings without modifying it; mutation hooks
(_remove_drawing, clear_drawings, undo restore, clear_project) call
notify_drawings_changed() to purge stale handles.

Design notes:
- Drawings are dicts; we key by id(drawing) and keep a parallel reverse map
  so a deleted drawing can never reappear via dangling reference.
- All highlight/unhighlight goes through DigitizeManager's existing
  _highlight_line / _unhighlight_line so visuals stay consistent with the
  legacy single-click path.
- Operates in four modes mirroring MicroStation: NEW, ADD, SUBTRACT, INVERT.
- Polygon-closing expansion: when selected line segments form part of a
  closed polygon, the remaining boundary segments are automatically included
  in the selection (GIS digitizing behavior).
"""

from collections import defaultdict
from PySide6.QtCore import QObject, Signal


class SelectionMode:
    NEW = "new"
    ADD = "add"
    SUBTRACT = "subtract"
    INVERT = "invert"


class SelectionManager(QObject):
    """Centralized selection state for digitized drawings."""

    selection_changed = Signal(int)

    def __init__(self, digitizer):
        super().__init__()
        self._digitizer = digitizer
        self._selected_ids = set()
        self._id_to_drawing = {}

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------
    def count(self):
        return len(self._selected_ids)

    def is_empty(self):
        return not self._selected_ids

    def contains(self, drawing):
        return drawing is not None and id(drawing) in self._selected_ids

    def _live_drawings(self):
        drawings = list(getattr(self._digitizer, 'drawings', None) or [])
        curve_tool = getattr(getattr(self._digitizer, 'app', None), 'curve_tool', None)
        if curve_tool is not None:
            for curve_data in list(getattr(curve_tool, 'finalized_actors', []) or []):
                if hasattr(self._digitizer, "_get_drawing_coords"):
                    coords = self._digitizer._get_drawing_coords(curve_data)
                elif isinstance(curve_data, dict):
                    coords = curve_data.get("coords")
                    if coords is None:
                        coords = curve_data.get("interpolated")
                else:
                    coords = []
                if coords is not None and len(coords) > 0:
                    if not any(curve_data is drawing for drawing in drawings):
                        drawings.append(curve_data)
        return drawings

    def get(self):
        """Return a list of currently-selected drawings (live references)."""
        drawings = self._live_drawings()
        live_ids = {id(d) for d in drawings}
        return [
            d for did, d in self._id_to_drawing.items()
            if did in live_ids
        ]

    # ------------------------------------------------------------------
    # Mutation API (mode-aware)
    # ------------------------------------------------------------------
    def apply(self, picked, mode=SelectionMode.NEW):
        """Apply a pick result against the current selection set.

        picked: iterable of drawing dicts (may be empty)
        mode:   NEW | ADD | SUBTRACT | INVERT
        """
        picked = [d for d in (picked or []) if d is not None]

        if mode == SelectionMode.NEW:
            self.clear(emit=False)
            for d in picked:
                self._add_one(d)
        elif mode == SelectionMode.ADD:
            for d in picked:
                self._add_one(d)
        elif mode == SelectionMode.SUBTRACT:
            for d in picked:
                self._remove_one(d)
        elif mode == SelectionMode.INVERT:
            for d in picked:
                if id(d) in self._selected_ids:
                    self._remove_one(d)
                else:
                    self._add_one(d)
        else:
            return

        if mode in (SelectionMode.NEW, SelectionMode.ADD) and self._selected_ids:
            self._expand_to_closed_polygons()

        self._emit()

    def select_all(self, predicate=None):
        """Select every drawing (optionally filtered by predicate(drawing) -> bool)."""
        self.clear(emit=False)
        drawings = self._live_drawings()
        for d in list(drawings):
            if predicate is None or predicate(d):
                self._add_one(d)
        self._emit()

    def invert_all(self):
        """Flip selected/unselected across every drawing."""
        drawings = self._live_drawings()
        all_drawings = list(drawings)
        currently = self.get()
        currently_set = {id(d) for d in currently}
        self.clear(emit=False)
        for d in all_drawings:
            if id(d) not in currently_set:
                self._add_one(d)
        self._emit()

    def clear(self, emit=True):
        """Deselect everything and remove highlights.

        Clears internal state first so that even if an unhighlight call raises,
        the selection set is already empty and consistent.
        """
        self._cleanup_segment_overlays()
        drawings_to_unhighlight = list(self._id_to_drawing.values())
        # Clear state atomically before touching VTK actors
        self._selected_ids.clear()
        self._id_to_drawing.clear()
        # Now unhighlight — failures here are cosmetic, not state-corrupting
        for d in drawings_to_unhighlight:
            self._unhighlight_safe(d)
        if emit:
            self._emit()

    # ------------------------------------------------------------------
    # Notifications from external mutators
    # ------------------------------------------------------------------
    def notify_drawing_removed(self, drawing):
        """Called by DigitizeManager._remove_drawing after geometry teardown."""
        if drawing is None:
            return
        did = id(drawing)
        if did in self._selected_ids:
            self._selected_ids.discard(did)
            self._id_to_drawing.pop(did, None)
            self._emit()

    def notify_drawings_changed(self):
        """Drawings list mutated wholesale (clear_drawings, undo restore,
        clear_project). Purge any ids that are no longer alive."""
        drawings = self._live_drawings()
        live = {id(d) for d in drawings}
        stale = self._selected_ids - live
        if not stale:
            return
        for did in stale:
            self._id_to_drawing.pop(did, None)
        self._selected_ids -= stale
        self._emit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _add_one(self, drawing):
        did = id(drawing)
        if did in self._selected_ids:
            return
        drawings = self._live_drawings()
        try:
            found = any(d is drawing for d in drawings)
            if not found:
                print(f"⚠️ Drawing is no longer live, skipping selection")
                return
        except Exception as e:
            print(f"⚠️ Error checking drawing membership: {e}")
            return
        self._selected_ids.add(did)
        self._id_to_drawing[did] = drawing
        self._highlight_safe(drawing)

    def _remove_one(self, drawing):
        did = id(drawing)
        if did not in self._selected_ids:
            return
        self._selected_ids.discard(did)
        self._id_to_drawing.pop(did, None)
        self._unhighlight_safe(drawing)

    def _highlight_safe(self, drawing):
        try:
            if hasattr(self._digitizer, "_highlight_line"):
                self._digitizer._highlight_line(drawing)
        except Exception as e:
            print(f"⚠️ SelectionManager highlight failed: {e}")

    def _unhighlight_safe(self, drawing):
        try:
            if hasattr(self._digitizer, "_unhighlight_line"):
                self._digitizer._unhighlight_line(drawing)
        except Exception as e:
            print(f"⚠️ SelectionManager unhighlight failed: {e}")

    def _emit(self):
        try:
            self.selection_changed.emit(len(self._selected_ids))
        except Exception:
            pass
        try:
            renderer = getattr(self._digitizer, 'renderer', None)
            vtk_widget = getattr(getattr(self._digitizer, 'app', None), 'vtk_widget', None)
            if renderer is not None:
                renderer.Modified()
            if vtk_widget is not None:
                vtk_widget.render()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # GIS-style polygon-closing expansion + segment overlay support
    # ------------------------------------------------------------------
    _SNAP_PRECISION = 6

    @staticmethod
    def _snap_coord(coord):
        return (round(coord[0], SelectionManager._SNAP_PRECISION),
                round(coord[1], SelectionManager._SNAP_PRECISION),
                round(coord[2] if len(coord) > 2 else 0, SelectionManager._SNAP_PRECISION))

    def _cleanup_segment_overlays(self):
        """Remove all temporary overlay actors (polygon-closing highlights)."""
        ov_actors = getattr(self, '_overlay_actors', None)
        if not ov_actors:
            return
        renderer = getattr(self._digitizer, 'overlay_renderer', None)
        for actor in ov_actors:
            try:
                if renderer and actor:
                    renderer.RemoveActor(actor)
            except Exception:
                pass
        self._overlay_actors.clear()

    def _create_segment_overlay_actor(self, p1_world, p2_world):
        """Create a visible highlight overlay for a single line segment.

        Returns the actor so it can be tracked and later removed.
        The actor is added to the overlay renderer.
        """
        import vtk
        points = vtk.vtkPoints()
        points.InsertNextPoint(float(p1_world[0]), float(p1_world[1]),
                               float(p1_world[2] if len(p1_world) > 2 else 0))
        points.InsertNextPoint(float(p2_world[0]), float(p2_world[1]),
                               float(p2_world[2] if len(p2_world) > 2 else 0))
        poly = vtk.vtkPolyLine()
        poly.GetPointIds().SetNumberOfIds(2)
        poly.GetPointIds().SetId(0, 0)
        poly.GetPointIds().SetId(1, 1)
        ca = vtk.vtkCellArray()
        ca.InsertNextCell(poly)
        pd = vtk.vtkPolyData()
        pd.SetPoints(points)
        pd.SetLines(ca)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(pd)
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(0.0, 1.0, 1.0)
        actor.GetProperty().SetLineWidth(5)
        actor.PickableOff()

        renderer = getattr(self._digitizer, 'overlay_renderer', None)
        if renderer is not None:
            renderer.AddActor(actor)
        return actor

    def _expand_to_closed_polygons(self):
        """When selected segments form an open chain, find the missing
        boundary segment(s) through unselected drawings.

        For multi-segment drawings (e.g. smartline rectangle) only a visual
        overlay is created for the shared segment - the parent drawing is
        NOT added to the selection.
        """
        # Clean up any overlays from a previous run
        self._cleanup_segment_overlays()

        drawings = getattr(self._digitizer, 'drawings', None)
        if not drawings:
            return

        selected_ids = set(self._selected_ids)

        # 1. Build segment graph from ALL drawings
        #    segments[idx] = (snapped_p1, snapped_p2, original_p1, original_p2)
        segments = []
        seg_to_drawing = {}
        graph = defaultdict(list)

        for d in drawings:
            coords = d.get('coords') or []
            if len(coords) < 2:
                continue
            for i in range(len(coords) - 1):
                p1_snap = self._snap_coord(coords[i])
                p2_snap = self._snap_coord(coords[i + 1])
                if p1_snap == p2_snap:
                    continue
                idx = len(segments)
                segments.append((p1_snap, p2_snap, coords[i], coords[i + 1]))
                seg_to_drawing[idx] = d
                graph[p1_snap].append((p2_snap, idx))
                graph[p2_snap].append((p1_snap, idx))

        if not segments:
            return

        # 2. Mark selected segment indices
        selected_seg = set()
        for idx, d in seg_to_drawing.items():
            if id(d) in selected_ids:
                selected_seg.add(idx)
        if not selected_seg:
            return

        # 3. Connected components of selected segments
        visited_segs = set()
        additional_drawings = []   # drawings to select normally
        overlay_segments = []      # (original_p1, original_p2)

        for start_idx in selected_seg:
            if start_idx in visited_segs:
                continue

            component = set()
            queue = [start_idx]
            while queue:
                idx = queue.pop(0)
                if idx in component:
                    continue
                component.add(idx)
                visited_segs.add(idx)
                p1, p2, _, _ = segments[idx]
                for _, nidx in graph[p1]:
                    if nidx in selected_seg and nidx not in component:
                        queue.append(nidx)
                for _, nidx in graph[p2]:
                    if nidx in selected_seg and nidx not in component:
                        queue.append(nidx)

            # 4. Vertex degrees -> open endpoints
            degree = defaultdict(int)
            for idx in component:
                p1, p2, _, _ = segments[idx]
                degree[p1] += 1
                degree[p2] += 1

            open_endpoints = [p for p, deg in degree.items() if deg == 1]
            if len(open_endpoints) != 2:
                continue

            ep1, ep2 = open_endpoints

            # 5. BFS for closing path through unselected segments
            bfs_vis = {ep1}
            bfs_q = [(ep1, [], 0)]
            found = None
            MAX_DEPTH = 50

            while bfs_q and found is None:
                cur, path, depth = bfs_q.pop(0)
                if depth >= MAX_DEPTH:
                    continue
                for nbr, sidx in graph[cur]:
                    if sidx in selected_seg:
                        continue
                    new_path = path + [sidx]
                    if nbr == ep2:
                        found = new_path
                        break
                    if nbr not in bfs_vis:
                        bfs_vis.add(nbr)
                        bfs_q.append((nbr, new_path, depth + 1))

            if found is None:
                continue

            # 6. Categorise each closing segment
            for sidx in found:
                d = seg_to_drawing[sidx]
                if id(d) in selected_ids:
                    continue
                coords = d.get('coords') or []
                if len(coords) <= 2:
                    additional_drawings.append(d)
                else:
                    _, _, p1_orig, p2_orig = segments[sidx]
                    overlay_segments.append((p1_orig, p2_orig))

        # 7. Apply: select drawings + create overlays
        for d in additional_drawings:
            self._add_one(d)

        if overlay_segments:
            actors = []
            for p1, p2 in overlay_segments:
                actor = self._create_segment_overlay_actor(p1, p2)
                if actor is not None:
                    actors.append(actor)
            if actors:
                if not hasattr(self, '_overlay_actors'):
                    self._overlay_actors = []
                self._overlay_actors.extend(actors)
