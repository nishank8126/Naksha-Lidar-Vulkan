"""
NakshaROIPreview — ROI-only partial-VBO brush preview actor.

Creates a SEPARATE small VTK actor containing only the k points inside the
current brush radius, colored with a preview tint.  On each mouse-move only
O(k) bytes are uploaded to the GPU; the main N-point cloud actor is untouched.

Usage
-----
    preview = NakshaROIPreview(vtk_widget)
    preview.update(xyz, indices, color)   # every mouse-move (O(k))
    preview.hide()                        # temporarily hide (pan, etc.)
    preview.destroy()                     # on brush release / cleanup
"""

import numpy as np
import vtk
from vtkmodules.util import numpy_support


class NakshaROIPreview:
    """
    Lightweight overlay actor for brush classification preview.

    Key design decisions
    --------------------
    - Separate actor from the main cloud — only k bytes dirty per frame.
    - Persistent numpy buffers owned by this object; VTK wraps them with
      deep=False so writes to the numpy arrays are immediately visible.
    - When k changes (brush moves to denser region) the actor is rebuilt;
      otherwise it is updated in-place in O(k) — no allocation.
    - `ImmediateModeRenderingOn()` is unavailable in modern VTK (>= 6.0),
      so we rely on the tiny actor size to guarantee fast GPU upload.
    """

    def __init__(self, vtk_widget):
        self._widget      = vtk_widget
        self._actor       = None
        self._mapper      = None
        self._poly        = None
        # Persistent owned numpy buffers (VTK wraps these with deep=False)
        self._pts_buf     = None   # (K*3,) float32 — flat XYZ
        self._rgb_buf     = None   # (K, 3) uint8
        # VTK array refs that wrap the above buffers
        self._vtk_pts_arr = None   # vtkFloatArray, 3-component
        self._vtk_rgb_arr = None   # vtkUnsignedCharArray, 3-component
        self._vtk_vpts    = None   # vtkPoints using _vtk_pts_arr
        self._n_pts       = 0

    # ──────────────────────────────────────────────────────────────────────
    def update(
        self,
        xyz: np.ndarray,
        indices: np.ndarray,
        color: tuple,
    ) -> None:
        """
        Refresh preview to show `indices` points colored with `color`.

        Complexity: O(k) — main cloud actor not touched.
        """
        if indices is None or len(indices) == 0:
            self.hide()
            return

        k       = len(indices)
        pts_flat = xyz[indices].astype(np.float32).ravel()   # (k*3,) C-contiguous

        if self._actor is None or k != self._n_pts:
            # Rebuild when point count changes (rare)
            self._rebuild(pts_flat, k, color)
        else:
            # ── In-place VBO poke — O(k), no allocation ───────────────────
            np.copyto(self._pts_buf, pts_flat)
            self._vtk_pts_arr.Modified()
            self._vtk_vpts.Modified()

            self._rgb_buf[:] = np.asarray(color, dtype=np.uint8)
            self._vtk_rgb_arr.Modified()

            self._poly.GetPointData().Modified()
            self._poly.Modified()
            if self._mapper:
                self._mapper.Modified()

        if self._actor:
            self._actor.VisibilityOn()

    def hide(self) -> None:
        """Hide the overlay without destroying it."""
        if self._actor:
            self._actor.VisibilityOff()

    def destroy(self) -> None:
        """Remove actor from renderer and release all VTK / numpy refs."""
        if self._actor:
            renderer = self._get_renderer()
            if renderer:
                try:
                    renderer.RemoveActor(self._actor)
                except Exception:
                    pass
        self._actor       = None
        self._mapper      = None
        self._poly        = None
        self._pts_buf     = None
        self._rgb_buf     = None
        self._vtk_pts_arr = None
        self._vtk_rgb_arr = None
        self._vtk_vpts    = None
        self._n_pts       = 0

    # ──────────────────────────────────────────────────────────────────────
    def _rebuild(self, pts_flat: np.ndarray, k: int, color: tuple) -> None:
        """Create (or replace) the preview actor for exactly k points."""
        # Remove existing actor
        if self._actor:
            renderer = self._get_renderer()
            if renderer:
                try:
                    renderer.RemoveActor(self._actor)
                except Exception:
                    pass

        # ── Owned numpy buffers (VTK wraps these; no copy) ────────────────
        self._pts_buf = pts_flat.copy()   # ensure C-contiguous, owned
        self._rgb_buf = np.tile(
            np.asarray(color, dtype=np.uint8), (k, 1)
        )   # (K, 3)

        # ── VTK arrays wrapping numpy buffers (deep=False) ─────────────────
        vtk_pts_arr = numpy_support.numpy_to_vtk(
            self._pts_buf, deep=False, array_type=vtk.VTK_FLOAT
        )
        vtk_pts_arr.SetNumberOfComponents(3)

        vpoints = vtk.vtkPoints()
        vpoints.SetData(vtk_pts_arr)

        vtk_rgb = numpy_support.numpy_to_vtk(self._rgb_buf, deep=False)
        vtk_rgb.SetName("RGB")

        # ── PolyData: one vertex cell per point ────────────────────────────
        poly = vtk.vtkPolyData()
        poly.SetPoints(vpoints)
        poly.GetPointData().SetScalars(vtk_rgb)

        id_arr        = np.arange(k, dtype=np.int64)
        cells_np      = np.empty(k * 2, dtype=np.int64)
        cells_np[0::2] = 1        # 1 point per cell
        cells_np[1::2] = id_arr
        verts = vtk.vtkCellArray()
        verts.SetCells(k, numpy_support.numpy_to_vtkIdTypeArray(cells_np, deep=True))
        poly.SetVerts(verts)

        # ── Mapper ─────────────────────────────────────────────────────────
        mapper = vtk.vtkOpenGLPolyDataMapper()
        mapper.SetInputData(poly)
        mapper.SetScalarModeToUsePointFieldData()
        mapper.SelectColorArray("RGB")
        mapper.ScalarVisibilityOn()

        # ── Actor ──────────────────────────────────────────────────────────
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetPointSize(7.0)   # slightly larger than cloud
        actor.GetProperty().LightingOff()

        renderer = self._get_renderer()
        if renderer:
            renderer.AddActor(actor)

        # ── Store refs for in-place updates ────────────────────────────────
        self._actor       = actor
        self._mapper      = mapper
        self._poly        = poly
        self._vtk_pts_arr = vtk_pts_arr
        self._vtk_rgb_arr = vtk_rgb
        self._vtk_vpts    = vpoints
        self._n_pts       = k

    def _get_renderer(self):
        w = self._widget
        if w is None:
            return None
        r = getattr(w, "renderer", None)
        if r is not None:
            return r
        if hasattr(w, "GetRenderer"):
            return w.GetRenderer()
        return None
