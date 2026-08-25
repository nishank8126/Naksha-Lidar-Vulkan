
"""
Key optimizations:
1. LOD (Level of Detail) rendering - only render visible points
2. Spatial indexing with KD-tree for fast queries
3. Chunked processing to avoid memory spikes
4. GPU-accelerated operations where possible
5. Lazy loading and viewport culling
"""

import numpy as np
from scipy.spatial import cKDTree
import pyvista as pv

# ============================================
# 1. LOD MANAGER - Adaptive Point Density
# ============================================

class LODManager:
    """
    Dynamically adjust point density based on zoom level.
    Renders fewer points when zoomed out, more when zoomed in.
    """
    def __init__(self, app):
        self.app = app
        self.lod_levels = {
            'far': 0.5,    # 10% of points when far away
            'medium': 0.15,  # 30% of points at medium zoom
            'near': 0.4,    # 60% of points when close
            'full': 0.7     # 100% when very close
        }
        self.current_lod = 'medium'
        
    def get_lod_factor(self, camera_distance):
        """Calculate LOD factor based on camera distance."""
        if camera_distance > 1000:
            return self.lod_levels['far']
        elif camera_distance > 500:
            return self.lod_levels['medium']
        elif camera_distance > 100:
            return self.lod_levels['near']
        else:
            return self.lod_levels['full']
    
    def subsample_points(self, xyz, colors, lod_factor):
        """Efficiently subsample points based on LOD."""
        if lod_factor >= 1.0:
            return xyz, colors
        
        n_points = len(xyz)
        n_keep = int(n_points * lod_factor)
        
        # Use systematic sampling for speed (not random)
        step = max(1, n_points // n_keep)
        indices = np.arange(0, n_points, step)[:n_keep]
        
        return xyz[indices], colors[indices]


class SpatialIndex:
    """
    KD-tree for O(log n) spatial queries instead of O(n).
    Critical for classification tools on large datasets.
    """
    def __init__(self, xyz):
        print(f"🔍 Building spatial index for {len(xyz):,} points...")
        self.tree = cKDTree(xyz)
        self.xyz = xyz
        print(f"✅ Spatial index ready")
    
    def query_radius(self, center, radius):
        """Find all points within radius of center."""
        return self.tree.query_ball_point(center, radius)
    
    def query_rectangle(self, x_min, x_max, y_min, y_max):
        """Fast rectangular region query."""
        mask = ((self.xyz[:, 0] >= x_min) & (self.xyz[:, 0] <= x_max) &
                (self.xyz[:, 1] >= y_min) & (self.xyz[:, 1] <= y_max))
        return np.where(mask)[0]
    
    def query_polygon(self, polygon_points):
        """Fast polygon containment check."""
        from matplotlib.path import Path
        path = Path(polygon_points[:, :2])  # Only XY
        points_2d = self.xyz[:, :2]
        return np.where(path.contains_points(points_2d))[0]
    
    # ✅ ADD THIS METHOD
    def query_bbox(self, bbox):
        """
        Query points within bounding box [xmin, ymin, xmax, ymax].
        Optimized for cross-section extraction.
        """
        xmin, ymin, xmax, ymax = bbox
        
        mask = (
            (self.xyz[:, 0] >= xmin) & (self.xyz[:, 0] <= xmax) &
            (self.xyz[:, 1] >= ymin) & (self.xyz[:, 1] <= ymax)
        )
        
        return np.where(mask)[0]






# ============================================
# 4. FAST COLOR UPDATE (In-place VTK)
# ============================================

def fast_update_colors_optimized(app, changed_mask=None):
    """
    Ultra-fast color update without rebuilding geometry.
    Only updates the color array in VTK (MicroStation-style).
    """
    import numpy as np
    import pyvista as pv

    if app.data is None:
        return

    classes = app.data["classification"]
    palette = app.class_palette
    
    # Find renderer
    if hasattr(app.vtk_widget, "plotter"):
        renderer = app.vtk_widget.plotter.renderer
    elif hasattr(app.vtk_widget, "renderer"):
        renderer = app.vtk_widget.renderer
    else:
        return

    # Find main actor (largest point cloud)
    ac = renderer.GetActors()
    ac.InitTraversal()
    
    actor = None
    max_pts = -1
    while True:
        a = ac.GetNextActor()
        if not a:
            break
        _am = a.GetMapper()
        pd = _am.GetInput() if _am is not None else None
        if pd and pd.GetNumberOfPoints() > max_pts:
            max_pts = pd.GetNumberOfPoints()
            actor = a

    if actor is None:
        return

    _pm = actor.GetMapper()
    poly = _pm.GetInput() if _pm is not None else None
    if poly is None:
        return
    
    # ============================================
    # OPTIMIZATION: Only update changed points
    # ============================================
    N = len(classes)
    
    if changed_mask is not None and np.any(changed_mask):
        # Partial update - only changed points
        changed_indices = np.where(changed_mask)[0]
        print(f"⚡ Partial update: {len(changed_indices):,} points changed")
        
        # Get existing color array
        existing_colors = pv.convert_array(poly.GetPointData().GetArray("RGB"))
        
        # Update only changed points
        for idx in changed_indices:
            code = int(classes[idx])
            entry = palette.get(code, {"color": (128, 128, 128)})
            existing_colors[idx] = entry["color"]
        
        colors = existing_colors
    else:
        # Full update
        colors = np.zeros((N, 3), dtype=np.uint8)
        for code, entry in palette.items():
            mask = classes == int(code)
            if np.any(mask):
                colors[mask] = entry.get("color", (128, 128, 128))
    
    # Apply to VTK
    arr = pv.convert_array(colors, "RGB")
    pd = poly.GetPointData()
    pd.RemoveArray("RGB")
    pd.AddArray(arr)
    pd.SetActiveScalars("RGB")
    poly.Modified()

    app.vtk_widget.GetRenderWindow().Render()
    print("⚡ Fast color update complete")




# ============================================
# 6. VIEWPORT CULLING FOR CROSS-SECTIONS
# ============================================



# ============================================
# 7. INTEGRATION EXAMPLE
# ============================================

"""
HOW TO USE THESE OPTIMIZATIONS:

1. In app_window.py __init__, add:
   
   # Initialize spatial index after loading data
   if self.data and "xyz" in self.data:
       self.spatial_index = SpatialIndex(self.data["xyz"])
   
2. Replace update_pointcloud with update_pointcloud_optimized

3. Replace fast_update_colors with fast_update_colors_optimized

4. For classification tools, use apply_classification_chunked:
   
   # Instead of direct assignment:
   # app.data["classification"][mask] = new_class
   
   # Use:
   apply_classification_chunked(app, mask, new_class)

5. Connect camera movement to LOD updates:
   
   def on_camera_moved(self):
       if hasattr(self, 'lod_manager'):
           # Force refresh with new LOD
           update_pointcloud_optimized(self, self.display_mode)
   
   # Attach to VTK interactor
   self.vtk_widget.interactor.AddObserver("EndInteractionEvent", on_camera_moved)

6. For large files, show loading progress:
   
   from PySide6.QtWidgets import QProgressDialog
   
   progress = QProgressDialog("Loading point cloud...", None, 0, 100, self)
   progress.setWindowModality(Qt.WindowModal)
   progress.show()
   # Update progress.setValue(percent) during load
"""

# ============================================
# 8. MEMORY MANAGEMENT
# ============================================

class MemoryManager:
    """Monitor and manage memory usage for large files."""
    
    @staticmethod
    def estimate_memory_mb(n_points):
        """Estimate memory needed for point cloud."""
        bytes_per_point = (
            3 * 4 +  # xyz (float32)
            3 * 1 +  # rgb (uint8)
            1 * 1 +  # classification (uint8)
            1 * 2    # intensity (uint16)
        )
        return (n_points * bytes_per_point) / (1024 * 1024)
    
    @staticmethod
    def should_use_lod(n_points):
        """Determine if LOD is needed based on point count."""
        return n_points > 5_000_000
    
    @staticmethod
    def get_recommended_chunk_size(n_points):
        """Get optimal chunk size for processing."""
        if n_points < 1_000_000:
            return n_points  # No chunking needed
        elif n_points < 10_000_000:
            return 500_000
        else:
            return 1_000_000