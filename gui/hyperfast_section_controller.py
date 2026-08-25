import numpy as np
import vtk
import time
from numba import jit, prange
import pyvista as pv


# ═════════════════════════════════════════════════════════════════════════
# ULTRA-FAST NUMBA-ACCELERATED GEOMETRY FUNCTIONS
# ═════════════════════════════════════════════════════════════════════════

@jit(nopython=True, parallel=True, cache=True)
def compute_distances_parallel(rel_points, dir_vec, perp):
    """⚡ BLAZING FAST parallel distance calculation using Numba JIT."""
    n = rel_points.shape[0]
    along = np.empty(n, dtype=np.float32)
    across = np.empty(n, dtype=np.float32)
    
    for i in prange(n):
        dx = rel_points[i, 0]
        dy = rel_points[i, 1]
        along[i] = dx * dir_vec[0] + dy * dir_vec[1]
        across[i] = dx * perp[0] + dy * perp[1]
    
    return along, across


@jit(nopython=True, parallel=True, cache=True)
def filter_section_parallel(along, across, length, half_width, buffer_width):
    """⚡ BLAZING FAST parallel filtering using Numba JIT."""
    n = along.shape[0]
    core_mask = np.empty(n, dtype=np.bool_)
    buffer_mask = np.empty(n, dtype=np.bool_)
    
    max_width = half_width + buffer_width
    
    for i in prange(n):
        in_length = (along[i] >= 0) and (along[i] <= length)
        abs_across = abs(across[i])
        
        core_mask[i] = in_length and (abs_across <= half_width)
        buffer_mask[i] = in_length and (abs_across <= max_width)
    
    return core_mask, buffer_mask


@jit(nopython=True, parallel=True, cache=True)
def bounding_box_filter(points_xy, bbox_min, bbox_max):
    """⚡ Fast bounding box pre-filter (no memory allocation for KD-tree!)"""
    n = points_xy.shape[0]
    mask = np.empty(n, dtype=np.bool_)
    
    for i in prange(n):
        x_ok = (points_xy[i, 0] >= bbox_min[0]) and (points_xy[i, 0] <= bbox_max[0])
        y_ok = (points_xy[i, 1] >= bbox_min[1]) and (points_xy[i, 1] <= bbox_max[1])
        mask[i] = x_ok and y_ok
    
    return mask


@jit(nopython=True, parallel=True, cache=True)
def apply_colors_parallel(classes, palette_codes, palette_colors, n_classes):
    """⚡ BLAZING FAST parallel color application using Numba JIT."""
    n = classes.shape[0]
    colors = np.empty((n, 3), dtype=np.uint8)
    
    for i in prange(n):
        cls = classes[i]
        found = False
        
        for j in range(n_classes):
            if palette_codes[j] == cls:
                colors[i, 0] = palette_colors[j, 0]
                colors[i, 1] = palette_colors[j, 1]
                colors[i, 2] = palette_colors[j, 2]
                found = True
                break
        
        if not found:
            colors[i, 0] = 128
            colors[i, 1] = 128
            colors[i, 2] = 128
    
    return colors


class HyperFastCrossSectionController:
    """
    ⚡⚡⚡ MEMORY-EFFICIENT cross-section for 100M+ point clouds.
    
    KEY OPTIMIZATION: NO KD-TREE!
    Instead uses:
    1. Tight bounding box pre-filter (Numba parallel)
    2. Chunked processing to avoid memory spikes
    3. Direct geometric filtering on candidates only
    4. Zero-copy where possible
    """
    
    def __init__(self, app):
        self.app = app
        self.P1 = None
        self.P2 = None
        self.half_width = None
        self.active_view = 0
        
        # Preview actors
        self.rubber_actor = None
        self.rubber_points = None
        self.rubber_poly = None
        
        # Section state
        self.last_mask = None
        self.section_points = None
        
        # ⚡ Aggressive optimization settings
        self._preview_downsample = 20
        self._min_query_interval = 0.1
        self._last_query_time = 0
        
        # ⚡ Chunk processing for massive datasets
        self._chunk_size = 10_000_000  # Process 10M points at a time
        
        # ⚡ Cache compiled functions on first use
        self._numba_warmed_up = False
        
        print("✅ HyperFastCrossSectionController initialized (Memory-Efficient, No KD-Tree)")
    
    # ═════════════════════════════════════════════════════════════════════
    # NUMBA WARMUP
    # ═════════════════════════════════════════════════════════════════════
    
    def _warmup_numba(self):
        """Warm up Numba JIT on first use (compile functions)."""
        if self._numba_warmed_up:
            return
        
        print("🔥 Warming up Numba JIT compiler...")
        start = time.time()
        
        # Compile functions with small test data
        test_rel = np.random.rand(1000, 2).astype(np.float32)
        test_dir = np.array([0.707, 0.707], dtype=np.float32)
        test_perp = np.array([-0.707, 0.707], dtype=np.float32)
        
        compute_distances_parallel(test_rel, test_dir, test_perp)
        
        test_along = np.random.rand(1000).astype(np.float32)
        test_across = np.random.rand(1000).astype(np.float32)
        filter_section_parallel(test_along, test_across, 10.0, 5.0, 2.0)
        
        test_bbox_pts = np.random.rand(100, 2).astype(np.float64)
        test_bbox_min = np.array([0.0, 0.0], dtype=np.float64)
        test_bbox_max = np.array([1.0, 1.0], dtype=np.float64)
        bounding_box_filter(test_bbox_pts, test_bbox_min, test_bbox_max)
        
        test_classes = np.random.randint(0, 10, 1000, dtype=np.int32)
        test_codes = np.arange(10, dtype=np.int32)
        test_colors = np.random.randint(0, 255, (10, 3), dtype=np.uint8)
        apply_colors_parallel(test_classes, test_codes, test_colors, 10)
        
        self._numba_warmed_up = True
        elapsed = (time.time() - start) * 1000
        print(f"   ✅ Numba warmed up in {elapsed:.0f}ms")
    
    # ═════════════════════════════════════════════════════════════════════
    # MEMORY-EFFICIENT SECTION COMPUTATION (NO KD-TREE!)
    # ═════════════════════════════════════════════════════════════════════
    
    def compute_section_hyperfast(self, P1, P2, half_width, buffer=2.0):
        """
        ⚡⚡⚡ MEMORY-EFFICIENT section computation.
        NO KD-TREE - uses tight bounding box + chunked processing.
        """
        self._warmup_numba()
        
        start = time.time()
        
        xyz = self.app.data["xyz"]
        n_points = len(xyz)
        
        print(f"   📊 Total points: {n_points:,}")
        
        # Direction vectors (float32 for speed)
        v = (P2[:2] - P1[:2]).astype(np.float32)
        length = np.linalg.norm(v)
        
        if length < 1e-9:
            return None, None, None, None
        
        dir_vec = (v / length).astype(np.float32)
        perp = np.array([-dir_vec[1], dir_vec[0]], dtype=np.float32)
        
        # ⚡ OPTIMIZATION 1: Compute tight bounding box around section
        max_dist = half_width + buffer
        
        # Four corners of the section rectangle
        corners = np.array([
            P1[:2] + perp * max_dist,
            P1[:2] - perp * max_dist,
            P2[:2] + perp * max_dist,
            P2[:2] - perp * max_dist
        ], dtype=np.float32)
        
        bbox_min = corners.min(axis=0)
        bbox_max = corners.max(axis=0)
        
        # Add 10% margin to be safe
        margin = 0.1 * (bbox_max - bbox_min)
        bbox_min -= margin
        bbox_max += margin
        
        print(f"   🔲 BBox: X=[{bbox_min[0]:.1f}, {bbox_max[0]:.1f}], Y=[{bbox_min[1]:.1f}, {bbox_max[1]:.1f}]")
        
        # ⚡ OPTIMIZATION 2: Chunked bounding box filtering (memory-safe)
        candidate_indices = []
        
        for chunk_start in range(0, n_points, self._chunk_size):
            chunk_end = min(chunk_start + self._chunk_size, n_points)
            # ✅ PERFORMANCE: Direct slice of float64 array (No memory allocation/cast)
            chunk_xy = xyz[chunk_start:chunk_end, :2]
            
            # Numba parallel bounding box test
            chunk_mask = bounding_box_filter(chunk_xy, bbox_min, bbox_max)
            
            # Collect indices
            chunk_indices = np.where(chunk_mask)[0] + chunk_start
            candidate_indices.append(chunk_indices)
            
            if chunk_end % (self._chunk_size * 5) == 0 or chunk_end == n_points:
                n_candidates = sum(len(ci) for ci in candidate_indices)
                print(f"   ⏳ Processed {chunk_end:,}/{n_points:,} ({100*chunk_end/n_points:.0f}%) - {n_candidates:,} candidates")
        
        # Combine all candidate indices
        candidate_idx = np.concatenate(candidate_indices) if candidate_indices else np.array([], dtype=np.int64)
        
        if len(candidate_idx) == 0:
            print("   ⚠️ No candidates found in bounding box")
            return None, None, None, None
        
        print(f"   ✅ BBox filtered: {len(candidate_idx):,} / {n_points:,} ({100*len(candidate_idx)/n_points:.1f}%)")
        
        # ⚡ OPTIMIZATION 3: Numba parallel distance calculation on candidates (float64 centering)
        # ✅ PERFORMANCE: Only performing float64 centering on the small candidate subset.
        candidate_points = xyz[candidate_idx, :2]
        rel = candidate_points - P1[:2]
        rel_f32 = rel.astype(np.float32)
        
        along, across = compute_distances_parallel(
            rel_f32, dir_vec, perp
        )
        
        # ⚡ OPTIMIZATION 4: Numba parallel geometric filtering
        core_local, buffer_local = filter_section_parallel(
            along, across, length, half_width, buffer
        )
        
        # ⚡ OPTIMIZATION 5: Build local (transformed) coordinates for classification & plotting
        # This prevents the interactor from having to re-project points during brushing.
        # Format: (along, across, elevation)
        all_points_local = np.column_stack([
            along, across, xyz[candidate_idx, 2]
        ]).astype(np.float32)

        core_mask = np.zeros(n_points, dtype=bool)
        buffer_mask = np.zeros(n_points, dtype=bool)
        
        core_mask[candidate_idx[core_local]] = True
        buffer_mask[candidate_idx[buffer_local]] = True
        
        # ⚡ Return BOTH world and local points to stay synchronized with regular controller
        core_points_world = xyz[core_mask]
        buffer_points_world = xyz[buffer_mask & ~core_mask]
        
        elapsed = (time.time() - start) * 1000
        print(f"   ⚡ Computed in {elapsed:.0f}ms: {len(core_points_world):,} core, {len(buffer_points_world):,} buffer")
        
        return core_points_world, buffer_points_world, core_mask, buffer_mask, all_points_local
    
    # ═════════════════════════════════════════════════════════════════════
    # PREVIEW RENDERING
    # ═════════════════════════════════════════════════════════════════════
    
    def draw_centerline(self, P1, P2):
        """Fast preview line with aggressive throttling."""
        now = time.time()
        if now - self._last_query_time < self._min_query_interval:
            return
        self._last_query_time = now
        
        if self.rubber_points is None or self.rubber_points.GetNumberOfPoints() < 2:
            self._init_rectangle(npoints=2)
            self.rubber_actor.GetProperty().SetLineStipplePattern(0xF0F0)
            self.rubber_actor.GetProperty().SetLineStippleRepeatFactor(1)
        
        line_points = [[P1[0], P1[1], P1[2]], [P2[0], P2[1], P2[2]]]
        
        for i, c in enumerate(line_points):
            self.rubber_points.SetPoint(i, c)
        
        self.rubber_points.Modified()
        self.rubber_poly.Modified()
        self.app.vtk_widget.render()
    
    def draw_rubber_rectangle(self, P1, P2, half_width):
        """Fast preview rectangle with aggressive throttling."""
        now = time.time()
        if now - self._last_query_time < self._min_query_interval:
            return
        self._last_query_time = now
        
        if self.rubber_points is None or self.rubber_points.GetNumberOfPoints() < 5:
            if self.rubber_actor:
                self.app.vtk_widget.renderer.RemoveActor(self.rubber_actor)
                self.rubber_actor = None
            self._init_rectangle(npoints=5)
        
        v = P2[:2] - P1[:2]
        if np.linalg.norm(v) < 1e-9:
            return
        
        dir_vec = v / np.linalg.norm(v)
        perp = np.array([-dir_vec[1], dir_vec[0]])
        
        c1 = P1[:2] + perp * half_width
        c2 = P2[:2] + perp * half_width
        c3 = P2[:2] - perp * half_width
        c4 = P1[:2] - perp * half_width
        
        corners = [
            [c1[0], c1[1], P1[2]],
            [c2[0], c2[1], P2[2]],
            [c3[0], c3[1], P2[2]],
            [c4[0], c4[1], P1[2]],
            [c1[0], c1[1], P1[2]],
        ]
        
        for i, c in enumerate(corners):
            self.rubber_points.SetPoint(i, c)
        
        self.rubber_points.Modified()
        self.rubber_poly.Modified()
        self.app.vtk_widget.render()
        
        self.P1, self.P2, self.half_width = P1, P2, half_width
    
    def _init_rectangle(self, npoints=5):
        """Initialize preview rectangle."""
        from vtkmodules.util import numpy_support as nps
        
        self.rubber_points = vtk.vtkPoints()
        self.rubber_points.SetData(nps.numpy_to_vtk(np.zeros((npoints, 3)), deep=True))
        
        self.rubber_poly = vtk.vtkPolyData()
        self.rubber_poly.SetPoints(self.rubber_points)
        
        lines = vtk.vtkCellArray()
        for i in range(npoints - 1):
            lines.InsertNextCell(2)
            lines.InsertCellPoint(i)
            lines.InsertCellPoint(i + 1)
        self.rubber_poly.SetLines(lines)
        
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(self.rubber_poly)
        
        self.rubber_actor = vtk.vtkActor()
        self.rubber_actor.SetMapper(mapper)
        self.rubber_actor.GetProperty().SetColor(1, 0, 1)
        self.rubber_actor.GetProperty().SetLineWidth(2)
        
        self.app.vtk_widget.renderer.AddActor(self.rubber_actor)
    
    # ═════════════════════════════════════════════════════════════════════
    # FINALIZE
    # ═════════════════════════════════════════════════════════════════════
    
    def finalize_section(self, P1, P2):
        """Compute and display section using hyper-fast method."""
        if self.half_width is None:
            return
        
        print(f"\n{'='*60}")
        print(f"⚡⚡⚡ FINALIZING CROSS-SECTION (MEMORY-EFFICIENT MODE)")
        
        self.finalize_rectangle()
        
        buffer = getattr(self.app, "section_buffer", 2.0)
        
        # ⚡ Use memory-efficient computation (now returns local points too)
        core_pts_w, buffer_pts_w, core_mask, buffer_mask, all_pts_local = self.compute_section_hyperfast(
            P1, P2, self.half_width, buffer
        )
        
        if core_pts_w is None or len(core_pts_w) == 0:
            print("⚠️ No points in section")
            print(f"{'='*60}\n")
            return
        
        # Store data
        view_index = self.active_view
        setattr(self.app, f'section_{view_index}_P1', P1)
        setattr(self.app, f'section_{view_index}_P2', P2)
        setattr(self.app, f'section_{view_index}_half_width', self.half_width)
        
        # ✅ SYNC: Store exactly what SectionController stores so interactor/GPU poke works
        setattr(self.app, f'section_{view_index}_core_points', core_pts_w)
        setattr(self.app, f'section_{view_index}_buffer_points', buffer_pts_w)
        setattr(self.app, f'section_{view_index}_core_mask', core_mask)
        setattr(self.app, f'section_{view_index}_buffer_mask', buffer_mask)
        setattr(self.app, f'section_{view_index}_combined_mask', buffer_mask) # buffer_mask IS combined in HyperFast
        setattr(self.app, f'section_{view_index}_points_transformed', all_pts_local)
        
        # ⚡ CRITICAL FIX: Populate global indices for undo/redo GPU patching
        global_idx = np.where(buffer_mask)[0]
        setattr(self.app, f'_section_{view_index}_global_indices', global_idx)
        
        # Global state (Backward Compat)
        self.app.section_core_mask = core_mask
        self.app.section_buffer_points = buffer_pts_w
        self.last_mask = buffer_mask
        
        # ✅ SYNC: section_points should be LOCAL (along, across, elev) for 2D interactor tools
        self.app.section_points = all_pts_local
        
        # ⚡ Plot with extreme optimization
        self._plot_section_hyperfast(core_pts_w, buffer_pts_w, core_mask, buffer_mask, view="side")
        
        print(f"{'='*60}\n")
    
    def finalize_rectangle(self):
        """Remove preview."""
        if self.rubber_actor:
            self.app.vtk_widget.renderer.RemoveActor(self.rubber_actor)
        self.rubber_actor = None
        self.rubber_points = None
        self.rubber_poly = None
        self.app.vtk_widget.render()
    
    # ═════════════════════════════════════════════════════════════════════
    # PLOTTING
    # ═════════════════════════════════════════════════════════════════════
    
    def _plot_section_hyperfast(self, core_points, buffer_points, core_mask, buffer_mask, view="side"):
        """⚡⚡⚡ HYPER-FAST plotting with Numba-accelerated coloring."""
        self.app.cross_view_mode = view
        
        vtk_widget = self._get_active_vtk()
        if vtk_widget is None:
            return
        
        print(f"🎯 Plotting section to View {self.active_view + 1}")
        
        # ⚡ EXTREME DOWNSAMPLING for huge sections
        total_points = len(core_points) + (len(buffer_points) if buffer_points is not None else 0)
        
        if total_points > 10_000_000:
            downsample = 10
        elif total_points > 5_000_000:
            downsample = 5
        elif total_points > 1_000_000:
            downsample = 3
        else:
            downsample = 1
        
        if downsample > 1:
            print(f"   ⚡ Downsampling {downsample}x: {total_points:,} → {total_points//downsample:,}")
        
        # Get palette
        view_palette = self._get_view_palette(self.active_view)
        visible_classes = self._get_visible_classes_from_palette(view_palette)
        
        # Clear old actors
        if hasattr(self, "_core_actor") and self._core_actor is not None:
            try:
                vtk_widget.remove_actor(self._core_actor, reset_camera=False)
            except Exception:
                pass
        if hasattr(self, "_buffer_actor") and self._buffer_actor is not None:
            try:
                vtk_widget.remove_actor(self._buffer_actor, reset_camera=False)
            except Exception:
                pass
        
        self._core_actor = None
        self._buffer_actor = None
        
        # ⚡ Plot core with Numba acceleration
        if len(core_points) > 0:
            start = time.time()
            
            core_classes = self.app.data["classification"][core_mask]
            
            # Visibility filter
            if visible_classes is not None:
                vis_mask = np.isin(core_classes, visible_classes)
                filtered_core_points = core_points[vis_mask]
                filtered_core_classes = core_classes[vis_mask]
            else:
                filtered_core_points = core_points
                filtered_core_classes = core_classes
            
            # Downsample
            if downsample > 1 and len(filtered_core_points) > 100:
                ds_idx = np.arange(0, len(filtered_core_points), downsample)
                filtered_core_points = filtered_core_points[ds_idx]
                filtered_core_classes = filtered_core_classes[ds_idx]
            
            if len(filtered_core_points) > 0:
                # ⚡ NUMBA-ACCELERATED COLOR APPLICATION
                core_colors = self._make_colors_numba(filtered_core_classes, view_palette)
                
                core_cloud = pv.PolyData(filtered_core_points)
                core_cloud["RGB"] = core_colors
                
                self._core_actor = vtk_widget.add_points(
                    core_cloud,
                    scalars="RGB",
                    rgb=True,
                    point_size=3,
                    render_points_as_spheres=True,
                    name=f"_section_{self.active_view}_unified"  # ⚡ Unified name for patching
                )
                
                elapsed = (time.time() - start) * 1000
                print(f"   ✅ Core plotted in {elapsed:.0f}ms ({len(filtered_core_points):,} points)")
        
        # ⚡ Plot buffer
        if buffer_points is not None and len(buffer_points) > 0:
            buf_mask = buffer_mask & ~core_mask
            buffer_classes = self.app.data["classification"][buf_mask]
            
            if visible_classes is not None:
                vis_mask = np.isin(buffer_classes, visible_classes)
                filtered_buffer_points = buffer_points[vis_mask]
                filtered_buffer_classes = buffer_classes[vis_mask]
            else:
                filtered_buffer_points = buffer_points
                filtered_buffer_classes = buffer_classes
            
            if downsample > 1 and len(filtered_buffer_points) > 100:
                ds_idx = np.arange(0, len(filtered_buffer_points), downsample)
                filtered_buffer_points = filtered_buffer_points[ds_idx]
                filtered_buffer_classes = filtered_buffer_classes[ds_idx]
            
            if len(filtered_buffer_points) > 0:
                buffer_colors = self._make_colors_numba(filtered_buffer_classes, view_palette)
                
                buf_cloud = pv.PolyData(filtered_buffer_points)
                buf_cloud["RGB"] = buffer_colors
                
                self._buffer_actor = vtk_widget.add_points(
                    buf_cloud,
                    scalars="RGB",
                    rgb=True,
                    point_size=2,
                    render_points_as_spheres=True
                )
        
        # Setup camera
        cam = vtk_widget.renderer.GetActiveCamera()
        cam.ParallelProjectionOn()
        
        if view == "side":
            vtk_widget.view_xz()
        elif view == "front":
            vtk_widget.view_yz()
        
        # Fit camera on first plot
        if not hasattr(self.app, "_section_initialized"):
            if len(core_points) > 0:
                bounds = [
                    core_points[:, 0].min(), core_points[:, 0].max(),
                    core_points[:, 1].min(), core_points[:, 1].max(),
                    core_points[:, 2].min(), core_points[:, 2].max()
                ]
                pad = 0.2 * max(bounds[1]-bounds[0], bounds[3]-bounds[2])
                bounds[0] -= pad; bounds[1] += pad
                bounds[2] -= pad; bounds[3] += pad
                vtk_widget.renderer.ResetCamera(bounds)
            self.app._section_initialized = True
        
        vtk_widget.renderer.ResetCameraClippingRange()
        vtk_widget.render()
        
        print(f"✅ Section rendered")
    
    def _make_colors_numba(self, classes, palette):
        """⚡ NUMBA-ACCELERATED color application."""
        if len(classes) == 0:
            return np.zeros((0, 3), dtype=np.uint8)
        
        # Convert palette to Numba-friendly arrays
        palette_codes = np.array(list(palette.keys()), dtype=np.int32)
        palette_colors = np.array(
            [palette[c].get("color", (128, 128, 128)) for c in palette_codes],
            dtype=np.uint8
        )
        
        # ⚡ Call Numba JIT function
        colors = apply_colors_parallel(
            classes.astype(np.int32),
            palette_codes,
            palette_colors,
            len(palette_codes)
        )
        
        return colors
    
    # ═════════════════════════════════════════════════════════════════════
    # HELPERS
    # ═════════════════════════════════════════════════════════════════════
    
    def _get_active_vtk(self):
        """Get VTK widget."""
        if hasattr(self.app, 'section_vtks') and self.active_view in self.app.section_vtks:
            return self.app.section_vtks[self.active_view]
        if hasattr(self.app, 'sec_vtk') and self.app.sec_vtk:
            return self.app.sec_vtk
        return None
    
    def _get_view_palette(self, view_index):
        """Get view palette."""
        try:
            target_slot = view_index + 1
            if hasattr(self.app, 'display_mode_dialog'):
                dialog = self.app.display_mode_dialog
                if hasattr(dialog, 'view_palettes') and target_slot in dialog.view_palettes:
                    return dialog.view_palettes[target_slot]

            if hasattr(self.app, 'view_palettes') and isinstance(self.app.view_palettes, dict):
                slot_palette = self.app.view_palettes.get(target_slot)
                if slot_palette:
                    return slot_palette

            # Do not leak slot-0 colors into cross-section slots by default.
            return {}
        except Exception:
            return {}
    
    def _get_visible_classes_from_palette(self, palette):
        """Get visible classes."""
        if not palette:
            return None
        visible = [code for code, info in palette.items() if info.get('show', True)]
        return visible if visible and len(visible) < len(palette) else None
    
    def clear(self):
        """Clear section."""
        vtk_widget = self._get_active_vtk()
        if vtk_widget:
            vtk_widget.clear()
        if self.rubber_actor:
            try:
                self.app.vtk_widget.renderer.RemoveActor(self.rubber_actor)
            except Exception:
                pass
        self.rubber_actor = None
        self.rubber_points = None
        self.rubber_poly = None
        self.app.vtk_widget.render()
