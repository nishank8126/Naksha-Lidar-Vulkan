"""
Ultra-optimized classification tools for massive point clouds (100M+ points)
Key optimizations:
- Batch processing for multiple operations
- Minimal memory allocation
- Direct color buffer updates (no rebuilds)
- Chunked processing for huge datasets
- Async/threaded color updates
"""

import numpy as np
from concurrent.futures import ThreadPoolExecutor
import time


class UltraFastClassifier:
    """
    High-performance classifier with batch operations and minimal overhead.
    """
    
    def __init__(self, app, chunk_size=1_000_000):
        self.app = app
        self.chunk_size = chunk_size
        self.executor = ThreadPoolExecutor(max_workers=4)
        
    def classify_region(self, region_mask, new_class, skip_undo=False):
        """
        Fastest possible classification with optional undo skip for batch ops.
        """
        if not np.any(region_mask):
            return 0
        
        t0 = time.perf_counter()
        
        # Get changed indices efficiently
        changed_indices = np.flatnonzero(region_mask)
        n_changed = len(changed_indices)
        
        if n_changed == 0:
            return 0
        
        # Keep old classes once. Used by undo AND Surface rebuild decision.
        old_classes_for_surface = self.app.data["classification"][changed_indices].copy()

        # Store undo data (only if not in batch mode)
        if not skip_undo:
            self._add_to_undo(changed_indices, old_classes_for_surface, new_class)
        
        # Apply classification change (in-place)
        self.app.data["classification"][region_mask] = new_class
        
        # Update display WITHOUT geometry rebuild.
        # In Surface mode, refresh the Surface mesh only when Surface membership changes.
        if str(getattr(self.app, "display_mode", "class") or "class").lower() == "surface":
            try:
                from gui.surface_mode import (
                    refresh_surface_after_classification,
                    surface_class_change_needs_rebuild,
                )
                if surface_class_change_needs_rebuild(self.app, old_classes_for_surface, new_class):
                    self.app._surface_refresh_already_scheduled = True
                    refresh_surface_after_classification(
                        self.app,
                        region_mask,
                        operation="classification",
                        delay_ms=int(getattr(self.app, "surface_classification_rebuild_delay_ms", 300) or 300),
                    )
                else:
                    self.app._surface_skip_next_classification_finished_refresh = True
                    self.app._surface_needs_rebuild_after_classification = False
                    print("Surface classification changed - skipped main Surface rebuild")
            except Exception:
                pass
        else:
            from gui.unified_actor_manager import fast_classify_update
            border_percent = float(getattr(self.app, "point_border_percent", 0.0))
            fast_classify_update(self.app, region_mask, new_class, border_percent=border_percent)
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"⚡ {n_changed:,} pts → class {new_class} in {elapsed:.1f}ms")
        return n_changed
    
    def _add_to_undo(self, indices, old_classes, new_class):
        """
        Store undo data in the standard format expected by app_window.py.
        """
        if not hasattr(self.app, 'undo_stack'):
            self.app.undo_stack = []
            
        final_mask = np.zeros(len(self.app.data["xyz"]), dtype=bool)
        final_mask[indices] = True
        
        new_classes = np.full(old_classes.shape, new_class, dtype=old_classes.dtype)

        undo_data = {
            "mask": final_mask,
            "old_classes": old_classes,
            "new_classes": new_classes
        }
        
        self.app.undo_stack.append(undo_data)
        # Clear stale redo entries after new action
        if hasattr(self.app, 'redo_stack'):
            self.app.redo_stack.clear()

        # Limit stack size (keep last 30 operations to save memory)
        if len(self.app.undo_stack) > 30:
            from gui.memory_manager import _free_undo_entry
            _free_undo_entry(self.app.undo_stack.pop(0))
    
    def classify_brush_spatial(self, center_3d, radius, new_class):
        """
        Ultra-fast brush using spatial index (KD-tree or octree).
        """
        t0 = time.perf_counter()
        
        # Try spatial index first (O(log n))
        if hasattr(self.app, 'spatial_index') and self.app.spatial_index:
            try:
                indices = self.app.spatial_index.query_ball_point(center_3d, radius)
                
                if len(indices) == 0:
                    return 0
                
                # Create mask efficiently
                mask = np.zeros(len(self.app.data["xyz"]), dtype=bool)
                mask[indices] = True
                
                n = self.classify_region(mask, new_class)
                
                elapsed = (time.perf_counter() - t0) * 1000
                print(f"🎯 Brush (spatial): {n:,} pts in {elapsed:.1f}ms")
                return n
                
            except Exception as e:
                print(f"Spatial index failed: {e}")
        
        # Fallback: vectorized distance (still fast)
        return self._classify_brush_fallback(center_3d, radius, new_class, t0)
    
    def _classify_brush_fallback(self, center_3d, radius, new_class, t0):
        """
        Vectorized fallback for brush (no spatial index).
        """
        xyz = self.app.data["xyz"]
        
        # Chunked processing for huge datasets
        if len(xyz) > 10_000_000:
            mask = self._chunked_distance_check(xyz, center_3d, radius)
        else:
            # Single vectorized operation for smaller datasets
            dist_sq = np.sum((xyz - center_3d) ** 2, axis=1)
            mask = dist_sq <= radius ** 2
        
        n = self.classify_region(mask, new_class)
        
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"🎯 Brush (vectorized): {n:,} pts in {elapsed:.1f}ms")
        return n
    
    def _chunked_distance_check(self, xyz, center, radius):
        """
        Process distance check in chunks to avoid memory spikes.
        """
        n = len(xyz)
        mask = np.zeros(n, dtype=bool)
        radius_sq = radius ** 2
        
        for i in range(0, n, self.chunk_size):
            end = min(i + self.chunk_size, n)
            chunk = xyz[i:end]
            dist_sq = np.sum((chunk - center) ** 2, axis=1)
            mask[i:end] = dist_sq <= radius_sq
        
        return mask
    
    def classify_rectangle_spatial(self, x_min, x_max, y_min, y_max, new_class, z_min=None, z_max=None):
        """
        Ultra-fast rectangular selection with optional Z bounds.
        """
        t0 = time.perf_counter()
        xyz = self.app.data["xyz"]
        
        # Vectorized bounds check (fastest for rectangles)
        mask = ((xyz[:, 0] >= x_min) & (xyz[:, 0] <= x_max) &
                (xyz[:, 1] >= y_min) & (xyz[:, 1] <= y_max))
        
        # Optional Z filtering
        if z_min is not None and z_max is not None:
            mask &= (xyz[:, 2] >= z_min) & (xyz[:, 2] <= z_max)
        
        n = self.classify_region(mask, new_class)
        
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"📦 Rectangle: {n:,} pts in {elapsed:.1f}ms")
        return n
    
    def classify_polygon_fast(self, polygon_points, new_class):
        """
        Fast polygon classification using ray casting.
        """
        t0 = time.perf_counter()
        xyz = self.app.data["xyz"]
        
        # Use optimized point-in-polygon test
        mask = self._points_in_polygon(xyz[:, :2], polygon_points)
        
        n = self.classify_region(mask, new_class)
        
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"🔷 Polygon: {n:,} pts in {elapsed:.1f}ms")
        return n
    
    def _points_in_polygon(self, points, polygon):
        """
        Vectorized ray-casting algorithm for point-in-polygon test.
        """
        n = len(points)
        mask = np.zeros(n, dtype=bool)
        
        px, py = points[:, 0], points[:, 1]
        
        # Ray casting from each point
        for i in range(len(polygon)):
            j = (i + 1) % len(polygon)
            xi, yi = polygon[i]
            xj, yj = polygon[j]
            
            # Check if ray crosses edge
            intersect = ((yi > py) != (yj > py)) & \
                        (px < (xj - xi) * (py - yi) / (yj - yi + 1e-10) + xi)
            
            mask ^= intersect  # Toggle for each crossing
        return mask
    
    def batch_classify(self, operations):
        """
        Batch multiple classification operations for efficiency.
        Operations format: [(mask, class), (mask, class), ...]
        """
        t0 = time.perf_counter()
        total_changed = 0
        
        # Combine all undo data first
        all_indices = []
        all_old_classes = []
        
        for mask, new_class in operations:
            if not np.any(mask):
                continue
            
            indices = np.flatnonzero(mask)
            old_classes = self.app.data["classification"][indices].copy()
            
            all_indices.extend(indices)
            all_old_classes.extend(old_classes)
            
            # Apply change
            self.app.data["classification"][mask] = new_class
            total_changed += len(indices)
        
        # Single undo entry for entire batch
        if all_indices:
            self._add_to_undo(np.array(all_indices), np.array(all_old_classes), -1)
        
        # Single color update for all changes
        if total_changed > 0:
            self._trigger_minimal_render()
        
        elapsed = (time.perf_counter() - t0) * 1000
        print(f"📦 Batch: {total_changed:,} pts in {elapsed:.1f}ms ({len(operations)} ops)")
        
        return total_changed

# Convenience functions for backward compatibility
def classify_region_ultra_fast(app, region_mask, new_class):
    """Legacy wrapper for existing code."""
    if not hasattr(app, '_classifier'):
        app._classifier = UltraFastClassifier(app)
    return app._classifier.classify_region(region_mask, new_class)

def classify_brush_ultra_fast(app, center_3d, radius, new_class):
    """Legacy wrapper for existing code."""
    if not hasattr(app, '_classifier'):
        app._classifier = UltraFastClassifier(app)
    return app._classifier.classify_brush_spatial(center_3d, radius, new_class)

def classify_rectangle_ultra_fast(app, x_min, x_max, y_min, y_max, new_class):
    """Legacy wrapper for existing code."""
    if not hasattr(app, '_classifier'):
        app._classifier = UltraFastClassifier(app)
    return app._classifier.classify_rectangle_spatial(x_min, x_max, y_min, y_max, new_class)