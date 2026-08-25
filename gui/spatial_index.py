"""
Optimized spatial indexing for ultra-fast spatial queries.
Supports: KD-tree, Octree, and grid-based acceleration structures.

Task-2 upgrades (persistent cache + bbox-culled section query):
  - _INDEX_CACHE: module-level singleton — KDTree built ONCE per file load,
    reused across all brush/section/rectangle operations.
  - query_section_box(): vectorized bbox query into cached tree with no Python loop.
  - invalidate_cache(): called by app on file load/close — safe single reset point.
"""

import numpy as np
from scipy.spatial import cKDTree

# ── Persistent global index cache ────────────────────────────────────────────
# Key insight: for a 50M-point cloud the KDTree build takes ~3-8 s and 1-2 GB RAM.
# Rebuilding it on every section slice or brush drag is the #1 source of "heaviness".
# This cache ensures it is built exactly ONCE per file load.
_INDEX_CACHE: dict = {
    "index":       None,    # SpatialIndex instance
    "point_id":    None,    # id() of the xyz array — detects file reload
    "n_points":    0,
}


def get_or_build_index(xyz: np.ndarray, method: str = "auto") -> "SpatialIndex":
    """
    Return the cached SpatialIndex for xyz, building it only when the array
    identity or content fingerprint changes (i.e. a new file was loaded).

    id(xyz) alone is unsafe: CPython reuses addresses after an array is freed,
    causing false cache hits across file loads. A cheap 3-point content
    fingerprint is added as a secondary guard.
    """
    arr_id = id(xyz)
    n      = len(xyz)

    def _fp(a):
        if n == 0:
            return (0.0, 0.0, 0.0)
        mid = n // 2
        return (float(a[0, 0]), float(a[mid, 0]), float(a[-1, 0]))

    if (
        _INDEX_CACHE["index"]    is not None
        and _INDEX_CACHE["point_id"]  == arr_id
        and _INDEX_CACHE["n_points"]  == n
        and _INDEX_CACHE.get("fp")    == _fp(xyz)
    ):
        return _INDEX_CACHE["index"]

    idx = build_spatial_index_auto(xyz, method)
    _INDEX_CACHE["index"]    = idx
    _INDEX_CACHE["point_id"] = arr_id
    _INDEX_CACHE["n_points"] = n
    _INDEX_CACHE["fp"]       = _fp(xyz)
    return idx


def invalidate_cache() -> None:
    """
    Flush the persistent index.  Call this in app on file load/close.
    Safe to call multiple times — idempotent.
    """
    _INDEX_CACHE["index"]    = None
    _INDEX_CACHE["point_id"] = None
    _INDEX_CACHE["n_points"] = 0


class SpatialIndex:
    """
    High-performance spatial index with multiple backend options.
    """
    
    def __init__(self, points, method='kdtree', grid_size=50):
        """
        Initialize spatial index.
        
        Args:
            points: Nx3 array of XYZ coordinates
            method: 'kdtree', 'grid', or 'octree'
            grid_size: For grid method, cells per dimension
        """
        self.points = points
        self.method = method
        self.grid_size = grid_size
        
        if method == 'kdtree':
            self._build_kdtree()
        elif method == 'grid':
            self._build_grid(grid_size)
        elif method == 'octree':
            self._build_octree()
        else:
            raise ValueError(f"Unknown method: {method}")
    
    def _build_kdtree(self):
        """Build KD-tree (best for general queries)."""
        self.tree = cKDTree(self.points, leafsize=32, balanced_tree=True)
    
    def _build_grid(self, grid_size):
        """Build uniform grid (best for uniform distributions)."""
        # Compute bounds
        mins = self.points.min(axis=0)
        maxs = self.points.max(axis=0)
        
        self.grid_mins = mins
        self.grid_maxs = maxs
        self.grid_size = grid_size
        
        # Compute cell sizes
        ranges = maxs - mins
        self.cell_sizes = ranges / grid_size
        
        # Assign points to grid cells
        cell_indices = ((self.points - mins) / self.cell_sizes).astype(np.int32)
        cell_indices = np.clip(cell_indices, 0, grid_size - 1)
        
        # Build grid dictionary
        self.grid = {}
        for i, cell_idx in enumerate(cell_indices):
            cell_key = tuple(cell_idx)
            if cell_key not in self.grid:
                self.grid[cell_key] = []
            self.grid[cell_key].append(i)
        
        # Convert lists to numpy arrays for faster indexing
        for key in self.grid:
            self.grid[key] = np.array(self.grid[key], dtype=np.int32)
    
    def _build_octree(self):
        """Build octree (best for non-uniform distributions)."""
        # Simple octree implementation
        self.octree = OctreeNode(self.points, np.arange(len(self.points)))
    
    def query_ball_point(self, center, radius):
        """
        Find all points within radius of center.
        
        Returns: array of point indices
        """
        if self.method == 'kdtree':
            return self.tree.query_ball_point(center, radius)
        
        elif self.method == 'grid':
            return self._query_grid_ball(center, radius)
        
        elif self.method == 'octree':
            return self.octree.query_ball(center, radius)
    
    def _query_grid_ball(self, center, radius):
        """Grid-based ball query."""
        # Find grid cells that overlap with sphere
        min_cell = ((center - radius - self.grid_mins) / self.cell_sizes).astype(np.int32)
        max_cell = ((center + radius - self.grid_mins) / self.cell_sizes).astype(np.int32)
        
        min_cell = np.clip(min_cell, 0, self.grid_size - 1)
        max_cell = np.clip(max_cell, 0, self.grid_size - 1)
        
        # Collect candidates from overlapping cells
        candidates = []
        for x in range(min_cell[0], max_cell[0] + 1):
            for y in range(min_cell[1], max_cell[1] + 1):
                for z in range(min_cell[2], max_cell[2] + 1):
                    cell_key = (x, y, z)
                    if cell_key in self.grid:
                        candidates.extend(self.grid[cell_key])
        
        if not candidates:
            return np.array([], dtype=np.int32)
        
        candidates = np.array(candidates)
        
        # Filter by actual distance
        dists = np.linalg.norm(self.points[candidates] - center, axis=1)
        return candidates[dists <= radius]
    
    def query_rectangle(self, x_min, x_max, y_min, y_max, z_min=None, z_max=None):
        """
        Find all points within rectangular bounds.
        """
        if self.method == 'kdtree':
            # KD-tree rectangular query
            if z_min is None:
                z_min = self.points[:, 2].min()
            if z_max is None:
                z_max = self.points[:, 2].max()
            
            # Use vectorized bounds check (faster than tree for rectangles)
            mask = ((self.points[:, 0] >= x_min) & (self.points[:, 0] <= x_max) &
                    (self.points[:, 1] >= y_min) & (self.points[:, 1] <= y_max) &
                    (self.points[:, 2] >= z_min) & (self.points[:, 2] <= z_max))
            
            return np.flatnonzero(mask)
        
        elif self.method == 'grid':
            return self._query_grid_rectangle(x_min, x_max, y_min, y_max, z_min, z_max)
        
        elif self.method == 'octree':
            return self.octree.query_rectangle(x_min, x_max, y_min, y_max, z_min, z_max)
    
    def _query_grid_rectangle(self, x_min, x_max, y_min, y_max, z_min, z_max):
        """Grid-based rectangle query."""
        if z_min is None:
            z_min = self.grid_mins[2]
        if z_max is None:
            z_max = self.grid_maxs[2]
        
        # Find overlapping grid cells
        min_cell = ((np.array([x_min, y_min, z_min]) - self.grid_mins) / self.cell_sizes).astype(np.int32)
        max_cell = ((np.array([x_max, y_max, z_max]) - self.grid_mins) / self.cell_sizes).astype(np.int32)
        
        min_cell = np.clip(min_cell, 0, self.grid_size - 1)
        max_cell = np.clip(max_cell, 0, self.grid_size - 1)
        
        # Collect candidates
        candidates = []
        for x in range(min_cell[0], max_cell[0] + 1):
            for y in range(min_cell[1], max_cell[1] + 1):
                for z in range(min_cell[2], max_cell[2] + 1):
                    cell_key = (x, y, z)
                    if cell_key in self.grid:
                        candidates.extend(self.grid[cell_key])
        
        if not candidates:
            return np.array([], dtype=np.int32)
        
        candidates = np.array(candidates)
        
        # Filter by actual bounds
        pts = self.points[candidates]
        mask = ((pts[:, 0] >= x_min) & (pts[:, 0] <= x_max) &
                (pts[:, 1] >= y_min) & (pts[:, 1] <= y_max) &
                (pts[:, 2] >= z_min) & (pts[:, 2] <= z_max))
        
        return candidates[mask]


class OctreeNode:
    """
    Simple octree for spatial queries.
    """
    
    def __init__(self, points, indices, max_depth=10, max_points=1000, depth=0):
        self.points = points
        self.indices = indices
        self.children = None
        self.depth = depth
        
        # Compute bounds
        if len(points) > 0:
            self.min_bounds = points.min(axis=0)
            self.max_bounds = points.max(axis=0)
            self.center = (self.min_bounds + self.max_bounds) / 2
        
        # Subdivide if needed
        if len(points) > max_points and depth < max_depth:
            self._subdivide(max_depth, max_points)
    
    def _subdivide(self, max_depth, max_points):
        """Split node into 8 children."""
        self.children = []
        
        # Create 8 octants
        for i in range(8):
            # Determine octant bounds
            x_min = self.min_bounds[0] if (i & 1) == 0 else self.center[0]
            x_max = self.center[0] if (i & 1) == 0 else self.max_bounds[0]
            
            y_min = self.min_bounds[1] if (i & 2) == 0 else self.center[1]
            y_max = self.center[1] if (i & 2) == 0 else self.max_bounds[1]
            
            z_min = self.min_bounds[2] if (i & 4) == 0 else self.center[2]
            z_max = self.center[2] if (i & 4) == 0 else self.max_bounds[2]
            
            # Find points in this octant
            mask = ((self.points[:, 0] >= x_min) & (self.points[:, 0] < x_max) &
                    (self.points[:, 1] >= y_min) & (self.points[:, 1] < y_max) &
                    (self.points[:, 2] >= z_min) & (self.points[:, 2] < z_max))
            
            octant_indices = self.indices[mask]
            octant_points = self.points[mask]
            
            if len(octant_points) > 0:
                child = OctreeNode(octant_points, octant_indices, 
                                  max_depth, max_points, self.depth + 1)
                self.children.append(child)
    
    def query_ball(self, center, radius):
        """Find points within radius of center."""
        if self.children is None:
            # Leaf node - check all points
            dists = np.linalg.norm(self.points - center, axis=1)
            return self.indices[dists <= radius]
        
        # Check children
        results = []
        for child in self.children:
            # Check if sphere overlaps child bounds
            if self._sphere_intersects_box(center, radius, child.min_bounds, child.max_bounds):
                results.extend(child.query_ball(center, radius))
        
        return np.array(results, dtype=np.int32)
    
    def _sphere_intersects_box(self, center, radius, box_min, box_max):
        """Check if sphere intersects axis-aligned box."""
        # Find closest point on box to sphere center
        closest = np.clip(center, box_min, box_max)
        dist = np.linalg.norm(center - closest)
        return dist <= radius


def build_spatial_index_auto(points, method: str = 'auto') -> "SpatialIndex":
    """
    Automatically choose best spatial index method based on point count.
    Prefer calling get_or_build_index() instead — it avoids redundant builds.
    """
    n = len(points)
    if method == 'auto':
        if n < 1_000_000:
            method = 'kdtree'
        elif n < 10_000_000:
            method = 'grid'
        else:
            method = 'kdtree'   # cKDTree scales to 100M+ with leafsize=32
    return SpatialIndex(points, method=method)


# ── Bbox-culled section slice (Task-2: "Lazy Loading") ────────────────────────

def query_section_box(
    xyz:        np.ndarray,
    x_min: float, x_max: float,
    y_min: float, y_max: float,
    depth: float = 0.5,
    use_cache: bool = True,
) -> np.ndarray:
    """
    Return indices of all points inside a cross-section selection box.

    This is the core of the "Lazy Loading" section approach:
    - Uses the persistent cached KDTree (built once, not per-section).
    - For the KDTree path: queries a bounding-box candidate set in C,
      then applies an exact vectorized NumPy filter — zero Python loops.
    - For datasets where the tree is not yet built, falls back to pure
      vectorized NumPy (still O(N) but fully in C via boolean indexing).

    Parameters
    ----------
    xyz     : (N, 3) float32/float64 point array
    x_min/x_max, y_min/y_max : section corridor bounds in world coords
    depth   : half-thickness in Z (ignored when z_min/z_max not applicable)
    use_cache : True → use get_or_build_index(), False → vectorized-only fallback

    Returns
    -------
    indices : 1-D int64 array of matching point indices
    """
    if use_cache:
        idx_obj = get_or_build_index(xyz)
        if idx_obj.method == 'kdtree':
            # KDTree path: query_ball_point on box center + half-diagonal radius
            # gives O(log N) candidates; exact filter is O(k), k << N.
            cx = (x_min + x_max) * 0.5
            cy = (y_min + y_max) * 0.5
            hw = (x_max - x_min) * 0.5
            hh = (y_max - y_min) * 0.5
            radius = (hw**2 + hh**2 + depth**2) ** 0.5  # bounding sphere radius

            candidates = np.asarray(
                idx_obj.tree.query_ball_point([cx, cy, 0.0], radius),
                dtype=np.int64,
            )
            if candidates.size == 0:
                return candidates

            # Exact AABB filter on candidates — vectorized, O(k)
            pts = xyz[candidates]
            mask = (
                (pts[:, 0] >= x_min) & (pts[:, 0] <= x_max) &
                (pts[:, 1] >= y_min) & (pts[:, 1] <= y_max)
            )
            return candidates[mask]

    # Fallback: pure vectorized NumPy — still O(N) but no Python loop
    mask = (
        (xyz[:, 0] >= x_min) & (xyz[:, 0] <= x_max) &
        (xyz[:, 1] >= y_min) & (xyz[:, 1] <= y_max)
    )
    return np.flatnonzero(mask).astype(np.int64)